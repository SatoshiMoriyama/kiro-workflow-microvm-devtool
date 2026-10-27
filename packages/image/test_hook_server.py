#!/usr/bin/env python3
"""hook_server.py の純関数ユニットテスト(pytest / unittest どちらでも動く)。"""

import json
import os
import sys
import tempfile
import unittest

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
    def _status(self, exit_code, timed_out, artifacts=None):
        return hs.build_status(
            request_id="req-1",
            prompt="p",
            exit_code=exit_code,
            timed_out=timed_out,
            artifacts=artifacts or [],
            skipped=[],
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

    def test_missing_file_false(self):
        self.assertFalse(hs.contains_secret("/no/such/file.txt", None))


if __name__ == "__main__":
    unittest.main()
