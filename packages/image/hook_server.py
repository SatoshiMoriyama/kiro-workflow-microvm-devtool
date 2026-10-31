#!/usr/bin/env python3
"""MicroVM フックサーバ。

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

# repoUrl 未指定時の baseBranch 既定。
DEFAULT_BASE_BRANCH = "main"

# GitHub への commit に使う固定 author(実行者個人を識別しない bot 表記)。
GIT_AUTHOR_NAME = "kiro-bot"
GIT_AUTHOR_EMAIL = "kiro-bot@users.noreply.github.com"

# GIT_ASKPASS で使う helper スクリプトのファイル名(Kiro の HOME 配下に置く)。
GIT_ASKPASS_FILENAME = "git-askpass.sh"

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

# MicroVM 内 kiro-cli 向けグローバル steering の焼き込み先(= KIRO_HOME/steering)。
# Dockerfile で packages/image/steering/*.md をここへ COPY し、ENV KIRO_HOME=/root/.kiro
# 固定で天気タスク・GitHub フロー双方の kiro-cli に常時適用させる。
GLOBAL_STEERING_DIR = "/root/.kiro/steering"

# GitHub フローのフォールバックで clone 先リポジトリ直下に置く steering の相対パス。
GLOBAL_STEERING_WORKSPACE_SUBDIR = ".kiro/steering"

# キー形式の保険パターン(第2段マスク)。
_KSK_PATTERN = re.compile(r"ksk_\S+")

# contains_secret のバイト列検査でキー形式を拾う保険パターン(空白以外を貪欲に)。
_KSK_PATTERN_BYTES = re.compile(rb"ksk_\S+")

# GitHub トークンの保険パターン(新形式: ghp_/gho_/ghu_/ghs_/ghr_ と fine-grained
# PAT の github_pat_ を含む)。
_GH_TOKEN_PATTERN = re.compile(r"(gh[pousr]|github_pat)_\S+")
_GH_TOKEN_PATTERN_BYTES = re.compile(rb"(gh[pousr]|github_pat)_\S+")

# GitHub トークンの旧形式(40 桁 hex)。単語境界で囲み通常の hex 文字列を巻き込みにくくする。
_GH_HEX_PATTERN = re.compile(r"\b[0-9a-f]{40}\b")
_GH_HEX_PATTERN_BYTES = re.compile(rb"\b[0-9a-f]{40}\b")

# テキストのマスク(ログ・status.json・stdout)に使う保険パターン。実キー値の
# 完全一致に加え、値が分からなくてもパターンで拾う。マスクは「置換して出さない」
# だけなので、40 桁 hex(旧 PAT 形式)もここに含めて取りこぼしを無くす。
_SECRET_PATTERNS = (_KSK_PATTERN, _GH_TOKEN_PATTERN, _GH_HEX_PATTERN)

# 成果物のファイル名・内容の漏洩検査(put させず skip する fail-closed 判定)に
# 使うパターン。こちらは 40 桁 hex を**含めない**: git のコミット SHA / blob hash が
# ちょうど 40 桁 hex で、含めると clone 由来の正当な成果物(report.json の
# pushedCommit 等)を軒並み skip してしまう。実 PAT 値(完全一致)と ksk_ /
# gh[pousr]_ の明確なトークン形式だけで判定する。
_SKIP_PATTERNS = (_KSK_PATTERN, _GH_TOKEN_PATTERN)
_SKIP_PATTERNS_BYTES = (_KSK_PATTERN_BYTES, _GH_TOKEN_PATTERN_BYTES)


def log(msg: str) -> None:
    print(f"[hook_server] {msg}", flush=True)


# --------------------------------------------------------------------------
# 純関数ユーティリティ(kiro-cli / AWS に依存しない。単体テスト対象)
# --------------------------------------------------------------------------


def mask_secrets(text, secret_values, patterns=_SECRET_PATTERNS):
    """複数シークレットの非漏洩マスク(二段構え)。

    第1段: secret_values の各実値(非空)を文字列完全一致で [REDACTED] に置換。
    第2段: patterns の各正規表現にマッチした箇所を [REDACTED] に置換。
    text が非文字列でも落ちない(str() してから処理)。
    secret_values は str/None の反復可能(None・空文字はスキップ)。
    """
    if not isinstance(text, str):
        text = str(text)
    for value in secret_values or ():
        if value:
            text = text.replace(value, "[REDACTED]")
    for pattern in patterns:
        text = pattern.sub("[REDACTED]", text)
    return text


def mask_secret(text, api_key):
    """単一キー向けの後方互換ラッパ。mask_secrets に委譲する。

    KIRO_API_KEY の実値(非空時)に加え、ksk_ / GitHub トークンの各パターンも
    [REDACTED] に置換する(非漏洩境界を単一関数で共有する)。
    """
    return mask_secrets(text, [api_key])


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


def _as_secret_values(secret_values):
    """後方互換: 単一の api_key(str/None)でも実値リストでも受けられるよう正規化する。

    既存呼び出し(api_key 1 本)を壊さず、PAT を含む複数実値のリストも受けられる。
    戻りは非空文字列のタプル。
    """
    if secret_values is None:
        return ()
    if isinstance(secret_values, (str, bytes, bytearray)):
        secret_values = [secret_values]
    result = []
    for value in secret_values:
        if value:
            result.append(value)
    return tuple(result)


def name_contains_secret(name, secret_values):
    """ファイル名に実値(非空時)または明確なトークン形式が含まれれば True。

    --trust-all-tools の Kiro はファイル名に API キー / PAT を埋め込みうる。名前は
    S3 key・status.json・ログ・/run 応答へ伝播するため、内容と同じ検査を
    名前にも適用する(非漏洩境界をファイル名が迂回しないように)。検査対象は
    実値(KIRO_API_KEY・PAT)の完全一致と ksk_ / (gh[pousr]|github_pat)_ 形式。
    40 桁 hex は git SHA と衝突するため skip 判定には使わない(マスク側のみで扱う)。
    secret_values は単一の api_key(str/None)でも実値リストでも受ける(後方互換)。
    """
    if not isinstance(name, str):
        name = str(name)
    for value in _as_secret_values(secret_values):
        text_value = value if isinstance(value, str) else value.decode("utf-8", "replace")
        if text_value and text_value in name:
            return True
    for pattern in _SKIP_PATTERNS:
        if pattern.search(name):
            return True
    return False


def contains_secret(path, secret_values):
    """ファイル内容に実値(非空時)または明確なトークン形式が含まれれば True。

    FR-5 を満たすため fail-closed。バイト列として読み、各実値(KIRO_API_KEY・PAT)の
    UTF-8 バイト列と ksk_ / (gh[pousr]|github_pat)_ のバイトパターンを検査する
    (エンコーディング・不正バイトに依存しない)。40 桁 hex は git SHA と衝突する
    ため skip 判定には使わない。読取不能(OSError 等)は検査不能として安全側に倒し
    True を返す(put させず skippedArtifacts に回す)。デコード失敗を「漏洩なし」に
    しない。secret_values は単一の api_key(str/None)でも実値リストでも受ける
    (後方互換)。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        # 検査できない = 安全を保証できない。put させない(fail-closed)。
        return True
    for value in _as_secret_values(secret_values):
        if isinstance(value, (bytes, bytearray)):
            value_bytes = bytes(value)
        else:
            value_bytes = value.encode("utf-8", "surrogatepass")
        if value_bytes and value_bytes in data:
            return True
    for pattern in _SKIP_PATTERNS_BYTES:
        if pattern.search(data):
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


