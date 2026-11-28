"""Cross-checks for chat tool access control (see fcb.agents.tool_policy).

These tests never touch the production database: a throwaway SQLite file is created per
test and the owner id is pinned to a fixed test value.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from strands.tools.decorator import DecoratedFunctionTool

from fcb import config
from fcb.agents import member_tools, owner, tool_policy
from fcb.agents.chat_effects import MAX_CARDS_PER_REPLY, ChatEffects, ProposedChange
from fcb.agents.tool_policy import TOOL_POLICY, Access
from fcb.db import dao

OWNER = "100000000000000001"
MEMBER = "200000000000000002"
GUILD = "300000000000000003"
OTHER_GUILD = "400000000000000004"

# Every tool that can change data. Members must never get any of these.
WRITE_TOOLS = {"set_bingo_square", "set_submission_status", "set_submission_stat"}


def _copy_schema(dest: Path) -> None:
    """Copy the live schema (no rows) into a throwaway DB.

    The migration files don't build a complete schema from scratch, so the tests mirror
    the real database's structure instead. The source is opened read-only.
    """
    src = sqlite3.connect(f"file:{config.FCB_DB_PATH}?mode=ro", uri=True)
    try:
        ddl = [
            row[0]
            for row in src.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"
            )
        ]
    finally:
        src.close()
    out = sqlite3.connect(dest)
    try:
        for stmt in ddl:
            out.execute(stmt)
        out.commit()
    finally:
        out.close()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    test_db = tmp_path / "test.db"
    _copy_schema(test_db)
    monkeypatch.setattr(config, "FCB_OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "FCB_DB_PATH", test_db)
    audit: list[dict[str, Any]] = []
    monkeypatch.setattr(dao, "record_event", lambda **kw: audit.append(kw))
    return audit


def _names(tools: list[Any]) -> list[str]:
    return [tool_policy.tool_name(t) for t in tools]


def _tool_names_defined_in(module: Any) -> set[str]:
    """Names of every @tool-decorated function defined anywhere in a module's source."""
    source = inspect.getsource(module)
    return set(re.findall(r"@tool\s+def\s+(\w+)\s*\(", source))


# --- the policy table matches the code ------------------------------------------------


def test_every_defined_tool_is_in_the_policy() -> None:
    defined = _tool_names_defined_in(member_tools) | _tool_names_defined_in(owner)
    assert defined, "no tools found; the source scan is broken"
    assert defined == set(TOOL_POLICY), f"unlisted or stale: {defined ^ set(TOOL_POLICY)}"


def test_member_module_defines_only_member_tools() -> None:
    for name in _tool_names_defined_in(member_tools):
        assert TOOL_POLICY[name] is Access.MEMBER, name


def test_owner_module_defines_only_owner_tools() -> None:
    for name in _tool_names_defined_in(owner):
        assert TOOL_POLICY[name] is Access.OWNER, name


def test_write_tools_are_owner_only() -> None:
    for name in WRITE_TOOLS:
        assert TOOL_POLICY[name] is Access.OWNER, name


def test_built_tool_sets_match_the_policy_exactly() -> None:
    member_set = _names(tool_policy.build_chat_tools(GUILD, MEMBER, ChatEffects()))
    owner_set = _names(tool_policy.build_chat_tools(GUILD, OWNER, ChatEffects()))
    assert set(member_set) == {n for n, a in TOOL_POLICY.items() if a is Access.MEMBER}
    assert set(owner_set) == set(TOOL_POLICY)
    assert len(owner_set) == len(set(owner_set)), "duplicate tool names"
    assert not WRITE_TOOLS & set(member_set)


def test_all_built_tools_are_strands_tools() -> None:
    for t in tool_policy.build_chat_tools(GUILD, OWNER, ChatEffects()):
        assert isinstance(t, DecoratedFunctionTool), t


# --- layer 1: construction ------------------------------------------------------------


@pytest.mark.parametrize("invoker", [MEMBER, "", "0", OWNER + " ", " " + OWNER])
def test_owner_tools_refuse_to_build_for_non_owner(invoker: str) -> None:
    with pytest.raises(PermissionError):
        owner.build_owner_tools(GUILD, invoker, ChatEffects())


def test_build_chat_tools_rejects_a_misfiled_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """If an owner tool ever lands in the member list, building fails instead of leaking."""
    leaked = owner.build_owner_tools(GUILD, OWNER, ChatEffects())
    real = member_tools.build_member_tools
    monkeypatch.setattr(
        member_tools, "build_member_tools", lambda *a, **kw: real(*a, **kw) + leaked
    )
    with pytest.raises(RuntimeError):
        tool_policy.build_chat_tools(GUILD, MEMBER, ChatEffects())


