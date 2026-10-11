import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import simulator
from memory_sim.report import compare_runs, ratio
from memory_sim.grading import decode_answer, probe_keys
from memory_sim.runner import atomic_json, query_metrics
from memory_sim.runtime import BudgetExceeded, Harness, Transport
from memory_sim.workload import Ledger, generate, observed_state, render_state, workload_hash

ROOT = Path(simulator.HERE)


def delayed_worker(folder):
    path = Path(folder)
    path.mkdir()
    atomic_json(path / "result.json", {"status": "running", "cycles": []})
    time.sleep(5)


def valid_config(**changes):
    return {
        **simulator.PRESETS["smoke"], "preset": "smoke", "payload_chars": 300,
        "context_length": 64000, "timeout": 30, "max_disk_mb": 256,
        "session_every_cycles": 3, "max_api_calls": 12, "inference": "offline",
        "index_policy": "natural", "core_root": str(ROOT),
        "restart_mode": "process",
        "plugin": str(ROOT / "simulator.py"), **changes,
    }


def fixture_run(fingerprint="same", policy="natural", correct=5):
    score = {"correct": correct, "total": 10}
    retrieval = {
        "known_correct": correct, "known_total": 10, "critical_correct": 1, "critical_total": 1,
        "critical_available": 1,
        "critical_evidence_retrieved": 1,
        "active_correct": correct, "active_total": 10,
        "active_answered_correct": correct, "active_answered_total": 10,
        "answered_total": 12, "planned_queries": 12,
        "errors": 0, "budget_skipped": 0,
    }
    return {
        "config": valid_config(index_policy=policy),
        "seeds": [{
            "seed": 1, "status": "complete", "workload_sha256": fingerprint,
            "grading_version": 2, "workload_version": 2, "harness_sha256": "same-harness",
            "python_version": "same-python",
            "dependency_versions": {"pydantic": "synthetic-version"},
            "cycles": [{"cycle": 3, "turns": 200, "session": 0,
                        "restart_state_matches": True, "restart_raw_state_matches": True,
                        "saved": score, "context_after": score, "after_restart": score,
                        "retrieval": retrieval, "retrieval_after_restart": dict(retrieval),
                        "indexed_records": 10, "records": 10,
                        "duplicate_ids": 0, "facts_lost_this_compaction": 0}],
        }],
    }


class WorkloadTests(unittest.TestCase):
    def test_seed_is_reproducible(self):
        first = generate(12, 20, 60, 700)
        self.assertEqual(first, generate(12, 20, 60, 700))
        self.assertEqual(workload_hash(first), workload_hash(generate(12, 20, 60, 700)))

    def test_seed_changes_corpus(self):
        self.assertNotEqual(workload_hash(generate(1, 10, 20, 700)),
                            workload_hash(generate(2, 10, 20, 700)))

    def test_tool_facts_are_not_in_final_reply(self):
        events = generate(3, 10, 20, 700)
        tool = next(event for event in events if event.group == "tool")
        self.assertNotIn("FACT[", tool.user + tool.assistant)
        self.assertTrue(any("FACT[" in message["content"] for message in tool.messages))

    def test_buried_fact_is_beyond_cutoff(self):
        event = next(event for event in generate(1, 10, 20, 700) if event.group == "buried")
        self.assertGreater(event.user.index("FACT["), 250)

    def test_revisions_not_listing_order_determine_latest(self):
        text = "FACT[f-00001]=v-new REV[9]\nFACT[f-00001]=v-old REV[1]"
        self.assertEqual(observed_state(text), {"f-00001": (9, "v-new")})

    def test_deletions_do_not_revive_old_value(self):
        text = "DELETE[f-00001] REV[9]\nFACT[f-00001]=v-old REV[1]"
        self.assertEqual(observed_state(text), {"f-00001": (9, None)})

    def test_marker_roundtrip(self):
        state = {"f-00001": (5, "v-a"), "f-00002": (8, None)}
        self.assertEqual(observed_state(render_state(state)), state)

    def test_full_source_matches_independent_ledger(self):
        events = generate(9, 20, 100, 700)
        ledger = Ledger()
        for event in events:
            ledger.apply(event)
        material = "\n".join(message["content"] for event in events for message in event.messages)
        grade = ledger.grade_state(observed_state(material))
        self.assertEqual(grade["correct"], grade["total"])
        self.assertGreater(sum(event.value is None for event in events), 0)

    def test_grader_rejects_invented_fact(self):
        ledger = Ledger()
        event = generate(1, 1, 1, 300)[0]
        ledger.apply(event)
        self.assertEqual(ledger.grade_state({event.key: (1, "v-not-real")})["correct"], 0)

    def test_critical_facts_cover_every_evidence_group(self):
        for seed in (1, 2, 3, 4, 5):
            events = generate(seed, 100, 100, 700)
            self.assertEqual({e.group for e in events if e.critical}, {"short", "buried", "tool"})
            self.assertEqual(sum(e.critical for e in events), 10)

    def test_capped_critical_probes_cover_every_group(self):
        ledger = Ledger()
        for event in generate(1, 500, 500, 700):
            ledger.apply(event)
        keys = probe_keys(ledger, 3)[:-2]
        self.assertEqual({ledger.latest[key].group for key in keys}, {"short", "buried", "tool"})

    def test_small_workloads_have_a_critical_fact_per_available_group(self):
        for facts in (5, 10, 20):
            events = generate(1, facts, facts, 700)
            self.assertEqual({e.group for e in events if e.critical}, {"short", "buried", "tool"})


