# kiro-workflow-microvm-devtool

Developer tooling to run [Kiro CLI](https://kiro.dev) headless inside AWS Lambda MicroVMs. It is a pnpm monorepo.

A MicroVM runs `kiro-cli` on a task you give it and stores the outputs in S3. When you pass a GitHub repository, Kiro clones it, creates a branch, does the work, commits, pushes, and writes a work report (`report.json` / `report.md`). The `kiro-microvm` CLI starts runs and inspects their results.

> **Note:** The specification and availability of Lambda MicroVMs may change. Check the latest AWS documentation.

## Security and cost warnings (read first)

You run everything below in your own AWS account and with your own GitHub permissions. You are responsible for what it does.

- **Kiro runs with `--trust-all-tools`.** There is nobody in the guest to approve tool calls, so Kiro can run any command with the guest's credentials. Approval prompts are not a safety barrier here. The barriers are the least-privilege execution role (see [Design notes](#design-notes)) and the network settings.
- **Use a fine-grained GitHub PAT limited to a single repository.** Do not use a classic PAT or an all-repositories token. Based on the current code, the guest only clones and pushes over HTTPS, so the minimum permissions are:
  - Contents: read and write (clone, create the branch, push)
  - Metadata: read (granted automatically)
  - Pull requests: read and write is only needed if you let Kiro or your own tooling open or update pull requests with this same token. The current guest flow does not call the pull request API.
- **Outbound traffic is not restricted.** The image uses the `INTERNET_EGRESS` network connector, so the guest can reach any destination. A prompt-injected or misbehaving Kiro could send data anywhere it can read, including the PAT (see [Limits of approach A](#limits-of-approach-a-pat-extractability)).
- **You are billed for MicroVM run time.** A MicroVM lives for at most 1800 seconds (`--maximum-duration-in-seconds`). Each run can cost up to that duration, plus S3, Secrets Manager, CloudWatch Logs, and Kiro credit usage. Failed or hung runs still consume time until they time out or are terminated.
- **Limits of approach A.** The PAT is hidden from prompts, URLs, and outputs, but it is not physically blocked from the guest. See [Limits of approach A](#limits-of-approach-a-pat-extractability).

### Known limitations / future work

These are not implemented. They are listed as constraints and future work.

- **Replace the PAT with a GitHub App installation token.** Installation tokens expire after 1 hour and can be scoped to specific repositories and permissions, which limits the damage if a token leaks.
- **Restrict egress destinations** to GitHub, the Kiro API, and AWS only, instead of open `INTERNET_EGRESS`.

## Structure

```
.
├─ pnpm-workspace.yaml
├─ package.json
├─ packages/
│  ├─ infra/        # CDK (TypeScript): S3 x2 + IAM x2 + CfnMicrovmImage
│  ├─ image/        # MicroVM image contents (code artifact)
│  │  ├─ Dockerfile       # al2023-minimal + awscli + Node 22 + pnpm + kiro-cli (musl) + Python hook server
│  │  ├─ hook_server.py   # /ready returns 200; /run runs Kiro CLI and puts outputs + status.json to S3
│  │  ├─ steering/        # global steering for kiro-cli inside the MicroVM only
│  │  └─ test_hook_server.py
│  └─ runs-cli/     # the `kiro-microvm` Python CLI (run / list / show / stats)
├─ scripts/
│  ├─ deploy.sh           # Base -> zip upload -> Image in one command (pnpm deploy)
│  ├─ install.sh          # installs the skills and agents into ~/.kiro
│  └─ package-image.sh    # zips packages/image and uploads it to the artifact bucket
├─ .kiro/
│  ├─ skills/       # run-microvm (one request on a VM) / microvm-pipeline (split pipeline)
│  └─ agents/       # per-step agents for the pipeline (split / run / merge / pr)
├─ docs/
│  └─ microvm-pipeline-agents.md   # notes on the dedicated-agent design
└─ README.md
```

## Prerequisites

- AWS CLI 2.38+ (`lambda-microvms` subcommand). Confirmed with 2.39.0.
- CDK CLI 2.1146 series / `aws-cdk-lib` 2.275.0 (includes `CfnMicrovmImage`)
- Node 22, pnpm 12, and a running Docker (on your machine)
- Python 3.10+ and [pipx](https://pipx.pypa.io/) for the CLI
- Region `ap-northeast-1` (MicroVMs availability confirmed there)
- An AWS profile, passed as `AWS_PROFILE=<your-profile>`

## Setup

The stack is split in two to avoid a chicken-and-egg problem (`CfnMicrovmImage` needs the zip, but the image build runs during deploy):

- `KiroMicrovmBase`: S3 x2 + IAM x2 + LogGroup (no zip needed)
- `KiroMicrovmImage`: `CfnMicrovmImage` (zip required)

### 1. Deploy (once, at setup)

Deploy is not part of the CLI. Run it from this repository.

```bash
# Install dependencies
pnpm install

# (First time only) CDK bootstrap
AWS_PROFILE=<your-profile> pnpm --filter infra exec cdk bootstrap aws://<ACCOUNT_ID>/ap-northeast-1

# Deploy Base -> zip upload -> Image (scripts/deploy.sh).
# Re-running after changing the image contents makes CloudFormation detect the diff and rebuild.
AWS_PROFILE=<your-profile> pnpm deploy
```

`pnpm deploy` runs BaseStack, then `package-image.sh`, then ImageStack. You can also run `scripts/deploy.sh` and `scripts/package-image.sh` directly.

### 2. Put the secrets

Put the real values into Secrets Manager. They are never baked into IaC or the image.

```bash
# Kiro API key
AWS_PROFILE=<your-profile> aws secretsmanager put-secret-value \
  --secret-id kiro-microvm/kiro-api-key --secret-string 'ksk_...' --region ap-northeast-1

# GitHub PAT (only for the GitHub flow). Use a fine-grained PAT limited to one repository.
AWS_PROFILE=<your-profile> aws secretsmanager put-secret-value \
  --secret-id kiro-microvm/github-token --secret-string 'github_pat_...' --region ap-northeast-1
```

### 3. Install the CLI

```bash
pipx install ./packages/runs-cli
```

This puts `kiro-microvm` on your `PATH`. You can run it from any directory.

### 4. Configure the CLI

Settings come from `~/.config/kiro-microvm/config` (`KEY=VALUE` lines) and from environment variables. Precedence: CLI argument > environment variable > config file.

| Key | Default | Notes |
| --- | --- | --- |
| `AWS_PROFILE` | none | Required |
| `AWS_REGION` | `ap-northeast-1` | |
| `STACK_NAME` | `KiroMicrovmBase` | |
| `MICROVM_IMAGE_NAME` | `kiro-microvm` | |
| `KIRO_MICROVM_RESULTS_DIR` | `~/.local/share/kiro-microvm/results/` | Where results are stored |

```bash
mkdir -p ~/.config/kiro-microvm
echo 'AWS_PROFILE=<your-profile>' > ~/.config/kiro-microvm/config
```

## Usage

```bash
kiro-microvm run [PROMPT] [--repo-url URL] [--base-branch B] [--request-id ID] [--json]
kiro-microvm list [--json]
kiro-microvm show REQUEST_ID [--json]
kiro-microvm stats [--json]
```

```bash
# Run a task and collect the outputs
kiro-microvm run "Look up the weather in Tokyo and create weather.html"

# GitHub flow: clone -> branch -> work -> commit -> push -> work report
kiro-microvm run "Add an overview to the README" \
  --repo-url https://github.com/owner/repo.git --base-branch main

# Inspect results
kiro-microvm list
kiro-microvm show <request-id>
kiro-microvm stats        # success rate, total credits used, runs without credit data
```

Results are collected under `~/.local/share/kiro-microvm/results/<requestId>/` (override with `KIRO_MICROVM_RESULTS_DIR`). The CLI waits for `status.json` to appear, prints `ok` and `exitCode` (and whether a work report exists), and terminates the MicroVM.

### Run input (`--run-hook-payload` JSON)

The CLI builds the payload. The prompt is passed through S3, not inline, because `runHookPayload` is limited to 4096 bytes (a non-ASCII prompt grows about 3x in JSON; GitHub issue #3). The CLI puts the prompt at `<requestId>/inputs/prompt.md` (UTF-8, tagged `kind=input`) in the result bucket and puts `promptKey` in the payload. `hook_server` fetches that object as the prompt.

- `promptKey`: S3 key of the input prompt, `<requestId>/inputs/prompt.md`.
- `prompt` (backward compatible): inline prompt, for short prompts only. Used when `promptKey` is absent. A default prompt is used if both are absent.
- `prefix`: output prefix, `<requestId>/runs/`.
- `requestId`: request ID, used to build the `inputs/` and `runs/` keys.
- `repoUrl`: GitHub HTTPS URL to clone. Setting it enables the GitHub flow. Without it, the prompt runs with no clone.
- `baseBranch`: branch to start from. Defaults to `main`.

The CLI checks the payload JSON size against 4096 bytes before sending and stops with an error if it is exceeded.

### Expected output

`<requestId>/runs/` contains `status.json`, `kiro-stdout.txt`, `kiro-events.jsonl`, and the generated files. The GitHub flow adds `report.json` and `report.md`. Strings starting with `ksk_` and GitHub PATs must not appear in CloudWatch Logs, any S3 object, `report.*`, `status.json`, or host stdout.

### Commit author (set at deploy time)

The author and committer of commits made by Kiro in the MicroVM are set at deploy time.

- **Default:** `kiro-bot <kiro-bot@users.noreply.github.com>`.
- **How to set it:**

```bash
# Environment variables (inherited by the cdk process; deploy.sh needs no change)
KIRO_GIT_AUTHOR_NAME="Taro Yamada" KIRO_GIT_AUTHOR_EMAIL="12345+taro@users.noreply.github.com" \
  AWS_PROFILE=<your-profile> pnpm deploy

# CDK context
AWS_PROFILE=<your-profile> pnpm --filter infra exec cdk deploy KiroMicrovmImage \
  -c gitAuthorName="Taro Yamada" -c gitAuthorEmail="12345+taro@users.noreply.github.com"
```

- **Precedence:** context `gitAuthorName` / `gitAuthorEmail` > environment `KIRO_GIT_AUTHOR_NAME` / `KIRO_GIT_AUTHOR_EMAIL`. Values are trimmed and an empty string counts as unset. Name and email are independent: setting one leaves the other at its default.
- **How it takes effect:** ImageStack injects each specified item as the image environment variable of the same name. `hook_server.py` reads it at run time and uses it for the commit env (`GIT_AUTHOR_*` / `GIT_COMMITTER_*`) and for the `git config user.name` / `git config user.email` instructions in the prompt.
- **Validation:** after trimming, neither value may contain control characters (U+0000 to U+001F, U+007F) or any of `"` `\` `` ` `` `$` `<` `>`. The email must also match `^[^@\s]+@[^@\s]+$`. The values are executed in a shell as `git config user.name "<value>"`, so characters that break quoting or expansion are rejected.
- **Invalid values:** ImageStack throws at synth time and does not deploy. On the image side, an invalid value logs a warning (variable name only, never the value) and falls back to the default.
- **When it applies:** after redeploy (`pnpm deploy`). It cannot be changed per run.
- **Avatar on GitHub:** use an email linked to your GitHub account (a `<id>+<user>@users.noreply.github.com` noreply address, or a verified email).
- **Local commits:** merge commits you make locally use your local git config and are not affected.

To change the kiro-cli download URL or version, edit the `ARG KIRO_CLI_DL_URL` default in `packages/image/Dockerfile` directly (the image is built on the AWS side, so there is no build-arg path), then run `scripts/package-image.sh` and `cdk deploy KiroMicrovmImage`.

## Split pipeline (skills and agents)

The pipeline splits one request into a leading (shared foundation) task and parallel tasks, implements each on a MicroVM with kiro-cli, merges into an integration branch, runs multi-model review, handles findings, and opens a PR, all in one `run_workflow`.

- Skills: `.kiro/skills/microvm-pipeline/SKILL.md` (overall structure, review policy, pause relay) and `.kiro/skills/run-microvm/SKILL.md` (one request).
- Step agents: `microvm-split` / `microvm-run` / `microvm-merge` / `microvm-pr` in `.kiro/agents/`. They are the primary source for step-specific rules.
- Background and design rationale: [docs/microvm-pipeline-agents.md](docs/microvm-pipeline-agents.md)
- Prompt mix-up check: uses `promptSha256` in `status.json` (SHA-256 of the unmasked prompt). The `prompt` body can be replaced with `[REDACTED]` by `mask_secrets`, so it is not used for matching.

### Prerequisites for using the skills and agents

1. Complete [Setup](#setup): deploy the stacks and put the secrets.
2. Install the CLI so that `kiro-microvm` is on your `PATH` (`pipx install ./packages/runs-cli`).
3. Configure it (`AWS_PROFILE` at minimum), as in [Configure the CLI](#4-configure-the-cli).
4. Install the skills and agents into `~/.kiro/skills` and `~/.kiro/agents` with `scripts/install.sh`:

```bash
scripts/install.sh            # copy
scripts/install.sh --link     # symlink (for development)
scripts/install.sh --force    # overwrite existing files
scripts/install.sh --dry-run  # show what would happen
scripts/install.sh --uninstall
```

Your current directory does not need to be this repository. Use the skills from your own project's directory.

## Design notes

- **CDK only builds up to the MicroVM image.** The running instance (`Microvm`) is not a CloudFormation resource and is started with the `run-microvm` API, which `kiro-microvm run` calls.
- **Async execution model.** The Dockerfile `CMD` keeps the hook server running. `/ready` returns 200 and the snapshot is taken. `run-microvm` restores it. The `/run` hook starts the Kiro CLI run (`kiro-cli chat --trust-all-tools --no-interactive --output-format stream-json "<prompt>"`) and the S3 puts in a background thread and returns 200 (`{"status":"accepted"}`) at once, because `/run` `RunTimeoutInSeconds` is capped at 60 seconds. When Kiro finishes, it puts the generated files, `kiro-stdout.txt`, `kiro-events.jsonl`, and `status.json` under the S3 prefix.
  - `kiro-stdout.txt` holds the final response text (`runFinished.data.finalText`, or the whole stdout if unavailable). The raw event stream (JSON Lines) is in `kiro-events.jsonl`, passed through `mask_secrets` like `kiro-stdout.txt`.
  - `status.json` `creditsUsed` (number | null) is the total credits of the last `session_usage` event, unrounded, and `null` on timeout, missing events, or parse failure. It does not affect `ok`.
  - Success is judged by `status.json` fields `ok` / `exitCode` / `timedOut` / `failedUploads`. Only preconditions known before the background start (for example `RESULT_BUCKET` unset) make `/run` return 500.
- **Kiro CLI (musl build).** The Dockerfile installs `kirocli-aarch64-linux-musl.zip`. AL2023 has glibc 2.34, below the 2.39 the gnu build requires, so the statically linked musl build is used. Authentication is not baked in (`Q_SKIP_SETUP=1`).
- **How `KIRO_API_KEY` is passed.** The real key is not in IaC or the image. `image-stack.ts` injects `KIRO_API_KEY_SECRET_ARN` (the ARN of `kiro-microvm/kiro-api-key` only). At `/run` the guest fetches it with `aws secretsmanager get-secret-value` using the execution role's short-lived credentials. The value is kept out of CloudWatch Logs, S3 outputs, `status.json`, and host stdout by two-stage masking: exact-match replacement of the real value, plus a `ksk_\S+` pattern replacement.
- **GitHub flow (clone, branch, work, commit, push, report).** For a run with `repoUrl`, `hook_server` wraps the prompt with pre- and post-steps and has **Kiro itself** clone, run `git config` (fixed author), create branch `kiro/<requestId>`, work, commit, push, and write the report. `hook_server` does not run git on its behalf; it only sets up the credential helper and gives the steps. `baseBranch` defaults to `main`.
- **GitHub authentication (approach A: the PAT is not shown to Kiro).** The PAT lives in Secret `kiro-microvm/github-token`. `base-stack.ts` grants the execution role read on that Secret, and `image-stack.ts` injects `GITHUB_TOKEN_SECRET_ARN` (ARN only). `hook_server` places a `GIT_ASKPASS` script under Kiro's `HOME` and passes `GIT_ASKPASS` / `GIT_TERMINAL_PROMPT=0` to Kiro's child processes. Only when git asks for credentials does the script fetch the PAT with `aws secretsmanager get-secret-value`. The PAT value never appears in the prompt, URLs, stdout, or the script body. The helper's temporary files are always deleted after the run.
  - **Masking:** the PAT is masked like `KIRO_API_KEY`: exact-match replacement, plus patterns `(gh[pousr]|github_pat)_\S+` (including the fine-grained `github_pat_` form) and the old 40-hex form. The 40-hex pattern collides with git commit SHAs and blob hashes, so it is used only for masking logs, `status.json`, and stdout, never for the fail-closed decision to skip uploading outputs.
  - **Leak checks:** artifacts, file names, and `report.*` are checked against the exact values of both `KIRO_API_KEY` and the PAT, so a PAT value that matches no known prefix also causes a skip.
  - **Fail-closed cases:** with `repoUrl` set, Kiro is not run and the run ends with `ok=false` and a reason in `failedUploads` for: malformed `repoUrl` (`repoUrl-rejected`), missing `GITHUB_TOKEN_SECRET_ARN` (`github-token-arn-missing`), PAT fetch failure (`github-token-fetch`), and credential helper setup failure (`git-credential-setup`). Runs without `repoUrl` are unaffected.
  - **Input validation:** `repoUrl` must be directly under `https://github.com/`. URLs with userinfo such as `user:pass@`, other hosts, and non-HTTPS are rejected. `baseBranch` and the `requestId` in the branch name are normalized to safe characters (alphanumerics and `-_./`).
  - **Author:** the author and committer are also fixed through `GIT_AUTHOR_*` / `GIT_COMMITTER_*`, in addition to the prompt's `git config` instructions. The helper's username answer stays `kiro-bot` (GitHub token auth ignores it).
  - **Required reports:** `report.json` / `report.md` are required artifacts. If they are missing, skipped by secret detection, or fail to upload, that is recorded in `failedUploads` and `ok=false`.

### Limits of approach A (PAT extractability)

Approach A guarantees that the PAT is not placed in prompts, URLs, or outputs. It does not physically block access. A `--trust-all-tools` guest can run the `GIT_ASKPASS` helper or `aws secretsmanager get-secret-value` itself and obtain the PAT in principle. The defense therefore does not rest on hiding the PAT. It rests on:

- Least privilege on the execution role: read on that one Secret, `s3:GetObject` only on `<bucket>/*/inputs/*`, and `s3:PutObject` only on `<bucket>/*/runs/*` of the result bucket (explicit policy).
- Egress control (`INTERNET_EGRESS`), which is currently open. See [Known limitations / future work](#known-limitations--future-work).

Use a single-repository fine-grained PAT so that extraction exposes as little as possible.

### Other notes

- **Git config scope (to confirm on first real run).** Approach A replaces Kiro's child-process `HOME` with a dedicated directory for the `GIT_ASKPASS` helper. This can affect config lookup of non-git tools such as kiro-cli and the aws CLI. On the first real `run-microvm`, check that Kiro and the aws CLI behave normally. If not, switch to limiting the change to git with `GIT_CONFIG_GLOBAL` and similar, instead of replacing `HOME`.
- **Global steering (MicroVM kiro-cli only).** Steering shared by all runs is placed in `packages/image/steering/*.md` and copied to `/root/.kiro/steering/` in the Dockerfile. `ENV KIRO_HOME=/root/.kiro` keeps the load path constant even though the GitHub flow replaces `HOME` per run. As a fallback, the GitHub flow prompt tells Kiro to copy `/root/.kiro/steering/<name>.md` into the clone's `.kiro/steering/` if absent and to exclude it from `git add`. Updating the steering requires a redeploy. This steering affects only the headless kiro-cli in the MicroVM, not your local Kiro IDE or the repository's `.kiro/steering/`.
- **Time limits (async).**
  - `/run` `RunTimeoutInSeconds` = 60 (`image-stack.ts`): the limit until `/run` returns 200 (service model max = 60). It returns at once, so 60 is enough.
  - Guest kiro-cli timeout = `KIRO_TIMEOUT_SECONDS` (`hook_server.py` default 1200, overridable by env).
  - MicroVM lifetime `--maximum-duration-in-seconds` = 1800 (set by the CLI): covers the background Kiro run.
  - Host polling limit = 1860 s (372 x 5 s, in the CLI): slightly longer than the VM lifetime.
  - Keep `KIRO_TIMEOUT_SECONDS (1200) < --maximum-duration (1800) < host polling limit (1860)`. This lets the guest write `status.json` even on timeout, lets Kiro finish before the VM expires, and keeps the poll from giving up before the VM lifetime ends.
- **Memory.** `minimumMemoryInMiB=2048`. This leaves headroom for the Rust kiro-cli, tool runs, and child processes; the peak limit is automatically up to 4x. Not measured. If OOM occurs, raise `minimumMemoryInMiB` to 4096 in `image-stack.ts` or limit Node with `NODE_OPTIONS=--max-old-space-size=1536`.
- **Node and pnpm in the MicroVM (issue #16).** The image includes Node 22 (AL2023 dnf `nodejs22` + `nodejs22-npm`) and pnpm (`npm install -g pnpm@12.10.1`, `ARG PNPM_VERSION`), so Kiro can run `pnpm install` and `pnpm --filter infra exec tsc --noEmit` on a pnpm monorepo such as this one. The rationale (dnf vs official tar, npm vs corepack) is in the Dockerfile comments. Applying it needs a redeploy (`pnpm deploy`). **The build result is unconfirmed**, since the build runs on the AWS side.
- **`cdk synth` inside the MicroVM (from reading the code, not run).**
  - `packages/infra/artifact-hash.txt` is gitignored and absent after a clone. `image-stack.ts` checks with `fs.existsSync` and uses the placeholder key `PLACEHOLDER-run-package-image-sh-first`, so synth does not throw. The output is not deployable.
  - `bin/infra.ts` fixes `env.region` to `ap-northeast-1`, and `account` comes from `CDK_DEFAULT_ACCOUNT`. There are no context lookups, so synth needs no AWS API calls.
  - `cdk.json` runs `npx ts-node`, so each synth type-checks, which may take tens of seconds to a minute. If it fails, the first thing to try is `TS_NODE_TRANSPILE_ONLY=1`.
  - It is likely to pass, but confirm with `pnpm --filter infra exec cdk synth` after redeploy.
- **Image size and build time (rough, unmeasured).** `nodejs22` + `nodejs22-npm` add roughly 100 to 150 MiB and pnpm tens of MiB. Expect about 1 minute more build time. The `/ready` snapshot grows similarly, so check startup time after redeploy.
- **Completion condition.** After `/run` returns 200, the CLI polls for `status.json`, which always appears after the background Kiro run ends, whether it succeeded or not. `ok` means `exitCode==0 && !timedOut && no failedUploads`.
- **Authentication and permissions.** The guest gets short-lived execution-role credentials automatically, so `aws s3 cp` works directly. No long-lived keys are needed.
- **Code artifact zip.** `package-image.sh` places the zip outside CDK under the content-hash key `code/app-<hash>.zip` and writes the hash to `packages/infra/artifact-hash.txt`. `ImageStack` reads it to determine the URI. When the zip changes, the key (and URI) changes, so CloudFormation detects the diff and rebuilds.
- **Always update the image through CloudFormation.** Calling `update-microvm-image` directly drops properties you did not specify (environment variables, hooks) and causes drift, so do not use it.
- **Per-request prefix.** The CLI generates a request ID (`req-<datetime>-<pid>`) and passes the prefix in the `--run-hook-payload` JSON. The `/run` body arrives as `{"microvmId","runHookPayload"}`. `hook_server` re-parses `runHookPayload` (a string) and stores outputs under `<requestId>/runs/`. If no prefix is given, it falls back to `<microvmId>/runs/`.

## Verification status

Confirmed on real infrastructure:
- `lambda-microvms` is available in Tokyo; the base image is `al2023-1` version `2`.
- `CfnMicrovmImage` enums: architecture=`ARM_64`, hooks ready/run=`ENABLED`/`DISABLED`.
- `egressNetworkConnectors` takes the managed connector ARN (`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS`).
- A build without the zip fails with `"The artifact was not found in S3."`, which the two-stack split resolves.

Confirmed end to end (2026-10-28):
- Image build succeeded (Graviton3/4 both SUCCESSFUL). The Dockerfile dnf step (python3/awscli) and the /ready hook work.
- `run-microvm` (INTERNET_EGRESS) -> /run hook -> `aws s3 cp` with the execution role -> output in the result bucket -> collected locally -> terminate.
- The `run-microvm` response identifier is `microvmId`.

After that, the echo was replaced with a headless Kiro CLI run, and a task ("look up the weather in Sapporo and create weather.html") produced outputs under `<requestId>/runs/` (no `KIRO_API_KEY` leak, `ok:true`).
