# Phase2 MicroVM イメージ。
# al2023 の Lambda MicroVM コンテナベースに awscli・Python フックサーバ・kiro-cli を載せる。
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# awscli(Secrets 取得・S3 put)・python3(フックサーバ)・kiro-cli 実行に要るツール。
# unzip/tar は配布 zip の展開用、ca-certificates は HTTPS 取得に必須。
# curl は dnf で入れない: フル版 curl は AL2023 プリインストールの curl-minimal と
# 衝突し dnf 全体が失敗する(実機ビルドで確認)。Kiro の HTTP 取得は curl-minimal で足りる。
# フル curl がどうしても要る場合のみ `dnf install -y --allowerasing curl` を別 RUN で行う。
RUN { dnf install -y python3 awscli-2 unzip tar ca-certificates \
      || dnf install -y python3 awscli unzip tar ca-certificates; } \
    && dnf clean all

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

WORKDIR /app
COPY hook_server.py /app/hook_server.py

# ライフサイクルフックを受ける HTTP サーバを常駐させる。
# /ready に 200 を返すとビルド時にスナップショットが取られ、
# run-microvm 後に /run が叩かれて Kiro CLI を実行し成果物を S3 に put する。
ENV HOOK_PORT=8080
CMD ["python3", "/app/hook_server.py"]
