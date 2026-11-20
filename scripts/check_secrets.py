#!/usr/bin/env python3
"""ブランチ差分の追加行に秘密情報らしき文字列が含まれていないか検査する。

使い方: python3 scripts/check_secrets.py [BASE_REF]   (既定: origin/main、無ければ main)
終了コード: 0=検出なし / 1=検出あり / 2=実行エラー
steering(packages/image/steering/00-global.md)の「秘密情報を書かない」を機械強制する。
"""
import re
import subprocess
import sys

PATTERNS = {
    "AWS access key ID": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    "Kiro API key": re.compile(r"\bksk_[A-Za-z0-9_\-]{8,}"),
    "Private key block": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "Generic secret assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"'][A-Za-z0-9/+_\-]{20,}[\"']"
    ),
}


def scan_diff(diff_text):
    """unified diff の追加行を検査し、(ファイル, 行番号, 種別) のリストを返す。"""
    findings = []
    path = None
    lineno = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) - 1 if m else 0
        elif line.startswith("+"):
            lineno += 1
            for name, pat in PATTERNS.items():
                if pat.search(line[1:]):
                    findings.append((path, lineno, name))
        elif not line.startswith("-"):
            lineno += 1
    return findings


def main(argv):
    base = argv[1] if len(argv) > 1 else None
    refs = [base] if base else ["origin/main", "main"]
    diff = None
    for ref in refs:
        r = subprocess.run(
            ["git", "diff", "--unified=0", f"{ref}...HEAD"], capture_output=True, text=True
        )
        if r.returncode == 0:
            diff = r.stdout
            break
    if diff is None:
        print(f"error: base ref not found: {refs}", file=sys.stderr)
        return 2
    findings = scan_diff(diff)
    for path, lineno, name in findings:
        print(f"{path}:{lineno}: {name}")
    if findings:
        print(f"NG: {len(findings)} 件の秘密情報らしき文字列を検出", file=sys.stderr)
        return 1
    print("OK: 秘密情報らしき文字列なし")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
