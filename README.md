# gesture-control

A webcam claw machine controller. Your hand is the joystick: the palm steers the
claw across a normalized cabinet, and closing an open palm drops it.

Built on the MediaPipe Tasks API and OpenCV, with an Arduino sketch on the other
end of the wire. Commands can also go to stdout or over UDP, so the vision half
is useful without any hardware at all.

```
python src/main.py --emit serial:COM3
```

<!-- A short screen capture goes here once recorded. -->

## Why this exists

Most hand-tracking samples you'll find are written against `mp.solutions.hands`,
the legacy MediaPipe solutions API. That API was **removed in MediaPipe 1.x**, so
those samples no longer run at all on a current install. This repo is a small,
correct reference for the replacement: the Tasks API
(`mediapipe.tasks.python.vision.HandLandmarker`), which requires an explicit
`.task` model bundle and hands you raw normalized landmarks with no drawing
helpers attached.

Landmarks, though, are the easy part. Everything between "21 points per frame"
and "a machine that does what the player meant" is the actual work: the same
gesture has to mean the same thing at 40cm and at 120cm, one physical grab has to
fire exactly one command, a bystander walking behind the player must not steal
the claw, and a 9600-baud serial link has to carry it all without falling behind
real time. Those problems are what most of this codebase is about.

## How it works

```
camera ─> HandLandmarker ─> features ─> arbitration ─> mapping ─> emitter ─> claw
          (async)           palm        which hand     + One Euro  stdout /
                            centre      is playing     smoothing   UDP /
                            + radius                   + grab FSM  serial
```

