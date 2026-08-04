"""Vision agent for extracting workout stats from screenshots.

Given a batch of images (all attachments from one Discord message) and
the active event's editable prompt (from ``events.prompt``), returns a
``SessionBatch`` — one or more ``WorkoutStats`` entries, one per session
the model identifies in the batch.

The batch shape matters. Multiple images from one message may be:
- Cumulative snapshots of ONE cardio session (Pattern A in
  ``WORKOUT_DOMAIN_KNOWLEDGE``) — return a single ``WorkoutStats``.
- Independent sessions on the same machine (Pattern B) — return one
  ``WorkoutStats`` per session.
- A multi-exercise strength workout (multiple exercise screens) — return
  a single ``WorkoutStats`` with combined stats.
- A same-session display cycle (summary + elevation-detail) — merge
  into a single ``WorkoutStats``.

The model reasons about all images together and returns the sessions
list. Callers write one submission row per session and link the
appropriate images to each.
"""

import logging
from typing import Sequence

from pydantic import BaseModel, Field
from strands import Agent
from strands.types.content import ContentBlock

from fcb.agents import WORKOUT_DOMAIN_KNOWLEDGE, bedrock_model

logger = logging.getLogger(__name__)

_ALLOWED_FORMATS = ("png", "jpeg", "gif", "webp")


class WorkoutStats(BaseModel):
    """Structured stats for one workout session."""

    is_workout_screenshot: bool = Field(
        description="True if this session represents an actual workout (tracker/gym app/fitness stats)."
    )
    workout_type: str | None = Field(
        default=None,
        description="Short category label, e.g. 'treadmill', 'stairmaster', 'weightlifting', 'cycling', 'hike'.",
    )
    duration_seconds: float | None = Field(
        default=None, description="Total workout duration in seconds."
    )
    distance_miles: float | None = Field(
        default=None, description="Total distance in miles."
    )
    elevation_gain_feet: float | None = Field(
        default=None, description="Total elevation gained in feet."
    )
    calories: int | None = Field(
        default=None, description="Total calories burned."
    )
    heart_rate_avg_bpm: int | None = Field(
        default=None, description="Average heart rate in beats per minute."
    )
    reps: int | None = Field(default=None, description="Total repetitions.")
    weight_lb: float | None = Field(
        default=None, description="Weight used (lb) for strength exercises."
    )
    sets: int | None = Field(default=None, description="Total sets performed.")
    volume_lb: float | None = Field(
        default=None,
        description=(
            "Total training volume in lb-reps. Fill this by computing "
            "weight_lb * reps * sets when all three are visible for a "
            "single strength session, OR read it directly from a "
            "summary/tonnage chart if the image shows aggregate volume "
            "instead of per-set numbers."
        ),
    )
    extras: dict[str, str] = Field(
        default_factory=dict,
        description="Any other clearly-visible metric not covered above. Short string values only.",
    )
    confidence: float = Field(
        description="0.0-1.0 confidence in the overall extraction quality."
    )
    notes: str | None = Field(
        default=None, description="Optional short note (e.g. what part of the image was unclear)."
    )
    image_indices: list[int] = Field(
        default_factory=list,
        description=(
            "Zero-based indices of the input images that belong to this "
            "session. For a single-image session, this is [0]. For a "
            "multi-image session, list every contributing image index in "
            "the order they were provided."
        ),
    )


class SessionBatch(BaseModel):
    """One vision call over N images returns a list of sessions plus
    optional clarification state.

    When ``needs_clarification`` is true, ``sessions`` still contains the
    model's best-guess split, but the split should be treated as
    tentative until the user answers ``clarification_question``.
    """

    sessions: list[WorkoutStats] = Field(
        description=(
            "One entry per workout session identified in the batch. May be "
            "shorter than the input image list (multi-image sessions), or "
            "equal to it (each image is its own session). Should not be "
            "longer than the input image list."
        )
    )
    needs_clarification: bool = Field(
        default=False,
        description=(
            "Set true ONLY when the images are genuinely ambiguous even "
            "after applying the multi-image rules in the shared workout "
            "domain knowledge. This should be rare — most multi-image "
            "cases resolve unambiguously via Pattern A / Pattern B / "
            "display-cycle / multi-exercise. Only flag when the numbers "
            "and visuals genuinely cannot distinguish between plausible "
            "interpretations."
        ),
    )
    clarification_question: str | None = Field(
        default=None,
        description=(
            "When needs_clarification is true, a short conversational "
            "question asking the user what they need to know. Be specific "
            "about what's unclear across the images."
        ),
    )
    reasoning: str = Field(
        default="",
        description="One or two short sentences explaining how you split (or didn't split) the images into sessions.",
    )


