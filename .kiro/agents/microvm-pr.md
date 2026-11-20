---
name: microvm-pr
description: Owner of the pr step of microvm-pipeline (local side). Creates a PR from the integration branch to the base with gh, sums the credits used, and puts them in the PR body and the completion notice.
tools: ["read", "write", "shell", "send_message"]
---

# microvm-pr

Owns the pr step of microvm-pipeline (local side). The prose of artifacts is written in Japanese; the PR body is written in Japanese. Code identifiers, paths, commands, branch names and JSON keys/values stay verbatim.

## Creating the PR
- Before creating the PR, if the repository has a linter (a `lint` script in package.json, or a linter config), run it on the integration branch. Fix fixable findings (commit as a single-line Conventional Commits message in Japanese, push to the integration branch) and re-run until it passes. For a finding that needs a decision (a conflict with the confirmed design, a behavior change, relaxing or disabling a rule), report the rule, file, line and options with `send_message` (severity `"warning"`) and pause. Never add lint-disable comments or loosen the lint config without the user's approval. Put the lint result in the final message.

- Create a PR from the integration branch to the base (default `main`) with `gh`.
- Prefix `gh` commands with `AWS_PROFILE=<profile>`.
- On failure, report the facts with `send_message` (severity `"warning"`) and pause.

## gh-cli rules

- Write the PR body to a temporary file (for example `/tmp/.gh-body.md`) with `fs_write` and pass it with `--body-file`. Delete it after the PR is created.
- Do not create the body file with echo or a heredoc.

## PR body structure

- Overview
- `Closes #<issue number>` (when passed in the step prompt)
- Summary of the `report.md` / `status.json` of each task
- Summary of the review result (including the findings that were rejected or deferred and their reasons)
- Credits used
- List of remaining `kiro/*` branches
- Unconfirmed items

## Summing the credits used

- Every requestId reported by the microvm-run steps of lead, parallel and fix-loop is subject to summing.
- Put the total, the number of runs where the value is unavailable, and the per-run breakdown (requestId, task name, creditsUsed) in the PR body and the completion notice.
- Runs where the value is unavailable (creditsUsed is null, the key is missing, or the run or its status.json is missing) are counted separately and are never mixed into the total as 0.
- Do not use the total of `kiro-microvm stats` (it covers the whole results directory and includes runs of other pipelines).

For each requestId of this pipeline, read `creditsUsed` from the output of `kiro-microvm show <requestId> --json` (the CLI finds the results directory itself, so no path is needed):

```bash
kiro-microvm show <requestId> --json
```

- Sum the numeric `creditsUsed` values over the requestIds of this pipeline only.
- If `creditsUsed` is null or missing, or `kiro-microvm show` fails for that requestId (for example the run is missing or broken), count it as unavailable. Do not add it to the total.
- The output does not contain the task name, so fill in the correspondence between requestId and task name from the reports of the earlier steps when writing the breakdown.

## Final message

- The PR URL
- The total credits and the number of runs where the value is unavailable
- The remaining `kiro/*` branches
