# kiro-workflow-microvm-devtool

Lambda MicroVMs 上で Kiro を動かすための開発ツール。pnpm モノレポ。

MicroVM の中で Kiro CLI を headless 実行し、指示タスク（例: 札幌の天気を調べて HTML を作る）の成果物を S3 に格納する。

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
│  ├─ deploy.sh                 # Base -> zip 投入 -> Image を 1 コマンドで(pnpm deploy)
│  ├─ package-image.sh          # packages/image を zip 化し artifact バケットへ
│  └─ run.sh                    # run-microvm -> 結果をポーリング回収 -> terminate
└─ README.md
```

※ 調査・設計メモ(`verification.md` / `memo.md`)は `blog/` に置き、リポジトリには含めない(gitignore)。

## 前提

- AWS CLI 2.38+（`lambda-microvms` サブコマンド）。確認済み: 2.39.0
- CDK 2.1146 系 CLI / `aws-cdk-lib` 2.275.0（`CfnMicrovmImage` を含む）
- Node 22 / pnpm 12 / Docker 稼働
- リージョン `ap-northeast-1`（MicroVMs 利用可を確認済み）
- `AWS_PROFILE=work`

## 設計上の要点

- **CDK で作れるのは MicroVM イメージまで**。実行インスタンス(`Microvm`)は CFN リソースに無く、`run-microvm` API で起動する。よって起動は `scripts/run.sh`。
- **実行モデル(非同期)**: Dockerfile の CMD でフックサーバを常駐 → `/ready` に 200 でスナップショット → `run-microvm` で復帰 → `/run` フックは Kiro CLI 実行+S3 put 一式をバックグラウンドスレッドで起動し即 200(`{"status":"accepted"}`)を返す。`/run` の `RunTimeoutInSeconds` は上限 60 秒のため Kiro をここで同期実行できない。Kiro は MicroVM が生きている限り裏で走り、完了時に生成ファイル・`kiro-stdout.txt`・`status.json` を S3 prefix 配下へ put する。ホスト(`run.sh`)は `status.json` の出現をポーリングして完了を検知する(成否は `status.json` の `ok`/`exitCode`/`timedOut`/`failedUploads` で判定)。`RESULT_BUCKET` 未設定などバックグラウンド起動前に判明する前提エラーのみ `/run` を 500 にする。
- **Kiro CLI(musl 版)**: Dockerfile は `kirocli-aarch64-linux-musl.zip` を導入する。AL2023 の glibc は 2.34 で gnu ビルドの要求 2.39 に満たないため(install.sh の glibc 検査で弾かれる)、静的リンクの musl ビルドを使う。認証は焼かない(`Q_SKIP_SETUP=1`)。
- **KIRO_API_KEY の渡し方**: 実キーは IaC/イメージに焼かない。`image-stack.ts` が env `KIRO_API_KEY_SECRET_ARN`(Secret `kiro-microvm/kiro-api-key` の ARN のみ)を注入し、`/run` 実行時に executionRole の短命 credential でゲストが `aws secretsmanager get-secret-value` して取得する。値は CloudWatch Logs・S3 成果物・`status.json`・ホスト stdout のいずれにも出さない(二段マスク: 実キー値の完全一致置換 + `ksk_\S+` パターン置換)。
- **`--trust-all-tools` の安全柵**: 人のいないゲストで承認待ちを出さないため全許可にする。承認は安全柵にならないので、代わりに executionRole の最小権限(result バケットへの put と当該 Secret の read のみ)と egress 制御(INTERNET_EGRESS)が安全柵を担う。
- **時間制約の階層(非同期)**: 2 つの時間制約は別物。
  - `/run` の `RunTimeoutInSeconds`=60(`image-stack.ts`): `/run` が 200 を返すまでの上限(サービスモデル max=60)。即 200 を返すので 60 で十分。
  - ゲスト `kiro-cli` timeout = `KIRO_TIMEOUT_SECONDS`(`hook_server.py` 既定 1200、環境変数で上書き可)。
  - MicroVM 寿命 `--maximum-duration-in-seconds`=1800(`run.sh`): バックグラウンドの Kiro 実行時間を担保する。
  - ホストポーリング上限=1860s(372×5s, `run.sh`): VM 寿命より少し長く待つ。
  - 単調関係 `KIRO_TIMEOUT_SECONDS(1200) < --maximum-duration(1800) < ホストポーリング上限(1860)` を保つ。これで「ゲスト timeout でも status.json を書ける」「VM 寿命切れ前に Kiro 完了」「ポーリングが VM 寿命より先に諦めない」を同時に満たす。
- **メモリ**: `minimumMemoryInMiB=2048`。Rust 製 kiro-cli がコンテキスト・ツール実行・子プロセス(curl)を抱えるため、baseline を 2048 にして peak 上限(自動で最大 4x)を確保する。
- **完了条件**: `/run` が即 200 を返した後、`run.sh` は `status.json` の出現をポーリングで待つ(バックグラウンドの Kiro 完了後に成否に関わらず必ず出る)。`status.json` の `ok`(= `exitCode==0 && !timedOut && failedUploads なし`)と `exitCode` を表示する。
- **認証/権限**: ゲストには executionRole の短命 credential が自動付与され、`aws s3 cp` が直接叩ける。長期キー不要。
- **code artifact の zip** は CDK 外(`package-image.sh`)で内容ハッシュ付きキー `code/app-<hash>.zip` に置き、ハッシュを `packages/infra/artifact-hash.txt` に書き出す。`ImageStack` はそれを読んで URI を決める。zip が変わればキー(= URI)が変わるので CFN が差分を検知して再ビルドする。
- **イメージ更新は常に CFN 経由**。CLI の `update-microvm-image` を直接叩くと、指定しなかったプロパティ(環境変数・hooks)が落ちてドリフトするため使わない。
- **依頼ごとのプレフィックス**: `run.sh` が依頼ID(`req-<日時>-<pid>`)を生成し `--run-hook-payload` の JSON で prefix を渡す。`/run` ボディは `{"microvmId","runHookPayload"}` 構造で届き、hook_server が `runHookPayload`(文字列)を再パースして `runs/<requestId>/` 配下に格納する。prefix 未指定時は `microvmId` にフォールバック。

## 手順

鶏卵問題(`CfnMicrovmImage` は zip を要求するが deploy 中にビルドまで走るため zip を先に置けない)を避けるため、スタックを2つに分ける。

- `KiroMicrovmBase`: S3 x2 + IAM x2 + LogGroup(zip 不要)
- `KiroMicrovmImage`: `CfnMicrovmImage`(zip 必須)

```bash
# 1. 依存インストール
pnpm install

