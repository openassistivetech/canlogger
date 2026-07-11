#!/usr/bin/env python3
"""
direction_beeper_v3.py - Fast Python version of the direction-aware
horn beeper for an R-Net wheelchair.

Why this exists:
  - direction_beeper.sh / direction_beeper_v2.sh fork a subshell on
    every joystick frame (~100 Hz on R-Net) for arithmetic and string
    handling. On a Raspberry Pi that produces 400+ forks per second
    and bash cannot keep up, so the candump pipe buffer fills and the
    script reacts seconds late ("4-second delay" symptom).
  - Python with python-can reads CAN frames natively, no forks, no
    pipes, so timing is reliable and the motion gate can react in
    real time.

What this script does:
  - Listens for joystick frames (02000100#XxYy).
  - Classifies the dominant direction (forward / reverse / left / right)
    with hysteresis so a held stick does not flap between axes.
  - When the direction is stable for STABLE_FRAMES samples (30 ms),
    plays a horn pattern unique to that direction.
  - While the stick is held in the same direction, repeats the pattern
    every REPEAT_INTERVAL_MS.
  - Repeats stop when the stick returns to center (deadzone for at least
    STABLE_FRAMES samples) OR when REQUIRE_MOTION=1 and the motor stops
    drawing current.

Frame formats (from RNET_FRAME_DICTIONARY.md):
  Joystick:       02000100#XxYy        Xx=X axis, Yy=Y axis (signed int8)
                                       X+ = right,   X- = left
                                       Y+ = forward, Y- = reverse
  Motor current:  14300000#LlHh        little-endian 16-bit, 0 = stopped
  Horn start:     0C040100#
  Horn stop:      0C040101#

Patterns:
  forward = 1 short beep
  reverse = 2 short beeps
  left    = 3 short beeps
  right   = 4 short beeps

(Beep duration is not actually controllable on this chair's piezo - any
"long" beep sounds like a short click. Directions are distinguished
purely by COUNT.)

SAFETY -- READ THIS BEFORE RUNNING
  - Run only on YOUR OWN wheelchair.
  - Have someone present.
  - Be on an open test area with a clear escape path.
  - Stop the test the moment the chair behaves unexpectedly.
"""

import argparse
import sys
import time

try:
    import can
except ImportError:
    sys.stderr.write(
        "python-can is not installed. Run:\n"
        "  pip3 install python-can --break-system-packages\n"
    )
    sys.exit(1)

# -----------------------------------------------------------------------------
# Frame IDs from Bumblebee chair
# -----------------------------------------------------------------------------
JOY_ID   = 0x02000200   # Joystick from device 1, 29-bit extended
MOTOR_ID = 0x14300000   # Drive motor current, 29-bit extended
# Horn frames are sent via python-can. cansend writes the same bytes.
HORN_START_ID = 0x0C040100
HORN_STOP_ID  = 0x0C040101

# # -----------------------------------------------------------------------------
# # Frame IDs from Hackathon chair
# # -----------------------------------------------------------------------------
# JOY_ID   = 0x02000100   # Joystick from device 1, 29-bit extended
# MOTOR_ID = 0x14300000   # Drive motor current, 29-bit extended
# # Horn frames are sent via python-can. cansend writes the same bytes.
# HORN_START_ID = 0x0C040100
# HORN_STOP_ID  = 0x0C040101

# TODO: make these overridable via environment variables, like the bash version.
# JOY_ID = int(os.getenv("RNET_JOY_ID", "02000100"), 16)
# MOTOR_ID = int(os.getenv("RNET_MOTOR_ID", "14300000"), 16)
# HORN_START_ID = int(os.getenv("RNET_HORN_START_ID", "0C040100"), 16)
# HORN_STOP_ID = int(os.getenv("RNET_HORN_STOP_ID", "0C040101"), 16)
# INVERT_X = os.getenv("RNET_INVERT_X", "0") == "1"
# INVERT_Y = os.getenv("RNET_INVERT_Y", "0") == "1"

# -----------------------------------------------------------------------------
# Defaults (overridable via CLI)
# -----------------------------------------------------------------------------
DEADZONE_DEFAULT       = 20
HYST_NUM               = 3       # orthogonal axis must exceed active by 3/2 = 1.5x
HYST_DEN               = 2
STABLE_FRAMES_DEFAULT  = 3       # 30 ms at 100 Hz
REPEAT_INTERVAL_MS     = 1500
BEEP_LEN_DEFAULT       = 0.12    # seconds for one horn click
GAP_DEFAULT            = 0.15    # seconds between clicks in a pattern
MOTION_TIMEOUT_DEFAULT = 1.5     # seconds since last non-zero motor frame


