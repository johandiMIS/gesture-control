"""Thin wrapper around the MediaPipe HandLandmarker task.

Runs in LIVE_STREAM mode so webcam frames never block on inference: results
arrive via callback and we keep the most recent one.
"""

import threading
import time
from pathlib import Path

import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    RunningMode,
)

DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "models" / "hand_landmarker.task"


class HandTracker:
    def __init__(
        self,
        model_path=DEFAULT_MODEL,
        num_hands=2,
        min_detection_confidence=0.5,
        min_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    ):
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model not found at {model_path}. Run: python scripts/download_model.py"
            )

        self._lock = threading.Lock()
        self._result = None

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=RunningMode.LIVE_STREAM,
            num_hands=num_hands,
            min_hand_detection_confidence=min_detection_confidence,
            min_hand_presence_confidence=min_presence_confidence,
            min_tracking_confidence=min_tracking_confidence,
            result_callback=self._on_result,
        )
        self._landmarker = HandLandmarker.create_from_options(options)

    def _on_result(self, result, output_image, timestamp_ms):
        with self._lock:
            self._result = result

    def process(self, rgb_frame, timestamp_ms=None):
        """Submit an RGB frame for async detection; returns the latest result.

        The result may lag the submitted frame by a frame or two - that is the
        trade-off for never blocking the capture loop.
        """
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        self._landmarker.detect_async(mp_image, timestamp_ms)

        with self._lock:
            return self._result

    def close(self):
        self._landmarker.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