Each stage is a module (see [Layout](#layout)). `ClawController.update()` turns
one detection result into one `ClawState`, and the emitter decides where that
goes; nothing downstream of the controller knows about OpenCV or MediaPipe.

## Design notes

**Palm radius is the unit of length.** Every measurement is divided by the mean
distance from the palm centre out to the wrist and finger knuckles. That quantity
scales with distance to the camera in the same proportion as every other distance
in the frame, so dividing by it makes the whole mapping depth-invariant — the
same physical hand movement moves the claw the same amount whether the player
stands near or far. The palm centre, not the wrist, anchors the cursor: the wrist
swings when the hand rotates, and would drift the claw every time the player
closed their fist to grab.

**The mapping is relative by default.** Hand motion nudges the claw rather than
positioning it absolutely, and the anchor is dropped whenever positional
continuity breaks — a new hand taking over, a mirror toggle, a tracking dropout.
A fresh hand therefore contributes no delta on its first frame and the claw stays
where the previous player left it instead of teleporting across the cabinet.
`--mapping absolute` is there for when hand position in frame *should* map
straight through.

**One hand is the player, and it has to be beaten to be replaced.** Every
detected hand is tracked frame to frame by proximity — MediaPipe does not promise
a stable ordering — and scored on apparent size, framing and detector confidence.
The active hand keeps control until a challenger outscores it by a margin for a
sustained window, and a candidate must persist for a moment before it is even
considered. Single-frame false positives and hands in the background cannot take
the claw.

**One physical grab fires exactly one command.** Openness — mean fingertip
distance from the palm centre, in palm radii — runs through a Schmitt trigger, so
a hand hovering near the threshold cannot chatter; then a frame debounce; then an
arm/re-arm rule that requires a deliberate open before the next close counts;
then a cooldown. Hand shape and lens vary too much for a hard-coded constant, so
the thresholds are calibrated in place with `o` and `c`.

**The claw fires where the hand *was*.** Curling fingers drag the palm centre a
little, and the debounce costs a few more frames on top. Without compensation the
claw consistently lands short of the toy the player was aiming at, so a grab
rewinds through the position history by `grab_latency` and drops there.

**Smoothing is speed-adaptive.** Plain exponential smoothing forces a bad
choice: light smoothing leaves jitter, heavy smoothing adds lag. The One Euro
filter (Casiez, Roussel & Vogel, CHI 2012) raises its cutoff frequency with the
speed of the signal, so a hand held still is smoothed hard and a fast sweep is
barely touched. Tune `--min-cutoff` with the hand still, then `--beta` while
sweeping.

**Position and GRAB have different reliability requirements.** Position is a
stream: every frame supersedes the last, so a dropped update costs nothing. GRAB
is an edge — it happens once, and losing it loses the round. Every emitter treats
the two differently. On serial, position is rate-limited, deadbanded and skipped
when the write queue backs up, while GRAB bypasses all of that and is re-queued
on failure until it goes out or goes stale.

**The serial protocol exists because JSON does not fit.** 9600 8N1 is 960
bytes/second, and that is the entire budget. JSON at 30fps would need roughly
1650 B/s — nearly twice the wire — so the link would fall steadily further behind
real time until the claw was seconds late. Instead, a fixed-grammar line protocol
of ~11 bytes per frame, rate limited well under the ceiling:

```
P<x>,<y>\n    position; both axes as integers 0..1000
G<x>,<y>\n    grab, at this exact position
I\n           idle — no player in control, hold still
```

GRAB carries its own coordinates rather than relying on the preceding `P`, so a
dropped position update can never make the claw drop in the wrong place. The host
sends something at least every 250ms, which is what makes silence on the link
unambiguous to the board.

**Inference is asynchronous, and the pipeline is deliberately lossy.**
`HandTracker` runs the landmarker in `RunningMode.LIVE_STREAM`. `process()`
submits a frame via `detect_async()` and immediately returns the most recent
result that has come back over the callback — it never waits. The capture loop
therefore runs at camera speed regardless of how long detection takes, and the
overlay may trail the displayed frame by a frame or two. For a live preview that
trade is the right one; for frame-accurate offline analysis it is not, and you'd
want `RunningMode.VIDEO` instead. The callback writes the shared result under a
lock, since MediaPipe delivers it on its own thread.

**Timestamps must increase monotonically.** MediaPipe rejects out-of-order
timestamps on a live stream. `main.py` derives them from `time.perf_counter()`
relative to loop start rather than wall-clock time, which is immune to NTP steps
and clock adjustments mid-session.

**Drawing is hand-rolled.** With `drawing_utils` gone, `src/drawing.py` converts
normalized coordinates to pixels and strokes the connections itself. The
connection list comes from `HandLandmarksConnections.HAND_CONNECTIONS`, whose
entries are objects with `.start` / `.end` attributes — not the tuples the old
API used. The overlay also draws a claw pad showing commanded position and an
openness meter with both thresholds marked, which is what makes calibration a
ten-second job instead of guesswork.

**The mirror is a view concern, not a model concern.** The selfie flip happens
before inference, so `handedness` describes the image as the user sees it. That
keeps the label intuitive on screen; if you consume handedness programmatically
with `--no-mirror`, be aware the convention flips with it.

**Windows opens the camera through DirectShow.** `cv2.CAP_DSHOW` is selected on
`win32` because the default MSMF backend stalls for several seconds on
`VideoCapture` construction. Other platforms use `CAP_ANY`.

**The model bundle isn't committed.** It's ~7.8 MB of binary that would bloat
clone times and repo history for no benefit — `scripts/download_model.py` fetches
it on demand and is idempotent.

## Setup

Requires Python 3.11 and a webcam. The Arduino side is optional.

```bash
conda env create -f environment.yml
conda activate hand-tracking
python scripts/download_model.py
```

Or with plain pip, if you'd rather manage the environment yourself:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/download_model.py
```

`download_model.py` pulls the float16 `hand_landmarker.task` bundle from Google's
model storage into `models/`, and no-ops if it's already there. `pyserial` is
needed only for the serial emitter and is imported lazily, so everything else
runs without it.

## Usage

```bash
python src/main.py                          # preview and overlay only
python src/main.py --emit stdout            # print commands
python src/main.py --emit serial:COM3       # drive an Arduino
python src/main.py --emit 127.0.0.1:9000    # stream JSON over UDP
python src/main.py --list-ports             # which serial ports exist
```

Hold up an open palm to take control, move it to steer, close it to grab. The
overlay shows which hand is being followed, where the claw currently is, and
where the openness score sits relative to both thresholds.

**Calibrate first.** Hold an open palm and press `o`, then make a fist and press
`c`. The thresholds are set from your hand, at your distance, under your lens;
the defaults are only a starting point.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--camera N` | `0` | Webcam device index |
| `--width` / `--height` | `1280` / `720` | Requested capture resolution (the driver may not honor it) |
| `--hands N` | `2` | Maximum hands to track — above 1 lets the arbiter reject bystanders |
| `--no-mirror` | off | Disable the selfie-view horizontal flip |
| `--mapping` | `relative` | `relative` nudges the claw and survives hand swaps; `absolute` maps frame position straight through |
| `--gain F` | `9.0` | Palm radii of hand travel needed to cross the full claw range; higher is less sensitive |
| `--invert-y` | off | Raising the hand increases y instead of decreasing it |
| `--min-cutoff F` | `1.2` | One Euro: lower is steadier when still |
| `--beta F` | `0.35` | One Euro: higher is less laggy when moving |
| `--open-threshold F` | `2.4` | Openness at or above which the hand reads as open |
| `--close-threshold F` | `1.6` | Openness at or below which it reads as closed |
| `--grab-cooldown F` | `1.2` | Seconds before another grab can fire |
| `--emit TARGET` | none | `stdout`, `serial:PORT`, or `HOST:PORT` for UDP |
| `--baud N` | `9600` | Serial baud rate |
| `--serial-rate F` | `20.0` | Maximum position frames per second on the serial link |
| `--no-serial-reset` | off | Keep DTR low so opening the port doesn't reboot the board (also skips the READY handshake) |
| `--list-ports` | — | List serial ports and exit |
| `--verbose` | off | Log every move, and echo the board's replies |
| `--headless` | off | No preview window |

Keys while running (preview window only):

| Key | Action |
| --- | --- |
| `q` / `Esc` | Quit |
| `o` | Calibrate open palm — hold one up first |
| `c` | Calibrate closed fist |
| `r` | Recentre the claw at 0.5, 0.5 |
| `m` | Toggle mirror (re-anchors, so the claw doesn't lurch) |
| `f` | Toggle the status overlay |

### UDP output

One JSON object per datagram; GRAB frames are sent three times for redundancy.

```json
{"x": 0.4213, "y": 0.6120, "grab": false, "active": true}
```

## Hardware

`arduino/claw_receiver/` is an Uno-class sketch that speaks the protocol above.
Flash it, then point the host at the board's port. It replies `READY` once after
boot, and the host waits for that before sending: anything written during the
~1.6s bootloader window after the DTR reset is discarded by the board.

Two things in the sketch are not obvious but matter:

- **Link watchdog.** If the host stops talking — unplugged cable, crashed app —
  the gantry parks itself instead of holding whatever target it last received.
  The host's 250ms heartbeat is what makes silence meaningful.
- **Slew limiting.** Targets are approached at a bounded rate, so a corrupted
  packet or a hand that teleports across the frame can never slam the mechanism
  from one end to the other. Lines are parsed strictly, and anything malformed or
  out of range is ignored rather than acted on.

Pin assignments, travel limits and grab-cycle timings are constants at the top of
the sketch, written for hobby servos on a small rig — match them to your build.
The hoist steps are stubbed with `TODO`, since that part is specific to your
mechanism; if you drive steppers instead of servos, replace `applyOutputs()` and
everything above it stays as-is, because it is transport rather than mechanism.

Without hardware, `--emit stdout` or the UDP emitter is enough to develop
against.

## Layout

```
src/hand_tracker.py         HandLandmarker wrapper; async LIVE_STREAM, thread-safe result handoff
src/hand_features.py        landmarks -> palm centre, palm radius, openness
src/filters.py              One Euro filter and a fixed-rate EMA
src/claw_control.py         hand tracking, arbitration, mapping, grab state machine
src/emitter.py              stdout / UDP / serial transports
src/drawing.py              skeleton, claw pad and openness meter overlays
src/main.py                 camera loop, CLI, keyboard handling
scripts/download_model.py   idempotent model fetch
arduino/claw_receiver/      Arduino sketch: protocol, watchdog, slew limiting, grab cycle
models/                     model bundles (gitignored)
```

`ClawController` is the piece worth reusing. It takes a `HandLandmarkerResult`,
the frame size and a timestamp, and returns one `ClawState`; it has no opinion
about OpenCV, windows, or where the commands go.

```python
from claw_control import ClawController

controller = ClawController(gain=7.0)
state = controller.update(result, width, height, t)
if state.grab:
    fire(state.x, state.y)
```

`HandTracker` is a context manager and is the only place that touches MediaPipe.

## Troubleshooting

**`FileNotFoundError` mentioning `hand_landmarker.task`** — run
`python scripts/download_model.py`. The tracker fails fast on a missing bundle
rather than surfacing an opaque MediaPipe error later.

**`Could not open camera 0`** — another application is holding the device, or the
index is wrong. Try `--camera 1`.

**`AttributeError: module 'mediapipe' has no attribute 'solutions'`** — you're
reading a tutorial written for MediaPipe 0.10. See [Why this exists](#why-this-exists).

**Nothing takes control** — a hand has to read as *open* to take over, and
openness depends on your hand and your lens. Calibrate with `o` and `c`, and
watch the meter: if the score never crosses the open threshold, the threshold is
wrong for you.

**Grabs fire on their own, or not at all** — the two thresholds need a real dead
band between them. Calibration enforces a minimum gap; if you set them by hand,
keep `--open-threshold` comfortably above `--close-threshold`.

**The claw is twitchy, or sluggish** — `--gain` sets travel per palm radius, and
the One Euro pair sets the jitter/lag trade. Adjust gain first, then
`--min-cutoff` with the hand held still, then `--beta` while sweeping.

**`serial: no READY from COM3`** — the sketch isn't flashed, the port is wrong,
or the board doesn't reset on DTR. Check `--list-ports` first. The host continues
regardless, so this is a warning rather than a failure.

**The claw lags behind on serial** — 9600 baud is a hard ceiling. Lower
`--serial-rate` before raising `--baud`, and if you do raise the baud rate,
change it at both ends.

**Low frame rate** — drop the capture resolution (`--width 640 --height 480`) and
`--hands 1` first; both cut per-frame cost meaningfully. The landmarker runs on
CPU by default.

**Overlay lags the video** — expected, and explained under Design notes. It's the
cost of a non-blocking capture loop.

## Roadmap

- [ ] Recorded-clip fixtures, so controller changes are testable without a webcam
- [ ] A second gesture axis — claw depth or grip strength, rather than a fixed cycle
- [ ] Position feedback from the board, instead of assuming commanded == actual
- [ ] An on-screen calibration wizard for first-run setup

## Non-goals

Multi-camera rigs, pose or face tracking, and custom model training. This stays a
focused hand-tracking and gesture layer; if you need the rest, MediaPipe's other
task bundles compose cleanly alongside it.

## Acknowledgements

Landmark model: Google's [MediaPipe Hand Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker),
used under its own terms. Smoothing follows Casiez, Roussel & Vogel, *1€ Filter*
(CHI 2012).

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Johandi Lim.
