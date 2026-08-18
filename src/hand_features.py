"""Scale-invariant descriptors for one detected hand.

Everything the claw controller needs is derived from two quantities that do
*not* change when the fingers open or close:

    palm centre  - mean of the wrist and the four finger MCP knuckles
    palm radius  - mean distance from that centre out to those same points

The radius is this project's unit of length. It shrinks and grows with distance
to the camera in the same proportion as every other distance in the frame, so
dividing by it makes any other measurement depth-invariant. Using the palm
rather than the wrist for the centre also matters: the wrist swings when the
hand rotates, and a cursor anchored to it would drift every time the player
closed their fist to grab.
"""

import math
from dataclasses import dataclass, field

# Wrist + index/middle/ring/pinky MCP. The thumb is excluded everywhere: it
# moves independently of the other fingers and is the first landmark to become
# unreliable when the hand turns edge-on to the camera.
PALM_IDS = (0, 5, 9, 13, 17)
TIP_IDS = (8, 12, 16, 20)


@dataclass
class HandFeatures:
    center: tuple           # palm centre, pixels
    radius: float           # palm radius, pixels - the unit of length
    openness: float         # mean(tip -> centre) / radius; ~1 fist, ~3.5 flat
    handedness: str
    confidence: float
    points: list = field(repr=False)   # all 21 landmarks in pixels


def extract(landmarks, handedness, width, height):
    """Landmarks for one hand -> HandFeatures.

    Landmarks are normalised per-axis, so they must be scaled back to pixels
    before any distance is computed - otherwise a non-square frame stretches
    every measurement horizontally.
    """
    points = [(lm.x * width, lm.y * height) for lm in landmarks]

    palm = [points[i] for i in PALM_IDS]
    cx = sum(p[0] for p in palm) / len(palm)
    cy = sum(p[1] for p in palm) / len(palm)

    radius = sum(math.dist(p, (cx, cy)) for p in palm) / len(palm)
    radius = max(radius, 1e-6)   # a degenerate hand must not divide by zero

    openness = sum(math.dist(points[i], (cx, cy)) for i in TIP_IDS) / (len(TIP_IDS) * radius)

    label, score = "", 0.0
    if handedness:
        label, score = handedness[0].category_name, handedness[0].score

    return HandFeatures(
        center=(cx, cy),
        radius=radius,
        openness=openness,
        handedness=label,
        confidence=score,
        points=points,
    )


def features_from_result(result, width, height):
    """All hands in a HandLandmarkerResult -> list of HandFeatures."""
    if not result or not result.hand_landmarks:
        return []
    out = []
    for i, landmarks in enumerate(result.hand_landmarks):
        handedness = result.handedness[i] if i < len(result.handedness) else None
        out.append(extract(landmarks, handedness, width, height))
    return out
