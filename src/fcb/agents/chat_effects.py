"""Side effects requested by chat tools, carried back to the bot for posting.

Chat tools run in a worker thread inside the Strands agent loop, so they cannot post to
Discord themselves. Instead they record what should happen here (card images to post,
a leaderboard to post, owner changes awaiting confirmation) and the bot acts on it after
the agent returns. Caps keep a manipulated model from flooding the channel.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

MAX_CARDS_PER_REPLY = 2
MAX_PROPOSALS_PER_REPLY = 10


@dataclass(frozen=True)
class ProposedChange:
    """An owner write that has been validated but not applied.

    ``kind`` selects the apply function in ``fcb.agents.owner.apply_change``; ``params``
    holds already-resolved ids, never names. ``summary`` is shown on the confirm prompt.
    """

    kind: str
    summary: str
    params: dict[str, Any]


@dataclass
class ChatEffects:
    cards: list[tuple[str, str]] = field(default_factory=list)  # (user_id, display_name)
    leaderboard: bool = False
    proposals: list[ProposedChange] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_card(self, user_id: str, display_name: str) -> str:
        """Queue one member's card image. Returns a status line for the model."""
        with self._lock:
            if any(uid == user_id for uid, _ in self.cards):
                return f"{display_name}'s card is already queued."
            if len(self.cards) >= MAX_CARDS_PER_REPLY:
                return f"Limit reached: at most {MAX_CARDS_PER_REPLY} card images per reply."
            self.cards.append((user_id, display_name))
        return f"{display_name}'s card image will be posted right after your reply."

    def request_leaderboard(self) -> str:
        with self._lock:
            self.leaderboard = True
        return "The leaderboard will be posted right after your reply."

    def add_proposal(self, change: ProposedChange) -> bool:
        with self._lock:
            if len(self.proposals) >= MAX_PROPOSALS_PER_REPLY:
                return False
            self.proposals.append(change)
        return True
