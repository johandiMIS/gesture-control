# gesture-control

Computer-vision hand tracking and gesture control, built on MediaPipe + OpenCV.

## Environment

Conda env **`hand-tracking`** (Python 3.11, conda-forge channel). Always run
project code with that interpreter:

```
C:\Users\limjo\miniconda3\envs\hand-tracking\python.exe
```

`conda` is not on the default PATH; it lives at `~/miniconda3/Scripts/conda.exe`.
Create envs with `-c conda-forge --override-channels` — the Anaconda default
channels require a Terms-of-Service acceptance that has not been given.

## Commands

| Task | Command |
| --- | --- |
| Run the live demo | `python src/main.py` |
| Fetch the model | `python scripts/download_model.py` |
| Recreate the env | `conda env create -f environment.yml` |

## MediaPipe API notes — important

This project uses **MediaPipe 1.0.1**, where the legacy solutions API is **gone**:

- `mp.solutions.hands`, `mp.solutions.drawing_utils` — removed. Don't write code
  against them; most tutorials online still do.
- Use `mediapipe.tasks.python.vision.HandLandmarker` with an explicit
  `models/hand_landmarker.task` bundle.
- Connections come from `vision.HandLandmarksConnections.HAND_CONNECTIONS`
  (objects with `.start` / `.end`, not tuples). Drawing is hand-rolled in
  `src/drawing.py`.

## Architecture

- `src/hand_tracker.py` — wraps `HandLandmarker` in `RunningMode.LIVE_STREAM`.
  Detection is asynchronous: `process()` submits a frame and returns the most
  recent result, so the capture loop never blocks on inference. Results can lag
  the current frame slightly.
- `src/drawing.py` — normalized-landmark → pixel conversion and overlay drawing.
- `src/main.py` — camera loop, CLI flags, keyboard handling.

Timestamps passed to `process()` must increase monotonically; `main.py` derives
them from `time.perf_counter()` relative to loop start.

## Platform notes

Windows: open the camera with `cv2.CAP_DSHOW`, otherwise the default MSMF backend
stalls for several seconds on startup.
