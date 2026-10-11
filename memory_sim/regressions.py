"""Synthetic behavior contracts, without historical-session or credential readers."""
import builtins
import concurrent.futures
import contextvars
from pathlib import Path
import threading
from unittest.mock import patch


def store(module, root, summarize=False):
    (root / "tree").mkdir(parents=True, exist_ok=True)
    return module.UniichatStore(root, summarize=summarize)


def leaves(store):
    return {i for level, i in store._load_tree() if level == 0}


def regression_checks(module, tmp):
    results = []

    def check(name, fn):
        try:
            passed, details = fn()
        except Exception as exc:
            passed, details = False, {"exception_type": type(exc).__name__}
        results.append({"name": name, "pass": bool(passed), "details": details})

    def cold():
        s = module.UniichatStore(tmp / "cold", summarize=False)
        s.append("user", "First synthetic fact.")
        s.build_tree()
        return len(leaves(s)) == 1, {}

    def roundtrip():
        s = store(module, tmp / "roundtrip")
        for i in range(8):
            s.append("user", f"FACT[round-{i}]=value-{i}")
        s.build_tree(force=True)
        reopened = module.UniichatStore(s.root, summarize=False)
        return len(leaves(reopened)) == 8 and len(reopened.messages()) == 8, {}

    def appended(restart=False):
        s = store(module, tmp / ("restart" if restart else "append"))
        for i in range(8):
            s.append("user", f"initial-{i}")
        s.build_tree(force=True)
        s.append("user", "new fact")
        if restart:
            s = module.UniichatStore(s.root, summarize=False)
        s.build_tree()
        count = len(leaves(s))
        return count == 9, {"log_records": 9, "indexed_records": count}

    def pairs():
        s = store(module, tmp / "pairs", summarize=True)
        with patch.object(module, "_llm_summarize", side_effect=lambda text: text[-100:]):
            for text in ["A" * 512, "B" * 512, "C" * 512]:
                s.append("user", text)
            s.build_tree(force=True)
            s.append("user", "D" * 512)
            s.build_tree(force=True)
        return "D" in s._load_tree()[(1, 2)]["text"], {}

    def profile():
        with patch.object(module.Path, "home", return_value=tmp / "fake-user"):
            p = module.UniichatMemoryProvider({"summarize": False})
            home = tmp / "fake-user" / ".hermes" / "profiles" / "profile-a"
            p.initialize("synthetic-session", hermes_home=str(home))
        respected = p.store.root == home / "uniichat"
        return respected, {"used_supplied_profile_home": respected}

    def provenance():
        p = module.UniichatMemoryProvider({"root": str(tmp / "provenance"), "summarize": False})
        p.initialize("synthetic-session-a")
        p.sync_turn("question", "answer", session_id="synthetic-session-a")
        p.initialize("synthetic-session-b")
        p.sync_turn("another question", "another answer", session_id="synthetic-session-b")
        data = [row for row in p.store.messages() if row["kind"] in ("user", "unii")]
        return all(row.get("session_id") for row in data), {
            "records": len(data), "session_tagged": sum(bool(row.get("session_id")) for row in data)}

    def checkpoint():
        p = module.UniichatMemoryProvider({"root": str(tmp / "checkpoint"), "summarize": False})
        before = len(p.store.messages())
        p.on_pre_compress([{"role": "user", "content": "FACT[checkpoint]=kept"}])
        from agent.memory_provider import PRE_COMPRESS_CHECKPOINT_API_VERSION
        version = p.pre_compress_checkpoint_api_version
        return version >= PRE_COMPRESS_CHECKPOINT_API_VERSION and len(p.store.messages()) > before, {
            "advertised_api": version, "required_api": PRE_COMPRESS_CHECKPOINT_API_VERSION,
            "records_added": len(p.store.messages()) - before}

    def sync_evidence():
        from agent.memory_manager import MemoryManager
        p = module.UniichatMemoryProvider({"root": str(tmp / "tool-evidence"), "summarize": False})
        manager = MemoryManager()
        manager.add_provider(p)
        try:
            manager.sync_all("request", "finished", session_id="synthetic-session", messages=[
                {"role": "user", "content": "request"},
                {"role": "tool", "content": "FACT[tool-only]=evidence"},
                {"role": "assistant", "content": "finished"}])
            manager.flush_pending(timeout=5)
            found = any("tool-only" in row["text"] for row in p.store.messages())
            return found, {"tool_fact_saved": found}
        finally:
            manager.shutdown_all()

    def interrupted():
        s = store(module, tmp / "interrupted")
        for i in range(8):
            s.append("user", f"old-{i}")
        s.build_tree(force=True)
        before = s._tree_path().read_bytes()
        s.append("user", "new record")
        real_open = builtins.open

        class FailingWriter:
            def __init__(self, path, *args, **kwargs):
                self.f = real_open(path, *args, **kwargs)
            def __enter__(self):
                return self
            def write(self, value):
                self.f.write(value[:10])
                self.f.flush()
                raise OSError("Synthetic interrupted index write")
            def __exit__(self, *args):
                self.f.close()

        def injected_open(path, mode="r", *args, **kwargs):
            if Path(path) == s._tree_path() and mode == "w":
                return FailingWriter(path, mode, *args, **kwargs)
            return real_open(path, mode, *args, **kwargs)

        try:
            with patch("builtins.open", side_effect=injected_open):
                s.build_tree(force=True)
        except OSError:
            pass
        preserved = s._tree_path().read_bytes() == before
        return preserved, {"previous_index_preserved": preserved, "raw_log_preserved": len(s.messages()) == 9}

    def concurrency():
        root = tmp / "concurrent"
        root.mkdir()
        a, b = module.UniichatStore(root, False), module.UniichatStore(root, False)
        a.append("note", "seed")
        barrier = threading.Barrier(2)
        real_open = builtins.open

        class BarrierReader:
            def __init__(self, path, *args, **kwargs):
                self.f = real_open(path, *args, **kwargs)
            def __enter__(self):
                return self
            def __iter__(self):
                snapshot = list(self.f)
                barrier.wait(timeout=5)
                return iter(snapshot)
            def __exit__(self, *args):
                self.f.close()

        def injected_open(path, mode="r", *args, **kwargs):
            if mode == "rb" and Path(path).suffix == ".jsonl":
                return BarrierReader(path, mode, *args, **kwargs)
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=injected_open), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(s.append, "user", f"concurrent-{i}") for i, s in enumerate([a, b])]
            ids = [f.result(timeout=10) for f in futures]
        return len(set(ids)) == 2, {"assigned_ids": ids, "raw_records": len(a.messages())}

    def unavailable():
        s = store(module, tmp / "unavailable", summarize=True)
        for i in range(8):
            s.append("user", "Background. " * 50 + f"FACT[unavailable-{i}]=tail-{i}")
        with patch.object(module, "_llm_summarize", return_value=None):
            s.build_tree(force=True)
        return len(s.messages()) == 8 and len(leaves(s)) == 8, {"raw_records": len(s.messages())}

    def thread_scope():
        marker = contextvars.ContextVar("eval_profile", default="unbound")
        token = marker.set("profile-a")
        observed = []
        p = module.UniichatMemoryProvider({"root": str(tmp / "thread-scope"), "summarize": False})
        try:
            with patch.object(p.store, "build_tree", side_effect=lambda: observed.append(marker.get()) or 0):
                p._ensure_tree_async()
                for thread in list(threading.enumerate()):
                    if thread.name == "uniichat-tree":
                        thread.join(timeout=5)
            return observed == ["profile-a"], {"worker_scope": observed}
        finally:
            marker.reset(token)

    for name, fn in [
        ("fresh_store_initialization", cold), ("forced_build_and_restart", roundtrip),
        ("append_refreshes_index", appended), ("restart_refreshes_stale_index", lambda: appended(True)),
        ("odd_to_even_summary_cache", pairs), ("profile_home_respected", profile),
        ("shared_records_have_session_provenance", provenance), ("pre_compaction_checkpoint", checkpoint),
        ("tool_only_evidence_synced", sync_evidence), ("interrupted_index_write_is_atomic", interrupted),
        ("concurrent_instances_assign_unique_ids", concurrency), ("unavailable_summarizer_preserves_raw_log", unavailable),
        ("background_thread_preserves_profile_scope", thread_scope),
    ]:
        check(name, fn)
    return results
