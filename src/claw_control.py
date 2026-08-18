"""Hand gestures -> claw machine commands.

Produces a normalised claw position (0 <= x, y <= 1) plus a discrete GRAB
command, and owns the four awkward parts of doing that from a webcam:

1. depth        every motion is measured in *palm radii* rather than pixels, so
                the same physical hand travel moves the claw the same amount
                whether the player stands 40cm or 120cm from the camera.
2. hand swap    the mapping is relative and re-anchored whenever the active hand
                changes, so a fresh hand picks up exactly where the previous one
                left off instead of teleporting the claw across the cabinet.
3. grab         an open -> closed edge on a scale-invariant openness score,
                gated by a Schmitt trigger, a frame debounce, an arm/re-arm rule
                and a cooldown, so one physical grab fires exactly one command.
4. arbitration  every detected hand is tracked frame to frame and scored; the
                active hand keeps control until a challenger beats it by a
                margin for a sustained window. Background hands and one-frame
                false positives therefore cannot steal the claw.
"""

import math
from collections import deque
from dataclasses import dataclass, field

from filters import Ema, OneEuroFilter
from hand_features import features_from_result

GESTURE_NONE = "none"
GESTURE_OPEN = "open"
GESTURE_CLOSED = "closed"


@dataclass
class ClawState:
    """One frame of controller output. `grab` is true on exactly one frame."""

    x: float = 0.5
    y: float = 0.5
    active: bool = False
    grab: bool = False
    grab_active: bool = False        # claw is mid-drop; position is frozen
    gesture: str = GESTURE_NONE
    openness: float = 0.0
    hand_id: int = None
    handedness: str = ""
    hands_seen: int = 0
    tracks: list = field(default_factory=list, repr=False)

    def as_command(self):
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "grab": self.grab,
            "active": self.active,
        }


class _Track:
    """One hand followed across frames. MediaPipe does not promise a stable
    ordering between frames, so identity is re-established by proximity."""

    _next_id = 1

    def __init__(self, feat, t):
        self.id = _Track._next_id
        _Track._next_id += 1
        self.radius = Ema(0.25)
        self.first_seen = t
        self.update(feat, t)

    def update(self, feat, t):
        self.center = feat.center
        self.radius(feat.radius)
        self.openness = feat.openness
        self.handedness = feat.handedness
        self.confidence = feat.confidence
        self.feat = feat
        self.last_seen = t

    def age(self, t):
        return t - self.first_seen


