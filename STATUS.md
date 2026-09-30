# Measured v8 TDD study

The public v8 freeze precedes inference at commit d868d3ee7f7196eba2bf89b452d8f9b69849d9f0. Its design SHA is ab691ac00f73abc1cef9648da58c20a1816d7ab16cda7e59536054bea9a66b52.

Run `ir-composition-tdd-v8-20260930T104054Z-d868d3ee7f71` completed all 64 cells over the same 32 intentions. It made 86 actual multilingual Laya POSTs, zero warmups/retries, with no unsettled forwards or stop reason. Owned service shutdown preceded independent Go replay. Raw request/reply/typed-receipt binding independently passes for all 64 cells; fresh Go replay compiled/executed all emitted sources.

| Mode | Attempts allowed | Training tests | Holdout tests | First intended choice |
|---|---:|---:|---:|---:|
| Laya single | 1 | 91/137 | 68/95 | 10/32 |
| Offline single | 1 | 86/137 | 69/95 | 11/32 |
| Laya + local tests/search | 3 | 137/137 | 95/95 | 10/32 |
| Offline + local tests/search | 3 | 137/137 | 95/95 | 11/32 |

Both 3-attempt settings emitted byte-identical sources for all 32 intentions. The finite test gain therefore does not establish an accuracy advantage from Laya. The live search improved 22/32 initial proposals, adding 46 passed training cases. Ten third attempts selected a sole remaining candidate deterministically, with no model call. The two arms jointly change feedback and attempt budget; they do not isolate feedback causally.

Laya CLI active medians were 102.66 ms (single) and 212.27 ms (search). The full model capture window was about 9.86 s. Sampled server RSS peaked at 1,362,464 KB (~1.30 GiB), cumulative sampled process CPU delta was 16.54 s, and rolling process CPU max was 194.5% (one-core scale). These are process observations; host CPU increase and continuous peaks are unmeasured. Warm resident service timing excludes model startup. The matched offline 64-cell CLI capture took about 0.48 s; differing measurement phases limit causal latency comparison.

Postselection extra-domain replay in the master experiment repository executes four additional frozen inputs for each intention: single 87/128, local-feedback search 128/128, zero unknowns. Its handwritten reference shares lineage with the original finite oracle; it is a correlated diagnostic. Dynamic branch coverage/full-domain correctness remain unmeasured.

The separate v7 run remains PARTIAL: one actual POST, one completed CLI cell, 63 unstarted. The doubled proxy prefix stopped its collector; original validated=0 and independent raw-bound=1 remain separate. Its source scored training2/5 and holdout2/3. This call is excluded from v8 metrics. All superseded/failed harness attempts and explicit historical retention gaps remain retained or documented; no immutable capture report was corrected in place.

See `results/paired-live-offline-v8-20260930T1058Z/report.md` for per-cell matched comparisons. Model proposal, native adjustment, source-unit completeness and independent finite scores are separate metrics. This is three-candidate selection within Gooo IR bodies, not free-form model code generation or general intent completeness.
