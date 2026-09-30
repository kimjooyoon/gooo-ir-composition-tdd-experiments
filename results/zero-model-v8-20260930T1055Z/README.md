# Matched zero-model TDD baseline

Status: **CAPTURED_ZERO_MODEL**

This is the deterministic no-provider baseline for the same 32 frozen intents in both v8 arms. It is not counted as a new intent cohort. The original live capture remains in its own result directory.

- Planned cells: 64
- Successful CLI outputs: 64
- Unknown or failed cells: 0
- Provider operations observed in native receipts: 0
- Attempt receipts: 96
- Deterministic fallback receipts: 85

| Arm | Planned | CLI success | Unknown | Provider operations | Fallback receipts |
|---|---:|---:|---:|---:|---:|
| `compact_multilingual_single` | 32 | 32 | 0 | 0 | 32 |
| `compact_multilingual_local_feedback` | 32 | 32 | 0 | 0 | 53 |
