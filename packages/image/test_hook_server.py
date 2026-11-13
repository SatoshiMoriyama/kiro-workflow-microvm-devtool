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

_AUTHOR_ENV_KEYS = ("KIRO_GIT_AUTHOR_NAME", "KIRO_GIT_AUTHOR_EMAIL")
_module_env_patcher = None


def setUpModule():
    # 手元に KIRO_GIT_AUTHOR_* が設定されていても既定値前提のテストが壊れないよう除去する。
    global _module_env_patcher
    _module_env_patcher = unittest.mock.patch.dict(os.environ)
    _module_env_patcher.start()
    for k in _AUTHOR_ENV_KEYS:
        os.environ.pop(k, None)


def tearDownModule():
    _module_env_patcher.stop()


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
            stdout_key="req-1/runs/kiro-stdout.txt",
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
                (True, "s3://events"),  # kiro-events.jsonl
                (True, "s3://status"),  # status.json
            ]
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "p"
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
                f"{self._req}/runs/", self._req, "p"
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
                (True, "s3://events"),  # kiro-events.jsonl
                (False, "aws s3 cp exited 1"),  # status.json
            ]
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "p"
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
                f"{self._req}/runs/", self._req, "p"
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
                f"{self._req}/runs/", self._req, "p"
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
                f"{self._req}/runs/", self._req, "p"
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


class TestParseKiroStreamJson(unittest.TestCase):
    @staticmethod
    def _usage(credits):
        return json.dumps({"type": "sessionUpdate", "data": {"sessionId": "s", "update": {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "session_usage", "credits": credits}}}}})

    @staticmethod
    def _finished(text):
        return json.dumps({"type": "runFinished", "data": {
            "sessionId": "s", "status": "success", "stopReason": "end_turn",
            "finalText": text, "finalTextTruncated": False}})

    def _credit(self, usage, unit="credit"):
        return {"usage": usage, "unit": unit, "unitPlural": unit + "s"}

    def test_sample_events(self):
        lines = [
            '{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v3"}}',
            json.dumps({"type": "sessionUpdate", "data": {"update": {"_meta": {"kiro": {
                "kind": "turn_completion", "promptTurnSummaries": [{"usage": 0.01}]}}}}}),
            self._usage([self._credit(0.06937422736318409)]),
            self._finished("2"),
        ]
        self.assertEqual(hs.parse_kiro_stream_json("\n".join(lines) + "\n"),
                         ("2", 0.06937422736318409))

    def test_last_session_usage_wins(self):
        text = "\n".join([self._usage([self._credit(0.1)]),
                          self._usage([self._credit(0.25)]), self._finished("x")])
        self.assertEqual(hs.parse_kiro_stream_json(text), ("x", 0.25))

    def test_sums_credit_units_only(self):
        text = self._usage([self._credit(0.1), self._credit(5, "token"), self._credit(2)])
        self.assertEqual(hs.parse_kiro_stream_json(text), (None, 2.1))

    def test_no_credit_element_is_none(self):
        text = self._usage([self._credit(5, "token")])
        self.assertEqual(hs.parse_kiro_stream_json(text), (None, None))

    def test_bool_and_string_usage_ignored(self):
        text = self._usage([self._credit(True), self._credit("0.5")])
        self.assertEqual(hs.parse_kiro_stream_json(text), (None, None))
        text = self._usage([self._credit(True), self._credit(0.5)])
        self.assertEqual(hs.parse_kiro_stream_json(text), (None, 0.5))

    def test_non_finite_usage_ignored(self):
        text = self._usage([self._credit(float("nan")), self._credit(0.5),
                            self._credit(float("inf")), self._credit(float("-inf"))])
        # json.dumps は NaN/Infinity をそのまま出力し、json.loads も受理する
        self.assertEqual(hs.parse_kiro_stream_json(text), (None, 0.5))
        text = self._usage([self._credit(float("nan")), self._credit(float("inf"))])
        self.assertEqual(hs.parse_kiro_stream_json(text), (None, None))

    def test_credits_not_list_is_none(self):
        self.assertEqual(hs.parse_kiro_stream_json(self._usage("x")), (None, None))

    def test_invalid_lines_skipped(self):
        text = "\n".join(["not json", "", "[1,2]", "42", "{broken",
                          self._usage([self._credit(0.3)]), self._finished("ok")])
        self.assertEqual(hs.parse_kiro_stream_json(text), ("ok", 0.3))

    def test_empty_text_and_none(self):
        self.assertEqual(hs.parse_kiro_stream_json(""), (None, None))
        self.assertEqual(hs.parse_kiro_stream_json("2"), (None, None))
        self.assertEqual(hs.parse_kiro_stream_json(None), (None, None))
        self.assertEqual(hs.parse_kiro_stream_json(b"x"), (None, None))


