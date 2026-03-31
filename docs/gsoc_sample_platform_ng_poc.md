# Sample Platform NG PoC

This branch contains a small reviewer-facing proof of concept for the
Sample Platform NG proposal.

## What this PoC adds

- a read-only run manifest route at `/test/run_manifest/<test_id>/json`
- a lightweight run overview page at `/test/run_manifest/<test_id>`
- a run-centric summary built from the current `Test`, `TestProgress`,
  `TestResult`, and `TestResultFile` tables

The goal is to validate the proposal direction without forcing the full schema
and UI work into one deadline branch.

## What this PoC does not try to prove yet

- new database tables such as `run_manifest` or `run_artifact`
- baseline state such as `never_worked`
- smarter subtitle-aware diffing
- bisect queueing

## Why this is still useful

The proposal claims that one run should be inspectable from one structured
surface. This PoC proves that the current data can already be shaped that way.
That lowers the design risk before the additive schema work begins.

## Local test command

From the repository root:

```bash
python -m unittest \
  tests.test_test.test_controllers.TestControllers.test_run_manifest_json \
  tests.test_test.test_controllers.TestControllers.test_run_manifest_json_includes_diff_download \
  tests.test_test.test_controllers.TestControllers.test_run_manifest_page_loads
```

## Reviewer path

1. open the branch linked from the proposal
2. read this file
3. inspect `mod_test/controllers.py`
4. run the controller tests above
5. open `/test/run_manifest/<test_id>` on a local instance
6. compare the PoC against the proposal sections on run manifests and validation
