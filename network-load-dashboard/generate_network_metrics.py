#!/usr/bin/env python3
"""
Nym Network Metrics - static HTML generator.
Fetches gateway API + bonded nodes + families.
Writes index.html + country/XX.html pages.
Cron every 5 min:
    */5 * * * * /usr/bin/python3 /opt/nym-metrics/generate_network_metrics.py >> /var/log/nym-metrics.log 2>&1
"""

import json
import sys
import sqlite3
import datetime
import urllib.request
from pathlib import Path
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
GATEWAYS_API   = "https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways"
SUMMARY_API    = "https://mainnet-node-status-api.nymtech.cc/v2/summary"
FAMILIES_API   = "https://validator.nymtech.net/api/v1/node-families?size=100&page={page}"
DESCRIBED_API  = "https://validator.nymtech.net/api/v1/nym-nodes/described?size=100&page={page}"
OUTPUT_INDEX   = Path("/var/www/html/network-load/index.html")
OUTPUT_COUNTRY     = Path("/var/www/html/network-load/country")
OUTPUT_RESIDENTIAL = Path("/var/www/html/network-load/residential.html")
DB_PATH        = Path("/var/lib/nym-metrics/history.db")
TIMEOUT_SEC    = 30

HARBOURMASTER = "https://harbourmaster.nymtech.net/gateway/{k}"
SPECTREDAO    = "https://explorer.nym.spectredao.net/nodes/{k}"

LOAD_SCORE_MAP = {"low": 0.0, "medium": 0.5, "high": 1.0, "offline": 1.0}


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nym-metrics/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode())


def fetch_gateways():
    return fetch_json(GATEWAYS_API)


def fetch_total_nodes():
    data = fetch_json(SUMMARY_API)
    return data["total_nodes"]


def fetch_family_stats():
    all_fam = []
    page = 0
    while True:
        data = fetch_json(FAMILIES_API.format(page=page))
        all_fam.extend(data["data"])
        if len(all_fam) >= data["pagination"]["total"]:
            break
        page += 1

    active = [f for f in all_fam if len(f.get("members", [])) > 0]
    node_ids = set()
    for f in active:
        for m in f.get("members", []):
            node_ids.add(m["node_id"])

    # get role breakdown via described endpoint
    all_desc = []
    page = 0
    while True:
        data = fetch_json(DESCRIBED_API.format(page=page))
        all_desc.extend(data["data"])
        if len(all_desc) >= data["pagination"]["total"]:
            break
        page += 1

    role_map = {}
    for n in all_desc:
        nid  = n["node_id"]
        desc = n.get("description") or {}
        role = desc.get("declared_role") or {}
        role_map[nid] = {
            "mixnode": role.get("mixnode", False),
            "gateway": (role.get("entry", False) or
                        role.get("exit_ipr", False) or
                        role.get("exit_nr", False)),
        }

    mix_in_fam = sum(1 for nid in node_ids if role_map.get(nid, {}).get("mixnode"))
    gw_in_fam  = sum(1 for nid in node_ids if role_map.get(nid, {}).get("gateway"))

    return {
        "active_families":   len(active),
        "nodes_in_families": len(node_ids),
        "mix_in_families":   mix_in_fam,
        "gw_in_families":    gw_in_fam,
    }


# ── Scoring ───────────────────────────────────────────────────────────────────
def compute_node_perf(node):
    p = node.get("performance")
    if p is None:
        return None, False
    try:
        return float(p), True
    except (ValueError, TypeError):
        return None, False


def perf_tier(score):
    if score > 0.75: return "high"
    if score > 0.50: return "medium"
    if score > 0.10: return "low"
    return "offline"


def load_tier_from_score(score):
    if score < 0.25: return "low"
    if score < 0.75: return "medium"
    return "high"


def mean(lst):
    return sum(lst) / len(lst) if lst else None


# ── Aggregate gateways ────────────────────────────────────────────────────────
def aggregate(gateways):
    total       = len(gateways)
    perf_scores = []
    load_scores = []
    no_probe    = 0
    perf_tiers  = defaultdict(int)
    load_tiers  = defaultdict(int)

    countries = defaultdict(lambda: {
        "perf_scores": [], "load_scores": [],
        "perf_tiers":  defaultdict(int),
        "load_tiers":  defaultdict(int),
        "node_count":  0,
        "nodes":       [],
    })

    residential_nodes = []

    for node in gateways:
        pv2  = node.get("performance_v2") or {}
        loc  = node.get("location") or {}
        cc   = (loc.get("two_letter_iso_country_code") or "??").upper()
        ikey = node.get("identity_key") or ""
        name = node.get("name") or (ikey[:16] + "...")

        load_str = pv2.get("load") or ""
        load_num = None
        if load_str in LOAD_SCORE_MAP:
            load_num = LOAD_SCORE_MAP[load_str]
            load_scores.append(load_num)
            load_tiers[load_str] += 1
            countries[cc]["load_scores"].append(load_num)
            countries[cc]["load_tiers"][load_str] += 1

        ps, has_probe = compute_node_perf(node)
        if not has_probe:
            no_probe += 1
        else:
            perf_scores.append(ps)
            t = perf_tier(ps)
            perf_tiers[t] += 1
            countries[cc]["perf_scores"].append(ps)
            countries[cc]["perf_tiers"][t] += 1

        countries[cc]["node_count"] += 1
        node_detail = {
            "identity_key": ikey,
            "name":         name,
            "city":         loc.get("city") or "",
            "perf_score":   ps,
            "has_probe":    has_probe,
            "perf_tier":    perf_tier(ps) if has_probe else "unknown",
            "load_str":     load_str,
            "uptime":       pv2.get("uptime_percentage_last_24_hours"),
        }
        countries[cc]["nodes"].append(node_detail)
        if ((loc.get("asn") or {}).get("kind")) == "residential":
            residential_nodes.append(dict(node_detail, cc=cc,
                                          asn_name=((loc.get("asn") or {}).get("name") or "")))

    country_data = []
    for cc, d in countries.items():
        cp = mean(d["perf_scores"])
        cl = mean(d["load_scores"])
        country_data.append({
            "cc":         cc,
            "node_count": d["node_count"],
            "low_sample": d["node_count"] < 3,
            "mean_perf":  cp,
            "mean_load":  cl,
            "perf_tier":  perf_tier(cp) if cp is not None else "unknown",
            "load_tier":  load_tier_from_score(cl) if cl is not None else "unknown",
            "perf_tiers": dict(d["perf_tiers"]),
            "load_tiers": dict(d["load_tiers"]),
            "nodes":      sorted(d["nodes"],
                                 key=lambda n: (LOAD_SCORE_MAP.get(n["load_str"], 0),
                                                -(n["perf_score"] or 0)),
                                 reverse=True),
        })

    perf_alerts = sorted(
        [c for c in country_data if c["mean_perf"] is not None and c["mean_perf"] < 0.50],
        key=lambda c: c["mean_perf"]
    )
    load_alerts = sorted(
        [c for c in country_data if c["mean_load"] is not None and c["mean_load"] >= 0.25],
        key=lambda c: -c["mean_load"]
    )
    return {
        "total":          total,
        "no_probe":       no_probe,
        "probe_count":    total - no_probe,
        "location_count": len(country_data),
        # gateways offering at least one QUIC bridge transport (bridges.transports[].transport_type)
        "quic_bridges":   sum(1 for g in gateways if any(((t.get("transport_type") or "").startswith("quic")) for t in ((g.get("bridges") or {}).get("transports") or []))),
        "mean_perf":      mean(perf_scores),
        "mean_load":      mean(load_scores),
        "perf_tiers":     dict(perf_tiers),
        "load_tiers":     dict(load_tiers),
        "perf_scores":    perf_scores,
        "countries":      sorted(country_data, key=lambda c: -(c["mean_perf"] or 0)),
        "perf_alerts":    perf_alerts,
        "load_alerts":    load_alerts,
        "residential":    len(residential_nodes),
        "residential_locations": len(set(n["cc"] for n in residential_nodes)),
        # same method as network load: mean of per-node load scores (low=0, medium=0.5, high/offline=1)
        "residential_load": mean([LOAD_SCORE_MAP[n["load_str"]] for n in residential_nodes
                                  if n["load_str"] in LOAD_SCORE_MAP]),
        "residential_nodes": sorted(residential_nodes,
                                      key=lambda n: (LOAD_SCORE_MAP.get(n["load_str"], 0),
                                                     -(n["perf_score"] or 0)),
                                      reverse=True),
    }


