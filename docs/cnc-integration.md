# Driving the claw machine from GRBL

Working notes for moving this project's output off the custom `claw_receiver`
protocol and onto an Arduino CNC shield running GRBL. The second half is a
checklist to work through **with the hardware powered and connected** — nothing
below it can be settled from the code alone.

Status: design agreed, blocked on hardware verification. No code written yet.

## The system

Hand tracking already produces everything needed. `ClawController` emits one
`ClawState` per frame — normalized `x`, `y` in 0..1 plus `active` and a `grab`
edge — and has no opinion about transport. Open palm steers the gantry; closing
the fist launches the claw.

The vision half does not change. What changes is `emitter.py`, which is the only
seam that knows about hardware.

## Why the current output cannot drive GRBL

Today the serial emitter sends a private grammar invented for
`arduino/claw_receiver/claw_receiver.ino`:

```
P421,612\n     position, axes as integers 0..1000
G421,612\n     grab at this point
I\n            idle
```

GRBL parses lines as G-code words — a letter followed by a number. Fed the
above it replies `error:2` (bad numeric format) to `P421,612` and `I`, and
`error:20` (unsupported command) to `G421,612`. Nothing moves, and because the
emitter never reads replies, it fails silently from the host side.

Stacked mismatches, all of which have to be fixed together:

| Layer | Now | GRBL needs |
| --- | --- | --- |
| Baud | 9600 | 115200 |
| Grammar | `P/G/I` lines | G-code, answered with `ok` / `error:N` |
| Units | 0..1000 unitless | millimetres in a real coordinate system |
| Startup | send immediately | boots into `ALARM` when homing is on; needs `$H` or `$X` |
| Flow control | fire and forget | 128-byte RX buffer, host must count `ok` |
| Grab | one line on the wire | a multi-second G-code macro |

## Target design

**Motion.** One absolute jog per update:
`$J=G53 G90 G21 X<mm> Y<mm> F<feed>`. Feed is derived from distance over the
update period so each move takes about one period — continuous motion rather
than stop-start — clamped to the machine's own `$110`/`$111` max rates.

At most one jog is left unacknowledged at a time, tracked by counting `ok`
replies. This is the whole latency story: streaming plain `G1` moves at 20 Hz
would queue in GRBL's ~15-block planner and the gantry would chase a trail of
stale hand positions, drifting further behind real time every second. It is also
the safety story — if the host process dies mid-stream, the machine completes
one short queued move and stops.

**Coordinates.** 0..1 maps into a work area in millimetres, targeting G53
machine coordinates. Image Y grows downward and CNC Y grows away from the
operator, so Y flips in the mapping. Every target is clamped to the configured
area, and the area is validated at startup against `$130`/`$131` read from the
board — a config outside the envelope fails fast, before the camera opens.

**Grab.** A templated macro from `config/cnc.toml`, run as a non-blocking state
machine ticked by the capture loop: `0x85` jog cancel → wait for `Idle` → send
macro lines one at a time, each gated on its `ok` → resume jogging. The jog
cancel is not optional; without it queued gantry motion continues after the drop
has started.

**Connect.** Read the boot banner, `$I` and `$$`; auto-detect the version and
refuse `$J` streaming on pre-1.1. Homing is physical motion, so `$H` runs only
under an explicit `--home` flag and `$X` only under `--unlock`.

## Hardware verification checklist

Work through these with the board connected. Each says how to check it and what
it changes in the code, so an unexpected answer is caught before it becomes a
bug in a machine that moves.

### A — Board and link

- [ ] **Q1. What board is it?** USB-C rules out a classic Uno (USB-B). Likely a
      USB-C Uno/Nano clone with a CH340, an ESP32 board running FluidNC or
      grbl_esp32, or an Uno R4.
      *Check:* plug it in, then in PowerShell —
      `Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match 'COM' } | Select-Object Name, DeviceID`
      — and read the VID/PID.
      *Changes:* the connect sequence. A CH340 clone behaves exactly like an Uno.
      ESP32 ports differ on reset behaviour and settings names. Stock GRBL does
      not run on an Uno R4 at all.
- [ ] **Q2. Which COM port, and does it survive a replug?** Some clones
      re-enumerate on a different port number.
      *Check:* `python src/main.py --list-ports`, unplug, replug, run again.
      *Changes:* whether the port can be hard-coded in config or has to be
      selected each run.
- [ ] **Q3. What baud does it actually answer on?** 115200 for GRBL 1.1, 9600
      for 0.9, sometimes 230400 on ESP32 builds.
      *Check:* open a terminal at 115200 and press Enter — a healthy board
      answers `ok` or prints its banner.
- [ ] **Q4. Does opening the port reset the board?** On an Uno-class board DTR
      toggles a reboot, which re-triggers `ALARM` every run.
      *Check:* open the port and watch for the Grbl banner. If it appears, the
      board reset.
      *Changes:* whether the alarm-clear step runs on every start or only once.

### B — Firmware and settings

- [ ] **Q5. Exact firmware and version string.** Earlier described as "grbl v2",
      which is not an official GRBL version — the last official release is 1.1h.
      It may be a vendor build, grblHAL, or FluidNC.
      *Check:* send `$I` and record the reply verbatim.
      *Changes:* whether `$J` jogging exists at all. Pre-1.1 has no jog mode and
      the whole realtime approach needs a worse fallback.
- [ ] **Q6. Full settings dump.** Send `$$` and paste the whole thing. The ones
      that drive code decisions: `$20` soft limits, `$21` hard limits,
      `$22` homing enable, `$23` homing direction invert, `$100`/`$101` steps per
      mm, `$110`/`$111` max rate, `$120`/`$121` acceleration,
      `$130`/`$131`/`$132` max travel.
