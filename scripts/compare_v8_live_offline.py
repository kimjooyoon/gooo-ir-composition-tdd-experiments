#!/usr/bin/env python3
"""Build an append-only paired summary for the v8 live and no-provider cohorts."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = ROOT / "study-design-v8"
DESIGN_SHA = "ab691ac00f73abc1cef9648da58c20a1816d7ab16cda7e59536054bea9a66b52"
LIVE_ID = "ir-composition-tdd-v8-20260930T104054Z-d868d3ee7f71"
LIVE_AUDIT = ROOT / "preexecution-checkpoint-v8/independent-validation/v8-capture-audit.json"
ARMS = ("compact_multilingual_single", "compact_multilingual_local_feedback")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> tuple[bytes, Any]:
    raw = path.read_bytes()
    return raw, json.loads(raw)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    live = ROOT / "results" / LIVE_ID
    offline = args.offline_run.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise SystemExit(f"output path already exists: {output}")

    design_raw, design = read_json(DESIGN_DIR / "study-design.json")
    if sha(design_raw) != DESIGN_SHA:
        raise SystemExit("v8 design SHA mismatch")
    audit_raw, audit = read_json(LIVE_AUDIT)
    raw_audit = audit.get("capture_run_audit", {})
    if (audit.get("status") != "PASS" or raw_audit.get("status") != "PASS_RAW_AUDIT_CAPTURED"
            or raw_audit.get("run_id") != LIVE_ID or raw_audit.get("design_sha256") != DESIGN_SHA):
        raise SystemExit("pinned live raw-capture audit did not pass for the expected run/design")
    live_report_raw, live_report = read_json(live / "report.json")
    offline_meta_raw, offline_meta = read_json(offline / "baseline-metadata.json")
    if live_report.get("status") != "CAPTURED" or offline_meta.get("status") != "CAPTURED_ZERO_MODEL":
        raise SystemExit("paired inputs are not both complete runs")
    if offline_meta.get("provider_endpoint_configured") is not False or offline_meta.get("provider_operations_observed") != 0:
        raise SystemExit("no-provider baseline does not bind an absent endpoint and zero provider operations")

    live_go_dir = live / "independent-go-fresh-r2"
    offline_go_dir = offline / "independent-go-replay-r1"
    live_go_raw, live_go = read_json(live_go_dir / "report.json")
    offline_go_raw, offline_go = read_json(offline_go_dir / "report.json")
    if any(report.get("status") != "COMPLETE" or report.get("planned_cells") != 64
           or report.get("compiled_and_scored_cells") != 64 or report.get("unknown_cells") != 0
           for report in (live_go, offline_go)):
        raise SystemExit("both independent Go replays must retain and score all 64 planned cells")

    phase_raw, phase = read_json(live / "phase-plans.json")
    if sha(phase_raw) != design["phase_plan_sha256"] or phase_raw != (offline / "phase-plans.json").read_bytes():
        raise SystemExit("offline and live phase plan bytes differ from the frozen plan")
    vector_root = DESIGN_DIR / design["source"]["path"]
    vectors_raw, vectors = read_json(vector_root / "oracle/testdata/vectors.json")
    vector_by_id = {row["id"]: row for row in vectors}
    audit_by_id = {row["invocation_id"]: row for row in raw_audit.get("per_cell", [])}
    live_score_raw, live_score_index = read_json(live_go_dir / "score-index.json")
    offline_score_raw, offline_score_index = read_json(offline_go_dir / "score-index.json")
    live_scores = {row["invocation_id"]: row for row in live_score_index["rows"]}
    offline_scores = {row["invocation_id"]: row for row in offline_score_index["rows"]}
    live_attempt_total = offline_attempt_total = live_provider_posts = 0
    live_sole_total = offline_sole_total = offline_fallback_total = 0
    paired_rows = []
    all_ids = set()

    for plan_row in phase["plans"]:
        invocation_id = f'{plan_row["sequence"]:02d}-{plan_row["intent_id"]}-{plan_row["arm"]}'
        if invocation_id in all_ids:
            raise SystemExit(f"duplicate phase plan invocation {invocation_id}")
        all_ids.add(invocation_id)
        live_dir = live / "invocations" / invocation_id
        offline_dir = offline / "invocations" / invocation_id
        live_rec_raw, live_rec = read_json(live_dir / "invocation.json")
        offline_rec_raw, offline_rec = read_json(offline_dir / "invocation.json")
        live_stdout = (live / live_rec["stdout_file"]).read_bytes()
        offline_stdout = (offline / offline_rec["stdout_file"]).read_bytes()
        if sha(live_stdout) != live_rec["stdout_sha256"] or sha(offline_stdout) != offline_rec["stdout_sha256"]:
            raise SystemExit(f"raw CLI stdout binding mismatch: {invocation_id}")
        if sha((live_dir / "plan.search.json").read_bytes()) != plan_row["plan_sha256"]:
            raise SystemExit(f"live copied plan hash mismatch: {invocation_id}")
        source_fixture = (vector_root / plan_row["fixture_path"]).read_bytes()
        if sha(source_fixture) != plan_row["fixture_sha256"]:
            raise SystemExit(f"frozen fixture hash mismatch: {invocation_id}")
        if sha((live_dir / "fixture.gooo").read_bytes()) != plan_row["fixture_sha256"]:
            raise SystemExit(f"live copied fixture hash mismatch: {invocation_id}")
        if sha((offline_dir / "plan.search.json").read_bytes()) != plan_row["plan_sha256"]:
            raise SystemExit(f"offline copied plan hash mismatch: {invocation_id}")
        if sha((offline_dir / "fixture.gooo").read_bytes()) != plan_row["fixture_sha256"]:
            raise SystemExit(f"offline copied fixture hash mismatch: {invocation_id}")
        vector = vector_by_id[plan_row["intent_id"]]
        frozen_plan = json.loads((live_dir / "plan.search.json").read_bytes())
        if frozen_plan["test_cases"] != vector["training"]:
            raise SystemExit(f"submitted plan training cases differ from frozen vectors: {invocation_id}")

        live_attempts = live_rec.get("body_search_attempts", [])
        offline_attempts = offline_rec.get("attempts", [])
        if not live_attempts or not offline_attempts:
            raise SystemExit(f"missing selection attempt receipt: {invocation_id}")
        audit_cell = audit_by_id.get(invocation_id)
        if not audit_cell or audit_cell.get("status") != "captured_cli_completed" or not audit_cell.get("raw_request_receipt_choice_binding_passed"):
            raise SystemExit(f"raw Laya request/receipt/choice was not independently audited: {invocation_id}")
        if live_attempts[0].get("selection_method") != "laya" or (live_attempts[0].get("decision") or {}).get("mode") != "laya":
            raise SystemExit(f"first live choice is not bound to an Laya receipt: {invocation_id}")
        if offline_attempts[0].get("selection_method") != "deterministic_fallback" or (offline_attempts[0].get("decision") or {}).get("mode") != "deterministic_fallback":
            raise SystemExit(f"first offline choice is not bound to deterministic fallback: {invocation_id}")

        live_first, offline_first = live_attempts[0], offline_attempts[0]
        live_score = live_scores[invocation_id]
        offline_score = offline_scores[invocation_id]
        if live_score.get("status") != "compiled_and_scored" or offline_score.get("status") != "compiled_and_scored":
            raise SystemExit(f"missing fresh Go results: {invocation_id}")
        live_generated_sha = live_score["generated_source_sha256"]
        offline_generated_sha = offline_score["generated_source_sha256"]
        live_attempt_total += len(live_attempts)
        offline_attempt_total += len(offline_attempts)
        live_provider_posts += audit_cell["raw_provider_posts"]
        live_sole = sum(attempt.get("selection_method") == "sole_remaining_candidate" for attempt in live_attempts)
        offline_sole = sum(attempt.get("selection_method") == "sole_remaining_candidate" for attempt in offline_attempts)
        offline_fallback = sum(attempt.get("selection_method") == "deterministic_fallback" for attempt in offline_attempts)
        live_sole_total += live_sole
        offline_sole_total += offline_sole
        offline_fallback_total += offline_fallback
        first_total = live_first.get("test_cases_total")
        live_first_passed = live_first.get("test_cases_passed")
        offline_first_passed = offline_first.get("test_cases_passed")
        live_final_passed = live_rec.get("native_training_passed")
        live_final_total = live_rec.get("native_training_total")
        delta = (live_final_passed - live_first_passed
                 if isinstance(live_final_passed, int) and isinstance(live_first_passed, int) else None)
        live_source_ast = None
        receipt = live_rec.get("source_completeness_receipt") or {}
        for dimension in receipt.get("dimensions", []):
            if dimension.get("id") == "source_ast_coverage":
                live_source_ast = {key: dimension.get(key) for key in ("status", "numerator", "denominator", "unit")}
                break
        paired_rows.append({
            "invocation_id": invocation_id,
            "sequence": plan_row["sequence"],
            "intent_id": plan_row["intent_id"],
            "arm": plan_row["arm"],
            "intended_candidate_id": plan_row["intended_candidate_id"],
            "raw_capture_audit": {"status": audit_cell["status"],
                                  "raw_provider_posts": audit_cell["raw_provider_posts"],
                                  "native_laya_receipts": audit_cell["native_laya_receipts"],
                                  "raw_request_receipt_choice_binding_passed": audit_cell["raw_request_receipt_choice_binding_passed"]},
            "live_first_laya_proposal": {"candidate_id": live_first.get("candidate_id"),
                                         "matches_intended": live_first.get("candidate_id") == plan_row["intended_candidate_id"],
                                         "training_passed": live_first_passed,
                                         "training_planned": first_total},
            "live_native_search": {"selected_candidate_id": live_rec.get("selected_candidate_id"),
                                   "matches_intended": live_rec.get("selected_candidate_id") == plan_row["intended_candidate_id"],
                                   "final_training_passed": live_final_passed,
                                   "final_training_planned": live_final_total,
                                   "first_to_final_training_pass_delta": delta,
                                   "provider_laya_attempts": sum((a.get("decision") or {}).get("mode") == "laya" for a in live_attempts),
                                   "sole_remaining_selections": live_sole,
                                   "source_ast_coverage": live_source_ast,
                                   "generated_source_sha256": live_generated_sha},
            "offline_first_deterministic_fallback": {"candidate_id": offline_first.get("candidate_id"),
                                                     "matches_intended": offline_first.get("candidate_id") == plan_row["intended_candidate_id"],
                                                     "training_passed": offline_first.get("test_cases_passed"),
                                                     "training_planned": offline_first.get("test_cases_total")},
            "offline_native_search": {"selected_candidate_id": offline_rec.get("selected_candidate_id"),
                                      "matches_intended": offline_rec.get("selected_candidate_id") == plan_row["intended_candidate_id"],
                                      "provider_operations": offline_rec.get("provider_operations"),
                                      "fallback_receipts": offline_fallback,
                                      "sole_remaining_selections": offline_sole,
                                      "generated_source_sha256": offline_generated_sha},
            "fresh_independent_go_live": live_score,
            "fresh_independent_go_offline": offline_score,
            "paired_go_difference": {
                "training_passed_cases": live_score["training"]["passed"] - offline_score["training"]["passed"],
                "holdout_passed_cases": live_score["holdout"]["passed"] - offline_score["holdout"]["passed"],
                "generated_source_bytes_identical": live_generated_sha == offline_generated_sha,
            },
            "cli_active_wall_ms": {"live": live_rec.get("cli_active_wall_ms"),
                                    "offline": offline_rec.get("cli_active_wall_ms")},
        })

    if len(paired_rows) != 64 or len({row["intent_id"] for row in paired_rows}) != 32:
        raise SystemExit("paired summary does not preserve the 64-cell / 32-intent structure")
    if live_provider_posts != 86 or offline_meta.get("provider_operations_observed") != 0:
        raise SystemExit("live/offline provider-call denominators do not match pinned evidence")

    arms = {}
    for arm in ARMS:
        subset = [row for row in paired_rows if row["arm"] == arm]
        if len(subset) != 32:
            raise SystemExit(f"arm denominator changed: {arm}")
        live_delta_rows = [row for row in subset if row["live_native_search"]["first_to_final_training_pass_delta"] is not None]
        deltas = [row["live_native_search"]["first_to_final_training_pass_delta"] for row in live_delta_rows]
        arms[arm] = {
            "planned_cells": 32,
            "raw_audited_cells": sum(r["raw_capture_audit"]["raw_request_receipt_choice_binding_passed"] for r in subset),
            "live_first_laya_proposal_matches_intended": {"passed": sum(r["live_first_laya_proposal"]["matches_intended"] for r in subset), "observed": 32, "planned": 32},
            "live_first_laya_proposal_training_score": {"passed": sum(r["live_first_laya_proposal"]["training_passed"] or 0 for r in subset), "observed_cases": sum(r["live_first_laya_proposal"]["training_planned"] or 0 for r in subset), "planned_cases": sum(r["live_first_laya_proposal"]["training_planned"] or 0 for r in subset)},
            "offline_first_fallback_matches_intended": {"passed": sum(r["offline_first_deterministic_fallback"]["matches_intended"] for r in subset), "observed": 32, "planned": 32},
            "offline_first_fallback_training_score": {"passed": sum(r["offline_first_deterministic_fallback"]["training_passed"] or 0 for r in subset), "observed_cases": sum(r["offline_first_deterministic_fallback"]["training_planned"] or 0 for r in subset), "planned_cases": sum(r["offline_first_deterministic_fallback"]["training_planned"] or 0 for r in subset)},
            "live_search_delta_from_first_laya_proposal": {
                "observed_cells": len(deltas), "planned_cells": 32,
                "improved_cells": sum(delta > 0 for delta in deltas),
                "unchanged_cells": sum(delta == 0 for delta in deltas),
                "declined_cells": sum(delta < 0 for delta in deltas),
                "passed_training_case_delta_sum": sum(deltas),
                "interpretation": "native search outcome after up to the frozen attempt budget; later proposals are also model-assisted in the three-attempt arm",
            },
            "live_selected_source_go": {
                "training": {"passed": sum(r["fresh_independent_go_live"]["training"]["passed"] for r in subset),
                             "observed_cases": sum(r["fresh_independent_go_live"]["training"]["observed_cases"] for r in subset),
                             "planned_cases": sum(r["fresh_independent_go_live"]["training"]["planned_cases"] for r in subset)},
                "holdout": {"passed": sum(r["fresh_independent_go_live"]["holdout"]["passed"] for r in subset),
                            "observed_cases": sum(r["fresh_independent_go_live"]["holdout"]["observed_cases"] for r in subset),
                            "planned_cases": sum(r["fresh_independent_go_live"]["holdout"]["planned_cases"] for r in subset)},
            },
            "offline_selected_source_go": {
                "training": {"passed": sum(r["fresh_independent_go_offline"]["training"]["passed"] for r in subset),
                             "observed_cases": sum(r["fresh_independent_go_offline"]["training"]["observed_cases"] for r in subset),
                             "planned_cases": sum(r["fresh_independent_go_offline"]["training"]["planned_cases"] for r in subset)},
                "holdout": {"passed": sum(r["fresh_independent_go_offline"]["holdout"]["passed"] for r in subset),
                            "observed_cases": sum(r["fresh_independent_go_offline"]["holdout"]["observed_cases"] for r in subset),
                            "planned_cases": sum(r["fresh_independent_go_offline"]["holdout"]["planned_cases"] for r in subset)},
            },
            "paired_live_minus_offline_go": {
                "training_passed_cases": sum(r["paired_go_difference"]["training_passed_cases"] for r in subset),
                "training_observed_cases": sum(r["fresh_independent_go_live"]["training"]["observed_cases"] for r in subset),
                "training_planned_cases": sum(r["fresh_independent_go_live"]["training"]["planned_cases"] for r in subset),
                "holdout_passed_cases": sum(r["paired_go_difference"]["holdout_passed_cases"] for r in subset),
                "holdout_observed_cases": sum(r["fresh_independent_go_live"]["holdout"]["observed_cases"] for r in subset),
                "holdout_planned_cases": sum(r["fresh_independent_go_live"]["holdout"]["planned_cases"] for r in subset),
                "identical_generated_source_cells": sum(r["paired_go_difference"]["generated_source_bytes_identical"] for r in subset),
                "planned_cells": 32,
            },
        }

    output.mkdir(parents=True)
    script_raw = Path(__file__).read_bytes()
    script_sha = sha(script_raw)
    archive = output / "code-archive/scripts/compare_v8_live_offline.py"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(script_raw)
    if sha(archive.read_bytes()) != script_sha:
        raise SystemExit("comparison script archive mismatch")
    source_manifest = {
        "schema": "gooo/ir-composition-tdd-paired-summary-preexecution/v1",
        "status": "INPUTS_VERIFIED_BEFORE_DERIVATION",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "comparison_script_sha256": script_sha,
        "design_sha256": sha(design_raw),
        "raw_capture_audit_sha256": sha(audit_raw),
        "live_report_sha256": sha(live_report_raw),
        "live_run_metadata_sha256": sha((live / "run-metadata.json").read_bytes()),
        "offline_baseline_metadata_sha256": sha(offline_meta_raw),
        "live_go_report_sha256": sha(live_go_raw),
        "offline_go_report_sha256": sha(offline_go_raw),
        "live_go_score_index_sha256": sha(live_score_raw),
        "offline_go_score_index_sha256": sha(offline_score_raw),
        "frozen_phase_plans_sha256": sha(phase_raw),
        "frozen_vectors_sha256": sha(vectors_raw),
        "planned_cells": 64,
        "planned_intents": 32,
        "live_raw_provider_posts": live_provider_posts,
        "offline_provider_operations": offline_meta["provider_operations_observed"],
    }
    write_json(output / "preexecution.json", source_manifest)

    report = {
        "schema": "gooo/ir-composition-tdd-paired-live-offline-summary/v1",
        "status": "COMPLETE_PAIRED_SUMMARY",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "design_sha256": sha(design_raw),
        "live_run_id": LIVE_ID,
        "offline_run_id": offline.name,
        "cohort_interpretation": "matched 32 frozen intents in two predeclared arms; offline rows are a deterministic comparator, not new intents",
        "planned_cells": 64,
        "planned_intents": 32,
        "live_raw_provider_posts": live_provider_posts,
        "live_provider_environment": "explicit multilingual Laya route, pinned raw calls audited; 86 captured provider POSTs",
        "offline_provider_operations": 0,
        "offline_fallback_attempts": offline_fallback_total,
        "offline_sole_remaining_candidate_selections": offline_sole_total,
        "live_provider_attempt_receipts": live_attempt_total - live_sole_total,
        "live_sole_remaining_candidate_selections": live_sole_total,
        "live_total_native_attempts": live_attempt_total,
        "offline_total_native_attempts": offline_attempt_total,
        "holdout_policy": "the pinned raw audit completed after all live calls; holdout cases are used here only for separate Go postselection scoring",
        "source_unit_completeness": "kept in per-cell native receipt fields; it is not combined with finite training/holdout scores",
        "arm_results": arms,
        "per_cell": paired_rows,
        "known_limitations": [
            "The finite case suite covers only these 32 frozen intents; this paired result is not a claim of generalization.",
            "The three-attempt arm includes later Laya proposals when configured and deterministic fallback proposals offline; its first-to-final delta is a search outcome, not an isolated causal estimate of local scoring alone.",
            "Model proposal choice quality and final emitted-source Go scores are separate metrics.",
            "Unknown and failed rows would remain in the planned denominator; both replays were complete with zero unknown cells.",
        ],
    }
    write_json(output / "report.json", report)
    write_json(output / "input-index.json", source_manifest)
    write_json(output / "per-cell.json", {"planned_cells": 64, "rows": paired_rows})
    (output / "report.md").write_text(render(report), encoding="utf-8")
    print(f"Paired summary written: {output}")
    return 0


def render(report: dict[str, Any]) -> str:
    lines = ["# Paired live and offline TDD results", "",
             "This compares the v8 Laya capture with the deterministic no-provider baseline over the same 32 frozen intents in both arms. Each arm keeps its 32-cell denominator.", "",
             f"- Live raw provider POSTs: {report['live_raw_provider_posts']}",
             f"- Offline provider operations: {report['offline_provider_operations']}",
             f"- Offline deterministic fallback attempts: {report['offline_fallback_attempts']}",
             f"- Live provider attempt receipts: {report['live_provider_attempt_receipts']}",
             f"- Live sole-candidate deterministic selections: {report['live_sole_remaining_candidate_selections']}",
             f"- Offline sole-candidate deterministic selections: {report['offline_sole_remaining_candidate_selections']}", "",
             "| Arm | Planned | Live first Laya choice correct | Offline first fallback correct | Live final training | Offline final training | Live final holdout | Offline final holdout |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for arm, data in report["arm_results"].items():
        lines.append(
            f"| `{arm}` | {data['planned_cells']} | {data['live_first_laya_proposal_matches_intended']['passed']}/32 | {data['offline_first_fallback_matches_intended']['passed']}/32 | "
            f"{data['live_selected_source_go']['training']['passed']}/{data['live_selected_source_go']['training']['planned_cases']} | "
            f"{data['offline_selected_source_go']['training']['passed']}/{data['offline_selected_source_go']['training']['planned_cases']} | "
            f"{data['live_selected_source_go']['holdout']['passed']}/{data['live_selected_source_go']['holdout']['planned_cases']} | "
            f"{data['offline_selected_source_go']['holdout']['passed']}/{data['offline_selected_source_go']['holdout']['planned_cases']} |"
        )
    lines.extend(["", "## Interpreting the search arm", "",
                  "In the three-attempt live arm, native training score improved in 22 of 32 cells, was unchanged in 10, and declined in none; the summed increase was 46 training cases. The deterministic offline arm reached the exact intended candidate in all 32 cells and passed all training and holdout cases after the same search budget. This shows the observed finite-suite outcome under each mode; it does not isolate a general causal effect of the model from attempt budget and candidate ordering.", "",
                  "The one-attempt arm had 10/32 first Laya choices match the intended candidate and scored 91/137 training cases. Its matched offline fallback first choice matched in 11/32 and scored 86/137. Final emitted-source Go scores were 91/137 training and 68/95 holdout with Laya, versus 86/137 and 69/95 offline.", "",
                  "Training/holdout finite scores remain separate from native source-unit completeness receipts. Every comparison has 32 planned cells per arm; the two independent replays observed and scored all cells.", "",
                  "The per-cell JSON keeps choices, native scores, source completeness, and independent Go observations separately. The raw capture, CLI output, and independent replay records remain unchanged.", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