# Per-direction click counts. Distinguished by count alone.
PATTERN_COUNTS = {
    "forward": 1,
    "reverse": 2,
    "left":    3,
    "right":   4,
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def signed_i8(byte_value):
    """Decode one byte as signed int8 (two's complement)."""
    return byte_value - 256 if byte_value > 127 else byte_value


def now_ms():
    """Monotonic time in milliseconds."""
    return int(time.monotonic() * 1000)


def classify(x, y, prev_dir, deadzone):
    """
    Classify joystick (x, y) into a direction or None for centered.
    Uses hysteresis so a held direction does not flap between axes.
    """
    ax = abs(x)
    ay = abs(y)
    if ax < deadzone and ay < deadzone:
        return None

    # Hysteresis: orthogonal axis must dominate by HYST_NUM/HYST_DEN.
    if prev_dir in ("left", "right"):
        # Currently turning. Switch to forward/reverse only if Y dominates.
        if ay * HYST_DEN > ax * HYST_NUM:
            return "forward" if y > 0 else "reverse"
        if x > 0:
            return "right"
        if x < 0:
            return "left"
        return prev_dir
    elif prev_dir in ("forward", "reverse"):
        # Currently driving. Switch to left/right only if X dominates.
        if ax * HYST_DEN > ay * HYST_NUM:
            return "right" if x > 0 else "left"
        if y > 0:
            return "forward"
        if y < 0:
            return "reverse"
        return prev_dir
    else:
        # Fresh classification (just left center). Default to turning
        # unless Y clearly dominates X.
        if ay * HYST_DEN > ax * HYST_NUM:
            return "forward" if y > 0 else "reverse"
        return "right" if x > 0 else "left"


# -----------------------------------------------------------------------------
# Horn / pattern playback
# -----------------------------------------------------------------------------

class Horn:
    """Plays horn click patterns via python-can."""

    def __init__(self, bus, beep_len, gap, dry_run=False):
        self.bus = bus
        self.beep_len = beep_len
        self.gap = gap
        self.dry_run = dry_run
        self.start_msg = can.Message(
            arbitration_id=HORN_START_ID, is_extended_id=True, data=b""
        )
        self.stop_msg = can.Message(
            arbitration_id=HORN_STOP_ID,  is_extended_id=True, data=b""
        )

    def _click(self):
        if self.dry_run:
            print("    click")
            time.sleep(self.beep_len)
            return
        try:
            self.bus.send(self.start_msg)
            time.sleep(self.beep_len)
            self.bus.send(self.stop_msg)
            # Belt-and-braces: one extra stop in case the first dropped.
            self.bus.send(self.stop_msg)
        except can.CanError as exc:
            sys.stderr.write("horn send failed: %s\n" % exc)

    def play_pattern(self, count):
        """Play `count` clicks separated by self.gap seconds."""
        for i in range(count):
            self._click()
            if i < count - 1:
                time.sleep(self.gap)

    def silence(self):
        """Force the horn off. Used at shutdown."""
        if self.dry_run:
            return
        try:
            self.bus.send(self.stop_msg)
        except can.CanError:
            pass


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Fast direction-aware horn beeper for R-Net (Python)."
    )
    p.add_argument("--interface", default="can0",
                   help="SocketCAN interface (default: can0)")
    p.add_argument("--bustype", default="socketcan",
                   help="python-can bustype (default: socketcan)")
    p.add_argument("--deadzone", type=int, default=DEADZONE_DEFAULT,
                   help="Joystick magnitude treated as centered "
                        "(default: %d)" % DEADZONE_DEFAULT)
    p.add_argument("--stable", type=int, default=STABLE_FRAMES_DEFAULT,
                   help="Consecutive frames required to confirm a state "
                        "(default: %d)" % STABLE_FRAMES_DEFAULT)
    p.add_argument("--repeat-ms", type=int, default=REPEAT_INTERVAL_MS,
                   help="Repeat interval while direction is held, ms "
                        "(default: %d)" % REPEAT_INTERVAL_MS)
    p.add_argument("--beep-len", type=float, default=BEEP_LEN_DEFAULT,
                   help="Length of one click in seconds "
                        "(default: %.2f)" % BEEP_LEN_DEFAULT)
    p.add_argument("--gap", type=float, default=GAP_DEFAULT,
                   help="Gap between clicks within a pattern "
                        "(default: %.2f)" % GAP_DEFAULT)
    p.add_argument("--require-motion", action="store_true",
                   help="Beep only while drive motor is drawing current")
    p.add_argument("--motion-timeout", type=float,
                   default=MOTION_TIMEOUT_DEFAULT,
                   help="Seconds since last non-zero motor frame to still "
                        "consider the chair as moving (default: %.1f)"
                        % MOTION_TIMEOUT_DEFAULT)
    p.add_argument("--dry-run", action="store_true",
                   help="Do not transmit; print intended actions only")
    p.add_argument("--verbose", action="store_true",
                   help="Print every joystick sample")
    args = p.parse_args()

    # ----- Open the bus (or skip in dry-run) -------------------------------
    if args.dry_run:
        print("[DRY RUN] no frames will be transmitted.")
        bus = None
    else:
        try:
            bus = can.interface.Bus(channel=args.interface, bustype=args.bustype)
        except OSError as exc:
            sys.stderr.write("Could not open %s: %s\n" % (args.interface, exc))
            sys.exit(1)

    horn = Horn(bus, args.beep_len, args.gap, dry_run=args.dry_run)

    # ----- State -----------------------------------------------------------
    candidate_dir = None
    candidate_count = 0
    last_dir = None
    last_beep_ms = 0
    motor_value = 0
    motor_last_ts = 0.0

    print("Listening on %s. Press Ctrl+C to stop." % args.interface)
    print("Patterns:  forward=1  reverse=2  left=3  right=4 clicks")
    print("Deadzone=%d  Stable=%d  Repeat=%dms  Motion gate=%s"
          % (args.deadzone, args.stable, args.repeat_ms,
             "ON" if args.require_motion else "OFF"))
    print()

    try:
        while True:
            # ----------------------------------------------------------
            # Block on the bus. recv() returns one frame per call, so we
            # never fall behind the way bash + candump pipe does.
            # ----------------------------------------------------------
            if bus is None:
                # Dry-run: read fake "X Y" lines from stdin.
                line = sys.stdin.readline()
                if not line:
                    break
                try:
                    x_raw, y_raw = (int(t) for t in line.split())
                except ValueError:
                    continue
                x = signed_i8(x_raw & 0xFF)
                y = signed_i8(y_raw & 0xFF)
                arb_id = JOY_ID  # pretend it was a joystick frame
            else:
                msg = bus.recv(timeout=0.5)
                if msg is None:
                    continue

                # ------ Motor current frame ----------------------------
                if msg.arbitration_id == MOTOR_ID and len(msg.data) >= 2:
                    # Little-endian 16-bit current value
                    motor_value = msg.data[0] | (msg.data[1] << 8)
                    motor_last_ts = time.monotonic()
                    continue

                # ------ Joystick frame ---------------------------------
                if msg.arbitration_id != JOY_ID or len(msg.data) < 2:
                    continue

                arb_id = msg.arbitration_id
                x = signed_i8(msg.data[0])
                y = signed_i8(msg.data[1])

            # ----------------------------------------------------------
            # Classify with hysteresis based on the direction we last
            # actually beeped (last_dir), not the candidate.
            # ----------------------------------------------------------
            current_dir = classify(x, y, last_dir, args.deadzone)

            if args.verbose:
                print("  X=%+4d Y=%+4d  prev=%s cand=%s/%d -> %s motor=%d"
                      % (x, y, last_dir, candidate_dir, candidate_count,
                         current_dir, motor_value))

            # ----------------------------------------------------------
            # Stability filter
            # ----------------------------------------------------------
            if current_dir == candidate_dir:
                candidate_count += 1
            else:
                candidate_dir = current_dir
                candidate_count = 1

            if candidate_count < args.stable:
                continue

            # ----------------------------------------------------------
            # Stable centered: clear last_dir so the next deflection
            # always fires (even if the same direction).
            # ----------------------------------------------------------
            if candidate_dir is None:
                if last_dir is not None:
                    print("[%s] center -> silence" %
                          time.strftime("%H:%M:%S"))
                    last_dir = None
                continue

            # ----------------------------------------------------------
            # Decide whether to fire:
            #   - direction changed: fire immediately
            #   - same direction held: fire if REPEAT_INTERVAL_MS elapsed
            # ----------------------------------------------------------
            now = now_ms()
            if candidate_dir != last_dir:
                fire = True
            elif now - last_beep_ms >= args.repeat_ms:
                fire = True
            else:
                fire = False

            if not fire:
                continue

            # ----------------------------------------------------------
            # Optional motion gate. If the motors are not actually
            # drawing current right now, suppress the beep but keep
            # last_dir so direction changes are still tracked.
            # ----------------------------------------------------------
            if args.require_motion:
                age = time.monotonic() - motor_last_ts
                if motor_value == 0 or age > args.motion_timeout:
                    if candidate_dir != last_dir:
                        print("[%s] X=%+d Y=%+d -> %s (gated, motor=%d, age=%.1fs)"
                              % (time.strftime("%H:%M:%S"),
                                 x, y, candidate_dir, motor_value, age))
                        last_dir = candidate_dir
                        last_beep_ms = now  # don't immediately re-eval
                    continue

            # ----------------------------------------------------------
            # Fire the pattern.
            # ----------------------------------------------------------
            print("[%s] X=%+d Y=%+d -> %s (clicks=%d)"
                  % (time.strftime("%H:%M:%S"),
                     x, y, candidate_dir, PATTERN_COUNTS[candidate_dir]))
            horn.play_pattern(PATTERN_COUNTS[candidate_dir])
            last_dir = candidate_dir
            last_beep_ms = now_ms()

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        # Make absolutely sure the horn is off before we exit.
        horn.silence()
        if bus is not None:
            bus.shutdown()


if __name__ == "__main__":
    main()
