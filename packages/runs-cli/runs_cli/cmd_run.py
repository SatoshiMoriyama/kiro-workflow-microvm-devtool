"""run subcommand: start a MicroVM, wait for results on S3, and fetch them.

S3 layout (both the input key and the output prefix start with <requestId>):
  input : s3://<bucket>/<requestId>/inputs/prompt.md  (UTF-8, put by this command)
  output: s3://<bucket>/<requestId>/runs/             (status.json, report.json,
          report.md, kiro-stdout.txt, artifacts, ... put by hook_server)

The payload carries a promptKey instead of the prompt body so it stays under
the 4096-byte runHookPayload limit (GitHub issue #3). AWS is driven through
the `aws` CLI via subprocess; boto3 is not used.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from runs_cli import config as config_mod

DEFAULT_PROMPT = "Look up the weather in Sapporo and create weather.html"
DEFAULT_BASE_BRANCH = "main"
MAX_PAYLOAD_BYTES = 4096
POLL_INTERVAL_SECONDS = 5
# 372 x 5s = 1860s: slightly above --maximum-duration-in-seconds (1800) so the
# poll can outlast the VM lifetime. KIRO_TIMEOUT_SECONDS (1200) < 1800 < 1860.
POLL_MAX_ATTEMPTS = 372
MAX_DURATION_SECONDS = 1800

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RunError(Exception):
    """A fatal error; the message is shown to the user."""


class _Terminated(BaseException):
    """Raised from the SIGTERM handler so cleanup in `finally` runs."""


class _Deferred:
    pending: BaseException | None = None


@contextlib.contextmanager
def _defer_signals():
    """Hold SIGINT/SIGTERM until the block ends; record the first one."""
    state = _Deferred()
    def handler(signum, frame):
        if state.pending is None:
            state.pending = KeyboardInterrupt() if signum == signal.SIGINT else _Terminated()
    old = {s: signal.signal(s, handler) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield state
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def log(message: str) -> None:
    print(f"[run] {message}", file=sys.stderr, flush=True)


def generate_request_id(now: time.struct_time | None = None, pid: int | None = None) -> str:
    """Return req-YYYYmmdd-HHMMSS-<pid>."""
    now = now or time.localtime()
    return f"req-{time.strftime('%Y%m%d-%H%M%S', now)}-{pid if pid is not None else os.getpid()}"


def validate_request_id(request_id: str) -> str:
    if not _REQUEST_ID_RE.fullmatch(request_id) or ".." in request_id:
        raise RunError(
            f"Invalid request ID '{request_id}'. Use letters, digits, '.', '_' and '-' only."
        )
    # The image derives the branch name kiro/<requestId>. A trailing '-' is
    # stripped there, and a trailing '.' or '.lock' is not a valid Git ref.
    if request_id.endswith(("-", ".", ".lock")):
        raise RunError(
            f"Invalid request ID '{request_id}'. It must not end with '-', '.' or '.lock' "
            "(the branch name kiro/<requestId> would change or be invalid)."
        )
    return request_id


def build_payload(
    request_id: str, prefix: str, prompt_key: str, repo_url: str, base_branch: str
) -> str:
    """Build the run-hook payload JSON. repoUrl is omitted when empty."""
    payload = {
        "requestId": request_id,
        "prefix": prefix,
        "promptKey": prompt_key,
        "baseBranch": base_branch,
    }
    if repo_url:
        payload["repoUrl"] = repo_url
    return json.dumps(payload)


def check_payload_size(payload: str) -> int:
    """Return the UTF-8 byte length, or raise RunError if it exceeds 4096."""
    size = len(payload.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise RunError(
            f"runHookPayload exceeds {MAX_PAYLOAD_BYTES} bytes "
            f"(actual {size} bytes, over by {size - MAX_PAYLOAD_BYTES} bytes).\n"
            "       Hint: shorten the request ID, base branch or repo URL. The prompt "
            "itself is passed through promptKey (S3), so it does not count."
        )
    return size


class Aws:
    """Thin wrapper around the `aws` CLI bound to a profile and region."""

    def __init__(self, profile: str, region: str):
        self.base = ["aws", "--profile", profile, "--region", region]

    def call(
        self, *args: str, check: bool = True, isolate: bool = False
    ) -> subprocess.CompletedProcess:
        cmd = self.base + list(args)
        extra = {"start_new_session": True} if isolate else {}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, **extra)
        except FileNotFoundError:
            raise RunError("The 'aws' CLI was not found in PATH.")
        if check and proc.returncode != 0:
            detail = (proc.stderr or "").strip()
            raise RunError(f"aws {' '.join(args[:2])} failed (exit {proc.returncode}): {detail}")
        return proc

    def text(self, *args: str, isolate: bool = False) -> str:
        return (self.call(*args, isolate=isolate).stdout or "").strip()

    def ok(self, *args: str) -> bool:
        try:
            return self.call(*args, check=False).returncode == 0
        except RunError:
            return False


def _stack_output(aws: Aws, stack: str, key: str) -> str:
    return aws.text(
        "cloudformation", "describe-stacks", "--stack-name", stack,
        "--query", f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue",
        "--output", "text",
    )


def _unset(value: str) -> bool:
    return not value or value == "None"


def execute(cfg, prompt, repo_url, base_branch, request_id, as_json) -> int:
    profile = cfg.require_profile()
    aws = Aws(profile, cfg.aws_region)

    bucket = _stack_output(aws, cfg.stack_name, "ResultBucketName")
    role_arn = _stack_output(aws, cfg.stack_name, "ExecutionRoleArn")
    image_arn = aws.text(
        "lambda-microvms", "list-microvm-images",
        "--query", f"items[?name=='{cfg.image_name}'].imageArn | [0]",
        "--output", "text",
    )
    if _unset(bucket):
        raise RunError("ResultBucketName could not be resolved (has cdk deploy been run?)")
    if _unset(image_arn):
        raise RunError(f"Image '{cfg.image_name}' not found (has the image build finished?)")

    prefix = f"{request_id}/runs/"
    prompt_key = f"{request_id}/inputs/prompt.md"
    egress_arn = (
        f"arn:aws:lambda:{cfg.aws_region}:aws:network-connector:"
        "aws-network-connector:INTERNET_EGRESS"
    )

    # Validate the payload size before uploading anything, so an oversized
    # payload leaves nothing behind on S3.
    payload = build_payload(request_id, prefix, prompt_key, repo_url, base_branch)
    check_payload_size(payload)

    prompt_put = False
    microvm_id = ""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(prompt.encode("utf-8"))
        # Reject a reused request ID: any object under <requestId>/ means an
        # earlier run owns it (its status.json would be mistaken for ours).
        used = aws.text(
            "s3api", "list-objects-v2", "--bucket", bucket,
            "--prefix", f"{request_id}/", "--max-items", "1",
            "--query", "Contents[0].Key", "--output", "text",
        )
        if not _unset(used) or os.path.exists(os.path.join(cfg.results_dir, request_id)):
            raise RunError(
                f"Request ID '{request_id}' was already used. Choose a new --request-id."
            )
        # --if-none-match makes the reservation atomic: a concurrent run with
        # the same ID fails here instead of overwriting the prompt.
        proc = aws.call(
            "s3api", "put-object", "--bucket", bucket, "--key", prompt_key,
            "--body", tmp_path, "--content-type", "text/markdown; charset=utf-8",
            "--tagging", "kind=input", "--if-none-match", "*",
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()
            if "PreconditionFailed" in detail or "ConditionalRequestConflict" in detail:
                raise RunError(
                    f"Request ID '{request_id}' was already used. Choose a new --request-id."
                )
            raise RunError(f"aws s3api put-object failed (exit {proc.returncode}): {detail}")
        prompt_put = True
        os.unlink(tmp_path)
        tmp_path = None

        msg = (
            f"request={request_id} prefix=s3://{bucket}/{prefix} "
            f"promptKey=s3://{bucket}/{prompt_key}"
        )
        if repo_url:
            msg += f" repo={repo_url} base={base_branch}"
        log(msg)

        # /run starts Kiro in the background and returns at once. Kiro keeps
        # running while the MicroVM is alive and puts status.json when done.
        # Signals are deferred while the VM is being created, so microvm_id is
        # always captured and the `finally` below can terminate the VM.
        with _defer_signals() as deferred:
            microvm_id = aws.text(
                "lambda-microvms", "run-microvm",
                "--image-identifier", image_arn,
                "--execution-role-arn", role_arn,
                "--egress-network-connectors", json.dumps([egress_arn]),
                "--run-hook-payload", payload,
                "--maximum-duration-in-seconds", str(MAX_DURATION_SECONDS),
                "--query", "microvmId", "--output", "text",
                isolate=True,
            )
        # From here prompt.md is the VM's real input, so it is no longer removed.
        prompt_put = False
        if deferred.pending is not None:
            raise deferred.pending
        log(f"microvm={microvm_id}")

        status_key = f"{prefix}status.json"
        found = False
        for _ in range(POLL_MAX_ATTEMPTS):
            if aws.ok("s3api", "head-object", "--bucket", bucket, "--key", status_key):
                found = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        if not found:
            raise RunError(
                "status.json not found. Check CloudWatch Logs (/aws/lambda-microvms/kiro-microvm)."
            )

        local_dir = os.path.join(cfg.results_dir, request_id)
        os.makedirs(local_dir, exist_ok=True)
        aws.call("s3", "sync", f"s3://{bucket}/{prefix}", local_dir + "/")

        ok, exit_code = _read_status(os.path.join(local_dir, "status.json"))
        log(f"done: ok={ok if ok is not None else '?'} "
            f"exitCode={exit_code if exit_code is not None else '?'} -> {local_dir}/")
        for name in ("report.json", "report.md"):
            if os.path.isfile(os.path.join(local_dir, name)):
                log(f"report: {os.path.join(local_dir, name)} found")

        if as_json:
            print(json.dumps({
                "requestId": request_id,
                "resultsDir": local_dir,
                "ok": ok is True,
                "exitCode": exit_code,
                "microvmId": microvm_id,
            }, ensure_ascii=False))
        else:
            print(f"Results: {local_dir}")
        return 0
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if prompt_put:
            aws.ok("s3api", "delete-object", "--bucket", bucket, "--key", prompt_key)
        if microvm_id:
            aws.ok("lambda-microvms", "terminate-microvm", "--microvm-identifier", microvm_id)


def _read_status(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("ok"), data.get("exitCode")
    except (OSError, ValueError, AttributeError):
        return None, None


def run(args) -> int:
    """Entry point for `kiro-microvm run`. Returns the process exit code."""
    env = os.environ
    cfg = config_mod.resolve({
        "AWS_PROFILE": args.profile,
        "AWS_REGION": args.region,
        "STACK_NAME": args.stack_name,
        "MICROVM_IMAGE_NAME": args.image_name,
        "KIRO_MICROVM_RESULTS_DIR": args.results_dir,
    })
    prompt = args.prompt or env.get("PROMPT") or DEFAULT_PROMPT
    repo_url = args.repo_url or env.get("REPO_URL") or ""
    base_branch = args.base_branch or env.get("BASE_BRANCH") or DEFAULT_BASE_BRANCH
    request_id = args.request_id or env.get("REQUEST_ID") or generate_request_id()

    def on_term(signum, frame):
        raise _Terminated()

    old_term = signal.signal(signal.SIGTERM, on_term)
    try:
        cfg.require_profile()
        validate_request_id(request_id)
        return execute(cfg, prompt, repo_url, base_branch, request_id, args.json)
    except (config_mod.ConfigError, RunError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except _Terminated:
        print("Terminated.", file=sys.stderr)
        return 143
    finally:
        signal.signal(signal.SIGTERM, old_term)