class TestCreditsUsedStatusAndArtifacts(unittest.TestCase):
    def _status(self, **kw):
        return hs.build_status(
            request_id="r", prompt="p", exit_code=0, timed_out=False,
            artifacts=[], skipped=[], failed_uploads=[], stdout_key="k",
            started_at="s", finished_at="f", **kw)

    def test_credits_default_none(self):
        st = self._status()
        self.assertIn("creditsUsed", st)
        self.assertIsNone(st["creditsUsed"])
        self.assertTrue(st["ok"])

    def test_prompt_sha256_matches_unmasked_prompt(self):
        import hashlib
        st = self._status()
        expected = hashlib.sha256(st["prompt"].encode("utf-8")).hexdigest()
        self.assertEqual(st["promptSha256"], expected)

    def test_credits_value_not_rounded(self):
        st = self._status(credits_used=0.06937422736318409)
        self.assertEqual(st["creditsUsed"], 0.06937422736318409)
        json.dumps(st)

    def test_select_artifacts_excludes_events(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "kiro-events.jsonl"), "w").close()
            open(os.path.join(d, "a.md"), "w").close()
            self.assertEqual(hs.select_artifacts(d), ["a.md"])


class TestRunKiroStreamJson(TestRunKiroUploadFailures):
    """stream-json の stdout を解析して kiro-stdout.txt / kiro-events.jsonl / creditsUsed に反映する。"""

    # 継承したテストを再実行しないよう空にする。
    def test_stdout_put_failure_sets_ok_false(self): pass
    def test_artifact_put_failure_sets_ok_false(self): pass
    def test_status_put_failure_returns_not_ok(self): pass
    def test_all_success_ok_true(self): pass
    def test_secret_in_filename_not_uploaded_or_leaked(self): pass

    def _run(self, stdout, put_results=None):
        proc = unittest.mock.Mock()
        proc.returncode = 0
        proc.stdout = stdout
        puts = {}

        def put_bytes(bucket, key, data):
            puts[key] = data
            if put_results and key.rsplit("/", 1)[-1] in put_results:
                return put_results[key.rsplit("/", 1)[-1]]
            return (True, "s3://ok")

        with unittest.mock.patch.object(
            hs.subprocess, "run", return_value=proc
        ) as run, unittest.mock.patch.object(
            hs, "_s3_put_bytes", side_effect=put_bytes
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(f"{self._req}/runs/", self._req, "p")
        return ok, status, puts, run

    def _stream(self, extra_text="2"):
        return "\n".join([
            '{"type":"runStarted","data":{}}',
            TestParseKiroStreamJson._usage([{"usage": 0.5, "unit": "credit"}]),
            TestParseKiroStreamJson._finished(extra_text),
        ]).encode("utf-8")

    def test_stream_json_flow(self):
        raw = self._stream()
        ok, status, puts, run = self._run(raw)
        p = f"{self._req}/runs/"
        self.assertTrue(ok)
        self.assertEqual(puts[p + "kiro-stdout.txt"], b"2")
        self.assertEqual(puts[p + "kiro-events.jsonl"], raw)
        self.assertEqual(status["creditsUsed"], 0.5)
        self.assertEqual(status["stdoutKey"], p + "kiro-stdout.txt")
        argv = run.call_args[0][0]
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")

    def test_timeout_discards_partial_credits(self):
        raw = self._stream("partial")
        exc = hs.subprocess.TimeoutExpired(cmd="kiro-cli", timeout=1, output=raw)
        puts = {}

        def put_bytes(bucket, key, data):
            puts[key] = data
            return (True, "s3://ok")

        with unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=exc
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", side_effect=put_bytes
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(f"{self._req}/runs/", self._req, "p")
        self.assertFalse(ok)
        self.assertTrue(status["timedOut"])
        self.assertIsNone(status["creditsUsed"])
        # finalText の扱いは従来どおり(部分 stdout を解析して使う)
        self.assertEqual(puts[f"{self._req}/runs/" + "kiro-stdout.txt"], b"partial")

    def test_text_stdout_fallback(self):
        ok, status, puts, _ = self._run(b"done")
        p = f"{self._req}/runs/"
        self.assertEqual(puts[p + "kiro-stdout.txt"], b"done")
        self.assertEqual(puts[p + "kiro-events.jsonl"], b"done")
        self.assertIsNone(status["creditsUsed"])
        self.assertTrue(ok)

    def test_events_put_failure(self):
        ok, status, _, _ = self._run(
            self._stream(), {"kiro-events.jsonl": (False, "exit 1")})
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("kiro-events.jsonl", status["failedUploads"])

    def test_events_masked(self):
        raw = self._stream("leak ksk_live end")
        _, _, puts, _ = self._run(raw)
        p = f"{self._req}/runs/"
        self.assertNotIn(b"ksk_live", puts[p + "kiro-events.jsonl"])
        self.assertNotIn(b"ksk_live", puts[p + "kiro-stdout.txt"])


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
                f"{self._req}/runs/",
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
                f"{self._req}/runs/",
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
                f"{self._req}/runs/",
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
                f"{self._req}/runs/",
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
                f"{self._req}/runs/",
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
                f"{self._req}/runs/", self._req, "p"
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
                f"{self._req}/runs/",
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


class TestReviewSecondRound(unittest.TestCase):
    """レビュー差し戻し 2 周目(finding 1 blocking, 2/3/4/7/8)の回帰テスト。"""

    # --- finding 2: baseBranch の安全文字正規化 ---

    def test_base_branch_sanitized(self):
        # 危険文字(スペース・`;`)を含む baseBranch は正規化される。
        self.assertEqual(
            hs.resolve_base_branch({"baseBranch": "feature/x"}), "feature/x"
        )
        self.assertNotIn(" ", hs.resolve_base_branch({"baseBranch": "a b"}))
        self.assertNotIn(";", hs.resolve_base_branch({"baseBranch": "a;rm -rf"}))
        # 記号のみ・空は既定 "main"。
        self.assertEqual(hs.resolve_base_branch({"baseBranch": "@@@"}), "main")
        self.assertEqual(hs.resolve_base_branch({}), "main")

    def test_base_branch_used_in_prompt_is_safe(self):
        p = hs.build_repo_prompt(
            "https://github.com/a/b.git", "a;rm -rf", "req-1", "作業"
        )
        self.assertNotIn("a;rm -rf", p)

    # --- finding 3: repoUrl 指定ありの区別 ---

    def test_repo_url_specified_detects_presence(self):
        # 不正形式でも「指定あり」は True(= fail-closed の対象)。
        self.assertTrue(
            hs.repo_url_specified({"repoUrl": "https://evil.example.com/a/b"})
        )
        self.assertTrue(
            hs.repo_url_specified({"repoUrl": "https://github.com/a/b"})
        )
        # 未指定・空・非文字列は「指定なし」。
        self.assertFalse(hs.repo_url_specified({}))
        self.assertFalse(hs.repo_url_specified({"repoUrl": "  "}))
        self.assertFalse(hs.repo_url_specified({"repoUrl": 123}))

    # --- finding 4: commit author/committer を env で確定 ---

    def test_build_git_env_fixes_commit_identity(self):
        env = hs.build_git_env("/tmp/home-x")
        self.assertEqual(env["GIT_AUTHOR_NAME"], hs.GIT_AUTHOR_NAME)
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], hs.GIT_AUTHOR_EMAIL)
        self.assertEqual(env["GIT_COMMITTER_NAME"], hs.GIT_AUTHOR_NAME)
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], hs.GIT_AUTHOR_EMAIL)


