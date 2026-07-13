#!/usr/bin/env python3
"""
direction_beeper_profile.py - Direction-aware horn beeper for an R-Net wheelchair.

Example call:
python3 direction_beeper_updated.py \
  --interface can0 \
  --profile meet_greet_files/rnet_meet_greet_profile.json \
  --enable-lights

What this script does:
  - Listens for the chair-specific joystick CAN frame.
  - Classifies joystick movement as forward / reverse / left / right.
  - Plays a horn click pattern for the stable direction.
  - Optionally gates beeps on motor-current activity.

Meet-and-greet profile support:
  - By default, this script can load rnet_meet_greet_profile.json using --profile.
  - It uses the confirmed joystick CAN ID from the profile instead of hardcoding it.
  - If the profile contains direction mapping, it uses that mapping instead of assuming
    X+ = right, X- = left, Y+ = forward, Y- = reverse.
  - --joystick-id can still manually override the profile.

Safety:
  - Run only on your own wheelchair.
  - Test in an open area with a spotter.
  - Stop immediately if the chair behaves unexpectedly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import can
except ImportError:
    sys.stderr.write(
        "python-can is not installed. Run:\n"
        "  pip3 install python-can --break-system-packages\n"
    )
    sys.exit(1)

# -----------------------------------------------------------------------------
# Default frame IDs. These can be overridden by --profile, --joystick-id,
# and --motor-id.
# -----------------------------------------------------------------------------
DEFAULT_JOYSTICK_ID = 0x02000200
DEFAULT_MOTOR_ID = 0x14300000
HORN_START_ID = 0x0C040100
HORN_STOP_ID = 0x0C040101

# -----------------------------------------------------------------------------
# Defaults, overridable via CLI.
# -----------------------------------------------------------------------------
DEADZONE_DEFAULT = 20
HYST_NUM = 3               # orthogonal axis must exceed active by 3/2 = 1.5x
HYST_DEN = 2
STABLE_FRAMES_DEFAULT = 3  # 30 ms at 100 Hz
REPEAT_INTERVAL_MS = 1500
BEEP_LEN_DEFAULT = 0.12
GAP_DEFAULT = 0.15
MOTION_TIMEOUT_DEFAULT = 1.5

# Per-direction click counts. Distinguished by count alone.
PATTERN_COUNTS = {
    "forward": 1,
    "reverse": 2,
    "left": 3,
    "right": 4,
}

DEFAULT_DIRECTION_MAPPING = {
    "forward": {"axis": "y", "sign": 1, "source": "default"},
    "reverse": {"axis": "y", "sign": -1, "source": "default"},
    "left": {"axis": "x", "sign": -1, "source": "default"},
    "right": {"axis": "x", "sign": 1, "source": "default"},
}

OPPOSITE_DIRECTIONS = {
    "forward": "reverse",
    "reverse": "forward",
    "left": "right",
    "right": "left",
}

AXIS_FOR_DIRECTION_GROUP = {
    "forward": "y",
    "reverse": "y",
    "left": "x",
    "right": "x",
}


@dataclass
class JoystickConfig:
    can_id: int = DEFAULT_JOYSTICK_ID
    center_x: int = 0
    center_y: int = 0
    directions: dict[str, dict[str, Any]] = field(
        default_factory=lambda: dict(DEFAULT_DIRECTION_MAPPING)
    )
    source: str = "fallback_default"


# -----------------------------------------------------------------------------
# Profile loading helpers
# -----------------------------------------------------------------------------

def parse_can_id_value(value: Any) -> int | None:
    """Parse CAN IDs stored as int, '0x02000200', or '02000200'."""
    if value is None:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 0)
        except ValueError:
            try:
                return int(text, 16)
            except ValueError:
                return None

    return None


def load_json_file(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def direction_mapping_from_info(
    direction_info: dict[str, Any] | None,
    *,
    source: str,
) -> dict[str, Any] | None:
    if not direction_info:
        return None

    axis = direction_info.get("axis")
    sign = direction_info.get("sign")

    if axis not in {"x", "y"}:
        return None

    if sign not in {-1, 1}:
        return None

    return {
        "axis": axis,
        "sign": sign,
        "source": source,
        "primary_abs_peak": direction_info.get("primary_abs_peak"),
        "signed_peak_from_center": direction_info.get("signed_peak_from_center"),
    }


def merge_direction_mapping(
    base: dict[str, dict[str, Any]],
    updates: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = dict(base)
    for direction_name, direction_info in updates.items():
        if direction_name in PATTERN_COUNTS:
            merged[direction_name] = direction_info
    return merged


def extract_direction_mapping_from_confirmed(
    confirmed: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    directions: dict[str, dict[str, Any]] = {}
    confirmed_mapping = confirmed.get("joystick_mapping") or {}

    for direction_name in PATTERN_COUNTS:
        direction_info = confirmed_mapping.get(direction_name) or {}
        parsed = direction_mapping_from_info(
            direction_info,
            source="confirmed.joystick_mapping",
        )
        if parsed is not None:
            directions[direction_name] = parsed

    return directions


def extract_direction_mapping_from_steps(
    steps: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    directions: dict[str, dict[str, Any]] = {}

    # Calibration mapping is useful as a baseline.
    calibration_step = steps.get("joystick_calibration", {})
    observations = calibration_step.get("observations", {})
    recognition = observations.get("recognition", {})
    best_candidate = recognition.get("best_candidate") or {}
    inferred_mapping = best_candidate.get("inferred_mapping") or {}

    for direction_name in PATTERN_COUNTS:
        direction_info = inferred_mapping.get(direction_name) or {}
        parsed = direction_mapping_from_info(
            direction_info,
            source="joystick_calibration.inferred_mapping",
        )
        if parsed is not None:
            directions[direction_name] = parsed

    # Independent direction tests can override calibration with stronger samples.
    for direction_name in PATTERN_COUNTS:
        step_key = f"joystick_{direction_name}"
        step = steps.get(step_key, {})
        observations = step.get("observations", {})
        recognition = observations.get("recognition", {})

        if recognition.get("status") != "confirmed":
            continue

        best = recognition.get("best_candidate") or {}
        direction_range = best.get("direction_range") or {}
        parsed = direction_mapping_from_info(
            direction_range,
            source=f"{step_key}.direction_range",
        )
        if parsed is not None:
            directions[direction_name] = parsed

    return directions


def extract_joystick_config_from_profile(profile: dict[str, Any]) -> JoystickConfig:
    """Extract joystick ID, center, and direction mapping from a profile JSON."""
    confirmed = profile.get("confirmed", {})
    steps = profile.get("steps", {})

    joystick_can_id = parse_can_id_value(
        confirmed.get("joystick_can_id_int")
        or confirmed.get("joystick_can_id")
    )
    center = confirmed.get("joystick_center") or {}
    source = "profile.confirmed"

    if joystick_can_id is None:
        calibration_step = steps.get("joystick_calibration", {})
        observations = calibration_step.get("observations", {})
        recognition = observations.get("recognition", {})
        best_candidate = recognition.get("best_candidate") or {}
        joystick_can_id = parse_can_id_value(
            best_candidate.get("can_id_int")
            or best_candidate.get("can_id")
        )
        center = best_candidate.get("center") or center
        source = "profile.steps.joystick_calibration"

    if joystick_can_id is None:
        raise ValueError(
            "Could not find a confirmed joystick CAN ID in the meet-and-greet profile."
        )

    directions = dict(DEFAULT_DIRECTION_MAPPING)
    directions = merge_direction_mapping(
        directions,
        extract_direction_mapping_from_confirmed(confirmed),
    )
    directions = merge_direction_mapping(
        directions,
        extract_direction_mapping_from_steps(steps),
    )

    return JoystickConfig(
        can_id=joystick_can_id,
        center_x=int(center.get("x", 0) or 0),
        center_y=int(center.get("y", 0) or 0),
        directions=directions,
        source=source,
    )


def load_joystick_config_from_profile(profile_path: str | Path) -> JoystickConfig:
    profile = load_json_file(profile_path)
    return extract_joystick_config_from_profile(profile)


def parse_cli_can_id(value: str) -> int:
    parsed = parse_can_id_value(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"Invalid CAN ID: {value}")
    return parsed


# -----------------------------------------------------------------------------
# Joystick classification helpers
# -----------------------------------------------------------------------------

def signed_i8(byte_value: int) -> int:
    """Decode one byte as signed int8, using two's complement."""
    return byte_value - 256 if byte_value > 127 else byte_value


