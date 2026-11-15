# Phase2 MicroVM イメージ。
# al2023 の Lambda MicroVM コンテナベースに awscli・Python フックサーバ・kiro-cli を載せる。
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# awscli(Secrets 取得・S3 put)・python3(フックサーバ)・kiro-cli 実行に要るツール。
# unzip/tar は配布 zip の展開用、ca-certificates は HTTPS 取得に必須。
# git は GitHub の clone/commit/push に必須(Kiro 自身が git を実行する)。
# curl は dnf で入れない: フル版 curl は AL2023 プリインストールの curl-minimal と
# 衝突し dnf 全体が失敗する(実機ビルドで確認)。Kiro の HTTP 取得は curl-minimal で足りる。
# フル curl がどうしても要る場合のみ `dnf install -y --allowerasing curl` を別 RUN で行う。
RUN { dnf install -y python3 awscli-2 git unzip tar ca-certificates \
      || dnf install -y python3 awscli git unzip tar ca-certificates; } \
    && dnf clean all

# Node 22 と pnpm(MicroVM 内で pnpm install / tsc --noEmit / cdk synth を実行するため。issue #16)。
# 選定理由:
# - Node: AL2023 公式リポジトリの nodejs22(+ npm)を dnf で入れる。ARM_64 向けビルドが
#   提供され、AL2023 の glibc 2.34 とリンクが一致し、既存の dnf 層と同じ経路で署名検証・更新が効く。
#   公式バイナリ tar は取得 URL・チェックサム・展開先の管理が増えるため採らない。
#   nodejs22 が引けない版のベースでは、公式 linux-arm64 tar(glibc 2.28 以上で動作)へ切り替える。
# - pnpm: ルート package.json の packageManager(pnpm@12.10.1)に合わせ npm -g で版固定して入れる。
#   corepack は Node 25 以降で同梱されなくなる方向で、初回実行時にネットワーク取得が走って
#   スナップショット後の挙動が不安定になりうるため使わない。
# 別 RUN にして、既存の python3/awscli 層のキャッシュと失敗切り分けを保つ。
ARG PNPM_VERSION=12.10.1
RUN dnf install -y nodejs22 nodejs22-npm \
    && dnf clean all \
    && npm install -g "pnpm@${PNPM_VERSION}" \
    && npm cache clean --force

# kiro-cli の配布 URL(既定値集約用の定数)。
# AL2023 の glibc は 2.34 で、gnu ビルドの要求 2.39 に満たないため musl ビルドを使う
# (install.sh の glibc 検査を静的リンクで回避できる)。
# MicroVM イメージは AWS 側でビルドされ docker build --build-arg 経路は無いので、
# URL/版を変えるときは この ARG 既定値を直接編集 -> package-image.sh 再実行 -> cdk deploy 再ビルド。
ARG KIRO_CLI_DL_URL=https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-aarch64-linux-musl.zip

# zip を取得 -> 展開 -> グローバル導入(認証は焼かない: Q_SKIP_SETUP=1) -> クリーンアップ。
RUN curl -fsSL "$KIRO_CLI_DL_URL" -o /tmp/kirocli.zip \
    && unzip -q /tmp/kirocli.zip -d /tmp \
    && cd /tmp/kirocli \
    && Q_INSTALL_GLOBAL=1 Q_SKIP_SETUP=1 ./install.sh \
    && rm -rf /tmp/kirocli /tmp/kirocli.zip
# 現行インストーラは Q_INSTALL_GLOBAL=1 でも /root/.local/bin に導入する(実機ビルドで確認)。
# hook_server は "kiro-cli" を PATH 解決で起動するため、導入先を PATH に追加する。
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app
COPY hook_server.py /app/hook_server.py

# MicroVM 内 kiro-cli 向けグローバル steering を焼き込む。
# KIRO_HOME を固定することで、GitHub フローで HOME を per-run に差し替えても
# グローバル設定ルート(= steering の読み込み先)が /root/.kiro で一定になる。
# steering/ 配下の *.md を /root/.kiro/steering/ に COPY する(全 run に常時適用)。
# 更新は packages/image/steering/ に .md を足す -> 再デプロイ(内容ハッシュ更新で CFN 再ビルド)。
ENV KIRO_HOME=/root/.kiro
COPY steering/ /root/.kiro/steering/

# ライフサイクルフックを受ける HTTP サーバを常駐させる。
# /ready に 200 を返すとビルド時にスナップショットが取られ、
# run-microvm 後に /run が叩かれて Kiro CLI を実行し成果物を S3 に put する。
ENV HOOK_PORT=8080
CMD ["python3", "/app/hook_server.py"]