class TestReportRequired(unittest.TestCase):
    """finding 1(blocking): report が S3 に無いときは必ず ok=false になる。"""

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

    def _fetch(self, arn):
        if arn == "arn:gh":
            return (self._pat, None)
        return ("ksk_live", None)

    def _run_writing(self, files):
        """kiro-cli をモックし、呼ばれたら workdir に files を生成する。"""
        def _run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "kiro-cli":
                wd = kwargs.get("cwd")
                for name, content in files.items():
                    mode = "wb" if isinstance(content, bytes) else "w"
                    with open(os.path.join(wd, name), mode) as fh:
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

    def test_report_with_secret_skipped_is_failed_not_ok(self):
        # report.json に実 PAT を混入 -> contains_secret で skip -> S3 に無い。
        # report は必須のため failedUploads に記録し ok=false。
        files = {
            "report.json": json.dumps(
                {
                    "summary": f"leaked {self._pat}",
                    "changes": [],
                    "limitations": [],
                    "branch": f"kiro/{self._req}",
                    "pushedCommit": "abc",
                }
            ),
            "report.md": "# 報告\n内容",
        }
        put_file = unittest.mock.Mock(return_value=(True, "s3://ok"))
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=self._run_writing(files)
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(hs, "_s3_put_file", put_file):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/",
                self._req,
                "作業",
                repo_url="https://github.com/a/b.git",
            )
        # 秘密入り report は put されず(その key が cp 呼び出しに現れない)。
        for call in put_file.call_args_list:
            self.assertNotIn("report.json", call.args[1])
        # report.json は skippedArtifacts かつ failedUploads に入る。
        self.assertIn("report.json", status["skippedArtifacts"])
        self.assertIn("report.json", status["failedUploads"])
        # report が S3 に無いので ok=false。
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        # PAT 実値はどこにも漏れない。
        self.assertNotIn(self._pat, json.dumps(status, ensure_ascii=False))

    def test_report_put_failure_is_failed_not_ok(self):
        # report は生成されるが S3 put が失敗 -> failedUploads, ok=false。
        files = {
            "report.json": json.dumps(
                {
                    "summary": "s",
                    "changes": [],
                    "limitations": [],
                    "branch": f"kiro/{self._req}",
                    "pushedCommit": "abc",
                }
            ),
            "report.md": "# 報告",
        }
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=self._run_writing(files)
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(False, "aws s3 cp exited 1")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/",
                self._req,
                "作業",
                repo_url="https://github.com/a/b.git",
            )
        self.assertIn("report.json", status["failedUploads"])
        self.assertIn("report.md", status["failedUploads"])
        self.assertFalse(ok)


