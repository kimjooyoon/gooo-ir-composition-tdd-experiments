#!/usr/bin/env python3
"""Independently validate the frozen model-free TDD study artifacts.

This validator uses only Python's standard library. It does not start Gooo,
Laya, a model, a tokenizer, or a Go toolchain.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
ARMS = {
    "compact_multilingual_single": 1,
    "compact_multilingual_local_feedback": 3,
}
EXPECTED_TEMPLATE_ROLES = {
    "single_initial": 32,
    "search_initial": 32,
    "search_second": 64,
}


class InvalidStudy(RuntimeError):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except Exception as exc:
        raise InvalidStudy(f"cannot read JSON {path}: {type(exc).__name__}: {exc}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidStudy(message)


def canonical_go_json(value) -> bytes:
    """Match compact Go encoding/json output used for decision request hashes."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (raw.replace(b"&", b"\\u0026").replace(b"<", b"\\u003c")
            .replace(b">", b"\\u003e").replace("\u2028".encode(), b"\\u2028")
            .replace("\u2029".encode(), b"\\u2029"))


def typed_request_sha(outer: dict) -> str:
    try:
        state_wire = outer["state"]["request"]
        state = json.loads(state_wire)
        questions = outer["questions"]
        require(isinstance(questions, dict) and len(questions) == 1,
                "mock wire must contain one typed chooser question")
        question_id, question = next(iter(questions.items()))
        candidates = state["remaining_candidates"]
        require(bool(candidates), "mock wire contains no remaining candidates")
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
        return "sha256:" + digest(canonical_go_json(request))
    except InvalidStudy:
        raise
    except Exception as exc:
        raise InvalidStudy(f"cannot reconstruct typed request digest: {type(exc).__name__}: {exc}") from exc


def recursive_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from recursive_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from recursive_keys(child)


def contains_case_pair(value, pairs: set[tuple[object, object]]) -> bool:
    """Find exact structured input/expected rows without matching incidental literals."""
    if isinstance(value, dict):
        if "input" in value and "expected" in value:
            try:
                if (value["input"], value["expected"]) in pairs:
                    return True
            except TypeError:
                pass
        return any(contains_case_pair(child, pairs) for child in value.values())
    if isinstance(value, list):
        return any(contains_case_pair(child, pairs) for child in value)
    return False


def choose_design_dir(root: Path, requested: str | None) -> Path:
    if requested:
        candidate = (root / requested).resolve()
        require(candidate.is_relative_to(root.resolve()), "design path must remain inside the repository")
        return candidate
    versions = []
    for path in root.glob("study-design-v*"):
        match = re.fullmatch(r"study-design-v(\d+)", path.name)
        if path.is_dir() and match:
            versions.append((int(match.group(1)), path))
    require(bool(versions), "no numbered study-design freeze was found")
    return max(versions, key=lambda item: item[0])[1]


def check_sha_file(path: Path, expected: str, label: str) -> bytes:
    require(path.is_file() and not path.is_symlink(), f"{label} is missing or is a symlink: {path}")
    raw = path.read_bytes()
    require(digest(raw) == expected, f"{label} SHA-256 mismatch: {path}")
    return raw


def check_source_tree(design_dir: Path, design: dict) -> dict:
    source = design["source"]
    provenance_path = design_dir / source["tree_inventory_path"]
    provenance_raw = check_sha_file(provenance_path, source["tree_inventory_sha256"], "source tree inventory")
    provenance = json.loads(provenance_raw)
    source_root = design_dir / source["path"]
    require(digest((source_root / "design-freeze.json").read_bytes()) == source["design_freeze_sha256"],
            "copied source design-freeze digest differs from the frozen study")
    require(digest((source_root / "revision-manifest.json").read_bytes()) == source["revision_manifest_sha256"],
            "copied source revision-manifest digest differs from the frozen study")
    copied_files = provenance.get("copied_tree_files", [])
    require(len(copied_files) == provenance.get("copied_file_count"),
            "source provenance copied-file count is inconsistent")
    for item in copied_files:
        path = source_root / item["path"]
        check_sha_file(path, item["sha256"], f"copied source file {item['path']}")
        require(path.stat().st_size == item.get("bytes"), f"copied source file length mismatch: {item['path']}")
    catalog = read_json(source_root / "catalog.json")
    designs = catalog.get("designs", [])
    ids = {item.get("id") for item in designs}
    require(catalog.get("design_count") == 32 and catalog.get("same_intention_ids_as_original") is True
            and catalog.get("original_and_revision2_count_as_64_intentions") is False
            and len(designs) == 32 and len(ids) == 32
            and all(item.get("original_intent_id") == item.get("id") for item in designs),
            "copied revision-2 source is not exactly the original 32-intent cohort")
    require(len(ids) == source.get("intent_count") == 32 and source.get("new_intentions") == 0,
            "frozen source intent denominator changed")
    return {"copied_file_count": len(copied_files), "intent_count": len(ids), "new_intentions": 0}


def check_phase_plan(design_dir: Path, design: dict) -> tuple[list[dict], dict]:
    phase_path = design_dir / design["phase_plan_path"]
    raw = check_sha_file(phase_path, design["phase_plan_sha256"], "randomized phase plan")
    phases = json.loads(raw)
    rows = phases.get("plans", [])
    require(phases.get("planned_cells") == 64 and len(rows) == 64
            and phases.get("new_intention_count") == 0 and phases.get("same_intention_ids_as_revision2") is True,
            "phase plan must contain exactly 64 frozen cells")
    ids = {row.get("intent_id") for row in rows}
    pairs = {(row.get("intent_id"), row.get("arm")) for row in rows}
    require(len(ids) == 32 and len(pairs) == 64, "phase plan is not 32 intents crossed with two arms")
    require({row.get("arm") for row in rows} == set(ARMS), "phase plan arm set changed")
    require(all(row.get("max_attempts") == ARMS[row["arm"]] for row in rows),
            "phase plan max-attempt treatment differs from its declared arm")
    require([row.get("sequence") for row in rows] == list(range(1, 65)),
            "phase plan sequence is not a unique contiguous randomized order")
    source_catalog = read_json(design_dir / design["source"]["path"] / "catalog.json")
    require(ids == {item["id"] for item in source_catalog["designs"]},
            "phase plan intent IDs differ from the frozen source cohort")
    return rows, phases


def check_plans_and_holdouts(design_dir: Path, rows: list[dict]) -> dict:
    pairs: dict[str, dict[str, tuple[dict, dict, list, list]]] = {}
    plans_checked = 0
    holdouts_checked = 0
    source_root = design_dir / "source/revision-2"
    for row in rows:
        plan_raw = check_sha_file(design_dir / row["plan_path"], row["plan_sha256"],
                                  f"search plan {row['intent_id']}/{row['arm']}")
        fixture_raw = check_sha_file(source_root / row["fixture_path"], row["fixture_sha256"],
                                     f"source fixture {row['intent_id']}")
        plan = json.loads(plan_raw)
        forbidden = {key.lower() for key in recursive_keys(plan)}
        require("holdout_test_cases" not in forbidden and "holdout" not in forbidden and "evaluation" not in forbidden,
                f"holdout/evaluation data is present in CLI plan {row['intent_id']}/{row['arm']}")
        require(len(plan.get("candidates", [])) == 3 and plan.get("test_cases"),
                f"search plan is not a three-candidate finite plan: {row['intent_id']}/{row['arm']}")
        candidate_ids = {item.get("id") for item in plan["candidates"]}
        require(row.get("intended_candidate_id") in candidate_ids,
                f"intended candidate is absent from plan: {row['intent_id']}")
        holdout_raw = check_sha_file(design_dir / row["holdout_path"], row["holdout_sha256"],
                                     f"post-capture holdout {row['intent_id']}")
        holdout = json.loads(holdout_raw)
        train_inputs = {case.get("input") for case in plan["test_cases"]}
        holdout_inputs = {case.get("input") for case in holdout}
        require(bool(holdout) and not train_inputs.intersection(holdout_inputs),
                f"training/holdout inputs overlap or holdout is empty: {row['intent_id']}")
        require(len(plan["test_cases"]) == row.get("training_case_count")
                and len(holdout) == row.get("holdout_case_count"),
                f"training/holdout planned case counts changed: {row['intent_id']}")
        plans_checked += 1
        holdouts_checked += 1
        pairs.setdefault(row["intent_id"], {})[row["arm"]] = (row, plan, plan["test_cases"], holdout)
    for intent_id, by_arm in pairs.items():
        require(set(by_arm) == set(ARMS), f"intent does not have both arms: {intent_id}")
        left = by_arm["compact_multilingual_single"]
        right = by_arm["compact_multilingual_local_feedback"]
        require(left[0]["fixture_sha256"] == right[0]["fixture_sha256"]
                and left[1]["intent"] == right[1]["intent"]
                and left[1]["candidates"] == right[1]["candidates"]
                and left[2] == right[2] and left[3] == right[3],
                f"paired arm plans differ beyond max-attempt policy: {intent_id}")
    return {"plans_checked": plans_checked, "holdouts_checked": holdouts_checked,
            "paired_intents": len(pairs)}


def event_raw(mock_dir: Path, event: dict, which: str) -> bytes:
    rel = event[f"{which}_file"]
    path = (mock_dir / rel).resolve()
    require(path.is_relative_to(mock_dir.resolve()), f"mock event path escapes preflight directory: {rel}")
    require(path.is_file() and not path.is_symlink(), f"mock event raw file missing or symlinked: {rel}")
    raw = path.read_bytes()
    require(digest(raw) == event.get(f"{which}_sha256"), f"mock event {which} digest mismatch: {rel}")
    return raw


def check_mock_cli_invocation(mock_dir: Path, row: dict, label: str,
                              expected_plan: dict | None = None,
                              expected_fixture_sha256: str | None = None) -> dict:
    invocation_id = row.get("invocation_id")
    require(isinstance(invocation_id, str) and invocation_id
            and not Path(invocation_id).is_absolute() and ".." not in Path(invocation_id).parts,
            f"{label}: invocation ID is not a safe path component")
    invocation_root = mock_dir / "invocations"
    inv_dir = invocation_root / invocation_id
    require(inv_dir.resolve().is_relative_to(invocation_root.resolve())
            and inv_dir.is_dir() and not inv_dir.is_symlink(),
            f"{label}: raw MOCK CLI invocation directory is missing or escaped")
    stdout_raw = check_sha_file(inv_dir / "stdout.raw", row.get("stdout_sha256", ""),
                                f"{label} stdout")
    stderr_raw = check_sha_file(inv_dir / "stderr.raw", row.get("stderr_sha256", ""),
                                f"{label} stderr")
    plan_raw = (inv_dir / "plan.search.json").read_bytes()
    fixture_raw = (inv_dir / "fixture.gooo").read_bytes()
    require(digest(plan_raw) == row.get("plan_sha256")
            and digest(fixture_raw) == row.get("fixture_sha256"),
            f"{label}: copied CLI plan or fixture digest differs from its invocation receipt")
    if expected_fixture_sha256 is not None:
        require(row.get("fixture_sha256") == expected_fixture_sha256,
                f"{label}: fixture differs from the frozen source fixture")
    plan = json.loads(plan_raw)
    if expected_plan is not None:
        require(plan == expected_plan,
                f"{label}: copied CLI plan differs from the frozen plan bytes")
    forbidden = {key.lower() for key in recursive_keys(plan)}
    require("holdout" not in forbidden and "evaluation" not in forbidden
            and "holdout_test_cases" not in forbidden and "evaluation_cases" not in forbidden,
            f"{label}: holdout/evaluation fields appear in the copied MOCK CLI plan")
    require(row.get("exit_code") == 0 and row.get("cli_exit_code") == 0
            and row.get("capture_validation_passed") is True,
            f"{label}: model-free native CLI cell did not pass")
    try:
        payload = json.loads(stdout_raw)
    except Exception as exc:
        raise InvalidStudy(f"{label}: stdout is not the captured JSON report: {exc}") from exc
    report = payload.get("report", {})
    body = report.get("body_search", {})
    attempts = body.get("attempts", [])
    require(report.get("decision") == "PASS"
            and attempts == row.get("body_search_attempts")
            and body.get("selected_candidate_id") == row.get("selected_candidate_id")
            and body.get("training_passed") == row.get("native_training_passed")
            and body.get("training_total") == row.get("native_training_total")
            and body.get("provider_operations") == row.get("provider_operations")
            and report.get("completeness_receipt") == row.get("source_completeness_receipt"),
            f"{label}: summary row differs from its raw Gooo CLI stdout report")
    attempt_ids = [attempt.get("candidate_id") for attempt in attempts]
    attempt_passes = [attempt.get("test_cases_passed") for attempt in attempts]
    raw_provider_attempts = [attempt for attempt in attempts
                             if (attempt.get("decision") or {}).get("mode") == "laya"]
    require(row.get("attempt_candidate_ids") == attempt_ids
            and row.get("attempt_training_passes") == attempt_passes
            and row.get("native_search_attempts") == len(attempts)
            and row.get("provider_operations") == len(raw_provider_attempts)
            and row.get("raw_provider_post_count") == len(raw_provider_attempts),
            f"{label}: attempt/POST counts differ from the raw Gooo CLI report")
    return {"invocation_id": invocation_id, "stdout_sha256": digest(stdout_raw),
            "stderr_sha256": digest(stderr_raw), "plan_sha256": digest(plan_raw),
            "fixture_sha256": digest(fixture_raw), "attempt_count": len(attempts),
            "provider_posts": len(raw_provider_attempts)}


