# Phase1 検証メモ — MicroVM で Kiro を API キー起動し「今日の天気は？」に答えさせる

調査日: 2026-10-27
目的: Lambda MicroVM 上で Kiro を非対話で起動し、「今日の天気は？」→ 天気取得 → 回答取得、までを回す最小 PoC の前提整理。

## ゴール（Phase1 のスコープ）

MicroVM 内で Kiro CLI を起動 → 「今日の天気は？」を投げる → Kiro が天気を取得 → その回答を手元で受け取る。git clone / push / PR / 通知は Phase2 以降（memo.md 側）。

## 結論: API キー 1 個で起動できる（ヘッドレス前提の正解）

Kiro CLI は `KIRO_API_KEY` 環境変数で非対話認証できる。ブラウザも人もいない MicroVM ゲストではこれ一択。ツール承認は後述のとおり v3 の `--trust-all-tools` で全許可（YOLO）にする。

```bash
export KIRO_API_KEY="your-api-key"
kiro-cli chat --trust-all-tools --no-interactive "今日の天気は？"
```

出典: Kiro CLI ドキュメント「API Key Authentication」および v3 permissions / headless（下記「承認は全許可」参照）。前提は Kiro CLI v3。

## 押さえる落とし穴 3 点

1. 認証の優先順位（ドキュメント明記）
   1. External IdP（エンタープライズ）
   2. 保存済み認証情報（`kiro-cli login` 由来）
   3. `KIRO_API_KEY`
   - API キーは「保存済み認証情報が無いときだけ」使われる。クリーンな MicroVM ゲストなら保存情報は無いので問題なし。
   - 注意: イメージビルド時に `kiro-cli login` 済みの状態を焼き込むと API キーが無視される。memo.md の「スナップショットに認証を焼くな」がそのまま効く。

2. 空文字は未設定扱い
   - `KIRO_API_KEY=""` は「未ログイン」エラーになる。受け渡しで空にしない。

3. キーのスコープ / 使えるツール・モデルは未確認
   - API キー認証自体が通っても、Kiro が `shell`(curl) や `web_fetch` を使えるか、どのモデルを呼べるかは API キー種別・エージェント設定に依存。ドキュメントだけでは断定不可。実機検証が必要。

## 天気取得の経路（要決定）

「今日の天気は？」に答えるには、ゲスト内の Kiro が外部から天気を取得できる必要がある。MicroVM は egress 既定でインターネット可。`--trust-all-tools`（全許可）にするので、どちらの経路でも承認待ちは出ない。残る前提は「そのツールがエージェントで有効か」と「ネットワーク到達性」だけ。

- A. `shell` ツールで `curl`（例: `curl wttr.in/Tokyo`）
  - 確実。Dockerfile に curl 同梱。全許可なので `allowedCommands` の個別指定は不要。
  - PoC では事故りにくい本命。
- B. Kiro 内蔵 `web_fetch` ツール
  - エージェント設定の `tools` に含まれていれば使える。承認は全許可で通る。

→ Phase1 は A（curl + shell）で進める想定。web_fetch も `tools` に入れておけば併用可。

## MicroVM 最小構成への落とし込み

イメージ（Dockerfile）に同梱:
- `kiro-cli` バイナリ（v3）
- `curl`、`awscli`（S3 回収用）
- エージェント設定 `~/.kiro/agents/default.json`
  - `tools` に `shell`（と必要なら `web_fetch`）を含める = ツールを「有効」にする
  - 承認は `--trust-all-tools` で全許可にするので `allowedCommands` の個別列挙は不要（下記「承認は全許可」参照）

run 時:
- Secrets Manager から `KIRO_API_KEY` を取得してゲスト env に設定（値でなく ARN 参照で渡す。ビルド時に焼かない。memo.md の設計に従う）
- `kiro-cli chat --trust-all-tools --no-interactive "札幌の天気を調べて weather.html を作って"` を実行
- 完了を自前センチネル(`___KIRO_DONE___`)で検知、成果物は S3 経由で回収（上記「回答・成果物の回収シナリオ」参照）

## 回答・成果物の回収シナリオ（確定: S3 経由）

「札幌の天気を調べて HTML を作って」のような成果物付きタスクを、MicroVM 内の Kiro に投げて回収するまでの流れ。websocat(port 8022) は「進捗・完了の検知」役、ファイル実体は S3 で回収、と2レイヤーに分ける。

```
[ホスト] run-microvm → create-microvm-shell-auth-token → websocat で port 8022 に接続
   │
[ゲスト] kiro-cli chat --trust-all-tools --no-interactive "札幌の天気を調べて weather.html を作って"
   │     echo "___KIRO_DONE___ exit=$?"      # 完了 + exit code
   │     aws s3 cp weather.html s3://<bucket>/runs/<id>/weather.html
   │     echo "___UPLOADED___"
   │
[ホスト] websocat 出力から ___KIRO_DONE___ を検知 → 完了判定（exit code も取得）
   │     ___UPLOADED___ を確認
   │     aws s3 cp s3://<bucket>/runs/<id>/weather.html ./weather.html
```

### なぜ websocat でファイル実体を取らないか

port 8022 は PTY over WebSocket（対話端末, memo.md 4章）。流れるのは「端末に表示される文字列」であってファイルそのものではない。`cat weather.html` を流して手元で拾う手もあるが、改行・制御文字・エスケープが混ざりファイルが汚れる。実体は S3 経由が確実（複数ファイルは `aws s3 sync` でまとめて回収可）。

### 完了検知: 自前センチネル

Kiro CLI に「応答完了を外部へ push する」ネイティブ機能は無い（memo.md 6章）。`--no-interactive` は one-shot で exit する前提なので、プロセス終了をシェルで捕まえる。