# repoUrl として許可する形式: https://github.com/ 直下のパスのみ。userinfo
# (`user:pass@`)や別ホストへの差し替えを拒否し、clone 先を github.com に固定する。
# 認証はトークン非埋め込み(GIT_ASKPASS 経由)が前提のため、URL に `@` を許さない。
_REPO_URL_PATTERN = re.compile(r"^https://github\.com/[^@\s]+$")

# ブランチ名に使える安全な文字(requestId の正規化に使う)。英数と `-_./` のみ。
_BRANCH_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._/-]")


def is_valid_repo_url(value):
    """repoUrl が許可形式(https://github.com/ 直下・userinfo なし)か判定する。"""
    return bool(isinstance(value, str) and _REPO_URL_PATTERN.match(value))


def repo_url_specified(payload):
    """payload に非空の repoUrl 文字列が入っているかを返す(値の妥当性は問わない)。

    「未指定(= 従来モード)」と「指定ありだが不正(= fail-closed)」を呼び出し側で
    区別するための判定。resolve_repo_url は両者とも None を返すため、指定の有無は
    こちらで判定する。
    """
    value = payload.get("repoUrl") if isinstance(payload, dict) else None
    return bool(isinstance(value, str) and value.strip())


def resolve_repo_url(payload):
    """payload["repoUrl"] を取り出す。未指定/空/非文字列/不正形式なら None(= clone なし)。

    許可形式は https://github.com/ 直下のパスのみ。`@`(userinfo)を含む URL や
    別ホスト・別スキームは拒否し、clone 先を固定して資格情報混入 URL を弾く。
    不正形式の拒否と未指定の区別が要るときは repo_url_specified を併用する。
    """
    value = payload.get("repoUrl") if isinstance(payload, dict) else None
    if not (isinstance(value, str) and value.strip()):
        return None
    candidate = value.strip()
    if not is_valid_repo_url(candidate):
        log("repoUrl rejected: not an https://github.com/ URL without userinfo")
        return None
    return candidate


