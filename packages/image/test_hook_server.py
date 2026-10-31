#!/usr/bin/env python3
"""hook_server.py の純関数ユニットテスト(pytest / unittest どちらでも動く)。"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_server as hs  # noqa: E402


class TestMaskSecret(unittest.TestCase):
    def test_exact_value_match(self):
        key = "ksk_AbC123=+/def"
        text = f"token is {key} end"
        masked = hs.mask_secret(text, key)
        self.assertNotIn(key, masked)
        self.assertIn("[REDACTED]", masked)

    def test_pattern_match_without_api_key(self):
        text = "leaked ksk_xyz789 here"
        masked = hs.mask_secret(text, None)
        self.assertNotIn("ksk_xyz789", masked)
        self.assertIn("[REDACTED]", masked)

    def test_no_tail_leak_with_special_chars(self):
        # =,+,/ を含むキーでも末尾を取りこぼさない(\S+ で貪欲)。
        key = "ksk_a=b+c/d"
        masked = hs.mask_secret(f"x {key} y", key)
        self.assertNotIn("=b+c/d", masked)

    def test_non_string_input(self):
        masked = hs.mask_secret(12345, None)
        self.assertEqual(masked, "12345")
        # None 入力でも落ちない
        self.assertEqual(hs.mask_secret(None, None), "None")


class TestSelectArtifacts(unittest.TestCase):
    def test_excludes_hidden_dirs_reserved(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "weather.html"), "w").close()
            open(os.path.join(d, "notes.md"), "w").close()
            open(os.path.join(d, ".hidden"), "w").close()
            open(os.path.join(d, "kiro-stdout.txt"), "w").close()
            open(os.path.join(d, "status.json"), "w").close()
            os.mkdir(os.path.join(d, "subdir"))
            os.mkdir(os.path.join(d, ".kiro"))
            result = hs.select_artifacts(d)
            self.assertEqual(result, ["notes.md", "weather.html"])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(hs.select_artifacts("/no/such/dir/xyz"), [])


class TestBuildStatus(unittest.TestCase):
    def _status(self, exit_code, timed_out, artifacts=None, failed_uploads=None):
        return hs.build_status(
            request_id="req-1",
            prompt="p",
            exit_code=exit_code,
            timed_out=timed_out,
            artifacts=artifacts or [],
            skipped=[],
            failed_uploads=failed_uploads or [],
            stdout_key="runs/req-1/kiro-stdout.txt",
            started_at="s",
            finished_at="f",
        )

    def test_ok_true_on_zero_exit(self):
        self.assertTrue(self._status(0, False)["ok"])

    def test_ok_false_on_nonzero(self):
        self.assertFalse(self._status(1, False)["ok"])

    def test_ok_false_on_none_and_serializable(self):
        st = self._status(None, False)
        self.assertFalse(st["ok"])
        # None を含んでもシリアライズできる
        json.dumps(st)

    def test_ok_false_on_timeout(self):
        self.assertFalse(self._status(None, True)["ok"])

    def test_empty_artifacts_still_ok(self):
        st = self._status(0, False, artifacts=[])
        self.assertTrue(st["ok"])
        self.assertEqual(st["artifacts"], [])

    def test_ok_false_on_failed_uploads(self):
        # exit 0 でも put 失敗があれば ok=false(設計 §5: put 非ゼロは致命)。
        st = self._status(0, False, failed_uploads=["kiro-stdout.txt"])
        self.assertFalse(st["ok"])
        self.assertEqual(st["failedUploads"], ["kiro-stdout.txt"])


class TestParsePayload(unittest.TestCase):
    def _outer(self, inner_dict):
        return json.dumps(
            {"microvmId": "mvm-1", "runHookPayload": json.dumps(inner_dict)}
        )

    def test_prompt_present(self):
        payload, mvm = hs.parse_run_payload(self._outer({"prompt": "hello"}))
        self.assertEqual(mvm, "mvm-1")
        self.assertEqual(hs.resolve_prompt(payload), "hello")

    def test_prompt_missing_defaults(self):
        payload, _ = hs.parse_run_payload(self._outer({"requestId": "r"}))
        self.assertEqual(hs.resolve_prompt(payload), hs.DEFAULT_PROMPT)

    def test_prompt_empty_defaults(self):
        payload, _ = hs.parse_run_payload(self._outer({"prompt": "   "}))
        self.assertEqual(hs.resolve_prompt(payload), hs.DEFAULT_PROMPT)

    def test_prompt_non_string_defaults(self):
        payload, _ = hs.parse_run_payload(self._outer({"prompt": 123}))
        self.assertEqual(hs.resolve_prompt(payload), hs.DEFAULT_PROMPT)

    def test_bytes_input(self):
        payload, mvm = hs.parse_run_payload(
            self._outer({"prompt": "x"}).encode("utf-8")
        )
        self.assertEqual(mvm, "mvm-1")
        self.assertEqual(hs.resolve_prompt(payload), "x")

    def test_invalid_outer(self):
        payload, mvm = hs.parse_run_payload("not json")
        self.assertEqual(payload, {})
        self.assertEqual(mvm, "")


class TestContainsSecret(unittest.TestCase):
    def test_file_with_key_true(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w") as fh:
                fh.write("prefix ksk_secret123 suffix")
            self.assertTrue(hs.contains_secret(p, None))
            self.assertTrue(hs.contains_secret(p, "ksk_secret123"))

    def test_file_without_key_false(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w") as fh:
                fh.write("nothing sensitive here")
            self.assertFalse(hs.contains_secret(p, None))

    def test_unreadable_file_true_fail_closed(self):
        # 読取不能は検査不能 -> 安全側に倒して True(put させない)。
        self.assertTrue(hs.contains_secret("/no/such/file.txt", None))

    def test_invalid_utf8_with_key_true(self):
        # 不正 UTF-8 バイトを含み、かつ実キーのバイト列を含むファイル。
        # デコードに依存せずバイト列で検知し、put をスキップできること。
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "weather.html")
            with open(p, "wb") as fh:
                fh.write(b"<html>\xff\xfe ksk_live123 \x00 end</html>")
            self.assertTrue(hs.contains_secret(p, None))
            self.assertTrue(hs.contains_secret(p, "ksk_live123"))

    def test_invalid_utf8_without_key_false(self):
        # 不正 UTF-8 でもキーを含まなければ put を妨げない。
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "weather.html")
            with open(p, "wb") as fh:
                fh.write(b"<html>\xff\xfe nothing sensitive \x00</html>")
            self.assertFalse(hs.contains_secret(p, None))
            self.assertFalse(hs.contains_secret(p, "ksk_live123"))


class TestNameContainsSecret(unittest.TestCase):
    def test_exact_key_value_in_name_true(self):
        self.assertTrue(hs.name_contains_secret("ksk_live123.html", "ksk_live123"))

    def test_pattern_in_name_true_without_api_key(self):
        # 実キー値が分からなくても ksk_ パターンで検知する。
        self.assertTrue(hs.name_contains_secret("report-ksk_abc.txt", None))

    def test_clean_name_false(self):
        self.assertFalse(hs.name_contains_secret("weather.html", "ksk_live123"))
        self.assertFalse(hs.name_contains_secret("weather.html", None))

    def test_non_string_name(self):
        self.assertFalse(hs.name_contains_secret(12345, None))


class TestRunKiroUploadFailures(unittest.TestCase):
    """S3 put 失敗が ok=false に反映されることを確認する(設計 §5: 致命)。"""

    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        # 成果物 1 件を持つ作業ディレクトリ。
        with open(os.path.join(self._workdir.name, "weather.html"), "w") as fh:
            fh.write("<html>ok</html>")

        patches = [
            unittest.mock.patch.dict(
                os.environ,
                {
                    "RESULT_BUCKET": "bkt",
                    "KIRO_API_KEY_SECRET_ARN": "arn:secret",
                },
                clear=False,
            ),
            unittest.mock.patch.object(
                hs, "_fetch_secret", return_value=("ksk_live", None)
            ),
            unittest.mock.patch.object(
                hs, "WORKDIR_BASE", os.path.dirname(self._workdir.name)
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # request_id をディレクトリ名に合わせ、kiro-cli は即 exit 0 とする。
        self._req = os.path.basename(self._workdir.name)

    def _patch_kiro_exit0(self):
        proc = unittest.mock.Mock()
        proc.returncode = 0
        proc.stdout = b"done"
        return unittest.mock.patch.object(hs.subprocess, "run", return_value=proc)

    def test_stdout_put_failure_sets_ok_false(self):
        with self._patch_kiro_exit0(), unittest.mock.patch.object(
            hs, "_s3_put_bytes"
        ) as put_bytes, unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            # stdout put は失敗、status.json put は成功。
            put_bytes.side_effect = [
                (False, "aws s3 cp exited 1"),  # stdout
                (True, "s3://status"),  # status.json
            ]
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("kiro-stdout.txt", status["failedUploads"])

    def test_artifact_put_failure_sets_ok_false(self):
        with self._patch_kiro_exit0(), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(False, "aws s3 cp exited 1")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("weather.html", status["failedUploads"])
        self.assertNotIn("weather.html", status["artifacts"])

    def test_status_put_failure_returns_not_ok(self):
        # stdout/成果物は成功だが status.json 自体の put が失敗 -> ok=false。
        with self._patch_kiro_exit0(), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_bytes") as put_bytes:
            put_bytes.side_effect = [
                (True, "s3://stdout"),  # stdout
                (False, "aws s3 cp exited 1"),  # status.json
            ]
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        # status["ok"] は put 内容上 true だが、status.json 自体を残せないので ok=false。
        self.assertFalse(ok)

    def test_all_success_ok_true(self):
        with self._patch_kiro_exit0(), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        self.assertTrue(ok)
        self.assertTrue(status["ok"])
        self.assertEqual(status["failedUploads"], [])
        self.assertIn("weather.html", status["artifacts"])

    def test_secret_in_filename_not_uploaded_or_leaked(self):
        # 実キーをファイル名に埋め込んだ成果物を追加する。
        leak_name = "ksk_live.html"
        with open(os.path.join(self._workdir.name, leak_name), "w") as fh:
            fh.write("<html>clean content</html>")
        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with self._patch_kiro_exit0(), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        # 危険名は put されない(呼ばれた key に元名が出ない)。
        for call in put_file.call_args_list:
            key_arg = call.args[1]
            self.assertNotIn("ksk_live", key_arg)
        # artifacts/skipped と status 全体に元名も実キー値も出ない。
        self.assertNotIn(leak_name, status["artifacts"])
        self.assertNotIn(leak_name, status["skippedArtifacts"])
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("ksk_live", serialized)
        # 安全表記で記録される。
        self.assertIn("<redacted-filename-1>", status["skippedArtifacts"])
        # clean な成果物は従来どおり put される。
        self.assertIn("weather.html", status["artifacts"])

    def test_invalid_utf8_artifact_with_key_skipped(self):
        # 不正 UTF-8 バイト + 実キーを含む成果物は put されず skippedArtifacts に入る。
        bad_name = "report.html"
        with open(os.path.join(self._workdir.name, bad_name), "wb") as fh:
            fh.write(b"<html>\xff\xfe ksk_live \x00 end</html>")
        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with self._patch_kiro_exit0(), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        # 危険な成果物は put されない(その key が cp 呼び出しに現れない)。
        for call in put_file.call_args_list:
            self.assertNotIn(bad_name, call.args[1])
        self.assertIn(bad_name, status["skippedArtifacts"])
        self.assertNotIn(bad_name, status["artifacts"])
        # 実キー値はどこにも漏れない。
        self.assertNotIn("ksk_live", json.dumps(status, ensure_ascii=False))
        # clean な成果物は引き続き put される。
        self.assertIn("weather.html", status["artifacts"])


class TestMaskGithubToken(unittest.TestCase):
    """GitHub トークン(新旧)と実 PAT 値が [REDACTED] に置換されること。"""

    def test_new_format_token_pattern(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            tok = f"{prefix}AbCdEf0123456789"
            masked = hs.mask_secret(f"url https://x:{tok}@github.com/a/b", None)
            self.assertNotIn(tok, masked)
            self.assertIn("[REDACTED]", masked)

    def test_old_40hex_token_pattern(self):
        tok = "0123456789abcdef0123456789abcdef01234567"  # 40 桁 hex
        masked = hs.mask_secret(f"token={tok} end", None)
        self.assertNotIn(tok, masked)
        self.assertIn("[REDACTED]", masked)

    def test_real_pat_value_exact_match(self):
        pat = "ghp_realSecretValue123"
        masked = hs.mask_secrets("leak " + pat + " here", [pat])
        self.assertNotIn(pat, masked)

    def test_mask_secrets_multiple_values(self):
        masked = hs.mask_secrets(
            "a=ksk_one b=ghp_two c=plainsecret",
            ["plainsecret"],
        )
        # 実値(plainsecret)もパターン(ksk_/ghp_)も消える。
        self.assertNotIn("plainsecret", masked)
        self.assertNotIn("ksk_one", masked)
        self.assertNotIn("ghp_two", masked)

    def test_contains_secret_detects_github_token(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w") as fh:
                fh.write("remote url has ghp_abcdef123456 inside")
            self.assertTrue(hs.contains_secret(p, None))

    def test_name_contains_github_token(self):
        self.assertTrue(hs.name_contains_secret("ghp_abc.txt", None))


class TestResolveRepoInputs(unittest.TestCase):
    def test_repo_url_present(self):
        self.assertEqual(
            hs.resolve_repo_url({"repoUrl": " https://github.com/a/b.git "}),
            "https://github.com/a/b.git",
        )

    def test_repo_url_missing_none(self):
        self.assertIsNone(hs.resolve_repo_url({}))
        self.assertIsNone(hs.resolve_repo_url({"repoUrl": "   "}))
        self.assertIsNone(hs.resolve_repo_url({"repoUrl": 123}))

    def test_base_branch_default(self):
        self.assertEqual(hs.resolve_base_branch({}), "main")
        self.assertEqual(hs.resolve_base_branch({"baseBranch": "  "}), "main")
        self.assertEqual(hs.resolve_base_branch({"baseBranch": 5}), "main")

    def test_base_branch_present(self):
        self.assertEqual(
            hs.resolve_base_branch({"baseBranch": " develop "}), "develop"
        )


class TestGitCredentialHelper(unittest.TestCase):
    def test_askpass_script_no_pat_value(self):
        arn = "arn:aws:secretsmanager:ap-northeast-1:123:secret:gh-abc"
        script = hs.render_askpass_script(arn)
        # ARN は埋め込むが PAT 実値は本文に現れない(実行時取得する作り)。
        self.assertIn(arn, script)
        self.assertNotIn("ghp_", script)
        self.assertIn("get-secret-value", script)

    def test_build_git_env_fields(self):
        env = hs.build_git_env("/tmp/home-x")
        self.assertEqual(env["HOME"], "/tmp/home-x")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(
            env["GIT_ASKPASS"], os.path.join("/tmp/home-x", hs.GIT_ASKPASS_FILENAME)
        )

    def test_setup_then_cleanup_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            path = hs.setup_git_credentials(home, "arn:secret")
            self.assertTrue(os.path.isfile(path))
            # 実行権限が付く。
            self.assertTrue(os.access(path, os.X_OK))
            hs.cleanup_git_credentials(home)
            self.assertFalse(os.path.exists(path))

    def test_cleanup_missing_no_error(self):
        with tempfile.TemporaryDirectory() as d:
            # 無くても落ちない。
            hs.cleanup_git_credentials(d)


class TestBuildRepoPrompt(unittest.TestCase):
    def test_none_repo_passthrough(self):
        self.assertEqual(
            hs.build_repo_prompt(None, "main", "req-1", "do work"), "do work"
        )

    def test_repo_prompt_structure(self):
        p = hs.build_repo_prompt(
            "https://github.com/a/b.git", "main", "req-9", "作業内容"
        )
        # ブランチ名 kiro/<requestId>、固定 author、report スキーマへの言及。
        self.assertIn("kiro/req-9", p)
        self.assertIn(hs.GIT_AUTHOR_NAME, p)
        self.assertIn(hs.GIT_AUTHOR_EMAIL, p)
        self.assertIn("report.json", p)
        self.assertIn("report.md", p)
        self.assertIn("pushedCommit", p)
        self.assertIn("作業内容", p)
        # URL に認証情報を埋め込まない(@ を含む偽装 URL を書かない)。
        self.assertNotIn("ghp_", p)
        self.assertNotIn("@github.com", p)


class TestRunKiroGithubFlow(unittest.TestCase):
    """repoUrl 指定時のフロー: helper 設置/撤去・report put・PAT 非漏洩。"""

    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        self._req = os.path.basename(self._workdir.name)
        self._pat = "ghp_realPatValue999"

        patches = [
            unittest.mock.patch.dict(
                os.environ,
                {
                    "RESULT_BUCKET": "bkt",
                    "KIRO_API_KEY_SECRET_ARN": "arn:kiro",
                    "GITHUB_TOKEN_SECRET_ARN": "arn:gh",
                },
                clear=False,
            ),
            unittest.mock.patch.object(
                hs, "WORKDIR_BASE", os.path.dirname(self._workdir.name)
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fetch_secret_side(self, arn):
        if arn == "arn:gh":
            return (self._pat, None)
        return ("ksk_live", None)

    def _fake_kiro_run(self, reports):
        """kiro-cli をモックし、呼ばれたら workdir に report を生成する。"""
        def _run(cmd, *args, **kwargs):
            # kiro-cli 実行の検知(第1要素)。
            if cmd and cmd[0] == "kiro-cli":
                wd = kwargs.get("cwd")
                for name, content in reports.items():
                    with open(os.path.join(wd, name), "w", encoding="utf-8") as fh:
                        fh.write(content)
                proc = unittest.mock.Mock()
                proc.returncode = 0
                proc.stdout = b"done"
                return proc
            proc = unittest.mock.Mock()
            proc.returncode = 0
            proc.stdout = b""
            return proc
        return _run

    def test_github_flow_puts_reports_and_hides_pat(self):
        reports = {
            "report.json": json.dumps(
                {
                    "summary": "やったこと",
                    "changes": ["a"],
                    "limitations": [],
                    "branch": f"kiro/{self._req}",
                    "pushedCommit": "abc123",
                }
            ),
            "report.md": "# 作業報告\n内容",
        }
        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch_secret_side
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=self._fake_kiro_run(reports)
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/",
                self._req,
                "作業内容",
                repo_url="https://github.com/a/b.git",
                base_branch="main",
            )
        self.assertTrue(ok)
        # report.json / report.md が成果物として put された。
        self.assertIn("report.json", status["artifacts"])
        self.assertIn("report.md", status["artifacts"])
        # PAT 実値は status のどこにも出ない。
        self.assertNotIn(self._pat, json.dumps(status, ensure_ascii=False))
        # put した key にも PAT が出ない。
        for call in put_file.call_args_list:
            self.assertNotIn(self._pat, call.args[1])
        # helper は実行後に撤去されている。
        helper = os.path.join(
            self._workdir.name, ".kiro-git-home", hs.GIT_ASKPASS_FILENAME
        )
        self.assertFalse(os.path.exists(helper))

    def test_report_with_real_40hex_sha_not_skipped(self):
        # pushedCommit は 40 桁 hex の git SHA。これを「秘密」と誤判定して
        # report を skip しないこと(40hex は skip 判定に使わない設計)。
        sha = "0123456789abcdef0123456789abcdef01234567"
        reports = {
            "report.json": json.dumps(
                {
                    "summary": "s",
                    "changes": [],
                    "limitations": [],
                    "branch": f"kiro/{self._req}",
                    "pushedCommit": sha,
                }
            ),
            "report.md": f"# 報告\ncommit {sha}",
        }
        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch_secret_side
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=self._fake_kiro_run(reports)
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/",
                self._req,
                "作業内容",
                repo_url="https://github.com/a/b.git",
            )
        self.assertIn("report.json", status["artifacts"])
        self.assertIn("report.md", status["artifacts"])
        self.assertNotIn("report.json", status["skippedArtifacts"])

    def test_github_flow_missing_reports_recorded_as_failed(self):
        # Kiro が report を生成しなかった場合、欠落が failedUploads に出る。
        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch_secret_side
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=self._fake_kiro_run({})
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/",
                self._req,
                "作業内容",
                repo_url="https://github.com/a/b.git",
            )
        self.assertIn("report.json", status["failedUploads"])
        self.assertIn("report.md", status["failedUploads"])
        self.assertFalse(ok)

    def test_helper_cleaned_up_even_on_kiro_exception(self):
        def _raise(cmd, *args, **kwargs):
            if cmd and cmd[0] == "kiro-cli":
                raise OSError("boom")
            proc = unittest.mock.Mock()
            proc.returncode = 0
            proc.stdout = b""
            return proc
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch_secret_side
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=_raise
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            hs.run_kiro_and_upload(
                f"runs/{self._req}/",
                self._req,
                "作業内容",
                repo_url="https://github.com/a/b.git",
            )
        helper = os.path.join(
            self._workdir.name, ".kiro-git-home", hs.GIT_ASKPASS_FILENAME
        )
        self.assertFalse(os.path.exists(helper))


class TestReviewFindingRegressions(unittest.TestCase):
    """レビュー差し戻し(finding 1/2/4/5)の回帰テスト。"""

    # --- finding 2: github_pat_ (fine-grained PAT)のマスク ---

    def test_mask_fine_grained_github_pat(self):
        tok = "github_pat_11ABCDEF0_abcdefGHIJ1234567890"
        masked = hs.mask_secret(f"token={tok} end", None)
        self.assertNotIn(tok, masked)
        self.assertIn("[REDACTED]", masked)

    def test_contains_secret_detects_fine_grained_pat(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w") as fh:
                fh.write("remote github_pat_11ABCDEF0_xyz inside")
            self.assertTrue(hs.contains_secret(p, None))

    def test_name_contains_fine_grained_pat(self):
        self.assertTrue(hs.name_contains_secret("github_pat_11ABC_x.txt", None))

    # --- finding 1: skip 検査が PAT 実値(完全一致)を見る ---

    def test_contains_secret_detects_pat_real_value(self):
        # gh[pousr]_/github_pat_ 形式に当たらない PAT 実値でも、実値リストを
        # 渡せば完全一致で検知し put を skip する。
        pat = "tokenWithoutKnownPrefix123456"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w") as fh:
                fh.write(f"leaked {pat} here")
            self.assertTrue(hs.contains_secret(p, [pat]))
            # 実値を渡さなければパターンにも当たらず検知しない。
            self.assertFalse(hs.contains_secret(p, None))

    def test_name_contains_secret_pat_real_value(self):
        pat = "tokenWithoutKnownPrefix123456"
        self.assertTrue(hs.name_contains_secret(f"out-{pat}.txt", [pat]))
        self.assertFalse(hs.name_contains_secret(f"out-{pat}.txt", None))

    def test_contains_secret_multiple_values(self):
        # 複数実値(API キー + PAT)のいずれかに当たれば True。
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w") as fh:
                fh.write("only the second value appearsVALUE2 here")
            self.assertTrue(
                hs.contains_secret(p, ["VALUE1_absent", "appearsVALUE2"])
            )

    def test_name_contains_secret_backward_compat_single_key(self):
        # 既存の単一 api_key(str/None)シグネチャが引き続き動く。
        self.assertTrue(hs.name_contains_secret("ksk_live123.html", "ksk_live123"))
        self.assertFalse(hs.name_contains_secret("weather.html", "ksk_live123"))

    # --- finding 5: repoUrl 検証 / requestId のブランチ名正規化 ---

    def test_repo_url_valid_github_https(self):
        self.assertEqual(
            hs.resolve_repo_url({"repoUrl": "https://github.com/a/b.git"}),
            "https://github.com/a/b.git",
        )

    def test_repo_url_rejects_userinfo(self):
        # user:pass@ を含む偽装 URL は拒否(None)。
        self.assertIsNone(
            hs.resolve_repo_url({"repoUrl": "https://ghp_x@github.com/a/b.git"})
        )

    def test_repo_url_rejects_other_host(self):
        self.assertIsNone(
            hs.resolve_repo_url({"repoUrl": "https://evil.example.com/a/b.git"})
        )

    def test_repo_url_rejects_non_https_scheme(self):
        self.assertIsNone(
            hs.resolve_repo_url({"repoUrl": "git@github.com:a/b.git"})
        )
        self.assertIsNone(
            hs.resolve_repo_url({"repoUrl": "http://github.com/a/b.git"})
        )

    def test_is_valid_repo_url(self):
        self.assertTrue(hs.is_valid_repo_url("https://github.com/a/b"))
        self.assertFalse(hs.is_valid_repo_url("https://x@github.com/a/b"))
        self.assertFalse(hs.is_valid_repo_url(None))

    def test_sanitize_branch_component(self):
        # 危険文字を除去し kiro/<component> に安全に埋め込める。
        self.assertEqual(
            hs.sanitize_branch_component("req-2024/01/01-123"),
            "req-2024/01/01-123",
        )
        self.assertNotIn(" ", hs.sanitize_branch_component("a b c"))
        self.assertNotIn(";", hs.sanitize_branch_component("a;rm -rf"))
        # 空・記号のみは "run" にフォールバック。
        self.assertEqual(hs.sanitize_branch_component("  "), "run")
        self.assertEqual(hs.sanitize_branch_component("@@@"), "run")

    def test_build_repo_prompt_uses_sanitized_branch(self):
        p = hs.build_repo_prompt(
            "https://github.com/a/b.git", "main", "req 1;evil", "作業"
        )
        # 危険文字はブランチ名に出ない。
        self.assertIn("kiro/req-1-evil", p)
        self.assertNotIn("kiro/req 1;evil", p)


class TestGithubTokenFetchFailClosed(unittest.TestCase):
    """finding 4: GITHUB_TOKEN_SECRET_ARN 設定時に PAT 取得失敗なら git フロー非実行。"""

    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        self._req = os.path.basename(self._workdir.name)
        patches = [
            unittest.mock.patch.object(
                hs, "WORKDIR_BASE", os.path.dirname(self._workdir.name)
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_pat_fetch_failure_aborts_git_flow(self):
        def _fetch(arn):
            if arn == "arn:gh":
                return (None, "secret fetch exited 1")
            return ("ksk_live", None)

        kiro_run = unittest.mock.Mock()
        with unittest.mock.patch.dict(
            os.environ,
            {
                "RESULT_BUCKET": "bkt",
                "KIRO_API_KEY_SECRET_ARN": "arn:kiro",
                "GITHUB_TOKEN_SECRET_ARN": "arn:gh",
            },
            clear=False,
        ), unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=_fetch
        ), unittest.mock.patch.object(
            hs.subprocess, "run", kiro_run
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/",
                self._req,
                "作業",
                repo_url="https://github.com/a/b.git",
            )
        # git フローは走らない(kiro-cli を起動しない)。
        self.assertFalse(kiro_run.called)
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("github-token-fetch", status["failedUploads"])
        # helper も設置されない。
        helper = os.path.join(
            self._workdir.name, ".kiro-git-home", hs.GIT_ASKPASS_FILENAME
        )
        self.assertFalse(os.path.exists(helper))

    def test_legacy_mode_unaffected_by_github_arn_absence(self):
        # repoUrl 無し(従来モード)は GITHUB_TOKEN_SECRET_ARN と無関係に実行する。
        with open(os.path.join(self._workdir.name, "weather.html"), "w") as fh:
            fh.write("<html>ok</html>")
        proc = unittest.mock.Mock()
        proc.returncode = 0
        proc.stdout = b"done"
        with unittest.mock.patch.dict(
            os.environ,
            {"RESULT_BUCKET": "bkt", "KIRO_API_KEY_SECRET_ARN": "arn:kiro"},
            clear=False,
        ), unittest.mock.patch.object(
            hs, "_fetch_secret", return_value=("ksk_live", None)
        ), unittest.mock.patch.object(
            hs.subprocess, "run", return_value=proc
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/", self._req, "p"
            )
        self.assertTrue(ok)
        self.assertIn("weather.html", status["artifacts"])


class TestGithubFlowPatValueSkip(unittest.TestCase):
    """finding 1 回帰: gh 形式でない PAT 実値が成果物に混入したら skip される。"""

    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        self._req = os.path.basename(self._workdir.name)
        # 既知プレフィックスに当たらない PAT 実値。
        self._pat = "opaquePatNoKnownPrefix01234567"
        patches = [
            unittest.mock.patch.dict(
                os.environ,
                {
                    "RESULT_BUCKET": "bkt",
                    "KIRO_API_KEY_SECRET_ARN": "arn:kiro",
                    "GITHUB_TOKEN_SECRET_ARN": "arn:gh",
                },
                clear=False,
            ),
            unittest.mock.patch.object(
                hs, "WORKDIR_BASE", os.path.dirname(self._workdir.name)
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, arn):
        if arn == "arn:gh":
            return (self._pat, None)
        return ("ksk_live", None)

    def test_pat_value_in_artifact_skipped_and_not_leaked(self):
        pat = self._pat

        def _run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "kiro-cli":
                wd = kwargs.get("cwd")
                # Kiro が誤って PAT 実値を成果物に書いてしまったケース。
                with open(os.path.join(wd, "leak.txt"), "w") as fh:
                    fh.write(f"accidentally wrote {pat} to a file")
                proc = unittest.mock.Mock()
                proc.returncode = 0
                proc.stdout = b"done"
                return proc
            proc = unittest.mock.Mock()
            proc.returncode = 0
            proc.stdout = b""
            return proc

        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=_run
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"runs/{self._req}/",
                self._req,
                "作業",
                repo_url="https://github.com/a/b.git",
            )
        # PAT 実値を含む成果物は put されず skip される。
        self.assertIn("leak.txt", status["skippedArtifacts"])
        self.assertNotIn("leak.txt", status["artifacts"])
        # put した key・status のどこにも PAT 実値が出ない。
        for call in put_file.call_args_list:
            self.assertNotIn(pat, call.args[1])
        self.assertNotIn(pat, json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
