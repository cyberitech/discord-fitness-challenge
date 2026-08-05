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

User caption:
- The user may include a text caption on the same message as the
  screenshots. The caption is passed to you as text before the images
  when present.
- Treat any number or fact the caption states as authoritative user-
  supplied input. Use it to fill fields the images don't show
  clearly. Examples: "incline 6%", "6% grade", "Effort is 6.5%",
  "elevation 400 ft", "40 min run", "70 lb dumbbells".
- A caption number wins over the "no numeric value visible; ask for
  clarification" refusal path. If the user supplied it, take it.
- A caption number that directly contradicts a clearly-visible
  different number in the image loses to the image. Note the
  discrepancy in ``notes`` in that case.
- Never invent a number that neither the image nor the caption
  states.

Apps you may see (recognize the shape; do not assume anything not
visible):
- Hevy, Strong, JEFIT — strength training logs; per-set weight/reps
  plus a session-total volume/tonnage bar.
- Apple Fitness / Health, Garmin Connect, Strava, WHOOP — cardio-first
  displays with distance/pace/HR/elevation.
- Fitbod, Nike Training Club — mixed cardio + strength.

Apple Fitness "Effort" field convention (this community's rule):
- Apple Fitness displays an "Effort" score on Indoor Run / Indoor
  Walk / Indoor Cycle sessions where the equipment doesn't report
  incline directly. The screen shows a numeric value (1-10) with a
  text label like "Easy", "Moderate", "Hard" and a bar chart.
- For this community's elevation-style challenges, treat the numeric
  Effort value as the treadmill incline percentage for that session.
  Example: "Effort: 6 Moderate" means 6% incline; "Effort: 8 Hard"
  means 8% incline.
- Compute elevation from the incline and distance in the usual way:
  elevation_ft ≈ distance_miles × 5280 × (grade / 100).
- Also record ``extras.effort`` with the original label ("6 Moderate")
  so the raw display value is preserved.
- The user's caption overrides this convention when it states a
  different incline explicitly. "Effort is 6.5%" alongside a screen
  showing "6 Moderate" means the user is telling you the real incline
  was 6.5%.

Anti-patterns:
- Never invent a number the image does not show. Prefer null over a
  guess. If something is partially legible or ambiguous, drop a short
  note in ``notes`` instead of filling the field.
- Never classify a food photo, meme, or random selfie as a workout
  screenshot. is_workout_screenshot is TRUE only for actual workout
  trackers / gym apps / fitness dashboards.

Multi-image sessions on cardio equipment (treadmill, stationary bike,
elliptical, rower):

When a user posts multiple screenshots from the same machine in one
message, the images are one of two patterns.

Pattern A — cumulative snapshots of ONE session:
  Distance and time values are monotonically increasing across the
  images. Later images show larger totals for both. The user paused
  during their session or continued into a cooldown and took snapshots
  at different moments. Same machine, same session.

  Treat as ONE workout:
  - Sort the images by cumulative distance ascending.
  - Compute elevation per segment as (that segment's delta distance
    in miles) * 5280 * (that segment's incline percent / 100). The
    first segment uses the earliest image's own distance and grade;
    every later segment uses the distance delta from the prior image
    together with its own grade. Sum the per-segment elevations for
    the session total.
  - Take duration_seconds, calories, distance_miles from the LAST
    image — they are cumulative as of that moment.
  - Return ONE workout for the whole session.

  Example: image A shows 1.01 mi at 15% incline. Image B shows 1.05 mi
  at 7.5% incline. This is a workout + cooldown on ONE session.
     first segment: 1.01 * 5280 * 0.15 = 799 ft
     next segment: (1.05 - 1.01) * 5280 * 0.075 = 16 ft
     total: ~815 ft elevation, 1.05 mi distance.

Pattern B — SEPARATE sessions on the same machine:
  Distance and time values are independent across the images. Each has
  its own timer and its own distance count starting from zero. The
  values do NOT monotonically increase across the images. Common in
  interval-style classes or when a user resets the machine between
  rounds.

  Treat as MULTIPLE workouts — return one workout per image, each with
  its own elevation calculation:
     elev = distance * 5280 * (grade / 100)

  Example: four Woodway Shred screenshots with times 4:27, 3:34, 4:16,
  3:49 and distances 0.44, 0.50, 0.35, 0.19. Times don't increase;
  distances don't increase. Four separate sessions.
     session 1: 0.44 * 5280 * 0.05  = 116 ft
     session 2: 0.50 * 5280 * 0.015 = 40 ft
     session 3: 0.35 * 5280 * 0.12  = 222 ft
     session 4: 0.19 * 5280 * 0.10  = 100 ft

Related pattern — same-session display cycle:
  Some machines cycle their display between screens (summary, elevation
  detail, heart-rate detail). Two images from the same message may show
  the same session on different display screens: same duration and
  same distance visible, but each shows different additional fields.
  MERGE them into ONE workout by taking each field from whichever image
  shows it clearly. Do NOT count as two workouts.

Decision signals summary:
  - Distances AND times monotonically increasing across images → one
    session (Pattern A).
  - Independent timers and distances, no monotonic ordering → separate
    sessions (Pattern B).
  - Same duration and distance but different fields visible in each →
    display cycle, merge into one.
  - Different apps, different dates, different times of day → separate
    sessions.
  - Different exercises (strength split across multiple exercise
    screens) → one session with combined stats. Multi-exercise strength
    workouts are ALWAYS one session; do not treat separate exercise
    screens as separate sessions.
"""
