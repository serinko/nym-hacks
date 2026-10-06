#!/usr/bin/env python3
"""
Nym Network Metrics - network stats exporter (export_nym_network_stats.py).

Runs from anywhere with Python 3 (stdlib only). Pulls the interim API at
load.nymte.ch and prints a markdown table:

    Metric | This week | Change | Source | Note

"This week" = /api/v0/latest.json at run time.
"Change"    = vs the hourly snapshot in /api/v0/history.json closest to
              7 days ago (accepted within +/- 12h). "null" if there is none
              or the value was not recorded yet.

Change format:
    count metrics      -> relative change in %          (e.g. +3.2%)
    percentage metrics -> change in percentage points   (e.g. -1.4 pp)

Usage:
    python3 export_nym_network_stats.py
    python3 export_nym_network_stats.py -o meeting-stats.md
    python3 export_nym_network_stats.py --base-url https://load.nymte.ch/api/v0
"""

import sys
import json
import argparse
import datetime
import urllib.request
from pathlib import Path

DEFAULT_BASE    = "https://load.nymte.ch/api/v0"
SOURCE          = "[load.nymte.ch](https://load.nymte.ch)"
LOOKBACK_DAYS   = 7
TOLERANCE_HOURS = 12
TIMEOUT         = 30

# (metric name, API field, kind, note)
METRICS = [
    ("Nym nodes",                 "nodes_total",           "count", "Active nodes in Nym network"),
    ("dVPN Gateways",             "gateways_total",        "count", "Routable Gateways for fast mode"),
    ("Locations",                 "locations",             "count", "Unique locations of Nym network"),
    ("QUIC bridges",              "quic_bridges",          "count", "Gateways offering QUIC bridge transport"),
    ("Network performance",       "performance",           "pct",   "Mean performance across active nodes"),
    ("Network load",              "load",                  "pct",   "Load of the entire network"),
    ("Residential IPs",           "residential_ips",       "count", "Number of residential IP nodes"),
    ("Residential IPs locations", "residential_locations", "count", "Unique residential IP locations"),
    ("Residential IPs load",      "residential_load",      "pct",   "Load of the residential IP nodes"),
    ("Active families",           "active_families",       "count", "Total number of families with active nodes"),
    ("Nodes in families",         "nodes_in_families",     "count", "Summary of active nodes registered to a family"),
]


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nym-network-stats/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def parse_ts(s):
    return datetime.datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)


def week_ago(history, now):
    target = now - datetime.timedelta(days=LOOKBACK_DAYS)
    best, best_d = None, None
    for row in history:
        d = abs((parse_ts(row["ts"]) - target).total_seconds())
        if d <= TOLERANCE_HOURS * 3600 and (best_d is None or d < best_d):
            best, best_d = row, d
    return best


def fmt_value(v, kind):
    if v is None:
        return "null"
    return (str(round(v * 100, 1)) + "%") if kind == "pct" else str(v)


def fmt_change(now_v, old_v, kind):
    if now_v is None or old_v is None:
        return "null"
    if kind == "pct":
        d = round((now_v - old_v) * 100, 1)
        return ("+" if d >= 0 else "") + str(d) + " pp"
    if old_v == 0:
        return "null"
    d = round((now_v - old_v) / old_v * 100, 1)
    return ("+" if d >= 0 else "") + str(d) + "%"


def build_table(cur, old):
    header = ["Metric", "This week", "Change", "Source", "Note"]
    rows = []
    for name, field, kind, note in METRICS:
        now_v = cur.get(field)
        old_v = old.get(field) if old else None
        rows.append([name, fmt_value(now_v, kind), fmt_change(now_v, old_v, kind), SOURCE, note])

    # pad every column to its widest cell so the table also reads well in a terminal;
    # value and change columns are right-aligned
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    right = {1, 2}

    def line(cells):
        out = []
        for i, c in enumerate(cells):
            out.append(c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
        return "| " + " | ".join(out) + " |"

    sep = "|" + "|".join(
        ("-" * (widths[i] + 1) + ":") if i in right else ("-" * (widths[i] + 2))
        for i in range(len(header))
    ) + "|"
    return "\n".join([line(header), sep] + [line(r) for r in rows]) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Export weekly Nym network stats as a markdown table.")
    ap.add_argument("-o", "--out", help="write the table to this file instead of stdout")
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="interim API base URL (default: %(default)s)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    try:
        latest  = get_json(base + "/latest.json")
        history = get_json(base + "/history.json")
    except Exception as e:
        print("ERROR: could not fetch API at " + base + ": " + str(e), file=sys.stderr)
        sys.exit(1)

    now = datetime.datetime.now(datetime.timezone.utc)
    old = week_ago(history.get("data", []), now)
    if old:
        print("Comparing against snapshot " + old["ts"], file=sys.stderr)
    else:
        print("No snapshot found around 7 days ago - Change column is null", file=sys.stderr)

    table = build_table(latest.get("data", {}), old)
    if args.out:
        Path(args.out).write_text(table, encoding="utf-8")
        print("Written -> " + args.out, file=sys.stderr)
    else:
        sys.stdout.write(table)


if __name__ == "__main__":
    main()
