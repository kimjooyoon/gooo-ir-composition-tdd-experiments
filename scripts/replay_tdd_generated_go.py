#!/usr/bin/env python3
"""Independently compile and score emitted TDD study sources against frozen vectors.

This replay consumes either a completed live capture or the separate no-provider
baseline output. It preserves every scheduled row and reports missing/failed
outputs as unknown rather than shrinking finite-suite denominators.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GO_PIN = Path("/Users/alice/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.27.1.darwin-arm64/bin/go")
DESIGN_SHA = "ab691ac00f73abc1cef9648da58c20a1816d7ab16cda7e59536054bea9a66b52"
GO_SHA = "a19a71df81715c12d9a7e81bab036c12696fec1ddbd4258b48a2131a9080b267"
LIVE_RUN_ID = "ir-composition-tdd-v8-20260930T104054Z-d868d3ee7f71"
LIVE_CAPTURE_AUDIT = ROOT / "preexecution-checkpoint-v8/independent-validation/v8-capture-audit.json"
GO_MOD = b"module example.invalid/ir-composition-study\n\ngo 1.27.1\n"
ARMS = ("compact_multilingual_single", "compact_multilingual_local_feedback")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> tuple[bytes, Any]:
    raw = path.read_bytes()
    return raw, json.loads(raw)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, data: Any) -> None:
    write_bytes(path, (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())


def contained(base: Path, relative: str) -> Path:
    candidate = (base / relative).resolve()
    candidate.relative_to(base.resolve())
    return candidate


def base_result(planned_train: int, planned_holdout: int) -> dict[str, Any]:
    return {
        "status": "unknown",
        "training": {"passed": None, "observed_cases": 0, "planned_cases": planned_train, "cases": []},
        "holdout": {"passed": None, "observed_cases": 0, "planned_cases": planned_holdout, "cases": []},
    }


def test_source(activity: str, suite: list[dict[str, Any]]) -> bytes:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", activity):
        raise ValueError(f"unsafe activity symbol: {activity!r}")
    literals = "\n".join(
        f'{{split: {json.dumps(case["split"])}, input: int64({case["input"]}), expected: int64({case["expected"]})}},'
        for case in suite
    )
    source = f'''package bodycodegen
import ("encoding/json"; "os"; "testing")
func TestIndependentFiniteReplay(t *testing.T) {{
 cases := []struct {{ split string; input, expected int64 }}{{
{literals}
 }}
 results := make([]struct {{ Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }}, 0, len(cases))
 for _, item := range cases {{ results = append(results, struct {{ Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }}{{item.split,item.input,item.expected,{activity}(item.input)}}) }}
 raw, err := json.Marshal(results); if err != nil {{ t.Fatal(err) }}
 if err := os.WriteFile("independent-results.json", raw, 0o644); err != nil {{ t.Fatal(err) }}
}}
'''
    return source.encode("utf-8")


def empty_group(planned_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "planned_cells": len(planned_rows),
        "source_observed_cells": 0,
        "go_compiled_and_executed_cells": 0,
        "unknown_cells": len(planned_rows),
        "training": {"passed_cases": 0, "observed_cases": 0,
                     "planned_cases": sum(r["training_case_count"] for r in planned_rows)},
        "holdout": {"passed_cases": 0, "observed_cases": 0,
                    "planned_cases": sum(r["holdout_case_count"] for r in planned_rows)},
    }


def aggregate(rows: list[dict[str, Any]], scores: list[dict[str, Any]]) -> dict[str, Any]:
    score_by_id = {r["invocation_id"]: r for r in scores}
    arms: dict[str, Any] = {}
    for arm in ARMS:
        planned = [r for r in rows if r["arm"] == arm]
        group = empty_group(planned)
        train_passed = train_observed = hold_passed = hold_observed = 0
        source_observed = go_ok = unknown_cells = 0
        for row in planned:
            invocation_id = f'{row["sequence"]:02d}-{row["intent_id"]}-{row["arm"]}'
            item = score_by_id[invocation_id]
            source_observed += bool(item.get("generated_source_sha256"))
            is_scored = item.get("status") == "compiled_and_scored"
            go_ok += is_scored
            unknown_cells += not is_scored
            for split_name, acc in (("training", "train"), ("holdout", "hold")):
                split = item[split_name]
                if split["passed"] is not None:
                    if acc == "train":
                        train_passed += split["passed"]
                        train_observed += split["observed_cases"]
                    else:
                        hold_passed += split["passed"]
                        hold_observed += split["observed_cases"]
        group.update({
            "source_observed_cells": source_observed,
            "go_compiled_and_executed_cells": go_ok,
            "unknown_cells": unknown_cells,
            "training": {"passed_cases": train_passed, "observed_cases": train_observed,
                         "planned_cases": group["training"]["planned_cases"]},
            "holdout": {"passed_cases": hold_passed, "observed_cases": hold_observed,
                        "planned_cases": group["holdout"]["planned_cases"]},
        })
        arms[arm] = group
    return {"arms": arms}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", required=True, type=Path,
                        help="completed live capture or completed offline baseline directory")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--design-dir", type=Path, default=ROOT / "study-design-v8")
    parser.add_argument("--mode", choices=("live", "offline"), required=True)
    parser.add_argument("--go-bin", type=Path, default=GO_PIN)
    parser.add_argument("--go-timeout-seconds", type=int, default=90)
    args = parser.parse_args()

    design_dir = args.design_dir.resolve()
    source_run = args.source_run.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    if not source_run.is_dir():
        raise SystemExit(f"source run directory is missing: {source_run}")
    if args.go_timeout_seconds < 1:
        raise SystemExit("Go timeout must be positive")

    design_raw, design = read_json(design_dir / "study-design.json")
    if sha(design_raw) != DESIGN_SHA:
        raise SystemExit("v8 frozen design SHA differs from the pinned digest")
    if args.mode == "live":
        source_meta_raw, source_meta = read_json(source_run / "run-metadata.json")
        if source_run.name != LIVE_RUN_ID or source_meta.get("status") != "CAPTURED":
            raise SystemExit("live replay requires the pinned completed v8 run")
        audit_raw, audit = read_json(LIVE_CAPTURE_AUDIT)
        run_audit = audit.get("capture_run_audit", {})
        if audit.get("status") != "PASS" or run_audit.get("status") != "PASS_RAW_AUDIT_CAPTURED":
            raise SystemExit("pinned independent raw capture audit is not PASS")
        if run_audit.get("run_id") != LIVE_RUN_ID or run_audit.get("design_sha256") != DESIGN_SHA:
            raise SystemExit("pinned raw capture audit belongs to a different run/design")
        raw_capture_audit_sha = sha(audit_raw)
    else:
        baseline_raw, baseline_meta = read_json(source_run / "baseline-metadata.json")
        source_meta_raw, source_meta = baseline_raw, baseline_meta
        if baseline_meta.get("status") not in ("CAPTURED_ZERO_MODEL", "PARTIAL_ZERO_MODEL"):
            raise SystemExit("offline baseline metadata is missing or has an invalid status")
        if baseline_meta.get("design_sha256") != DESIGN_SHA or baseline_meta.get("provider_endpoint_configured") is not False:
            raise SystemExit("offline baseline is not bound to the frozen design or has a provider endpoint")
        if baseline_meta.get("provider_operations_observed") != 0:
            raise SystemExit("offline baseline reports provider operations; refusing to score as deterministic")
        raw_capture_audit_sha = None

    go_bin = args.go_bin.resolve()
    if not go_bin.is_file() or sha(go_bin.read_bytes()) != GO_SHA:
        raise SystemExit("Go toolchain executable does not match the pinned Go 1.27 bytes")
    go_version = subprocess.run([str(go_bin), "version"], capture_output=True, check=False, timeout=15)
    if go_version.returncode != 0 or b"go1.27.1" not in go_version.stdout:
        raise SystemExit("pinned Go binary does not report go1.27.1")

    phase_raw, phase = read_json(source_run / "phase-plans.json")
    frozen_phase_raw, frozen_phase = read_json(design_dir / design["phase_plan_path"])
    if sha(phase_raw) != sha(frozen_phase_raw) or sha(phase_raw) != design["phase_plan_sha256"]:
        raise SystemExit("source run phase plan bytes differ from the v8 frozen plan")
    rows = phase.get("plans", [])
    if len(rows) != 64 or len({r["intent_id"] for r in rows}) != 32 or len({r["arm"] for r in rows}) != 2:
        raise SystemExit("v8 replay must retain all 64 planned cells across the two arms")

    output.mkdir(parents=True)
    script_raw = Path(__file__).read_bytes()
    script_sha = sha(script_raw)
    archive_script = output / "code-archive/scripts/replay_tdd_generated_go.py"
    write_bytes(archive_script, script_raw)
    if sha(archive_script.read_bytes()) != script_sha:
        raise SystemExit("could not archive the exact replay script bytes")
    write_bytes(output / "source-run/run-metadata.json", source_meta_raw)
    write_bytes(output / "source-run/phase-plans.json", phase_raw)

    source_root = contained(design_dir, design["source"]["path"])
    vectors_raw, vectors = read_json(source_root / "oracle/testdata/vectors.json")
    vector_by_id = {item["id"]: item for item in vectors}
    if len(vector_by_id) != 32:
        raise SystemExit("frozen oracle vector count is not 32")
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("GOOO_LAYA_") or key.startswith("LAYA_"):
            env.pop(key, None)
    env.update({"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off"})
    env["PATH"] = str(go_bin.parent) + os.pathsep + env.get("PATH", "")

    pre = {
        "schema": "gooo/ir-composition-tdd-independent-go-replay-preexecution/v1",
        "status": "ARCHIVED_BEFORE_GO_REPLAY",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "source_run_id": source_run.name,
        "design_sha256": sha(design_raw),
        "phase_plans_sha256": sha(phase_raw),
        "source_run_metadata_sha256": sha(source_meta_raw),
        "pinned_raw_capture_audit_sha256": raw_capture_audit_sha,
        "runner_script_sha256": script_sha,
        "runner_script_archive": str(archive_script.relative_to(output)),
        "go_binary": str(go_bin),
        "go_binary_sha256": sha(go_bin.read_bytes()),
        "go_version_stdout": go_version.stdout.decode(errors="replace").strip(),
        "go_version_stderr": go_version.stderr.decode(errors="replace").strip(),
        "go_environment": {k: env[k] for k in ("GOTOOLCHAIN", "GOPROXY", "GOSUMDB", "GOWORK")},
        "provider_environment_removed": True,
        "holdout_access": "post-capture scoring only; every captured CLI raw output is validated before holdout vectors are opened",
        "planned_cells": 64,
        "holdout_vector_file_sha256": sha(vectors_raw),
    }
    write_json(output / "preexecution.json", pre)

    records: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, row in enumerate(rows, 1):
        invocation_id = f'{row["sequence"]:02d}-{row["intent_id"]}-{row["arm"]}'
        if invocation_id in used_ids:
            raise SystemExit(f"duplicate invocation id {invocation_id}")
        used_ids.add(invocation_id)
        cell_source = source_run / "invocations" / invocation_id
        cell_output = output / "cells" / invocation_id
        cell_output.mkdir(parents=True)
        record_path = cell_source / "invocation.json"
        item: dict[str, Any] = {"invocation_id": invocation_id, "sequence": row["sequence"],
                                "intent_id": row["intent_id"], "arm": row["arm"],
                                "planned_training_cases": row["training_case_count"],
                                "planned_holdout_cases": row["holdout_case_count"],
                                "source_invocation_record_sha256": None}
        scored = base_result(row["training_case_count"], row["holdout_case_count"])
        record = None
        try:
            record_raw, record = read_json(record_path)
            item["source_invocation_record_sha256"] = sha(record_raw)
            stdout_path = contained(source_run, record["stdout_file"])
            stderr_path = contained(source_run, record["stderr_file"])
            stdout = stdout_path.read_bytes()
            stderr = stderr_path.read_bytes()
            stdout_sha = sha(stdout)
            stderr_sha = sha(stderr)
            item.update({"source_stdout_sha256": stdout_sha, "source_stderr_sha256": stderr_sha,
                         "source_stdout_bytes": len(stdout), "source_stderr_bytes": len(stderr)})
            expected_stdout = record.get("stdout_sha256")
            expected_stderr = record.get("stderr_sha256")
            if expected_stdout != stdout_sha or expected_stderr != stderr_sha:
                raise ValueError("source CLI stdout/stderr differs from invocation record hashes")
            plan_source_path = cell_source / "plan.search.json"
            fixture_source_path = cell_source / "fixture.gooo"
            plan_raw = plan_source_path.read_bytes()
            fixture_raw = fixture_source_path.read_bytes()
            if sha(plan_raw) != row["plan_sha256"] or sha(fixture_raw) != row["fixture_sha256"]:
                raise ValueError("copied plan/fixture bytes differ from frozen per-cell hashes")
            frozen_plan_raw = contained(design_dir, row["plan_path"]).read_bytes()
            frozen_fixture_raw = contained(source_root, row["fixture_path"]).read_bytes()
            if plan_raw != frozen_plan_raw or fixture_raw != frozen_fixture_raw:
                raise ValueError("cell plan/fixture bytes differ from frozen source files")
            plan = json.loads(plan_raw)
            vector = vector_by_id[row["intent_id"]]
            if plan.get("test_cases") != vector.get("training"):
                raise ValueError("body search plan training cases differ from frozen training vectors")
            if record.get("intent_id") != row["intent_id"] or record.get("arm") != row["arm"]:
                raise ValueError("invocation record intent/arm differs from frozen plan row")
            if record.get("completion") != "completed" or record.get("exit_code") != 0:
                scored["status"] = "cli_failed"
                item["status"] = "cli_failed"
                records.append(item)
                scores.append({"invocation_id": invocation_id, **scored})
                continue
            payload = json.loads(stdout)
            source = payload.get("source")
            report = payload.get("report", {})
            if not isinstance(source, str) or not source.strip():
                scored["status"] = "missing_emitted_source"
                item["status"] = scored["status"]
                records.append(item)
                scores.append({"invocation_id": invocation_id, **scored})
                continue
            source_bytes = source.encode("utf-8")
            source_sha = sha(source_bytes)
            item.update({"generated_source_sha256": source_sha,
                         "captured_selected_candidate_id": record.get("selected_candidate_id"),
                         "captured_first_proposal_candidate_id": (
                             (record.get("body_search_attempts") or [{}])[0].get("candidate_id")),
                         "captured_native_training_passed": record.get("native_training_passed"),
                         "captured_native_training_total": record.get("native_training_total"),
                         "captured_source_completeness_decision": (
                             (record.get("source_completeness_receipt") or {}).get("decision")),
                         "report_generated_digest": report.get("generated_digest")})
            expected_generated_digest = report.get("generated_digest")
            if expected_generated_digest and expected_generated_digest != "sha256:" + source_sha:
                raise ValueError("captured source bytes do not match compiler generated_digest")

            # Only now, after all raw CLI artifacts and frozen training bytes passed binding checks,
            # load postselection holdout values for the independent scorer.
            holdout_path = contained(design_dir, row["holdout_path"])
            holdout_raw = holdout_path.read_bytes()
            if sha(holdout_raw) != row["holdout_sha256"]:
                raise ValueError("frozen postselection holdout SHA differs from the plan row")
            holdout = json.loads(holdout_raw)
            if holdout != vector.get("evaluation") or len(holdout) != row["holdout_case_count"]:
                raise ValueError("holdout bytes differ from independent frozen vector evaluation cases")

            suite = [{"split": "training", **case} for case in plan["test_cases"]]
            suite.extend({"split": "holdout", **case} for case in holdout)
            module_dir = cell_output / "go"
            module_dir.mkdir()
            write_bytes(module_dir / "generated.go", source_bytes)
            generated_test = test_source(row["activity"], suite)
            write_bytes(module_dir / "generated_test.go", generated_test)
            write_bytes(module_dir / "go.mod", GO_MOD)
            write_json(cell_output / "input-binding.json", {
                "invocation_id": invocation_id,
                "source_cli_stdout_sha256": stdout_sha,
                "source_cli_invocation_record_sha256": sha(record_raw),
                "frozen_design_sha256": sha(design_raw),
                "frozen_plan_sha256": sha(plan_raw),
                "frozen_fixture_sha256": sha(fixture_raw),
                "frozen_holdout_sha256": sha(holdout_raw),
                "generated_source_sha256": source_sha,
                "generated_test_source_sha256": sha(generated_test),
                "go_mod_sha256": sha(GO_MOD),
                "activity": row["activity"],
            })
            try:
                result = subprocess.run([str(go_bin), "test", "-count=1", "./..."], cwd=module_dir,
                                        env=env, capture_output=True, check=False,
                                        timeout=args.go_timeout_seconds)
                write_bytes(cell_output / "go-stdout.raw", result.stdout)
                write_bytes(cell_output / "go-stderr.raw", result.stderr)
                values_path = module_dir / "independent-results.json"
                values = json.loads(values_path.read_bytes()) if values_path.is_file() else []
                value_ok = len(values) == len(suite)
                split_scores = {}
                for split in ("training", "holdout"):
                    subset = [value for value in values if value.get("split") == split]
                    planned = row["training_case_count"] if split == "training" else row["holdout_case_count"]
                    if len(subset) != planned:
                        value_ok = False
                    split_scores[split] = {
                        "passed": sum(v.get("actual") == v.get("expected") for v in subset) if len(subset) == planned else None,
                        "observed_cases": len(subset) if len(subset) == planned else 0,
                        "planned_cases": planned,
                        "cases": subset if len(subset) == planned else [],
                    }
                scored.update({"status": "compiled_and_scored" if result.returncode == 0 and value_ok else "go_compile_or_test_failed",
                               "go_exit_code": result.returncode,
                               "go_stdout_sha256": sha(result.stdout), "go_stderr_sha256": sha(result.stderr),
                               "go_stdout_bytes": len(result.stdout), "go_stderr_bytes": len(result.stderr),
                               "generated_source_sha256": source_sha,
                               "training": split_scores["training"], "holdout": split_scores["holdout"]})
                item.update({"status": scored["status"], "go_exit_code": result.returncode,
                             "go_stdout_sha256": sha(result.stdout), "go_stderr_sha256": sha(result.stderr),
                             "go_result_path": str((cell_output / "go/independent-results.json").relative_to(output))
                             if values_path.is_file() else None})
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout or b""
                stderr = error.stderr or b""
                if isinstance(stdout, str):
                    stdout = stdout.encode()
                if isinstance(stderr, str):
                    stderr = stderr.encode()
                write_bytes(cell_output / "go-stdout.raw", stdout)
                write_bytes(cell_output / "go-stderr.raw", stderr)
                scored["status"] = "go_timeout"
                item.update({"status": "go_timeout", "go_stdout_sha256": sha(stdout), "go_stderr_sha256": sha(stderr)})
        except Exception as error:
            scored["status"] = "input_validation_failed"
            item["status"] = "input_validation_failed"
            item["error"] = f"{type(error).__name__}: {error}"
        records.append(item)
        scores.append({"invocation_id": invocation_id, **scored})
        write_json(cell_output / "cell.json", {"invocation": item, "score": scored})
        print(f"[{index:02d}/64] {invocation_id}: {item.get('status', scored['status'])}", flush=True)

    group_summary = aggregate(rows, scores)
    completed = sum(row["status"] == "compiled_and_scored" for row in scores)
    report = {
        "schema": "gooo/ir-composition-tdd-independent-go-replay/v1",
        "status": "COMPLETE" if completed == 64 else "PARTIAL",
        "mode": args.mode,
        "source_run_id": source_run.name,
        "design_sha256": sha(design_raw),
        "planned_cells": 64,
        "source_rows_observed": sum(bool(r.get("generated_source_sha256")) for r in records),
        "compiled_and_scored_cells": completed,
        "unknown_cells": 64 - completed,
        "arms": group_summary["arms"],
        "proposal_metrics_source": "captured receipt/first raw proposal separately; this report scores only selected emitted Go",
        "source_unit_completeness_source": "native Gooo receipt, copied per row; never aggregated into finite vector scores",
        "training_holdout_separation": "training comes from the exact submitted search plan; holdout loaded after raw capture/audit and checked against independent vectors",
        "errors": [r for r in records if r.get("error")],
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output / "invocation-index.json", {"planned_cells": 64, "rows": records})
    write_json(output / "score-index.json", {"planned_cells": 64, "rows": scores})
    write_json(output / "report.json", report)
    write_bytes(output / "report.md", render_report(report).encode())
    final_meta = {"schema": "gooo/ir-composition-tdd-independent-go-replay-metadata/v1",
                  "status": report["status"], "runner_script_sha256": script_sha,
                  "preexecution_sha256": sha((output / "preexecution.json").read_bytes()),
                  "report_sha256": sha((output / "report.json").read_bytes()),
                  "planned_cells": 64, "compiled_and_scored_cells": completed,
                  "unknown_cells": 64 - completed}
    write_json(output / "run-metadata.json", final_meta)
    print(f"Replay finished: {completed}/64 scored; results: {output}", flush=True)
    return 0 if completed == 64 else 2


def render_report(report: dict[str, Any]) -> str:
    out = ["# Independent Go replay", "", f"Status: **{report['status']}**",
           f"Mode: `{report['mode']}`", f"Planned cells: {report['planned_cells']}",
           f"Compiled and scored: {report['compiled_and_scored_cells']}",
           f"Unknown cells: {report['unknown_cells']}", "",
           "Proposal-choice and source-completeness metrics stay in the captured CLI receipts. This replay only reports the selected emitted source against finite training and postselection holdout cases.", "",
           "| Arm | Cells planned | Go scored | Unknown | Training passed / observed / planned | Holdout passed / observed / planned |",
           "|---|---:|---:|---:|---:|---:|"]
    for arm, values in report["arms"].items():
        tr, ho = values["training"], values["holdout"]
        out.append(f"| `{arm}` | {values['planned_cells']} | {values['go_compiled_and_executed_cells']} | {values['unknown_cells']} | {tr['passed_cases']} / {tr['observed_cases']} / {tr['planned_cases']} | {ho['passed_cases']} / {ho['observed_cases']} / {ho['planned_cases']} |")
    if report["errors"]:
        out.extend(["", "## Cell validation errors", ""])
        out.extend(f"- `{item['invocation_id']}`: {item.get('error')}" for item in report["errors"])
    out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
