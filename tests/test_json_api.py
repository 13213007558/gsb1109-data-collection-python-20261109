"""Tests for the JSON feed parser entry points and parse() options."""

import io
import json

import responses

import feedparser

DOC = {
    "version": "https://jsonfeed.org/version/1.1",
    "title": "JSON Feed",
    "items": [
        {
            "id": "1",
            "content_text": "Plain text.",
            "content_html": '<p>See <a href="/about">about</a>.</p><script>alert(1)</script>',
            "date_modified": "2021-06-01T10:00:00Z",
        }
    ],
}

HEADERS = {
    "content-type": "application/feed+json",
    "content-location": "https://example.org/feeds/main.json",
    "content-language": "en",
}


def parse_bytes(**kwargs):
    return feedparser.parse(io.BytesIO(json.dumps(DOC).encode("utf-8")), **kwargs)


def test_json_bytes_input():
    result = feedparser.parse(json.dumps(DOC).encode("utf-8"))
    assert not result.bozo
    assert result.version == "json11"
    assert result.feed.title == "JSON Feed"
    assert [c.type for c in result.entries[0].content] == ["text/plain", "text/html"]
    assert result.entries[0].updated == "2021-06-01T10:00:00Z"
    assert result.entries[0].updated_parsed[:6] == (2021, 6, 1, 10, 0, 0)


def test_json_response_headers_resolve_relative_uris():
    result = parse_bytes(response_headers=HEADERS)
    assert not result.bozo
    html = result.entries[0].content[1].value
    assert html == '<p>See <a href="https://example.org/about">about</a>.</p>'


def test_json_response_headers_content_language():
    result = parse_bytes(response_headers=HEADERS)
    assert result.feed.language == "en"
    assert result.entries[0].language == "en"
    assert result.entries[0].content[0].language == "en"


def test_json_sanitize_html_option():
    result = parse_bytes(response_headers=HEADERS, sanitize_html=False)
    assert "<script>alert(1)</script>" in result.entries[0].content[1].value


def test_json_resolve_relative_uris_option():
    result = parse_bytes(response_headers=HEADERS, resolve_relative_uris=False)
    html = result.entries[0].content[1].value
    assert html == '<p>See <a href="/about">about</a>.</p>'


def test_json_http_response():
    url = "http://example.org/feed.json"
    responses.get(
        url,
        body=json.dumps(DOC).encode("utf-8"),
        headers={"Content-Type": "application/feed+json"},
    )
    result = feedparser.parse(url)
    assert not result.bozo
    assert result.version == "json11"
    html = result.entries[0].content[1].value
    assert html == '<p>See <a href="http://example.org/about">about</a>.</p>'
