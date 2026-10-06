#!/usr/bin/env python3
"""
Nym Network Metrics - hourly snapshot recorder.
Fetches gateway API + bonded nodes + families, writes to SQLite.
Trims rows older than 30 days.

Cron (hourly):
    0 * * * * /usr/bin/python3 /opt/nym-metrics/record_snapshot.py >> /var/log/nym-metrics-history.log 2>&1
"""

import sys
import json
import sqlite3
import datetime
import urllib.request
from pathlib import Path

DB_PATH     = Path("/var/lib/nym-metrics/history.db")
RETAIN_DAYS = 30
TIMEOUT     = 30

SUMMARY_API = "https://mainnet-node-status-api.nymtech.cc/v2/summary"
FAMILIES_API = "https://validator.nymtech.net/api/v1/node-families?size=100&page={page}"
GATEWAYS_API = "https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways"
LOAD_SCORE_MAP = {"low": 0.0, "medium": 0.5, "high": 1.0, "offline": 1.0}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nym-metrics/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


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
    return len(active), len(node_ids)


def fetch_gateways():
    req = urllib.request.Request(GATEWAYS_API, headers={"User-Agent": "nym-metrics/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def compute_perf(node):
    p = node.get("performance")
    if p is None:
        return None
    try:
        return float(p)
    except (ValueError, TypeError):
        return None


def mean(lst):
    return sum(lst) / len(lst) if lst else None


def aggregate_gateways(gateways):
    perf_scores = []
    load_scores = []
    from collections import defaultdict
    countries = defaultdict(lambda: {"node_count": 0, "perf_scores": [], "load_scores": []})

    for node in gateways:
        pv2  = node.get("performance_v2") or {}
        loc  = node.get("location") or {}
        cc   = (loc.get("two_letter_iso_country_code") or "??").upper()
        load_str = pv2.get("load") or ""
        if load_str in LOAD_SCORE_MAP:
            ls = LOAD_SCORE_MAP[load_str]
            load_scores.append(ls)
            countries[cc]["load_scores"].append(ls)
        ps = compute_perf(node)
        if ps is not None:
            perf_scores.append(ps)
            countries[cc]["perf_scores"].append(ps)
        countries[cc]["node_count"] += 1

    country_data = []
    for cc, d in countries.items():
        country_data.append({
            "cc":        cc,
            "node_count": d["node_count"],
            "mean_perf": mean(d["perf_scores"]),
            "mean_load": mean(d["load_scores"]),
        })

    res_nodes = [
        node for node in gateways
        if ((node.get("location") or {}).get("asn") or {}).get("kind") == "residential"
    ]
    res_ccs = [((n.get("location") or {}).get("two_letter_iso_country_code") or "??").upper()
               for n in res_nodes]
    res_loads = [LOAD_SCORE_MAP[l] for l in
                 (((n.get("performance_v2") or {}).get("load") or "") for n in res_nodes)
                 if l in LOAD_SCORE_MAP]
    residential = len(res_ccs)
    return {
        "gw_count":      len(gateways),
        "location_count": len(countries),
        "quic_bridges":   sum(1 for g in gateways if any(((t.get("transport_type") or "").startswith("quic")) for t in ((g.get("bridges") or {}).get("transports") or []))),
        "mean_perf":     mean(perf_scores),
        "mean_load":     mean(load_scores),
        "countries":     country_data,
        "residential":   residential,
        "residential_locations": len(set(res_ccs)),
        "residential_load": mean(res_loads),
    }


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS global_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT NOT NULL,
            node_count       INTEGER NOT NULL,
            loc_count        INTEGER NOT NULL,
            mean_perf        REAL,
            mean_load        REAL,
            total_nodes      INTEGER,
            active_families  INTEGER,
            nodes_in_families INTEGER,
            residential      INTEGER,
            residential_locations INTEGER,
            residential_load REAL,
            quic_bridges     INTEGER
        );
        CREATE TABLE IF NOT EXISTS country_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            cc          TEXT NOT NULL,
            node_count  INTEGER NOT NULL,
            mean_perf   REAL,
            mean_load   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_global_ts  ON global_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_country_ts ON country_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_country_cc ON country_snapshots(cc);
    """)
    # migrate: add new columns if they don't exist yet
    existing = {row[1] for row in conn.execute("PRAGMA table_info(global_snapshots)").fetchall()}
    for col, typedef in [
        ("total_nodes",       "INTEGER"),
        ("active_families",   "INTEGER"),
        ("nodes_in_families", "INTEGER"),
        ("residential",       "INTEGER"),
        ("residential_locations", "INTEGER"),
        ("residential_load",      "REAL"),
        ("quic_bridges",          "INTEGER"),
    ]:
        if col not in existing:
            conn.execute("ALTER TABLE global_snapshots ADD COLUMN " + col + " " + typedef)
            print("  Migrated DB: added column " + col)
    conn.commit()


def trim_old(conn):
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) -
        datetime.timedelta(days=RETAIN_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    dg = conn.execute("DELETE FROM global_snapshots WHERE ts < ?", (cutoff,)).rowcount
    dc = conn.execute("DELETE FROM country_snapshots WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return dg, dc


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    ts  = now.strftime("%Y-%m-%dT%H:%M:%S")
    print("[" + ts + "] Recording snapshot ...")

    try:
        gateways = fetch_gateways()
    except Exception as e:
        print("ERROR: gateways fetch failed: " + str(e), file=sys.stderr)
        sys.exit(1)

    try:
        total_nodes = fetch_total_nodes()
    except Exception as e:
        print("WARN: bonded nodes fetch failed: " + str(e))
        total_nodes = None

    try:
        active_fam, nodes_in_fam = fetch_family_stats()
    except Exception as e:
        print("WARN: families fetch failed: " + str(e))
        active_fam, nodes_in_fam = None, None

    data = aggregate_gateways(gateways)
    print("  Gateways: " + str(data["gw_count"]) +
          "  Total nodes: " + str(total_nodes) +
          "  Locations: " + str(data["location_count"]) +
          "  QUIC bridges: " + str(data["quic_bridges"]) +
          "  Active families: " + str(active_fam) +
          "  Nodes in families: " + str(nodes_in_fam) +
          "  Residential: " + str(data["residential"]) +
          " in " + str(data["residential_locations"]) + " locations")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        conn.execute(
            "INSERT INTO global_snapshots "
            "(ts, node_count, loc_count, mean_perf, mean_load, total_nodes, active_families, nodes_in_families, residential, residential_locations, residential_load, quic_bridges) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, data["gw_count"], data["location_count"],
             data["mean_perf"], data["mean_load"],
             total_nodes, active_fam, nodes_in_fam, data["residential"],
             data["residential_locations"], data["residential_load"], data["quic_bridges"])
        )
        conn.executemany(
            "INSERT INTO country_snapshots (ts, cc, node_count, mean_perf, mean_load) VALUES (?,?,?,?,?)",
            [(ts, c["cc"], c["node_count"], c["mean_perf"], c["mean_load"]) for c in data["countries"]]
        )
        conn.commit()
        dg, dc = trim_old(conn)
        if dg or dc:
            print("  Trimmed " + str(dg) + " global + " + str(dc) + " country rows")
        size_kb = DB_PATH.stat().st_size / 1024
        print("  DB size: " + str(round(size_kb, 1)) + " KB -> " + str(DB_PATH))


if __name__ == "__main__":
    main()
