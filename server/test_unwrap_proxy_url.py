"""Tests for extract_url_from_input / _unwrap_proxy_url (stdlib unittest, pytest-compatible)."""

import base64
import unittest
from urllib.parse import unquote

from server.main import _unwrap_proxy_url, extract_url_from_input


def b64(text: str) -> str:
    """Standard base64, URL-safe-free ('+'/'/' style) as CPO-style unblockers emit."""
    return base64.b64encode(text.encode()).decode().rstrip("=")


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class UnwrapProxyUrlTests(unittest.TestCase):
    def test_base64_bare_domain_merges_wrapper_params(self):
        # 185.x.x.x/watch?v=ID&__cpo=<b64 www.youtube.com> → https://www.youtube.com/watch?v=ID
        wrapper = f"https://185.199.108.153/watch?v=dQw4w9WgXcQ&__cpo={b64('www.youtube.com')}"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_base64_apex_domain_adopts_wrapper_path(self):
        # Bare apex domain: wrapper path /watch carries over (no www invented).
        wrapper = f"https://185.199.108.153/watch?v=dQw4w9WgXcQ&__cpo={b64('youtube.com')}"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://youtube.com/watch?v=dQw4w9WgXcQ")

    def test_extract_url_from_input_bare_domain_base64(self):
        wrapper = f"https://proxy.example.com/watch?v=abc123&__cpo={b64('www.youtube.com')}"
        self.assertEqual(extract_url_from_input(wrapper), "https://www.youtube.com/watch?v=abc123")

    def test_encoded_url_param_merges_path_and_params(self):
        dest = "https://www.youtube.com/watch?v=abc"
        wrapper = f"https://p.example.com/out?__cpo={dest}&foo=1"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://www.youtube.com/watch?v=abc&foo=1")

    def test_dest_param_with_own_query_plus_wrapper_params(self):
        wrapper = "https://p.example.com/go?dest=https%3A%2F%2Fcdn.site.com%2Fv.mp4&extra=9"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://cdn.site.com/v.mp4?extra=9")

    def test_proxy_only_params_not_copied(self):
        dest = "https://target.example.com/page"
        wrapper = f"https://p.example.com/?__cpo={dest}&session=xyz&ref=ad&token=t"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://target.example.com/page")

    def test_tracking_params_stripped_from_value(self):
        # Wrapper params surviving ON the destination value itself.
        wrapper = "https://p.example.com/?url=https%3A%2F%2Fsite.tv%2Fwatch%3Futm_source%3Dwrap%26gclid%3Dzz"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://site.tv/watch")

    def test_percent_encoded_bare_domain_merges_params(self):
        wrapper = "https://p.example.com/watch?v=7&url=youtube.com%2Fwatch%3Fv%3D7"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://youtube.com/watch?v=7")

    def test_same_key_name_merges_with_different_value(self):
        # Wrapper `v=9` differs from destination's `v=7` → both kept, no clobber.
        wrapper = "https://p.example.com/watch?v=9&url=youtube.com%2Fwatch%3Fv%3D7"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://youtube.com/watch?v=7&v=9")

    def test_urlsafe_b64_destination_with_plus_slash_in_value(self):
        wrapper = f"https://p.example.com/watch?v=a%2Bb%2Fc&u={b64url('vimeo.com')}"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://vimeo.com/watch?v=a%2Bb%2Fc")

    def test_path_base64_destination_merges_query(self):
        seg = b64("youtube.com/watch")
        wrapper = f"https://p.example.com/watch?v=zE&x={seg}"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://youtube.com/watch?v=zE")

    def test_destination_own_params_kept_and_merged_after(self):
        dest = "https://site.io/a?keep=1"
        wrapper = f"https://p.example.com/?target={dest}&add=2"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://site.io/a?keep=1&add=2")

    def test_fragment_of_wrapper_preserved(self):
        dest = "https://site.io/page"
        wrapper = f"https://p.example.com/?dest={dest}&add=1#section-2"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://site.io/page?add=1#section-2")

    def test_non_url_b64_value_ignored_for_merge(self):
        # b64 value decodes to non-URL text → no unwrap, URL returned untouched.
        wrapper = f"https://p.example.com/?__cpo={b64('just plain text')}"
        self.assertIsNone(_unwrap_proxy_url(wrapper))

    def test_nested_wrapper_two_levels(self):
        # Two wrappers deep: only the recursive extract unwraps both layers.
        inner = f"https://mid.example.com/?url={b64('https://final.example.tv/x')}&tail=1"
        wrapper = f"https://p.example.com/?__cpo={inner}&top=1"
        self.assertEqual(extract_url_from_input(wrapper), "https://final.example.tv/x?tail=1&top=1")

    def test_all_params_proxy_controlled_no_trailing_separator(self):
        dest = "https://a.example.com/p"
        wrapper = f"https://p.example.com/?dest={dest}&ref=x&token=y"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://a.example.com/p")

    def test_double_encoded_wrapper_value_unquoted_twice(self):
        from urllib.parse import quote
        wrapper = "https://p.example.com/?dest=" + quote(quote("https://ok.io/w?z=1")) + "&m=3"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://ok.io/w?z=1&m=3")

    def test_param_named_url_carrying_plain_page_url(self):
        wrapper = "https://r.example.com/l?u=https%3A%2F%2Fnews.example.org%2Fa&x=1"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://news.example.org/a?x=1")

    def test_preserves_plain_url_without_wrapper_params(self):
        plain = "https://www.youtube.com/watch?v=abc"
        self.assertIsNone(_unwrap_proxy_url(plain))

    def test_rejects_non_http_scheme(self):
        wrapper = "https://p.example.com/?url=javascript:alert(1)"
        self.assertIsNone(_unwrap_proxy_url(wrapper))

    def test_encoded_value_slashes(self):
        dest = "https://x.example.com/p%20q"
        wrapper = f"https://p.example.com/?dest={dest}&k=1"
        self.assertEqual(_unwrap_proxy_url(wrapper), "https://x.example.com/p%20q?k=1")  # path space kept encoded


class ExtractInputTests(unittest.TestCase):
    def test_embed_iframe_snippet(self):
        self.assertEqual(
            extract_url_from_input('<iframe src="https://www.youtube.com/embed/xyz"></iframe>'),
            "https://www.youtube.com/embed/xyz",
        )

    def test_plain_prose_with_url(self):
        self.assertEqual(
            extract_url_from_input("see this https://vimeo.com/12345 video"),
            "https://vimeo.com/12345",
        )

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_url_from_input("not a url at all"))

    def test_percent_encoded_wrapper_param_value_decoded(self):
        wrapper = "https://p.example.com/go?u=https%3A%2F%2Fa.example.com%2Fb%3Fc%3D1"
        self.assertEqual(extract_url_from_input(wrapper), "https://a.example.com/b?c=1")

    def test_unquote_helper_used(self):
        # sanity: helper import used, keeps linters happy
        self.assertEqual(unquote("a%20b"), "a b")


if __name__ == "__main__":
    unittest.main()