def sanitize_branch_component(value):
    """ブランチ名に埋め込む文字列(requestId)を安全な文字だけに正規化する。

    英数と `-_./` 以外を `-` に置換する。空になるときは "run" にフォールバック。
    `git checkout -b kiro/<component>` のインジェクション・不正ブランチ名を防ぐ。
    """
    if not isinstance(value, str):
        value = str(value)
    cleaned = _BRANCH_SAFE_PATTERN.sub("-", value).strip("-/")
    return cleaned or "run"


def resolve_base_branch(payload):
    """payload["baseBranch"] を取り出す。未指定/空/非文字列なら既定 "main"。

    値は repoUrl/requestId と一貫して安全な文字(英数と `-_./`)に正規化する。
    呼び出し元は信頼する前提だが、プロンプトへ補間する値の入力検証を低コストで
    揃える(危険文字は `-` に置換。正規化後に空なら既定 "main")。
    """
    value = payload.get("baseBranch") if isinstance(payload, dict) else None
    if not (isinstance(value, str) and value.strip()):
        return DEFAULT_BASE_BRANCH
    # 安全文字(英数と `-_./`)以外を `-` に置換する。正規化で中身が無くなる
    # (記号のみ等)なら既定 "main" に倒す。sanitize_branch_component の "run"
    # フォールバックは requestId 用のため、baseBranch では自前で "main" を既定にする。
    cleaned = _BRANCH_SAFE_PATTERN.sub("-", value.strip()).strip("-/")
    return cleaned or DEFAULT_BASE_BRANCH


# --------------------------------------------------------------------------
# git 認証(方式A: PAT を Kiro に見せない。GIT_ASKPASS 経由で透過)
# --------------------------------------------------------------------------


def render_askpass_script(secret_arn):
    """GIT_ASKPASS 用スクリプト本文を返す。

    git が資格情報を要求したとき、このスクリプトが実行時に
    `aws secretsmanager get-secret-value` で PAT を取得して stdout に出す。
    PAT 実値はスクリプト本文にもログにも焼かない(ARN のみ埋め込む)。
    git は 1 引数目に "Username" / "Password" を含む prompt を渡す。GitHub の
    HTTPS 認証は username 任意・password に PAT で通るため、username には固定の
    プレースホルダを返し、password 要求時に PAT を返す。
    """
    return (
        "#!/bin/sh\n"
        "# Kiro には PAT を見せない。git の資格情報要求時のみ実行時取得する。\n"
        'case "$1" in\n'
        "  *[Uu]sername*) printf '%s' 'kiro-bot' ;;\n"
        "  *)\n"
        "    aws secretsmanager get-secret-value \\\n"
        f'      --secret-id "{secret_arn}" \\\n'
        "      --query SecretString --output text ;;\n"
        "esac\n"
    )


