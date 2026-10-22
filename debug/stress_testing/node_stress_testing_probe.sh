#!/usr/bin/env bash
#
# node_stress_testing_probe.sh
#
# Why is this node's stress_testing_score low?
#
# The stress test sends its packets over ONE connection, and nym-node handles
# one connection on ONE core. Single-core speed is the binding constraint -
# core count and RAM are not. This probe measures the things that actually
# predict the score:
#
#   clock speed      - strongest single predictor across the fleet
#   CPU model        - emulated/generic models indicate a shared tier
#   steal time       - how much CPU the hypervisor takes from you
#   X25519 rate      - one scalar mult per packet, ~60-65% of the work.
#                      Measured with OpenSSL as a proxy: nym-node uses
#                      x25519-dalek. Compare against a known-good node.
#   jitter           - whether your vCPU gets descheduled
#
# Steal and jitter are sampled while the node is mostly idle. During a stress
# burst on a shared host they can be much higher.
#
# Read-only. Changes nothing.
#
#   ./node_stress_testing_probe.sh           # 60s sample
#   ./node_stress_testing_probe.sh 300       # 5min, catches bursty contention
#
# Run it on a node that scores WELL too, and compare.

set -uo pipefail
SECS="${1:-60}"
VERDICT=()

hr(){ printf '\n=============== %s ===============\n' "$1"; }
flag(){ VERDICT+=("$1"); echo "  >> $1"; }

# ---------------------------------------------------------------- identity --
hr "IDENTITY"
MODEL=$(lscpu | awk -F': *' '/Model name/{print $2; exit}')
MHZ=$(lscpu | awk -F': *' '/CPU max MHz/{printf "%.0f",$2; exit}')
[ -z "${MHZ:-}" ] && MHZ=$(lscpu | awk -F': *' '/CPU MHz/{printf "%.0f",$2; exit}')
[ -z "${MHZ:-}" ] && MHZ=$(awk -F': ' '/cpu MHz/{printf "%.0f",$2; exit}' /proc/cpuinfo)
echo "  host    : $(hostname)"
echo "  model   : ${MODEL:-unknown}"
echo "  clock   : ${MHZ:-unknown} MHz"
echo "  cores   : $(nproc)   (not a factor - one connection uses one core)"
echo "  RAM     : $(free -g | awk '/Mem:/{print $2}') GB   (not a factor)"
echo "  virt    : $(systemd-detect-virt 2>/dev/null || echo '?')"

# Fleet data: 80% of nodes at 2.0-2.2 GHz fail, 9% above 2.8 GHz fail.
# Nominal clock is a decent predictor but not conclusive: some fast cores
# report a low nominal MHz under virtualisation.
if [ -n "${MHZ:-}" ] && [ "$MHZ" -gt 0 ] 2>/dev/null; then
  if   [ "$MHZ" -ge 2800 ]; then echo "  >> clock is comfortable (>=2.8 GHz band: 9% fail)"
  elif [ "$MHZ" -ge 2500 ]; then echo "  >> clock is adequate (2.5-2.8 band: 21% fail)"
  elif [ "$MHZ" -ge 2200 ]; then flag "clock is marginal (2.2-2.5 GHz band: 41% fail in fleet data)"
  else                           flag "clock is low (<2.2 GHz band: 80% fail in fleet data)"
  fi
fi

case "${MODEL:-}" in
  *QEMU*|*"Common KVM"*)
      flag "EMULATED CPU model - fleet median stress score 0.137" ;;
  *[0-9][0-9][0-9][0-9]*|*Ryzen*|*EPYC\ [0-9]*)
      echo "  >> real CPU model exposed - fleet median 0.991" ;;
  *)  flag "generic CPU model - hypervisor masking a shared tier, fleet median 0.853" ;;
esac

# ------------------------------------------------------------------ X25519 --
hr "X25519 THROUGHPUT  (one per packet - the dominant cost)"
RATE=""
if command -v openssl >/dev/null 2>&1; then
  RATE=$(openssl speed -elapsed ecdhx25519 2>/dev/null | awk '/X25519/{print $NF}')
  if [ -n "${RATE:-}" ]; then
    echo "  X25519 ECDH : ${RATE} ops/sec   (OpenSSL proxy - nym-node uses x25519-dalek)"
    echo "  >> no fixed threshold: compare against a known-good node on similar hardware"
    echo "     reference points: 21,300 op/s scored 1.0; a QEMU vCPU at 10,300 op/s scored 0.24"
  else
    echo "  could not parse openssl output - try: openssl speed -elapsed ecdhx25519"
  fi
