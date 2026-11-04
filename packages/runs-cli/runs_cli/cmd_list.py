#!/usr/bin/env python3
"""runs list サブコマンドの実装。

loader が返す Run のリストを受け取り、固定幅の表として標準出力へ一覧表示する。
標準ライブラリのみを使用する(loader との契約はフィールド名でのみ依存)。

OK 列の意味と failed フィルタの扱い(本実装で固定する方針):
- run.broken が True の run は「broken」と表示し、「失敗(failed)」側に含める。
  broken は status.json を読めなかった run であり、成功(ok=True)ではないため。
- --ok-only は ok が True の run のみ(broken は除外)。
- --failed-only は ok が False または broken の run のみ。
- --ok-only と --failed-only の同時指定は不可(エラーとして exit code 2 を返す)。
  argparse 側(__main__)でも排他になっているが、ここでも防御的に判定する。
"""

from __future__ import annotations

import sys
from datetime import datetime

# ヘッダ(原文英大文字)。列順もこの順で固定する。
_HEADERS = ["REQUEST_ID", "OK", "EXIT", "TIMED_OUT", "ARTIFACTS", "WHEN"]

# ARTIFACTS 列で表示する最大件数。これを超える分は "…" に省略する。
_MAX_ARTIFACTS = 3


def _parse_finished(value):
    """finished_at(ISO8601 文字列)を datetime へ変換する。欠損/解釈不能は None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _sort_key(run):
    """finished_at 降順、欠損(解釈不能含む)は末尾へ回すためのソートキー。

    sorted(reverse=True) 前提で (有効フラグ, datetime) のタプルを返す。
    欠損は有効フラグ 0 + datetime.min とし、降順でも必ず末尾に来るようにする。
    """
    dt = _parse_finished(run.finished_at)
    if dt is None:
        return (0, datetime.min)
    # tz-aware / naive が混在すると比較で例外になるため naive へ正規化する。
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return (1, dt)


def _format_ok(run) -> str:
    """OK 列の表示文字列を返す。"""
    if run.broken:
        return "broken"
    if run.ok is True:
        return "true"
    if run.ok is False:
        return "false"
    return "-"


def _format_exit(run) -> str:
    """EXIT 列。exit_code が None なら '-'。"""
    if run.exit_code is None:
        return "-"
    return str(run.exit_code)


def _format_timed_out(run) -> str:
    """TIMED_OUT 列。None なら '-'。"""
    if run.timed_out is True:
        return "true"
    if run.timed_out is False:
        return "false"
    return "-"


def _format_artifacts(run) -> str:
    """ARTIFACTS 列。カンマ区切り、長ければ先頭数個 + '…'。"""
    artifacts = run.artifacts or []
    if not artifacts:
        return "-"
    if len(artifacts) > _MAX_ARTIFACTS:
        shown = artifacts[:_MAX_ARTIFACTS]
        return ",".join(shown) + ",…"
    return ",".join(artifacts)


def _format_when(run) -> str:
    """WHEN 列。finished_at をローカル時刻へ整形する。欠損/解釈不能は '-'。"""
    dt = _parse_finished(run.finished_at)
    if dt is None:
        return "-"
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _passes_filter(run, ok_only: bool, failed_only: bool) -> bool:
    """フィルタ条件を満たすか判定する。"""
    if ok_only:
        return run.ok is True and not run.broken
    if failed_only:
        return run.broken or run.ok is False
    return True


def run(runs, args) -> int:
    """list サブコマンドのエントリポイント。正常終了で 0 を返す。"""
    ok_only = bool(getattr(args, "ok_only", False))
    failed_only = bool(getattr(args, "failed_only", False))

    # 両方同時指定は不可。防御的にエラーを返す。
    if ok_only and failed_only:
        print(
            "エラー: --ok-only と --failed-only は同時に指定できません。",
            file=sys.stderr,
        )
        return 2

    filtered = [r for r in runs if _passes_filter(r, ok_only, failed_only)]

    # finished_at 降順、欠損は末尾。
    ordered = sorted(filtered, key=_sort_key, reverse=True)

    rows = []
    for r in ordered:
        rows.append(
            [
                str(r.request_id),
                _format_ok(r),
                _format_exit(r),
                _format_timed_out(r),
                _format_artifacts(r),
                _format_when(r),
            ]
        )

    # 固定幅: 各列はヘッダと全セルの最大幅に揃える。
    widths = [len(h) for h in _HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def _fmt_line(cells) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(_fmt_line(_HEADERS))
    for row in rows:
        print(_fmt_line(row))

    return 0
