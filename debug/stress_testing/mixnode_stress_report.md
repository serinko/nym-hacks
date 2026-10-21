# Why Nym mixnodes fail the stress test

**Data:** 246 bonded mixnodes, October 2026 · **Sources:** public endpoints only, no token required

Operator-facing fix guide: [fixing-low-stress-score.md](./fixing-low-stress-score.md)

---

## 1. Summary

The stress test drives its load down a **single connection**, and `nym-node` processes one connection on **one core**. Single-core throughput is therefore the binding constraint, and clock speed predicts the score while core count and RAM do not.

The dominant cost is **one X25519 scalar multiplication per packet** to derive its shared secret - around 60-65% of CPU during a stress run, measured by an operator profiling a live test. The AES over the payload is 3-4%.

What makes a core too slow is therefore its raw single-thread speed: 80% of nodes between 2.0 and 2.2 GHz fail, against 9% above 2.8 GHz. Virtualised CPUs that mask the host model are slow at this and everything else, which is why an emulated CPU model is such a strong negative signal.

None of it is fixable by buying more cores or more RAM.

Nym is reducing the size of the stress bursts, which will lift borderline nodes without operator action. The deeper question - why one connection cannot use more than one core - is a development matter covered in §6.

---

## 2. How the score works

```
performance = 0.3 × stress_score + 0.7 × (routing_score × config_score)
```

Verified against all 246 nodes: exact to float precision.

- Stress can cost at most **0.30**. Perfect routing and config with `stress = 0` lands on exactly **0.700**.
- The field lives at `/api/v2/nym-nodes/annotation/{node_id}` as `stress_testing_score`, with a `was_reachable` flag separating *tested and slow* from *never contacted*.
- **The v1 route omits it entirely.** Anyone diagnosing from v1 cannot see the component responsible. Worth surfacing in docs and explorers.

---

## 3. Isolating the stress-driven cases

Of **79** mixnodes at performance ≤ 0.8:

| Driver | n | Remedy |
|---|---|---|
| config score (version behind, T&C, API unreachable) | 38 | operator upgrade - cheapest win |
| routing score | 1 | - |
| **stress score** | **40** | this report |

The 40 all have `routing ≥ 0.95` and `config = 1.00`, so stress carries their entire deficit.

They contain two unrelated failure modes:

- **23 nodes `was_reachable: false`** - never contacted. Concentrated at hosttech (7), OVH (5), Hetzner (3). Routing score is ~0.99, so ordinary mixnet traffic reaches them. **This is a connectivity fault, not capacity**, and is not addressed below.
- **The rest scored 0.04-0.42 while reachable.** A smooth gradient, not pass/fail - the test measures throughput.

---

## 4. What predicts failure

### Clock speed

151 nodes with healthy routing and config:

| Clock | n | median stress | failing (<0.8) |
|---|---|---|---|
| < 2.0 GHz | 12 | 0.810 | 50% |
| 2.0 - 2.2 GHz | 20 | **0.175** | **80%** |
| 2.2 - 2.5 GHz | 44 | 0.927 | 41% |
| 2.5 - 2.8 GHz | 42 | 0.991 | 21% |
| 2.8 - 3.5 GHz | 23 | 0.986 | 9% |
| ≥ 3.5 GHz | 10 | 0.995 | 0% |

The sub-2.0 band is small and mixed - a few genuinely fast cores report low nominal clocks. The 2.0-2.2 band is the clearest failure zone.

### CPU model exposure

| CPU model as reported by the node | n | median stress |
|---|---|---|
| `QEMU Virtual CPU` (emulated) | 7 | **0.137** |
| generic family placeholder, e.g. `Intel Xeon Processor (Skylake, IBRS, no TSX)` | 76 | 0.853 |
| real SKU, e.g. `AMD EPYC 7543P 32-Core` | 68 | **0.991** |

A generic name means the hypervisor masks the real CPU for live-migration compatibility - the signature of an oversubscribed shared tier. A real SKU means dedicated or passthrough.

### The actual cost: X25519, not AES

Sphinx derives a shared secret per packet with one X25519 scalar multiplication. On the same reference CPU:

