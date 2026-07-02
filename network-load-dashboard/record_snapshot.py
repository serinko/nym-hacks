#!/usr/bin/env python3
"""
Nym Network Metrics — hourly snapshot recorder.
Fetches gateway API, writes one global row + one row per country to SQLite.
Trims rows older than 30 days.

Cron (hourly, separate from the 5-min HTML generator):
    0 * * * * /usr/bin/python3 /opt/nym-metrics/record_snapshot.py >> /var/log/nym-metrics-history.log 2>&1
"""

import sys
import sqlite3
import datetime
from pathlib import Path

# import shared logic from same directory
sys.path.insert(0, str(Path(__file__).parent))
from nym_metrics_common import fetch_gateways, aggregate, mean, DB_PATH

RETAIN_DAYS = 30


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS global_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,          -- ISO8601 UTC
            node_count  INTEGER NOT NULL,
            loc_count   INTEGER NOT NULL,
            mean_perf   REAL,                   -- 0.0–1.0, NULL if no probe data
            mean_load   REAL                    -- 0.0–1.0
        );

        CREATE TABLE IF NOT EXISTS country_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            cc          TEXT NOT NULL,          -- ISO 3166-1 alpha-2, uppercase
            node_count  INTEGER NOT NULL,
            mean_perf   REAL,
            mean_load   REAL
        );

        CREATE INDEX IF NOT EXISTS idx_global_ts  ON global_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_country_ts ON country_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_country_cc ON country_snapshots(cc);
    """)
    conn.commit()


def trim_old(conn):
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) -
        datetime.timedelta(days=RETAIN_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        "DELETE FROM global_snapshots WHERE ts < ?", (cutoff,)
    )
    deleted_g = cur.rowcount
    cur = conn.execute(
        "DELETE FROM country_snapshots WHERE ts < ?", (cutoff,)
    )
    deleted_c = cur.rowcount
    conn.commit()
    return deleted_g, deleted_c


def record(conn, ts: str, data: dict):
    conn.execute(
        "INSERT INTO global_snapshots (ts, node_count, loc_count, mean_perf, mean_load) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, data["total"], data["location_count"], data["mean_perf"], data["mean_load"])
    )
    conn.executemany(
        "INSERT INTO country_snapshots (ts, cc, node_count, mean_perf, mean_load) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (ts, c["cc"], c["node_count"], c["mean_perf"], c["mean_load"])
            for c in data["countries"]
        ]
    )
    conn.commit()


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    ts  = now.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] Recording snapshot ...")

    try:
        gateways = fetch_gateways()
    except Exception as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    data = aggregate(gateways)
    print(f"  Nodes: {data['total']}  Locations: {data['location_count']}  "
          f"Perf: {data['mean_perf']:.3f}  Load: {data['mean_load']:.3f}")

    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        init_db(conn)
        record(conn, ts, data)
        dg, dc = trim_old(conn)
        if dg or dc:
            print(f"  Trimmed {dg} global + {dc} country rows older than {RETAIN_DAYS} days")

    # quick size report
    size_kb = db_path.stat().st_size / 1024
    print(f"  DB size: {size_kb:.1f} KB  →  {db_path}")


if __name__ == "__main__":
    main()
