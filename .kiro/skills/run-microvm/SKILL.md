---
name: run-microvm
description: Skill that sends a request to kiro-cli running on a MicroVM, collects the run results from S3 and returns them as-is. Use it when the user explicitly asks to start it, for example "ask the microVM", "run this with run-microvm" or "let a VM do the work". Success judgement, retries and follow-up processing are not done here; they are left to the caller (the orchestration).
---

# run-microvm

Send one request to kiro-cli running on a MicroVM, collect the result files written to S3 after completion, and return the contents of `status.json` together with the list of collected file paths. The execution logic lives in the `kiro-microvm` CLI (`kiro-microvm run`). This skill is a thin procedure that calls it correctly and presents the result unchanged.

## Scope and responsibility boundary

- **What this skill does**: ask the user for the inputs (AWS_PROFILE / PROMPT / REPO_URL / BASE_BRANCH), run `kiro-microvm run --json`, and present the results collected into the directory reported as `resultsDir`.
- **What this skill does not do**: judge success from `ok` / `exitCode`, retry, check for PAT leaks, do post-push follow-up, or create PRs. **The caller orchestrates these.** Return the result unprocessed.
- The target is kiro-cli inside the MicroVM. This is not about changing the behavior of the local Kiro (you).

## Prerequisites

- The `kiro-microvm` command must be on PATH. It does not depend on the current directory, so there is no need to `cd` anywhere or to be inside a particular repository. If `command -v kiro-microvm` finds nothing, tell the user to install it (`pipx install ./packages/runs-cli` from the devtool repository root, or run `scripts/install.sh`, which prints the instructions) and stop.
- The stacks (Base/Image) and the MicroVM image must be deployed, the secret `kiro-microvm/kiro-api-key` must have a value, and `kiro-microvm/github-token` must have a value if the GitHub flow is used. If not deployed or not populated, tell the user and stop (this skill does not deploy or populate PATs).
- **The AWS profile is a required input. There is no default fallback** (no environment-specific profile name is hard-coded). The CLI reads it from the `AWS_PROFILE` environment variable or from `~/.config/kiro-microvm/config`, and stops with an error if neither is set. Ask the user every time (step 1), or use the value you were given.
- The region is `ap-northeast-1` (the region where MicroVMs are offered). If the SSO token has expired (`Token has expired`), ask the user to run `aws sso login --profile <specified profile>` and stop.
- The commit author of kiro-cli inside the MicroVM is set at deploy time (environment variables `KIRO_GIT_AUTHOR_NAME` / `KIRO_GIT_AUTHOR_EMAIL`, or the CDK context `gitAuthorName` / `gitAuthorEmail`; see the README section on configuring the commit author). It defaults to `kiro-bot <kiro-bot@users.noreply.github.com>`. This skill cannot change it per run (do not ask for it as an input).

## Procedure

### 1. Ask the user for the inputs (every time)

Do not assume defaults; confirm each time:

- **AWS_PROFILE** (required): the AWS profile name to use. Never assume a default; always ask (or use the value you were given). Prefix the command with it as `AWS_PROFILE=<profile>`.
- **PROMPT** (required): the work for kiro-cli inside the MicroVM. The CLI passes the PROMPT **via S3** (input key `<requestId>/inputs/prompt.md`, UTF-8). Long prompts, in any language, can be passed as-is; no manual compression or translation is needed (it is not embedded in the payload, so the 4096-byte limit does not apply).
- **REPO_URL** (optional): the target repository when the GitHub flow (clone -> create branch -> work -> commit -> push -> generate report) is wanted. Only the form `https://github.com/owner/repo.git` is accepted (limited to `https://github.com/` URLs).
  - **If empty, there is no GitHub flow**: only the PROMPT work runs and the artifacts go to S3 (the classic mode). Ask whether repository work is wanted, and ask for REPO_URL only if it is.
- **BASE_BRANCH** (optional): the starting branch when REPO_URL is given. Default `main`. Use the default unless told otherwise.

### 2. Run it

Run the command below from any directory. It can take a long time (the VM lifetime is at most 1800s and the host polling limit is 1860s), so use a long enough timeout (about 1800000ms).

