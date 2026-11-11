---
name: run-microvm
description: MicroVM 上の kiro-cli に依頼を投げ、S3 の実行結果を回収して「そのまま」返すスキル。ユーザーが「microVM に依頼して」「run-microvm で実行して」「VM で作業させて」のように明示的に起動したときに使う。成否判断・リトライ・後続処理はこのスキルでは行わず、呼び出し側(オーケストレーション)に委ねる。
---

# run-microvm

MicroVM 上で動く kiro-cli に 1 件の依頼を投げ、完了後に S3 へ出力された結果一式を回収し、`status.json` の中身と回収物のファイルパス一覧を返す。実行ロジックは既存の `scripts/run.sh`(= `pnpm run-microvm`)が持つ。このスキルはそれを正しく叩き、結果を素のまま提示する薄い手順書。

## スコープと責務の境界

- **このスキルがやること**: 入力(REPO_URL / PROMPT)をユーザーに聞く → `pnpm run-microvm` を実行 → `results/<requestId>/` に回収された結果を提示する。
- **このスキルがやらないこと**: `ok` / `exitCode` の成否判断、リトライ、PAT 漏洩チェック、push 後の後続処理、PR 作成。これらは**呼び出し側がオーケストレーションする**。結果は加工せず素のまま返す。
- 対象は MicroVM 内の kiro-cli。手元の Kiro(あなた)の挙動を変える話ではない。

## 前提

- 対象ワークスペースは `kiro-workflow-microvm-devtool`。`pnpm run-microvm` が `scripts/run.sh` を呼ぶ。
- デプロイ済み(Base/Image スタック、MicroVM イメージ)で、Secret `kiro-microvm/kiro-api-key`、GitHub フローを使うなら `kiro-microvm/github-token` に値が入っていること。未デプロイ・未投入ならその旨をユーザーに伝えて中断する(このスキルはデプロイ・PAT 投入をしない)。
- **AWS プロファイルは必須入力。既定のフォールバックは無い**(公開前提で環境依存のプロファイル名を決め打ちしない)。毎回ユーザーに聞く(下記ステップ 1)。`scripts/run.sh` も `AWS_PROFILE` 未指定ならエラーで止まる。
- リージョンは `ap-northeast-1`(MicroVMs 提供リージョン)。SSO トークン切れ(`Token has expired`)なら、ユーザーに `aws sso login --profile <指定プロファイル>` を促して中断する。
- MicroVM 内 kiro-cli のコミット author はデプロイ時設定(環境変数 `KIRO_GIT_AUTHOR_NAME` / `KIRO_GIT_AUTHOR_EMAIL` または CDK コンテキスト `gitAuthorName` / `gitAuthorEmail`。詳細は README「コミット author の設定」)で、未指定時は `kiro-bot <kiro-bot@users.noreply.github.com>`。このスキルからラン単位で変更する手段は無い(入力として聞かない)。

## 手順

### 1. 入力をユーザーに聞く(毎回)

既定値を仮定せず、毎回確認する:

- **AWS_PROFILE**(必須): 使用する AWS プロファイル名。既定値を仮定せず必ず聞く(公開前提でフォールバックを置かない方針)。これを `AWS_PROFILE=<profile>` として実行コマンドに前置する。
- **PROMPT**(必須): MicroVM 内の kiro-cli にやらせる作業内容。PROMPT は run.sh が **S3 経由**で渡す(入力キー `<requestId>/inputs/prompt.md`、UTF-8)。長い日本語でもそのまま渡してよく、手動の圧縮・英訳は不要(payload 直載せではないので 4096 バイト制限に当たらない)。
- **REPO_URL**(任意): GitHub フロー(clone→ブランチ作成→作業→commit→push→report 生成)をさせる場合の対象リポジトリ。`https://github.com/owner/repo.git` 形式のみ(`scripts/run.sh` / hook_server が `https://github.com/` 直下に限定)。
  - **空ならGitHub フローなし**で、PROMPT の作業だけ実行して成果物を S3 に出す従来モードになる。「リポジトリ作業をするか」をユーザーに確認し、する場合のみ REPO_URL を聞く。