class TestRepoUrlRejectedFailClosed(unittest.TestCase):
    """finding 3: repoUrl 指定ありで不正形式なら Kiro を実行せず ok=false。"""

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

    def test_rejected_repo_url_aborts_without_running_kiro(self):
        kiro_run = unittest.mock.Mock()
        with unittest.mock.patch.dict(
            os.environ,
            {"RESULT_BUCKET": "bkt", "KIRO_API_KEY_SECRET_ARN": "arn:kiro"},
            clear=False,
        ), unittest.mock.patch.object(
            hs, "_fetch_secret", return_value=("ksk_live", None)
        ), unittest.mock.patch.object(
            hs.subprocess, "run", kiro_run
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/",
                self._req,
                "作業",
                repo_url=None,
                repo_url_rejected=True,
            )
        self.assertFalse(kiro_run.called)
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("repoUrl-rejected", status["failedUploads"])


class TestGithubArnMissingFailClosed(unittest.TestCase):
    """finding 8: repoUrl 指定ありで GITHUB_TOKEN_SECRET_ARN 未設定なら ok=false。"""

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

    def test_arn_missing_with_repo_url_aborts(self):
        kiro_run = unittest.mock.Mock()
        # GITHUB_TOKEN_SECRET_ARN を明示的に空にする。
        with unittest.mock.patch.dict(
            os.environ,
            {
                "RESULT_BUCKET": "bkt",
                "KIRO_API_KEY_SECRET_ARN": "arn:kiro",
                "GITHUB_TOKEN_SECRET_ARN": "",
            },
            clear=False,
        ), unittest.mock.patch.object(
            hs, "_fetch_secret", return_value=("ksk_live", None)
        ), unittest.mock.patch.object(
            hs.subprocess, "run", kiro_run
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/",
                self._req,
                "作業",
                repo_url="https://github.com/a/b.git",
            )
        self.assertFalse(kiro_run.called)
        self.assertFalse(ok)
        self.assertIn("github-token-arn-missing", status["failedUploads"])


class TestSetupFailureStillWritesStatus(unittest.TestCase):
    """finding 7: credential 準備が失敗しても status.json を必ず put する。"""

    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        self._req = os.path.basename(self._workdir.name)
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
            return ("ghp_pat", None)
        return ("ksk_live", None)

    def test_status_put_attempted_when_setup_fails(self):
        # credential 準備が例外を投げても、例外は呼び出し側へ伝播させず
        # status.json を put して fail-closed で返す(ホストが status を得られる)。
        put_bytes = unittest.mock.Mock(return_value=(True, "s3://status"))
        with unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch
        ), unittest.mock.patch.object(
            hs, "setup_git_credentials", side_effect=OSError("disk full")
        ), unittest.mock.patch.object(
            hs.subprocess, "run"
        ) as kiro_run, unittest.mock.patch.object(
            hs, "_s3_put_bytes", put_bytes
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/",
                self._req,
                "作業",
                repo_url="https://github.com/a/b.git",
            )
        # 準備失敗でも status.json の put は試みられる。
        self.assertTrue(put_bytes.called)
        # kiro-cli は走らない(準備段階で失敗)。
        self.assertFalse(kiro_run.called)
        # fail-closed: ok=false、理由が failedUploads に残る。
        self.assertFalse(ok)
        self.assertIn("git-credential-setup", status["failedUploads"])


