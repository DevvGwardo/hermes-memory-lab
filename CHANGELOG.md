# Changelog

## 0.3.0 · 2026-10-11

### Gemini-assisted hardening

- Use Gemini Flash Lite through the existing OmniRoute service for two scoped
  reviews, a repair consultation, and a final patch review. Reproduce findings
  locally rather than accepting model suggestions as evidence.
- Reserve API slots under a lock before I/O. Concurrent requests cannot exceed
  the cap. Atomic metadata-only journals retain in-flight reservations across
  hard worker exits; failed workers and parent recovery retain usage.
- Guard standard Python `connect_ex`, UDP and DNS paths as well as TCP connects.
  Explicitly budgeted transport retains permission; native subprocesses remain
  outside the declared isolation model.
- Drain threads created through the target's `threading` module before restoring
  fake adapters, homes and network guards. Restore `sys.path` between object-mode
  scopes. Stuck target threads remain subject to the owned-process deadline.
- Require committed, non-budget-aborted compactions and restart tokens that match
  configured process/object mode. Process tokens must be unique across cycles.
- Reject duplicate comparison checkpoints and missing environment metadata unless
  explicitly confounded. Earlier shared checkpoints always report partial status.
- Expand tests from **43 to 60**, including concurrent calls, abrupt exit during
  I/O, failed-worker telemetry, network bypasses, thread drainage and gate checks.
- Remove hardcoded test-count/date/version claims from the release exporter.
  Preserve the v0.2 baseline and add separately identified v0.3 evidence.

## 0.2.0 · 2026-10-11

### Correctness

- Require a complete exact-value/abstention response or strict JSON. Contradictory
  prose and multiple values can no longer pass by exposing the first matching token.
  Duplicate JSON answer fields and wire fields are also rejected.
- Keep errors, malformed answers and budget skips in planned-query denominators.
  Report conditional answer accuracy and completion separately.
- Stratify critical facts across short, buried and tool-only evidence, independently
  of their key numbers. Capped probes also cover these groups.
- Require matching grading/workload versions, harness fingerprints, settings,
  environment metadata and aligned checkpoint positions in comparisons. Explicit
  overrides mark partial/confounded comparisons rather than hiding differences.

### Lifecycle and safety

- Default to fresh component interpreters for checkpoint replay and a separate
  fresh interpreter for persistence verification and post-restart recall.
- Compare exact observed fact states, not just equal aggregate counts.
- Require critical correctness and current evidence before and after restart.
- Handle SIGINT/SIGTERM and timeouts by stopping owned workers and descendants.
  Preserve reports and stop scheduling remaining seeds on cancellation.
- Stream workload generation/hashing, check the owned run directory's footprint,
  and use unique temporary files for atomic checkpoint writes.

### Reuse and documentation

- Make regression contracts self-contained; remove runtime dependence on local
  historical-audit scripts.
- Discover installed interpreters without a user-specific pinned path.
- Add a generated architecture illustration and reproducible Mermaid PNG/SVG
  diagrams, with checked-in sources and a pinned renderer.
- Track reusable source and safe numeric result summaries in a private repository.
  Ignore historical audit artifacts, raw runs, credentials and installed targets.

## 0.1.0 · Local baseline

Initial seeded component simulator, object-level reopen tests, fault suite and
stage reports. Historical local v1 runs are not silently comparable with v2 grades.