def now_ms() -> int:
    """Monotonic time in milliseconds."""
    return int(time.monotonic() * 1000)


def normalized_direction_value(
    dx: int,
    dy: int,
    direction_info: dict[str, Any],
) -> int | None:
    """
    Return a positive value when dx/dy points toward the named direction.

    Example:
      forward axis=y sign=1 -> dy
      reverse axis=y sign=-1 -> -dy
    """
    axis = direction_info.get("axis")
    sign = direction_info.get("sign")

    if axis == "x":
        value = dx
    elif axis == "y":
        value = dy
    else:
        return None

    if sign not in {-1, 1}:
        return None

    return value * sign


def strongest_direction(
    dx: int,
    dy: int,
    joystick_config: JoystickConfig,
) -> tuple[str | None, int]:
    best_direction = None
    best_value = 0

    for direction_name, direction_info in joystick_config.directions.items():
        value = normalized_direction_value(dx, dy, direction_info)
        if value is None:
            continue
        if value > best_value:
            best_direction = direction_name
            best_value = value

    return best_direction, best_value


def classify_with_profile(
    dx: int,
    dy: int,
    prev_dir: str | None,
    deadzone: int,
    joystick_config: JoystickConfig,
) -> str | None:
    """
    Classify joystick movement using profile-derived axis/sign mapping.

    Hysteresis behavior mirrors the old script:
      - If already driving forward/reverse, switch to left/right only when
        the turn axis clearly dominates.
      - If already turning, switch to forward/reverse only when the drive
        axis clearly dominates.
    """
    strongest, strongest_value = strongest_direction(dx, dy, joystick_config)
    if strongest is None or strongest_value < deadzone:
        return None

    if prev_dir in PATTERN_COUNTS:
        prev_info = joystick_config.directions.get(prev_dir)
        prev_value = (
            normalized_direction_value(dx, dy, prev_info)
            if prev_info is not None
            else None
        )

        if prev_value is not None and prev_value >= deadzone:
            prev_axis_group = AXIS_FOR_DIRECTION_GROUP.get(prev_dir)
            strongest_axis_group = AXIS_FOR_DIRECTION_GROUP.get(strongest)

            # Stay in the same axis group unless the other group dominates by 1.5x.
            if prev_axis_group == strongest_axis_group:
                return strongest

            if strongest_value * HYST_DEN > prev_value * HYST_NUM:
                return strongest

            return prev_dir

    return strongest


