# kiro-workflow-microvm-devtool

Lambda MicroVMs 上で Kiro を動かすための開発ツール。pnpm モノレポ。

Phase1 は「MicroVM が起動して結果を S3 に吐く」最小疎通（echo）。Phase2 で echo を Kiro CLI の headless 実行に置き換え、指示タスク（例: 札幌の天気を調べて HTML を作る）の成果物を S3 に格納する。

## 構成

```
.
├─ pnpm-workspace.yaml
├─ package.json                 # ルート
├─ packages/
│  ├─ infra/                    # CDK(TypeScript)。S3x2 + IAMx2 + CfnMicrovmImage
│  │  ├─ bin/infra.ts
│  │  └─ lib/microvm-stack.ts
│  └─ image/                    # MicroVM イメージの中身(code artifact)
│     ├─ Dockerfile             # al2023-minimal + awscli + kiro-cli(musl) + Python フックサーバ
│     ├─ hook_server.py         # /ready に 200、/run で Kiro CLI 実行 -> 成果物+status.json を S3 に put
│     └─ test_hook_server.py    # hook_server の純関数ユニットテスト
├─ scripts/
│  ├─ package-image.sh          # packages/image を zip 化し artifact バケットへ
│  └─ run.sh                    # run-microvm -> 結果をポーリング回収 -> terminate
└─ phase1-verification.md       # 調査メモ(Kiro headless / 回収 / 承認)
```

## 前提

- AWS CLI 2.38+（`lambda-microvms` サブコマンド）。確認済み: 2.39.0
- CDK 2.1146 系 CLI / `aws-cdk-lib` 2.275.0（`CfnMicrovmImage` を含む）
- Node 22 / pnpm 12 / Docker 稼働
- リージョン `ap-northeast-1`（MicroVMs 利用可を確認済み）
- `AWS_PROFILE=work`

## 設計上の要点

- **CDK で作れるのは MicroVM イメージまで**。実行インスタンス(`Microvm`)は CFN リソースに無く、`run-microvm` API で起動する。よって起動は `scripts/run.sh`。
- **実行モデル(Phase2)**: Dockerfile の CMD でフックサーバを常駐 → `/ready` に 200 でスナップショット → `run-microvm` で復帰 → `/run` フックが同期で Kiro CLI を実行 → 生成ファイル・`kiro-stdout.txt`・`status.json` を S3 prefix 配下へ put してから 200/500 を返す。
- **Kiro CLI(musl 版)**: Dockerfile は `kirocli-aarch64-linux-musl.zip` を導入する。AL2023 の glibc は 2.34 で gnu ビルドの要求 2.39 に満たないため(install.sh の glibc 検査で弾かれる)、静的リンクの musl ビルドを使う。認証は焼かない(`Q_SKIP_SETUP=1`)。
- **KIRO_API_KEY の渡し方**: 実キーは IaC/イメージに焼かない。`image-stack.ts` が env `KIRO_API_KEY_SECRET_ARN`(Secret `kiro-phase1/kiro-api-key` の ARN のみ)を注入し、`/run` 実行時に executionRole の短命 credential でゲストが `aws secretsmanager get-secret-value` して取得する。値は CloudWatch Logs・S3 成果物・`status.json`・ホスト stdout のいずれにも出さない(二段マスク: 実キー値の完全一致置換 + `ksk_\S+` パターン置換)。
- **`--trust-all-tools` の安全柵**: 人のいないゲストで承認待ちを出さないため全許可にする。承認は安全柵にならないので、代わりに executionRole の最小権限(result バケットへの put と当該 Secret の read のみ)と egress 制御(INTERNET_EGRESS)が安全柵を担う。
- **タイムアウト階層(単調 `840 < 900 < 1200 < 1260`)**: ゲスト `kiro-cli` timeout=840(`hook_server.py`)< `/run` `runTimeoutInSeconds`=900(`image-stack.ts`)< MicroVM `--maximum-duration-in-seconds`=1200(`run.sh`)< ホストポーリング上限=1260s(252×5s, `run.sh`)。これで「ゲスト timeout でも status.json を書ける」「VM 寿命切れ前にフック完了」「ポーリングが VM 寿命より先に諦めない」を同時に満たす。
- **メモリ**: `minimumMemoryInMiB=2048`。Rust 製 kiro-cli がコンテキスト・ツール実行・子プロセス(curl)を抱えるため、baseline を 2048 にして peak 上限(自動で最大 4x)を確保する。
- **完了条件**: `run.sh` は `status.json` の出現をポーリングで待つ(成否に関わらず実行完了後に必ず出る)。`status.json` の `ok`(= `exitCode==0 && !timedOut`)と `exitCode` を表示する。
- **認証/権限**: ゲストには executionRole の短命 credential が自動付与され、`aws s3 cp` が直接叩ける。長期キー不要。
- **code artifact の zip** は CDK 外(`package-image.sh`)で内容ハッシュ付きキー `code/app-<hash>.zip` に置き、ハッシュを `packages/infra/artifact-hash.txt` に書き出す。`ImageStack` はそれを読んで URI を決める。zip が変わればキー(= URI)が変わるので CFN が差分を検知して再ビルドする。
- **イメージ更新は常に CFN 経由**。CLI の `update-microvm-image` を直接叩くと、指定しなかったプロパティ(環境変数・hooks)が落ちてドリフトするため使わない。
- **依頼ごとのプレフィックス**: `run.sh` が依頼ID(`req-<日時>-<pid>`)を生成し `--run-hook-payload` の JSON で prefix を渡す。`/run` ボディは `{"microvmId","runHookPayload"}` 構造で届き、hook_server が `runHookPayload`(文字列)を再パースして `runs/<requestId>/` 配下に格納する。prefix 未指定時は `microvmId` にフォールバック。

