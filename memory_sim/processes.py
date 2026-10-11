"""Bounded, owned processes with cancellation and descendant cleanup."""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import threading
import time


class JobCancelled(KeyboardInterrupt):
    pass


@contextlib.contextmanager
def cancellation_signals():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    def cancel(signum, frame):
        raise JobCancelled()
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def job_entry(target, args, isolate_group, ready):
    if isolate_group and hasattr(os, "setsid"):
        os.setsid()
    ready.set()
    target(*args)


def stop_owned(process, isolate_group, ready):
    group = isolate_group and ready.is_set() and hasattr(os, "killpg")
    if group:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(5)
    if group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.kill()
    process.join()


def owned_job(target, args, result_path, timeout, *, isolate_group=True):
    """Only stop a process handle/group created here; never match names or argv."""
    from .runner import atomic_json
    from .runtime import recover_inference_calls
    result_path = Path(result_path)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=job_entry, args=(target, args, isolate_group, ready))
    reason = None
    started = time.monotonic()
    with cancellation_signals():
        try:
            process.start()
            process.join(timeout)
            if process.is_alive():
                reason = "timed_out"
                stop_owned(process, isolate_group, ready)
            else:
                stop_owned(process, isolate_group, ready)
        except KeyboardInterrupt:
            reason = "interrupted"
            # Ignore repeat cancellation while disposing of our own workers.
            with contextlib.ExitStack() as stack:
                if threading.current_thread() is threading.main_thread():
                    handlers = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
                    stack.callback(lambda: [signal.signal(s, h) for s, h in handlers.items()])
                if process.pid is not None:
                    stop_owned(process, isolate_group, ready)
        except BaseException:
            if process.pid is not None:
                stop_owned(process, isolate_group, ready)
            raise
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        result = {"status": "error", "error_type": "WorkerExited", "cycles": []}
    if reason:
        result["status"] = reason
        if reason == "timed_out":
            result["timeout_s"] = timeout
    elif process.exitcode != 0:
        result.update(status="error", worker_exitcode=process.exitcode)
    try:
        calls = recover_inference_calls(result_path)
        if calls is not None:
            result["inference_calls"] = calls
    except (OSError, ValueError):
        result.update(status="error", error_type="InferenceJournalInvalid")
    result.setdefault("elapsed_s", time.monotonic() - started)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(result_path, result)
    return result
