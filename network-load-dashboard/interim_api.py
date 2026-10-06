"""
Nym Network Metrics - interim static API (v0).

Called by generate_network_metrics.py on every run. Writes static JSON files
plus an OpenAPI description and a Swagger UI page, all served by nginx:

    /api/v0/latest.json                  current network stats (same as the dashboard top bar)
    /api/v0/history.json                 global hourly snapshots
    /api/v0/countries.json               current per-country aggregates
    /api/v0/history/country/{CC}.json    per-country hourly snapshots
    /api/v0/residential.json             residential IP gateways and their stats
    /api/v0/openapi.json                 OpenAPI 3.0 description of the above
    /swagger/                            Swagger UI for the description

INTERIM: these endpoints are a stopgap until the same data is served by the
Node Status API. Field names are chosen to be proposed for NS API so that
consumers only need to change the base URL when it moves.
"""

import json
import sqlite3
from pathlib import Path

API_VERSION = "v0"
BASE_URL    = "https://load.nymte.ch"
DISCLAIMER  = (
    "Interim, unofficial API. Served as static files from load.nymte.ch and "
    "refreshed every 5 minutes. Not a Nym Technologies product API and not covered "
    "by any stability guarantee. These endpoints are planned to move to the Node "
    "Status API; when they do, field names are intended to stay the same."
)

# DB column -> API field name (global_snapshots)
GLOBAL_FIELDS = [
    ("total_nodes",           "nodes_total"),
    ("node_count",            "gateways_total"),
    ("loc_count",             "locations"),
    ("quic_bridges",          "quic_bridges"),
    ("mean_perf",             "performance"),
    ("mean_load",             "load"),
    ("residential",           "residential_ips"),
    ("residential_locations", "residential_locations"),
    ("residential_load",      "residential_load"),
    ("active_families",       "active_families"),
    ("nodes_in_families",     "nodes_in_families"),
]

RATIO_FIELDS = {"performance", "load", "residential_load"}


def _r(v, field):
    """Round ratios to 4 decimals, leave counts and nulls as they are."""
    if v is None or field not in RATIO_FIELDS:
        return v
    return round(v, 4)


def _ts(db_ts):
    return db_ts + "Z" if db_ts and not db_ts.endswith("Z") else db_ts


def _envelope(generated_iso, data):
    return {
        "api_version":   API_VERSION,
        "generated_utc": generated_iso,
        "source":        BASE_URL,
        "disclaimer":    DISCLAIMER,
        "data":          data,
    }


def _write(path, obj, atomic_write):
    atomic_write(path, json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


# ── payload builders ──────────────────────────────────────────────────────────
def build_latest(data, fam, total_nodes):
    d = {
        "nodes_total":           total_nodes,
        "gateways_total":        data["total"],
        "gateways_measured":     data["probe_count"],
        "gateways_no_probe":     data["no_probe"],
        "locations":             data["location_count"],
        "quic_bridges":          data["quic_bridges"],
        "performance":           data["mean_perf"],
        "load":                  data["mean_load"],
        "performance_tiers":     data["perf_tiers"],
        "load_tiers":            data["load_tiers"],
        "residential_ips":       data["residential"],
        "residential_locations": data["residential_locations"],
        "residential_load":      data["residential_load"],
        "active_families":       fam.get("active_families"),
        "nodes_in_families":     fam.get("nodes_in_families"),
        "gateways_in_families":  fam.get("gw_in_families"),
        "mixnodes_in_families":  fam.get("mix_in_families"),
    }
    return {k: _r(v, k) for k, v in d.items()}


def build_countries(data):
    out = []
    for c in sorted(data["countries"], key=lambda x: x["cc"]):
        out.append({
            "cc":                c["cc"],
            "gateways":          c["node_count"],
            "low_sample":        c["low_sample"],
            "performance":       _r(c["mean_perf"], "performance"),
            "load":              _r(c["mean_load"], "load"),
            "performance_tier":  c["perf_tier"],
            "load_tier":         c["load_tier"],
            "performance_tiers": c["perf_tiers"],
            "load_tiers":        c["load_tiers"],
        })
    return out


def build_residential(data):
    nodes = []
    for n in data["residential_nodes"]:
        nodes.append({
            "identity_key":     n["identity_key"],
            "name":             n["name"],
            "cc":               n.get("cc"),
            "city":             n.get("city") or None,
            "asn_name":         n.get("asn_name") or None,
            "performance":      _r(n["perf_score"], "performance") if n["has_probe"] else None,
            "performance_tier": n["perf_tier"],
            "load_tier":        n["load_str"] or None,
            "uptime_24h":       n.get("uptime"),
        })
    return {
        "residential_ips":       data["residential"],
        "residential_locations": data["residential_locations"],
        "residential_load":      _r(data["residential_load"], "residential_load"),
        "nodes":                 nodes,
    }


def build_history(db_path):
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(global_snapshots)").fetchall()}
        present = [(c, f) for c, f in GLOBAL_FIELDS if c in cols]
        rows = conn.execute(
            "SELECT ts, " + ", ".join(c for c, _ in present) +
            " FROM global_snapshots ORDER BY ts ASC"
        ).fetchall()
    fields = [f for _, f in GLOBAL_FIELDS]
    out = []
    for r in rows:
        rec = {"ts": _ts(r[0])}
        vals = dict(zip([f for _, f in present], r[1:]))
        for f in fields:                       # every row has every field, null if not recorded
            rec[f] = _r(vals.get(f), f)
        out.append(rec)
    return out


def build_country_histories(db_path):
    if not Path(db_path).exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT cc, ts, node_count, mean_perf, mean_load "
            "FROM country_snapshots ORDER BY cc, ts ASC"
        ).fetchall()
    out = {}
    for cc, ts, n, p, l in rows:
        out.setdefault(cc, []).append({
            "ts": _ts(ts), "gateways": n,
            "performance": _r(p, "performance"), "load": _r(l, "load"),
        })
    return out


