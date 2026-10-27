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
- **code artifact の zip** は CDK 外(`package-image.sh`)で固定キー `code/app.zip` に置き、URI を透明にする。`CfnMicrovmImage` はこの zip を参照するので zip アップロード → deploy の順。

## 手順

```bash
# 1. 依存インストール
pnpm install

# 2. (初回のみ) CDK bootstrap
AWS_PROFILE=work pnpm --filter infra exec cdk bootstrap aws://495236580548/ap-northeast-1

# 3. バケット/ロールを先に作る(イメージはこの時点では zip 不在でビルド失敗しうるため、
#    まずバケットだけ作り、zip 投入後にイメージを再デプロイする二段構え)
AWS_PROFILE=work pnpm --filter infra deploy

# 4. code artifact を zip 化して artifact バケットへ
AWS_PROFILE=work ./scripts/package-image.sh

# 5. イメージをビルドさせる(zip 投入後に再 deploy、または update-microvm-image)
AWS_PROFILE=work pnpm --filter infra deploy

# 6. MicroVM を起動して結果を回収
AWS_PROFILE=work ./scripts/run.sh
```

成功条件: `run.sh` が result バケットから `hello from microvm <timestamp>` を取得して表示する。

## 未検証（実機で詰める）

- `CfnMicrovmImage` が zip 不在だとビルド失敗する場合のデプロイ順序（手順3/5 の二段構えで回避する想定）。
- `run-microvm` のレスポンスの microvm 識別子フィールド名（`run.sh` は複数候補をフォールバック）。
- `/ready` 即 200 でスナップショットが成立するか（アプリ初期化が実質ゼロのため問題ない見込み）。
- arm64 ベースと Docker ビルドアーキテクチャの整合。
