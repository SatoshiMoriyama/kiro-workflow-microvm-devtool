#!/usr/bin/env python3
"""stats subcommand.

Aggregates the runs returned by load_runs and prints either a human-readable
summary or, with --json, machine-readable JSON on stdout.

Definitions (fixed by tests):
- total: number of runs.
- ok: runs with ok is True.
- failed: runs that are neither ok nor broken (ok=False and ok=None both
  count). total == ok + failed + broken always holds.
- broken: runs with broken is True.
- successRate (%): ok / total * 100. The denominator is all runs, broken
  included. None when total is 0.
- avgDurationSeconds: mean over runs whose duration can be computed.
  None when there are none.
- creditsTotal: sum of credits_used over runs where it is not None (not
  rounded). None when there are none.
- creditsUnavailable: runs whose credits_used is None (broken included).
"""

from __future__ import annotations

import json

from runs_cli.loader import Run, duration_seconds


def _aggregate(runs: list[Run]) -> dict:
    """Aggregate runs into plain, JSON-friendly values."""
    total = len(runs)
    broken = sum(1 for r in runs if r.broken)
    ok = sum(1 for r in runs if r.ok is True)
    # failed = not ok and not broken, so total == ok + failed + broken always
    # holds (ok=None and non-broken counts as failed).
    failed = sum(1 for r in runs if not r.broken and r.ok is not True)

    # The denominator is all runs (broken included); None when there are no runs.
    success_rate = (ok / total * 100.0) if total > 0 else None

    # Average only over runs whose duration can be computed.
    durations = [
        d for d in (duration_seconds(r) for r in runs) if d is not None
    ]
    avg_duration = (sum(durations) / len(durations)) if durations else None

    # Sum credits over numeric runs only (not rounded); None if there are none.
    credits = [r.credits_used for r in runs if r.credits_used is not None]
    credits_total = sum(credits) if credits else None
    credits_unavailable = total - len(credits)

    return {
        "total": total,
        "ok": ok,
        "failed": failed,
        "broken": broken,
        "successRate": success_rate,
        "avgDurationSeconds": avg_duration,
        "creditsTotal": credits_total,
        "creditsUnavailable": credits_unavailable,
    }


def _format_human(stats: dict) -> str:
    """Format the stats as human-readable text."""
    rate = stats["successRate"]
    rate_str = f"{rate:.1f}%" if rate is not None else "N/A"

    avg = stats["avgDurationSeconds"]
    avg_str = f"{avg:.1f} s" if avg is not None else "N/A"

    credits = stats["creditsTotal"]
    credits_str = str(credits) if credits is not None else "N/A"

    lines = [
        "Run summary",
        f"  Total runs      : {stats['total']}",
        f"  Succeeded (ok)  : {stats['ok']}",
        f"  Failed          : {stats['failed']}",
        f"  Broken          : {stats['broken']}",
        f"  Success rate    : {rate_str}",
        f"  Avg duration    : {avg_str}",
        f"  Credits total   : {credits_str} credits"
        f" ({stats['creditsUnavailable']} unavailable)",
    ]
    return "\n".join(lines)


def run(runs: list[Run], args) -> int:
    """Body of the stats subcommand.

    Prints JSON when args.json is set, otherwise the human-readable summary.
    Returns 0.
    """
    stats = _aggregate(runs)

    if getattr(args, "json", False):
        print(json.dumps(stats, ensure_ascii=False))
    else:
        print(_format_human(stats))

    return 0
