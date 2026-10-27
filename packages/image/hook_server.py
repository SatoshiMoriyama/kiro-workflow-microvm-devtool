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


def write_result_to_s3(prefix: str, request_id: str) -> tuple[bool, str]:
    """echo 相当の結果を S3 に put する。成否とメッセージを返す。

    prefix: 依頼ごとのキー接頭辞(例 "runs/req-.../")。末尾スラッシュ込み想定。
    request_id: ログ/本文に埋める依頼ID(任意)。
    """
    bucket = os.environ.get("RESULT_BUCKET", "").strip()
    if not bucket:
        return False, "RESULT_BUCKET is not set"

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = f"hello from microvm {now} (requestId={request_id or '-'})\n"
    # prefix 配下に固定ファイル名で置く。prefix 未指定なら従来互換のフラット配置。
    if prefix:
        if not prefix.endswith("/"):
            prefix += "/"
        key = f"{prefix}result.txt"
    else:
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

    def _read_run_body(self) -> tuple[dict, str]:
        """/run のリクエストボディを読む。

        Lambda は固定構造 { "microvmId": "...", "runHookPayload": "<文字列>" } を送る。
        runHookPayload は文字列なので、こちら側でさらに JSON とみなしてパースする。
        戻り値: (payload_dict, microvm_id)。パース不能なら payload は {}。
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return {}, ""
        raw = self.rfile.read(length)
        try:
            outer = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log(f"run body parse failed: {exc}")
            return {}, ""
        if not isinstance(outer, dict):
            return {}, ""
        microvm_id = str(outer.get("microvmId", "") or "")
        inner_raw = outer.get("runHookPayload", "") or ""
        payload: dict = {}
        if isinstance(inner_raw, str) and inner_raw.strip():
            try:
                parsed = json.loads(inner_raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception as exc:  # noqa: BLE001
                log(f"runHookPayload parse failed: {exc}")
        return payload, microvm_id

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == READY_PATH:
            log("ready hook -> 200")
            self._respond(200, {"status": "ready"})
            return
        if path == RUN_PATH:
            payload, microvm_id = self._read_run_body()
            request_id = str(payload.get("requestId", "")).strip()
            prefix = str(payload.get("prefix", "")).strip()
            # prefix が未指定なら microvmId でフォールバック(依頼IDが無くても分離できる)。
            if not prefix and microvm_id:
                prefix = f"runs/{microvm_id}/"
            log(
                f"run hook -> microvmId={microvm_id!r} "
                f"prefix={prefix!r} requestId={request_id!r}"
            )
            ok, detail = write_result_to_s3(prefix, request_id or microvm_id)
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
