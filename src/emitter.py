"""Where claw commands go once they leave the controller.

This is the seam between the vision code and whatever actually drives the
cabinet. The controller does not know or care which emitter is in use.

Position is a stream - every frame supersedes the last, so a dropped position
update costs nothing. GRAB is an edge: it happens once, and losing it loses the
round. Every transport here treats the two differently.
"""

import json
import socket
import time

GRAB_REPEATS = 3


class NullEmitter:
    def send(self, state):
        pass

    def close(self):
        pass


class StdoutEmitter:
    """Prints every grab, and position only while a hand is in control."""

    def __init__(self, verbose=False):
        self.verbose = verbose

    def send(self, state):
        if state.grab:
            print(f"GRAB  x={state.x:.3f} y={state.y:.3f}")
        elif self.verbose and state.active:
            print(f"MOVE  x={state.x:.3f} y={state.y:.3f}")

    def close(self):
        pass


class UdpEmitter:
    """One JSON object per datagram."""

    def __init__(self, host, port):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, state):
        payload = json.dumps(state.as_command()).encode()
        repeats = GRAB_REPEATS if state.grab else 1
        for _ in range(repeats):
            self.sock.sendto(payload, self.addr)

    def close(self):
        self.sock.close()


# --------------------------------------------------------------------- serial

# 9600 baud 8N1 is 960 bytes/second, and that is the entire budget. JSON would
# need ~1650 B/s at 30fps - nearly twice the wire - so the link would fall
# steadily further behind real time until the claw was seconds late. Hence a
# compact fixed-grammar line protocol, ~11 bytes a frame, rate limited well
# under the ceiling:
#
#     P<x>,<y>\n    position, both axes as integers 0..1000
#     G<x>,<y>\n    grab, at this exact position
#     I\n           idle - no player in control, hold still
#
# Grab carries its own coordinates rather than relying on the preceding P, so a
# dropped position update can never make the claw drop in the wrong place.
POS_SCALE = 1000
IDLE_FRAME = b"I\n"

# Bytes allowed to sit unsent before position updates start being skipped. One
# frame of slack: beyond that the queue is backing up, and stale positions are
# worth less than a responsive claw.
MAX_BACKLOG = 24

# How long a grab that could not be sent is still worth sending. The claw drops
# where the player aimed a moment ago; firing one that has been stuck in a queue
# for longer than this would drop it into an empty cabinet.
GRAB_TTL = 2.0


def _frame(kind, x, y):
    xi = min(POS_SCALE, max(0, int(round(x * POS_SCALE))))
    yi = min(POS_SCALE, max(0, int(round(y * POS_SCALE))))
    return f"{kind}{xi},{yi}\n".encode("ascii")


