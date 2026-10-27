#!/usr/bin/env python3
"""Phase1 最小フックサーバ。

Lambda MicroVM のライフサイクルフックを HTTP で受ける常駐プロセス。

- GET/POST /aws/lambda-microvms/runtime/v1/ready
    ビルド時。即 200 を返す(初期化完了シグナル -> スナップショット取得)。
- GET/POST /aws/lambda-microvms/runtime/v1/run
    run-microvm 後に 1 回呼ばれる。echo 結果を S3 に put して 200 を返す。

結果は環境変数 RESULT_BUCKET のバケットへ `result-<microvm-id>.txt` として書く。
"""

import datetime
import json
import os
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

READY_PATH = "/aws/lambda-microvms/runtime/v1/ready"
RUN_PATH = "/aws/lambda-microvms/runtime/v1/run"
PORT = int(os.environ.get("HOOK_PORT", "8080"))


def log(msg: str) -> None:
    print(f"[hook_server] {msg}", flush=True)


def write_result_to_s3() -> tuple[bool, str]:
    """echo 相当の結果を S3 に put する。成否とメッセージを返す。"""
    bucket = os.environ.get("RESULT_BUCKET", "").strip()
    if not bucket:
        return False, "RESULT_BUCKET is not set"

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = f"hello from microvm {now}\n"
    key = f"result-{uuid.uuid4().hex[:12]}.txt"
    s3_uri = f"s3://{bucket}/{key}"

    try:
        proc = subprocess.run(
            ["aws", "s3", "cp", "-", s3_uri],
            input=body.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"aws s3 cp failed to launch: {exc}"

    if proc.returncode != 0:
        return False, (
            f"aws s3 cp exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return True, s3_uri


class HookHandler(BaseHTTPRequestHandler):
    def _respond(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == READY_PATH:
            log("ready hook -> 200")
            self._respond(200, {"status": "ready"})
            return
        if path == RUN_PATH:
            ok, detail = write_result_to_s3()
            if ok:
                log(f"run hook -> wrote {detail}")
                self._respond(200, {"status": "done", "s3": detail})
            else:
                log(f"run hook -> ERROR {detail}")
                # run フック失敗は 500 で返す(外部トラフィックは届かない)
                self._respond(500, {"status": "error", "detail": detail})
            return
        # 未知パスは 404
        self._respond(404, {"status": "not_found", "path": path})

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        # アクセスログは自前の log() に寄せる
        log("http " + (fmt % args))


def main() -> int:
    log(f"starting on :{PORT} (RESULT_BUCKET={os.environ.get('RESULT_BUCKET', '')})")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