# 2. (初回のみ) CDK bootstrap
AWS_PROFILE=work pnpm --filter infra exec cdk bootstrap aws://495236580548/ap-northeast-1

# 3. デプロイ(Base -> zip 投入 -> Image を 1 コマンドで。scripts/deploy.sh)
#    image の中身を変えても再実行すれば CFN が差分を検知して再ビルドする。
AWS_PROFILE=work pnpm deploy

# 4. MicroVM を起動して Kiro を実行し、成果物を回収する
#    prompt は PROMPT 環境変数か引数(-- で渡す)で指定。省略時はデフォルト
#    (札幌の天気を調べて weather.html を作って)。
AWS_PROFILE=work pnpm run-microvm
AWS_PROFILE=work PROMPT="東京の天気を調べて weather.html を作って" pnpm run-microvm
AWS_PROFILE=work pnpm run-microvm -- "東京の天気を調べて weather.html を作って"
```

個別に叩きたい場合は従来どおり `scripts/deploy.sh`・`scripts/package-image.sh`・`scripts/run.sh` を直接実行してもよい。`pnpm deploy` は内部でこの順序(BaseStack → `package-image.sh` → ImageStack)を実行する。

事前準備: Secret `kiro-microvm/kiro-api-key` に実キーを投入しておく(IaC には焼かない)。

```bash
AWS_PROFILE=work aws secretsmanager put-secret-value \
  --secret-id kiro-microvm/kiro-api-key --secret-string 'ksk_...' --region ap-northeast-1
```

期待結果: `runs/<requestId>/` 配下に `status.json`・`kiro-stdout.txt`・成果物(`weather.html` など)が出る。`run.sh` は `status.json` の出現を待ち、`ok` と `exitCode` を表示して `results/<requestId>/` に回収する。`ksk_` で始まる文字列は CloudWatch Logs・S3 の全オブジェクト・ホスト stdout のいずれにも出ない。

kiro-cli の配布 URL や版を変えるときは、`run-microvm` が AWS 側ビルドのため build-arg 経路が無い。`packages/image/Dockerfile` の `ARG KIRO_CLI_DL_URL` 既定値を直接編集 → 手順 4(`package-image.sh`)→ 手順 5(`cdk deploy KiroMicrovmImage`)で再ビルドする。

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

その後 echo を Kiro CLI の headless 実行に置き換え、「札幌の天気を調べて weather.html を作って」で成果物が `runs/<requestId>/` 配下に出ることを実機確認済み(KIRO_API_KEY 漏洩なし、`ok:true`)。設計メモは `blog/verification.md`(リポジトリ外)。
