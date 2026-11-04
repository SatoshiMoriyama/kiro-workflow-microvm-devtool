#!/usr/bin/env python3
"""stats サブコマンドの実装。

load_runs が返す Run のリストを集計し、人間向け整形表示または
--json 指定時に機械可読 JSON を stdout へ出力する。

集計の定義(テストで固定する):
- 総 run 数   : runs の総数。
- 成功数(ok)  : ok is True の run 数。
- 失敗数      : ok が True でない かつ broken でない run 数
                (ok is False だけでなく ok is None も含む)。
                total == ok + failed + broken が常に成立する。
- 壊れ数      : broken is True の run 数。
- 成功率(%)   : 成功数 / 総 run 数 * 100。分母は「全 run」(broken も含む)。
                総 run 数が 0 のときは None 相当とする。
- 平均実行時間 : duration_seconds が算出できた run のみで平均(秒)。
                算出可能な run が 0 件なら None 相当とする。
"""

from __future__ import annotations

import json

from runs_cli.loader import Run, duration_seconds


def _aggregate(runs: list[Run]) -> dict:
    """runs を集計して素の値(JSON 化しやすい形)を返す。"""
    total = len(runs)
    broken = sum(1 for r in runs if r.broken)
    ok = sum(1 for r in runs if r.ok is True)
    # 失敗は「ok が True でない」かつ「broken でない」run とする。
    # こうすると total == ok + failed + broken が常に成立する
    # (ok=None かつ非 broken の run も failed に含まれる)。
    failed = sum(1 for r in runs if not r.broken and r.ok is not True)

    # 成功率の分母は全 run(broken を含む)。総数 0 のときは None。
    success_rate = (ok / total * 100.0) if total > 0 else None

    # 平均実行時間は duration が算出できた run のみで平均する。
    durations = [
        d for d in (duration_seconds(r) for r in runs) if d is not None
    ]
    avg_duration = (sum(durations) / len(durations)) if durations else None

    return {
        "total": total,
        "ok": ok,
        "failed": failed,
        "broken": broken,
        "successRate": success_rate,
        "avgDurationSeconds": avg_duration,
    }


def _format_human(stats: dict) -> str:
    """集計結果を日本語の人間向け表示に整形する。"""
    rate = stats["successRate"]
    rate_str = f"{rate:.1f}%" if rate is not None else "N/A"

    avg = stats["avgDurationSeconds"]
    avg_str = f"{avg:.1f} 秒" if avg is not None else "N/A"

    lines = [
        "run 集計サマリ",
        f"  総 run 数     : {stats['total']}",
        f"  成功数(ok)   : {stats['ok']}",
        f"  失敗数        : {stats['failed']}",
        f"  壊れ数        : {stats['broken']}",
        f"  成功率        : {rate_str}",
        f"  平均実行時間  : {avg_str}",
    ]
    return "\n".join(lines)


def run(runs: list[Run], args) -> int:
    """stats サブコマンドの本体。

    args.json(--json)が真なら機械可読 JSON を、そうでなければ
    日本語の整形表示を stdout に出力する。正常終了で 0 を返す。
    """
    stats = _aggregate(runs)

    if getattr(args, "json", False):
        print(json.dumps(stats, ensure_ascii=False))
    else:
        print(_format_human(stats))

    return 0
