import contextlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import simulator
from memory_sim.pipeline import cycle_worker, restart_worker
from memory_sim.processes import owned_job
from memory_sim.runtime import (
    BudgetExceeded, Harness, Transport, _TRANSPORT_ALLOWED, host, network_guard,
    recover_inference_calls,
)
from memory_sim.report import compare_runs
from test_simulator import fixture_run


class Response:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return json.dumps({"choices": [{"finish_reason": "stop",
                                      "message": {"content": "v-test"}}]}).encode()


def killed_request_worker(folder):
    output = Path(folder) / "result.json"
    os.environ["SURPLUS_API_KEY"] = "synthetic-key"
    transport = Transport("surplus", 1, output.with_suffix(".calls.json"))
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: os._exit(73)):
        transport.ask("synthetic source only")


class HardeningTests(unittest.TestCase):
    def test_concurrent_requests_reserve_budget_before_io(self):
        entered, release = threading.Event(), threading.Event()
        def request(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return Response()
        with patch.dict(os.environ, {"SURPLUS_API_KEY": "synthetic-key"}), \
                patch("urllib.request.urlopen", side_effect=request) as http:
            transport = Transport("surplus", 1)
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(transport.ask, "synthetic first")
                try:
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(BudgetExceeded):
                        pool.submit(transport.ask, "synthetic second").result(timeout=3)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3), "v-test")
            self.assertEqual(http.call_count, 1)
            self.assertEqual(len(transport.calls), 1)

    def test_journal_contains_reservation_before_http_and_no_payload(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {"SURPLUS_API_KEY": "synthetic-key"}):
            path = Path(td) / "result.calls.json"
            def request(*args, **kwargs):
                self.assertEqual(json.loads(path.read_text())[0]["status"], "started")
                return Response()
            with patch("urllib.request.urlopen", side_effect=request):
                transport = Transport("surplus", 1, path)
                transport.ask("private-synthetic-payload")
            text = path.read_text()
            self.assertNotIn("private-synthetic-payload", text)
            self.assertNotIn("synthetic-key", text)
            self.assertEqual(json.loads(text)[0]["status"], "finished")

    def test_killed_worker_preserves_inflight_reservation(self):
        with tempfile.TemporaryDirectory() as td:
            result = owned_job(killed_request_worker, (td,), Path(td) / "result.json", 10)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["worker_exitcode"], 73)
            self.assertEqual(len(result["inference_calls"]), 1)
            self.assertEqual(result["inference_calls"][0]["status"], "started")

    def test_seed_recovery_collects_all_phase_journals(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "checkpoints"
            folder.mkdir()
            (folder / "cycle-1.calls.json").write_text('[{"stage":"recall","status":"finished"}]')
            (folder / "restart-1.calls.json").write_text('[{"stage":"recall","status":"started"}]')
            calls = recover_inference_calls(Path(td) / "result.json")
            self.assertEqual(len(calls), 2)
            self.assertEqual({c["worker_phase"] for c in calls}, {"cycle-1", "restart-1"})

    def test_corrupt_journal_is_not_silently_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "result.json"
            path.with_suffix(".calls.json").write_text('{"not":"a list"}')
            with self.assertRaises(ValueError):
                recover_inference_calls(path)

    def test_failed_workers_keep_usage(self):
        for worker in (cycle_worker, restart_worker):
            with self.subTest(worker=worker.__name__), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                source = root / "source.jsonl"
                source.write_text("")
                calls = [{"stage": "recall", "ok": True}, {"stage": "recall", "ok": False}]
                harness = SimpleNamespace(transport=SimpleNamespace(calls=calls))
                provider = SimpleNamespace(store=SimpleNamespace(
                    root=root, messages=lambda: (_ for _ in ()).throw(RuntimeError())))
                manager = SimpleNamespace(flush_pending=lambda **kwargs: False)
                db = SimpleNamespace(
                    get_messages_as_conversation=lambda sid: [])
                @contextlib.contextmanager
                def fake_runtime(*args):
                    yield harness, provider, manager, db
                with patch("memory_sim.pipeline.runtime", fake_runtime), \
                        patch.dict(sys.modules, {"agent.model_metadata": SimpleNamespace(
                            estimate_messages_tokens_rough=lambda _: 0)}):
                    output = root / "result.json"
                    args = ({"turns": 1}, td, str(source), "synthetic", [], 3, str(output))
                    worker(*(args if worker is cycle_worker else (
                        {"turns": 1}, td, "synthetic", [], 3, str(output))))
                result = json.loads(output.read_text())
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["inference_calls"], calls)

    def test_offline_tcp_udp_and_dns_paths_are_blocked(self):
        with network_guard({"inference": "offline"}):
            with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
                for action in (
                    lambda: tcp.connect(("127.0.0.1", 1)),
                    lambda: tcp.connect_ex(("127.0.0.1", 1)),
                    lambda: udp.sendto(b"synthetic", ("127.0.0.1", 1)),
                    lambda: socket.getaddrinfo("synthetic.invalid", 443),
                    lambda: socket.gethostbyname("synthetic.invalid"),
                    lambda: socket.gethostbyaddr("127.0.0.1"),
                ):
                    with self.subTest(action=action), self.assertRaises(RuntimeError):
                        action()

    def test_network_permission_is_transport_scoped(self):
        with patch("socket.getaddrinfo", return_value=[]) as dns:
            with network_guard({"inference": "surplus"}):
                with self.assertRaises(RuntimeError):
                    socket.getaddrinfo("synthetic.invalid", 443)
                token = _TRANSPORT_ALLOWED.set(True)
                try:
                    self.assertEqual(socket.getaddrinfo("synthetic.invalid", 443), [])
                finally:
                    _TRANSPORT_ALLOWED.reset(token)
            self.assertEqual(dns.call_count, 1)

    def test_host_restores_import_paths_and_environment(self):
        original_path = sys.path
        original_env = dict(os.environ)
        @contextlib.contextmanager
        def bound(*args):
            sys.path.insert(0, "/synthetic/core")
            os.environ["SYNTHETIC_SCOPE"] = "inside"
            yield None, None
        with tempfile.TemporaryDirectory() as td, patch("memory_sim.runtime._bound_host", bound):
            with host({}, Path(td)):
                self.assertIsNot(sys.path, original_path)
                self.assertEqual(os.environ["SYNTHETIC_SCOPE"], "inside")
        self.assertIs(sys.path, original_path)
        self.assertEqual(dict(os.environ), original_env)

    def test_interrupted_request_keeps_budget_and_error_metadata(self):
        with patch.dict(os.environ, {"SURPLUS_API_KEY": "synthetic-key"}), \
                patch("urllib.request.urlopen", side_effect=KeyboardInterrupt):
            transport = Transport("surplus", 1)
            with self.assertRaises(KeyboardInterrupt):
                transport.ask("synthetic")
            self.assertEqual(transport.calls[0]["error_type"], "KeyboardInterrupt")
            self.assertFalse(transport.calls[0]["ok"])
            self.assertFalse(_TRANSPORT_ALLOWED.get())
            with self.assertRaises(BudgetExceeded):
                transport.ask("synthetic")

    def test_adapter_threads_finish_before_patches_restore(self):
        module = SimpleNamespace(
            threading=threading, time=time, _chat=lambda: None, _llm_summarize=lambda: None,
            _agy_verify=lambda: None, _summarizer_key=lambda: "original")
        harness = Harness(module, Path("/private/tmp/synthetic"), Transport())
        release, completed = threading.Event(), threading.Event()
        values = []
        def callback():
            release.wait(3)
            values.append(module._summarizer_key())
            completed.set()
        timer = threading.Timer(.15, release.set)
        try:
            with harness.adapters():
                thread = module.threading.Thread(target=callback)
                thread.start()
                timer.start()
            self.assertTrue(completed.is_set())
            self.assertEqual(values, [None])
            self.assertIs(module.threading, threading)
        finally:
            release.set()
            timer.join(3)
            thread.join(3)

    def test_complete_but_earlier_shared_checkpoint_is_labeled_partial(self):
        left, right = fixture_run(), fixture_run()
        early = deepcopy(left["seeds"][0]["cycles"][0])
        early.update(turns=67, cycle=1)
        left["seeds"][0]["cycles"].insert(0, deepcopy(early))
        right["seeds"][0]["cycles"].insert(0, deepcopy(early))
        right["seeds"][0]["cycles"][-1]["turns"] = 201
        result = compare_runs(left, right, allow_incomplete=True)
        self.assertEqual(result["seeds"][0]["checkpoint_turn"], 67)
        self.assertTrue(result["partial_comparison"])

    def test_duplicate_checkpoint_positions_are_rejected(self):
        left, right = fixture_run(), fixture_run()
        right["seeds"][0]["cycles"].append(deepcopy(right["seeds"][0]["cycles"][0]))
        with self.assertRaises(ValueError):
            compare_runs(left, right, allow_incomplete=True)

    def test_missing_environment_requires_confounded_override(self):
        for field in ("python_version", "dependency_versions"):
            with self.subTest(field=field):
                left, right = fixture_run(), fixture_run()
                left["seeds"][0].pop(field)
                right["seeds"][0].pop(field)
                with self.assertRaises(ValueError):
                    compare_runs(left, right)
                self.assertTrue(compare_runs(left, right, allow_confounded=True)["confounded"])

    def test_noop_or_budget_aborted_compaction_cannot_pass(self):
        result = fixture_run()
        row = result["seeds"][0]["cycles"][0]
        self.assertFalse(simulator.acceptance_gates(result)["all_compactions_committed"])
        row.update(compression_committed=True, compression_budget_exhausted=False)
        self.assertTrue(simulator.acceptance_gates(result)["all_compactions_committed"])
        row["compression_budget_exhausted"] = True
        self.assertFalse(simulator.acceptance_gates(result)["all_compactions_committed"])

    def test_process_restart_requires_distinct_nonempty_tokens(self):
        result = fixture_run()
        row = result["seeds"][0]["cycles"][0]
        row["restart"] = {"mode": "process", "checkpoint_start_token": "same",
                          "verification_start_token": "same"}
        self.assertFalse(simulator.acceptance_gates(result)["restart_identity_matches_mode"])
        row["restart"]["verification_start_token"] = "different"
        self.assertTrue(simulator.acceptance_gates(result)["restart_identity_matches_mode"])
        row["restart"]["mode"] = "objects"
        self.assertFalse(simulator.acceptance_gates(result)["restart_identity_matches_mode"])
        row["restart"]["verification_start_token"] = "same"
        result["config"]["restart_mode"] = "objects"
        self.assertTrue(simulator.acceptance_gates(result)["restart_identity_matches_mode"])

    def test_reused_interpreter_across_cycles_is_rejected(self):
        result = fixture_run()
        row = result["seeds"][0]["cycles"][0]
        row["restart"] = {"mode": "process", "checkpoint_start_token": "first",
                          "verification_start_token": "second"}
        result["seeds"][0]["cycles"].append(deepcopy(row))
        self.assertFalse(simulator.acceptance_gates(result)["restart_identity_matches_mode"])
