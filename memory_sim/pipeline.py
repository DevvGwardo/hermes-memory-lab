"""Persistent source replay, fresh checkpoint workers, and independent grading."""
from __future__ import annotations

import contextlib
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

from .grading import collect_answers, grade_answers, probe_keys
from .processes import owned_job
from .runtime import BudgetExceeded, Harness, Transport, host, recover_inference_calls
from .workload import Event, Ledger, WORKLOAD_VERSION, iter_generate, observed_state

GRADING_VERSION = 2
PROCESS_START_TOKEN = uuid.uuid4().hex


def state(texts):
    return observed_state("\n".join(text or "" for text in texts))


def normalize_state(value):
    return {key: tuple(item) for key, item in value.items()}


def check_disk(root, config):
    from .runner import disk_bytes
    if disk_bytes(root) > config["max_disk_mb"] * 1024**2:
        raise BudgetExceeded("Owned run directory disk budget exceeded")


@contextlib.contextmanager
def runtime(config, scratch, sid, remaining_calls, journal_path=None):
    with host(config, Path(scratch)) as (module, home):
        harness = Harness(module, home, Transport(config["inference"], remaining_calls, journal_path))
        with harness.adapters():
            from hermes_state import SessionDB
            provider, manager = harness.provider(sid)
            db = None
            try:
                db = SessionDB(db_path=home / "state.db")
                if db.get_session(sid) is None:
                    db.create_session(sid, source="cli")
                yield harness, provider, manager, db
            finally:
                manager.shutdown_all()
                if db is not None:
                    db.close()


def identity():
    return {"worker_pid": os.getpid(), "process_start_token": PROCESS_START_TOKEN}


def source_records(path):
    with Path(path).open() as stream:
        for line in stream:
            yield json.loads(line)


def cycle_worker(config, scratch, source_path, sid, keys, remaining_calls, output):
    from .runner import atomic_json, disk_bytes
    result = {"status": "running", **identity()}
    output = Path(output)
    harness = None
    atomic_json(output, result)
    try:
        with runtime(config, scratch, sid, remaining_calls, output.with_suffix(".calls.json")) as (harness, provider, manager, db):
            from agent.model_metadata import estimate_messages_tokens_rough
            history = db.get_messages_as_conversation(sid)
            (provider.store.root / "tree").mkdir(parents=True, exist_ok=True)
            for record in source_records(source_path):
                db.append_messages_batch(sid, record["messages"])
                history.extend(record["messages"])
                manager.sync_all(
                    record["user"], record["assistant"], session_id=sid, messages=record["messages"])
                if record["turn"] + 1 == min(10, config["turns"]):
                    if not manager.flush_pending(timeout=30):
                        raise RuntimeError("Memory sync did not drain")
                    provider.store.build_tree(force=True)
                if (record["turn"] + 1) % 100 == 0:
                    check_disk(output.parent.parent, config)
            if not manager.flush_pending(timeout=30):
                raise RuntimeError("Memory sync did not drain")
            raw = provider.store.messages()
            ids = [row["i"] for row in raw]
            before = state(row.get("content") for row in history)
            tree_start = time.monotonic()
            index_error = None
            try:
                if config["index_policy"] == "rebuild":
                    provider.store.build_tree(force=True)
                else:
                    provider.prefetch("Synthetic memory health check")
                indexed_ids = {i for level, i in provider.store._load_tree() if level == 0}
                menu = observed_state(provider.store.index_menu())
            except Exception as exc:
                indexed_ids, menu, index_error = set(), {}, type(exc).__name__
            index_seconds = time.monotonic() - tree_start
            observations = collect_answers(harness, provider, keys)
            compressor = harness.compressor(config["context_length"])
            tokens_before = estimate_messages_tokens_rough(history)
            compression_start = time.monotonic()
            exhaustion_before = harness.transport.exhaustions
            from agent.conversation_compression import _direct_messages_for_pre_compress_memory
            memory_context = manager.on_pre_compress(
                history, evidence_messages=_direct_messages_for_pre_compress_memory(history))
            compacted = compressor.compress(
                history, current_tokens=tokens_before, force=True, memory_context=memory_context)
            compression_seconds = time.monotonic() - compression_start
            after = state(row.get("content") for row in compacted)
            changed = len(compacted) < len(history)
            if changed:
                db.archive_and_compact(sid, compacted)
            result.update(
                status="complete", raw_state=state(row["text"] for row in raw),
                menu_state=menu, context_before_state=before, context_after_state=after,
                archive_state=state(row[0] for row in db._conn.execute(
                    "SELECT content FROM messages WHERE role IN ('user','tool') ORDER BY id")),
                observations=observations, records=len(raw),
                indexed_records=len(set(ids) & indexed_ids), duplicate_ids=len(ids) - len(set(ids)),
                index_error=index_error, index_seconds=index_seconds,
                compression_seconds=compression_seconds, messages_before=len(history),
                messages_after=len(compacted), tokens_before_rough=tokens_before,
                tokens_after_rough=estimate_messages_tokens_rough(compacted),
                compression_committed=changed,
                compression_budget_exhausted=harness.transport.exhaustions > exhaustion_before,
                inference_calls=harness.transport.calls, temporary_bytes=disk_bytes(Path(scratch)),
                index_bytes=disk_bytes(provider.store.root / "tree"),
            )
    except Exception as exc:
        result.update(status="budget_exhausted" if isinstance(exc, BudgetExceeded) else "error",
                      error_type=type(exc).__name__)
    finally:
        if harness is not None:
            result["inference_calls"] = harness.transport.calls
        atomic_json(output, result)


