# Paired live and offline TDD results

This compares the v8 Laya capture with the deterministic no-provider baseline over the same 32 frozen intents in both arms. Each arm keeps its 32-cell denominator.

- Live raw provider POSTs: 86
- Offline provider operations: 0
- Offline deterministic fallback attempts: 85
- Live provider attempt receipts: 86
- Live sole-candidate deterministic selections: 10
- Offline sole-candidate deterministic selections: 11

| Arm | Planned | Live first Laya choice correct | Offline first fallback correct | Live final training | Offline final training | Live final holdout | Offline final holdout |
|---|---:|---:|---:|---:|---:|---:|---:|
| `compact_multilingual_single` | 32 | 10/32 | 11/32 | 91/137 | 86/137 | 68/95 | 69/95 |
| `compact_multilingual_local_feedback` | 32 | 10/32 | 11/32 | 137/137 | 137/137 | 95/95 | 95/95 |

## Interpreting the search arm

In the three-attempt live arm, native training score improved in 22 of 32 cells, was unchanged in 10, and declined in none; the summed increase was 46 training cases. The deterministic offline arm reached the exact intended candidate in all 32 cells and passed all training and holdout cases after the same search budget. This shows the observed finite-suite outcome under each mode; it does not isolate a general causal effect of the model from attempt budget and candidate ordering.

The one-attempt arm had 10/32 first Laya choices match the intended candidate and scored 91/137 training cases. Its matched offline fallback first choice matched in 11/32 and scored 86/137. Final emitted-source Go scores were 91/137 training and 68/95 holdout with Laya, versus 86/137 and 69/95 offline.

Training/holdout finite scores remain separate from native source-unit completeness receipts. Every comparison has 32 planned cells per arm; the two independent replays observed and scored all cells.

The per-cell JSON keeps choices, native scores, source completeness, and independent Go observations separately. The raw capture, CLI output, and independent replay records remain unchanged.
