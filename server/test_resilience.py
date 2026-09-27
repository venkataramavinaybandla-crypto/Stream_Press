"""Offline tests for the anti-'source refused the connection' resilience layer:
_refused_connection classification, _resilient_network_opts hardening, the
_extract_with_resilience retry ladder, and the _http_get persona ladder."""

import unittest
from unittest import mock

import server.main as m


def _err(text: str) -> Exception:
    return RuntimeError(text)


class RefusedConnectionClassificationTests(unittest.TestCase):
    def test_connection_reset(self):
        self.assertTrue(m._refused_connection(_err("URLError: Connection reset by peer")))

    def test_connection_refused(self):
        self.assertTrue(m._refused_connection(_err("URLError: [Errno 111] Connection refused")))

    def test_timeout(self):
        self.assertTrue(m._refused_connection(_err("socket.timeout: timed out")))

    def test_bot_wall_403(self):
        self.assertTrue(m._refused_connection(_err("ERROR: HTTP Error 403: Forbidden")))

    def test_tls_rejection(self):
        self.assertTrue(m._refused_connection(_err("ssl: CERTIFICATE_VERIFY_FAILED")))

    def test_dns_failure(self):
        self.assertTrue(m._refused_connection(_err("URLError: [Errno -2] Name or service not known")))

    def test_unsupported_url_is_not_retryable(self):
        self.assertFalse(m._refused_connection(_err("Unsupported URL: https://x/y")))

    def test_drm_is_not_retryable(self):
        self.assertFalse(m._refused_connection(_err("This video is DRM protected")))

    def test_auth_wall_is_not_retryable(self):
        self.assertFalse(m._refused_connection(_err("ERROR: Sign in to confirm you're not a bot")))


class ResilientNetworkOptsTests(unittest.TestCase):
    def test_hardening_present_and_base_preserved(self):
        m.PROXY_URL = None  # deterministic
        opts = m._resilient_network_opts()
        self.assertEqual(opts["source_address"], "0.0.0.0")
        self.assertTrue(opts["legacy_server_connect"])
        # base options survive
        self.assertIn("User-Agent", opts["http_headers"])
        base = m._network_opts()
        for k, v in base.items():
            self.assertEqual(opts[k], v)

    def test_does_not_mutate_network_opts(self):
        before = dict(m._network_opts())
        m._resilient_network_opts()
        self.assertEqual(m._network_opts(), before)


class ExtractWithResilienceTests(unittest.TestCase):
    def _patch(self, target_none=True, fail_first=False, raise_non_connection=False,
               fail_all=False):
        """Patch impersonate discovery + YoutubeDL construction."""
        target = None if target_none else object()
        p1 = mock.patch.object(m, "_get_impersonate_target", return_value=target)

        created = []

        class FakeYDL:
            def __init__(self, opts):
                created.append(opts)
                if fail_all or (fail_first and "impersonate" not in opts):
                    raise _err("Connection reset by peer")
                if raise_non_connection:
                    raise _err("Unsupported URL: x")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {"id": "ok", "url": url}

        p2 = mock.patch.object(m.yt_dlp, "YoutubeDL", FakeYDL)
        return p1, p2, created

    def test_success_on_first_rung(self):
        p1, p2, created = self._patch()
        with p1, p2:
            info = m._extract_with_resilience({"quiet": True}, "https://x.test/v")
        self.assertEqual(info["id"], "ok")
        self.assertEqual(len(created), 1)
        self.assertNotIn("impersonate", created[0])
        self.assertEqual(created[0]["source_address"], "0.0.0.0")

    def test_impersonate_rung_used_and_caller_opts_win(self):
        p1, p2, created = self._patch(target_none=False, fail_first=True)
        with p1, p2:
            info = m._extract_with_resilience({"quiet": True, "format": "best"}, "https://x.test/v")
        self.assertEqual(info["id"], "ok")
        self.assertEqual(len(created), 2)
        self.assertNotIn("impersonate", created[0])
        self.assertIn("impersonate", created[1])
        self.assertEqual(created[1]["format"], "best")   # caller opts survive merge

    def test_non_connection_error_stops_climb(self):
        p1, p2, created = self._patch(target_none=False, raise_non_connection=True)
        with p1, p2:
            with self.assertRaises(Exception) as ctx:
                m._extract_with_resilience({}, "https://x.test/v")
        self.assertIn("Unsupported URL", str(ctx.exception))
        self.assertEqual(len(created), 1)                # never tried rung 2

    def test_no_impersonate_available_single_rung(self):
        p1, p2, created = self._patch(target_none=True, fail_first=True)
        with p1, p2:
            with self.assertRaises(Exception) as ctx:
                m._extract_with_resilience({}, "https://x.test/v")
        self.assertIn("Connection reset", str(ctx.exception))
        self.assertEqual(len(created), 1)

    def test_persona_failure_invalidates_cached_target(self):
        p1, p2, created = self._patch(target_none=False, fail_all=True)
        sentinel = object()
        with p1, p2:
            m._IMPERSONATE_STATE["tried"] = True
            m._IMPERSONATE_STATE["target"] = sentinel
            try:
                with self.assertRaises(Exception):
                    m._extract_with_resilience({}, "https://x.test/v")
                self.assertIsNot(m._IMPERSONATE_STATE["target"], sentinel)  # persona rejected → invalidated
                self.assertEqual(len(created), 2)                            # both rungs attempted
            finally:
                m._IMPERSONATE_STATE["tried"] = False
                m._IMPERSONATE_STATE["target"] = None


class HttpGetPersonaLadderTests(unittest.TestCase):
    def test_persona_success_short_circuits(self):
        fake = mock.Mock()
        fake.status_code = 200
        fake.text = "<html>persona</html>"
        with mock.patch.object(m, "_curl_get", return_value=fake) as cg, \
             mock.patch.object(m._URL_OPENER, "open") as opener:
            out = m._http_get("https://x.test/page")
        self.assertEqual(out, "<html>persona</html>")
        opener.assert_not_called()
        cg.assert_called_once()

    def test_persona_failure_falls_back_to_urllib(self):
        fake = mock.Mock()
        fake.status_code = 503
        fake.text = "busy"
        resp = mock.Mock()
        resp.read.return_value = b"<html>urllib</html>"
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(m, "_curl_get", return_value=fake), \
             mock.patch.object(m._URL_OPENER, "open", return_value=resp) as opener:
            out = m._http_get("https://x.test/page")
        self.assertEqual(out, "<html>urllib</html>")
        opener.assert_called_once()

    def test_both_fail_returns_none(self):
        with mock.patch.object(m, "_curl_get", return_value=None), \
             mock.patch.object(m._URL_OPENER, "open", side_effect=OSError("no")):
            self.assertIsNone(m._http_get("https://x.test/page"))


if __name__ == "__main__":
    unittest.main()
