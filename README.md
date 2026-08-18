# gesture-control

Real-time hand tracking on a webcam, built on the MediaPipe Tasks API and OpenCV.

Today it does one thing well: it detects up to two hands per frame, resolves 21
landmarks on each, and renders a skeleton overlay with handedness labels and a
live FPS readout — without ever blocking the capture loop on inference. Gesture
classification is the next layer up and is not implemented yet; see
[Roadmap](#roadmap).

```
python src/main.py
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

The secondary goal is a capture loop that stays smooth. Synchronous inference
per frame couples your display rate to your model latency; this one doesn't.

## Design notes

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
API used. Fingertips are drawn larger and in a separate color because they're
the landmarks that matter for nearly every downstream gesture.

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

Requires Python 3.11 and a webcam.

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
model storage into `models/`, and no-ops if it's already there.

## Usage

```bash
python src/main.py
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--camera N` | `0` | Webcam device index |
| `--width` / `--height` | `1280` / `720` | Requested capture resolution (the driver may not honor it) |
| `--hands N` | `2` | Maximum hands to track — lower it to cut per-frame cost |
| `--no-mirror` | off | Disable the selfie-view horizontal flip |

Keys while running:

| Key | Action |
| --- | --- |
| `q` / `Esc` | Quit |
| `m` | Toggle mirror |
| `f` | Toggle the FPS overlay |

## Layout

```
src/hand_tracker.py         HandLandmarker wrapper; async LIVE_STREAM, thread-safe result handoff
src/drawing.py              normalized landmarks -> pixels, skeleton and label rendering
src/main.py                 camera loop, CLI, keyboard handling
scripts/download_model.py   idempotent model fetch
models/                     model bundles (gitignored)
```

`HandTracker` is a context manager and is the only place that touches MediaPipe.
If you want to build on this, import it directly — it takes a frame and a
timestamp and has no opinion about OpenCV, windows, or rendering:

```python
from hand_tracker import HandTracker

with HandTracker(num_hands=1) as tracker:
    result = tracker.process(rgb_frame, timestamp_ms)
    if result and result.hand_landmarks:
        ...  # 21 normalized landmarks per hand
```

## Troubleshooting

**`FileNotFoundError` mentioning `hand_landmarker.task`** — run
`python scripts/download_model.py`. The tracker fails fast on a missing bundle
rather than surfacing an opaque MediaPipe error later.

**`Could not open camera 0`** — another application is holding the device, or the
index is wrong. Try `--camera 1`.

**`AttributeError: module 'mediapipe' has no attribute 'solutions'`** — you're
reading a tutorial written for MediaPipe 0.10. See [Why this exists](#why-this-exists).

**Low frame rate** — drop the capture resolution (`--width 640 --height 480`) and
`--hands 1` first; both cut per-frame cost meaningfully. The landmarker runs on
CPU by default.

**Overlay lags the video** — expected, and explained under Design notes. It's the
cost of a non-blocking capture loop.

## Roadmap

- [ ] Gesture classification from landmark geometry (pinch, point, open palm, fist)
- [ ] Temporal smoothing / debouncing so single-frame noise can't fire an action
- [ ] Action bindings — cursor control, scroll, media keys
- [ ] Recorded-clip regression fixtures, so classifier changes are testable without a webcam

## Non-goals

Multi-camera rigs, pose or face tracking, and custom model training. This stays a
focused hand-tracking and gesture layer; if you need the rest, MediaPipe's other
task bundles compose cleanly alongside it.

## Acknowledgements

Landmark model: Google's [MediaPipe Hand Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker),
used under its own terms.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Johandi Lim.