# ── History ───────────────────────────────────────────────────────────────────
def load_global_history():
    if not DB_PATH.exists():
        return None
    optional = ["total_nodes", "active_families", "nodes_in_families",
                "residential", "residential_locations"]
    with sqlite3.connect(DB_PATH) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(global_snapshots)").fetchall()}
        present = [c for c in optional if c in cols]
        rows = conn.execute(
            "SELECT ts, node_count, loc_count, mean_perf, mean_load"
            + "".join(", " + c for c in present)
            + " FROM global_snapshots ORDER BY ts ASC"
        ).fetchall()
    if not rows:
        return None

    result = {
        "labels":    [r[0][:16].replace("T", " ") for r in rows],
        "gateways":  [r[1] for r in rows],
        "locations": [r[2] for r in rows],
        "perf":      [round(r[3] * 100, 1) if r[3] is not None else None for r in rows],
        "load":      [round(r[4] * 100, 1) if r[4] is not None else None for r in rows],
    }
    for i, c in enumerate(present):
        result[c] = [r[5 + i] for r in rows]
    return result


def load_country_history(cc):
    if not DB_PATH.exists():
        return None
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ts, node_count, mean_perf, mean_load "
            "FROM country_snapshots WHERE cc=? ORDER BY ts ASC",
            (cc,)
        ).fetchall()
    if not rows:
        return None
    return {
        "labels": [r[0][:16].replace("T", " ") for r in rows],
        "nodes":  [r[1] for r in rows],
        "perf":   [round(r[2] * 100, 1) if r[2] is not None else None for r in rows],
        "load":   [round(r[3] * 100, 1) if r[3] is not None else None for r in rows],
    }


# ── HTML helpers ──────────────────────────────────────────────────────────────
def pct(v):
    if v is None: return "&#8212;"
    return str(round(v * 100, 1)) + "%"


def flag(cc):
    cc = cc.upper()
    if len(cc) != 2 or cc == "??":
        return "&#127760;"
    return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)


def gauge_color(v, invert=False):
    if invert:
        if v < 0.25: return "#00d26a"
        if v < 0.75: return "#f5a623"
        return "#e05b2b"
    else:
        if v > 0.75: return "#00d26a"
        if v > 0.50: return "#f5a623"
        return "#e05b2b"


def perf_badge(tier):
    c = {
        "high":    "background:#0a2a1a;color:#00d26a;border:1px solid #00d26a44",
        "medium":  "background:#2a1e00;color:#f5a623;border:1px solid #f5a62344",
        "low":     "background:#2a1200;color:#e05b2b;border:1px solid #e05b2b44",
        "offline": "background:#1e1e1e;color:#666;border:1px solid #44444444",
        "unknown": "background:#1a1a1a;color:#555;border:1px solid #33333344",
    }.get(tier, "background:#1a1a1a;color:#555")
    return '<span class="badge" style="' + c + '">' + tier + '</span>'


def load_badge(load_str):
    c = {
        "low":     "background:#0a2a1a;color:#00d26a;border:1px solid #00d26a44",
        "medium":  "background:#2a1e00;color:#f5a623;border:1px solid #f5a62344",
        "high":    "background:#2a1200;color:#e05b2b;border:1px solid #e05b2b44",
        "offline": "background:#1e1e1e;color:#666;border:1px solid #44444444",
        "unknown": "background:#1a1a1a;color:#555;border:1px solid #33333344",
    }.get(load_str, "background:#1a1a1a;color:#555")
    return '<span class="badge" style="' + c + '">' + (load_str or "?") + '</span>'


def build_histogram(perf_scores):
    buckets = [0] * 10
    for ps in perf_scores:
        idx = min(int(ps * 10), 9)
        buckets[idx] += 1
    bmax = max(buckets) or 1
    bars = ""
    for i, b in enumerate(buckets):
        h = max(4, int(b / bmax * 80))
        bars += ('<div class="histo-bar" title="' + str(i*10) + '-' + str(i*10+10) +
                 '%: ' + str(b) + ' nodes" style="height:' + str(h) + 'px"></div>')
    return bars