def restart_worker(config, scratch, sid, keys, remaining_calls, output):
    from .runner import atomic_json
    result = {"status": "running", **identity()}
    output = Path(output)
    harness = None
    atomic_json(output, result)
    try:
        with runtime(config, scratch, sid, remaining_calls, output.with_suffix(".calls.json")) as (harness, provider, manager, db):
            result.update(
                status="complete",
                context_state=state(row.get("content") for row in db.get_messages_as_conversation(sid)),
                raw_state=state(row["text"] for row in provider.store.messages()),
                observations=collect_answers(harness, provider, keys),
                inference_calls=harness.transport.calls,
            )
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__)
    finally:
        if harness is not None:
            result["inference_calls"] = harness.transport.calls
        atomic_json(output, result)


def run_worker(config, target, args, output, remaining_time):
    from .runner import atomic_json
    if config["restart_mode"] == "process":
        return owned_job(target, args, output, max(.1, remaining_time), isolate_group=False)
    target(*args)
    result = json.loads(Path(output).read_text())
    calls = recover_inference_calls(output)
    if calls is not None:
        result["inference_calls"] = calls
    atomic_json(Path(output), result)
    return result


def write_workload(config, seed, output):
    digest = hashlib.sha256()
    with (output / "workload.jsonl").open("w") as source, \
            (output / "ground-truth.jsonl").open("w") as truth:
        for event in iter_generate(
                seed, config["facts"], config["turns"], config["payload_chars"], config.get("shape")):
            line = json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n"
            digest.update(line.encode())
            source.write(line)
            truth.write(json.dumps({
                "turn": event.turn, "key": event.key, "value": event.value,
                "revision": event.turn + 1, "group": event.group, "critical": event.critical,
            }) + "\n")
            if (event.turn + 1) % 100 == 0:
                source.flush()
                truth.flush()
                check_disk(output, config)
    return digest.hexdigest()


