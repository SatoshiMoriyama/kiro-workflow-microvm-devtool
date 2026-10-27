# Phase1 最小 MicroVM イメージ。
# al2023 の Lambda MicroVM コンテナベースに awscli と Python のフックサーバを載せる。
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# awscli(結果を S3 に put) と python3(フックサーバ) を導入
RUN dnf install -y python3 awscli-2 || dnf install -y python3 awscli; \
    dnf clean all

WORKDIR /app
COPY hook_server.py /app/hook_server.py

# ライフサイクルフックを受ける HTTP サーバを常駐させる。
# /ready に 200 を返すとビルド時にスナップショットが取られ、
# run-microvm 後に /run が叩かれて echo 結果を S3 に put する。
ENV HOOK_PORT=8080
CMD ["python3", "/app/hook_server.py"]
