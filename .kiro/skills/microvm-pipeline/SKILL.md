---
name: microvm-pipeline
description: Split pipeline that divides one request into a "lead (shared foundation) task" and "parallelizable tasks", has kiro-cli on a MicroVM implement each task, merges the lead task into an integration branch before running the parallel tasks on top of it, merges all parallel tasks, runs a multi-model review on the whole, handles review findings on MicroVM as well, and finally opens one PR from the single integration branch. Task decomposition, lead, parallel, merge, review, the fix loop and the PR are all executed in one run_workflow. Use it when the user explicitly asks to start it, for example "use the microVM split pipeline", "split the request and let MicroVMs work on it in parallel" or "implement this with the MicroVM pipeline".
---

# microvm-pipeline

A dynamic pipeline that implements one implementation request using several kiro-cli instances on MicroVMs. It runs **task decomposition -> lead task -> lead merge -> parallel tasks -> merge of all parallel tasks -> one multi-model review of the whole -> fix loop for findings -> PR** inside **a single run_workflow**.

The step-specific rules have their primary source in the dedicated agent definitions under `.kiro/agents/` (microvm-split / microvm-run / microvm-merge / microvm-pr). This skill keeps only the overall structure, the review policy and the pause mediation, and does not duplicate the same rules.

The role of this skill is to receive a request, assemble one workflow (the workflowPrompt of run_workflow) and start it, and to mediate when the workflow pauses and asks for a user decision. The number of splits and the dependencies change with each request, so this is not a fixed recipe; the workflowPrompt is generated every time. A decomposition step is placed at the head of the workflow, and its result is used to generate the following steps dynamically through `replace_remaining`.

## Responsibility boundary (most important)

- **kiro-cli inside the MicroVM**: only "the implementation itself" of each task (clone -> create branch -> work -> commit -> push). It does not merge, create PRs or review.
- **Local-side steps in the workflow (outside the MicroVM)**: creating the integration branch, starting `kiro-microvm run` and collecting its results, merging, deleting merged work branches, and creating the PR. Git operations and merges are not delegated to the MicroVM; the dedicated agents (microvm-run / microvm-merge / microvm-pr) do them.
- **Review/aggregation steps in the workflow**: the multi-model review and the aggregation.
- **This skill (the orchestrator)**: asks for the inputs, assembles and starts the workflow, and mediates user decisions when it pauses.

## Prerequisites

