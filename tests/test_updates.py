"""Tests for the GitHub update check (network calls are mocked)."""

import io
import json
import urllib.error

import pytest

import xsltomermaid
from xsltomermaid import updates
from xsltomermaid.updates import (
    UpdateCheckError,
    fetch_latest_release,
    is_newer,
    parse_version,
)


def test_parse_version():
    assert parse_version("v0.11.1") == (0, 11, 1)
    assert parse_version("0.11.1") == (0, 11, 1)
    assert parse_version("V1.2") == (1, 2)
    assert parse_version("v2.0.0-rc1") == (2, 0, 0)
    assert parse_version("latest") == ()
    assert parse_version("") == ()


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("v0.11.2", "0.11.1", True),
        ("v0.12.0", "0.11.9", True),
        ("v1.0.0", "0.99.99", True),
        ("v0.10.0", "0.10.0", False),  # same version
        ("v0.12", "0.12.0", False),  # padded equal
        ("v0.9.2", "0.11.1", False),  # older release
        ("v0.11.10", "0.11.9", True),  # numeric, not string, comparison
        ("nightly", "0.11.1", False),  # unparseable tag
    ],
)
def test_is_newer(latest, current, expected):
    assert is_newer(latest, current) is expected


def test_package_version_matches_pyproject_dynamic_source():
    # pyproject reads the version from __version__, so it must be parseable.
    assert parse_version(xsltomermaid.__version__)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload=None, error=None):
    def urlopen(request, timeout=None):
        assert request.full_url == updates.LATEST_RELEASE_API
        assert request.get_header("User-agent").startswith("xsltomermaid/")
        if error is not None:
            raise error
        return _Response(json.dumps(payload).encode())

    return urlopen


def test_fetch_latest_release(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _fake_urlopen({
            "tag_name": "v0.12.0",
            "name": "v0.12.0",
            "html_url": "https://github.com/damien-bafile/xsltomermaid/releases/tag/v0.12.0",
            "body": "notes",
        }),
    )
    release = fetch_latest_release()
    assert release.tag == "v0.12.0"
    assert release.url.endswith("/v0.12.0")
    assert release.notes == "notes"


@pytest.mark.parametrize(
    "error,message",
    [
        (urllib.error.HTTPError("u", 404, "Not Found", {}, None), "No releases"),
        (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), "rate limit"),
        (urllib.error.HTTPError("u", 500, "Oops", {}, None), "HTTP 500"),
        (urllib.error.URLError("offline"), "Couldn't reach GitHub"),
        (TimeoutError("timed out"), "Couldn't reach GitHub"),
    ],
)
def test_fetch_latest_release_errors(monkeypatch, error, message):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen(error=error))
    with pytest.raises(UpdateCheckError, match=message):
        fetch_latest_release()


def test_fetch_latest_release_rejects_unversioned_tag(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", _fake_urlopen({"tag_name": "nightly"})
    )
    with pytest.raises(UpdateCheckError, match="no version tag"):
        fetch_latest_release()
