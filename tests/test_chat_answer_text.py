"""Which text from a chat agent's tool loop becomes the posted reply."""

from __future__ import annotations

from typing import Any

from fcb.agents import member_tools
from fcb.agents.tool_policy import TOOL_POLICY, Access
from fcb.agents.voice import _chat_answer_text


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text: str = "", *tools: str) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [{"text": text}] if text else []
    blocks += [{"toolUse": {"toolUseId": t, "name": t, "input": {}}} for t in tools]
    return {"role": "assistant", "content": blocks}


def _result() -> dict[str, Any]:
    return {"role": "user", "content": [{"toolResult": {"toolUseId": "x", "content": []}}]}


def test_strategy_written_next_to_card_tool_is_kept() -> None:
    """The production failure: answer + show_bingo_card in one turn, empty final turn."""
    messages = [
        _user("review my cards and give me a strategy"),
        _assistant("Let me pull your cards.", "bingo_overview"),
        _result(),
        _assistant("Here's the plan: finish Chest first.", "show_bingo_card"),
        _result(),
        _assistant(),
    ]
    assert _chat_answer_text(messages) == "Here's the plan: finish Chest first."


def test_lookup_narration_is_dropped() -> None:
    messages = [
        _user("q"),
        _assistant("Let me check your cards.", "bingo_card", "bingo_card"),
        _result(),
        _assistant("Final answer."),
    ]
    assert _chat_answer_text(messages) == "Final answer."


def test_text_next_to_mixed_tools_is_dropped() -> None:
    messages = [
        _user("q"),
        _assistant("Checking and posting.", "bingo_card", "show_bingo_card"),
        _result(),
        _assistant("Done."),
    ]
    assert _chat_answer_text(messages) == "Done."


def test_answer_before_and_after_posting_tool_are_joined() -> None:
    messages = [
        _user("q"),
        _assistant("Plan part one.", "show_leaderboard"),
        _result(),
        _assistant("Plan part two."),
    ]
    assert _chat_answer_text(messages) == "Plan part one.\n\nPlan part two."


def test_no_tools_plain_reply() -> None:
    assert _chat_answer_text([_user("hi"), _assistant("hey!")]) == "hey!"


def test_nothing_written_returns_empty() -> None:
    messages = [_user("show my card"), _assistant("", "show_bingo_card"), _result(), _assistant()]
    assert _chat_answer_text(messages) == ""


def test_posting_tools_are_member_tools() -> None:
    for name in member_tools.POSTING_TOOLS:
        assert TOOL_POLICY[name] is Access.MEMBER, name
