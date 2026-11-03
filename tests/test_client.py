"""Tests for backchannel.py against the fake provider. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backchannel as bc  # noqa: E402
from fake_provider import CODE, DOMAIN, FakeProvider  # noqa: E402


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProvider().start()
        self.addCleanup(self.fake.stop)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home.name, "BACKCHANNEL_URL": self.fake.url})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BACKCHANNEL_PROFILE", None)
        self.sleeps = []
        sleep = mock.patch.object(bc.time, "sleep", self.sleeps.append)
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_cli(self, *argv):
        """Run one command in-process; return (exit code, the JSON it printed)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                code = bc.main(list(argv))
            except SystemExit as e:  # argparse errors
                code = e.code
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, f"expected one JSON line, got {buf.getvalue()!r}")
        return code, json.loads(lines[0])

    def sign_up(self, email="ana@example.com", profile=None):
        p = ["--profile", profile] if profile else []
        code, r = self.run_cli(*p, "onboard", "--email", email)
        self.assertEqual((code, r["state"]), (0, "waiting_for_code"))
        code, r = self.run_cli(*p, "verify", "--email", email, "--code", CODE)
        self.assertEqual((code, r["state"]), (0, "onboarded"))
        return r["address"]

    # sign-up

    def test_onboard_verify_doctor(self):
        address = self.sign_up()
        self.assertEqual(address, f"ana@example.com#{DOMAIN}")
        creds = bc.credentials_path(None)
        self.assertEqual(stat.S_IMODE(creds.stat().st_mode), 0o600)
        self.assertFalse(bc.pending_path(None).exists())
        code, r = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(r["token"], "accepted")
        self.assertEqual(r["health"], {"status": "ok"})
        self.assertNotIn("provider", r)  # the token check's 404 stays out of the top level
        self.assertEqual(self.fake.count("GET", "/messages"), 0)  # doctor never fetches

    def test_onboard_twice_does_not_request_a_second_code(self):
        self.run_cli("onboard", "--email", "ana@example.com")
        code, r = self.run_cli("onboard", "--email", "ana@example.com")
        self.assertEqual((code, r["new_code"]), (0, False))
        self.assertEqual(self.fake.signups, ["ana@example.com"])
        self.run_cli("onboard", "--email", "ana@example.com", "--again")
        self.assertEqual(len(self.fake.signups), 2)

    def test_already_onboarded(self):
        self.sign_up()
        code, r = self.run_cli("onboard", "--email", "ana@example.com")
        self.assertEqual((code, r["state"]), (0, "already_onboarded"))
        code, r = self.run_cli("onboard", "--email", "bo@example.com")
        self.assertEqual((code, r["error"]), (1, "profile_in_use"))

    def test_wrong_code(self):
        self.run_cli("onboard", "--email", "ana@example.com")
        code, r = self.run_cli("verify", "--email", "ana@example.com", "--code", "000000")
        self.assertEqual((code, r["error"]), (3, "invalid_code"))
        self.assertIn("newest code", r["next"])
        self.assertFalse(bc.credentials_path(None).exists())

    def test_doctor_without_credentials(self):
        code, r = self.run_cli("doctor")
        self.assertEqual((code, r["token"], r["has_credentials"]), (0, "none", False))
        self.assertIn("onboard", r["next"])

    def test_dead_token(self):
        self.sign_up()
        self.fake.tokens.clear()
        code, r = self.run_cli("doctor")
        self.assertEqual((code, r["error"]), (4, "unauthorized"))
        self.assertIn("--again", r["next"])
        code, r = self.run_cli("fetch")
        self.assertEqual(code, 4)

    # messages

    def test_send_fetch_ack_status(self):
        ana = self.sign_up("ana@example.com")
        bo = self.sign_up("bo@example.com", profile="bo")
        code, sent = self.run_cli("send", "--to", bo, "--body", "hello", "--subject", "hi")
        self.assertEqual((code, sent["state"]), (0, "delivered"))
        code, r = self.run_cli("status", sent["id"])
        self.assertEqual((code, r["state"]), (0, "delivered"))

        code, got = self.run_cli("--profile", "bo", "fetch")
        self.assertEqual((code, got["count"]), (0, 1))
        msg = got["messages"][0]
        self.assertEqual((msg["from"], msg["body"], msg["subject"]), (ana, "hello", "hi"))
        self.assertEqual(got["new_senders"], [ana])

        code, r = self.run_cli("--profile", "bo", "ack", msg["id"])
        self.assertEqual((code, r["acked"]), (0, [msg["id"]]))
        self.assertNotIn("not_in_inbox", r)
        code, r = self.run_cli("status", sent["id"])
        self.assertEqual(r["state"], "acked")
        code, got = self.run_cli("--profile", "bo", "fetch")
        self.assertEqual(got["count"], 0)

    def test_reply_keeps_thread_and_subject(self):
        ana = self.sign_up("ana@example.com")
        bo = self.sign_up("bo@example.com", profile="bo")
        _, first = self.run_cli("send", "--to", bo, "--body", "question", "--subject", "plans")
        _, got = self.run_cli("--profile", "bo", "fetch", "--ack")
        self.assertEqual(got["acked"], [first["id"]])
        code, reply = self.run_cli("--profile", "bo", "send", "--to", ana, "--body", "answer", "--reply-to", first["id"])
        self.assertEqual((code, reply["thread"]), (0, first["thread"]))
        self.assertEqual(reply["provider"]["id"], reply["id"])

        _, got = self.run_cli("fetch", "--ack")
        m = got["messages"][0]
        self.assertEqual((m["reply_to"], m["thread"], m["subject"]), (first["id"], first["thread"], "plans"))
        code, r = self.run_cli("threads")
        self.assertEqual((code, r["count"]), (0, 1))
        self.assertEqual(r["threads"][0]["count"], 2)
        self.assertEqual(r["threads"][0]["with"], [bo])

    def test_reply_to_unknown_id_needs_thread(self):
        self.sign_up()
        code, r = self.run_cli("send", "--to", "x@y.z#fake.test", "--body", "b", "--reply-to", "nope@fake.test")
        self.assertEqual((code, r["error"]), (1, "usage"))

    def test_send_checks_address_and_size(self):
        self.sign_up()
        code, r = self.run_cli("send", "--to", "not-an-address", "--body", "b")
        self.assertEqual((code, r["error"]), (1, "usage"))
        code, r = self.run_cli("send", "--to", "x@y.z#fake.test", "--body", "x" * (bc.MAX_ENVELOPE + 1))
        self.assertEqual((code, r["error"]), (1, "too_large"))
        self.assertEqual(self.fake.count("POST", "/messages"), 0)

    def test_ack_unknown_id_is_flagged(self):
        self.sign_up()
        code, r = self.run_cli("ack", "ghost@fake.test")
        self.assertEqual(code, 0)
        self.assertEqual(r["not_in_inbox"], ["ghost@fake.test"])

    def test_fetch_clamps_limit_and_wait(self):
        self.sign_up()
        code, r = self.run_cli("fetch", "--limit", "500", "--wait", "99")
        self.assertEqual((code, r["clamped"]), (0, {"limit": 100, "wait": 30}))

    def test_revoke(self):
        self.sign_up()
        code, r = self.run_cli("revoke")
        self.assertEqual((code, r["state"]), (0, "revoked"))
        self.assertFalse(bc.credentials_path(None).exists())
        self.assertEqual(self.fake.tokens, {})
        code, r = self.run_cli("whoami")
        self.assertEqual((code, r["error"]), (1, "no_credentials"))

    # retries and failures

    def test_5xx_is_retried(self):
        self.sign_up()
        self.fake.fail_next("GET", "/messages", 503, {"error": "busy"})
        code, r = self.run_cli("fetch")
        self.assertEqual((code, r["attempts"]), (0, 2))
        self.assertEqual(r["retries"], [{"http_status": 503, "waited_s": 1.0}])

    def test_5xx_on_send_suggests_resend_with_same_id(self):
        self.sign_up()
        for _ in range(bc.MAX_ATTEMPTS):
            self.fake.fail_next("POST", "/messages", 500, {"error": "oops"})
        code, r = self.run_cli("send", "--to", "x@y.z#fake.test", "--body", "b")
        self.assertEqual((code, r["attempts"]), (5, bc.MAX_ATTEMPTS))
        self.assertIn(f"--id {r['id']}", r["next"])
        code, again = self.run_cli("send", "--to", "x@y.z#fake.test", "--body", "b", "--id", r["id"])
        self.assertEqual((code, again["id"]), (0, r["id"]))

    def test_short_429_is_waited_out(self):
        self.sign_up()
        self.fake.fail_next("GET", "/messages", 429, {"error": "rate_limited", "retry_after": 2})
        code, r = self.run_cli("fetch")
        self.assertEqual(code, 0)
        self.assertEqual(self.sleeps, [2.0])

    def test_long_429_is_reported_not_slept(self):
        self.sign_up()
        self.fake.fail_next("GET", "/messages", 429, {"error": "rate_limited", "retry_after": 60})
        code, r = self.run_cli("fetch")
        self.assertEqual((code, r["error"], r["retry_after"]), (5, "rate_limited", 60.0))
        self.assertEqual(self.sleeps, [])

    def test_signup_is_not_retried(self):
        self.fake.fail_next("POST", "/signup/request", 503, {"error": "busy"})
        code, r = self.run_cli("onboard", "--email", "ana@example.com")
        self.assertEqual((code, r["attempts"]), (5, 1))
        self.assertEqual(self.fake.count("POST", "/signup/request"), 1)

    def test_redirect_is_not_followed(self):
        self.fake.fail_next("GET", "/health", 302, "", {"Location": "https://evil.test/v1/health"})
        code, r = self.run_cli("doctor")
        self.assertEqual((code, r["error"]), (3, "redirect"))
        self.assertEqual(self.fake.count("GET", "/health"), 1)

    def test_wrong_kind_of_server(self):
        self.fake.fail_next("GET", "/health", 200, "<html>hello</html>")
        code, r = self.run_cli("doctor")
        self.assertEqual((code, r["error"]), (3, "bad_response"))

    def test_unreachable(self):
        self.fake.stop()
        code, r = self.run_cli("doctor")
        self.assertEqual((code, r["error"], r["http_status"]), (5, "unreachable", 0))

    # local checks

    def test_base_url_must_be_https_unless_local(self):
        code, r = self.run_cli("--base-url", "http://example.com/v1", "doctor")
        self.assertEqual((code, r["error"]), (1, "usage"))
        bc.check_base("https://example.com/v1")
        bc.check_base("http://localhost:8000/v1")

    def test_other_provider_refused(self):
        self.sign_up()
        code, r = self.run_cli("--base-url", "https://elsewhere.test/v1", "fetch")
        self.assertEqual((code, r["error"]), (1, "other_provider"))

    def test_bad_profile_name(self):
        code, r = self.run_cli("--profile", "../x", "whoami")
        self.assertEqual((code, r["error"]), (1, "usage"))

    def test_usage_errors_are_json(self):
        code, r = self.run_cli("send", "--body", "no recipient")
        self.assertEqual((code, r["error"]), (1, "usage"))

    def test_bad_credentials_file(self):
        path = bc.credentials_path(None)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        code, r = self.run_cli("whoami")
        self.assertEqual((code, r["error"]), (1, "bad_file"))

    def test_inbox_is_trimmed(self):
        with mock.patch.object(bc, "INBOX_MAX", 2000), mock.patch.object(bc, "INBOX_KEEP", 1000):
            bc.remember(None, [{"id": f"m{i}", "body": "x" * 100} for i in range(30)])
            lines = bc.inbox_lines(None)
        self.assertLessEqual(sum(len(x) + 1 for x in lines), 1000)
        self.assertEqual(json.loads(lines[-1])["id"], "m29")
        bc.remember(None, [{"id": "m29", "body": "dup"}])
        self.assertEqual(sum(1 for m in bc.inbox_messages(None) if m["id"] == "m29"), 1)


if __name__ == "__main__":
    unittest.main()