class ClawController:
    def __init__(
        self,
        mapping="relative",
        gain=9.0,
        invert_y=False,
        # gesture
        open_threshold=2.4,
        close_threshold=1.6,
        debounce_frames=3,
        grab_cooldown=1.2,
        grab_hold=0.8,
        grab_latency=0.15,
        # smoothing
        min_cutoff=1.2,
        beta=0.35,
        # arbitration
        takeover_ratio=1.25,
        takeover_frames=8,
        candidate_age=0.3,
        track_timeout=0.5,
        lost_grace=0.4,
        require_open_takeover=True,
    ):
        self.mapping = mapping
        self.gain = gain
        self.invert_y = invert_y

        self.open_threshold = open_threshold
        self.close_threshold = close_threshold
        self.debounce_frames = debounce_frames
        self.grab_cooldown = grab_cooldown
        self.grab_hold = grab_hold
        self.grab_latency = grab_latency

        self.takeover_ratio = takeover_ratio
        self.takeover_frames = takeover_frames
        self.candidate_age = candidate_age
        self.track_timeout = track_timeout
        self.lost_grace = lost_grace
        self.require_open_takeover = require_open_takeover

        self._fx = OneEuroFilter(min_cutoff, beta)
        self._fy = OneEuroFilter(min_cutoff, beta)

        self._tracks = []
        self._active_id = None
        self._challenger_id = None
        self._challenge_frames = 0
        self._lost_since = None
        self._prev_center = None

        self._tx, self._ty = 0.5, 0.5      # pre-filter target
        self._x, self._y = 0.5, 0.5        # smoothed output
        self._history = deque(maxlen=120)  # (t, x, y), for the grab rewind

        self._gesture = GESTURE_NONE
        self._pending = None
        self._pending_frames = 0
        self._armed = False
        self._last_grab = -1e9

    # ------------------------------------------------------------------ api

    def update(self, result, width, height, t):
        feats = features_from_result(result, width, height)
        self._track_hands(feats, t)
        active = self._arbitrate(t, width, height)

        grab_active = (t - self._last_grab) < self.grab_hold
        self._move(active, t, width, height, grab_active)
        grab = self._gesture_fsm(active, t)
        if grab:
            self._rewind(t)
            grab_active = True

        self._history.append((t, self._x, self._y))

        return ClawState(
            x=self._x,
            y=1.0 - self._y if self.invert_y else self._y,
            active=active is not None,
            grab=grab,
            grab_active=grab_active,
            gesture=self._gesture,
            openness=active.openness if active else 0.0,
            hand_id=active.id if active else None,
            handedness=active.handedness if active else "",
            hands_seen=len(feats),
            tracks=[(tr, tr.id == self._active_id) for tr in self._tracks],
        )

    def reanchor(self):
        """Drop positional continuity without moving the claw. Call after
        anything that teleports the hand within the frame - a mirror toggle,
        a camera switch."""
        self._prev_center = None

    def recenter(self):
        self._tx = self._ty = 0.5
        self._x = self._y = 0.5
        self._fx.reseed(0.5)
        self._fy.reseed(0.5)
        self._prev_center = None

    def calibrate(self, kind):
        """Set one threshold from the hand currently held up.

        Openness varies with hand shape, lens and how far the fingers splay, so
        a two-pose calibration beats any hard-coded constant."""
        track = self._active_track()
        if track is None:
            return None
        value = track.openness
        if kind == GESTURE_OPEN:
            self.open_threshold = value * 0.85
        else:
            self.close_threshold = value * 1.15
        # Keep a usable dead band even if the two poses measured close together.
        if self.open_threshold - self.close_threshold < 0.3:
            mid = 0.5 * (self.open_threshold + self.close_threshold)
            self.open_threshold, self.close_threshold = mid + 0.15, mid - 0.15
        return value

    # -------------------------------------------------------------- internal

    def _active_track(self):
        for tr in self._tracks:
            if tr.id == self._active_id:
                return tr
        return None

    def _track_hands(self, feats, t):
        """Greedy nearest-neighbour association, gated by palm size so a hand
        is never confused with a different hand elsewhere in the frame."""
        pairs = []
        for ti, tr in enumerate(self._tracks):
            for fi, feat in enumerate(feats):
                d = math.dist(tr.center, feat.center)
                if d <= 2.5 * max(tr.radius.value, feat.radius):
                    pairs.append((d, ti, fi))
        pairs.sort()

        used_t, used_f = set(), set()
        for _, ti, fi in pairs:
            if ti in used_t or fi in used_f:
                continue
            self._tracks[ti].update(feats[fi], t)
            used_t.add(ti)
            used_f.add(fi)

        for fi, feat in enumerate(feats):
            if fi not in used_f:
                self._tracks.append(_Track(feat, t))

        self._tracks = [tr for tr in self._tracks
                        if t - tr.last_seen <= self.track_timeout]

    def _score(self, track, width, height):
        """How much this hand looks like the player's.

        Nearness dominates - the player reaches toward the cabinet and bystanders
        do not - with a mild bonus for central framing and detector confidence."""
        near = track.radius.value / (0.25 * height)
        d = math.dist(track.center, (width / 2.0, height / 2.0))
        central = 1.0 - min(1.0, d / (0.5 * math.hypot(width, height)))
        return near + 0.25 * central + 0.15 * track.confidence

    def _arbitrate(self, t, width, height):
        active = self._active_track()
        if active is not None and active.last_seen < t:
            active = None       # tracked, but not detected on this frame

        if active is None:
            # Hold the claw still through brief dropouts before handing over.
            if self._lost_since is None:
                self._lost_since = t
            if t - self._lost_since < self.lost_grace:
                return None
            self._active_id = None
        else:
            self._lost_since = None

        # A hand must persist for a moment before it is even considered, which
        # is what filters out single-frame false detections.
        candidates = [tr for tr in self._tracks
                      if tr.last_seen >= t and tr.age(t) >= self.candidate_age]

        if self._active_id is None:
            eligible = [tr for tr in candidates
                        if not self.require_open_takeover
                        or tr.openness >= self.open_threshold]
            if not eligible:
                return None
            winner = max(eligible, key=lambda tr: self._score(tr, width, height))
            self._take_control(winner)
            return winner

        # The active hand keeps control until a challenger is clearly better for
        # a sustained window - one good frame is never enough.
        others = [tr for tr in candidates if tr.id != self._active_id]
        if others:
            best = max(others, key=lambda tr: self._score(tr, width, height))
            better = (self._score(best, width, height)
                      > self._score(active, width, height) * self.takeover_ratio)
            open_enough = (not self.require_open_takeover
                           or best.openness >= self.open_threshold)
            if better and open_enough:
                if best.id == self._challenger_id:
                    self._challenge_frames += 1
                else:
                    self._challenger_id, self._challenge_frames = best.id, 1
                if self._challenge_frames >= self.takeover_frames:
                    self._take_control(best)
                    return best
                return active
        self._challenger_id, self._challenge_frames = None, 0
        return active

    def _take_control(self, track):
        self._active_id = track.id
        self._challenger_id, self._challenge_frames = None, 0
        self._lost_since = None
        # No previous centre means the new hand contributes no delta on its
        # first frame, so the claw stays put. The grab detector also restarts
        # from this hand's own pose, so taking over with a fist cannot fire.
        self._prev_center = None
        self._gesture = GESTURE_NONE
        self._pending, self._pending_frames = None, 0
        self._armed = False

    def _move(self, track, t, width, height, grab_active):
        if track is None:
            self._prev_center = None
        else:
            center = track.center
            # Depth normalisation: a pixel displacement only means something
            # relative to how big the hand currently looks. `gain` is how many
            # palm radii of travel it takes to cross the full claw range.
            span = self.gain * track.radius.value
            if self.mapping == "absolute":
                self._tx = 0.5 + (center[0] - width / 2.0) / span
                self._ty = 0.5 + (center[1] - height / 2.0) / span
            elif self._prev_center is not None and not grab_active:
                self._tx += (center[0] - self._prev_center[0]) / span
                self._ty += (center[1] - self._prev_center[1]) / span
            # Clamping the accumulator rather than only the output is what keeps
            # the edges wind-up free: back off a wall and the claw moves at once.
            self._tx = min(1.0, max(0.0, self._tx))
            self._ty = min(1.0, max(0.0, self._ty))
            # Tracked even while frozen, so unfreezing does not replay the delta.
            self._prev_center = center

        self._x = self._fx(self._tx, t)
        self._y = self._fy(self._ty, t)

    def _gesture_fsm(self, track, t):
        if track is None:
            self._gesture = GESTURE_NONE
            self._pending, self._pending_frames = None, 0
            self._armed = False
            return False

        # Schmitt trigger: inside the dead band the previous state stands, so a
        # hand hovering near the threshold cannot chatter.
        if track.openness >= self.open_threshold:
            observed = GESTURE_OPEN
        elif track.openness <= self.close_threshold:
            observed = GESTURE_CLOSED
        else:
            observed = self._gesture

        if observed == self._gesture:
            self._pending, self._pending_frames = None, 0
            return False

        if observed == self._pending:
            self._pending_frames += 1
        else:
            self._pending, self._pending_frames = observed, 1
        if self._pending_frames < self.debounce_frames:
            return False

        previous, self._gesture = self._gesture, observed
        self._pending, self._pending_frames = None, 0

        if self._gesture == GESTURE_OPEN:
            self._armed = True          # re-arm: one grab per open-then-close
            return False

        if (self._gesture == GESTURE_CLOSED and previous == GESTURE_OPEN
                and self._armed and t - self._last_grab >= self.grab_cooldown):
            self._armed = False
            self._last_grab = t
            return True
        return False

    def _rewind(self, t):
        """Fire the claw at where the hand was just before it started closing.

        Curling fingers drag the palm centre a little, and the debounce costs a
        few more frames on top; without this the claw consistently lands short
        of the toy the player was actually aiming at."""
        cutoff = t - self.grab_latency
        for ts, x, y in reversed(self._history):
            if ts <= cutoff:
                self._tx, self._ty = x, y
                self._x, self._y = x, y
                self._fx.reseed(x)
                self._fy.reseed(y)
                return
