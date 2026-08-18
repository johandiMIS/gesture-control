"""Live webcam hand tracking, driving a claw machine.

The hand becomes the joystick: palm position steers the claw within a
normalized 0..1 x 0..1 cabinet, and closing an open palm drops it.

Usage:
    python src/main.py                    # default webcam, overlay only
    python src/main.py --emit stdout      # print commands
    python src/main.py --emit 127.0.0.1:9000   # stream JSON over UDP
    python src/main.py --camera 1 --gain 7 --mapping absolute

Keys:  q / Esc  quit          m  toggle mirror     f  toggle FPS overlay
       o        calibrate open palm (hold it up first)
       c        calibrate closed fist
       r        recentre the claw at 0.5, 0.5
"""

import argparse
import sys
import time
from collections import deque
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claw_control import GESTURE_CLOSED, GESTURE_OPEN, ClawController
from drawing import draw_claw_pad, draw_label, draw_openness_meter, draw_points
from emitter import make_emitter
from hand_tracker import HandTracker

WINDOW = "Claw Control"


def parse_args():
    p = argparse.ArgumentParser(description="Hand gesture claw machine controller")
    p.add_argument("--camera", type=int, default=0, help="webcam index")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--hands", type=int, default=2,
                   help="max hands to track; >1 lets the arbiter reject bystanders")
    p.add_argument("--no-mirror", action="store_true", help="don't flip horizontally")

    p.add_argument("--mapping", choices=("relative", "absolute"), default="relative",
                   help="relative: hand motion nudges the claw, seamless across "
                        "hand swaps. absolute: hand position in frame maps "
                        "straight to claw position")
    p.add_argument("--gain", type=float, default=9.0,
                   help="palm radii of hand travel needed to cross the full "
                        "claw range; higher is less sensitive")
    p.add_argument("--invert-y", action="store_true",
                   help="raising the hand increases y instead of decreasing it")

    p.add_argument("--min-cutoff", type=float, default=1.2,
                   help="One Euro filter: lower is steadier when still")
    p.add_argument("--beta", type=float, default=0.35,
                   help="One Euro filter: higher is less laggy when moving")

    p.add_argument("--open-threshold", type=float, default=2.4)
    p.add_argument("--close-threshold", type=float, default=1.6)
    p.add_argument("--grab-cooldown", type=float, default=1.2)

    p.add_argument("--emit", metavar="TARGET",
                   help="stdout, or HOST:PORT to stream JSON commands over UDP")
    p.add_argument("--verbose", action="store_true", help="also log every move")
    p.add_argument("--headless", action="store_true", help="no preview window")
    return p.parse_args()


def open_camera(index, width, height):
    # CAP_DSHOW avoids the multi-second MSMF startup stall on Windows.
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


def build_controller(args):
    return ClawController(
        mapping=args.mapping,
        gain=args.gain,
        invert_y=args.invert_y,
        open_threshold=args.open_threshold,
        close_threshold=args.close_threshold,
        grab_cooldown=args.grab_cooldown,
        min_cutoff=args.min_cutoff,
        beta=args.beta,
    )


def draw_overlay(frame, state, controller, fps, show_fps):
    for track, is_active in state.tracks:
        draw_points(frame, track.feat.points, active=is_active)
        if is_active:
            wrist = track.feat.points[0]
            draw_label(frame, f"player #{track.id} {track.handedness}",
                       (int(wrist[0]) - 30, int(wrist[1]) + 30))

    draw_claw_pad(frame, state)
    draw_openness_meter(frame, state, controller.open_threshold,
                        controller.close_threshold)

    if show_fps:
        status = "GRAB" if state.grab_active else ("live" if state.active else "no player")
        draw_label(frame, f"{fps:5.1f} FPS | hands: {state.hands_seen} | {status}",
                   (12, 32))
    if not state.active:
        h, w = frame.shape[:2]
        draw_label(frame, "show an open palm to take control", (12, h - 20))


def main():
    args = parse_args()
    mirror = not args.no_mirror
    show_fps = True

    cap = open_camera(args.camera, args.width, args.height)
    controller = build_controller(args)
    emitter = make_emitter(args.emit, args.verbose)

    frame_times = deque(maxlen=30)
    start = time.perf_counter()

    try:
        with HandTracker(num_hands=args.hands) as tracker:
            print("Claw control started. Press q or Esc to quit.")
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("Dropped frame from camera, stopping.")
                    break

                if mirror:
                    frame = cv2.flip(frame, 1)

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                now = time.perf_counter() - start
                result = tracker.process(rgb, int(now * 1000))

                h, w = frame.shape[:2]
                state = controller.update(result, w, h, now)
                emitter.send(state)

                frame_times.append(time.perf_counter())
                span = frame_times[-1] - frame_times[0]
                fps = (len(frame_times) - 1) / span if span > 0 else 0.0

                if args.headless:
                    if state.grab:
                        print(f"GRAB at {state.x:.3f}, {state.y:.3f}")
                    continue

                draw_overlay(frame, state, controller, fps, show_fps)
                cv2.imshow(WINDOW, frame)

                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("m"):
                    mirror = not mirror
                    # The hand jumps across the frame; swallow that delta so the
                    # claw does not lurch.
                    controller.reanchor()
                if key == ord("f"):
                    show_fps = not show_fps
                if key == ord("r"):
                    controller.recenter()
                if key in (ord("o"), ord("c")):
                    kind = GESTURE_OPEN if key == ord("o") else GESTURE_CLOSED
                    value = controller.calibrate(kind)
                    if value is None:
                        print("Calibration needs an active hand in frame.")
                    else:
                        print(f"{kind} measured {value:.2f} -> thresholds "
                              f"open>={controller.open_threshold:.2f} "
                              f"close<={controller.close_threshold:.2f}")
    finally:
        emitter.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