Replace `<profile>` with the AWS profile name the user gave in step 1. Pass the PROMPT through the `PROMPT` environment variable (this avoids quoting problems with prompts that start with `-` or contain special characters).

GitHub flow (with REPO_URL):

```bash
AWS_PROFILE=<profile> \
PROMPT="<the work requested by the user>" \
kiro-microvm run --json --repo-url "https://github.com/owner/repo.git"
```

Classic mode (without REPO_URL):

```bash
AWS_PROFILE=<profile> PROMPT="<the work requested by the user>" kiro-microvm run --json
```

- To change the base branch, add `--base-branch "<branch>"`. To fix the request ID, add `--request-id <id>` (otherwise one is generated).
- Progress is written to **stderr**. With `--json`, the **last line of stdout** is a JSON object `{"requestId","resultsDir","ok","exitCode","microvmId"}`. Parse that line.
- `kiro-microvm run` generates the requestId, puts the PROMPT at the input key `<requestId>/inputs/prompt.md`, builds the payload (it carries `promptKey`, not the text), starts the VM, runs kiro-cli in the background through the `/run` hook, polls S3 for `status.json`, downloads the results into `resultsDir`, and terminates the VM. Do not write your own wait loop for waiting, collection or cleanup.
- Before sending the payload to the VM, the CLI **validates the JSON payload size against 4096 bytes** and stops with a clear error message if it is exceeded (normally it is not, because the PROMPT goes via S3; if it is, another field such as the repo URL is oversized. Do not put the text back into the payload).
- **The exit code of `kiro-microvm run` is 0 even when `ok` is false.** Do not rely on the exit code; the caller judges `ok` from the JSON line.
- `resultsDir` is the directory of this run (it already includes the requestId). The default location is under `~/.local/share/kiro-microvm/results/` (overridable with `KIRO_MICROVM_RESULTS_DIR`). Always take the path from `resultsDir` in the JSON line; never assume a relative path such as `results/<requestId>/`.

### 3. Triage failures (report them as they are)

- If the CLI stops with an error such as `status.json not found`: `status.json` did not appear in the VM (for example the VM lifetime expired before completion was detected). Report that fact as it is, adding that CloudWatch Logs (`/aws/lambda-microvms/kiro-microvm`) should be checked. The final success judgement and retries are left to the caller.
- For failures caused by SSO, deployment or secrets that are not in place, ask the user to fix them and stop, as described in the prerequisites.

### 4. Return the result "as-is"

Once the results are collected into `resultsDir`, present the following without processing or judging:

- The **requestId**, the **S3 prefix** (`s3://<bucket>/<requestId>/runs/`; the input prompt is at `s3://<bucket>/<requestId>/inputs/prompt.md`), and the **local path** (`resultsDir`).
- **The list of collected file paths** directly under `resultsDir`. Typical files: `status.json` / `report.json` / `report.md` / `kiro-stdout.txt` (the final response text of kiro-cli) / `kiro-events.jsonl` (the stream-json event stream of kiro-cli, JSON Lines) / artifacts. Show only the paths; the caller opens the contents if needed.
- **The full contents of `status.json`, unchanged** (it is the caller's starting point for judging success; this is the only file whose contents you return). Read it from `<resultsDir>/status.json`.
- `status.json` may contain `creditsUsed` (number | null, the credits used by kiro-cli; the key is absent for old runs and runs on old images). Present `status.json` in full as usual. This skill does not sum or interpret `creditsUsed` (summing is the caller's responsibility).
- For the GitHub flow, always show the path of `report.json` as well (if it exists).

**Do not add**: an interpretation of success (such as "it succeeded"), a PAT leak check, a retry suggestion, or post-push follow-up. Return only the facts (the paths and the contents of `status.json`) and leave the judgement to the caller.

## Notes

- The two-stage masking in the VM hook server (`ksk_`, `(gh[pousr]|github_pat)_` and so on) has already been applied by the time results reach S3. This skill adds no extra masking (the policy is to return results as they are).
- Keep this procedure aligned with the behavior of the `kiro-microvm` CLI. If the CLI changes, follow it here.
