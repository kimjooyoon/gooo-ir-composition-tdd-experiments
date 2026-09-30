#!/usr/bin/env python3
"""Independently validate the frozen model-free TDD study artifacts.

This validator uses only Python's standard library. It does not start Gooo,
Laya, a model, a tokenizer, or a Go toolchain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
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


def check_public_preexecution_checkpoint(root: Path, design_dir: Path, design: dict,
                                         design_sha256: str) -> dict:
    checkpoint = root / "preexecution-checkpoint"
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
    pins = {
        "study-code/scripts/prepare_study.py": design.get("study_code_provenance", {}).get("preparation_script_sha256"),
        "study-code/scripts/run_study.py": design.get("study_code_provenance", {}).get("capture_script_sha256"),
        "study-code/dependencies/run_pinned_context_study.py": design.get("capture_proxy_dependency", {}).get("sha256"),
        "study-code/dependencies/selection_support.py": (design.get("capture_proxy_dependency", {})
                                                            .get("import_closure", [{}])[0].get("sha256")),
    }
    manifest_files = manifest.get("files", [])
    by_path = {item.get("path"): item for item in manifest_files}
    require(len(manifest_files) == len(pins) and len(by_path) == len(manifest_files)
            and set(by_path) == set(pins),
            "public preexecution checkpoint does not archive exactly the four pinned source files")
    verified = []
    for relative, expected_sha in pins.items():
        item = by_path[relative]
        raw = check_sha_file(checkpoint / relative, expected_sha or "", relative)
        require(item.get("sha256") == expected_sha and item.get("bytes") == len(raw),
                f"public checkpoint manifest SHA/length differs from design pin: {relative}")
        verified.append({"path": relative, "sha256": expected_sha, "bytes": len(raw)})
    return {"status": "PASS_BEFORE_LAYA_START", "actual_laya_calls": 0,
            "design_sha256": design_sha256, "archived_files": verified}


def validate(root: Path, design_dir: Path) -> dict:
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
    for key, path in (("capture_script_sha256", root / "scripts/run_study.py"),
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
    preexecution_checkpoint = check_public_preexecution_checkpoint(root, design_dir, design, design_sha)
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
        "historical_archive_note": history,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--design-dir", help="numbered study-design directory; defaults to the highest revision")
    parser.add_argument("--output", type=Path, help="optional JSON result file outside the frozen design")
    args = parser.parse_args()
    root = args.root.resolve()
    design_dir = choose_design_dir(root, args.design_dir)
    try:
        report = validate(root, design_dir)
    except Exception as exc:
        report = {"schema": "gooo/ir-composition-tdd-independent-freeze-validation/v1",
                  "status": "FAIL", "design_path": str(design_dir),
                  "error": f"{type(exc).__name__}: {exc}"}
    raw = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if args.output:
        output = args.output.resolve()
        require(output != design_dir.resolve() and design_dir.resolve() not in output.parents,
                "validation output must not be written inside the frozen design")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
    sys.stdout.buffer.write(raw)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