| operation | rate | per packet |
|---|---|---|
| X25519 ECDH | 26,515 ops/sec | **37.7 µs** |
| AES-256-GCM over a ~2 KB payload | 2,990,555k B/s | 0.68 µs |

X25519 costs roughly **55x** more than the packet's AES. An operator profiling a live stress run measured 62-67% of CPU in Curve25519 against 3-4% in AES, which matches.

This also explains the burst sizes directly. At ~26,500 ops/sec a 45,000-packet burst is about **1.7 seconds** of solid single-core crypto; on a core a third as fast, 5 seconds - long enough for the egress queue to fill and drop.

Benchmark it with:

```sh
openssl speed -elapsed ecdhx25519 | tail -2
```

**Correction.** An earlier version of this report identified AES-NI as the mechanism, after measuring 290,264k on a `QEMU Virtual CPU version 2.5+` node against 11,779,003k on reference hardware - software-AES speed despite `/proc/cpuinfo` advertising `aes`. That node was predicted to fail from hardware alone and subsequently scored **0.256**, which appeared to confirm it.

The prediction held but the explanation was wrong. An emulated CPU is slow at *everything*, including X25519; AES was simply the easier thing to measure, and it correlated. The clock-speed findings stand, because clock is a proxy for single-thread speed generally. The AES-NI causal story does not.

### Provider, as a consequence

| ASN | n | median stress |
|---|---|---|
| Contabo (DE) | 6 | 0.672 |
| Contabo Asia | 4 | 0.692 |
| The Constant Company | 5 | 0.835 |
| OVH | 16 | 0.921 |
| Hetzner | 24 | 0.964 |
| Hohl IT | 13 | 0.997 |
| RoyaleHosting | 8 | 0.999 |
| netcup | 4 | 0.999 |

Provider matters only as a proxy for which CPU tier you land on - see the correction below.

---

## 5. What does *not* predict failure

| Factor | Finding |
|---|---|
| **Core count** | Non-monotonic. 2 cores median 0.995; 4 cores 0.875. Follows directly from one-connection-one-core. |
| **RAM** | Non-monotonic. 2 GB median 0.995; 4 GB 0.742. |
| **AES-NI** | Universal (144/144 report the flag), and AES is only 3-4% of the work. Not the bottleneck. |
| **AVX2** | Only 3 nodes lack it; they score 0.943. |
| **CPU age** | `Xeon E5-2690 v4` (2016) scores 0.999. Old dedicated silicon beats new shared vCPU. |
| **Intel vs AMD** | Appears strong (0.765 vs 0.987) but dissolves - Intel is over-represented in the shared tier. |
| **Kernel / OS** | Kernel 6.1 appears bad (0.192), but 8 of those 11 nodes are shared-vCPU; the other 3 score 0.950. |
| **Datacenter location** | Confounded - see below. |

### Correction: geography was a confounder

Hetzner first looked like a location story: Helsinki 0.998, Nuremberg 0.806, Falkenstein 0.394. Splitting by city *and* CPU:

| City | CPU exposure | n | median | vendor | MHz |
|---|---|---|---|---|---|
| Falkenstein | generic | 6 | **0.144** | Intel | 2196 |
| Nuremberg | generic | 5 | 0.806 | Intel | 2100 |
| Helsinki | generic | 7 | 0.995 | AMD | 2400 |
| **Falkenstein** | **real SKU** | 2 | **0.997** | Intel | 3212 |
| Helsinki | real SKU | 4 | 1.000 | AMD | 3946 |

Falkenstein nodes on a real CPU score 0.997. The datacenter was never the variable; it is where the cheap Intel instances happen to sit. An earlier version of this report attributed the failures to geography. That was wrong.

---

## 6. Why one connection uses one core

Traced in the monorepo.

**One task per inbound connection** - `nym-node/src/node/mixnet/shared/mod.rs:210`:

```rust
let mut handler = ConnectionHandler::new(self, remote_addr);
let join_handle = tokio::spawn(async move { handler.handle_connection(socket).await });
```

Tokio spreads tasks across worker threads, so with many peers **all cores are used**. That part of the common claim is correct.

