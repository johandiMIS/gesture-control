"""Landmark rendering helpers.

MediaPipe 1.x dropped `mp.solutions.drawing_utils`, so the Tasks API gives us
raw normalized landmarks and we draw them ourselves.
"""

import cv2

from mediapipe.tasks.python.vision import HandLandmarksConnections

CONNECTIONS = [
    (c.start, c.end) for c in HandLandmarksConnections.HAND_CONNECTIONS
]

LANDMARK_COLOR = (0, 220, 255)   # BGR
CONNECTION_COLOR = (255, 255, 255)
TIP_COLOR = (0, 80, 255)
IDLE_COLOR = (110, 110, 110)     # hands the controller is ignoring
GRAB_COLOR = (60, 220, 60)

FINGERTIPS = (4, 8, 12, 16, 20)


def to_pixels(landmarks, width, height):
    """Normalized landmarks -> list of (x, y) pixel coordinates."""
    return [(int(lm.x * width), int(lm.y * height)) for lm in landmarks]


def draw_hand(frame, landmarks, active=True):
    """Draw one hand's skeleton onto a BGR frame, in place.

    Inactive hands are drawn flat grey, so it is obvious at a glance which hand
    the controller has picked as the player's."""
    h, w = frame.shape[:2]
    points = to_pixels(landmarks, w, h)

    line_color = CONNECTION_COLOR if active else IDLE_COLOR
    for start, end in CONNECTIONS:
        cv2.line(frame, points[start], points[end], line_color, 2)

    for idx, point in enumerate(points):
        if not active:
            color, radius = IDLE_COLOR, 3
        else:
            color = TIP_COLOR if idx in FINGERTIPS else LANDMARK_COLOR
            radius = 6 if idx in FINGERTIPS else 4
        cv2.circle(frame, point, radius, color, -1)

    return points


def draw_points(frame, points, active=True):
    """Same as draw_hand, for landmarks already converted to pixels."""
    line_color = CONNECTION_COLOR if active else IDLE_COLOR
    ipoints = [(int(x), int(y)) for x, y in points]
    for start, end in CONNECTIONS:
        cv2.line(frame, ipoints[start], ipoints[end], line_color, 2)
    for idx, point in enumerate(ipoints):
        if not active:
            color, radius = IDLE_COLOR, 3
        else:
            color = TIP_COLOR if idx in FINGERTIPS else LANDMARK_COLOR
            radius = 6 if idx in FINGERTIPS else 4
        cv2.circle(frame, point, radius, color, -1)
    return ipoints


def draw_label(frame, text, anchor, color=(0, 220, 255)):
    """Draw a small text label with a dark backing box at `anchor`."""
    x, y = anchor
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    cv2.rectangle(frame, (x - 4, y - th - 8), (x + tw + 4, y + 4), (0, 0, 0), -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)


def draw_claw_pad(frame, state, size=190, margin=16):
    """A miniature top-down view of the cabinet showing the normalized output."""
    h, w = frame.shape[:2]
    x0, y0 = w - size - margin, margin
    x1, y1 = x0 + size, y0 + size

    overlay = frame.copy()
    cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    border = GRAB_COLOR if state.grab_active else (
        (0, 220, 255) if state.active else IDLE_COLOR)
    cv2.rectangle(frame, (x0, y0), (x1, y1), border, 2)
    for f in (0.25, 0.5, 0.75):
        gx, gy = int(x0 + f * size), int(y0 + f * size)
        cv2.line(frame, (x0 + 1, gy), (x1 - 1, gy), (60, 60, 60), 1)
        cv2.line(frame, (gx, y0 + 1), (gx, y1 - 1), (60, 60, 60), 1)

    cx = int(x0 + state.x * size)
    cy = int(y0 + state.y * size)
    cv2.line(frame, (x0 + 1, cy), (x1 - 1, cy), (90, 90, 90), 1)
    cv2.line(frame, (cx, y0 + 1), (cx, y1 - 1), (90, 90, 90), 1)
    if state.grab_active:
        cv2.circle(frame, (cx, cy), 16, GRAB_COLOR, 2)
    cv2.circle(frame, (cx, cy), 7, border, -1)

    draw_label(frame, f"x {state.x:.2f}  y {state.y:.2f}", (x0, y1 + 24), border)
    return (x0, y0, x1, y1)


def draw_openness_meter(frame, state, open_threshold, close_threshold,
                        anchor=(12, 70), width=220, height=14, full_scale=4.0):
    """Live openness with both trigger thresholds marked - the readout to watch
    when tuning, or when a grab refuses to fire."""
    x, y = anchor
    cv2.rectangle(frame, (x, y), (x + width, y + height), (0, 0, 0), -1)
    cv2.rectangle(frame, (x, y), (x + width, y + height), (90, 90, 90), 1)

    fill = int(min(1.0, state.openness / full_scale) * width)
    color = GRAB_COLOR if state.gesture == "closed" else (0, 220, 255)
    cv2.rectangle(frame, (x + 1, y + 1), (x + max(1, fill), y + height - 1), color, -1)

    for threshold, mark in ((open_threshold, (0, 220, 255)), (close_threshold, (0, 80, 255))):
        mx = x + int(min(1.0, threshold / full_scale) * width)
        cv2.line(frame, (mx, y - 3), (mx, y + height + 3), mark, 2)

    draw_label(frame, f"{state.gesture:6s} {state.openness:4.2f}",
               (x + width + 12, y + height), color)
