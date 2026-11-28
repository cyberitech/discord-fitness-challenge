"""Who may call which chat tool. The single source of truth for chat tool access.

Every chat tool MUST be listed in ``TOOL_POLICY``. Access is enforced at three
independent layers so one mistake cannot hand a write tool to a member:

1. Construction: ``build_chat_tools`` only builds owner tools for the owner's Discord id,
   and refuses to return any tool that the policy does not allow for the invoker
   (including tools missing from the policy).
2. Call time: ``ToolGate`` is a Strands hook that re-checks every tool call against the
   policy and the invoker's id and cancels anything not allowed.
3. Execution: every owner tool re-checks the id it was bound to, and owner changes are
   only applied by ``owner.apply_change`` after the owner presses Confirm.

``tests/test_tool_policy.py`` cross-checks the policy against the tool modules.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from fcb.agents import member_tools, owner
from fcb.agents.chat_effects import ChatEffects
from fcb.db import dao

logger = logging.getLogger(__name__)


class Access(Enum):
    MEMBER = "member"  # anyone in the channel; read-only or channel-posting only
    OWNER = "owner"  # only config.FCB_OWNER_USER_ID


TOOL_POLICY: dict[str, Access] = {
    # member_tools.build_member_tools
    "find_member": Access.MEMBER,
    "bingo_overview": Access.MEMBER,
    "bingo_card": Access.MEMBER,
    "show_bingo_card": Access.MEMBER,
    "show_leaderboard": Access.MEMBER,
    # owner.build_owner_tools
    "member_report": Access.OWNER,
    "set_bingo_square": Access.OWNER,
    "set_submission_status": Access.OWNER,
    "set_submission_stat": Access.OWNER,
}


def is_allowed(tool_name: str, invoker_id: str | None) -> bool:
    """Fail closed: unknown tools and unknown invokers are never allowed."""
    access = TOOL_POLICY.get(tool_name)
    if access is None or not invoker_id:
        return False
    if access is Access.MEMBER:
        return True
    return access is Access.OWNER and owner.is_owner(invoker_id)


def tool_name(t: Any) -> str:
    return str(getattr(t, "tool_name", None) or getattr(t, "__name__", repr(t)))


def build_chat_tools(guild_id: str, invoker_id: str, effects: ChatEffects) -> list[Any]:
    """Build the tool list for one @-mention reply from ``invoker_id``."""
    is_owner = owner.is_owner(invoker_id)
    tools = member_tools.build_member_tools(guild_id, effects, scope_to_guild=not is_owner)
    if is_owner:
        tools += owner.build_owner_tools(guild_id, invoker_id, effects)
    names = [tool_name(t) for t in tools]
    denied = [n for n in names if not is_allowed(n, invoker_id)]
    if denied or len(set(names)) != len(names):
        raise RuntimeError(
            f"chat tool set violates TOOL_POLICY for {invoker_id}: denied={denied} names={names}"
        )
    return tools


class ToolGate(HookProvider):
    """Strands hook: re-check every tool call against ``TOOL_POLICY`` and audit it."""

    def __init__(self, *, guild_id: str, invoker_id: str) -> None:
        self.guild_id = guild_id
        self.invoker_id = invoker_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check)

    def check(self, event: BeforeToolCallEvent) -> None:
        name = str(event.tool_use.get("name", ""))
        args = json.dumps(event.tool_use.get("input", {}), default=str)[:500]
        allowed = is_allowed(name, self.invoker_id)
        if not allowed:
            event.cancel_tool = f"Tool {name!r} is not available to this user."
            logger.warning(f"tool.denied name={name} invoker={self.invoker_id} args={args}")
        self._audit(
            "WARNING" if not allowed else "INFO",
            "tool.denied" if not allowed else "tool.call",
            name,
            args,
        )

    def _audit(self, level: str, category: str, name: str, args: str) -> None:
        try:
            dao.record_event(
                level=level,
                category=category,
                message=f"{category} {name} by {self.invoker_id}",
                actor=self.invoker_id,
                guild_id=self.guild_id,
                context={"tool": name, "args": args},
            )
        except Exception:  # auditing must never break the reply
            logger.exception("tool audit log failed")