def check_mock_preflight(design_dir: Path, design: dict, phase_rows: list[dict]) -> dict:
    preflight_path = design_dir / design["mock_preflight_path"]
    preflight_raw = check_sha_file(preflight_path, design["mock_preflight_sha256"], "mock preflight summary")
    preflight = json.loads(preflight_raw)
    require(preflight.get("status") == "PASS_MODEL_FREE_REACHABLE_TEMPLATE_PREFLIGHT"
            and preflight.get("planned_cells") == 64 and preflight.get("completed_cells") == 64
            and preflight.get("unique_intent_arm_cells") == 64
            and preflight.get("new_intention_count") == 0
            and preflight.get("actual_laya_calls") == 0 and preflight.get("provider_calls") == 0,
            "model-free preflight status or 64-cell/no-inference bounds failed")
    require(preflight.get("branch_scenario_count") == 64, "preflight must cover all 64 losing-first branch states")
    require(preflight.get("reachable_request_template_count") == 128
            and preflight.get("reachable_request_template_role_counts") == EXPECTED_TEMPLATE_ROLES,
            "preflight does not preserve the 128 first/second request template roles")
    resource_smoke = preflight.get("resource_sampler_smoke", {})
    require(resource_smoke.get("status") == "PASS_MODEL_FREE_PROCESS_SAMPLER_SMOKE"
            and resource_smoke.get("actual_processes_sampled") == 0
            and resource_smoke.get("synthetic_sample_count") == 2
            and resource_smoke.get("coarse_cpu_delta_seconds") == 1.0
            and resource_smoke.get("sampled_rss_peak_kb") == 140
            and resource_smoke.get("sampled_pcpu_peak") == 17.0
            and resource_smoke.get("one_sample_cpu_delta") is None
            and resource_smoke.get("one_sample_cpu_status") == "unknown_fewer_than_two_live_ps_samples",
            "model-free process sampler smoke failed or turns short CPU windows into zero")
    resource_raw = check_sha_file(design_dir / resource_smoke.get("raw_samples_path", ""),
                                  resource_smoke.get("raw_samples_sha256", ""),
                                  "synthetic process sampler raw observations")
    require(resource_raw.count(b"ps_raw") == 2,
            "synthetic resource sampler evidence does not retain both raw process samples")
    require(preflight.get("mock_choice_posts") == 214 and preflight.get("mock_health_checks") == 213,
            "preflight raw mock POST/health inventory differs from the frozen reachable-path total")
    mock_dir = design_dir / "mock-preflight"
    intent_by_invocation = {}
    for row in preflight.get("invocations", []):
        intent_by_invocation[row.get("invocation_id")] = row.get("intent_id")
    for row in preflight.get("branch_invocations", []):
        intent_by_invocation[row.get("invocation_id")] = row.get("intent_id")
    intent_by_request_sha: dict[str, str] = {}
    for item in preflight.get("post_exchange_inventory", []):
        intent_id = intent_by_invocation.get(item.get("invocation_id"))
        if intent_id is not None:
            previous = intent_by_request_sha.setdefault(item.get("request_sha256"), intent_id)
            require(previous == intent_id,
                    "identical mock request bytes are attributed to different intents")
    holdout_pairs_by_intent: dict[str, set[tuple[object, object]]] = {}
    for row in phase_rows:
        intent_id = row["intent_id"]
        if intent_id in holdout_pairs_by_intent:
            continue
        holdout = read_json(design_dir / row["holdout_path"])
        holdout_pairs_by_intent[intent_id] = {
            (case.get("input"), case.get("expected")) for case in holdout
        }
    event_raw_bytes = check_sha_file(mock_dir / "events.json", preflight["events_sha256"], "raw mock events index")
    event_doc = json.loads(event_raw_bytes)
    events = event_doc.get("events", [])
    require(event_doc.get("counted_as_laya_calls") is False,
            "mock event index claims inference/provider calls")
    sequences = [event.get("sequence") for event in events]
    require(len(events) == len(set(sequences)), "mock event sequence IDs are not unique")
    posts = [event for event in events if event.get("method") == "POST"]
    health = [event for event in events if event.get("method") == "GET"]
    require(len(posts) == 214 and len(health) == 213 and len(events) == 427,
            "raw mock event count differs from 214 POST / 213 health GET design")
    for event in events:
        req = event_raw(mock_dir, event, "request")
        resp = event_raw(mock_dir, event, "response")
        require(event.get("counted_as_laya_call") is False, "a synthetic mock event is counted as a Laya call")
        if event.get("method") == "GET":
            health_value = json.loads(resp)
            require(event.get("path") == "/health" and event.get("kind") == "mock_health"
                    and event.get("response_status") == 200 and req == b""
                    and health_value.get("status") == "ok" and health_value.get("device") == "cpu"
                    and health_value.get("revisions", {}).get("multilingual") == design["provider"]["model_revision"],
                    f"invalid mock health exchange {event.get('sequence')}")
        else:
            require(event.get("path") == "/v1/systemone" and event.get("kind") == "mock_choice"
                    and event.get("response_status") == 200,
                    f"invalid mock choice exchange {event.get('sequence')}")
            outer = json.loads(req)
            state = json.loads(outer["state"]["request"])
            intent_id = intent_by_invocation.get(event.get("invocation_id"))
            if intent_id is None:
                intent_id = intent_by_request_sha.get(event.get("request_sha256"))
            require(intent_id in holdout_pairs_by_intent,
                    f"mock POST is not attributed to a frozen intent: {event.get('sequence')}")
            require(not contains_case_pair(state, holdout_pairs_by_intent[intent_id])
                    and not contains_case_pair(outer, holdout_pairs_by_intent[intent_id]),
                    f"a structured holdout input/expected pair appears in a provider request: {event.get('sequence')}")
    event_by_seq = {event["sequence"]: event for event in events}

    inventory = preflight.get("post_exchange_inventory", [])
    require(len(inventory) == 214, "mock preflight inventory must bind every raw choice POST")
    by_invocation: dict[str, list[dict]] = {}
    post_inventory_by_seq = {}
    for item in inventory:
        event = event_by_seq.get(item.get("event_sequence"))
        require(event is not None and event.get("method") == "POST"
                and event.get("request_sha256") == item.get("request_sha256")
                and event.get("response_sha256") == item.get("response_sha256"),
                "raw POST inventory does not bind its event index")
        req = event_raw(mock_dir, event, "request")
        resp = event_raw(mock_dir, event, "response")
        outer, reply = json.loads(req), json.loads(resp)
        state = json.loads(outer["state"]["request"])
        require(outer.get("model") == design["provider"]["model"]
                and typed_request_sha(outer) == item.get("typed_request_sha256"),
                f"raw mock POST typed digest or explicit model pin differs at event {event['sequence']}")
        keys = {key.lower() for key in recursive_keys(state)}
        require("holdout" not in keys and "evaluation" not in keys
                and not any(key in keys for key in ("test_cases", "holdout_test_cases", "evaluation_cases")),
                f"holdout/evaluation keys appear in a raw model state at event {event['sequence']}")
        choice = reply.get("answers", {}).get("body_ir_search", {}).get("choice")
        options = {candidate.get("id") for candidate in state.get("remaining_candidates", [])}
        if item.get("is_negative_routing_case") is True:
            require(reply.get("routing", {}).get("model") != design["provider"]["model"]
                    and item.get("receipt_mode") == "deterministic_fallback"
                    and item.get("receipt_selected") == state["remaining_candidates"][0]["id"]
                    and choice != item.get("receipt_selected"),
                    "wrong-route mock must preserve a rejected choice and deterministic fallback")
        else:
            require(reply.get("routing", {}).get("model") == design["provider"]["model"]
                    and item.get("receipt_mode") == "laya"
                    and choice == item.get("receipt_selected") and choice in options,
                    f"mock reply, route, option, and Gooo receipt disagree at event {event['sequence']}")
        item = {**item, "raw_request": req, "raw_response": resp, "outer": outer,
                "state": state, "reply": reply, "choice": choice}
        by_invocation.setdefault(event.get("invocation_id"), []).append(item)
        post_inventory_by_seq[event["sequence"]] = item

    invocation_rows = preflight.get("invocations", [])
    require(len(invocation_rows) == 64 and len({row.get("invocation_id") for row in invocation_rows}) == 64,
            "MOCK preflight invocation rows do not cover 64 unique cells")
    phase_by_invocation = {
        f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}": row for row in phase_rows
    }
    inv_by_id = {row["invocation_id"]: row for row in invocation_rows}
    require(set(inv_by_id) == set(phase_by_invocation), "MOCK invocation IDs differ from the frozen plan")
    plan_capture_checks = []
    for invocation_id, captured_row in inv_by_id.items():
        planned = phase_by_invocation[invocation_id]
        expected_plan = read_json(design_dir / planned["plan_path"])
        plan_capture_checks.append(check_mock_cli_invocation(
            mock_dir, captured_row, f"MOCK cell {invocation_id}", expected_plan,
            planned.get("fixture_sha256")))

    initial_choice_by_cell = {}
    arm_counts = {arm: {
        "planned_cells": 32, "captured_cli_successes": 0, "capture_validated_cells": 0,
        "raw_provider_posts": 0, "native_provider_operations": 0, "provider_routed_receipt_count": 0,
        "provider_choice_distribution_all_attempts_valid_or_invalid_cell": {},
        "first_provider_proposal_choice_distribution_validated_cells_only": {},
        "first_proposal_matches_intended_candidate": {"passed": 0, "observed": 0, "planned": 32},
        "first_proposal_training_score": {"passed_sum": 0, "observed_cells": 0, "planned_cells": 32},
        "emitted_training_score_native": {"passed_sum": 0, "observed_cells": 0, "planned_cells": 32},
        "local_score_adjustment": {"improved_cells": 0, "unchanged_cells": 0, "declined_cells": 0,
                                   "observed_cells": 0, "planned_cells": 32, "observed_delta_sum": 0},
        "source_completeness_receipts": {"observed_cells": 0, "planned_cells": 32},
    } for arm in ARMS}
    native_attempt_total = {arm: 0 for arm in ARMS}
    sole_total = {arm: 0 for arm in ARMS}

    for invocation_id, planned in phase_by_invocation.items():
        record = inv_by_id[invocation_id]
        events_for_cell = sorted(by_invocation.get(invocation_id, []), key=lambda item: item["event_sequence"])
        attempts = record.get("body_search_attempts", [])
        provider_attempts = [attempt for attempt in attempts
                             if (attempt.get("decision") or {}).get("mode") == "laya"]
        arm = planned["arm"]
        stats = arm_counts[arm]
        require(record.get("exit_code") == 0 and record.get("cli_exit_code") == 0,
                f"MOCK CLI failed in supposedly clean cell {invocation_id}")
        require(record.get("capture_validation_passed") is True,
                f"MOCK cell failed capture validation: {invocation_id}")
        require(record.get("raw_provider_post_count") == len(events_for_cell)
                and record.get("mock_provider_posts") == len(events_for_cell)
                and len(events_for_cell) == len(provider_attempts)
                and record.get("provider_operations") == len(events_for_cell),
                f"raw POST/native operation/provider receipt counts disagree for {invocation_id}")
        require(len(attempts) <= planned["max_attempts"], f"attempt cap exceeded in {invocation_id}")
        stats["captured_cli_successes"] += 1
        stats["capture_validated_cells"] += 1
        stats["raw_provider_posts"] += len(events_for_cell)
        stats["native_provider_operations"] += record["provider_operations"]
        stats["provider_routed_receipt_count"] += len(provider_attempts)
        native_attempt_total[arm] += len(attempts)
        sole = [attempt for attempt in attempts if attempt.get("selection_method") == "sole_remaining_candidate"]
        sole_total[arm] += len(sole)
        for attempt in provider_attempts:
            candidate_id = attempt.get("candidate_id")
            dist = stats["provider_choice_distribution_all_attempts_valid_or_invalid_cell"]
            dist[candidate_id] = dist.get(candidate_id, 0) + 1
        for event_item, attempt in zip(events_for_cell, provider_attempts):
            require(event_item.get("receipt_mode") == attempt.get("decision", {}).get("mode") == "laya"
                    and event_item.get("choice") == attempt.get("candidate_id"),
                    f"raw mock choice does not align to native attempt receipt in {invocation_id}")
        require(isinstance(record.get("source_completeness_receipt"), dict),
                f"source-completeness receipt missing in mock cell {invocation_id}")
        stats["source_completeness_receipts"]["observed_cells"] += 1
        if provider_attempts:
            first = provider_attempts[0]
            first_choice = first.get("candidate_id")
            require(events_for_cell and events_for_cell[0]["choice"] == first_choice,
                    f"raw first-choice reply differs from Gooo receipt in {invocation_id}")
            initial_choice_by_cell[(planned["intent_id"], arm)] = events_for_cell[0]
            choices = stats["first_provider_proposal_choice_distribution_validated_cells_only"]
            choices[first_choice] = choices.get(first_choice, 0) + 1
            proposal = stats["first_proposal_matches_intended_candidate"]
            proposal["observed"] += 1
            proposal["passed"] += int(first_choice == planned["intended_candidate_id"])
            if isinstance(first.get("test_cases_passed"), int):
                score = stats["first_proposal_training_score"]
                score["passed_sum"] += first["test_cases_passed"]
                score["observed_cells"] += 1
            emitted = record.get("native_training_passed")
            first_passed = first.get("test_cases_passed")
            if isinstance(first_passed, int) and isinstance(emitted, int):
                score = stats["emitted_training_score_native"]
                score["passed_sum"] += emitted
                score["observed_cells"] += 1
                adj = stats["local_score_adjustment"]
                adj["observed_cells"] += 1
                delta = emitted - first_passed
                adj["observed_delta_sum"] += delta
                if delta > 0:
                    adj["improved_cells"] += 1
                elif delta < 0:
                    adj["declined_cells"] += 1
                else:
                    adj["unchanged_cells"] += 1

    summary_arms = preflight.get("mock_aggregate_smoke", {}).get("arms", {})
    require(set(summary_arms) == set(ARMS), "mock aggregate smoke must include both arms")
    for arm, expected in arm_counts.items():
        reported = summary_arms[arm]
        for field in ("planned_cells", "captured_cli_successes", "capture_validated_cells",
                      "raw_provider_posts", "native_provider_operations", "provider_routed_receipt_count"):
            require(reported.get(field) == expected[field], f"mock aggregate {arm}.{field} is not raw-derived")
        for field in ("provider_choice_distribution_all_attempts_valid_or_invalid_cell",
                      "first_provider_proposal_choice_distribution_validated_cells_only"):
            require(reported.get(field) == expected[field], f"mock aggregate {arm}.{field} differs from raw choices")
        for field in ("first_proposal_matches_intended_candidate", "first_proposal_training_score",
                      "emitted_training_score_native", "local_score_adjustment", "source_completeness_receipts"):
            for key, value in expected[field].items():
                require(reported.get(field, {}).get(key) == value,
                        f"mock aggregate {arm}.{field}.{key} differs from raw per-cell evidence: "
                        f"reported={reported.get(field, {}).get(key)!r}, recomputed={value!r}")
        require(reported.get("native_attempt_count_including_sole_remaining") == native_attempt_total[arm]
                and reported.get("sole_remaining_candidate_deterministic_selections") == sole_total[arm],
                f"mock aggregate {arm} native/sole-candidate attempt counts differ from receipts")

    # Paired initial prompts should be identical; the treatment starts after the first choice.
    for intent_id in {row["intent_id"] for row in phase_rows}:
        a = initial_choice_by_cell[(intent_id, "compact_multilingual_single")]
        b = initial_choice_by_cell[(intent_id, "compact_multilingual_local_feedback")]
        require(a["outer"]["state"] == b["outer"]["state"]
                and a["outer"]["questions"] == b["outer"]["questions"],
                f"paired first-request contexts differ across arms: {intent_id}")

    branches = preflight.get("branch_invocations", [])
    require(len(branches) == 64, "preflight branch inventory must preserve 64 losing-first scenarios")
    sole_branches = []
    source_plan_by_pair = {}
    for planned in phase_rows:
        source_plan_by_pair[(planned["intent_id"], planned["arm"])] = read_json(
            design_dir / planned["plan_path"])
    for branch in branches:
        branch_plan_capture = check_mock_cli_invocation(
            mock_dir, branch, f"MOCK losing-first branch {branch.get('invocation_id')}",
            expected_fixture_sha256=next(row["fixture_sha256"] for row in phase_rows
                                          if row["intent_id"] == branch.get("intent_id")))
        branch_plan_path = mock_dir / "invocations" / branch["invocation_id"] / "plan.search.json"
        branch_plan = json.loads(branch_plan_path.read_bytes())
        source_plan = source_plan_by_pair[(branch["intent_id"], branch["arm"])]
        source_candidates = {item["id"]: item["expression"] for item in source_plan["candidates"]}
        branch_candidates = {item["id"]: item["expression"] for item in branch_plan["candidates"]}
        require(branch_candidates == source_candidates
                and len(branch_plan.get("candidates", [])) == len(source_plan.get("candidates", [])) == 3
                and branch_plan.get("intent") == source_plan.get("intent")
                and branch_plan.get("test_cases") == source_plan.get("test_cases")
                and branch_plan.get("max_attempts") == source_plan.get("max_attempts")
                and branch_plan.get("provider_model") == source_plan.get("provider_model")
                and branch_plan.get("prompt_profile") == source_plan.get("prompt_profile")
                and branch_plan.get("hole_id") == source_plan.get("hole_id")
                and branch_plan.get("schema") == source_plan.get("schema"),
                f"branch plan changes the source intent, candidates, suite, or attempt cap: {branch.get('invocation_id')}")
        plan_capture_checks.append(branch_plan_capture)
        attempts = branch.get("body_search_attempts", [])
        events_for_branch = sorted(by_invocation.get(branch.get("invocation_id"), []),
                                   key=lambda item: item["event_sequence"])
        require(branch.get("attempt_candidate_ids", [None])[0] == branch.get("forced_first_candidate_id"),
                f"branch did not force its declared first distractor: {branch.get('invocation_id')}")
        require(branch.get("forced_first_candidate_id") != next(
                    row["intended_candidate_id"] for row in phase_rows
                    if row["intent_id"] == branch.get("intent_id") and row["arm"] == branch.get("arm")),
                f"branch forced the intended candidate instead of a declared distractor: {branch.get('invocation_id')}")
        require(len(attempts) >= 2 and len(events_for_branch) >= 2
                and attempts[0].get("candidate_id") == events_for_branch[0]["choice"]
                and attempts[1].get("candidate_id") == events_for_branch[1]["choice"],
                f"branch raw first/second choices do not match native attempts: {branch.get('invocation_id')}")
        second_state = events_for_branch[1]["state"]
        require(len(second_state.get("prior_attempts", [])) == 1
                and second_state["prior_attempts"][0].get("candidate_id") == branch["forced_first_candidate_id"],
                f"second request does not expose the losing first attempt: {branch.get('invocation_id')}")
        if branch.get("sole_candidate_deterministic_fallback_exercised") is True:
            sole_branches.append(branch)
            require(len(attempts) == 3 and len(events_for_branch) == 2
                    and attempts[2].get("selection_method") == "sole_remaining_candidate"
                    and attempts[2].get("decision") is None,
                    "third attempt must prove sole-candidate deterministic selection without another POST")
    require(len(sole_branches) == 1 and preflight.get("sole_candidate_deterministic_smoke_count") == 1,
            "preflight must prove exactly one local deterministic third attempt")

    # The invalid-route probe is an actual captured native CLI report too. Bind
    # its deterministic fallback receipt to the separately retained bad-route
    # HTTP reply and to the same plan/fixture as its original normal request.
    negative = preflight.get("negative_routing_mismatch_fallback", {})
    negative_dir = mock_dir / "invocations/negative-routing-model-mismatch"
    require((negative_dir / "stdout.raw").is_file() and not (negative_dir / "stdout.raw").is_symlink()
            and (negative_dir / "stderr.raw").is_file() and not (negative_dir / "stderr.raw").is_symlink(),
            "wrong-route native CLI raw stdout/stderr are missing")
    negative_stdout = (negative_dir / "stdout.raw").read_bytes()
    negative_stderr = (negative_dir / "stderr.raw").read_bytes()
    negative_payload = json.loads(negative_stdout)
    negative_report = negative_payload.get("report", {})
    negative_body = negative_report.get("body_search", {})
    negative_attempts = negative_body.get("attempts", [])
    negative_item = next((item for item in inventory if item.get("is_negative_routing_case") is True), None)
    require(len(negative_attempts) == 1 and negative_report.get("decision") == "PASS"
            and negative_body.get("provider_operations") == 1
            and negative_body.get("selected_candidate_id") == negative.get("native_selected_candidate")
            and negative_attempts[0].get("decision", {}).get("mode") == "deterministic_fallback"
            and negative_attempts[0].get("decision", {}).get("fallback_reason") == "PROVIDER_RESULT_INVALID"
            and negative_attempts[0].get("decision", {}).get("request_sha256") == negative.get("typed_request_sha256")
            and negative_attempts[0].get("decision") == negative.get("fallback_receipt")
            and negative_item is not None
            and negative_item.get("request_sha256") == negative.get("raw_request_sha256")
            and negative_item.get("response_sha256") == negative.get("raw_response_sha256")
            and negative_item.get("typed_request_sha256") == negative.get("typed_request_sha256")
            and negative_attempts[0].get("candidate_id") == negative.get("native_selected_candidate"),
            "wrong-route native stdout, deterministic fallback receipt, and raw reply do not agree")
    negative_plan = json.loads((negative_dir / "plan.search.json").read_bytes())
    request_source_intent = intent_by_request_sha.get(negative.get("raw_request_sha256"))
    negative_plan_row = next((row for row in phase_rows
                              if row.get("intent_id") == request_source_intent
                              and row.get("arm") == "compact_multilingual_single"), None)
    require(request_source_intent is not None and negative_plan_row is not None
            and negative_plan == source_plan_by_pair[(request_source_intent, negative_plan_row["arm"])]
            and digest((negative_dir / "plan.search.json").read_bytes()) == negative_plan_row["plan_sha256"]
            and digest((negative_dir / "fixture.gooo").read_bytes()) == negative_plan_row["fixture_sha256"],
            "wrong-route CLI probe plan or fixture is not the source-bound plan used by its raw request")

    # Reachable role templates intentionally preserve duplicate roles across paired arms.
    templates = preflight.get("template_inventory", [])
    require(len(templates) == 128, "template inventory row count differs from 128 role contexts")
    role_counts = {role: sum(item.get("role") == role for item in templates) for role in EXPECTED_TEMPLATE_ROLES}
    require(role_counts == EXPECTED_TEMPLATE_ROLES
            and preflight.get("reachable_request_template_unique_raw_sha_count")
            == len({item.get("raw_request_sha256") for item in templates}),
            "template role or distinct raw request digest counts differ")
    token_path = mock_dir / "tokenizer-output.json"
    tokenizer = preflight["tokenizer"]
    token_input_path = mock_dir / "tokenizer-input.json"
    token_input_raw = check_sha_file(token_input_path, tokenizer.get("input_sha256", ""),
                                     "tokenizer preflight exact request input")
    token_input = json.loads(token_input_raw)
    token_raw = check_sha_file(token_path, tokenizer["output_sha256"], "tokenizer preflight output")
    token_out = json.loads(token_raw)
    token_rows = token_out.get("rows", [])
    require(tokenizer.get("status") == "PASS_CACHED_MULTILINGUAL_TOKENIZER_NO_INFERENCE"
            and tokenizer.get("request_count") == 128 and len(token_rows) == 128
            and len(token_input) == 128
            and tokenizer.get("model_weights_loaded") is False
            and token_out.get("laya_version") == "0.3.21",
            "cached tokenizer preflight is incomplete or implies model inference")
    tokenizer_revision = design["provider"].get("model_revision")
    require(tokenizer.get("tokenizer_revision") == tokenizer_revision,
            "cached tokenizer revision differs from the pinned model revision")
    cache_files = tokenizer.get("tokenizer_cache_files", [])
    require(bool(cache_files) and all(isinstance(item.get("path"), str)
                                      and isinstance(item.get("sha256"), str)
                                      and len(item["sha256"]) == 64
                                      and isinstance(item.get("bytes"), int)
                                      and item["bytes"] > 0 for item in cache_files),
            "cached tokenizer file inventory is absent or malformed")
    inventory_raw = json.dumps(cache_files, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
    require(digest(inventory_raw) == tokenizer.get("tokenizer_cache_inventory_sha256"),
            "cached tokenizer inventory digest is not reproducible from its file pins")
    snapshot = Path(design["provider"].get("model_cache_snapshot", "")).expanduser()
    local_cache_verified = snapshot.is_dir()
    if local_cache_verified:
        snapshot = snapshot.resolve()
        require(snapshot.name == tokenizer_revision,
                "locally available model snapshot directory does not match pinned tokenizer revision")
        # Hugging Face may deduplicate tokenizer blobs into the shared hub/blobs
        # directory (one level above models--*/). Permit that documented cache
        # layout while keeping paths relative to this exact pinned snapshot.
        cache_root = snapshot.parents[2]
        for item in cache_files:
            relative_cache_path = Path(item["path"])
            require(not relative_cache_path.is_absolute() and ".." not in relative_cache_path.parts,
                    f"pinned tokenizer cache path is not a safe relative path: {item['path']}")
            cache_file = (snapshot / relative_cache_path).resolve()
            require(cache_file.is_relative_to(cache_root) and cache_file.is_file(),
                    f"pinned tokenizer cache path is missing or escapes the snapshot: {item['path']}")
            require(digest(cache_file.read_bytes()) == item["sha256"]
                    and cache_file.stat().st_size == item["bytes"],
                    f"local cached tokenizer bytes differ from the frozen inventory: {item['path']}")
    token_by_id = {row.get("invocation_id"): row for row in token_rows}
    require(len(token_by_id) == 128, "tokenizer output contains duplicate/missing template IDs")
    token_input_by_id = {row.get("invocation_id"): row for row in token_input}
    require(len(token_input_by_id) == 128,
            "tokenizer input contains duplicate/missing request templates")
    for template in templates:
        token = token_by_id.get(template.get("template_id"))
        token_input_row = token_input_by_id.get(template.get("template_id"))
        require(token is not None and token.get("request_sha256") == template.get("raw_request_sha256")
                and token.get("token_count_exact_sequence", 1025) <= 1024
                and token.get("state_truncated") is False
                and token.get("tokenizer_revision") == tokenizer_revision
                and token.get("provider_model") == design["provider"]["model"],
                f"reachable request was truncated/over budget or not token-counted: {template.get('template_id')}")
        exchange = post_inventory_by_seq.get(template.get("event_sequence"))
        if exchange is None:
            matches = [item for item in inventory if item.get("request_file") == template.get("request_file")]
            require(len(matches) == 1, f"template raw request is not uniquely mapped: {template.get('template_id')}")
            exchange = matches[0]
        require(exchange.get("request_sha256") == template.get("raw_request_sha256")
                and exchange.get("response_sha256") == template.get("response_sha256"),
                f"template does not bind to exact mock exchange: {template.get('template_id')}")
        event_for_template = post_inventory_by_seq.get(exchange.get("event_sequence"))
        if event_for_template is None:
            event_for_template = next(item for item in inventory
                                      if item.get("request_file") == template.get("request_file"))
        require(token_input_row is not None
                and token_input_row.get("request_sha256") == template.get("raw_request_sha256")
                and token_input_row.get("request") == event_for_template.get("outer"),
                f"tokenizer input does not bind the exact raw reachable request: {template.get('template_id')}")

    negative = preflight.get("negative_routing_mismatch_fallback", {})
    require(negative.get("raw_post_count") == 1 and negative.get("actual_laya_calls") == 0
            and negative.get("provider_receipt_mode") == "deterministic_fallback"
            and negative.get("provider_fallback_reason") == "PROVIDER_RESULT_INVALID"
            and negative.get("rejected_reply_choice") != negative.get("native_selected_candidate"),
            "wrong-route MOCK case did not exercise preserved deterministic fallback")
    negative_item = next((item for item in inventory if item.get("is_negative_routing_case") is True), None)
    require(negative_item is not None and negative_item.get("request_sha256") == negative.get("raw_request_sha256")
            and negative_item.get("response_sha256") == negative.get("raw_response_sha256")
            and negative_item.get("typed_request_sha256") == negative.get("typed_request_sha256"),
            "wrong-route summary does not bind the exact raw and typed request")

    # Test invalid-route aggregate handling without changing the clean 64-cell design.
    invalid_smoke = preflight.get("mock_invalid_cell_denominator_smoke")
    require(isinstance(invalid_smoke, dict), "preflight lacks the invalid-route denominator smoke")
    target_id = invalid_smoke.get("replaced_intent_id")
    clean_matches = 0
    target_clean_match = None
    for planned in phase_rows:
        if planned["arm"] != "compact_multilingual_single":
            continue
        inv_id = f"{planned['sequence']:02d}-{planned['intent_id']}-{planned['arm']}"
        raw_first = sorted(by_invocation.get(inv_id, []), key=lambda item: item["event_sequence"])[0]
        match = raw_first["choice"] == planned["intended_candidate_id"]
        clean_matches += int(match)
        if planned["intent_id"] == target_id:
            target_clean_match = match
            target_choice = raw_first["choice"]
    require(target_clean_match is not None
            and invalid_smoke.get("baseline_first_proposal_candidate") == target_choice
            and invalid_smoke.get("baseline_first_proposal_was_intended") is target_clean_match
            and invalid_smoke.get("clean_raw_proposal_matches") == clean_matches
            and invalid_smoke.get("expected_proposal_matches_after_exclusion") == clean_matches - int(target_clean_match),
            "invalid-route smoke match counts are not recomputed from raw replies and frozen gold candidates")
    # The invalid-cell smoke reports only the affected arm's aggregate because
    # the other arm is unchanged and already covered by the clean aggregate.
    single = invalid_smoke.get("summary", {})
    proposal = single.get("first_proposal_matches_intended_candidate", {})
    adjustment = single.get("local_score_adjustment", {})
    require(single.get("planned_cells") == 32 and single.get("captured_cli_successes") == 32
            and single.get("capture_validated_cells") == 31 and single.get("raw_provider_posts") == 32
            and single.get("provider_routed_receipt_count") == 31
            and proposal.get("passed") == clean_matches - int(target_clean_match)
            and proposal.get("observed") == 31 and proposal.get("planned") == 32
            and adjustment.get("observed_cells") == 31 and adjustment.get("planned_cells") == 32,
            "invalid-route denominator smoke dropped failures or counted fallback as a model proposal")
    require(single.get("raw_posts_minus_provider_routed_receipts") == 1
            or single.get("deterministic_fallback_receipt_count") == 1
            or single.get("provider_route_invalid_count") == 1,
            "invalid-route smoke does not separately expose the fallback/raw-receipt gap")

    return {
        "planned_cells": 64,
        "intents": 32,
        "mock_posts": len(posts),
        "mock_health_gets": len(health),
        "raw_mock_cli_invocations_verified": len(plan_capture_checks) + 1,
        "reachable_role_contexts": len(templates),
        "unique_template_request_digests": len({item.get("raw_request_sha256") for item in templates}),
        "actual_laya_calls": 0,
        "tokenizer_input_rows": len(token_input),
        "tokenizer_cache_file_count": len(cache_files),
        "tokenizer_cache_bytes_verified_locally": local_cache_verified,
        "raw_derived_first_proposal_matches": {
            arm: arm_counts[arm]["first_proposal_matches_intended_candidate"]["passed"]
            for arm in ARMS
        },
        "invalid_route_smoke": {
            "intent_id": target_id,
            "clean_raw_matches": clean_matches,
            "fallback_excluded_match_count": clean_matches - int(target_clean_match),
            "planned": 32,
            "proposal_observed": proposal.get("observed"),
            "provider_receipts": single.get("provider_routed_receipt_count"),
            "raw_posts": single.get("raw_provider_posts"),
        },
    }


def check_archive_history(root: Path) -> dict:
    # This historical gap is intentionally preserved. Never interpret the later
    # archive-time snapshots as reconstructed original v2 source bytes.
    v2 = root / "study-design-archives/superseded-v2-initial/archive-manifest.json"
    if not v2.is_file():
        return {"v2_original_script_bytes": "unavailable_manifest_missing"}
    raw = v2.read_bytes()
    sidecar = Path(str(v2) + ".sha256")
    require(sidecar.is_file() and sidecar.read_text(encoding="ascii").split()[0] == digest(raw),
            "historical v2 archive manifest sidecar digest mismatch")
    manifest = json.loads(raw)
    note = manifest.get("retention_note", "").lower()
    require("unavailable" in note and "different sha" in note,
            "v2 original script retention gap is not explicitly disclosed")
    files = {item["path"]: item for item in manifest.get("archived_files", [])}
    old_capture = files.get("scripts-at-archive-time/run_study.py", {})
    old_prepare = files.get("scripts-at-archive-time/prepare_study.py", {})
    require(old_capture and old_prepare
            and old_capture.get("sha256") != manifest.get("pinned_capture_script_sha256")
            and old_prepare.get("sha256") != manifest.get("pinned_preparation_script_sha256"),
            "v2 archive history falsely equates archive-time script copies with the missing pinned bytes")
    return {"v2_original_script_bytes": "unavailable_and_not_reconstructed",
            "v2_pinned_capture_sha256": manifest["pinned_capture_script_sha256"],
            "v2_archive_time_capture_sha256": old_capture["sha256"],
            "v2_pinned_prepare_sha256": manifest["pinned_preparation_script_sha256"],
            "v2_archive_time_prepare_sha256": old_prepare["sha256"]}


def capture_script_relative_path(design: dict) -> str:
    provenance = design.get("study_code_provenance", {})
    explicit = provenance.get("capture_script_path")
    derivation = design.get("derivation")
    if derivation is not None:
        derived_path = derivation.get("derived_runner", {}).get("path")
        require(isinstance(explicit, str) and explicit == derived_path,
                "derived study design must explicitly bind capture_script_path to its derived runner")
    elif explicit is None:
        explicit = "scripts/run_study.py"
    rel = Path(explicit)
    require(not rel.is_absolute() and ".." not in rel.parts and rel.parts[:1] == ("scripts",)
            and rel.suffix == ".py", "capture_script_path is not a safe scripts-relative Python path")
    return rel.as_posix()


def check_v8_derivation(root: Path, design_dir: Path, design: dict, design_sha256: str) -> dict | None:
    derivation = design.get("derivation")
    if derivation is None:
        return None
    require(derivation.get("schema") == "gooo/ir-composition-tdd-study-design-derivation/v1",
            "derived design has an unknown derivation schema")
    manifest = read_json(design_dir / "derivation-manifest.json")
    require(manifest.get("schema") == "gooo/ir-composition-tdd-v8-derivation-manifest/v1"
            and manifest.get("preflight_reused_without_regeneration") is True
            and manifest.get("new_mock_or_go_runs") == 0
            and manifest.get("derived_design_sha256") == design_sha256,
            "v8 derivation manifest is not bound to its frozen design or claims regenerated preflight")
    parent_dir = root / "study-design-v7"
    parent_design_raw = (parent_dir / "study-design.json").read_bytes()
    parent_sha = digest(parent_design_raw)
    parent_runner_raw = (root / "scripts/run_study.py").read_bytes()
    parent_runner_sha = digest(parent_runner_raw)
    derived_path = capture_script_relative_path(design)
    derived_runner_raw = check_sha_file(root / derived_path,
                                        design.get("study_code_provenance", {}).get("capture_script_sha256", ""),
                                        "derived capture runner")
    derivation_script = derivation.get("derivation_script", {})
    derivation_script_raw = check_sha_file(root / derivation_script.get("path", ""),
                                           derivation_script.get("sha256", ""),
                                           "v8 derivation script")
    parent_design = json.loads(parent_design_raw)
    require(derivation.get("derived_from_design") == {
                "path": "study-design-v7/study-design.json", "sha256": parent_sha}
            and derivation.get("derived_from_capture_runner") == {
                "path": "scripts/run_study.py", "sha256": parent_runner_sha}
            and derivation.get("derived_runner") == {"path": derived_path,
                                                       "sha256": digest(derived_runner_raw)}
            and derivation.get("parent_preparation_script_sha256")
            == parent_design["study_code_provenance"]["preparation_script_sha256"]
            and manifest.get("parent_design_sha256") == parent_sha
            and manifest.get("parent_runner_sha256") == parent_runner_sha
            and manifest.get("derived_runner_sha256") == digest(derived_runner_raw)
            and manifest.get("derivation_script_sha256") == digest(derivation_script_raw),
            "v8 derivation provenance does not bind exact parent, runner, or derivation script bytes")

    patch_rows = derivation.get("allowed_patches", [])
    require(patch_rows == manifest.get("allowed_patches")
            and [row.get("id") for row in patch_rows]
            == ["design-directory", "runtime-self-identity", "event-path-resolution"],
            "v8 design and manifest do not agree on the narrow allowed runner patch list")
    parent_inventory = manifest.get("parent_v7_file_inventory", {})
    copied_inventory = manifest.get("copied_v8_unchanged_file_inventory", {})
    require(bool(parent_inventory) and "study-design.json" in parent_inventory
            and "study-design.sha256" in parent_inventory
            and "study-design.json" not in copied_inventory
            and "study-design.sha256" not in copied_inventory
            and set(copied_inventory) == set(parent_inventory) - {"study-design.json", "study-design.sha256"},
            "v8 unchanged-file inventory does not cover the full parent design tree")
    for relative, item in parent_inventory.items():
        source = check_sha_file(parent_dir / relative, item.get("sha256", ""),
                                f"v7 parent file {relative}")
        require(len(source) == item.get("bytes"), f"v7 parent byte count changed: {relative}")
        if relative in {"study-design.json", "study-design.sha256"}:
            continue
        copied = copied_inventory[relative]
        target = check_sha_file(design_dir / relative, copied.get("sha256", ""),
                                f"v8 inherited file {relative}")
        require(len(target) == copied.get("bytes") == item.get("bytes")
                and copied.get("sha256") == item.get("sha256"),
                f"v8 protocol/data bytes differ from v7: {relative}")
    actual_files = {path.relative_to(design_dir).as_posix() for path in design_dir.rglob("*")
                    if path.is_file() and path.name != "derivation-manifest.json"}
    require(actual_files == set(parent_inventory),
            "v8 design tree has missing or extra non-manifest files compared with v7")

    # Recreate the derived runner from v7 bytes and the exact allowlisted patches.
    derived = parent_runner_raw
    old_anchor, new_anchor = (b'DESIGN_DIR = ROOT / "study-design-v7"',
                              b'DESIGN_DIR = ROOT / "study-design-v8"')
    require(derived.count(old_anchor) == 1, "v7 design-directory patch anchor changed")
    derived = derived.replace(old_anchor, new_anchor, 1)
    old_name, new_name = b'"scripts/run_study.py"', b'"scripts/run_study_v8.py"'
    require(derived.count(old_name) == 2, "v7 runner archive-name patch anchors changed")
    derived = derived.replace(old_name, new_name)
    reader_start = b"def raw_event_files(run_dir: Path, event: dict) -> tuple[bytes, bytes]:\n"
    reader_end = b"\n\n\ndef validate_invocation("
    start = derived.find(reader_start)
    end = derived.find(reader_end, start)
    require(start >= 0 and end > start and derived.find(reader_start, start + 1) < 0,
            "cannot isolate v7 proxy-event path reader")
    derivation_tree = ast.parse(derivation_script_raw)
    assignment = next((node for node in derivation_tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "NEW_RAW_EVENT_FILES"
                               for target in node.targets)), None)
    require(assignment is not None, "v8 derivation script does not expose its event-reader patch bytes")
    new_reader = ast.literal_eval(assignment.value)
    require(isinstance(new_reader, bytes)
            and digest(derived[start:end]) == patch_rows[2].get("parent_reader_sha256")
            and digest(new_reader) == patch_rows[2].get("derived_reader_sha256"),
            "v8 event-reader patch differs from its declared exact parent/derived hashes")
    derived = derived[:start] + new_reader + derived[end:]
    require(derived == derived_runner_raw,
            "v8 capture runner contains changes outside the three declared patches")

    expected_design = json.loads(parent_design_raw)
    expected_design["study_code_provenance"]["capture_script_sha256"] = digest(derived_runner_raw)
    expected_design["study_code_provenance"]["capture_script_path"] = derived_path
    expected_design["derivation"] = derivation
    expected_design_raw = (json.dumps(expected_design, ensure_ascii=False, indent=2,
                                      sort_keys=True, allow_nan=False) + "\n").encode()
    require((design_dir / "study-design.json").read_bytes() == expected_design_raw
            and (design_dir / "study-design.sha256").read_text(encoding="ascii").split()[0] == design_sha256,
            "v8 design changes fields outside the capture source pin and derivation receipt")
    return {"status": "PASS_DERIVATION_ALLOWLIST_AND_INHERITED_PROTOCOL_BYTES",
            "parent_design_sha256": parent_sha, "parent_capture_runner_sha256": parent_runner_sha,
            "derived_capture_runner_sha256": digest(derived_runner_raw),
            "derivation_script_sha256": digest(derivation_script_raw),
            "allowed_patch_ids": [row["id"] for row in patch_rows],
            "inherited_file_count": len(copied_inventory),
            "inherited_protocol_files_byte_identical": len(copied_inventory)}


