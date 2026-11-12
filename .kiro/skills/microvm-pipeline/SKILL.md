---
name: microvm-pipeline
description: 1 つの依頼を「先行(共通基盤)タスク」と「並列可能タスク」に分割し、各タスクを MicroVM 上の kiro-cli に投げて実装させる分割パイプライン。先行タスクを統合ブランチにマージしてから並列タスクをその上で走らせ、全並列タスクをマージしてからまとめてマルチモデルレビュー、指摘対応も MicroVM で行い、最後に 1 つの統合ブランチから PR を作る。作業分解・先行・並列・マージ・レビュー・指摘対応ループ・PR までを 1 つの run_workflow にまとめて実行する。ユーザーが「microVM 分割パイプラインで」「依頼を分割して並列で MicroVM に作業させて」「分割して MicroVM パイプラインで実装して」のように明示的に起動したときに使う。
---

# microvm-pipeline

1 件の実装依頼を、MicroVM 上の kiro-cli を複数使って実装する動的パイプライン。**作業分解 → 先行タスク → 先行マージ → 並列タスク → 全並列マージ → まとめてマルチモデルレビュー → 指摘対応ループ → PR** までを **1 つの run_workflow** にまとめて実行する。

ステップ固有ルールの一次ソースは `.kiro/agents/` の専用エージェント定義(microvm-split / microvm-run / microvm-merge / microvm-pr)。このスキルには全体構成・レビュー方針・pause 仲介だけを残し、同じルールを二重に書かない。

このスキルの役割は、依頼を受けて 1 本の workflow を組み立て(run_workflow の workflowPrompt)、起動することと、workflow が pause してユーザー判断を求めてきたときに仲介すること。構成は依頼ごとに分割数・依存関係が変わるため固定レシピにはせず、毎回 workflowPrompt で生成する。workflow の先頭に作業分解ステップを置き、その結果で後続を `replace_remaining` により動的生成する。

## 責務の境界(最重要)

- **MicroVM 内の kiro-cli**: 各タスクの「実装そのもの」だけ(clone → ブランチ作成 → 作業 → commit → push)。マージ・PR・レビューはしない。
- **workflow 内の手元側ステップ(MicroVM 外)**: 統合ブランチ作成、`pnpm run-microvm` の起動と結果回収、マージ、マージ済み作業ブランチの削除、PR 作成。git 操作とマージは MicroVM にはやらせず、専用エージェント(microvm-run / microvm-merge / microvm-pr)が担う。
- **workflow 内のレビュー/集約ステップ**: マルチモデルレビューと集約。
- **このスキル(オーケストレーター)**: 入力を聞き、workflow を組んで起動し、pause 時にユーザーへ判断を仲介する。

## 前提