_SYSTEM_PROMPT_TEMPLATE = (
    WORKOUT_DOMAIN_KNOWLEDGE
    + "\n\n"
    + """You analyze fitness workout screenshots for a Discord fitness challenge tracker.

The current challenge is:
{event_prompt}

You will receive one or more images posted together in a single Discord message. Your job is to figure out how many WORKOUT SESSIONS are in the batch and extract the stats for each.

For every session you identify, produce one WorkoutStats entry with:
- duration_seconds: total session duration in seconds (convert hours/minutes as needed)
- distance_miles: total distance in miles (convert km if shown)
- elevation_gain_feet: total elevation gained in feet (convert meters if shown)
- calories: total calories burned
- heart_rate_avg_bpm: average heart rate in bpm
- reps / weight_lb / sets: for strength or lifting workouts
- volume_lb: total training volume in lb-reps (compute weight_lb * reps * sets when all three are visible for a single strength session, or read directly from a summary/tonnage chart)
- extras: any other clearly-visible metric not covered above, as short string key-value pairs
- image_indices: the zero-based indices of the input images that contributed to this session

Extract only values you can clearly see. Prefer null over a guess. Set `confidence` based on legibility.

Use the multi-image rules in the shared workout domain knowledge (Pattern A / Pattern B / display cycle) to decide how to group images into sessions. Return one session per group.

If a screenshot is NOT a workout tracker (meme, food photo, random selfie), set is_workout_screenshot=false for that session and leave stat fields null.

Ambiguity handling:
Only set needs_clarification=true when the batch is genuinely ambiguous even after applying the multi-image rules. Most cases resolve unambiguously. When you do flag ambiguity, still return your best-guess split in `sessions`, and write a specific question in `clarification_question` explaining what you need the user to tell you.

Fill `reasoning` with a short sentence about how you interpreted the batch (e.g., "One treadmill session with a cooldown segment", "Four independent interval sessions on the same machine", "Multi-exercise strength workout combined into one session").
"""
)


def extract(
    images: Sequence[tuple[bytes, str]], event_prompt: str
) -> SessionBatch:
    """Run vision extraction against Bedrock on a batch of images.

    Args:
        images: A sequence of ``(image_bytes, image_format)`` tuples,
            one per attachment in the Discord message. Order is
            preserved and becomes the zero-based ``image_indices``
            values in the returned sessions.
        event_prompt: The active event's editable prompt.

    Returns:
        A ``SessionBatch`` describing every workout session the model
        identified across the input images.

    Raises:
        ValueError: if the image list is empty, or any format is not
            one of png/jpeg/gif/webp.
        RuntimeError: if the model returns no structured output.
    """
    if not images:
        raise ValueError("extract requires at least one image")
    for i, (_, fmt) in enumerate(images):
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError(
                f"image {i}: unsupported format {fmt!r}; expected one of {_ALLOWED_FORMATS}"
            )

    logger.info(
        f"invoking vision agent: {len(images)} image(s), "
        f"total_bytes={sum(len(b) for b, _ in images)}"
    )

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.replace(
        "{event_prompt}", event_prompt
    )
    agent = Agent(
        model=bedrock_model,
        system_prompt=system_prompt,
        structured_output_model=SessionBatch,
    )
    content: list[ContentBlock] = [
        {
            "text": (
                f"You are given {len(images)} image(s) posted together in one "
                f"Discord message. Reason about them as a batch, group them into "
                f"workout sessions per the domain rules, and return one entry per "
                f"session."
            )
        },
    ]
    for image_bytes, image_format in images:
        content.append(
            {"image": {"format": image_format, "source": {"bytes": image_bytes}}}
        )

    result = agent(content)
    if result.structured_output is None:
        raise RuntimeError("vision agent returned no structured output")

    batch = result.structured_output

    # Defensive fill-in of image_indices for any session that omitted it.
    # If a session has no indices but there's only one session and one
    # image, assign [0]. Otherwise leave empty and let the caller decide.
    if len(batch.sessions) == 1 and not batch.sessions[0].image_indices:
        batch.sessions[0].image_indices = list(range(len(images)))

    logger.info(
        f"vision batch result: {len(batch.sessions)} session(s), "
        f"needs_clarification={batch.needs_clarification}, "
        f"reasoning={batch.reasoning!r}"
    )
    return batch