# ── OpenAPI description ───────────────────────────────────────────────────────
def openapi_spec():
    ratio = {"type": "number", "nullable": True, "minimum": 0, "maximum": 1}
    count = {"type": "integer", "nullable": True}
    tiers = {"type": "object", "additionalProperties": {"type": "integer"},
             "example": {"high": 590, "medium": 5, "low": 2, "offline": 18}}

    def envelope(data_schema):
        return {"type": "object", "properties": {
            "api_version":   {"type": "string", "example": API_VERSION},
            "generated_utc": {"type": "string", "format": "date-time", "example": "2026-10-02T09:30:00Z"},
            "source":        {"type": "string", "example": BASE_URL},
            "disclaimer":    {"type": "string"},
            "data":          data_schema,
        }}

    snapshot = {"type": "object", "properties": {
        "ts":                    {"type": "string", "format": "date-time", "example": "2026-10-02T09:00:00Z"},
        "nodes_total":           dict(count, description="Active nodes in the Nym network (all roles). Null before this was recorded."),
        "gateways_total":        dict(count, description="Gateways in the dVPN directory."),
        "locations":             dict(count, description="Unique gateway countries."),
        "quic_bridges":          dict(count, description="Gateways offering a QUIC bridge transport."),
        "performance":           dict(ratio, description="Mean gateway performance, 0..1."),
        "load":                  dict(ratio, description="Mean gateway load score, 0..1 (low=0, medium=0.5, high/offline=1)."),
        "residential_ips":       dict(count, description="Gateways whose ASN is classified as residential."),
        "residential_locations": dict(count, description="Unique countries of residential gateways."),
        "residential_load":      dict(ratio, description="Mean load score of residential gateways, 0..1."),
        "active_families":       dict(count, description="Families with at least one member."),
        "nodes_in_families":     dict(count, description="Nodes registered to a family."),
    }}

    def get(summary, desc, schema, tag):
        return {"get": {"summary": summary, "description": desc, "tags": [tag],
                        "responses": {"200": {"description": "OK",
                                              "content": {"application/json": {"schema": schema}}}}}}

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Nym Network Metrics - interim API",
            "version": API_VERSION,
            "description": (
                "**" + DISCLAIMER + "**\n\n"
                "Static JSON files, no query parameters: every request returns the full file. "
                "Ratios are fractions between 0 and 1 (multiply by 100 for %). "
                "Timestamps are UTC. History is recorded hourly; fields added later are `null` "
                "for rows recorded before they existed.\n\n"
                "Underlying data comes from the Node Status API dVPN directory, the Node Status API "
                "summary, and the nym-api node-families and described-nodes endpoints."
            ),
        },
        "servers": [{"url": "/api/" + API_VERSION, "description": "this host"}],
        "tags": [{"name": "current"}, {"name": "history"}],
        "paths": {
            "/latest.json": get(
                "Current network stats",
                "The values shown in the load.nymte.ch top bar, refreshed every 5 minutes.",
                envelope({"type": "object", "properties": {
                    "nodes_total": count, "gateways_total": count,
                    "gateways_measured": count, "gateways_no_probe": count,
                    "locations": count, "quic_bridges": count, "performance": ratio, "load": ratio,
                    "performance_tiers": tiers, "load_tiers": tiers,
                    "residential_ips": count, "residential_locations": count, "residential_load": ratio,
                    "active_families": count, "nodes_in_families": count,
                    "gateways_in_families": count, "mixnodes_in_families": count,
                }}), "current"),
            "/countries.json": get(
                "Current per-country aggregates",
                "One entry per gateway country. `low_sample` is true for fewer than 3 gateways.",
                envelope({"type": "array", "items": {"type": "object", "properties": {
                    "cc": {"type": "string", "example": "DE"}, "gateways": count,
                    "low_sample": {"type": "boolean"},
                    "performance": ratio, "load": ratio,
                    "performance_tier": {"type": "string", "enum": ["high", "medium", "low", "offline", "unknown"]},
                    "load_tier": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
                    "performance_tiers": tiers, "load_tiers": tiers,
                }}}), "current"),
            "/residential.json": get(
                "Residential IP gateways",
                "Gateways whose `location.asn.kind` is `residential`, with summary stats.",
                envelope({"type": "object", "properties": {
                    "residential_ips": count, "residential_locations": count, "residential_load": ratio,
                    "nodes": {"type": "array", "items": {"type": "object", "properties": {
                        "identity_key": {"type": "string"}, "name": {"type": "string"},
                        "cc": {"type": "string"}, "city": {"type": "string", "nullable": True},
                        "asn_name": {"type": "string", "nullable": True},
                        "performance": ratio, "performance_tier": {"type": "string"},
                        "load_tier": {"type": "string", "nullable": True},
                        "uptime_24h": ratio,
                    }}},
                }}), "current"),
            "/history.json": get(
                "Global hourly history",
                "All recorded hourly snapshots, oldest first, for the full retention window.",
                envelope({"type": "array", "items": snapshot}), "history"),
            "/history/country/{cc}.json": dict(get(
                "Per-country hourly history",
                "Hourly snapshots for one gateway country. Countries with no history return 404.",
                envelope({"type": "array", "items": {"type": "object", "properties": {
                    "ts": {"type": "string", "format": "date-time"},
                    "gateways": count, "performance": ratio, "load": ratio,
                }}}), "history"), **{"parameters": [{
                    "name": "cc", "in": "path", "required": True,
                    "description": "ISO 3166-1 alpha-2 country code, uppercase",
                    "schema": {"type": "string", "example": "DE"},
                }]}),
        },
    }


SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nym Network Metrics - interim API</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui.min.css">
<style>
body{margin:0;background:#fafafa;font-family:system-ui,sans-serif}
.nym-bar{background:#111413;color:#e8edeb;padding:14px 24px;display:flex;align-items:center;gap:12px;font-family:'JetBrains Mono','Fira Mono',monospace}
.nym-bar b{color:#00d26a;font-size:18px}
.nym-bar a{color:#00d26a;text-decoration:none;margin-left:auto;font-size:13px}
.nym-warn{background:#fff8e6;border-bottom:1px solid #f5a623;color:#5a4300;padding:10px 24px;font-size:13px;line-height:1.5}
.swagger-ui .topbar{display:none}
.fallback{padding:24px;font-family:monospace}
</style></head><body>
<div class="nym-bar"><b>NYM</b><span>Network Metrics &#183; interim API v0</span><a href="/">&#8592; dashboard</a></div>
<div class="nym-warn"><strong>Disclaimer:</strong> __DISCLAIMER__</div>
<div id="swagger"></div>
<noscript><div class="fallback">
<p>Endpoints (base: __BASE__/api/v0):</p>
<ul>
<li>/latest.json</li><li>/countries.json</li><li>/residential.json</li>
<li>/history.json</li><li>/history/country/{CC}.json</li><li>/openapi.json</li>
</ul></div></noscript>
<script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.min.js"></script>
<script>
window.onload=function(){
  if(!window.SwaggerUIBundle){document.getElementById("swagger").innerHTML=
    '<div class="fallback">Swagger UI failed to load. Raw description: <a href="/api/v0/openapi.json">/api/v0/openapi.json</a></div>';return;}
  SwaggerUIBundle({url:"/api/v0/openapi.json",dom_id:"#swagger",deepLinking:true,
    docExpansion:"list",defaultModelsExpandDepth:0,tryItOutEnabled:true});
};
</script>
</body></html>
"""

API_INDEX_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="0; url=/swagger/"><title>Nym Network Metrics API</title></head>
<body><a href="/swagger/">API documentation</a></body></html>
"""


# ── entry point ───────────────────────────────────────────────────────────────
def write_all(web_root, db_path, data, fam, total_nodes, generated, atomic_write):
    """Write every interim API file. Returns the number of files written."""
    web_root = Path(web_root)
    api = web_root / "api" / API_VERSION
    n = 0

    def w(path, payload):
        nonlocal n
        _write(path, _envelope(generated, payload), atomic_write)
        n += 1

    w(api / "latest.json",      build_latest(data, fam, total_nodes))
    w(api / "countries.json",   build_countries(data))
    w(api / "residential.json", build_residential(data))
    w(api / "history.json",     build_history(db_path))
    for cc, rows in build_country_histories(db_path).items():
        w(api / "history" / "country" / (cc + ".json"), rows)

    _write(api / "openapi.json", openapi_spec(), atomic_write); n += 1
    atomic_write(web_root / "swagger" / "index.html",
                 SWAGGER_HTML.replace("__DISCLAIMER__", DISCLAIMER).replace("__BASE__", BASE_URL)); n += 1
    atomic_write(web_root / "api" / "index.html", API_INDEX_HTML); n += 1
    return n
