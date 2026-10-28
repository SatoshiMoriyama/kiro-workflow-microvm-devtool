#!/usr/bin/env python3
"""Phase2 フックサーバ。

Lambda MicroVM のライフサイクルフックを HTTP で受ける常駐プロセス。

- GET/POST /aws/lambda-microvms/runtime/v1/ready
    ビルド時。即 200 を返す(初期化完了シグナル -> スナップショット取得)。
- GET/POST /aws/lambda-microvms/runtime/v1/run
    run-microvm 後に 1 回呼ばれる。/run の RunTimeoutInSeconds は上限 60 秒のため、
    Kiro をここで同期実行はできない。Secrets Manager からのキー取得 →
    `kiro-cli chat --trust-all-tools --no-interactive "<prompt>"` 実行 →
    生成ファイル・stdout・status.json の S3 put 一式をバックグラウンドスレッドで
    起動し、即 200({"status":"accepted"})を返す。Kiro は MicroVM が生きている限り
    裏で走り、完了時に status.json を S3 に put する。ホスト(run.sh)は status.json の
    出現をポーリングして完了を検知する。

成果物は環境変数 RESULT_BUCKET のバケットの prefix(runs/<requestId>/)配下に置く。
KIRO_API_KEY の値は CloudWatch Logs・S3 成果物・status.json・レスポンスに一切出さない。
成果物の内容だけでなくファイル名も検査し、キーを含む名前は S3 key・status・ログ・
応答のいずれにも出さず固定の安全表記に置換する。
"""

import datetime
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

READY_PATH = "/aws/lambda-microvms/runtime/v1/ready"
RUN_PATH = "/aws/lambda-microvms/runtime/v1/run"
PORT = int(os.environ.get("HOOK_PORT", "8080"))

# prompt 未指定時のデフォルト(run.sh 側にも同じ既定を持つ)。
DEFAULT_PROMPT = "札幌の天気を調べて weather.html を作って"

# ゲスト内で kiro-cli を実行する作業ディレクトリのベース。
WORKDIR_BASE = "/tmp/kiro-work"

# ゲスト kiro-cli プロセスの timeout(秒)。非同期モデルでは /run の 60 秒上限から
# 独立し、MicroVM 寿命(run.sh の --maximum-duration-in-seconds)に収まる値にする。
# 単調関係: KIRO_TIMEOUT_SECONDS < VM 寿命(--maximum-duration) < ホストポーリング上限。
# 既定 1200。環境変数 KIRO_TIMEOUT_SECONDS で上書き可能。
try:
    KIRO_TIMEOUT_SECONDS = int(os.environ.get("KIRO_TIMEOUT_SECONDS", "1200"))
except ValueError:
    KIRO_TIMEOUT_SECONDS = 1200

# 成果物として put しない予約名。
RESERVED_NAMES = {"kiro-stdout.txt", "status.json"}

# キー形式の保険パターン(第2段マスク)。
_KSK_PATTERN = re.compile(r"ksk_\S+")

# contains_secret のバイト列検査でキー形式を拾う保険パターン(空白以外を貪欲に)。
_KSK_PATTERN_BYTES = re.compile(rb"ksk_\S+")


def log(msg: str) -> None:
    print(f"[hook_server] {msg}", flush=True)


# --------------------------------------------------------------------------
# 純関数ユーティリティ(kiro-cli / AWS に依存しない。単体テスト対象)
# --------------------------------------------------------------------------


def mask_secret(text, api_key):
    """API キーの非漏洩マスク(二段構え)。

    第1段: api_key(非空時)を文字列完全一致で [REDACTED] に置換。
    第2段: 正規表現 ksk_\\S+ を [REDACTED] に置換。
    text が非文字列でも落ちない(str() してから処理)。
    """
    if not isinstance(text, str):
        text = str(text)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = _KSK_PATTERN.sub("[REDACTED]", text)
    return text


def select_artifacts(workdir):
    """workdir 直下(非再帰)の通常ファイル名のみを昇順で返す。

    除外: "." 始まりの隠しファイル、ディレクトリ、予約名(kiro-stdout.txt / status.json)。
    戻りは basename の昇順リスト。
    """
    result = []
    try:
        entries = os.listdir(workdir)
    except OSError:
        return result
    for name in entries:
        if name.startswith("."):
            continue
        if name in RESERVED_NAMES:
            continue
        full = os.path.join(workdir, name)
        if not os.path.isfile(full):
            continue
        result.append(name)
    result.sort()
    return result


