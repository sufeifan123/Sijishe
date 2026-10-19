import os
import unittest
from unittest.mock import Mock, patch

from sijishe import (
    CheckinError,
    driver_indicates_logged_in,
    extract_https_candidates,
    has_forum_fingerprint,
    mask_username,
    normalize_base_url,
    parse_accounts,
    resolve_forum_urls,
)


class ParseAccountsTests(unittest.TestCase):
    def test_single_account_and_ampersand_in_password(self):
        accounts = parse_accounts("demo&pass&word")
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].username, "demo")
        self.assertEqual(accounts[0].password, "pass&word")

    def test_email_username_is_not_split(self):
        accounts = parse_accounts("demo@example.com&password")
        self.assertEqual(accounts[0].username, "demo@example.com")

    def test_invalid_account_is_rejected(self):
        with self.assertRaises(CheckinError):
            parse_accounts("missing-separator")

    def test_username_is_masked(self):
        self.assertEqual(mask_username("someone"), "s***e")


class UrlDiscoveryTests(unittest.TestCase):
    def test_only_https_urls_are_normalized(self):
        self.assertEqual(
            normalize_base_url("https://Example.COM/path?q=1"),
            "https://example.com",
        )
        self.assertIsNone(normalize_base_url("http://example.com"))
        self.assertIsNone(normalize_base_url("https://user:pass@example.com"))
        self.assertIsNone(normalize_base_url("https://127.0.0.1"))
        self.assertIsNone(normalize_base_url("https://169.254.169.254"))

    def test_candidates_are_extracted_and_sources_are_excluded(self):
        page = """
        <a href="https://forum-one.example/path">线路一</a>
        <a href="http://insecure.example">不安全线路</a>
        <script>const backup = "https:\\/\\/forum-two.example\\/login";</script>
        <a href="https://47447.net/">发布页</a>
        """
        self.assertEqual(
            extract_https_candidates(page, "https://47447.net/"),
            ["https://forum-one.example", "https://forum-two.example"],
        )

    def test_forum_fingerprint_requires_brand_and_technology(self):
        self.assertTrue(
            has_forum_fingerprint(
                "<title>司机社</title><form action='member.php?mod=logging'>"
            )
        )
        self.assertFalse(has_forum_fingerprint("<title>司机社发布页</title>"))
        self.assertFalse(has_forum_fingerprint("Discuz login formhash"))

    @patch("sijishe.validate_candidate_with_browser")
    @patch("sijishe.validate_candidate_with_requests")
    @patch("sijishe.collect_candidates")
    @patch("sijishe.build_http_session")
    def test_multiple_validated_candidates_are_retained_for_failover(
        self,
        build_session,
        collect_candidates,
        validate_with_requests,
        validate_with_browser,
    ):
        build_session.return_value = Mock()
        collect_candidates.return_value = [
            "https://one.example",
            "https://two.example",
            "https://three.example",
        ]
        validate_with_requests.side_effect = [
            "https://one.example",
            None,
            "https://three.example",
        ]
        validate_with_browser.side_effect = ["https://two.example"]

        with patch.dict(os.environ, {"FORUM_URL": ""}):
            result = resolve_forum_urls(Mock())

        self.assertEqual(
            result,
            [
                "https://one.example",
                "https://two.example",
                "https://three.example",
            ],
        )


class LoginStateTests(unittest.TestCase):
    def test_auth_cookie_takes_precedence_over_failure_text(self):
        driver = Mock()
        driver.page_source = "登录失败"
        driver.get_cookies.return_value = [{"name": "abc_auth", "value": "token"}]

        self.assertTrue(driver_indicates_logged_in(driver))


if __name__ == "__main__":
    unittest.main()
