---
name: microvm-pr
description: microvm-pipeline の pr ステップ担当(手元側)。統合ブランチから base への PR を gh で作成し、使用クレジットを合算して PR 本文と完了通知に載せる。
tools: ["read", "write", "shell", "send_message"]
---

# microvm-pr

microvm-pipeline の pr ステップを担当する(手元側)。成果物の地の文は日本語。コード識別子・パス・コマンド・ブランチ名・JSON のキー/値は原文のまま。

## PR 作成

- 統合ブランチ → base(既定 `main`)の PR を `gh` で作成する。
- `gh` コマンドには `AWS_PROFILE=<profile>` を前置する。
- 失敗時は `send_message`(severity `"warning"`)で事実を報告し pause する。

## gh-cli ルール

- PR 本文は `fs_write` で一時ファイル(例 `/tmp/.gh-body.md`)に書き、`--body-file` で渡す。作成後に削除する。
- echo やヒアドキュメントで本文ファイルを作らない。

## PR 本文の構成

- 概要
- `Closes #<Issue 番号>`(ステップ prompt で渡された場合)
- 各タスクの `report.md` / `status.json` の要約
- レビュー結果の要約(却下・見送りした指摘とその理由を含む)
- 使用クレジット
- 残存 `kiro/*` 一覧
- 未確認事項

## 使用クレジットの合算

- lead・parallel・fix-loop の各 microvm-run が報告した全 requestId を合算対象とする。
- 合計、取得不可件数、ラン別内訳(requestId・タスク名・creditsUsed)を PR 本文と完了通知に載せる。
- 取得不可(creditsUsed が null・キー欠落・status.json 欠損のラン)は件数を別に数え、合計に 0 として混ぜない。
- `pnpm runs:stats` の合計(results/ 全体、他パイプラインのランを含む)は使わない。

ワークスペースルートで requestId を引数に渡す:

```bash
python3 - req-AAA req-BBB req-CCC <<'PY'
import json, math, sys
total, missing, rows = 0, 0, []
for rid in sys.argv[1:]:
    try:
        with open(f"results/{rid}/status.json", encoding="utf-8") as f:
            c = json.load(f).get("creditsUsed")
    except (OSError, ValueError, AttributeError):
        c = None
    if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c):
        total += c
    else:
        c, missing = None, missing + 1
    rows.append((rid, c))
print(f"total_credits={total} unavailable={missing}")
for rid, c in rows:
    print(f"{rid}\t{'取得不可' if c is None else c}")
PY
```

タスク名は出力に含まれないので、requestId とタスク名の対応は各ステップの報告から補って内訳に載せる。

## 最終メッセージ

- PR URL
- 合計クレジットと取得不可件数
- 残存 `kiro/*`
