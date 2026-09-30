# Independent Go replay

Status: **COMPLETE**
Mode: `live`
Planned cells: 64
Compiled and scored: 64
Unknown cells: 0

Proposal-choice and source-completeness metrics stay in the captured CLI receipts. This replay only reports the selected emitted source against finite training and postselection holdout cases.

| Arm | Cells planned | Go scored | Unknown | Training passed / observed / planned | Holdout passed / observed / planned |
|---|---:|---:|---:|---:|---:|
| `compact_multilingual_single` | 32 | 32 | 0 | 91 / 137 / 137 | 68 / 95 / 95 |
| `compact_multilingual_local_feedback` | 32 | 32 | 0 | 137 / 137 / 137 | 95 / 95 / 95 |
