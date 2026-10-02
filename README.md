# IR composition finite-choice search study

## Development context — 2026-10-03

This repository records the September Laya composition/TDD experiment: propose a
permitted body, evaluate finite examples and use failures in the next attempt.
Its paired arms retain their original model, task and attempt-budget definitions.

Current work explores the same construction loop with independently trained
Gooo judges and a Go runtime. See the
[current research](https://github.com/kimjooyoon/gooo-neural-decision-experiments),
[public model card](https://huggingface.co/asketeddy/gooo-shared-judgment-tiny-v1)
and [language direction, 한국어](https://github.com/kimjooyoon/meta-ontology-go/blob/dev/docs/language-direction.ko.md)
for progress, limitations and research acknowledgments.

## Recorded study

This study uses the finalized revision-2 cohort from
`gooo-metaprogramming-experiments/cohorts/ir-composition-curriculum-2026-09-30/revision-2`.
It reuses the same 32 intent IDs. It does not add or recount 32 new intents.

The study compares two compact multilingual search settings across those 32
paired intents:

| Arm | Provider model | Maximum attempts | Search feedback |
| --- | --- | ---: | --- |
| `compact_multilingual_single` | `multilingual` | 1 | None; one finite candidate proposal |
| `compact_multilingual_local_feedback` | `multilingual` | 3 | The native search state includes prior attempts' local typecheck and training-test results |

Each plan keeps the revision-2 three-candidate finite choice set. This is a
finite selection experiment, not free-form code generation. The 64 scheduled
cells are 32 intents times two arms. The single-choice arm uses at most 32
provider POSTs. In the feedback-search arm, the first two attempts can each
make one POST; once only one of three candidates remains, native search selects
it deterministically. Its native attempt cap is three, but it uses at most 64
provider POSTs across 32 cells. The measured study therefore has a hard cap of
96 provider POSTs and no warmup calls. The attempt cap is not a retry policy,
and the runner never retries a provider request.

The arms differ in both attempt budget and access to previous local results, so
this comparison estimates the combined single-choice versus feedback-search
setting. It does not isolate the effect of feedback from the effect of having
more attempts.

## Holdout handling

The source cohort's holdout vectors are copied and hash-bound as postselection
inputs. The generated native search plans omit `holdout_test_cases`, so neither
the provider request nor native candidate selection receives holdout values.
After all scheduled captures finish, an independent Go test compiles and scores
the emitted source against the held-out vectors. Holdout results do not change
the selected candidate or native training score.

## Metrics

The replay separates what the provider proposed from what the native search
emitted after local scoring:

- **Model proposal:** first response candidate, its predeclared finite-candidate
  training score, whether it is the design's intended candidate, and the full
  per-attempt proposal distribution.
- **Local adjustment:** emitted candidate training score minus the first
  proposal's training score, plus the number of cells whose score improved,
  stayed level, or declined. This is an observed finite-suite score delta; it
  does not claim general correctness.
- **Emitted result:** exact selected expression and independent Go training
  and holdout scores for the emitted source. Holdout is postselection only.
- **Compiler completeness receipt:** native Gooo dimension statuses, reported
  separately from the finite-suite scores and independent Go replay.

Every metric reports the planned denominator, observed denominator, and
unresolved cells separately. Failed CLI invocations remain in the planned
denominator. Provider routing, receipt request hashes, raw request/reply bytes,
and the candidate options in each request are joined by independent replay.

## Preparation and capture

The preparation program uses Python's standard library. It verifies and copies
the finalized revision-2 source bytes, derives the 64 plans, and writes a seeded
phase order. Its model-free protocol preflight runs the pinned native binary
against a loopback mock, saves every exact request and reply, then counts each
reachable protocol context with the already-cached multilingual tokenizer in
offline mode. It covers 32 single-choice requests, 32 search initial requests,
and both possible losing-first-choice second requests for every search intent
(64 more roles, including request bytes duplicated across arms). The frozen
preflight has 128 template roles; its captured raw exchanges also include the
branch-driving mock requests and a wrong-routing reply that must trigger a
deterministic fallback. The 214 mock POSTs and 213 health checks are local
protocol fixtures; mock receipts with mode `laya` do not represent model
inference. The tokenizer check is limited to token accounting; it does not load
model weights or run inference. No cache, virtual environment, or weights are
copied into this repository.

The first preparation draft is preserved byte-for-byte in
`study-design-archives/superseded-v1/`. The initial reachable-state draft is
preserved in `study-design-archives/superseded-v2-initial/`, and the first
finalization attempt with a tokenizer inventory digest issue is preserved in
`study-design-archives/superseded-v3-tokenizer-inventory-digest/`. The fourth
draft is preserved in `study-design-archives/superseded-v4-service-exit-score-gate/`,
and the fifth in `study-design-archives/superseded-v5-live-raw-event-validation/`.
The first v6 freeze is retained in `study-design-archives/superseded-v6-denominator-smoke-assertion/`;
its verifier incorrectly assumed every proposal matched gold and was corrected before
the final freeze. The final freeze is written to `study-design-v7/` and keeps the v6
design schema. Its `study-design.json` SHA-256 is
`ecce451059062b11f6fa8c8198bcfa53318d0533dddd5e1c5d119e22066bcd0b`.

The capture program is sequential and records raw CLI stdout/stderr, raw
provider POSTs and replies, typed decision receipts, and per-invocation hashes.
The frozen design pins both the CI-context capture proxy and its imported
`selection_support.py` sibling. Before starting Laya, each run archives the exact
bytes and SHA-256 values for the preparation script, capture runner, proxy, and
sibling dependency in its preexecution record. The runner then imports the proxy
only after checking both source hashes. It waits for each exchange to
settle before scheduling the next cell. A timed-out or unresolved exchange
stops the run; later cells are recorded as not started. The runner uses the
loopback Laya service, cached model revision, offline flags, and no credentials.
It also saves raw process-level `ps` observations for the compiler CLI and
owned Laya server. A 2.5-second model-ready idle sample follows health and
precedes all cells; it makes no provider POST. Reports separate CLI active
wall time, proxy drain time, and harness wall time. `ps` CPU time has one-second
resolution, so missing or zero short-window deltas are unknown; sampled RSS and
rolling per-process CPU percent are not host CPU increase measurements.

Do not start live capture until the root task confirms the compiler/source pin,
the public preparation checkpoint, and the final capture gate. Preparation
and mock preflight make zero Laya inference calls.

The source revision-2 freeze predates the compiler under study. The study
manifest binds both source freezes independently: the revision-2 cohort bytes
and the clean native compiler binary/build metadata.


## Live capture status

The v7 run is a preserved one-call partial capture: 1/64 completed cells and
63 unstarted. A doubled proxy-directory prefix stopped collection despite a
normal provider response. The original report is immutable; its independent
raw-evidence audit is separate. See [STATUS.md](STATUS.md) for measured timing,
resource observations and the finite test scores.

The v8 derived freeze repairs only collector path resolution and capture source
identity. All protocol inputs remain byte-identical to v7; no additional mock
preflight or inference is attributed to derivation. The exact new sources are
archived in [preexecution-checkpoint-v8](preexecution-checkpoint-v8). The prior
local v8 draft missing an explicit capture path is retained in the superseded
archive. New measured runs use separate IDs and keep v7's one call in its own
partial cohort.


The v8 live capture and matched offline baseline are now complete. See
[STATUS.md](STATUS.md) and [paired results](results/paired-live-offline-v8-20260930T1058Z/report.md).
The bounded search reached the same finite score with and without Laya.
A new [Go-runtime neural decision repository](https://github.com/kimjooyoon/gooo-neural-decision-experiments)
explores domain-specific small models and ternary training separately.
