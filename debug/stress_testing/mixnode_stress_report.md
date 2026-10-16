# Why Nym mixnodes fail the stress test


## Summary

Mixnodes failing the stress test are not short of cores, RAM or crypto instructions. They are running on **oversubscribed shared-vCPU instances**, identifiable by two signals the node reports about itself: a **masked (generic) CPU model name** and a **clock below 2.5 GHz**.

That profile covers 54 of 151 analysable nodes and accounts for **18 of the 22 worst performers**. It holds *within* providers, which rules out "bad hosting company" as the explanation.

An earlier read of this data attributed the failures to datacenter geography. That was a confounder and is corrected in §5.

---

## How performance is built

```
performance = 0.3 × stress_score + 0.7 × (routing_score × config_score)
```

Verified against all 246 nodes: **zero error**, exact to float precision.

Consequences:

- Stress can cost a node at most **0.30**. A node with perfect routing and config but `stress = 0` lands on exactly **0.700** — the floor, and precisely where the unreachable cluster sits.
- The field is exposed at `/api/v2/nym-nodes/annotation/{node_id}` as `stress_testing_score`, with a `was_reachable` boolean distinguishing *tested and slow* from *never contacted*.
- **The v1 route omits it entirely.** Anyone diagnosing a low score from v1 cannot see the component responsible. Worth surfacing in docs and explorers.


## Isolating the stress-driven cases

Of **79** mixnodes at performance ≤ 0.8:

| driver | n | fix |
|---|---|---|
| config score (version behind, T&C, API unreachable) | 38 | operator upgrade — cheapest win |
| routing score | 1 | — |
| **stress score** | **40** | this report |

The 40 all have `routing ≥ 0.95` and `config = 1.00`, so the stress component carries their entire deficit.

They split into two unrelated failure modes:

- **23 nodes `was_reachable: false`** — never contacted by the test agent at all. Of these, 10 are a single provider (hosttech-AS, Switzerland) scoring a clean `0.000` across distinct bonding addresses and two /24s. Routing score is 0.99, so ordinary mixnet traffic reaches them. **This is a connectivity fault, not a capacity one**, and is out of scope below.
- **The remainder scored 0.04–0.42 while reachable.** A smooth gradient, not pass/fail — the test measures throughput under load.

## What predicts failure

### CPU model exposure

| CPU model as reported by the node | n | median stress |
|---|---|---|
| `QEMU Virtual CPU version 2.1.2` (emulated) | 7 | **0.137** |
| generic family placeholder, e.g. `Intel Xeon Processor (Skylake, IBRS, no TSX)` | 76 | 0.853 |
| real SKU passed through, e.g. `AMD EPYC 7543P 32-Core` | 68 | **0.991** |

A generic family name means the hypervisor is masking the real CPU for live-migration compatibility — the signature of a shared, oversubscribed tier. A real SKU means dedicated or passthrough.

### Reported clock

| clock | n | median stress |
|---|---|---|
| 2.0–2.5 GHz | 64 | 0.760 |
| < 2.0 GHz | 12 | 0.810 |
| 2.5–3.0 GHz | 58 | **0.992** |
| ≥ 3.5 GHz | 10 | **0.995** |

### Combined, and controlled by provider

```
generic CPU model AND < 2.5 GHz    n=54   median 0.698   24 below 0.5
everything else                    n=97   median 0.980   11 below 0.5
```

| provider | shared profile | other |
|---|---|---|
| Hetzner | 0.768 (n=20) | **1.000** (n=6) |
| OVH | 0.431 (n=6) | **0.966** (n=10) |
| Contabo | 0.737 (n=4) | 0.700 (n=8) — no effect |

Contabo is the exception: both groups score poorly, so something else limits them.


## What does *not* predict failure

Each of these looked plausible and was ruled out by the data.

