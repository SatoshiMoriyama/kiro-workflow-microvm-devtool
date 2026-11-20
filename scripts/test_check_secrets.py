import unittest

from check_secrets import scan_diff


AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


def diff_with(added):
    return "+++ b/x.txt\n@@ -0,0 +1 @@\n+" + added + "\n"


class ScanDiffTest(unittest.TestCase):
    def test_aws_key_detected(self):
        self.assertEqual(scan_diff(diff_with("key=" + AWS))[0][2], "AWS access key ID")

    def test_github_token_detected(self):
        self.assertEqual(scan_diff(diff_with("ghp_" + "a" * 36))[0][2], "GitHub token")

    def test_private_key_detected(self):
        self.assertTrue(scan_diff(diff_with("-----BEGIN RSA " + "PRIVATE KEY-----")))

    def test_generic_assignment_detected(self):
        self.assertTrue(scan_diff(diff_with('api_key = "' + "a" * 32 + '"')))

    def test_plain_text_passes(self):
        self.assertEqual(scan_diff(diff_with("token を書かない")), [])

    def test_removed_lines_ignored(self):
        self.assertEqual(scan_diff("+++ b/x\n@@ -1 +0,0 @@\n-" + AWS + "\n"), [])

    def test_line_number_reported(self):
        d = "+++ b/x.txt\n@@ -0,0 +5 @@\n+" + AWS + "\n"
        self.assertEqual(scan_diff(d)[0][:2], ("x.txt", 5))


if __name__ == "__main__":
    unittest.main()
