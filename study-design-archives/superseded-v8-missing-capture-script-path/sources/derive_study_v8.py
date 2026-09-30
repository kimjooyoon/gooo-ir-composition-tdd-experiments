#!/usr/bin/env python3
"""Derive the v8 capture runner/design from frozen v7 inputs with a strict patch allowlist."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V7_DESIGN_DIR = ROOT / "study-design-v7"
V7_RUNNER = ROOT / "scripts/run_study.py"
V8_DESIGN_DIR = ROOT / "study-design-v8"
V8_RUNNER = ROOT / "scripts/run_study_v8.py"
V7_DESIGN_SHA256 = "ecce451059062b11f6fa8c8198bcfa53318d0533dddd5e1c5d119e22066bcd0b"
V7_RUNNER_SHA256 = "47ad354947e0ff999ecce284fd12884dbd939b4096e7f934ec342b6bf9e55a3e"
V7_RUN_ID = "ir-composition-tdd-v7-20260930T101537Z-f1832bd67ec9"
V7_REPORT_SHA256 = "7261c2235baa415b200e468041d1f67b8dc3e8e2fb9085a85aa1b0311f578e68"
V7_EVENTS_SHA256 = "7edf7621cdc9c0898a063670cf412401df34cc27a016844463a48f9e48e556ba"
V7_BINDING_SHA256 = "86425d01fe655c52b15ab09f328ae9448467962a02a548272b3c0c9664784849"
V7_AUTHORIZATION_RECEIPT_SHA256 = "33f994fbb222af3a1220737abd9ba2c706ae1d957c4dd51d6f7463c7bc42e476"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def replace_once(source: bytes, old: bytes, new: bytes, label: str) -> bytes:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"patch {label!r} expected one match, found {count}")
    return source.replace(old, new, 1)


NEW_RAW_EVENT_FILES = b'''def raw_event_files(run_dir: Path, event: dict) -> tuple[bytes, bytes]:
    """Read proxy event files relative to run_dir and reject paths outside its proxy tree."""
    from pathlib import PurePosixPath

    root = run_dir.resolve()

    def read_event_file(field: str, expected_directory: str) -> bytes:
        value = event.get(field)
        if not isinstance(value, str) or not value or "\\\\" in value or "\\x00" in value:
            raise ValueError(f"unsafe proxy event path in {field}")
        relative = PurePosixPath(value)
        if (relative.is_absolute() or relative.as_posix() != value
                or len(relative.parts) != 3 or relative.parts[0] != "proxy"
                or relative.parts[1] != expected_directory
                or any(part in ("", ".", "..") for part in relative.parts)):
            raise ValueError(f"unsafe proxy event path in {field}")
        try:
            target = (root / Path(*relative.parts)).resolve(strict=True)
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"proxy event path escapes or is unavailable under run directory: {field}") from exc
        if not target.is_file():
            raise ValueError(f"proxy event path is not a file: {field}")
        return target.read_bytes()

    return (read_event_file("request_file", "requests"),
            read_event_file("response_file", "responses"))
'''


def source_inventory(directory: Path) -> dict[str, dict[str, object]]:
    return {path.relative_to(directory).as_posix():
            {"sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}
            for path in sorted(directory.rglob("*")) if path.is_file()}


def main() -> None:
    if V8_DESIGN_DIR.exists() or V8_RUNNER.exists():
        raise SystemExit("refusing to overwrite study-design-v8 or scripts/run_study_v8.py")

    v7_design_raw = (V7_DESIGN_DIR / "study-design.json").read_bytes()
    if digest(v7_design_raw) != V7_DESIGN_SHA256:
        raise RuntimeError("frozen v7 study design bytes changed")
    v7_runner_raw = V7_RUNNER.read_bytes()
    if digest(v7_runner_raw) != V7_RUNNER_SHA256:
        raise RuntimeError("frozen v7 capture runner bytes changed")

    parent_run = ROOT / "results" / V7_RUN_ID
    parent_report_raw = (parent_run / "report.json").read_bytes()
    parent_events_raw = (parent_run / "proxy/events.json").read_bytes()
    parent_binding_raw = (parent_run / "capture-authorization-binding.json").read_bytes()
    auth_raw = (ROOT / "preexecution-checkpoint/capture-authorization-receipt.json").read_bytes()
    if (digest(parent_report_raw) != V7_REPORT_SHA256
            or digest(parent_events_raw) != V7_EVENTS_SHA256
            or digest(parent_binding_raw) != V7_BINDING_SHA256
            or digest(auth_raw) != V7_AUTHORIZATION_RECEIPT_SHA256):
        raise RuntimeError("v7 partial capture or its authorization evidence changed")
    parent_report = json.loads(parent_report_raw)
    parent_events = json.loads(parent_events_raw)
    if (parent_report.get("status") != "PARTIAL_CAPTURE"
            or parent_report.get("scheduled_cells") != 64
            or parent_report.get("completed_cli_cells") != 1
            or parent_report.get("raw_provider_posts") != 1
            or sum(event.get("kind") == "laya_choice" for event in parent_events.get("events", [])) != 1):
        raise RuntimeError("v7 parent run no longer reflects its one-call partial capture")

    patches = []
    derived = v7_runner_raw
    old_design_dir = b'DESIGN_DIR = ROOT / "study-design-v7"'
    new_design_dir = b'DESIGN_DIR = ROOT / "study-design-v8"'
    derived = replace_once(derived, old_design_dir, new_design_dir, "design-directory")
    patches.append({"id": "design-directory", "matches": 1,
                    "change": "point the derived runner at study-design-v8"})

    old_archive_names = b'"scripts/run_study.py"'
    new_archive_names = b'"scripts/run_study_v8.py"'
    if derived.count(old_archive_names) != 2:
        raise RuntimeError("capture source archive allowlist expected two v7 runner labels")
    derived = derived.replace(old_archive_names, new_archive_names)
    patches.append({"id": "runtime-self-identity", "matches": 2,
                    "change": "name and hash-check the exact v8 __file__ bytes in preexecution archive"})

    old_reader_start = b"def raw_event_files(run_dir: Path, event: dict) -> tuple[bytes, bytes]:\n"
    old_reader_end = b"\n\n\ndef validate_invocation("
    start = derived.find(old_reader_start)
    end = derived.find(old_reader_end, start)
    if start < 0 or end < 0 or derived.find(old_reader_start, start + 1) >= 0:
        raise RuntimeError("could not isolate exactly one frozen v7 event-file reader")
    old_reader = derived[start:end]
    derived = derived[:start] + NEW_RAW_EVENT_FILES + derived[end:]
    patches.append({"id": "event-path-resolution", "matches": 1,
                    "parent_reader_sha256": digest(old_reader),
                    "derived_reader_sha256": digest(NEW_RAW_EVENT_FILES),
                    "change": "read proxy paths relative to run root; reject absolute, traversal, wrong-tree, and symlink-escape paths"})

    runner_sha = digest(derived)
    prep_raw = (ROOT / "scripts/prepare_study.py").read_bytes()
    v7_design = json.loads(v7_design_raw)
    design = json.loads(v7_design_raw)
    design["study_code_provenance"]["capture_script_sha256"] = runner_sha
    design["derivation"] = {
        "schema": "gooo/ir-composition-tdd-study-design-derivation/v1",
        "derived_from_design": {"path": "study-design-v7/study-design.json", "sha256": V7_DESIGN_SHA256},
        "derived_from_capture_runner": {"path": "scripts/run_study.py", "sha256": V7_RUNNER_SHA256},
        "derived_from_capture": {"run_id": V7_RUN_ID, "report_sha256": V7_REPORT_SHA256,
                                 "events_sha256": V7_EVENTS_SHA256,
                                 "authorization_binding_sha256": V7_BINDING_SHA256,
                                 "authorization_receipt_sha256": V7_AUTHORIZATION_RECEIPT_SHA256,
                                 "status": "PARTIAL_CAPTURE", "completed_cli_cells": 1,
                                 "actual_provider_posts": 1, "new_capture_attempts": 0},
        "derived_runner": {"path": "scripts/run_study_v8.py", "sha256": runner_sha},
        "derivation_script": {"path": "scripts/derive_study_v8.py",
                              "sha256": digest(Path(__file__).read_bytes())},
        "parent_preparation_script_sha256": v7_design["study_code_provenance"]["preparation_script_sha256"],
        "allowed_patches": patches,
        "unchanged_protocol_inputs": ["phase-plans.json", "source/revision-2/**",
                                      "all raw MOCK request/reply and fixture files",
                                      "128 tokenizer request templates and tokenizer cache inventory"],
        "expected_changes_outside_design_metadata": ["capture runner event-file path resolution and v8 self-identity only"],
    }
    if digest(prep_raw) != v7_design["study_code_provenance"]["preparation_script_sha256"]:
        raise RuntimeError("preparation script changed from the parent v7 source pin")

    stage_root = ROOT / f".study-design-v8-staging-{uuid.uuid4().hex}"
    stage_design = stage_root / "study-design-v8"
    stage_script = stage_root / "run_study_v8.py"
    stage_root.mkdir()
    try:
        shutil.copytree(V7_DESIGN_DIR, stage_design)
        stage_script.write_bytes(derived)
        (stage_design / "study-design.json").write_bytes(canonical_json(design))
        (stage_design / "study-design.sha256").write_text(
            f"{digest((stage_design / 'study-design.json').read_bytes())}  study-design.json\n",
            encoding="ascii")

        parent_inventory = source_inventory(V7_DESIGN_DIR)
        copied_inventory = source_inventory(stage_design)
        allowed_changed = {"study-design.json", "study-design.sha256"}
        changed = [path for path, details in parent_inventory.items()
                   if path not in allowed_changed and copied_inventory.get(path) != details]
        if changed or set(parent_inventory) - set(copied_inventory):
            raise RuntimeError(f"v7 protocol/data files changed during v8 derivation: {changed[:5]}")
        if set(copied_inventory) - set(parent_inventory) - {"derivation-manifest.json"}:
            raise RuntimeError("unexpected new files appeared in copied v8 design tree")
        if (digest((stage_design / "phase-plans.json").read_bytes())
                != v7_design["phase_plan_sha256"]
                or digest((stage_design / "mock-preflight/summary.json").read_bytes())
                != v7_design["mock_preflight_sha256"]):
            raise RuntimeError("phase plan or 129-exchange/128-tokenizer model-free preflight changed")
        derived_design_sha = digest((stage_design / "study-design.json").read_bytes())
        manifest = {
            "schema": "gooo/ir-composition-tdd-v8-derivation-manifest/v1",
            "parent_design_sha256": V7_DESIGN_SHA256,
            "parent_runner_sha256": V7_RUNNER_SHA256,
            "derived_runner_sha256": runner_sha,
            "derivation_script_sha256": digest(Path(__file__).read_bytes()),
            "derived_design_sha256": derived_design_sha,
            "parent_v7_file_inventory": parent_inventory,
            "copied_v8_unchanged_file_inventory": {
                path: details for path, details in copied_inventory.items()
                if path not in allowed_changed},
            "allowed_patches": patches,
            "preflight_reused_without_regeneration": True,
            "new_mock_or_go_runs": 0,
        }
        (stage_design / "derivation-manifest.json").write_bytes(canonical_json(manifest))
        # Validate the staged runner/design pin before publishing these local derivation outputs.
        staged_design_json = json.loads((stage_design / "study-design.json").read_bytes())
        if (staged_design_json["study_code_provenance"]["capture_script_sha256"] != runner_sha
                or staged_design_json["study_code_provenance"]["preparation_script_sha256"] != digest(prep_raw)
                or staged_design_json["phase_plan_sha256"] != v7_design["phase_plan_sha256"]
                or staged_design_json["mock_preflight_sha256"] != v7_design["mock_preflight_sha256"]):
            raise RuntimeError("derived v8 design has inconsistent code or frozen input pins")
        if V8_DESIGN_DIR.exists() or V8_RUNNER.exists():
            raise RuntimeError("v8 target appeared during derivation; refusing to replace it")
        os.replace(stage_script, V8_RUNNER)
        os.replace(stage_design, V8_DESIGN_DIR)
        stage_root.rmdir()
    except BaseException:
        # Leave the unique stage in place for audit if a derivation attempt fails.
        raise

    print(json.dumps({"status": "DERIVED_V8_NO_MOCK_OR_GO_EXECUTION",
                      "parent_design_sha256": V7_DESIGN_SHA256,
                      "derived_design_sha256": derived_design_sha,
                      "derived_runner_sha256": runner_sha,
                      "derivation_script_sha256": digest(Path(__file__).read_bytes()),
                      "path": str(V8_DESIGN_DIR), "runner": str(V8_RUNNER),
                      "allowed_patches": patches,
                      "preflight_sha256_unchanged": v7_design["mock_preflight_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