class TestGlobalSteering(unittest.TestCase):
    def test_read_md_only_sorted(self):
        # .md のみを名前昇順で (name, text) で返す。非 md・サブディレクトリは除外。
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "10-x.md"), "w", encoding="utf-8") as fh:
                fh.write("second")
            with open(os.path.join(d, "00-global.md"), "w", encoding="utf-8") as fh:
                fh.write("first")
            open(os.path.join(d, "notes.txt"), "w").close()
            os.mkdir(os.path.join(d, "sub.md"))  # .md 拡張子だがディレクトリ
            result = hs.read_global_steering_files(d)
            self.assertEqual(
                result, [("00-global.md", "first"), ("10-x.md", "second")]
            )

    def test_read_missing_dir_returns_empty(self):
        self.assertEqual(hs.read_global_steering_files("/no/such/steering"), [])

    def test_fallback_instructions_empty_for_no_files(self):
        self.assertEqual(hs.build_steering_fallback_instructions([]), "")
        self.assertEqual(hs.build_steering_fallback_instructions(None), "")

    def test_fallback_instructions_contains_required_elements(self):
        text = hs.build_steering_fallback_instructions(
            [("00-global.md", "本文")]
        )
        self.assertIn(".kiro/steering/", text)
        self.assertIn("00-global.md", text)
        self.assertIn(hs.GLOBAL_STEERING_DIR, text)
        self.assertIn("git add", text)
        # mkdir と .git/info/exclude 追記で、git add -A から確実に除外する。
        self.assertIn("mkdir -p .kiro/steering", text)
        self.assertIn(".git/info/exclude", text)
        self.assertIn("echo '.kiro/steering/00-global.md' >> .git/info/exclude", text)
        self.assertIn("commit/push に含まれない", text)

    def test_none_repo_passthrough_unchanged_with_steering(self):
        # steering が焼かれていても repoUrl=None は user_prompt を素通し。
        with unittest.mock.patch.object(
            hs, "read_global_steering_files",
            return_value=[("00-global.md", "本文")],
        ):
            self.assertEqual(
                hs.build_repo_prompt(None, "main", "req-1", "do work"), "do work"
            )

    def test_repo_prompt_injects_steering_before_work(self):
        # repoUrl 指定時、clone 後・作業前に steering 配置手順と add 除外文言が入り、
        # 既存の必須要素も保持される。
        with unittest.mock.patch.object(
            hs, "read_global_steering_files",
            return_value=[("00-global.md", "本文")],
        ):
            p = hs.build_repo_prompt(
                "https://github.com/a/b.git", "main", "req-9", "作業内容"
            )
        self.assertIn(".kiro/steering/00-global.md", p)
        self.assertIn("git add", p)
        self.assertIn("mkdir -p .kiro/steering", p)
        self.assertIn(
            "echo '.kiro/steering/00-global.md' >> .git/info/exclude", p
        )
        self.assertIn("commit/push に含まれない", p)
        # clone(手順1)の後、config(手順2)の前に差し込まれる。
        self.assertLess(p.index("git clone"), p.index(".kiro/steering/00-global.md"))
        self.assertLess(
            p.index(".kiro/steering/00-global.md"),
            p.index(f'git config user.name "{hs.GIT_AUTHOR_NAME}"'),
        )
        # 既存の必須要素は保持。
        self.assertIn("kiro/req-9", p)
        self.assertIn("git push", p)
        self.assertIn("report.json", p)
        self.assertIn("report.md", p)

    def test_repo_prompt_no_steering_matches_legacy(self):
        # steering が無いときは差し込みが空で、従来の必須要素だけ含む。
        with unittest.mock.patch.object(
            hs, "read_global_steering_files", return_value=[]
        ):
            p = hs.build_repo_prompt(
                "https://github.com/a/b.git", "main", "req-9", "作業内容"
            )
        self.assertNotIn(".kiro/steering/", p)
        self.assertIn("kiro/req-9", p)
        self.assertIn("report.json", p)


class TestResolvePromptKey(unittest.TestCase):
    """promptKey の取り出しと形式検証(fail-closed)。"""

    def test_resolve_prompt_key_present(self):
        self.assertEqual(
            hs.resolve_prompt_key({"promptKey": " req-1/inputs/prompt.md "}),
            "req-1/inputs/prompt.md",
        )

    def test_resolve_prompt_key_missing_none(self):
        self.assertIsNone(hs.resolve_prompt_key({}))
        self.assertIsNone(hs.resolve_prompt_key({"promptKey": "   "}))
        self.assertIsNone(hs.resolve_prompt_key({"promptKey": 123}))
        self.assertIsNone(hs.resolve_prompt_key("not a dict"))

    def test_valid_prompt_key_accepts_inputs_prefix(self):
        self.assertTrue(
            hs.is_valid_prompt_key("req-1/inputs/prompt.md", "req-1")
        )

    def test_invalid_prompt_key_wrong_prefix(self):
        # <requestId>/inputs/ で始まらない(runs/ 配下・別 requestId)。
        self.assertFalse(
            hs.is_valid_prompt_key("req-1/runs/prompt.md", "req-1")
        )
        self.assertFalse(
            hs.is_valid_prompt_key("other/inputs/prompt.md", "req-1")
        )

    def test_invalid_prompt_key_traversal(self):
        self.assertFalse(
            hs.is_valid_prompt_key("req-1/inputs/../../etc/passwd", "req-1")
        )

    def test_invalid_prompt_key_leading_slash(self):
        self.assertFalse(
            hs.is_valid_prompt_key("/req-1/inputs/prompt.md", "req-1")
        )

    def test_invalid_prompt_key_empty_or_non_string(self):
        self.assertFalse(hs.is_valid_prompt_key("", "req-1"))
        self.assertFalse(hs.is_valid_prompt_key(None, "req-1"))

    def test_invalid_prompt_key_empty_request_id(self):
        # requestId が空だと接頭辞を確定できない -> 安全側で拒否。
        self.assertFalse(
            hs.is_valid_prompt_key("req-1/inputs/prompt.md", "")
        )