- **BASE_BRANCH**(任意): REPO_URL 指定時の起点ブランチ。既定 `main`。特に指定が無ければ既定でよい。

### 2. 実行する

ワークスペースルートで次を実行する。長時間(VM 寿命は最大 1800s、ホストポーリング上限 1860s)かかりうるので、タイムアウトを十分長く取る(1800000ms 程度)。

`<profile>` はステップ 1 でユーザーに聞いた AWS プロファイル名に置き換える。

GitHub フロー(REPO_URL あり)の場合:

```bash
AWS_PROFILE=<profile> \
REPO_URL="https://github.com/owner/repo.git" \
PROMPT="<ユーザーが指定した作業内容>" \
pnpm run-microvm
```

従来モード(REPO_URL なし)の場合:

```bash
AWS_PROFILE=<profile> PROMPT="<ユーザーが指定した作業内容>" pnpm run-microvm
```

- BASE_BRANCH を変える場合は `BASE_BRANCH="<branch>"` を前置する。
- `scripts/run.sh` が内部で requestId(`req-<日時>-<pid>`)を発番し、PROMPT を入力キー `<requestId>/inputs/prompt.md` へ put してから `--run-hook-payload` の JSON(本文ではなく `promptKey` を載せる)を組み立て、`run-microvm` で VM を起動 → `/run` フックで kiro-cli をバックグラウンド実行 → `status.json` の S3 出現をポーリング → `results/<requestId>/` へ回収 → VM を terminate する。待ち・回収・後始末は run.sh がやるので、追加の待機ループを自分で書かない。
- run.sh は payload を VM へ渡す前に **JSON のバイトサイズを 4096 バイトと事前検証**し、超えていれば分かりやすいエラーメッセージで停止する(PROMPT は S3 経由なので通常は超えない。超える場合は `repoUrl` 等の他フィールドが過大。本文を payload に戻さない)。
- run.sh の標準出力に `request=<requestId>`、`prefix=s3://<bucket>/<requestId>/runs/`、回収先 `-> results/<requestId>/` が出る。これらを控える。

### 3. 失敗の切り分け(素のまま伝える)

- run.sh が `ERROR: status.json なし` で終わった場合: VM 内で status.json が出なかった(= 完了検知前に VM 寿命切れ等)。CloudWatch Logs(`/aws/lambda-microvms/kiro-microvm`)を確認する旨を添えて、その事実をそのまま報告する。成否の最終判断・リトライは呼び出し側に委ねる。
- SSO/デプロイ/Secret 未整備による失敗は前提どおりユーザーに促して中断する。

### 4. 結果を「そのまま」返す

`results/<requestId>/` を回収したら、加工・判断せずに次を提示する:

- **requestId** と **S3 prefix**(`s3://<bucket>/<requestId>/runs/`、入力プロンプトは `s3://<bucket>/<requestId>/inputs/prompt.md`)、**回収先ローカルパス**(`results/<requestId>/`)。
- **回収物のファイルパス一覧**(`results/<requestId>/` 直下)。典型: `status.json` / `report.json` / `report.md` / `kiro-stdout.txt` / 成果物。各ファイルはパスだけ示す(中身は呼び出し側が必要に応じて開く)。
- **`status.json` の中身はそのまま全文**提示する(呼び出し側が最初に見る成否判断の起点のため。これだけは中身を返す)。
- GitHub フローなら `report.json` のパスも必ず示す(存在すれば)。

**加えない**こと: 成否の解釈(「成功しました」等の断定)、PAT 漏洩チェック、リトライ提案、push 後の後続処理。結果の事実(パスと status.json の内容)だけ返し、判断は呼び出し側に渡す。

## 注意

- S3 に出る時点で hook_server の二段マスク(`ksk_` / `(gh[pousr]|github_pat)_` 等)が済んでいる。このスキルで追加マスクはしない(そのまま返す方針)。
- `scripts/run.sh` の実装に挙動を合わせる。run.sh が変わったら本スキルの手順も追従する。
