#!/usr/bin/env python3
"""Freeze the same 32 revision-2 intents and model-free 64-cell preflight."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "study-design-v2"
SOURCE_FREEZE_SHA256 = "9ef3d4bf5c3be68ef4da9c0e7c712eefb313d6bd325446e229e48b8441e33aca"
SOURCE_REVISION_MANIFEST_SHA256 = "d29362bcf9894ac53dc34eb44685cfeaf46b99841574e9473285fac00bfd1dd1"
COMPILER_REVISION = "f3e576ad55796c0d42b2af8b86f874b49baa61d8"
COMPILER_BINARY_SHA256 = "ecbae47a877f57117e4f493ab85adc0279956c437a68e9fa374bee1b3772db1f"
GO_VERSION = "go1.27.0"
LAYA_VERSION = "0.3.21"
MODEL_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
SEED = 20260930
ARMS = (
    ("compact_multilingual_single", 1),
    ("compact_multilingual_local_feedback", 3),
)
SEARCH_SCHEMA = "gooo/body-codegen-ir-search-plan/v1"


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


def load_json(path: Path):
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
        raise RuntimeError("captured MOCK wire does not contain one typed choice question")
    question_id, question = next(iter(questions.items()))
    candidates = state["remaining_candidates"]
    request = {
        "schema": "gooo/typed-decision-request/v1",
        "state": state_wire,
        "question": {
            "id": question_id,
            "instructions": question["instructions"],
            "options": [{"id": item["id"],
                         "description": "Try this exact expression: " + item["expression"]}
                        for item in candidates],
        },
        "fallback": candidates[0]["id"],
        "provider_model": outer["model"],
    }
    return "sha256:" + sha(canonical_go_json(request))


def verify_and_copy_source(source: Path, destination: Path) -> dict:
    source = source.resolve()
    if not source.is_dir():
        raise RuntimeError(f"revision-2 source directory does not exist: {source}")
    freeze_bytes = (source / "design-freeze.json").read_bytes()
    manifest_bytes = (source / "revision-manifest.json").read_bytes()
    if sha(freeze_bytes) != SOURCE_FREEZE_SHA256:
        raise RuntimeError("revision-2 design-freeze SHA differs from B's finalized pin")
    if sha(manifest_bytes) != SOURCE_REVISION_MANIFEST_SHA256:
        raise RuntimeError("revision-2 manifest SHA differs from B's finalized pin")
    freeze = json.loads(freeze_bytes)
    if (freeze.get("schema") != "gooo/ir-composition-revision2-design-freeze/v1"
            or freeze.get("revision") != "revision-2" or freeze.get("design_count") != 32
            or freeze.get("same_intention_ids_as_original") is not True):
        raise RuntimeError("source is not the finalized same-32-intent revision-2 cohort")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"revision-2 source tree contains a symlink and cannot be byte-frozen: {path}")
    for relative, expected in freeze.get("files", {}).items():
        path = source / relative
        if not path.is_file() or path.is_symlink() or sha(path.read_bytes()) != expected:
            raise RuntimeError(f"revision-2 frozen source mismatch: {relative}")
    if destination.exists():
        raise RuntimeError(f"refusing to replace existing source freeze: {destination}")
    shutil.copytree(source, destination, symlinks=False)
    copied = []
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"copied revision-2 source contains an unexpected symlink: {path}")
        if path.is_file():
            relative = str(path.relative_to(destination))
            copied.append({"path": relative, "sha256": sha(path.read_bytes()), "bytes": path.stat().st_size})
    if sha((destination / "design-freeze.json").read_bytes()) != SOURCE_FREEZE_SHA256:
        raise RuntimeError("copied revision-2 design freeze changed during copy")
    return {
        "schema": "gooo/ir-composition-tdd-source-provenance/v1",
        "source_path_at_freeze": str(source),
        "revision": "revision-2",
        "design_freeze_sha256": SOURCE_FREEZE_SHA256,
        "revision_manifest_sha256": SOURCE_REVISION_MANIFEST_SHA256,
        "original_design_freeze_sha256": freeze.get("original_design_freeze_sha256"),
        "design_count": 32,
        "same_intention_ids_as_original": True,
        "new_intention_count": 0,
        "declared_freeze_file_count": len(freeze["files"]),
        "copied_file_count": len(copied),
        "copied_tree_files": copied,
    }


def verify_binary(binary: Path, go_bin: Path) -> dict:
    binary = binary.resolve()
    raw = binary.read_bytes()
    if sha(raw) != COMPILER_BINARY_SHA256:
        raise RuntimeError("native compiler binary SHA differs from the root-provided immutable pin")
    result = subprocess.run([str(go_bin), "version", "-m", str(binary)], capture_output=True,
                            text=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError(f"cannot inspect native binary build metadata: {result.stderr.strip()}")
    build_lines = [line.strip().removeprefix("build\t") for line in result.stdout.splitlines()]
    info = {line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in build_lines if "=" in line}
    expected = {
        "vcs.revision": COMPILER_REVISION,
        "vcs.modified": "false",
        "GOOS": "darwin",
        "GOARCH": "arm64",
        "CGO_ENABLED": "1",
        "-trimpath": "true",
    }
    for key, wanted in expected.items():
        if info.get(key) != wanted:
            raise RuntimeError(f"native binary build pin mismatch for {key}: {info.get(key)!r}")
    version = subprocess.run([str(go_bin), "version"], capture_output=True, text=True, check=False, timeout=10)
    if version.returncode or GO_VERSION not in version.stdout:
        raise RuntimeError(f"Go runner must be physical {GO_VERSION}: {version.stdout.strip()!r}")
    return {"path": str(binary), "sha256": sha(raw), "source_revision": COMPILER_REVISION,
            "vcs_modified": False, "go_version": GO_VERSION, "goos": "darwin", "goarch": "arm64",
            "cgo_enabled": True, "trimpath_expected": True,
            "build_info_raw_sha256": sha(result.stdout.encode())}


class MockProvider:
    """Loopback protocol responder. Requests are recorded as non-provider MOCK evidence."""
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.current_invocation: str | None = None
        self.forced_first_choice: dict[str, str] = {}
        self.forced_choice_sequence: dict[str, list[str]] = {}
        self.mismatched_routing_invocations: set[str] = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"status": "ok", "loaded": ["multilingual"],
                                   "revisions": {"multilingual": MODEL_REVISION}, "device": "cpu"},
                                  separators=(",", ":")).encode()
                owner.exchange(self, "GET", b"", body, 200, "mock_health")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    outer = json.loads(raw)
                    model = outer["model"]
                    state = json.loads(outer["state"]["request"])
                    if model != "multilingual":
                        raise ValueError(f"unexpected model pin {model!r}")
                    if any("holdout" in key.lower() or "evaluation" in key.lower()
                           for key in walk_keys(state)):
                        raise ValueError("holdout/evaluation field leaked into provider state")
                    candidates = state["remaining_candidates"]
                    if not candidates:
                        raise ValueError("request has no remaining finite candidates")
                    invocation = owner.current_invocation
                    forced = owner.forced_first_choice.get(invocation or "")
                    prior_count = len(state.get("prior_attempts", []))
                    sequence = owner.forced_choice_sequence.get(invocation or "", [])
                    forced_in_sequence = sequence[prior_count] if prior_count < len(sequence) else None
                    if forced_in_sequence in {item["id"] for item in candidates}:
                        selected = forced_in_sequence
                    elif not prior_count and forced in {item["id"] for item in candidates}:
                        selected = forced
                    else:
                        selected = candidates[0]["id"]
                    routing_model = "wrong-model-for-negative-preflight" if invocation in owner.mismatched_routing_invocations else model
                    response = {
                        "model": "laya-rl-agent",
                        "answers": {"body_ir_search": {"type": "choice", "choice": selected,
                                                          "probabilities": {selected: 1.0}}},
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                        "routing": {"model": routing_model, "repo": "local-mock-only",
                                    "reason": "model-free protocol preflight; no inference",
                                    "detection": {"script": "unknown", "language": "und",
                                                  "is_english": None, "language_undecided": True}},
                    }
                    status, kind = 200, "mock_choice"
                except Exception as exc:
                    response = {"error": f"MOCK preflight could not build reply: {type(exc).__name__}: {exc}"}
                    status, kind, selected, model = 400, "mock_protocol_error", None, None
                body = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                owner.exchange(self, "POST", raw, body, status, kind, model=model, selected=selected)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"

    def exchange(self, handler, method, request, response, status, kind, model=None, selected=None):
        with self.lock:
            seq = len(self.events) + 1
            invocation = self.current_invocation
            directory = self.out_dir / "exchanges" / f"{seq:04d}-{invocation or 'health'}"
            directory.mkdir(parents=True, exist_ok=False)
            write_bytes(directory / "request.raw", request)
            write_bytes(directory / "response.raw", response)
            event = {"sequence": seq, "kind": kind, "invocation_id": invocation,
                     "method": method, "path": handler.path, "provider_model": model,
                     "selected_candidate_id": selected, "request_file": str((directory / "request.raw").relative_to(self.out_dir)),
                     "response_file": str((directory / "response.raw").relative_to(self.out_dir)),
                     "request_sha256": sha(request), "response_sha256": sha(response),
                     "response_status": status, "counted_as_laya_call": False,
                     "started_unix_ns": time.time_ns()}
            self.events.append(event)
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(response)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(response)
        handler.close_connection = True

    def set_invocation(self, invocation: str | None, forced_first: str | None = None,
                       forced_sequence: list[str] | None = None, mismatched_routing: bool = False) -> None:
        with self.lock:
            self.current_invocation = invocation
            if invocation is not None and forced_first is not None:
                self.forced_first_choice[invocation] = forced_first
            if invocation is not None and forced_sequence is not None:
                self.forced_choice_sequence[invocation] = list(forced_sequence)
            if invocation is not None and mismatched_routing:
                self.mismatched_routing_invocations.add(invocation)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def compile_plans(source_root: Path, stage: Path) -> tuple[list[dict], dict[str, bytes], dict[str, bytes]]:
    catalog = load_json(source_root / "catalog.json")
    if (catalog.get("design_count") != 32 or catalog.get("same_intention_ids_as_original") is not True
            or catalog.get("original_and_revision2_count_as_64_intentions") is not False):
        raise RuntimeError("revision-2 catalog does not prove it reuses the same 32 original intent IDs")
    if len(catalog.get("designs", [])) != 32:
        raise RuntimeError("catalog does not contain exactly 32 designs")
    ids = [row["id"] for row in catalog["designs"]]
    if len(set(ids)) != 32 or any(row.get("original_intent_id") != row["id"] for row in catalog["designs"]):
        raise RuntimeError("revision-2 catalog IDs do not bind one-to-one to the original 32 intents")
    rows, plan_bytes, fixture_bytes, holdout_bytes = [], {}, {}, {}
    for design_index, item in enumerate(catalog["designs"], start=1):
        case_id = item["id"]
        source_plan = source_root / "plans/laya/compact-no-feedback" / item["plan_basename"]
        # The source layout uses plan_basename under each Laya arm.
        if not source_plan.is_file():
            candidates = list((source_root / "plans/laya/compact-no-feedback").glob(f"*{case_id}.json"))
            if len(candidates) != 1:
                raise RuntimeError(f"cannot resolve source compact plan for {case_id}")
            source_plan = candidates[0]
        plan = load_json(source_plan)
        if plan.get("schema") != SEARCH_SCHEMA or len(plan.get("candidates", [])) != 3:
            raise RuntimeError(f"{case_id}: source plan is not a three-option finite IR search plan")
        if plan.get("intent") != item.get("intent"):
            raise RuntimeError(f"{case_id}: catalog intent and compact plan differ")
        if not plan.get("test_cases") or not plan.get("holdout_test_cases"):
            raise RuntimeError(f"{case_id}: training/holdout suites must both be declared")
        training_inputs = {case["input"] for case in plan["test_cases"]}
        holdout = plan.pop("holdout_test_cases")
        if training_inputs & {case["input"] for case in holdout}:
            raise RuntimeError(f"{case_id}: training and holdout inputs overlap")
        holdout_raw = (json.dumps(holdout, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        holdout_rel = f"evaluation-holdout/{design_index:02d}-{case_id}.json"
        holdout_bytes[holdout_rel] = holdout_raw
        fixture_rel = item["fixture"]
        fixture_raw = (source_root / fixture_rel).read_bytes()
        fixture_bytes[fixture_rel] = fixture_raw
        for arm, max_attempts in ARMS:
            arm_plan = dict(plan)
            arm_plan["provider_model"] = "multilingual"
            arm_plan["prompt_profile"] = "compact"
            arm_plan["max_attempts"] = max_attempts
            arm_plan.pop("external_training_feedback", None)
            # Local search feedback is the native sequence of prior attempt outcomes.
            # No precomputed external-feedback artifact enters either arm.
            plan_rel = f"plans/{arm}/{design_index:02d}-{case_id}.json"
            raw = (json.dumps(arm_plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            plan_bytes[plan_rel] = raw
            rows.append({"intent_id": case_id, "activity": item["activity"], "design_index": design_index,
                         "arm": arm, "max_attempts": max_attempts, "provider_model": "multilingual",
                         "intended_candidate_id": item.get("gold_candidate_id"),
                         "training_case_count": len(plan["test_cases"]),
                         "holdout_case_count": len(holdout),
                         "plan_path": plan_rel, "plan_sha256": sha(raw), "fixture_path": fixture_rel,
                         "fixture_sha256": sha(fixture_raw), "holdout_path": holdout_rel,
                         "holdout_sha256": sha(holdout_raw), "first_proposal_metric": "finite_training_score"})
    if len(rows) != 64 or len({(row["intent_id"], row["arm"]) for row in rows}) != 64:
        raise RuntimeError("expected exactly 64 unique same-32-intent × two-arm plan cells")
    return rows, plan_bytes, fixture_bytes, holdout_bytes


def tokenize_preflight(laya_venv: Path, model_snapshot: Path, mock_dir: Path,
                       requests: list[dict]) -> dict:
    model_snapshot = model_snapshot.expanduser().resolve()
    if model_snapshot.name != MODEL_REVISION:
        raise RuntimeError("multilingual tokenizer cache must resolve to the pinned Laya revision")
    multilingual = model_snapshot / "multilingual"
    config_path = multilingual / "rl_agent_config.json"
    tokenizer_path = multilingual / "tokenizer"
    if not config_path.is_file() or not tokenizer_path.is_dir():
        raise RuntimeError("cached multilingual tokenizer/config files are incomplete")
    cache_files = [config_path, *sorted(path for path in tokenizer_path.rglob("*") if path.is_file())]
    tokenizer_inventory = [{"path": str(path.relative_to(model_snapshot)), "sha256": sha(path.read_bytes()),
                            "bytes": path.stat().st_size} for path in cache_files]
    input_path, output_path = mock_dir / "tokenizer-input.json", mock_dir / "tokenizer-output.json"
    write_json(input_path, requests)
    python = laya_venv.expanduser().resolve() / "bin/python"
    if not python.is_file():
        raise RuntimeError(f"cached Laya virtual environment is unavailable: {python}")
    code = r'''import json, os, sys
from pathlib import Path
os.environ.update({"HF_HUB_OFFLINE":"1", "TRANSFORMERS_OFFLINE":"1", "HF_DATASETS_OFFLINE":"1",
                  "HF_HUB_DISABLE_TELEMETRY":"1"})
import importlib.metadata as metadata
import transformers, tokenizers
from transformers import AutoTokenizer
from laya.common import build_sequence, encode_text, serialize_state
input_path, output_path, model_root = map(Path, sys.argv[1:4])
requests=json.loads(input_path.read_text(encoding="utf-8")); model_dir=model_root/"multilingual"
config=json.loads((model_dir/"rl_agent_config.json").read_text(encoding="utf-8"))
tok=AutoTokenizer.from_pretrained(str(model_dir/"tokenizer"), local_files_only=True)
max_len=int(config.get("max_len", 1024)); head_max_len=int(config.get("head_max_len", 256))
if max_len != 1024: raise RuntimeError(f"expected the pinned multilingual tokenizer limit to be 1024, got {max_len}")
out=[]
for item in requests:
 wire=item["request"]; state=wire["state"]
 if wire.get("model") != "multilingual": raise RuntimeError("request is not pinned to multilingual")
 qid,qdef=next(iter(wire["questions"].items())); crit=qdef.get("criteria")
 if qdef["type"] == "choice" and isinstance(crit,list): crit={choice:None for choice in crit}
 elif qdef["type"] == "noul" and isinstance(crit,dict): crit={str(k).lower():v for k,v in crit.items()}
 ins=qdef["instructions"]
 if not isinstance(ins,str): ins=json.dumps(ins,ensure_ascii=False)
 q={"t":qdef["type"],"ins":ins,"crit":crit}
 if "labels" in qdef: q["labels"]=qdef["labels"]
 state_ids=encode_text(tok,serialize_state(state).replace(tok.mask_token," "),add_special_tokens=False)["input_ids"]
 seq,markers,stats=build_sequence(tok,state,q,max_len=max_len,head_max_len=head_max_len,
     truncate_left=isinstance(state,list),state_ids=state_ids,return_stats=True)
 empty,_=build_sequence(tok,state,q,max_len=max_len,head_max_len=head_max_len,
     truncate_left=isinstance(state,list),state_ids=[],return_stats=False)
 state_room=max(0,max_len-(len(empty)-1)-1)
 out.append({"invocation_id":item["invocation_id"],"request_sha256":item["request_sha256"],
   "provider_model":"multilingual","tokenizer_revision":sys.argv[4],
   "token_count_exact_sequence":len(seq),"token_limit":1024,
   "state_tokens_before_model_truncation":len(state_ids),"state_token_room":state_room,
   "state_truncated":len(state_ids)>state_room,"head_option_stats":stats,
   "question_count":len(wire["questions"])})
Path(output_path).write_text(json.dumps({"laya_version":metadata.version("laya"),
 "transformers_version":transformers.__version__,"tokenizers_version":tokenizers.__version__,
 "rows":out},ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
'''
    env = os.environ.copy()
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1"})
    result = subprocess.run([str(python), "-c", code, str(input_path), str(output_path), str(model_snapshot),
                             MODEL_REVISION], cwd=mock_dir, env=env, capture_output=True, text=True,
                            check=False, timeout=300)
    if result.returncode:
        write_json(mock_dir / "tokenizer-error.json", {"returncode": result.returncode,
                                                         "stdout": result.stdout, "stderr": result.stderr})
        raise RuntimeError("cached multilingual tokenizer preflight failed; no model calls were made")
    token_output = load_json(output_path)
    if token_output.get("laya_version") != LAYA_VERSION:
        raise RuntimeError("cached tokenizer runtime Laya version differs from its pinned version")
    token_rows = token_output.get("rows", [])
    if len(token_rows) != len(requests):
        raise RuntimeError("tokenizer preflight did not account for every captured mock provider POST")
    over = [row for row in token_rows if row["token_count_exact_sequence"] > 1024 or row["state_truncated"]]
    if over:
        write_json(mock_dir / "tokenizer-overflow.json", over)
        raise RuntimeError(f"{len(over)} mocked request sequence(s) exceed or truncate the 1024-token budget")
    return {"status": "PASS_CACHED_MULTILINGUAL_TOKENIZER_NO_INFERENCE", "token_limit": 1024,
            "tokenizer_revision": MODEL_REVISION, "request_count": len(token_rows),
            "token_rows": token_rows, "input_sha256": sha(input_path.read_bytes()),
            "output_sha256": sha(output_path.read_bytes()), "model_weights_loaded": False,
            "laya_version": token_output["laya_version"],
            "transformers_version": token_output["transformers_version"],
            "tokenizers_version": token_output["tokenizers_version"],
            "tokenizer_cache_files": tokenizer_inventory,
            "tokenizer_cache_inventory_sha256": sha(canonical_go_json(tokenizer_inventory))}


def run_mock_preflight(binary: Path, go_bin: Path, stage: Path, rows: list[dict],
                       plan_bytes: dict[str, bytes], fixture_bytes: dict[str, bytes],
                       model_snapshot: Path, laya_venv: Path) -> dict:
    mock_dir = stage / "mock-preflight"
    (mock_dir / "exchanges").mkdir(parents=True)
    provider = MockProvider(mock_dir)
    raw_requests = []
    invocation_rows = []
    branch_rows = []
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("GOOO_LAYA_") or key.startswith("LAYA_"):
            env.pop(key, None)
    env.update({"GOOO_LAYA_URL": provider.url, "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1"})

    def execute(row: dict, invocation: str, forced_first: str | None = None,
                forced_sequence: list[str] | None = None, mismatched_routing: bool = False
                ) -> tuple[dict, list[dict], dict]:
        cell_dir = mock_dir / "invocations" / invocation
        cell_dir.mkdir(parents=True, exist_ok=False)
        plan_raw, fixture_raw = plan_bytes[row["plan_path"]], fixture_bytes[row["fixture_path"]]
        plan_path, fixture_path = cell_dir / "plan.search.json", cell_dir / "fixture.gooo"
        write_bytes(plan_path, plan_raw)
        write_bytes(fixture_path, fixture_raw)
        before = len(provider.events)
        provider.set_invocation(invocation, forced_first, forced_sequence, mismatched_routing)
        command = [str(binary), "body-codegen", "--json", "--fill-search", str(plan_path),
                   "--activity", row["activity"], str(fixture_path)]
        started = time.monotonic_ns()
        result = subprocess.run(command, cwd=cell_dir, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, check=False, timeout=120)
        elapsed = (time.monotonic_ns() - started) / 1_000_000
        provider.set_invocation(None)
        write_bytes(cell_dir / "stdout.raw", result.stdout)
        write_bytes(cell_dir / "stderr.raw", result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"{invocation}: model-free native CLI preflight returned {result.returncode}: "
                               + result.stderr.decode(errors="replace")[-700:])
        payload = json.loads(result.stdout)
        body = payload.get("report", {}).get("body_search", {})
        attempts = body.get("attempts", [])
        provider_attempts = [attempt for attempt in attempts
                             if (attempt.get("decision") or {}).get("mode") == "laya"]
        exchange_attempts = (provider_attempts if not mismatched_routing else
                             [attempt for attempt in attempts if attempt.get("decision")])
        events = [event for event in provider.events[before:] if event["method"] == "POST"]
        if not attempts or (not mismatched_routing and len(events) != len(provider_attempts)):
            raise RuntimeError(f"{invocation}: MOCK POST count {len(events)} differs from provider-routed attempts "
                               f"{len(provider_attempts)}; stderr={result.stderr.decode(errors='replace')[-500:]!r}")
        if mismatched_routing and len(events) != len(exchange_attempts):
            raise RuntimeError(f"{invocation}: negative routing test did not preserve a receipt for its raw POST")
        for event, attempt in zip(events, exchange_attempts):
            request_raw = (mock_dir / event["request_file"]).read_bytes()
            response_raw = (mock_dir / event["response_file"]).read_bytes()
            if event["request_sha256"] != sha(request_raw) or event["response_sha256"] != sha(response_raw):
                raise RuntimeError(f"{invocation}: raw MOCK exchange hash mismatch")
            if event.get("path") != "/v1/systemone":
                raise RuntimeError(f"{invocation}: unexpected MOCK provider path {event.get('path')!r}")
            outer, response = json.loads(request_raw), json.loads(response_raw)
            if outer.get("model") != "multilingual":
                raise RuntimeError(f"{invocation}: MOCK POST is not explicitly pinned to multilingual")
            state = json.loads(outer["state"]["request"])
            if any("holdout" in key.lower() or "evaluation" in key.lower() for key in walk_keys(state)):
                raise RuntimeError(f"{invocation}: holdout/evaluation field leaked into provider state")
            typed_sha = typed_request_sha(outer)
            decision = attempt.get("decision") or {}
            choice = response.get("answers", {}).get("body_ir_search", {}).get("choice")
            if mismatched_routing:
                if (response.get("routing", {}).get("model") == "multilingual"
                        or decision.get("mode") != "deterministic_fallback"
                        or decision.get("fallback_reason") != "PROVIDER_RESULT_INVALID"
                        or decision.get("requested_provider_model") != "multilingual"
                        or decision.get("request_sha256") != typed_sha
                        or decision.get("selected") != state["remaining_candidates"][0]["id"]
                        or choice == attempt.get("candidate_id")):
                    raise RuntimeError(f"{invocation}: wrong-model reply did not trigger declared deterministic fallback")
            elif (decision.get("requested_provider_model") != "multilingual"
                    or decision.get("request_sha256") != typed_sha
                    or response.get("routing", {}).get("model") != "multilingual"
                    or choice != attempt.get("candidate_id")):
                raise RuntimeError(f"{invocation}: raw MOCK wire/reply does not bind the typed native receipt")
            raw_requests.append({"invocation_id": invocation, "request_sha256": event["request_sha256"],
                                 "typed_request_sha256": typed_sha, "request": outer})
        record = {"invocation_id": invocation, "intent_id": row["intent_id"], "arm": row["arm"],
                  "sequence": row.get("sequence"), "exit_code": result.returncode,
                  "capture_validation_passed": not mismatched_routing,
                  "raw_provider_post_count": len(events), "body_search_attempts": attempts,
                  "native_training_passed": body.get("training_passed"),
                  "native_training_total": body.get("training_total"),
                  "provider_operations": body.get("provider_operations", 0),
                  "forced_first_candidate_id": forced_first, "planned_max_attempts": row["max_attempts"],
                  "mock_provider_posts": len(events), "native_search_attempts": len(attempts),
                  "cli_exit_code": result.returncode, "cli_elapsed_ms": elapsed,
                  "attempt_candidate_ids": [attempt.get("candidate_id") for attempt in attempts],
                  "attempt_training_passes": [attempt.get("test_cases_passed") for attempt in attempts],
                  "training_total": attempts[0].get("test_cases_total") if attempts else None,
                  "selected_candidate_id": body.get("selected_candidate_id"),
                  "stdout_sha256": sha(result.stdout), "stderr_sha256": sha(result.stderr),
                  "plan_sha256": sha(plan_raw), "fixture_sha256": sha(fixture_raw),
                  "post_sequences": [event["sequence"] for event in events]}
        return record, events, payload

    try:
        for sequence, row in enumerate(rows, start=1):
            invocation = f"{sequence:02d}-{row['intent_id']}-{row['arm']}"
            record, _, _ = execute(row, invocation)
            record["sequence"] = sequence
            invocation_rows.append(record)
        # All two possible second-choice states follow a losing first candidate.
        # The source revision-2 oracle declares exactly two distinct distractors per design.
        search_rows = [row for row in rows if row["arm"] == "compact_multilingual_local_feedback"]
        for row in search_rows:
            plan = json.loads(plan_bytes[row["plan_path"]])
            distractors = [candidate["id"] for candidate in plan["candidates"]
                           if candidate["id"] != row["intended_candidate_id"]]
            if len(distractors) != 2:
                raise RuntimeError(f"{row['intent_id']}: expected exactly two declared distractors")
            for candidate_id in distractors:
                invocation = f"branch-{row['design_index']:02d}-{row['intent_id']}-first-{candidate_id}"
                forced_sequence = None
                if row["design_index"] == 1 and candidate_id == distractors[0]:
                    forced_sequence = [candidate_id, distractors[1]]
                record, events, _ = execute(row, invocation, candidate_id, forced_sequence)
                if record["attempt_candidate_ids"][0] != candidate_id:
                    raise RuntimeError(f"{invocation}: MOCK did not force the requested losing candidate first")
                if (not record["attempt_training_passes"]
                        or record["attempt_training_passes"][0] >= record["training_total"]):
                    raise RuntimeError(f"{invocation}: declared distractor unexpectedly passed the full training suite")
                if len(events) < 2:
                    raise RuntimeError(f"{invocation}: losing first choice did not reach the second provider request")
                second_raw = (mock_dir / events[1]["request_file"]).read_bytes()
                second_outer = json.loads(second_raw)
                second_state = json.loads(second_outer["state"]["request"])
                prior = second_state.get("prior_attempts", [])
                if len(prior) != 1 or prior[0].get("candidate_id") != candidate_id:
                    raise RuntimeError(f"{invocation}: captured second request does not bind its losing first attempt")
                sole_fallback = bool(forced_sequence and len(record["attempt_candidate_ids"]) == 3
                                     and record["body_search_attempts"][2].get("selection_method") == "sole_remaining_candidate"
                                     and record["body_search_attempts"][2].get("decision") is None)
                if forced_sequence and not sole_fallback:
                    raise RuntimeError(f"{invocation}: forced final candidate was not selected by the sole-candidate deterministic path")
                branch_rows.append({**record, "design_index": row["design_index"],
                                    "second_request_sha256": events[1]["request_sha256"],
                                    "second_typed_request_sha256": typed_request_sha(second_outer),
                                    "sole_candidate_deterministic_fallback_exercised": sole_fallback})
        # Exercise response-routing rejection and deterministic fallback with a raw mock POST.
        negative_row = next(row for row in rows if row["arm"] == ARMS[0][0])
        negative_plan = json.loads(plan_bytes[negative_row["plan_path"]])
        fallback_candidate = negative_plan["candidates"][0]["id"]
        wrong_reply_candidate = next(candidate["id"] for candidate in negative_plan["candidates"]
                                     if candidate["id"] != fallback_candidate)
        negative_record, negative_events, _ = execute(
            negative_row, "negative-routing-model-mismatch", wrong_reply_candidate,
            mismatched_routing=True)
        negative_request_raw = (mock_dir / negative_events[0]["request_file"]).read_bytes()
        negative_response_raw = (mock_dir / negative_events[0]["response_file"]).read_bytes()
        negative_outer = json.loads(negative_request_raw)
        negative_case = {"raw_post_count": len(negative_events), "provider_receipt_mode":
                         (negative_record["body_search_attempts"][0].get("decision") or {}).get("mode"),
                         "provider_fallback_reason":
                         (negative_record["body_search_attempts"][0].get("decision") or {}).get("fallback_reason"),
                         "rejected_reply_choice": json.loads((mock_dir / negative_events[0]["response_file"]).read_bytes())
                         .get("answers", {}).get("body_ir_search", {}).get("choice"),
                         "native_selected_candidate": negative_record["selected_candidate_id"],
                         "raw_request_sha256": sha(negative_request_raw),
                         "typed_request_sha256": typed_request_sha(negative_outer),
                         "raw_response_sha256": sha(negative_response_raw),
                         "request_file": negative_events[0]["request_file"],
                         "response_file": negative_events[0]["response_file"],
                         "actual_laya_calls": 0}
    finally:
        provider.stop()
    write_json(mock_dir / "events.json", {"schema": "gooo/ir-composition-tdd-mock-events/v1",
                                           "counted_as_laya_calls": False, "events": provider.events})
    posts = [event for event in provider.events if event["method"] == "POST"]
    health = [event for event in provider.events if event["method"] == "GET" and event["path"] == "/health"]
    if len(invocation_rows) != 64 or len({row["invocation_id"] for row in invocation_rows}) != 64:
        raise RuntimeError("model-free preflight did not execute all 64 unique native CLI cells")
    if any(event["kind"] != "mock_choice" or event["counted_as_laya_call"]
           or event.get("path") != "/v1/systemone" for event in posts):
        raise RuntimeError("mock preflight contains a malformed or billable provider event")
    if any(event["kind"] != "mock_health" or event.get("path") != "/health" for event in health):
        raise RuntimeError("mock preflight contains an unexpected health exchange")
    posts_by_invocation: dict[str, list[dict]] = {}
    for event in posts:
        posts_by_invocation.setdefault(event["invocation_id"], []).append(event)
    template_rows = []
    for row in rows:
        events = posts_by_invocation.get(f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}", [])
        if not events:
            raise RuntimeError(f"{row['intent_id']}/{row['arm']}: missing initial request template")
        template_rows.append(("single_initial" if row["arm"] == ARMS[0][0] else "search_initial",
                              row["intent_id"], row["arm"], None, events[0]))
    for branch in branch_rows:
        events = posts_by_invocation.get(branch["invocation_id"], [])
        if len(events) < 2:
            raise RuntimeError(f"{branch['invocation_id']}: missing second request template")
        template_rows.append(("search_second", branch["intent_id"], branch["arm"],
                              branch["forced_first_candidate_id"], events[1]))
    if len(template_rows) != 128:
        raise RuntimeError(f"expected 128 context template roles (32 single + 32 search initial + 64 search second), got {len(template_rows)}")
    tokenizer_requests = []
    template_inventory = []
    for role_index, (role, intent_id, arm, previous, event) in enumerate(template_rows, start=1):
        request_raw = (mock_dir / event["request_file"]).read_bytes()
        response_raw = (mock_dir / event["response_file"]).read_bytes()
        if event["request_sha256"] != sha(request_raw) or event["response_sha256"] != sha(response_raw):
            raise RuntimeError(f"template {role_index}: raw request or reply hash mismatch")
        outer = json.loads(request_raw)
        state = json.loads(outer["state"]["request"])
        typed_sha = typed_request_sha(outer)
        template_id = f"template-{role_index:03d}-{role}-{intent_id}-{previous or 'initial'}"
        tokenizer_requests.append({"invocation_id": template_id, "request_sha256": event["request_sha256"],
                                   "request": outer})
        template_inventory.append({"template_id": template_id, "role": role, "intent_id": intent_id,
                                   "arm": arm, "after_first_candidate_id": previous,
                                   "raw_request_sha256": event["request_sha256"],
                                   "typed_request_sha256": typed_sha,
                                   "response_sha256": event["response_sha256"],
                                   "request_file": event["request_file"],
                                   "response_file": event["response_file"],
                                   "prior_attempt_count": len(state.get("prior_attempts", []))})
    token = tokenize_preflight(laya_venv, model_snapshot, mock_dir, tokenizer_requests)
    runner_path = ROOT / "scripts/run_study.py"
    runner_spec = importlib.util.spec_from_file_location("ir_composition_tdd_runner_for_preflight", runner_path)
    if runner_spec is None or runner_spec.loader is None:
        raise RuntimeError("cannot import capture runner for model-free aggregate smoke check")
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)
    smoke_summary = runner.aggregate(invocation_rows, [], rows)
    for arm, _ in ARMS:
        result = smoke_summary["arms"][arm]
        if (result["planned_cells"] != 32 or result["captured_cli_successes"] != 32
                or result["capture_validated_cells"] != 32
                or result["raw_provider_posts"] != result["provider_routed_receipt_count"]
                or result["first_proposal_matches_intended_candidate"]["observed"] != 32
                or result["local_score_adjustment"]["observed_cells"] != 32):
            raise RuntimeError(f"{arm}: model-free 64-cell aggregate smoke check lost a denominator or receipt distinction")
    if sum(row["sole_candidate_deterministic_fallback_exercised"] for row in branch_rows) != 1:
        raise RuntimeError("mock branch suite did not exercise exactly one sole-candidate deterministic selection")
    if (negative_case["raw_post_count"] != 1
            or negative_case["provider_receipt_mode"] != "deterministic_fallback"
            or negative_case["provider_fallback_reason"] != "PROVIDER_RESULT_INVALID"
            or negative_case["rejected_reply_choice"] == negative_case["native_selected_candidate"]):
        raise RuntimeError("mock wrong-routing reply failed to exercise the deterministic fallback path")
    role_counts = {role: sum(item["role"] == role for item in template_inventory)
                   for role in ("single_initial", "search_initial", "search_second")}
    return {"schema": "gooo/ir-composition-tdd-mock-preflight/v1",
            "status": "PASS_MODEL_FREE_REACHABLE_TEMPLATE_PREFLIGHT", "planned_cells": 64,
            "completed_cells": len(invocation_rows), "unique_intent_arm_cells": len({(r["intent_id"], r["arm"]) for r in invocation_rows}),
            "same_intention_ids_as_revision2": True, "new_intention_count": 0,
            "mock_choice_posts": len(posts), "mock_health_checks": len(health),
            "actual_laya_calls": 0, "provider_calls": 0,
            "branch_scenario_count": len(branch_rows),
            "reachable_request_template_role_counts": role_counts,
            "reachable_request_template_count": len(template_inventory),
            "reachable_request_template_unique_raw_sha_count": len({item["raw_request_sha256"] for item in template_inventory}),
            "template_inventory": template_inventory,
            "mock_search_attempts_by_arm": {arm: sum(row["native_search_attempts"] for row in invocation_rows if row["arm"] == arm)
                                      for arm, _ in ARMS},
            "mock_provider_posts_by_arm": {arm: sum(row["mock_provider_posts"] for row in invocation_rows if row["arm"] == arm)
                                      for arm, _ in ARMS},
            "mock_aggregate_smoke": smoke_summary,
            "negative_routing_mismatch_fallback": negative_case,
            "sole_candidate_deterministic_smoke_count": sum(
                row["sole_candidate_deterministic_fallback_exercised"] for row in branch_rows),
            "tokenizer": token, "invocations": invocation_rows, "branch_invocations": branch_rows,
            "events_sha256": sha((mock_dir / "events.json").read_bytes())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cohort", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--go-bin", type=Path, required=True)
    parser.add_argument("--capture-proxy-source", type=Path, required=True,
                        help="existing run_pinned_context_study.py; its bytes are SHA-bound")
    parser.add_argument("--laya-venv", type=Path, required=True,
                        help="existing pinned environment; used only for cached tokenizer accounting here")
    parser.add_argument("--model-snapshot", type=Path, required=True,
                        help="already-cached pinned snapshot; no files are copied")
    args = parser.parse_args()
    if DESIGN.exists() and any(DESIGN.iterdir()):
        raise SystemExit(f"refusing to overwrite existing study design: {DESIGN}")
    compiler = verify_binary(args.binary, args.go_bin)
    proxy_source = args.capture_proxy_source.resolve()
    proxy_bytes = proxy_source.read_bytes()
    proxy_pin = {"path_at_freeze": str(proxy_source), "sha256": sha(proxy_bytes),
                 "bytes": len(proxy_bytes), "reused_symbol": "CaptureProxy"}
    stage = ROOT / f".study-design-staging-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        source_root = stage / "source/revision-2"
        provenance = verify_and_copy_source(args.source_cohort, source_root)
        rows, plan_bytes, fixture_bytes, holdout_bytes = compile_plans(source_root, stage)
        rng = random.Random(SEED)
        order = list(rows)
        rng.shuffle(order)
        order = [dict(row, sequence=index) for index, row in enumerate(order, start=1)]
        for relative, raw in plan_bytes.items():
            write_bytes(stage / relative, raw)
        for relative, raw in holdout_bytes.items():
            write_bytes(stage / relative, raw)
        # Preserve exact fixture bytes by binding the copied source path, not creating a divergent fixture copy.
        plans_order = {"schema": "gooo/ir-composition-tdd-phase-plan/v1", "seed": SEED,
                       "randomization": "seeded uniform shuffle of 64 intent-arm cells; one row per intent-arm pair",
                       "planned_cells": 64, "same_intention_ids_as_revision2": True,
                       "new_intention_count": 0, "plans": order}
        phase_plan_bytes = write_json(stage / "phase-plans.json", plans_order)
        source_provenance_bytes = write_json(stage / "source-provenance.json", provenance)
        preflight = run_mock_preflight(args.binary.resolve(), args.go_bin.resolve(), stage, order,
                                       plan_bytes, fixture_bytes, args.model_snapshot, args.laya_venv)
        write_json(stage / "mock-preflight/summary.json", preflight)
        design = {
            "schema": "gooo/ir-composition-tdd-study-design/v2",
            "study_id": "ir-composition-tdd-2026-09-30",
            "preparation_revision": 2,
            "preflight_policy": "capture all reachable first and second provider request states; retain duplicate template roles separately",
            "status": "frozen_before_live_provider_calls",
            "source": {"path": "source/revision-2", "design_freeze_sha256": SOURCE_FREEZE_SHA256,
                       "revision_manifest_sha256": SOURCE_REVISION_MANIFEST_SHA256,
                       "intent_count": 32, "new_intentions": 0, "tree_inventory_path": "source-provenance.json",
                       "tree_inventory_sha256": sha(source_provenance_bytes)},
            "compiler": compiler,
            "capture_proxy_dependency": proxy_pin,
            "study_code_provenance": {
                "preparation_script_sha256": sha(Path(__file__).read_bytes()),
                "capture_script_sha256": sha((ROOT / "scripts/run_study.py").read_bytes()),
            },
            "provider": {"model": "multilingual", "model_revision": MODEL_REVISION,
                         "live_calls_during_preparation": 0,
                         "live_calls_authorized": False,
                         "measured_provider_post_cap": 96, "warmup_calls": 0,
                         "provider_post_caps_by_arm": {ARMS[0][0]: 32, ARMS[1][0]: 64},
                         "model_cache_snapshot": str(args.model_snapshot.expanduser().resolve()),
                         "retry_policy": "none; at most one provider POST per native attempt"},
            "arms": [{"id": arm, "max_attempts": attempts,
                      "prompt_profile": "compact", "provider_model": "multilingual",
                      "external_training_feedback": False,
                      "feedback_semantics": "native prior-attempt typecheck/training results only"}
                     for arm, attempts in ARMS],
            "planned_cells": 64, "phase_plan_path": "phase-plans.json",
            "phase_plan_sha256": sha(phase_plan_bytes),
            "mock_preflight_path": "mock-preflight/summary.json",
            "mock_preflight_sha256": sha((stage / "mock-preflight/summary.json").read_bytes()),
            "holdout_policy": "holdout vectors omitted from every CLI search plan and provider request; load only after capture for independent Go postselection scoring",
            "metrics": {
                "model_proposal": ["first_attempt_candidate_id", "first_attempt_finite_training_passes", "first_attempt_training_total", "first_attempt_matches_intended_candidate"],
                "local_adjustment": ["emitted_candidate_training_passes_minus_first_proposal_training_passes", "improved_cells", "unchanged_cells", "declined_cells"],
                "emitted_source": ["independent_go_training_score", "independent_go_holdout_score", "source_unit_completeness_receipt_separate"],
                "denominators": "planned 32 per arm; report observed and unresolved separately; failures remain planned failures",
            },
        }
        design_bytes = write_json(stage / "study-design.json", design)
        write_bytes(stage / "study-design.sha256", f"{sha(design_bytes)}  study-design.json\n".encode("ascii"))
        if DESIGN.exists():
            DESIGN.rmdir()
        if DESIGN.exists():
            raise RuntimeError(f"study design destination appeared during preparation: {DESIGN}")
        os.replace(stage, DESIGN)
        print(json.dumps({"status": design["status"], "design_sha256": sha(design_bytes),
                          "source_design_freeze_sha256": SOURCE_FREEZE_SHA256,
                          "planned_cells": 64, "same_intentions": 32, "new_intentions": 0,
                          "mock_choice_posts": preflight["mock_choice_posts"],
                          "mock_health_checks": preflight["mock_health_checks"],
                          "tokenizer_sequences": preflight["tokenizer"]["request_count"],
                          "live_laya_calls": 0, "path": str(DESIGN)}, indent=2))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


if __name__ == "__main__":
    main()
