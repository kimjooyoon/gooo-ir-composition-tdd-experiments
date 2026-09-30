#!/usr/bin/env python3
"""Model-free CI audit for the published v8 capture and paired replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = ROOT / "study-design-v8"
CHECKPOINT_DIR = ROOT / "preexecution-checkpoint-v8"
LIVE_ID = "ir-composition-tdd-v8-20260930T104054Z-d868d3ee7f71"
LIVE_DIR = ROOT / "results" / LIVE_ID
OFFLINE_DIR = ROOT / "results" / "zero-model-v8-20260930T1055Z"
PAIRED_DIR = ROOT / "results" / "paired-live-offline-v8-20260930T1058Z"
FROZEN_AUDIT = CHECKPOINT_DIR / "independent-validation/v8-capture-audit.json"
DESIGN_SHA = "ab691ac00f73abc1cef9648da58c20a1816d7ab16cda7e59536054bea9a66b52"


class AuditError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> tuple[bytes, Any]:
    require(path.is_file() and not path.is_symlink(), f"missing or symlinked JSON file: {path}")
    raw = path.read_bytes()
    try:
        return raw, json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"invalid JSON in {path}: {exc}") from exc


def contained_file(base: Path, relative: str, label: str) -> Path:
    rel = Path(relative)
    require(not rel.is_absolute() and ".." not in rel.parts,
            f"{label} path is not a safe relative path: {relative}")
    path = base / rel
    require(path.resolve().is_relative_to(base.resolve()) and path.is_file() and not path.is_symlink(),
            f"{label} is missing, symlinked, or escapes its evidence directory: {relative}")
    return path


def checked_output_path(path: Path) -> Path:
    output = path.resolve()
    require(not output.is_relative_to(ROOT),
            "audit output must be outside the repository so it cannot alter frozen evidence")
    return output


def verify_capture_binding() -> dict[str, Any]:
    _, design = read_json(DESIGN_DIR / "study-design.json")
    report_raw, report = read_json(LIVE_DIR / "report.json")
    metadata_raw, metadata = read_json(LIVE_DIR / "run-metadata.json")
    binding_path = LIVE_DIR / "capture-authorization-binding.json"
    binding_raw, binding = read_json(binding_path)
    require(sha((DESIGN_DIR / "study-design.json").read_bytes()) == DESIGN_SHA,
            "v8 design bytes differ from the published design SHA")
    require(binding.get("schema") == "gooo/ir-composition-tdd-capture-authorization-binding/v1"
            and binding.get("run_id") == LIVE_ID,
            "capture authorization binding schema or run identity differs")
    require(report.get("status") == "CAPTURED" and metadata.get("status") == "CAPTURED"
            and metadata.get("run_id") == LIVE_ID,
            "the raw capture and run metadata are not complete v8 evidence")

    authorization = binding.get("authorization", {})
    auth_path = contained_file(ROOT, authorization.get("path", ""), "capture authorization receipt")
    auth_raw = auth_path.read_bytes()
    require(sha(auth_raw) == authorization.get("sha256"),
            "capture authorization receipt digest differs from its binding")
    require(sha((CHECKPOINT_DIR / "capture-authorization-receipt.json").read_bytes())
            == authorization.get("sha256"),
            "bound capture authorization is not the published v8 checkpoint receipt")

    checked_runtime_files: list[str] = []
    runtime = binding.get("runtime_evidence", {})
    require(isinstance(runtime, dict) and runtime,
            "capture authorization binding lacks runtime file evidence")
    for relative, expected in runtime.items():
        path = contained_file(LIVE_DIR, relative, "bound runtime evidence")
        raw = path.read_bytes()
        require(len(raw) == expected.get("bytes") and sha(raw) == expected.get("sha256"),
                f"capture authorization binding does not match {relative}")
        checked_runtime_files.append(relative)

    checked_analysis_files: list[str] = []
    for name, expected in binding.get("analysis_files", {}).items():
        path = contained_file(ROOT, expected.get("path", ""), f"capture analysis {name}")
        require(sha(path.read_bytes()) == expected.get("sha256"),
                f"capture analysis digest differs for {name}")
        checked_analysis_files.append(name)
    require(set(checked_analysis_files) == {"json", "markdown"},
            "capture authorization binding lacks both analysis outputs")

    frozen = binding.get("frozen_inputs", {})
    preexecution_raw = (LIVE_DIR / "preexecution.json").read_bytes()
    phase_raw = (LIVE_DIR / "phase-plans.json").read_bytes()
    require(frozen.get("design_sha256") == DESIGN_SHA
            and frozen.get("phase_plans_sha256") == sha(phase_raw)
            and frozen.get("preexecution_sha256") == sha(preexecution_raw)
            and frozen.get("capture_runner_sha256") == design.get("study_code_provenance", {}).get("capture_script_sha256")
            and frozen.get("model") == design.get("provider", {}).get("model")
            and frozen.get("model_revision") == design.get("provider", {}).get("model_revision"),
            "capture authorization binding does not match the v8 source/model/input pins")

    outcome = binding.get("capture_outcome", {})
    summary_raw, summary = read_json(LIVE_DIR / "summary.json")
    replay_raw, replay = read_json(LIVE_DIR / "independent-go/replay-results.json")
    fallback_receipts = sum(arm.get("deterministic_fallback_receipt_count", 0)
                           for arm in summary.get("arms", {}).values())
    expected_counts = {
        "status": "CAPTURED",
        "planned_cells": 64,
        "completed_cells": 64,
        "validated_cells": 64,
        "existing_intents": 32,
        "new_intents": 0,
        "provider_posts": 86,
        "health_checks": 86,
        "post_cap": 96,
        "warmups": 0,
        "retries": 0,
        "fallbacks": 0,
    }
    for key, expected in expected_counts.items():
        require(outcome.get(key) == expected, f"capture authorization outcome has wrong {key}")
    require(fallback_receipts == outcome.get("fallbacks"),
            "capture authorization fallback count differs from the raw-derived native summary")
    require(report.get("scheduled_cells") == outcome["planned_cells"]
            and report.get("completed_cli_cells") == outcome["completed_cells"]
            and report.get("raw_provider_posts") == outcome["provider_posts"]
            and metadata.get("actual_provider_posts") == outcome["provider_posts"]
            and metadata.get("measured_provider_post_cap") == outcome["post_cap"],
            "bound capture outcome disagrees with the original raw report or metadata")
    require(outcome.get("owned_service_stopped") is True
            and outcome.get("owned_service_pid") == report.get("owned_service_shutdown", {}).get("pid")
            and outcome.get("owned_service_return_code") == report.get("owned_service_shutdown", {}).get("return_code")
            and report.get("owned_service_shutdown", {}).get("confirmed") is True,
            "owned service shutdown evidence differs between binding and report")
    require(outcome.get("drain_settled_pre_shutdown") is True
            and outcome.get("drain_settled_post_shutdown") is True
            and report.get("provider_forward_drain", {}).get("pre_shutdown", {}).get("settled") is True
            and report.get("provider_forward_drain", {}).get("post_shutdown", {}).get("settled") is True,
            "capture authorization binding does not attest settled forwards before/after shutdown")
    require(outcome.get("independent_go_after_shutdown") == {"compiled_and_scored": 64, "planned": 64}
            and isinstance(replay, list)
            and len(replay) == 64
            and sum(row.get("status") == "compiled_and_scored" for row in replay) == 64,
            "post-shutdown independent Go execution evidence is incomplete")
    require(binding.get("public_checkpoint", {}).get("commit")
            and LIVE_ID.endswith(binding["public_checkpoint"]["commit"][:12]),
            "capture authorization binding does not identify the public v8 checkpoint")

    return {
        "status": "PASS_CAPTURE_BINDING",
        "binding_sha256": sha(binding_raw),
        "raw_report_sha256": sha(report_raw),
        "run_metadata_sha256": sha(metadata_raw),
        "summary_sha256": sha(summary_raw),
        "go_replay_sha256": sha(replay_raw),
        "authorization_sha256": sha(auth_raw),
        "runtime_files_verified": len(checked_runtime_files),
        "analysis_files_verified": checked_analysis_files,
        "planned_cells": 64,
        "completed_cells": 64,
        "raw_provider_posts": 86,
    }


def remove_created_utc(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: remove_created_utc(item) for key, item in value.items() if key != "created_utc"}
    if isinstance(value, list):
        return [remove_created_utc(item) for item in value]
    return value


def verify_raw_audit(temp: Path) -> dict[str, Any]:
    # The preserved post-capture binding uses the v8 schema; the frozen raw
    # checker predates that sidecar and expects its older outcome fields. Verify
    # the original binding above, then give the checker an otherwise byte-identical
    # temporary view without that optional sidecar. No captured evidence is edited.
    compat_root = temp / "compatibility-view"
    compat_run = compat_root / LIVE_ID
    shutil.copytree(LIVE_DIR, compat_run,
                    ignore=shutil.ignore_patterns("capture-authorization-binding.json"))
    audit_path = temp / "raw-capture-audit.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/validate_frozen_study.py"),
        "--root", str(ROOT),
        "--design-dir", "study-design-v8",
        "--checkpoint-dir", "preexecution-checkpoint-v8",
        "--run-dir", str(compat_run),
        "--output", str(audit_path),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    require(result.returncode == 0,
            "frozen raw audit failed: " + (result.stderr or result.stdout).strip()[-2500:])
    _, fresh = read_json(audit_path)
    audit = fresh.get("capture_run_audit", {})
    require(fresh.get("status") == "PASS"
            and audit.get("status") == "PASS_RAW_AUDIT_CAPTURED"
            and audit.get("run_id") == LIVE_ID
            and audit.get("design_sha256") == DESIGN_SHA
            and audit.get("completed_cli_cells_raw_observed") == 64
            and audit.get("independently_audited_cells_raw_request_receipt_choice") == 64
            and audit.get("not_started_cells") == 0,
            "frozen raw audit did not validate all 64 planned cells")
    require(audit.get("provider_events", {}).get("provider_posts") == 86
            and audit.get("provider_events", {}).get("health_checks") == 86,
            "frozen raw audit provider/health event counts differ from the authorized capture")
    require(len(audit.get("per_cell", [])) == 64
            and all(row.get("raw_request_receipt_choice_binding_passed") is True
                    and row.get("status") == "captured_cli_completed"
                    for row in audit["per_cell"]),
            "one or more per-cell request, receipt, choice, or CLI raw bindings failed")

    _, historical = read_json(FROZEN_AUDIT)
    historical_audit = historical.get("capture_run_audit", {})
    require(historical.get("status") == "PASS"
            and historical_audit.get("status") == "PASS_RAW_AUDIT_CAPTURED"
            and historical_audit.get("run_id") == LIVE_ID
            and historical_audit.get("design_sha256") == DESIGN_SHA,
            "published historical raw audit is not a PASS for the exact v8 run/design")
    by_id = {row["invocation_id"]: row for row in audit["per_cell"]}
    old_by_id = {row["invocation_id"]: row for row in historical_audit.get("per_cell", [])}
    require(set(by_id) == set(old_by_id) and len(by_id) == 64,
            "fresh raw audit cells differ from the stored independent audit")
    for invocation_id, row in by_id.items():
        old = old_by_id[invocation_id]
        for key in ("status", "exit_code", "sequence", "arm", "raw_provider_posts",
                    "native_laya_receipts", "native_attempts", "first_proposal_candidate_id",
                    "first_proposal_matches_intended", "raw_request_receipt_choice_binding_passed"):
            require(row.get(key) == old.get(key),
                    f"fresh and stored raw audits disagree for {invocation_id}.{key}")
    return {
        "status": "PASS_RAW_AUDIT_CAPTURED",
        "audit_sha256": sha(audit_path.read_bytes()),
        "historical_audit_sha256": sha(FROZEN_AUDIT.read_bytes()),
        "planned_cells": 64,
        "audited_cells": 64,
        "provider_posts": 86,
        "health_checks": 86,
    }


def verify_paired_replay(temp: Path) -> dict[str, Any]:
    output_dir = temp / "paired-recomputed"
    command = [
        sys.executable,
        str(ROOT / "scripts/compare_v8_live_offline.py"),
        "--offline-run", str(OFFLINE_DIR),
        "--output-dir", str(output_dir),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    require(result.returncode == 0,
            "paired offline recomputation failed: " + (result.stderr or result.stdout).strip()[-2500:])
    checks = []
    for filename in ("report.json", "per-cell.json", "input-index.json", "preexecution.json"):
        _, recomputed = read_json(output_dir / filename)
        _, published = read_json(PAIRED_DIR / filename)
        require(remove_created_utc(recomputed) == remove_created_utc(published),
                f"recomputed paired result differs from published {filename}")
        checks.append(filename)
    require((output_dir / "report.md").read_bytes() == (PAIRED_DIR / "report.md").read_bytes(),
            "recomputed paired narrative differs from the published narrative")
    archive = output_dir / "code-archive/scripts/compare_v8_live_offline.py"
    require(archive.is_file() and archive.read_bytes() == (ROOT / "scripts/compare_v8_live_offline.py").read_bytes(),
            "paired output did not archive the exact comparison script")
    _, report = read_json(PAIRED_DIR / "report.json")
    require(report.get("status") == "COMPLETE_PAIRED_SUMMARY"
            and report.get("planned_cells") == 64
            and report.get("planned_intents") == 32
            and report.get("live_raw_provider_posts") == 86
            and report.get("offline_provider_operations") == 0,
            "published paired report changed its planned or provider-call denominators")
    for arm, values in report.get("arm_results", {}).items():
        require(values.get("planned_cells") == 32
                and values.get("raw_audited_cells") == 32
                and values.get("live_selected_source_go", {}).get("training", {}).get("planned_cases") == 137
                and values.get("live_selected_source_go", {}).get("holdout", {}).get("planned_cases") == 95
                and values.get("offline_selected_source_go", {}).get("training", {}).get("planned_cases") == 137
                and values.get("offline_selected_source_go", {}).get("holdout", {}).get("planned_cases") == 95,
                f"paired arm denominators changed: {arm}")
    return {"status": "PASS_PAIRED_REPLAY", "published_report_sha256": sha((PAIRED_DIR / "report.json").read_bytes()),
            "recomputed_artifacts_compared": checks + ["report.md"], "planned_cells": 64,
            "planned_intents": 32, "live_raw_provider_posts": 86,
            "offline_provider_operations": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write a JSON audit receipt outside evidence directories")
    args = parser.parse_args()
    try:
        binding_result = verify_capture_binding()
        with tempfile.TemporaryDirectory(prefix="tdd-v8-publication-audit-") as temporary:
            temp = Path(temporary)
            raw_result = verify_raw_audit(temp)
            paired_result = verify_paired_replay(temp)
        report: dict[str, Any] = {
            "schema": "gooo/ir-composition-tdd-v8-publication-audit/v1",
            "status": "PASS",
            "design_sha256": DESIGN_SHA,
            "live_run_id": LIVE_ID,
            "capture_authorization_binding": binding_result,
            "raw_capture_audit": raw_result,
            "matched_live_offline_replay": paired_result,
            "execution_policy": {"provider_calls": 0, "go_toolchain_calls": 0,
                                 "tokenizer_or_model_calls": 0,
                                 "raw_capture_files_modified": False,
                                 "frozen_study_scripts_modified": False},
        }
        raw = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
        if args.output:
            output = checked_output_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
        sys.stdout.buffer.write(raw)
        return 0
    except Exception as exc:
        failure = {"schema": "gooo/ir-composition-tdd-v8-publication-audit/v1",
                   "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        raw = (json.dumps(failure, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
        if args.output:
            output = checked_output_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
        sys.stdout.buffer.write(raw)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
