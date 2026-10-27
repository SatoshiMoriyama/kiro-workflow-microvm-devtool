# kiro-workflow-microvm-devtool

Lambda MicroVMs 上で Kiro を動かすための開発ツール。pnpm モノレポ。

Phase1 は「MicroVM が起動して結果を S3 に吐く」最小疎通の確認（Kiro はまだ載せない）。

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
│     ├─ Dockerfile             # al2023-minimal + awscli + Python フックサーバ
│     └─ hook_server.py         # /ready に 200、/run で echo 結果を S3 に put
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
- **実行モデル**: Dockerfile の CMD でフックサーバを常駐 → `/ready` に 200 でスナップショット → `run-microvm` で復帰 → `/run` フックで echo→S3。
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

# 6. MicroVM を起動して結果を回収(依頼IDプレフィックス配下に格納・回収)
AWS_PROFILE=work ./scripts/run.sh
```

成功条件: `run.sh` が `runs/<requestId>/result.txt` を取得し、
`hello from microvm <timestamp> (requestId=...)` を表示して `results/<requestId>/` に回収する。

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
