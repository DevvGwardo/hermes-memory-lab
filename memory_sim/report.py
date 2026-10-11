"""Dependency-free JSON, CSV, Markdown and HTML reporting."""
from __future__ import annotations

import csv
import html
from pathlib import Path
import statistics


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def final_metrics(seed, row=None):
    if not seed.get("cycles"):
        return {}
    row = row or seed["cycles"][-1]
    retrieval = row["retrieval"]
    return {
        "saved": ratio(row["saved"]["correct"], row["saved"]["total"]),
        "indexed_records": ratio(row["indexed_records"], row["records"]),
        "recall": ratio(retrieval["known_correct"], retrieval["known_total"]),
        "active_recall": ratio(retrieval.get("active_correct", 0), retrieval.get("active_total", 0)),
        "active_answer_accuracy": ratio(retrieval.get("active_answered_correct", 0),
                                       retrieval.get("active_answered_total", 0)),
        "query_completion": ratio(retrieval.get("answered_total", 0), retrieval.get("planned_queries", 0)),
        "deletion_abstention": ratio(retrieval.get("deleted_correct", 0), retrieval.get("deleted_total", 0)),
        "context": ratio(row["context_after"]["correct"], row["context_after"]["total"]),
        "critical_recall": ratio(retrieval["critical_correct"], retrieval["critical_total"]),
        "duplicate_ids": row["duplicate_ids"],
        "facts_lost": sum(cycle["facts_lost_this_compaction"] for cycle in seed["cycles"]
                          if cycle["turns"] <= row["turns"]),
    }


def summary(run):
    complete = [seed for seed in run.get("seeds", []) if seed["status"] == "complete"]
    metrics = [final_metrics(seed) for seed in complete]
    output = {"complete_seeds": len(complete), "requested_seeds": len(run.get("requested_seeds", run.get("seeds", [])))}
    for key in ["saved", "indexed_records", "recall", "active_recall", "active_answer_accuracy",
                "query_completion", "deletion_abstention", "context", "critical_recall"]:
        values = [row[key] for row in metrics if row.get(key) is not None]
        output[key] = {"mean": statistics.mean(values), "min": min(values), "max": max(values)} if values else None
    return output


def percent(value):
    return f"{value * 100:.1f}%" if value is not None else "not measured"


def curve_svg(seeds):
    grouped = {}
    for seed in seeds:
        for row in seed.get("cycles", []):
            entry = grouped.setdefault(row["cycle"], {"saved": [], "context": [], "recall": []})
            entry["saved"].append(ratio(row["saved"]["correct"], row["saved"]["total"]))
            entry["context"].append(ratio(row["context_after"]["correct"], row["context_after"]["total"]))
            value = ratio(row["retrieval"].get("active_correct", 0), row["retrieval"].get("active_total", 0))
            if value is not None:
                entry["recall"].append(value)
    width, height, left, top, plot_w, plot_h = 720, 300, 55, 20, 640, 220
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Evidence and recall by compaction cycle">']
    parts.append('<rect width="720" height="300" fill="#fff"/>')
    for tick in (0, .25, .5, .75, 1):
        y = top + (1 - tick) * plot_h
        parts.append(f'<path d="M{left},{y}H{left + plot_w}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="5" y="{y + 4}" font-size="12" fill="#475569">{int(tick * 100)}%</text>')
    keys = sorted(grouped)
    for name, color in [("saved", "#2563eb"), ("context", "#15803d"), ("recall", "#c2410c")]:
        points = []
        for i, key in enumerate(keys):
            values = grouped[key][name]
            if values:
                x = left + i * plot_w / max(1, len(keys) - 1)
                y = top + (1 - statistics.mean(values)) * plot_h
                points.append(f"{x:.1f},{y:.1f}")
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"/>')
    for i, key in enumerate(keys):
        x = left + i * plot_w / max(1, len(keys) - 1)
        parts.append(f'<text x="{x:.1f}" y="260" text-anchor="middle" font-size="11">{key}</text>')
    parts.append('<text x="280" y="288" font-size="13">Compaction cycle (mean across available seeds)</text></svg>')
    return "".join(parts)


