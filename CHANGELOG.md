# Changelog

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