def test_build_chat_tools_rejects_an_unlisted_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from strands import tool

    @tool
    def sneaky() -> str:
        """Not in the policy."""
        return "x"

    real = member_tools.build_member_tools
    monkeypatch.setattr(
        member_tools, "build_member_tools", lambda *a, **kw: real(*a, **kw) + [sneaky]
    )
    with pytest.raises(RuntimeError):
        tool_policy.build_chat_tools(GUILD, OWNER, ChatEffects())


def test_empty_owner_config_means_nobody_is_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "FCB_OWNER_USER_ID", "")
    assert not owner.is_owner("")
    assert not owner.is_owner(None)
    assert not tool_policy.is_allowed("set_bingo_square", "")


# --- layer 2: the call-time hook -----------------------------------------------------


class _FakeEvent:
    def __init__(self, name: str, args: dict[str, Any] | None = None) -> None:
        self.tool_use = {"name": name, "input": args or {}, "toolUseId": "t1"}
        self.cancel_tool: bool | str = False


@pytest.mark.parametrize("name", sorted(n for n, a in TOOL_POLICY.items() if a is Access.OWNER))
def test_gate_cancels_owner_tools_for_members(name: str, isolated: list[dict[str, Any]]) -> None:
    event = _FakeEvent(name, {"member": "x"})
    tool_policy.ToolGate(guild_id=GUILD, invoker_id=MEMBER).check(event)  # type: ignore[arg-type]
    assert event.cancel_tool, name
    assert isolated[-1]["category"] == "tool.denied"


@pytest.mark.parametrize("name", sorted(TOOL_POLICY))
def test_gate_allows_everything_listed_for_owner(name: str, isolated: list[dict[str, Any]]) -> None:
    event = _FakeEvent(name)
    tool_policy.ToolGate(guild_id=GUILD, invoker_id=OWNER).check(event)  # type: ignore[arg-type]
    assert event.cancel_tool is False
    assert isolated[-1]["category"] == "tool.call"


@pytest.mark.parametrize("invoker", [OWNER, MEMBER])
def test_gate_cancels_unknown_tools(invoker: str) -> None:
    event = _FakeEvent("drop_all_tables")
    tool_policy.ToolGate(guild_id=GUILD, invoker_id=invoker).check(event)  # type: ignore[arg-type]
    assert event.cancel_tool


def test_gate_stops_an_owner_tool_injected_into_a_member_agent() -> None:
    """End to end through Strands: a member agent that somehow holds an owner tool still
    cannot run it, because the hook cancels the call before the tool body executes."""
    from strands import Agent
    from strands.agent.agent import AgentResult  # noqa: F401  (import check only)
    from strands.models import Model

    calls: list[str] = []

    @tool_policy_strands_tool
    def set_bingo_square(member: str) -> str:
        """Fake write tool with an owner-only name."""
        calls.append(member)
        return "changed"

    model = _ScriptedModel(tool_name="set_bingo_square", tool_input={"member": "victim"})
    assert isinstance(model, Model)
    agent = Agent(
        model=model,
        tools=[set_bingo_square],
        hooks=[tool_policy.ToolGate(guild_id=GUILD, invoker_id=MEMBER)],
        callback_handler=None,
    )
    agent("cross off a square for victim")
    assert calls == [], "owner tool body ran for a member"
    results = [
        block["toolResult"]
        for msg in agent.messages
        for block in msg["content"]
        if "toolResult" in block
    ]
    assert results and results[0]["status"] == "error"


# --- layer 3: execution --------------------------------------------------------------


