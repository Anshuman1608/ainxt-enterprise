# SPDX-License-Identifier: MIT
"""R18 — API-key requests must be classified as the "api" client source.

Background. `capabilities.channels` is the mechanism replacing three per-SKU
channel feature flags (ENABLE_CHAT_OPUS, ENABLE_CLI_OPUS_48,
ENABLE_CLI_OPUS_5). It only works if `request.state.client_source` is accurate.
Before this change an OpenAI-SDK caller sent `User-Agent: OpenAI/Python 1.x`,
matched none of the User-Agent patterns, and fell through to "platform" — so
channel restrictions silently did not apply to API callers at all. Retiring
working flags onto a silently-bypassed mechanism would be a governance
regression, which is why this is a prerequisite for Phase 8.

The non-regression tests below are the important ones: CLI and IDE clients
authenticate with API keys too, so a naive "API key ⇒ api" rule would
misclassify them. The detection deliberately sits AFTER the explicit-header
and IDE-path checks so it can only ever narrow the "platform" default.
"""

from __future__ import annotations

import pytest

from middleware.client_source_middleware import (
    CLIENT_API,
    CLIENT_CLI,
    CLIENT_DESKTOP,
    CLIENT_IDE_VSCODE,
    CLIENT_PLATFORM,
    _detect,
)

# Shape that auth.api_key_auth.is_api_key() recognises: at least one hyphen,
# fewer than two dots (a JWT has exactly two).
_API_KEY = "ainxt-3f6c1e02-9a4d-4c71-8b25-77c0de11ab99"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1LTEifQ.c2lnbmF0dXJl"


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    """Minimal duck-type — _detect() reads only .headers and .url.path."""

    def __init__(self, headers: dict | None = None, path: str = "/ainxt/v1/api/ask") -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.url = _FakeURL(path)


# ── The fix ──────────────────────────────────────────────────────────────────


def test_api_key_bearer_is_classified_api() -> None:
    """An SDK client with no client header must be tagged `api`, not `platform`."""
    req = _FakeRequest({"authorization": f"Bearer {_API_KEY}",
                        "user-agent": "OpenAI/Python 1.109.1"})
    assert _detect(req) == CLIENT_API


def test_api_key_detection_is_case_insensitive_on_scheme() -> None:
    req = _FakeRequest({"authorization": f"bearer {_API_KEY}"})
    assert _detect(req) == CLIENT_API


def test_jwt_bearer_is_not_api() -> None:
    """A browser session carries a JWT, not an API key — still `platform`."""
    req = _FakeRequest({"authorization": f"Bearer {_JWT}",
                        "user-agent": "Mozilla/5.0"})
    assert _detect(req) == CLIENT_PLATFORM


def test_no_auth_header_is_platform() -> None:
    assert _detect(_FakeRequest({"user-agent": "Mozilla/5.0"})) == CLIENT_PLATFORM


# ── NON-REGRESSION: the tests that actually matter ───────────────────────────
#
# CLI, IDE, and desktop clients all authenticate with API keys. If any of these
# start returning CLIENT_API, real clients have been misclassified and their
# usage will be attributed to the wrong channel in model_usages.


def test_cli_with_api_key_stays_cli() -> None:
    req = _FakeRequest({"x-ainxt-client": "cli/1.2.0",
                        "authorization": f"Bearer {_API_KEY}"})
    assert _detect(req) == CLIENT_CLI


def test_ide_header_with_api_key_stays_ide() -> None:
    req = _FakeRequest({"x-ainxt-client": "ide-vscode/0.9",
                        "authorization": f"Bearer {_API_KEY}"})
    assert _detect(req) == CLIENT_IDE_VSCODE


def test_ide_path_with_api_key_stays_ide() -> None:
    """The /ide/* path check precedes API-key detection."""
    req = _FakeRequest({"authorization": f"Bearer {_API_KEY}"},
                       path="/ainxt/v1/api/ide/ask")
    assert _detect(req) == CLIENT_IDE_VSCODE


def test_desktop_surface_with_api_key_stays_desktop() -> None:
    req = _FakeRequest({"x-ainxt-surface": "desktop",
                        "authorization": f"Bearer {_API_KEY}"})
    assert _detect(req) == CLIENT_DESKTOP


def test_cli_user_agent_with_api_key_stays_cli() -> None:
    """UA heuristics run before API-key detection, so ainxt-cli wins."""
    req = _FakeRequest({"user-agent": "ainxt-cli/2.0",
                        "authorization": f"Bearer {_API_KEY}"})
    assert _detect(req) == CLIENT_CLI


@pytest.mark.parametrize(
    "ua, expected",
    [("curl/8.4.0", CLIENT_API), ("python-requests/2.32", CLIENT_API)],
)
def test_existing_ua_classification_unchanged(ua: str, expected: str) -> None:
    """Clients already classified by User-Agent must be unaffected."""
    assert _detect(_FakeRequest({"user-agent": ua})) == expected


# ── Kill-switch ──────────────────────────────────────────────────────────────


def test_detection_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLIENT_SOURCE_API_DETECTION=false restores the previous behaviour.

    The flag is read at import time, so this patches the resolved module
    constant rather than the environment.
    """
    import middleware.client_source_middleware as mod

    monkeypatch.setattr(mod, "_API_KEY_DETECTION", False)
    req = _FakeRequest({"authorization": f"Bearer {_API_KEY}",
                        "user-agent": "OpenAI/Python 1.109.1"})
    assert mod._detect(req) == CLIENT_PLATFORM


def test_malformed_authorization_header_does_not_raise() -> None:
    """Classification sits on every request — it must never 500 one."""
    for bad in ["", "Bearer", "Bearer    ", "Basic dXNlcjpwYXNz", "garbage"]:
        assert _detect(_FakeRequest({"authorization": bad})) == CLIENT_PLATFORM
