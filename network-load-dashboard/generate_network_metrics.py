#!/usr/bin/env python3
"""
Nym Network Metrics — static HTML generator.

Writes:
  /var/www/html/network-load/index.html          — main dashboard
  /var/www/html/network-load/country/XX.html     — one page per country

Cron (every 5 minutes):
    */5 * * * * /usr/bin/python3 /opt/nym-metrics/generate_network_metrics.py >> /var/log/nym-metrics.log 2>&1
"""

import sys
import json
import sqlite3
import datetime
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from nym_metrics_common import (
    fetch_gateways, aggregate, mean,
    perf_tier, load_tier_from_score,
    API_URL, DB_PATH, WEB_ROOT,
    HARBOURMASTER_URL, SPECTREDAO_URL,
)

OUTPUT_INDEX   = Path(WEB_ROOT) / "index.html"
OUTPUT_COUNTRY = Path(WEB_ROOT) / "country"


# ── History from SQLite ───────────────────────────────────────────────────────

def load_global_history() -> dict:
    """Returns dict of lists ready for Chart.js: labels, nodes, locations, perf, load."""
    db = Path(DB_PATH)
    if not db.exists():
        return None
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ts, node_count, loc_count, mean_perf, mean_load "
            "FROM global_snapshots "
            "ORDER BY ts ASC"
        ).fetchall()
    if not rows:
        return None
    return {
        "labels":    [r[0][:16].replace("T", " ") for r in rows],  # "YYYY-MM-DD HH:MM"
        "nodes":     [r[1] for r in rows],
        "locations": [r[2] for r in rows],
        "perf":      [round(r[3] * 100, 1) if r[3] is not None else None for r in rows],
        "load":      [round(r[4] * 100, 1) if r[4] is not None else None for r in rows],
    }


def load_country_history(cc: str) -> dict:
    """Returns history for a single country."""
    db = Path(DB_PATH)
    if not db.exists():
        return None
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ts, node_count, mean_perf, mean_load "
            "FROM country_snapshots "
            "WHERE cc = ? ORDER BY ts ASC",
            (cc,)
        ).fetchall()
    if not rows:
        return None
    return {
        "labels":    [r[0][:16].replace("T", " ") for r in rows],
        "nodes":     [r[1] for r in rows],
        "perf":      [round(r[2] * 100, 1) if r[2] is not None else None for r in rows],
        "load":      [round(r[3] * 100, 1) if r[3] is not None else None for r in rows],
    }


# ── Shared rendering helpers ──────────────────────────────────────────────────

def pct(v) -> str:
    if v is None: return "—"
    return f"{v * 100:.1f}%"


def flag(cc: str) -> str:
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
            "low":     ("#00d26a", "#0a2a1a"),
            "medium":  ("#f5a623", "#2a1e00"),
            "high":    ("#e05b2b", "#2a1200"),
            "unknown": ("#555",    "#1a1a1a"),
        },
    }
    col = colors.get(kind, colors["perf"]).get(tier, ("#555", "#1a1a1a"))
    return (f'<span class="badge" style="background:{col[1]};color:{col[0]};'
            f'border:1px solid {col[0]}44">{tier}</span>')


def load_badge(load_str: str) -> str:
    m = {"low": "low", "medium": "medium", "high": "high"}
    return tier_badge(m.get(load_str, "unknown"), "load")


