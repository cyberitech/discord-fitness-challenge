"""Routing agent: decides which of the active events a submission counts for.

Text-only Bedrock call. Only invoked by the bot when 2+ events are active
simultaneously — the single-event case is a fast local path (no LLM).

Given the extracted workout stats and every active event's prompt, the
model returns an ordered list of matched event IDs. The first entry is
treated as the "primary" event for reply framing; every match ends up in
the ``submission_events`` junction, so a workout can count toward more
than one challenge at once.
"""

import logging
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field
from strands import Agent

from fcb.agents import WORKOUT_DOMAIN_KNOWLEDGE, bedrock_model

logger = logging.getLogger(__name__)


class RoutingDecision(BaseModel):
    """LLM router output."""

    matched_event_ids: list[int] = Field(
        description=(
            "Ordered list of event IDs this submission counts toward. "
            "Empty if no active event applies. First entry is the primary."
        )
    )
    reasoning: str = Field(
        description="One short sentence explaining the choice."
    )


_SYSTEM_PROMPT = (
    WORKOUT_DOMAIN_KNOWLEDGE
    + "\n\n"
    + """\
You are the routing agent for a Discord fitness challenge tracker.

Given a set of extracted workout stats and a list of currently active
challenges, decide which challenges this workout counts toward.

Rules:
- A workout MAY count toward multiple challenges if it satisfies each of
  their described criteria.
- Only include an event ID whose prompt is clearly satisfied by the
  extracted stats. If the stats and the prompt don't line up, omit that
  event. Apply the shared domain knowledge above — a specific-exercise
  challenge is NOT satisfied by a workout that lacked that exercise,
  even when the session had high total volume.
- Return event IDs in priority order — the strongest match first.
- If no active event applies, return an empty list.
- Never invent an ID. Use only IDs from the input.
"""
)


def route_submission(
    *,
    stats: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
) -> RoutingDecision:
    """Ask the router which events this submission counts toward.

    Args:
        stats: The vision agent's extracted stats (a JSON-safe dict).
        events: Active event rows; each MUST have ``id``, ``name``, ``prompt``.

    Returns:
        RoutingDecision with matched_event_ids in priority order.
    """
    event_list = list(events)
    if not event_list:
        return RoutingDecision(matched_event_ids=[], reasoning="no active events")

    prompt_lines = ["Extracted workout stats:", f"  {dict(stats)}", "", "Active challenges:"]
    for e in event_list:
        prompt_lines.append(f"  - ID {e['id']}: {e['name']!r} — {e['prompt']}")
    prompt_lines.append("")
    prompt_lines.append("Which challenges does this workout count toward?")

    logger.info(
        f"router.route_submission: {len(event_list)} active events, "
        f"stats_keys={sorted(k for k, v in stats.items() if v not in (None, [], {}))}"
    )
    agent = Agent(
        model=bedrock_model,
        system_prompt=_SYSTEM_PROMPT,
        structured_output_model=RoutingDecision,
    )
    result = agent("\n".join(prompt_lines))
    if result.structured_output is None:
        raise RuntimeError("router returned no structured output")

    decision = result.structured_output
    # Defensive filter: never return an ID we didn't offer.
    valid_ids = {int(e["id"]) for e in event_list}
    decision.matched_event_ids = [
        eid for eid in decision.matched_event_ids if eid in valid_ids
    ]
    logger.info(
        f"router decision: matched={decision.matched_event_ids} "
        f"reasoning={decision.reasoning!r}"
    )
    return decision
