#!/usr/bin/env python3
"""run 結果(results/ 配下)を読み込み、Run データクラスへ正規化するローダ。

このモジュールの公開 API(Run / load_runs / duration_seconds /
default_results_dir)は並列タスク(cmd_list / cmd_show / cmd_stats)との
契約であり、名前・シグネチャを厳密に維持する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Run:
    """1 つの run を表す正規化済みデータ。

    フィールド名は並列タスクが import するため厳密に維持すること(snake_case)。
    """

    request_id: str
    ok: bool | None
    exit_code: int | None
    timed_out: bool | None
    artifacts: list[str]
    skipped_artifacts: list[str]
    failed_uploads: list[str]
    started_at: str | None
    finished_at: str | None
    prompt: str | None
    branch: str | None
    pushed_commit: str | None
    summary: str | None
    path: str
    broken: bool = False


def _read_json(path: str):
    """JSON ファイルを読む。存在しない・壊れている場合は例外を送出する。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _run_from_status(request_id: str, abs_dir: str, status: dict) -> Run:
    """status.json の dict から Run を構築する(report.json 分は未設定)。"""
    return Run(
        request_id=status.get("requestId", request_id),
        ok=status.get("ok"),
        exit_code=status.get("exitCode"),
        timed_out=status.get("timedOut"),
        artifacts=status.get("artifacts") or [],
        skipped_artifacts=status.get("skippedArtifacts") or [],
        failed_uploads=status.get("failedUploads") or [],
        started_at=status.get("startedAt"),
        finished_at=status.get("finishedAt"),
        prompt=status.get("prompt"),
        branch=None,
        pushed_commit=None,
        summary=None,
        path=abs_dir,
        broken=False,
    )


def _broken_run(request_id: str, abs_dir: str) -> Run:
    """status.json が欠損・破損している場合の broken な Run を返す。"""
    return Run(
        request_id=request_id,
        ok=None,
        exit_code=None,
        timed_out=None,
        artifacts=[],
        skipped_artifacts=[],
        failed_uploads=[],
        started_at=None,
        finished_at=None,
        prompt=None,
        branch=None,
        pushed_commit=None,
        summary=None,
        path=abs_dir,
        broken=True,
    )


def load_runs(results_dir: str) -> list[Run]:
    """results_dir 直下の各サブディレクトリを 1 run として読み込む。

    - status.json が無い/壊れている場合はクラッシュせず broken=True の Run を返す。
    - report.json があれば branch / pushed_commit / summary を取り込む。
      report.json が壊れている場合は status 部分を保持し report 側のみ None とする。
    - results_dir が存在しない場合は空リストを返す。
    """
    if not os.path.isdir(results_dir):
        return []

    runs: list[Run] = []
    for name in sorted(os.listdir(results_dir)):
        sub = os.path.join(results_dir, name)
        if not os.path.isdir(sub):
            continue
        abs_dir = os.path.abspath(sub)

        status_path = os.path.join(sub, "status.json")
        try:
            status = _read_json(status_path)
            run = _run_from_status(name, abs_dir, status)
        except (FileNotFoundError, ValueError, OSError):
            runs.append(_broken_run(name, abs_dir))
            continue

        report_path = os.path.join(sub, "report.json")
        if os.path.isfile(report_path):
            try:
                report = _read_json(report_path)
                run.branch = report.get("branch")
                run.pushed_commit = report.get("pushedCommit")
                run.summary = report.get("summary")
            except (ValueError, OSError):
                # report.json が壊れている: status 部分は保持し report 側のみ None。
                run.branch = None
                run.pushed_commit = None
                run.summary = None

        runs.append(run)

    return runs


def duration_seconds(run: Run) -> float | None:
    """started_at と finished_at の差(秒)を返す。

    ISO8601(オフセット付き)を datetime.fromisoformat で解釈する。
    いずれかが欠損/解釈不能なら None を返す。
    """
    if not run.started_at or not run.finished_at:
        return None
    try:
        start = datetime.fromisoformat(run.started_at)
        finish = datetime.fromisoformat(run.finished_at)
    except (ValueError, TypeError):
        return None
    return (finish - start).total_seconds()


def default_results_dir() -> str:
    """リポジトリルート直下の results/ の絶対パスを返す。

    このファイルは packages/runs-cli/runs_cli/ にあるため、3 階層上がリポジトリルート。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(repo_root, "results")