else
  echo "  openssl not installed - apt-get install -y openssl"
fi

# --------------------------------------------------------------- contention --
hr "CPU CONTENTION  (${SECS}s sample)"
read -r _ u1 n1 s1 i1 w1 q1 sq1 st1 _ < /proc/stat
sleep "$SECS"
read -r _ u2 n2 s2 i2 w2 q2 sq2 st2 _ < /proc/stat
tot=$(( (u2+n2+s2+i2+w2+q2+sq2+st2) - (u1+n1+s1+i1+w1+q1+sq1+st1) ))
awk -v s="$((st2-st1))" -v t="$tot" -v w="$((w2-w1))" 'BEGIN{
  printf "  steal   : %.2f%%\n", 100*s/t
  printf "  iowait  : %.2f%%\n", 100*w/t
}'
STEAL=$(awk -v s="$((st2-st1))" -v t="$tot" 'BEGIN{printf "%.2f",100*s/t}')
awk -v s="$STEAL" 'BEGIN{exit !(s >= 2)}' \
  && flag "noticeable CPU steal (${STEAL}%) at idle - the host is oversubscribed" \
  || echo "  >> contention is low at idle (a burst on a shared host can still push it up)"

# ------------------------------------------------------------ single core --
hr "SINGLE-CORE SPEED"
s=$(date +%s%N)
awk 'BEGIN{x=0; for(i=0;i<4000000;i++) x+=sqrt(i)}' >/dev/null
e=$(date +%s%N)
MS=$(( (e-s)/1000000 ))
echo "  4M sqrt loop : ${MS} ms   (lower is faster - compare against a known-good node)"

hr "SCHEDULING JITTER"
if command -v python3 >/dev/null 2>&1; then
python3 - <<'PY'
import time
d=[]
for _ in range(200):
    t=time.perf_counter(); time.sleep(0.005)
    d.append((time.perf_counter()-t-0.005)*1000)
d.sort()
p95=d[int(len(d)*.95)]
print(f"  median {d[len(d)//2]:.2f} ms   p95 {p95:.2f} ms   max {d[-1]:.2f} ms")
print("  >> vCPU is being descheduled" if p95>5 else "  >> scheduling is clean at idle")
PY
else
  echo "  python3 not available"
fi

# ----------------------------------------------------------- node config --
hr "NODE CONFIG"
# Resolve the node's id, binary and config from the running systemd unit,
# falling back to whatever exists under /root/.nym/nym-nodes/.
EXEC=$(systemctl show nym-node -p ExecStart --value 2>/dev/null)
BIN=$(printf '%s' "$EXEC" | grep -oP 'path=\K[^ ;]+' | head -1)
[ -x "${BIN:-}" ] || BIN=$(command -v nym-node 2>/dev/null)
[ -x "${BIN:-}" ] || BIN=/root/nym-binaries/nym-node
NODE_ID_DIR="${NYM_ID:-$(printf '%s' "$EXEC" | grep -oP -- '--id[= ]\K[^ ;]+' | head -1)}"
if [ -z "$NODE_ID_DIR" ]; then
  NODE_ID_DIR=$(ls -1 /root/.nym/nym-nodes/ 2>/dev/null | head -1)
fi
NODE_DIR="/root/.nym/nym-nodes/${NODE_ID_DIR:-default-nym-node}"
CFG="$NODE_DIR/config/config.toml"

if [ -f "$CFG" ]; then
  echo "  id      : ${NODE_ID_DIR}"
  echo "  binary  : ${BIN}"
  echo "  config  : $CFG"
  BUF=$(grep -oP '^\s*maximum_connection_buffer_size\s*=\s*\K[0-9]+' "$CFG" 2>/dev/null | head -1)
  if [ -n "$BUF" ] && [ "$BUF" -ne 192 ]; then
    flag "egress buffer changed to $BUF - comment it out and restart nym-node"
    echo "     A deeper queue lifts the score but adds latency; in a mixnet a packet"
    echo "     held that long has already missed its window."
  else
    echo "  >> egress buffer at the default (192)"
  fi
else
  echo "  no nym-node config found at $CFG (set NYM_ID=<id> to override)"
fi