class TestS3GetBytes(unittest.TestCase):
    """_s3_get_bytes が aws s3 cp <uri> - を使い、成否を返すこと。"""

    def test_get_ok_returns_bytes(self):
        proc = unittest.mock.Mock()
        proc.returncode = 0
        proc.stdout = "これはプロンプト本文です".encode("utf-8")
        with unittest.mock.patch.object(
            hs.subprocess, "run", return_value=proc
        ) as run:
            ok, data = hs._s3_get_bytes("bkt", "req-1/inputs/prompt.md")
        self.assertTrue(ok)
        self.assertEqual(data.decode("utf-8"), "これはプロンプト本文です")
        # aws s3 cp <s3uri> - の形で呼ばれる。
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["aws", "s3", "cp"])
        self.assertEqual(args[3], "s3://bkt/req-1/inputs/prompt.md")
        self.assertEqual(args[4], "-")

    def test_get_nonzero_returns_error(self):
        proc = unittest.mock.Mock()
        proc.returncode = 1
        proc.stdout = b""
        proc.stderr = b"NoSuchKey"
        with unittest.mock.patch.object(hs.subprocess, "run", return_value=proc):
            ok, err = hs._s3_get_bytes("bkt", "req-1/inputs/prompt.md")
        self.assertFalse(ok)
        self.assertIn("exited 1", err)

    def test_get_launch_failure_returns_error(self):
        with unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=OSError("boom")
        ):
            ok, err = hs._s3_get_bytes("bkt", "req-1/inputs/prompt.md")
        self.assertFalse(ok)
        self.assertIn("failed to launch", err)


