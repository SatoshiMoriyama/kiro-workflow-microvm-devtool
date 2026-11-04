#!/usr/bin/env python3
"""show サブコマンド: 指定した run の詳細を表示する。

__main__.py から cmd_show.run(runs, args) で呼ばれる。
args.request_id(位置引数)で run を 1 件特定し、整形表示する。
特定は完全一致を最優先し、無ければ requestId の前方一致(短縮指定)で解決する。

公開 API(loader.Run / load_runs など)は並列タスクとの契約のため参照のみ行い変更しない。
"""

from __future__ import annotations

import os
import sys


def _resolve_run(runs, request_id):
    """request_id から run を 1 件解決する。

    戻り値は (run, matches)。
    - 完全一致が 1 件あればそれを最優先で返す(matches は [run])。
    - 完全一致が無く前方一致が 1 件ならそれを返す。
    - 前方一致が複数なら run は None、matches に候補一覧を入れる。
    - 該当なしなら run は None、matches は空。
    """
    exact = [r for r in runs if r.request_id == request_id]
    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        # requestId は本来一意だが、万一重複していれば候補列挙にフォールバックする。
        return None, exact

    prefix = [r for r in runs if r.request_id.startswith(request_id)]
    if len(prefix) == 1:
        return prefix[0], prefix
    return None, prefix


def _format_ok(run) -> str:
    """ok を exit_code / timed_out 併記で文字列化する。"""
    return f"{run.ok} (exit_code={run.exit_code}, timed_out={run.timed_out})"


def _read_report_md(run):
    """run.path 直下に report.md があれば中身を返す。無ければ None。"""
    report_path = os.path.join(run.path, "report.md")
    if not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _print_run(run, out) -> None:
    """特定した run を整形して out に表示する。"""
    if run.broken:
        # broken な run は読めるフィールドだけ出す。
        print("broken: この run は status.json が欠損/破損しています", file=out)
        print(f"requestId: {run.request_id}", file=out)
        print(f"path: {run.path}", file=out)
        return

    print(f"requestId: {run.request_id}", file=out)
    print(f"ok: {_format_ok(run)}", file=out)
    print(f"prompt: {run.prompt}", file=out)
    print(f"branch: {run.branch}", file=out)
    print(f"pushedCommit: {run.pushed_commit}", file=out)

    print("artifacts:", file=out)
    if run.artifacts:
        for a in run.artifacts:
            print(f"  - {a}", file=out)
    else:
        print("  (なし)", file=out)

    report_md = _read_report_md(run)
    if report_md is not None:
        print("", file=out)
        print("--- report.md ---", file=out)
        # report.md の中身はそのまま出力する。
        out.write(report_md)
        if not report_md.endswith("\n"):
            out.write("\n")


def run(runs, args) -> int:
    """show サブコマンドの本体。正常終了で 0、該当なし/複数一致で 0 以外を返す。"""
    out = sys.stdout
    request_id = args.request_id

    target, matches = _resolve_run(runs, request_id)

    if target is None:
        if matches:
            # 前方一致(または重複)が複数: 候補を列挙して異常終了。
            print(
                f"'{request_id}' に一致する run が複数あります。候補:",
                file=out,
            )
            for r in matches:
                print(f"  - {r.request_id}", file=out)
            return 1
        # 該当なし。
        print(f"'{request_id}' に一致する run が見つかりませんでした", file=out)
        return 1

    _print_run(target, out)
    return 0
