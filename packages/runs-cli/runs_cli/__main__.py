#!/usr/bin/env python3
"""runs-cli のエントリポイント。

list / show / stats のサブコマンドを argparse でディスパッチする薄い骨組み。
各サブコマンドの実装(cmd_list / cmd_show / cmd_stats)は並列タスクが追加するため、
ここでは選択された分岐内で遅延 import する(未作成モジュールを top-level import しない)。
"""

from __future__ import annotations

import argparse
import sys

from runs_cli import loader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runs",
        description="run 結果(results/ 配下)を一覧・表示・集計する CLI",
    )

    # 全サブコマンド共通の親パーサ。--results-dir を共有する。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--results-dir",
        default=loader.default_results_dir(),
        help="results ディレクトリのパス(デフォルト: リポジトリルートの results/)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list サブコマンド
    p_list = subparsers.add_parser(
        "list", parents=[common], help="run を一覧表示する"
    )
    group = p_list.add_mutually_exclusive_group()
    group.add_argument(
        "--ok-only", action="store_true", help="成功(ok)の run のみ表示する"
    )
    group.add_argument(
        "--failed-only", action="store_true", help="失敗した run のみ表示する"
    )

    # show サブコマンド
    p_show = subparsers.add_parser(
        "show", parents=[common], help="指定した run の詳細を表示する"
    )
    p_show.add_argument("request_id", help="表示する run の requestId")

    # stats サブコマンド
    p_stats = subparsers.add_parser(
        "stats", parents=[common], help="run の集計統計を表示する"
    )
    p_stats.add_argument(
        "--json", action="store_true", help="集計結果を JSON で出力する"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    runs = loader.load_runs(args.results_dir)

    # cmd_*.py は並列タスクが追加する。top-level import は失敗するため遅延 import する。
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
