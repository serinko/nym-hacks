#!/usr/bin/env python3
"""
Nym Network Metrics - static HTML generator.
Fetches https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways
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
API_URL        = "https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways"
OUTPUT_INDEX   = Path("/var/www/html/network-load/index.html")
OUTPUT_COUNTRY = Path("/var/www/html/network-load/country")
DB_PATH        = Path("/var/lib/nym-metrics/history.db")
TIMEOUT_SEC    = 30

HARBOURMASTER = "https://harbourmaster.nymtech.net/gateway/{k}"
SPECTREDAO    = "https://explorer.nym.spectredao.net/nodes/{k}"

# ── Scoring ───────────────────────────────────────────────────────────────────
# Load: low=good=0.0, high=bad=1.0
LOAD_SCORE_MAP = {"low": 0.0, "medium": 0.5, "high": 1.0, "offline": 1.0}



def compute_node_perf(node):
    # Use the top-level "performance" field which is the authoritative
    # score already computed by the API (e.g. "0.88"). This correctly
    # reflects uptime and all probe results as weighted by the API.
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
    # score 0.0=all low-load, 1.0=all high-load
    if score < 0.25: return "low"
    if score < 0.75: return "medium"
    return "high"


def mean(lst):
    return sum(lst) / len(lst) if lst else None


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_gateways():
    req = urllib.request.Request(API_URL, headers={"User-Agent": "nym-metrics/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode())


# ── Aggregate ─────────────────────────────────────────────────────────────────
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

    for node in gateways:
        pv2  = node.get("performance_v2") or {}
        loc  = node.get("location") or {}
        cc   = (loc.get("two_letter_iso_country_code") or "??").upper()
        ikey = node.get("identity_key") or ""
        name = node.get("name") or (ikey[:16] + "...")

        # load - raw API string is "low", "medium", "high", "offline"
        load_str = pv2.get("load") or ""
        load_num = None
        if load_str in LOAD_SCORE_MAP:
            load_num = LOAD_SCORE_MAP[load_str]
            load_scores.append(load_num)
            load_tiers[load_str] += 1
            countries[cc]["load_scores"].append(load_num)
            countries[cc]["load_tiers"][load_str] += 1

        # performance
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
        countries[cc]["nodes"].append({
            "identity_key": ikey,
            "name":         name,
            "city":         loc.get("city") or "",
            "perf_score":   ps,
            "has_probe":    has_probe,
            "perf_tier":    perf_tier(ps) if has_probe else "unknown",
            "load_str":     load_str,
            "uptime":       pv2.get("uptime_percentage_last_24_hours"),
        })

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
            "nodes":      sorted(d["nodes"], key=lambda n: (LOAD_SCORE_MAP.get(n["load_str"], 0), -(n["perf_score"] or 0)), reverse=True),
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
        "mean_perf":      mean(perf_scores),
        "mean_load":      mean(load_scores),
        "perf_tiers":     dict(perf_tiers),
        "load_tiers":     dict(load_tiers),
        "perf_scores":    perf_scores,
        "countries":      sorted(country_data, key=lambda c: -(c["mean_perf"] or 0)),
        "perf_alerts":    perf_alerts,
        "load_alerts":    load_alerts,
    }


# ── History ───────────────────────────────────────────────────────────────────
def load_global_history():
    if not DB_PATH.exists():
        return None
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ts, node_count, loc_count, mean_perf, mean_load "
            "FROM global_snapshots ORDER BY ts ASC"
        ).fetchall()
    if not rows:
        return None
    return {
        "labels":    [r[0][:16].replace("T", " ") for r in rows],
        "nodes":     [r[1] for r in rows],
        "locations": [r[2] for r in rows],
        "perf":      [round(r[3] * 100, 1) if r[3] is not None else None for r in rows],
        "load":      [round(r[4] * 100, 1) if r[4] is not None else None for r in rows],
    }


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
    return f"{v * 100:.1f}%"


def flag(cc):
    cc = cc.upper()
    if len(cc) != 2 or cc == "??":
        return "&#127760;"
    return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)


def gauge_color(v, invert=False):
    # invert=True for load: high load score = red
    if invert:
        if v < 0.25: return "#00d26a"
        if v < 0.75: return "#f5a623"
        return "#e05b2b"
    else:
        if v > 0.75: return "#00d26a"
        if v > 0.50: return "#f5a623"
        return "#e05b2b"


def perf_badge(tier):
    # high perf = green, low perf = red
    c = {
        "high":    ("background:#0a2a1a;color:#00d26a;border:1px solid #00d26a44"),
        "medium":  ("background:#2a1e00;color:#f5a623;border:1px solid #f5a62344"),
        "low":     ("background:#2a1200;color:#e05b2b;border:1px solid #e05b2b44"),
        "offline": ("background:#1e1e1e;color:#666;border:1px solid #44444444"),
        "unknown": ("background:#1a1a1a;color:#555;border:1px solid #33333344"),
    }.get(tier, "background:#1a1a1a;color:#555")
    return '<span class="badge" style="' + c + '">' + tier + '</span>'


def load_badge(load_str):
    # low load = green (healthy), high load = red (stressed)
    c = {
        "low":     ("background:#0a2a1a;color:#00d26a;border:1px solid #00d26a44"),
        "medium":  ("background:#2a1e00;color:#f5a623;border:1px solid #f5a62344"),
        "high":    ("background:#2a1200;color:#e05b2b;border:1px solid #e05b2b44"),
        "offline": ("background:#1e1e1e;color:#666;border:1px solid #44444444"),
        "unknown": ("background:#1a1a1a;color:#555;border:1px solid #33333344"),
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
        bars += '<div class="histo-bar" title="' + str(i*10) + '-' + str(i*10+10) + '%: ' + str(b) + ' nodes" style="height:' + str(h) + 'px"></div>'
    return bars


def history_chart(hist, chart_id, show_locations=True):
    if hist is None:
        return '<div class="chart-empty">No historical data yet. Accumulates after first hourly snapshot.</div>'

    labels_j = json.dumps(hist["labels"])
    nodes_j  = json.dumps(hist["nodes"])
    perf_j   = json.dumps(hist["perf"])
    load_j   = json.dumps(hist["load"])

    ds = (
        "{"
        "label:'Performance %',"
        "data:" + perf_j + ","
        "borderColor:'#00d26a',backgroundColor:'#00d26a22',"
        "yAxisID:'yPct',tension:0.3,pointRadius:2,fill:false"
        "},{"
        "label:'Load %',"
        "data:" + load_j + ","
        "borderColor:'#e05b2b',backgroundColor:'#e05b2b22',"
        "yAxisID:'yPct',tension:0.3,pointRadius:2,fill:false"
        "},{"
        "label:'Nodes',"
        "data:" + nodes_j + ","
        "borderColor:'#f5a623',backgroundColor:'#f5a62322',"
        "yAxisID:'yCount',tension:0.3,pointRadius:2,fill:false"
        "}"
    )
    if show_locations:
        loc_j = json.dumps(hist.get("locations", []))
        ds += (",{"
               "label:'Locations',"
               "data:" + loc_j + ","
               "borderColor:'#7b61ff',backgroundColor:'#7b61ff22',"
               "yAxisID:'yCount',tension:0.3,pointRadius:2,fill:false"
               "}")

    return (
        '<canvas id="' + chart_id + '" height="120"></canvas>'
        '<script>(function(){'
        'var ctx=document.getElementById("' + chart_id + '");'
        'var dk=document.documentElement.getAttribute("data-theme")!=="light";'
        'var gc=dk?"#2e343244":"#d4dbd844";'
        'var lc=dk?"#8a9693":"#5a6662";'
        'new Chart(ctx,{'
        'type:"line",'
        'data:{labels:' + labels_j + ',datasets:[' + ds + ']},'
        'options:{'
        'responsive:true,'
        'interaction:{mode:"index",intersect:false},'
        'plugins:{'
        'legend:{labels:{color:lc,boxWidth:12,font:{size:11}}},'
        'tooltip:{backgroundColor:dk?"#1c201f":"#fff",borderColor:dk?"#2e3432":"#d4dbd8",borderWidth:1,titleColor:dk?"#e8edeb":"#111413",bodyColor:lc}'
        '},'
        'scales:{'
        'x:{ticks:{color:lc,maxTicksLimit:10,font:{size:10},maxRotation:30},grid:{color:gc}},'
        'yPct:{type:"linear",position:"left",min:0,max:100,ticks:{color:lc,font:{size:10},callback:function(v){return v+"%"}},grid:{color:gc},title:{display:true,text:"%",color:lc,font:{size:10}}},'
        'yCount:{type:"linear",position:"right",min:0,ticks:{color:lc,font:{size:10}},grid:{drawOnChartArea:false},title:{display:true,text:"count",color:lc,font:{size:10}}}'
        '}'
        '}'
        '});'
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
.summary{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 32px;display:flex;align-items:center;gap:32px;flex-wrap:wrap}
.s-stat{display:flex;align-items:baseline;gap:8px}
.s-val{font-family:var(--mono);font-size:22px;font-weight:700;color:var(--accent)}
.s-lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.s-div{width:1px;height:24px;background:var(--border)}
.main{max-width:1200px;margin:0 auto;padding:32px 24px;display:flex;flex-direction:column;gap:32px}
.section{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.sec-hdr{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sec-hdr h2{font-size:14px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:var(--muted)}
.sec-num{font-family:var(--mono);font-size:11px;color:var(--accent);background:var(--bg3);border:1px solid var(--border);padding:2px 6px;border-radius:3px}
.sec-body{padding:24px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}
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


CHARTJS = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'


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
    return '<script>' + THEME_JS + '</script></body></html>'


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


def country_rows(countries, sort_key):
    is_perf = sort_key == "mean_perf"
    rows = ""
    for c in sorted(countries, key=lambda x: -(x[sort_key] or 0)):
        val   = c["mean_perf"] if is_perf else c["mean_load"]
        ls    = ('<span class="ls">low sample</span>' if c["low_sample"] else "")
        link  = '<a href="country/' + c["cc"] + '.html">' + flag(c["cc"]) + ' <strong>' + c["cc"] + '</strong></a>' + ls

        if is_perf:
            badge = perf_badge(c["perf_tier"])
            # perf distribution: high=green, low=red
            pill_colors = {"high": "#0a2a1a;color:#00d26a", "medium": "#2a1e00;color:#f5a623", "low": "#2a1200;color:#e05b2b", "offline": "#1e1e1e;color:#666"}
            order = ["high", "medium", "low", "offline"]
            src   = c["perf_tiers"]
        else:
            badge = load_badge(c["load_tier"])
            # load distribution: low=green (good), high=red (bad)
            pill_colors = {"low": "#0a2a1a;color:#00d26a", "medium": "#2a1e00;color:#f5a623", "high": "#2a1200;color:#e05b2b", "offline": "#1e1e1e;color:#666"}
            order = ["low", "medium", "high", "offline"]
            src   = c["load_tiers"]

        pills = ""
        for k in order:
            v = src.get(k, 0)
            if v > 0:
                pills += '<span class="tp" style="background:' + pill_colors.get(k, "#1e1e1e;color:#666") + '">' + k[0].upper() + ':' + str(v) + '</span>'

        rows += (
            '<tr>'
            '<td>' + link + '</td>'
            '<td>' + str(c["node_count"]) + '</td>'
            '<td>' + pct(val) + '</td>'
            '<td>' + badge + '</td>'
            '<td class="tpills">' + pills + '</td>'
            '</tr>'
        )
    return rows


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
def render_index(data, hist, generated):
    mp = data["mean_perf"] or 0
    ml = data["mean_load"] or 0
    pt = data["perf_tiers"]
    lt = data["load_tiers"]
    lt_total = max(sum(lt.values()), 1)

    pc = gauge_color(mp, invert=False)
    lc = gauge_color(ml, invert=True)

    lw = str(round(lt.get("low",  0) / lt_total * 100, 1))
    mw = str(round(lt.get("medium", 0) / lt_total * 100, 1))
    hw = str(round(lt.get("high", 0) / lt_total * 100, 1))

    histo = build_histogram(data["perf_scores"])
    chart = history_chart(hist, "globalChart", show_locations=True)
    pr    = country_rows(data["countries"], "mean_perf")
    lr    = country_rows(data["countries"], "mean_load")
    pa    = alert_rows(data["perf_alerts"], "perf")
    la    = alert_rows(data["load_alerts"], "load")

    return (
        page_open("Nym Network Metrics")
        + render_header("Network Metrics", generated)
        + render_summary([
            (data["total"],          "Total Nodes"),
            (data["location_count"], "Locations"),
            (data["probe_count"],    "Nodes Measured"),
            (data["no_probe"],       "No Probe Data"),
        ])
        + '<main class="main">'

        # history
        + '<div class="section">'
        + '<div class="sec-hdr"><span class="sec-num">00</span><h2>30-Day History</h2></div>'
        + '<div class="chart-wrap">' + chart + '</div>'
        + '</div>'

        # perf + load
        + '<div class="pair">'

        + '<div class="section">'
        + '<div class="sec-hdr"><span class="sec-num">01</span><h2>Network Performance</h2></div>'
        + '<div class="sec-body">'
        + '<div class="big"><div class="bignum" style="color:' + pc + '">' + pct(mp) + '</div>'
        + '<div class="meta"><strong>Mean performance score</strong>Across ' + str(data["probe_count"]) + ' measurable nodes<br>Formula: mixnet &#215; (download_speed &#215; ping_v4)</div></div>'
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
        + '<div class="sec-hdr"><span class="sec-num">02</span><h2>Network Load</h2></div>'
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

        # location tables
        + '<div class="pair">'
        + '<div class="section"><div class="sec-hdr"><span class="sec-num">03</span><h2>Locations &#8212; Performance</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Country</th><th>Nodes</th><th>Score</th><th>Tier</th><th>Distribution</th></tr></thead>'
        + '<tbody>' + pr + '</tbody></table></div></div></div>'
        + '<div class="section"><div class="sec-hdr"><span class="sec-num">04</span><h2>Locations &#8212; Load</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Country</th><th>Nodes</th><th>Load</th><th>Tier</th><th>Distribution</th></tr></thead>'
        + '<tbody>' + lr + '</tbody></table></div></div></div>'
        + '</div>'

        # alerts
        + '<div class="pair">'
        + '<div class="section"><div class="sec-hdr"><span class="sec-num">05</span><h2>Performance Alerts</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Country</th><th>Nodes</th><th>Score</th><th>Status</th></tr></thead>'
        + '<tbody>' + pa + '</tbody></table></div></div></div>'
        + '<div class="section"><div class="sec-hdr"><span class="sec-num">06</span><h2>Load Alerts</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Country</th><th>Nodes</th><th>Load</th><th>Status</th></tr></thead>'
        + '<tbody>' + la + '</tbody></table></div></div></div>'
        + '</div>'

        + '</main>'
        + '<footer class="footer">Source: ' + API_URL + ' &nbsp;&#183;&nbsp; HTML every 5 min &#183; History hourly</footer>'
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

    # perf pills
    perf_pills = ""
    for k, sym in [("high", "&#9650;"), ("medium", "&#9670;"), ("low", "&#9660;"), ("offline", "&#10005;")]:
        v = pt.get(k, 0)
        if v > 0:
            cls = {"high": "p-ph", "medium": "p-pm", "low": "p-pl", "offline": "p-po"}.get(k, "p-pn")
            perf_pills += '<span class="pill ' + cls + '">' + sym + ' ' + k.title() + ': ' + str(v) + '</span>'

    # node rows
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
            (str(country["node_count"]) + ("" if not country["low_sample"] else ""), "Nodes"),
            (pct(cp), "Mean Performance"),
            (pct(cl), "Mean Load"),
        ])
        + '<main class="main">'

        # history
        + '<div class="section">'
        + '<div class="sec-hdr"><span class="sec-num">00</span><h2>30-Day History &#8212; ' + cc + '</h2></div>'
        + '<div class="chart-wrap">' + chart + '</div>'
        + '</div>'

        # perf + load
        + '<div class="pair">'

        + '<div class="section"><div class="sec-hdr"><span class="sec-num">01</span><h2>Performance</h2></div>'
        + '<div class="sec-body">'
        + '<div class="big"><div class="bignum" style="color:' + pc + '">' + pct(cp) + '</div>'
        + '<div class="meta"><strong>Mean across ' + str(measurable) + ' measurable nodes</strong>'
        + str(country["node_count"] - measurable) + ' without probe data</div></div>'
        + '<div class="pills">' + perf_pills + '</div>'
        + '</div></div>'

        + '<div class="section"><div class="sec-hdr"><span class="sec-num">02</span><h2>Load</h2></div>'
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

        # node table
        + '<div class="section"><div class="sec-hdr"><span class="sec-num">03</span><h2>Nodes in ' + cc + '</h2></div>'
        + '<div class="sec-body" style="padding:0"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Identity Key</th><th>Name</th><th>City</th><th>Performance</th><th>Perf Tier</th><th>Load</th><th>Uptime 24h</th><th>Links</th></tr></thead>'
        + '<tbody>' + node_rows + '</tbody>'
        + '</table></div></div></div>'

        + '</main>'
        + '<footer class="footer">' + API_URL + ' &nbsp;&#183;&nbsp; ' + flag(cc) + ' ' + cc + ' &nbsp;&#183;&nbsp; <a href="../index.html">&#8592; All locations</a></footer>'
        + page_close()
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now       = datetime.datetime.now(datetime.timezone.utc)
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    print("[" + now.isoformat() + "] Fetching " + API_URL + " ...")

    try:
        gateways = fetch_gateways()
    except Exception as e:
        print("ERROR: fetch failed: " + str(e), file=sys.stderr)
        sys.exit(1)

    print("  Got " + str(len(gateways)) + " gateway entries")
    data = aggregate(gateways)
    print("  Nodes: " + str(data["total"]) + "  Locations: " + str(data["location_count"]) +
          "  Perf: " + pct(data["mean_perf"]) + "  Load: " + pct(data["mean_load"]))

    hist = load_global_history()
    print("  History: " + str(len(hist["labels"]) if hist else 0) + " snapshots")

    atomic_write(OUTPUT_INDEX, render_index(data, hist, generated))
    print("  Written -> " + str(OUTPUT_INDEX))

    OUTPUT_COUNTRY.mkdir(parents=True, exist_ok=True)
    for c in data["countries"]:
        cc_hist = load_country_history(c["cc"])
        atomic_write(OUTPUT_COUNTRY / (c["cc"] + ".html"), render_country(c, cc_hist, generated))

    print("  Written -> " + str(len(data["countries"])) + " country pages")


if __name__ == "__main__":
    main()
