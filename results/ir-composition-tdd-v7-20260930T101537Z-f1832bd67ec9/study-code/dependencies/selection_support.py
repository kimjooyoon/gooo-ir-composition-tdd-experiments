"""Provenance gate and recursive privacy scanner for the Laya selection study."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile


class PrivacyScanError(RuntimeError):
    pass


class ContextRejected(ValueError):
    pass


class ArtifactExtractionRejected(ValueError):
    pass


class JSONMap(dict):
    """Keep duplicate JSON keys so the privacy audit cannot hide later values."""
    def __init__(self, pairs):
        super().__init__()
        self.raw_pairs = pairs
        for key, value in pairs:
            self[key] = value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def verify_zip_extraction(archive_path: Path, extracted_root: Path) -> dict[str, int]:
    """Bind every extracted evidence byte to the downloaded GitHub archive bytes."""
    try:
        with ZipFile(archive_path) as archive:
            entries = {info.filename: info for info in archive.infolist() if not info.is_dir()}
            if not entries:
                raise ArtifactExtractionRejected("CI artifact archive contains no files")
            seen = set()
            for name, info in entries.items():
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ArtifactExtractionRejected(f"unsafe CI artifact archive path: {name}")
                disk_path = extracted_root.joinpath(*relative.parts)
                try:
                    actual = disk_path.read_bytes()
                    expected = archive.read(info)
                except (OSError, KeyError) as exc:
                    raise ArtifactExtractionRejected(f"missing extracted CI artifact file: {name}") from exc
                if actual != expected:
                    raise ArtifactExtractionRejected(f"extracted CI artifact differs from archive entry: {name}")
                seen.add(str(disk_path.resolve()))
    except ArtifactExtractionRejected:
        raise
    except Exception as exc:
        raise ArtifactExtractionRejected(f"cannot read downloaded CI artifact archive: {exc}") from exc
    for disk_path in extracted_root.rglob("*"):
        if disk_path.is_file() and str(disk_path.resolve()) not in seen:
            raise ArtifactExtractionRejected(f"unbound extracted file is not in the CI archive: {disk_path}")
    return {"archive_entries": len(entries), "extracted_files_bound_to_archive": len(seen)}


def verify_ci_failure_context(
    evidence: dict[str, Any], expected_binding: dict[str, str], *, max_append_codepoints: int = 900
) -> str:
    """Return a short training-only context only when every source binding matches."""
    binding = evidence.get("binding") or {}
    required = (
        "compiler_revision", "fixture_sha256", "plan_sha256", "activity", "activity_id",
        "training_suite_sha256", "candidate_id", "candidate_expression",
        "compiler_generated_digest", "generated_source_sha256",
    )
    for key in required:
        if key not in expected_binding or binding.get(key) != expected_binding[key]:
            raise ContextRejected(f"stale_or_mismatched_{key}")
    if evidence.get("result") != "EXPECTED_TRAINING_MISMATCHES_REPRODUCED":
        raise ContextRejected("ci_failure_result_not_verified")
    cases = evidence.get("training_observations")
    if not isinstance(cases, list) or not cases or any(row.get("passed") for row in cases):
        raise ContextRejected("ci_training_mismatch_observations_missing")
    if len(cases) != evidence.get("training_case_count") or len(cases) != evidence.get("training_mismatch_count"):
        raise ContextRejected("ci_training_mismatch_denominator_mismatch")
    summary = {
        "source": "verified_external_go_test_training_only",
        "compiler_revision": binding["compiler_revision"],
        "fixture_sha256": binding["fixture_sha256"],
        "plan_sha256": binding["plan_sha256"],
        "activity_id": binding["activity_id"],
        "candidate_id": binding["candidate_id"],
        "candidate_expression": binding["candidate_expression"],
        "generated_source_sha256": binding["generated_source_sha256"],
        "training_suite_sha256": binding["training_suite_sha256"],
        "training_mismatches": [
            {"input": row["input"], "expected": row["expected"], "actual": row["actual"]}
            for row in cases
        ],
        "training_mismatch_count": len(cases),
    }
    suffix = "\n\n[Bounded verified CI training failure context] " + canonical_json(summary)
    if len(suffix) > max_append_codepoints:
        raise ContextRejected("ci_context_append_exceeds_bound")
    return suffix


def holdout_case_pairs(oracle: dict[str, Any]) -> set[tuple[str, str]]:
    suite = oracle["holdout"]
    if len(suite["inputs"]) != len(suite["expected"]):
        raise ValueError("holdout input/expected arrays have different lengths")
    return {
        (canonical_json(input_value), canonical_json(expected_value))
        for input_value, expected_value in zip(suite["inputs"], suite["expected"])
    }


def scan_selection_body(raw_body: bytes, forbidden_pairs: set[tuple[str, str]]) -> dict[str, int]:
    """Recursively scan JSON, including complete/embedded JSON held inside strings."""
    try:
        root = json.loads(raw_body, object_pairs_hook=JSONMap)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PrivacyScanError(f"selection request is not valid JSON: {exc}") from exc
    decoder = json.JSONDecoder(object_pairs_hook=JSONMap)
    violations: list[str] = []
    seen_strings: set[str] = set()
    seen_containers: set[int] = set()
    keepalive: list[Any] = []
    nodes = 0

    def visit(value: Any, path: str, depth: int = 0) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 100_000 or depth > 64:
            raise PrivacyScanError("selection request exceeds recursive scan bounds")
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
            keepalive.append(value)
            pairs = getattr(value, "raw_pairs", list(value.items()))
            normalized: dict[str, list[Any]] = {}
            key_names = [str(key) for key, _child in pairs]
            if len(key_names) != len(set(key_names)):
                violations.append(f"{path}: duplicate JSON object key")
            for key, child in pairs:
                normalized.setdefault(str(key).lower(), []).append(child)
                if "holdout" in str(key).lower():
                    violations.append(f"{path}.{key}: held-out field name")
            if "input" in normalized and "expected" in normalized:
                for input_value in normalized["input"]:
                    for expected_value in normalized["expected"]:
                        if (canonical_json(input_value), canonical_json(expected_value)) in forbidden_pairs:
                            violations.append(f"{path}: exact held-out case-shaped object")
            for key, child in pairs:
                visit(child, f"{path}.{key}", depth + 1)
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
            keepalive.append(value)
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]", depth + 1)
        elif isinstance(value, str):
            if value in seen_strings:
                return
            seen_strings.add(value)
            stripped = value.strip()
            if not stripped:
                return
            try:
                decoded = json.loads(stripped, object_pairs_hook=JSONMap)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            full_structured = isinstance(decoded, (dict, list))
            if isinstance(decoded, (dict, list, str)) and decoded != value:
                visit(decoded, f"{path}<embedded-json>", depth + 1)
            offset = 0
            while not full_structured and offset < len(value):
                indexes = [index for index in (value.find("{", offset), value.find("[", offset)) if index >= 0]
                if not indexes:
                    break
                object_index = min(indexes)
                try:
                    embedded, end = decoder.raw_decode(value, object_index)
                except json.JSONDecodeError:
                    offset = object_index + 1
                    continue
                if isinstance(embedded, (dict, list)):
                    visit(embedded, f"{path}<embedded-json@{object_index}>", depth + 1)
                offset = max(end, object_index + 1)

    visit(root, "$request")
    if violations:
        raise PrivacyScanError("; ".join(dict.fromkeys(violations)))
    return {"scanned_nodes": nodes, "heldout_fields_or_pairs_found": 0}