class TestRunKiroPromptKey(unittest.TestCase):
    """run_kiro_and_upload の promptKey 取得フロー(取得成功/拒否/取得失敗/後方互換)。"""

    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        with open(os.path.join(self._workdir.name, "weather.html"), "w") as fh:
            fh.write("<html>ok</html>")
        patches = [
            unittest.mock.patch.dict(
                os.environ,
                {"RESULT_BUCKET": "bkt", "KIRO_API_KEY_SECRET_ARN": "arn:secret"},
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
        self._req = os.path.basename(self._workdir.name)

    def _patch_kiro_capturing_prompt(self):
        """kiro-cli をモックし、渡された prompt 引数を記録する。"""
        captured = {}

        def _run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "kiro-cli":
                captured["prompt"] = cmd[-1]
            proc = unittest.mock.Mock()
            proc.returncode = 0
            proc.stdout = b"done"
            return proc

        return captured, _run

    def test_prompt_key_fetch_ok_used_as_prompt(self):
        body = "S3 から取得した日本語プロンプト".encode("utf-8")
        key = f"{self._req}/inputs/prompt.md"
        captured, run = self._patch_kiro_capturing_prompt()
        with unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=run
        ), unittest.mock.patch.object(
            hs, "_s3_get_bytes", return_value=(True, body)
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "payload-prompt-ignored",
                prompt_key=key,
            )
        self.assertTrue(ok)
        # S3 本文がプロンプトとして使われ、payload 側の prompt は使われない。
        self.assertEqual(captured["prompt"], "S3 から取得した日本語プロンプト")
        # status.prompt にも取得本文が入る。
        self.assertEqual(status["prompt"], "S3 から取得した日本語プロンプト")

    def test_prompt_key_rejected_does_not_run_kiro(self):
        # <requestId>/inputs/ で始まらない promptKey は fail-closed。
        bad_key = f"{self._req}/runs/prompt.md"
        kiro_run = unittest.mock.Mock()
        get_bytes = unittest.mock.Mock()
        with unittest.mock.patch.object(
            hs.subprocess, "run", kiro_run
        ), unittest.mock.patch.object(
            hs, "_s3_get_bytes", get_bytes
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "p", prompt_key=bad_key,
            )
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("promptKey-rejected", status["failedUploads"])
        # 拒否なら Kiro も S3 取得も走らない。
        self.assertFalse(kiro_run.called)
        self.assertFalse(get_bytes.called)

    def test_prompt_key_traversal_rejected(self):
        bad_key = f"{self._req}/inputs/../../secret"
        kiro_run = unittest.mock.Mock()
        with unittest.mock.patch.object(
            hs.subprocess, "run", kiro_run
        ), unittest.mock.patch.object(
            hs, "_s3_get_bytes", unittest.mock.Mock()
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "p", prompt_key=bad_key,
            )
        self.assertFalse(ok)
        self.assertIn("promptKey-rejected", status["failedUploads"])
        self.assertFalse(kiro_run.called)

    def test_prompt_key_fetch_failure_recorded(self):
        key = f"{self._req}/inputs/prompt.md"
        kiro_run = unittest.mock.Mock()
        with unittest.mock.patch.object(
            hs.subprocess, "run", kiro_run
        ), unittest.mock.patch.object(
            hs, "_s3_get_bytes",
            return_value=(False, "aws s3 cp(get) exited 1: NoSuchKey"),
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "p", prompt_key=key,
            )
        self.assertFalse(ok)
        self.assertFalse(status["ok"])
        self.assertIn("promptKey-fetch", status["failedUploads"])
        # 取得失敗なら Kiro は走らない(fail-closed)。
        self.assertFalse(kiro_run.called)

    def test_legacy_prompt_used_when_no_prompt_key(self):
        # promptKey 未指定なら従来どおり payload['prompt'](引数 prompt)を使う。
        captured, run = self._patch_kiro_capturing_prompt()
        get_bytes = unittest.mock.Mock()
        with unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=run
        ), unittest.mock.patch.object(
            hs, "_s3_get_bytes", get_bytes
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            ok, status = hs.run_kiro_and_upload(
                f"{self._req}/runs/", self._req, "直接渡したプロンプト",
            )
        self.assertTrue(ok)
        self.assertEqual(captured["prompt"], "直接渡したプロンプト")
        # S3 取得は呼ばれない(後方互換)。
        self.assertFalse(get_bytes.called)


class TestRunHookPrefixFallback(unittest.TestCase):
    """/run の prefix フォールバックが <microvmId>/runs/ になること。"""

    def test_prefix_fallback_uses_microvm_runs(self):
        import io

        inner = json.dumps({"prompt": "x"})  # prefix / requestId なし
        outer = json.dumps({"microvmId": "mvm-77", "runHookPayload": inner})
        body = outer.encode("utf-8")

        captured = {}

        class _FakeHandler(hs.HookHandler):
            def __init__(self):  # BaseHTTPRequestHandler の __init__ を回避
                self.path = hs.RUN_PATH
                self.headers = {"Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)

            def _respond(self, code, payload):
                captured["code"] = code
                captured["payload"] = payload

        handler = _FakeHandler()
        run_args = {}

        def _fake_run(prefix, request_id, prompt, **kwargs):
            run_args["prefix"] = prefix
            run_args["request_id"] = request_id
            return True, {}

        # Thread をその場で同期実行するスタブに置き換える。
        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        with unittest.mock.patch.dict(
            os.environ, {"RESULT_BUCKET": "bkt"}, clear=False
        ), unittest.mock.patch.object(
            hs, "run_kiro_and_upload", side_effect=_fake_run
        ), unittest.mock.patch.object(
            hs.threading, "Thread", _SyncThread
        ):
            handler._handle()

        # 200 accepted が返る。
        self.assertEqual(captured["code"], 200)
        # prefix 未指定・requestId 未指定なら <microvmId>/runs/ にフォールバックし、
        # request_id も microvmId で補完される。
        self.assertEqual(run_args["prefix"], "mvm-77/runs/")
        self.assertEqual(run_args["request_id"], "mvm-77")


_TARO_NAME = "Taro Yamada"
_TARO_EMAIL = "12345+taro@users.noreply.github.com"


class TestResolveGitAuthor(unittest.TestCase):
    def test_defaults_when_unset(self):
        self.assertEqual(
            hs.resolve_git_author({}),
            ("kiro-bot", "kiro-bot@users.noreply.github.com"),
        )

    def test_both_set_and_trimmed(self):
        env = {
            "KIRO_GIT_AUTHOR_NAME": f"  {_TARO_NAME}\t",
            "KIRO_GIT_AUTHOR_EMAIL": f" {_TARO_EMAIL} ",
        }
        self.assertEqual(hs.resolve_git_author(env), (_TARO_NAME, _TARO_EMAIL))

    def test_empty_and_blank_use_default(self):
        for v in ("", "   ", "\t"):
            env = {"KIRO_GIT_AUTHOR_NAME": v, "KIRO_GIT_AUTHOR_EMAIL": v}
            self.assertEqual(
                hs.resolve_git_author(env), (hs.GIT_AUTHOR_NAME, hs.GIT_AUTHOR_EMAIL)
            )

    def test_independent_resolution(self):
        self.assertEqual(
            hs.resolve_git_author({"KIRO_GIT_AUTHOR_NAME": _TARO_NAME}),
            (_TARO_NAME, hs.GIT_AUTHOR_EMAIL),
        )
        self.assertEqual(
            hs.resolve_git_author({"KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL}),
            (hs.GIT_AUTHOR_NAME, _TARO_EMAIL),
        )

    def test_invalid_name_falls_back(self):
        for bad in ['a"b', "a$b", "a`b", "a\\b", "a<b", "a>b", "a\nb", "a\x7fb"]:
            with unittest.mock.patch.object(hs, "log"):
                name, email = hs.resolve_git_author(
                    {"KIRO_GIT_AUTHOR_NAME": bad, "KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL}
                )
            self.assertEqual(name, hs.GIT_AUTHOR_NAME, repr(bad))
            self.assertEqual(email, _TARO_EMAIL)

    def test_invalid_email_falls_back(self):
        for bad in ["noatmark", "a@b@c", "a b@c.d", 'a"b@c.d', "@c.d", "a@"]:
            with unittest.mock.patch.object(hs, "log"):
                name, email = hs.resolve_git_author(
                    {"KIRO_GIT_AUTHOR_NAME": _TARO_NAME, "KIRO_GIT_AUTHOR_EMAIL": bad}
                )
            self.assertEqual(name, _TARO_NAME)
            self.assertEqual(email, hs.GIT_AUTHOR_EMAIL, repr(bad))

    def test_warning_does_not_log_value(self):
        bad_name = 'secret"name'
        bad_email = "secret-email-no-at"
        with unittest.mock.patch.object(hs, "log") as m:
            hs.resolve_git_author(
                {"KIRO_GIT_AUTHOR_NAME": bad_name, "KIRO_GIT_AUTHOR_EMAIL": bad_email}
            )
        self.assertTrue(m.called)
        logged = " ".join(str(a) for c in m.call_args_list for a in c.args)
        self.assertIn("KIRO_GIT_AUTHOR_NAME", logged)
        self.assertIn("KIRO_GIT_AUTHOR_EMAIL", logged)
        self.assertNotIn("secret", logged)

    def test_uses_os_environ_by_default(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"KIRO_GIT_AUTHOR_NAME": _TARO_NAME, "KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL},
        ):
            self.assertEqual(hs.resolve_git_author(), (_TARO_NAME, _TARO_EMAIL))

    def test_build_git_env_uses_configured_author(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"KIRO_GIT_AUTHOR_NAME": _TARO_NAME, "KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL},
        ):
            env = hs.build_git_env("/tmp/home-x")
        self.assertEqual(env["GIT_AUTHOR_NAME"], _TARO_NAME)
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], _TARO_EMAIL)
        self.assertEqual(env["GIT_COMMITTER_NAME"], _TARO_NAME)
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], _TARO_EMAIL)

    def test_build_repo_prompt_uses_configured_author(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"KIRO_GIT_AUTHOR_NAME": _TARO_NAME, "KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL},
        ):
            p = hs.build_repo_prompt("https://github.com/a/b.git", "main", "req-1", "作業")
        self.assertIn(f'git config user.name "{_TARO_NAME}"', p)
        self.assertIn(f'git config user.email "{_TARO_EMAIL}"', p)
        self.assertNotIn('git config user.name "kiro-bot"', p)
        self.assertNotIn("git config user.email \"kiro-bot@", p)

    def test_askpass_username_stays_kiro_bot(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"KIRO_GIT_AUTHOR_NAME": _TARO_NAME, "KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL},
        ):
            script = hs.render_askpass_script("arn:x")
        self.assertIn("printf '%s' 'kiro-bot'", script)
        self.assertNotIn(_TARO_NAME, script)


class TestRunKiroGithubFlowAuthor(unittest.TestCase):
    """GitHub フローで kiro-cli に渡る env がデプロイ時設定の author になる。"""

    # 既存クラスのセットアップ/ヘルパーを流用する(継承すると既存テストが再実行されるため)。
    setUp = TestRunKiroGithubFlow.setUp
    _fetch_secret_side = TestRunKiroGithubFlow._fetch_secret_side
    _fake_kiro_run = TestRunKiroGithubFlow._fake_kiro_run

    def test_kiro_env_has_configured_author(self):
        captured = {}
        base = self._fake_kiro_run({})

        def _run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "kiro-cli":
                captured["env"] = kwargs.get("env")
            return base(cmd, *args, **kwargs)

        with unittest.mock.patch.dict(
            os.environ,
            {"KIRO_GIT_AUTHOR_NAME": _TARO_NAME, "KIRO_GIT_AUTHOR_EMAIL": _TARO_EMAIL},
        ), unittest.mock.patch.object(
            hs, "_fetch_secret", side_effect=self._fetch_secret_side
        ), unittest.mock.patch.object(
            hs.subprocess, "run", side_effect=_run
        ), unittest.mock.patch.object(
            hs, "_s3_put_bytes", return_value=(True, "s3://ok")
        ), unittest.mock.patch.object(
            hs, "_s3_put_file", return_value=(True, "s3://ok")
        ):
            hs.run_kiro_and_upload(
                f"{self._req}/runs/",
                self._req,
                "作業内容",
                repo_url="https://github.com/a/b.git",
                base_branch="main",
            )
        env = captured["env"]
        self.assertEqual(env["GIT_AUTHOR_NAME"], _TARO_NAME)
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], _TARO_EMAIL)
        self.assertEqual(env["GIT_COMMITTER_NAME"], _TARO_NAME)
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], _TARO_EMAIL)


if __name__ == "__main__":
    unittest.main()
