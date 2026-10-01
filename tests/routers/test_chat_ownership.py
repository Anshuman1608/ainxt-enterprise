# SPDX-License-Identifier: MIT
"""D92/D93 — what the shadowed handlers held that the live ones did not.

`gateway.py` registered `GET /chats/{chat_id}/messages` with an ownership
check, but `chat_router` claimed the same path first, and its copy had none:
any logged-in user could read any chat by id. `delete_chat` had the same gap.
Every other `chat_id` endpoint in `chat_router` already filters on the owner.

The dead `feedback_router` POST stored the thumbs-down modal's fields; the
live one dropped them. The live `GET /auth/sso/provider` reported SSO enabled
when it was off.

The fake session EVALUATES the filter criteria. One that ignored them — the
usual shape — would pass with the ownership filter deleted.
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
OWNER = "11111111-1111-1111-1111-111111111111"
INTRUDER = "22222222-2222-2222-2222-222222222222"
CHAT_ID = "33333333-3333-3333-3333-333333333333"


class _Row(types.SimpleNamespace):
    """A table row; any column the test did not set reads as NULL."""

    def __getattr__(self, _name):
        return None


def _matches(row, criterion) -> bool:
    key = getattr(criterion.left, "key", None)
    value = getattr(criterion.right, "value", None)
    op = getattr(criterion.operator, "__name__", "")
    actual = getattr(row, key, None)
    if op == "in_op":
        return actual in (value or [])
    return str(actual) == str(value)


class _Query:
    def __init__(self, session, rows):
        self._s, self._rows = session, list(rows)

    def filter(self, *criteria):
        return _Query(self._s, [r for r in self._rows
                                if all(_matches(r, c) for c in criteria)])

    def order_by(self, *_a):
        return self

    def limit(self, _n):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, tables):
        self.tables, self.deleted, self.added, self.committed = tables, [], [], False

    def query(self, model):
        return _Query(self, self.tables.get(model.__name__, []))

    def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def refresh(self, _obj):
        pass

    def close(self):
        pass


@pytest.fixture
def session(monkeypatch):
    import db.database
    chat = _Row(id=CHAT_ID, user_id=OWNER)
    msg = _Row(id="m1", chat_id=CHAT_ID, role="user", content="secret", attachment_ids=[])
    s = _Session({"Chat": [chat], "ChatMessage": [msg]})
    monkeypatch.setattr(db.database, "SessionLocal", lambda: s, raising=True)
    return s


def _client(user_id: str) -> TestClient:
    from auth.dependencies import get_current_user
    from routers import chat_router

    app = FastAPI()
    app.include_router(chat_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": user_id, "user_id": user_id, "role": "user"}
    return TestClient(app)


# ── ownership ───────────────────────────────────────────────────────────────


def test_the_owner_reads_their_chat(session):
    r = _client(OWNER).get(f"/api/chats/{CHAT_ID}/messages")
    assert r.status_code == 200
    assert [m["content"] for m in r.json()["messages"]] == ["secret"]


def test_another_user_cannot_read_it(session):
    r = _client(INTRUDER).get(f"/api/chats/{CHAT_ID}/messages")
    assert r.status_code == 404
    assert "secret" not in r.text


def test_another_user_cannot_delete_it(session):
    r = _client(INTRUDER).delete(f"/api/chats/{CHAT_ID}")
    assert r.status_code == 404
    assert session.deleted == [] and not session.committed


def test_the_owner_can_delete_it(session):
    r = _client(OWNER).delete(f"/api/chats/{CHAT_ID}")
    assert r.status_code == 200 and len(session.deleted) == 1


def _functions_missing_an_owner_check() -> list[str]:
    tree = ast.parse((ROOT / "routers" / "chat_router.py").read_text(
        encoding="utf-8", errors="replace"))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        looks_up_by_id = any(
            isinstance(n, ast.Compare)
            and isinstance(n.left, ast.Attribute) and n.left.attr == "id"
            and isinstance(n.left.value, ast.Name) and n.left.value.id == "Chat"
            and any(isinstance(c, ast.Name) and c.id == "chat_id" for c in n.comparators)
            for n in ast.walk(fn))
        checks_owner = any(
            isinstance(n, ast.Attribute) and n.attr == "user_id"
            for c in ast.walk(fn) if isinstance(c, ast.Compare)
            for n in ast.walk(c))
        if looks_up_by_id and not checks_owner:
            out.append(fn.name)
    return out


def test_every_chat_lookup_checks_the_owner():
    """Either in the filter (`Chat.user_id == user_id`) or after the fetch
    (`row.user_id != user_id`). Two handlers did neither."""
    assert _functions_missing_an_owner_check() == []


# ── the feedback fields (D93) ───────────────────────────────────────────────


def test_the_thumbs_down_fields_are_stored(session, monkeypatch):
    import db.models

    captured = {}

    class _FB:
        message_id = types.SimpleNamespace(key="message_id")
        user_id = types.SimpleNamespace(key="user_id")

        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(db.models, "MessageFeedback", _FB, raising=True)
    monkeypatch.setattr(_Query, "delete", lambda self, **k: 0, raising=False)
    monkeypatch.setattr(_Session, "query", lambda self, model: _Query(self, []), raising=True)

    r = _client(OWNER).post("/api/chat/messages/m1/feedback", json={
        "rating": -1, "rag_mode": "off", "issue": "inaccurate", "sub_issue": "math",
        "comment": "c" * 1500, "user_prompt": "p" * 2500, "assistant_summary": "a" * 1500,
    })
    assert r.status_code == 200, r.text
    assert captured["issue"] == "inaccurate" and captured["sub_issue"] == "math"
    assert len(captured["comment"]) == 1000
    assert len(captured["user_prompt"]) == 2000
    assert len(captured["assistant_summary"]) == 1000


# ── SSO (D93) ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider,enabled", [("none", False), ("keycloak", True)])
def test_sso_is_reported_enabled_only_when_it_is(monkeypatch, provider, enabled):
    import auth.sso
    from routers.auth_router import sso_provider_info

    monkeypatch.setattr(auth.sso, "get_sso_provider", lambda: provider, raising=True)
    assert sso_provider_info() == {"provider": provider, "enabled": enabled}