- run-microvm skill(.kiro/skills/run-microvm/SKILL.md)と同じ MicroVM 実行基盤を使う。各タスク実行の手順は microvm-run エージェント(`.kiro/agents/microvm-run.md`)が run-microvm skill に従って行う。
- GitHub フロー前提。REPO_URL が必須(https://github.com/owner/repo.git)。デプロイ済み・Secret(kiro-api-key / github-token)投入済み・AWS SSO 有効・gh 認証済みであること。未整備なら run-microvm skill のガードに従い中断・報告する。
- **AWS プロファイルは必須入力。既定のフォールバックは無い**(公開前提)。手順 1 で毎回ユーザーに聞き、workflowPrompt に明記して各ステップへ渡す。
- MicroVM の GitHub フローは「渡された base ブランチを clone → kiro/<requestId> ブランチを切って作業 → push」までで、マージはしない。base は run-microvm に BASE_BRANCH で渡す。
- MicroVM が作る実装コミットの author はデプロイ時設定(未指定時 `kiro-bot <kiro-bot@users.noreply.github.com>`、詳細は README「コミット author の設定」)で、パイプラインやラン単位では変えない。microvm-merge が作るマージコミットは手元の git 設定の author になる。両者を同じ名義にしたい場合は、デプロイ時に手元と同じ author(GitHub アカウントに紐づくメール)を設定する。
- レビューは workflow-review.md(ユーザーのグローバル steering)に従う: Claude 中量級 + GPT 中量級の 2 系統並列 + 中量級の集約。高級モデル・高 effort は使わない。モデル ID は実行環境の生きた一覧から解決する(ハードコードしない)。

## ブランチ構造

```
main
 └─ 統合ブランチ (feature/<機能名>)                          ← microvm-merge が main から作成し origin へ push
     ├─ 先行タスク   : 統合ブランチを base に clone → kiro/<reqId> → push → 統合ブランチへマージ → origin 更新 → kiro/<reqId> 削除
     ├─ 並列タスクA  : 更新済み統合ブランチを base に clone → kiro/<reqId> → push ┐
     ├─ 並列タスクB  : 同上                                                       ├ 全完了後に順次マージ → 各 kiro/<reqId> 削除
     └─ 並列タスクC  : 同上                                                       ┘
     (全並列マージ後) 統合ブランチ全体をまとめてマルチモデルレビュー
     ├─ 指摘対応     : 指摘があれば MicroVM が新規 kiro/<reqId2> で修正 → push → マージ → kiro/<reqId2> 削除 → 再レビュー (max 3)
 → APPROVED になったら 統合ブランチ → main の PR を作成
```

作業ブランチ kiro/<requestId> は、統合ブランチへマージして push した後に origin から削除する。確認手順と例外は `.kiro/agents/microvm-merge.md` を参照。

**統合ブランチの命名**: 依頼内容(機能)が分かる名前。例 `feature/user-auth`。`microvm-pipeline` 等のツール名・パイプライン名キーワードは入れない。機能名は作業分解ステップで依頼から決める。

## レビューと指摘対応の方針(確定事項)

- **レビューのタイミング**: 並列タスクを個別にレビューしない。**全並列タスクを統合ブランチにマージし終えてから、統合ブランチ全体(main との diff)を 1 回だけ**まとめてマルチモデルレビューする。先行タスクも統合ブランチに含まれるので一括で見る。タスク横断の整合性もここで見る。
- **指摘対応は常に新規ブランチで MicroVM**: 集約 verdict が APPROVED でなく、ユーザーが「直す」と判断した指摘は、MicroVM で修正する。修正は**必ず新しい kiro/<requestId> を切る**(既存ブランチは使い回さない)。修正対象の最新統合ブランチを BASE_BRANCH にして clone させ、指摘の要点(対象ファイル・直す内容)を PROMPT に渡す。push 後、microvm-merge が統合ブランチへマージして再レビューする。
- **ループ上限**: レビュー→指摘対応の実装ループは maxIterations 3。3 周で APPROVED に至らなければ onMaxIterations で pause。
- **集約後はユーザー判断を仰ぐ**: 集約 verdict が APPROVED でない周は、自動で次の指摘対応へ回さず、send_message(severity=warning) で pause し、集約結果(blocking/non-blocking、各指摘の対象ファイルと概要、アグリゲーターが rejected と判断した指摘はその理由)を提示してユーザーに「対応/却下/見送り」を仰ぐ。APPROVED なら pause せず PR へ進む。
- **確定仕様と矛盾する指摘は reject**: 集約ステップは各指摘が今回のブランチの変更に対するものか(base からの diff か)を検証し、確定方針・設計と矛盾する指摘は rejected 理由を付けて残す(握り潰さず残す)。

## 手順

### 1. 入力をユーザーに聞く

- **AWS_PROFILE**(必須): 使用する AWS プロファイル名。既定値を仮定せず必ず聞く(公開前提でフォールバックを置かない)。workflowPrompt に明記し、各 MicroVM 実行ステップへ `AWS_PROFILE=<profile>` として渡す。
- **REPO_URL**(必須): 対象リポジトリ(https://github.com/owner/repo.git)。
- **依頼内容**(必須): 実装させたい作業の全体像。
- base は既定 main(統合ブランチの派生元)。別の派生元が必要なら聞く。

### 2. 1 本の workflow を組んで起動する

run_workflow の workflowPrompt に 1 本のパイプラインを記述して起動する。先頭の split の結果で後続を `replace_remaining` により動的生成する(生成は microvm-split が行う)。各ステップは次の専用エージェントを使う。ルール本文は各エージェント定義にあり、workflowPrompt には書き写さない。

| ステップ | 担当エージェント |
| --- | --- |
| split | microvm-split |
| setup | microvm-merge |
| lead(先行タスクがある場合) | microvm-run → microvm-merge |
| parallel(join allSettled、最大 3) | microvm-run |
| merge-all | microvm-merge |
| review | semantic_reviewer × 2(Claude 中量級 / GPT 中量級)+ wf-review-aggregator |
| fix-loop(maxIterations 3) | microvm-run → microvm-merge → 再レビュー |
| pr | microvm-pr |

workflowPrompt に必ず含める事項:
- ラン固有の値: AWS_PROFILE、REPO_URL、依頼内容、base(既定 main)。
- 各ステップは上表の専用エージェントを使うこと。
- レビュー方針の要点: 全並列マージ後に 1 回、Claude 中量級 + GPT 中量級 + 中量級集約(モデル ID は生きた一覧から解決、ハードコード不可)。指摘対応は常に新規ブランチで MicroVM、maxIterations 3、APPROVED でない周は pause してユーザー判断。確定仕様と矛盾する指摘は rejected 理由を付けて残す。
- 成果物の地の文は日本語。コミットは Conventional Commits 一行。

### 3. pause 時の仲介(あなた)

workflow が send_message(warning) で pause したら、内容に応じてユーザーに仲介する:

- **先行/並列/指摘対応の MicroVM 実行が ok=false・push 失敗・未実行(認証切れ等)**: 事実を提示し、リトライ/中止/手動対応の判断を仰ぐ。勝手に突き進まない。
- **レビュー集約が APPROVED でない**: 集約結果(blocking/non-blocking、各指摘の対象ファイルと概要、rejected 判断とその理由)を提示し、各指摘を「対応/却下/見送り」に切り分けてもらう。
  - 「対応」: update_workflow で fix-loop を継続(extend_repeat または resume)。次周の修正 MicroVM run に「どの指摘を直し、どれを却下/見送るか」を明示して渡す。却下・見送りは review の該当 finding に status=rejected / rejectionReason を残す方針を伝える。
  - 「対応不要(誤認/non-blocking)」: update_status でループを completed にして pr ステップへ進める。
- **maxIterations 到達**: 残る指摘を要約し、もう 1 周回すか(extend_repeat)、指摘を残したまま終える(update_status completed)かをユーザーに確認する。verdict を勝手に APPROVED に書き換えない。

## 注意

- 失敗(ok=false・push 失敗・未実行・非自明な競合)やレビュー未承認で自動的に突き進まない。必ず pause してユーザーに判断を仰ぐ。
- 全工程を 1 つの run_workflow に収める。フェーズごとに別々の run_workflow を起動しない。
- マージ先は常に統合ブランチ。main 直には入れない(最後は PR 経由)。
- 順次マージで競合した場合の扱いは microvm-merge の定義に従う。非自明な競合の判断はユーザーに仰ぐ。
- run.sh / run-microvm skill の挙動に合わせる。それらが変わったら専用エージェント定義と本スキルも追従する。