def build_git_env(home_dir):
    """Kiro 子プロセスに重ねる git 認証用 env 差分を返す。

    - HOME: 専用 HOME(askpass helper と git 設定の置き場)。
    - GIT_ASKPASS: helper スクリプトの絶対パス。
    - GIT_TERMINAL_PROMPT=0: 端末での対話的資格情報入力を禁止(無人実行の保険)。
    - GIT_AUTHOR_* / GIT_COMMITTER_*: commit author/committer を kiro-bot に固定。
      プロンプトの `git config` 指示に加えて env でも確定させ、Kiro が config を
      省いても個人を識別しない bot 表記で commit されるようにする。
    """
    return {
        "HOME": home_dir,
        "GIT_ASKPASS": os.path.join(home_dir, GIT_ASKPASS_FILENAME),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }


def setup_git_credentials(home_dir, secret_arn):
    """home_dir に GIT_ASKPASS helper を 0700 で設置する。

    PAT 実値はスクリプトに焼かない(render_askpass_script が ARN のみ埋め込む)。
    戻り値: helper スクリプトの絶対パス。
    """
    os.makedirs(home_dir, exist_ok=True)
    script_path = os.path.join(home_dir, GIT_ASKPASS_FILENAME)
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(render_askpass_script(secret_arn))
    os.chmod(script_path, 0o700)
    return script_path


def cleanup_git_credentials(home_dir):
    """setup_git_credentials が置いた helper を削除する。

    実行後に資格情報取得経路の痕跡を残さない。存在しなくても落ちない。
    """
    script_path = os.path.join(home_dir, GIT_ASKPASS_FILENAME)
    try:
        os.remove(script_path)
    except OSError:
        pass


