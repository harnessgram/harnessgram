# Risk-based CI

Every pull request declares its CI class and explains the choice in one sentence. A ready pull request without a valid declaration fails the lightweight PR-body check; drafts are exempt while being prepared.

## Classes

- **Low** covers copy, documentation, UI kept behind an off switch, scripts, and tests. It merges after review and the writer's local tests. Heavy CI runs on `main`; if that run is red, the change is reverted through review.
- **Medium** covers ordinary product code. Pull-request CI is selected by changed paths: `src/ctb/` and `tests/` run Python tests, packaging paths run package checks, and `ios/` or `macos/` run native validation. Documentation-only changes run no heavy jobs.
- **High** covers database, authentication, money, children's data, native or store builds, and infrastructure. Every heavy job runs before the pull request joins a batch.

Use this exact shape in the pull-request body, replacing the class and rationale:

```text
CI class: Medium — changes Python request handling, so the Python suite must pass.
```

## Flow

1. Open the pull request as a draft and choose the class conservatively.
2. Run appropriate tests while writing it. When it is ready, the body check enforces the declaration and Medium or High CI runs as described above.
3. Merge reviewed Low changes directly; combine reviewed Medium and High changes through the normal batch process.
4. Every push to `main` runs the complete Python and package suites on the quality runner fleet.
5. If either suite fails, automation reverts the head commit on a new branch, opens a **draft** pull request labeled `ci-revert`, and links it from the merged pull request. A reviewer decides whether to merge it; automation never does. A revert created by this workflow does not recursively create another proposal.