- [ ] **Q7. Does `$J` work?** With the machine homed and clear, send
      `$J=G91 G21 X5 F500`.
      *Check:* it should move 5 mm and reply `ok`. Then send `0x85` mid-move and
      confirm it decelerates and stops.
      *Changes:* if jog cancel does not work, the grab macro cannot safely
      interrupt gantry motion and needs a feed-hold-based fallback.

### C — Envelope and homing

- [ ] **Q8. Do the limit switches work on all axes?** Run `$H` and confirm each
      axis seeks and finds its switch.
      *Changes:* machine coordinates are only trustworthy if homing is reliable.
      Without it the whole G53 mapping has to fall back to work coordinates.
- [ ] **Q9. What is the usable XY envelope in mm, after homing?** Not the same
      as `$130`/`$131` — the reachable play area is smaller than the mechanical
      travel.
      *Check:* jog to each corner of the area the claw should cover and record
      `MPos` from `?` at each.
      *Changes:* these four numbers are the work area in `config/cnc.toml` and
      define what 0..1 maps onto.
- [ ] **Q10. Which sign is which direction?** GRBL homing usually leaves machine
      coordinates spanning `-max..0`, so the numbers are negative.
      *Check:* jog +X and watch which way the gantry physically moves; same for
      +Y.
      *Changes:* whether Y needs flipping on top of the image-space flip. Getting
      this wrong mirrors the controls.
- [ ] **Q11. What is safe Z, and what is the drop depth?**
      *Check:* jog Z to full travel height (safe) and to the depth where the claw
      reaches the prizes. Record both `MPos` values.
- [ ] **Q12. Where is the prize chute in machine coordinates?**
      *Check:* jog there and record `MPos`.

### D — Claw mechanism

This is the group most likely to force a design change.

- [ ] **Q13. What closes the claw — solenoid, DC motor, or hobby servo?**
      *Why it matters:* GRBL exposes only spindle enable, spindle PWM (D11) and
      coolant (A3) as general outputs. A solenoid or DC motor on a relay or
      MOSFET is driven with `M3`/`M5` and works out of the box. **A hobby servo
      does not** — GRBL's spindle PWM runs around 1 kHz, not the 50 Hz a servo
      needs, so it would need a recompiled firmware (a grbl-servo fork) or a
      separate microcontroller just for the claw.
- [ ] **Q14. Which pin is the claw wired to?** Spindle enable, spindle PWM, or
      coolant.
      *Changes:* whether the macro uses `M3`/`M5` or `M8`/`M9`.
- [ ] **Q15. Is the claw closed-when-powered or open-when-powered?** Many claw
      builds are spring-loaded one way.
      *Changes:* whether the macro energizes to grab and de-energizes to
      release, or the reverse. Getting this backwards drops the prize on the way
      to the chute.
- [ ] **Q16. What raises and lowers the claw — a stepper on Z, or a DC winch?**
      *Why it matters:* a stepper means the plunge is positional (`G1 Z-nn`) and
      repeatable. A winch motor on a relay is timed and open-loop, which GRBL
      cannot position at all — that would need the depth handled by dwell
      duration instead of coordinates.
- [ ] **Q17. How long does a full cycle take?** Time drop, close, lift, traverse,
      release.
      *Changes:* the position freeze in `claw_control.py` is currently a fixed
      `grab_hold=0.8` seconds. On a real gantry the cycle is several seconds and
      variable, so that freeze should be driven by the macro's actual completion
      instead. Q17 gives the fallback timeout.
- [ ] **Q18. Does the claw need to grip during the traverse?** Some mechanisms
      hold power the whole way to the chute, some latch mechanically.

### E — Safety

- [ ] **Q19. Is there a physical e-stop that cuts motor power?** Software feed
      hold is not a substitute.
- [ ] **Q20. Are soft limits (`$20=1`) enabled?** They are the last line of
      defence if the mapping is misconfigured.
- [ ] **Q21. What happens on `ALARM` mid-cycle?** Trigger a limit switch by hand
      during a jog and confirm the machine stops and reports `ALARM`.
      *Changes:* the emitter must suspend all output on `Alarm`, `Hold` and
      `Door` — this verifies that state is actually reported.
- [ ] **Q22. What is the maximum safe feed rate for the gantry?** May be well
      below `$110`/`$111` once a claw and prize are hanging off it.
      *Changes:* the feed clamp in the jog streamer.

## Capture template

Paste the results back in this shape and the config can be filled in directly:

```
Q1  board:
Q2  port:
Q5  $I reply:
Q6  $$ dump:          (paste in full)
Q9  X range (mm):     ..
    Y range (mm):     ..
Q11 safe Z / drop Z:  ..
Q12 chute X,Y:        ..
Q13 claw actuator:    solenoid | DC motor | servo
Q14 claw pin:         spindle enable | spindle PWM | coolant
Q15 polarity:         closed-when-powered | open-when-powered
Q16 hoist:            Z stepper | DC winch
Q17 cycle time (s):
Q22 max safe feed:
```

## Decisions blocked on the above

- Whether the grab macro is pure G-code, or needs a second microcontroller for
  the claw actuator (Q13).
- Whether the plunge is positional or timed (Q16).
- Whether mapping targets G53 machine coordinates or falls back to G54 work
  coordinates (Q8).
- Whether `$J` jogging is available, or a queue-depth-limited `G1` fallback is
  needed (Q5, Q7).
