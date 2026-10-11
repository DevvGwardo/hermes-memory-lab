#!/usr/bin/env python3
"""Memory endurance simulator CLI. Run --help for bounded, isolated test options."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from memory_sim.chaos import chaos_run
from memory_sim.processes import cancellation_signals, owned_job
from memory_sim.report import compare_runs, write_report
from memory_sim.runner import atomic_json, seed_run
from memory_sim.workload import profile_shape

HERE = Path(__file__).resolve().parent
DEFAULT_CORE = str(Path(os.environ.get("MEMORY_SIM_CORE_ROOT", Path.home() / ".hermes/hermes-agent")).absolute())
DEFAULT_PLUGIN = str(Path(os.environ.get("MEMORY_SIM_PLUGIN", Path.home() / ".hermes/plugins/uniichat/__init__.py")).absolute())
PRESETS = {
    "smoke": {"facts": 100, "turns": 200, "cycles": 3, "workers": 2, "max_probes": 100},
    "endurance": {"facts": 500, "turns": 5000, "cycles": 30, "workers": 2, "max_probes": 100},
    "chaos": {"facts": 30, "turns": 60, "cycles": 3, "workers": 2, "max_probes": 30},
}


def parse_seeds(text):
    try:
        seeds = [int(part) for part in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Seeds must be comma-separated integers") from exc
    if not seeds or len(set(seeds)) != len(seeds) or len(seeds) > 20 or any(seed < 0 for seed in seeds):
        raise argparse.ArgumentTypeError("Use 1–20 distinct nonnegative seeds")
    return seeds


def validate(config):
    for key in ["facts", "turns", "cycles", "workers", "max_probes", "payload_chars",
                "timeout", "max_disk_mb", "context_length", "session_every_cycles", "max_api_calls"]:
        if config[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if config["facts"] > config["turns"] or config["cycles"] > config["turns"]:
        raise ValueError("facts and cycles cannot exceed turns")
    if config["facts"] > 10000 or config["turns"] > 50000 or config["workers"] > 8:
        raise ValueError("Safety caps: 10,000 facts, 50,000 turns, eight worker processes")
    if config["max_probes"] < 2 or config["payload_chars"] > 8192 or config["context_length"] < 64000:
        raise ValueError("Use at least two probes, payload <=8192 characters, context >=64000 tokens")
    if config["timeout"] > 3600 or config["max_disk_mb"] > 2048 or config["max_api_calls"] > 100:
        raise ValueError("Safety caps: one hour/job, 2048MB scratch, 100 inference calls/seed")
    if config["inference"] == "surplus" and config["preset"] != "smoke":
        raise ValueError("Real inference is limited to smoke workloads; use offline stress tests")
    lengths = config.get("shape", {}).get("length_samples", [])
    payload = max(min(8192, max(96, length)) for length in lengths) if lengths else config["payload_chars"]
    if config["turns"] * 3 * (payload + 800) > config["max_disk_mb"] * 1024**2:
        raise ValueError("Estimated source corpus exceeds the selected disk budget")
    for key in ["core_root", "plugin"]:
        if not Path(config[key]).is_absolute() or not Path(config[key]).exists():
            raise ValueError(f"{key} must be an existing absolute path")


def new_output(path):
    root = Path(path).expanduser().absolute() if path else HERE / "runs" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    if root.exists():
        raise ValueError("Output directory already exists; choose a new path to preserve existing runs")
    root.mkdir(parents=True)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Generate, replay, grade, and report an isolated workload")
    run.add_argument("--preset", choices=PRESETS, default="smoke")
    run.add_argument("--seeds", type=parse_seeds, default=[1, 2, 3, 4, 5])
    run.add_argument("--output")
    run.add_argument("--plugin", default=DEFAULT_PLUGIN)
    run.add_argument("--core-root", default=DEFAULT_CORE)
    for name in ["facts", "turns", "cycles", "workers", "max-probes"]:
        run.add_argument("--" + name, type=int)
    run.add_argument("--payload-chars", type=int, default=700)
    run.add_argument("--context-length", type=int, default=64000)
    run.add_argument("--session-every-cycles", type=int, default=3)
    run.add_argument("--restart-mode", choices=["process", "objects"], default="process")
    run.add_argument("--timeout", type=int, default=180, help="Per spawned job deadline in seconds")
    run.add_argument("--max-disk-mb", type=int, default=256)
    run.add_argument("--index-policy", choices=["natural", "rebuild"], default="natural")
    run.add_argument("--shape-from-profile", type=Path)
    run.add_argument("--inference", choices=["offline", "surplus"], default="offline")
    run.add_argument("--max-api-calls", type=int, default=12, help="Per-seed cap; default mode makes zero calls")
    run.add_argument("--strict", action="store_true", help="Exit 1 for failed acceptance gates; keep the report")
    compare = sub.add_parser("compare", help="Compare matching workloads without running inference")
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    compare.add_argument("--output")
    compare.add_argument("--allow-confounded", action="store_true", help="Explicitly allow and label changed settings")
    compare.add_argument("--allow-incomplete", action="store_true", help="Compare only a shared checkpoint")
    sub.add_parser("test", help="Run simulator tests, not the production repository test suite")
    args = parser.parse_args(argv)
    if args.command == "test":
        return subprocess.call([sys.executable, "-B", "-m", "unittest", "discover",
                                "-s", str(HERE / "tests"), "-v"], cwd=HERE)
    if args.command == "compare":
        result = compare_runs(
            json.loads(args.left.read_text()), json.loads(args.right.read_text()),
            allow_confounded=args.allow_confounded, allow_incomplete=args.allow_incomplete)
        output = new_output(args.output)
        atomic_json(output / "comparison.json", result)
        lines = ["# Memory simulator comparison", "", result["warning"], ""]
        for row in result["seeds"]:
            lines += [f"## Seed {row['seed']}", "", "```json", json.dumps(row["delta"], indent=2), "```", ""]
        (output / "comparison.md").write_text("\n".join(lines))
        print(output / "comparison.md")
        return 0
    config = {**PRESETS[args.preset], **{
        key: value for key, value in vars(args).items()
        if key not in {"command", "seeds", "output", "shape_from_profile", "strict"} and value is not None
    }}
    validate(config)
    if args.shape_from_profile:
        config["shape"] = profile_shape(args.shape_from_profile.expanduser().absolute())
        validate(config)
    output = new_output(args.output)
    config["preset"] = args.preset
    config["privacy"] = "Synthetic content only; optional history shaping copies numeric lengths and roles only."
    result = {
        "config": config, "seeds": [], "requested_seeds": args.seeds,
        "started_at": datetime.now(timezone.utc).isoformat(), "schema_version": 2,
    }
    print(f"Writing isolated run to {output}", flush=True)
    try:
        with cancellation_signals():
            for seed in args.seeds:
                print(f"Seed {seed}: {config['turns']} turns, {config['cycles']} compactions", flush=True)
                folder = output / f"seed-{seed}"
                row = owned_job(seed_run, (config, seed, str(folder)), folder / "result.json", config["timeout"])
                row.setdefault("seed", seed)
                result["seeds"].append(row)
                atomic_json(output / "run.json", result)
                print(f"Seed {seed}: {row['status']}, {len(row.get('cycles', []))} recorded cycles", flush=True)
                if row["status"] == "interrupted":
                    result["interrupted"] = True
                    break
            if args.preset == "chaos" and not result.get("interrupted"):
                print("Running bounded fault checks and actual process crashes", flush=True)
                folder = output / "chaos"
                result["chaos"] = owned_job(chaos_run, (config, str(folder)), folder / "result.json", config["timeout"])
                result["chaos"].setdefault("checks", [])
                result["chaos"].setdefault("passed", 0)
                result["chaos"].setdefault("total", len(result["chaos"]["checks"]))
                if result["chaos"]["status"] == "interrupted":
                    result["interrupted"] = True
    except KeyboardInterrupt:
        result["interrupted"] = True
    result["gates"] = acceptance_gates(result)
    write_report(output, result)
    atomic_json(output / "run.json", result)
    print(json.dumps({"summary": result["summary"], "gates": result["gates"]}, indent=2))
    print(f"Report: {output / 'report.html'}")
    if result.get("interrupted") or any(seed["status"] != "complete" for seed in result["seeds"]) or (
        result.get("chaos") and result["chaos"].get("status") != "complete"
    ):
        return 2
    if args.strict and not all(result["gates"].values()):
        return 1
    return 0


def acceptance_gates(result):
    rows = [cycle for seed in result["seeds"] for cycle in seed.get("cycles", [])]
    complete = not result.get("interrupted") and (
        len(result["seeds"]) == len(result.get("requested_seeds", result["seeds"]))) and all(
        seed["status"] == "complete" for seed in result["seeds"])
    return {
        "all_jobs_complete": complete and bool(rows),
        "no_duplicate_ids": bool(rows) and all(row["duplicate_ids"] == 0 for row in rows),
        "full_index_coverage": bool(rows) and all(row["indexed_records"] == row["records"] for row in rows),
        "current_evidence_saved": bool(rows) and all(row["saved"]["correct"] == row["saved"]["total"] for row in rows),
        "sqlite_restart_adds_no_loss": bool(rows) and all(
            row.get("restart_state_matches", False) for row in rows),
        "persistent_memory_restart_adds_no_loss": bool(rows) and all(
            row.get("restart_raw_state_matches", False) for row in rows),
        "critical_recall_complete": bool(rows) and all(
            score["critical_total"] > 0 and
            score["critical_total"] == score["critical_available"] and
            score["critical_correct"] == score["critical_total"] and
            score.get("critical_evidence_retrieved", 0) == score["critical_total"] and
            score["errors"] == 0 and score["budget_skipped"] == 0
            for row in rows for score in (row["retrieval"], row["retrieval_after_restart"])),
        "chaos_checks_pass": result["chaos"].get("status") == "complete" and
        result["chaos"].get("total", 0) > 0 and result["chaos"].get("passed") == result["chaos"].get("total")
        if result.get("chaos") else True,
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Simulator configuration/output error: {exc}", file=sys.stderr)
        raise SystemExit(2)
