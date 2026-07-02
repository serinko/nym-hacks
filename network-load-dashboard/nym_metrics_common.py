"""
Shared scoring logic for Nym Network Metrics tools.
Imported by both generate_network_metrics.py and record_snapshot.py.
"""

import json
import urllib.request
from collections import defaultdict

API_URL     = "https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways"
TIMEOUT_SEC = 30
DB_PATH     = "/var/lib/nym-metrics/history.db"
WEB_ROOT    = "/var/www/html/network-load"

MIXNET_SCORE_MAP = {"high": 1.0, "medium": 0.625, "low": 0.30, "offline": 0.0}
LOAD_SCORE_MAP   = {"low": 0.0, "medium": 0.5, "high": 1.0}

HARBOURMASTER_URL = "https://harbourmaster.nymtech.net/gateway/{identity_key}"
SPECTREDAO_URL    = "https://explorer.nym.spectredao.net/nodes/{identity_key}"


def fetch_gateways() -> list:
    req = urllib.request.Request(API_URL, headers={"User-Agent": "nym-metrics/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode())


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
    if score > 0.75: return "high"
    if score > 0.50: return "medium"
    if score > 0.10: return "low"
    return "offline"


def load_tier_from_score(score: float) -> str:
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
    mixnet = MIXNET_SCORE_MAP.get(pv2.get("mixnet_score", "offline"), 0.0)
    dl     = download_speed_score(wg)
    ping   = wg.get("ping_ips_performance_v4") or 0.0
    return mixnet * (dl * ping), True


def mean(lst):
    return sum(lst) / len(lst) if lst else None


def aggregate(gateways: list) -> dict:
    """
    Full aggregation: global metrics + per-country + per-node detail.
    Returns a dict suitable for both the HTML generator and the snapshot recorder.
    """
    total       = len(gateways)
    perf_scores = []
    load_scores = []
    no_probe    = 0

    perf_tiers = defaultdict(int)
    load_tiers = defaultdict(int)

    countries = defaultdict(lambda: {
        "perf_scores": [], "load_scores": [],
        "perf_tiers": defaultdict(int), "load_tiers": defaultdict(int),
        "node_count": 0,
        "nodes": [],          # full per-node detail for country pages
    })

    for node in gateways:
        pv2  = node.get("performance_v2") or {}
        loc  = node.get("location") or {}
        cc   = (loc.get("two_letter_iso_country_code") or "??").upper()
        city = loc.get("city") or ""
        ikey = node.get("identity_key") or ""
        name = node.get("name") or ikey[:16] + "…"

        # load
        load_str = pv2.get("load")
        load_num = None
        if load_str and load_str in LOAD_SCORE_MAP:
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
            tier = perf_tier(ps)
            perf_tiers[tier] += 1
            countries[cc]["perf_scores"].append(ps)
            countries[cc]["perf_tiers"][tier] += 1

        countries[cc]["node_count"] += 1
        countries[cc]["nodes"].append({
            "identity_key":  ikey,
            "name":          name,
            "city":          city,
            "perf_score":    ps,
            "has_probe":     has_probe,
            "perf_tier":     perf_tier(ps) if has_probe else "unknown",
            "load_str":      load_str or "unknown",
            "load_score":    load_num,
            "uptime":        pv2.get("uptime_percentage_last_24_hours"),
            "mixnet_score":  pv2.get("mixnet_score", "unknown"),
            "overall_score": pv2.get("score", "unknown"),
        })

    # flatten countries
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
            "nodes":      sorted(d["nodes"], key=lambda n: -(n["perf_score"] or 0)),
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