| factor | finding |
|---|---|
| **CPU core count** | Non-monotonic. 2 cores → 0.995; 4 cores → 0.875. More cores is not better. |
| **RAM** | Non-monotonic. 1.9 GB → 0.995; 3.7 GB → 0.578. |
| **AES-NI** | Universal (144/144 true). Sphinx being crypto-bound suggested this would matter; it cannot discriminate. |
| **AVX2** | Only 3 nodes lack it; they score 0.943. |
| **CPU age** | `Xeon E5-2690 v4` (2016) scores 0.999. Old bare metal beats new shared vCPU. |
| **Intel vs AMD** | Appears strong (0.765 vs 0.987) but dissolves on inspection — Intel is simply over-represented in the shared-vCPU tier. Hetzner's Intel/AMD gap (0.354 vs 1.000) is an instance-type gap. |
| **Kernel 6.1 / Debian 12** | Appears bad (0.192) but confounded: 8 of 11 such nodes are shared-vCPU; the other 3 score 0.950. |
| **Datacenter location** | **Confounded — see below.** |

### The geography correction

Hetzner initially looked like a location story: Helsinki 0.998, Nuremberg 0.806, Falkenstein 0.394. Splitting by city *and* CPU:

| city | CPU exposure | n | median | vendor | MHz |
|---|---|---|---|---|---|
| Falkenstein | generic | 6 | **0.144** | Intel | 2196 |
| Nuremberg | generic | 5 | 0.806 | Intel | 2100 |
| Helsinki | generic | 7 | 0.995 | AMD | 2400 |
| **Falkenstein** | **real SKU** | 2 | **0.997** | Intel | 3212 |
| Helsinki | real SKU | 4 | 1.000 | AMD | 3946 |

Falkenstein nodes on a real CPU score 0.997. The datacenter was never the variable; it is where the cheap Intel instances happen to be.

## Recommendations

**For operators.** The requirement is a **dedicated or passthrough vCPU at ≥ 2.5 GHz** — not more cores, not more RAM. Self-check:

```bash
curl -s localhost:8080/api/v1/system-info | jq '.hardware.cpu[0].brand, .hardware.cpu[0].frequency'
```

A generic family name plus sub-2.5 GHz predicts a poor stress score. On Hetzner specifically this means CPX/CCX rather than the Intel CX line.

**For the Delegations Program.** Current hardware criteria specify cores and RAM, the two attributes shown here not to matter. Replace with CPU model exposure and clock, both already machine-readable from `system-info`.

**For nym-api / explorers.** Expose `stress_testing_score` on the v1 annotation route, or redirect operators to v2. At present the component responsible for up to 0.30 of performance is invisible on the endpoint most tooling uses.

**Open items.**

1. **The 23 unreachable nodes**, especially the 10 at hosttech. Routing 0.99 but the test agent cannot connect — likely a port or protocol the stress test uses that ordinary mixnet traffic does not. Different fix, probably a quick one.
2. **Contabo**, where the shared/dedicated split has no effect and both tiers underperform.
3. **Agent topology.** The geographic signal is explained, but confirming where test agents run would close off the remaining possibility that distance contributes.

---

## Method and limits

**Sources:** `explorer/v3/nym-nodes` (inventory), `/api/v2/nym-nodes/annotation/{id}` (scores), per-node `:8080/api/v1/system-info` (hardware), Spectre `api/v1/nodes` (ASN).

**Coverage:** 246 mixnodes scored; **192 returned hardware**; **151** had hardware *and* healthy routing/config, which is the set used for all hardware claims. The 54 missing hardware are not random — nodes with the endpoint disabled may differ systematically — so treat absolute medians as indicative and the within-provider comparisons as the reliable part.

Group sizes are small once split (several cells n=2–7). The direction is consistent across independent providers, which is what gives confidence; individual cell medians should not be over-read.

Scores are current aggregates, not time series. Per-run history from the orchestrator (`/v1/results/nym-node/{id}/testruns`, bearer token) would show how long each node has been failing and the error text on the unreachable ones.