def check_public_preexecution_checkpoint(root: Path, design_dir: Path, design: dict,
                                         design_sha256: str,
                                         checkpoint_dir: Path | None = None) -> dict:
    checkpoint = (checkpoint_dir or root / "preexecution-checkpoint").resolve()
    manifest_path = checkpoint / "manifest.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(),
            "public preexecution source checkpoint manifest is missing")
    manifest = read_json(manifest_path)
    require(manifest.get("schema") == "gooo/ir-composition-tdd-public-preexecution-checkpoint/v1"
            and manifest.get("status") == "before_laya_start"
            and manifest.get("actual_laya_calls") == 0
            and manifest.get("design_path") == str((design_dir / "study-design.json").relative_to(root))
            and manifest.get("design_sha256") == design_sha256
            and manifest.get("compiler_binary_sha256") == design.get("compiler", {}).get("sha256")
            and manifest.get("compiler_source_revision") == design.get("compiler", {}).get("source_revision"),
            "public preexecution checkpoint is not bound to this frozen design/compiler before inference")
    capture_path = capture_script_relative_path(design)
    pins = {
        "study-code/scripts/prepare_study.py": design.get("study_code_provenance", {}).get("preparation_script_sha256"),
        "study-code/" + capture_path: design.get("study_code_provenance", {}).get("capture_script_sha256"),
        "study-code/dependencies/run_pinned_context_study.py": design.get("capture_proxy_dependency", {}).get("sha256"),
        "study-code/dependencies/selection_support.py": (design.get("capture_proxy_dependency", {})
                                                            .get("import_closure", [{}])[0].get("sha256")),
    }
    derivation = design.get("derivation")
    supplemental = []
    if derivation:
        derivation_path = derivation.get("derivation_script", {}).get("path")
        derivation_sha = derivation.get("derivation_script", {}).get("sha256")
        require(isinstance(derivation_path, str) and derivation_path.startswith("scripts/"),
                "derived design has no safe derivation script path")
        supplemental = manifest.get("supplemental_derivation_files", [])
        require(any(item.get("path") == "derivation-code/" + derivation_path
                    and item.get("sha256") == derivation_sha for item in supplemental),
                "v8 source archive lacks the exact supplemental derivation script")
    manifest_files = manifest.get("files", [])
    by_path = {item.get("path"): item for item in manifest_files}
    require(len(manifest_files) == len(pins) and len(by_path) == len(manifest_files)
            and set(by_path) == set(pins),
            "public preexecution checkpoint does not archive exactly the pinned source files")
    verified = []
    for relative, expected_sha in pins.items():
        item = by_path[relative]
        raw = check_sha_file(checkpoint / relative, expected_sha or "", relative)
        require(item.get("sha256") == expected_sha and item.get("bytes") == len(raw),
                f"public checkpoint manifest SHA/length differs from design pin: {relative}")
        verified.append({"path": relative, "sha256": expected_sha, "bytes": len(raw)})
    supplemental_verified = []
    if derivation:
        supplemental_map = {item.get("path"): item for item in supplemental}
        for item in supplemental:
            rel = item.get("path", "")
            raw = check_sha_file(checkpoint / rel, item.get("sha256", ""), rel)
            require(item.get("bytes") == len(raw), f"supplemental source archive byte count differs: {rel}")
            supplemental_verified.append({"path": rel, "sha256": item["sha256"], "bytes": len(raw)})
        require(len(supplemental_map) == 2
                and "derivation-code/scripts/derive_study_v8.py" in supplemental_map
                and "derivation-code/scripts/test_run_study_v8_paths.py" in supplemental_map,
                "v8 supplemental archive must include derivation source and its path containment checks")
    return {"status": "PASS_BEFORE_LAYA_START", "actual_laya_calls": 0,
            "design_sha256": design_sha256, "archived_files": verified,
            "supplemental_derivation_files": supplemental_verified}