def read_global_steering_files(steering_dir=GLOBAL_STEERING_DIR):
    """steering_dir 直下(非再帰)の *.md を名前昇順で読み、[(filename, text), ...] を返す。

    ディレクトリ不在・読取不能・個別ファイルの読取失敗は、そのファイルを飛ばして
    処理を続ける(フォールバックが無くても既存挙動を壊さない)。戻りは basename と
    その内容(str)のタプルのリスト。
    """
    result = []
    try:
        names = os.listdir(steering_dir)
    except OSError:
        return result
    for name in sorted(names):
        if not name.endswith(".md"):
            continue
        full = os.path.join(steering_dir, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        result.append((name, text))
    return result


def build_steering_fallback_instructions(steering_files):
    """GitHub フロー用プロンプトに差し込む steering 配置手順を返す。

    clone 後、リポジトリ直下の .kiro/steering/<name>.md が無ければ、イメージ同梱の
    /root/.kiro/steering/<name>.md をコピーして配置し、その内容を遵守させる。これらの
    steering ファイルは対象リポジトリの履歴を汚さないよう commit/push に含めない。
    手順5の `git add -A` でも拾わせないため、配置した各ファイルのパスを
    .git/info/exclude に追記させる。steering_files が空なら空文字列を返す(従来プロンプト
    と同一の文面を保つ)。
    """
    files = list(steering_files or ())
    if not files:
        return ""
    lines = []
    for name, _text in files:
        src = os.path.join(GLOBAL_STEERING_DIR, name)
        dst = os.path.join(GLOBAL_STEERING_WORKSPACE_SUBDIR, name)
        lines.append(
            f"     - `{dst}` が無ければ `{src}` をコピーして配置し、"
            f"`echo '{dst}' >> .git/info/exclude` でこのパスを除外対象に追記する。"
        )
    placements = "\n".join(lines)
    return (
        "   clone したリポジトリ直下に共通注意事項の steering を配置し、その内容を遵守する。"
        f"まず `mkdir -p {GLOBAL_STEERING_WORKSPACE_SUBDIR}` でディレクトリを用意し、"
        "各ファイルを配置する:\n"
        f"{placements}\n"
        f"     これら `{GLOBAL_STEERING_WORKSPACE_SUBDIR}/` の steering ファイルは "
        "`.git/info/exclude` に追記済みのため、手順5の `git add -A` でも拾われず "
        "commit/push に含まれない。\n"
    )


def build_repo_prompt(repo_url, base_branch, request_id, user_prompt):
    """Kiro に渡す実行プロンプトを組み立てる。

    repo_url が None のときは user_prompt をそのまま返す(従来挙動 = clone なし)。
    repo_url が有るときは、clone→ブランチ作成→作業→commit→push→作業報告までを
    Kiro 自身に実行させる手順を日本語で明示したプロンプトを返す。
    認証は GIT_ASKPASS 経由で透過するため、PAT も認証情報も URL もプロンプトに書かない。
    """
    if repo_url is None:
        return user_prompt
    branch = f"kiro/{sanitize_branch_component(request_id)}"
    # base_branch は resolve_base_branch で正規化済みの想定だが、直接呼ばれても
    # 危険文字を補間しないよう、ここでも安全文字に正規化する(多重防御)。
    base_branch = _BRANCH_SAFE_PATTERN.sub("-", str(base_branch)).strip("-/") \
        or DEFAULT_BASE_BRANCH
    # clone 後・作業前に、イメージ同梱のグローバル steering を clone 先へ配置する手順。
    # steering が無ければ空文字列になり、従来のプロンプト文面と一致する。
    steering_step = build_steering_fallback_instructions(read_global_steering_files())
    return (
        "あなたは Git リポジトリ上で作業する開発エージェントです。"
        "以下の手順を順番に実行してください。\n"
        "\n"
        "## 前提\n"
        f"- 対象リポジトリ: {repo_url}\n"
        f"- ベースブランチ: {base_branch}\n"
        f"- 作業ブランチ: {branch}\n"
        "- Git 認証は GIT_ASKPASS 経由で自動的に通ります。"
        "認証情報(トークン等)を自分で入力・表示・URL への埋め込みをしてはいけません。\n"
        "\n"
        "## 手順\n"
        f"1. カレントディレクトリに `git clone {repo_url}` でリポジトリを clone し、"
        "clone したディレクトリに移動する。\n"
        f"{steering_step}"
        f"2. `git config user.name \"{GIT_AUTHOR_NAME}\"` と "
        f"`git config user.email \"{GIT_AUTHOR_EMAIL}\"` を設定する(commit author を固定する)。\n"
        f"3. `git checkout {base_branch}` の後、`git checkout -b {branch}` で作業ブランチを作成する。\n"
        f"4. 次の作業指示を実施する:\n"
        f"   {user_prompt}\n"
        "5. 変更を `git add -A` してから commit する。\n"
        f"6. `git push --set-upstream origin {branch}` で push する"
        "(認証は自動。URL にトークンを付けない)。\n"
        "7. 作業報告を 2 ファイル、clone したディレクトリの**親ディレクトリ**"
        "(作業開始時のカレントディレクトリ)に出力する:\n"
        "   - `report.json`(機械可読): キー `summary`(文字列), `changes`(配列), "
        "`limitations`(配列), `branch`(文字列), `pushedCommit`(push したコミットの SHA) を含む JSON。\n"
        "   - `report.md`(PR 本文に貼れる Markdown): 「何をしたか」「変更点」「制限事項」"
        "の 3 節で構成する。\n"
        "   どちらも日本語で書く。`branch` は "
        f"`{branch}`、`pushedCommit` は push 済みコミットの SHA を正確に記載する。\n"
    )


# --------------------------------------------------------------------------
# AWS / kiro-cli 連携
# --------------------------------------------------------------------------


def _fetch_secret(secret_arn):
    """Secrets Manager から Secret 値を取得する汎用関数(KIRO_API_KEY / GitHub PAT 共用)。

    戻り値: (value, error)。成功時は (値, None)、失敗時は (None, 値を伏せた理由)。
    例外メッセージも素で返さず、値が混ざりうる箇所は出さない。
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
        return None, "secret fetch failed to launch"
    if proc.returncode != 0:
        return None, f"secret fetch exited {proc.returncode}"
    value = proc.stdout.decode("utf-8", "replace").strip()
    if not value:
        return None, "secret is empty"
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


def run_kiro_and_upload(prefix, request_id, prompt, repo_url=None,
                        base_branch=DEFAULT_BASE_BRANCH,
                        repo_url_rejected=False):
    """Kiro CLI を同期実行し、stdout・成果物・status.json を S3 に put する。

    戻り値: (ok, status_dict)。ok は
    (exitCode==0 and not timedOut and stdout・成果物の put 失敗なし
     and status.json 自体の put 成功)。設計 §5 どおり S3 put 非ゼロは致命。
    失敗時も status.json は必ず put を試みる。KIRO_API_KEY の値も GitHub PAT も
    ログ・返り値・status・成果物に一切出さない。

    repo_url が指定されたとき(GitHub フロー):
      - GIT_ASKPASS helper を専用 HOME に設置し、Kiro 子プロセス env に git 認証を
        重ねる。clone→ブランチ作成→作業→commit→push→report 生成は Kiro 自身が行う。
      - helper は try/finally で必ず撤去する。PAT 実値はマスク対象に追加する。
      - report.json / report.md は workdir 直下に出る想定で、既存の select_artifacts
        経由に加えて明示的にも put を試みる。

    repo_url_rejected=True は「呼び出し元が repoUrl を指定したが不正形式で
    resolve_repo_url が拒否した」ことを示す。このとき Kiro は実行せず fail-closed
    で終える(ok=false、status に理由)。不正 repoUrl を黙って従来モードに縮退させ
    「push された」と誤認させないため(repoUrl 未指定の従来モードは影響しない)。
    """
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    bucket = os.environ.get("RESULT_BUCKET", "").strip()
    if not bucket:
        # バケットが無いと status も put できない。致命として扱う。
        raise RuntimeError("RESULT_BUCKET is not set")

    stdout_key = f"{prefix}kiro-stdout.txt"
    status_key = f"{prefix}status.json"

    def finalize(secret_values, exit_code, timed_out, artifacts, skipped, failed_uploads):
        """status.json を組み立てて put する。

        戻り値: (status_dict, status_put_ok)。status.json 自体の put 失敗は
        status を残せないため、呼び出し側で必ず /run 500 とする(設計 §5)。
        status["ok"] は exitCode / timedOut / stdout・成果物の put 失敗を集約する。
        secret_values は実キー値(KIRO_API_KEY・PAT)のマスク対象リスト。
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
        body = mask_secrets(body, secret_values)
        status_put_ok, detail = _s3_put_bytes(
            bucket, status_key, body.encode("utf-8")
        )
        if not status_put_ok:
            log(f"status.json put failed: {mask_secrets(detail, secret_values)}")
        return status, status_put_ok

    # ① API キー取得(値はログ/返り値に出さない)
    secret_arn = os.environ.get("KIRO_API_KEY_SECRET_ARN", "").strip()
    if not secret_arn:
        log("KIRO_API_KEY_SECRET_ARN is not set")
        status, _ = finalize([], None, False, [], [], [])
        return False, status

    api_key, err = _fetch_secret(secret_arn)
    if api_key is None:
        # err は値を伏せた理由のみ。
        log(f"api key fetch failed: {err}")
        status, _ = finalize([], None, False, [], [], [])
        return False, status

    # マスク対象の実値リスト(KIRO_API_KEY に加え、GitHub フローでは PAT も載せる)。
    secret_values = [api_key]

    # ①' repoUrl 指定ありで不正形式(resolve_repo_url が拒否)なら fail-closed。
    # 黙って従来モードに縮退させると「clone/push した」と誤認しうるため、Kiro を
    # 実行せず status に理由を残して ok=false で返す(repoUrl 未指定は影響しない)。
    if repo_url_rejected:
        log("repoUrl specified but rejected; aborting git flow (fail-closed)")
        status, _ = finalize(
            secret_values, None, False, [], [], ["repoUrl-rejected"]
        )
        return False, status

    # ② GitHub フロー時は PAT 用 Secret ARN と専用 HOME を用意する(実値は helper 経由)。
    github_token_arn = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "").strip()

    # ③ 作業ディレクトリ作成
    workdir = os.path.join(WORKDIR_BASE, request_id or "default")
    os.makedirs(workdir, exist_ok=True)

    # ④ kiro-cli 実行(API キーは子プロセス env にのみ渡す)
    env = dict(os.environ)
    env["KIRO_API_KEY"] = api_key

    # GitHub フロー: PAT 取得と認証 helper の設置。設置失敗でも status.json を必ず
    # put するため、credential 準備以降は try/finally の内側で行う。helper は成否に
    # 関わらず finally で撤去する。
    git_home = None
    run_prompt = prompt
    if repo_url is not None:
        if not github_token_arn:
            # repoUrl 指定ありで Secret ARN 未設定は push 不能。黙って走らせず
            # fail-closed にする(repoUrl 無しの従来モードには影響しない)。
            log("GITHUB_TOKEN_SECRET_ARN is not set but repoUrl given; "
                "aborting git flow (fail-closed)")
            status, _ = finalize(
                secret_values, None, False, [], [], ["github-token-arn-missing"]
            )
            return False, status
        # マスク境界に PAT 実値を載せ、同時に取得可否を fail-closed 判定に使う。
        # ここで取得できない(= push もできず PAT を制御できない)なら、git フローは
        # 走らせず failedUploads に記録して ok=false で返す。実値を制御下に置けない
        # まま Kiro を走らせない(非漏洩境界を担保できないため)。
        pat, pat_err = _fetch_secret(github_token_arn)
        if not pat:
            log(f"github token fetch failed; aborting git flow: {pat_err}")
            status, _ = finalize(
                secret_values, None, False, [], [],
                ["github-token-fetch"],
            )
            return False, status
        secret_values.append(pat)

    # GitHub フロー: 認証 helper の設置とプロンプト合成。設置に失敗しても status.json
    # を必ず put して返す(準備失敗でホストが status を得られず待ち続ける事態を防ぐ)。
    # 準備段階で helper を置いた後に失敗した場合もここで撤去する。
    if repo_url is not None:
        git_home = os.path.join(workdir, ".kiro-git-home")
        try:
            setup_git_credentials(git_home, github_token_arn)
            env.update(build_git_env(git_home))
            run_prompt = build_repo_prompt(repo_url, base_branch, request_id, prompt)
        except Exception as exc:  # noqa: BLE001
            log("git credential setup failed; aborting git flow: "
                f"{mask_secrets(str(exc), secret_values)}")
            cleanup_git_credentials(git_home)
            status, _ = finalize(
                secret_values, None, False, [], [], ["git-credential-setup"]
            )
            return False, status

    exit_code = None
    timed_out = False
    stdout_bytes = b""
    try:
        try:
            proc = subprocess.run(
                [
                    "kiro-cli",
                    "chat",
                    "--trust-all-tools",
                    "--no-interactive",
                    run_prompt,
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
            log(f"kiro-cli failed to launch: {mask_secrets(str(exc), secret_values)}")
    finally:
        # 認証 helper は成否に関わらず必ず撤去する(PAT 取得経路の痕跡を残さない)。
        if git_home is not None:
            cleanup_git_credentials(git_home)

    # put 失敗(aws s3 cp 非ゼロ)は設計 §5 で致命。対象名を集約して ok=false にする。
    # ※キー混入でのスキップ(skipped)は意図的な FR-5 防御で put 失敗ではない。
    failed_uploads = []

    # ⑤ stdout をマスクして put
    stdout_text = stdout_bytes.decode("utf-8", "replace")
    stdout_masked = mask_secrets(stdout_text, secret_values)
    put_ok, detail = _s3_put_bytes(
        bucket, stdout_key, stdout_masked.encode("utf-8")
    )
    if not put_ok:
        log(f"stdout put failed: {mask_secrets(detail, secret_values)}")
        failed_uploads.append("kiro-stdout.txt")

    # ⑥ 成果物を選別し、キー混入チェックに合格したものだけ put
    artifacts = []
    skipped = []
    redacted_name_count = 0
    for name in select_artifacts(workdir):
        full = os.path.join(workdir, name)
        # 名前にキーが埋め込まれている場合、元名は S3 key・status・ログ・応答の
        # どこにも出さない。put もせず、固定の安全表記で skipped に記録する。
        if name_contains_secret(name, secret_values):
            redacted_name_count += 1
            safe_name = f"<redacted-filename-{redacted_name_count}>"
            skipped.append(safe_name)
            log(f"artifact skipped (secret in filename): {safe_name}")
            continue
        if contains_secret(full, secret_values):
            skipped.append(name)
            log(f"artifact skipped (secret detected): {name}")
            continue
        put_ok, detail = _s3_put_file(bucket, f"{prefix}{name}", full)
        if put_ok:
            artifacts.append(name)
        else:
            # put 失敗は致命。ファイル名のみ記録(キー値は含めない)。
            failed_uploads.append(name)
            log(f"artifact put failed: {name}: {mask_secrets(detail, secret_values)}")

    # ⑥' GitHub フロー: report.json / report.md は必須成果物として put を保証する。
    # 既存の select_artifacts 経由(workdir 直下)に加え、生成・put の有無を status に
    # 反映する(置き換えでなく追加)。report は「S3 に無ければ失敗」とする必須扱いの
    # ため、未生成・秘密検知での skip・put 失敗のいずれも failed_uploads に記録して
    # ok=false にする。既に artifacts に入っていれば(select_artifacts 経由で put 済み)
    # 二重 put しない。
    if repo_url is not None:
        for report_name in ("report.json", "report.md"):
            if report_name in artifacts:
                # select_artifacts 経由で put 済み。成功扱い。
                continue
            if report_name in skipped:
                # select_artifacts 側で秘密検知により skip 済み = S3 に無い。
                # report は必須のため failed_uploads にも記録し ok=false にする。
                failed_uploads.append(report_name)
                log(f"report skipped (secret detected), required -> failed: "
                    f"{report_name}")
                continue
            report_path = os.path.join(workdir, report_name)
            if not os.path.isfile(report_path):
                # Kiro が生成しなかった。欠落が分かるよう failed_uploads に記録。
                failed_uploads.append(report_name)
                log(f"report missing: {report_name}")
                continue
            if contains_secret(report_path, secret_values):
                # 秘密検知で put させない。report は必須のため failed_uploads にも
                # 記録して ok=false にする(S3 に無いまま成功扱いにしない)。
                skipped.append(report_name)
                failed_uploads.append(report_name)
                log(f"report skipped (secret detected), required -> failed: "
                    f"{report_name}")
                continue
            put_ok, detail = _s3_put_file(bucket, f"{prefix}{report_name}", report_path)
            if put_ok:
                artifacts.append(report_name)
            else:
                failed_uploads.append(report_name)
                log(f"report put failed: {report_name}: "
                    f"{mask_secrets(detail, secret_values)}")

    # ⑦ status.json を生成・put
    status, status_put_ok = finalize(
        secret_values, exit_code, timed_out, artifacts, skipped, failed_uploads
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
            repo_url = resolve_repo_url(payload)
            # repoUrl 指定ありで resolve_repo_url が拒否(不正形式)したら fail-closed。
            # 「未指定で従来モード」と「指定ありだが不正」を区別して誤認を防ぐ。
            repo_url_rejected = repo_url is None and repo_url_specified(payload)
            base_branch = resolve_base_branch(payload)
            # prefix 未指定なら microvmId でフォールバックし末尾スラッシュ正規化。
            if not prefix and microvm_id:
                prefix = f"runs/{microvm_id}/"
            if prefix and not prefix.endswith("/"):
                prefix += "/"
            # repoUrl は PAT を含まないがマスクを通して一貫させる。
            log(
                f"run hook -> microvmId={microvm_id!r} "
                f"prefix={prefix!r} requestId={request_id!r} "
                f"repoUrl={mask_secret(repo_url, None)!r} baseBranch={base_branch!r}"
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
                            microvm_id=microvm_id, prompt=prompt,
                            repo_url=repo_url, base_branch=base_branch,
                            repo_url_rejected=repo_url_rejected):
                # バックグラウンド実行の戻り(ok/status)で HTTP ステータスは決めない。
                # 成否は run_kiro_and_upload が status.json に書く。例外は握りつぶさず
                # ログに出す(キー値は env にしか無いが念のためパターンマスクを通す)。
                try:
                    run_kiro_and_upload(
                        prefix, request_id or microvm_id, prompt,
                        repo_url=repo_url, base_branch=base_branch,
                        repo_url_rejected=repo_url_rejected,
                    )
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