- It uses the same MicroVM execution base as the run-microvm skill (`.kiro/skills/run-microvm/SKILL.md`). The microvm-run agent (`.kiro/agents/microvm-run.md`) runs each task following the run-microvm skill.
- The `kiro-microvm` command must be on PATH. It does not depend on the current directory, so the workflow does not need to run inside the devtool repository. If it is missing, ask the user to install it (`pipx install ./packages/runs-cli`, or run `scripts/install.sh`, which prints the instructions) and stop.
- GitHub flow only. REPO_URL is required (https://github.com/owner/repo.git). Deployment must be done, the secrets (kiro-api-key / github-token) populated, AWS SSO valid and gh authenticated. If anything is missing, stop and report following the guards of the run-microvm skill.
- **The AWS profile is a required input. There is no default fallback** (public-release premise). Ask the user every time in step 1, state it in the workflowPrompt, and pass it to each step.
- The GitHub flow of the MicroVM goes only as far as "clone the given base branch -> create a kiro/<requestId> branch and work -> push"; it does not merge. The base is passed to run-microvm as the base branch (`--base-branch`).
- The author of the implementation commits made on the MicroVM is set at deploy time (`kiro-bot <kiro-bot@users.noreply.github.com>` when unspecified; see the README section on configuring the commit author). The pipeline and individual runs do not change it. The merge commits made by microvm-merge use the author of the local git configuration. If you want both to use the same identity, set the same author as the local one (an email tied to the GitHub account) at deploy time.
- The review follows workflow-review.md (the user's global steering): two parallel lines, a mid-tier Claude and a mid-tier GPT, plus a mid-tier aggregation. Do not use top-tier models or high effort. Resolve model IDs from the live list of the execution environment (do not hard-code them).

## Branch structure

```
main
 └─ integration branch (feature/<feature-name>)                 <- created from main by microvm-merge and pushed to origin
     ├─ lead task      : clone with the integration branch as base -> kiro/<reqId> -> push -> merge into the integration branch -> update origin -> delete kiro/<reqId>
     ├─ parallel task A: clone with the updated integration branch as base -> kiro/<reqId> -> push ┐
     ├─ parallel task B: same as above                                                              ├ merge one by one after all finish -> delete each kiro/<reqId>
     └─ parallel task C: same as above                                                              ┘
     (after all parallel merges) multi-model review of the whole integration branch
     ├─ fix-up         : if there are findings, MicroVM fixes them on a new kiro/<reqId2> -> push -> merge -> delete kiro/<reqId2> -> re-review (max 3)
 -> once APPROVED, create the PR integration branch -> main
```

A work branch kiro/<requestId> is deleted from origin after it has been merged into the integration branch and pushed. See `.kiro/agents/microvm-merge.md` for the confirmation steps and the exceptions.

**Naming the integration branch**: a name that tells what the request (feature) is. Example: `feature/user-auth`. Do not include tool or pipeline names such as `microvm-pipeline`. The feature name is decided from the request in the decomposition step.

## Review and finding-handling policy (confirmed decisions)

- **Review timing**: do not review the parallel tasks individually. **After all parallel tasks have been merged into the integration branch, review the whole integration branch (the diff against main) exactly once** with the multi-model review. The lead task is part of the integration branch, so it is reviewed together. Consistency across tasks is checked here.
- **Fixes always go on a new branch, on MicroVM**: findings for which the aggregated verdict is not APPROVED and the user decides "fix" are fixed on a MicroVM. A fix **must always create a new kiro/<requestId>** (never reuse an existing branch). Use the latest integration branch to be fixed as the base branch, have it cloned, and give the gist of the findings (target files and what to change) in the PROMPT. After the push, microvm-merge merges it into the integration branch and the review runs again.
- **Loop limit**: the review -> fix implementation loop has maxIterations 3. If it does not reach APPROVED in 3 rounds, pause with onMaxIterations.
- **Ask the user after aggregation**: in a round where the aggregated verdict is not APPROVED, do not automatically move to the next fix; pause with send_message(severity=warning), present the aggregated result (blocking/non-blocking, the target file and summary of each finding, and the reason for any finding the aggregator judged rejected), and ask the user to choose "fix / reject / defer". If APPROVED, do not pause and go to the PR.
- **Reject findings that contradict confirmed specifications**: the aggregation step verifies whether each finding concerns the changes of this branch (the diff from base), and keeps the findings that contradict the confirmed policy or design with a rejection reason (do not bury them; keep them).

## Lint policy (required when the repository has a linter)

- **Applies when a lint is defined**: a `lint` script in package.json, or a linter config in the repository (ESLint, Ruff, etc.). If none exists, skip and say so in the step report.
- **When to run**: in every MicroVM implementation task (lead / parallel / fix), run lint before the final commit and push. Run it once more on the integration branch after merge-all and before the pr step.
- **Fixable findings**: fix them (auto-fix or a direct edit) and re-run lint until it passes. Format and style violations are in this group.
- **Findings that need a decision**: do not decide on your own. Examples: a rule that conflicts with the confirmed design, a fix that changes behavior, a rule that would need to be disabled or relaxed. Stop and ask the user (send_message severity=warning with the rule, file, line and the options).
- **Never bypass**: do not add lint-disable comments, loosen the lint config, or skip the step to make it pass without the user's approval.
- **Report**: each step reports the lint command, the result (pass / fixed / pending decision), and the files changed by fixes.

## Recurrence-prevention check step (mandatory, never skipped)
Runs for every target repository, after the fix loop ends (APPROVED, or the loop finished with the user's decision) and before the pr step. Do not skip it even when there were no findings; then report "no findings" and still do the steering inventory part.
- **Inputs**: all review findings of the run (including rejected / deferred ones), failed or retried MicroVM runs (ok=false, failed push, lint failures), and the target repository's steering / rule documents (e.g. `.kiro/steering/`, `packages/image/steering/`).
- **Primary judgement (the agent decides, not the user)**: for each finding or failure, decide "checkable / not checkable" by a deterministic check (lint rule, test, script, hook), with a one-line reason, and decide "add / do not add" with a reason. Prefer a check when the criterion is mechanical and false positives are rare. Not checkable examples: design taste, naming judgement, scope judgement.
- **Steering inventory**: list each constraint of the steering documents and classify it as enforceable / conditionally enforceable / not enforceable with the means or the reason. Pick at most one new candidate per run and record why.
- **No linter in the target repository**: do not install one on your own. Present it as an "adoption proposal" (tool, target, the command, expected effect) and wait for the user.
- **Present before the pr step**: pause with send_message(severity=warning) and show the table (finding / checkable or not / reason / add or not), the inventory result and any adoption proposals. Adjust to the user's answer. Never create the PR before the answer.
- **Executing additions**: per the user's answer, either (a) throw the check as a separate task to MicroVM (new `kiro/<requestId>`, merged into the integration branch before the pr step, with tests that run and pass) or (b) leave it as an Issue (the body states the finding, the proposed check and the done criteria). Default proposal: (a) for a small check with a clear criterion, (b) when a design decision is involved. Replace the corresponding steering wording when a check takes over a constraint.
- **Secret scan (this repository)**: on the integration branch after merge-all and before the pr step, the general agent runs `python3 scripts/check_secrets.py <base>` with the base decided in step 1 (default origin/main). Any non-zero exit stops before the pr step: 1 = detected, 2 = execution error; report either with send_message warning and pause. It scans only added lines of the diff; logs and commit messages are out of scope. The script is not in the MicroVM image, so it is not run inside MicroVM tasks.
- **Record**: write the result into the repository's documentation location (`docs/` if there is no convention) and put its gist in the PR body.
## Procedure

### 1. Ask the user for the inputs

- **AWS_PROFILE** (required): the AWS profile name to use. Never assume a default; always ask (no fallback, for public release). State it in the workflowPrompt and pass it to each MicroVM execution step as `AWS_PROFILE=<profile>`.
- **REPO_URL** (required): the target repository (https://github.com/owner/repo.git).
- **Request** (required): the overall picture of the work to implement.
- The base defaults to main (the branch the integration branch is created from). Ask if another origin is needed.

### 2. Assemble and start one workflow

Describe one pipeline in the workflowPrompt of run_workflow and start it. The result of the leading split step is used to generate the following steps dynamically through `replace_remaining` (microvm-split does the generation). Each step uses the dedicated agent below. The rule text lives in each agent definition; do not copy it into the workflowPrompt.

| Step | Responsible agent |
| --- | --- |
| split | microvm-split |
| setup | microvm-merge |
| lead (if there is a lead task) | microvm-run -> microvm-merge |
| parallel (join allSettled, max 3) | microvm-run |
| merge-all | microvm-merge |
| review | semantic_reviewer x 2 (mid-tier Claude / mid-tier GPT) + wf-review-aggregator |
| fix-loop (maxIterations 3) | microvm-run -> microvm-merge -> re-review |
| check-proposal (mandatory) | general agent runs the judgement and presents to the user (no wf-coder); if (a), hand the check to a new MicroVM task: microvm-run -> microvm-merge |
| pr | microvm-pr |

Always include in the workflowPrompt:
- Run-specific values: AWS_PROFILE, REPO_URL, the request, and the base (default main).
- Each step uses the dedicated agent in the table above.
- The gist of the review policy: once after all parallel merges, mid-tier Claude + mid-tier GPT + mid-tier aggregation (model IDs resolved from the live list, never hard-coded). Fixes always go on a new branch on MicroVM, maxIterations 3, pause and ask the user in a round that is not APPROVED. Keep findings that contradict confirmed specifications with a rejection reason.
- The gist of the lint policy: if the repository has a linter, run it in every implementation task before push and on the integration branch before the pr step. Fix what can be fixed; pause with send_message(warning) for anything that needs a decision. Never bypass lint.
- The gist of the recurrence-prevention check step: mandatory between the fix loop and the pr step, never skipped. Judge each finding as checkable / not checkable, inventory steering constraints, propose a linter instead of installing one, and pause to present the result before the pr step.
- Language of the pipeline artifacts: the prose of artifacts is written in Japanese. Commits are a single-line Conventional Commits message (in Japanese). PR bodies are written in Japanese.

### 3. Mediation when paused (you)

When the workflow pauses with send_message(warning), mediate to the user according to the content:

- **A MicroVM run of the lead / parallel / fix step has ok=false, a failed push, or did not run (expired credentials, etc.)**: present the facts and ask the user to choose retry, abort or manual handling. Do not push ahead on your own.
- **The review aggregation is not APPROVED**: present the aggregated result (blocking/non-blocking, the target file and summary of each finding, the rejected judgements and their reasons) and ask the user to sort each finding into "fix / reject / defer".
  - "Fix": continue the fix-loop with update_workflow (extend_repeat or resume). In the next round's fixing MicroVM run, state explicitly which findings to fix and which to reject or defer. Tell the workflow that rejected or deferred findings should be left in the matching review finding with status=rejected / rejectionReason.
  - "No action needed (misjudgement / non-blocking)": complete the loop with update_status and proceed to the pr step.
- **A lint finding needs a decision**: present the rule, file, line and the options (fix by behavior change, relax the rule, defer) and ask the user to choose. Do not disable or relax lint on your own.
- **Recurrence-prevention check proposal**: present the table and adoption proposals, ask the user to adjust (add / do not add, MicroVM task or Issue), then continue to the pr step.
- **maxIterations reached**: summarize the remaining findings and ask the user whether to run one more round (extend_repeat) or to finish with the findings left (update_status completed). Do not rewrite the verdict to APPROVED on your own.

## Notes

- Do not push ahead automatically on a failure (ok=false, failed push, not run, non-trivial conflict) or an unapproved review. Always pause and ask the user to decide.
- Keep the whole process in a single run_workflow. Do not start a separate run_workflow for each phase.
- The merge target is always the integration branch. Never merge directly into main (the final step goes through a PR).
- Follow the microvm-merge definition for conflicts during sequential merges. Ask the user to decide non-trivial conflicts.
- Keep this skill aligned with the behavior of the `kiro-microvm` CLI and the run-microvm skill. If they change, follow with the dedicated agent definitions and this skill.