def run_file(run_dir: Path, relative: str, label: str) -> Path:
    rel = Path(relative)
    require(not rel.is_absolute() and ".." not in rel.parts,
            f"{label}: path is not relative to the run directory")
    path = run_dir / rel
    require(path.resolve().is_relative_to(run_dir.resolve()) and path.is_file() and not path.is_symlink(),
            f"{label}: file is missing, symlinked, or escapes the run directory")
    return path


def observed_process_summary(samples: list[dict], key: str) -> dict:
    rows = [row[key] for row in samples if isinstance(row.get(key), dict) and row[key].get("alive")]
    cpu = [row["cpu_seconds"] for row in rows if isinstance(row.get("cpu_seconds"), (int, float))]
    pcpu = [row["pcpu_percent_sample"] for row in rows
            if isinstance(row.get("pcpu_percent_sample"), (int, float))]
    rss = [row["rss_kb"] for row in rows if isinstance(row.get("rss_kb"), int)]
    delta = max(0.0, cpu[-1] - cpu[0]) if len(cpu) > 1 else None
    if len(rows) < 2:
        delta, delta_status = None, "unknown_fewer_than_two_live_ps_samples"
    elif delta is None or delta <= 0:
        delta, delta_status = None, "unknown_below_ps_cputime_resolution_or_zero"
    else:
        delta_status = "coarse_observed_at_ps_second_resolution"
    return {"sample_count": len(rows), "cpu_seconds_delta_coarse": delta,
            "rss_peak_kb_sampled": max(rss) if rss else None,
            "pcpu_percent_max_sampled": max(pcpu) if pcpu else None,
            "cpu_delta_status": delta_status,
            "pcpu_interpretation": "sampled rolling process CPU percent; not host CPU increase"}


