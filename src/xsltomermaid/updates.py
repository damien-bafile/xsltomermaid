"""Check GitHub for a newer release of the app.

Free of Qt so it can be unit-tested; the GUI runs :func:`fetch_latest_release`
on a worker thread because it does network I/O.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

REPOSITORY = "damien-bafile/xsltomermaid"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
# "latest" skips drafts and pre-releases, which is what an update check wants.
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


@dataclass
class Release:
    tag: str  # e.g. "v0.11.1"
    name: str
    url: str  # the release's page on GitHub
    notes: str = ""


class UpdateCheckError(Exception):
    """The latest release couldn't be determined (offline, rate-limited, …)."""


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.11.1"`` -> ``(0, 11, 1)``; a suffix such as ``-rc1`` is ignored.

    Returns ``()`` when the text has no leading version number.
    """
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "", re.IGNORECASE)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str = __version__) -> bool:
    """Whether release ``latest`` is a higher version than ``current``."""
    new, old = parse_version(latest), parse_version(current)
    if not new or not old:
        return False
    # Pad so "0.12" compares equal to "0.12.0".
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def fetch_latest_release(timeout: float = 10.0) -> Release:
    """Ask the GitHub API for the newest published release.

    Raises :class:`UpdateCheckError` with a user-presentable message on any
    network, HTTP or parsing failure.
    """
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"xsltomermaid/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateCheckError("No releases have been published yet.") from exc
        if exc.code in (403, 429):
            raise UpdateCheckError(
                "GitHub's rate limit was reached. Try again in a few minutes."
            ) from exc
        raise UpdateCheckError(f"GitHub returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateCheckError(f"Couldn't reach GitHub ({reason}).") from exc
    except ValueError as exc:
        raise UpdateCheckError("GitHub sent an unreadable response.") from exc

    tag = str(data.get("tag_name") or "")
    if not parse_version(tag):
        raise UpdateCheckError(f"The latest release has no version tag ({tag!r}).")
    return Release(
        tag=tag,
        name=str(data.get("name") or tag),
        url=str(data.get("html_url") or RELEASES_PAGE),
        notes=str(data.get("body") or ""),
    )