# ---------------------------------------------------------------- score --
hr "CURRENT SCORE"
# The API wants a node_id, not the identity key. Path:
#   identity key (bonding-information, or the .pub PEM) -> /v1/nym-nodes/bonded -> node_id
#   node_id -> /v2/nym-nodes/annotation   (v1 does not expose stress_testing_score)
IDKEY=""
if [ -x "$BIN" ]; then
  IDKEY=$("$BIN" --no-banner bonding-information --id "${NODE_ID_DIR:-default-nym-node}" 2>/dev/null \
          | grep -oP 'Identity Key:\s*\K[1-9A-HJ-NP-Za-km-z]+' | head -1)
fi
PUBFILE=$(grep -oP "^\s*public_ed25519_identity_key_file\s*=\s*['\"]\K[^'\"]+" "$CFG" 2>/dev/null | head -1)
[ -f "${PUBFILE:-}" ] || PUBFILE=$(ls "$NODE_DIR"/{keys,data}/ed25519_identity.pub 2>/dev/null | head -1)
HOSTS=$( { grep -oP "^\s*hostname\s*=\s*['\"]\K[^'\"]+" "$CFG"; \
           sed -n '/^public_ips/,/]/p' "$CFG" | grep -oP "['\"]\K[^'\"]+"; } 2>/dev/null | tr '\n' ' ')

if command -v python3 >/dev/null 2>&1; then
  SCORE_OUT=$(IDKEY="$IDKEY" PUBFILE="${PUBFILE:-}" HOSTS="$HOSTS" python3 - <<'PY'
import base64, json, os, re, urllib.request

def b58(raw):
    A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n, s = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58); s = A[r] + s
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + s

def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)

key = os.environ.get("IDKEY", "")
if not key and os.environ.get("PUBFILE"):
    try:
        body = "".join(l for l in open(os.environ["PUBFILE"]).read().splitlines()
                       if l and "-----" not in l)
        key = b58(base64.b64decode(body))
    except Exception:
        pass
hosts = set(os.environ.get("HOSTS", "").split())

try:
    bonded = get("https://validator.nymtech.net/api/v1/nym-nodes/bonded")
    bonded = bonded.get("data", bonded) if isinstance(bonded, dict) else bonded
except Exception as e:
    print(f"  could not reach nym-api: {e}"); raise SystemExit

node_id, how = None, ""
for x in bonded:
    b = x.get("bond_information") or {}
    n = b.get("node") or {}
    if key and n.get("identity_key") == key:
        node_id, how = b.get("node_id"), "identity key"; break
if node_id is None and hosts:
    for x in bonded:
        b = x.get("bond_information") or {}
        if (b.get("node") or {}).get("host") in hosts:
            node_id, how = b.get("node_id"), "host match"; break

print(f"  identity : {key or 'not found'}")
if node_id is None:
    print("  node_id  : not bonded, or not found")
    raise SystemExit
print(f"  node_id  : {node_id}   (via {how})")

try:
    a = get(f"https://validator.nymtech.net/api/v2/nym-nodes/annotation/{node_id}")["annotation"]
except Exception as e:
    print(f"  could not fetch annotation: {e}"); raise SystemExit
dp = a.get("detailed_performance") or {}
st = dp.get("stress_testing_score") or {}
print(f"  role     : {a.get('current_role') or 'standby'}")
print(f"  perf     : {dp.get('performance_score', 0):.3f}")
print(f"  routing  : {(dp.get('routing_score') or {}).get('score', 0):.3f}")
print(f"  config   : {(dp.get('config_score') or {}).get('score', 0):.3f}")
print(f"  stress   : {st.get('score', 0):.3f}   reachable={st.get('was_reachable')}")
if st.get("was_reachable") is False:
    print("  >> tester could not connect - check firewall/ports before blaming the CPU")
elif (st.get("score") or 0) < 0.8:
    print("  >> stress score is low - see the findings above")
PY
)
  printf '%s\n' "$SCORE_OUT"
  # carry the API verdict into the SUMMARY
  while IFS= read -r l; do VERDICT+=("${l#*>> }"); done < <(printf '%s\n' "$SCORE_OUT" | grep '>> ')
else
  echo "  python3 needed for the score lookup"
fi

# --------------------------------------------------------------- summary --
hr "SUMMARY"
if [ ${#VERDICT[@]} -eq 0 ]; then
  echo "  Nothing flagged. If the score is still low, check was_reachable -"
  echo "  a false there is a firewall problem, not CPU."
else
  printf '  - %s\n' "${VERDICT[@]}"
fi
echo
echo "  Guide : https://nym.com/docs/operators/troubleshooting/nodes#fixing-a-low-mixnode-stress-test-score"
echo "  Report: https://github.com/serinko/nym-hacks/blob/master/debug/stress_testing/mixnode_stress_report.md"
echo
