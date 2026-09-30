# V8 capture analysis

Run: `ir-composition-tdd-v8-20260930T104054Z-d868d3ee7f71` (2026-09-30 UTC).

## Captured workload

- 64/64 CLI cells completed and passed capture validation across 32 existing intents; no new intent was added.
- The runner captured 86 multilingual provider POSTs and 86 health checks. There were no warmups, retries, or fallback receipts. Provider drains settled before owned service shutdown, and independent Go replay ran afterward.
- The earlier v7 partial run (1 POST, 1 completed cell, 63 unstarted) remains separate and is excluded from v8 metrics.

## Proposal, adjustment, and emitted scores

| Arm | First provider proposal score | Intended first choice | Local adjustment | Emitted training | Independent holdout | Provider POSTs / native attempts |
|---|---:|---:|---|---:|---:|---:|
| Single attempt | 91/137 | 10/32 | 0 improved, 32 unchanged, 0 declined (delta +0) | 91/137 | 68/95 | 32 / 32 |
| Local feedback, up to 3 attempts | 91/137 | 10/32 | 22 improved, 10 unchanged, 0 declined (delta +46) | 137/137 | 95/95 | 54 / 64 |

Both arms had the same first provider choice counts: A 6, B 11, C 15; 10/32 matched the intended candidate in each arm. The feedback arm made 54 provider decisions and ended 10 searches with a locally determined sole remaining candidate. Local search improved 22 cell scores by 46 passed cases total, left 10 unchanged, and declined none. Its emitted code passed 137/137 training cases and 95/95 independent postselection holdout cases. The single-attempt arm passed 91/137 training and 68/95 holdout cases. These scores cover finite suites. Since the arms vary both feedback/search and attempt cap, they do not isolate feedback as a cause.

## Latency and sampled resources

Latency is milliseconds; p95 uses nearest-rank. Provider POST duration is calculated from raw proxy event start/end timestamps. CLI active, proxy drain, and harness wall come from invocation receipts.

| Group | POST wall (n) | CLI active (n) | Proxy drain (n) | Harness wall (n) |
|---|---:|---:|---:|---:|
| All captured | 97.389 / 125.523 (n=86) | 107.179 / 241.571 (n=64) | 0.012 / 0.016 (n=64) | 114.902 / 249.450 (n=64) |
| Single attempt | 94.008 / 107.551 (n=32) | 102.664 / 116.372 (n=32) | 0.011 / 0.014 (n=32) | 110.441 / 124.024 (n=32) |
| Local feedback | 100.015 / 133.192 (n=54) | 212.274 / 250.118 (n=32) | 0.012 / 0.018 (n=32) | 220.666 / 258.660 (n=32) |

| Group | Laya CPU delta, coarse sec (observed) | Laya RSS sampled peak KB | Laya sampled `pcpu` max | CLI CPU delta, coarse sec (observed) | CLI RSS sampled peak KB |
|---|---:|---:|---:|---:|---:|
| All captured | 0.170 / 0.510 (n=64) (64/64) | 1225792.000 / 1274704.000 (n=64) | 175.450 / 193.000 (n=64)% | 0.010 / 0.010 (n=1) (1/64; 63 unknown) | 3816.000 / 5840.000 (n=64) |
| Single attempt | 0.160 / 0.240 (n=32) (32/32) | 1225496.000 / 1250112.000 (n=32) | 169.200 / 193.000 (n=32)% | unknown / unknown (n=0) (0/32; 32 unknown) | 3496.000 / 5680.000 (n=32) |
| Local feedback | 0.395 / 0.520 (n=32) (32/32) | 1225904.000 / 1358512.000 (n=32) | 179.600 / 194.500 (n=32)% | 0.010 / 0.010 (n=1) (1/32; 31 unknown) | 4152.000 / 6800.000 (n=32) |

`ps` CPU is cumulative process time sampled at one-second resolution; missing or too-few live samples remain unknown. RSS and `pcpu` are sampled process observations, not continuous maxima. `pcpu` is rolling per-process percentage, not host CPU increase. Model-ready idle baseline and whole-capture totals appear separately in JSON and are not added to cell measures.

## Integrity and limits

- Request and response SHA-256 values match all 172 indexed proxy events; all events returned HTTP 200 and sequences were unique.
- Independent Go replay compiled and scored all 64 outputs after drain and service shutdown.
- Finite scores do not establish full-domain semantics or general correctness; source-completeness receipts are a separate measure.
- Hashes for the authorization, frozen inputs, and durable raw/result artifacts are recorded in the binding and JSON companion.
