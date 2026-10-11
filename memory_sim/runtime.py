"""Bind real Hermes components to a synthetic, temporary home."""
from __future__ import annotations

import contextlib
import contextvars
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import urllib.request

from .workload import observed_state, render_state

_TRANSPORT_ALLOWED = contextvars.ContextVar("memory_sim_transport_allowed", default=False)


class BudgetExceeded(RuntimeError):
    pass


class Transport:
    def __init__(self, mode="offline", max_calls=12):
        self.mode, self.max_calls = mode, max_calls
        self.calls = []
        self.exhaustions = 0
        self.key = os.environ.get("SURPLUS_API_KEY") if mode == "surplus" else None
        if mode == "surplus" and not self.key:
            raise ValueError("SURPLUS_API_KEY must already be in the shell; no credential files are searched.")

    def ask(self, prompt, *, stage="recall", max_tokens=1500):
        if self.mode == "offline":
            raise ValueError("Offline transport cannot make inference calls")
        if len(self.calls) >= self.max_calls:
            self.exhaustions += 1
            raise BudgetExceeded("Inference call budget exhausted")
        request = urllib.request.Request(
            "https://api.surplusintelligence.ai/v1/chat/completions",
            data=json.dumps({"model": "glm-5.3-flash", "temperature": .2, "max_tokens": max_tokens,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        start = time.monotonic()
        metadata = {"stage": stage}
        permission = _TRANSPORT_ALLOWED.set(True)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
            metadata.update(ok=True, usage=result.get("usage", {}), model=result.get("model"),
                            finish_reason=result["choices"][0].get("finish_reason"))
            if metadata["finish_reason"] == "length":
                raise RuntimeError("Truncated inference output")
            return result["choices"][0]["message"].get("content") or ""
        except Exception as exc:
            metadata.update(ok=False, error_type=type(exc).__name__)
            raise
        finally:
            _TRANSPORT_ALLOWED.reset(permission)
            metadata["latency_s"] = time.monotonic() - start
            self.calls.append(metadata)


class Harness:
    def __init__(self, module, home, transport):
        self.module, self.home, self.transport = module, home, transport
        self.material = ""
        self.api_failed = False
        self.chat_calls = 0

    def tree_summary(self, text, budget=512):
        # A deterministic substitute for this side-model only; source leaf clipping is real.
        return render_state(observed_state(text))[:budget] or text[:budget]

    def chat(self, prompt, **kwargs):
        self.chat_calls += 1
        self.material = "" if prompt.startswith("Memory index") else self.material
        if not prompt.startswith("Memory index"):
            self.material = prompt.split("\n\nQuestion:", 1)[0]
        if self.transport.mode != "offline":
            try:
                return self.transport.ask(prompt)
            except BudgetExceeded:
                raise
            except Exception:
                self.api_failed = True
                return None
        match = re.search(r"\bf-\d+\b", prompt.rsplit("Question:", 1)[-1])
        if not match:
            return "NONE" if prompt.startswith("Memory index") else "NOT FOUND"
        key = match.group()
        if prompt.startswith("Memory index"):
            candidates = []
            for block in re.finditer(r"\[(\d+)\+(\d+)\](.*?)(?=\[\d+\+\d+\]|\Z)", prompt, re.DOTALL):
                state = observed_state(block.group(3).split("Question:", 1)[0])
                if key in state:
                    candidates.append((state[key][0], int(block.group(1)), int(block.group(2))))
            if not candidates:
                return "NONE"
            _, start, span = max(candidates)
            return f"[{start}+{span}]"
        state = observed_state(self.material)
        return state.get(key, (-1, None))[1] or "NOT FOUND"

    def summary(self, prompt, started):
        if self.transport.mode == "surplus":
            return self.transport.ask(prompt, stage="compression", max_tokens=8192)
        # Perfect preservation of selected source, not perfect access to the ledger.
        return "## Goal\nPreserve observed synthetic settings.\n\n## Active State\n" + render_state(observed_state(prompt))

    @contextlib.contextmanager
    def adapters(self):
        with patch.object(self.module, "_chat", side_effect=self.chat), \
                patch.object(self.module, "_llm_summarize", side_effect=self.tree_summary), \
                patch.object(self.module, "_agy_verify", return_value=None), \
                patch.object(self.module, "_summarizer_key", return_value=None), \
                patch.object(self.module, "time", SimpleNamespace(
                    time=time.time, strftime=time.strftime, sleep=lambda seconds: None)):
            yield

    def provider(self, session_id="synthetic-session"):
        provider = self.module.UniichatMemoryProvider({"root": str(self.home / "uniichat"), "summarize": True})
        from agent.memory_manager import MemoryManager
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.initialize_all(session_id, hermes_home=str(self.home), platform="cli",
                               agent_context="primary")
        return provider, manager

    def compressor(self, context_length):
        from agent.context_compressor import ContextCompressor
        compressor = ContextCompressor("synthetic-memory-sim", config_context_length=context_length,
                                       quiet_mode=True, abort_on_summary_failure=True)
        compressor._call_summary_llm = self.summary
        return compressor


@contextlib.contextmanager
def _bound_host(config, scratch):
    home = scratch / "fake-user" / ".hermes" / "profiles" / "sim"
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(HOME=str(scratch / "fake-user"), HERMES_HOME=str(home),
                      HERMES_RUNTIME_DIR=str(scratch / "runtime"), PYTHONDONTWRITEBYTECODE="1")
    # Offline jobs cannot accidentally inherit a real inference credential.
    if config.get("inference", "offline") == "offline":
        for name in list(os.environ):
            if name.endswith(("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
                os.environ.pop(name, None)
    sys.path.insert(0, config["core_root"])
    with patch("hermes_cli.venv_sync.prepare_launch", return_value=None), \
            patch("pm.environments.activate_dependencies", return_value=None), \
            patch("hermes_cli._early_recovery.recover_if_needed", return_value=None):
        original_connect = socket.socket.connect

        def guarded_connect(sock, address):
            if config.get("inference", "offline") == "offline" or not _TRANSPORT_ALLOWED.get():
                raise RuntimeError("Only explicitly budgeted inference may access the network")
            return original_connect(sock, address)

        with patch.object(socket.socket, "connect", new=guarded_connect):
            spec = importlib.util.spec_from_file_location("memory_sim_target", config["plugin"])
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            # Import before attaching adapters; no actual agent is constructed.
            from agent.context_compressor import ContextCompressor
            from agent.memory_manager import MemoryManager
            from hermes_state import SessionDB
            with patch.object(socket.socket, "connect", new=guarded_connect):
                yield module, home


@contextlib.contextmanager
def host(config, scratch):
    with patch.dict(os.environ, {}, clear=False):
        with _bound_host(config, scratch) as bound:
            yield bound
