# microvm-pipeline 専用エージェント 調査メモ(Issue #11)

microvm-pipeline の各ステップのルールを、汎用エージェントへ毎回 workflowPrompt で渡す方式から、リポジトリ内 `.kiro/agents/` の専用エージェント定義に固定する方式へ移した。

## 専用化の対象と粒度

| 名前 | ファイル | 担当ステップ | 固定ルールの要点 |
| --- | --- | --- | --- |
| microvm-split | `.kiro/agents/microvm-split.md` | split | 分割の指針、タスク PROMPT の書き方(Node 検証を要求しない)、承認後に必ず `replace_remaining`、workflowPrompt に入れる事項 |
| microvm-run | `.kiro/agents/microvm-run.md` | lead / parallel / fix-loop の MicroVM 実行 | run-microvm skill 起動、`pnpm run-microvm` の呼び方、PROMPT 取り違え照合、失敗時 pause、最終報告の必須項目 |
| microvm-merge | `.kiro/agents/microvm-merge.md` | setup / lead・merge-all・fix-loop のマージ | 統合ブランチ作成、`--no-ff` 順次マージ、マージ前確認、競合対応、手元検証、`kiro/<requestId>` 削除手順と例外 |
| microvm-pr | `.kiro/agents/microvm-pr.md` | pr | gh-cli ルール、PR 本文構成、使用クレジット合算(python3 スクリプト) |

review / aggregate は専用化しない。ユーザーのグローバル steering の multi-model レビュー方針に従う汎用エージェント(semantic_reviewer / wf-review-aggregator)で足りるため。

## 置き場所

- リポジトリ内 `.kiro/agents/`。workflow のエージェント一覧に `(workspace)` 行として出る。
- 本メモを `.kiro/agents/` に置かない理由: Markdown 形式のエージェント定義として読まれる恐れがあるため。`docs/` に置く。

## 定義形式

- Kiro カスタムエージェント公式の Markdown 形式(YAML front matter + 本文 = システムプロンプト)を採用した。公式形式なら独自変換が要らず、他の Kiro クライアントでも同じ定義を読める。
- front matter のキーは `name` / `description` / `tools` のみ。
- `model` は書かない。モデル ID をハードコードせず、実行環境の既定・選択に任せる。

## tools の選定

| エージェント | tools | 根拠 |
| --- | --- | --- |
| microvm-split | `read`, `shell`, `write`, `send_message`, `update_workflow`, `inspect_workflow` | read=SKILL.md・ソース読み取り / shell=`gh issue view`・`git log` 等の読み取り / write=split.md 書き込み / send_message=承認待ち warning と完了 success / update_workflow・inspect_workflow=承認後の replace_remaining と事前の状態確認 |
| microvm-run | `read`, `shell`, `disclose_context`, `send_message` | disclose_context=run-microvm skill 起動 / shell=`pnpm run-microvm` と照合用の `git fetch`・`git diff --name-only` / read=`results/<requestId>/` の回収物読み取り / send_message=失敗時 pause(ファイルを作らないので write 不要) |
| microvm-merge | `read`, `write`, `shell`, `send_message` | shell=git 操作・手元検証(`pnpm build` / `pnpm synth` / `pnpm runs:test`)・`git push origin --delete` / read・write=軽微な競合の解消 / send_message=失敗・非自明な競合で pause |
| microvm-pr | `read`, `write`, `shell`, `send_message` | write=PR 本文の一時ファイル作成(gh-cli ルールで fs_write 必須) / shell=`gh pr create`・クレジット合算の python3・一時ファイル削除 / read=report.md・status.json 読み取り / send_message=失敗時 pause |

選外: `web` / `subagent` / `@mcp` / `*`。いずれも各ステップの役割に不要で、権限を最小に保つため。

## ルールの一次ソース

- ステップ固有ルール: `.kiro/agents/` のエージェント定義。
- 全体構成・レビュー方針・pause 仲介: `.kiro/skills/microvm-pipeline/SKILL.md`。
- 同じルールを両方に書かない(二重管理しない)。

## Issue #11 の要調査項目ごとの結論

| 項目 | 結論 |
| --- | --- |
| 置き場所 | `.kiro/agents/` に決定 |
| tools / allowedTools / model の反映範囲 | 未確認。実地確認待ち |
| 同期方法 | 一次ソースを分けて二重管理しない |
| `~/.kiro/agents` の既存形式の流用 | しない。公式形式で新規作成 |

## 既知の落とし穴と反映先

| 落とし穴 | 反映先 |
| --- | --- |
| split の承認後に進まない | microvm-split(承認後に同一ターンで `replace_remaining` を必須化) |
| PROMPT 取り違え | microvm-run(status.json と変更ファイルの照合)/ microvm-merge(マージ前の変更ファイル再確認) |
| MicroVM に Node が無い | microvm-split(PROMPT で Node 検証を要求しない)/ microvm-run(未実施を失敗扱いしない)/ microvm-merge(手元検証) |

## 未確認事項

- 実際の microvm-pipeline 実行で専用エージェントが動くことの実地確認。
- `send_message` / `update_workflow` / `inspect_workflow` / `disclose_context` は workflow 実行時に注入されるツールで、`tools` へのツール ID 直指定で選択できるか未検証。使えなければ ID の綴りを直すか、そのエージェントだけ `@builtin` へ広げる。
- tools / allowedTools / model が実行時にどこまで反映されるか。
