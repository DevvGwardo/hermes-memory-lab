# Release baseline: 0.2.0

> **Harness validation is not plugin validation.** All **43 harness tests pass**.
> The unchanged target plugin passes **5 of 19 behavior contracts**.

Measured on October 11, 2026 using isolated synthetic data, grading/workload v2,
fresh component interpreters, and **zero inference calls**. The
[numeric evidence](release-summary.json) includes source fingerprints, dependencies,
seed-level metrics, restart invariants, gates, and the matched comparison.

## Matched five-seed smoke comparison

Each seed generates **200 turns / 100 facts / 3 compactions**. Both cohorts use
the same workload and settings; the only treatment is `natural` versus
`rebuild` indexing.

| Final-checkpoint metric | Natural index | Forced rebuild |
|:---|---:|---:|
| Current evidence retained in raw memory, mean | 80.0% | 80.0% |
| Indexed records | 21 / 405 | 405 / 405 |
| Indexed coverage | 5.19% | 100.0% |
| Active-value recall, mean of seed ratios | **0.65%** | **39.36%** |
| Active correct / planned, pooled | 3 / 460 | 181 / 460 |
| Known-state correct, including deletion abstentions | 8.4% | 44.2% |
| Current facts present in active context, mean | 87.6% | 87.6% |
| Planned retrieval queries completed | 100.0% | 100.0% |
| Exact context/raw state survives restart | Yes | Yes |

The guarded comparison accepted every seed and checkpoint without confounded or
incomplete overrides. These are paired diagnostics, not confidence intervals.
Background scheduling can affect the target's rebuilt cache and results.

**Interpretation:** rebuilding fixes index coverage, not end-to-end retrieval.
It does not recover missing tool evidence or repair clipping, relevance routing,
revision handling, or profile scope. High deletion abstention is not active recall.

## Endurance

| Measurement | Result |
|:---|---:|
| Turns / facts / compactions | **5,000 / 500 / 30** |
| Distinct fresh worker startup tokens | **60** |
| Raw memory records / indexed | 10,059 / 21 |
| Active-value recall | **0 / 91** |
| Known-state correct | 9 / 100 |
| Deletion abstention | 9 / 9 |
| Original SQLite evidence control | **500 / 500** |
| Current facts present in active context | 53.8% |
| Exact context/raw state survives every restart | Yes |
| Inference calls | **0** |

Every successful known-state answer here was a deletion abstention. The original
SQLite control surviving does not mean the compressed active context or plugin
recall preserved those facts.

## Fault contracts

The target passes **5 / 19** checks. This run reproduced:

- **200 / 200 concurrent records committed, with 85 duplicate IDs.** The duplicate
  count depends on scheduling, so the invariant is ID uniqueness, not that count.
- A killed index writer left **zero index nodes**, despite surviving raw records.
- **Two-profile leakage** and lost background profile context.
- Cold-start/stale indexing, destructive truncation, tool omission, revision/delete
  handling, and checkpoint weaknesses.

Synthetic timeout/malformed-response summary cases abort safely. Failed target
contracts remain red gates; ordinary diagnostic runs still return zero so their
reports can be inspected. Use `--strict` to turn red gates into exit code 1.

## What was validated

- Contradictory prose, conflicting values, duplicate JSON keys/wire fields, and
  invalid structured answers cannot earn a correct answer.
- Failed, skipped, and unqueried probes remain in planned denominators.
- Short, buried, and tool-only evidence appear among critical facts, including
  small workloads and capped probe batches.
- Actual **SIGINT/SIGTERM** tests stop owned descendants and preserve partial
  reports; CLI cancellation also prevents future seeds from starting.
- Checkpoints and verification use distinct fresh Python interpreters. Tests
  retain an explicit same-interpreter object-reopen control.
- Comparisons reject changed cadence/inference settings, incompatible versions,
  dependencies, mismatched checkpoints, and incomplete runs unless explicitly
  labeled otherwise.

## Limits and privacy

This is a **component persistence test**, not a deployed agent/gateway restart or
semantic model-accuracy score. Scripted source-only adapters grade exact synthetic
symbols; real model inference requires explicit opt-in and a budget.

Installed core/plugin/profile files were not modified. Raw runs, truth ledgers,
answers, historical audits, credentials, and private conversations remain local
and ignored by Git. Only reusable code, diagrams, and safe aggregate/fingerprint
evidence are published.

### Recreate this evidence

```bash
./run-simulator.sh test
./run-simulator.sh run --preset smoke --output runs/final-smoke-natural
./run-simulator.sh run --preset smoke --index-policy rebuild --output runs/final-smoke-rebuild
./run-simulator.sh run --preset endurance --seeds 1 --timeout 300 --output runs/final-endurance
./run-simulator.sh run --preset chaos --seeds 1 --workers 8 --output runs/final-chaos
./run-simulator.sh compare \
  --left runs/final-smoke-natural/run.json \
  --right runs/final-smoke-rebuild/run.json \
  --output runs/final-comparison
python3 tools/export_results.py --runs-root runs
```

Use a new output location or rename the cohort prefix for another release.
The simulator refuses to overwrite existing run directories.