- ゲスト側: `kiro-cli chat ...` の直後に `echo "___KIRO_DONE___ exit=$?"` を実行。完了と exit code を同時に PTY へ出す。
- ホスト側: websocat 出力をパースし `___KIRO_DONE___` を検知したら完了判定。exit code で成否の一次判定（ただし完了 ≠ タスク成功。出力内容の妥当性は別途確認）。
- S3 アップロード完了は `___UPLOADED___` マーカーで確認してから `aws s3 cp` で取得。

ホスト側パース例:

```bash
websocat "wss://<endpoint>/shell" -H "..." | tee ~/microvm-out.log | \
while IFS= read -r line; do
  echo "$line"
  case "$line" in
    *___KIRO_DONE___*)  echo "[host] Kiro 応答完了" ;;
    *___UPLOADED___*)   echo "[host] S3 アップロード完了"; break ;;
  esac
done
aws s3 cp s3://<bucket>/runs/<id>/weather.html ./weather.html
```

### S3 経由にするための前提

- バケット（例: `s3://<bucket>/runs/<id>/`）。`<id>` は MicroVM 識別子や run ごとの一意キー。
- execution role に対象プレフィックスへの `s3:PutObject`（ゲスト書き込み用）。ゲストには短命 credential が自動付与される(memo.md 3章)ので長期キー不要。
- ホスト側の AWS 認証に対象プレフィックスへの `s3:GetObject`。
- 成果物・ログにシークレット（`KIRO_API_KEY` 等）を混ぜない。

### 承認は全許可（YOLO）にする — v3 `--trust-all-tools`

`--no-interactive` でもツール承認が要る設定だと、人のいないゲストで承認プロンプト待ちのまま固まる。これが MicroVM で一番詰まりやすい。MicroVM は Firecracker で VM レベル隔離 + 使い捨て(memo.md 1章)なので、ゲスト内のツール承認は全許可（YOLO）にして承認待ちを一切出さないのが素直。

- **`--trust-all-tools`**（v3）: セッション全体で全ツールを信頼＝全許可。Claude の `--dangerously-skip-permissions` 相当。`chat` サブコマンドに付ける（`kiro-cli chat --trust-all-tools ...`）。
- **`--trust-tools`**（v3）: headless で v3 のツールID を指定して個別事前承認。対話UIでは無視される（非対話専用）。全許可でなく絞りたい場合に使う。
- これで shell(curl)・web_fetch・fs 系（ファイル作成）・`aws s3 cp` が承認なしで通る。`allowedCommands` を1個ずつ列挙する必要はない。

出典: [Kiro CLI v3 permissions](https://kiro.dev/docs/cli/v3/permissions/) / [headless](https://kiro.dev/docs/cli/headless/)。内容は要約（Content was rephrased for compliance with licensing restrictions）。

#### v3 移行の注意（旧 allowedTools/toolsSettings は要変換）

[v3 agent-config](https://kiro.dev/docs/cli/v3/agent-config/) によると、`allowedTools` / `toolsSettings` を使う旧プロファイルは `permissions` ブロックが無いと `/upgrade-agent` で変換するまでスキップされる（要約）。つまり v2 流儀の `toolsSettings.shell.allowedCommands` は v3 ではそのまま効かない恐れがある。YOLO 方針なら細かい `allowedCommands` に頼らず `--trust-all-tools` 一発にするのが v3 では確実。ツールを「有効化」する `tools` の指定は引き続き必要（有効 ≠ 承認）。

#### 安全柵は「承認」から「IAM と egress」へ移る

全許可にすると、承認はもう安全柵として機能しない。MicroVM 隔離の内側なのでホストへの波及は無いが、残るリスクは別レイヤー:

- **egress 全開**: ゲストから外部へ何でも投げられる。シークレット（`KIRO_API_KEY`、Phase2 の git トークン）をゲスト env に置くので、全許可の Kiro がそれをログ・成果物・外部に出さないよう、出力にシークレットを混ぜない（memo.md 既出。全許可だと一層効く）。
- **execution role の権限範囲**: ゲストには execution role の短命 credential が入り(memo.md 3章)、Kiro が `aws` を自由に叩ける。全許可の実害はツール承認ではなく **IAM 権限の広さ**で決まる。execution role を対象 S3 プレフィックス等に最小権限で絞るのが実質の安全柵。

完了 ≠ 成功。exit 0 でも的外れな HTML を作って終わる可能性はあるので、回収後に中身を検品する。

## 検証を切り分ける順序（提案）

いきなり MicroVM に載せると切り分け不能。段階を踏む。

1. ローカル検証: ローカル（または素の Docker）で `KIRO_API_KEY` + `kiro-cli chat --trust-all-tools --no-interactive "今日の天気は？"` が通るか。天気取得の経路（curl/web_fetch）と、`--trust-all-tools` で承認待ちが出ないことの確認もここで。
2. イメージ化: 上記が通る構成を Dockerfile に固める。
3. MicroVM 化: `create-microvm-image` → `run-microvm`(SHELL_INGRESS + INTERNET_EGRESS) → シェル接続 → 実行 → 回答回収。

## 未確認・要対応（Phase1 を始める前のブロッカー）

- Kiro の API キーを保有しているか（無いと PoC 開始不可）。
- 天気取得は A（curl wttr.in）でよいか。対象都市（例: 東京）。
- AWS CLI 認証・リージョン、当該アカウントで `aws lambda-microvms` が使えるか（新機能のため要確認）。
- API キーで `shell`/`web_fetch` と対象モデルが実際に使えるか（実機検証）。

## 参考

- Kiro CLI: API Key Authentication / kiro-cli login / kiro-cli chat `--no-interactive`
- MicroVM 側の設計・API・通知・認証受け渡しは memo.md を参照
