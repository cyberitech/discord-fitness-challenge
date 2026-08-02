"""Strands agents backed by Amazon Bedrock.

One BedrockModel instance is constructed at import time and shared by
every agent module in this package. See ``.kiro/steering/bedrock.md`` for
why we pool the client rather than build one per invocation.

``WORKOUT_DOMAIN_KNOWLEDGE`` is the shared factual baseline every agent
that reasons about workouts prepends to its system prompt (vision +
router today; more may follow). Definitions live here so no individual
event prompt has to redefine "session volume" or "tonnage", and no
challenge author can accidentally omit the rule that a specific-
exercise challenge MUST NOT scoop up unrelated exercise volume from a
summary bar. This is stable code, not admin-tunable persona.
"""

from strands.models import BedrockModel

from fcb import config

bedrock_model = BedrockModel(
    region_name=config.AWS_REGION,
    model_id=config.BEDROCK_MODEL_ID,
)


WORKOUT_DOMAIN_KNOWLEDGE = """\
Shared workout domain knowledge — applies to every challenge regardless
of the event's own prompt. Treat this as authoritative baseline.

Volume terminology:
- Set volume (one set) = weight_lb * reps.
- Exercise volume (one exercise across all its sets in a session) is
  the sum of that exercise's set volumes. When every set of the
  exercise uses the same weight and reps, this equals
  weight_lb * reps * sets.
- Session volume (a whole workout) = sum of exercise volumes across
  every exercise performed in that session. Apps display this at the
  top of a workout screen as "Volume", "Total Volume", or "Tonnage".
- "Tonnage" is a synonym for volume regardless of which app used it.

Exercise-specific extraction rule (load-bearing):
- When the challenge prompt names a specific exercise or lift,
  volume_lb and any per-lift stats MUST come from THAT exercise only.
  Do NOT copy a session-level total from a summary bar or tonnage
  graph if the session included exercises unrelated to the challenge.
  A session total that mixes deadlifts + bench + the target exercise
  is NOT the target exercise's volume — it is the workout's total.
- If the target exercise is not present in the screenshot at all,
  treat the submission as unrelated to the challenge: leave volume_lb
  and other target-specific fields null, and put a short note in
  ``notes`` describing what the image actually showed.

Unit conventions:
- Distance: convert kilometers to miles (multiply by 0.6214) if the
  app shows km.
- Elevation: convert meters to feet (multiply by 3.281) if the app
  shows meters.
- Weight: convert kilograms to pounds (multiply by 2.2046) if the
  app shows kg.
- Duration: convert any HH:MM:SS or "1h 12m 5s" display into total
  seconds.

Apps you may see (recognize the shape; do not assume anything not
visible):
- Hevy, Strong, JEFIT — strength training logs; per-set weight/reps
  plus a session-total volume/tonnage bar.
- Apple Fitness / Health, Garmin Connect, Strava, WHOOP — cardio-first
  displays with distance/pace/HR/elevation.
- Fitbod, Nike Training Club — mixed cardio + strength.

Anti-patterns:
- Never invent a number the image does not show. Prefer null over a
  guess. If something is partially legible or ambiguous, drop a short
  note in ``notes`` instead of filling the field.
- Never classify a food photo, meme, or random selfie as a workout
  screenshot. is_workout_screenshot is TRUE only for actual workout
  trackers / gym apps / fitness dashboards.
"""
