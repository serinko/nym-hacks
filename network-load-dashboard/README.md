# Nym Network Metrics - Dashboard

**Problem**

Nym is a decentralized network where Nym developers don't have access to the majority of nodes (above 700, from which ~450 are gateways), except about a dozen of self hosted testing nodes on mainnet. The problem is then to measure the network routing (or computing) capacity maximum versus the load at presense - live - is then limited. 


**Solution**

Nym Network wide metrics dashboard. While that is getting developped, here is a limitted and non-audited version, working for now. It's deployed [here](https://node-install.devrel.nymte.ch/network-load/)


## Logic

`generate_network_metrics.py` is a simple Python program creating static html dashboard by doing going through these steps:

1. pulling API endpoints probing each of nodes [load and performance](https://nym.com/docs/operators/performance-and-testing#wireguard-gateways-performance-calculation)
1. calculating whole network load
1. paring it per location
1. creating a static html with basic stats, perfomance and load for whole network and per country

This whole process is on cron job, re-running every 5 minutes. 


You can spin it up yourself anywhere, below is the setup.

# Setup

## Files

- `nym_metrics_common.py` - shared scoring/fetch/aggregate logic, imported by both other scripts - put all three in the same directory.
- `generate_network_metrics.py` - runs every 5 min, writes `index.html` + `country/XX.html` for all (~70) countries
- `record_snapshot.py` - runs hourly, writes to SQLite, trims >30 days automatically

---

## Deploy

### 1. Place the script

SSH to your server and do:

```bash
sudo mkdir -p /opt/nym-metrics
sudo cp nym_metrics_common.py record_snapshot.py generate_network_metrics.py /opt/nym-metrics/
sudo chmod 755 /opt/nym-metrics/generate_network_metrics.py
sudo chmod 755 /opt/nym-metrics/record_snapshot.py
sudo chmod 755 /opt/nym-metrics/nym_metrics_common.py
sudo mkdir -p /var/lib/nym-metrics
```

### 2. Create the nginx web directory

```bash
sudo mkdir -p /var/www/html/network-load
```

### 3. Add nginx location block

In whichever vhost config is serving your main domain, add:

```nginx
location ^~ /network-load/ {
    alias /var/www/html/network-load/;
    index index.html;
}
```

Then reload nginx:

```bash
sudo nginx -t && sudo service nginx reload
```

### 4. Run once manually to verify

```bash
sudo python3 /opt/nym-metrics/record_snapshot.py
sudo python3 /opt/nym-metrics/generate_network_metrics.py

# should print:
# [2026-07-02T12:30:37] Recording snapshot ...
#   Nodes: 468  Locations: 70  Perf: 0.900  Load: 0.092
#   DB size: 44.0 KB  →  /var/lib/nym-metrics/history.db
# [2026-07-02T12:30:38.181404+00:00] Fetching https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways ...
#   Got 468 gateway entries
#   Nodes: 468  Locations: 70  Perf: 90.0%  Load: 9.2%
#   History: 3 global snapshots loaded
#   Written → /var/www/html/network-load/index.html
#   Written → 70 country pages in /var/www/html/network-load/country/
```

### 5. Wire up cron (every 5 minutes)

```bash
sudo crontab -e
```

Add:

```
*/5 * * * * /usr/bin/python3 /opt/nym-metrics/generate_network_metrics.py >> /var/log/nym-metrics.log 2>&1
0 * * * *   /usr/bin/python3 /opt/nym-metrics/record_snapshot.py >> /var/log/nym-metrics-history.log 2>&1
```

---

## OUTPUT_PATH

The script writes to `/var/www/html/network-load/index.html` by default.
Change `OUTPUT_PATH` near the top of the script if your nginx root differs.

The write is atomic (write to `.tmp`, then `rename`) so nginx never serves a partial file.

---

## Dependencies

Standard library only — no pip installs needed. Requires Python 3.8+.

---

## Updating the output directory

If you need a different path (e.g. your nginx root is `/var/www/dev` or similar),
just edit line 19 of the script:

```python
OUTPUT_PATH = Path("/your/actual/nginx/root/network-load/index.html")