**But the Sphinx unwrap is synchronous inside that task** - `nym-node/src/node/mixnet/handler.rs:688`:

```rust
fn try_full_unwrap_packet(&self, packet: FramedNymPacket) -> Result<...> {
    let key = self.resolve_rotation_key(packet.header().key_rotation)?;
    process_framed_packet(packet, key.inner().as_ref())   // no spawn_blocking, no rayon
}
```

No offload. A task runs on one thread at a time, so every packet on a given connection is decrypted sequentially on one core. A second core cannot assist that connection.

The node therefore scales **across** connections, not **within** one - and the stress test uses one.

**The egress buffer** - `common/client-libs/mixnet-client/src/client.rs:547`:

```rust
let (sender, receiver) = mpsc::channel(self.config.maximum_connection_buffer_size);
```

One bounded queue per outbound connection; default **192** (`nym-node/src/config/mod.rs:934`). When full, packets drop.

Raising it was circulated as a workaround and does lift the score, but the field's own doc comment rules it out: *"a short-term burst absorber, not a queue... an oversized value is just bufferbloat... further packets are dropped rather than delayed, which is preferable in a mixnet where a packet held that long has already missed its usefulness window."* It improves the metric while making the node marginally worse at its actual job. **Not recommended.**

### Development options

- **Reduce burst size** - already in progress. Burst has moved 30k → 90k → 45k; at ~26,500 X25519/s, 45k is roughly 1.7s of single-core work on a healthy node.
- **Use multiple connections in the stress test**, measuring aggregate capacity rather than single-core speed. More representative of real traffic, but it would stop surfacing slow-core nodes, which is useful signal.
- **Parallelising the unwrap** across cores is *not* planned. Note this does not mean a node only ever uses one core: each connection gets its own task and those are spread across all cores, so with real traffic from many peers the whole CPU is in use. It is a single connection that cannot be split.

---

## 7. Recommendations

**Operators.** A dedicated or passthrough vCPU at **≥ 2.8 GHz**. Not more cores, not more RAM. Check before buying:

```sh
lscpu | grep "Model name"
openssl speed -elapsed ecdhx25519 | tail -2
```

A real CPU model at 2.8 GHz or above, reaching 25,000+ X25519 ops/sec.

**Delegations Program.** Current hardware criteria specify cores and RAM - the two attributes shown here not to matter. Replace with clock speed and CPU model exposure, both machine-readable from `:8080/api/v1/system-info`.

**nym-api / explorers.** Expose `stress_testing_score` on v1, or redirect operators to v2. The component responsible for up to 0.30 of performance is invisible on the endpoint most tooling uses.

**Open items.**

1. The 23 unreachable nodes, especially hosttech's 7. Routing 0.99 but the agent cannot connect - likely a port or protocol the test uses that ordinary traffic does not.
2. Contabo, where both shared and dedicated tiers underperform.
3. Stress-agent topology - confirming where agents run would close off any residual distance effect.

---

## 8. Method and limits

Sources: `explorer/v3/nym-nodes` (inventory), `/api/v2/nym-nodes/annotation/{id}` (scores), per-node `:8080/api/v1/system-info` (hardware), Spectre `api/v1/nodes` (ASN).

Coverage: 246 scored; **192 returned hardware**; **151** had hardware *and* healthy routing/config - the set behind all hardware claims. The 54 without hardware are not a random sample, since nodes with the endpoint disabled may differ systematically. Treat absolute medians as indicative; the within-provider comparisons are the reliable part.

Several cells are n=2-7. The direction is consistent across independent providers, which is what gives confidence; individual cell medians should not be over-read.

Scores are current aggregates, not time series. Per-run history from the orchestrator (`/v1/results/nym-node/{id}/testruns`, bearer token) would show how long each node has been failing and the error text on the unreachable ones.

Credit: the single-connection/single-core mechanism came from engineering investigation, and the Curve25519-vs-AES profile from an operator running `perf` against a live stress test. Both corrected earlier conclusions in this report. The `maximum_connection_buffer_size` workaround was also suggested by operators but is **not** recommended - see §2 of the operator guide.
