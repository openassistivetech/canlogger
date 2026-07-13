#!/usr/bin/env python3
"""
direction_beeper.py - Direction-aware horn and light feedback
for an R-Net wheelchair using a meet-and-greet profile.

Sample call:
  python3 direction_beeper.py \
    --interface can0 \
    --profile meet_greet_files/rnet_meet_greet_profile.json \
    --enable-lights

What this script does:
  - Listens for the chair-specific joystick CAN frame.
  - Loads joystick ID, center, direction mapping, horn IDs, light toggle IDs,
    and observed joystick ranges from rnet_meet_greet_profile.json when present.
  - Derives direction thresholds from the observed joystick ranges unless
    --deadzone is explicitly provided.
  - Waits a short configurable delay before starting a feedback pattern so
    tiny course corrections do not immediately chirp.
  - Cancels horn feedback immediately when the observed direction changes.
  - Optionally turns on direction-specific light toggles after the direction
    delay, then turns them off when the joystick returns to center or changes
    to another direction.

Safety:
  - Run only on your own wheelchair.
  - Test in an open area with a spotter.
  - Stop immediately if the chair behaves unexpectedly.
  - Light commands are toggle commands, so start with hazards, indicators, and
    flood/headlights off when using --enable-lights. The script balances each
    on-toggle with an off-toggle when the held direction ends.
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
# Fallback frame IDs. Profile values override these when available.
# -----------------------------------------------------------------------------
DEFAULT_JOYSTICK_ID = 0x02000200
DEFAULT_MOTOR_ID = 0x14300000
DEFAULT_HORN_START_ID = 0x0C040100
DEFAULT_HORN_STOP_ID = 0x0C040101

DEFAULT_LIGHT_TOGGLE_IDS = {
    "flood_headlight": 0x0C000104,
    "hazard": 0x0C000103,
    "left_indicator": 0x0C000101,
    "right_indicator": 0x0C000102,
}

DIRECTION_LIGHTS = {
    "forward": "flood_headlight",
    "reverse": "hazard",
    "left": "left_indicator",
    "right": "right_indicator",
}

# -----------------------------------------------------------------------------
# Defaults, overridable via CLI.
# -----------------------------------------------------------------------------
DEADZONE_FALLBACK = 20
MIN_DERIVED_DEADZONE_DEFAULT = 4
MAX_DERIVED_DEADZONE_DEFAULT = 20
ACTIVATION_FRACTION_DEFAULT = 0.30
HYST_NUM = 3
HYST_DEN = 2
STABLE_FRAMES_DEFAULT = 3
START_DELAY_MS_DEFAULT = 250
REPEAT_INTERVAL_MS = 1500
BEEP_LEN_DEFAULT = 0.12
GAP_DEFAULT = 0.15
MOTION_TIMEOUT_DEFAULT = 1.5

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

AXIS_FOR_DIRECTION_GROUP = {
    "forward": "y",
    "reverse": "y",
    "left": "x",
    "right": "x",
}

PROFILE_LIGHT_STEPS = {
    "flood_headlight": "flood_headlight",
    "hazard": "hazard",
    "left_indicator": "left_indicator",
    "right_indicator": "right_indicator",
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


@dataclass
class ActionConfig:
    horn_start_id: int = DEFAULT_HORN_START_ID
    horn_stop_id: int = DEFAULT_HORN_STOP_ID
    horn_source: str = "fallback_default"
    light_toggle_ids: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_LIGHT_TOGGLE_IDS)
    )
    light_sources: dict[str, str] = field(
        default_factory=lambda: {
            name: "fallback_default" for name in DEFAULT_LIGHT_TOGGLE_IDS
        }
    )


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


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    return None


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

    primary_abs_peak = as_int(direction_info.get("primary_abs_peak"))
    signed_peak = as_int(direction_info.get("signed_peak_from_center"))

    phase = direction_info.get("phase") or {}
    if primary_abs_peak is None:
        if axis == "x":
            primary_abs_peak = as_int(phase.get("max_abs_dx"))
        elif axis == "y":
            primary_abs_peak = as_int(phase.get("max_abs_dy"))

    if signed_peak is None:
        signed_peak = as_int(phase.get("signed_peak"))

    if primary_abs_peak is None and signed_peak is not None:
        primary_abs_peak = abs(signed_peak)

    return {
        "axis": axis,
        "sign": sign,
        "source": source,
        "primary_abs_peak": primary_abs_peak,
        "signed_peak_from_center": signed_peak,
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


def extract_action_config_from_profile(profile: dict[str, Any]) -> ActionConfig:
    confirmed = profile.get("confirmed", {})
    steps = profile.get("steps", {})
    config = ActionConfig()

    # Optional future promoted shape.
    promoted_horn = confirmed.get("horn") or {}
    start_id = parse_can_id_value(
        confirmed.get("horn_start_id")
        or promoted_horn.get("start_id")
        or promoted_horn.get("start")
    )
    stop_id = parse_can_id_value(
        confirmed.get("horn_stop_id")
        or promoted_horn.get("stop_id")
        or promoted_horn.get("stop")
    )
    if start_id is not None and stop_id is not None:
        config.horn_start_id = start_id
        config.horn_stop_id = stop_id
        config.horn_source = "confirmed.horn"

    # Current meet-and-greet shape: steps.horn.observations.recognition.
    horn_recognition = (
        steps.get("horn", {})
        .get("observations", {})
        .get("recognition", {})
    )
    if horn_recognition:
        joystick_pair_count = (
            horn_recognition.get("joystick_evidence", {})
            .get("pair_count", 0)
        )
        programmer_pair_count = (
            horn_recognition.get("programmer_evidence", {})
            .get("pair_count", 0)
        )

        joystick_start = parse_can_id_value(
            horn_recognition.get("expected_joystick_start_id")
        )
        joystick_stop = parse_can_id_value(
            horn_recognition.get("expected_joystick_stop_id")
        )
        programmer_start = parse_can_id_value(
            horn_recognition.get("expected_programmer_start_id")
        )
        programmer_stop = parse_can_id_value(
            horn_recognition.get("expected_programmer_stop_id")
        )

        # Prefer the joystick-button horn frames if observed. If the profile only
        # observed programmer/diagnostic horn frames, use those instead.
        if joystick_pair_count and joystick_start is not None and joystick_stop is not None:
            config.horn_start_id = joystick_start
            config.horn_stop_id = joystick_stop
            config.horn_source = "steps.horn.joystick_evidence"
        elif programmer_pair_count and programmer_start is not None and programmer_stop is not None:
            config.horn_start_id = programmer_start
            config.horn_stop_id = programmer_stop
            config.horn_source = "steps.horn.programmer_evidence"
        elif joystick_start is not None and joystick_stop is not None:
            config.horn_start_id = joystick_start
            config.horn_stop_id = joystick_stop
            config.horn_source = "steps.horn.expected_joystick_ids"

    promoted_lights = confirmed.get("light_toggle_ids") or confirmed.get("lights") or {}
    for light_name in DEFAULT_LIGHT_TOGGLE_IDS:
        value = promoted_lights.get(light_name)
        if isinstance(value, dict):
            value = value.get("toggle_id") or value.get("id")
        parsed = parse_can_id_value(value)
        if parsed is not None:
            config.light_toggle_ids[light_name] = parsed
            config.light_sources[light_name] = "confirmed.light_toggle_ids"

    for step_key, light_name in PROFILE_LIGHT_STEPS.items():
        recognition = (
            steps.get(step_key, {})
            .get("observations", {})
            .get("recognition", {})
        )
        parsed = parse_can_id_value(recognition.get("expected_physical_toggle_id"))
        if parsed is None:
            continue

        status = recognition.get("status")
        physical_count = recognition.get("physical_toggle_count", 0)

        if physical_count or status in {"confirmed", "candidate"}:
            config.light_toggle_ids[light_name] = parsed
            config.light_sources[light_name] = f"steps.{step_key}.expected_physical_toggle_id"

    return config


def parse_cli_can_id(value: str) -> int:
    parsed = parse_can_id_value(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"Invalid CAN ID: {value}")
    return parsed


# -----------------------------------------------------------------------------
# Joystick classification helpers
# -----------------------------------------------------------------------------

def signed_i8(byte_value: int) -> int:
    return byte_value - 256 if byte_value > 127 else byte_value


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def direction_peak(direction_info: dict[str, Any]) -> int | None:
    peak = as_int(direction_info.get("primary_abs_peak"))
    if peak is not None and peak > 0:
        return peak

    signed_peak = as_int(direction_info.get("signed_peak_from_center"))
    if signed_peak is not None and signed_peak != 0:
        return abs(signed_peak)

    return None


def derive_direction_thresholds(
    joystick_config: JoystickConfig,
    *,
    manual_deadzone: int | None,
    activation_fraction: float,
    minimum_deadzone: int,
    maximum_deadzone: int,
) -> tuple[dict[str, int], str]:
    if manual_deadzone is not None:
        return (
            {direction_name: manual_deadzone for direction_name in PATTERN_COUNTS},
            "manual --deadzone",
        )

    thresholds: dict[str, int] = {}
    derived_any = False

    for direction_name in PATTERN_COUNTS:
        direction_info = joystick_config.directions.get(direction_name, {})
        peak = direction_peak(direction_info)

        if peak is None:
            thresholds[direction_name] = DEADZONE_FALLBACK
            continue

        threshold = int(round(float(peak) * activation_fraction))
        threshold = clamp_int(threshold, minimum_deadzone, maximum_deadzone)
        thresholds[direction_name] = threshold
        derived_any = True

    if derived_any:
        return thresholds, "profile joystick ranges"

    return (
        {direction_name: DEADZONE_FALLBACK for direction_name in PATTERN_COUNTS},
        "fallback default",
    )


def normalized_direction_value(
    dx: int,
    dy: int,
    direction_info: dict[str, Any],
) -> int | None:
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
    direction_thresholds: dict[str, int],
    joystick_config: JoystickConfig,
) -> str | None:
    strongest, strongest_value = strongest_direction(dx, dy, joystick_config)
    if strongest is None:
        return None

    strongest_threshold = direction_thresholds.get(strongest, DEADZONE_FALLBACK)
    if strongest_value < strongest_threshold:
        return None

    if prev_dir in PATTERN_COUNTS:
        prev_info = joystick_config.directions.get(prev_dir)
        prev_value = (
            normalized_direction_value(dx, dy, prev_info)
            if prev_info is not None
            else None
        )
        prev_threshold = direction_thresholds.get(prev_dir, DEADZONE_FALLBACK)

        if prev_value is not None and prev_value >= prev_threshold:
            prev_axis_group = AXIS_FOR_DIRECTION_GROUP.get(prev_dir)
            strongest_axis_group = AXIS_FOR_DIRECTION_GROUP.get(strongest)

            if prev_axis_group == strongest_axis_group:
                return strongest

            if strongest_value * HYST_DEN > prev_value * HYST_NUM:
                return strongest

            return prev_dir

    return strongest


# -----------------------------------------------------------------------------
# CAN output helpers
# -----------------------------------------------------------------------------

class Horn:
    """Interruptible horn control via python-can."""

    def __init__(
        self,
        bus: Any,
        *,
        start_id: int,
        stop_id: int,
        dry_run: bool = False,
    ):
        self.bus = bus
        self.dry_run = dry_run
        self.start_id = start_id
        self.stop_id = stop_id
        self.start_msg = can.Message(
            arbitration_id=start_id,
            is_extended_id=True,
            data=b"",
        )
        self.stop_msg = can.Message(
            arbitration_id=stop_id,
            is_extended_id=True,
            data=b"",
        )

    def start(self) -> None:
        if self.dry_run:
            print("    horn start")
            return
        try:
            self.bus.send(self.start_msg)
        except can.CanError as exc:
            sys.stderr.write(f"horn start failed: {exc}\n")

    def stop(self) -> None:
        if self.dry_run:
            print("    horn stop")
            return
        try:
            self.bus.send(self.stop_msg)
            self.bus.send(self.stop_msg)
        except can.CanError as exc:
            sys.stderr.write(f"horn stop failed: {exc}\n")

    def silence(self) -> None:
        if self.dry_run:
            return
        try:
            self.bus.send(self.stop_msg)
        except can.CanError:
            pass


class LightDance:
    """Hold one direction-specific light toggle while a direction is active."""

    def __init__(
        self,
        bus: Any,
        action_config: ActionConfig,
        *,
        enabled: bool = False,
        dry_run: bool = False,
    ):
        self.bus = bus
        self.enabled = enabled
        self.dry_run = dry_run
        self.active_light: str | None = None
        self.messages = {
            light_name: can.Message(
                arbitration_id=can_id,
                is_extended_id=True,
                data=b"",
            )
            for light_name, can_id in action_config.light_toggle_ids.items()
        }

    def _toggle(self, light_name: str, reason: str) -> None:
        if not self.enabled:
            return

        if light_name not in self.messages:
            sys.stderr.write(f"unknown light toggle: {light_name}\n")
            return

        if self.dry_run:
            print(f"    light toggle {light_name} ({reason})")
            return

        try:
            self.bus.send(self.messages[light_name])
        except can.CanError as exc:
            sys.stderr.write(f"light toggle failed for {light_name}: {exc}\n")

    def turn_on_for_direction(self, direction_name: str) -> None:
        """Toggle the direction light on, but only after the direction is accepted."""
        if not self.enabled:
            return

        light_name = DIRECTION_LIGHTS.get(direction_name)
        if light_name is None:
            return

        if self.active_light == light_name:
            return

        self.turn_off("direction changed")
        self._toggle(light_name, f"{direction_name} active")
        self.active_light = light_name

    def turn_off(self, reason: str = "direction ended") -> None:
        if not self.enabled:
            return

        if self.active_light is None:
            return

        light_name = self.active_light
        self.active_light = None
        self._toggle(light_name, reason)

    # Backward-compatible aliases for older call sites.
    def pulse_start(self, direction_name: str) -> None:
        self.turn_on_for_direction(direction_name)

    def pulse_stop(self) -> None:
        self.turn_off("direction ended")

    def silence(self) -> None:
        self.pulse_stop()


class FeedbackPlayer:
    """Non-blocking horn/light pattern player that can be cancelled immediately."""

    def __init__(
        self,
        horn: Horn,
        lights: LightDance,
        *,
        beep_len_ms: int,
        gap_ms: int,
    ):
        self.horn = horn
        self.lights = lights
        self.beep_len_ms = beep_len_ms
        self.gap_ms = gap_ms
        self.active = False
        self.direction: str | None = None
        self.target_clicks = 0
        self.clicks_started = 0
        self.click_is_on = False
        self.next_event_ms = 0

    def start(self, direction_name: str, count: int, now: int) -> None:
        self.cancel("new pattern")
        self.active = True
        self.direction = direction_name
        self.target_clicks = count
        self.clicks_started = 0
        self.click_is_on = False
        self.next_event_ms = now
        self._start_next_click(now)

    def _start_next_click(self, now: int) -> None:
        if self.clicks_started >= self.target_clicks:
            self.finish()
            return

        self.clicks_started += 1
        self.horn.start()
        self.click_is_on = True
        self.next_event_ms = now + self.beep_len_ms

    def tick(self, now: int) -> None:
        if not self.active:
            return

        if now < self.next_event_ms:
            return

        if self.click_is_on:
            self.horn.stop()
            self.click_is_on = False

            if self.clicks_started >= self.target_clicks:
                self.finish()
            else:
                self.next_event_ms = now + self.gap_ms
            return

        self._start_next_click(now)

    def cancel(self, reason: str = "cancel") -> None:
        if not self.active:
            return

        if self.click_is_on:
            self.horn.stop()
        else:
            self.horn.silence()

        self.active = False
        self.direction = None
        self.target_clicks = 0
        self.clicks_started = 0
        self.click_is_on = False
        self.next_event_ms = 0

    def finish(self) -> None:
        # Finish only the horn pattern. The light stays on while the joystick
        # remains held in this direction, and is toggled off by the main loop
        # when the direction ends.
        self.active = False
        self.direction = None
        self.target_clicks = 0
        self.clicks_started = 0
        self.click_is_on = False
        self.next_event_ms = 0

    def silence(self) -> None:
        self.cancel("shutdown")


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Direction-aware horn/light beeper for R-Net using a meet-and-greet profile."
    )
    p.add_argument("--interface", default="can0", help="SocketCAN interface")
    p.add_argument(
        "--bustype",
        default="socketcan",
        help="python-can interface backend (default: socketcan)",
    )
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
        default=None,
        help=(
            "Manual joystick activation threshold. If omitted, thresholds are "
            "derived from profile joystick ranges."
        ),
    )
    p.add_argument(
        "--activation-fraction",
        type=float,
        default=ACTIVATION_FRACTION_DEFAULT,
        help=(
            "Fraction of each observed direction peak used as activation threshold "
            f"(default: {ACTIVATION_FRACTION_DEFAULT:.2f})"
        ),
    )
    p.add_argument(
        "--minimum-deadzone",
        type=int,
        default=MIN_DERIVED_DEADZONE_DEFAULT,
        help=f"Minimum derived threshold (default: {MIN_DERIVED_DEADZONE_DEFAULT})",
    )
    p.add_argument(
        "--maximum-deadzone",
        type=int,
        default=MAX_DERIVED_DEADZONE_DEFAULT,
        help=f"Maximum derived threshold (default: {MAX_DERIVED_DEADZONE_DEFAULT})",
    )
    p.add_argument(
        "--stable",
        type=int,
        default=STABLE_FRAMES_DEFAULT,
        help=f"Consecutive frames required to confirm state (default: {STABLE_FRAMES_DEFAULT})",
    )
    p.add_argument(
        "--start-delay-ms",
        type=int,
        default=START_DELAY_MS_DEFAULT,
        help=(
            "Delay after a stable new direction before first beep, ms "
            f"(default: {START_DELAY_MS_DEFAULT})"
        ),
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
        "--enable-lights",
        action="store_true",
        help=(
            "Also pulse direction-specific light toggles: "
            "forward=flood/headlight, reverse=hazard, "
            "left=left indicator, right=right indicator"
        ),
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


def load_runtime_configs(args: argparse.Namespace) -> tuple[JoystickConfig, ActionConfig]:
    if args.profile is not None:
        profile = load_json_file(args.profile)
        joystick_config = extract_joystick_config_from_profile(profile)
        action_config = extract_action_config_from_profile(profile)
    else:
        joystick_config = JoystickConfig()
        action_config = ActionConfig()

    if args.joystick_id is not None:
        joystick_config.can_id = args.joystick_id
        joystick_config.source = "manual_override"

    return joystick_config, action_config


def print_startup_summary(
    args: argparse.Namespace,
    joystick_config: JoystickConfig,
    action_config: ActionConfig,
    direction_thresholds: dict[str, int],
    threshold_source: str,
) -> None:
    print(f"Listening on {args.interface}. Press Ctrl+C to stop.")
    print("Patterns:  forward=1  reverse=2  left=3  right=4 clicks")
    print(
        f"Joystick ID=0x{joystick_config.can_id:08X} from {joystick_config.source}"
    )
    print(f"Joystick center: X={joystick_config.center_x} Y={joystick_config.center_y}")
    print("Joystick direction mapping and activation thresholds:")
    for direction_name in ["forward", "reverse", "left", "right"]:
        info = joystick_config.directions.get(direction_name, {})
        peak = direction_peak(info)
        threshold = direction_thresholds.get(direction_name)
        print(
            f"  {direction_name:7s}: axis={info.get('axis')} "
            f"sign={info.get('sign')} peak={peak} "
            f"threshold={threshold} source={info.get('source')}"
        )
    print(f"Threshold source: {threshold_source}")
    print(
        f"Stable={args.stable}  Start delay={args.start_delay_ms}ms  "
        f"Repeat={args.repeat_ms}ms  Motion gate={'ON' if args.require_motion else 'OFF'}"
    )
    print(
        f"Horn: start=0x{action_config.horn_start_id:08X} "
        f"stop=0x{action_config.horn_stop_id:08X} "
        f"source={action_config.horn_source}"
    )
    print(f"Light dance={'ON' if args.enable_lights else 'OFF'}")
    if args.enable_lights:
        print("Light mapping:")
        for direction_name in ["forward", "reverse", "left", "right"]:
            light_name = DIRECTION_LIGHTS[direction_name]
            can_id = action_config.light_toggle_ids[light_name]
            source = action_config.light_sources.get(light_name, "unknown")
            print(f"  {direction_name:7s}: {light_name} 0x{can_id:08X} source={source}")
        print(
            "Note: these are toggle commands. For the intended effect, start "
            "with hazards, indicators, and flood/headlights off."
        )
    if args.require_motion:
        print(f"Motor/current ID=0x{args.motor_id:08X}")
    print()


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        joystick_config, action_config = load_runtime_configs(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Could not load profile: {exc}\n")
        sys.exit(1)

    direction_thresholds, threshold_source = derive_direction_thresholds(
        joystick_config,
        manual_deadzone=args.deadzone,
        activation_fraction=args.activation_fraction,
        minimum_deadzone=args.minimum_deadzone,
        maximum_deadzone=args.maximum_deadzone,
    )

    if args.dry_run:
        print("[DRY RUN] no frames will be transmitted.")
        bus = None
    else:
        try:
            bus = can.interface.Bus(channel=args.interface, interface=args.bustype)
        except OSError as exc:
            sys.stderr.write(f"Could not open {args.interface}: {exc}\n")
            sys.exit(1)

    horn = Horn(
        bus,
        start_id=action_config.horn_start_id,
        stop_id=action_config.horn_stop_id,
        dry_run=args.dry_run,
    )
    lights = LightDance(
        bus,
        action_config,
        enabled=args.enable_lights,
        dry_run=args.dry_run,
    )
    player = FeedbackPlayer(
        horn,
        lights,
        beep_len_ms=int(round(args.beep_len * 1000)),
        gap_ms=int(round(args.gap * 1000)),
    )

    candidate_dir: str | None = None
    candidate_count = 0
    candidate_since_ms = now_ms()
    last_dir: str | None = None
    last_feedback_ms = 0
    motor_value = 0
    motor_last_ts = 0.0

    print_startup_summary(
        args,
        joystick_config,
        action_config,
        direction_thresholds,
        threshold_source,
    )

    try:
        while True:
            loop_now = now_ms()
            player.tick(loop_now)

            if bus is None:
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
                msg = bus.recv(timeout=0.02)
                player.tick(now_ms())
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
                direction_thresholds,
                joystick_config,
            )
            current_ms = now_ms()

            if player.active and current_dir != player.direction:
                print(
                    f"[{time.strftime('%H:%M:%S')}] direction changed "
                    f"{player.direction} -> {current_dir}; cancel horn feedback"
                )
                player.cancel("direction changed")

            if last_dir is not None and current_dir is not None and current_dir != last_dir:
                # Direction changed without returning to center. Turn the old held
                # light off immediately. The new direction must pass the stable-frame
                # and start-delay gates before its light is toggled on.
                lights.turn_off("direction changed")
                last_dir = None

            if args.verbose:
                thresholds_text = ",".join(
                    f"{name}:{value}" for name, value in direction_thresholds.items()
                )
                print(
                    "  X=%+4d Y=%+4d dX=%+4d dY=%+4d prev=%s cand=%s/%d -> %s motor=%d thresholds=%s"
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
                        thresholds_text,
                    )
                )

            if current_dir == candidate_dir:
                candidate_count += 1
            else:
                candidate_dir = current_dir
                candidate_count = 1
                candidate_since_ms = current_ms

            if current_dir is None:
                if last_dir is not None:
                    print(f"[{time.strftime('%H:%M:%S')}] center -> silence")
                    player.cancel("centered")
                    lights.turn_off("centered")
                    last_dir = None
                continue

            if candidate_count < args.stable:
                continue

            stable_age_ms = current_ms - candidate_since_ms
            if candidate_dir != last_dir and stable_age_ms < args.start_delay_ms:
                continue

            if player.active:
                continue

            if candidate_dir != last_dir:
                should_fire = True
            elif current_ms - last_feedback_ms >= args.repeat_ms:
                should_fire = True
            else:
                should_fire = False

            if not should_fire:
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
                        last_feedback_ms = current_ms
                    continue

            print(
                "[%s] X=%+d Y=%+d dX=%+d dY=%+d -> %s (clicks=%d, delayed=%dms)"
                % (
                    time.strftime("%H:%M:%S"),
                    x,
                    y,
                    dx,
                    dy,
                    candidate_dir,
                    PATTERN_COUNTS[candidate_dir],
                    stable_age_ms,
                )
            )
            lights.turn_on_for_direction(candidate_dir)
            player.start(candidate_dir, PATTERN_COUNTS[candidate_dir], current_ms)
            last_dir = candidate_dir
            last_feedback_ms = current_ms

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        player.silence()
        lights.silence()
        horn.silence()
        if bus is not None:
            bus.shutdown()


if __name__ == "__main__":
    main()