def name_contains_secret(name, api_key):
    """ファイル名に実キー値(非空時)または ksk_\\S+ が含まれれば True。

    --trust-all-tools の Kiro はファイル名に API キーを埋め込みうる。名前は
    S3 key・status.json・ログ・/run 応答へ伝播するため、内容と同じ二段検査を
    名前にも適用する(FR-5 の非漏洩境界をファイル名が迂回しないように)。
    """
    if not isinstance(name, str):
        name = str(name)
    if api_key and api_key in name:
        return True
    if _KSK_PATTERN.search(name):
        return True
    return False


def contains_secret(path, api_key):
    """ファイル内容に実キー値(非空時)または ksk_\\S+ が含まれれば True。

    FR-5 を満たすため fail-closed。バイト列として読み、実キーの UTF-8 バイト列と
    ksk_ のバイトパターンを検査する(エンコーディング・不正バイトに依存しない)。
    読取不能(OSError 等)は検査不能として安全側に倒し True を返す(put させず
    skippedArtifacts に回す)。デコード失敗を「漏洩なし」にしない。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        # 検査できない = 安全を保証できない。put させない(fail-closed)。
        return True
    if api_key:
        key_bytes = api_key.encode("utf-8", "surrogatepass") if isinstance(api_key, str) else bytes(api_key)
        if key_bytes and key_bytes in data:
            return True
    if _KSK_PATTERN_BYTES.search(data):
        return True
    return False


def build_status(
    request_id,
    prompt,
    exit_code,
    timed_out,
    artifacts,
    skipped,
    failed_uploads,
    stdout_key,
    started_at,
    finished_at,
):
    """status.json の辞書を組み立てる。

    ok = (exit_code == 0 and not timed_out and not failed_uploads)。
    exit_code は None 許容。S3 put 失敗(stdout/成果物)があれば ok=false
    (設計 §5: put 非ゼロは致命)。キー名は設計 §3 status.json スキーマの原文どおり。
    failedUploads は put に失敗した対象名(stdout / 成果物名)。キー値は含めない。
    """
    ok = (exit_code == 0) and (not timed_out) and (not failed_uploads)
    return {
        "requestId": request_id,
        "prompt": prompt,
        "exitCode": exit_code,
        "timedOut": bool(timed_out),
        "artifacts": list(artifacts),
        "skippedArtifacts": list(skipped),
        "failedUploads": list(failed_uploads),
        "stdoutKey": stdout_key,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "ok": ok,
    }


def parse_run_payload(outer_raw):
    """/run の外側ボディ { "microvmId", "runHookPayload" } を読み、

    内側 runHookPayload(文字列 JSON)を再パースして
    (payload_dict, microvm_id) を返す。prompt の補完は呼び出し側で行う。
    パース不能なら payload は {}。
    """
    if isinstance(outer_raw, (bytes, bytearray)):
        try:
            outer_raw = outer_raw.decode("utf-8")
        except Exception:  # noqa: BLE001
            return {}, ""
    if not isinstance(outer_raw, str) or not outer_raw.strip():
        return {}, ""
    try:
        outer = json.loads(outer_raw)
    except Exception as exc:  # noqa: BLE001
        log(f"run body parse failed: {exc}")
        return {}, ""
    if not isinstance(outer, dict):
        return {}, ""
    microvm_id = str(outer.get("microvmId", "") or "")
    inner_raw = outer.get("runHookPayload", "") or ""
    payload = {}
    if isinstance(inner_raw, str) and inner_raw.strip():
        try:
            parsed = json.loads(inner_raw)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception as exc:  # noqa: BLE001
            log(f"runHookPayload parse failed: {exc}")
    return payload, microvm_id


def resolve_prompt(payload):
    """payload["prompt"] を取り出し、未指定/空/非文字列ならデフォルトへ。"""
    value = payload.get("prompt") if isinstance(payload, dict) else None
    if isinstance(value, str) and value.strip():
        return value
    return DEFAULT_PROMPT


# --------------------------------------------------------------------------
# AWS / kiro-cli 連携
# --------------------------------------------------------------------------


def _fetch_api_key(secret_arn):
    """Secrets Manager から KIRO_API_KEY を取得する。

    戻り値: (api_key, error)。成功時は (値, None)、失敗時は (None, 値を伏せた理由)。
    例外メッセージも素で返さず、キー値が混ざりうる箇所は出さない。
    """
    try:
        proc = subprocess.run(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                secret_arn,
                "--query",
                "SecretString",
                "--output",
                "text",
            ],
            capture_output=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None, "api key fetch failed to launch"
    if proc.returncode != 0:
        return None, f"api key fetch exited {proc.returncode}"
    value = proc.stdout.decode("utf-8", "replace").strip()
    if not value:
        return None, "api key is empty"
    return value, None


def _s3_put_bytes(bucket, key, data):
    """標準入力経由で S3 に put する。成否とエラー文字列を返す。"""
    s3_uri = f"s3://{bucket}/{key}"
    try:
        proc = subprocess.run(
            ["aws", "s3", "cp", "-", s3_uri],
            input=data,
            capture_output=True,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"aws s3 cp(stdin) failed to launch: {exc}"
    if proc.returncode != 0:
        return False, (
            f"aws s3 cp(stdin) exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return True, s3_uri


def _s3_put_file(bucket, key, path):
    """ローカルファイルを S3 に put する。成否とエラー文字列を返す。"""
    s3_uri = f"s3://{bucket}/{key}"
    try:
        proc = subprocess.run(
            ["aws", "s3", "cp", path, s3_uri],
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"aws s3 cp failed to launch: {exc}"
    if proc.returncode != 0:
        return False, (
            f"aws s3 cp exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return True, s3_uri


def run_kiro_and_upload(prefix, request_id, prompt):
    """Kiro CLI を同期実行し、stdout・成果物・status.json を S3 に put する。

    戻り値: (ok, status_dict)。ok は
    (exitCode==0 and not timedOut and stdout・成果物の put 失敗なし
     and status.json 自体の put 成功)。設計 §5 どおり S3 put 非ゼロは致命。
    失敗時も status.json は必ず put を試みる。KIRO_API_KEY の値はログ・返り値・
    status・成果物に一切出さない。
    """
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    bucket = os.environ.get("RESULT_BUCKET", "").strip()
    if not bucket:
        # バケットが無いと status も put できない。致命として扱う。
        raise RuntimeError("RESULT_BUCKET is not set")

    stdout_key = f"{prefix}kiro-stdout.txt"
    status_key = f"{prefix}status.json"

    def finalize(api_key, exit_code, timed_out, artifacts, skipped, failed_uploads):
        """status.json を組み立てて put する。

        戻り値: (status_dict, status_put_ok)。status.json 自体の put 失敗は
        status を残せないため、呼び出し側で必ず /run 500 とする(設計 §5)。
        status["ok"] は exitCode / timedOut / stdout・成果物の put 失敗を集約する。
        """
        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        status = build_status(
            request_id=request_id,
            prompt=prompt,
            exit_code=exit_code,
            timed_out=timed_out,
            artifacts=artifacts,
            skipped=skipped,
            failed_uploads=failed_uploads,
            stdout_key=stdout_key,
            started_at=started_at,
            finished_at=finished_at,
        )
        body = json.dumps(status, ensure_ascii=False, indent=2)
        body = mask_secret(body, api_key)
        status_put_ok, detail = _s3_put_bytes(
            bucket, status_key, body.encode("utf-8")
        )
        if not status_put_ok:
            log(f"status.json put failed: {mask_secret(detail, api_key)}")
        return status, status_put_ok

    # ① API キー取得(値はログ/返り値に出さない)
    secret_arn = os.environ.get("KIRO_API_KEY_SECRET_ARN", "").strip()
    if not secret_arn:
        log("KIRO_API_KEY_SECRET_ARN is not set")
        status, _ = finalize(None, None, False, [], [], [])
        return False, status

    api_key, err = _fetch_api_key(secret_arn)
    if api_key is None:
        # err は値を伏せた理由のみ。
        log(f"api key fetch failed: {err}")
        status, _ = finalize(None, None, False, [], [], [])
        return False, status

    # ③ 作業ディレクトリ作成
    workdir = os.path.join(WORKDIR_BASE, request_id or "default")
    os.makedirs(workdir, exist_ok=True)

    # ④ kiro-cli 実行(API キーは子プロセス env にのみ渡す)
    env = dict(os.environ)
    env["KIRO_API_KEY"] = api_key
    exit_code = None
    timed_out = False
    stdout_bytes = b""
    try:
        proc = subprocess.run(
            [
                "kiro-cli",
                "chat",
                "--trust-all-tools",
                "--no-interactive",
                prompt,
            ],
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=KIRO_TIMEOUT_SECONDS,
        )
        exit_code = proc.returncode
        stdout_bytes = proc.stdout or b""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout_bytes = exc.stdout or b""
        log("kiro-cli timed out")
    except Exception as exc:  # noqa: BLE001
        exit_code = None
        log(f"kiro-cli failed to launch: {mask_secret(str(exc), api_key)}")

    # put 失敗(aws s3 cp 非ゼロ)は設計 §5 で致命。対象名を集約して ok=false にする。
    # ※キー混入でのスキップ(skipped)は意図的な FR-5 防御で put 失敗ではない。
    failed_uploads = []

    # ⑤ stdout をマスクして put
    stdout_text = stdout_bytes.decode("utf-8", "replace")
    stdout_masked = mask_secret(stdout_text, api_key)
    put_ok, detail = _s3_put_bytes(
        bucket, stdout_key, stdout_masked.encode("utf-8")
    )
    if not put_ok:
        log(f"stdout put failed: {mask_secret(detail, api_key)}")
        failed_uploads.append("kiro-stdout.txt")

    # ⑥ 成果物を選別し、キー混入チェックに合格したものだけ put
    artifacts = []
    skipped = []
    redacted_name_count = 0
    for name in select_artifacts(workdir):
        full = os.path.join(workdir, name)
        # 名前にキーが埋め込まれている場合、元名は S3 key・status・ログ・応答の
        # どこにも出さない。put もせず、固定の安全表記で skipped に記録する。
        if name_contains_secret(name, api_key):
            redacted_name_count += 1
            safe_name = f"<redacted-filename-{redacted_name_count}>"
            skipped.append(safe_name)
            log(f"artifact skipped (secret in filename): {safe_name}")
            continue
        if contains_secret(full, api_key):
            skipped.append(name)
            log(f"artifact skipped (secret detected): {name}")
            continue
        put_ok, detail = _s3_put_file(bucket, f"{prefix}{name}", full)
        if put_ok:
            artifacts.append(name)
        else:
            # put 失敗は致命。ファイル名のみ記録(キー値は含めない)。
            failed_uploads.append(name)
            log(f"artifact put failed: {name}: {mask_secret(detail, api_key)}")

    # ⑦ status.json を生成・put
    status, status_put_ok = finalize(
        api_key, exit_code, timed_out, artifacts, skipped, failed_uploads
    )
    # status.json 自体の put に失敗したら status を残せないため必ず /run 500。
    ok = status["ok"] and status_put_ok
    return ok, status


class HookHandler(BaseHTTPRequestHandler):
    def _respond(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_run_body(self) -> tuple[dict, str]:
        """/run のリクエストボディを読み、(payload_dict, microvm_id) を返す。"""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return {}, ""
        raw = self.rfile.read(length)
        return parse_run_payload(raw)

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
            prompt = resolve_prompt(payload)
            # prefix 未指定なら microvmId でフォールバックし末尾スラッシュ正規化。
            if not prefix and microvm_id:
                prefix = f"runs/{microvm_id}/"
            if prefix and not prefix.endswith("/"):
                prefix += "/"
            log(
                f"run hook -> microvmId={microvm_id!r} "
                f"prefix={prefix!r} requestId={request_id!r}"
            )
            # 非同期モデル: /run の RunTimeoutInSeconds は上限 60 秒のため Kiro を
            # ここで待てない。線引きは次のとおり。
            #   - バックグラウンド起動"前"に判明する前提エラー(即時に判定でき、
            #     status すら書けないもの。例: RESULT_BUCKET 未設定)は /run を 500。
            #   - Kiro 実行やキー取得の失敗など"実行時"の成否は従来どおり
            #     run_kiro_and_upload がバックグラウンドで status.json に記録する。
            #     /run 自体は受理成功として 200({"status":"accepted"})を返す。
            # 完了検知・成否判定はホスト側が status.json(ok/exitCode/timedOut/
            # failedUploads)で行う。
            bucket = os.environ.get("RESULT_BUCKET", "").strip()
            if not bucket:
                # status も put できない前提エラー。即時に判定できるので 500。
                detail = "RESULT_BUCKET is not set"
                log(f"run hook -> ERROR {detail}")
                self._respond(500, {"status": "error", "detail": detail})
                return

            def _background(prefix=prefix, request_id=request_id,
                            microvm_id=microvm_id, prompt=prompt):
                # バックグラウンド実行の戻り(ok/status)で HTTP ステータスは決めない。
                # 成否は run_kiro_and_upload が status.json に書く。例外は握りつぶさず
                # ログに出す(キー値は env にしか無いが念のためパターンマスクを通す)。
                try:
                    run_kiro_and_upload(prefix, request_id or microvm_id, prompt)
                except Exception as exc:  # noqa: BLE001
                    log(f"run hook background -> ERROR {mask_secret(str(exc), None)}")

            threading.Thread(target=_background, daemon=True).start()
            log("run hook -> accepted (background started)")
            self._respond(200, {"status": "accepted"})
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