## 手順

鶏卵問題(`CfnMicrovmImage` は zip を要求するが deploy 中にビルドまで走るため zip を先に置けない)を避けるため、スタックを2つに分ける。

- `KiroMicrovmPhase1Base`: S3 x2 + IAM x2 + LogGroup(zip 不要)
- `KiroMicrovmPhase1Image`: `CfnMicrovmImage`(zip 必須)

```bash
# 1. 依存インストール
pnpm install

# 2. (初回のみ) CDK bootstrap
AWS_PROFILE=work pnpm --filter infra exec cdk bootstrap aws://495236580548/ap-northeast-1

# 3. 基盤を先にデプロイ(artifact バケットは固定名で作られる)
AWS_PROFILE=work pnpm --filter infra exec cdk deploy KiroMicrovmPhase1Base

# 4. code artifact を zip 化し、ハッシュ付きキーで投入(artifact-hash.txt も生成)
AWS_PROFILE=work ./scripts/package-image.sh

# 5. zip が置かれた状態でイメージをデプロイ(ここでビルドが走る。数分)
#    image の中身を変えたら 4 -> 5 をやり直せば CFN が再ビルドする
AWS_PROFILE=work pnpm --filter infra exec cdk deploy KiroMicrovmPhase1Image

# 6. MicroVM を起動して Kiro を実行し、成果物を回収する
#    prompt は PROMPT 環境変数か第1引数で渡す。省略時はデフォルト
#    (札幌の天気を調べて weather.html を作って)。
AWS_PROFILE=work ./scripts/run.sh
AWS_PROFILE=work PROMPT="東京の天気を調べて weather.html を作って" ./scripts/run.sh
AWS_PROFILE=work ./scripts/run.sh "東京の天気を調べて weather.html を作って"
```

事前準備: Secret `kiro-phase1/kiro-api-key` に実キーを投入しておく(IaC には焼かない)。

```bash
AWS_PROFILE=work aws secretsmanager put-secret-value \
  --secret-id kiro-phase1/kiro-api-key --secret-string 'ksk_...' --region ap-northeast-1
```

期待結果: `runs/<requestId>/` 配下に `status.json`・`kiro-stdout.txt`・成果物(`weather.html` など)が出る。`run.sh` は `status.json` の出現を待ち、`ok` と `exitCode` を表示して `results/<requestId>/` に回収する。`ksk_` で始まる文字列は CloudWatch Logs・S3 の全オブジェクト・ホスト stdout のいずれにも出ない。

kiro-cli の配布 URL や版を変えるときは、`run-microvm` が AWS 側ビルドのため build-arg 経路が無い。`packages/image/Dockerfile` の `ARG KIRO_CLI_DL_URL` 既定値を直接編集 → 手順 4(`package-image.sh`)→ 手順 5(`cdk deploy KiroMicrovmPhase1Image`)で再ビルドする。

## 検証状況

確認済み(実機):
- `lambda-microvms` は東京で利用可、base image は `al2023-1` 版 `2`。
- `CfnMicrovmImage` の enum: architecture=`ARM_64`、hooks の ready/run=`ENABLED`/`DISABLED`。
- `egressNetworkConnectors` はマネージドコネクタ ARN(`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS`)。
- zip 不在ビルドは `"The artifact was not found in S3."` で失敗 → Base/Image の2スタック分割で解消。

確認済み(2026-10-28 実機疎通):
- イメージビルド成功(Graviton3/4 とも SUCCESSFUL)。Dockerfile の dnf(python3/awscli) と /ready フックが通る。
- `run-microvm`(INTERNET_EGRESS) -> /run フック -> executionRole で `aws s3 cp` -> result バケットに出力 -> 手元で回収 -> terminate、まで全経路成立。
- 出力例: `hello from microvm 2026-10-27T20:23:43.780973+00:00`
- `run-microvm` レスポンスの識別子は `microvmId`。

Phase1 完了。次は同じ構成に Kiro CLI を載せる(phase1-verification.md の設計)。