def _ds(label, data, color, axis, dash=None):
    return (
        "{label:'" + label + "',data:" + json.dumps(data) + ","
        "borderColor:'" + color + "',backgroundColor:'" + color + "22',"
        "yAxisID:'" + axis + "',tension:0.3,borderWidth:1.5,"
        "pointRadius:0,pointHoverRadius:3,spanGaps:false,fill:false"
        + (",borderDash:" + json.dumps(dash) if dash else "") +
        "}"
    )


def history_chart(hist, chart_id, show_locations=True):
    if hist is None:
        return '<div class="chart-empty">No historical data yet. Accumulates after first hourly snapshot.</div>'

    ds = [
        _ds("Performance %", hist["perf"], "#00d26a", "yPct"),
        _ds("Load %",        hist["load"], "#e05b2b", "yPct"),
        _ds("Gateways",      hist.get("gateways") or hist.get("nodes", []), "#f5a623", "yCount"),
    ]
    if show_locations:
        ds += [
            _ds("Locations",             hist.get("locations", []),             "#a855f7", "yCount"),
            _ds("Total Nodes",           hist.get("total_nodes", []),           "#0ea5e9", "yCount"),
            _ds("Active Families",       hist.get("active_families", []),       "#f43f5e", "yCount"),
            _ds("Nodes in Families",     hist.get("nodes_in_families", []),     "#84cc16", "yCount"),
            _ds("Residential IPs",       hist.get("residential", []),           "#94a3b8", "yCount"),
            _ds("Residential Locations", hist.get("residential_locations", []), "#14b8a6", "yCount", [5, 3]),
        ]

    cid = chart_id
    buttons = (
        '<div class="zoom-bar">'
        '<button class="zbtn" data-c="' + cid + '" data-h="24">24h</button>'
        '<button class="zbtn" data-c="' + cid + '" data-h="168">7d</button>'
        '<button class="zbtn zon" data-c="' + cid + '" data-h="0">30d</button>'
        '<span class="zhint">drag to zoom &#183; shift+drag to pan &#183; ctrl+wheel to zoom</span>'
        '</div>'
    )

    return (
        buttons
        + '<canvas id="' + cid + '" height="140"></canvas>'
        '<script>(function(){'
        'if(window.ChartZoom){try{Chart.register(window.ChartZoom);}catch(e){}}'
        'var ctx=document.getElementById("' + cid + '");'
        'var dk=document.documentElement.getAttribute("data-theme")!=="light";'
        'var gc=dk?"#2e343244":"#d4dbd844";'
        'var lc=dk?"#8a9693":"#5a6662";'
        'var labels=' + json.dumps(hist["labels"]) + ';'
        'var ch=new Chart(ctx,{'
        'type:"line",'
        'data:{labels:labels,datasets:[' + ",".join(ds) + ']},'
        'options:{'
        'responsive:true,'
        'interaction:{mode:"index",intersect:false},'
        'plugins:{'
        'legend:{labels:{color:lc,boxWidth:12,font:{size:11}}},'
        'tooltip:{backgroundColor:dk?"#1c201f":"#fff",borderColor:dk?"#2e3432":"#d4dbd8",borderWidth:1,titleColor:dk?"#e8edeb":"#111413",bodyColor:lc},'
        'zoom:{'
        'limits:{x:{minRange:3}},'
        'pan:{enabled:true,mode:"x",modifierKey:"shift"},'
        'zoom:{mode:"x",'
        'wheel:{enabled:true,modifierKey:"ctrl"},'
        'pinch:{enabled:true},'
        'drag:{enabled:true,backgroundColor:"rgba(0,210,106,0.12)",borderColor:"#00d26a",borderWidth:1}},'
        'onZoomComplete:function(){setActive(null);}'
        '}'
        '},'
        'scales:{'
        'x:{ticks:{color:lc,maxTicksLimit:10,font:{size:10},maxRotation:30},grid:{color:gc}},'
        'yPct:{type:"linear",position:"left",min:0,max:100,ticks:{color:lc,font:{size:10},callback:function(v){return v+"%"}},grid:{color:gc},title:{display:true,text:"%",color:lc,font:{size:10}}},'
        'yCount:{type:"linear",position:"right",min:0,ticks:{color:lc,font:{size:10}},grid:{drawOnChartArea:false},title:{display:true,text:"count",color:lc,font:{size:10}}}'
        '}'
        '}'
        '});'
        'var btns=document.querySelectorAll(\'.zbtn[data-c="' + cid + '"]\');'
        'function setActive(b){btns.forEach(function(x){x.classList.toggle("zon",x===b);});}'
        'function lastTs(){return new Date(labels[labels.length-1].replace(" ","T")+":00Z").getTime();}'
        'btns.forEach(function(b){b.addEventListener("click",function(){'
        'var h=parseInt(b.dataset.h,10);'
        'if(!h||!ch.zoomScale){if(ch.resetZoom)ch.resetZoom();setActive(b);return;}'
        'var cut=lastTs()-h*3600000,i=0;'
        'while(i<labels.length-1&&new Date(labels[i].replace(" ","T")+":00Z").getTime()<cut)i++;'
        'ch.zoomScale("x",{min:i,max:labels.length-1},"default");setActive(b);'
        '});});'
        'ctx.addEventListener("dblclick",function(){if(ch.resetZoom)ch.resetZoom();setActive(btns[btns.length-1]);});'
        '})();</script>'
    )


# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
:root{--bg:#111413;--bg2:#1c201f;--bg3:#252a29;--border:#2e3432;--text:#e8edeb;--muted:#8a9693;--accent:#00d26a;--sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono','Fira Mono',monospace;--r:6px}
[data-theme=light]{--bg:#f5f7f6;--bg2:#fff;--bg3:#eef1f0;--border:#d4dbd8;--text:#111413;--muted:#5a6662;--accent:#009950}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;line-height:1.6;min-height:100vh}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 32px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.header-left{display:flex;align-items:center;gap:12px}
.logo{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--accent)}
.header-title{font-size:13px;color:var(--muted);border-left:1px solid var(--border);padding-left:12px}
.header-right{display:flex;align-items:center;gap:16px}
.updated{font-size:12px;color:var(--muted);font-family:var(--mono)}
.theme-btn{background:var(--bg3);border:1px solid var(--border);color:var(--text);cursor:pointer;padding:5px 10px;border-radius:var(--r);font-size:13px}
.theme-btn:hover{background:var(--border)}
.summary{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 32px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.s-stat{display:flex;align-items:baseline;gap:8px}
.s-val{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--accent)}
.s-lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.s-div{width:1px;height:24px;background:var(--border);flex-shrink:0}
.main{max-width:1200px;margin:0 auto;padding:32px 24px;display:flex;flex-direction:column;gap:32px}
.section{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.sec-hdr{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sec-hdr h2{font-size:14px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:var(--muted)}
.sec-body{padding:24px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}
.pair.top{align-items:start}
@media(max-width:800px){.pair{grid-template-columns:1fr}.header,.summary{padding:12px 16px}.main{padding:16px}}
.big{display:flex;align-items:flex-end;gap:16px;margin-bottom:20px}
.bignum{font-family:var(--mono);font-size:52px;font-weight:700;line-height:1}
.meta{padding-bottom:6px;color:var(--muted);font-size:12px;line-height:1.8}
.meta strong{display:block;font-size:13px;color:var(--text)}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.pill{font-family:var(--mono);font-size:11px;padding:3px 9px;border-radius:20px;border:1px solid transparent}
.p-ph{background:#0a2a1a;color:#00d26a;border-color:#00d26a44}
.p-pm{background:#2a1e00;color:#f5a623;border-color:#f5a62344}
.p-pl{background:#2a1200;color:#e05b2b;border-color:#e05b2b44}
.p-po{background:#1e1e1e;color:#666;border-color:#44444444}
.p-pn{background:#1a1a1a;color:#555;border-color:#33333344}
.p-ll{background:#0a2a1a;color:#00d26a;border-color:#00d26a44}
.p-lm{background:#2a1e00;color:#f5a623;border-color:#f5a62344}
.p-lh{background:#2a1200;color:#e05b2b;border-color:#e05b2b44}
.p-lo{background:#1e1e1e;color:#666;border-color:#44444444}
[data-theme=light] .p-ph{background:#e6faf0;color:#009950;border-color:#009950}
[data-theme=light] .p-pm{background:#fff8e6;color:#c47d00;border-color:#f5a623}
[data-theme=light] .p-pl{background:#fef0e8;color:#c04010;border-color:#e05b2b}
[data-theme=light] .p-ll{background:#e6faf0;color:#009950;border-color:#009950}
[data-theme=light] .p-lm{background:#fff8e6;color:#c47d00;border-color:#f5a623}
[data-theme=light] .p-lh{background:#fef0e8;color:#c04010;border-color:#e05b2b}
.histo{display:flex;align-items:flex-end;gap:3px;height:88px;margin-top:4px}
.histo-bar{flex:1;background:var(--accent);opacity:.65;border-radius:2px 2px 0 0;min-height:4px;transition:opacity .15s;cursor:default}
.histo-bar:hover{opacity:1}
.histo-lbl{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);font-family:var(--mono);margin-top:4px}
.lbar-wrap{margin-top:8px}
.lbar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--bg3)}
.lbar-leg{display:flex;justify-content:space-between;font-size:10px;font-family:var(--mono);margin-top:6px}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);padding:8px 12px;border-bottom:1px solid var(--border);font-weight:500}
td{padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg3)}
td a{color:inherit}td a:hover{color:var(--accent)}
.badge{display:inline-block;font-size:10px;font-family:var(--mono);padding:1px 6px;border-radius:3px;font-weight:600}
.ls{font-size:10px;color:var(--muted);background:var(--bg3);border:1px solid var(--border);padding:1px 5px;border-radius:3px;margin-left:4px;font-family:var(--mono)}
.tpills{display:flex;gap:4px;flex-wrap:wrap}
.tp{font-size:10px;font-family:var(--mono);padding:1px 5px;border-radius:3px}
.alert-badge{font-size:11px;font-family:var(--mono);padding:2px 8px;border-radius:3px;font-weight:600}
.ac{background:#2a0a00;color:#ff5533;border:1px solid #ff553344}
.ad{background:#2a1e00;color:#f5a623;border:1px solid #f5a62344}
[data-theme=light] .ac{background:#fff0ec;color:#cc2200;border-color:#e05b2b}
[data-theme=light] .ad{background:#fff8e6;color:#c47d00;border-color:#f5a623}
.no-alerts{color:var(--accent);font-size:13px;padding:20px 12px;text-align:center}
.chart-wrap{padding:20px 24px}
.chart-empty{padding:24px;text-align:center;color:var(--muted);font-size:13px;font-family:var(--mono);border:1px dashed var(--border);border-radius:var(--r);margin:20px 24px}
.nlink{font-size:10px;font-family:var(--mono);padding:2px 7px;border-radius:3px;background:var(--bg3);border:1px solid var(--border);color:var(--accent)}
.nlink:hover{background:var(--border)}
.ikey{font-family:var(--mono);font-size:11px;color:var(--muted)}
.zoom-bar{display:flex;align-items:center;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.zbtn{background:var(--bg3);border:1px solid var(--border);color:var(--muted);font-family:var(--mono);font-size:11px;padding:3px 10px;border-radius:var(--r);cursor:pointer}
.zbtn:hover{color:var(--text)}
.zbtn.zon{color:var(--accent);border-color:var(--accent)}
.zhint{font-size:10px;color:var(--muted);font-family:var(--mono);margin-left:8px}
table.sortable th[data-t]{cursor:pointer;user-select:none;white-space:nowrap}
table.sortable th[data-t]:hover{color:var(--text)}
table.sortable th.sasc::after{content:" \\25B2";color:var(--accent)}
table.sortable th.sdesc::after{content:" \\25BC";color:var(--accent)}
.footer{text-align:center;color:var(--muted);font-size:11px;font-family:var(--mono);padding:24px;border-top:1px solid var(--border)}
"""

THEME_JS = (
    'function toggleTheme(){'
    'var h=document.documentElement;'
    'var n=h.getAttribute("data-theme")==="light"?"dark":"light";'
    'h.setAttribute("data-theme",n);'
    'document.querySelector(".theme-btn").textContent=n==="light"?"🌙 Dark":"☀ Light";'
    'localStorage.setItem("nmt",n);}'
    '(function(){'
    'var s=localStorage.getItem("nmt");'
    'if(s==="light"){'
    'document.documentElement.setAttribute("data-theme","light");'
    'document.addEventListener("DOMContentLoaded",function(){'
    'var b=document.querySelector(".theme-btn");'
    'if(b)b.textContent="🌙 Dark";});'
    '}})();'
)

CHARTJS = (
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/hammer.js/2.0.8/hammer.min.js"></script>'
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.0.1/chartjs-plugin-zoom.min.js"></script>'
)


def page_open(title):
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + title + '</title>'
        + CHARTJS +
        '<style>' + CSS + '</style>'
        '</head><body>'
    )


def page_close():
    return '<script>' + THEME_JS + SORT_JS + '</script></body></html>'


def render_header(title, generated, back=None):
    back_html = ''
    if back:
        back_html = '<a href="' + back + '" style="font-size:13px;color:var(--muted)">&#8592; Network Metrics</a>'
    return (
        '<header class="header">'
        '<div class="header-left">'
        '<span class="logo">NYM</span>'
        '<span class="header-title">' + title + '</span>'
        + back_html +
        '</div>'
        '<div class="header-right">'
        '<span class="updated">Updated: ' + generated + '</span>'
        '<button class="theme-btn" onclick="toggleTheme()">&#9728; Light</button>'
        '</div>'
        '</header>'
    )


def render_summary(stats):
    parts = []
    for val, lbl in stats:
        parts.append(
            '<div class="s-stat">'
            '<span class="s-val">' + str(val) + '</span>'
            '<span class="s-lbl">' + lbl + '</span>'
            '</div>'
        )
    return '<div class="summary">' + '<div class="s-div"></div>'.join(parts) + '</div>'


def locations_rows(countries):
    """One row per country: performance + load side by side, sortable client-side."""
    rows = ""
    for c in sorted(countries, key=lambda x: -(x["mean_perf"] or 0)):
        ls   = ('<span class="ls">low sample</span>' if c["low_sample"] else "")
        link = '<a href="country/' + c["cc"] + '.html">' + flag(c["cc"]) + ' <strong>' + c["cc"] + '</strong></a>' + ls
        pv   = c["mean_perf"] if c["mean_perf"] is not None else -1
        lv   = c["mean_load"] if c["mean_load"] is not None else -1
        rows += (
            '<tr>'
            '<td data-v="' + c["cc"] + '">' + link + '</td>'
            '<td data-v="' + str(c["node_count"]) + '">' + str(c["node_count"]) + '</td>'
            '<td data-v="' + str(pv) + '">' + pct(c["mean_perf"]) + ' ' + perf_badge(c["perf_tier"]) + '</td>'
            '<td data-v="' + str(lv) + '">' + pct(c["mean_load"]) + ' ' + load_badge(c["load_tier"]) + '</td>'
            '</tr>'
        )
    return rows


SORT_JS = (
    'document.querySelectorAll("table.sortable").forEach(function(t){'
    'var ths=t.querySelectorAll("th[data-t]");'
    'ths.forEach(function(th,ci){th.addEventListener("click",function(){'
    'var idx=Array.prototype.indexOf.call(th.parentNode.children,th);'
    'var asc=th.classList.contains("sdesc");'
    'ths.forEach(function(x){x.classList.remove("sasc","sdesc");});'
    'th.classList.add(asc?"sasc":"sdesc");'
    'var tb=t.tBodies[0],rs=Array.prototype.slice.call(tb.rows);'
    'var num=th.dataset.t==="n";'
    'rs.sort(function(a,b){'
    'var x=a.cells[idx].dataset.v,y=b.cells[idx].dataset.v;'
    'var r=num?(parseFloat(x)-parseFloat(y)):x.localeCompare(y);'
    'return asc?r:-r;});'
    'rs.forEach(function(r){tb.appendChild(r);});'
    '});});});'
)


def alert_rows(alerts, kind, from_country=False):
    if not alerts:
        return '<tr><td colspan="4" class="no-alerts">&#10003; All locations within normal range</td></tr>'
    prefix = "../" if from_country else ""
    rows = ""
    for c in alerts:
        val = c["mean_perf"] if kind == "perf" else c["mean_load"]
        if kind == "perf":
            sev = "Critical" if (val is not None and val <= 0.10) else "Degraded"
            cls = "ac" if sev == "Critical" else "ad"
        else:
            sev = "Overloaded" if (val is not None and val >= 0.75) else "Elevated"
            cls = "ac" if sev == "Overloaded" else "ad"
        ls = ('<span class="ls">low sample</span>' if c["low_sample"] else "")
        rows += (
            '<tr>'
            '<td><a href="' + prefix + 'country/' + c["cc"] + '.html">' + flag(c["cc"]) + ' <strong>' + c["cc"] + '</strong></a>' + ls + '</td>'
            '<td>' + str(c["node_count"]) + '</td>'
            '<td>' + pct(val) + '</td>'
            '<td><span class="alert-badge ' + cls + '">' + sev + '</span></td>'
            '</tr>'
        )
    return rows


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


# ── Index page ────────────────────────────────────────────────────────────────
def render_index(data, fam, total_nodes, hist, generated):
    mp = data["mean_perf"] or 0
    ml = data["mean_load"] or 0
    pt = data["perf_tiers"]
    lt = data["load_tiers"]
    lt_total = max(sum(lt.values()), 1)

    pc = gauge_color(mp, invert=False)
    lc = gauge_color(ml, invert=True)

    lw = str(round(lt.get("low",    0) / lt_total * 100, 1))
    mw = str(round(lt.get("medium", 0) / lt_total * 100, 1))
    hw = str(round(lt.get("high",   0) / lt_total * 100, 1))

    histo = build_histogram(data["perf_scores"])
    chart = history_chart(hist, "globalChart", show_locations=True)
    locs  = locations_rows(data["countries"])
    pa    = alert_rows(data["perf_alerts"], "perf")
    la    = alert_rows(data["load_alerts"], "load")

    tn_str  = str(total_nodes) if total_nodes is not None else "?"
    af_str  = str(fam["active_families"])
    nf_str  = str(fam["nodes_in_families"])
    gf_str  = str(fam["gw_in_families"])
    mf_str  = str(fam["mix_in_families"])

    return (
        page_open("Nym Network Metrics")
        + render_header("Network Metrics", generated)
        + render_summary([
            (data["total"],          "Gateways Total"),
            (tn_str,                 "Nodes Total"),
            (data["location_count"], "Locations"),
            (data["quic_bridges"],   "QUIC Bridges"),
            (af_str,                 "Active Families"),
            (nf_str,                 "Nodes in Families"),
            (gf_str,                 "Gateways in Families"),
            (mf_str,                 "Mixnodes in Families"),
            ('<a href="residential.html" style="color:var(--accent);text-decoration:none">' + str(data["residential"]) + '</a>', "Residential IPs"),
            ('<a href="residential.html" style="color:var(--accent);text-decoration:none">' + str(data["residential_locations"]) + '</a>', "Residential Locations"),
            ('<a href="residential.html" style="text-decoration:none;color:'
             + gauge_color(data["residential_load"] or 0, invert=True) + '">'
             + pct(data["residential_load"]) + '</a>', "Residential Load"),
        ])
        + '<main class="main">'

        + '<div class="section">'
        + '<div class="sec-hdr"><h2>30-Day History</h2></div>'
        + '<div class="chart-wrap">' + chart + '</div>'
        + '</div>'

        + '<div class="pair">'

        + '<div class="section">'
        + '<div class="sec-hdr"><h2>Network Performance</h2></div>'
        + '<div class="sec-body">'
        + '<div class="big"><div class="bignum" style="color:' + pc + '">' + pct(mp) + '</div>'
        + '<div class="meta"><strong>Mean performance score</strong>Across ' + str(data["probe_count"]) + ' measurable gateways<br>Source: API performance field (uptime-based)</div></div>'
        + '<div class="pills">'
        + '<span class="pill p-ph">&#9650; High: ' + str(pt.get("high", 0)) + '</span>'
        + '<span class="pill p-pm">&#9670; Medium: ' + str(pt.get("medium", 0)) + '</span>'
        + '<span class="pill p-pl">&#9660; Low: ' + str(pt.get("low", 0)) + '</span>'
        + '<span class="pill p-po">&#10005; Offline: ' + str(pt.get("offline", 0)) + '</span>'
        + '<span class="pill p-pn">? No data: ' + str(data["no_probe"]) + '</span>'
        + '</div>'
        + '<div class="histo">' + histo + '</div>'
        + '<div class="histo-lbl"><span>0%</span><span>50%</span><span>100%</span></div>'
        + '</div></div>'

        + '<div class="section">'
        + '<div class="sec-hdr"><h2>Network Load</h2></div>'
        + '<div class="sec-body">'
        + '<div class="big"><div class="bignum" style="color:' + lc + '">' + pct(ml) + '</div>'
        + '<div class="meta"><strong>Mean load score</strong>0% = all nodes low load (healthy)<br>100% = all nodes high load (stressed)</div></div>'
        + '<div class="pills">'
        + '<span class="pill p-ll">&#10003; Low (healthy): ' + str(lt.get("low", 0)) + '</span>'
        + '<span class="pill p-lm">&#9670; Medium: ' + str(lt.get("medium", 0)) + '</span>'
        + '<span class="pill p-lh">&#9888; High (stressed): ' + str(lt.get("high", 0)) + '</span>'
        + '<span class="pill p-lo">&#10005; Offline: ' + str(lt.get("offline", 0)) + '</span>'
        + '</div>'
        + '<div class="lbar-wrap">'
        + '<div class="lbar">'
        + '<div style="width:' + lw + '%;background:#00d26a"></div>'
        + '<div style="width:' + mw + '%;background:#f5a623"></div>'
        + '<div style="width:' + hw + '%;background:#e05b2b"></div>'
        + '</div>'
        + '<div class="lbar-leg">'
        + '<span style="color:#00d26a">Low (' + str(lt.get("low", 0)) + ')</span>'
        + '<span style="color:#f5a623">Medium (' + str(lt.get("medium", 0)) + ')</span>'
        + '<span style="color:#e05b2b">High (' + str(lt.get("high", 0)) + ')</span>'
        + '</div></div>'
        + '</div></div>'
        + '</div>'

        + '<div class="pair top">'
        + '<div class="section"><div class="sec-hdr"><h2>Locations</h2>'
        + '<span class="zhint" style="margin-left:auto">click a column header to sort</span></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table class="sortable">'
        + '<thead><tr>'
        + '<th data-t="s">Country</th>'
        + '<th data-t="n">Nodes</th>'
        + '<th data-t="n" class="sdesc">Performance</th>'
        + '<th data-t="n">Load</th>'
        + '</tr></thead>'
        + '<tbody>' + locs + '</tbody></table></div></div></div>'
        + render_residential_inline(data["residential_nodes"])
        + '</div>'

        + '<div class="pair">'
        + '<div class="section"><div class="sec-hdr"><h2>Performance Alerts</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Country</th><th>Nodes</th><th>Score</th><th>Status</th></tr></thead>'
        + '<tbody>' + pa + '</tbody></table></div></div></div>'
        + '<div class="section"><div class="sec-hdr"><h2>Load Alerts</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Country</th><th>Nodes</th><th>Load</th><th>Status</th></tr></thead>'
        + '<tbody>' + la + '</tbody></table></div></div></div>'
        + '</div>'

        + '</main>'
        + '<footer class="footer">Source: ' + GATEWAYS_API + ' &nbsp;&#183;&nbsp; HTML every 5 min &#183; History hourly'
        + ' &nbsp;&#183;&nbsp; <a href="/swagger/">API (interim)</a></footer>'
        + page_close()
    )


# ── Country page ──────────────────────────────────────────────────────────────
def render_country(country, hist, generated):
    cc   = country["cc"]
    cp   = country["mean_perf"]
    cl   = country["mean_load"]
    pt   = country["perf_tiers"]
    lt   = country["load_tiers"]
    lt_t = max(sum(lt.values()), 1)
    pc   = gauge_color(cp or 0, invert=False)
    lc   = gauge_color(cl or 0, invert=True)

    lw = str(round(lt.get("low",    0) / lt_t * 100, 1))
    mw = str(round(lt.get("medium", 0) / lt_t * 100, 1))
    hw = str(round(lt.get("high",   0) / lt_t * 100, 1))

    chart = history_chart(hist, "ccChart", show_locations=False)
    ls    = ('<span class="ls">low sample</span>' if country["low_sample"] else "")

    perf_pills = ""
    for k, sym in [("high", "&#9650;"), ("medium", "&#9670;"), ("low", "&#9660;"), ("offline", "&#10005;")]:
        v = pt.get(k, 0)
        if v > 0:
            cls = {"high": "p-ph", "medium": "p-pm", "low": "p-pl", "offline": "p-po"}.get(k, "p-pn")
            perf_pills += '<span class="pill ' + cls + '">' + sym + ' ' + k.title() + ': ' + str(v) + '</span>'

    node_rows = ""
    for n in country["nodes"]:
        ikey  = n["identity_key"]
        ishrt = (ikey[:20] + "&#8230;") if len(ikey) > 20 else ikey
        hm    = HARBOURMASTER.format(k=ikey)
        sd    = SPECTREDAO.format(k=ikey)
        upt   = (str(round(n["uptime"] * 100)) + "%") if n.get("uptime") is not None else "&#8212;"
        ps    = pct(n["perf_score"]) if n["has_probe"] else "no probe"
        node_rows += (
            '<tr>'
            '<td><span class="ikey" title="' + ikey + '">' + ishrt + '</span></td>'
            '<td>' + n["name"] + '</td>'
            '<td>' + (n.get("city") or "&#8212;") + '</td>'
            '<td>' + ps + '</td>'
            '<td>' + perf_badge(n["perf_tier"]) + '</td>'
            '<td>' + load_badge(n["load_str"] or "unknown") + '</td>'
            '<td>' + upt + '</td>'
            '<td style="display:flex;gap:8px;flex-wrap:wrap">'
            '<a class="nlink" href="' + hm + '" target="_blank" rel="noopener">Harbourmaster</a>'
            '<a class="nlink" href="' + sd + '" target="_blank" rel="noopener">SpectrDAO</a>'
            '</td>'
            '</tr>'
        )

    measurable = len([n for n in country["nodes"] if n["has_probe"]])

    return (
        page_open("Nym &#8212; " + cc + " Network Metrics")
        + render_header(flag(cc) + " " + cc + " &#8212; Gateway Metrics", generated, back="../index.html")
        + render_summary([
            (str(country["node_count"]), "Gateways"),
            (pct(cp), "Mean Performance"),
            (pct(cl), "Mean Load"),
        ])
        + '<main class="main">'

        + '<div class="section">'
        + '<div class="sec-hdr"><h2>30-Day History &#8212; ' + cc + '</h2></div>'
        + '<div class="chart-wrap">' + chart + '</div>'
        + '</div>'

        + '<div class="pair">'

        + '<div class="section"><div class="sec-hdr"><h2>Performance</h2></div>'
        + '<div class="sec-body">'
        + '<div class="big"><div class="bignum" style="color:' + pc + '">' + pct(cp) + '</div>'
        + '<div class="meta"><strong>Mean across ' + str(measurable) + ' measurable gateways</strong>'
        + str(country["node_count"] - measurable) + ' without probe data</div></div>'
        + '<div class="pills">' + perf_pills + '</div>'
        + '</div></div>'

        + '<div class="section"><div class="sec-hdr"><h2>Load</h2></div>'
        + '<div class="sec-body">'
        + '<div class="big"><div class="bignum" style="color:' + lc + '">' + pct(cl) + '</div>'
        + '<div class="meta"><strong>Mean load score</strong>0% = all nodes low load (healthy)<br>100% = all nodes high load (stressed)</div></div>'
        + '<div class="pills">'
        + '<span class="pill p-ll">&#10003; Low (healthy): ' + str(lt.get("low", 0)) + '</span>'
        + '<span class="pill p-lm">&#9670; Medium: ' + str(lt.get("medium", 0)) + '</span>'
        + '<span class="pill p-lh">&#9888; High (stressed): ' + str(lt.get("high", 0)) + '</span>'
        + '<span class="pill p-lo">&#10005; Offline: ' + str(lt.get("offline", 0)) + '</span>'
        + '</div>'
        + '<div class="lbar-wrap"><div class="lbar">'
        + '<div style="width:' + lw + '%;background:#00d26a"></div>'
        + '<div style="width:' + mw + '%;background:#f5a623"></div>'
        + '<div style="width:' + hw + '%;background:#e05b2b"></div>'
        + '</div>'
        + '<div class="lbar-leg">'
        + '<span style="color:#00d26a">Low (' + str(lt.get("low", 0)) + ')</span>'
        + '<span style="color:#f5a623">Medium (' + str(lt.get("medium", 0)) + ')</span>'
        + '<span style="color:#e05b2b">High (' + str(lt.get("high", 0)) + ')</span>'
        + '</div></div>'
        + '</div></div>'
        + '</div>'

        + '<div class="section"><div class="sec-hdr"><h2>Nodes in ' + cc + '</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Identity Key</th><th>Name</th><th>City</th><th>Performance</th><th>Perf Tier</th><th>Load</th><th>Uptime 24h</th><th>Links</th></tr></thead>'
        + '<tbody>' + node_rows + '</tbody>'
        + '</table></div></div></div>'

        + '</main>'
        + '<footer class="footer">' + GATEWAYS_API + ' &nbsp;&#183;&nbsp; ' + flag(cc) + ' ' + cc + ' &nbsp;&#183;&nbsp; <a href="../index.html">&#8592; All locations</a></footer>'
        + page_close()
    )




# ── Residential inline section (for index page) ───────────────────────────────
def render_residential_inline(nodes):
    """Compact residential board for the right column of the index page."""
    if not nodes:
        body = '<tr><td colspan="5" class="no-alerts">No residential IP gateways right now</td></tr>'
    else:
        body = ""
        for n in nodes:
            ikey = n["identity_key"]
            cc   = n.get("cc", "??")
            hm   = HARBOURMASTER.format(k=ikey)
            sd   = SPECTREDAO.format(k=ikey)
            ps   = pct(n["perf_score"]) if n["has_probe"] else "no probe"
            body += (
                '<tr>'
                '<td><span title="' + ikey + ' &#183; ' + (n.get("asn_name") or "") + '">' + n["name"] + '</span>'
                '<br><span class="ikey">' + (n.get("city") or "&#8212;") + '</span></td>'
                '<td><a href="country/' + cc + '.html">' + flag(cc) + ' ' + cc + '</a></td>'
                '<td>' + ps + ' ' + perf_badge(n["perf_tier"]) + '</td>'
                '<td>' + load_badge(n["load_str"] or "unknown") + '</td>'
                '<td style="white-space:nowrap">'
                '<a class="nlink" title="Harbourmaster" href="' + hm + '" target="_blank" rel="noopener">HM</a> '
                '<a class="nlink" title="SpectreDAO explorer" href="' + sd + '" target="_blank" rel="noopener">SD</a>'
                '</td>'
                '</tr>'
            )
    return (
        '<div class="section">'
        '<div class="sec-hdr"><h2>Residential IP Gateways</h2>'
        '<span style="margin-left:auto;font-size:11px;font-family:var(--mono)">'
        '<a href="residential.html">full view &#8594;</a></span></div>'
        '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        '<thead><tr><th>Name</th><th>Country</th><th>Performance</th><th>Load</th><th>Links</th></tr></thead>'
        '<tbody>' + body + '</tbody>'
        '</table></div></div></div>'
    )


# ── Residential page ──────────────────────────────────────────────────────────
def render_residential(nodes, generated):
    node_rows = ""
    for n in nodes:
        ikey  = n["identity_key"]
        ishrt = (ikey[:20] + "&#8230;") if len(ikey) > 20 else ikey
        hm    = HARBOURMASTER.format(k=ikey)
        sd    = SPECTREDAO.format(k=ikey)
        upt   = (str(round(n["uptime"] * 100)) + "%") if n.get("uptime") is not None else "&#8212;"
        ps    = pct(n["perf_score"]) if n["has_probe"] else "no probe"
        node_rows += (
            "<tr>"
            "<td><span class=\"ikey\" title=\"" + ikey + "\">" + ishrt + "</span></td>"
            "<td>" + n["name"] + "</td>"
            "<td>" + flag(n["cc"]) + " " + n["cc"] + "</td>"
            "<td>" + (n.get("city") or "&#8212;") + "</td>"
            "<td>" + (n.get("asn_name") or "&#8212;") + "</td>"
            "<td>" + ps + "</td>"
            "<td>" + perf_badge(n["perf_tier"]) + "</td>"
            "<td>" + load_badge(n["load_str"] or "unknown") + "</td>"
            "<td>" + upt + "</td>"
            "<td style=\"display:flex;gap:8px;flex-wrap:wrap\">"
            "<a class=\"nlink\" href=\"" + hm + "\" target=\"_blank\" rel=\"noopener\">Harbourmaster</a>"
            "<a class=\"nlink\" href=\"" + sd + "\" target=\"_blank\" rel=\"noopener\">SpectrDAO</a>"
            "</td>"
            "</tr>"
        )

    return (
        page_open("Nym &#8212; Residential IPs")
        + render_header("Residential IPs &#8212; Gateway Nodes", generated, back="index.html")
        + render_summary([
            (str(len(nodes)), "Residential Nodes"),
            (str(len(set(n["cc"] for n in nodes))), "Countries"),
        ])
        + "<main class=\"main\">"
        + "<div class=\"section\">"
        + "<div class=\"sec-hdr\"><h2>Residential IP Nodes</h2></div>"
        + "<div class=\"sec-body\" style=\"padding:0\"><div class=\"tbl-wrap\"><table>"
        + "<thead><tr>"
        + "<th>Identity Key</th><th>Name</th><th>Country</th><th>City</th>"
        + "<th>ASN</th><th>Performance</th><th>Perf Tier</th><th>Load</th>"
        + "<th>Uptime 24h</th><th>Links</th>"
        + "</tr></thead>"
        + "<tbody>" + node_rows + "</tbody>"
        + "</table></div></div></div>"
        + "</main>"
        + "<footer class=\"footer\">"
        + "Residential nodes identified via location.asn.kind field &nbsp;&#183;&nbsp; "
        + "<a href=\"index.html\">&#8592; Network Metrics</a>"
        + "</footer>"
        + page_close()
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now       = datetime.datetime.now(datetime.timezone.utc)
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    print("[" + now.isoformat() + "] Starting ...")

    try:
        gateways = fetch_gateways()
        print("  Gateways: " + str(len(gateways)))
    except Exception as e:
        print("ERROR: gateways fetch failed: " + str(e), file=sys.stderr)
        sys.exit(1)

    try:
        total_nodes = fetch_total_nodes()
        print("  Total nodes (all types): " + str(total_nodes))
    except Exception as e:
        print("  WARN: total nodes fetch failed: " + str(e))
        total_nodes = None

    try:
        fam = fetch_family_stats()
        print("  Active families: " + str(fam["active_families"]) +
              "  Nodes in families: " + str(fam["nodes_in_families"]) +
              "  (GW: " + str(fam["gw_in_families"]) +
              "  MX: " + str(fam["mix_in_families"]) + ")")
    except Exception as e:
        print("  WARN: families fetch failed: " + str(e))
        fam = {"active_families": None, "nodes_in_families": None,
               "gw_in_families": None, "mix_in_families": None}

    data = aggregate(gateways)
    print("  Locations: " + str(data["location_count"]) +
          "  Perf: " + pct(data["mean_perf"]) +
          "  Load: " + pct(data["mean_load"]))

    hist = load_global_history()
    print("  History: " + str(len(hist["labels"]) if hist else 0) + " snapshots")

    atomic_write(OUTPUT_INDEX, render_index(data, fam, total_nodes, hist, generated))
    print("  Written -> " + str(OUTPUT_INDEX))

    OUTPUT_COUNTRY.mkdir(parents=True, exist_ok=True)
    for c in data["countries"]:
        cc_hist = load_country_history(c["cc"])
        atomic_write(OUTPUT_COUNTRY / (c["cc"] + ".html"), render_country(c, cc_hist, generated))

    print("  Written -> " + str(len(data["countries"])) + " country pages")

    atomic_write(OUTPUT_RESIDENTIAL, render_residential(data["residential_nodes"], generated))
    print("  Written -> " + str(OUTPUT_RESIDENTIAL))

    # interim static API (/api/v0/ + /swagger/); a failure here must never break the dashboard
    try:
        import interim_api
        n = interim_api.write_all(OUTPUT_INDEX.parent, DB_PATH, data, fam, total_nodes,
                                  now.strftime("%Y-%m-%dT%H:%M:%SZ"), atomic_write)
        print("  Written -> " + str(n) + " interim API files (/api/v0/, /swagger/)")
    except Exception as e:
        print("  WARN: interim API write failed: " + str(e), file=sys.stderr)


if __name__ == "__main__":
    main()
