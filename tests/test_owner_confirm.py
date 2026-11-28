"""Owner confirmation buttons: only the owner can apply proposed changes, and only once."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fcb import config
from fcb.agents import owner
from fcb.agents.chat_effects import ProposedChange
from fcb.bot import client
from fcb.db import dao

OWNER = "100000000000000001"
MEMBER = "200000000000000002"
GUILD = "300000000000000003"


class _Response:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.done = False

    async def send_message(self, content: str, **kw: Any) -> None:
        self.sent.append((content, kw))
        self.done = True

    async def defer(self, **kw: Any) -> None:
        self.done = True

    async def edit_message(self, **kw: Any) -> None:
        self.done = True

    def is_done(self) -> bool:
        return self.done


class _User:
    def __init__(self, uid: str) -> None:
        self.id = int(uid)


class _Interaction:
    def __init__(self, uid: str) -> None:
        self.user = _User(uid)
        self.response = _Response()
        self.edits: list[dict[str, Any]] = []

    async def edit_original_response(self, **kw: Any) -> None:
        self.edits.append(kw)


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    monkeypatch.setattr(config, "FCB_OWNER_USER_ID", OWNER)
    monkeypatch.setattr(dao, "record_event", lambda **kw: None)
    applied: list[tuple[str, str]] = []

    def fake_apply(change: ProposedChange, *, guild_id: str, actor_id: str) -> str:
        owner._require_owner(actor_id)  # same guard as the real function
        applied.append((change.summary, actor_id))
        return f"done: {change.summary}"

    monkeypatch.setattr(owner, "apply_change", fake_apply)
    return applied


def _view() -> Any:
    changes = [ProposedChange("submission_status", "approve #1", {})]
    return client._OwnerConfirmView(guild_id=GUILD, owner_id=OWNER, changes=changes)


def test_non_owner_cannot_press_the_buttons(setup: list[tuple[str, str]]) -> None:
    async def run() -> None:
        view = _view()
        interaction = _Interaction(MEMBER)
        assert await view.interaction_check(interaction) is False
        assert interaction.response.sent and interaction.response.sent[0][1]["ephemeral"]

    asyncio.run(run())
    assert setup == []


def test_owner_confirm_applies_once(setup: list[tuple[str, str]]) -> None:
    async def run() -> None:
        view = _view()
        first, second = _Interaction(OWNER), _Interaction(OWNER)
        assert await view.interaction_check(first) is True
        await view.confirm.callback(first)
        await view.confirm.callback(second)  # double click
        assert "Applied" in first.edits[0]["content"]

    asyncio.run(run())
    assert setup == [("approve #1", OWNER)]


def test_cancel_applies_nothing(setup: list[tuple[str, str]]) -> None:
    async def run() -> None:
        view = _view()
        await view.cancel.callback(_Interaction(OWNER))
        await view.confirm.callback(_Interaction(OWNER))

    asyncio.run(run())
    assert setup == []


def test_view_bound_to_owner_rejects_owner_change(
    setup: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the configured owner changes after the prompt was posted, the old owner is refused."""

    async def run() -> None:
        view = _view()
        monkeypatch.setattr(config, "FCB_OWNER_USER_ID", "999")
        assert await view.interaction_check(_Interaction(OWNER)) is False

    asyncio.run(run())
    assert setup == []