class AdapterTests(unittest.TestCase):
    def test_offline_transport_cannot_connect(self):
        with patch("urllib.request.urlopen") as request:
            with self.assertRaises(ValueError):
                Transport().ask("Synthetic content.")
            request.assert_not_called()

    def test_swallowed_budget_exception_is_not_scored(self):
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        ledger = Ledger()
        ledger.apply(generate(1, 1, 1, 300)[0])
        class Provider:
            def handle_tool_call(self, *args):
                adapter.transport.exhaustions += 1
                return {"answer": "no relevant block found in the index"}
        score = query_metrics(adapter, Provider(), ledger, 2)
        self.assertEqual(score["correct"], 0)
        self.assertEqual(score["total"], score["planned_queries"])
        self.assertEqual(score["budget_skipped"], score["planned_queries"])

    def test_deletion_abstention_is_not_active_value_recall(self):
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        ledger = Ledger()
        ledger.apply(replace(generate(1, 1, 1, 300)[0], value=None))
        class Provider:
            def handle_tool_call(self, *args):
                return {"answer": "no relevant block found in the index"}
        score = query_metrics(adapter, Provider(), ledger, 2)
        self.assertEqual(score["known_correct"], 1)
        self.assertEqual(score["active_total"], 0)
        self.assertEqual(score["deleted_correct"], 1)
        self.assertEqual(score["critical_evidence_retrieved"], 0)
    def test_router_only_uses_visible_index(self):
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        self.assertEqual(adapter.chat(
            "Memory index:\n[0+4] FACT[f-00001]=v-a REV[1]\nQuestion: Latest f-00002?"), "NONE")
        self.assertEqual(adapter.chat(
            "Memory index:\n[0+4] FACT[f-00001]=v-a REV[1]\nQuestion: Latest f-00001?"), "[0+4]")

    def test_reader_does_not_invent_an_absent_value(self):
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        self.assertEqual(adapter.chat(
            "Raw messages:\nFACT[f-00001]=v-a REV[1]\n\nQuestion: Latest f-00002?"), "NOT FOUND")

    def test_summary_preserves_only_supplied_source(self):
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        state = observed_state(adapter.summary("FACT[f-00001]=v-a REV[1]", 0))
        self.assertEqual(state, {"f-00001": (1, "v-a")})

    def test_real_transport_enforces_budget(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps({"model": "synthetic", "usage": {},
                                   "choices": [{"finish_reason": "stop",
                                                "message": {"content": "NOT FOUND"}}]}).encode()
        with patch.dict(os.environ, {"SURPLUS_API_KEY": "synthetic-key"}), \
                patch("urllib.request.urlopen", return_value=Response()) as request:
            transport = Transport("surplus", max_calls=1)
            self.assertEqual(transport.ask("Synthetic content."), "NOT FOUND")
            with self.assertRaises(BudgetExceeded):
                transport.ask("More synthetic content.")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(transport.exhaustions, 1)

    def test_contradictory_prose_is_rejected(self):
        self.assertEqual(decode_answer("v-correct is old; use v-wrong instead."), (None, False, False))
        self.assertEqual(decode_answer("Do not use v-correct."), (None, False, False))
        self.assertEqual(decode_answer("v-correct NOT FOUND"), (None, False, False))

    def test_strict_plain_and_structured_answers(self):
        self.assertEqual(decode_answer(" v-correct\n"), ("v-correct", False, True))
        self.assertEqual(decode_answer("NOT_FOUND"), (None, True, True))
        self.assertEqual(decode_answer('{"status":"found","value":"v-correct"}'), ("v-correct", False, True))
        self.assertEqual(decode_answer('{"status":"not_found","value":null}'), (None, True, True))
        for text in ['{"status":"not_found","value":"v-correct"}', '["v-correct"]', None]:
            self.assertFalse(decode_answer(text)[2])

    def test_errors_stay_in_active_denominator(self):
        ledger = Ledger()
        for event in generate(1, 2, 2, 300):
            ledger.apply(event)
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        class Provider:
            def handle_tool_call(self, name, args):
                if "f-00000" in args["query"]:
                    return {"answer": ledger.latest["f-00000"].value}
                raise RuntimeError("Synthetic retrieval failure")
        score = query_metrics(adapter, Provider(), ledger, 2)
        self.assertEqual((score["active_correct"], score["active_total"]), (1, 2))
        self.assertEqual((score["active_answered_correct"], score["active_answered_total"]), (1, 1))
        self.assertEqual(score["errors"], 3)

    def test_duplicate_json_values_and_wire_fields_are_rejected(self):
        self.assertFalse(decode_answer(
            '{"status":"found","value":"v-wrong","value":"v-correct"}')[2])
        ledger = Ledger()
        ledger.apply(generate(1, 1, 1, 300)[0])
        adapter = Harness(None, Path("/private/tmp/synthetic"), Transport())
        class Provider:
            def handle_tool_call(self, *args):
                return '{"answer":"v-wrong","answer":"' + ledger.latest["f-00000"].value + '"}'
        score = query_metrics(adapter, Provider(), ledger, 2)
        self.assertEqual(score["active_correct"], 0)
        self.assertEqual(score["errors"], 3)


class CliTests(unittest.TestCase):
    def test_duplicate_seeds_are_rejected(self):
        with self.assertRaises(Exception):
            simulator.parse_seeds("1,1")

    def test_limits_are_validated(self):
        simulator.validate(valid_config())
        for change in [
            {"turns": -1}, {"facts": 1000, "turns": 100}, {"workers": 9},
            {"timeout": 3601}, {"inference": "surplus", "preset": "endurance"},
            {"payload_chars": 9000}, {"max_api_calls": 101},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                simulator.validate(valid_config(**change))

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            existing = Path(td) / "existing"
            existing.mkdir()
            marker = existing / "user-file"
            marker.write_text("preserve")
            with self.assertRaises(ValueError):
                simulator.new_output(str(existing))
            self.assertEqual(marker.read_text(), "preserve")

    def test_shape_limits_cannot_bypass_disk_budget(self):
        with self.assertRaises(ValueError):
            simulator.validate(valid_config(turns=40000, shape={"length_samples": [999999]}))

    def test_timeout_keeps_checkpoint_and_stops_owned_worker(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "worker"
            result = simulator.owned_job(delayed_worker, (str(folder),), folder / "result.json", 1)
            self.assertEqual(result["status"], "timed_out")
            self.assertEqual(json.loads((folder / "result.json").read_text())["status"], "timed_out")

    def test_comparison_requires_matching_workload(self):
        with self.assertRaises(ValueError):
            compare_runs(fixture_run("a"), fixture_run("b"))
        result = compare_runs(fixture_run(correct=5), fixture_run(correct=8, policy="rebuild"))
        self.assertAlmostEqual(result["seeds"][0]["delta"]["recall"], .3)
        self.assertTrue(result["index_policy_changed"])

    def test_confounded_settings_require_explicit_permission(self):
        from copy import deepcopy
        left = fixture_run()
        right = deepcopy(left)
        right["config"]["session_every_cycles"] = 1
        with self.assertRaises(ValueError):
            compare_runs(left, right)
        result = compare_runs(left, right, allow_confounded=True)
        self.assertTrue(result["confounded"])
        self.assertIn("session_every_cycles", result["setting_differences"])

    def test_incomplete_and_unaligned_runs_are_rejected(self):
        from copy import deepcopy
        left, right = fixture_run(), deepcopy(fixture_run())
        right["seeds"][0]["status"] = "timed_out"
        with self.assertRaises(ValueError):
            compare_runs(left, right)
        self.assertTrue(compare_runs(left, right, allow_incomplete=True)["partial_comparison"])
        right["seeds"][0]["cycles"][0]["turns"] = 100
        with self.assertRaises(ValueError):
            compare_runs(left, right, allow_incomplete=True)

    def test_different_grading_versions_are_rejected(self):
        right = fixture_run()
        right["seeds"][0]["grading_version"] = 1
        with self.assertRaises(ValueError):
            compare_runs(fixture_run(), right)

    def test_different_dependency_versions_are_rejected(self):
        left, right = fixture_run(), fixture_run()
        left["seeds"][0]["dependency_versions"] = {"pydantic": "2.1"}
        right["seeds"][0]["dependency_versions"] = {"pydantic": "2.2"}
        with self.assertRaises(ValueError):
            compare_runs(left, right)
        self.assertTrue(compare_runs(left, right, allow_confounded=True)["confounded"])

    def test_partial_comparison_uses_only_shared_checkpoint(self):
        from copy import deepcopy
        left, right = fixture_run(), deepcopy(fixture_run())
        early = deepcopy(left["seeds"][0]["cycles"][0])
        early.update(cycle=1, turns=67)
        left["seeds"][0]["cycles"].insert(0, early)
        right["seeds"][0]["cycles"] = [deepcopy(early)]
        right["seeds"][0]["status"] = "timed_out"
        result = compare_runs(left, right, allow_incomplete=True)
        self.assertEqual(result["seeds"][0]["checkpoint_turn"], 67)

    def test_post_restart_critical_failure_cannot_pass(self):
        result = fixture_run()
        result["seeds"][0]["cycles"][0]["retrieval_after_restart"]["critical_correct"] = 0
        self.assertFalse(simulator.acceptance_gates(result)["critical_recall_complete"])

    def test_early_failure_is_not_a_matching_workload(self):
        with self.assertRaises(ValueError):
            compare_runs(fixture_run(None), fixture_run(None))

    def test_undefined_ratio_is_not_false_zero(self):
        self.assertIsNone(ratio(0, 0))

    def test_failed_chaos_job_cannot_pass_gate(self):
        result = fixture_run()
        result["chaos"] = {"status": "error", "passed": 0, "total": 0}
        self.assertFalse(simulator.acceptance_gates(result)["chaos_checks_pass"])

    def test_unqueried_critical_facts_cannot_pass_gate(self):
        result = fixture_run()
        result["seeds"][0]["cycles"][0]["retrieval"]["critical_available"] = 2
        self.assertFalse(simulator.acceptance_gates(result)["critical_recall_complete"])

    @unittest.skipUnless(
        Path(simulator.DEFAULT_CORE).is_dir() and Path(simulator.DEFAULT_PLUGIN).is_file(),
        "Integration replay needs the installed Hermes core and Uniichat plugin")
    def test_small_actual_component_run_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "integration"
            command = [
                sys.executable, "-B", str(ROOT / "simulator.py"), "run", "--seeds", "7",
                "--facts", "10", "--turns", "24", "--cycles", "2", "--max-probes", "10",
                "--payload-chars", "300", "--timeout", "45", "--output", str(output),
                "--session-every-cycles", "1",
            ]
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=65)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = json.loads((output / "run.json").read_text())
            self.assertEqual(result["seeds"][0]["status"], "complete")
            self.assertEqual(len(result["seeds"][0]["cycles"]), 2)
            self.assertEqual(result["seeds"][0]["cycles"][1]["session_id"], "synthetic-seed-7-session-1")
            starts = set()
            for row in result["seeds"][0]["cycles"]:
                self.assertTrue(row["restart_state_matches"])
                self.assertTrue(row["restart_raw_state_matches"])
                self.assertNotEqual(row["restart"]["checkpoint_start_token"], row["restart"]["verification_start_token"])
                starts.update((row["restart"]["checkpoint_start_token"], row["restart"]["verification_start_token"]))
            self.assertEqual(len(starts), 4)
            self.assertEqual(result["seeds"][0]["inference_calls"], [])
            self.assertTrue((output / "ground-truth.jsonl").exists() is False)
            self.assertTrue((output / "seed-7" / "ground-truth.jsonl").exists())
            self.assertTrue((output / "curve.csv").exists())
            self.assertIn("<svg", (output / "report.html").read_text())
            self.assertFalse(list((output / "seed-7").glob("scratch-*")))

    @unittest.skipUnless(
        Path(simulator.DEFAULT_CORE).is_dir() and Path(simulator.DEFAULT_PLUGIN).is_file(),
        "Object-mode integration needs installed targets")
    def test_object_control_uses_same_interpreter(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "objects"
            process = subprocess.run([
                sys.executable, "-B", str(ROOT / "simulator.py"), "run",
                "--seeds", "1", "--facts", "10", "--turns", "20", "--cycles", "1",
                "--restart-mode", "objects", "--output", str(output),
            ], cwd=ROOT, capture_output=True, text=True, timeout=45)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            row = json.loads((output / "run.json").read_text())["seeds"][0]["cycles"][0]
            self.assertEqual(row["restart"]["checkpoint_start_token"], row["restart"]["verification_start_token"])
            self.assertTrue(row["restart_state_matches"])


if __name__ == "__main__":
    unittest.main()
