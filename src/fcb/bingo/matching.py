"""Bingo matching agent: given a member's open squares and a batch of check-in images,
decide which squares the images satisfy.

The model reads the screenshots and judges satisfaction; it does NOT score, assign, or
pick patterns. It returns the distinct activities it saw (indexed) and, per satisfied
square, which activity indices justify it. Deterministic code (`scoring.solve_assignment`)
then crosses the maximum disjoint set so one activity never backs two squares.
"""

from __future__ import annotations

import logging
from typing import Sequence

from pydantic import BaseModel, Field
from strands import Agent
from strands.types.content import ContentBlock

from fcb.agents import WORKOUT_DOMAIN_KNOWLEDGE, bedrock_model

logger = logging.getLogger(__name__)

_ALLOWED_FORMATS = ("png", "jpeg", "gif", "webp")


class BingoMatch(BaseModel):
    cell_key: str = Field(description="The square id from the offered list, e.g. 'core:12'.")
    activity_indices: list[int] = Field(
        description=(
            "Indices into the `activities` list that justify this square. A single-exercise "
            "task uses exactly one activity; a session-level task (variety, cumulative, rounds) "
            "lists every activity it relies on."
        )
    )
    confidence: float = Field(description="0.0-1.0 confidence that this square is satisfied.")
    reason: str = Field(description="Short factual reason.")


class BingoMatchResult(BaseModel):
    activities: list[str] = Field(
        default_factory=list,
        description="Distinct activities/exercises visible across the images, each a short label.",
    )
    matches: list[BingoMatch] = Field(default_factory=list)
    needs_clarification: bool = Field(default=False)
    clarification_question: str | None = Field(default=None)
    reasoning: str = Field(default="")


_SYSTEM_PROMPT = (
    WORKOUT_DOMAIN_KNOWLEDGE
    + "\n\n"
    + """\
You match a member's workout check-in against their OPEN fitness-bingo squares.

You are given the images posted in one Discord message (optionally a caption) and a list
of the member's currently-open squares. Each square has an id (`cell_key`) and a task
description. Tasks are single-session: a rep/volume threshold, a structural set
(drop set, rep ladder), a per-side or cumulative carry, or a variety requirement.

Do two things:
1. List the distinct activities/exercises you can clearly see across the images, in
   `activities` (short labels, e.g. "100 crunches", "5k treadmill run"). Index order
   matters — matches refer to these by index.
2. For each OPEN square that the images genuinely satisfy, add a match with its
   `cell_key` and the `activity_indices` that justify it.

Rules:
- Only mark a square when the images clearly meet it (right exercise AND required
  amount/structure). When unsure, leave it out. Never invent a number.
- A single-exercise square uses exactly one activity index. A session-level square
  (e.g. "5 different core exercises", "5 cumulative minutes") lists every activity it
  relies on.
- You MAY reference the same activity in more than one candidate match; the caller
  enforces that one activity ultimately crosses only one square and picks the best
  combination. Just report every genuinely satisfied square.
- Only use `cell_key` values from the offered list. Fail closed: omit anything uncertain.
- Heart-rate zones are an intensity FLOOR, not a narrow band. A task asking for
  "Zone 2 cardio" is satisfied by any steady cardio performed at Zone 2 OR HIGHER
  intensity for the task's required duration/continuity. Treat walking, incline-treadmill
  walking, easy jogging, hiking, easy cycling, and similar steady efforts as Zone 2
  cardio. If the tracker shows a higher zone or a higher average heart rate than the
  Zone 2 range (Zone 3/4/5), it STILL counts toward a Zone 2 requirement because it
  exceeds the floor — only the duration/continuity condition must still be met.
- Set needs_clarification only when one specific member answer would resolve a genuine
  ambiguity about whether a square was met.
"""
)


def match_bingo(
    images: Sequence[tuple[bytes, str]],
    open_squares: Sequence[dict],
    caption: str | None = None,
    *,
    credited_squares: Sequence[dict] = (),
    clarification: tuple[str, str] | None = None,
) -> BingoMatchResult:
    """Run the bingo matching agent.

    Args:
        images: ``(image_bytes, image_format)`` tuples.
        open_squares: ``[{"cell_key": "core:12", "task": "100 crunches ..."}, ...]``.
        caption: optional message caption.
        credited_squares: squares these same images already crossed (same shape as
            ``open_squares``). Used on a clarification re-check so the model reports
            which activities back them, letting the caller keep those activities from
            being reused for a new square.
        clarification: ``(question, answer)`` when re-checking after the member answered
            a clarification question.
    """
    if not images:
        raise ValueError("match_bingo requires at least one image")
    if not open_squares:
        return BingoMatchResult()
    for i, (_, fmt) in enumerate(images):
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError(f"image {i}: unsupported format {fmt!r}")

    offered = {str(sq["cell_key"]) for sq in open_squares} | {
        str(sq["cell_key"]) for sq in credited_squares
    }
    caption_clean = (caption or "").strip()
    square_lines = "\n".join(f"  - {sq['cell_key']}: {sq['task']}" for sq in open_squares)
    intro = (
        f"The member has {len(open_squares)} open bingo squares:\n{square_lines}\n\n"
        f"Here are the check-in image(s). Identify the activities and which open squares "
        f"they satisfy."
    )
    if credited_squares:
        credited_lines = "\n".join(
            f"  - {sq['cell_key']}: {sq['task']}" for sq in credited_squares
        )
        intro += (
            f"\n\nThese squares were ALREADY crossed off from these same images:\n"
            f"{credited_lines}\n"
            f"Report a match for each of them too, with the activity indices that back it, "
            f"so the caller knows which activities are already used."
        )
    if caption_clean:
        intro += f"\n\nThe member's caption was:\n{caption_clean!r}"
    if clarification is not None:
        question, answer = clarification
        intro += (
            f"\n\nThis is a re-check. You previously asked the member:\n{question!r}\n"
            f"They replied:\n{answer!r}\n"
            f"Use the reply to resolve what the images left ambiguous. It explains the "
            f"images; it does not replace them, so only match squares the images plus that "
            f"reply genuinely support. Do NOT set needs_clarification: the member has "
            f"already answered."
        )

    content: list[ContentBlock] = [{"text": intro}]
    for image_bytes, image_format in images:
        content.append({"image": {"format": image_format, "source": {"bytes": image_bytes}}})

    agent = Agent(
        model=bedrock_model,
        system_prompt=_SYSTEM_PROMPT,
        structured_output_model=BingoMatchResult,
    )
    result = agent(content)
    if result.structured_output is None:
        raise RuntimeError("bingo matching agent returned no structured output")

    decision = result.structured_output
    activity_count = len(decision.activities)
    clean: list[BingoMatch] = []
    for m in decision.matches:
        if m.cell_key not in offered:
            continue
        valid_indices = [i for i in m.activity_indices if 0 <= i < activity_count]
        if not valid_indices:
            continue
        m.activity_indices = valid_indices
        clean.append(m)
    decision.matches = clean
    logger.info(
        f"bingo match: {activity_count} activities, {len(clean)} satisfied squares, "
        f"needs_clarification={decision.needs_clarification}"
    )
    return decision
