# v0.3.0: deeper, verified hardening

**60 tests pass.** This release strengthens the lab, not the installed memory plugin.
The [v0.2 baseline](results.md) remains historical evidence; do not silently compare
different harness fingerprints.

## Gemini through OmniRoute

The existing local OmniRoute service was healthy. Starting it again correctly refused
to duplicate the instance. Its advertised Gemini 3.8 route was unavailable; the
canonical **`antigravity/gemini-3.1-flash-lite`** route worked.

Gemini provided four code-only consultations: grading/experiment review,
lifecycle/safety review, focused repair advice, and final patch review.
Historical conversations, raw workloads, truth ledgers and credentials were not
included. Prompts/responses remain local and ignored.

Several suggestions were already covered or contradicted the intended contract.
They were rejected. The final model review found no verified defect in the patch,
but it did not execute tests and is not a substitute for the results below.

## Reproduced defects and repairs

| Gap | Reproduction / repair |
|:---|:---|
| Concurrent budget overshoot | A one-call cap issued two mocked requests. Slots now reserve under a lock before I/O. |
| Failed-worker usage loss | A failing worker with two calls reported zero. Usage now survives normal failures and hard exits. |
| Python network bypasses | TCP-only patching left `connect_ex`, UDP and DNS unguarded. Standard Python paths now share a permission guard. |
| Escaping target threads | Adapter scopes could restore real helpers before callbacks finished. Target-thread drainage now happens while fake adapters and host guards remain active. |
| Import-path leakage | Object-mode scopes accumulated target paths. Host exit restores the original path list and environment. |
| Misleading comparison labels | Two complete runs could compare an earlier shared snapshot while reporting non-partial. Labels now reflect the selected checkpoint. |
| Weak lifecycle acceptance | No-op compaction or reused interpreter identities had no dedicated rejection gate. Committed compaction and configured restart identity are now required. |

The new tests include a genuine `os._exit(73)` during mocked request I/O. The parent
recovers the already-written reservation without making any external inference call.
Journals contain metadata only. Unfinished requests count conservatively as attempts,
not as proven completed or billed requests.

## Executed release matrix

[Sanitized numeric evidence](hardening-summary.json) contains per-seed metrics,
fingerprints, environment metadata, gates and the guarded matched comparison.

| Check | Result |
|:---|:---|
| Harness tests | **60 / 60** |
| Matched smoke | **5 natural + 5 rebuild seeds**, 200 turns and 3 compactions each |
| Active-value recall, natural | **0.87% mean**, 4 / 460 pooled |
| Active-value recall, rebuild | **39.15% mean**, 180 / 460 pooled |
| Endurance | **5,000 turns / 30 compactions / 60 fresh interpreters** |
| Endurance active recall | **0 / 91** |
| Original SQLite evidence | **500 / 500** |
| Exact context/raw restart state | Preserved at every completed checkpoint |
| New compaction/identity gates | Pass for every completed cohort |
| Evaluation inference calls | **0** |
| Unchanged target behavior checks | **5 / 19 pass** |

The first endurance attempt hit a **300-second deadline after 26 recorded cycles**.
Its partial report, checkpoints and journals were preserved. Shared machine load was
high, so a fresh directory was retried with a **600-second deadline**; all 30 cycles
completed in approximately **170 seconds**. This is not a performance confidence claim.

Target results remain poor. Rebuilding fixes index coverage, not tool ingestion,
clipping, relevance routing, profile isolation or revision/deletion behavior.
Background scheduling can vary retrieval results. Do not mistake deletion abstention
or a green infrastructure gate for correct active-value recall.

## Recreate

```sh
./run-simulator.sh test
./run-simulator.sh run --preset smoke --output runs/v3-smoke-natural
./run-simulator.sh run --preset smoke --index-policy rebuild --output runs/v3-smoke-rebuild
./run-simulator.sh run --preset endurance --seeds 1 --timeout 600 --output runs/v3-long-endurance
./run-simulator.sh run --preset chaos --seeds 1 --workers 8 --output runs/v3-chaos
./run-simulator.sh compare --left runs/v3-smoke-natural/run.json \
  --right runs/v3-smoke-rebuild/run.json --output runs/v3-comparison
python3 tools/export_results.py --runs-root runs --prefix v3 \
  --endurance-run runs/v3-long-endurance --release 0.3.0 --harness-tests 60 \
  --output docs/hardening-summary.json
```

Use fresh directories for another run. Production core/plugin/profile files remain
untouched; pre-existing core changes are preserved.

**Isolation limit:** thread drainage covers threads created through the target module's
`threading` reference. Arbitrary aliases, native subprocess networking, malicious target
code, hard OS quotas and deployed gateway restarts remain outside this component lab.
