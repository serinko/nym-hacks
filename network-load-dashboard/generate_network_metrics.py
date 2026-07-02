#!/usr/bin/env python3
"""
Nym Network Metrics — static HTML generator
Fetches https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways
and writes a self-contained HTML page to OUTPUT_PATH.

Run manually or via cron every 5 minutes:
    */5 * * * * /usr/bin/python3 /opt/nym-metrics/generate_network_metrics.py

Place the output under your nginx root, e.g.:
    OUTPUT_PATH = "/var/www/html/network-load/index.html"
"""

import json
import sys
import datetime
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
API_URL     = "https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways"
OUTPUT_PATH = Path("/var/www/html/network-load/index.html")
TIMEOUT_SEC = 30

# ── Scoring helpers ───────────────────────────────────────────────────────────

MIXNET_SCORE_MAP = {"high": 1.0, "medium": 0.625, "low": 0.30, "offline": 0.0}
LOAD_SCORE_MAP   = {"low": 0.0, "medium": 0.5, "high": 1.0}


def download_speed_score(wg: dict) -> float:
    error = wg.get("download_error_v4", "")
    ms    = wg.get("download_duration_milliseconds_v4", 0) or 0
    if error or ms == 0:
        return 0.1
    size_bytes = wg.get("downloaded_file_size_bytes_v4", 0) or 0
    mbps = (size_bytes * 8) / (ms / 1000) / 1_000_000
    if mbps > 5:   return 1.0
    if mbps > 2:   return 0.75
    if mbps > 1:   return 0.5
    if mbps > 0.5: return 0.25
    return 0.1


def perf_tier(score: float) -> str:
    """Spec: High >75%, Medium >50%, Low >10%, Offline <=10%."""
    if score > 0.75: return "high"
    if score > 0.50: return "medium"
    if score > 0.10: return "low"
    return "offline"


def load_tier_from_score(score: float) -> str:
    """
    Load score: low=0.0, medium=0.5, high=1.0 per LOAD_SCORE_MAP.
    A country mean <0.25 is predominantly low-load.
    A country mean <0.75 is predominantly medium-load.
    Otherwise predominantly high-load.
    """
    if score < 0.25: return "low"
    if score < 0.75: return "medium"
    return "high"


def compute_node_perf(node: dict):
    """Return (performance_score, has_probe_data). Score is None if no probe."""
    pv2   = node.get("performance_v2") or {}
    probe = node.get("last_probe") or {}
    wg    = (probe.get("outcome") or {}).get("wg") or {}

    if not probe or not wg:
        return None, False

    mixnet_str = pv2.get("mixnet_score", "offline")
    mixnet     = MIXNET_SCORE_MAP.get(mixnet_str, 0.0)
    dl_score   = download_speed_score(wg)
    ping       = wg.get("ping_ips_performance_v4") or 0.0

    return mixnet * (dl_score * ping), True


# ── Fetch & process ───────────────────────────────────────────────────────────