# -----------------------------------------------------------------------------
# Horn / pattern playback
# -----------------------------------------------------------------------------

class Horn:
    """Play horn click patterns via python-can."""

    def __init__(self, bus: Any, beep_len: float, gap: float, dry_run: bool = False):
        self.bus = bus
        self.beep_len = beep_len
        self.gap = gap
        self.dry_run = dry_run
        self.start_msg = can.Message(
            arbitration_id=HORN_START_ID,
            is_extended_id=True,
            data=b"",
        )
        self.stop_msg = can.Message(
            arbitration_id=HORN_STOP_ID,
            is_extended_id=True,
            data=b"",
        )

    def _click(self) -> None:
        if self.dry_run:
            print("    click")
            time.sleep(self.beep_len)
            return

        try:
            self.bus.send(self.start_msg)
            time.sleep(self.beep_len)
            self.bus.send(self.stop_msg)
            # Extra stop in case one stop frame drops.
            self.bus.send(self.stop_msg)
        except can.CanError as exc:
            sys.stderr.write(f"horn send failed: {exc}\n")

    def play_pattern(self, count: int) -> None:
        for i in range(count):
            self._click()
            if i < count - 1:
                time.sleep(self.gap)

    def silence(self) -> None:
        if self.dry_run:
            return

        try:
            self.bus.send(self.stop_msg)
        except can.CanError:
            pass


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Direction-aware horn beeper for R-Net using a meet-and-greet profile."
    )
    p.add_argument("--interface", default="can0", help="SocketCAN interface")
    p.add_argument("--bustype", default="socketcan", help="python-can bustype")
    p.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Path to rnet_meet_greet_profile.json",
    )
    p.add_argument(
        "--joystick-id",
        type=parse_cli_can_id,
        default=None,
        help="Manual joystick CAN ID override, e.g. 0x02000200",
    )
    p.add_argument(
        "--motor-id",
        type=parse_cli_can_id,
        default=DEFAULT_MOTOR_ID,
        help="Motor/current CAN ID for --require-motion, e.g. 0x14300000",
    )
    p.add_argument(
        "--deadzone",
        type=int,
        default=DEADZONE_DEFAULT,
        help=f"Joystick magnitude treated as centered (default: {DEADZONE_DEFAULT})",
    )
    p.add_argument(
        "--stable",
        type=int,
        default=STABLE_FRAMES_DEFAULT,
        help=f"Consecutive frames required to confirm state (default: {STABLE_FRAMES_DEFAULT})",
    )
    p.add_argument(
        "--repeat-ms",
        type=int,
        default=REPEAT_INTERVAL_MS,
        help=f"Repeat interval while direction is held, ms (default: {REPEAT_INTERVAL_MS})",
    )
    p.add_argument(
        "--beep-len",
        type=float,
        default=BEEP_LEN_DEFAULT,
        help=f"Length of one click in seconds (default: {BEEP_LEN_DEFAULT:.2f})",
    )
    p.add_argument(
        "--gap",
        type=float,
        default=GAP_DEFAULT,
        help=f"Gap between clicks within a pattern (default: {GAP_DEFAULT:.2f})",
    )
    p.add_argument(
        "--require-motion",
        action="store_true",
        help="Beep only while drive motor is drawing current",
    )
    p.add_argument(
        "--motion-timeout",
        type=float,
        default=MOTION_TIMEOUT_DEFAULT,
        help=f"Seconds since last non-zero motor frame to count as moving (default: {MOTION_TIMEOUT_DEFAULT:.1f})",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not transmit; print intended actions only")
    p.add_argument("--verbose", action="store_true", help="Print every joystick sample")
    return p


def load_runtime_joystick_config(args: argparse.Namespace) -> JoystickConfig:
    if args.profile is not None:
        joystick_config = load_joystick_config_from_profile(args.profile)
    else:
        joystick_config = JoystickConfig()

    if args.joystick_id is not None:
        joystick_config.can_id = args.joystick_id
        joystick_config.source = "manual_override"

    return joystick_config


def print_startup_summary(args: argparse.Namespace, joystick_config: JoystickConfig) -> None:
    print(f"Listening on {args.interface}. Press Ctrl+C to stop.")
    print("Patterns:  forward=1  reverse=2  left=3  right=4 clicks")
    print(
        f"Joystick ID=0x{joystick_config.can_id:08X} from {joystick_config.source}"
    )
    print(f"Joystick center: X={joystick_config.center_x} Y={joystick_config.center_y}")
    print("Joystick direction mapping:")
    for direction_name in ["forward", "reverse", "left", "right"]:
        info = joystick_config.directions.get(direction_name, {})
        print(
            f"  {direction_name:7s}: axis={info.get('axis')} "
            f"sign={info.get('sign')} source={info.get('source')}"
        )
    print(
        f"Deadzone={args.deadzone}  Stable={args.stable}  "
        f"Repeat={args.repeat_ms}ms  Motion gate={'ON' if args.require_motion else 'OFF'}"
    )
    if args.require_motion:
        print(f"Motor/current ID=0x{args.motor_id:08X}")
    print()


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        joystick_config = load_runtime_joystick_config(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Could not load joystick profile: {exc}\n")
        sys.exit(1)

    if args.dry_run:
        print("[DRY RUN] no frames will be transmitted.")
        bus = None
    else:
        try:
            bus = can.interface.Bus(channel=args.interface, interface=args.bustype)
        except OSError as exc:
            sys.stderr.write(f"Could not open {args.interface}: {exc}\n")
            sys.exit(1)

    horn = Horn(bus, args.beep_len, args.gap, dry_run=args.dry_run)

    candidate_dir = None
    candidate_count = 0
    last_dir = None
    last_beep_ms = 0
    motor_value = 0
    motor_last_ts = 0.0

    print_startup_summary(args, joystick_config)

    try:
        while True:
            if bus is None:
                # Dry-run input format: "X Y" raw byte values or signed values.
                line = sys.stdin.readline()
                if not line:
                    break
                try:
                    x_raw, y_raw = (int(token) for token in line.split())
                except ValueError:
                    continue
                x = signed_i8(x_raw & 0xFF)
                y = signed_i8(y_raw & 0xFF)
            else:
                msg = bus.recv(timeout=0.5)
                if msg is None:
                    continue

                if msg.arbitration_id == args.motor_id and len(msg.data) >= 2:
                    motor_value = msg.data[0] | (msg.data[1] << 8)
                    if motor_value != 0:
                        motor_last_ts = time.monotonic()
                    continue

                if msg.arbitration_id != joystick_config.can_id or len(msg.data) < 2:
                    continue

                x = signed_i8(msg.data[0])
                y = signed_i8(msg.data[1])

            dx = x - joystick_config.center_x
            dy = y - joystick_config.center_y

            current_dir = classify_with_profile(
                dx,
                dy,
                last_dir,
                args.deadzone,
                joystick_config,
            )

            if args.verbose:
                print(
                    "  X=%+4d Y=%+4d dX=%+4d dY=%+4d prev=%s cand=%s/%d -> %s motor=%d"
                    % (
                        x,
                        y,
                        dx,
                        dy,
                        last_dir,
                        candidate_dir,
                        candidate_count,
                        current_dir,
                        motor_value,
                    )
                )

            if current_dir == candidate_dir:
                candidate_count += 1
            else:
                candidate_dir = current_dir
                candidate_count = 1

            if candidate_count < args.stable:
                continue

            if candidate_dir is None:
                if last_dir is not None:
                    print(f"[{time.strftime('%H:%M:%S')}] center -> silence")
                    last_dir = None
                continue

            now = now_ms()
            if candidate_dir != last_dir:
                fire = True
            elif now - last_beep_ms >= args.repeat_ms:
                fire = True
            else:
                fire = False

            if not fire:
                continue

            if args.require_motion:
                age = time.monotonic() - motor_last_ts
                if motor_value == 0 or age > args.motion_timeout:
                    if candidate_dir != last_dir:
                        print(
                            "[%s] X=%+d Y=%+d dX=%+d dY=%+d -> %s "
                            "(gated, motor=%d, age=%.1fs)"
                            % (
                                time.strftime("%H:%M:%S"),
                                x,
                                y,
                                dx,
                                dy,
                                candidate_dir,
                                motor_value,
                                age,
                            )
                        )
                        last_dir = candidate_dir
                        last_beep_ms = now
                    continue

            print(
                "[%s] X=%+d Y=%+d dX=%+d dY=%+d -> %s (clicks=%d)"
                % (
                    time.strftime("%H:%M:%S"),
                    x,
                    y,
                    dx,
                    dy,
                    candidate_dir,
                    PATTERN_COUNTS[candidate_dir],
                )
            )
            horn.play_pattern(PATTERN_COUNTS[candidate_dir])
            last_dir = candidate_dir
            last_beep_ms = now_ms()

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        horn.silence()
        if bus is not None:
            bus.shutdown()


if __name__ == "__main__":
    main()
