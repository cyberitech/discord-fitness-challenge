"""Vision agent for extracting workout stats from screenshots.

Given an image and the active event's editable prompt (from
``events.prompt``), returns a ``WorkoutStats`` that gets serialized to
``submissions.extracted_stats``.
"""

import logging

from pydantic import BaseModel, Field
from strands import Agent
from strands.types.content import ContentBlock

from fcb.agents import WORKOUT_DOMAIN_KNOWLEDGE, bedrock_model

logger = logging.getLogger(__name__)

_ALLOWED_FORMATS = ("png", "jpeg", "gif", "webp")


class WorkoutStats(BaseModel):
    """Structured stats extracted from a workout screenshot."""

    is_workout_screenshot: bool = Field(
        description="True if the image shows a workout tracker, watch, gym app, or fitness stats screen."
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


_SYSTEM_PROMPT_TEMPLATE = (
    WORKOUT_DOMAIN_KNOWLEDGE
    + "\n\n"
    + """You analyze fitness workout screenshots for a Discord fitness challenge tracker.

The current challenge is:
{event_prompt}

Look at the image and extract every workout statistic you can see. Only extract values that are clearly visible; do not guess. Convert units as needed:
- duration_seconds: total workout duration in seconds (convert hours/minutes as needed)
- distance_miles: total distance in miles (convert km if shown)
- elevation_gain_feet: total elevation gained in feet (convert meters if shown)
- calories: total calories burned
- heart_rate_avg_bpm: average heart rate in bpm
- reps / weight_lb / sets: for strength or lifting workouts
- volume_lb: total training volume in lb-reps. When weight_lb, reps, and sets are all visible for a single session, compute `volume_lb = weight_lb * reps * sets` and fill it in. If the image is a summary or tonnage chart that already shows total volume, read that number into `volume_lb` directly and leave weight_lb / reps / sets null.

Any other clearly-visible metric that doesn't fit the above fields goes into `extras` as short string key-value pairs.

If the image does NOT show a workout tracker, watch, gym app, or fitness stats screen (e.g., a meme, a photo of food, a random selfie), set is_workout_screenshot=false and leave all stat fields null.

Set `confidence` based on how legible and unambiguous the source is. A crisp screenshot from a known app is high confidence; a blurry off-angle phone photo is low.
"""
)


def extract(image_bytes: bytes, image_format: str, event_prompt: str) -> WorkoutStats:
    """Run vision extraction against Bedrock. Returns a validated WorkoutStats.

    Raises:
        ValueError: if ``image_format`` isn't one of png/jpeg/gif/webp.
        RuntimeError: if the model returns no structured output.
    """
    if image_format not in _ALLOWED_FORMATS:
        raise ValueError(
            f"unsupported image format {image_format!r}; expected one of {_ALLOWED_FORMATS}"
        )

    logger.info(
        f"invoking vision agent: format={image_format} bytes={len(image_bytes)}"
    )
    agent = Agent(
        model=bedrock_model,
        system_prompt=_SYSTEM_PROMPT_TEMPLATE.format(event_prompt=event_prompt),
        structured_output_model=WorkoutStats,
    )
    content: list[ContentBlock] = [
        {"text": "Extract the workout stats from this screenshot."},
        {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
    ]
    result = agent(content)
    if result.structured_output is None:
        raise RuntimeError("vision agent returned no structured output")

    stats = result.structured_output
    logger.info(
        f"vision extracted: is_workout={stats.is_workout_screenshot} "
        f"workout_type={stats.workout_type} confidence={stats.confidence:.2f} "
        f"stop_reason={result.stop_reason}"
    )
    return stats
