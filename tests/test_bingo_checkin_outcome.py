"""When a bingo check-in asks a question, and what status it gets."""

from __future__ import annotations

import pytest

from fcb.bot.client import _bingo_checkin_outcome

Q = "Did you do 5 rounds or 4?"


@pytest.mark.parametrize(
    ("needs", "question", "matched", "expected"),
    [
        # Squares were awarded: never ask, even if the model wanted to.
        (True, Q, True, (None, "approved")),
        (False, None, True, (None, "approved")),
        (True, "", True, (None, "approved")),
        # Nothing awarded and the model has a real question: ask it.
        (True, Q, False, (Q, "pending")),
        (True, f"  {Q}  ", False, (Q, "pending")),
        # Nothing awarded, ambiguity flagged but no question text: don't sit pending.
        (True, None, False, (None, "rejected")),
        (True, "   ", False, (None, "rejected")),
        # Nothing awarded, nothing to ask.
        (False, Q, False, (None, "rejected")),
        (False, None, False, (None, "rejected")),
    ],
)
def test_outcome(
    needs: bool, question: str | None, matched: bool, expected: tuple[str | None, str]
) -> None:
    assert _bingo_checkin_outcome(needs, question, matched) == expected
