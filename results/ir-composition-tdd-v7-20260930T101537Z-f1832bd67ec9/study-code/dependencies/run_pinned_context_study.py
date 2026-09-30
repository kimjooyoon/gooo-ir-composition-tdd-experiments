#!/usr/bin/env python3
"""Capture the frozen pinned-context Laya study, then independently score saved outputs."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from selection_support import holdout_case_pairs, scan_selection_body, sha256


ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = ROOT / "pinned-context-design"
DEFAULT_RUN_ID = "pinned-compact-context-2026-09-30"
DEFAULT_VENV = Path("/tmp/meta-ontology-go-laya-venv-20260930")
LAYA_VERSION = "0.3.21"
MODEL_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
FROZEN_PUBLIC_CHECKPOINT_REVISION = "9dc4beb54002880da2a919d2b9b65148718500f5"
SAMPLE_INTERVAL = 0.12
CALL_TIMEOUT_SECONDS = 90
RESOLVER_REQUEST_BUDGET_SECONDS = 8
PROXY_UPSTREAM_TIMEOUT_SECONDS = RESOLVER_REQUEST_BUDGET_SECONDS + 2
PENDING_SETTLE_TIMEOUT_SECONDS = PROXY_UPSTREAM_TIMEOUT_SECONDS + 3
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: Any) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    write_bytes(path, raw)
    return raw


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_cputime(raw: str) -> float:
    parts = raw.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def sample_pid(pid: int | None) -> dict | None:
    if not pid:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "cputime=,pcpu=,rss=,pid="],
                            capture_output=True, text=True, check=False)
    raw = result.stdout.strip()
    if result.returncode or not raw:
        return {"pid": pid, "alive": False, "ps_raw": raw}
    parts = raw.split()
    if len(parts) < 4:
        return {"pid": pid, "alive": True, "ps_raw": raw}
    try:
        return {"pid": int(parts[-1]), "alive": True, "cpu_seconds": parse_cputime(parts[0]),
                "pcpu_percent_sample": float(parts[1]), "rss_kb": int(parts[2]), "ps_raw": raw}
    except (ValueError, IndexError):
        return {"pid": pid, "alive": True, "ps_raw": raw}


def process_summary(samples: list[dict], key: str) -> dict:
    rows = [row[key] for row in samples if row.get(key) and row[key].get("alive")]
    cpu = [row["cpu_seconds"] for row in rows if "cpu_seconds" in row]
    pcpu = [row["pcpu_percent_sample"] for row in rows if "pcpu_percent_sample" in row]
    rss = [row["rss_kb"] for row in rows if "rss_kb" in row]
    return {
        "sample_count": len(rows),
        "cpu_seconds_delta_coarse": max(0.0, cpu[-1] - cpu[0]) if len(cpu) > 1 else None,
        "rss_peak_kb_sampled": max(rss) if rss else None,
        "pcpu_percent_max_sampled": max(pcpu) if pcpu else None,
        "measurement_note": "Process-level ps samples; CPU is cumulative and second-granularity; RSS and CPU-percent peaks are sampled.",
    }


def find_build_metadata(binary: Path) -> tuple[str, str | None]:
    result = subprocess.run(["go", "version", "-m", str(binary)], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"cannot inspect compiler build metadata: {result.stderr.strip()}")
    revision = modified = None
    for line in result.stdout.splitlines():
        if match := re.search(r"\bvcs\.revision=(\S+)", line):
            revision = match.group(1)
        if match := re.search(r"\bvcs\.modified=(\S+)", line):
            modified = match.group(1)
    return revision or "", modified


def verify_binary(binary: Path, expected_sha: str, expected_revision: str) -> dict:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f"compiler binary is missing or non-executable: {binary}")
    digest = sha256(binary.read_bytes())
    if digest != expected_sha:
        raise RuntimeError("compiler binary differs from the frozen design SHA-256")
    revision, modified = find_build_metadata(binary)
    if revision != expected_revision or modified != "false":
        raise RuntimeError(f"compiler binary is not the frozen clean revision: {revision!r}/{modified!r}")
    return {"path": str(binary), "sha256": digest, "source_revision": revision, "vcs_modified": modified}


def verify_local_model_cache_binding(path: Path) -> dict:
    binding = read_json(path)
    if binding.get("model_revision") != MODEL_REVISION:
        raise RuntimeError("local model cache binding names a different model revision")
    snapshot = Path(binding["snapshot_path"])
    if not snapshot.is_dir() or snapshot.name != MODEL_REVISION:
        raise RuntimeError("pinned local model cache snapshot path is unavailable or has a different revision")
    checked, changed = 0, []
    for item in binding.get("cached_snapshot_files", []):
        target = snapshot / item["path"]
        if not target.is_file():
            changed.append({"path": item["path"], "reason": "missing"})
            continue
        raw_hash = hashlib.sha256()
        size = 0
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                raw_hash.update(chunk)
        checked += 1
        if size != item["bytes"] or raw_hash.hexdigest() != item["sha256"]:
            changed.append({"path": item["path"], "reason": "content_changed"})
    for item in binding.get("laya_runtime_source_files", []):
        target = Path(item["path"])
        if not target.is_file() or sha256(target.read_bytes()) != item["sha256"]:
            changed.append({"path": item["path"], "reason": "runtime_source_changed_or_missing"})
    if changed:
        raise RuntimeError(f"cached model or tokenization runtime changed after preflight: {changed[:5]}")
    return {"schema": binding.get("schema"), "model_revision": MODEL_REVISION,
            "verified_cached_files": checked, "verified_cached_bytes": binding.get("cached_snapshot_total_bytes"),
            "claim_scope": binding.get("claim_scope")}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_health(port: int, timeout: float = 3.0) -> tuple[bytes, dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
        raw = response.read()
    return raw, json.loads(raw)


def wait_for_health(process: subprocess.Popen, port: int, run_dir: Path, expected_models: list[str]) -> dict:
    deadline = time.monotonic() + 300
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"owned offline Laya process exited: {process.returncode}")
        try:
            raw, health = fetch_health(port)
            write_bytes(run_dir / "laya" / "health-before.json", raw)
            revisions = health.get("revisions", {})
            if (health.get("status") != "ok" or health.get("device") != "cpu"
                    or set(health.get("loaded", [])) != set(expected_models)):
                raise RuntimeError(f"Laya did not load the two pinned CPU models: {health!r}")
            if any(revisions.get(model) != MODEL_REVISION for model in expected_models):
                raise RuntimeError(f"Laya cached model revision differs from the study pin: {revisions!r}")
            return health
        except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if isinstance(exc, RuntimeError) and "pinned" in str(exc):
                raise
            time.sleep(1)
    raise RuntimeError(f"timed out waiting for both local cached Laya models: {last_error}")


def stop_owned_server(process: subprocess.Popen | None, pgid: int | None, *handles) -> None:
    if process is not None and process.poll() is None:
        try:
            if os.getpgid(process.pid) == pgid == process.pid:
                os.killpg(pgid, signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    if process.poll() is None and os.getpgid(process.pid) == pgid:
                        os.killpg(pgid, signal.SIGKILL)
                        process.wait(timeout=4)
        except (ProcessLookupError, PermissionError):
            pass
    for handle in handles:
        if handle is not None and not handle.closed:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()


class CaptureProxy:
    """Loopback-only raw POST/response recorder for the owned offline service."""
    def __init__(self, target_port: int, run_dir: Path):
        self.target_port = target_port
        self.run_dir = run_dir
        self.lock = threading.Lock()
        self.current_invocation: str | None = None
        self.events: list[dict] = []
        self.next_seq = 0
        self.pending: dict[int, threading.Event] = {}
        proxy = self

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

            def _forward(self, method: str):
                started_ns, started = time.time_ns(), time.monotonic()
                started_utc = now_utc()
                length = int(self.headers.get("Content-Length", "0"))
                request_body = self.rfile.read(length) if length else b""
                with proxy.lock:
                    proxy.next_seq += 1
                    seq = proxy.next_seq
                    settled = threading.Event()
                    proxy.pending[seq] = settled
                    invocation_id = proxy.current_invocation
                request_rel = f"proxy/requests/{seq:04d}.request.raw"
                response_rel = f"proxy/responses/{seq:04d}.response.raw"
                write_bytes(proxy.run_dir / request_rel, request_body)
                upstream = None
                try:
                    upstream = http.client.HTTPConnection("127.0.0.1", proxy.target_port,
                                                          timeout=PROXY_UPSTREAM_TIMEOUT_SECONDS)
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in ("host", "connection", "content-length", "transfer-encoding",
                                                      "authorization", "x-api-key", "api-key")}
                    headers["Content-Length"] = str(len(request_body))
                    upstream.request(method, self.path, body=request_body, headers=headers)
                    upstream_response = upstream.getresponse()
                    response_body = upstream_response.read()
                    status = upstream_response.status
                    response_headers = {key: value for key, value in upstream_response.getheaders()
                                        if key.lower() in ("content-type", "content-encoding", "cache-control")}
                except Exception as exc:  # preserve a failed exchange as raw evidence too
                    response_body = json.dumps({"error": str(exc)}).encode()
                    response_headers, status = {"Content-Type": "application/json"}, 502
                finally:
                    if upstream is not None:
                        upstream.close()
                write_bytes(proxy.run_dir / response_rel, response_body)
                try:
                    response = json.loads(response_body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response = {}
                choice = response.get("answers", {}).get("body_ir_search", {}).get("choice") if isinstance(response, dict) else None
                event = {
                    "seq": seq,
                    "kind": ("health_check" if method == "GET" and self.path == "/health"
                             else "laya_choice" if method == "POST" and self.path == "/v1/systemone"
                             else "unexpected_proxy_request"),
                    "invocation_id": invocation_id,
                    "method": method, "path": self.path, "started_utc": started_utc,
                    "started_unix_ns": started_ns, "completed_utc": now_utc(),
                    "completed_unix_ns": time.time_ns(), "duration_ms": (time.monotonic() - started) * 1000,
                    "status": status, "request_file": request_rel, "response_file": response_rel,
                    "request_sha256": sha256(request_body), "response_sha256": sha256(response_body),
                    "selected_candidate_id": choice,
                }
                with proxy.lock:
                    proxy.events.append(event)
                    with (proxy.run_dir / "proxy" / "events.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    settled.set()
                self.send_response(status)
                for key, value in response_headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionResetError):
                    # The compiler may have hit its own timeout. The raw upstream
                    # reply is already durable in the event; a disconnected client
                    # must not interrupt capture completion or leak a traceback.
                    pass
                finally:
                    self.close_connection = True

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="pinned-context-capture", daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"

    def set_invocation(self, invocation_id: str | None) -> None:
        with self.lock:
            self.current_invocation = invocation_id

    def wait_for_pending(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                pending = list(self.pending.items())
            unsettled = [(seq, done) for seq, done in pending if not done.is_set()]
            if not unsettled:
                with self.lock:
                    for seq, done in list(self.pending.items()):
                        if done.is_set():
                            self.pending.pop(seq, None)
                return {"settled": True, "pending_sequences": []}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"settled": False, "pending_sequences": [seq for seq, _ in unsettled]}
            unsettled[0][1].wait(min(remaining, 0.1))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def parse_state(raw: bytes) -> tuple[dict, dict]:
    outer = json.loads(raw)
    wrapper = outer.get("state", {})
    encoded = wrapper.get("request", "") if isinstance(wrapper, dict) else ""
    state = json.loads(encoded) if isinstance(encoded, str) and encoded else {}
    return outer, state


def canonical_go_json(value: Any) -> bytes:
    """Match encoding/json's compact UTF-8 output for the typed request structs."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (encoded.replace(b"&", b"\\u0026").replace(b"<", b"\\u003c").replace(b">", b"\\u003e")
            .replace("\u2028".encode(), b"\\u2028").replace("\u2029".encode(), b"\\u2029"))