def country_table_rows(countries: list, sort_key: str, with_link: bool = True) -> str:
    rows = []
    is_perf    = sort_key == "mean_perf"
    badge_kind = "perf" if is_perf else "load"
    pill_order = ["high", "medium", "low", "offline"] if is_perf else ["low", "high"]

    for c in sorted(countries, key=lambda x: -(x[sort_key] or 0)):
        val       = c["mean_perf"] if is_perf else c["mean_load"]
        tier      = c["perf_tier"] if is_perf else c["load_tier"]
        low_s     = ' <span class="badge-low-sample">low sample</span>' if c["low_sample"] else ""
        tiers_src = c["perf_tiers"] if is_perf else c["load_tiers"]
        tier_pills = " ".join(
            f'<span class="tier-pill tier-{k}">{k[0].upper()}:{tiers_src.get(k,0)}</span>'
            for k in pill_order if tiers_src.get(k, 0) > 0
        )
        cc_cell = f'{flag(c["cc"])} <strong>{c["cc"]}</strong>{low_s}'
        if with_link:
            cc_cell = f'<a href="../country/{c["cc"]}.html">{cc_cell}</a>'
        rows.append(
            f"<tr>"
            f"<td>{cc_cell}</td>"
            f"<td>{c['node_count']}</td>"
            f"<td>{pct(val)}</td>"
            f"<td>{tier_badge(tier, badge_kind)}</td>"
            f"<td class='tier-pills'>{tier_pills}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def alert_rows(alerts: list, kind: str, from_country: bool = False) -> str:
    if not alerts:
        return '<tr><td colspan="4" class="no-alerts">✓ All locations within normal range</td></tr>'
    rows = []
    prefix = "../" if from_country else ""
    for c in alerts:
        val = c["mean_perf"] if kind == "perf" else c["mean_load"]
        if kind == "perf":
            sev     = "Critical" if (val is not None and val <= 0.10) else "Degraded"
            sev_cls = "critical" if sev == "Critical" else "degraded"
        else:
            sev     = "Overloaded" if (val is not None and val >= 0.75) else "Elevated"
            sev_cls = "critical" if sev == "Overloaded" else "degraded"
        low_s = ' <span class="badge-low-sample">low sample</span>' if c["low_sample"] else ""
        rows.append(
            f"<tr>"
            f"<td><a href='{prefix}country/{c['cc']}.html'>"
            f"{flag(c['cc'])} <strong>{c['cc']}</strong></a>{low_s}</td>"
            f"<td>{c['node_count']}</td>"
            f"<td>{pct(val)}</td>"
            f"<td><span class='alert-badge alert-{sev_cls}'>{sev}</span></td>"
            f"</tr>"
        )
    return "\n".join(rows)


def build_histogram(perf_scores: list) -> str:
    buckets = [0] * 10
    for ps in perf_scores:
        idx = min(int(ps * 10), 9)
        buckets[idx] += 1
    bucket_max = max(buckets) or 1
    bars = ""
    for i, b in enumerate(buckets):
        h     = max(4, int(b / bucket_max * 80))
        label = f"{i * 10}–{(i + 1) * 10}%: {b} nodes"
        bars += f'<div class="histo-bar" title="{label}" style="height:{h}px"></div>'
    return bars


def gauge_color(v: float, invert: bool = False) -> str:
    if invert:
        if v < 0.25: return "#00d26a"
        if v < 0.75: return "#f5a623"
        return "#e05b2b"
    else:
        if v > 0.75: return "#00d26a"
        if v > 0.50: return "#f5a623"
        return "#e05b2b"


def history_chart_js(hist: dict, chart_id: str, show_locations: bool = True) -> str:
    """Render a Chart.js canvas + inline script for the history graph."""
    if hist is None:
        return (
            f'<div class="chart-empty">'
            f'No historical data yet — starts accumulating after first hourly snapshot.'
            f'</div>'
        )

    labels_json    = json.dumps(hist["labels"])
    nodes_json     = json.dumps(hist["nodes"])
    perf_json      = json.dumps(hist["perf"])
    load_json      = json.dumps(hist["load"])

    datasets = f"""
        {{
            label: 'Performance %',
            data: {perf_json},
            borderColor: '#00d26a',
            backgroundColor: '#00d26a22',
            yAxisID: 'yPct',
            tension: 0.3,
            pointRadius: 2,
            fill: false,
        }},
        {{
            label: 'Load %',
            data: {load_json},
            borderColor: '#e05b2b',
            backgroundColor: '#e05b2b22',
            yAxisID: 'yPct',
            tension: 0.3,
            pointRadius: 2,
            fill: false,
        }},
        {{
            label: 'Nodes',
            data: {nodes_json},
            borderColor: '#f5a623',
            backgroundColor: '#f5a62322',
            yAxisID: 'yCount',
            tension: 0.3,
            pointRadius: 2,
            fill: false,
        }},"""

    if show_locations:
        locations_json = json.dumps(hist.get("locations", []))
        datasets += f"""
        {{
            label: 'Locations',
            data: {locations_json},
            borderColor: '#7b61ff',
            backgroundColor: '#7b61ff22',
            yAxisID: 'yCount',
            tension: 0.3,
            pointRadius: 2,
            fill: false,
        }},"""

    return f"""
<canvas id="{chart_id}" height="120"></canvas>
<script>
(function() {{
  var ctx = document.getElementById('{chart_id}');
  var isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  var gridColor  = isDark ? '#2e343244' : '#d4dbd844';
  var labelColor = isDark ? '#8a9693'   : '#5a6662';
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {labels_json},
      datasets: [{datasets}]
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{
          labels: {{ color: labelColor, boxWidth: 12, font: {{ size: 11 }} }}
        }},
        tooltip: {{
          backgroundColor: isDark ? '#1c201f' : '#ffffff',
          borderColor:     isDark ? '#2e3432' : '#d4dbd8',
          borderWidth: 1,
          titleColor: isDark ? '#e8edeb' : '#111413',
          bodyColor:  isDark ? '#8a9693' : '#5a6662',
        }}
      }},
      scales: {{
        x: {{
          ticks: {{
            color: labelColor,
            maxTicksLimit: 10,
            font: {{ size: 10 }},
            maxRotation: 30,
          }},
          grid: {{ color: gridColor }},
        }},
        yPct: {{
          type: 'linear',
          position: 'left',
          min: 0,
          max: 100,
          ticks: {{ color: labelColor, font: {{ size: 10 }}, callback: v => v + '%' }},
          grid: {{ color: gridColor }},
          title: {{ display: true, text: '%', color: labelColor, font: {{ size: 10 }} }},
        }},
        yCount: {{
          type: 'linear',
          position: 'right',
          min: 0,
          ticks: {{ color: labelColor, font: {{ size: 10 }} }},
          grid: {{ drawOnChartArea: false }},
          title: {{ display: true, text: 'count', color: labelColor, font: {{ size: 10 }} }},
        }},
      }}
    }}
  }});
}})();
</script>"""


# ── Shared CSS ────────────────────────────────────────────────────────────────

SHARED_CSS = """
:root {
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
}
[data-theme="light"] {
  --bg:         #f5f7f6;
  --bg2:        #ffffff;
  --bg3:        #eef1f0;
  --border:     #d4dbd8;
  --text:       #111413;
  --text-muted: #5a6662;
  --accent:     #009950;
  --accent-dim: #00d26a;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.6;
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.header {
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 14px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky; top: 0; z-index: 10;
}
.header-left { display: flex; align-items: center; gap: 12px; }
.logo { font-family: var(--font-mono); font-size: 18px; font-weight: 700; color: var(--accent); }
.header-title {
  font-size: 13px; color: var(--text-muted);
  border-left: 1px solid var(--border); padding-left: 12px;
}
.header-right { display: flex; align-items: center; gap: 16px; }
.last-updated { font-size: 12px; color: var(--text-muted); font-family: var(--font-mono); }
.theme-toggle {
  background: var(--bg3); border: 1px solid var(--border); color: var(--text);
  cursor: pointer; padding: 5px 10px; border-radius: var(--radius);
  font-size: 13px; transition: background 0.15s;
}
.theme-toggle:hover { background: var(--border); }
.back-link { font-size: 13px; color: var(--text-muted); }
.back-link a { color: var(--accent); }

.summary-bar {
  background: var(--bg2); border-bottom: 1px solid var(--border);
  padding: 10px 32px; display: flex; align-items: center; gap: 32px; flex-wrap: wrap;
}
.summary-stat { display: flex; align-items: baseline; gap: 8px; }
.summary-value { font-family: var(--font-mono); font-size: 22px; font-weight: 700; color: var(--accent); }
.summary-label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
.summary-divider { width: 1px; height: 24px; background: var(--border); }

.main { max-width: 1200px; margin: 0 auto; padding: 32px 24px; display: flex; flex-direction: column; gap: 32px; }

.section { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.section-header {
  padding: 16px 24px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
}
.section-header h2 {
  font-size: 14px; font-weight: 600; letter-spacing: 0.03em;
  text-transform: uppercase; color: var(--text-muted);
}
.section-number {
  font-family: var(--font-mono); font-size: 11px; color: var(--accent);
  background: var(--bg3); border: 1px solid var(--border);
  padding: 2px 6px; border-radius: 3px;
}
.section-body { padding: 24px; }

.section-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 800px) {
  .section-pair { grid-template-columns: 1fr; }
  .header { padding: 12px 16px; }
  .summary-bar { padding: 12px 16px; gap: 16px; }
  .main { padding: 16px; }
}

.headline-metric { display: flex; align-items: flex-end; gap: 16px; margin-bottom: 20px; }
.big-number { font-family: var(--font-mono); font-size: 52px; font-weight: 700; line-height: 1; }
.metric-meta { padding-bottom: 6px; color: var(--text-muted); font-size: 12px; line-height: 1.8; }
.metric-meta strong { display: block; font-size: 13px; color: var(--text); }

.pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
.pill { font-family: var(--font-mono); font-size: 11px; padding: 3px 9px; border-radius: 20px; border: 1px solid transparent; }
.pill-high    { background:#0a2a1a; color:#00d26a; border-color:#00d26a44; }
.pill-medium  { background:#2a1e00; color:#f5a623; border-color:#f5a62344; }
.pill-low     { background:#2a1200; color:#e05b2b; border-color:#e05b2b44; }
.pill-offline { background:#1e1e1e; color:#666;    border-color:#44444444; }
.pill-no-data { background:#1a1a1a; color:#555;    border-color:#33333344; }
[data-theme="light"] .pill-high    { background:#e6faf0; color:#009950; border-color:#009950; }
[data-theme="light"] .pill-medium  { background:#fff8e6; color:#c47d00; border-color:#f5a623; }
[data-theme="light"] .pill-low     { background:#fef0e8; color:#c04010; border-color:#e05b2b; }
[data-theme="light"] .pill-offline { background:#f0f0f0; color:#666;    border-color:#ccc; }
[data-theme="light"] .pill-no-data { background:#f0f0f0; color:#888;    border-color:#ccc; }

.histogram { display: flex; align-items: flex-end; gap: 3px; height: 88px; margin-top: 4px; }
.histo-bar {
  flex: 1; background: var(--accent); opacity: 0.65;
  border-radius: 2px 2px 0 0; min-height: 4px;
  transition: opacity 0.15s; cursor: default;
}
.histo-bar:hover { opacity: 1; }
.histo-label {
  display: flex; justify-content: space-between;
  font-size: 10px; color: var(--text-muted); font-family: var(--font-mono); margin-top: 4px;
}

.load-bar-wrap { margin-top: 8px; }
.load-bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; background: var(--bg3); }
.load-bar-seg { transition: width 0.3s; }
.load-bar-legend { display: flex; justify-content: space-between; font-size: 10px; font-family: var(--font-mono); margin-top: 6px; }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-muted);
  padding: 8px 12px; border-bottom: 1px solid var(--border); font-weight: 500;
}
td { padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--bg3); }
td a { color: inherit; }
td a:hover { color: var(--accent); }

.badge {
  display: inline-block; font-size: 10px; font-family: var(--font-mono);
  padding: 1px 6px; border-radius: 3px; font-weight: 600;
}
.badge-low-sample {
  font-size: 10px; color: var(--text-muted); background: var(--bg3);
  border: 1px solid var(--border); padding: 1px 5px; border-radius: 3px;
  margin-left: 4px; font-family: var(--font-mono);
}
.tier-pills { display: flex; gap: 4px; flex-wrap: wrap; }
.tier-pill { font-size: 10px; font-family: var(--font-mono); padding: 1px 5px; border-radius: 3px; }
.tier-pill.tier-high    { background:#0a2a1a; color:#00d26a; }
.tier-pill.tier-medium  { background:#2a1e00; color:#f5a623; }
.tier-pill.tier-low     { background:#2a1200; color:#e05b2b; }
.tier-pill.tier-offline { background:#222;    color:#666; }
[data-theme="light"] .tier-pill.tier-high    { background:#e6faf0; color:#009950; }
[data-theme="light"] .tier-pill.tier-medium  { background:#fff8e6; color:#c47d00; }
[data-theme="light"] .tier-pill.tier-low     { background:#fef0e8; color:#c04010; }
[data-theme="light"] .tier-pill.tier-offline { background:#f0f0f0; color:#888; }

.alert-badge { font-size: 11px; font-family: var(--font-mono); padding: 2px 8px; border-radius: 3px; font-weight: 600; }
.alert-critical { background:#2a0a00; color:#ff5533; border:1px solid #ff553344; }
.alert-degraded { background:#2a1e00; color:#f5a623; border:1px solid #f5a62344; }
[data-theme="light"] .alert-critical { background:#fff0ec; color:#cc2200; border-color:#e05b2b; }
[data-theme="light"] .alert-degraded { background:#fff8e6; color:#c47d00; border-color:#f5a623; }

.no-alerts { color: var(--accent); font-size: 13px; padding: 20px 12px; text-align: center; }

.chart-wrap { padding: 20px 24px; }
.chart-empty {
  padding: 24px; text-align: center; color: var(--text-muted);
  font-size: 13px; font-family: var(--font-mono);
  border: 1px dashed var(--border); border-radius: var(--radius); margin: 20px 24px;
}

.node-links { display: flex; gap: 8px; flex-wrap: wrap; }
.node-link {
  font-size: 10px; font-family: var(--font-mono);
  padding: 2px 7px; border-radius: 3px;
  background: var(--bg3); border: 1px solid var(--border);
  color: var(--accent);
}
.node-link:hover { background: var(--border); }
.ikey { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }

.footer {
  text-align: center; color: var(--text-muted);
  font-size: 11px; font-family: var(--font-mono);
  padding: 24px; border-top: 1px solid var(--border);
}
"""

THEME_JS = """
function toggleTheme() {
  var html = document.documentElement;
  var next = html.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  html.setAttribute('data-theme', next);
  document.querySelector('.theme-toggle').textContent = next === 'light' ? '🌙 Dark' : '☀ Light';
  localStorage.setItem('nym-metrics-theme', next);
}
(function () {
  var saved = localStorage.getItem('nym-metrics-theme');
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    document.addEventListener('DOMContentLoaded', function () {
      var btn = document.querySelector('.theme-toggle');
      if (btn) btn.textContent = '🌙 Dark';
    });
  }
})();
"""

CHARTJS_CDN = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'


def html_head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{CHARTJS_CDN}
<style>{SHARED_CSS}</style>
</head>
<body>"""


def html_tail() -> str:
    return f"<script>{THEME_JS}</script>\n</body>\n</html>"


def header_html(title: str, subtitle: str, generated: str, back: str = None) -> str:
    back_html = f'<span class="back-link"><a href="{back}">← Network Metrics</a></span>' if back else ""
    return f"""
<header class="header">
  <div class="header-left">
    <span class="logo">NYM</span>
    <span class="header-title">{title}</span>
    {back_html}
  </div>
  <div class="header-right">
    <span class="last-updated">Updated: {generated}</span>
    <button class="theme-toggle" onclick="toggleTheme()">☀ Light</button>
  </div>
</header>"""


# ── Index page ────────────────────────────────────────────────────────────────

def render_index(data: dict, hist: dict, generated: str) -> str:
    mp = data["mean_perf"] or 0
    ml = data["mean_load"] or 0
    pt = data["perf_tiers"]
    lt = data["load_tiers"]
    lt_total = max(sum(lt.values()), 1)

    perf_color = gauge_color(mp, invert=False)
    load_color = gauge_color(ml, invert=True)
    histo_bars = build_histogram(data["perf_scores"])

    perf_rows = country_table_rows(data["countries"], "mean_perf")
    load_rows = country_table_rows(data["countries"], "mean_load")
    p_alerts  = alert_rows(data["perf_alerts"], "perf")
    l_alerts  = alert_rows(data["load_alerts"], "load")

    history_section = f"""
  <div class="section">
    <div class="section-header">
      <span class="section-number">00</span>
      <h2>30-Day History</h2>
    </div>
    <div class="chart-wrap">
      {history_chart_js(hist, "globalChart", show_locations=True)}
    </div>
  </div>"""

    return (
        html_head("Nym Network Metrics")
        + header_html("Network Metrics", "", generated)
        + f"""
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

{history_section}

  <div class="section-pair">
    <div class="section">
      <div class="section-header"><span class="section-number">01</span><h2>Network Performance</h2></div>
      <div class="section-body">
        <div class="headline-metric">
          <div class="big-number" style="color:{perf_color}">{pct(mp)}</div>
          <div class="metric-meta">
            <strong>Mean performance score</strong>
            Across {data['probe_count']} measurable nodes<br>
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

    <div class="section">
      <div class="section-header"><span class="section-number">02</span><h2>Network Load</h2></div>
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
          <span class="pill pill-no-data">◆ Medium tier in per-country aggregates</span>
        </div>
        <div class="load-bar-wrap">
          <div class="load-bar">
            <div class="load-bar-seg" style="width:{lt.get('low',0)/lt_total*100:.1f}%;background:#00d26a"></div>
            <div class="load-bar-seg" style="width:{lt.get('high',0)/lt_total*100:.1f}%;background:#e05b2b"></div>
          </div>
          <div class="load-bar-legend">
            <span style="color:#00d26a">Low ({lt.get('low',0)} nodes)</span>
            <span style="color:#e05b2b">High ({lt.get('high',0)} nodes)</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="section-pair">
    <div class="section">
      <div class="section-header"><span class="section-number">03</span><h2>Locations — Performance</h2></div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Country</th><th>Nodes</th><th>Score</th><th>Tier</th><th>Distribution</th></tr></thead>
            <tbody>{perf_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="section">
      <div class="section-header"><span class="section-number">04</span><h2>Locations — Load</h2></div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Country</th><th>Nodes</th><th>Load</th><th>Tier</th><th>Distribution</th></tr></thead>
            <tbody>{load_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="section-pair">
    <div class="section">
      <div class="section-header"><span class="section-number">05</span><h2>Performance Alerts</h2></div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Country</th><th>Nodes</th><th>Score</th><th>Status</th></tr></thead>
            <tbody>{p_alerts}</tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="section">
      <div class="section-header"><span class="section-number">06</span><h2>Load Alerts</h2></div>
      <div class="section-body" style="padding:0">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Country</th><th>Nodes</th><th>Load</th><th>Status</th></tr></thead>
            <tbody>{l_alerts}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

</main>
<footer class="footer">
  Source: {API_URL} &nbsp;·&nbsp; Nym Network Metrics &nbsp;·&nbsp; HTML refreshed every 5 min · History recorded hourly
</footer>"""
        + html_tail()
    )


# ── Country page ──────────────────────────────────────────────────────────────

def render_country(country: dict, hist: dict, generated: str) -> str:
    cc        = country["cc"]
    nodes     = country["nodes"]
    cp        = country["mean_perf"]
    cl        = country["mean_load"]
    lt        = country["load_tiers"]
    lt_total  = max(sum(lt.values()), 1)

    perf_color = gauge_color(cp or 0, invert=False)
    load_color = gauge_color(cl or 0, invert=True)

    # node table rows
    node_rows = []
    for n in nodes:
        ikey      = n["identity_key"]
        ikey_short = ikey[:20] + "…" if len(ikey) > 20 else ikey
        hm_url    = HARBOURMASTER_URL.format(identity_key=ikey)
        sd_url    = SPECTREDAO_URL.format(identity_key=ikey)
        uptime    = f"{n['uptime']*100:.0f}%" if n.get("uptime") is not None else "—"
        perf_str  = pct(n["perf_score"]) if n["has_probe"] else "no probe"
        node_rows.append(
            f"<tr>"
            f"<td><span class='ikey' title='{ikey}'>{ikey_short}</span></td>"
            f"<td>{n['name']}</td>"
            f"<td>{n.get('city') or '—'}</td>"
            f"<td>{perf_str}</td>"
            f"<td>{tier_badge(n['perf_tier'], 'perf')}</td>"
            f"<td>{load_badge(n['load_str'])}</td>"
            f"<td>{uptime}</td>"
            f"<td class='node-links'>"
            f"<a class='node-link' href='{hm_url}' target='_blank' rel='noopener'>Harbourmaster</a>"
            f"<a class='node-link' href='{sd_url}' target='_blank' rel='noopener'>SpectrDAO</a>"
            f"</td>"
            f"</tr>"
        )
    node_rows_html = "\n".join(node_rows)

    low_s = ' <span class="badge-low-sample">low sample</span>' if country["low_sample"] else ""

    return (
        html_head(f"Nym — {flag(cc)} {cc} Network Metrics")
        + header_html(
            f"{flag(cc)} {cc} — Gateway Metrics",
            "",
            generated,
            back="../index.html"
          )
        + f"""
<div class="summary-bar">
  <div class="summary-stat">
    <span class="summary-value">{country['node_count']}</span>
    <span class="summary-label">Nodes{low_s}</span>
  </div>
  <div class="summary-divider"></div>
  <div class="summary-stat">
    <span class="summary-value" style="color:{perf_color}">{pct(cp)}</span>
    <span class="summary-label">Mean Performance</span>
  </div>
  <div class="summary-divider"></div>
  <div class="summary-stat">
    <span class="summary-value" style="color:{load_color}">{pct(cl)}</span>
    <span class="summary-label">Mean Load</span>
  </div>
</div>

<main class="main">

  <div class="section">
    <div class="section-header">
      <span class="section-number">00</span>
      <h2>30-Day History — {cc}</h2>
    </div>
    <div class="chart-wrap">
      {history_chart_js(hist, "countryChart", show_locations=False)}
    </div>
  </div>

  <div class="section-pair">
    <div class="section">
      <div class="section-header"><span class="section-number">01</span><h2>Performance</h2></div>
      <div class="section-body">
        <div class="headline-metric">
          <div class="big-number" style="color:{perf_color}">{pct(cp)}</div>
          <div class="metric-meta">
            <strong>Mean across {len([n for n in nodes if n['has_probe']])} measurable nodes</strong>
            {country['node_count'] - len([n for n in nodes if n['has_probe']])} nodes without probe data
          </div>
        </div>
        <div class="pill-row">
          {''.join(f'<span class="pill pill-{k}">{"▲◆▼✕"[["high","medium","low","offline"].index(k)] if k in ["high","medium","low","offline"] else "?"} {k.title()}: {country["perf_tiers"].get(k,0)}</span>' for k in ["high","medium","low","offline"] if country["perf_tiers"].get(k,0) > 0)}
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><span class="section-number">02</span><h2>Load</h2></div>
      <div class="section-body">
        <div class="headline-metric">
          <div class="big-number" style="color:{load_color}">{pct(cl)}</div>
          <div class="metric-meta">
            <strong>Mean load score</strong>
            0% = all nodes low load<br>100% = all nodes high load
          </div>
        </div>
        <div class="pill-row">
          <span class="pill pill-high">✓ Low: {lt.get('low',0)}</span>
          <span class="pill pill-low">⚠ High: {lt.get('high',0)}</span>
        </div>
        <div class="load-bar-wrap">
          <div class="load-bar">
            <div class="load-bar-seg" style="width:{lt.get('low',0)/lt_total*100:.1f}%;background:#00d26a"></div>
            <div class="load-bar-seg" style="width:{lt.get('high',0)/lt_total*100:.1f}%;background:#e05b2b"></div>
          </div>
          <div class="load-bar-legend">
            <span style="color:#00d26a">Low ({lt.get('low',0)})</span>
            <span style="color:#e05b2b">High ({lt.get('high',0)})</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">
      <span class="section-number">03</span>
      <h2>Nodes in {cc}</h2>
    </div>
    <div class="section-body" style="padding:0">
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Identity Key</th><th>Name</th><th>City</th>
            <th>Performance</th><th>Perf Tier</th><th>Load</th>
            <th>Uptime 24h</th><th>Links</th>
          </tr></thead>
          <tbody>{node_rows_html}</tbody>
        </table>
      </div>
    </div>
  </div>

</main>
<footer class="footer">
  Source: {API_URL} &nbsp;·&nbsp; {flag(cc)} {cc} Gateway Metrics &nbsp;·&nbsp; <a href="../index.html">← All locations</a>
</footer>"""
        + html_tail()
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def main():
    now       = datetime.datetime.now(datetime.timezone.utc)
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now.isoformat()}] Fetching {API_URL} ...")

    try:
        gateways = fetch_gateways()
    except Exception as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  Got {len(gateways)} gateway entries")
    data = aggregate(gateways)
    print(f"  Nodes: {data['total']}  Locations: {data['location_count']}  "
          f"Perf: {pct(data['mean_perf'])}  Load: {pct(data['mean_load'])}")

    hist = load_global_history()
    print(f"  History: {len(hist['labels']) if hist else 0} global snapshots loaded")

    # write index
    index_html = render_index(data, hist, generated)
    atomic_write(OUTPUT_INDEX, index_html)
    print(f"  Written → {OUTPUT_INDEX}")

    # write country pages
    OUTPUT_COUNTRY.mkdir(parents=True, exist_ok=True)
    for country in data["countries"]:
        cc         = country["cc"]
        cc_hist    = load_country_history(cc)
        cc_html    = render_country(country, cc_hist, generated)
        cc_path    = OUTPUT_COUNTRY / f"{cc}.html"
        atomic_write(cc_path, cc_html)

    print(f"  Written → {len(data['countries'])} country pages in {OUTPUT_COUNTRY}/")


if __name__ == "__main__":
    main()