def check_run_resource_observations(run_dir: Path, records: list[dict], report: dict) -> dict:
    resource_path = report.get("resource_summary_path")
    if not resource_path:
        require(report.get("status") == "PARTIAL_CAPTURE",
                "complete run report is missing process resource observations")
        return {"status": "unknown_missing_resource_summary", "server_samples": 0}
    summary_path = run_file(run_dir, resource_path, "process resource summary")
    summary = read_json(summary_path)
    raw_relative = summary.get("raw_samples_path")
    raw_path = run_file(run_dir, raw_relative, "server raw ps samples")
    raw_lines = raw_path.read_bytes().splitlines()
    samples = [json.loads(line) for line in raw_lines if line.strip()]
    require(len(samples) == len(raw_lines), "server process sample log has a malformed/truncated JSONL row")
    indices = [row.get("sample_index") for row in samples]
    require(all(isinstance(value, int) for value in indices)
            and len(indices) == len(set(indices)) and indices == sorted(indices),
            "server process sample indices are missing, duplicated, or out of order")
    expected_all = observed_process_summary(samples, "laya_server")
    reported_all = summary.get("model_ready_through_capture_total", {}).get("process_summary", {})
    for field, value in expected_all.items():
        require(reported_all.get(field) == value,
                f"server all-window process metric {field} differs from raw ps samples")
    idle_path = run_dir / "resources/model-ready-idle-baseline.json"
    if idle_path.is_file():
        idle = read_json(idle_path)
        require(summary.get("server_only_idle_baseline") == idle
                and isinstance(idle.get("duration_ms"), (int, float))
                and idle["duration_ms"] >= 2000,
                "separate model-ready idle baseline is missing, short, or differs from resource summary")
        idle_samples = idle.get("samples", [])
        idle_expected = observed_process_summary(idle_samples, "laya_server")
        idle_reported = idle.get("process_summary", {})
        for field, value in idle_expected.items():
            require(idle_reported.get(field) == value,
                    f"idle-baseline process metric {field} differs from raw ps samples")
    else:
        require(report.get("status") == "PARTIAL_CAPTURE",
                "complete capture lacks the model-ready idle baseline file")

    per_invocation_checks = 0
    for record in records:
        resource_file = record.get("resource_observations_file")
        if not resource_file:
            continue
        path = run_file(run_dir, resource_file, "per-invocation process observations")
        details = read_json(path)
        require(details.get("invocation_id") == record.get("invocation_id"),
                "per-invocation process observations are attributed to a different cell")
        for array_name, key, detail_summary_field, record_summary_field in (
            ("laya_server_samples", "laya_server", "laya_server_summary", "laya_server_resource_summary"),
            ("cli_process_samples", "cli_process", "cli_process_summary", "cli_process_resource_summary"),
        ):
            rows = details.get(array_name, [])
            expected = observed_process_summary(rows, key)
            reported = details.get(detail_summary_field, {})
            for field, value in expected.items():
                require(reported.get(field) == value,
                        f"{record.get('invocation_id')} {key} {field} differs from raw ps samples")
            require(record.get(record_summary_field) == reported,
                    f"{record.get('invocation_id')} record differs from its process observation file")
        require("not host CPU increase" in details.get("notes", [""])[-1],
                "process resource note omits that sampled pcpu is not host CPU increase")
        per_invocation_checks += 1
    return {"status": "PASS_RAW_PROCESS_SAMPLES", "server_samples": len(samples),
            "per_invocation_resource_files": per_invocation_checks,
            "idle_baseline_present": idle_path.is_file()}