def reconstruct_typed_request(outer: dict, state: dict) -> dict:
    questions = outer.get("questions")
    if not isinstance(questions, dict) or len(questions) != 1:
        raise RuntimeError("captured Laya wire must contain exactly one typed chooser question")
    question_id, question_wire = next(iter(questions.items()))
    candidates = state.get("remaining_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("captured Laya state has no remaining typed candidates")
    options = [{"id": item["id"], "description": "Try this exact expression: " + item["expression"]}
               for item in candidates]
    state_wire = outer.get("state", {}).get("request")
    if not isinstance(state_wire, str):
        raise RuntimeError("captured Laya wire has no serialized state string")
    return {
        "schema": "gooo/typed-decision-request/v1",
        "state": state_wire,
        "question": {"id": question_id, "instructions": question_wire["instructions"], "options": options},
        "fallback": candidates[0]["id"],
        "provider_model": outer.get("model"),
    }


def typed_request_digest(outer: dict, state: dict) -> str:
    return "sha256:" + sha256(canonical_go_json(reconstruct_typed_request(outer, state)))


def attach_exchange_refs(run_dir: Path, records: list[dict], events: list[dict]) -> None:
    by_invocation: dict[str, list[dict]] = {}
    for event in events:
        by_invocation.setdefault(event.get("invocation_id") or "", []).append(event)
    for record in records:
        invocation_id = record["invocation_id"]
        refs = []
        for event in sorted(by_invocation.get(invocation_id, []), key=lambda item: item["seq"]):
            refs.append({key: event[key] for key in (
                "seq", "kind", "method", "path", "status", "request_file", "response_file",
                "request_sha256", "response_sha256", "duration_ms", "selected_candidate_id") if key in event})
        record["provider_exchange_refs"] = [item for item in refs if item["kind"] == "laya_choice"]
        record["health_event_refs"] = [item for item in refs if item["kind"] == "health_check"]
        record["other_event_refs"] = [item for item in refs if item["kind"] not in ("laya_choice", "health_check")]
        record["event_sequences"] = [item["seq"] for item in refs]
        record["choice_event_sequences"] = [item["seq"] for item in record["provider_exchange_refs"]]
        inv_dir = run_dir / "invocations" / invocation_id
        if (inv_dir / "stdout.raw").is_file():
            stdout = (inv_dir / "stdout.raw").read_bytes()
            try:
                payload = json.loads(stdout)
                report = payload.get("report")
                if isinstance(report, dict):
                    report_bytes = canonical_go_json(report)
                    write_bytes(inv_dir / "compiler-report.json", report_bytes)
                    record["compiler_report_file"] = "compiler-report.json"
                    record["compiler_report_sha256"] = sha256(report_bytes)
                    body = report.get("body_search")
                    if isinstance(body, dict):
                        receipt_bytes = canonical_go_json(body)
                        write_bytes(inv_dir / "compiler-body-search-receipt.json", receipt_bytes)
                        record["compiler_body_search_receipt_file"] = "compiler-body-search-receipt.json"
                        record["compiler_body_search_receipt_sha256"] = sha256(receipt_bytes)
            except (json.JSONDecodeError, AttributeError):
                pass
        write_json(inv_dir / "invocation.json", record)


def write_invocation_index(run_dir: Path, study: dict, records: list[dict], results: list[dict] | None = None) -> None:
    by_id = {item["invocation_id"]: item for item in records}
    result_by_id = {item["invocation_id"]: item for item in (results or [])}
    rows = []
    for plan in study["plans"]:
        invocation_id = plan["invocation_id"]
        record = by_id.get(invocation_id, {})
        result = result_by_id.get(invocation_id, {})
        inv_dir = run_dir / "invocations" / invocation_id
        inv_rel = f"invocations/{invocation_id}"
        validation_receipt = run_dir / "validation" / invocation_id / "validation-receipt.json"
        rows.append({
            "sequence": plan["sequence"], "phase": plan["phase"], "invocation_id": invocation_id,
            "intent_id": plan["intent_id"], "treatment": plan["treatment"], "arm": plan["treatment"],
            "provider_model": plan["provider_model"], "model_revision": plan["model_revision"],
            "replicate": plan["replicate"],
            "plan": {"path": f"{inv_rel}/plan.search-plan.json", "source_path": plan["plan_path"],
                     "sha256": plan["plan_sha256"]},
            "fixture": {"path": f"{inv_rel}/fixture.gooo.fixture", "source_path": plan["fixture"],
                        "sha256": plan["fixture_sha256"]},
            "cli": {"stdout_path": f"{inv_rel}/stdout.raw", "stdout_sha256": record.get("stdout_sha256"),
                    "stderr_path": f"{inv_rel}/stderr.raw", "stderr_sha256": record.get("stderr_sha256"),
                    "exit_code": record.get("exit_code"), "attempted": record.get("attempted", True),
                    "resource_path": f"{inv_rel}/resource.json",
                    "resource_sha256": sha256((inv_dir / "resource.json").read_bytes())
                    if (inv_dir / "resource.json").is_file() else None},
            "provider_exchange_refs": record.get("provider_exchange_refs", []),
            "health_event_refs": record.get("health_event_refs", []),
            "compiler_report": {"path": f"{inv_rel}/compiler-report.json"
                                 if record.get("compiler_report_file") else None,
                                 "sha256": record.get("compiler_report_sha256"),
                                 "body_search_receipt_path": f"{inv_rel}/compiler-body-search-receipt.json"
                                 if record.get("compiler_body_search_receipt_file") else None,
                                 "body_search_receipt_sha256": record.get("compiler_body_search_receipt_sha256")},
            "validation_receipt": {"path": str(validation_receipt.relative_to(run_dir)) if validation_receipt.is_file() else None,
                                   "sha256": sha256(validation_receipt.read_bytes()) if validation_receipt.is_file() else None},
            "decision": result.get("decision", record.get("execution_status", "captured_not_runtime_validated")),
        })
    write_json(run_dir / "invocation-index.json", {
        "schema": "gooo/pinned-context-invocation-index/v1", "study_id": study["study_id"],
        "planned_invocations": len(study["plans"]), "invocations": rows,
        "index_note": "Includes every planned invocation; absent provider exchanges, compiler reports, or Go validation receipts remain explicit null/empty evidence fields.",
    })


def write_partial_capture_report(run_dir: Path, study: dict, records: list[dict], events: list[dict],
                                 design_sha: str, binary_receipt: dict, metadata: dict,
                                 capture_error: str | None) -> dict:
    record_by_id = {item["invocation_id"]: item for item in records}
    invocation_rows = []
    for plan in study["plans"]:
        record = record_by_id.get(plan["invocation_id"], {})
        invocation_rows.append({
            "sequence": plan["sequence"], "phase": plan["phase"], "invocation_id": plan["invocation_id"],
            "intent_id": plan["intent_id"], "treatment": plan["treatment"], "arm": plan["treatment"],
            "provider_model_pin": plan["provider_model"], "replicate": plan["replicate"],
            "attempted": record.get("attempted", record.get("exit_code") is not None),
            "cli_exit_code": record.get("exit_code"),
            "choice_post_count": len(record.get("provider_exchange_refs", [])),
            "decision": record.get("pre_next_call_review", {}).get("issues", record.get("error")),
            "unstarted": record.get("attempted") is False,
        })
    group_rows = []
    for treatment in ("legacy_no_feedback", "compact_no_feedback", "compact_external_feedback"):
        for model in ("english", "multilingual"):
            expected = [row for row in study["plans"] if row["phase"] == "measured"
                        and row["treatment"] == treatment and row["provider_model"] == model]
            actual = [row for row in invocation_rows if row["phase"] == "measured"
                      and row["treatment"] == treatment and row["provider_model_pin"] == model]
            group_rows.append({
                "context_treatment": treatment, "provider_model": model,
                "planned_invocations": len(expected),
                "attempted_invocations": sum(bool(row["attempted"]) for row in actual),
                "unstarted_or_unknown_invocations": sum(bool(row["unstarted"]) for row in actual),
                "captured_choice_posts": sum(row["choice_post_count"] for row in actual),
                "compiled_finite_scores": None,
                "runtime_validation_status": "not_started_incomplete_raw_capture",
            })
    captured_posts = [event for event in events if event.get("kind") == "laya_choice"]
    attempted = sum(bool(row["attempted"]) for row in invocation_rows)
    failed = sum(row["attempted"] and row["cli_exit_code"] != 0 for row in invocation_rows)
    unstarted = [row["invocation_id"] for row in invocation_rows if row["unstarted"]]
    report = {
        "schema": "gooo/pinned-compact-context-study-report/v1",
        "study_id": study["study_id"], "run_id": run_dir.name,
        "decision": "PARTIAL_RAW_CAPTURE",
        "design_sha256": design_sha, "compiler": binary_receipt,
        "laya": {"package_version": LAYA_VERSION, "model_revision": MODEL_REVISION,
                 "device": "cpu", "threads": 4},
        "capture": {
            "planned_warmup_calls": 2, "planned_measured_calls": 72,
            "planned_invocations_all_phases": 74, "attempted_invocations": attempted,
            "failed_invocations": failed, "unstarted_or_unknown_invocations": len(unstarted),
            "unstarted_invocation_ids": unstarted,
            "captured_choice_posts_all_phases": len(captured_posts),
            "captured_choice_posts_measured": sum(event.get("invocation_id") in {
                row["invocation_id"] for row in study["plans"] if row["phase"] == "measured"}
                for event in captured_posts),
            "runtime_oracle_validation_started": False,
            "reused_oracle_bytes_opened": False,
            "protocol_preflight_mock_posts": study["protocol_preflight"]["mock_choice_posts"],
            "protocol_preflight_actual_laya_calls": study["protocol_preflight"]["actual_laya_calls"],
            "last_capture_error": capture_error,
        },
        "warmup_results_separate": {model: {"planned_warmups": 1,
            "attempted_warmups": sum(row["phase"] == "warmup" and row["provider_model_pin"] == model
                                      and row["attempted"] for row in invocation_rows)}
            for model in ("english", "multilingual")},
        "results_by_context_and_model": group_rows,
        "invocation_results": invocation_rows,
        "request_route_and_feedback_audits": [], "privacy_request_audits": [],
        "finite_score_status": "unavailable_until_all_raw_provider_exchanges_are_durable",
        "limitations": study["limitations"] + [
            "Capture stopped on the first timeout, fallback, provider error, bad route, or unsettled exchange to prevent overlapping calls.",
            "No finite oracle was read because the complete 74-call raw capture did not finish.",
        ],
        "run_metadata": metadata,
    }
    report_raw = write_json(run_dir / "report.json", report)
    md = ["# Pinned compact training context study", "", "Run ended before runtime oracle validation.", "",
          f"Captured {len(captured_posts)}/74 planned provider POSTs; attempted {attempted}/74 invocations; "
          f"{failed} CLI failures; {len(unstarted)} unstarted/unknown.", "",
          "No finite score is reported because oracle validation begins only after every request and reply is saved.", "",
          f"Capture error: {capture_error or 'none'}", ""]
    write_bytes(run_dir / "report.md", "\n".join(md).encode())
    metadata["partial_report_sha256"] = sha256(report_raw)
    return report


def preserve_failed_or_unattempted(run_dir: Path, row: dict, error: str, *, attempted: bool) -> dict:
    inv_dir = run_dir / "invocations" / row["invocation_id"]
    inv_dir.mkdir(parents=True, exist_ok=True)
    plan_raw = (DESIGN_DIR / row["plan_path"]).read_bytes()
    fixture_raw = (ROOT / row["fixture"]).read_bytes()
    if not (inv_dir / "plan.search-plan.json").exists():
        write_bytes(inv_dir / "plan.search-plan.json", plan_raw)
    if not (inv_dir / "fixture.gooo.fixture").exists():
        write_bytes(inv_dir / "fixture.gooo.fixture", fixture_raw)
    if not (inv_dir / "training-cases.json").exists():
        write_json(inv_dir / "training-cases.json", read_json(inv_dir / "plan.search-plan.json")["test_cases"])
    if not (inv_dir / "stdout.raw").exists():
        write_bytes(inv_dir / "stdout.raw", b"")
    if not (inv_dir / "stderr.raw").exists():
        write_bytes(inv_dir / "stderr.raw", (error + "\n").encode())
    if not (inv_dir / "resource.json").exists():
        write_json(inv_dir / "resource.json", {
            "schema": "gooo/pinned-context-process-resource/v1", "cli_active_wall_ms": None,
            "harness_sampling_window_ms": None, "cli_process": {"sample_count": 0},
            "owned_laya_server_process": {"sample_count": 0},
            "process_completion_kind": "runner_exception" if attempted else "not_started_due_to_pending_exchange",
        })
    record = {
        "schema": "gooo/pinned-context-invocation/v1", "sequence": row["sequence"],
        "invocation_id": row["invocation_id"], "phase": row["phase"], "intent_id": row["intent_id"],
        "treatment": row["treatment"], "arm": row["treatment"], "provider_model": row["provider_model"],
        "model_revision": row["model_revision"], "replicate": row["replicate"], "activity": row["activity"],
        "plan_source_path": row["plan_path"], "fixture_source_path": row["fixture"],
        "plan_file": "plan.search-plan.json", "fixture_file": "fixture.gooo.fixture",
        "training_cases_file": "training-cases.json", "plan_sha256": sha256(plan_raw),
        "fixture_sha256": sha256(fixture_raw), "stdout_sha256": sha256((inv_dir / "stdout.raw").read_bytes()),
        "stderr_sha256": sha256((inv_dir / "stderr.raw").read_bytes()),
        "exit_code": None, "attempted": attempted, "execution_status": "runner_error" if attempted else "not_started",
        "error": error, "resource_file": "resource.json", "choice_event_sequences": [], "event_sequences": [],
    }
    write_json(inv_dir / "runner-error.json", {"invocation_id": row["invocation_id"], "attempted": attempted,
                                                "error": error})
    write_json(inv_dir / "invocation.json", record)
    return record


def invocation_completion_issues(row: dict, record: dict, run_dir: Path, events: list[dict]) -> list[str]:
    """Fail closed before the next model call if this request fell back or timed out."""
    issues = []
    if record.get("exit_code") != 0:
        issues.append(f"CLI exit code {record.get('exit_code')}")
    current = [event for event in events if event.get("invocation_id") == row["invocation_id"]]
    health = [event for event in current if event.get("kind") == "health_check"]
    posts = [event for event in current if event.get("kind") == "laya_choice"]
    unexpected = [event for event in current if event.get("kind") == "unexpected_proxy_request"]
    if not health or any(event.get("status") != 200 for event in health):
        issues.append("captured resolver health check missing or unsuccessful")
    if unexpected:
        issues.append("unexpected proxy method or path captured")
    if len(posts) != 1:
        issues.append(f"expected one provider POST, captured {len(posts)}")
    if any(event.get("status") != 200 for event in posts):
        issues.append("provider POST did not return HTTP 200; stop to prevent backend overlap")
    if any(event.get("request_sha256") != row.get("protocol_preflight_request_sha256") for event in posts):
        issues.append("raw provider POST differs from the exact frozen MOCK preflight template")

    replies = []
    for event in posts:
        try:
            request_raw = (run_dir / event["request_file"]).read_bytes()
            response_raw = (run_dir / event["response_file"]).read_bytes()
            outer, state = parse_state(request_raw)
            if outer.get("model") != row["provider_model"]:
                issues.append("raw wire model differs from the frozen provider pin")
            replies.append(json.loads(response_raw))
        except Exception as exc:
            issues.append(f"raw provider exchange could not be decoded: {exc}")
    inv_dir = run_dir / "invocations" / row["invocation_id"]
    try:
        payload = json.loads((inv_dir / "stdout.raw").read_bytes())
        report = payload.get("report", {})
        body = report.get("body_search", {})
        attempts = body.get("attempts", [])
        decisions = [attempt.get("decision", {}) for attempt in attempts
                     if attempt.get("decision", {}).get("mode") in ("laya", "provider")]
        if report.get("decision") != "PASS":
            issues.append(f"compiler result is {report.get('decision')!r}")
        if len(decisions) != 1:
            issues.append(f"expected one routed model receipt, found {len(decisions)}")
        elif len(posts) == 1:
            decision = decisions[0]
            outer, state = parse_state((run_dir / posts[0]["request_file"]).read_bytes())
            typed_digest = typed_request_digest(outer, state)
            if decision.get("requested_provider_model") != row["provider_model"]:
                issues.append("Gooo receipt requested_provider_model differs from the explicit pin")
            if decision.get("model_revision") != row["model_revision"]:
                issues.append("Gooo receipt model revision differs from the frozen pin")
            if decision.get("request_sha256") != typed_digest:
                issues.append("Gooo typed request digest does not bind the actual wire request")
            if replies:
                reply = replies[0]
                if (reply.get("routing", {}).get("model") != row["provider_model"]
                        or reply.get("answers", {}).get("body_ir_search", {}).get("choice")
                        != body.get("selected_candidate_id")):
                    issues.append("provider routing or returned choice differs from the compiler receipt")
    except Exception as exc:
        issues.append(f"compiler receipt could not be decoded: {exc}")
    return issues


def run_cli(binary: Path, row: dict, run_dir: Path, proxy: CaptureProxy, server_pid: int, env: dict) -> dict:
    invocation_id = row["invocation_id"]
    inv_dir = run_dir / "invocations" / invocation_id
    inv_dir.mkdir(parents=True, exist_ok=False)
    plan_source = DESIGN_DIR / row["plan_path"]
    fixture_source = ROOT / row["fixture"]
    plan_bytes, fixture_bytes = plan_source.read_bytes(), fixture_source.read_bytes()
    if sha256(plan_bytes) != row["plan_sha256"] or sha256(fixture_bytes) != row["fixture_sha256"]:
        raise RuntimeError(f"{invocation_id}: frozen plan or source fixture hash changed")
    plan = json.loads(plan_bytes)
    if "holdout_test_cases" in plan:
        raise RuntimeError(f"{invocation_id}: refusing a choice plan containing holdout cases")
    write_bytes(inv_dir / "plan.search-plan.json", plan_bytes)
    write_bytes(inv_dir / "fixture.gooo.fixture", fixture_bytes)
    write_json(inv_dir / "training-cases.json", plan["test_cases"])
    command = [str(binary), "body-codegen", "--json", "--fill-search", str(inv_dir / "plan.search-plan.json"),
               "--activity", row["activity"], str(inv_dir / "fixture.gooo.fixture")]
    cli_env = env.copy()
    cli_env["GOOO_LAYA_URL"] = proxy.url
    cli_env.pop("GOOO_LAYA_API_KEY", None)
    started_utc, started_ns, started = now_utc(), time.time_ns(), time.monotonic()
    proxy.set_invocation(invocation_id)
    samples: list[dict] = []
    sample_stop = threading.Event()

    def take_sample(cli_pid: int | None) -> None:
        sample = {"sampled_utc": now_utc(), "sampled_unix_ns": time.time_ns(),
                  "elapsed_ms": (time.monotonic() - started) * 1000,
                  "cli": sample_pid(cli_pid), "laya_server": sample_pid(server_pid)}
        samples.append(sample)
        with (inv_dir / "resource-samples.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    process = None
    sampler = None
    stdout = stderr = b""
    exit_code = 124
    completion_kind = "not_started"
    completed_mono = None
    completed_ns = None
    try:
        process = subprocess.Popen(command, cwd=inv_dir, env=cli_env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        take_sample(process.pid)

        def sample_loop():
            while not sample_stop.wait(SAMPLE_INTERVAL):
                take_sample(process.pid if process is not None else None)

        sampler = threading.Thread(target=sample_loop, daemon=True)
        sampler.start()
        try:
            stdout, stderr = process.communicate(timeout=CALL_TIMEOUT_SECONDS)
            exit_code = process.returncode
            completion_kind = "communicate_returned"
        except subprocess.TimeoutExpired:
            if process.poll() is None and os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if process.poll() is None and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=5)
            exit_code = 124
            stderr += f"\nrunner watchdog: {CALL_TIMEOUT_SECONDS} seconds\n".encode()
            completion_kind = "runner_watchdog_killed"
        completed_mono, completed_ns = time.monotonic(), time.time_ns()
    except Exception as exc:
        stderr = (str(exc) + "\n").encode()
        completion_kind = "runner_exception"
    finally:
        if process is not None:
            take_sample(process.pid)
        sample_stop.set()
        if sampler is not None:
            sampler.join(timeout=3)
        proxy.set_invocation(None)
    harness_done_mono, harness_done_ns = time.monotonic(), time.time_ns()
    write_bytes(inv_dir / "stdout.raw", stdout)
    write_bytes(inv_dir / "stderr.raw", stderr)
    resource = {
        "schema": "gooo/pinned-context-process-resource/v1",
        "cli_active_wall_ms": (completed_mono - started) * 1000 if completed_mono is not None else None,
        "process_completion_kind": completion_kind,
        "harness_sampling_window_ms": (harness_done_mono - started) * 1000,
        "sampling_interval_ms": SAMPLE_INTERVAL * 1000,
        "cli_process": process_summary(samples, "cli"),
        "owned_laya_server_process": process_summary(samples, "laya_server"),
        "raw_samples": "resource-samples.jsonl",
    }
    write_json(inv_dir / "resource.json", resource)
    events = [event for event in proxy.events if event.get("invocation_id") == invocation_id]
    record = {
        "schema": "gooo/pinned-context-invocation/v1", "sequence": row["sequence"],
        "invocation_id": invocation_id, "phase": row["phase"], "intent_id": row["intent_id"],
        "treatment": row["treatment"], "provider_model": row["provider_model"],
        "model_revision": row["model_revision"], "replicate": row["replicate"], "activity": row["activity"],
        "plan_source_path": row["plan_path"], "fixture_source_path": row["fixture"],
        "started_utc": started_utc, "started_unix_ns": started_ns,
        "process_completed_unix_ns": completed_ns, "harness_completed_unix_ns": harness_done_ns,
        "exit_code": exit_code, "harness_watchdog_seconds": CALL_TIMEOUT_SECONDS, "argv": command,
        "plan_file": "plan.search-plan.json", "fixture_file": "fixture.gooo.fixture",
        "training_cases_file": "training-cases.json",
        "plan_sha256": sha256(plan_bytes), "fixture_sha256": sha256(fixture_bytes),
        "expected_input_token_count": row["expected_input_token_count"],
        "expected_input_token_budget": row["expected_input_token_budget"],
        "stdout_sha256": sha256(stdout), "stderr_sha256": sha256(stderr),
        "resource_file": "resource.json", "choice_event_sequences": [event["seq"] for event in events],
        "event_sequences": [event["seq"] for event in events],
    }
    write_json(inv_dir / "invocation.json", record)
    return record


def validate_captured_calls(run_dir: Path, study: dict, cli_records: list[dict], events: list[dict],
                            oracle_dir: Path, binary_env: dict) -> tuple[list[dict], dict]:
    """Read holdout vectors only after all raw model request/response files are durable."""
    if len(cli_records) != 74:
        raise RuntimeError("runtime validation is gated on completion of all 74 scheduled CLI captures")
    measured = [row for row in study["plans"] if row["phase"] == "measured"]
    if len(measured) != 72:
        raise RuntimeError("frozen design does not contain exactly 72 measured choice calls")
    for row in study["plans"]:
        inv_dir = run_dir / "invocations" / row["invocation_id"]
        if not (inv_dir / "stdout.raw").is_file() or not (inv_dir / "stderr.raw").is_file():
            raise RuntimeError(f"{row['invocation_id']}: raw CLI bytes were not saved before validation")
    if not all((run_dir / event[path_key]).is_file()
               for event in events for path_key in ("request_file", "response_file")):
        raise RuntimeError("raw provider exchanges are incomplete before validation")

    manifest = read_json(ROOT / "manifest.json")
    manifest_items = {item["id"]: item for item in manifest["intents"]}
    plan_by_id = {row["invocation_id"]: read_json(run_dir / "invocations" / row["invocation_id"] / "plan.search-plan.json")
                  for row in study["plans"]}
    request_audits: list[dict] = []
    privacy_rows: list[dict] = []
    result_rows: list[dict] = []
    decoded_questions: dict[tuple[str, str, str], bytes] = {}

    # This is deliberately the first point where independent finite oracles are read.
    for intent_id, item in manifest_items.items():
        oracle_path = oracle_dir / f"{intent_id}.oracle.json"
        oracle_bytes = oracle_path.read_bytes()
        oracle = json.loads(oracle_bytes)
        if sha256(oracle_bytes) != item["independent_oracle_sha256"]:
            raise RuntimeError(f"{intent_id}: reused finite oracle does not match the predeclared digest")
        if oracle.get("schema") != "gooo/ir-search-finite-oracle/v1" or oracle.get("intent_id") != intent_id:
            raise RuntimeError(f"{intent_id}: finite oracle identity mismatch")
        base_plan = read_json(ROOT / item["plan"])
        if ([case["input"] for case in base_plan["test_cases"]] != oracle["training"]["inputs"]
                or [case["expected"] for case in base_plan["test_cases"]] != oracle["training"]["expected"]):
            raise RuntimeError(f"{intent_id}: finite oracle training suite differs from frozen training plan")
        forbidden = holdout_case_pairs(oracle)

        for row in study["plans"]:
            if row["intent_id"] != intent_id:
                continue
            invocation_id = row["invocation_id"]
            inv_dir = run_dir / "invocations" / invocation_id
            cli_record = read_json(inv_dir / "invocation.json")
            plan = plan_by_id[invocation_id]
            all_events = [event for event in events if event.get("invocation_id") == invocation_id]
            choice_events = [event for event in all_events if event.get("kind") == "laya_choice"]
            request_check = {"invocation_id": invocation_id, "raw_choice_posts": len(choice_events),
                             "requests_scanned_for_reused_holdout_pairs": 0,
                             "provider_route_pin_match": False, "provider_context_pin_match": False,
                             "provider_model_typed_request_match": False, "provider_model_outer_wire_match": False,
                             "provider_receipt_route_pin_match": False,
                             "protocol_preflight_request_match": False,
                             "reconstructed_typed_request_sha256s": []}
            captured_choices = []
            sent_state = None
            response_payloads = []
            feedback_matches = []
            for event in choice_events:
                request_bytes = (run_dir / event["request_file"]).read_bytes()
                response_bytes = (run_dir / event["response_file"]).read_bytes()
                if sha256(request_bytes) != event["request_sha256"] or sha256(response_bytes) != event["response_sha256"]:
                    raise RuntimeError(f"{invocation_id}: raw Laya exchange hash mismatch")
                scan = scan_selection_body(request_bytes, forbidden)
                request_check["requests_scanned_for_reused_holdout_pairs"] += 1
                outer, sent_state = parse_state(request_bytes)
                if (sent_state.get("intent") != plan["intent"] or sent_state.get("activity") != row["activity"]
                        or sent_state.get("training_test_count") != len(plan["test_cases"])):
                    raise RuntimeError(f"{invocation_id}: captured request intent/activity/training count differs from its plan")
                expected_profile = row["expected_model_state_profile"]
                if (sent_state != expected_profile["decoded_state"]
                        or sha256(outer.get("state", {}).get("request", "").encode("utf-8"))
                        != expected_profile["state_request_sha256"]):
                    raise RuntimeError(f"{invocation_id}: captured decoded model state differs from the frozen mock template")
                question_wire_bytes = json.dumps(outer.get("questions", {}), ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8")
                if sha256(question_wire_bytes) != row["expected_question_sha256"]:
                    raise RuntimeError(f"{invocation_id}: captured chooser question differs from its frozen mock template")
                request_check["protocol_preflight_request_match"] = event["request_sha256"] == row["protocol_preflight_request_sha256"]
                if not request_check["protocol_preflight_request_match"]:
                    raise RuntimeError(f"{invocation_id}: actual raw provider request differs from its frozen mock template")
                canonical_training = json.dumps(
                    [{"input": case["input"], "expected": case["expected"]} for case in plan["test_cases"]],
                    separators=(",", ":"), ensure_ascii=False,
                ).encode()
                suite_digest = "sha256:" + sha256(canonical_training)
                prompt_profile = plan.get("prompt_profile", "")
                if prompt_profile == "compact":
                    if "training_suite_sha256" in sent_state:
                        raise RuntimeError(f"{invocation_id}: compact model state exposes the omitted training-suite hash")
                elif sent_state.get("training_suite_sha256") != suite_digest:
                    raise RuntimeError(f"{invocation_id}: legacy typed training-suite digest differs from plan")
                wanted = [{"id": candidate["id"], "expression": candidate["expression"]}
                          for candidate in plan["candidates"]]
                received = [{"id": candidate["id"], "expression": candidate["expression"]}
                            for candidate in sent_state.get("remaining_candidates", [])]
                if wanted != received:
                    raise RuntimeError(f"{invocation_id}: captured candidate options differ from the frozen plan")
                for key in sent_state:
                    if "holdout" in str(key).lower():
                        raise RuntimeError(f"{invocation_id}: holdout field reached the selection request")
                outer_model = outer.get("model")
                request_check["provider_model_outer_wire_match"] = outer_model == row["provider_model"]
                request_check["reconstructed_typed_request_sha256s"].append(typed_request_digest(outer, sent_state))
                request_check["provider_route_pin_match"] = request_check["provider_model_outer_wire_match"]
                sent_feedback = sent_state.get("external_training_feedback")
                source_feedback = plan.get("external_training_feedback")
                if row["treatment"] == "compact_external_feedback":
                    failures = [observation for observation in source_feedback["observations"]
                                if observation.get("passed") is False]
                    expected_triples = [{key: observation[key] for key in ("input", "expected", "actual")}
                                        for observation in failures[:8]]
                    if not isinstance(sent_feedback, dict):
                        raise RuntimeError(f"{invocation_id}: compact feedback arm omitted model-visible training feedback")
                    for key, expected in (("candidate_id", source_feedback["candidate_id"]),
                                          ("failed_cases", expected_triples),
                                          ("failed_cases_total", len(failures)),
                                          ("failed_cases_truncated", len(failures) > 8)):
                        if sent_feedback.get(key) != expected:
                            raise RuntimeError(f"{invocation_id}: compact model feedback differs from source CI failures")
                    if any(key in sent_feedback for key in ("source_digest", "training_suite_sha256")):
                        raise RuntimeError(f"{invocation_id}: compact state exposed local-only feedback provenance hashes")
                    feedback_matches.append(True)
                else:
                    if sent_feedback is not None:
                        raise RuntimeError(f"{invocation_id}: no-feedback arm received external feedback")
                    feedback_matches.append(True)
                response_payload = json.loads(response_bytes)
                if event.get("selected_candidate_id") != response_payload.get("answers", {}).get("body_ir_search", {}).get("choice"):
                    raise RuntimeError(f"{invocation_id}: capture selection differs from raw provider reply")
                routing_model = response_payload.get("routing", {}).get("model")
                request_check["provider_receipt_route_pin_match"] = (
                    request_check["provider_receipt_route_pin_match"]
                    if response_payloads else True
                ) and routing_model == row["provider_model"]
                response_payloads.append(response_payload)
                captured_choices.append(event.get("selected_candidate_id"))
                privacy_rows.append({"invocation_id": invocation_id, "sequence": event["seq"], **scan})
                qwire = json.dumps(outer.get("questions", {}), ensure_ascii=False, separators=(",", ":")).encode()
                decoded_questions[(intent_id, row["treatment"], row["provider_model"])] = qwire

            result = {"sequence": row["sequence"], "phase": row["phase"], "invocation_id": invocation_id,
                      "intent_id": intent_id, "treatment": row["treatment"], "arm": row["treatment"],
                      "provider_model_pin": row["provider_model"], "model_revision_pin": row["model_revision"],
                      "replicate": row["replicate"], "cli_exit_code": cli_record["exit_code"],
                      "choice_post_count": len(choice_events), "provider_reply_choices": captured_choices,
                      "provider_route_pin_match": request_check["provider_route_pin_match"],
                      "provider_context_pin_match": all(feedback_matches) and bool(feedback_matches),
                      "provider_reply_routing_models": [payload.get("routing", {}).get("model") for payload in response_payloads],
                      "cli_active_wall_ms": read_json(inv_dir / "resource.json")["cli_active_wall_ms"],
                      "harness_sampling_window_ms": read_json(inv_dir / "resource.json")["harness_sampling_window_ms"],
                      "cli_process_samples": read_json(inv_dir / "resource.json")["cli_process"],
                      "owned_server_process_samples": read_json(inv_dir / "resource.json")["owned_laya_server_process"],
                      "capture_response_match": bool(captured_choices),
                      "choice_post_exactly_one": len(choice_events) == 1}
            stdout = (inv_dir / "stdout.raw").read_bytes()
            if cli_record["exit_code"] != 0:
                result["decision"] = "CLI_FAILED"
                result["error"] = (inv_dir / "stderr.raw").read_text(encoding="utf-8", errors="replace")[-1000:]
                result_rows.append(result)
                request_audits.append(request_check)
                continue
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                result.update({"decision": "INVALID_COMPILER_JSON", "error": str(exc)})
                result_rows.append(result)
                request_audits.append(request_check)
                continue
            report = payload.get("report", {})
            body = report.get("body_search", {})
            result["compiler_decision"] = report.get("decision")
            result["candidate_id"] = body.get("selected_candidate_id")
            result["candidate_expression"] = body.get("selected_expression")
            result["compiler_training_score"] = {
                "passed": body.get("training_passed"), "total": body.get("training_total")
            }
            attempts = body.get("attempts", [])
            attempt = attempts[0] if attempts else {}
            chooser = attempt.get("decision", {})
            result["chooser_receipt"] = chooser
            request_check["receipt_request_sha256"] = chooser.get("request_sha256")
            request_check["provider_model_typed_request_match"] = (
                bool(request_check["reconstructed_typed_request_sha256s"])
                and all(value == chooser.get("request_sha256")
                        for value in request_check["reconstructed_typed_request_sha256s"])
            )
            request_check["typed_request_hash_matches_receipt"] = request_check["provider_model_typed_request_match"]
            request_check["provider_route_pin_match"] = (
                request_check["provider_model_outer_wire_match"]
                and request_check["provider_model_typed_request_match"]
            )
            result["provider_receipt_model"] = chooser.get("requested_provider_model",
                                                            chooser.get("provider_model", chooser.get("model")))
            result["provider_receipt_model_revision"] = chooser.get("model_revision")
            result["provider_receipt_route_pin_match"] = (
                result["provider_receipt_model"] == row["provider_model"]
                and result["provider_receipt_model_revision"] == row["model_revision"]
                and request_check["provider_receipt_route_pin_match"]
                and request_check["provider_route_pin_match"]
            )
            request_check["provider_receipt_route_pin_match"] = result["provider_receipt_route_pin_match"]
            result["model_choice_attempt_count"] = sum(
                candidate_attempt.get("decision", {}).get("mode") in ("laya", "provider")
                for candidate_attempt in attempts
            )
            result["expected_input_token_count"] = row["expected_input_token_count"]
            result["expected_input_token_budget"] = row["expected_input_token_budget"]
            result["provider_reported_input_tokens"] = [
                payload.get("usage", {}).get("input_tokens") for payload in response_payloads
            ]
            result["provider_reply_capture_refs"] = [
                {"seq": event["seq"], "request_file": event["request_file"],
                 "request_sha256": event["request_sha256"], "response_file": event["response_file"],
                 "response_sha256": event["response_sha256"], "routing_model": payload.get("routing", {}).get("model")}
                for event, payload in zip(choice_events, response_payloads)
            ]
            if len(choice_events) != 1 or result["model_choice_attempt_count"] != 1:
                result["decision"] = "PROVIDER_CHOICE_POST_COUNT_MISMATCH"
                result_rows.append(result)
                request_audits.append(request_check)
                continue
            result["captured_reply_matches_compiler_choice"] = bool(
                captured_choices and body.get("selected_candidate_id") == captured_choices[-1]
            )
            if report.get("decision") != "PASS" or not payload.get("source"):
                result["decision"] = report.get("decision", "NO_REPORT")
                result_rows.append(result)
                request_audits.append(request_check)
                continue
            source = payload["source"].encode()
            if report.get("generated_digest") != "sha256:" + sha256(source):
                raise RuntimeError(f"{invocation_id}: generated Go source bytes differ from the compiler digest")
            if not result["captured_reply_matches_compiler_choice"]:
                result["decision"] = "CAPTURED_REPLY_RECEIPT_MISMATCH"
                result_rows.append(result)
                request_audits.append(request_check)
                continue
            selected_id = result["candidate_id"]
            expected_expression = next((candidate["expression"] for candidate in plan["candidates"]
                                        if candidate["id"] == selected_id), None)
            if not expected_expression or expected_expression != result["candidate_expression"]:
                raise RuntimeError(f"{invocation_id}: selected expression differs from frozen candidate list")

            # Runtime oracle validation starts only here, after all 72 measured
            # posts/replies (and both warmups) have been saved by main().
            result["compiled_validation"] = compile_and_score(
                run_dir, row, source, item["activity"], oracle, oracle_bytes,
                plan, selected_id, report, body, binary_env,
            )
            result["decision"] = ("CAPTURED_AND_COMPILED" if result["provider_receipt_route_pin_match"]
                                  else "COMPILED_PROVIDER_ROUTE_MISMATCH")
            request_audits.append(request_check)
            result_rows.append(result)
    for intent_id in manifest_items:
        for model in ("english", "multilingual"):
            compact_no = decoded_questions.get((intent_id, "compact_no_feedback", model))
            compact_yes = decoded_questions.get((intent_id, "compact_external_feedback", model))
            if compact_no is not None and compact_yes is not None and compact_no != compact_yes:
                raise RuntimeError(f"{intent_id}/{model}: compact arms did not use identical chooser instructions")
    return result_rows, {"invocation_checks": request_audits, "privacy_scans": privacy_rows}


def make_probe_source(activity: str, suites: dict[str, list[dict[str, int]]]) -> str:
    cases = []
    for suite_name, rows in suites.items():
        for index, case in enumerate(rows):
            if not INT64_MIN <= int(case["input"]) <= INT64_MAX or not INT64_MIN <= int(case["expected"]) <= INT64_MAX:
                raise RuntimeError("finite oracle includes a value outside int64")
            cases.append(f'{{Suite: {json.dumps(suite_name)}, Index: {index}, Input: {int(case["input"])}, Expected: {int(case["expected"])}}}')
    return f'''package bodycodegen

import (
	"encoding/json"
	"testing"
)

type pinnedProbeCase struct {{ Suite string; Index int; Input int64; Expected int64 }}
type pinnedProbeObservation struct {{ Suite string `json:"suite"`; Index int `json:"index"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"`; Passed bool `json:"passed"` }}

func TestPinnedFiniteOracle(t *testing.T) {{
	cases := []pinnedProbeCase{{{', '.join(cases)}}}
	for _, testCase := range cases {{
		observation := pinnedProbeObservation{{Suite: testCase.Suite, Index: testCase.Index, Input: testCase.Input, Expected: testCase.Expected, Actual: {activity}(testCase.Input)}}
		observation.Passed = observation.Actual == observation.Expected
		encoded, err := json.Marshal(observation)
		if err != nil {{ t.Fatal(err) }}
		t.Logf("PINNED_CASE_RESULT:%s", encoded)
		if !observation.Passed {{ t.Errorf("PINNED_CASE_MISMATCH:%s", encoded) }}
	}}
}}
'''


def parse_markers(output: bytes, prefix: str) -> list[dict]:
    observations = []
    for line in output.decode(errors="replace").splitlines():
        marker = prefix + ":"
        if marker in line:
            payload = line.split(marker, 1)[1]
            try:
                observations.append(json.loads(payload))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"compiled Go probe emitted invalid observation JSON: {payload}") from exc
    return observations


def compile_and_score(run_dir: Path, row: dict, source: bytes, activity: str, oracle: dict, oracle_bytes: bytes,
                      plan: dict, selected_id: str, report: dict, body: dict, env: dict) -> dict:
    validation = run_dir / "validation" / row["invocation_id"]
    validation.mkdir(parents=True, exist_ok=False)
    write_bytes(validation / "emitted.go", source)
    suites = {name: [{"input": input_value, "expected": expected}
                     for input_value, expected in zip(oracle[name]["inputs"], oracle[name]["expected"])]
              for name in ("training", "holdout")}
    if any(len(oracle[name]["inputs"]) != len(oracle[name]["expected"]) for name in suites):
        raise RuntimeError(f"{row['invocation_id']}: finite oracle suite has different input/expected lengths")
    probe_bytes = make_probe_source(activity, suites).encode()
    write_bytes(validation / "probe_test.go", probe_bytes)
    write_bytes(validation / "independent-oracle.json", oracle_bytes)
    write_bytes(validation / "go.mod", b"module pinned-context-probe\n\ngo 1.27.0\n")
    started_ns = time.time_ns()
    test = subprocess.run(["go", "test", "-count=1", "-v", "./..."], cwd=validation,
                          env=env, capture_output=True, check=False, timeout=120)
    finished_ns = time.time_ns()
    write_bytes(validation / "go-test.stdout.raw", test.stdout)
    write_bytes(validation / "go-test.stderr.raw", test.stderr)
    observations = sorted(parse_markers(test.stdout, "PINNED_CASE_RESULT"), key=lambda item: (item["suite"], item["index"]))
    mismatches = parse_markers(test.stdout, "PINNED_CASE_MISMATCH")
    expected_count = sum(len(rows) for rows in suites.values())
    if len(observations) != expected_count:
        raise RuntimeError(f"{row['invocation_id']}: compiled Go did not observe every finite oracle case")
    compiled: dict[str, list[int]] = {"training": [], "holdout": []}
    for observation in observations:
        name, index = observation["suite"], int(observation["index"])
        if name not in suites or not 0 <= index < len(suites[name]):
            raise RuntimeError(f"{row['invocation_id']}: compiled Go emitted an undeclared suite index")
        case = suites[name][index]
        if (observation["input"], observation["expected"], observation["passed"]) != (
                case["input"], case["expected"], observation["actual"] == case["expected"]):
            raise RuntimeError(f"{row['invocation_id']}: compiled Go observation fields are inconsistent")
        if len(compiled[name]) <= index:
            compiled[name].extend([None] * (index + 1 - len(compiled[name])))
        compiled[name][index] = int(observation["actual"])
    expected_exit = 1 if mismatches else 0
    combined = (test.stdout + test.stderr).lower()
    if test.returncode != expected_exit or b"setup failed" in combined or b"build failed" in combined:
        raise RuntimeError(f"{row['invocation_id']}: Go runtime oracle probe failed outside expected case mismatches")
    if any(any(value is None for value in outputs) for outputs in compiled.values()):
        raise RuntimeError(f"{row['invocation_id']}: compiled Go observations are incomplete")

    candidate_outputs = oracle["candidate_outputs"]
    if selected_id not in candidate_outputs:
        raise RuntimeError(f"{row['invocation_id']}: selected candidate is missing from the reused finite oracle")
    score_parts = {}
    candidate_partition = {}
    for suite_name in ("training", "holdout"):
        suite = oracle[suite_name]
        expected = suite["expected"]
        vectors = {key: output[suite_name] for key, output in candidate_outputs.items()}
        if any(len(vector) != len(expected) for vector in vectors.values()):
            raise RuntimeError(f"{row['invocation_id']}: oracle candidate vector length mismatch")
        differing = [index for index in range(len(expected))
                     if len({vector[index] for vector in vectors.values()}) > 1]
        invariant = [index for index in range(len(expected)) if index not in differing]
        if compiled[suite_name] != vectors[selected_id]:
            raise RuntimeError(f"{row['invocation_id']}: compiled Go output differs from selected candidate's reused oracle vector")

        def score(indexes: list[int]) -> dict:
            return {"passed": sum(compiled[suite_name][index] == expected[index] for index in indexes),
                    "total": len(indexes)}

        score_parts[suite_name] = {
            "all_cases": score(list(range(len(expected)))),
            "candidate_discriminating_cases": score(differing),
            "candidate_invariant_cases": score(invariant),
        }
        candidate_partition[suite_name] = {"total": len(expected), "candidate_discriminating_indexes": differing,
                                           "candidate_invariant_indexes": invariant}
    training_score = score_parts["training"]["all_cases"]
    if body.get("training_passed") != training_score["passed"] or body.get("training_total") != training_score["total"]:
        raise RuntimeError(f"{row['invocation_id']}: compiled training score differs from the Gooo receipt")
    receipt = {
        "schema": "gooo/pinned-context-independent-go-validation/v1",
        "invocation_id": row["invocation_id"], "intent_id": row["intent_id"],
        "candidate_id": selected_id, "candidate_expression": body.get("selected_expression"),
        "source_sha256": sha256(source), "compiler_generated_digest": report.get("generated_digest"),
        "oracle_sha256": sha256(oracle_bytes), "compiled_case_count": len(observations),
        "training_score": score_parts["training"]["all_cases"],
        "holdout_score_reused_benchmark": score_parts["holdout"]["all_cases"],
        "finite_scores": score_parts, "candidate_partition": candidate_partition,
        "go_test_exit_code": test.returncode, "go_test_expected_exit_code": expected_exit,
        "go_test_stdout_sha256": sha256(test.stdout), "go_test_stderr_sha256": sha256(test.stderr),
        "timing_unix_ns": {"go_test_started": started_ns, "go_test_finished": finished_ns},
        "scope": "Independent compiled Go execution against the already-known finite oracle; holdout vectors are reused benchmark cases, not fresh unseen generalization evidence.",
    }
    write_json(validation / "validation-receipt.json", receipt)
    return receipt


def summarize(study: dict, rows: list[dict], events: list[dict], request_audits: dict, run_dir: Path,
              design_sha: str, binary_receipt: dict, metadata: dict) -> dict:
    measured = [row for row in rows if row["phase"] == "measured"]
    warmups = [row for row in rows if row["phase"] == "warmup"]
    group_reports = []
    for treatment in ("legacy_no_feedback", "compact_no_feedback", "compact_external_feedback"):
        for model in ("english", "multilingual"):
            group = [row for row in measured if row["treatment"] == treatment and row["provider_model_pin"] == model]
            aggregate: dict[str, dict[str, dict[str, int]]] = {
                suite: {scope: {"passed": 0, "observed_total": 0, "planned_total": 0, "unknown_total": 0}
                        for scope in ("all_cases", "candidate_discriminating_cases", "candidate_invariant_cases")}
                for suite in ("training", "holdout")
            }
            for row in group:
                oracle = read_json(run_dir / "oracles" / f"{row['intent_id']}.oracle.json")
                for suite in ("training", "holdout"):
                    candidate_vectors = {key: value[suite] for key, value in oracle["candidate_outputs"].items()}
                    size = len(oracle[suite]["expected"])
                    discriminating = sum(
                        len({vector[index] for vector in candidate_vectors.values()}) > 1
                        for index in range(size)
                    )
                    aggregate[suite]["all_cases"]["planned_total"] += size
                    aggregate[suite]["candidate_discriminating_cases"]["planned_total"] += discriminating
                    aggregate[suite]["candidate_invariant_cases"]["planned_total"] += size - discriminating
                validation = row.get("compiled_validation")
                if not validation:
                    continue
                for suite in ("training", "holdout"):
                    for scope, score in validation["finite_scores"][suite].items():
                        target = aggregate[suite][scope]
                        target["passed"] += score["passed"]
                        target["observed_total"] += score["total"]
            for suite in aggregate.values():
                for score in suite.values():
                    score["unknown_total"] = score["planned_total"] - score["observed_total"]
            routes = sum(bool(row.get("provider_receipt_route_pin_match")) for row in group)
            replies = sum(bool(row.get("captured_reply_matches_compiler_choice")) for row in group)
            validated = sum("compiled_validation" in row for row in group)
            group_reports.append({
                "context_treatment": treatment, "provider_model": model,
                "planned_intent_replicate_invocations": 12,
                "completed_invocation_records": len(group),
                "captured_laya_choice_posts": sum(row["choice_post_count"] for row in group),
                "compiler_cli_successes": sum(row["cli_exit_code"] == 0 for row in group),
                "failed_invocations": sum(row.get("decision") != "CAPTURED_AND_COMPILED" for row in group),
                "independent_go_validations": validated,
                "unvalidated_or_unknown_invocations": 12 - validated,
                "captured_reply_compiler_choice_agreement": {"passed": replies, "total": 12},
                "provider_model_receipt_pin_agreement": {"passed": routes, "total": 12},
                "compiled_finite_scores": aggregate,
                "latency_ms": {
                    "cli_active_wall_ms_observations": [row["cli_active_wall_ms"] for row in group],
                    "harness_sampling_window_ms_observations": [row["harness_sampling_window_ms"] for row in group],
                    "warmup_latencies_excluded": True,
                },
                "resource_samples": [{"invocation_id": row["invocation_id"],
                                      "cli": row["cli_process_samples"],
                                      "owned_server": row["owned_server_process_samples"]} for row in group],
            })
    model_counts = {model: {
        "planned_warmups": 1,
        "completed_warmups": sum(row["cli_exit_code"] == 0 for row in warmups if row["provider_model_pin"] == model),
        "warmup_cli_active_wall_ms": [row["cli_active_wall_ms"] for row in warmups if row["provider_model_pin"] == model],
    } for model in ("english", "multilingual")}
    invocation_checks = request_audits.get("invocation_checks", [])
    measured_ids = {row["invocation_id"] for row in measured}
    measured_checks = [row for row in invocation_checks if row.get("invocation_id") in measured_ids]
    route_posts = sum(bool(row.get("provider_receipt_route_pin_match")) for row in measured_checks)
    feedback_matches = sum(bool(row.get("provider_context_pin_match")) for row in measured_checks)
    planned_invocations = 74
    invocation_records_complete = len(rows) == planned_invocations
    every_invocation_validated = (invocation_records_complete
                                  and all(row.get("decision") == "CAPTURED_AND_COMPILED" for row in rows))
    total_choice_posts = sum(event.get("kind") == "laya_choice" for event in events)
    measured_choice_posts = sum(event.get("kind") == "laya_choice"
                                and event.get("invocation_id") in {row["invocation_id"] for row in measured}
                                for event in events)
    route_agreement_complete = route_posts == 72
    feedback_agreement_complete = feedback_matches == 72
    health_after_valid = bool(metadata.get("health_after_validated"))
    return {
        "schema": "gooo/pinned-compact-context-study-report/v1",
        "study_id": study["study_id"], "run_id": run_dir.name,
        "decision": "CAPTURED_AND_COMPILED" if len(measured) == 72 and len(warmups) == 2
                    and total_choice_posts == planned_invocations and measured_choice_posts == 72
                    and every_invocation_validated and route_agreement_complete
                    and feedback_agreement_complete and health_after_valid
                    else "PARTIAL_CAPTURE_OR_VALIDATION",
        "design_sha256": design_sha, "compiler": binary_receipt,
        "laya": {"package_version": LAYA_VERSION, "model_revision": MODEL_REVISION,
                 "device": "cpu", "threads": 4},
        "capture": {"planned_warmup_calls": 2, "planned_measured_calls": 72,
                    "planned_invocations_all_phases": planned_invocations,
                    "completed_cli_invocations": len(rows),
                    "failed_invocations": sum(row.get("decision") != "CAPTURED_AND_COMPILED" for row in rows),
                    "validated_invocations": sum(row.get("decision") == "CAPTURED_AND_COMPILED" for row in rows),
                    "unknown_or_unvalidated_invocations": planned_invocations - sum(
                        row.get("decision") == "CAPTURED_AND_COMPILED" for row in rows),
                    "captured_choice_posts_all_phases": total_choice_posts,
                    "captured_choice_posts_measured": measured_choice_posts,
                    "captured_choice_posts_warmup": total_choice_posts - measured_choice_posts,
                    "measured_requests_matching_plan_provider_model": {"passed": route_posts, "total": 72},
                    "measured_requests_matching_plan_feedback": {"passed": feedback_matches, "total": 72},
                    "health_after_status_and_model_revisions_validated": health_after_valid,
                    "captured_choice_posts_exactly_one_per_invocation": total_choice_posts == planned_invocations,
                    "provider_posts_have_raw_request_and_response": all(
                        (run_dir / event[key]).is_file() for event in events if event.get("kind") == "laya_choice"
                        for key in ("request_file", "response_file")),
                    "laya_choice_events": total_choice_posts,
                    "protocol_preflight_mock_posts": study["protocol_preflight"]["mock_choice_posts"],
                    "protocol_preflight_mock_health_checks": study["protocol_preflight"]["mock_health_checks"],
                    "protocol_preflight_actual_laya_calls": study["protocol_preflight"]["actual_laya_calls"],
                    "preflight_mock_requests_excluded": True},
        "warmup_results_separate": model_counts,
        "results_by_context_and_model": group_reports,
        "invocation_results": rows,
        "request_route_and_feedback_audits": request_audits.get("invocation_checks", []),
        "privacy_request_audits": request_audits.get("privacy_scans", []),
        "timing_scope": "CLI active wall time measures the compiler process; harness sampling window additionally includes process sampling cleanup. Resolver/provider POST latency is captured separately per raw exchange; none is model-forward-only latency.",
        "resource_scope": "Cumulative CPU and sampled RSS are recorded for the compiler CLI and owned Laya server processes. These observations are process-local and are not host CPU increase measurements.",
        "limitations": study["limitations"] + [
            "A raw model reply, Gooo selected-candidate receipt, explicit provider model pin, and source-bound feedback field are recorded separately.",
            "A failed invocation is kept in the denominator and never silently retried or replaced.",
            "The known finite oracle is used as a reused benchmark; no unseen intent-generalization claim is made.",
            "The Gooo resolver has an 8-second request budget. The local capture proxy waits at most 10 seconds upstream and the runner drains pending exchanges for at most 13 seconds before it aborts later planned calls; upstream forwarding can remain active until that bound, and calls are never retried.",
        ],
        "run_metadata": metadata,
    }


def render_report(report: dict) -> str:
    lines = ["# Pinned compact training context study", "",
             f"Run `{report['run_id']}` captured the frozen study with compiler revision `{report['compiler']['source_revision']}` and Laya {report['laya']['package_version']} model revision `{report['laya']['model_revision']}`.", "",
             f"The design scheduled {report['capture']['planned_measured_calls']} measured choices in 4 known intent × 3 prompt-profile arms × 2 explicit-model cells with 3 repeats, plus {report['capture']['planned_warmup_calls']} separately counted warmups. It captured {report['capture']['captured_choice_posts_measured']}/72 measured choice posts and {report['capture']['captured_choice_posts_all_phases']}/74 total choice posts.", "",
             "## Finite compiled scores", "",
             "Scores use independently compiled Go output against the existing finite oracle. Holdout vectors are reused benchmark data and are not a fresh generalization set. Discriminating cases are defined relative to the candidate outputs already declared for that intent.", "",
             "| Context | Explicit model | Go validations | Training all | Training candidate-discriminating | Reused holdout all | Reused holdout candidate-discriminating | Model receipt matches pin |", "|---|---|---:|---:|---:|---:|---:|---:|"]

    def fmt(score: dict | None) -> str:
        return (f"{score.get('passed')}/{score.get('planned_total')} (observed {score.get('observed_total')}, "
                f"unknown {score.get('unknown_total')})") if score else "n/a"

    for group in report["results_by_context_and_model"]:
        scores = group["compiled_finite_scores"]
        train, holdout = scores.get("training", {}), scores.get("holdout", {})
        lines.append("| {context} | {model} | {validations}/12 | {train} | {train_diff} | {holdout} | {holdout_diff} | {route} |".format(
            context=group["context_treatment"], model=group["provider_model"],
            validations=group["independent_go_validations"], train=fmt(train.get("all_cases")),
            train_diff=fmt(train.get("candidate_discriminating_cases")), holdout=fmt(holdout.get("all_cases")),
            holdout_diff=fmt(holdout.get("candidate_discriminating_cases")),
            route=fmt(group["provider_model_receipt_pin_agreement"])))
    lines.extend(["", "## Capture and timing", "",
                  f"Provider-model request pins matched {report['capture']['measured_requests_matching_plan_provider_model']['passed']}/72 measured requests; source-bound feedback state matched its frozen plan for {report['capture']['measured_requests_matching_plan_feedback']['passed']}/72. Any missing route or context observation remains a failed denominator entry.", "",
                  "Warmup CLI latencies are saved under `warmup_results_separate` and excluded from measured-cell latency lists. Each invocation retains compiler active wall time, the wider harness sampling window, per-process CPU/RSS samples, and raw Laya POST/reply bytes.", "",
                  report["timing_scope"], "", report["resource_scope"], "",
                  "## Limits", "", *[f"- {limitation}" for limitation in report["limitations"]], ""])
    return "\n".join(lines)


def copy_reused_oracles(run_dir: Path, oracle_dir: Path) -> dict:
    """Bind known finite benchmark bytes into the run after every provider exchange is saved."""
    manifest = read_json(ROOT / "manifest.json")
    rows = []
    for item in manifest["intents"]:
        source = oracle_dir / f"{item['id']}.oracle.json"
        raw = source.read_bytes()
        if sha256(raw) != item["independent_oracle_sha256"]:
            raise RuntimeError(f"{item['id']}: reused finite benchmark digest differs from manifest")
        destination = run_dir / "oracles" / source.name
        write_bytes(destination, raw)
        rows.append({"intent_id": item["id"], "file": str(destination.relative_to(run_dir)),
                     "sha256": sha256(raw), "bytes": len(raw),
                     "classification": "reused finite benchmark oracle; not fresh unseen cases"})
    index = {"schema": "gooo/pinned-context-reused-oracle-index/v1",
             "loaded_after_all_choice_posts_saved": True, "oracle_count": len(rows), "oracles": rows}
    write_json(run_dir / "oracles" / "index.json", index)
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--laya-venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--oracle-dir", type=Path, default=Path("/Users/alice/meta-go/research/metaprogramming/gooo-metaprogramming-experiments/cohorts/ir-search-2026-09-30/oracles"))
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--max-invocations", type=int, default=74,
                        help="Safety bound. The pinned full study requires 74 (2 warmups + 72 measured).")
    args = parser.parse_args()
    design_bytes = (DESIGN_DIR / "study-design.json").read_bytes()
    design_sha = sha256(design_bytes)
    design = json.loads(design_bytes)
    if (DESIGN_DIR / "study-design.sha256").read_text(encoding="ascii").split()[0] != design_sha:
        raise RuntimeError("frozen study design checksum mismatch")
    if design.get("status") != "frozen_before_laya_calls" or len(design.get("plans", [])) != 74:
        raise RuntimeError("missing, mutable, or incomplete frozen 74-call design")
    planned_rows = design["plans"]
    measured_rows = [row for row in planned_rows if row.get("phase") == "measured"]
    warmup_rows = [row for row in planned_rows if row.get("phase") == "warmup"]
    factorial = {(row["intent_id"], row["treatment"], row["provider_model"], row["replicate"])
                 for row in measured_rows}
    if len(measured_rows) != 72 or len(warmup_rows) != 2 or len(factorial) != 72:
        raise RuntimeError("frozen design does not contain exactly 72 factorial calls plus two warmups")
    if [row["phase"] for row in planned_rows[:2]] != ["warmup", "warmup"]:
        raise RuntimeError("the two declared model warmups must run before randomized measured calls")
    if args.max_invocations != 74:
        raise RuntimeError("the study must preserve both warmups and all 72 measured choices")
    preflight = read_json(DESIGN_DIR / design["protocol_preflight"]["path"])
    if (preflight.get("status") != "PASS_CACHED_TOKENIZERS_NO_MODEL_INFERENCE"
            or preflight.get("mock_choice_posts") != 24 or preflight.get("mock_health_checks") != 24
            or preflight.get("actual_laya_calls") != 0 or preflight.get("provider_calls") != 0
            or preflight.get("unique_intent_arm_model_templates") != 24):
        raise RuntimeError("frozen local MOCK/token preflight is incomplete or includes model calls")
    expected_mock_posts = [event for event in preflight.get("mock_events", [])
                           if event.get("kind") == "protocol_capture_mock"]
    if len(expected_mock_posts) != 24 or any(event.get("counted_as_laya_call") is not False
                                              for event in expected_mock_posts):
        raise RuntimeError("preflight does not retain exactly 24 explicitly excluded MOCK provider posts")
    for row in planned_rows:
        plan_raw = (DESIGN_DIR / row["plan_path"]).read_bytes()
        fixture_raw = (ROOT / row["fixture"]).read_bytes()
        if sha256(plan_raw) != row["plan_sha256"] or sha256(fixture_raw) != row["fixture_sha256"]:
            raise RuntimeError(f"{row['invocation_id']}: frozen plan or fixture digest mismatch before Laya starts")
        if row.get("expected_input_token_count", 10**12) > row.get("expected_input_token_budget", -1):
            raise RuntimeError(f"{row['invocation_id']}: frozen tokenizer preflight exceeds model input budget")
        expected_state = row.get("expected_model_state_profile", {})
        if (expected_state.get("provider_model") != row["provider_model"]
                or expected_state.get("training_suite_sha256_present") != (row.get("prompt_profile", "") != "compact")
                or expected_state.get("external_training_feedback_present") != row["external_training_feedback_present"]):
            raise RuntimeError(f"{row['invocation_id']}: frozen expected model-state profile is inconsistent")
    binary = args.binary.resolve()
    compiler = design["compiler"]
    binary_receipt = verify_binary(binary, compiler["binary_sha256"], compiler["source_revision"])
    venv = args.laya_venv.resolve()
    server_exe, python_exe = venv / "bin" / "laya-serve", venv / "bin" / "python"
    if not server_exe.is_file() or not os.access(server_exe, os.X_OK) or not python_exe.is_file():
        raise RuntimeError(f"pinned offline Laya environment unavailable: {venv}")
    version = subprocess.run([str(python_exe), "-c", "import importlib.metadata as m; print(m.version('laya'))"],
                             capture_output=True, text=True, check=False)
    if version.returncode or version.stdout.strip() != LAYA_VERSION:
        raise RuntimeError(f"Laya version mismatch: {version.stdout.strip()!r}")
    model_cache_receipt = verify_local_model_cache_binding(DESIGN_DIR / design["local_model_cache_binding"])
    run_dir = ROOT / "audit" / args.run_id
    if run_dir.exists():
        raise RuntimeError(f"refusing to overwrite an existing run: {run_dir}")
    run_dir.mkdir(parents=True)
    for subdir in ("laya", "proxy", "invocations"):
        (run_dir / subdir).mkdir()
    import shutil
    frozen_preflight = DESIGN_DIR / "protocol-preflight"
    if not frozen_preflight.is_dir():
        raise RuntimeError("frozen design is missing local MOCK protocol/token preflight captures")
    shutil.copytree(frozen_preflight, run_dir / "protocol-preflight")
    write_bytes(run_dir / "study-design.json", design_bytes)
    write_bytes(run_dir / "study-design.sha256", (DESIGN_DIR / "study-design.sha256").read_bytes())
    write_bytes(run_dir / "source-provenance.json", (DESIGN_DIR / "source-provenance.json").read_bytes())
    write_bytes(run_dir / "native-identity-bridge.json", (DESIGN_DIR / "native-identity-bridge.json").read_bytes())
    write_bytes(run_dir / "local-model-cache-binding.json",
                (DESIGN_DIR / design["local_model_cache_binding"]).read_bytes())
    write_bytes(run_dir / "manifest.json", (ROOT / "manifest.json").read_bytes())
    shutil.copytree(DESIGN_DIR / "feedback", run_dir / "feedback")

    preexecution = {
        "schema": "gooo/pinned-context-preexecution/v1", "study_id": design["study_id"],
        "status": "all_plans_frozen_before_laya_process_start", "design_sha256": design_sha,
        "study_code_provenance": {
            "runner_script_sha256": sha256(Path(__file__).read_bytes()),
            "preparation_script_sha256": sha256((ROOT / "scripts" / "prepare_pinned_context_study.py").read_bytes()),
            "public_frozen_checkpoint_revision": FROZEN_PUBLIC_CHECKPOINT_REVISION,
        },
        "compiler": binary_receipt, "laya_version": version.stdout.strip(),
        "expected_model_revision": MODEL_REVISION, "runtime": design["laya"],
        "protocol_preflight": design["protocol_preflight"],
        "local_model_cache_binding": model_cache_receipt,
        "offline_flags": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                          "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"},
        "invocation_plan_hashes": [{"invocation_id": row["invocation_id"], "phase": row["phase"],
                                     "plan_sha256": row["plan_sha256"], "fixture_sha256": row["fixture_sha256"],
                                     "plan_path": row["plan_path"], "fixture_path": row["fixture"],
                                     "provider_model": row["provider_model"], "treatment": row["treatment"],
                                     "prompt_profile": row.get("prompt_profile", "")}
                                    for row in design["plans"]],
        "holdout_vectors_loaded": False,
    }
    pre_bytes = write_json(run_dir / "preexecution.json", preexecution)
    metadata = {
        "schema": "gooo/pinned-context-run-metadata/v1", "run_id": args.run_id,
        "status": "starting", "started_utc": now_utc(), "preexecution_sha256": sha256(pre_bytes),
        "design_sha256": design_sha, "planned_warmups": 2, "planned_measured_invocations": 72,
        "provider_policy": "owned offline loopback-only Laya; credentials unset; cached models only; no downloads",
        "laya_venv": str(venv), "laya_version": version.stdout.strip(), "model_revision": MODEL_REVISION,
        "device": "cpu", "threads": 4, "host_logical_cpu_count": os.cpu_count(),
        "local_model_cache_binding": model_cache_receipt,
    }
    write_json(run_dir / "run-metadata.json", metadata)

    port = free_port()
    service_env = os.environ.copy()
    service_env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1", "LAYA_HOST": "127.0.0.1", "LAYA_PORT": str(port),
                        "LAYA_MODELS": "english,multilingual", "LAYA_DEVICE": "cpu", "LAYA_THREADS": "4",
                        "LAYA_PRELOAD": "1", "LAYA_MAX_LOADED": "2", "LAYA_AUTO_TASK": "0",
                        "LAYA_REVISION": MODEL_REVISION})
    service_env.pop("GOOO_LAYA_API_KEY", None)
    service_env.pop("LAYA_API_KEY", None)
    server_stdout = (run_dir / "laya" / "stdout.log").open("wb")
    server_stderr = (run_dir / "laya" / "stderr.log").open("wb")
    server = None
    proxy = None
    pgid = None
    cli_records: list[dict] = []
    capture_error = None
    pending_failure = None
    capture_status = {"schema": "gooo/pinned-context-capture-status/v1",
                      "status": "starting", "planned_invocations": 74,
                      "completed_invocation_records": 0, "captured_laya_choice_posts": 0,
                      "runtime_oracle_validation_started": False}
    try:
        server = subprocess.Popen([str(server_exe)], cwd=run_dir, env=service_env, stdin=subprocess.DEVNULL,
                                  stdout=server_stdout, stderr=server_stderr, start_new_session=True)
        pgid = server.pid
        write_json(run_dir / "laya" / "owned-process.json", {
            "schema": "gooo/pinned-context-owned-process/v1", "pid": server.pid,
            "process_group_id": pgid, "started_utc": now_utc(), "executable": str(server_exe),
            "owned_by_runner": True, "host": "127.0.0.1", "port": port,
            "device": "cpu", "loaded_models": ["english", "multilingual"],
        })
        health = wait_for_health(server, port, run_dir, ["english", "multilingual"])
        proxy = CaptureProxy(port, run_dir)
        metadata.update({"status": "running", "laya_port": port, "capture_proxy_url": proxy.url,
                         "health_before": {"device": health["device"], "loaded": health["loaded"],
                                           "revisions": health["revisions"]}})
        write_json(run_dir / "run-metadata.json", metadata)
        cli_env = os.environ.copy()
        cli_env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1", "GOOO_LAYA_URL": proxy.url})
        cli_env.pop("GOOO_LAYA_API_KEY", None)
        stopped_before: list[dict] = []
        for index, row in enumerate(design["plans"]):
            try:
                record = run_cli(binary, row, run_dir, proxy, server.pid, cli_env)
                drain = proxy.wait_for_pending(PENDING_SETTLE_TIMEOUT_SECONDS)
                completion_issues = invocation_completion_issues(row, record, run_dir, proxy.events)
                if not drain["settled"]:
                    completion_issues.append(
                        f"provider exchange pending after {PENDING_SETTLE_TIMEOUT_SECONDS}s: {drain['pending_sequences']}")
                record["pending_exchange_drain"] = {
                    **drain, "timeout_seconds": PENDING_SETTLE_TIMEOUT_SECONDS,
                    "upstream_timeout_seconds": PROXY_UPSTREAM_TIMEOUT_SECONDS,
                }
                record["pre_next_call_review"] = {"passed": not completion_issues, "issues": completion_issues}
                write_json(run_dir / "invocations" / row["invocation_id"] / "invocation.json", record)
                cli_records.append(record)
                if completion_issues:
                    pending_failure = {"invocation_id": row["invocation_id"], **drain,
                                       "issues": completion_issues}
                    capture_error = f"stopped after {row['invocation_id']}: {'; '.join(completion_issues)}"
                    stopped_before = design["plans"][index + 1:]
                    break
            except Exception as exc:
                capture_error = f"{row['invocation_id']}: {exc}"
                cli_records.append(preserve_failed_or_unattempted(run_dir, row, str(exc), attempted=True))
                if proxy is not None:
                    drain = proxy.wait_for_pending(PENDING_SETTLE_TIMEOUT_SECONDS)
                    pending_failure = {"invocation_id": row["invocation_id"], **drain,
                                       "issues": [str(exc)]}
                stopped_before = design["plans"][index + 1:]
                break
        if stopped_before:
            for row in stopped_before:
                cli_records.append(preserve_failed_or_unattempted(
                    run_dir, row, "not started because a prior provider exchange did not settle", attempted=False))
        try:
            raw, health_after = fetch_health(port)
            write_bytes(run_dir / "laya" / "health-after.json", raw)
            valid_after = (health_after.get("status") == "ok" and health_after.get("device") == "cpu"
                           and set(health_after.get("loaded", [])) == {"english", "multilingual"}
                           and all(health_after.get("revisions", {}).get(model) == MODEL_REVISION
                                   for model in ("english", "multilingual")))
            metadata["health_after"] = {"status": health_after.get("status"),
                                         "device": health_after.get("device"),
                                         "loaded": health_after.get("loaded"),
                                         "revisions": health_after.get("revisions", {}),
                                         "raw_sha256": sha256(raw)}
            metadata["health_after_validated"] = valid_after
            if not valid_after:
                metadata["health_after_validation_error"] = "health response status/device/loaded model revisions differ from frozen pin"
        except Exception as exc:
            metadata["health_after_error"] = str(exc)
            metadata["health_after_validated"] = False
        attach_exchange_refs(run_dir, cli_records, proxy.events)
        write_json(run_dir / "cli-invocation-records.json", cli_records)
        write_json(run_dir / "proxy-events.json", {"events": proxy.events,
                                                     "choice_post_count": sum(event["kind"] == "laya_choice" for event in proxy.events)})
        capture_status = {"schema": "gooo/pinned-context-capture-status/v1",
                          "status": "raw_calls_captured" if len(cli_records) == 74 and not capture_error else "partial_raw_capture",
                          "planned_invocations": 74, "completed_invocation_records": len(cli_records),
                          "planned_invocation_ids": [row["invocation_id"] for row in design["plans"]],
                          "unstarted_invocation_ids": [record["invocation_id"] for record in cli_records
                                                        if record.get("attempted") is False],
                          "captured_laya_choice_posts": sum(event["kind"] == "laya_choice" for event in proxy.events),
                          "runtime_oracle_validation_started": False, "last_capture_error": capture_error,
                          "unsettled_provider_exchange": pending_failure,
                          "health_after_validated": metadata.get("health_after_validated", False)}
        write_json(run_dir / "capture-status.json", capture_status)
        metadata.update({"status": capture_status["status"], "completed_utc": now_utc(),
                         "completed_invocations": len(cli_records),
                         "choice_post_count": capture_status["captured_laya_choice_posts"]})
        write_json(run_dir / "run-metadata.json", metadata)
    except Exception as exc:
        capture_error = str(exc)
        metadata.update({"status": "incomplete_raw_capture", "error": capture_error, "completed_utc": now_utc()})
        write_json(run_dir / "run-metadata.json", metadata)
    finally:
        stop_owned_server(server, pgid, server_stdout, server_stderr)
        if proxy is not None:
            final_drain = proxy.wait_for_pending(PENDING_SETTLE_TIMEOUT_SECONDS)
            metadata["final_pending_exchange_drain"] = {
                **final_drain, "timeout_seconds": PENDING_SETTLE_TIMEOUT_SECONDS,
                "owned_laya_server_stopped_before_drain": True,
            }
            if not final_drain["settled"]:
                pending_failure = pending_failure or {"phase": "final_after_server_stop", **final_drain}
            proxy.stop()

    recorded_ids = {record.get("invocation_id") for record in cli_records}
    for row in design["plans"]:
        if row["invocation_id"] not in recorded_ids:
            cli_records.append(preserve_failed_or_unattempted(
                run_dir, row, "not started because capture setup stopped before this invocation", attempted=False))

    events = proxy.events if proxy else []
    attach_exchange_refs(run_dir, cli_records, events)
    write_invocation_index(run_dir, design, cli_records)
    measured_ids = {row["invocation_id"] for row in design["plans"] if row["phase"] == "measured"}
    measured_posts = sum(event.get("kind") == "laya_choice" and event.get("invocation_id") in measured_ids
                         for event in events)
    total_choice_posts = sum(event.get("kind") == "laya_choice" for event in events)
    posts_by_invocation = {
        row["invocation_id"]: sum(event.get("kind") == "laya_choice"
                                  and event.get("invocation_id") == row["invocation_id"] for event in events)
        for row in design["plans"]
    }
    one_post_per_planned_invocation = (len(posts_by_invocation) == 74
                                       and all(count == 1 for count in posts_by_invocation.values()))
    complete_raw_files = all((run_dir / "invocations" / row["invocation_id"] / filename).is_file()
                             for row in design["plans"] for filename in ("stdout.raw", "stderr.raw"))
    full_raw_capture = (len(cli_records) == 74 and measured_posts == 72 and total_choice_posts == 74
                        and one_post_per_planned_invocation and complete_raw_files
                        and all((run_dir / "invocations" / row["invocation_id"] / filename).is_file()
                                for row in design["plans"]
                                for filename in ("resource.json", "invocation.json")))
    if not full_raw_capture:
        write_json(run_dir / "cli-invocation-records.json", cli_records)
        write_json(run_dir / "proxy-events.json", {"events": events,
                                                     "choice_post_count": total_choice_posts})
        write_json(run_dir / "capture-status.json", {
            "schema": "gooo/pinned-context-capture-status/v1", "status": "partial_raw_capture",
            "planned_invocations": 74, "completed_invocation_records": len(cli_records),
            "planned_invocation_ids": [row["invocation_id"] for row in design["plans"]],
            "unstarted_invocation_ids": [record["invocation_id"] for record in cli_records
                                          if record.get("attempted") is False],
            "captured_laya_choice_posts": total_choice_posts, "captured_measured_choice_posts": measured_posts,
            "captured_choice_posts_exactly_one_per_planned_invocation": one_post_per_planned_invocation,
            "runtime_oracle_validation_started": False, "last_capture_error": capture_error,
            "unsettled_provider_exchange": pending_failure,
            "health_after_validated": metadata.get("health_after_validated", False),
        })
        metadata.update({"status": "partial_raw_capture", "completed_utc": now_utc(),
                         "capture_error": capture_error,
                         "unstarted_invocation_count": len([row for row in cli_records
                                                            if row.get("attempted") is False])})
        write_json(run_dir / "run-metadata.json", metadata)
        partial_report = write_partial_capture_report(run_dir, design, cli_records, events, design_sha,
                                                      binary_receipt, metadata, capture_error)
        metadata["partial_report_sha256"] = sha256((run_dir / "report.json").read_bytes())
        write_json(run_dir / "run-metadata.json", metadata)
        print(f"Preserved partial capture at {run_dir}; runtime oracle validation was not started.", file=sys.stderr)
        raise SystemExit(2)

    # Full raw bytes are persisted and the owned Laya service has stopped before
    # any holdout oracle file is read or compiled Go runtime is executed.
    write_json(run_dir / "cli-invocation-records.json", cli_records)
    write_json(run_dir / "proxy-events.json", {"events": events, "choice_post_count": total_choice_posts})
    metadata["status"] = "all_raw_calls_captured_before_runtime_oracle_validation"
    write_json(run_dir / "run-metadata.json", metadata)
    write_json(run_dir / "capture-status.json", {
        "schema": "gooo/pinned-context-capture-status/v1",
        "status": "all_74_cli_invocations_recorded_before_oracle_validation",
        "planned_invocations": 74, "completed_invocation_records": len(cli_records),
        "planned_invocation_ids": [row["invocation_id"] for row in design["plans"]],
        "unstarted_invocation_ids": [record["invocation_id"] for record in cli_records
                                      if record.get("attempted") is False],
        "captured_laya_choice_posts": total_choice_posts, "captured_measured_choice_posts": measured_posts,
        "health_after_validated": metadata.get("health_after_validated", False),
        "runtime_oracle_validation_started": False,
        "all_raw_provider_exchange_files_saved": all((run_dir / event[key]).is_file()
                                                       for event in events for key in ("request_file", "response_file")),
    })
    reused_oracle_index = copy_reused_oracles(run_dir, args.oracle_dir.resolve())
    write_json(run_dir / "capture-status.json", {
        "schema": "gooo/pinned-context-capture-status/v1",
        "status": "all_74_raw_choices_and_reused_oracles_saved_before_runtime_validation",
        "planned_invocations": 74, "completed_invocation_records": len(cli_records),
        "planned_invocation_ids": [row["invocation_id"] for row in design["plans"]],
        "unstarted_invocation_ids": [record["invocation_id"] for record in cli_records
                                      if record.get("attempted") is False],
        "captured_laya_choice_posts": total_choice_posts, "captured_measured_choice_posts": measured_posts,
        "all_raw_provider_exchange_files_saved": all((run_dir / event[key]).is_file()
                                                       for event in events for key in ("request_file", "response_file")),
        "reused_oracle_index": reused_oracle_index, "runtime_oracle_validation_started": False,
    })
    binary_env = os.environ.copy()
    binary_env.update({"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off"})
    capture_status = read_json(run_dir / "capture-status.json")
    capture_status["runtime_oracle_validation_started"] = True
    capture_status["runtime_oracle_validation_started_utc"] = now_utc()
    write_json(run_dir / "capture-status.json", capture_status)
    try:
        rows, request_audits = validate_captured_calls(run_dir, design, cli_records, events,
                                                       run_dir / "oracles", binary_env)
        report = summarize(design, rows, events, request_audits, run_dir, design_sha, binary_receipt, metadata)
        report_bytes = write_json(run_dir / "report.json", report)
        (run_dir / "report.md").write_text(render_report(report), encoding="utf-8")
        write_invocation_index(run_dir, design, cli_records, rows)
        write_json(run_dir / "privacy-audit.json", {
            "schema": "gooo/pinned-context-privacy-audit/v1",
            "choice_requests_scanned": len(request_audits["privacy_scans"]),
            "choice_request_raw_holdout_field_or_exact_reused_case_violations": 0,
            "all_choice_requests_saved_before_reused_oracle_open": True,
            "request_results": request_audits["privacy_scans"],
            "invocation_route_and_feedback_checks": request_audits["invocation_checks"],
        })
        metadata.update({"status": report["decision"], "runtime_validation_finished_utc": now_utc(),
                         "report_sha256": sha256(report_bytes)})
        write_json(run_dir / "run-metadata.json", metadata)
        capture_status["runtime_oracle_validation_started"] = True
        capture_status["runtime_oracle_validation_completed"] = True
        write_json(run_dir / "capture-status.json", capture_status)
        print(f"Saved {report['capture']['captured_choice_posts_measured']}/72 measured choice posts; report: {run_dir / 'report.md'}")
    except Exception as exc:
        metadata.update({"status": "raw_capture_complete_runtime_validation_failed",
                         "runtime_validation_error": str(exc), "runtime_validation_finished_utc": now_utc()})
        write_json(run_dir / "run-metadata.json", metadata)
        capture_status["runtime_oracle_validation_started"] = True
        capture_status["runtime_oracle_validation_error"] = str(exc)
        write_json(run_dir / "capture-status.json", capture_status)
        raise


if __name__ == "__main__":
    main()
