#!/usr/bin/env python3
"""Export only numeric/fingerprint release evidence, never source turns or answers."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memory_sim.report import final_metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--prefix", default="final")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/release-summary.json")
    args = parser.parse_args()
    report = {"date": "2026-10-11", "harness_tests": 43, "release": "0.2.0", "runs": {}}
    for suffix in ("smoke-natural", "smoke-rebuild", "endurance", "chaos"):
        name = f"{args.prefix}-{suffix}"
        source = json.loads((args.runs_root / name / "run.json").read_text())
        seeds = []
        for seed in source["seeds"]:
            row = seed["cycles"][-1]
            seeds.append({
                "seed": seed["seed"], "status": seed["status"], "cycles": len(seed["cycles"]),
                "elapsed_s": seed["elapsed_s"], "final_metrics": final_metrics(seed),
                "records": row["records"], "indexed_records": row["indexed_records"],
                "active_correct": row["retrieval"]["active_correct"],
                "active_total": row["retrieval"]["active_total"],
                "archive_correct": row["sqlite_archive_control"]["correct"],
                "archive_total": row["sqlite_archive_control"]["total"],
                "exact_context_restarts": all(c["restart_state_matches"] for c in seed["cycles"]),
                "exact_raw_restarts": all(c["restart_raw_state_matches"] for c in seed["cycles"]),
                "fresh_interpreters": len({
                    token for c in seed["cycles"] for token in (
                        c["restart"]["checkpoint_start_token"], c["restart"]["verification_start_token"])}),
                "inference_calls": len(seed["inference_calls"]),
                "harness_sha256": seed["harness_sha256"], "plugin_sha256": seed["plugin_sha256"],
                "workload_sha256": seed["workload_sha256"],
                "python_version": seed["python_version"], "dependency_versions": seed["dependency_versions"],
            })
        report["runs"][name] = {"summary": source["summary"], "gates": source["gates"], "seeds": seeds}
        if source.get("chaos"):
            report["runs"][name]["chaos"] = source["chaos"]
    comparison = json.loads((args.runs_root / f"{args.prefix}-comparison/comparison.json").read_text())
    report["comparison"] = comparison
    text = json.dumps(report, indent=2) + "\n"
    if "/Users/" in text or '"answer"' in text or '"answer_text"' in text or '"messages"' in text:
        raise ValueError("Export accidentally included a local path or raw data")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)
    print(f"Safe release summary saved to {args.output}")


if __name__ == "__main__":
    main()
