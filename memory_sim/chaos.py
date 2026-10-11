"""Fault scenarios using real target code and disposable stores."""
from __future__ import annotations

import builtins
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from .runner import atomic_json
from .runtime import Harness, Transport, host


def writer_process(config, scratch, shared, barrier, count, worker):
    with host(config, Path(scratch)) as (module, _):
        store = module.UniichatStore(Path(shared), summarize=False)
        barrier.wait(timeout=15)
        ids = []
        for i in range(count):
            ids.append(store.append("user", f"writer-{worker}-record-{i}"))
            time.sleep(.001)
        atomic_json(Path(shared) / f"writer-{worker}.json", {"ids": ids})


def crash_index_process(config, scratch, shared):
    import os
    with host(config, Path(scratch)) as (module, _):
        store = module.UniichatStore(Path(shared), summarize=False)
        real_open = builtins.open

        class CrashWriter:
            def __init__(self, path, mode, *args, **kwargs):
                self.file = real_open(path, mode, *args, **kwargs)
            def __enter__(self):
                return self
            def write(self, text):
                self.file.write(text[:10])
                self.file.flush()
                os._exit(73)
            def __exit__(self, *args):
                self.file.close()

        def open_hook(path, mode="r", *args, **kwargs):
            if Path(path) == store._tree_path() and mode == "w":
                return CrashWriter(path, mode, *args, **kwargs)
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=open_hook):
            store.build_tree(force=True)


def join_owned(processes, timeout=20):
    deadline = time.monotonic() + timeout
    for process in processes:
        process.join(max(0, deadline - time.monotonic()))
    timed_out = any(process.is_alive() for process in processes)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    return timed_out


def chaos_run(config, output):
    output = Path(output)
    output.mkdir()
    checks = []
    with tempfile.TemporaryDirectory(prefix="scratch-", dir=output) as td:
        scratch = Path(td)
        with host({**config, "inference": "offline"}, scratch / "host") as (module, home):
            harness = Harness(module, home, Transport())
            with harness.adapters():
                from .regressions import regression_checks
                checks.extend(regression_checks(module, scratch / "regressions"))

                from agent.memory_manager import MemoryManager
                profiles, managers = [], []
                try:
                    for label in ("a", "b"):
                        profile = module.UniichatMemoryProvider({"summarize": False})
                        manager = MemoryManager()
                        manager.add_provider(profile)
                        manager.initialize_all(
                            f"profile-{label}-session", hermes_home=str(home.parent / f"profile-{label}"),
                            platform="cli", agent_context="primary")
                        profiles.append(profile)
                        managers.append(manager)
                        (profile.store.root / "tree").mkdir(parents=True, exist_ok=True)
                        manager.sync_all(
                            f"SYNTHETIC-PROFILE-{label}-ONLY", "Acknowledged.",
                            session_id=f"profile-{label}-session")
                        if not manager.flush_pending(timeout=10):
                            raise RuntimeError("Profile isolation sync did not drain")
                    leaked = [
                        any(f"SYNTHETIC-PROFILE-{other}-ONLY" in row["text"]
                            for row in profile.store.messages())
                        for profile, other in zip(profiles, ("b", "a"))
                    ]
                    checks.append({
                        "name": "two_profile_default_stores_are_isolated",
                        "pass": profiles[0].store.root != profiles[1].store.root and not any(leaked),
                        "details": {"distinct_roots": profiles[0].store.root != profiles[1].store.root,
                                    "cross_profile_evidence_visible": any(leaked)},
                    })
                finally:
                    for manager in managers:
                        manager.shutdown_all()

                shared = scratch / "shared"
                store = module.UniichatStore(shared, summarize=False)
                store.append("note", "synthetic concurrency seed")
                ctx = multiprocessing.get_context("spawn")
                barrier = ctx.Barrier(config["workers"])
                processes = [ctx.Process(
                    target=writer_process,
                    args=({**config, "inference": "offline"}, str(scratch / f"child-{i}"),
                          str(shared), barrier, 25, i),
                ) for i in range(config["workers"])]
                for process in processes:
                    process.start()
                timed_out = join_owned(processes, 45)
                raw = store.messages()
                committed = [row for row in raw if row["kind"] == "user"]
                ids = [row["i"] for row in committed]
                checks.append({
                    "name": "multiprocess_writers", "pass": not timed_out and
                    all(process.exitcode == 0 for process in processes) and
                    len(committed) == config["workers"] * 25 and len(ids) == len(set(ids)),
                    "details": {"workers": config["workers"], "expected_records": config["workers"] * 25,
                                "committed_records": len(committed), "duplicate_ids": len(ids) - len(set(ids)),
                                "timed_out": timed_out},
                })

                crash_store = module.UniichatStore(scratch / "crash-store", summarize=False)
                (crash_store.root / "tree").mkdir(parents=True)
                for i in range(8):
                    crash_store.append("user", f"before-crash-{i}")
                crash_store.build_tree(force=True)
                process = ctx.Process(
                    target=crash_index_process,
                    args=({**config, "inference": "offline"}, str(scratch / "crash-child"), str(crash_store.root)),
                )
                process.start()
                timeout = join_owned([process])
                reopened = module.UniichatStore(crash_store.root, summarize=False)
                node_count = len(reopened._load_tree())
                checks.append({
                    "name": "process_crash_during_index_write",
                    "pass": not timeout and process.exitcode == 73 and node_count >= 8,
                    "details": {"child_exit": process.exitcode, "fault_injected": process.exitcode == 73,
                                "reopened_nodes": node_count, "raw_records_survived": len(reopened.messages()) == 8},
                })

                damaged = module.UniichatStore(scratch / "malformed", summarize=False)
                damaged.append("user", "valid-before")
                log = next((damaged.root / "main").glob("*.jsonl"))
                with log.open("a") as stream:
                    stream.write("{synthetic-bad-json}\n")
                damaged.append("user", "valid-after")
                recovered = [row["text"] for row in damaged.messages()]
                checks.append({
                    "name": "malformed_record_preserves_valid_neighbors",
                    "pass": recovered == ["valid-before", "valid-after"],
                    "details": {"valid_records_recovered": len(recovered), "invalid_records_injected": 1},
                })

                for error_name, error in [
                    ("summary_timeout_aborts_losslessly", TimeoutError("Synthetic timeout")),
                    ("malformed_summary_response_aborts_losslessly", ValueError("Synthetic malformed response")),
                ]:
                    compressor = harness.compressor(config["context_length"])
                    history = [{"role": "user" if i % 2 == 0 else "assistant",
                                "content": f"FACT[f-{i:05d}]=v-{i} REV[{i + 1}]\n" +
                                "Synthetic filler. " * 1000} for i in range(30)]
                    before = json.dumps(history, sort_keys=True)
                    with patch.object(compressor, "_call_summary_llm", side_effect=error):
                        after = compressor.compress(history, current_tokens=150000, force=True)
                    checks.append({"name": error_name, "pass": json.dumps(after, sort_keys=True) == before,
                                   "details": {"policy": "abort_on_summary_failure=true"}})
    result = {
        "status": "complete",
        "method": "Offline faults in disposable stores; actual spawned writer/crash processes. "
                  "Thread/context and host integration checks reuse the original regression suite.",
        "checks": checks, "passed": sum(check["pass"] for check in checks), "total": len(checks),
        "offline": True,
    }
    atomic_json(output / "result.json", result)
    return result