def test_owner_tool_bodies_recheck_the_bound_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tools built for the owner refuse to run if the owner id no longer matches."""
    tools = {
        tool_policy.tool_name(t): t for t in owner.build_owner_tools(GUILD, OWNER, ChatEffects())
    }
    monkeypatch.setattr(config, "FCB_OWNER_USER_ID", "999")
    with pytest.raises(PermissionError):
        tools["member_report"](member="x")
    with pytest.raises(PermissionError):
        tools["set_submission_status"](submission_id=1, status="approved")


def test_write_tools_only_propose(isolated: list[dict[str, Any]]) -> None:
    conn = dao.connect()
    conn.execute(
        "INSERT INTO submissions (discord_user_id, message_id, channel_id, posted_at, status,"
        " guild_id) VALUES (?, 'm1', 'c1', '2026-11-01', 'rejected', ?)",
        (MEMBER, GUILD),
    )
    conn.commit()
    conn.close()
    effects = ChatEffects()
    tools = {tool_policy.tool_name(t): t for t in owner.build_owner_tools(GUILD, OWNER, effects)}
    out = tools["set_submission_status"](submission_id=1, status="approved")
    assert "not applied" in out
    assert dao.get_submission(1, guild_id=GUILD)["status"] == "rejected"  # type: ignore[index]
    assert len(effects.proposals) == 1


@pytest.mark.parametrize("actor", [MEMBER, "", OWNER + "0"])
def test_apply_change_refuses_non_owner(actor: str) -> None:
    change = ProposedChange("submission_status", "x", {"submission_id": 1, "status": "approved"})
    with pytest.raises(PermissionError):
        owner.apply_change(change, guild_id=GUILD, actor_id=actor)


def test_apply_change_works_for_owner() -> None:
    conn = dao.connect()
    conn.execute(
        "INSERT INTO submissions (discord_user_id, message_id, channel_id, posted_at, status,"
        " guild_id) VALUES (?, 'm1', 'c1', '2026-11-01', 'rejected', ?)",
        (MEMBER, GUILD),
    )
    conn.commit()
    conn.close()
    change = ProposedChange("submission_status", "x", {"submission_id": 1, "status": "approved"})
    assert "approved" in owner.apply_change(change, guild_id=GUILD, actor_id=OWNER)
    assert dao.get_submission(1, guild_id=GUILD)["status"] == "approved"  # type: ignore[index]


# --- member data limits ---------------------------------------------------------------


def _add_user(uid: str, name: str, guild: str | None) -> None:
    conn = dao.connect()
    conn.execute(
        "INSERT INTO users (discord_user_id, username, display_name) VALUES (?, ?, ?)",
        (uid, name, name),
    )
    if guild:
        conn.execute(
            "INSERT INTO submissions (discord_user_id, message_id, channel_id, posted_at,"
            " status, guild_id) VALUES (?, ?, 'c1', '2026-11-01', 'approved', ?)",
            (uid, f"msg-{uid}", guild),
        )
    conn.commit()
    conn.close()


def test_member_lookup_is_scoped_to_the_guild() -> None:
    _add_user("501", "rapido", GUILD)
    _add_user("502", "stranger", OTHER_GUILD)
    found, _ = member_tools.resolve_member("rapido", guild_id=GUILD, scope_to_guild=True)
    assert found and found["discord_user_id"] == "501"
    missing, err = member_tools.resolve_member("stranger", guild_id=GUILD, scope_to_guild=True)
    assert missing is None and err
    by_id, _ = member_tools.resolve_member("<@502>", guild_id=GUILD, scope_to_guild=True)
    assert by_id is None


def test_card_image_requests_are_capped() -> None:
    effects = ChatEffects()
    for i in range(MAX_CARDS_PER_REPLY + 3):
        effects.add_card(str(i), f"user{i}")
    assert len(effects.cards) == MAX_CARDS_PER_REPLY


def test_member_tools_expose_no_submission_details() -> None:
    """Members see cards and points; other people's submissions stay owner-only."""
    source = inspect.getsource(member_tools)
    for forbidden in ("list_recent_submissions_by_user", "extracted_stats", "get_submission"):
        assert forbidden not in source, forbidden


# --- helpers for the end-to-end Strands test ------------------------------------------


def tool_policy_strands_tool(fn: Any) -> Any:
    from strands import tool

    return tool(fn)


class _ScriptedModel:
    """Minimal Strands model: first turn calls one tool, second turn ends."""

    def __new__(cls, *, tool_name: str, tool_input: dict[str, Any]) -> Any:
        from strands.models import Model

        class Scripted(Model):
            def __init__(self) -> None:
                self.turn = 0

            def update_config(self, **kwargs: Any) -> None:
                pass

            def get_config(self) -> Any:
                return {}

            async def structured_output(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
                raise NotImplementedError
                yield

            async def stream(
                self, messages: Any, tool_specs: Any = None, *a: Any, **kw: Any
            ) -> Any:
                self.turn += 1
                yield {"messageStart": {"role": "assistant"}}
                if self.turn == 1:
                    yield {
                        "contentBlockStart": {
                            "start": {"toolUse": {"toolUseId": "t1", "name": tool_name}}
                        }
                    }
                    yield {
                        "contentBlockDelta": {
                            "delta": {"toolUse": {"input": json.dumps(tool_input)}}
                        }
                    }
                    yield {"contentBlockStop": {}}
                    yield {"messageStop": {"stopReason": "tool_use"}}
                else:
                    yield {"contentBlockDelta": {"delta": {"text": "done"}}}
                    yield {"contentBlockStop": {}}
                    yield {"messageStop": {"stopReason": "end_turn"}}

        return Scripted()