class SerialEmitter:
    """Streams the line protocol above to an Arduino.

    rate/heartbeat keep the link inside its budget: position frames go out at
    most `rate` times a second and only when the claw has actually moved, but
    something is always sent at least every `heartbeat` seconds so the sketch's
    watchdog can tell a still hand from a dead PC.
    """

    def __init__(self, port, baud=9600, rate=20.0, heartbeat=0.25,
                 deadband=0.001, ready_timeout=3.0, reset=True, verbose=False,
                 clock=time.monotonic):
        import serial   # imported lazily so the rest of the app runs without it

        self._serial = serial
        # Pacing is about the physical wire, so this is wall clock, not the
        # controller's session time. Injectable so it can be driven in tests.
        self.clock = clock
        self.port = port
        self.verbose = verbose
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.heartbeat = heartbeat
        self.deadband = deadband

        is_url = "://" in port
        if is_url:
            self.ser = serial.serial_for_url(port, baudrate=baud, timeout=0,
                                             write_timeout=0.05)
        else:
            self.ser = serial.Serial()
            self.ser.port = port
            self.ser.baudrate = baud
            self.ser.timeout = 0            # never block the capture loop
            self.ser.write_timeout = 0.05
            # Opening the port toggles DTR, which reboots an Uno/Nano into its
            # bootloader. Leaving it on is the predictable path - we just have
            # to wait the reboot out - but boards wired to stay alive across a
            # reconnect can turn it off.
            self.ser.dtr = reset
            self.ser.open()

        self._pending = []          # grab frames that have not made it out yet
        self._last_tx = 0.0
        self._last_xy = None
        self._last_active = None
        self.dropped = 0
        self.sent = 0

        if not is_url and reset:
            self._await_ready(ready_timeout)

    def _await_ready(self, timeout):
        """Wait for the sketch to announce itself after the reset.

        Anything sent during the ~1.6s bootloader window is discarded by the
        board, so without this the first commands of a session vanish."""
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            buf += self.ser.read(64)
            if b"READY" in buf:
                if self.verbose:
                    print(f"serial: {self.port} ready")
                self.ser.reset_input_buffer()
                return True
            time.sleep(0.02)
        # Not fatal: a sketch that does not print READY still works, it just
        # cost us the timeout to find out.
        print(f"serial: no READY from {self.port} within {timeout:.0f}s, "
              f"continuing anyway")
        self.ser.reset_input_buffer()
        return False

    def _backlogged(self):
        try:
            return self.ser.out_waiting > MAX_BACKLOG
        except (OSError, NotImplementedError, AttributeError):
            return False

    def _write(self, payload):
        """Attempt one write. Queue policy is the caller's decision."""
        try:
            self.ser.write(payload)
            self.sent += len(payload)
            return True
        except self._serial.SerialTimeoutException:
            return False
        except self._serial.SerialException as exc:
            print(f"serial: write failed on {self.port}: {exc}")
            return False

    def _drain_input(self):
        """Read and discard whatever the sketch has said. Left unread it just
        accumulates in the OS buffer."""
        try:
            data = self.ser.read(256)
        except self._serial.SerialException:
            return
        if data and self.verbose:
            for line in data.decode("ascii", "replace").splitlines():
                if line.strip():
                    print(f"  arduino: {line.strip()}")

    def send(self, state):
        now = self.clock()
        self._drain_input()

        # Grabs that previously failed to go out get priority over anything new,
        # unless they have gone stale waiting.
        self._pending = [(exp, f) for exp, f in self._pending if exp > now]
        while self._pending:
            if not self._write(self._pending[0][1]):
                return          # link still blocked; try again next frame
            self._pending.pop(0)
            self._last_tx = now

        if state.grab:
            # Bypasses the rate limit and the backlog check - this is the one
            # message that must not be skipped.
            frame = _frame("G", state.x, state.y)
            if not self._write(frame):
                self._pending.append((now + GRAB_TTL, frame))
            self._last_tx = now
            self._last_xy = (state.x, state.y)
            self._last_active = state.active
            return

        moved = (
            self._last_xy is None
            or abs(state.x - self._last_xy[0]) >= self.deadband
            or abs(state.y - self._last_xy[1]) >= self.deadband
            or state.active != self._last_active
        )
        since = now - self._last_tx
        if not ((since >= self.interval and moved) or since >= self.heartbeat):
            return
        if self._backlogged():
            self.dropped += 1
            return

        payload = _frame("P", state.x, state.y) if state.active else IDLE_FRAME
        if self._write(payload):
            self._last_tx = now
            self._last_xy = (state.x, state.y)
            self._last_active = state.active
        else:
            self.dropped += 1

    def close(self):
        try:
            self.ser.write(IDLE_FRAME)   # leave the machine parked, not latched
            self.ser.flush()
        except Exception:
            pass
        self.ser.close()
        if self.verbose:
            print(f"serial: {self.sent} bytes sent, {self.dropped} frames dropped")


def list_ports():
    from serial.tools import list_ports as _lp
    return [(p.device, p.description) for p in _lp.comports()]


def make_emitter(spec, verbose=False, baud=9600, rate=20.0, reset=True):
    """`spec` is None, "stdout", "serial:PORT", or "HOST:PORT" for UDP."""
    if not spec:
        return StdoutEmitter(verbose) if verbose else NullEmitter()
    if spec == "stdout":
        return StdoutEmitter(verbose=True)
    if spec.startswith("serial:"):
        return SerialEmitter(spec[len("serial:"):], baud=baud, rate=rate,
                             reset=reset, verbose=verbose)
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(
            f"Bad emitter spec {spec!r}; expected stdout, serial:PORT, or HOST:PORT")
    return UdpEmitter(host, int(port))
