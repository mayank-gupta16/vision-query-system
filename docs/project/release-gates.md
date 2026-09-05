# Release gates

A milestone may be released only when:

- required milestone issues are closed;
- acceptance criteria, relevant tests, and contract suites pass;
- the CPU-LITE benchmark and any milestone-specific accuracy benchmarks ran on
  pinned inputs/configuration and recorded actual results;
- no P0 defects remain, and every P1 is resolved or explicitly waived with risk;
- schema/API/migration and breaking changes are documented;
- dependency, model, dataset, security, privacy, and license records are current;
- every release with distributable dependencies includes an SBOM from the actual
  release environment and no unwaived high/critical known vulnerability;
- `docs/project/current.md` and `CHANGELOG.md` reflect the release;
- regression or benchmark deterioration is explained and approved;
- the release commit is tagged and a GitHub Release contains reproducible notes.

Before v1.0, public APIs must be labeled experimental. A benchmark run is not a
gate unless hardware, input manifest, revision, configuration, model/runtime
versions, wall time, resource metrics, and accuracy impact are recorded.