def source_fingerprints(config):
    root = Path(__file__).resolve().parent
    paths = [root.parent / "simulator.py", *sorted(root.glob("*.py"))]
    harness = hashlib.sha256()
    for path in paths:
        harness.update(path.name.encode())
        harness.update(path.read_bytes())
    core = Path(config["core_root"])
    modules = [
        "agent/context_compressor.py", "agent/memory_manager.py", "agent/memory_provider.py",
        "hermes_state.py", "hermes_state_messages.py", "hermes_state_sessions.py",
    ]
    dependencies = {}
    for package in ("pydantic", "litellm", "openai", "httpx"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    return {
        "harness_sha256": harness.hexdigest(),
        "plugin_sha256": hashlib.sha256(Path(config["plugin"]).read_bytes()).hexdigest(),
        "core_sources_sha256": {name: hashlib.sha256((core / name).read_bytes()).hexdigest()
                               for name in modules if (core / name).exists()},
        "python_version": sys.version,
        "dependency_versions": dependencies,
    }


def seed_run(config, seed, output):
    from .runner import atomic_json
    output = Path(output)
    output.mkdir()
    (output / "checkpoints").mkdir()
    config = {**config, "restart_mode": config.get("restart_mode", "process")}
    started = time.monotonic()
    result = {
        "seed": seed, "status": "running", "cycles": [], "config": config, **identity(),
        "workload_version": WORKLOAD_VERSION, "grading_version": GRADING_VERSION,
        "inference_calls": [],
    }
    atomic_json(output / "result.json", result)
    ledger = Ledger()
    try:
        result.update(source_fingerprints(config))
        result["workload_sha256"] = write_workload(config, seed, output)
        atomic_json(output / "result.json", result)
        with tempfile.TemporaryDirectory(prefix="scratch-", dir=output) as td, \
                (output / "workload.jsonl").open() as source, \
                (output / "trace.jsonl").open("w") as trace:
            scratch, session_number = Path(td), 0
            for cycle in range(1, config["cycles"] + 1):
                end = math.ceil(config["turns"] * cycle / config["cycles"])
                beginning = math.ceil(config["turns"] * (cycle - 1) / config["cycles"])
                sid = f"synthetic-seed-{seed}-session-{session_number}"
                input_path = scratch / "source.jsonl"
                with input_path.open("w") as segment:
                    for _ in range(end - beginning):
                        event = Event(**json.loads(next(source)))
                        ledger.apply(event)
                        # No expected values, critical labels or ledger are sent to workers.
                        segment.write(json.dumps({
                            "turn": event.turn, "user": event.user, "assistant": event.assistant,
                            "messages": event.messages,
                        }) + "\n")
                        trace.write(json.dumps({
                            "event": "turn", "turn": event.turn, "session": session_number,
                            "source_messages": len(event.messages), "group": event.group,
                        }) + "\n")
                probe_limit = min(config["max_probes"], 4) if config["inference"] != "offline" else config["max_probes"]
                keys = probe_keys(ledger, probe_limit)
                remaining_calls = max(0, config["max_api_calls"] - len(result["inference_calls"]))
                checkpoint = output / "checkpoints" / f"cycle-{cycle}.json"
                worker = run_worker(
                    config, cycle_worker,
                    (config, str(scratch), str(input_path), sid, keys, remaining_calls, str(checkpoint)),
                    checkpoint, config["timeout"] - (time.monotonic() - started))
                result["inference_calls"].extend(worker.get("inference_calls", []))
                if worker["status"] != "complete":
                    result.update(status=worker["status"], error_type=worker.get("error_type"))
                    break
                restart_path = output / "checkpoints" / f"restart-{cycle}.json"
                remaining_calls = max(0, config["max_api_calls"] - len(result["inference_calls"]))
                restart = run_worker(
                    config, restart_worker,
                    (config, str(scratch), sid, keys, remaining_calls, str(restart_path)),
                    restart_path, config["timeout"] - (time.monotonic() - started))
                result["inference_calls"].extend(restart.get("inference_calls", []))
                if restart["status"] != "complete":
                    result.update(status=restart["status"], error_type=restart.get("error_type"))
                    break
                before, after = normalize_state(worker["context_before_state"]), normalize_state(worker["context_after_state"])
                row = {
                    key: worker[key] for key in [
                        "records", "indexed_records", "duplicate_ids", "index_error", "index_seconds",
                        "compression_seconds", "messages_before", "messages_after", "tokens_before_rough",
                        "tokens_after_rough", "compression_committed", "compression_budget_exhausted",
                        "temporary_bytes", "index_bytes",
                    ]
                }
                row.update(
                    cycle=cycle, turns=end, session=session_number, session_id=sid,
                    saved=ledger.grade_state(normalize_state(worker["raw_state"])),
                    index_visible_facts=ledger.grade_state(normalize_state(worker["menu_state"])),
                    retrieval=grade_answers(worker["observations"], ledger),
                    retrieval_after_restart=grade_answers(restart["observations"], ledger),
                    context_before=ledger.grade_state(before), context_after=ledger.grade_state(after),
                    after_restart=ledger.grade_state(normalize_state(restart["context_state"])),
                    saved_after_restart=ledger.grade_state(normalize_state(restart["raw_state"])),
                    sqlite_archive_control=ledger.grade_state(normalize_state(worker["archive_state"])),
                    restart_state_matches=after == normalize_state(restart["context_state"]),
                    restart_raw_state_matches=normalize_state(worker["raw_state"]) == normalize_state(restart["raw_state"]),
                    restart={
                        "mode": config["restart_mode"], "checkpoint_worker": worker["worker_pid"],
                        "verification_worker": restart["worker_pid"],
                        "checkpoint_start_token": worker["process_start_token"],
                        "verification_start_token": restart["process_start_token"],
                    },
                    facts_lost_this_compaction=sum(
                        before.get(key) == (fact.turn + 1, fact.value) and
                        after.get(key) != (fact.turn + 1, fact.value)
                        for key, fact in ledger.latest.items()),
                )
                result["cycles"].append(row)
                trace.write(json.dumps({
                    "event": "checkpoint", "cycle": cycle, "facts_lost": row["facts_lost_this_compaction"],
                    "worker_pid": worker["worker_pid"], "verification_pid": restart["worker_pid"],
                }) + "\n")
                trace.flush()
                result["elapsed_s"] = time.monotonic() - started
                atomic_json(output / "result.json", result)
                check_disk(output, config)
                if cycle % config["session_every_cycles"] == 0:
                    session_number += 1
            else:
                result["status"] = "complete"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
    except Exception as exc:
        result.update(status="budget_exhausted" if isinstance(exc, BudgetExceeded) else "error",
                      error_type=type(exc).__name__)
    result["elapsed_s"] = time.monotonic() - started
    result["method"] = {
        "target": "Real MemoryManager, Uniichat provider/tool, ContextCompressor and SessionDB.",
        "inference": config["inference"], "index_policy": config["index_policy"],
        "restart": config["restart_mode"],
        "process_scope": "Fresh component interpreter per checkpoint and separate persistence verification; no full agent loop.",
        "ledger": "Grader-only; workers receive source messages and query keys, never expected values.",
        "warm_start": "Tree directory prepared; forced index after the first ten turns.",
        "sessions": "Context resets at configured session boundaries; persistent memory remains.",
        "limits": "Scripted evidence diagnostics, not semantic model accuracy; disk checks are not OS quotas.",
    }
    atomic_json(output / "result.json", result)
    return result
