#!/usr/bin/env python3
"""Sequentially capture the 64 frozen native CLI cells, once each, without retries."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = ROOT / "study-design-v3"
COMPILER_BINARY_SHA256 = "ecbae47a877f57117e4f493ab85adc0279956c437a68e9fa374bee1b3772db1f"
COMPILER_REVISION = "f3e576ad55796c0d42b2af8b86f874b49baa61d8"
MODEL_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
GO_VERSION = "go1.27.0"
LAYA_VERSION = "0.3.21"
WALL_TIMEOUT_SECONDS = 180
EXCHANGE_DRAIN_SECONDS = 14
LAYER_TIMEOUT_SECONDS = 10


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    write_bytes(path, raw)
    return raw


def read_json(path: Path):
    return json.loads(path.read_bytes())


def canonical_go_json(value) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (raw.replace(b"&", b"\\u0026").replace(b"<", b"\\u003c").replace(b">", b"\\u003e")
            .replace("\u2028".encode(), b"\\u2028").replace("\u2029".encode(), b"\\u2029"))


def typed_request_sha(outer: dict) -> str:
    state_wire = outer["state"]["request"]
    state = json.loads(state_wire)
    questions = outer["questions"]
    if len(questions) != 1 or not state.get("remaining_candidates"):
        raise RuntimeError("raw request does not contain one typed candidate question")
    question_id, question = next(iter(questions.items()))
    candidates = state["remaining_candidates"]
    request = {
        "schema": "gooo/typed-decision-request/v1",
        "state": state_wire,
        "question": {"id": question_id, "instructions": question["instructions"],
                     "options": [{"id": candidate["id"],
                                  "description": "Try this exact expression: " + candidate["expression"]}
                                 for candidate in candidates]},
        "fallback": candidates[0]["id"],
        "provider_model": outer["model"],
    }
    return "sha256:" + sha(canonical_go_json(request))


def recursive_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from recursive_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from recursive_keys(child)


def verify_design() -> tuple[dict, list[dict], bytes]:
    design_raw = (DESIGN_DIR / "study-design.json").read_bytes()
    expected = (DESIGN_DIR / "study-design.sha256").read_text(encoding="ascii").split()[0]
    if sha(design_raw) != expected:
        raise RuntimeError("frozen study design checksum mismatch")
    design = json.loads(design_raw)
    if (design.get("schema") != "gooo/ir-composition-tdd-study-design/v3"
            or design.get("preparation_revision") != 3
            or design.get("status") != "frozen_before_live_provider_calls"
            or design.get("planned_cells") != 64 or design.get("provider", {}).get("live_calls_authorized") is not False):
        raise RuntimeError("study design is absent, malformed, or was not frozen before live calls")
    provider_pin = design.get("provider", {})
    if (provider_pin.get("measured_provider_post_cap") != 96 or provider_pin.get("warmup_calls") != 0
            or provider_pin.get("provider_post_caps_by_arm") != {
                "compact_multilingual_single": 32, "compact_multilingual_local_feedback": 64}):
        raise RuntimeError("frozen design does not bind the 96-POST measured cap and zero warmups")
    code_pin = design.get("study_code_provenance", {})
    if (code_pin.get("capture_script_sha256") != sha(Path(__file__).read_bytes())
            or code_pin.get("preparation_script_sha256") != sha((ROOT / "scripts/prepare_study.py").read_bytes())):
        raise RuntimeError("preparation/capture runner bytes differ from the code pins in the frozen design")
    preflight_path = DESIGN_DIR / design["mock_preflight_path"]
    preflight_raw = preflight_path.read_bytes()
    if sha(preflight_raw) != design.get("mock_preflight_sha256"):
        raise RuntimeError("model-free mock/tokenizer preflight SHA differs from frozen design")
    preflight = json.loads(preflight_raw)
    if (preflight.get("status") != "PASS_MODEL_FREE_REACHABLE_TEMPLATE_PREFLIGHT"
            or preflight.get("completed_cells") != 64 or preflight.get("actual_laya_calls") != 0
            or preflight.get("provider_calls") != 0
            or preflight.get("mock_receipt_provenance") != "synthetic local response only; mode=laya is fixture data and does not indicate Laya inference"
            or preflight.get("branch_scenario_count") != 64
            or preflight.get("reachable_request_template_count") != 128
            or preflight.get("mock_choice_posts") != 214 or preflight.get("mock_health_checks") != 213
            or preflight.get("reachable_request_template_role_counts") != {
                "single_initial": 32, "search_initial": 32, "search_second": 64}
            or preflight.get("tokenizer", {}).get("request_count") != 128
            or preflight.get("tokenizer", {}).get("token_limit") != 1024
            or preflight.get("tokenizer", {}).get("status") != "PASS_CACHED_MULTILINGUAL_TOKENIZER_NO_INFERENCE"):
        raise RuntimeError("frozen model-free preflight is incomplete")
    negative = preflight.get("negative_routing_mismatch_fallback", {})
    if (negative.get("raw_post_count") != 1 or negative.get("provider_receipt_mode") != "deterministic_fallback"
            or negative.get("provider_fallback_reason") != "PROVIDER_RESULT_INVALID"
            or negative.get("actual_laya_calls") != 0
            or negative.get("rejected_reply_choice") == negative.get("native_selected_candidate")
            or preflight.get("sole_candidate_deterministic_smoke_count") != 1):
        raise RuntimeError("mock abnormal-path preflight did not prove wrong-route fallback and sole-candidate selection")
    negative_request = (DESIGN_DIR / "mock-preflight" / negative["request_file"]).read_bytes()
    negative_reply = (DESIGN_DIR / "mock-preflight" / negative["response_file"]).read_bytes()
    if (sha(negative_request) != negative.get("raw_request_sha256")
            or sha(negative_reply) != negative.get("raw_response_sha256")
            or typed_request_sha(json.loads(negative_request)) != negative.get("typed_request_sha256")):
        raise RuntimeError("mock wrong-routing negative case raw/typed digest binding changed")
    mock_aggregate = preflight.get("mock_aggregate_smoke", {}).get("arms", {})
    for arm in ("compact_multilingual_single", "compact_multilingual_local_feedback"):
        counts = mock_aggregate.get(arm, {})
        if (counts.get("planned_cells") != 32 or counts.get("captured_cli_successes") != 32
                or counts.get("capture_validated_cells") != 32
                or counts.get("raw_provider_posts") != counts.get("provider_routed_receipt_count")
                or counts.get("raw_provider_posts") != counts.get("native_provider_operations")
                or counts.get("first_proposal_matches_intended_candidate", {}).get("observed") != 32
                or counts.get("local_score_adjustment", {}).get("observed_cells") != 32):
            raise RuntimeError(f"model-free mock aggregate denominators or receipt counts changed for {arm}")
    mock_dir = DESIGN_DIR / "mock-preflight"
    mock_events_raw = (mock_dir / "events.json").read_bytes()
    if sha(mock_events_raw) != preflight.get("events_sha256"):
        raise RuntimeError("mock exchange event index changed after preparation")
    mock_events = json.loads(mock_events_raw).get("events", [])
    event_by_sequence = {event.get("sequence"): event for event in mock_events}
    if len(event_by_sequence) != len(mock_events):
        raise RuntimeError("mock exchange index contains duplicate sequence numbers")
    for event in mock_events:
        request_raw = (mock_dir / event["request_file"]).read_bytes()
        response_raw = (mock_dir / event["response_file"]).read_bytes()
        if (sha(request_raw) != event.get("request_sha256")
                or sha(response_raw) != event.get("response_sha256")
                or event.get("counted_as_laya_call") is not False):
            raise RuntimeError(f"model-free mock raw exchange failed its frozen SHA/no-inference check at {event.get('sequence')}")
    exchange_inventory = preflight.get("post_exchange_inventory", [])
    if len(exchange_inventory) != 214:
        raise RuntimeError("mock preflight does not inventory all 214 raw POST exchanges")
    mode_counts = {"laya": 0, "deterministic_fallback": 0}
    for item in exchange_inventory:
        event = event_by_sequence.get(item.get("event_sequence"))
        if (event is None or event.get("method") != "POST" or event.get("path") != "/v1/systemone"
                or event.get("kind") != "mock_choice" or event.get("request_sha256") != item.get("request_sha256")
                or event.get("response_sha256") != item.get("response_sha256")):
            raise RuntimeError("mock POST inventory does not bind to its exact raw event")
        request_raw = (mock_dir / item["request_file"]).read_bytes()
        response_raw = (mock_dir / item["response_file"]).read_bytes()
        outer = json.loads(request_raw)
        state = json.loads(outer["state"]["request"])
        reply = json.loads(response_raw)
        if (sha(request_raw) != item.get("request_sha256")
                or sha(response_raw) != item.get("response_sha256")
                or outer.get("model") != "multilingual"
                or typed_request_sha(outer) != item.get("typed_request_sha256")
                or any("holdout" in key.lower() or "evaluation" in key.lower() for key in recursive_keys(state))):
            raise RuntimeError("mock POST failed request model, typed digest, raw hash, or holdout validation")
        if item.get("is_negative_routing_case") is True:
            mode_counts["deterministic_fallback"] += 1
            if (item.get("receipt_mode") != "deterministic_fallback"
                    or item.get("response_routing_model") == "multilingual"
                    or item.get("receipt_selected") != state["remaining_candidates"][0]["id"]
                    or item.get("response_choice") == item.get("receipt_selected")):
                raise RuntimeError("negative mock route did not preserve fallback provenance")
        else:
            mode_counts["laya"] += 1
            if (item.get("receipt_mode") != "laya"
                    or item.get("response_routing_model") != "multilingual"
                    or item.get("response_choice") != item.get("receipt_selected")
                    or item.get("response_choice") not in {candidate["id"] for candidate in state["remaining_candidates"]}):
                raise RuntimeError("synthetic mock choice reply does not match its raw Laya-mode receipt")
    if mode_counts != {"laya": 213, "deterministic_fallback": 1}:
        raise RuntimeError(f"synthetic mock receipt-mode counts changed: {mode_counts}")
    for item in preflight.get("template_inventory", []):
        request_path = mock_dir / item["request_file"]
        response_path = mock_dir / item["response_file"]
        request_raw, response_raw = request_path.read_bytes(), response_path.read_bytes()
        if sha(request_raw) != item["raw_request_sha256"] or sha(response_raw) != item["response_sha256"]:
            raise RuntimeError(f"mock template raw exchange hash mismatch: {item['template_id']}")
        outer = json.loads(request_raw)
        state = json.loads(outer["state"]["request"])
        if (outer.get("model") != "multilingual" or typed_request_sha(outer) != item["typed_request_sha256"]
                or any("holdout" in key.lower() or "evaluation" in key.lower() for key in recursive_keys(state))):
            raise RuntimeError(f"mock template request pin/hash/holdout validation failed: {item['template_id']}")
    token_path = mock_dir / "tokenizer-output.json"
    token_raw = token_path.read_bytes()
    if sha(token_raw) != preflight["tokenizer"].get("output_sha256"):
        raise RuntimeError("cached tokenizer output changed after preparation")
    token_output = json.loads(token_raw)
    token_rows = token_output.get("rows", [])
    if (len(token_rows) != 128 or any(row.get("token_count_exact_sequence", 1025) > 1024
                                      or row.get("state_truncated") is not False for row in token_rows)):
        raise RuntimeError("one or more of 128 reachable multilingual request templates exceeds 1024 tokens")
    tokenizer_inventory = preflight["tokenizer"].get("tokenizer_cache_files", [])
    if sha(canonical_go_json(tokenizer_inventory)) != preflight["tokenizer"].get("tokenizer_cache_inventory_sha256"):
        raise RuntimeError("cached multilingual tokenizer inventory digest does not match the frozen preflight")
    phases = read_json(DESIGN_DIR / design["phase_plan_path"])
    phase_raw = (DESIGN_DIR / design["phase_plan_path"]).read_bytes()
    if sha(phase_raw) != design.get("phase_plan_sha256"):
        raise RuntimeError("frozen 64-cell phase plan checksum mismatch")
    rows = phases.get("plans", [])
    cells = {(row.get("intent_id"), row.get("arm")) for row in rows}
    if len(rows) != 64 or len(cells) != 64 or phases.get("new_intention_count") != 0:
        raise RuntimeError("frozen phase order is not exactly 32 same intents × 2 arms")
    provenance_path = DESIGN_DIR / design["source"]["tree_inventory_path"]
    provenance_raw = provenance_path.read_bytes()
    if sha(provenance_raw) != design["source"].get("tree_inventory_sha256"):
        raise RuntimeError("source tree inventory checksum differs from the frozen study design")
    provenance = json.loads(provenance_raw)
    source_root = DESIGN_DIR / design["source"]["path"]
    if (sha((source_root / "design-freeze.json").read_bytes()) != design["source"]["design_freeze_sha256"]
            or sha((source_root / "revision-manifest.json").read_bytes()) != design["source"]["revision_manifest_sha256"]):
        raise RuntimeError("copied revision-2 source freeze no longer matches the design")
    for item in provenance.get("copied_tree_files", []):
        path = source_root / item["path"]
        if not path.is_file() or path.is_symlink() or sha(path.read_bytes()) != item["sha256"]:
            raise RuntimeError(f"copied revision-2 source tree changed: {item['path']}")
    catalog = read_json(source_root / "catalog.json")
    source_ids = {item.get("id") for item in catalog.get("designs", [])}
    if (catalog.get("design_count") != 32 or catalog.get("same_intention_ids_as_original") is not True
            or len(source_ids) != 32 or source_ids != {row["intent_id"] for row in rows}):
        raise RuntimeError("frozen run plan does not use exactly the source cohort's original 32 IDs")
    cached = Path(design["provider"]["model_cache_snapshot"]).expanduser().resolve()
    for item in preflight["tokenizer"].get("tokenizer_cache_files", []):
        path = cached / item["path"]
        if not path.is_file() or sha(path.read_bytes()) != item["sha256"]:
            raise RuntimeError(f"cached multilingual tokenizer file changed after preflight: {item['path']}")
    return design, rows, design_raw


def verify_binary(binary: Path, go_bin: Path, expected_sha: str) -> dict:
    binary = binary.resolve()
    binary_raw = binary.read_bytes()
    if sha(binary_raw) != expected_sha or sha(binary_raw) != COMPILER_BINARY_SHA256:
        raise RuntimeError("native compiler binary SHA differs from the frozen compiler pin")
    version = subprocess.run([str(go_bin), "version"], capture_output=True, text=True, check=False, timeout=10)
    if version.returncode or GO_VERSION not in version.stdout:
        raise RuntimeError(f"physical Go toolchain mismatch: {version.stdout.strip()!r}")
    metadata = subprocess.run([str(go_bin), "version", "-m", str(binary)], capture_output=True,
                              text=True, check=False, timeout=20)
    if metadata.returncode:
        raise RuntimeError(f"cannot inspect compiler binary metadata: {metadata.stderr.strip()}")
    flat = "\n".join(line.strip().removeprefix("build\t") for line in metadata.stdout.splitlines())
    required = (f"vcs.revision={COMPILER_REVISION}", "vcs.modified=false", "GOOS=darwin",
                "GOARCH=arm64", "CGO_ENABLED=1", "-trimpath=true")
    if any(item not in flat for item in required):
        raise RuntimeError("compiler metadata does not match the clean Go 1.27 darwin/arm64 pin")
    return {"path": str(binary), "sha256": sha(binary_raw), "source_revision": COMPILER_REVISION,
            "go_version": GO_VERSION, "build_metadata_sha256": sha(metadata.stdout.encode())}


def import_capture_proxy(design: dict):
    pin = design["capture_proxy_dependency"]
    source = Path(pin["path_at_freeze"]).resolve()
    raw = source.read_bytes()
    if sha(raw) != pin["sha256"]:
        raise RuntimeError("pinned capture proxy dependency source changed since preparation")
    scripts_dir = str(source.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("pinned_context_capture_proxy_dependency", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the pinned capture proxy source")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "CaptureProxy"):
        raise RuntimeError("pinned dependency does not export CaptureProxy")
    return module.CaptureProxy, source


def fetch_health(port: int, timeout: float = 3.0) -> tuple[bytes, dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
        raw = response.read()
    return raw, json.loads(raw)


def wait_health(process: subprocess.Popen, port: int, timeout: float = 180.0) -> tuple[bytes, dict]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Laya server exited before health: {process.returncode}")
        try:
            raw, value = fetch_health(port)
            if (value.get("status") == "ok" and value.get("device") == "cpu"
                    and value.get("loaded") == ["multilingual"]
                    and value.get("revisions", {}).get("multilingual") == MODEL_REVISION):
                return raw, value
            last = value
        except Exception as exc:
            last = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"pinned multilingual Laya service health did not pass: {last!r}")


def terminate_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        else:
            process.terminate()
            process.wait(timeout=5)
    except ProcessLookupError:
        pass


def raw_event_files(run_dir: Path, event: dict) -> tuple[bytes, bytes]:
    return ((run_dir / "proxy" / event["request_file"]).read_bytes(),
            (run_dir / "proxy" / event["response_file"]).read_bytes())


def validate_invocation(row: dict, record: dict, payload: dict | None, events: list[dict], run_dir: Path) -> list[str]:
    issues = []
    posts = [event for event in events if event.get("method") == "POST" and event.get("path") == "/v1/systemone"]
    health = [event for event in events if event.get("method") == "GET" and event.get("path") == "/health"]
    body = ((payload or {}).get("report") or {}).get("body_search") or {}
    attempts = body.get("attempts", [])
    provider_attempts = [attempt for attempt in attempts
                         if (attempt.get("decision") or {}).get("mode") == "laya"]
    if not health or any(event.get("status") != 200 for event in health):
        issues.append("missing or failed resolver health check")
    if len(posts) != len(provider_attempts):
        issues.append(f"raw POST count {len(posts)} differs from provider-routed attempt count {len(provider_attempts)}")
    cell_post_cap = 1 if row["arm"] == "compact_multilingual_single" else 2
    if len(posts) > cell_post_cap:
        issues.append(f"raw POST count {len(posts)} exceeds this finite-choice cell's {cell_post_cap}-POST cap")
    if not payload or payload.get("report", {}).get("decision") != "PASS":
        issues.append("native CLI did not return a PASS report")
    if len(attempts) > row["max_attempts"]:
        issues.append("native body-search exceeded the frozen attempt cap")
    for index, event in enumerate(posts):
        try:
            request_raw, response_raw = raw_event_files(run_dir, event)
            outer, reply = json.loads(request_raw), json.loads(response_raw)
            state = json.loads(outer["state"]["request"])
            if any("holdout" in key.lower() or "evaluation" in key.lower() for key in recursive_keys(state)):
                issues.append(f"POST {event['seq']} included a holdout/evaluation field")
            if outer.get("model") != "multilingual":
                issues.append(f"POST {event['seq']} was not pinned to multilingual")
            typed_sha = typed_request_sha(outer)
            attempt = provider_attempts[index] if index < len(provider_attempts) else {}
            decision = attempt.get("decision") or {}
            selected = reply.get("answers", {}).get("body_ir_search", {}).get("choice")
            if decision.get("mode") != "laya":
                issues.append(f"attempt {index + 1} was not a provider-routed decision")
            if decision.get("requested_provider_model") != "multilingual":
                issues.append(f"attempt {index + 1} receipt did not bind the requested multilingual model")
            if decision.get("model_revision") != MODEL_REVISION:
                issues.append(f"attempt {index + 1} receipt did not bind the pinned model revision")
            if decision.get("request_sha256") != typed_sha:
                issues.append(f"attempt {index + 1} receipt hash does not bind its exact raw POST")
            if reply.get("routing", {}).get("model") != "multilingual":
                issues.append(f"POST {event['seq']} reply routing model mismatched")
            if selected != attempt.get("candidate_id"):
                issues.append(f"POST {event['seq']} reply choice differs from native attempt receipt")
            options = [option.get("id") for option in state.get("remaining_candidates", [])]
            if selected not in options:
                issues.append(f"POST {event['seq']} reply chose an option absent from its request")
        except Exception as exc:
            issues.append(f"POST {event.get('seq')} raw exchange validation failed: {exc}")
    if len(posts) != body.get("provider_operations", len(posts)):
        issues.append("native provider operation count differs from captured provider POSTs")
    return issues


def run_one(binary: Path, go_bin: Path, design: dict, row: dict, run_dir: Path, proxy, env: dict,
            timeout_seconds: int) -> tuple[dict, dict | None, list[dict], bool]:
    invocation = f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}"
    inv_dir = run_dir / "invocations" / invocation
    inv_dir.mkdir(parents=True, exist_ok=False)
    source_root = DESIGN_DIR / "source/revision-2"
    plan_raw = (DESIGN_DIR / row["plan_path"]).read_bytes()
    fixture_raw = (source_root / row["fixture_path"]).read_bytes()
    if sha(plan_raw) != row["plan_sha256"] or sha(fixture_raw) != row["fixture_sha256"]:
        raise RuntimeError(f"{invocation}: frozen plan or fixture checksum mismatch")
    plan = json.loads(plan_raw)
    if "holdout_test_cases" in plan or "evaluation" in plan:
        raise RuntimeError(f"{invocation}: holdout was included in the native search plan")
    plan_path, fixture_path = inv_dir / "plan.search.json", inv_dir / "fixture.gooo"
    write_bytes(plan_path, plan_raw)
    write_bytes(fixture_path, fixture_raw)
    command = [str(binary), "body-codegen", "--json", "--fill-search", str(plan_path),
               "--activity", row["activity"], str(fixture_path)]
    cli_env = env.copy()
    cli_env["GOOO_LAYA_URL"] = proxy.url
    cli_env.pop("GOOO_LAYA_API_KEY", None)
    proxy.set_invocation(invocation)
    started_utc, started_ns, started = dt.datetime.now(dt.timezone.utc).isoformat(), time.time_ns(), time.monotonic()
    process = None
    stdout = stderr = b""
    exit_code, completion = 124, "not_started"
    timed_out = False
    try:
        process = subprocess.Popen(command, cwd=inv_dir, env=cli_env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            exit_code, completion = process.returncode, "completed"
        except subprocess.TimeoutExpired:
            timed_out, completion = True, "runner_watchdog_timeout"
            terminate_group(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b"runner failed to drain process pipes after kill\n"
            exit_code = 124
            stderr += f"\nrunner watchdog: {timeout_seconds} seconds\n".encode()
    finally:
        proxy.set_invocation(None)
    finished_ns = time.time_ns()
    write_bytes(inv_dir / "stdout.raw", stdout)
    write_bytes(inv_dir / "stderr.raw", stderr)
    payload = None
    parse_error = ""
    if stdout:
        try:
            payload = json.loads(stdout)
        except Exception as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    drain = proxy.wait_for_pending(EXCHANGE_DRAIN_SECONDS)
    events = [event for event in proxy.events if event.get("invocation_id") == invocation]
    record = {"schema": "gooo/ir-composition-tdd-invocation/v1", "sequence": row["sequence"],
              "invocation_id": invocation, "intent_id": row["intent_id"], "arm": row["arm"],
              "max_attempts": row["max_attempts"], "provider_model": "multilingual",
              "activity": row["activity"], "started_utc": started_utc, "started_unix_ns": started_ns,
              "completed_unix_ns": finished_ns, "wall_ms": (time.monotonic() - started) * 1000,
              "timeout_seconds": timeout_seconds, "timed_out": timed_out, "completion": completion,
              "exit_code": exit_code, "argv": command, "plan_sha256": sha(plan_raw),
              "fixture_sha256": sha(fixture_raw), "stdout_sha256": sha(stdout), "stderr_sha256": sha(stderr),
              "stdout_file": str((inv_dir / "stdout.raw").relative_to(run_dir)),
              "stderr_file": str((inv_dir / "stderr.raw").relative_to(run_dir)),
              "parse_error": parse_error, "proxy_event_sequences": [event.get("seq") for event in events],
              "pending_exchange_drain": {**drain, "timeout_seconds": EXCHANGE_DRAIN_SECONDS},
              "holdout_read_before_capture_complete": False}
    return record, payload, events, timed_out


def run_independent_go(binary_go: Path, run_dir: Path, captured: list[dict], timeout: int) -> list[dict]:
    """Only called after every scheduled provider capture is settled or explicitly stopped."""
    results = []
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("GOOO_LAYA_") or key.startswith("LAYA_"):
            env.pop(key, None)
    env.update({"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off"})
    env["PATH"] = str(binary_go.parent) + os.pathsep + env.get("PATH", "")
    for item in captured:
        row, payload = item["row"], item["payload"]
        if payload is None or item["record"]["exit_code"] != 0:
            results.append({"invocation_id": item["record"]["invocation_id"], "status": "not_scored_cli_failed",
                            "training": {"passed": None, "total": None}, "holdout": {"passed": None, "total": None}})
            continue
        # This is the first read of holdout values by the capture runner.
        holdout_path = DESIGN_DIR / row["holdout_path"]
        holdout_raw = holdout_path.read_bytes()
        if sha(holdout_raw) != row["holdout_sha256"]:
            raise RuntimeError(f"{item['record']['invocation_id']}: frozen postselection holdout checksum mismatch")
        holdout = json.loads(holdout_raw)
        plan = json.loads((DESIGN_DIR / row["plan_path"]).read_bytes())
        source = payload.get("source")
        if not isinstance(source, str) or not source:
            results.append({"invocation_id": item["record"]["invocation_id"], "status": "missing_emitted_source",
                            "training": {"passed": None, "total": len(plan["test_cases"])} ,
                            "holdout": {"passed": None, "total": len(holdout)}})
            continue
        suite = [{"split": "training", **case} for case in plan["test_cases"]]
        suite += [{"split": "holdout", **case} for case in holdout]
        literals = "\n".join(
            f'{{split: "{case["split"]}", input: int64({case["input"]}), expected: int64({case["expected"]})}},'
            for case in suite)
        test_source = f'''package bodycodegen
import ("encoding/json"; "os"; "testing")
func TestIndependentFiniteReplay(t *testing.T) {{
 cases := []struct {{ split string; input, expected int64 }}{{
{literals}
 }}
 results := make([]struct {{ Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }}, 0, len(cases))
 for _, item := range cases {{ results = append(results, struct {{ Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }}{{item.split,item.input,item.expected,{row["activity"]}(item.input)}}) }}
 raw, err := json.Marshal(results); if err != nil {{ t.Fatal(err) }}
 if err := os.WriteFile("independent-results.json", raw, 0o644); err != nil {{ t.Fatal(err) }}
}}
'''
        test_dir = run_dir / "independent-go" / item["record"]["invocation_id"]
        test_dir.mkdir(parents=True, exist_ok=False)
        write_bytes(test_dir / "generated.go", source.encode("utf-8"))
        write_bytes(test_dir / "generated_test.go", test_source.encode("utf-8"))
        write_bytes(test_dir / "go.mod", b"module example.invalid/ir-composition-study\n\ngo 1.27\n")
        result = subprocess.run([str(binary_go), "test", "-count=1", "./..."], cwd=test_dir, env=env,
                                capture_output=True, check=False, timeout=timeout)
        write_bytes(test_dir / "go-stdout.raw", result.stdout)
        write_bytes(test_dir / "go-stderr.raw", result.stderr)
        values = []
        if (test_dir / "independent-results.json").is_file():
            values = json.loads((test_dir / "independent-results.json").read_bytes())
        split_scores = {}
        for split in ("training", "holdout"):
            subset = [value for value in values if value["split"] == split]
            split_scores[split] = {"passed": sum(value["actual"] == value["expected"] for value in subset),
                                   "total": len(subset), "cases": subset}
        results.append({"invocation_id": item["record"]["invocation_id"],
                        "status": "compiled_and_scored" if result.returncode == 0 and values else "go_compile_or_test_failed",
                        "go_exit_code": result.returncode, "generated_source_sha256": sha(source.encode()),
                        "go_stdout_sha256": sha(result.stdout), "go_stderr_sha256": sha(result.stderr),
                        "training": split_scores["training"], "holdout": split_scores["holdout"]})
    return results


def aggregate(records: list[dict], replay: list[dict], design_rows: list[dict]) -> dict:
    replay_by_id = {row["invocation_id"]: row for row in replay}
    captured_by_id = {row["invocation_id"]: row for row in records if row.get("invocation_id")}
    arms = {}
    for arm in ("compact_multilingual_single", "compact_multilingual_local_feedback"):
        planned = [row for row in design_rows if row["arm"] == arm]
        cli_successes = 0
        validated_cells = 0
        improved = unchanged = declined = proposal_correct = adjustment_observed = 0
        first_proposal_scores, emitted_native_scores, emitted_independent_scores, holdout_scores = [], [], [], []
        first_proposal_choices: dict[str, int] = {}
        all_provider_choices: dict[str, int] = {}
        provider_cells_without_cli_success = 0
        raw_provider_posts = provider_operations = provider_receipts = native_attempts = sole_candidate_selections = 0
        sole_candidate_choices: dict[str, int] = {}
        independent_training_observed = independent_training_passed = 0
        independent_holdout_observed = independent_holdout_passed = 0
        independent_training_planned = sum(row.get("training_case_count", 0) for row in planned)
        independent_holdout_planned = sum(row.get("holdout_case_count", 0) for row in planned)
        for row in planned:
            invocation_id = f"{row['sequence']:02d}-{row['intent_id']}-{arm}"
            invocation = captured_by_id.get(invocation_id)
            if invocation is None:
                continue
            cli_successes += int(invocation.get("exit_code") == 0)
            raw_provider_posts += invocation.get("raw_provider_post_count", 0)
            source_attempts = invocation.get("body_search_attempts", [])
            native_attempts += len(source_attempts)
            sole_attempts = [attempt for attempt in source_attempts
                             if attempt.get("selection_method") == "sole_remaining_candidate"]
            sole_candidate_selections += len(sole_attempts)
            for attempt in sole_attempts:
                choice = attempt.get("candidate_id")
                if choice:
                    sole_candidate_choices[choice] = sole_candidate_choices.get(choice, 0) + 1
            provider_operations += invocation.get("provider_operations", 0)
            provider_attempts = [attempt for attempt in source_attempts
                                 if (attempt.get("decision") or {}).get("mode") == "laya"]
            provider_receipts += len(provider_attempts)
            provider_cells_without_cli_success += int(bool(provider_attempts) and invocation.get("exit_code") != 0)
            for attempt in provider_attempts:
                choice = attempt.get("candidate_id")
                if choice:
                    all_provider_choices[choice] = all_provider_choices.get(choice, 0) + 1

            capture_valid = invocation.get("capture_validation_passed") is True
            if capture_valid:
                validated_cells += 1
                if provider_attempts:
                    first = provider_attempts[0]
                    first_id = first.get("candidate_id")
                    first_passed = first.get("test_cases_passed")
                    emitted_passed = invocation.get("native_training_passed")
                    intended = row.get("intended_candidate_id")
                    if first_id:
                        first_proposal_choices[first_id] = first_proposal_choices.get(first_id, 0) + 1
                    proposal_correct += int(bool(intended and first_id == intended))
                    if isinstance(first_passed, int) and isinstance(emitted_passed, int):
                        adjustment_observed += 1
                        first_proposal_scores.append(first_passed)
                        emitted_native_scores.append(emitted_passed)
                        if emitted_passed > first_passed:
                            improved += 1
                        elif emitted_passed < first_passed:
                            declined += 1
                        else:
                            unchanged += 1

            score = replay_by_id.get(invocation_id, {})
            train = score.get("training", {})
            holdout = score.get("holdout", {})
            if isinstance(train.get("total"), int) and isinstance(train.get("passed"), int):
                independent_training_observed += train["total"]
                independent_training_passed += train["passed"]
                emitted_independent_scores.append(train["passed"])
            if isinstance(holdout.get("total"), int) and isinstance(holdout.get("passed"), int):
                independent_holdout_observed += holdout["total"]
                independent_holdout_passed += holdout["passed"]
                holdout_scores.append(holdout["passed"])
        arms[arm] = {
            "planned_cells": len(planned),
            "captured_cli_successes": cli_successes,
            "capture_validated_cells": validated_cells,
            "unresolved_or_failed_cells": len(planned) - cli_successes,
            "capture_not_validated_cells": len(planned) - validated_cells,
            "provider_routed_cells_without_cli_success": provider_cells_without_cli_success,
            "raw_provider_posts": raw_provider_posts,
            "native_provider_operations": provider_operations,
            "provider_routed_receipt_count": provider_receipts,
            "native_attempt_count_including_sole_remaining": native_attempts,
            "sole_remaining_candidate_deterministic_selections": sole_candidate_selections,
            "sole_remaining_candidate_choice_distribution": sole_candidate_choices,
            "provider_choice_distribution_all_attempts_valid_or_invalid_cell": all_provider_choices,
            "first_provider_proposal_choice_distribution_validated_cells_only": first_proposal_choices,
            "first_proposal_matches_intended_candidate": {"passed": proposal_correct,
                                                           "observed": sum(first_proposal_choices.values()),
                                                           "planned": len(planned)},
            "first_proposal_training_score": {"passed_sum": sum(first_proposal_scores),
                                              "observed_cells": len(first_proposal_scores),
                                              "planned_cells": len(planned)},
            "emitted_training_score_native": {"passed_sum": sum(emitted_native_scores),
                                              "observed_cells": len(emitted_native_scores),
                                              "planned_cells": len(planned)},
            "emitted_training_score_independent_go": {"passed_sum": sum(emitted_independent_scores),
                                                      "observed_cases": independent_training_observed,
                                                      "planned_cases": independent_training_planned},
            "local_score_adjustment": {"improved_cells": improved, "unchanged_cells": unchanged,
                                        "declined_cells": declined, "observed_cells": adjustment_observed,
                                        "planned_cells": len(planned),
                                        "observed_delta_sum": sum(b - a for a, b in zip(first_proposal_scores, emitted_native_scores))},
            "holdout_score_independent_go": {"passed_sum": sum(holdout_scores),
                                             "observed_cases": independent_holdout_observed,
                                             "planned_cases": independent_holdout_planned},
        }
    return {"schema": "gooo/ir-composition-tdd-summary/v1", "arms": arms,
            "metric_note": "Raw POSTs, Laya decision receipts, native attempts, and sole-candidate deterministic selections are counted separately. Provider proposal and local-adjustment metrics include only fully validated captures. CLI and independent Go denominators are separate; unresolved and failed cells remain in each arm's planned denominator."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--go-bin", type=Path, required=True)
    parser.add_argument("--laya-venv", type=Path, required=True)
    parser.add_argument("--run-id", default=dt.datetime.now(dt.timezone.utc).strftime("ir-composition-tdd-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--timeout-seconds", type=int, default=WALL_TIMEOUT_SECONDS)
    parser.add_argument("--independent-go-timeout-seconds", type=int, default=60)
    parser.add_argument("--start-model-capture", action="store_true",
                        help="required acknowledgement to start the cached local Laya service")
    args = parser.parse_args()
    if not args.start_model_capture:
        parser.error("live capture is gated; pass --start-model-capture only after the root task opens the capture gate")
    design, rows, design_raw = verify_design()
    binary_info = verify_binary(args.binary, args.go_bin, design["compiler"]["sha256"])
    proxy_class, proxy_source = import_capture_proxy(design)
    if args.timeout_seconds < 1 or args.independent_go_timeout_seconds < 1:
        raise RuntimeError("timeouts must be positive")
    laya_venv = args.laya_venv.resolve()
    server_exe = laya_venv / "bin/laya-serve"
    python_exe = laya_venv / "bin/python"
    if not server_exe.is_file() or not os.access(server_exe, os.X_OK) or not python_exe.is_file():
        raise RuntimeError(f"pinned offline Laya environment unavailable: {laya_venv}")
    package_version = subprocess.run([str(python_exe), "-c", "import importlib.metadata as m; print(m.version('laya'))"],
                                     capture_output=True, text=True, check=False, timeout=20)
    if package_version.returncode or package_version.stdout.strip() != LAYA_VERSION:
        raise RuntimeError(f"Laya runtime version mismatch: {package_version.stdout.strip()!r}")
    model_snapshot = Path(design["provider"]["model_cache_snapshot"]).expanduser().resolve()
    if model_snapshot.name != MODEL_REVISION or not (model_snapshot / "multilingual/tokenizer").is_dir():
        raise RuntimeError("pinned multilingual tokenizer/model cache is unavailable or changed")
    run_dir = ROOT / "results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    for subdir in ("laya", "proxy", "invocations", "independent-go"):
        (run_dir / subdir).mkdir()
    write_bytes(run_dir / "study-design.json", design_raw)
    write_bytes(run_dir / "study-design.sha256", (DESIGN_DIR / "study-design.sha256").read_bytes())
    write_bytes(run_dir / "phase-plans.json", (DESIGN_DIR / "phase-plans.json").read_bytes())
    code_sources = [
        ("scripts/run_study.py", Path(__file__).resolve()),
        ("scripts/prepare_study.py", ROOT / "scripts/prepare_study.py"),
        ("dependencies/run_pinned_context_study.py", proxy_source),
    ]
    code_archive = []
    for relative, source_path in code_sources:
        source_bytes = source_path.read_bytes()
        if relative == "scripts/run_study.py" and sha(source_bytes) != design["study_code_provenance"]["capture_script_sha256"]:
            raise RuntimeError("capture runner changed after frozen design verification")
        if relative == "scripts/prepare_study.py" and sha(source_bytes) != design["study_code_provenance"]["preparation_script_sha256"]:
            raise RuntimeError("preparation script changed after frozen design verification")
        if relative.startswith("dependencies/") and sha(source_bytes) != design["capture_proxy_dependency"]["sha256"]:
            raise RuntimeError("capture proxy dependency changed after frozen design verification")
        archive_path = run_dir / "study-code" / relative
        write_bytes(archive_path, source_bytes)
        code_archive.append({"path": str(archive_path.relative_to(run_dir)),
                             "source_path": str(source_path), "sha256": sha(source_bytes),
                             "bytes": len(source_bytes)})
    preexecution = {"schema": "gooo/ir-composition-tdd-preexecution/v1",
                    "status": "frozen_inputs_verified_before_laya_start", "design_sha256": sha(design_raw),
                    "compiler": binary_info, "capture_proxy_source_sha256": sha(proxy_source.read_bytes()),
                    "study_code_archive": code_archive,
                    "planned_cells": 64, "planned_new_intentions": 0,
                    "attempt_caps": {row["id"]: row["max_attempts"] for row in design["arms"]},
                    "measured_provider_post_cap": 96, "warmup_calls": 0,
                    "holdout_values_loaded": False, "live_capture_started": False}
    pre_bytes = write_json(run_dir / "preexecution.json", preexecution)
    metadata = {"schema": "gooo/ir-composition-tdd-run-metadata/v1", "run_id": args.run_id,
                "status": "starting", "preexecution_sha256": sha(pre_bytes), "design_sha256": sha(design_raw),
                "provider_model": "multilingual", "model_revision": MODEL_REVISION,
                "loopback_only": True, "credentials_unset": True, "downloads_disabled": True,
                "planned_cells": 64, "planned_intentions": 32, "new_intentions": 0,
                "laya_version": LAYA_VERSION, "model_snapshot_path": str(model_snapshot),
                "capture_proxy_source_sha256": sha(proxy_source.read_bytes())}
    write_json(run_dir / "run-metadata.json", metadata)

    port = free_port()
    service_env = os.environ.copy()
    for key in tuple(service_env):
        if key.startswith("GOOO_LAYA_") or key.startswith("LAYA_"):
            service_env.pop(key, None)
    service_env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1", "LAYA_HOST": "127.0.0.1", "LAYA_PORT": str(port),
                        "LAYA_MODELS": "multilingual", "LAYA_DEVICE": "cpu", "LAYA_THREADS": "4",
                        "LAYA_PRELOAD": "1", "LAYA_MAX_LOADED": "1", "LAYA_AUTO_TASK": "0",
                        "LAYA_REVISION": MODEL_REVISION})
    laya_stdout = (run_dir / "laya/stdout.log").open("wb")
    laya_stderr = (run_dir / "laya/stderr.log").open("wb")
    server = None
    proxy = None
    records, captured = [], []
    stop_reason = None
    post_capture_drain = {"settled": True, "pending_sequences": []}
    owned_service_shutdown = {"confirmed": False, "return_code": None}
    proxy_events_snapshot: list[dict] = []
    try:
        server = subprocess.Popen([str(server_exe)], cwd=run_dir, env=service_env,
                                  stdin=subprocess.DEVNULL, stdout=laya_stdout, stderr=laya_stderr,
                                  start_new_session=True)
        write_json(run_dir / "laya/owned-process.json", {"pid": server.pid, "process_group_id": server.pid,
                    "executable": str(server_exe), "owned_by_runner": True, "host": "127.0.0.1",
                    "port": port, "loaded_models": ["multilingual"], "device": "cpu"})
        health_raw, health = wait_health(server, port)
        write_bytes(run_dir / "laya/health-before.raw", health_raw)
        proxy = proxy_class(port, run_dir)
        metadata.update({"status": "running", "laya_port": port, "capture_proxy_url": proxy.url,
                         "health_before_sha256": sha(health_raw)})
        write_json(run_dir / "run-metadata.json", metadata)
        cli_env = os.environ.copy()
        for key in tuple(cli_env):
            if key.startswith("GOOO_LAYA_") or key.startswith("LAYA_"):
                cli_env.pop(key, None)
        cli_env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1"})
        for index, row in enumerate(rows):
            try:
                record, payload, events, timed_out = run_one(binary_info["path"], args.go_bin.resolve(), design,
                                                              row, run_dir, proxy, cli_env, args.timeout_seconds)
                issues = validate_invocation(row, record, payload, events, run_dir)
                if not record["pending_exchange_drain"].get("settled"):
                    issues.append("provider POST remained unresolved at bounded pre-next-cell drain")
                record["validation_issues"] = issues
                record["pre_next_cell_check"] = {"passed": not issues and not timed_out,
                                                  "issues": list(issues),
                                                  "timed_out": timed_out}
                record["capture_validation_passed"] = not issues and not timed_out
                invocation_dir = run_dir / "invocations" / record["invocation_id"]
                body = ((payload or {}).get("report") or {}).get("body_search") or {}
                record["body_search_attempts"] = body.get("attempts", [])
                record["raw_provider_post_count"] = sum(
                    event.get("method") == "POST" and event.get("path") == "/v1/systemone" for event in events)
                record["provider_operations"] = body.get("provider_operations", 0)
                record["selected_candidate_id"] = body.get("selected_candidate_id")
                record["native_training_passed"] = body.get("training_passed")
                record["native_training_total"] = body.get("training_total")
                record["body_search_stop_reason"] = body.get("stop_reason")
                write_json(invocation_dir / "invocation.json", record)
                records.append(record)
                captured.append({"row": row, "record": record, "payload": payload})
                posts_so_far = sum(event.get("kind") == "laya_choice" for event in proxy.events)
                if posts_so_far > design["provider"]["measured_provider_post_cap"]:
                    issues.append("captured raw provider POST count exceeded the frozen 96-POST study cap")
                    record["validation_issues"] = issues
                    record["capture_validation_passed"] = False
                    record["pre_next_cell_check"] = {"passed": False, "issues": list(issues),
                                                      "timed_out": timed_out}
                    write_json(invocation_dir / "invocation.json", record)
                if issues or timed_out:
                    stop_reason = {"invocation_id": record["invocation_id"], "issues": issues,
                                   "timed_out": timed_out, "pending": record["pending_exchange_drain"]}
                    break
            except Exception as exc:
                stop_reason = {"sequence": row["sequence"], "intent_id": row["intent_id"], "arm": row["arm"],
                               "error": f"{type(exc).__name__}: {exc}"}
                records.append({"schema": "gooo/ir-composition-tdd-invocation/v1", "sequence": row["sequence"],
                                "intent_id": row["intent_id"], "arm": row["arm"], "status": "failed_before_complete",
                                "error": stop_reason["error"]})
                break
        completed_sequences = {record.get("sequence") for record in records}
        for row in rows:
            if row["sequence"] not in completed_sequences:
                records.append({"schema": "gooo/ir-composition-tdd-invocation/v1", "sequence": row["sequence"],
                                "intent_id": row["intent_id"], "arm": row["arm"],
                                "status": "not_started_after_prior_failure", "not_started": True})
                write_json(run_dir / "invocations" / f"not-started-{row['sequence']:02d}.json",
                           records[-1])
        # Stop the owned inference service before any independent compiler work.
        # Give in-flight proxy forwards a final bounded chance to settle first.
        if proxy is not None:
            pre_shutdown_drain = proxy.wait_for_pending(0)
        else:
            pre_shutdown_drain = {"settled": True, "pending_sequences": []}
        if server is not None:
            terminate_group(server)
            owned_service_shutdown = {"confirmed": server.poll() is not None,
                                      "return_code": server.returncode, "pid": server.pid}
            write_json(run_dir / "laya/owned-process.json", {
                "pid": server.pid, "process_group_id": server.pid, "executable": str(server_exe),
                "owned_by_runner": True, "host": "127.0.0.1", "port": port,
                "loaded_models": ["multilingual"], "device": "cpu", "stopped": owned_service_shutdown["confirmed"],
                "return_code": server.returncode})
            server = None
        if proxy is not None:
            post_capture_drain = proxy.wait_for_pending(5)
            proxy_events_snapshot = list(proxy.events)
            proxy.stop()
            proxy = None
        laya_stdout.flush()
        os.fsync(laya_stdout.fileno())
        laya_stderr.flush()
        os.fsync(laya_stderr.fileno())
        write_json(run_dir / "proxy/events.json", {"schema": "gooo/ir-composition-tdd-capture-events/v1",
                    "events": proxy_events_snapshot, "raw_event_count": len(proxy_events_snapshot)})
        if post_capture_drain.get("settled"):
            # Holdout bytes are first opened here, after the provider service is stopped and all forwards settle.
            replay = run_independent_go(args.go_bin.resolve(), run_dir, captured,
                                        args.independent_go_timeout_seconds)
            scoring_status = ("ran_after_service_shutdown_and_forward_settlement" if owned_service_shutdown["confirmed"]
                              else "skipped_owned_service_exit_unconfirmed")
            if not owned_service_shutdown["confirmed"]:
                replay = [{"invocation_id": item["record"]["invocation_id"],
                           "status": "not_scored_owned_service_exit_unconfirmed",
                           "training": {"passed": None, "total": item["row"].get("training_case_count")},
                           "holdout": {"passed": None, "total": item["row"].get("holdout_case_count")}}
                          for item in captured]
        else:
            replay = [{"invocation_id": item["record"]["invocation_id"],
                       "status": "not_scored_unresolved_provider_forward",
                       "training": {"passed": None, "total": item["row"].get("training_case_count")},
                       "holdout": {"passed": None, "total": item["row"].get("holdout_case_count")}}
                      for item in captured]
            scoring_status = "skipped_unresolved_provider_forward"
        summary = aggregate(records, replay, rows)
        write_json(run_dir / "independent-go/replay-results.json", replay)
        write_json(run_dir / "summary.json", summary)
        complete_capture = (stop_reason is None and len(captured) == 64
                            and post_capture_drain.get("settled")
                            and owned_service_shutdown.get("confirmed"))
        report = {"schema": "gooo/ir-composition-tdd-capture-report/v1",
                  "status": "CAPTURED" if complete_capture else "PARTIAL_CAPTURE",
                  "scheduled_cells": 64, "completed_cli_cells": len(captured),
                  "raw_provider_posts": sum(1 for event in proxy_events_snapshot if event.get("kind") == "laya_choice"),
                  "new_intentions": 0, "same_revision2_intents": 32,
                  "stop_reason": stop_reason, "summary_path": "summary.json",
                  "provider_forward_drain": {"pre_shutdown": pre_shutdown_drain,
                                              "post_shutdown": post_capture_drain},
                  "owned_service_shutdown": owned_service_shutdown,
                  "independent_go_scoring": scoring_status,
                  "original_plan_failures_remain_in_denominator": True}
        write_json(run_dir / "report.json", report)
        metadata.update({"status": report["status"], "completed_cells": len(captured),
                         "actual_provider_posts": sum(1 for event in proxy_events_snapshot if event.get("kind") == "laya_choice"),
                         "provider_forward_drain": report["provider_forward_drain"],
                         "independent_go_scoring": scoring_status,
                         "stop_reason": stop_reason})
        write_json(run_dir / "run-metadata.json", metadata)
    finally:
        if proxy is not None:
            proxy.stop()
        if server is not None:
            terminate_group(server)
            laya_stdout.flush()
            os.fsync(laya_stdout.fileno())
            laya_stderr.flush()
            os.fsync(laya_stderr.fileno())
        laya_stdout.close()
        laya_stderr.close()
    print(json.dumps({"run_dir": str(run_dir), "status": read_json(run_dir / "report.json")["status"],
                      "completed_cells": len(captured), "stop_reason": stop_reason,
                      "actual_provider_posts": sum(1 for event in proxy_events_snapshot
                                                    if event.get("kind") == "laya_choice")}, indent=2))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    main()