def run_raw_event_bytes(run_dir: Path, event: dict, side: str) -> bytes:
    relative = event.get(f"{side}_file")
    require(isinstance(relative, str), f"proxy event lacks {side} file path")
    path = run_file(run_dir, relative, f"proxy {side} bytes")
    raw = path.read_bytes()
    require(digest(raw) == event.get(f"{side}_sha256"),
            f"proxy event {side} digest mismatch at sequence {event.get('seq')}")
    return raw


def canonical_file_sha(path: Path, label: str) -> str:
    require(path.is_file() and not path.is_symlink(), f"{label} is missing or symlinked")
    return digest(path.read_bytes())


def check_capture_run(root: Path, design_dir: Path, design: dict, phase_rows: list[dict],
                      run_dir: Path) -> dict:
    """Audit captured files without starting a service, model, tokenizer, or Go process."""
    run_dir = run_dir.resolve()
    require(run_dir.is_dir(), f"capture run directory does not exist: {run_dir}")
    copied_design = check_sha_file(run_dir / "study-design.json",
                                   digest((design_dir / "study-design.json").read_bytes()),
                                   "run-copied frozen design")
    require((run_dir / "study-design.sha256").read_text(encoding="ascii").split()[0]
            == digest(copied_design), "run design checksum sidecar mismatch")
    run_phase = check_sha_file(run_dir / "phase-plans.json", design["phase_plan_sha256"],
                               "run-copied randomized phase plan")
    require(json.loads(run_phase).get("plans") == phase_rows,
            "run randomized phase-plan rows differ from the frozen 64-cell plan")

    report = read_json(run_dir / "report.json")
    metadata = read_json(run_dir / "run-metadata.json")
    preexecution_path = run_dir / "preexecution.json"
    preexecution_raw = check_sha_file(preexecution_path, metadata.get("preexecution_sha256", ""),
                                      "run pre-execution record")
    preexecution = json.loads(preexecution_raw)
    require(report.get("schema") == "gooo/ir-composition-tdd-capture-report/v1"
            and report.get("status") in {"CAPTURED", "PARTIAL_CAPTURE"},
            "capture report schema or status is invalid")
    require(metadata.get("run_id") == run_dir.name
            and metadata.get("schema") == "gooo/ir-composition-tdd-run-metadata/v1"
            and metadata.get("design_sha256") == digest(copied_design)
            and preexecution.get("design_sha256") == digest(copied_design)
            and metadata.get("status") == report.get("status"),
            "run metadata, pre-execution record, report, or directory identity disagree")
    require(metadata.get("credentials_unset") is True
            and metadata.get("downloads_disabled") is True
            and metadata.get("provider_model") == design["provider"]["model"]
            and metadata.get("model_revision") == design["provider"]["model_revision"]
            and metadata.get("measured_provider_post_cap") == design["provider"]["measured_provider_post_cap"],
            "run metadata changed provider, model revision, credentials, download, or POST-cap policy")
    require(preexecution.get("holdout_values_loaded") is False
            and preexecution.get("planned_cells") == 64
            and preexecution.get("measured_provider_post_cap") == 96,
            "pre-execution record does not preserve the frozen denominator and holdout gate")

    archive = preexecution.get("study_code_archive", [])
    capture_path = capture_script_relative_path(design)
    require(len(archive) == 4,
            "run pre-execution archive does not contain the four frozen runtime code files")
    archive_verified = []
    for item in archive:
        rel = item.get("path", "")
        raw = check_sha_file(run_file(run_dir, rel, "archived capture source"),
                             item.get("sha256", ""), f"archived capture source {rel}")
        require(len(raw) == item.get("bytes"), f"archived capture source byte count differs: {rel}")
        archive_verified.append({"path": rel, "sha256": item["sha256"], "bytes": len(raw)})
    archive_map = {item["path"]: item["sha256"] for item in archive_verified}
    require(archive_map.get("study-code/" + capture_path)
            == design.get("study_code_provenance", {}).get("capture_script_sha256")
            and archive_map.get("study-code/scripts/prepare_study.py")
            == design.get("study_code_provenance", {}).get("preparation_script_sha256")
            and archive_map.get("study-code/dependencies/run_pinned_context_study.py")
            == design.get("capture_proxy_dependency", {}).get("sha256")
            and archive_map.get("study-code/dependencies/selection_support.py")
            == design.get("capture_proxy_dependency", {}).get("import_closure", [{}])[0].get("sha256"),
            "run archived capture source bytes differ from the frozen source pins")

    events_obj = read_json(run_dir / "proxy/events.json")
    events = events_obj.get("events", [])
    require(events_obj.get("schema") == "gooo/ir-composition-tdd-capture-events/v1"
            and events_obj.get("raw_event_count") == len(events)
            and [event.get("seq") for event in events] == list(range(1, len(events) + 1)),
            "proxy event index is malformed or sequence numbers are not contiguous")
    jsonl_path = run_dir / "proxy/events.jsonl"
    jsonl_rows = []
    for line_number, line in enumerate(jsonl_path.read_bytes().splitlines(), 1):
        if not line:
            continue
        try:
            jsonl_rows.append(json.loads(line))
        except Exception as exc:
            raise InvalidStudy(f"proxy event JSONL row {line_number} is invalid: {exc}") from exc
    require(jsonl_rows == events, "proxy JSON and JSONL event indexes differ")
    planned_ids = {f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}" for row in phase_rows}
    require(all(event.get("invocation_id") in planned_ids for event in events),
            "proxy event is not attributed to a frozen invocation")
    post_events = [event for event in events if event.get("kind") == "laya_choice"]
    health_events = [event for event in events if event.get("kind") == "health_check"]
    require(len(post_events) <= 96
            and all(event.get("method") == "POST" and event.get("path") == "/v1/systemone"
                    and event.get("status") == 200 for event in post_events)
            and all(event.get("method") == "GET" and event.get("path") == "/health"
                    and event.get("status") == 200 for event in health_events)
            and len(post_events) + len(health_events) == len(events),
            "proxy events contain an unexpected method, endpoint, status, or event kind")

    # The report may have legacy summary bookkeeping defects. Rebuild planned,
    # observed, and unknown denominators from the immutable phase plan and raw files.
    invocation_root = run_dir / "invocations"
    require(invocation_root.is_dir() and not invocation_root.is_symlink(),
            "run invocation directory is missing or symlinked")
    planned_ids = [f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}" for row in phase_rows]
    records_by_id: dict[str, dict] = {}
    unstarted_by_id: dict[str, dict] = {}
    for invocation_id in planned_ids:
        inv_dir = invocation_root / invocation_id
        not_started = invocation_root / f"not-started-{int(invocation_id[:2]):02d}.json"
        if inv_dir.is_dir():
            invocation_path = inv_dir / "invocation.json"
            record = read_json(invocation_path)
            require(record.get("invocation_id") == invocation_id,
                    f"invocation directory and record identity differ: {invocation_id}")
            require(invocation_id not in records_by_id and invocation_id not in unstarted_by_id,
                    f"duplicate invocation record: {invocation_id}")
            records_by_id[invocation_id] = record
        else:
            require(not_started.is_file() and not not_started.is_symlink(),
                    f"planned invocation has neither raw record nor not-started record: {invocation_id}")
            row = read_json(not_started)
            require(row.get("not_started") is True and row.get("sequence") == int(invocation_id[:2])
                    and row.get("status") == "not_started_after_prior_failure",
                    f"not-started record is malformed: {invocation_id}")
            unstarted_by_id[invocation_id] = row
    require(len(records_by_id) + len(unstarted_by_id) == 64,
            "captured and unstarted invocation rows do not preserve all 64 planned cells")
    for path in invocation_root.glob("*/invocation.json"):
        require(path.parent.name in records_by_id, f"unplanned or duplicated invocation directory: {path.parent.name}")

    # One holdout list per intent is loaded from the frozen copy solely to check
    # that no holdout input/output pair entered captured prompt state.
    holdout_pairs: dict[str, set[tuple[object, object]]] = {}
    for plan_row in phase_rows:
        intent_id = plan_row["intent_id"]
        if intent_id not in holdout_pairs:
            holdout = read_json(design_dir / plan_row["holdout_path"])
            holdout_pairs[intent_id] = {
                (case.get("input"), case.get("expected")) for case in holdout
                if isinstance(case, dict) and "input" in case and "expected" in case
            }
    plan_by_seq = {row["sequence"]: row for row in phase_rows}
    independently_audited_cells = 0
    raw_choice_matches = 0
    first_proposal_matches = 0
    validation_issues = []
    legacy_capture_validation_mismatches = []
    event_by_invocation: dict[str, list[dict]] = {}
    for event in events:
        event_by_invocation.setdefault(event["invocation_id"], []).append(event)
    exchange_rows = []
    raw_first_proposal: dict[str, str] = {}
    for event in events:
        request_raw = run_raw_event_bytes(run_dir, event, "request")
        response_raw = run_raw_event_bytes(run_dir, event, "response")
        require(event.get("request_sha256") == digest(request_raw)
                and event.get("response_sha256") == digest(response_raw),
                f"raw event hashes do not match bytes at sequence {event['seq']}")
        if event["kind"] == "health_check":
            require(request_raw == b"", f"health request has unexpected body at sequence {event['seq']}")
            reply = json.loads(response_raw)
            require(reply.get("status") == "ok"
                    and reply.get("revisions", {}).get(design["provider"]["model"])
                    == design["provider"]["model_revision"],
                    f"health revision does not match frozen model pin at sequence {event['seq']}")
            exchange_rows.append({"seq": event["seq"], "kind": "health_check",
                                  "request_sha256": digest(request_raw),
                                  "response_sha256": digest(response_raw), "revision_matches": True})
            continue
        try:
            outer = json.loads(request_raw)
            reply = json.loads(response_raw)
        except Exception as exc:
            raise InvalidStudy(f"provider exchange {event['seq']} has invalid JSON: {exc}") from exc
        require(outer.get("model") == design["provider"]["model"]
                and reply.get("routing", {}).get("model") == design["provider"]["model"]
                and outer.get("model") == reply.get("routing", {}).get("model"),
                f"provider model pin differs between request and reply at sequence {event['seq']}")
        typed_sha = typed_request_sha(outer)
        state = json.loads(outer["state"]["request"])
        question_id, question = next(iter(outer["questions"].items()))
        choices = reply.get("answers", {}).get(question_id, {})
        proposed = choices.get("choice")
        candidate_ids = [item.get("id") for item in state.get("remaining_candidates", [])]
        require(proposed in candidate_ids and set(choices.get("probabilities", {})) == set(candidate_ids),
                f"provider proposal/probabilities do not match request choices at sequence {event['seq']}")
        require(not contains_case_pair(state, holdout_pairs.get(
                    next((row["intent_id"] for row in phase_rows
                          if f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}" == event["invocation_id"]), ""), set())),
                f"holdout pair leaked into request state at sequence {event['seq']}")
        exchange_rows.append({"seq": event["seq"], "kind": "laya_choice",
                              "invocation_id": event["invocation_id"],
                              "request_sha256": digest(request_raw),
                              "typed_request_sha256": typed_sha,
                              "response_sha256": digest(response_raw),
                              "provider_model": outer["model"], "routing_model": reply["routing"]["model"],
                              "proposal": proposed})
        raw_first_proposal.setdefault(event["invocation_id"], proposed)

    per_cell = []
    for plan_row in phase_rows:
        invocation_id = f"{plan_row['sequence']:02d}-{plan_row['intent_id']}-{plan_row['arm']}"
        record = records_by_id.get(invocation_id)
        if record is None:
            per_cell.append({"invocation_id": invocation_id, "sequence": plan_row["sequence"],
                             "arm": plan_row["arm"], "status": "not_started",
                             "capture_observed": False, "capture_validated_independently": False})
            continue
        require(record.get("sequence") == plan_row["sequence"]
                and record.get("intent_id") == plan_row["intent_id"]
                and record.get("arm") == plan_row["arm"]
                and record.get("provider_model") == plan_row["provider_model"]
                and record.get("max_attempts") == plan_row["max_attempts"],
                f"invocation record differs from frozen phase plan: {invocation_id}")
        inv_dir = invocation_root / invocation_id
        for field, expected, label in (("plan_sha256", plan_row["plan_sha256"], "search plan"),
                                       ("fixture_sha256", plan_row["fixture_sha256"], "fixture")):
            require(record.get(field) == expected, f"{invocation_id} {label} SHA differs from frozen plan")
        plan_path = run_file(run_dir, f"invocations/{invocation_id}/plan.search.json", "captured search plan")
        fixture_path = run_file(run_dir, f"invocations/{invocation_id}/fixture.gooo", "captured source fixture")
        expected_plan_path = design_dir / plan_row["plan_path"]
        expected_fixture_path = design_dir / design["source"]["path"] / plan_row["fixture_path"]
        require(plan_path.read_bytes() == expected_plan_path.read_bytes()
                and fixture_path.read_bytes() == expected_fixture_path.read_bytes(),
                f"captured plan/fixture bytes differ from frozen inputs: {invocation_id}")
        inv_events = event_by_invocation.get(invocation_id, [])
        posts = [event for event in inv_events if event.get("kind") == "laya_choice"]
        attempts = record.get("body_search_attempts", [])
        if record.get("status") == "failed_before_complete":
            require(record.get("exit_code") is None and record.get("capture_validation_passed") is False
                    and record.get("raw_provider_post_count") == len(posts)
                    and record.get("proxy_event_sequences") == [event["seq"] for event in inv_events]
                    and isinstance(record.get("error"), str) and record["error"],
                    f"incomplete invocation does not preserve a coherent error/raw-event row: {invocation_id}")
            if raw_first_proposal.get(invocation_id) == plan_row.get("intended_candidate_id"):
                raw_choice_matches += 1
            per_cell.append({"invocation_id": invocation_id, "sequence": plan_row["sequence"],
                             "arm": plan_row["arm"], "status": "failed_before_complete",
                             "capture_observed": True, "cli_returned": False,
                             "raw_provider_posts": len(posts),
                             "native_attempts": len(attempts),
                             "raw_request_receipt_choice_binding_passed": False,
                             "legacy_capture_validation_passed": False,
                             "first_raw_proposal_candidate_id": raw_first_proposal.get(invocation_id),
                             "first_proposal_matches_intended": (
                                 raw_first_proposal.get(invocation_id) == plan_row.get("intended_candidate_id")),
                             "unknown_reason": record["error"]})
            continue
        stdout_raw = check_sha_file(run_file(run_dir, record.get("stdout_file", ""), "CLI stdout"),
                                    record.get("stdout_sha256", ""), f"{invocation_id} stdout")
        stderr_raw = check_sha_file(run_file(run_dir, record.get("stderr_file", ""), "CLI stderr"),
                                    record.get("stderr_sha256", ""), f"{invocation_id} stderr")
        require(record.get("completion") == "completed"
                and record.get("cli_completed_unix_ns") is not None,
                f"captured CLI process completion is not preserved: {invocation_id}")
        if record.get("exit_code") != 0 or record.get("timed_out") is True:
            if raw_first_proposal.get(invocation_id) == plan_row.get("intended_candidate_id"):
                raw_choice_matches += 1
            per_cell.append({"invocation_id": invocation_id, "sequence": plan_row["sequence"],
                             "arm": plan_row["arm"], "status": "cli_failed_or_timed_out",
                             "capture_observed": True, "cli_returned": True,
                             "exit_code": record.get("exit_code"), "timed_out": record.get("timed_out"),
                             "raw_provider_posts": len(posts), "native_attempts": len(attempts),
                             "raw_request_receipt_choice_binding_passed": False,
                             "legacy_capture_validation_passed": record.get("capture_validation_passed"),
                             "first_raw_proposal_candidate_id": raw_first_proposal.get(invocation_id),
                             "first_proposal_matches_intended": (
                                 raw_first_proposal.get(invocation_id) == plan_row.get("intended_candidate_id")),
                             "stderr_sha256": digest(stderr_raw),
                             "stdout_sha256": digest(stdout_raw)})
            continue
        try:
            cli_output = json.loads(stdout_raw)
        except Exception as exc:
            raise InvalidStudy(f"captured CLI stdout is not JSON for {invocation_id}: {exc}") from exc
        cli_report = cli_output.get("report", {})
        cli_body = cli_report.get("body_search", {})
        attempts = record.get("body_search_attempts", [])
        emitted_source = cli_output.get("source")
        generated_digest = cli_report.get("generated_digest", "")
        require(cli_report.get("decision") == "PASS" and cli_body.get("attempts") == attempts
                and isinstance(generated_digest, str) and generated_digest.startswith("sha256:")
                and isinstance(emitted_source, str)
                and generated_digest == "sha256:" + digest(emitted_source.encode("utf-8")),
                f"captured CLI JSON and invocation receipt differ or lack emitted source: {invocation_id}")
        frozen_plan = json.loads(plan_path.read_bytes())
        require(record.get("raw_provider_post_count") == len(posts)
                and record.get("proxy_event_sequences") == [event["seq"] for event in inv_events],
                f"captured raw event count/sequence attribution differs from invocation receipt: {invocation_id}")
        provider_attempts = [(native_index, attempt) for native_index, attempt in enumerate(attempts)
                             if (attempt.get("decision") or {}).get("mode") == "laya"]
        if len(posts) != len(provider_attempts):
            validation_issues.append(
                f"{invocation_id}: raw POST count {len(posts)} != native Laya receipt count {len(provider_attempts)}")
            raw_attempt_links_ok = False
        else:
            raw_attempt_links_ok = True
        row_independent_ok = True
        for provider_index, (event, (native_index, attempt)) in enumerate(zip(posts, provider_attempts)):
            request_raw = run_raw_event_bytes(run_dir, event, "request")
            response_raw = run_raw_event_bytes(run_dir, event, "response")
            outer = json.loads(request_raw)
            reply = json.loads(response_raw)
            decision = attempt.get("decision", {})
            typed_sha = typed_request_sha(outer)
            response_choice = reply.get("answers", {}).get("body_ir_search", {}).get("choice")
            state = json.loads(outer["state"]["request"])
            ordered_candidates = frozen_plan.get("candidates", [])
            previously_chosen = {prior.get("candidate_id") for prior in attempts[:native_index]}
            expected_remaining = [candidate for candidate in ordered_candidates
                                  if candidate.get("id") not in previously_chosen]
            state_candidates = state.get("remaining_candidates", [])
            state_candidate_pairs = [(candidate.get("id"), candidate.get("expression"))
                                     for candidate in state_candidates]
            expected_candidate_pairs = [(candidate.get("id"), candidate.get("expression"))
                                        for candidate in expected_remaining]
            if (state.get("intent") != frozen_plan.get("intent")
                    or state.get("training_test_count") != len(frozen_plan.get("test_cases", []))
                    or state_candidate_pairs != expected_candidate_pairs
                    or state.get("stage") != "choose_before_candidate_evaluation"):
                row_independent_ok = False
                validation_issues.append(f"{invocation_id} attempt {native_index + 1}: prompt state differs from frozen plan/candidate history")
            if not (outer.get("model") == record["provider_model"]
                    and reply.get("routing", {}).get("model") == record["provider_model"]
                    and decision.get("requested_provider_model") == record["provider_model"]
                    and decision.get("model_revision") == design["provider"]["model_revision"]
                    and decision.get("request_sha256") == typed_sha
                    and response_choice == decision.get("selected") == attempt.get("candidate_id")
                    and event.get("selected_candidate_id") == response_choice):
                row_independent_ok = False
                validation_issues.append(f"{invocation_id} attempt {native_index + 1}: raw model/typed receipt/choice mismatch")
            expected_question = next(iter(outer["questions"].values()))
            expected_criteria = {candidate["id"]: "Try this exact expression: " + candidate["expression"]
                                 for candidate in state.get("remaining_candidates", [])}
            if outer["questions"].get("body_ir_search", {}).get("criteria") != expected_criteria:
                row_independent_ok = False
                validation_issues.append(f"{invocation_id} attempt {native_index + 1}: criteria map differs from state candidates")
            if contains_case_pair(state, holdout_pairs[plan_row["intent_id"]]) or contains_case_pair(
                    outer, holdout_pairs[plan_row["intent_id"]]):
                row_independent_ok = False
                validation_issues.append(f"{invocation_id} attempt {native_index + 1}: holdout pair leaked into raw request")
        selected = provider_attempts[0][1].get("candidate_id") if provider_attempts else None
        if attempts and selected == plan_row.get("intended_candidate_id"):
            first_proposal_matches += 1
        if raw_first_proposal.get(invocation_id) == plan_row.get("intended_candidate_id"):
            raw_choice_matches += 1
        independently_valid = bool(attempts) and row_independent_ok and raw_attempt_links_ok
        if independently_valid:
            independently_audited_cells += 1
        reported_valid = record.get("capture_validation_passed")
        if independently_valid != (reported_valid is True):
            legacy_capture_validation_mismatches.append(
                f"{invocation_id}: independent raw audit={independently_valid}, legacy capture_validation_passed={reported_valid}")
        per_cell.append({"invocation_id": invocation_id, "sequence": plan_row["sequence"],
                         "arm": plan_row["arm"], "status": "captured_cli_completed",
                         "capture_observed": True, "cli_returned": True,
                         "exit_code": record.get("exit_code"),
                         "raw_provider_posts": len(posts),
                         "native_attempts": len(attempts),
                         "native_laya_receipts": len(provider_attempts),
                         "sole_candidate_deterministic_attempts": sum(
                             attempt.get("selection_method") == "sole_remaining_candidate" for attempt in attempts),
                         "raw_request_receipt_choice_binding_passed": independently_valid,
                         "legacy_capture_validation_passed": reported_valid,
                         "first_proposal_candidate_id": selected,
                         "first_proposal_matches_intended": selected == plan_row.get("intended_candidate_id"),
                         "source_completeness_receipt_decision": record.get("source_completeness_receipt", {}).get("decision")})

    cli_returned_cells = sum(1 for record in records_by_id.values()
                             if record.get("cli_completed_unix_ns") is not None)
    require(report.get("scheduled_cells") == 64
            and report.get("completed_cli_cells") == cli_returned_cells
            and report.get("raw_provider_posts") == len(post_events)
            and metadata.get("completed_cells") == cli_returned_cells
            and metadata.get("actual_provider_posts") == len(post_events),
            "capture report/metadata totals differ from independently enumerated raw rows")
    shutdown = report.get("owned_service_shutdown", {})
    drains = report.get("provider_forward_drain", {})
    forwards_settled = (drains.get("pre_shutdown", {}).get("settled") is True
                        and drains.get("post_shutdown", {}).get("settled") is True
                        and not drains.get("pre_shutdown", {}).get("pending_sequences", [])
                        and not drains.get("post_shutdown", {}).get("pending_sequences", []))
    scoring_status = report.get("independent_go_scoring")
    require(metadata.get("independent_go_scoring") == scoring_status,
            "run metadata and report disagree on post-capture scoring status")
    replay_path = run_file(run_dir, "independent-go/replay-results.json", "independent Go scoring result record")
    replay_rows = read_json(replay_path)
    if shutdown.get("confirmed") is True and forwards_settled:
        require(scoring_status == "ran_after_service_shutdown_and_forward_settlement"
                and isinstance(replay_rows, list),
                "post-capture Go scoring is not bound to confirmed service shutdown and forward settlement")
    else:
        require(scoring_status in {"skipped_unresolved_provider_forward",
                                   "skipped_owned_service_exit_unconfirmed"}
                and isinstance(replay_rows, list)
                and all(str(item.get("status", "")).startswith("not_scored_")
                        and "training" not in item.get("training", {})
                        and "holdout" not in item.get("holdout", {}) for item in replay_rows),
                "unsettled service/forward state must skip Go scoring and contain no holdout values")
    if report.get("status") == "PARTIAL_CAPTURE":
        incomplete_records = sum(record.get("status") == "failed_before_complete"
                                 for record in records_by_id.values())
        require(report.get("completed_cli_cells") + incomplete_records + len(unstarted_by_id) == 64,
                "partial report does not retain every planned cell as observed or unstarted")
    elif report.get("status") == "CAPTURED":
        require(report.get("completed_cli_cells") == 64 and not unstarted_by_id,
                "CAPTURED status has missing CLI cells")

    resource_result = check_run_resource_observations(run_dir, list(records_by_id.values()), report)
    summary = read_json(run_file(run_dir, report.get("summary_path", "summary.json"),
                                 "aggregate summary"))
    discrepancies = []
    for arm in ARMS:
        expected_cli = sum(1 for row in phase_rows if row["arm"] == arm
                           and (record := records_by_id.get(
                               f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}")) is not None
                           and record.get("cli_completed_unix_ns") is not None
                           and record.get("exit_code") == 0)
        expected_posts = sum(1 for event in post_events
                             if next(row["arm"] for row in phase_rows
                                     if f"{row['sequence']:02d}-{row['intent_id']}-{row['arm']}" == event["invocation_id"]) == arm)
        bucket = summary.get("arms", {}).get(arm, {})
        for key, expected in (("captured_cli_successes", expected_cli),
                              ("raw_provider_posts", expected_posts),
                              ("capture_validated_cells", sum(1 for item in per_cell
                                                               if item["arm"] == arm
                                                               and item.get("raw_request_receipt_choice_binding_passed")))):
            if bucket.get(key) != expected:
                discrepancies.append({"arm": arm, "field": key, "original_value": bucket.get(key),
                                       "raw_recomputed_value": expected})

    binding_path = run_dir / "capture-authorization-binding.json"
    binding_result = {"present": False}
    if binding_path.is_file() and not binding_path.is_symlink():
        binding = read_json(binding_path)
        bound_report = binding.get("capture_outcome", {})
        binding_result = {"present": True, "status": binding.get("status"),
                          "capture_outcome_matches_report": (
                              bound_report.get("report_sha256") == digest((run_dir / "report.json").read_bytes())
                              and bound_report.get("status") == report.get("status")
                              and bound_report.get("actual_laya_provider_posts") == len(post_events)
                              and bound_report.get("cells_not_started") == len(unstarted_by_id))}
        require(binding_result["capture_outcome_matches_report"],
                "capture authorization binding does not match preserved raw report and counts")

    arm_denominators = {}
    for arm in ARMS:
        planned = sum(1 for row in phase_rows if row["arm"] == arm)
        cells = [item for item in per_cell if item.get("arm") == arm]
        completed = sum(item.get("cli_returned") is True for item in cells)
        raw_observed = sum(item.get("raw_provider_posts", 0) > 0 for item in cells)
        raw_audited = sum(item.get("raw_request_receipt_choice_binding_passed") is True for item in cells)
        arm_denominators[arm] = {
            "planned_cells": planned,
            "completed_cli_cells": completed,
            "not_started_or_unknown_cells": planned - completed,
            "provider_proposal_observed_cells": raw_observed,
            "provider_proposal_unknown_cells": planned - raw_observed,
            "raw_request_receipt_choice_audited_cells": raw_audited,
            "raw_request_receipt_choice_unknown_cells": planned - raw_audited,
            "raw_first_proposal_matches_intended": sum(
                item.get("first_proposal_matches_intended") is True for item in cells
                if item.get("raw_provider_posts", 0) > 0),
        }

    return {
        "schema": "gooo/ir-composition-tdd-independent-capture-audit/v1",
        "status": (("PASS_RAW_AUDIT_PARTIAL_CAPTURE" if report.get("status") == "PARTIAL_CAPTURE"
                    else "PASS_RAW_AUDIT_CAPTURED") if not validation_issues else "FAIL_RAW_EVIDENCE_AUDIT"),
        "run_directory": str(run_dir), "run_id": metadata["run_id"],
        "capture_status_from_original_report": report["status"],
        "design_sha256": metadata["design_sha256"],
        "source_archive_verified": archive_verified,
        "provider_events": {"raw_events": len(events), "provider_posts": len(post_events),
                            "health_checks": len(health_events), "exchanges": exchange_rows},
        "planned_cells": 64, "completed_cli_cells_raw_observed": cli_returned_cells,
        "incomplete_invocation_records": sum(record.get("status") == "failed_before_complete"
                                              for record in records_by_id.values()),
        "not_started_cells": len(unstarted_by_id),
        "independently_audited_cells_raw_request_receipt_choice": independently_audited_cells,
        "raw_first_proposal_matches_intended": raw_choice_matches,
        "native_first_proposal_matches_intended": first_proposal_matches,
        "arm_denominators": arm_denominators,
        "per_cell": per_cell,
        "original_capture_validation_false_but_raw_audit_passed": [
            item["invocation_id"] for item in per_cell
            if item.get("raw_request_receipt_choice_binding_passed")
            and item.get("legacy_capture_validation_passed") is not True],
        "original_summary_discrepancies_preserved_and_reported": discrepancies,
        "validation_issues": validation_issues,
        "legacy_capture_validation_mismatches_separately_reported": legacy_capture_validation_mismatches,
        "service_shutdown_and_forward_settlement": {
            "owned_service_shutdown": shutdown,
            "provider_forward_drain": drains,
            "post_capture_scoring_gate": scoring_status,
            "independent_go_result_rows": len(replay_rows)},
        "process_resource_audit": resource_result,
        "capture_authorization_binding": binding_result,
        "summary_sha256": canonical_file_sha(run_file(run_dir, report.get("summary_path", "summary.json"),
                                                       "aggregate summary"), "aggregate summary"),
        "no_model_or_go_processes_started_by_this_audit": True,
    }


def validate(root: Path, design_dir: Path, checkpoint_dir: Path | None = None) -> dict:
    design_path = design_dir / "study-design.json"
    design_raw = design_path.read_bytes()
    design_sha = digest(design_raw)
    checksum = (design_dir / "study-design.sha256").read_text(encoding="ascii").split()[0]
    require(design_sha == checksum, "study-design.json checksum sidecar mismatch")
    design = json.loads(design_raw)
    match = re.fullmatch(r"study-design-v(\d+)", design_dir.name)
    require(match is not None, f"unexpected numbered design folder: {design_dir.name}")
    schema_match = re.fullmatch(r"gooo/ir-composition-tdd-study-design/v(\d+)",
                                design.get("schema", ""))
    require(schema_match is not None
            and design.get("preparation_revision") == int(schema_match.group(1))
            and design.get("status") == "frozen_before_live_provider_calls"
            and design.get("planned_cells") == 64,
            "design schema/revision/status/cell denominator is malformed")
    provider = design.get("provider", {})
    require(provider.get("live_calls_authorized") is False
            and provider.get("live_calls_during_preparation") == 0
            and provider.get("model") == "multilingual"
            and provider.get("warmup_calls") == 0
            and provider.get("measured_provider_post_cap") == 96,
            "design provider pin/call policy changed")
    code = design.get("study_code_provenance", {})
    capture_path = capture_script_relative_path(design)
    derivation_result = check_v8_derivation(root, design_dir, design, design_sha)
    for key, path in (("capture_script_sha256", root / capture_path),
                      ("preparation_script_sha256", root / "scripts/prepare_study.py")):
        require(path.is_file() and digest(path.read_bytes()) == code.get(key),
                f"exact frozen source code pin does not match {path}")
    proxy = design.get("capture_proxy_dependency", {})
    require(isinstance(proxy.get("sha256"), str) and len(proxy["sha256"]) == 64
            and proxy.get("bytes", 0) > 0 and proxy.get("reused_symbol") == "CaptureProxy",
            "external capture proxy dependency is missing a byte/SHA pin")
    proxy_path = Path(proxy.get("path_at_freeze", ""))
    proxy_local = proxy_path.is_file()
    if proxy_local:
        require(digest(proxy_path.read_bytes()) == proxy["sha256"]
                and proxy_path.stat().st_size == proxy["bytes"],
                "locally available capture proxy bytes differ from the freeze pin")
    import_closure = proxy.get("import_closure", [])
    require(len(import_closure) == 1, "capture proxy must pin its complete non-stdlib import closure")
    closure = import_closure[0]
    closure_path = Path(closure.get("path_at_freeze", ""))
    require(closure.get("module") == "selection_support" and len(closure.get("sha256", "")) == 64
            and closure.get("bytes", 0) > 0 and closure_path.name == "selection_support.py"
            and proxy_path.parent == closure_path.parent,
            "capture proxy transitive sibling import pin is incomplete or points outside its sibling directory")
    closure_local = closure_path.is_file()
    if closure_local:
        require(digest(closure_path.read_bytes()) == closure["sha256"]
                and closure_path.stat().st_size == closure["bytes"],
                "locally available capture proxy transitive source differs from the freeze pin")
    sampling = design.get("resource_sampling", {})
    require(sampling.get("source_dependency_sha256") == proxy.get("sha256")
            and sampling.get("server_sample_interval_seconds") == 1.0
            and sampling.get("cli_sample_interval_seconds") == 0.25
            and sampling.get("model_ready_idle_baseline_seconds") == 2.5
            and sampling.get("sample_helpers") == ["sample_pid", "process_summary"],
            "resource observation intervals/helper source are not bound to the pinned proxy dependency")
    source_result = check_source_tree(design_dir, design)
    phase_rows, _ = check_phase_plan(design_dir, design)
    plan_result = check_plans_and_holdouts(design_dir, phase_rows)
    mock_result = check_mock_preflight(design_dir, design, phase_rows)
    history = check_archive_history(root)
    preexecution_checkpoint = check_public_preexecution_checkpoint(root, design_dir, design, design_sha,
                                                                    checkpoint_dir)
    return {
        "schema": "gooo/ir-composition-tdd-independent-freeze-validation/v1",
        "status": "PASS",
        "design_path": str(design_dir.relative_to(root)),
        "design_sha256": design_sha,
        "preparation_revision": design.get("preparation_revision"),
        "provider_calls": 0,
        "go_toolchain_calls": 0,
        "source": source_result,
        "plans": plan_result,
        "mock_preflight": mock_result,
        "capture_proxy_dependency": {
            "sha256": proxy["sha256"], "bytes": proxy["bytes"],
            "local_source_verified": proxy_local,
            "transitive_import_closure": [{"module": closure.get("module"),
                                           "sha256": closure.get("sha256"),
                                           "bytes": closure.get("bytes"),
                                           "local_source_verified": closure_local}],
        },
        "public_preexecution_checkpoint": preexecution_checkpoint,
        "source_derivation": derivation_result,
        "historical_archive_note": history,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--design-dir", help="numbered study-design directory; defaults to the highest revision")
    parser.add_argument("--checkpoint-dir", type=Path,
                        help="pre-execution source checkpoint; defaults to the original v7 checkpoint")
    parser.add_argument("--run-dir", type=Path,
                        help="optional captured run directory to audit without starting Laya, a model, or Go")
    parser.add_argument("--output", type=Path, help="optional JSON result file outside the frozen design")
    args = parser.parse_args()
    root = args.root.resolve()
    design_dir = choose_design_dir(root, args.design_dir)
    try:
        checkpoint_dir = args.checkpoint_dir.resolve() if args.checkpoint_dir else None
        report = validate(root, design_dir, checkpoint_dir)
        if args.run_dir:
            design_raw = check_sha_file(design_dir / "study-design.json",
                                        (design_dir / "study-design.sha256").read_text(encoding="ascii").split()[0],
                                        "study design for capture audit")
            design = json.loads(design_raw)
            phase_rows, _ = check_phase_plan(design_dir, design)
            report["capture_run_audit"] = check_capture_run(root, design_dir, design, phase_rows,
                                                            args.run_dir.resolve())
    except Exception as exc:
        report = {"schema": "gooo/ir-composition-tdd-independent-freeze-validation/v1",
                  "status": "FAIL", "design_path": str(design_dir),
                  "error": f"{type(exc).__name__}: {exc}"}
    raw = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if args.output:
        output = args.output.resolve()
        require(output != design_dir.resolve() and design_dir.resolve() not in output.parents,
                "validation output must not be written inside the frozen design")
        if args.run_dir:
            run_dir = args.run_dir.resolve()
            require(output != run_dir and run_dir not in output.parents,
                    "validation output must not be written inside the captured run")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
    sys.stdout.buffer.write(raw)
    capture_passed = ("capture_run_audit" not in report
                      or report["capture_run_audit"].get("status", "").startswith("PASS_"))
    return 0 if report["status"] == "PASS" and capture_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