def write_report(root, run):
    root = Path(root)
    run["summary"] = summary(run)
    rows = []
    for seed in run.get("seeds", []):
        metric = final_metrics(seed)
        rows.append([seed["seed"], seed["status"], len(seed.get("cycles", [])),
                     percent(metric.get("saved")), percent(metric.get("indexed_records")),
                     percent(metric.get("recall")), percent(metric.get("active_recall")), percent(metric.get("context")),
                     metric.get("facts_lost", "not measured"), round(seed.get("elapsed_s", 0), 2)])
    header = ["Seed", "Status", "Cycles", "Evidence saved", "Records indexed",
              "Known-state answers", "Active-value recall", "Context facts", "Facts lost at compaction", "Seconds"]
    lines = [
        "# Memory endurance simulator",
        "",
        f"Preset: **{run['config']['preset']}**. Inference: **{run['config']['inference']}**. "
        f"Index policy: **{run['config']['index_policy']}**.",
        "",
        "Offline recall is scripted evidence availability, not semantic model accuracy. "
        "The grading ledger is not given to retrieval or summary adapters. Context is "
        "deliberately reset at configured session boundaries; persistent-memory stages remain comparable.",
        "Known-state answers include correct abstentions on deleted facts. Active-value recall "
        "excludes deleted and unknown facts. Deletion abstention alone is not proof of remembered evidence.",
        "Every planned probe remains in the end-to-end denominator, including errors and skipped "
        "calls. Conditional answer accuracy is separate. Restart observations come from the selected "
        "process/object mode, and exact observed states are compared.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *["| " + " | ".join(map(str, row)) + " |" for row in rows],
        "",
        "Summary statistics use complete seeds only. Charts include recorded checkpoints "
        "from incomplete seeds. A single run's timings are not performance confidence intervals.",
        "",
        "Artifacts: `run.json`, `curve.csv`, `report.html`, and per-seed synthetic workload, "
        "grader-only ledger, event trace, and checkpoint results.",
    ]
    if run.get("chaos"):
        chaos = run["chaos"]
        lines += ["", f"## Chaos checks: {chaos['passed']}/{chaos['total']} pass", "",
                  "| Check | Result |", "|---|---|"]
        lines += [f"| {row['name']} | {'PASS' if row['pass'] else 'FAIL'} |" for row in chaos["checks"]]
    (root / "report.md").write_text("\n".join(lines) + "\n")
    with (root / "curve.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["seed", "cycle", "turns", "session", "saved_fraction", "indexed_record_fraction",
                         "context_fraction", "known_state_fraction", "active_recall_fraction",
                         "deletion_abstention_fraction", "critical_recall_fraction", "query_completion",
                         "active_answer_accuracy", "errors", "budget_skipped", "stale_answers",
                         "invented_answers", "facts_lost", "latency_p95_s", "tokens_before_rough",
                         "tokens_after_rough", "temporary_bytes"])
        for seed in run.get("seeds", []):
            for row in seed.get("cycles", []):
                recall = row["retrieval"]
                writer.writerow([
                    seed["seed"], row["cycle"], row["turns"], row["session"],
                    ratio(row["saved"]["correct"], row["saved"]["total"]),
                    ratio(row["indexed_records"], row["records"]),
                    ratio(row["context_after"]["correct"], row["context_after"]["total"]),
                    ratio(recall["known_correct"], recall["known_total"]),
                    ratio(recall.get("active_correct", 0), recall.get("active_total", 0)),
                    ratio(recall.get("deleted_correct", 0), recall.get("deleted_total", 0)),
                    ratio(recall["critical_correct"], recall["critical_total"]),
                    ratio(recall.get("answered_total", 0), recall["planned_queries"]),
                    ratio(recall.get("active_answered_correct", 0), recall.get("active_answered_total", 0)),
                    recall["errors"], recall["budget_skipped"],
                    recall["stale"], recall["invented"], row["facts_lost_this_compaction"],
                    recall["latency_p95_s"], row["tokens_before_rough"], row["tokens_after_rough"],
                    row["temporary_bytes"],
                ])
    table = "<table><thead><tr>" + "".join(f"<th>{html.escape(cell)}</th>" for cell in header) + "</tr></thead><tbody>"
    table += "".join("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows)
    table += "</tbody></table>"
    faults = ""
    if run.get("chaos"):
        faults = "<h2>Fault checks</h2><ul>" + "".join(
            f"<li>{html.escape(row['name'])}: {'PASS' if row['pass'] else 'FAIL'}</li>"
            for row in run["chaos"]["checks"]) + "</ul>"
    (root / "report.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Memory endurance report</title><style>'
        'body{font:15px system-ui,sans-serif;margin:40px auto;max-width:1120px;padding:0 20px;color:#172033}'
        'h1{font-size:32px}table{border-collapse:collapse;width:100%;font-size:13px}'
        'th,td{padding:10px;text-align:left;border-bottom:1px solid #dde3ec}'
        'th{background:#f1f5f9}svg{width:100%;max-width:850px}p{max-width:850px;line-height:1.6}'
        '</style><h1>Memory endurance report</h1>'
        f'<p>Preset: {html.escape(run["config"]["preset"])}. '
        f'Index policy: {html.escape(run["config"]["index_policy"])}. '
        f'Inference: {html.escape(run["config"]["inference"])}.</p>'
        '<p>Blue: evidence saved. Green: facts in active context. Orange: active-value recall. '
        'Known-state answers also count abstentions on deleted facts; these alone do not prove evidence retention. '
        'Failed and skipped probes remain in end-to-end denominators. '
        'Offline values are scripted diagnostics, not semantic model accuracy. '
        'Session resets are deliberate; incomplete seeds contribute recorded checkpoints only.</p>'
        + curve_svg(run.get("seeds", [])) + table + faults +
        '<p>No live profile is modified. Temporary stores are disposable. Private session shaping, '
        'when requested, uses numeric metadata only.</p></html>'
    )


COMPARISON_FIELDS = (
    "facts", "turns", "cycles", "payload_chars", "context_length", "max_probes",
    "session_every_cycles", "restart_mode", "inference", "max_api_calls",
)


def compare_runs(left, right, *, allow_confounded=False, allow_incomplete=False):
    a = {seed["seed"]: seed for seed in left.get("seeds", [])}
    b = {seed["seed"]: seed for seed in right.get("seeds", [])}
    if set(a) != set(b) or not a or len(a) != len(left["seeds"]) or len(b) != len(right["seeds"]):
        raise ValueError("Comparisons require the same nonempty seed set")
    differences = {
        key: {"left": left["config"].get(key), "right": right["config"].get(key)}
        for key in COMPARISON_FIELDS if left["config"].get(key) != right["config"].get(key)
    }
    if differences and not allow_confounded:
        raise ValueError("Confounded settings differ: " + ", ".join(differences))
    output = []
    for seed in sorted(a):
        if not a[seed].get("workload_sha256") or not b[seed].get("workload_sha256"):
            raise ValueError(f"Seed {seed} has no completed workload fingerprint")
        if a[seed]["workload_sha256"] != b[seed]["workload_sha256"]:
            raise ValueError(f"Seed {seed} has a different generated workload")
        for key in ("grading_version", "workload_version", "harness_sha256"):
            if a[seed].get(key) is None or b[seed].get(key) is None:
                raise ValueError(f"Seed {seed} lacks {key}; legacy results need explicit regrading")
            if a[seed][key] != b[seed][key]:
                raise ValueError(f"Seed {seed} has a different {key}")
        if (a[seed]["status"] != "complete" or b[seed]["status"] != "complete") and not allow_incomplete:
            raise ValueError("Incomplete jobs require --allow-incomplete and a shared checkpoint")
        if not a[seed].get("python_version") or not b[seed].get("python_version"):
            if not allow_confounded:
                raise ValueError("Missing Python environment metadata")
            differences[f"missing_python_seed_{seed}"] = True
        if not a[seed].get("dependency_versions") or not b[seed].get("dependency_versions"):
            if not allow_confounded:
                raise ValueError("Missing dependency environment metadata")
            differences[f"missing_dependencies_seed_{seed}"] = True
        if a[seed].get("python_version") != b[seed].get("python_version"):
            if not allow_confounded:
                raise ValueError("Python environments differ")
            differences["python_version"] = {"left": a[seed].get("python_version"),
                                             "right": b[seed].get("python_version")}
        if a[seed].get("dependency_versions") != b[seed].get("dependency_versions"):
            if not allow_confounded:
                raise ValueError("Dependency environments differ")
            differences["dependency_versions"] = {"left": a[seed].get("dependency_versions"),
                                                  "right": b[seed].get("dependency_versions")}
        ac = {row["turns"]: row for row in a[seed].get("cycles", [])}
        bc = {row["turns"]: row for row in b[seed].get("cycles", [])}
        if len(ac) != len(a[seed].get("cycles", [])) or len(bc) != len(b[seed].get("cycles", [])):
            raise ValueError("Duplicate checkpoint positions")
        shared = sorted(set(ac) & set(bc))
        if not shared:
            raise ValueError(f"Seed {seed} has no shared checkpoint")
        turn = shared[-1]
        if not allow_incomplete and (turn != max(ac) or turn != max(bc)):
            raise ValueError("Final checkpoint positions differ")
        left_row, right_row = ac[turn], bc[turn]
        if left_row["session"] != right_row["session"] and not allow_confounded:
            raise ValueError("Checkpoint session positions differ")
        if left_row["session"] != right_row["session"]:
            differences[f"checkpoint_session_seed_{seed}"] = {
                "left": left_row["session"], "right": right_row["session"]}
        left_metrics, right_metrics = final_metrics(a[seed], left_row), final_metrics(b[seed], right_row)
        output.append({
            "seed": seed, "left_status": a[seed]["status"], "right_status": b[seed]["status"],
            "checkpoint_turn": turn, "left_cycle": left_row["cycle"], "right_cycle": right_row["cycle"],
            "partial_checkpoint": turn != max(ac) or turn != max(bc),
            "left": left_metrics, "right": right_metrics,
            "delta": {key: right_metrics[key] - left_metrics[key] for key in left_metrics
                      if left_metrics.get(key) is not None and right_metrics.get(key) is not None},
        })
    return {
        "method": "Matching seed/workload fingerprints. Deltas are right minus left.",
        "index_policy_changed": left["config"]["index_policy"] != right["config"]["index_policy"],
        "confounded": bool(differences), "setting_differences": differences,
        "partial_comparison": any(row["partial_checkpoint"] or row["left_status"] != "complete" or row["right_status"] != "complete"
                                  for row in output),
        "warning": ("CONFOUNDED comparison: changed settings are explicitly listed. " if differences else "") +
                   "Manual rebuild policy is a diagnostic control, not a deployed plugin fix.",
        "seeds": output,
    }
