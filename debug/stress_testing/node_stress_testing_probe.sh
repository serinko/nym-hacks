#!/usr/bin/env bash
#
# node-cpu-probe.sh - is this box's CPU actually yours?
#
# Measures the things that predict a mixnode's stress_testing_score:
#   steal time   - how much CPU the hypervisor takes away from you
#   crypto rate  - Sphinx packet processing is AES/SHA bound
#   jitter       - scheduling latency under contention
#
# Read-only, changes nothing. Run on the candidate box AND on a node you
# know scores well, then compare.
#
#   ./node-cpu-probe.sh            # 60s sample
#   ./node-cpu-probe.sh 300        # 5min sample, better for spotting bursts

set -uo pipefail
SECS="${1:-60}"

hr(){ printf '\n=============== %s ===============\n' "$1"; }

hr "IDENTITY"
echo "  host    : $(hostname)"
echo "  model   : $(lscpu | awk -F': *' '/Model name/{print $2; exit}')"
echo "  MHz     : $(lscpu | awk -F': *' '/CPU MHz|CPU max MHz/{print $2; exit}')"
echo "  cores   : $(nproc)"
echo "  RAM     : $(free -g | awk '/Mem:/{print $2" GB"}')"
echo "  virt    : $(systemd-detect-virt 2>/dev/null || echo '?')"
echo "  flags   : aes=$(grep -qm1 ' aes ' /proc/cpuinfo && echo yes || echo NO) \
avx2=$(grep -qm1 ' avx2 ' /proc/cpuinfo && echo yes || echo NO)"

# A generic model name means the hypervisor masks the real CPU - the signature
# of a shared, oversubscribed tier.
MODEL=$(lscpu | awk -F': *' '/Model name/{print $2; exit}')
case "$MODEL" in
  *QEMU*|*"Common KVM"*)  echo "  >> EMULATED CPU model - strongest predictor of a poor stress score" ;;
  *[0-9][0-9][0-9][0-9]*|*Ryzen*|*EPYC\ [0-9]*) echo "  >> real SKU exposed - good sign" ;;
  *) echo "  >> generic family placeholder - shared tier likely" ;;
esac

hr "STEAL TIME  (${SECS}s)   <-- the direct oversubscription measure"
read -r _ u1 n1 s1 i1 w1 irq1 sirq1 st1 _ < /proc/stat
sleep "$SECS"
read -r _ u2 n2 s2 i2 w2 irq2 sirq2 st2 _ < /proc/stat
tot=$(( (u2+n2+s2+i2+w2+irq2+sirq2+st2) - (u1+n1+s1+i1+w1+irq1+sirq1+st1) ))
stl=$(( st2 - st1 )); iow=$(( w2 - w1 )); idl=$(( i2 - i1 ))
awk -v s="$stl" -v t="$tot" -v w="$iow" -v i="$idl" 'BEGIN{
  printf "  steal   : %.2f%%\n", 100*s/t
  printf "  iowait  : %.2f%%\n", 100*w/t
  printf "  idle    : %.2f%%\n", 100*i/t
  st=100*s/t
  if (st < 0.5)      print "  >> negligible contention - CPU is effectively yours"
  else if (st < 2)   print "  >> mild contention - acceptable"
  else if (st < 5)   print "  >> NOTICEABLE contention - likely to cost stress score"
  else               print "  >> HEAVY contention - expect a poor stress score"
}'

hr "CRYPTO THROUGHPUT  (Sphinx is AES/SHA bound)"
if command -v openssl >/dev/null; then
  for alg in aes-256-gcm sha256; do
    out=$(openssl speed -elapsed -evp "$alg" 2>/dev/null | tail -1)
    printf "  %-12s %s\n" "$alg" "$(echo "$out" | sed 's/^[^ ]* *//')"
  done
  echo "  (compare the 8192-byte column against a known-good node)"
else
  echo "  openssl not installed - apt-get install -y openssl"
fi

hr "SINGLE-CORE COMPUTE"
s=$(date +%s%N)
awk 'BEGIN{x=0; for(i=0;i<4000000;i++) x+=sqrt(i); print ""}' >/dev/null
e=$(date +%s%N)
echo "  4M sqrt loop : $(( (e-s)/1000000 )) ms   (lower is better; <1500ms is healthy)"

hr "SCHEDULING JITTER"
if command -v python3 >/dev/null; then
python3 - <<'PY'
import time
d=[]
for _ in range(200):
    t=time.perf_counter(); time.sleep(0.005)
    d.append((time.perf_counter()-t-0.005)*1000)
d.sort()
print(f"  median {d[len(d)//2]:.2f} ms   p95 {d[int(len(d)*.95)]:.2f} ms   max {d[-1]:.2f} ms")
print("  >> p95 above ~5ms suggests the vCPU is being descheduled under load")
PY
else
  echo "  python3 not available"
fi

hr "NODE STRESS SCORE  (once bonded and tested)"
ID=$(cat /root/.nym/nym-nodes/*/data/ed25519_identity.pub 2>/dev/null | tr -d '"[:space:]' | head -1)
if [ -n "${ID:-}" ]; then
  NID=$(curl -s --max-time 20 "https://api.nym.spectredao.net/api/v1/nodes?limit=2000" \
        | python3 -c "
import json,sys
k='$ID'
for n in json.load(sys.stdin):
    if n.get('identity_key')==k:
        print(n['node_id'],n.get('stress_testing_score'),n.get('performance_score'));break
else: print('not-bonded-yet')
" 2>/dev/null)
  echo "  identity : $ID"
  echo "  node_id / stress / performance : $NID"
  echo "  track with: curl -s https://validator.nymtech.net/api/v2/nym-nodes/annotation/<node_id> | jq .annotation.detailed_performance"
else
  echo "  no node identity found yet"
fi
echo
