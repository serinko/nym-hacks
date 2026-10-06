# Nym Network Metrics - Dashboard

**Problem**

Nym is a decentralized network where Nym developers don't have access to the majority of nodes (above 850, from which ~600 are gateways), except about a dozen of self hosted testing nodes on mainnet. The problem is then to measure the network routing (or computing) capacity maximum versus the load at present - live - is then limited.


**Solution**

Nym Network wide metrics dashboard. While that is getting developed into the Node Status API, here is a limited and non-audited version, working for now. It's deployed at [load.nymte.ch](https://load.nymte.ch), with an interim API documented at [load.nymte.ch/swagger](https://load.nymte.ch/swagger/).


## Logic

`generate_network_metrics.py` is a simple Python program creating a static html dashboard by going through these steps:

1. pulling API endpoints probing each of the gateways [load and performance](https://nym.com/docs/operators/performance-and-testing#wireguard-gateways-performance-calculation)
1. pulling network totals (active nodes) and node families with their roles
1. calculating whole network performance and load
1. pairing it per location
1. counting residential IP gateways (by ASN kind) and QUIC bridge gateways
1. creating a static html with basic stats, performance and load for the whole network, per country and for residential IPs
1. writing the same data as static JSON files - the interim API under `/api/v0/` with a Swagger page under `/swagger/`

`record_snapshot.py` stores an hourly snapshot of the network stats into SQLite, which feeds the history graphs and the history endpoints.

This whole process is on cron job, the dashboard re-running every 5 minutes, the snapshot every hour.

Data sources:

- [`/dvpn/v1/directory/gateways`](https://mainnet-node-status-api.nymtech.cc/dvpn/v1/directory/gateways) - gateways, performance, load, location, ASN, bridges
- [`/v2/summary`](https://mainnet-node-status-api.nymtech.cc/v2/summary) - total active nodes
- [`/api/v1/node-families`](https://validator.nymtech.net/api/v1/node-families) - node families
- [`/api/v1/nym-nodes/described`](https://validator.nymtech.net/api/v1/nym-nodes/described) - node roles (gateway / mixnode) for family stats


You can spin it up yourself anywhere, below is the setup.

# Setup

## Files

All in this directory:

- `generate_network_metrics.py` - runs every 5 min, writes `index.html`, `residential.html` and `country/XX.html` for all (~70) countries
- `interim_api.py` - imported by the generator, writes the JSON files under `api/v0/` and the Swagger page under `swagger/` - must sit next to the generator
- `record_snapshot.py` - runs hourly, writes to SQLite, trims >30 days automatically, adds new DB columns by itself on upgrade

---

## Deploy

The dashboard expects to be served from the root of its own (sub)domain, e.g. `metrics.example.com`. The Swagger page and the API links use absolute paths (`/api/v0/...`, `/swagger/`), so serving it under a sub-path of another site breaks them.

### 1. DNS

Point an `A` (and `AAAA` if you have IPv6) record of your (sub)domain to the server IP.

If the subdomain lives in its own zone (e.g. its own zone at Linode while the parent domain is at Gandi), the parent zone also needs `NS` records delegating the subdomain to that zone's nameservers - otherwise it never resolves.

Check:

```bash
dig +short metrics.example.com
```

### 2. Place the scripts

SSH to your server and do:

```bash
git clone https://github.com/serinko/nym-hacks.git
sudo mkdir -p /opt/nym-metrics /var/lib/nym-metrics /var/www/html/network-load
sudo cp nym-hacks/network-load-dashboard/{generate_network_metrics.py,interim_api.py,record_snapshot.py} /opt/nym-metrics/
```

> Do not paste the scripts through `nvim`/`vim` - auto-indent and auto-pairing silently break Python. Use `git clone`, `scp` or `cat > file << 'EOF'`.

### 3. Add nginx vhost

Create `/etc/nginx/sites-available/metrics.example.com`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name metrics.example.com;

    root /var/www/html/network-load;
    index index.html;

    # interim API: JSON files, readable from scripts and other websites
    location /api/ {
        add_header Access-Control-Allow-Origin "*" always;
        add_header Cache-Control "public, max-age=60" always;
        try_files $uri $uri/ =404;
    }

    location / {
        try_files $uri $uri/ =404;
    }
}
```

Enable it, reload nginx and get the certificate - certbot adds the HTTPS block and the http->https redirect itself:

```bash
sudo ln -s /etc/nginx/sites-available/metrics.example.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo service nginx reload
sudo certbot --nginx -d metrics.example.com
```

### 4. Run once manually to verify

```bash
sudo python3 /opt/nym-metrics/record_snapshot.py
sudo python3 /opt/nym-metrics/generate_network_metrics.py

# should print:
# [2026-10-06T09:35:19] Recording snapshot ...
#   Gateways: 613  Total nodes: 862  Locations: 71  QUIC bridges: 559  Active families: 57  Nodes in families: 612  Residential: 32 in 5 locations
#   DB size: 180.0 KB -> /var/lib/nym-metrics/history.db
# [2026-10-06T09:47:29.446755+00:00] Starting ...
#   Gateways: 613
#   Total nodes (all types): 862
#   Active families: 57  Nodes in families: 612  (GW: 538  MX: 67)
#   Locations: 71  Perf: 96.7%  Load: 5.7%
#   History: 608 snapshots
#   Written -> /var/www/html/network-load/index.html
#   Written -> 71 country pages
#   Written -> /var/www/html/network-load/residential.html
#   Written -> 78 interim API files (/api/v0/, /swagger/)
```

Then check the site and the API:

```bash
curl -sI https://metrics.example.com/ | head -1
curl -sI https://metrics.example.com/api/v0/latest.json | grep -i "access-control"
```

### 5. Wire up cron

```bash
sudo crontab -e
```

Add:

```
*/5 * * * * /usr/bin/python3 /opt/nym-metrics/generate_network_metrics.py >> /var/log/nym-metrics.log 2>&1
0 * * * *   /usr/bin/python3 /opt/nym-metrics/record_snapshot.py >> /var/log/nym-metrics-history.log 2>&1
```

History graphs and the history endpoints fill in hour by hour from the first snapshot.

---

## Upgrading

Pull and copy the scripts again, nothing else:

```bash
cd nym-hacks && git pull
sudo cp network-load-dashboard/{generate_network_metrics.py,interim_api.py,record_snapshot.py} /opt/nym-metrics/
sudo python3 /opt/nym-metrics/record_snapshot.py
sudo python3 /opt/nym-metrics/generate_network_metrics.py
```

`record_snapshot.py` adds any new DB columns itself (`Migrated DB: added column ...`). Past data and graphs are kept, new fields are `null` for rows recorded before they existed.

---

## Interim API

Static JSON files regenerated every 5 minutes, documented in Swagger at `/swagger/`.

```
/api/v0/latest.json                  current network stats (same as the dashboard top bar)
/api/v0/countries.json               current per-country aggregates
/api/v0/residential.json             residential IP gateways and their stats
/api/v0/history.json                 global hourly snapshots
/api/v0/history/country/{CC}.json    per-country hourly snapshots, e.g. DE.json
/api/v0/openapi.json                 OpenAPI 3.0 description
```

This API is interim and unofficial - it's planned to move into the Node Status API, keeping the same field names.

---

## Network stats export

`export_nym_network_stats.py` (in the parent directory of this repo) pulls the interim API and prints a markdown table of the key metrics with week-over-week change - ready for team meeting notes. Runs anywhere with Python 3, no clone needed:

```bash
curl -s https://raw.githubusercontent.com/serinko/nym-hacks/main/export_nym_network_stats.py | python3 -
```

Save to a file or point to your own deployment:

```bash
curl -s https://raw.githubusercontent.com/serinko/nym-hacks/main/export_nym_network_stats.py | python3 - -o nym-network-stats.md
curl -s https://raw.githubusercontent.com/serinko/nym-hacks/main/export_nym_network_stats.py | python3 - --base-url https://metrics.example.com/api/v0
```

---

## Configuration

Paths are set at the top of the scripts:

```python
# generate_network_metrics.py
OUTPUT_INDEX       = Path("/var/www/html/network-load/index.html")
OUTPUT_COUNTRY     = Path("/var/www/html/network-load/country")
OUTPUT_RESIDENTIAL = Path("/var/www/html/network-load/residential.html")
DB_PATH            = Path("/var/lib/nym-metrics/history.db")

# record_snapshot.py
DB_PATH     = Path("/var/lib/nym-metrics/history.db")
RETAIN_DAYS = 30
```

If your nginx root differs, change all three `OUTPUT_*` paths in the generator and `root` in the nginx vhost - the API and Swagger files are written next to `index.html` automatically. `DB_PATH` must be the same in both scripts.

All writes are atomic (write to `.tmp`, then `rename`) so nginx never serves a partial file.

---

## Dependencies

Standard library only - no pip installs needed. Requires Python 3.8+.

The pages load [Chart.js](https://www.chartjs.org/), its zoom plugin and Swagger UI from `cdnjs.cloudflare.com` in the browser - nothing to install on the server.
