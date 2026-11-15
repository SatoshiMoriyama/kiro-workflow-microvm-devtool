#!/usr/bin/env python3
"""Entry point of the kiro-microvm CLI (run / list / show / stats)."""

from __future__ import annotations

import argparse
import sys

from runs_cli import config, loader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiro-microvm",
        description="Run Kiro CLI on a Lambda MicroVM and inspect past runs.",
    )

    # Shared by all subcommands.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--results-dir",
        default=None,
        help="results directory (default: $KIRO_MICROVM_RESULTS_DIR, "
        "then ~/.local/share/kiro-microvm/results)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_run = subparsers.add_parser(
        "run",
        parents=[common],
        help="start a MicroVM run and fetch its results",
        description="Start a MicroVM, wait for the result on S3 and download it. "
        "Progress goes to stderr. The exit code is 0 even if the run itself "
        "failed (ok=false); check the result.",
    )
    p_run.add_argument(
        "prompt", nargs="?", default=None,
        help="prompt text (or PROMPT env var; default: a sample prompt)",
    )
    p_run.add_argument("--repo-url", help="GitHub repo to clone (or REPO_URL env var)")
    p_run.add_argument("--base-branch", help="base branch (or BASE_BRANCH env var; default: main)")
    p_run.add_argument("--request-id", help="request ID (or REQUEST_ID env var; default: generated)")
    p_run.add_argument("--profile", help="AWS profile (overrides AWS_PROFILE)")
    p_run.add_argument("--region", help="AWS region (overrides AWS_REGION; default: ap-northeast-1)")
    p_run.add_argument("--stack-name", help="CloudFormation stack (overrides STACK_NAME)")
    p_run.add_argument("--image-name", help="MicroVM image name (overrides MICROVM_IMAGE_NAME)")
    p_run.add_argument(
        "--json", action="store_true",
        help="print a final JSON line: requestId, resultsDir, ok, exitCode, microvmId",
    )

    p_list = subparsers.add_parser("list", parents=[common], help="list runs")
    group = p_list.add_mutually_exclusive_group()
    group.add_argument("--ok-only", action="store_true", help="show only successful runs")
    group.add_argument("--failed-only", action="store_true", help="show only failed or broken runs")
    p_list.add_argument("--json", action="store_true", help="output JSON")

    p_show = subparsers.add_parser("show", parents=[common], help="show details of a run")
    p_show.add_argument("request_id", help="requestId of the run (a unique prefix also works)")
    p_show.add_argument("--json", action="store_true", help="output JSON")

    p_stats = subparsers.add_parser("stats", parents=[common], help="show aggregate statistics")
    p_stats.add_argument("--json", action="store_true", help="output JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        from runs_cli import cmd_run

        return cmd_run.run(args)

    results_dir = args.results_dir or config.resolve().results_dir
    runs = loader.load_runs(results_dir)

    # Lazy imports keep startup cheap.
    if args.command == "list":
        from runs_cli import cmd_list

        return cmd_list.run(runs, args)
    if args.command == "show":
        from runs_cli import cmd_show

        return cmd_show.run(runs, args)
    if args.command == "stats":
        from runs_cli import cmd_stats

        return cmd_stats.run(runs, args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
