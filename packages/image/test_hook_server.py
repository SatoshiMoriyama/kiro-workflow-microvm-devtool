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
                hs, "_fetch_api_key", return_value=("ksk_live", None)
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


if __name__ == "__main__":
    unittest.main()