def fetch_gateways() -> list:
    req = urllib.request.Request(API_URL, headers={"User-Agent": "nym-metrics-gen/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode())


def aggregate(gateways: list) -> dict:
    total       = len(gateways)
    perf_scores = []      # per-node scores, for network mean and histogram
    load_scores = []      # per-node load scores, for network mean
    no_probe    = 0

    # global tier counts (node-level)
    perf_tiers = defaultdict(int)   # high/medium/low/offline
    load_tiers = defaultdict(int)   # low/medium/high

    # per country
    countries = defaultdict(lambda: {
        "perf_scores": [], "load_scores": [],
        "perf_tiers": defaultdict(int), "load_tiers": defaultdict(int),
        "node_count": 0,
    })

    for node in gateways:
        pv2  = node.get("performance_v2") or {}
        loc  = node.get("location") or {}
        cc   = (loc.get("two_letter_iso_country_code") or "??").upper()

        # ── load (available whenever performance_v2.load is present) ──
        load_str = pv2.get("load")
        if load_str and load_str in LOAD_SCORE_MAP:
            ls = LOAD_SCORE_MAP[load_str]
            load_scores.append(ls)
            load_tiers[load_str] += 1
            countries[cc]["load_scores"].append(ls)
            countries[cc]["load_tiers"][load_str] += 1

        # ── performance (requires last_probe.outcome.wg) ──
        ps, has_probe = compute_node_perf(node)
        if not has_probe:
            no_probe += 1
        else:
            perf_scores.append(ps)
            tier = perf_tier(ps)
            perf_tiers[tier] += 1
            countries[cc]["perf_scores"].append(ps)
            countries[cc]["perf_tiers"][tier] += 1

        countries[cc]["node_count"] += 1

    def mean(lst): return sum(lst) / len(lst) if lst else None

    # flatten countries
    country_data = []
    for cc, d in countries.items():
        cp = mean(d["perf_scores"])
        cl = mean(d["load_scores"])
        country_data.append({
            "cc": cc,
            "node_count": d["node_count"],
            "low_sample": d["node_count"] < 3,
            "mean_perf": cp,
            "mean_load": cl,
            "perf_tier": perf_tier(cp) if cp is not None else "unknown",
            "load_tier": load_tier_from_score(cl) if cl is not None else "unknown",
            "perf_tiers": dict(d["perf_tiers"]),
            "load_tiers": dict(d["load_tiers"]),
        })

    # alert lists
    # perf alert: country mean in low or offline tier (score < 0.50)
    perf_alerts = sorted(
        [c for c in country_data if c["mean_perf"] is not None and c["mean_perf"] < 0.50],
        key=lambda c: c["mean_perf"]
    )
    # load alert: country mean predominantly medium or high load (score >= 0.25)
    load_alerts = sorted(
        [c for c in country_data if c["mean_load"] is not None and c["mean_load"] >= 0.25],
        key=lambda c: -c["mean_load"]
    )

    return {
        "total": total,
        "no_probe": no_probe,
        "probe_count": total - no_probe,
        "location_count": len(country_data),
        "mean_perf": mean(perf_scores),
        "mean_load": mean(load_scores),
        "perf_tiers": dict(perf_tiers),
        "load_tiers": dict(load_tiers),
        "perf_scores": perf_scores,          # individual node scores for histogram
        "countries": sorted(country_data, key=lambda c: -(c["mean_perf"] or 0)),
        "perf_alerts": perf_alerts,
        "load_alerts": load_alerts,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ── HTML rendering ────────────────────────────────────────────────────────────

def pct(v) -> str:
    if v is None: return "—"
    return f"{v * 100:.1f}%"


def flag(cc: str) -> str:
    """Convert ISO 3166-1 alpha-2 country code to flag emoji."""
    cc = cc.upper()
    if len(cc) != 2 or cc == "??":
        return "🌐"
    return chr(0x1F1E6 + ord(cc[0]) - ord('A')) + chr(0x1F1E6 + ord(cc[1]) - ord('A'))


def tier_badge(tier: str, kind: str = "perf") -> str:
    colors = {
        "perf": {
            "high":    ("#00d26a", "#0a2a1a"),
            "medium":  ("#f5a623", "#2a1e00"),
            "low":     ("#e05b2b", "#2a1200"),
            "offline": ("#666",    "#1e1e1e"),
            "unknown": ("#555",    "#1a1a1a"),
        },
        "load": {
            "low":    ("#00d26a", "#0a2a1a"),
            "medium": ("#f5a623", "#2a1e00"),
            "high":   ("#e05b2b", "#2a1200"),
            "unknown":("#555",    "#1a1a1a"),
        }
    }
    col = colors.get(kind, colors["perf"]).get(tier, ("#555", "#1a1a1a"))
    return (f'<span class="badge" style="background:{col[1]};color:{col[0]};'
            f'border:1px solid {col[0]}44">{tier}</span>')


def country_table_rows(countries: list, sort_key: str) -> str:
    rows = []
    is_perf = sort_key == "mean_perf"
    badge_kind = "perf" if is_perf else "load"

    for c in sorted(countries, key=lambda x: -(x[sort_key] or 0)):
        val       = c["mean_perf"] if is_perf else c["mean_load"]
        tier      = c["perf_tier"] if is_perf else c["load_tier"]
        low_s     = ' <span class="badge-low-sample">low sample</span>' if c["low_sample"] else ""
        tiers_src = c["perf_tiers"] if is_perf else c["load_tiers"]
        # fixed display order for pills
        pill_order = ["high", "medium", "low", "offline"] if is_perf else ["low", "medium", "high"]
        tier_pills = " ".join(
            f'<span class="tier-pill tier-{k}">{k[0].upper()}:{tiers_src[k]}</span>'
            for k in pill_order if tiers_src.get(k, 0) > 0
        )
        rows.append(
            f"<tr>"
            f"<td>{flag(c['cc'])} <strong>{c['cc']}</strong>{low_s}</td>"
            f"<td>{c['node_count']}</td>"
            f"<td>{pct(val)}</td>"
            f"<td>{tier_badge(tier, badge_kind)}</td>"
            f"<td class='tier-pills'>{tier_pills}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def alert_rows(alerts: list, kind: str) -> str:
    if not alerts:
        return '<tr><td colspan="4" class="no-alerts">✓ All locations within normal range</td></tr>'
    rows = []
    for c in alerts:
        val = c["mean_perf"] if kind == "perf" else c["mean_load"]
        if kind == "perf":
            # critical: offline tier (<=0.10); degraded: low tier (0.10–0.50)
            sev     = "Critical" if (val is not None and val <= 0.10) else "Degraded"
            sev_cls = "critical" if sev == "Critical" else "degraded"
        else:
            # overloaded: predominantly high load (>=0.75); elevated: mixed (>=0.25)
            sev     = "Overloaded" if (val is not None and val >= 0.75) else "Elevated"
            sev_cls = "critical" if sev == "Overloaded" else "degraded"
        low_s = ' <span class="badge-low-sample">low sample</span>' if c["low_sample"] else ""
        rows.append(
            f"<tr>"
            f"<td>{flag(c['cc'])} <strong>{c['cc']}</strong>{low_s}</td>"
            f"<td>{c['node_count']}</td>"
            f"<td>{pct(val)}</td>"
            f"<td><span class='alert-badge alert-{sev_cls}'>{sev}</span></td>"
            f"</tr>"
        )
    return "\n".join(rows)


def gauge_color(v: float, invert: bool = False) -> str:
    """Return hex colour. invert=True for load (high value = bad)."""
    if invert:
        if v < 0.25: return "#00d26a"
        if v < 0.75: return "#f5a623"
        return "#e05b2b"
    else:
        if v > 0.75: return "#00d26a"
        if v > 0.50: return "#f5a623"
        return "#e05b2b"


def build_histogram(perf_scores: list) -> str:
    """Build histogram bars from individual node performance scores."""
    buckets = [0] * 10
    for ps in perf_scores:
        idx = min(int(ps * 10), 9)
        buckets[idx] += 1
    bucket_max = max(buckets) or 1
    bars = ""
    for i, b in enumerate(buckets):
        h     = max(4, int(b / bucket_max * 80))
        label = f"{i * 10}–{(i + 1) * 10}%"
        bars += (
            f'<div class="histo-bar" title="{label}: {b} nodes" '
            f'style="height:{h}px"></div>'
        )
    return bars


def render_html(data: dict) -> str:
    mp = data["mean_perf"] or 0
    ml = data["mean_load"] or 0
    pt = data["perf_tiers"]
    lt = data["load_tiers"]

    perf_color = gauge_color(mp, invert=False)
    load_color = gauge_color(ml, invert=True)

    histo_bars        = build_histogram(data["perf_scores"])
    perf_country_rows = country_table_rows(data["countries"], "mean_perf")
    load_country_rows = country_table_rows(data["countries"], "mean_load")
    p_alerts          = alert_rows(data["perf_alerts"], "perf")
    l_alerts          = alert_rows(data["load_alerts"], "load")

    lt_total = max(sum(lt.values()), 1)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nym Network Metrics</title>
<style>
/* ── Design tokens ─────────────────────────────── */
:root {{
  --bg:         #111413;
  --bg2:        #1c201f;
  --bg3:        #252a29;
  --border:     #2e3432;
  --text:       #e8edeb;
  --text-muted: #8a9693;
  --accent:     #00d26a;
  --accent-dim: #00a854;
  --font-sans:  'Inter', system-ui, sans-serif;
  --font-mono:  'JetBrains Mono', 'Fira Mono', monospace;
  --radius:     6px;
}}
[data-theme="light"] {{
  --bg:         #f5f7f6;
  --bg2:        #ffffff;
  --bg3:        #eef1f0;
  --border:     #d4dbd8;
  --text:       #111413;
  --text-muted: #5a6662;
  --accent:     #009950;
  --accent-dim: #00d26a;
}}

/* ── Reset ─────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.6;
  min-height: 100vh;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* ── Header ────────────────────────────────────── */
.header {{
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 14px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 10;
}}
.header-left {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.logo {{
  font-family: var(--font-mono);
  font-size: 18px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: -0.5px;
}}
.header-title {{
  font-size: 13px;
  color: var(--text-muted);
  border-left: 1px solid var(--border);
  padding-left: 12px;
}}
.header-right {{
  display: flex;
  align-items: center;
  gap: 16px;
}}
.last-updated {{
  font-size: 12px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}}
.theme-toggle {{
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text);
  cursor: pointer;
  padding: 5px 10px;
  border-radius: var(--radius);
  font-size: 13px;
  transition: background 0.15s;
}}
.theme-toggle:hover {{ background: var(--border); }}

/* ── Summary bar ───────────────────────────────── */
.summary-bar {{
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 10px 32px;
  display: flex;
  align-items: center;
  gap: 32px;
}}
.summary-stat {{
  display: flex;
  align-items: baseline;
  gap: 8px;
}}
.summary-value {{
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  color: var(--accent);
}}
.summary-label {{
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.summary-divider {{
  width: 1px;
  height: 24px;
  background: var(--border);
}}

/* ── Main layout ───────────────────────────────── */
.main {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 32px 24px;
  display: flex;
  flex-direction: column;
  gap: 32px;
}}

/* ── Sections ──────────────────────────────────── */
.section {{
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}}
.section-header {{
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
}}
.section-header h2 {{
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-muted);
}}
.section-number {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--accent);
  background: var(--bg3);
  border: 1px solid var(--border);
  padding: 2px 6px;
  border-radius: 3px;
}}
.section-body {{ padding: 24px; }}

/* ── Paired layout ─────────────────────────────── */
.section-pair {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
}}
@media (max-width: 800px) {{
  .section-pair {{ grid-template-columns: 1fr; }}
  .summary-bar {{ flex-wrap: wrap; gap: 16px; padding: 12px 16px; }}
  .header {{ padding: 12px 16px; }}
}}

/* ── Headline metric ───────────────────────────── */
.headline-metric {{
  display: flex;
  align-items: flex-end;
  gap: 16px;
  margin-bottom: 20px;
}}
.big-number {{
  font-family: var(--font-mono);
  font-size: 52px;
  font-weight: 700;
  line-height: 1;
}}
.metric-meta {{
  padding-bottom: 6px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.8;
}}
.metric-meta strong {{
  display: block;
  font-size: 13px;
  color: var(--text);
}}

/* ── Pills ─────────────────────────────────────── */
.pill-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 20px;
}}
.pill {{
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 20px;
  border: 1px solid transparent;
}}
.pill-high    {{ background:#0a2a1a; color:#00d26a; border-color:#00d26a44; }}
.pill-medium  {{ background:#2a1e00; color:#f5a623; border-color:#f5a62344; }}
.pill-low     {{ background:#2a1200; color:#e05b2b; border-color:#e05b2b44; }}
.pill-offline {{ background:#1e1e1e; color:#666;    border-color:#44444444; }}
.pill-no-data {{ background:#1a1a1a; color:#555;    border-color:#33333344; }}
[data-theme="light"] .pill-high    {{ background:#e6faf0; color:#009950; border-color:#009950; }}
[data-theme="light"] .pill-medium  {{ background:#fff8e6; color:#c47d00; border-color:#f5a623; }}
[data-theme="light"] .pill-low     {{ background:#fef0e8; color:#c04010; border-color:#e05b2b; }}
[data-theme="light"] .pill-offline {{ background:#f0f0f0; color:#666;    border-color:#ccc; }}
[data-theme="light"] .pill-no-data {{ background:#f0f0f0; color:#888;    border-color:#ccc; }}

/* ── Histogram ─────────────────────────────────── */
.histogram {{
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 88px;
  margin-top: 4px;
}}
.histo-bar {{
  flex: 1;
  background: var(--accent);
  opacity: 0.65;
  border-radius: 2px 2px 0 0;
  min-height: 4px;
  transition: opacity 0.15s;
  cursor: default;
}}
.histo-bar:hover {{ opacity: 1; }}
.histo-label {{
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  margin-top: 4px;
}}

/* ── Load bar ──────────────────────────────────── */
.load-bar-wrap {{ margin-top: 8px; }}
.load-bar {{
  display: flex;
  height: 12px;
  border-radius: 6px;
  overflow: hidden;
  background: var(--bg3);
}}
.load-bar-seg {{ transition: width 0.3s; }}
.load-bar-legend {{
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  font-family: var(--font-mono);
  margin-top: 6px;
}}

/* ── Tables ─────────────────────────────────────── */
.table-wrap {{ overflow-x: auto; }}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}
th {{
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-weight: 500;
}}
td {{
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: var(--bg3); }}

/* ── Badges ─────────────────────────────────────── */
.badge {{
  display: inline-block;
  font-size: 10px;
  font-family: var(--font-mono);
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
}}
.badge-low-sample {{
  font-size: 10px;
  color: var(--text-muted);
  background: var(--bg3);
  border: 1px solid var(--border);
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 4px;
  font-family: var(--font-mono);
}}
.tier-pills {{ display: flex; gap: 4px; flex-wrap: wrap; }}
.tier-pill {{
  font-size: 10px;
  font-family: var(--font-mono);
  padding: 1px 5px;
  border-radius: 3px;
}}
.tier-pill.tier-high    {{ background:#0a2a1a; color:#00d26a; }}
.tier-pill.tier-medium  {{ background:#2a1e00; color:#f5a623; }}
.tier-pill.tier-low     {{ background:#2a1200; color:#e05b2b; }}
.tier-pill.tier-offline {{ background:#222;    color:#666; }}
[data-theme="light"] .tier-pill.tier-high    {{ background:#e6faf0; color:#009950; }}
[data-theme="light"] .tier-pill.tier-medium  {{ background:#fff8e6; color:#c47d00; }}
[data-theme="light"] .tier-pill.tier-low     {{ background:#fef0e8; color:#c04010; }}
[data-theme="light"] .tier-pill.tier-offline {{ background:#f0f0f0; color:#888; }}

/* ── Alert badges ───────────────────────────────── */
.alert-badge {{
  font-size: 11px;
  font-family: var(--font-mono);
  padding: 2px 8px;
  border-radius: 3px;
  font-weight: 600;
}}
.alert-critical {{ background:#2a0a00; color:#ff5533; border:1px solid #ff553344; }}
.alert-degraded {{ background:#2a1e00; color:#f5a623; border:1px solid #f5a62344; }}
[data-theme="light"] .alert-critical {{ background:#fff0ec; color:#cc2200; border-color:#e05b2b; }}
[data-theme="light"] .alert-degraded {{ background:#fff8e6; color:#c47d00; border-color:#f5a623; }}

.no-alerts {{
  color: var(--accent);
  font-size: 13px;
  padding: 20px 12px;
  text-align: center;
}}

/* ── Footer ─────────────────────────────────────── */
.footer {{
  text-align: center;
  color: var(--text-muted);
  font-size: 11px;
  font-family: var(--font-mono);
  padding: 24px;
  border-top: 1px solid var(--border);
}}
</style>
</head>
<body>

<header class="header">
  <div class="header-left">
    <span class="logo">NYM</span>
    <span class="header-title">Network Metrics</span>
  </div>
  <div class="header-right">
    <span class="last-updated">Updated: {data['generated_utc']}</span>
    <button class="theme-toggle" onclick="toggleTheme()">☀ Light</button>
  </div>
</header>

<div class="summary-bar">
  <div class="summary-stat">
    <span class="summary-value">{data['total']}</span>
    <span class="summary-label">Total Nodes</span>
  </div>
  <div class="summary-divider"></div>
  <div class="summary-stat">
    <span class="summary-value">{data['location_count']}</span>
    <span class="summary-label">Locations</span>
  </div>
  <div class="summary-divider"></div>
  <div class="summary-stat">
    <span class="summary-value">{data['probe_count']}</span>
    <span class="summary-label">Nodes Measured</span>
  </div>
  <div class="summary-divider"></div>
  <div class="summary-stat">
    <span class="summary-value">{data['no_probe']}</span>
    <span class="summary-label">No Probe Data</span>
  </div>
</div>

<main class="main">

  <!-- §1 + §2  Network Performance & Load -->
  <div class="section-pair">

    <!-- §1 Network Performance -->
    <div class="section">
      <div class="section-header">
        <span class="section-number">01</span>
        <h2>Network Performance</h2>
      </div>
      <div class="section-body">
        <div class="headline-metric">
          <div class="big-number" style="color:{perf_color}">{pct(mp)}</div>
          <div class="metric-meta">
            <strong>Mean performance score</strong>
            Computed across {data['probe_count']} measurable nodes<br>
            Formula: mixnet × (download_speed × ping_v4)
          </div>
        </div>
        <div class="pill-row">
          <span class="pill pill-high">▲ High: {pt.get('high', 0)}</span>
          <span class="pill pill-medium">◆ Medium: {pt.get('medium', 0)}</span>
          <span class="pill pill-low">▼ Low: {pt.get('low', 0)}</span>
          <span class="pill pill-offline">✕ Offline: {pt.get('offline', 0)}</span>
          <span class="pill pill-no-data">? No data: {data['no_probe']}</span>
        </div>
        <div class="histogram">{histo_bars}</div>
        <div class="histo-label"><span>0%</span><span>50%</span><span>100%</span></div>
      </div>
    </div>

    <!-- §2 Network Load -->
    <div class="section">
      <div class="section-header">
        <span class="section-number">02</span>
        <h2>Network Load</h2>
      </div>
      <div class="section-body">
        <div class="headline-metric">
          <div class="big-number" style="color:{load_color}">{pct(ml)}</div>
          <div class="metric-meta">
            <strong>Mean load score</strong>
            0% = all nodes low load<br>
            100% = all nodes high load
          </div>
        </div>
        <div class="pill-row">
          <span class="pill pill-high">✓ Low load: {lt.get('low', 0)}</span>
          <span class="pill pill-low">⚠ High load: {lt.get('high', 0)}</span>
          <span class="pill pill-no-data">◆ Medium tier appears in per-country aggregates below</span>
        </div>
        <div class="load-bar-wrap">
          <div class="load-bar">
            <div class="load-bar-seg" style="width:{lt.get('low',0)/lt_total*100:.1f}%;background:#00d26a"></div>
            <div class="load-bar-seg" style="width:{lt.get('high',0)/lt_total*100:.1f}%;background:#e05b2b"></div>
          </div>
          <div class="load-bar-legend">
            <span style="color:#00d26a">Low load ({lt.get('low',0)} nodes)</span>
            <span style="color:#e05b2b">High load ({lt.get('high',0)} nodes)</span>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- §3 + §4  Locations Performance & Load -->
  <div class="section-pair">

    <div class="section">
      <div class="section-header">
        <span class="section-number">03</span>
        <h2>Locations — Performance</h2>
      </div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Country</th><th>Nodes</th><th>Score</th><th>Tier</th><th>Distribution</th>
            </tr></thead>
            <tbody>{perf_country_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="section-number">04</span>
        <h2>Locations — Load</h2>
      </div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Country</th><th>Nodes</th><th>Load</th><th>Tier</th><th>Distribution</th>
            </tr></thead>
            <tbody>{load_country_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

  </div>

  <!-- §5 + §6  Alerts -->
  <div class="section-pair">

    <div class="section">
      <div class="section-header">
        <span class="section-number">05</span>
        <h2>Performance Alerts</h2>
      </div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Country</th><th>Nodes</th><th>Score</th><th>Status</th>
            </tr></thead>
            <tbody>{p_alerts}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header">
        <span class="section-number">06</span>
        <h2>Load Alerts</h2>
      </div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Country</th><th>Nodes</th><th>Load</th><th>Status</th>
            </tr></thead>
            <tbody>{l_alerts}</tbody>
          </table>
        </div>
      </div>
    </div>

  </div>

</main>

<footer class="footer">
  Source: {API_URL} &nbsp;·&nbsp; Nym Network Metrics &nbsp;·&nbsp; Refreshed every 5 min via cron
</footer>

<script>
function toggleTheme() {{
  const html = document.documentElement;
  const next = html.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  html.setAttribute('data-theme', next);
  document.querySelector('.theme-toggle').textContent = next === 'light' ? '🌙 Dark' : '☀ Light';
  localStorage.setItem('nym-metrics-theme', next);
}}
(function () {{
  const saved = localStorage.getItem('nym-metrics-theme');
  if (saved === 'light') {{
    document.documentElement.setAttribute('data-theme', 'light');
    document.addEventListener('DOMContentLoaded', function () {{
      const btn = document.querySelector('.theme-toggle');
      if (btn) btn.textContent = '🌙 Dark';
    }});
  }}
}})();
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}] Fetching {API_URL} ...")
    try:
        gateways = fetch_gateways()
    except Exception as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  Got {len(gateways)} gateway entries")
    data = aggregate(gateways)
    print(f"  Nodes: {data['total']}  Locations: {data['location_count']}")
    print(f"  Performance: {pct(data['mean_perf'])}  Load: {pct(data['mean_load'])}")
    print(f"  Perf alerts: {len(data['perf_alerts'])}  Load alerts: {len(data['load_alerts'])}")

    html = render_html(data)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.rename(OUTPUT_PATH)
    print(f"  Written → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
