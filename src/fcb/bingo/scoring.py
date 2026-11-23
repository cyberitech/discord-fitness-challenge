"""Pure bingo board + scoring engine. No I/O, no database, no AI.

Every function here is deterministic and unit-testable in isolation. The check-in
pipeline, the card renderer, and the status surfaces all depend on this module so the
rules live in exactly one place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Sequence, TypeVar

CARD_KEYS: tuple[str, ...] = (
    "chest_shoulders",
    "back",
    "legs",
    "arms",
    "core",
    "cardio_conditioning",
)

CARD_LABELS: dict[str, str] = {
    "chest_shoulders": "Chest & Shoulders",
    "back": "Back",
    "legs": "Legs",
    "arms": "Arms",
    "core": "Core",
    "cardio_conditioning": "Cardio & Conditioning",
}

GRID_SIZE = 5
CELL_COUNT = GRID_SIZE * GRID_SIZE  # 25
BOARD_TASK_COUNT = CELL_COUNT  # 25 drawn from the pool per card


def is_card_key(value: str) -> bool:
    return value in CARD_KEYS


def row_of(cell_index: int) -> int:
    return cell_index // GRID_SIZE


def col_of(cell_index: int) -> int:
    return cell_index % GRID_SIZE


# ---------------------------------------------------------------------------------------
# Deterministic board generation
# ---------------------------------------------------------------------------------------
#
# A player's board is never stored. It is derived at any time from the triple
# (event_id, discord_user_id, card_key) over the event's FROZEN, position-ordered task
# pool. The same inputs always yield the same 25-of-N selection and placement, which is
# why the pool must be locked when the event opens.


def board_seed(event_id: int | str, user_id: str, card_key: str) -> str:
    return f"fcb-bingo:{event_id}:{user_id}:{card_key}"


class _SeededRng:
    """Deterministic uint32 stream seeded by a string, independent of Python's random."""

    def __init__(self, seed: str) -> None:
        self._seed = seed
        self._counter = 0
        self._pool = b""
        self._offset = 0

    def _refill(self) -> None:
        self._pool = hashlib.sha256(f"{self._seed}:{self._counter}".encode()).digest()
        self._counter += 1
        self._offset = 0

    def next_u32(self) -> int:
        if self._offset + 4 > len(self._pool):
            self._refill()
        value = int.from_bytes(self._pool[self._offset : self._offset + 4], "big")
        self._offset += 4
        return value

    def bounded_int(self, bound: int) -> int:
        """Uniform integer in [0, bound) using rejection sampling to avoid modulo bias."""
        if bound <= 1:
            return 0
        limit = (0x1_0000_0000 // bound) * bound
        value = self.next_u32()
        while value >= limit:
            value = self.next_u32()
        return value % bound


T = TypeVar("T")


def derive_board(event_id: int | str, user_id: str, card_key: str, pool: Sequence[T]) -> list[T]:
    """Deterministically select and place BOARD_TASK_COUNT tasks for one card.

    `pool` MUST be ordered by its stable position and hold at least BOARD_TASK_COUNT
    entries. The returned list is indexed by cell (0..24, row = index // 5,
    col = index % 5). One Fisher-Yates shuffle supplies both selection and placement.
    """
    if len(pool) < BOARD_TASK_COUNT:
        raise ValueError(
            f"Bingo card {card_key} needs at least {BOARD_TASK_COUNT} pool tasks, "
            f"received {len(pool)}"
        )
    rng = _SeededRng(board_seed(event_id, user_id, card_key))
    shuffled = list(pool)
    for i in range(len(shuffled) - 1, 0, -1):
        j = rng.bounded_int(i + 1)
        shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
    return shuffled[:BOARD_TASK_COUNT]


# ---------------------------------------------------------------------------------------
# Pattern geometry + scoring
# ---------------------------------------------------------------------------------------

SQUARE_POINTS = 1  # every crossed square, before any pattern bonus

PATTERN_KEYS: tuple[str, ...] = ("four_corners", "line", "cross", "around_the_world", "blackout")

PATTERN_POINTS: dict[str, int] = {
    "four_corners": 5,
    "line": 10,
    "cross": 20,
    "around_the_world": 35,
    "blackout": 50,
}

PATTERN_LABELS: dict[str, str] = {
    "four_corners": "Four Corners",
    "line": "Line",
    "cross": "Cross",
    "around_the_world": "Around the World",
    "blackout": "Blackout",
}


def _cell(row: int, col: int) -> int:
    return row * GRID_SIZE + col


FOUR_CORNERS = [
    _cell(0, 0),
    _cell(0, GRID_SIZE - 1),
    _cell(GRID_SIZE - 1, 0),
    _cell(GRID_SIZE - 1, GRID_SIZE - 1),
]
_ROWS = [[_cell(r, c) for c in range(GRID_SIZE)] for r in range(GRID_SIZE)]
_COLS = [[_cell(r, c) for r in range(GRID_SIZE)] for c in range(GRID_SIZE)]
_DIAG_MAIN = [_cell(i, i) for i in range(GRID_SIZE)]
_DIAG_ANTI = [_cell(i, GRID_SIZE - 1 - i) for i in range(GRID_SIZE)]
# All straight lines: every row, every column, and both diagonals count as a "line".
LINES = [*_ROWS, *_COLS, _DIAG_MAIN, _DIAG_ANTI]

_CENTER = GRID_SIZE // 2
PLUS = sorted(set(_ROWS[_CENTER]) | set(_COLS[_CENTER]))
X_SHAPE = sorted(set(_DIAG_MAIN) | set(_DIAG_ANTI))
AROUND_THE_WORLD = [
    i
    for i in range(CELL_COUNT)
    if row_of(i) in (0, GRID_SIZE - 1) or col_of(i) in (0, GRID_SIZE - 1)
]
BLACKOUT = list(range(CELL_COUNT))

# Plain-language shape of each pattern, for rules text shown to members and the chat agent.
PATTERN_DESCRIPTIONS: dict[str, str] = {
    "four_corners": "the four corner squares",
    "line": "any full row, column, or diagonal",
    "cross": (
        "the full center row and center column (a plus), or both full diagonals (an X)"
    ),
    "around_the_world": f"all {len(AROUND_THE_WORLD)} squares on the outer edge",
    "blackout": f"all {CELL_COUNT} squares",
}


def _covers(completed: set[int], group: Sequence[int]) -> bool:
    return all(cell in completed for cell in group)


@dataclass
class CardScore:
    pattern: str | None
    points: int  # squares crossed + pattern bonus
    bonus: int = 0  # largest completed pattern bonus (0 if none)
    highlight_cells: list[int] = field(default_factory=list)
    completed_count: int = 0


def score_card(completed: set[int]) -> CardScore:
    """Score one card.

    Every crossed square is worth +1 point. On top of that, the single highest-value
    completed pattern adds a bonus (patterns never stack — only the largest bonus is
    awarded per card). So ``points = squares_crossed + best_pattern_bonus``.
    """
    candidates: list[tuple[str, list[int]]] = []

    if _covers(completed, FOUR_CORNERS):
        candidates.append(("four_corners", list(FOUR_CORNERS)))
    line = next((group for group in LINES if _covers(completed, group)), None)
    if line is not None:
        candidates.append(("line", list(line)))
    # A cross is satisfied by EITHER a plus (center row + center column) OR an X (both
    # diagonals); whichever is complete provides the highlight.
    if _covers(completed, PLUS):
        candidates.append(("cross", list(PLUS)))
    elif _covers(completed, X_SHAPE):
        candidates.append(("cross", list(X_SHAPE)))
    if _covers(completed, AROUND_THE_WORLD):
        candidates.append(("around_the_world", list(AROUND_THE_WORLD)))
    if _covers(completed, BLACKOUT):
        candidates.append(("blackout", list(BLACKOUT)))

    best_pattern: str | None = None
    best_bonus = 0
    best_cells: list[int] = []
    for pattern, cells in candidates:
        bonus = PATTERN_POINTS[pattern]
        if bonus > best_bonus:
            best_pattern, best_bonus, best_cells = pattern, bonus, list(cells)

    return CardScore(
        pattern=best_pattern,
        points=len(completed) * SQUARE_POINTS + best_bonus,
        bonus=best_bonus,
        highlight_cells=best_cells,
        completed_count=len(completed),
    )


@dataclass
class EventScore:
    cards: dict[str, CardScore]
    total: int


def score_event(completed_by_card: dict[str, set[int]]) -> EventScore:
    cards: dict[str, CardScore] = {}
    total = 0
    for key in CARD_KEYS:
        card = score_card(completed_by_card.get(key, set()))
        cards[key] = card
        total += card.points
    return EventScore(cards=cards, total=total)


def rules_text() -> str:
    """The scoring rules in plain language, generated from the constants above.

    Fed to the chat agent so its knowledge of the scoring follows the code: change a
    point value here and the bot's explanation changes with it, no prompt edit needed.
    """
    max_bonus = max(PATTERN_POINTS.values())
    card_max = CELL_COUNT * SQUARE_POINTS + max_bonus
    square_word = "point" if SQUARE_POINTS == 1 else "points"
    pattern_lines = "\n".join(
        f"  - {PATTERN_LABELS[key]} (+{PATTERN_POINTS[key]}): {PATTERN_DESCRIPTIONS[key]}."
        for key in sorted(PATTERN_KEYS, key=lambda k: PATTERN_POINTS[k])
    )
    return (
        "Fitness Bingo scoring rules (authoritative, generated from the scoring code; "
        "they override any scoring wording in the event description):\n"
        f"- Each member has {len(CARD_KEYS)} cards ({', '.join(CARD_LABELS[k] for k in CARD_KEYS)}), "
        f"each a {GRID_SIZE}x{GRID_SIZE} grid of {CELL_COUNT} tasks. Boards differ per member.\n"
        f"- Every crossed square is worth {SQUARE_POINTS} {square_word}.\n"
        "- Each card also earns a bonus for the single highest-value pattern completed on "
        "it. Bonuses do not stack: only the largest one per card counts.\n"
        f"{pattern_lines}\n"
        f"- Card score = squares crossed x {SQUARE_POINTS} + best pattern bonus "
        f"(max {card_max} per card). Total = sum of all {len(CARD_KEYS)} cards "
        f"(max {card_max * len(CARD_KEYS)}).\n"
        "- One activity in a check-in can cross off only one square.\n"
        "- Leaderboard ranks by total points; tied totals share a rank."
    )


# ---------------------------------------------------------------------------------------
# Max-disjoint-assignment solver (check-in resolution)
# ---------------------------------------------------------------------------------------
#
# A single check-in can satisfy several currently-open squares, but one specific activity
# in that check-in may back at most one crossing. Given candidate (square -> justifying
# activity indices) options, choose the assignment that crosses the MOST currently-open
# squares, with the chosen activity subsets pairwise disjoint. Ties break toward
# consuming the fewest activities, then by candidate order for determinism.

C = TypeVar("C")


@dataclass
class AssignmentCandidate:
    cell: object
    activity_indices: list[int]
    confidence: float
    reason: str


def _popcount(mask: int) -> int:
    return bin(mask).count("1")


def solve_assignment(
    candidates: Sequence[AssignmentCandidate],
    cell_key: Callable[[object], str],
) -> list[AssignmentCandidate]:
    """Solve the maximum disjoint assignment. Several options may target the same cell;
    at most one option per distinct cell is chosen. Activity indices must be < 31."""
    order: list[str] = []
    by_cell: dict[str, list[tuple[AssignmentCandidate, int]]] = {}
    for candidate in candidates:
        if not candidate.activity_indices:
            continue
        mask = 0
        for index in candidate.activity_indices:
            if index < 0 or index > 30:
                raise ValueError(f"Bingo assignment activity index {index} out of range")
            mask |= 1 << index
        key = cell_key(candidate.cell)
        if key not in by_cell:
            by_cell[key] = []
            order.append(key)
        by_cell[key].append((candidate, mask))

    cells = [(key, by_cell[key]) for key in order]
    best: dict[str, object] = {"count": 0, "activity_total": 1 << 62, "choices": []}
    chosen: list[AssignmentCandidate] = []

    def dfs(position: int, used_mask: int, count: int, activity_total: int) -> None:
        remaining = len(cells) - position
        if count + remaining < int(best["count"]):  # prune
            return
        if position == len(cells):
            if count > int(best["count"]) or (
                count == int(best["count"]) and activity_total < int(best["activity_total"])
            ):
                best["count"] = count
                best["activity_total"] = activity_total
                best["choices"] = list(chosen)
            return
        _key, options = cells[position]
        for candidate, mask in options:
            if mask & used_mask:
                continue
            chosen.append(candidate)
            dfs(position + 1, used_mask | mask, count + 1, activity_total + _popcount(mask))
            chosen.pop()
        dfs(position + 1, used_mask, count, activity_total)

    dfs(0, 0, 0, 0)
    return list(best["choices"])  # type: ignore[arg-type]
