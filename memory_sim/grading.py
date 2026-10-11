"""Strict responses and end-to-end denominators, separate from tested adapters."""
from __future__ import annotations

import json
import math
import re
import statistics
import time

from .runtime import BudgetExceeded
from .workload import observed_state

VALUE = re.compile(r"v-[a-z0-9-]+")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_answer(text):
    """Return (value, abstained, valid); never extract a token from prose."""
    if not isinstance(text, str):
        return None, False, False
    text = text.strip()
    if VALUE.fullmatch(text):
        return text, False, True
    if re.fullmatch(r"NOT[\s_-]+FOUND", text, re.I) or text == "no relevant block found in the index":
        return None, True, True
    try:
        value = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        return None, False, False
    if not isinstance(value, dict) or set(value) != {"status", "value"}:
        return None, False, False
    if value["status"] == "not_found" and value["value"] is None:
        return None, True, True
    if value["status"] == "found" and isinstance(value["value"], str) and VALUE.fullmatch(value["value"]):
        return value["value"], False, True
    return None, False, False


def probe_keys(ledger, limit):
    """Critical-first, with round-robin coverage if critical facts exceed the cap."""
    groups = {}
    for key, event in sorted(ledger.latest.items()):
        if event.critical:
            groups.setdefault(event.group, []).append(key)
    selected = []
    while groups and len(selected) < limit:
        for group in sorted(list(groups)):
            selected.append(groups[group].pop(0))
            if not groups[group]:
                del groups[group]
            if len(selected) == limit:
                break
    other = [key for key in sorted(ledger.latest) if key not in selected]
    remaining = min(limit - len(selected), len(other))
    if remaining:
        selected.extend(other[round(i * (len(other) - 1) / max(1, remaining - 1))]
                        for i in range(remaining))
    return sorted(selected) + ["f-99998", "f-99999"]


def collect_answers(harness, provider, keys):
    """Collect observations only. This function receives no expected values."""
    cases = []
    exhausted = False
    for key in keys:
        if exhausted:
            cases.append({"key": key, "status": "budget_skipped"})
            continue
        harness.material, harness.api_failed, harness.chat_calls = "", False, 0
        calls_before = len(harness.transport.calls)
        exhaustion_before = harness.transport.exhaustions
        start = time.monotonic()
        row = {"key": key, "status": "error"}
        try:
            result = provider.handle_tool_call(
                "uniichat_recall", {"query": f"Latest value for {key}? Return only its value or NOT FOUND."})
            result = json.loads(result, object_pairs_hook=unique_object) if isinstance(result, str) else result
            if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
                raise ValueError("Malformed recall response")
            actual, abstained, valid = decode_answer(result["answer"])
            row.update(
                status="transport_error" if harness.api_failed else "scored" if valid else "invalid_answer",
                answer=actual, answer_text=result["answer"], abstained=abstained,
                source_ids=result.get("message_ids", []),
                evidence_state=observed_state(harness.material),
            )
        except BudgetExceeded:
            exhausted = True
        except Exception as exc:
            row["error_type"] = type(exc).__name__
        if harness.transport.exhaustions > exhaustion_before:
            exhausted = True
        if exhausted:
            row.update(status="budget_skipped", answer=None, abstained=False)
        row.update(
            latency_s=time.monotonic() - start,
            api_calls=len(harness.transport.calls) - calls_before,
            scripted_chat_calls=harness.chat_calls,
        )
        cases.append(row)
    return cases


def grade_answers(cases, ledger):
    """Every planned query remains in the denominator, including errors and skips."""
    graded = []
    for observation in cases:
        row = dict(observation)
        event = ledger.latest.get(row["key"])
        expected = event.value if event else None
        actual = row.get("answer")
        successful = row["status"] == "scored"
        correct = successful and (
            actual == expected if expected is not None else actual is None and row.get("abstained", False))
        material = row.pop("evidence_state", {})
        evidence = event is not None and tuple(material.get(event.key, ())) == (event.turn + 1, expected)
        known_values = ledger.known.get(row["key"], set())
        row.update(
            group=event.group if event else "unknown",
            critical=bool(event and event.critical),
            expected_deleted=event is not None and expected is None,
            correct=bool(correct), evidence_retrieved=evidence,
            stale=actual is not None and actual != expected and actual in known_values,
            invented=actual is not None and actual not in known_values,
        )
        graded.append(row)
    known = [row for row in graded if row["group"] != "unknown"]
    unknown = [row for row in graded if row["group"] == "unknown"]
    active = [row for row in known if not row["expected_deleted"]]
    deleted = [row for row in known if row["expected_deleted"]]
    answered = [row for row in graded if row["status"] == "scored"]
    active_answered = [row for row in active if row["status"] == "scored"]
    latency = [row["latency_s"] for row in graded if row.get("latency_s") is not None]
    groups = {}
    for group in sorted({row["group"] for row in graded}):
        selected = [row for row in graded if row["group"] == group]
        groups[group] = {"correct": sum(row["correct"] for row in selected), "total": len(selected)}
    return {
        "correct": sum(row["correct"] for row in graded), "total": len(graded),
        "known_correct": sum(row["correct"] for row in known), "known_total": len(known),
        "active_correct": sum(row["correct"] for row in active), "active_total": len(active),
        "deleted_correct": sum(row["correct"] for row in deleted), "deleted_total": len(deleted),
        "unknown_correct": sum(row["correct"] for row in unknown), "unknown_total": len(unknown),
        "answered_correct": sum(row["correct"] for row in answered), "answered_total": len(answered),
        "active_answered_correct": sum(row["correct"] for row in active_answered),
        "active_answered_total": len(active_answered),
        "evidence_retrieved": sum(row["evidence_retrieved"] for row in known),
        "stale": sum(row["stale"] for row in graded), "invented": sum(row["invented"] for row in graded),
        "critical_correct": sum(row["correct"] for row in known if row["critical"]),
        "critical_evidence_retrieved": sum(row["evidence_retrieved"] for row in known if row["critical"]),
        "critical_total": sum(row["critical"] for row in known),
        "critical_available": sum(event.critical for event in ledger.latest.values()),
        "errors": sum(row["status"] not in {"scored", "budget_skipped"} for row in graded),
        "invalid_answers": sum(row["status"] == "invalid_answer" for row in graded),
        "groups": groups, "cases": graded, "planned_queries": len(graded),
        "budget_skipped": sum(row["status"] == "budget_skipped" for row in graded),
        "latency_p50_s": statistics.median(latency) if latency else None,
        "latency_p95_s": sorted(latency)[math.ceil(len(latency) * .95) - 1] if latency else None,
    }


def query_metrics(harness, provider, ledger, max_probes):
    return grade_answers(collect_answers(harness, provider, probe_keys(ledger, max_probes)), ledger)
