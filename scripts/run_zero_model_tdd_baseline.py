#!/usr/bin/env python3
"""Run the frozen 64-cell TDD plan with the pinned Gooo compiler and no provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = ROOT / "study-design-v8"
DESIGN_SHA = "ab691ac00f73abc1cef9648da58c20a1816d7ab16cda7e59536054bea9a66b52"
GOOO_PIN = Path("/private/tmp/gooo-composition-tdd-f3e576ad-20260930")
GOOO_SHA = "ecbae47a877f57117e4f493ab85adc0279956c437a68e9fa374bee1b3772db1f"
GO_PIN = Path("/Users/alice/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.27.1.darwin-arm64/bin/go")
GO_SHA = "a19a71df81715c12d9a7e81bab036c12696fec1ddbd4258b48a2131a9080b267"
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


def env_without_provider() -> tuple[dict[str, str], list[str]]:
    env = os.environ.copy()
    removed = []
    for key in tuple(env):
        if key.startswith("GOOO_LAYA_") or key.startswith("LAYA_"):
            removed.append(key)
            env.pop(key, None)
    # These flags prevent future library changes from attempting to populate local caches.
    env.update({"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off",
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1"})
    return env, sorted(removed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="new, empty path for the zero-model run; existing files are never overwritten")
    parser.add_argument("--design-dir", type=Path, default=DESIGN_DIR)
    parser.add_argument("--compiler", type=Path, default=GOOO_PIN)
    parser.add_argument("--go-bin", type=Path, default=GO_PIN)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()

    design_dir = args.design_dir.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    if args.timeout_seconds < 1:
        raise SystemExit("CLI timeout must be positive")
    design_raw, design = read_json(design_dir / "study-design.json")
    if sha(design_raw) != DESIGN_SHA:
        raise SystemExit("v8 design bytes do not match the frozen SHA")
    compiler = args.compiler.resolve()
    if not compiler.is_file() or sha(compiler.read_bytes()) != GOOO_SHA:
        raise SystemExit("Gooo compiler bytes do not match the frozen compiler pin")
    if compiler != Path(design["compiler"]["path"]).resolve() or design["compiler"].get("sha256") != GOOO_SHA:
        raise SystemExit("Gooo compiler path/digest differs from the v8 frozen compiler identity")
    go_bin = args.go_bin.resolve()
    if not go_bin.is_file() or sha(go_bin.read_bytes()) != GO_SHA:
        raise SystemExit("Go toolchain bytes do not match the pinned Go 1.27 executable")
    go_version = subprocess.run([str(go_bin), "version"], capture_output=True, check=False, timeout=15)
    if go_version.returncode != 0 or b"go1.27.1" not in go_version.stdout:
        raise SystemExit("pinned Go toolchain does not report go1.27.1")

    phase_source = design_dir / design["phase_plan_path"]
    phase_raw, phase = read_json(phase_source)
    if sha(phase_raw) != design["phase_plan_sha256"] or len(phase.get("plans", [])) != 64:
        raise SystemExit("frozen phase plan is missing, changed, or does not contain 64 cells")
    rows = phase["plans"]
    if {r["arm"] for r in rows} != set(ARMS) or any(sum(r["arm"] == arm for r in rows) != 32 for arm in ARMS):
        raise SystemExit("frozen phase plan does not have 32 rows in each of the two arms")
    source_root = (design_dir / design["source"]["path"]).resolve()
    for row in rows:
        plan_raw = (design_dir / row["plan_path"]).read_bytes()
        fixture_raw = (source_root / row["fixture_path"]).read_bytes()
        if sha(plan_raw) != row["plan_sha256"] or sha(fixture_raw) != row["fixture_sha256"]:
            raise SystemExit(f"frozen source plan/fixture digest mismatch: {row['intent_id']} / {row['arm']}")

    output.mkdir(parents=True)
    script_raw = Path(__file__).read_bytes()
    script_sha = sha(script_raw)
    script_archive = output / "code-archive/scripts/run_zero_model_tdd_baseline.py"
    write_bytes(script_archive, script_raw)
    if sha(script_archive.read_bytes()) != script_sha:
        raise SystemExit("could not archive exact baseline runner source bytes")
    write_bytes(output / "study-design.json", design_raw)
    write_bytes(output / "phase-plans.json", phase_raw)
    write_bytes(output / "study-design.sha256", (design_dir / "study-design.sha256").read_bytes())

    cli_env, removed_provider_keys = env_without_provider()
    # Ensure endpoint and credential variables are absent after all environment shaping.
    if any(key.startswith("GOOO_LAYA_") or key.startswith("LAYA_") for key in cli_env):
        raise SystemExit("provider configuration remains in the CLI environment")
    preexecution = {
        "schema": "gooo/ir-composition-tdd-zero-model-preexecution/v1",
        "status": "ARCHIVED_BEFORE_FIRST_CLI",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_label": "zero_model_deterministic_offline_baseline",
        "relationship_to_live_study": "matched baseline over the same 32 frozen intents x 2 arms; no new intent count",
        "design_sha256": sha(design_raw),
        "phase_plans_sha256": sha(phase_raw),
        "planned_cells": 64,
        "planned_cells_per_arm": 32,
        "runner_script_sha256": script_sha,
        "runner_script_archive": str(script_archive.relative_to(output)),
        "compiler_path": str(compiler),
        "compiler_sha256": sha(compiler.read_bytes()),
        "compiler_source_revision": design["compiler"].get("source_revision"),
        "go_binary_path": str(go_bin),
        "go_binary_sha256": sha(go_bin.read_bytes()),
        "go_version": go_version.stdout.decode(errors="replace").strip(),
        "provider_endpoint_configured": False,
        "provider_credentials_configured": False,
        "provider_environment_removed_keys": removed_provider_keys,
        "provider_actual_calls_before_execution": 0,
        "plan_and_fixture_policy": "copied exact bytes from the frozen v8 design for each invocation",
        "holdout_policy": "holdout files are not copied into the CLI workspace and are not read by this runner",
        "cli_timeout_seconds": args.timeout_seconds,
        "go_offline_environment": {key: cli_env[key] for key in
                                   ("GOTOOLCHAIN", "GOPROXY", "GOSUMDB", "GOWORK")},
        "cache_offline_environment": {key: cli_env[key] for key in
                                      ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
                                       "HF_HUB_DISABLE_TELEMETRY")},
    }
    write_json(output / "preexecution.json", preexecution)

    records = []
    provider_operations_total = 0
    any_failure = False
    any_provider_operation = False
    for index, row in enumerate(rows, 1):
        invocation_id = f'{row["sequence"]:02d}-{row["intent_id"]}-{row["arm"]}'
        cell = output / "invocations" / invocation_id
        cell.mkdir(parents=True)
        source_plan = (design_dir / row["plan_path"]).read_bytes()
        source_fixture = (source_root / row["fixture_path"]).read_bytes()
        plan_path = cell / "plan.search.json"
        fixture_path = cell / "fixture.gooo"
        write_bytes(plan_path, source_plan)
        write_bytes(fixture_path, source_fixture)
        argv = [str(compiler), "body-codegen", "--json", "--fill-search", str(plan_path),
                "--activity", row["activity"], str(fixture_path)]
        started = time.monotonic()
        started_utc = datetime.now(timezone.utc).isoformat()
        timeout = False
        try:
            proc = subprocess.run(argv, cwd=cell, env=cli_env, capture_output=True, check=False,
                                  timeout=args.timeout_seconds)
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as error:
            timeout = True
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            exit_code = None
        elapsed_ms = (time.monotonic() - started) * 1000
        write_bytes(cell / "stdout.raw", stdout)
        write_bytes(cell / "stderr.raw", stderr)
        payload = None
        parse_error = ""
        if exit_code == 0 and not timeout:
            try:
                payload = json.loads(stdout)
            except Exception as error:
                parse_error = f"{type(error).__name__}: {error}"
        report = (payload or {}).get("report", {})
        if not isinstance(report, dict):
            report = {}
        body = report.get("body_search", {}) if isinstance(report, dict) else {}
        attempts = body.get("attempts", []) if isinstance(body, dict) else []
        provider_modes = [((a.get("decision") or {}).get("mode")) for a in attempts]
        provider_operations = body.get("provider_operations", 0) if isinstance(body, dict) else 0
        if not isinstance(provider_operations, int) or provider_operations < 0:
            provider_operations = -1
        provider_operations_total += max(provider_operations, 0)
        laya_decisions = provider_modes.count("laya")
        fallback_decisions = provider_modes.count("deterministic_fallback")
        violation = provider_operations != 0 or laya_decisions != 0
        any_provider_operation = any_provider_operation or violation
        source = (payload or {}).get("source")
        generated_source_sha = sha(source.encode()) if isinstance(source, str) and source else None
        if report.get("generated_digest") and generated_source_sha:
            generated_digest_match = report["generated_digest"] == "sha256:" + generated_source_sha
        else:
            generated_digest_match = None
        if generated_digest_match is False:
            violation = True
        exit_status = "timeout" if timeout else ("success" if exit_code == 0 and payload is not None else "cli_or_parse_failure")
        if exit_status != "success" or violation:
            any_failure = True
        rec = {
            "schema": "gooo/ir-composition-tdd-zero-model-invocation/v1",
            "invocation_id": invocation_id,
            "sequence": row["sequence"],
            "intent_id": row["intent_id"],
            "arm": row["arm"],
            "activity": row["activity"],
            "provider_model_plan_label": row["provider_model"],
            "baseline_mode": "zero_model_deterministic_offline_baseline",
            "max_attempts": row["max_attempts"],
            "completion": "timed_out" if timeout else ("completed" if exit_code is not None else "unknown"),
            "status": exit_status,
            "exit_code": exit_code,
            "timeout_seconds": args.timeout_seconds,
            "cli_active_wall_ms": elapsed_ms,
            "started_utc": started_utc,
            "argv": argv,
            "working_directory": str(cell),
            "plan_file": "plan.search.json",
            "plan_sha256": sha(source_plan),
            "expected_plan_sha256": row["plan_sha256"],
            "fixture_file": "fixture.gooo",
            "fixture_sha256": sha(source_fixture),
            "expected_fixture_sha256": row["fixture_sha256"],
            "stdout_file": str((cell / "stdout.raw").relative_to(output)),
            "stdout_sha256": sha(stdout),
            "stdout_bytes": len(stdout),
            "stderr_file": str((cell / "stderr.raw").relative_to(output)),
            "stderr_sha256": sha(stderr),
            "stderr_bytes": len(stderr),
            "parse_error": parse_error,
            "generated_source_sha256": generated_source_sha,
            "report_generated_digest": report.get("generated_digest"),
            "generated_digest_matches_source": generated_digest_match,
            "selected_candidate_id": body.get("selected_candidate_id") if isinstance(body, dict) else None,
            "attempts": attempts,
            "provider_operations": provider_operations,
            "laya_mode_decision_count": laya_decisions,
            "deterministic_fallback_decision_count": fallback_decisions,
            "provider_operation_violation": violation,
            "completeness_receipt": report.get("completeness_receipt"),
            "error": parse_error or ("provider operation or generated digest mismatch in zero-model run" if violation else None),
        }
        if generated_source_sha and isinstance(source, str):
            write_bytes(cell / "generated.go", source.encode())
        write_json(cell / "invocation.json", rec)
        records.append(rec)
        print(f"[{index:02d}/64] {invocation_id}: {exit_status}; provider operations={provider_operations}", flush=True)

    # Confirm no provider variables leaked into the child environment after the run.
    metadata_status = "CAPTURED_ZERO_MODEL" if not any_failure and not any_provider_operation else "PARTIAL_ZERO_MODEL"
    metadata = {
        "schema": "gooo/ir-composition-tdd-zero-model-baseline-metadata/v1",
        "status": metadata_status,
        "baseline_label": "zero_model_deterministic_offline_baseline",
        "run_id": output.name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_live_run_id": "ir-composition-tdd-v8-20260930T104054Z-d868d3ee7f71",
        "design_sha256": sha(design_raw),
        "phase_plans_sha256": sha(phase_raw),
        "preexecution_sha256": sha((output / "preexecution.json").read_bytes()),
        "runner_script_sha256": script_sha,
        "planned_cells": 64,
        "completed_cli_cells": sum(r["status"] == "success" for r in records),
        "unknown_cells": 64 - sum(r["status"] == "success" for r in records),
        "provider_endpoint_configured": False,
        "provider_credentials_configured": False,
        "provider_environment_removed_keys": removed_provider_keys,
        "provider_operations_observed": provider_operations_total,
        "provider_operation_violation": any_provider_operation,
        "attempt_receipts": sum(len(r["attempts"]) for r in records),
        "deterministic_fallback_receipts": sum(r["deterministic_fallback_decision_count"] for r in records),
        "all_invocation_records": "invocations/<id>/invocation.json",
        "errors": [{"invocation_id": r["invocation_id"], "status": r["status"], "error": r.get("error")}
                   for r in records if r["status"] != "success" or r["provider_operation_violation"]],
    }
    write_json(output / "invocation-index.json", {"planned_cells": 64, "rows": records})
    write_json(output / "baseline-metadata.json", metadata)
    write_bytes(output / "README.md", render_markdown(metadata, records).encode())
    print(f"Offline baseline finished: {metadata['completed_cli_cells']}/64 CLI outputs; {provider_operations_total} provider operations; {output}", flush=True)
    return 0 if metadata_status == "CAPTURED_ZERO_MODEL" else 2


def render_markdown(meta: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = ["# Matched zero-model TDD baseline", "",
             f"Status: **{meta['status']}**", "",
             "This is the deterministic no-provider baseline for the same 32 frozen intents in both v8 arms. It is not counted as a new intent cohort. The original live capture remains in its own result directory.", "",
             f"- Planned cells: {meta['planned_cells']}",
             f"- Successful CLI outputs: {meta['completed_cli_cells']}",
             f"- Unknown or failed cells: {meta['unknown_cells']}",
             f"- Provider operations observed in native receipts: {meta['provider_operations_observed']}",
             f"- Attempt receipts: {meta['attempt_receipts']}",
             f"- Deterministic fallback receipts: {meta['deterministic_fallback_receipts']}", "",
             "| Arm | Planned | CLI success | Unknown | Provider operations | Fallback receipts |",
             "|---|---:|---:|---:|---:|---:|"]
    for arm in ARMS:
        subset = [r for r in rows if r["arm"] == arm]
        lines.append(f"| `{arm}` | {len(subset)} | {sum(r['status'] == 'success' for r in subset)} | {sum(r['status'] != 'success' for r in subset)} | {sum(max(r['provider_operations'], 0) for r in subset)} | {sum(r['deterministic_fallback_decision_count'] for r in subset)} |")
    if meta["errors"]:
        lines.extend(["", "## Failed or invalid rows", ""])
        lines.extend(f"- `{item['invocation_id']}`: {item['status']} ({item.get('error')})" for item in meta["errors"])
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
