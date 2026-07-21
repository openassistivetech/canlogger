#!/usr/bin/env python3
"""
rnet_meet_greet_skeleton.py - Passive R-Net chair capability discovery wizard.

Example call: 
python3 rnet_meet_greet.py \
  --interface can0 \
  --bustype socketcan \
  --profile-name bumblebee \
  --files-root meet_greet_files \
  --output rnet_meet_greet_profile.json \
  --replay-log-root meet_greet_log_snippets \
  --replay-pick ask

run with --custom-log in order to get manually tagged bit of interactive user sessions, like this:
meet_greet_files/custom_logs/20260713T225501Z_programmer_horn_candidate.log
  
Future goals:
1) multiple tests for range maximums for joystick inputs

Purpose:
  Interactive wizard script that helps map chair-specific R-Net frames on a new wheelchair.
  It will prompt users through a series of steps, like "honk the horn", "toggle the left indicator", etc.
  For each interaction step it will listen for 10 seconds and write the log snippet to a file.
  After the session we will have a json file containing the results, a txt file with a summary that's easy to read
  and raw logs in the following folder structure:
  meet_greet_log_snippets/
    horn_honk/
    left_indicator/
    short_drive_forward/
The resulting logs would have UTC timestamp and status of the listening session and will look like this:
meet_greet_log_snippets/horn_honk/20260711T164233Z_candidate.log
meet_greet_log_snippets/horn_honk/20260711T164402Z_timeout.log
meet_greet_log_snippets/left_indicator/20260711T164915Z_confirmed.log

Design goals:
  - Passive by default: listen/recognize, do not transmit.
  - User-guided: prompt for one chair action at a time.
  - Skippable: every step can be skipped.
  - Timeout-aware: each step listens for a bounded time window.
  - Evidence-based: later versions should store confirmed/candidate/not-observed.

This skeleton intentionally does NOT implement CAN recognition yet.
Expectation and recognition sections are placeholders to fill in later.

Related known-good ideas from prior scripts:
  - direction_beeper_v5.py uses joystick IDs 0x02000200 and 0x02000100.
  - It treats horn as 0C040100# / 0C040101#.
  - It learned that 0C000400#maskbitmap can update JSM icons, while physical
    light toggles may be separate frames such as 0C000101# through 0C000104#.

SAFETY:
  Run only on your own chair, in a safe area, with a spotter when movement tests
  are enabled. This script should remain passive unless a future explicit
  transmit-confirm mode is added.
"""

from __future__ import annotations

import argparse
import json
import re
import select
import sys
import time

try:
    import can
except ImportError:  # pragma: no cover - handled at runtime for live mode
    can = None
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

StepStatus = Literal[
    "not_run", "skipped", "timeout", "candidate", "confirmed", "failed"
]

LOG_SNIPPET_ROOT_DEFAULT = "meet_greet_log_snippets"
LISTEN_SECONDS_DEFAULT = 10.0
MEET_GREET_FILES_ROOT_DEFAULT = "meet_greet_files"
JOYSTICK_DEADZONE_DEFAULT = 3
CUSTOM_LOG_ROOT_DEFAULT = "custom_logs"

# Known R-Net frame IDs
HORN_START_ID = 0x0C040100
HORN_STOP_ID = 0x0C040101
HAZARD_TOGGLE_ID = 0x0C000103
LEFT_INDICATOR_TOGGLE_ID = 0x0C000101
RIGHT_INDICATOR_TOGGLE_ID = 0x0C000102
FLOOD_HEADLIGHT_TOGGLE_ID = 0x0C000104

# Optional UI/status evidence (displays on the screen-enabled joysticks when the signals are flashing).
# Useful, but weaker than the physical toggle.
LAMP_STATUS_ID = 0x0C000400
LAMP_HAZARD = 0x10
LAMP_LEFT = 0x01
LAMP_RIGHT = 0x04
LAMP_FLOOD_HEADLIGHT = 0x80

# Optional programmer-diagnostics versions
PROGRAMMER_HORN_START_ID = 0x0C040F00
PROGRAMMER_HORN_STOP_ID = 0x0C040F01
PROGRAMMER_HAZARD_TOGGLE_ID = 0x0C000F03
PROGRAMMER_LEFT_INDICATOR_TOGGLE_ID = 0x0C000F01
PROGRAMMER_RIGHT_INDICATOR_TOGGLE_ID = 0x0C000F02
PROGRAMMER_FLOOD_HEADLIGHT_TOGGLE_ID = 0x0C000F04

# Passive profile discovery/control-family evidence.
# These are used only for recognition of user-performed profile changes.
# Observed/confirmed on Bumblebee and also consistent with rnet-lib docs:
#   0x050 | node, payload 00 PP 00 00 requests wire profile index PP.
# R-Net profile configuration labels profiles as 1-8, while the wire index is
# zero-based: profile number = profile index + 1.
PROFILE_CONTROL_FAMILY_BASE = 0x050
PROFILE_CONTROL_FAMILY_MAX = 0x05F
PROFILE_INDEX_MIN = 0
PROFILE_INDEX_MAX = 7
PROFILE_NUMBER_MIN = 1
PROFILE_NUMBER_MAX = 8


def profile_index_to_number(profile_index: int | None) -> int | None:
    """Convert wire profile index 0-7 to user/config profile number 1-8."""
    if profile_index is None:
        return None
    if not (PROFILE_INDEX_MIN <= profile_index <= PROFILE_INDEX_MAX):
        return None
    return profile_index + 1


def profile_number_to_index(profile_number: int | None) -> int | None:
    """Convert user/config profile number 1-8 to wire profile index 0-7."""
    if profile_number is None:
        return None
    if not (PROFILE_NUMBER_MIN <= profile_number <= PROFILE_NUMBER_MAX):
        return None
    return profile_number - 1


def profile_indices_to_numbers(profile_indices: list[int]) -> list[int]:
    """Convert a list of valid wire profile indices to config profile numbers."""
    return [
        profile_number
        for profile_number in (profile_index_to_number(profile_index) for profile_index in profile_indices)
        if profile_number is not None
    ]


# Passive seating/display status evidence observed on this chair/profile.
# These are NOT transmit commands. They are used only to recognize user-performed
# seating function selection and joystick movement context.
SEATING_BLOCK_ID = 0x0C180300
SEATING_BLOCK_START_DATA = "0202"
SEATING_BLOCK_END_DATA = "0201"
SEATING_FUNCTION_STATUS_ID = 0x0C180301

SEATING_FUNCTION_SPECS: dict[str, dict[str, Any]] = {
    # code: observed selected function and expected joystick polarity in seating mode
    "20": {
        "name": "elevate",
        "display_name": "Elevate",
        "more_state": "y_pos",
        "less_state": "y_neg",
        "more_label": "elevate more / raise",
        "less_label": "elevate less / lower",
    },
    "21": {
        "name": "recline",
        "display_name": "Recline / backrest",
        "more_state": "y_neg",
        "less_state": "y_pos",
        "more_label": "recline more / backrest back",
        "less_label": "recline less / backrest forward",
    },
    "22": {
        "name": "tilt",
        "display_name": "Tilt",
        "more_state": "y_neg",
        "less_state": "y_pos",
        "more_label": "tilt more",
        "less_label": "tilt less",
    },
    "23": {
        "name": "legs",
        "display_name": "Legs",
        "more_state": "y_pos",
        "less_state": "y_neg",
        "more_label": "legs more / raise legs",
        "less_label": "legs less / lower legs",
    },
}

SEATING_FUNCTION_NAME_TO_CODE = {
    spec["name"]: code for code, spec in SEATING_FUNCTION_SPECS.items()
}


@dataclass
class StepResult:
    """One wizard step result. Recognition details will be added later."""

    key: str
    title: str
    status: StepStatus = "not_run"
    notes: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeetGreetProfile:
    """Draft profile structure produced by the wizard."""

    profile_name: str
    created_at: str
    interface: str
    bustype: str
    passive_only: bool = True
    steps: dict[str, StepResult] = field(default_factory=dict)
    confirmed: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, Any] = field(default_factory=dict)
    not_observed: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WizardStep:
    """Prompt metadata for one user-guided discovery step."""

    key: str
    title: str
    prompt: str
    timeout_seconds: float
    safety_note: str = ""
    optional: bool = True


CAN_LOG_LINE_RE = re.compile(
    r"^\((?P<timestamp>\d+(?:\.\d+)?)\)\s+"
    r"(?P<interface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)"
)


def data_hex_to_bytes(data_hex: str) -> bytes:
    """Safely decode CAN data hex into bytes."""
    try:
        return bytes.fromhex(data_hex)
    except ValueError:
        return b""


def parse_can_log_line(line: str) -> dict[str, Any] | None:
    """
    Parse a candump-style line.

    Example:
      (1783377282.654466) can0 02000200#0000

    Returns:
      {
        "timestamp": 1783377282.654466,
        "interface": "can0",
        "can_id": 0x02000200,
        "can_id_hex": "02000200",
        "data_hex": "0000",
        "raw": original line,
      }
    """
    match = CAN_LOG_LINE_RE.match(line.strip())
    if not match:
        return None

    can_id_text = match.group("can_id").upper()
    data_hex = match.group("data").upper()

    try:
        timestamp_value = float(match.group("timestamp"))
        can_id_value = int(can_id_text, 16)
    except ValueError:
        return None

    return {
        "timestamp": timestamp_value,
        "interface": match.group("interface"),
        "can_id": can_id_value,
        "can_id_hex": can_id_text.zfill(8),
        "data_hex": data_hex,
        "raw": line.rstrip("\n"),
    }


def signed_int8(value: int) -> int:
    """Interpret one byte as signed int8."""
    if value >= 128:
        return value - 256
    return value


def looks_like_rnet_joystick_family(can_id: int) -> bool:
    """
    Heuristic for R-Net joystick-like 29-bit IDs.

    Known examples so far:
      0x02000100
      0x02000200

    This intentionally does NOT hardcode those exact IDs.
    It looks for the broader 0x0200NN00 shape.
    """
    return (can_id & 0xFFFF00FF) == 0x02000000


def joystick_state_from_xy(
    x: int,
    y: int,
    *,
    center_x: int,
    center_y: int,
    deadzone: int,
) -> str:
    dx = x - center_x
    dy = y - center_y

    if abs(dx) <= deadzone and abs(dy) <= deadzone:
        return "center"

    if abs(dx) >= abs(dy):
        return "x_pos" if dx > 0 else "x_neg"

    return "y_pos" if dy > 0 else "y_neg"


def state_axis_and_sign(state: str) -> tuple[str | None, int | None]:
    if state == "x_pos":
        return "x", 1
    if state == "x_neg":
        return "x", -1
    if state == "y_pos":
        return "y", 1
    if state == "y_neg":
        return "y", -1
    return None, None


def states_are_opposites(a: str, b: str) -> bool:
    axis_a, sign_a = state_axis_and_sign(a)
    axis_b, sign_b = state_axis_and_sign(b)

    return (
        axis_a is not None
        and axis_a == axis_b
        and sign_a is not None
        and sign_b is not None
        and sign_a == -sign_b
    )


def extract_two_byte_xy_samples(lines: list[str]) -> list[dict[str, Any]]:
    """
    Extract all two-byte CAN frames as possible X/Y joystick samples.

    This does not assume the CAN ID yet.
    """
    samples: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is None:
            continue

        data = data_hex_to_bytes(frame["data_hex"])
        if len(data) != 2:
            continue

        x = signed_int8(data[0])
        y = signed_int8(data[1])

        samples.append(
            {
                "timestamp": frame["timestamp"],
                "can_id": frame["can_id"],
                "can_id_hex": frame["can_id_hex"],
                "x": x,
                "y": y,
                "raw": frame["raw"],
            }
        )

    return samples


def sample_is_centered(
    sample: dict[str, Any],
    *,
    center_x: int,
    center_y: int,
    deadzone: int,
) -> bool:
    dx = sample["x"] - center_x
    dy = sample["y"] - center_y
    return abs(dx) <= deadzone and abs(dy) <= deadzone


def compress_joystick_motion_phases(
    samples: list[dict[str, Any]],
    *,
    center_x: int,
    center_y: int,
    deadzone: int,
    min_phase_samples: int = 2,
) -> list[dict[str, Any]]:
    """
    Compress joystick samples into center/movement phases.

    This is tolerant of:
      - joystick ramping up through intermediate values
      - joystick returning through intermediate values
      - small off-axis noise during movement

    Example:
      0 -> y1 -> y2 -> y3 -> y2 -> y1 -> 0

    becomes one movement phase whose dominant state is y_pos.
    """
    if not samples:
        return []

    ordered = sorted(samples, key=lambda sample: sample["timestamp"])

    phases: list[dict[str, Any]] = []
    current_kind: str | None = None
    current_samples: list[dict[str, Any]] = []

    def classify_phase(
        phase_samples: list[dict[str, Any]],
        kind: str,
    ) -> dict[str, Any] | None:
        if len(phase_samples) < min_phase_samples:
            return None

        xs = [sample["x"] for sample in phase_samples]
        ys = [sample["y"] for sample in phase_samples]

        dx_values = [x - center_x for x in xs]
        dy_values = [y - center_y for y in ys]

        max_abs_dx = max(abs(value) for value in dx_values)
        max_abs_dy = max(abs(value) for value in dy_values)

        if kind == "center":
            return {
                "kind": "center",
                "state": "center",
                "axis": None,
                "sign": None,
                "signed_peak": 0,
                "max_abs_dx": max_abs_dx,
                "max_abs_dy": max_abs_dy,
                "dominance_ratio": None,
                "sample_count": len(phase_samples),
                "start_timestamp": phase_samples[0]["timestamp"],
                "end_timestamp": phase_samples[-1]["timestamp"],
                "duration_seconds": round(
                    phase_samples[-1]["timestamp"] - phase_samples[0]["timestamp"],
                    6,
                ),
                "x_min": min(xs),
                "x_max": max(xs),
                "y_min": min(ys),
                "y_max": max(ys),
                "example_lines": [sample["raw"] for sample in phase_samples[:3]],
            }

        # For a movement phase, choose the dominant axis over the whole phase,
        # not sample-by-sample.
        if max_abs_dx >= max_abs_dy:
            axis = "x"
            signed_peak = max(dx_values, key=lambda value: abs(value))
        else:
            axis = "y"
            signed_peak = max(dy_values, key=lambda value: abs(value))

        sign = 1 if signed_peak > 0 else -1
        state = f"{axis}_{'pos' if sign > 0 else 'neg'}"

        smaller_peak = min(max_abs_dx, max_abs_dy)
        larger_peak = max(max_abs_dx, max_abs_dy)
        dominance_ratio = (
            round(larger_peak / smaller_peak, 3) if smaller_peak > 0 else None
        )

        return {
            "kind": "movement",
            "state": state,
            "axis": axis,
            "sign": sign,
            "signed_peak": signed_peak,
            "max_abs_dx": max_abs_dx,
            "max_abs_dy": max_abs_dy,
            "dominance_ratio": dominance_ratio,
            "sample_count": len(phase_samples),
            "start_timestamp": phase_samples[0]["timestamp"],
            "end_timestamp": phase_samples[-1]["timestamp"],
            "duration_seconds": round(
                phase_samples[-1]["timestamp"] - phase_samples[0]["timestamp"],
                6,
            ),
            "x_min": min(xs),
            "x_max": max(xs),
            "y_min": min(ys),
            "y_max": max(ys),
            "example_lines": [sample["raw"] for sample in phase_samples[:3]],
        }

    def flush_phase() -> None:
        if not current_samples or current_kind is None:
            return

        phase = classify_phase(current_samples, current_kind)
        if phase is not None:
            phases.append(phase)

    for sample in ordered:
        centered = sample_is_centered(
            sample,
            center_x=center_x,
            center_y=center_y,
            deadzone=deadzone,
        )
        kind = "center" if centered else "movement"

        if current_kind is None:
            current_kind = kind
            current_samples = [sample]
            continue

        if kind == current_kind:
            current_samples.append(sample)
            continue

        flush_phase()
        current_kind = kind
        current_samples = [sample]

    flush_phase()

    return phases


def summarize_single_direction_phase(
    direction_name: str,
    phase: dict[str, Any],
    *,
    center_x: int,
    center_y: int,
) -> dict[str, Any]:
    """
    Summarize one independently prompted joystick direction.

    The direction_name comes from the wizard step:
      forward, reverse, left, right

    The axis/sign comes from the data.
    """
    dx_min = phase["x_min"] - center_x
    dx_max = phase["x_max"] - center_x
    dy_min = phase["y_min"] - center_y
    dy_max = phase["y_max"] - center_y

    axis = phase["axis"]
    sign = phase["sign"]

    if axis == "x":
        primary_delta_min = dx_min
        primary_delta_max = dx_max
        primary_abs_peak = phase["max_abs_dx"]
        off_axis = "y"
        off_axis_abs_peak = phase["max_abs_dy"]
    elif axis == "y":
        primary_delta_min = dy_min
        primary_delta_max = dy_max
        primary_abs_peak = phase["max_abs_dy"]
        off_axis = "x"
        off_axis_abs_peak = phase["max_abs_dx"]
    else:
        primary_delta_min = None
        primary_delta_max = None
        primary_abs_peak = None
        off_axis = None
        off_axis_abs_peak = None

    return {
        "direction": direction_name,
        "observed_state": phase["state"],
        "axis": axis,
        "sign": sign,
        "signed_peak_from_center": phase["signed_peak"],
        "primary_delta_min": primary_delta_min,
        "primary_delta_max": primary_delta_max,
        "primary_abs_peak": primary_abs_peak,
        "off_axis": off_axis,
        "off_axis_abs_peak": off_axis_abs_peak,
        "dominance_ratio": phase["dominance_ratio"],
        "center_x": center_x,
        "center_y": center_y,
        "x_min": phase["x_min"],
        "x_max": phase["x_max"],
        "y_min": phase["y_min"],
        "y_max": phase["y_max"],
        "dx_min": dx_min,
        "dx_max": dx_max,
        "dy_min": dy_min,
        "dy_max": dy_max,
        "sample_count": phase["sample_count"],
        "duration_seconds": phase["duration_seconds"],
        "start_timestamp": phase["start_timestamp"],
        "end_timestamp": phase["end_timestamp"],
        "example_lines": phase["example_lines"],
    }


def frame_is_inside_any_window(
    frame: dict[str, Any],
    windows: list[dict[str, Any]],
) -> bool:
    timestamp = frame["timestamp"]

    for window in windows:
        if window["start_timestamp"] <= timestamp <= window["end_timestamp"]:
            return True

    return False


def make_movement_window_from_phase(
    label: str,
    phase: dict[str, Any],
) -> dict[str, Any]:
    return {
        "label": label,
        "start_timestamp": phase["start_timestamp"],
        "end_timestamp": phase["end_timestamp"],
        "duration_seconds": phase["duration_seconds"],
        "axis": phase.get("axis"),
        "sign": phase.get("sign"),
        "state": phase.get("state"),
        "signed_peak": phase.get("signed_peak"),
    }


def summarize_data_values(
    frames: list[dict[str, Any]], limit: int = 8
) -> dict[str, Any]:
    values = [frame["data_hex"] for frame in frames]
    counts = Counter(values)

    return {
        "frame_count": len(frames),
        "unique_value_count": len(counts),
        "most_common_values": [
            {
                "data_hex": value,
                "count": count,
            }
            for value, count in counts.most_common(limit)
        ],
        "example_lines": [frame["raw"] for frame in frames[:5]],
    }


def parse_can_id_value(value: Any) -> int | None:
    if value is None:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None

    return None


def get_confirmed_joystick_can_id(profile: MeetGreetProfile) -> int | None:
    """
    Return the joystick CAN ID confirmed earlier in the wizard, if available.
    """
    value = profile.confirmed.get("joystick_can_id_int")
    parsed = parse_can_id_value(value)

    if parsed is not None:
        return parsed

    value = profile.confirmed.get("joystick_can_id")
    parsed = parse_can_id_value(value)

    if parsed is not None:
        return parsed

    calibration_result = profile.steps.get("joystick_calibration")
    if calibration_result is None:
        return None

    recognition = calibration_result.observations.get("recognition", {})
    if recognition.get("status") != "confirmed":
        return None

    best_candidate = recognition.get("best_candidate") or {}

    return parse_can_id_value(
        best_candidate.get("can_id_int") or best_candidate.get("can_id")
    )


def get_confirmed_joystick_center(profile: MeetGreetProfile) -> tuple[int, int] | None:
    """
    Return the joystick center confirmed earlier in the wizard, if available.

    Single-direction range tests should use this calibration center instead of
    re-learning center from their own capture. In a range test, the most common
    X/Y pair may be the held joystick value rather than true center.
    """

    def parse_center(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, dict):
            return None

        x = value.get("x")
        y = value.get("y")

        if isinstance(x, bool) or isinstance(y, bool):
            return None

        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None

        return int(round(x)), int(round(y))

    parsed = parse_center(profile.confirmed.get("joystick_center"))
    if parsed is not None:
        return parsed

    calibration_result = profile.steps.get("joystick_calibration")
    if calibration_result is None:
        return None

    recognition = calibration_result.observations.get("recognition", {})
    if recognition.get("status") != "confirmed":
        return None

    best_candidate = recognition.get("best_candidate") or {}
    return parse_center(best_candidate.get("center"))


def update_profile_from_step_result(
    profile: MeetGreetProfile,
    result: StepResult,
) -> None:
    """
    Promote important confirmed findings into profile.confirmed.

    This is what lets later steps remember that joystick_calibration already
    found the joystick command ID.
    """
    recognition = result.observations.get("recognition", {})

    if result.key == "profile_discovery":
        if recognition.get("status") in {"confirmed", "candidate"}:
            profile.confirmed["profile_control_family"] = "0x050|node"
            profile.confirmed["profile_request_pattern"] = "00 PP 00 00"
            profile.confirmed["observed_profile_indices"] = recognition.get("observed_profile_indices", [])
            profile.confirmed["observed_profile_numbers"] = recognition.get("observed_profile_numbers", [])
            profile.confirmed["active_profile_sequence"] = recognition.get("active_profile_sequence", [])
            profile.confirmed["active_profile_number_sequence"] = recognition.get("active_profile_number_sequence", [])
            profile.confirmed["enabled_profile_candidates"] = recognition.get("enabled_profile_candidates", [])
            profile.confirmed["profile_numbering_note"] = (
                "R-Net profile configuration uses profile numbers 1-8; "
                "wire frames use zero-based profile indices 0-7."
            )
        return

    if result.key != "joystick_calibration":
        return

    if recognition.get("status") != "confirmed":
        return

    best_candidate = recognition.get("best_candidate") or {}

    joystick_can_id_int = parse_can_id_value(
        best_candidate.get("can_id_int") or best_candidate.get("can_id")
    )

    if joystick_can_id_int is None:
        return

    profile.confirmed["joystick_can_id"] = f"0x{joystick_can_id_int:08X}"
    profile.confirmed["joystick_can_id_int"] = joystick_can_id_int
    profile.confirmed["joystick_center"] = best_candidate.get("center")
    profile.confirmed["joystick_mapping"] = best_candidate.get("inferred_mapping")


# -----------------------------------------------------------------------------
# Future expectation / recognition stubs
# -----------------------------------------------------------------------------

def open_can_bus(interface: str, bustype: str):
    """Open a python-can bus for passive receive-only logging."""
    if can is None:
        raise RuntimeError(
            "python-can is not installed. Install it with: pip install python-can"
        )

    try:
        # python-can 4.2+ prefers the keyword name `interface` over `bustype`.
        return can.interface.Bus(channel=interface, interface=bustype)
    except TypeError:
        # Compatibility fallback for older python-can versions.
        return can.interface.Bus(channel=interface, bustype=bustype)


def close_can_bus(bus) -> None:
    """Cleanly close a python-can bus if one was opened."""
    if bus is None:
        return

    shutdown = getattr(bus, "shutdown", None)
    if callable(shutdown):
        shutdown()


def flush_can_rx_queue(
    bus,
    *,
    max_seconds: float = 0.5,
) -> dict[str, Any]:
    """Drain already-buffered CAN frames before starting a capture window.

    This is meant to remove messages that arrived while the script was sitting
    at prompts between tests. It only receives and discards frames that are
    already available from python-can/socketcan; it never transmits.

    max_seconds is just a safety guard so a very busy bus cannot spin forever.
    """
    info: dict[str, Any] = {
        "flushed_count": 0,
        "oldest_timestamp": None,
        "newest_timestamp": None,
        "max_seconds": max_seconds,
    }

    if bus is None or max_seconds <= 0:
        return info

    deadline = time.monotonic() + max_seconds

    while time.monotonic() < deadline:
        # Non-blocking: return immediately if the current receive queue is empty.
        msg = bus.recv(timeout=0.0)
        if msg is None:
            break

        info["flushed_count"] += 1
        timestamp = getattr(msg, "timestamp", None)

        if timestamp is not None:
            if info["oldest_timestamp"] is None:
                info["oldest_timestamp"] = timestamp
            info["newest_timestamp"] = timestamp

    return info


# def confirm_candidate_by_repetition(candidate: dict, repeated_action_frames: list) -> bool:
#     """Future: confirm that a candidate repeats on a second/third trial."""
#     pass

# def write_human_report(profile: MeetGreetProfile, path: Path) -> None:
#     """Future: write a friendly report of confirmed/candidate/not-observed items."""
#     pass


def infer_joystick_id_from_idle_frames(
    parsed_frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Infer likely joystick ID candidates from idle traffic.

    Baseline assumptions:
      - Joystick idle frame is usually a high-rate 2-byte frame.
      - At rest, data is usually 0000.
      - Known examples are in the 0x0200NN00 family.
      - This is an inference from idle only, not final proof.

    Returns a ranked candidate list plus the best candidate.
    """
    frames_by_id: dict[int, list[dict[str, Any]]] = {}

    for frame in parsed_frames:
        data_hex = frame["data_hex"]

        # Joystick-style idle samples should be exactly two data bytes.
        if len(data_hex) != 4:
            continue

        frames_by_id.setdefault(frame["can_id"], []).append(frame)

    candidates: list[dict[str, Any]] = []

    for can_id, frames in frames_by_id.items():
        if len(frames) < 3:
            continue

        timestamps = [frame["timestamp"] for frame in frames]
        first_timestamp = min(timestamps)
        last_timestamp = max(timestamps)
        duration_seconds = max(0.0, last_timestamp - first_timestamp)

        if duration_seconds <= 0:
            continue

        count = len(frames)
        rate_hz = count / duration_seconds

        data_values = [frame["data_hex"] for frame in frames]
        unique_data_values = sorted(set(data_values))
        zero_count = sum(1 for value in data_values if value == "0000")
        zero_fraction = zero_count / count if count else 0.0

        intervals = [
            later - earlier
            for earlier, later in zip(timestamps, timestamps[1:])
            if later >= earlier
        ]

        if intervals:
            sorted_intervals = sorted(intervals)
            median_interval = sorted_intervals[len(sorted_intervals) // 2]
            min_interval = min(intervals)
            max_interval = max(intervals)
        else:
            median_interval = None
            min_interval = None
            max_interval = None

        rnet_family = looks_like_rnet_joystick_family(can_id)
        channel_byte = (can_id >> 8) & 0xFF

        # Scoring logic:
        #   - high-rate 2-byte traffic matters most
        #   - all-zero idle data is useful
        #   - 0x0200NN00 family is useful
        #   - lower NN values get a small preference over companion/status-like
        #     high NN values such as 0x11
        score = 0.0

        score += min(rate_hz, 120.0)
        score += zero_fraction * 40.0

        if rnet_family:
            score += 60.0
            score += max(0.0, 32.0 - float(channel_byte))

        # Penalize very slow things like motor current/status frames.
        if rate_hz < 10.0:
            score -= 50.0

        # Prefer very regular streams near joystick-ish timing.
        if median_interval is not None:
            if 0.005 <= median_interval <= 0.05:
                score += 20.0

        candidates.append(
            {
                "can_id": f"0x{can_id:08X}",
                "can_id_int": can_id,
                "score": round(score, 3),
                "rnet_joystick_family": rnet_family,
                "channel_byte": f"0x{channel_byte:02X}",
                "sample_count": count,
                "rate_hz": round(rate_hz, 3),
                "zero_count": zero_count,
                "zero_fraction": round(zero_fraction, 3),
                "unique_data_value_count": len(unique_data_values),
                "example_data_values": unique_data_values[:8],
                "first_timestamp": first_timestamp,
                "last_timestamp": last_timestamp,
                "duration_seconds": round(duration_seconds, 6),
                "median_interval_seconds": (
                    round(median_interval, 6) if median_interval is not None else None
                ),
                "min_interval_seconds": (
                    round(min_interval, 6) if min_interval is not None else None
                ),
                "max_interval_seconds": (
                    round(max_interval, 6) if max_interval is not None else None
                ),
                "example_lines": [frame["raw"] for frame in frames[:5]],
            }
        )

    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)

    if not candidates:
        return {
            "implemented": True,
            "status": "not_observed",
            "summary": "No likely joystick idle ID candidates found.",
            "best_candidate": None,
            "ranked_candidates": [],
        }

    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None

    if second is None:
        confidence = "medium"
        status = "candidate"
        summary = (
            f"Best joystick idle candidate is {best['can_id']} "
            f"at ~{best['rate_hz']} Hz."
        )
    else:
        score_gap = best["score"] - second["score"]

        if score_gap >= 20:
            confidence = "medium"
        elif score_gap >= 8:
            confidence = "low_medium"
        else:
            confidence = "low"

        status = "candidate"
        summary = (
            f"Best joystick idle candidate is {best['can_id']} "
            f"at ~{best['rate_hz']} Hz. "
            f"Next candidate is {second['can_id']} "
            f"at ~{second['rate_hz']} Hz. "
            "Movement steps should confirm which ID carries X/Y."
        )

    return {
        "implemented": True,
        "status": status,
        "summary": summary,
        "confidence": confidence,
        "best_candidate": best,
        "ranked_candidates": candidates[:12],
    }


def recognize_horn_start_stop(lines: list[str]) -> dict[str, Any]:
    """
    Recognize R-Net horn start/stop patterns.

    Joystick button evidence:
      horn start: 0C040100#
      horn stop:  0C040101#

    Programmer/diagnostic evidence:
      horn start: 0C040F00#
      horn stop:  0C040F01#

    This recognizer is intentionally conservative:
      - It does not transmit.
      - It does not assume the horn worked physically.
      - It reports whether joystick and/or programmer horn frames appear.
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is not None:
            parsed_frames.append(frame)

    def find_events(start_id: int, stop_id: int) -> dict[str, Any]:
        start_events = [
            frame for frame in parsed_frames
            if frame["can_id"] == start_id
        ]

        stop_events = [
            frame for frame in parsed_frames
            if frame["can_id"] == stop_id
        ]

        pairs: list[dict[str, Any]] = []
        unused_stops = stop_events.copy()

        for start in start_events:
            matching_stop = None

            for stop in unused_stops:
                if stop["timestamp"] >= start["timestamp"]:
                    matching_stop = stop
                    break

            if matching_stop is not None:
                unused_stops.remove(matching_stop)
                pairs.append(
                    {
                        "start_timestamp": start["timestamp"],
                        "stop_timestamp": matching_stop["timestamp"],
                        "duration_seconds": round(
                            matching_stop["timestamp"] - start["timestamp"],
                            6,
                        ),
                        "start_line": start["raw"],
                        "stop_line": matching_stop["raw"],
                    }
                )

        return {
            "start_id": f"0x{start_id:08X}",
            "stop_id": f"0x{stop_id:08X}",
            "start_count": len(start_events),
            "stop_count": len(stop_events),
            "pair_count": len(pairs),
            "start_events": [
                {
                    "timestamp": frame["timestamp"],
                    "raw": frame["raw"],
                }
                for frame in start_events
            ],
            "stop_events": [
                {
                    "timestamp": frame["timestamp"],
                    "raw": frame["raw"],
                }
                for frame in stop_events
            ],
            "pairs": pairs,
        }

    joystick_evidence = find_events(
        HORN_START_ID,
        HORN_STOP_ID,
    )

    programmer_evidence = find_events(
        PROGRAMMER_HORN_START_ID,
        PROGRAMMER_HORN_STOP_ID,
    )

    joystick_pair_count = joystick_evidence["pair_count"]
    programmer_pair_count = programmer_evidence["pair_count"]

    joystick_partial = (
        joystick_evidence["start_count"] > 0
        or joystick_evidence["stop_count"] > 0
    )

    programmer_partial = (
        programmer_evidence["start_count"] > 0
        or programmer_evidence["stop_count"] > 0
    )

    if joystick_pair_count and programmer_pair_count:
        recognition_status = "confirmed"
        horn_trigger_source = "both"
        summary = (
            f"Found joystick horn evidence "
            f"({joystick_pair_count} start/stop pair(s)) and "
            f"programmer horn evidence "
            f"({programmer_pair_count} start/stop pair(s))."
        )
    elif joystick_pair_count:
        recognition_status = "confirmed"
        horn_trigger_source = "joystick"
        summary = (
            f"Found {joystick_pair_count} joystick horn start/stop pair(s): "
            f"0x{HORN_START_ID:08X} -> 0x{HORN_STOP_ID:08X}."
        )
    elif programmer_pair_count:
        recognition_status = "confirmed"
        horn_trigger_source = "programmer"
        summary = (
            f"Found {programmer_pair_count} programmer horn start/stop pair(s): "
            f"0x{PROGRAMMER_HORN_START_ID:08X} -> "
            f"0x{PROGRAMMER_HORN_STOP_ID:08X}."
        )
    elif joystick_partial or programmer_partial:
        recognition_status = "candidate"

        partial_sources = []

        if joystick_partial:
            partial_sources.append(
                f"joystick start={joystick_evidence['start_count']}, "
                f"stop={joystick_evidence['stop_count']}"
            )

        if programmer_partial:
            partial_sources.append(
                f"programmer start={programmer_evidence['start_count']}, "
                f"stop={programmer_evidence['stop_count']}"
            )

        horn_trigger_source = "partial"
        summary = (
            "Found partial horn evidence: "
            + "; ".join(partial_sources)
            + "."
        )
    else:
        recognition_status = "not_observed"
        horn_trigger_source = None
        summary = "No joystick or programmer horn start/stop frames observed."

    return {
        "recognizer": "horn_start_stop",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "horn_trigger_source": horn_trigger_source,

        "expected_joystick_start_id": f"0x{HORN_START_ID:08X}",
        "expected_joystick_stop_id": f"0x{HORN_STOP_ID:08X}",
        "expected_programmer_start_id": f"0x{PROGRAMMER_HORN_START_ID:08X}",
        "expected_programmer_stop_id": f"0x{PROGRAMMER_HORN_STOP_ID:08X}",

        "line_count": len(lines),
        "parsed_frame_count": len(parsed_frames),

        "joystick_evidence": joystick_evidence,
        "programmer_evidence": programmer_evidence,
    }

def recognize_hazard_lights(lines: list[str]) -> dict[str, Any]:
    """
    Recognize hazard-light activity.

    Strong evidence:
      0C000103# appears when the physical hazard button/function is toggled.

    Weaker/status evidence:
      0C000400#maskbitmap may report/update lamp icon state.
      For that bitmap:
        bit 0x10 = hazard

    Expected user action for this step:
      toggle hazards ON, pause, toggle hazards OFF

    So two hazard toggle events is a strong match.
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is not None:
            parsed_frames.append(frame)

    physical_toggle_events = [
        frame for frame in parsed_frames if frame["can_id"] == HAZARD_TOGGLE_ID
    ]

    programmer_toggle_events = (
        [
            frame
            for frame in parsed_frames
            if frame["can_id"] == PROGRAMMER_HAZARD_TOGGLE_ID
        ]
        if "PROGRAMMER_HAZARD_TOGGLE_ID" in globals()
        else []
    )

    status_events: list[dict[str, Any]] = []

    for frame in parsed_frames:
        if frame["can_id"] != LAMP_STATUS_ID:
            continue

        data = data_hex_to_bytes(frame["data_hex"])
        if len(data) < 2:
            continue

        mask = data[0]
        bitmap = data[1]

        # Only treat it as hazard-relevant if the hazard bit is included
        # in the mask or shown as active in the bitmap.
        if (mask & LAMP_HAZARD) or (bitmap & LAMP_HAZARD):
            status_events.append(
                {
                    "timestamp": frame["timestamp"],
                    "raw": frame["raw"],
                    "mask": f"0x{mask:02X}",
                    "bitmap": f"0x{bitmap:02X}",
                    "hazard_masked": bool(mask & LAMP_HAZARD),
                    "hazard_on": bool(bitmap & LAMP_HAZARD),
                }
            )

    toggle_intervals: list[dict[str, Any]] = []
    all_toggle_events = physical_toggle_events + programmer_toggle_events
    all_toggle_events = sorted(all_toggle_events, key=lambda frame: frame["timestamp"])

    for previous, current in zip(all_toggle_events, all_toggle_events[1:]):
        toggle_intervals.append(
            {
                "from_timestamp": previous["timestamp"],
                "to_timestamp": current["timestamp"],
                "delta_seconds": round(current["timestamp"] - previous["timestamp"], 6),
            }
        )

    physical_count = len(physical_toggle_events)
    programmer_count = len(programmer_toggle_events)
    total_toggle_count = len(all_toggle_events)

    if physical_count >= 2:
        recognition_status = "confirmed"
        summary = (
            f"Found {physical_count} physical hazard toggle frame(s) "
            f"0x{HAZARD_TOGGLE_ID:08X}. This matches ON then OFF."
        )
    elif physical_count == 1:
        recognition_status = "candidate"
        summary = (
            f"Found one physical hazard toggle frame 0x{HAZARD_TOGGLE_ID:08X}; "
            "expected two for ON then OFF."
        )
    elif programmer_count >= 2:
        recognition_status = "candidate"
        summary = (
            f"Found {programmer_count} programmer-diagnostic hazard event(s), "
            "but no physical joystick-button hazard toggle frames."
        )
    elif status_events:
        recognition_status = "candidate"
        summary = (
            "Found hazard-related lamp status/icon evidence, but no physical "
            "hazard toggle command frame."
        )
    else:
        recognition_status = "not_observed"
        summary = "No hazard toggle or hazard-status evidence observed."

    return {
        "recognizer": "hazard_lights",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "expected_physical_toggle_id": f"0x{HAZARD_TOGGLE_ID:08X}",
        "expected_status_id": f"0x{LAMP_STATUS_ID:08X}",
        "hazard_status_bit": f"0x{LAMP_HAZARD:02X}",
        "line_count": len(lines),
        "parsed_frame_count": len(parsed_frames),
        "physical_toggle_count": physical_count,
        "programmer_toggle_count": programmer_count,
        "total_toggle_count": total_toggle_count,
        "status_event_count": len(status_events),
        "physical_toggle_events": [
            {
                "timestamp": frame["timestamp"],
                "raw": frame["raw"],
            }
            for frame in physical_toggle_events
        ],
        "programmer_toggle_events": [
            {
                "timestamp": frame["timestamp"],
                "raw": frame["raw"],
            }
            for frame in programmer_toggle_events
        ],
        "toggle_intervals": toggle_intervals,
        "status_events": status_events,
    }


def recognize_indicator_toggle(
    lines: list[str],
    *,
    name: str,
    physical_toggle_id: int,
    status_bit: int,
    programmer_toggle_id: int | None = None,
) -> dict[str, Any]:
    """
    Recognize left/right indicator activity.

    Strong evidence:
      physical toggle frame appears, e.g.
        left:  0C000101#
        right: 0C000102#

    Weaker/status evidence:
      0C000400#maskbitmap may report/update lamp icon state.
      This is useful but weaker than the physical toggle frame.
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is not None:
            parsed_frames.append(frame)

    physical_toggle_events = [
        frame for frame in parsed_frames if frame["can_id"] == physical_toggle_id
    ]

    if programmer_toggle_id is not None:
        programmer_toggle_events = [
            frame for frame in parsed_frames if frame["can_id"] == programmer_toggle_id
        ]
    else:
        programmer_toggle_events = []

    status_events: list[dict[str, Any]] = []

    for frame in parsed_frames:
        if frame["can_id"] != LAMP_STATUS_ID:
            continue

        data = data_hex_to_bytes(frame["data_hex"])
        if len(data) < 2:
            continue

        mask = data[0]
        bitmap = data[1]

        if (mask & status_bit) or (bitmap & status_bit):
            status_events.append(
                {
                    "timestamp": frame["timestamp"],
                    "raw": frame["raw"],
                    "mask": f"0x{mask:02X}",
                    "bitmap": f"0x{bitmap:02X}",
                    "status_bit_masked": bool(mask & status_bit),
                    "indicator_on": bool(bitmap & status_bit),
                }
            )

    all_toggle_events = sorted(
        physical_toggle_events + programmer_toggle_events,
        key=lambda frame: frame["timestamp"],
    )

    toggle_intervals: list[dict[str, Any]] = []
    for previous, current in zip(all_toggle_events, all_toggle_events[1:]):
        toggle_intervals.append(
            {
                "from_timestamp": previous["timestamp"],
                "to_timestamp": current["timestamp"],
                "delta_seconds": round(current["timestamp"] - previous["timestamp"], 6),
            }
        )

    physical_count = len(physical_toggle_events)
    programmer_count = len(programmer_toggle_events)
    total_toggle_count = len(all_toggle_events)

    if physical_count >= 2:
        recognition_status = "confirmed"
        summary = (
            f"Found {physical_count} physical {name} indicator toggle frame(s) "
            f"0x{physical_toggle_id:08X}. This matches ON then OFF."
        )
    elif physical_count == 1:
        recognition_status = "candidate"
        summary = (
            f"Found one physical {name} indicator toggle frame "
            f"0x{physical_toggle_id:08X}; expected two for ON then OFF."
        )
    elif programmer_count >= 2:
        recognition_status = "candidate"
        summary = (
            f"Found {programmer_count} programmer-diagnostic {name} indicator event(s), "
            f"but no physical joystick-button {name} toggle frames."
        )
    elif status_events:
        recognition_status = "candidate"
        summary = (
            f"Found {name} indicator lamp status/icon evidence, but no physical "
            f"{name} toggle command frame."
        )
    else:
        recognition_status = "not_observed"
        summary = f"No {name} indicator toggle or status evidence observed."

    return {
        "recognizer": f"{name}_indicator",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "expected_physical_toggle_id": f"0x{physical_toggle_id:08X}",
        "expected_status_id": f"0x{LAMP_STATUS_ID:08X}",
        "status_bit": f"0x{status_bit:02X}",
        "line_count": len(lines),
        "parsed_frame_count": len(parsed_frames),
        "physical_toggle_count": physical_count,
        "programmer_toggle_count": programmer_count,
        "total_toggle_count": total_toggle_count,
        "status_event_count": len(status_events),
        "physical_toggle_events": [
            {
                "timestamp": frame["timestamp"],
                "raw": frame["raw"],
            }
            for frame in physical_toggle_events
        ],
        "programmer_toggle_events": [
            {
                "timestamp": frame["timestamp"],
                "raw": frame["raw"],
            }
            for frame in programmer_toggle_events
        ],
        "toggle_intervals": toggle_intervals,
        "status_events": status_events,
    }


def recognize_left_indicator(lines: list[str]) -> dict[str, Any]:
    return recognize_indicator_toggle(
        lines,
        name="left",
        physical_toggle_id=LEFT_INDICATOR_TOGGLE_ID,
        status_bit=LAMP_LEFT,
        programmer_toggle_id=PROGRAMMER_LEFT_INDICATOR_TOGGLE_ID,
    )


def recognize_right_indicator(lines: list[str]) -> dict[str, Any]:
    return recognize_indicator_toggle(
        lines,
        name="right",
        physical_toggle_id=RIGHT_INDICATOR_TOGGLE_ID,
        status_bit=LAMP_RIGHT,
        programmer_toggle_id=PROGRAMMER_RIGHT_INDICATOR_TOGGLE_ID,
    )


def recognize_flood_headlight(lines: list[str]) -> dict[str, Any]:
    """
    Recognize flood/headlight activity.

    Strong evidence:
      0C000104# appears when the physical flood/headlight function is toggled.

    Weaker/status evidence:
      0C000400#maskbitmap may report/update lamp icon state.
      For that bitmap:
        bit 0x80 = flood/headlight

    Expected user action for this step:
      toggle flood/headlight ON, pause, toggle flood/headlight OFF

    So two physical toggle events is a strong match.
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is not None:
            parsed_frames.append(frame)

    physical_toggle_events = [
        frame for frame in parsed_frames if frame["can_id"] == FLOOD_HEADLIGHT_TOGGLE_ID
    ]

    programmer_toggle_events = [
        frame
        for frame in parsed_frames
        if frame["can_id"] == PROGRAMMER_FLOOD_HEADLIGHT_TOGGLE_ID
    ]

    status_events: list[dict[str, Any]] = []

    for frame in parsed_frames:
        if frame["can_id"] != LAMP_STATUS_ID:
            continue

        data = data_hex_to_bytes(frame["data_hex"])
        if len(data) < 2:
            continue

        mask = data[0]
        bitmap = data[1]

        if (mask & LAMP_FLOOD_HEADLIGHT) or (bitmap & LAMP_FLOOD_HEADLIGHT):
            status_events.append(
                {
                    "timestamp": frame["timestamp"],
                    "raw": frame["raw"],
                    "mask": f"0x{mask:02X}",
                    "bitmap": f"0x{bitmap:02X}",
                    "flood_headlight_masked": bool(mask & LAMP_FLOOD_HEADLIGHT),
                    "flood_headlight_on": bool(bitmap & LAMP_FLOOD_HEADLIGHT),
                }
            )

    all_toggle_events = sorted(
        physical_toggle_events + programmer_toggle_events,
        key=lambda frame: frame["timestamp"],
    )

    toggle_intervals: list[dict[str, Any]] = []
    for previous, current in zip(all_toggle_events, all_toggle_events[1:]):
        toggle_intervals.append(
            {
                "from_timestamp": previous["timestamp"],
                "to_timestamp": current["timestamp"],
                "delta_seconds": round(current["timestamp"] - previous["timestamp"], 6),
            }
        )

    physical_count = len(physical_toggle_events)
    programmer_count = len(programmer_toggle_events)
    total_toggle_count = len(all_toggle_events)

    if physical_count >= 2:
        recognition_status = "confirmed"
        summary = (
            f"Found {physical_count} physical flood/headlight toggle frame(s) "
            f"0x{FLOOD_HEADLIGHT_TOGGLE_ID:08X}. This matches ON then OFF."
        )
    elif physical_count == 1:
        recognition_status = "candidate"
        summary = (
            f"Found one physical flood/headlight toggle frame "
            f"0x{FLOOD_HEADLIGHT_TOGGLE_ID:08X}; expected two for ON then OFF."
        )
    elif programmer_count >= 2:
        recognition_status = "candidate"
        summary = (
            f"Found {programmer_count} programmer-diagnostic flood/headlight event(s), "
            "but no physical joystick-button flood/headlight toggle frames."
        )
    elif status_events:
        recognition_status = "candidate"
        summary = (
            "Found flood/headlight lamp status/icon evidence, but no physical "
            "flood/headlight toggle command frame."
        )
    else:
        recognition_status = "not_observed"
        summary = "No flood/headlight toggle or status evidence observed."

    return {
        "recognizer": "flood_headlight",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "expected_physical_toggle_id": f"0x{FLOOD_HEADLIGHT_TOGGLE_ID:08X}",
        "expected_status_id": f"0x{LAMP_STATUS_ID:08X}",
        "status_bit": f"0x{LAMP_FLOOD_HEADLIGHT:02X}",
        "line_count": len(lines),
        "parsed_frame_count": len(parsed_frames),
        "physical_toggle_count": physical_count,
        "programmer_toggle_count": programmer_count,
        "total_toggle_count": total_toggle_count,
        "status_event_count": len(status_events),
        "physical_toggle_events": [
            {
                "timestamp": frame["timestamp"],
                "raw": frame["raw"],
            }
            for frame in physical_toggle_events
        ],
        "programmer_toggle_events": [
            {
                "timestamp": frame["timestamp"],
                "raw": frame["raw"],
            }
            for frame in programmer_toggle_events
        ],
        "toggle_intervals": toggle_intervals,
        "status_events": status_events,
    }


def recognize_baseline_idle(lines: list[str]) -> dict[str, Any]:
    """
    Summarize basic CAN traffic during the baseline step.

    This intentionally does NOT check for known actions yet.
    Baseline is the first meet-and-greet capture, so its job is only to answer:
      - Did we capture anything?
      - How many lines were parseable CAN frames?
      - How many unique CAN IDs appeared?
      - Which IDs were most common?
      - Roughly how busy was the bus?
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is not None:
            parsed_frames.append(frame)

    if not parsed_frames:
        return {
            "recognizer": "baseline_idle",
            "implemented": True,
            "status": "timeout",
            "summary": "No parseable CAN frames observed during baseline capture.",
            "line_count": len(lines),
            "parsed_frame_count": 0,
            "unique_id_count": 0,
            "duration_seconds": 0.0,
            "approx_frame_rate_hz": None,
            "top_ids": [],
            "joystick_idle_inference": {
                "implemented": True,
                "status": "not_observed",
                "summary": "No parseable frames, so joystick ID could not be inferred.",
                "best_candidate": None,
                "ranked_candidates": [],
            },
        }

    timestamps = [frame["timestamp"] for frame in parsed_frames]
    first_timestamp = min(timestamps)
    last_timestamp = max(timestamps)
    duration_seconds = max(0.0, last_timestamp - first_timestamp)

    id_counter = Counter(frame["can_id"] for frame in parsed_frames)

    top_ids = []
    for can_id, count in id_counter.most_common(12):
        top_ids.append(
            {
                "can_id": f"0x{can_id:08X}",
                "count": count,
                "approx_rate_hz": (
                    round(count / duration_seconds, 3) if duration_seconds > 0 else None
                ),
            }
        )

    frame_rate_hz = (
        round(len(parsed_frames) / duration_seconds, 3)
        if duration_seconds > 0
        else None
    )

    summary = (
        f"Captured {len(parsed_frames)} parseable CAN frame(s) "
        f"across {len(id_counter)} unique CAN ID(s)"
    )

    if duration_seconds > 0:
        summary += f" over {duration_seconds:.3f}s"
        if frame_rate_hz is not None:
            summary += f" (~{frame_rate_hz} frames/sec)"

    summary += ". "

    # Infer joystick ID from idle frames. This is best candidate for joystick,
    # won't be confirmed until later tests.
    joystick_idle_inference = infer_joystick_id_from_idle_frames(parsed_frames)
    summary += joystick_idle_inference["summary"]

    return {
        "recognizer": "baseline_idle",
        "implemented": True,
        "status": "confirmed",
        "summary": summary,
        "line_count": len(lines),
        "parsed_frame_count": len(parsed_frames),
        "unique_id_count": len(id_counter),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "duration_seconds": round(duration_seconds, 6),
        "approx_frame_rate_hz": frame_rate_hz,
        "top_ids": top_ids,
        "joystick_idle_inference": joystick_idle_inference,
    }


def recognize_joystick_calibration(
    lines: list[str],
    *,
    deadzone: int = JOYSTICK_DEADZONE_DEFAULT,
) -> dict[str, Any]:
    """
    Recognize joystick ID, center, axes, and polarity from a guided pattern.

    Expected user pattern:
      center -> forward -> center -> reverse -> center -> left -> center -> right -> center

    The recognizer uses the order of movement phases to infer:
      movement phase 1 = forward
      movement phase 2 = reverse
      movement phase 3 = left
      movement phase 4 = right
    """
    samples = extract_two_byte_xy_samples(lines)

    if not samples:
        return {
            "recognizer": "joystick_calibration",
            "implemented": True,
            "status": "timeout",
            "summary": "No two-byte CAN frames found for joystick calibration.",
            "line_count": len(lines),
            "sample_count": 0,
            "ranked_candidates": [],
            "best_candidate": None,
        }

    samples_by_id: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        samples_by_id.setdefault(sample["can_id"], []).append(sample)

    ranked_candidates: list[dict[str, Any]] = []

    for can_id, id_samples in samples_by_id.items():
        if len(id_samples) < 5:
            continue

        pair_counts = Counter((sample["x"], sample["y"]) for sample in id_samples)
        center_pair, center_count = pair_counts.most_common(1)[0]
        center_x, center_y = center_pair
        center_fraction = center_count / len(id_samples)

        xs = [sample["x"] for sample in id_samples]
        ys = [sample["y"] for sample in id_samples]

        phases = compress_joystick_motion_phases(
            id_samples,
            center_x=center_x,
            center_y=center_y,
            deadzone=deadzone,
        )

        movement_phases = [phase for phase in phases if phase["kind"] == "movement"]

        movement_states = [phase["state"] for phase in movement_phases]

        unique_movement_states = sorted(set(movement_states))

        pattern_confirmed = False
        inferred_mapping: dict[str, Any] | None = None
        pattern_notes: list[str] = []

        if len(movement_phases) >= 4:
            forward_phase = movement_phases[0]
            reverse_phase = movement_phases[1]
            left_phase = movement_phases[2]
            right_phase = movement_phases[3]

            forward_state = forward_phase["state"]
            reverse_state = reverse_phase["state"]
            left_state = left_phase["state"]
            right_state = right_phase["state"]

            forward_axis, forward_sign = state_axis_and_sign(forward_state)
            reverse_axis, reverse_sign = state_axis_and_sign(reverse_state)
            left_axis, left_sign = state_axis_and_sign(left_state)
            right_axis, right_sign = state_axis_and_sign(right_state)

            forward_reverse_opposed = states_are_opposites(
                forward_state,
                reverse_state,
            )
            left_right_opposed = states_are_opposites(
                left_state,
                right_state,
            )
            axes_different = (
                forward_axis is not None
                and left_axis is not None
                and forward_axis != left_axis
            )

            pattern_confirmed = (
                forward_reverse_opposed and left_right_opposed and axes_different
            )

            inferred_mapping = {
                "forward": {
                    "state": forward_state,
                    "axis": forward_axis,
                    "sign": forward_sign,
                    "phase": forward_phase,
                },
                "reverse": {
                    "state": reverse_state,
                    "axis": reverse_axis,
                    "sign": reverse_sign,
                    "phase": reverse_phase,
                },
                "left": {
                    "state": left_state,
                    "axis": left_axis,
                    "sign": left_sign,
                    "phase": left_phase,
                },
                "right": {
                    "state": right_state,
                    "axis": right_axis,
                    "sign": right_sign,
                    "phase": right_phase,
                },
                "forward_reverse_opposed": forward_reverse_opposed,
                "left_right_opposed": left_right_opposed,
                "axes_different": axes_different,
            }

            if not forward_reverse_opposed:
                pattern_notes.append("First two movement phases were not opposites.")
            if not left_right_opposed:
                pattern_notes.append(
                    "Third and fourth movement phases were not opposites."
                )
            if not axes_different:
                pattern_notes.append(
                    "Forward/reverse axis and left/right axis were not clearly different."
                )
        else:
            pattern_notes.append(
                f"Only found {len(movement_phases)} movement phase(s); expected at least 4."
            )

        timestamps = [sample["timestamp"] for sample in id_samples]
        duration_seconds = max(0.0, max(timestamps) - min(timestamps))
        rate_hz = len(id_samples) / duration_seconds if duration_seconds > 0 else None

        rnet_family = looks_like_rnet_joystick_family(can_id)
        channel_byte = (can_id >> 8) & 0xFF

        x_range = max(xs) - min(xs)
        y_range = max(ys) - min(ys)

        score = 0.0

        if rnet_family:
            score += 60.0
            score += max(0.0, 32.0 - float(channel_byte))

        if pattern_confirmed:
            score += 200.0
        else:
            score += len(unique_movement_states) * 25.0

        score += min(float(x_range + y_range) * 2.0, 80.0)
        score += min(float(len(id_samples)) / 5.0, 40.0)
        score += center_fraction * 30.0

        if rate_hz is not None and rate_hz >= 10:
            score += 20.0

        movement_windows: list[dict[str, Any]] = []

        # use inferred mapping to find likely drive response candidates
        if inferred_mapping is not None:
            for direction_name in ["forward", "reverse", "left", "right"]:
                direction_info = inferred_mapping.get(direction_name)
                if not direction_info:
                    continue

                phase = direction_info.get("phase")
                if not phase:
                    continue

                movement_windows.append(
                    make_movement_window_from_phase(
                        direction_name,
                        phase,
                    )
                )
        else:
            for index, phase in enumerate(movement_phases, start=1):
                movement_windows.append(
                    make_movement_window_from_phase(
                        f"movement_{index}",
                        phase,
                    )
                )

        drive_response_candidates = recognize_drive_response_candidates(
            lines,
            movement_windows=movement_windows,
            joystick_can_id=can_id,
            source_step="joystick_calibration_low_movement",
        )

        ranked_candidates.append(
            {
                "can_id": f"0x{can_id:08X}",
                "can_id_int": can_id,
                "score": round(score, 3),
                "rnet_joystick_family": rnet_family,
                "channel_byte": f"0x{channel_byte:02X}",
                "sample_count": len(id_samples),
                "rate_hz": round(rate_hz, 3) if rate_hz is not None else None,
                "center": {
                    "x": center_x,
                    "y": center_y,
                    "sample_count": center_count,
                    "fraction": round(center_fraction, 3),
                },
                "x_min": min(xs),
                "x_max": max(xs),
                "x_range": x_range,
                "y_min": min(ys),
                "y_max": max(ys),
                "y_range": y_range,
                "movement_states": movement_states,
                "unique_movement_states": unique_movement_states,
                "pattern_confirmed": pattern_confirmed,
                "pattern_notes": pattern_notes,
                "inferred_mapping": inferred_mapping,
                "drive_response_candidates": drive_response_candidates,
                "phases": phases[:20],
            }
        )

    ranked_candidates.sort(
        key=lambda candidate: candidate["score"],
        reverse=True,
    )

    if not ranked_candidates:
        return {
            "recognizer": "joystick_calibration",
            "implemented": True,
            "status": "not_observed",
            "summary": "No usable joystick calibration candidates found.",
            "line_count": len(lines),
            "sample_count": len(samples),
            "ranked_candidates": [],
            "best_candidate": None,
        }

    best = ranked_candidates[0]

    drive_response_note = summarize_top_drive_response_candidates(
        best.get("drive_response_candidates"),
    )

    if best["pattern_confirmed"]:
        recognition_status = "confirmed"
        summary = (
            f"Confirmed joystick candidate {best['can_id']}. "
            f"Center appears to be X={best['center']['x']}, Y={best['center']['y']}. "
            "Movement pattern identified forward/reverse and left/right axes. "
            f"{drive_response_note}"
        )
    elif len(best["unique_movement_states"]) >= 2:
        recognition_status = "candidate"
        summary = (
            f"Found joystick-like movement candidate {best['can_id']}, "
            "but the full forward/reverse/left/right pattern was not clearly confirmed. "
            f"{drive_response_note}"
        )
    else:
        recognition_status = "not_observed"
        summary = (
            "No clear joystick movement pattern observed. "
            f"Best candidate was {best['can_id']}. "
            f"{drive_response_note}"
        )
    return {
        "recognizer": "joystick_calibration",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "line_count": len(lines),
        "sample_count": len(samples),
        "deadzone": deadzone,
        "best_candidate": best,
        "ranked_candidates": ranked_candidates[:12],
    }


def recognize_joystick_single_direction(
    lines: list[str],
    *,
    direction_name: str,
    deadzone: int = JOYSTICK_DEADZONE_DEFAULT,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """
    Recognize one independently prompted joystick push.

    The step tells us the intended direction:
      joystick_forward  -> direction_name="forward"
      joystick_reverse  -> direction_name="reverse"
      joystick_left     -> direction_name="left"
      joystick_right    -> direction_name="right"

    This function does NOT assume ahead of time whether that direction is:
      x positive, x negative, y positive, or y negative.

    It observes the strongest movement phase and records the axis/sign/range.
    """
    samples = extract_two_byte_xy_samples(lines)

    if not samples:
        return {
            "recognizer": f"joystick_{direction_name}",
            "implemented": True,
            "status": "timeout",
            "summary": f"No two-byte CAN frames found during joystick {direction_name} test.",
            "line_count": len(lines),
            "sample_count": 0,
            "ranked_candidates": [],
            "best_candidate": None,
        }

    samples_by_id: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        samples_by_id.setdefault(sample["can_id"], []).append(sample)

    used_known_joystick_id = False
    known_joystick_missing = False

    if known_joystick_can_id is not None:
        if known_joystick_can_id in samples_by_id:
            candidate_can_ids = [known_joystick_can_id]
            used_known_joystick_id = True
        else:
            candidate_can_ids = sorted(samples_by_id.keys())
            known_joystick_missing = True
    else:
        candidate_can_ids = sorted(samples_by_id.keys())

    ranked_candidates: list[dict[str, Any]] = []

    for can_id in candidate_can_ids:
        id_samples = samples_by_id[can_id]
        if len(id_samples) < 5:
            continue

        pair_counts = Counter((sample["x"], sample["y"]) for sample in id_samples)

        use_known_center = (
            known_joystick_center is not None
            and (known_joystick_can_id is None or can_id == known_joystick_can_id)
        )

        if use_known_center:
            center_x, center_y = known_joystick_center
            center_source = "joystick_calibration"
            center_count = sum(
                1
                for sample in id_samples
                if sample_is_centered(
                    sample,
                    center_x=center_x,
                    center_y=center_y,
                    deadzone=deadzone,
                )
            )
        else:
            center_pair, center_count = pair_counts.most_common(1)[0]
            center_x, center_y = center_pair
            center_source = "most_common_pair_in_this_step"

        center_fraction = center_count / len(id_samples)

        phases = compress_joystick_motion_phases(
            id_samples,
            center_x=center_x,
            center_y=center_y,
            deadzone=deadzone,
            min_phase_samples=2,
        )

        movement_phases = [phase for phase in phases if phase["kind"] == "movement"]

        if movement_phases:
            best_phase = max(
                movement_phases,
                key=lambda phase: max(
                    phase["max_abs_dx"],
                    phase["max_abs_dy"],
                ),
            )
            direction_range = summarize_single_direction_phase(
                direction_name,
                best_phase,
                center_x=center_x,
                center_y=center_y,
            )
            movement_peak = direction_range["primary_abs_peak"] or 0
        else:
            best_phase = None
            direction_range = None
            movement_peak = 0

        movement_windows = []

        if best_phase is not None:
            movement_windows.append(
                make_movement_window_from_phase(
                    direction_name,
                    best_phase,
                )
            )

        drive_response_candidates = recognize_drive_response_candidates(
            lines,
            movement_windows=movement_windows,
            joystick_can_id=can_id,
            source_step=f"joystick_{direction_name}_large_movement",
        )

        timestamps = [sample["timestamp"] for sample in id_samples]
        duration_seconds = max(0.0, max(timestamps) - min(timestamps))
        rate_hz = len(id_samples) / duration_seconds if duration_seconds > 0 else None

        xs = [sample["x"] for sample in id_samples]
        ys = [sample["y"] for sample in id_samples]

        rnet_family = looks_like_rnet_joystick_family(can_id)
        channel_byte = (can_id >> 8) & 0xFF

        score = 0.0

        # Movement strength matters most for the independent direction tests.
        score += movement_peak * 10.0

        # Still prefer R-Net joystick-looking IDs.
        if rnet_family:
            score += 60.0
            score += max(0.0, 32.0 - float(channel_byte))

        # Prefer streams with enough samples and a visible center/rest value.
        score += min(float(len(id_samples)) / 5.0, 40.0)
        score += center_fraction * 30.0

        if rate_hz is not None and rate_hz >= 10:
            score += 20.0

        ranked_candidates.append(
            {
                "can_id": f"0x{can_id:08X}",
                "can_id_int": can_id,
                "score": round(score, 3),
                "rnet_joystick_family": rnet_family,
                "channel_byte": f"0x{channel_byte:02X}",
                "sample_count": len(id_samples),
                "rate_hz": round(rate_hz, 3) if rate_hz is not None else None,
                "center": {
                    "x": center_x,
                    "y": center_y,
                    "sample_count": center_count,
                    "fraction": round(center_fraction, 3),
                    "source": center_source,
                },
                "x_min": min(xs),
                "x_max": max(xs),
                "x_range": max(xs) - min(xs),
                "y_min": min(ys),
                "y_max": max(ys),
                "y_range": max(ys) - min(ys),
                "movement_phase_count": len(movement_phases),
                "direction_range": direction_range,
                "drive_response_candidates": drive_response_candidates,
                "phases": phases[:20],
                "used_known_joystick_id": used_known_joystick_id,
                "known_joystick_missing": known_joystick_missing,
                "known_joystick_can_id": (
                    f"0x{known_joystick_can_id:08X}"
                    if known_joystick_can_id is not None
                    else None
                ),
            }
        )

    ranked_candidates.sort(
        key=lambda candidate: candidate["score"],
        reverse=True,
    )

    if not ranked_candidates:
        return {
            "recognizer": f"joystick_{direction_name}",
            "implemented": True,
            "status": "not_observed",
            "summary": f"No usable joystick candidates found during {direction_name} test.",
            "line_count": len(lines),
            "sample_count": len(samples),
            "ranked_candidates": [],
            "best_candidate": None,
        }

    best = ranked_candidates[0]
    direction_range = best["direction_range"]

    drive_response_note = summarize_top_drive_response_candidates(
        best.get("drive_response_candidates"),
    )

    if direction_range is None:
        recognition_status = "not_observed"
        summary = (
            f"No clear joystick movement phase observed during {direction_name} test. "
            f"Best candidate was {best['can_id']}. "
            f"{drive_response_note}"
        )
    elif (direction_range["primary_abs_peak"] or 0) > deadzone:
        recognition_status = "confirmed"
        summary = (
            f"Observed joystick {direction_name} range on {best['can_id']}: "
            f"axis={direction_range['axis']}, "
            f"sign={direction_range['sign']}, "
            f"peak={direction_range['signed_peak_from_center']} from center. "
            f"{drive_response_note}"
        )
    else:
        recognition_status = "candidate"
        summary = (
            f"Found weak joystick {direction_name} movement candidate on {best['can_id']}, "
            f"but peak was only {direction_range['primary_abs_peak']}. "
            f"{drive_response_note}"
        )

    return {
        "recognizer": f"joystick_{direction_name}",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "line_count": len(lines),
        "sample_count": len(samples),
        "deadzone": deadzone,
        "best_candidate": best,
        "ranked_candidates": ranked_candidates[:12],
    }


def recognize_joystick_forward(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_joystick_single_direction(
        lines,
        direction_name="forward",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_joystick_reverse(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_joystick_single_direction(
        lines,
        direction_name="reverse",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_joystick_left(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_joystick_single_direction(
        lines,
        direction_name="left",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_joystick_right(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_joystick_single_direction(
        lines,
        direction_name="right",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_drive_response_candidates(
    lines: list[str],
    *,
    movement_windows: list[dict[str, Any]],
    joystick_can_id: int | None,
    source_step: str,
    max_candidates: int = 12,
    exclude_rnet_joystick_family: bool = True,
) -> dict[str, Any]:
    """
    Rank non-joystick CAN IDs that appear to respond during joystick movement.

    This is intentionally generic:
      - It does not assume motor-current ID ahead of time.
      - It does not assume data length or byte meaning.
      - It scores frames that change during joystick movement windows.

    Good candidates:
      - have data during movement
      - have movement values that differ from center/rest values
      - show more variation during movement than during rest
      - appear repeatedly enough to be meaningful
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is None:
            continue

        # Ignore empty payloads for drive-response ranking.
        if not frame["data_hex"]:
            continue

        parsed_frames.append(frame)

    if not parsed_frames:
        return {
            "recognizer": "drive_response_candidates",
            "implemented": True,
            "status": "not_observed",
            "summary": "No parseable data frames available for drive-response ranking.",
            "source_step": source_step,
            "movement_windows": movement_windows,
            "ranked_candidates": [],
        }

    if not movement_windows:
        return {
            "recognizer": "drive_response_candidates",
            "implemented": True,
            "status": "not_observed",
            "summary": "No joystick movement windows available for drive-response ranking.",
            "source_step": source_step,
            "movement_windows": movement_windows,
            "ranked_candidates": [],
        }

    frames_by_id: dict[int, list[dict[str, Any]]] = {}

    for frame in parsed_frames:
        can_id = frame["can_id"]

        # Do not rank joystick command/status-family frames as motor/controller response.
        if joystick_can_id is not None and can_id == joystick_can_id:
            continue

        if exclude_rnet_joystick_family and looks_like_rnet_joystick_family(can_id):
            continue
        frames_by_id.setdefault(can_id, []).append(frame)

    candidates: list[dict[str, Any]] = []

    for can_id, id_frames in frames_by_id.items():
        if len(id_frames) < 3:
            continue

        movement_frames = [
            frame
            for frame in id_frames
            if frame_is_inside_any_window(frame, movement_windows)
        ]

        rest_frames = [
            frame
            for frame in id_frames
            if not frame_is_inside_any_window(frame, movement_windows)
        ]

        if not movement_frames:
            continue

        movement_values = [frame["data_hex"] for frame in movement_frames]
        rest_values = [frame["data_hex"] for frame in rest_frames]

        movement_value_counts = Counter(movement_values)
        rest_value_counts = Counter(rest_values)

        movement_unique = set(movement_values)
        rest_unique = set(rest_values)

        movement_only_values = movement_unique - rest_unique

        most_common_rest_value = None
        if rest_value_counts:
            most_common_rest_value = rest_value_counts.most_common(1)[0][0]

        if most_common_rest_value is not None:
            movement_changed_count = sum(
                1 for value in movement_values if value != most_common_rest_value
            )
            movement_changed_fraction = movement_changed_count / len(movement_values)
        else:
            movement_changed_count = len(movement_values)
            movement_changed_fraction = 1.0

        timestamps = [frame["timestamp"] for frame in id_frames]
        duration_seconds = max(0.0, max(timestamps) - min(timestamps))
        overall_rate_hz = (
            len(id_frames) / duration_seconds if duration_seconds > 0 else None
        )

        movement_window_duration = sum(
            max(0.0, window["end_timestamp"] - window["start_timestamp"])
            for window in movement_windows
        )

        movement_rate_hz = (
            len(movement_frames) / movement_window_duration
            if movement_window_duration > 0
            else None
        )

        rest_duration = max(0.0, duration_seconds - movement_window_duration)
        rest_rate_hz = len(rest_frames) / rest_duration if rest_duration > 0 else None

        score = 0.0

        # Strong signal: values during movement differ from rest.
        score += movement_changed_fraction * 80.0
        score += min(len(movement_only_values) * 12.0, 60.0)

        # Useful signal: movement window contains repeated frames.
        score += min(len(movement_frames) * 2.0, 50.0)

        # Useful signal: payload varies during movement.
        score += min(len(movement_unique) * 8.0, 50.0)

        # Slight boost if frame rate increases during movement.
        if movement_rate_hz is not None and rest_rate_hz is not None:
            if movement_rate_hz > rest_rate_hz * 1.25:
                score += 20.0

        # Penalize IDs that are totally constant.
        if (
            len(movement_unique) == 1
            and len(rest_unique) == 1
            and movement_unique == rest_unique
        ):
            score -= 100.0

        # Penalize super-chatty constant status frames a little.
        if len(movement_unique) <= 2 and movement_changed_fraction < 0.25:
            score -= 30.0

        candidates.append(
            {
                "can_id": f"0x{can_id:08X}",
                "can_id_int": can_id,
                "score": round(score, 3),
                "source_step": source_step,
                "total_frame_count": len(id_frames),
                "movement_frame_count": len(movement_frames),
                "rest_frame_count": len(rest_frames),
                "overall_rate_hz": (
                    round(overall_rate_hz, 3) if overall_rate_hz is not None else None
                ),
                "movement_rate_hz": (
                    round(movement_rate_hz, 3) if movement_rate_hz is not None else None
                ),
                "rest_rate_hz": (
                    round(rest_rate_hz, 3) if rest_rate_hz is not None else None
                ),
                "movement_changed_count": movement_changed_count,
                "movement_changed_fraction": round(movement_changed_fraction, 3),
                "movement_only_value_count": len(movement_only_values),
                "movement_only_values": sorted(movement_only_values)[:12],
                "rest_summary": summarize_data_values(rest_frames),
                "movement_summary": summarize_data_values(movement_frames),
            }
        )

    candidates.sort(
        key=lambda candidate: candidate["score"],
        reverse=True,
    )

    useful_candidates = [
        candidate for candidate in candidates if candidate["score"] > 0
    ]

    if useful_candidates:
        status = "candidate"
        summary = (
            f"Found {len(useful_candidates)} drive-response candidate ID(s) "
            f"during {source_step}."
        )
    else:
        status = "not_observed"
        summary = f"No useful drive-response candidates found during {source_step}."

    return {
        "recognizer": "drive_response_candidates",
        "implemented": True,
        "status": status,
        "summary": summary,
        "source_step": source_step,
        "joystick_can_id": (
            f"0x{joystick_can_id:08X}" if joystick_can_id is not None else None
        ),
        "movement_windows": movement_windows,
        "ranked_candidates": useful_candidates[:max_candidates],
    }


def summarize_top_drive_response_candidates(
    drive_response: dict[str, Any] | None,
    *,
    limit: int = 3,
) -> str:
    """
    Short human-readable note for joystick summaries.

    Example:
      Likely drive-response candidates: 0x14300000 score=124.5,
      0x1C300004 score=88.0.
    """
    if not drive_response:
        return "No drive-response candidate scan was available."

    candidates = drive_response.get("ranked_candidates", [])

    if not candidates:
        return "No likely drive-response candidates found."

    top = candidates[:limit]

    parts = []
    for candidate in top:
        can_id = candidate.get("can_id", "unknown")
        score = candidate.get("score", "?")
        changed_fraction = candidate.get("movement_changed_fraction")

        if changed_fraction is None:
            parts.append(f"{can_id} score={score}")
        else:
            parts.append(f"{can_id} score={score}, changed={changed_fraction}")

    extra_count = len(candidates) - len(top)

    note = "Likely drive-response candidates: " + "; ".join(parts)

    if extra_count > 0:
        note += f"; plus {extra_count} more"

    note += "."

    return note



def parse_seating_status_blocks(lines: list[str]) -> list[dict[str, Any]]:
    """
    Parse passive seating/display status blocks.

    Observed pattern for this chair/profile:
      0C180300#0202          block start candidate
      0C180301#220101        first function/status entry; often selected function
      0C180301#300101        metadata/status
      ...
      0C180300#0201          block end candidate

    This is passive evidence only. It is not a command map.
    """
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is None:
            continue

        can_id = frame["can_id"]
        data_hex = frame["data_hex"]

        if can_id == SEATING_BLOCK_ID and data_hex == SEATING_BLOCK_START_DATA:
            if current is not None:
                current["complete"] = False
                current["end_timestamp"] = None
                current["end_line"] = None
                _finalize_seating_status_block(current)
                blocks.append(current)

            current = {
                "start_timestamp": frame["timestamp"],
                "end_timestamp": None,
                "duration_seconds": None,
                "start_line": frame["raw"],
                "end_line": None,
                "complete": False,
                "function_entries": [],
                "selected_function_code": None,
                "selected_function_name": None,
                "selected_function_display_name": None,
                "selected_function_line": None,
            }
            continue

        if current is None:
            continue

        if can_id == SEATING_FUNCTION_STATUS_ID:
            code = data_hex[:2].upper() if len(data_hex) >= 2 else None
            spec = SEATING_FUNCTION_SPECS.get(code or "")
            current["function_entries"].append(
                {
                    "timestamp": frame["timestamp"],
                    "raw": frame["raw"],
                    "data_hex": data_hex,
                    "code": code,
                    "known_function": spec is not None,
                    "function_name": spec.get("name") if spec else None,
                    "function_display_name": spec.get("display_name") if spec else None,
                }
            )
            continue

        if can_id == SEATING_BLOCK_ID and data_hex == SEATING_BLOCK_END_DATA:
            current["complete"] = True
            current["end_timestamp"] = frame["timestamp"]
            current["end_line"] = frame["raw"]
            current["duration_seconds"] = round(
                frame["timestamp"] - current["start_timestamp"],
                6,
            )
            _finalize_seating_status_block(current)
            blocks.append(current)
            current = None
            continue

    if current is not None:
        current["complete"] = False
        _finalize_seating_status_block(current)
        blocks.append(current)

    return blocks


def _finalize_seating_status_block(block: dict[str, Any]) -> None:
    entries = block.get("function_entries") or []
    if not entries:
        return

    # Current best passive rule from Bumblebee logs:
    # the first 0C180301 entry in a block is the selected seating function.
    selected = entries[0]
    code = selected.get("code")
    spec = SEATING_FUNCTION_SPECS.get(code or "")

    block["selected_function_code"] = code
    block["selected_function_name"] = spec.get("name") if spec else None
    block["selected_function_display_name"] = spec.get("display_name") if spec else None
    block["selected_function_line"] = selected.get("raw")


def summarize_seating_blocks(blocks: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "start_timestamp": block.get("start_timestamp"),
            "end_timestamp": block.get("end_timestamp"),
            "duration_seconds": block.get("duration_seconds"),
            "complete": block.get("complete"),
            "selected_function_code": block.get("selected_function_code"),
            "selected_function_name": block.get("selected_function_name"),
            "selected_function_display_name": block.get("selected_function_display_name"),
            "selected_function_line": block.get("selected_function_line"),
            "function_entry_count": len(block.get("function_entries") or []),
            "function_entries": block.get("function_entries", [])[:8],
            "start_line": block.get("start_line"),
            "end_line": block.get("end_line"),
        }
        for block in blocks[:limit]
    ]


def latest_seating_block_before_or_during(
    blocks: list[dict[str, Any]],
    timestamp: float,
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None

    for block in blocks:
        start = block.get("start_timestamp")
        if start is None:
            continue
        if start <= timestamp:
            latest = block
        else:
            break

    return latest


def choose_joystick_samples_for_seating_validation(
    lines: list[str],
    *,
    known_joystick_can_id: int | None,
) -> tuple[int | None, list[dict[str, Any]], str]:
    samples = extract_two_byte_xy_samples(lines)
    if not samples:
        return None, [], "no_two_byte_samples"

    samples_by_id: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        samples_by_id.setdefault(sample["can_id"], []).append(sample)

    if known_joystick_can_id is not None and known_joystick_can_id in samples_by_id:
        return known_joystick_can_id, samples_by_id[known_joystick_can_id], "known_joystick_can_id"

    # Fallback for custom/replay logs when a profile has not been loaded yet.
    # Prefer 0x0200NN00 family streams with the most samples.
    ranked_ids = sorted(
        samples_by_id.keys(),
        key=lambda can_id: (
            looks_like_rnet_joystick_family(can_id),
            len(samples_by_id[can_id]),
        ),
        reverse=True,
    )

    if not ranked_ids:
        return None, [], "no_candidate_ids"

    chosen = ranked_ids[0]
    return chosen, samples_by_id[chosen], "fallback_highest_sample_joystick_like_id"


def seating_motion_label_for_state(
    state: str,
    *,
    expected_function_name: str,
) -> str | None:
    code = SEATING_FUNCTION_NAME_TO_CODE.get(expected_function_name)
    if code is None:
        return None

    spec = SEATING_FUNCTION_SPECS[code]
    if state == spec["more_state"]:
        return "more"
    if state == spec["less_state"]:
        return "less"
    return None



def is_profile_control_family(can_id: int) -> bool:
    """Return True for the 0x050 | node profile-control family."""
    return PROFILE_CONTROL_FAMILY_BASE <= can_id <= PROFILE_CONTROL_FAMILY_MAX


def classify_profile_control_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """
    Classify a frame from the R-Net profile-control family.

    Passive interpretation only. The request shape observed on Bumblebee and
    described by rnet-lib is:
      0x050 | node, payload 00 PP 00 00
    where PP is the requested zero-based wire profile index. In user-facing
    profile configuration, that same profile is numbered PP + 1.

    PM-owned 0x050 response/status trains commonly echo PP in byte 1:
      050#11PP0002
      050#20PP0000
      050#30PP0001
      050#91PP0000
      050#81PP0001
    """
    can_id = frame["can_id"]
    if not is_profile_control_family(can_id):
        return None

    data = data_hex_to_bytes(frame["data_hex"])
    if len(data) < 4:
        return {
            "timestamp": frame["timestamp"],
            "can_id": frame["can_id_hex"],
            "node": can_id & 0xF,
            "data_hex": frame["data_hex"],
            "role": "profile_family_short_or_empty",
            "profile_index": None,
            "profile_number": None,
            "raw": frame["raw"],
        }

    node = can_id & 0xF
    first = data[0]
    profile_index = data[1]
    profile_index_valid = PROFILE_INDEX_MIN <= profile_index <= PROFILE_INDEX_MAX
    profile_number = profile_index_to_number(profile_index) if profile_index_valid else None

    role = "profile_family_other"
    confidence = "low"

    if node != 0 and first == 0x00 and data[2] == 0x00 and data[3] == 0x00:
        role = "profile_request"
        confidence = "high" if profile_index_valid else "low"
    elif node == 0 and profile_index_valid and (first & 0xF0) in {0x10, 0x20, 0x30, 0x80, 0x90}:
        role = "profile_response_or_status"
        confidence = "medium_high"
    elif node == 0 and first in {0xC0, 0xD0, 0xF0}:
        # Often seen during startup/profile-map-like bursts. Keep separate so it
        # does not overstate active profile changes.
        role = "profile_startup_map_or_status"
        confidence = "low_medium"

    return {
        "timestamp": frame["timestamp"],
        "can_id": frame["can_id_hex"],
        "node": node,
        "data_hex": frame["data_hex"],
        "role": role,
        "confidence": confidence,
        "profile_index": profile_index if profile_index_valid else None,
        "profile_number": profile_number,
        "first_byte": f"0x{first:02X}",
        "raw": frame["raw"],
    }


def group_profile_events(
    classified_frames: list[dict[str, Any]],
    *,
    max_gap_seconds: float = 0.25,
) -> list[dict[str, Any]]:
    """
    Group nearby profile request/response frames into transition events.
    """
    interesting = [
        item for item in classified_frames
        if item.get("role") in {"profile_request", "profile_response_or_status"}
    ]
    if not interesting:
        return []

    interesting.sort(key=lambda item: item["timestamp"])
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    for item in interesting:
        if not current:
            current = [item]
            continue
        if item["timestamp"] - current[-1]["timestamp"] <= max_gap_seconds:
            current.append(item)
            continue
        groups.append(current)
        current = [item]

    if current:
        groups.append(current)

    events: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        profile_counts = Counter(
            item["profile_index"] for item in group
            if item.get("profile_index") is not None
        )
        requested = [
            item for item in group
            if item.get("role") == "profile_request"
            and item.get("profile_index") is not None
        ]
        responses = [
            item for item in group
            if item.get("role") == "profile_response_or_status"
            and item.get("profile_index") is not None
        ]

        requested_indices = sorted({item["profile_index"] for item in requested})
        response_indices = sorted({item["profile_index"] for item in responses})
        requested_numbers = profile_indices_to_numbers(requested_indices)
        response_numbers = profile_indices_to_numbers(response_indices)

        active_profile = None
        if response_indices:
            active_profile = profile_counts.most_common(1)[0][0]
        elif requested_indices:
            active_profile = requested_indices[-1]
        active_profile_number = profile_index_to_number(active_profile)

        events.append(
            {
                "event_index": index,
                "start_timestamp": group[0]["timestamp"],
                "end_timestamp": group[-1]["timestamp"],
                "duration_seconds": round(group[-1]["timestamp"] - group[0]["timestamp"], 6),
                "frame_count": len(group),
                "requested_profile_indices": requested_indices,
                "requested_profile_numbers": requested_numbers,
                "response_profile_indices": response_indices,
                "response_profile_numbers": response_numbers,
                "inferred_active_profile_index": active_profile,
                "inferred_active_profile_number": active_profile_number,
                "profile_index_counts": {
                    str(profile): count for profile, count in sorted(profile_counts.items())
                },
                "profile_number_counts": {
                    str(profile_index_to_number(profile)): count
                    for profile, count in sorted(profile_counts.items())
                    if profile_index_to_number(profile) is not None
                },
                "request_count": len(requested),
                "response_count": len(responses),
                "example_lines": [item["raw"] for item in group[:8]],
            }
        )

    return events


def recognize_profile_discovery(lines: list[str]) -> dict[str, Any]:
    """
    Recognize enabled/active profile indices from a user-performed profile cycle.

    User step: start in a known profile, then repeatedly press the chair's
    normal profile/mode button until the starting profile repeats. This
    recognizer reports observed request/response indices without assuming
    chair-specific display names.
    """
    frames = [parse_can_log_line(line) for line in lines]
    parsed_frames = [frame for frame in frames if frame is not None]

    classified = []
    for frame in parsed_frames:
        item = classify_profile_control_frame(frame)
        if item is not None:
            classified.append(item)

    requests = [item for item in classified if item.get("role") == "profile_request"]
    responses = [item for item in classified if item.get("role") == "profile_response_or_status"]
    startup_map = [item for item in classified if item.get("role") == "profile_startup_map_or_status"]
    other_profile_family = [
        item for item in classified
        if item.get("role") not in {
            "profile_request",
            "profile_response_or_status",
            "profile_startup_map_or_status",
        }
    ]

    request_indices = sorted({
        item["profile_index"] for item in requests
        if item.get("profile_index") is not None
    })
    response_indices = sorted({
        item["profile_index"] for item in responses
        if item.get("profile_index") is not None
    })
    observed_indices = sorted(set(request_indices) | set(response_indices))
    request_numbers = profile_indices_to_numbers(request_indices)
    response_numbers = profile_indices_to_numbers(response_indices)
    observed_numbers = profile_indices_to_numbers(observed_indices)

    events = group_profile_events(classified)
    active_sequence = [
        event.get("inferred_active_profile_index") for event in events
        if event.get("inferred_active_profile_index") is not None
    ]
    active_number_sequence = profile_indices_to_numbers(active_sequence)

    if len(observed_indices) >= 2 and responses:
        status = "confirmed"
        summary = (
            "Confirmed profile-control family activity while cycling profiles: "
            f"observed profile numbers {observed_numbers} "
            f"(wire indices {observed_indices}), with PM-owned 0x050 "
            "response/status evidence."
        )
    elif observed_indices and (requests or responses):
        status = "candidate"
        summary = (
            "Observed profile-control family activity, but only one profile index or "
            "limited response evidence was present."
        )
    else:
        status = "not_observed"
        summary = (
            "No clear 0x050|node profile request/response activity observed. "
            "Ask the user to cycle profiles with the chair's normal controls."
        )

    enabled_profile_candidates = [
        {
            "profile_index": profile_index,
            "profile_number": profile_index_to_number(profile_index),
            "request_count": sum(1 for item in requests if item.get("profile_index") == profile_index),
            "response_count": sum(1 for item in responses if item.get("profile_index") == profile_index),
            "event_count": sum(
                1 for event in events
                if event.get("inferred_active_profile_index") == profile_index
            ),
        }
        for profile_index in observed_indices
    ]

    return {
        "recognizer": "profile_discovery",
        "implemented": True,
        "status": status,
        "summary": summary,
        "line_count": len(lines),
        "profile_family_base": "0x050",
        "profile_request_pattern": "0x050|node payload 00 PP 00 00",
        "profile_numbering_note": (
            "Profile configuration uses profile numbers 1-8; wire frames use "
            "zero-based profile indices 0-7, so number = index + 1."
        ),
        "observed_profile_indices": observed_indices,
        "observed_profile_numbers": observed_numbers,
        "request_profile_indices": request_indices,
        "request_profile_numbers": request_numbers,
        "response_profile_indices": response_indices,
        "response_profile_numbers": response_numbers,
        "active_profile_sequence": active_sequence,
        "active_profile_number_sequence": active_number_sequence,
        "enabled_profile_candidates": enabled_profile_candidates,
        "profile_event_count": len(events),
        "profile_events": events[:20],
        "profile_request_count": len(requests),
        "profile_response_count": len(responses),
        "profile_startup_map_or_status_count": len(startup_map),
        "other_profile_family_count": len(other_profile_family),
        "profile_requests": requests[:20],
        "profile_responses": responses[:30],
        "profile_startup_map_or_status_examples": startup_map[:12],
        "other_profile_family_examples": other_profile_family[:12],
        "safety_note": (
            "Passive profile discovery only. Profile numbers and indices are "
            "chair/config specific; do not assume profile names or safety limits "
            "transfer to another chair."
        ),
    }

def recognize_seating_option_validation(
    lines: list[str],
    *,
    expected_function_name: str,
    deadzone: int = JOYSTICK_DEADZONE_DEFAULT,
    min_peak: int = 20,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """
    Validate one seating option using passive evidence.

    Expected user pattern, for example tilt:
      1. get the chair into tilt mode using normal controls
      2. tilt more for a bit
      3. center joystick
      4. tilt less for a bit
      5. center joystick

    The recognizer checks:
      - whether a selected-function block names the expected function
      - whether joystick movement phases occur while that function is selected
      - whether the first matching pair follows expected more -> less polarity
      - whether there is a center phase between more and less

    It never transmits. It only evaluates user-performed actions from logs.
    """
    expected_code = SEATING_FUNCTION_NAME_TO_CODE.get(expected_function_name)
    if expected_code is None:
        return {
            "recognizer": f"seating_{expected_function_name}_validation",
            "implemented": True,
            "status": "failed",
            "summary": f"Unknown seating function '{expected_function_name}'.",
            "known_functions": sorted(SEATING_FUNCTION_NAME_TO_CODE.keys()),
        }

    spec = SEATING_FUNCTION_SPECS[expected_code]
    blocks = parse_seating_status_blocks(lines)
    expected_blocks = [
        block for block in blocks
        if block.get("selected_function_code") == expected_code
    ]

    joystick_can_id, joystick_samples, joystick_source = choose_joystick_samples_for_seating_validation(
        lines,
        known_joystick_can_id=known_joystick_can_id,
    )

    if not joystick_samples or joystick_can_id is None:
        return {
            "recognizer": f"seating_{expected_function_name}_validation",
            "implemented": True,
            "status": "timeout",
            "summary": (
                f"No joystick samples found while validating {spec['display_name']}. "
                f"Selected-function block count={len(expected_blocks)}."
            ),
            "line_count": len(lines),
            "expected_function_code": expected_code,
            "expected_function_name": expected_function_name,
            "expected_function_display_name": spec["display_name"],
            "seating_blocks": summarize_seating_blocks(blocks),
            "expected_function_blocks": summarize_seating_blocks(expected_blocks),
            "joystick_source": joystick_source,
        }

    if known_joystick_center is not None:
        center_x, center_y = known_joystick_center
        center_source = "joystick_calibration"
    else:
        center_x, center_y = 0, 0
        center_source = "default_zero_fallback"

    phases = compress_joystick_motion_phases(
        joystick_samples,
        center_x=center_x,
        center_y=center_y,
        deadzone=deadzone,
        min_phase_samples=2,
    )

    annotated_movement_phases: list[dict[str, Any]] = []

    for phase_index, phase in enumerate(phases):
        if phase.get("kind") != "movement":
            continue

        block = latest_seating_block_before_or_during(
            blocks,
            phase["start_timestamp"],
        )
        selected_code = block.get("selected_function_code") if block else None
        selected_name = block.get("selected_function_name") if block else None
        motion_label = None

        if selected_code == expected_code:
            motion_label = seating_motion_label_for_state(
                phase.get("state"),
                expected_function_name=expected_function_name,
            )

        annotated_movement_phases.append(
            {
                "phase_index": phase_index,
                "kind": phase.get("kind"),
                "state": phase.get("state"),
                "axis": phase.get("axis"),
                "sign": phase.get("sign"),
                "signed_peak": phase.get("signed_peak"),
                "max_abs_dx": phase.get("max_abs_dx"),
                "max_abs_dy": phase.get("max_abs_dy"),
                "sample_count": phase.get("sample_count"),
                "duration_seconds": phase.get("duration_seconds"),
                "start_timestamp": phase.get("start_timestamp"),
                "end_timestamp": phase.get("end_timestamp"),
                "x_min": phase.get("x_min"),
                "x_max": phase.get("x_max"),
                "y_min": phase.get("y_min"),
                "y_max": phase.get("y_max"),
                "selected_function_code_at_start": selected_code,
                "selected_function_name_at_start": selected_name,
                "selected_function_block_start": block.get("start_timestamp") if block else None,
                "motion_label_for_expected_function": motion_label,
                "example_lines": phase.get("example_lines"),
            }
        )

    relevant = [
        phase for phase in annotated_movement_phases
        if phase.get("selected_function_code_at_start") == expected_code
        and max(phase.get("max_abs_dx") or 0, phase.get("max_abs_dy") or 0) >= min_peak
    ]

    expected_axis_states = {
        "more": spec["more_state"],
        "less": spec["less_state"],
    }

    sequence_match: dict[str, Any] | None = None

    for more_phase in relevant:
        if more_phase.get("state") != spec["more_state"]:
            continue

        for less_phase in relevant:
            if less_phase["phase_index"] <= more_phase["phase_index"]:
                continue
            if less_phase.get("state") != spec["less_state"]:
                continue

            center_between = any(
                phase.get("kind") == "center"
                for phase in phases[more_phase["phase_index"] + 1 : less_phase["phase_index"]]
            )

            sequence_match = {
                "more_phase": more_phase,
                "less_phase": less_phase,
                "center_between": center_between,
            }
            break

        if sequence_match is not None:
            break

    more_count = sum(1 for phase in relevant if phase.get("state") == spec["more_state"])
    less_count = sum(1 for phase in relevant if phase.get("state") == spec["less_state"])

    if expected_blocks and sequence_match and sequence_match.get("center_between"):
        status = "confirmed"
        summary = (
            f"Confirmed {spec['display_name']} validation: observed selected-function "
            f"code {expected_code}, then {spec['more_label']} "
            f"({spec['more_state']}), center, then {spec['less_label']} "
            f"({spec['less_state']})."
        )
    elif expected_blocks and sequence_match:
        status = "candidate"
        summary = (
            f"Found {spec['display_name']} selected-function code {expected_code} and "
            f"more -> less joystick polarity, but no clear center phase between them."
        )
    elif expected_blocks and (more_count or less_count):
        status = "candidate"
        summary = (
            f"Found {spec['display_name']} selected-function code {expected_code}; "
            f"observed {more_count} more-like and {less_count} less-like movement phase(s), "
            "but not a complete more -> center -> less sequence."
        )
    elif expected_blocks:
        status = "candidate"
        summary = (
            f"Found {spec['display_name']} selected-function code {expected_code}, "
            "but no strong joystick movement phase tied to that function."
        )
    elif sequence_match:
        status = "candidate"
        summary = (
            f"Observed joystick polarity consistent with {spec['display_name']}, "
            "but did not observe a selected-function seating block for that function."
        )
    else:
        status = "not_observed"
        summary = (
            f"Did not validate {spec['display_name']}: expected selected-function "
            f"code {expected_code}, more={spec['more_state']}, less={spec['less_state']}."
        )

    return {
        "recognizer": f"seating_{expected_function_name}_validation",
        "implemented": True,
        "status": status,
        "summary": summary,
        "line_count": len(lines),
        "expected_function_code": expected_code,
        "expected_function_name": expected_function_name,
        "expected_function_display_name": spec["display_name"],
        "expected_more_state": spec["more_state"],
        "expected_less_state": spec["less_state"],
        "expected_more_label": spec["more_label"],
        "expected_less_label": spec["less_label"],
        "deadzone": deadzone,
        "min_peak": min_peak,
        "joystick_can_id": f"0x{joystick_can_id:08X}",
        "joystick_source": joystick_source,
        "joystick_center": {
            "x": center_x,
            "y": center_y,
            "source": center_source,
        },
        "seating_block_count": len(blocks),
        "expected_function_block_count": len(expected_blocks),
        "seating_blocks": summarize_seating_blocks(blocks),
        "expected_function_blocks": summarize_seating_blocks(expected_blocks),
        "movement_phase_count": len(annotated_movement_phases),
        "relevant_movement_phase_count": len(relevant),
        "more_like_phase_count": more_count,
        "less_like_phase_count": less_count,
        "sequence_match": sequence_match,
        "movement_phases": annotated_movement_phases[:20],
        "phases": phases[:30],
        "safety_note": (
            "Passive validation only. These observations are not commands and should not "
            "be used as a transmit map for seating movement."
        ),
    }


def recognize_seating_tilt_validation(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_seating_option_validation(
        lines,
        expected_function_name="tilt",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_seating_elevate_validation(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_seating_option_validation(
        lines,
        expected_function_name="elevate",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_seating_recline_validation(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_seating_option_validation(
        lines,
        expected_function_name="recline",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )


def recognize_seating_legs_validation(
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return recognize_seating_option_validation(
        lines,
        expected_function_name="legs",
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )

def recognize_step(
    step_key: str,
    lines: list[str],
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """
    Dispatch step-specific recognition.

    Keep adding recognizers for different step types here.
    """

    if step_key == "baseline_idle":
        return recognize_baseline_idle(lines)

    if step_key == "horn":
        return recognize_horn_start_stop(lines)

    if step_key == "hazard":
        return recognize_hazard_lights(lines)

    if step_key == "left_indicator":
        return recognize_left_indicator(lines)

    if step_key == "right_indicator":
        return recognize_right_indicator(lines)

    if step_key == "flood_headlight":
        return recognize_flood_headlight(lines)

    if step_key == "joystick_calibration":
        return recognize_joystick_calibration(lines)

    if step_key == "joystick_forward":
        return recognize_joystick_forward(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "joystick_reverse":
        return recognize_joystick_reverse(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "joystick_left":
        return recognize_joystick_left(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "joystick_right":
        return recognize_joystick_right(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "profile_discovery":
        return recognize_profile_discovery(lines)

    if step_key == "seating_tilt_validation":
        return recognize_seating_tilt_validation(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "seating_elevate_validation":
        return recognize_seating_elevate_validation(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "seating_recline_validation":
        return recognize_seating_recline_validation(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    if step_key == "seating_legs_validation":
        return recognize_seating_legs_validation(
            lines,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    return {
        "recognizer": None,
        "implemented": False,
        "status": "not_implemented",
        "summary": f"No recognizer implemented yet for step '{step_key}'.",
        "line_count": len(lines),
    }


def print_recognition_summary(recognition: dict[str, Any]) -> None:
    print()
    print("Recognition:")
    print("  status:  %s" % recognition.get("status", "unknown"))
    print("  summary: %s" % recognition.get("summary", "No summary."))


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Turn a step name / label into a safe folder or filename component."""
    # Example:
    # slugify("Joystick Button: Left Indicator On/Off")
    # joystick_button_left_indicator_on_off
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_") or "unnamed"


def resolve_runtime_path(
    files_root: Path, path_value: str | Path | None
) -> Path | None:
    """
    Resolve user/runtime paths under the meet-greet files root.

    Rules:
      - None stays None.
      - Absolute paths are respected.
      - Relative paths are placed under files_root.
    """
    if path_value is None:
        return None

    path = Path(path_value)

    if path.is_absolute():
        return path

    return files_root / path


def ensure_log_root(root: str | Path) -> Path:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path


def ensure_step_log_dir(root: Path, step_name: str) -> Path:
    step_dir = root / slugify(step_name)
    step_dir.mkdir(parents=True, exist_ok=True)
    return step_dir


def build_custom_log_path(
    root: Path,
    *,
    title: str,
    label: str,
    suffix: str = ".log",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)

    ts = utc_timestamp_for_filename()
    safe_title = slugify(title)
    safe_label = slugify(label)

    return root / f"{ts}_{safe_title}_{safe_label}{suffix}"


def utc_timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_listening_session_path(
    root: Path,
    step_name: str,
    label: str,
    *,
    suffix: str = ".log",
) -> Path:
    step_dir = ensure_step_log_dir(root, step_name)
    ts = utc_timestamp_for_filename()
    safe_label = slugify(label)
    return step_dir / f"{ts}_{safe_label}{suffix}"


def load_saved_snippet_for_step(
    replay_root: Path,
    step_key: str,
    *,
    pick: str = "ask",
) -> tuple[list[str], Path | None]:
    """
    Load an existing saved log snippet for this step.

    Expected folder structure:
      replay_root/
        horn/
          20260711T164233Z_candidate.log
        left_indicator/
          20260711T164915Z_confirmed.log

    Returns:
      (lines, source_path)
    """
    step_dir = replay_root / slugify(step_key)

    if not step_dir.exists():
        print(f"No replay folder found for step: {step_key}")
        print(f"Expected: {step_dir}")
        return [], None

    candidates = sorted(step_dir.glob("*.log"))

    if not candidates:
        print(f"No .log snippets found for step: {step_key}")
        print(f"Folder: {step_dir}")
        return [], None

    if pick == "latest":
        chosen = max(candidates, key=lambda p: p.stat().st_mtime)
    elif pick == "first":
        chosen = candidates[0]
    else:
        print()
        print(f"Available saved snippets for step '{step_key}':")
        for i, path in enumerate(candidates, start=1):
            print(f"  {i}. {path.name}")

        while True:
            raw = input("Choose snippet number, or Enter for latest: ").strip()
            if not raw:
                chosen = max(candidates, key=lambda p: p.stat().st_mtime)
                break
            try:
                index = int(raw)
            except ValueError:
                print("Please enter a number.")
                continue
            if 1 <= index <= len(candidates):
                chosen = candidates[index - 1]
                break
            print(f"Please enter a number from 1 to {len(candidates)}.")

    raw_lines = chosen.read_text(encoding="utf-8").splitlines()

    # Keep only actual CAN-ish lines for future recognition logic.
    # This skips metadata headers like:
    #   # step: horn
    #   # label: candidate
    lines = [
        line for line in raw_lines if line.strip() and not line.lstrip().startswith("#")
    ]

    print(f"Loaded replay snippet: {chosen}")
    print(f"Loaded {len(lines)} CAN/log lines.")

    return lines, chosen


def write_listening_session(
    root: Path,
    *,
    step_name: str,
    label: str,
    lines: list[str],
    notes: str = "",
) -> Path:
    path = build_listening_session_path(root, step_name, label)

    with path.open("w", encoding="utf-8") as f:
        f.write(f"# step: {step_name}\n")
        f.write(f"# label: {label}\n")
        f.write(f"# captured_utc: {datetime.now(timezone.utc).isoformat()}\n")
        if notes:
            f.write(f"# notes: {notes}\n")
        f.write("\n")

        for line in lines:
            f.write(line.rstrip("\n") + "\n")

    return path


def write_custom_log_session(
    root: Path,
    *,
    title: str,
    label: str,
    lines: list[str],
    comments: str,
    interface: str,
    bustype: str,
    seconds: float,
    stop_reason: str,
) -> Path:
    path = build_custom_log_path(
        root,
        title=title,
        label=label,
    )

    with path.open("w", encoding="utf-8") as f:
        f.write("# custom_log: true\n")
        f.write(f"# title: {title}\n")
        f.write(f"# label: {label}\n")
        f.write(f"# captured_utc: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# interface: {interface}\n")
        f.write(f"# bustype: {bustype}\n")
        f.write(f"# listen_seconds: {seconds}\n")
        f.write(f"# stop_reason: {stop_reason}\n")
        f.write(f"# line_count: {len(lines)}\n")

        if comments:
            f.write("# comments:\n")
            for comment_line in comments.splitlines():
                f.write(f"#   {comment_line}\n")
        else:
            f.write("# comments: \n")

        f.write("\n")

        for line in lines:
            f.write(line.rstrip("\n") + "\n")

    return path


def collect_listening_lines_for_step(
    step_name: str,
    seconds: float,
    *,
    bus=None,
    interface: str = "can0",
    allow_enter_to_stop: bool = False,
    return_stop_reason: bool = False,
) -> list[str] | tuple[list[str], str]:
    """Collect live CAN frames for one meet-and-greet step.

    Returns candump-style text lines such as:
      (1783377282.654466) can0 02000200#0000

    The function only receives frames. It does not transmit anything.

    If allow_enter_to_stop is True, pressing Enter ends listening early.
    If return_stop_reason is True, returns (lines, stop_reason), where
    stop_reason is "timeout", "user_interrupted", or "no_bus".
    """
    if bus is None:
        print(f"[DRY/STUB] No CAN bus open for step: {step_name}")
        print("[DRY/STUB] Returning an empty listening window.")

        if return_stop_reason:
            return [], "no_bus"

        return []

    flush_info = flush_can_rx_queue(bus, max_seconds=0.5)
    flushed_count = flush_info.get("flushed_count", 0)
    if flushed_count:
        print(
            f"Flushed {flushed_count} queued frame(s) before capture "
            f"(max {0.5:.2f}s)."
        )

    lines: list[str] = []
    end_time = time.monotonic() + seconds
    stop_reason = "timeout"

    print(f"Listening on {interface} for {seconds:.1f}s for step: {step_name}")
    if allow_enter_to_stop:
        print("Press Enter to stop listening early.")

    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            stop_reason = "timeout"
            break

        if allow_enter_to_stop and stdin_enter_pressed():
            stop_reason = "user_interrupted"
            break

        msg = bus.recv(timeout=min(0.25, remaining))

        if msg is not None:
            lines.append(format_can_message(msg, interface=interface))

        if allow_enter_to_stop:
            sys.stdout.write(
                "\rListening... %4.1fs remaining, %d frame(s) captured; Enter=stop"
                % (max(0.0, remaining), len(lines))
            )
        else:
            sys.stdout.write(
                "\rListening... %4.1fs remaining, %d frame(s) captured"
                % (max(0.0, remaining), len(lines))
            )

        sys.stdout.flush()

    if stop_reason == "user_interrupted":
        sys.stdout.write(
            "\rListening... stopped early. %d frame(s) captured.          \n"
            % len(lines)
        )
    else:
        sys.stdout.write(
            "\rListening... done. %d frame(s) captured.          \n"
            % len(lines)
        )

    sys.stdout.flush()

    if return_stop_reason:
        return lines, stop_reason

    return lines


def format_can_message(msg, interface: str = "can0") -> str:
    """
    Format a python-can Message in candump-like style.

    Example:
      (1783377282.654466) can0 02000200#0000
    """
    arbitration_id = (
        f"{msg.arbitration_id:08X}"
        if msg.is_extended_id
        else f"{msg.arbitration_id:03X}"
    )
    data = msg.data.hex().upper()
    timestamp = getattr(msg, "timestamp", None)
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).timestamp()
    return f"({timestamp:.6f}) {interface} {arbitration_id}#{data}"


def ask_user_for_step_result() -> str:
    """Ask how to label the listening window."""
    choice = ask_choice(
        "Label this listening session as candidate, timeout, retry, skip, failed, or quit?",
        {"c", "t", "r", "s", "f", "q"},
        default="c",
    )

    if choice == "c":
        return "candidate"
    if choice == "t":
        return "timeout"
    if choice == "r":
        return "retry"
    if choice == "s":
        return "skipped"
    if choice == "f":
        return "failed"
    if choice == "q":
        return "quit"

    return "unclear"


def timestamp() -> str:
    return (
        time.strftime("%Y-%m-%d %H:%M:%S local time")
        + " ("
        + utc_timestamp_for_filename()
        + " UTC)"
    )


def ask_choice(prompt: str, choices: set[str], default: str | None = None) -> str:
    """Ask until the user enters a valid one-letter choice."""
    suffix = "/".join(sorted(choices))
    if default:
        suffix += f", Enter={default}"
    while True:
        raw = input(f"{prompt} [{suffix}]: ").strip().lower()
        if not raw and default:
            return default
        if raw in choices:
            return raw
        print("Please enter one of: %s" % ", ".join(sorted(choices)))


def stdin_enter_pressed() -> bool:
    """
    Return True if the user pressed Enter.

    Intended for Linux/RPi/Docker terminals. If stdin is not selectable,
    return False rather than breaking listening.
    """
    try:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return False

    if not readable:
        return False

    # Consume the Enter line so it does not affect the next input() prompt.
    sys.stdin.readline()
    return True


def ask_listen_seconds_for_retry(default_seconds: float) -> float:
    """Ask for a custom listening timeout before retrying a custom log."""
    while True:
        raw = input(
            f"Retry listen time in seconds [Enter={default_seconds:.1f}]: "
        ).strip()

        if not raw:
            return default_seconds

        try:
            seconds = float(raw)
        except ValueError:
            print("Please enter a number, like 10, 20, or 45.")
            continue

        if seconds <= 0:
            print("Please enter a positive number of seconds.")
            continue

        return seconds
    

def ask_multiline_comments() -> str:
    """
    Ask the user for freeform notes.

    Finish with a single dot on its own line.
    """
    print()
    print("Enter manual comments / metadata for this custom log.")
    print("Examples:")
    print("  - what action you performed")
    print("  - what buttons or programmer screens you used")
    print("  - timing notes, surprises, physical result")
    print("Finish with a single '.' on its own line.")
    print()

    lines: list[str] = []

    while True:
        raw = input("> ")
        if raw.strip() == ".":
            break
        lines.append(raw)

    return "\n".join(lines).strip()

def stdin_enter_pressed() -> bool:
    """
    Return True if the user pressed Enter.

    This is intended for Linux/macOS terminals. It should also work inside
    a normal Linux Docker/RPi shell. If stdin is not selectable, return False.
    """
    try:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return False

    if not readable:
        return False

    # Consume the line so it does not affect the next input() prompt.
    sys.stdin.readline()
    return True


def wait_countdown(seconds: float, *, label: str = "Listening") -> None:
    """Placeholder countdown. Future version will collect CAN frames here."""
    end_time = time.monotonic() + seconds
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        sys.stdout.write("\r%s... %4.1fs remaining" % (label, remaining))
        sys.stdout.flush()
        time.sleep(min(0.25, remaining))
    sys.stdout.write("\r%s... done.                 \n" % label)
    sys.stdout.flush()


def run_step(
    step: WizardStep,
    log_root: Path,
    listen_seconds: float | None,
    *,
    replay_root: Path | None = None,
    replay_pick: str = "ask",
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
    bus=None,
    interface: str = "can0",
) -> StepResult:
    if replay_root is not None:
        return run_replay_step(
            step,
            log_root,
            replay_root=replay_root,
            replay_pick=replay_pick,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
        )

    return run_live_step(
        step,
        log_root,
        listen_seconds,
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
        bus=bus,
        interface=interface,
    )


def run_live_step(
    step: WizardStep,
    log_root: Path,
    listen_seconds: float | None,
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
    bus=None,
    interface: str = "can0",
) -> StepResult:
    """Run one skippable wizard step with a timeout placeholder."""
    print("\n" + "=" * 78)
    print(step.title)
    print("=" * 78)
    if step.safety_note:
        print("Safety: %s" % step.safety_note)
    print(step.prompt)
    print()
    print("Options:")
    print("  r = ready / start listening")
    print("  s = skip this step")
    print("  q = quit wizard")

    choice = ask_choice("Choice", {"r", "s", "q"}, default="r")
    if choice == "q":
        raise KeyboardInterrupt
    if choice == "s":
        write_listening_session(
            log_root,
            step_name=step.key,
            label="skipped",
            lines=[],
            notes=f"User skipped this step.",
        )
        return StepResult(
            key=step.key,
            title=step.title,
            status="skipped",
            notes=["User skipped this step."],
        )

    source_path = None

    seconds = step.timeout_seconds if listen_seconds is None or listen_seconds <= 0 else listen_seconds

    lines = collect_listening_lines_for_step(
        step.key,
        seconds=seconds,
        bus=bus,
        interface=interface,
    )

    recognition = recognize_step(
        step.key,
        lines,
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )
    print_recognition_summary(recognition)

    label = ask_user_for_step_result()

    notes = f"title={step.title}; timeout_seconds={seconds}; source=live_can"

    snippet_path = write_listening_session(
        log_root,
        step_name=step.key,
        label=label,
        lines=lines,
        notes=notes,
    )

    print(f"Saved listening snippet: {snippet_path}")

    if label == "quit":
        raise KeyboardInterrupt
    if label == "retry":
        return run_live_step(
            step,
            log_root,
            listen_seconds,
            known_joystick_can_id=known_joystick_can_id,
            known_joystick_center=known_joystick_center,
            bus=bus,
            interface=interface,
        )
    if label == "skipped":
        return StepResult(
            key=step.key,
            title=step.title,
            status="skipped",
            notes=["User skipped after listening window."],
        )
    if label == "timeout":
        return StepResult(
            key=step.key,
            title=step.title,
            status="timeout",
            notes=["Listening window completed"],
        )
    if label == "failed":
        return StepResult(
            key=step.key,
            title=step.title,
            status="failed",
            notes=["User marked this listening window as failed."],
        )

    return StepResult(
        key=step.key,
        title=step.title,
        status="candidate",
        notes=["Recognition partially implemented."],
        observations={
            "timeout_seconds": step.timeout_seconds,
            "recognition": recognition,
        },
    )


def run_custom_log_mode(
    *,
    custom_root: Path,
    listen_seconds: float,
    bus=None,
    interface: str = "can0",
    bustype: str = "socketcan",
    preset_title: str | None = None,
) -> Path:
    print("\n" + "=" * 78)
    print("Custom CAN log grab")
    print("=" * 78)
    print("This mode captures raw CAN traffic only.")
    print("No recognizers will run and no profile step will be updated.")
    print()

    if preset_title is not None:
        title = preset_title
        print(f"Retrying custom test: {title}")
    else:
        while True:
            title = input("Title for this custom test: ").strip()
            if title:
                break
            print("Please enter a short title, e.g. programmer horn or charger plugged in.")

    print()
    print("Prepare the action you want to capture.")
    print("Options:")
    print("  r = ready / start listening")
    print("  q = quit")

    choice = ask_choice("Choice", {"r", "q"}, default="r")
    if choice == "q":
        raise KeyboardInterrupt

    lines, stop_reason = collect_listening_lines_for_step(
        f"custom:{title}",
        seconds=listen_seconds,
        bus=bus,
        interface=interface,
        allow_enter_to_stop=True,
        return_stop_reason=True,
    )

    print()
    print(f"Captured {len(lines)} line(s). Stop reason: {stop_reason}.")
    label = ask_user_for_step_result()

    if label == "quit":
        raise KeyboardInterrupt

    if label == "retry":
        retry_seconds = ask_listen_seconds_for_retry(listen_seconds)
        print(f"Retrying custom capture for {retry_seconds:.1f}s.")
        return run_custom_log_mode(
            custom_root=custom_root,
            listen_seconds=retry_seconds,
            bus=bus,
            interface=interface,
            bustype=bustype,
            preset_title=title,
        )

    comments = ask_multiline_comments()

    path = write_custom_log_session(
        custom_root,
        title=title,
        label=label,
        lines=lines,
        comments=comments,
        interface=interface,
        bustype=bustype,
        seconds=listen_seconds,
        stop_reason=stop_reason,
    )

    print(f"Saved custom log: {path}")
    return path


def run_replay_step(
    step: WizardStep,
    log_root: Path,
    replay_root: Path | None = None,
    replay_pick: str = "ask",
    *,
    known_joystick_can_id: int | None = None,
    known_joystick_center: tuple[int, int] | None = None,
) -> StepResult:
    """Run one step using an existing saved log snippet, not live chair actions."""
    print("\n" + "=" * 78)
    print(step.title)
    print("=" * 78)
    print("Replay mode: choose an existing saved snippet for this step.")
    print(f"Expected folder: {replay_root / slugify(step.key)}")
    print()

    lines, source_path = load_saved_snippet_for_step(
        replay_root,
        step.key,
        pick=replay_pick,
    )

    if source_path is None:
        label = "timeout"
        snippet_path = write_listening_session(
            log_root,
            step_name=step.key,
            label=label,
            lines=[],
            notes=f"title={step.title}; replay_source=missing; source=replay",
        )
        print(f"Saved empty replay result: {snippet_path}")
        return StepResult(
            key=step.key,
            title=step.title,
            status="timeout",
            notes=["Replay mode: no saved snippet was available for this step."],
            observations={
                "source": "replay",
                "replay_source": None,
                "line_count": 0,
                "recognition": None,
            },
        )

    recognition = recognize_step(
        step.key,
        lines,
        known_joystick_can_id=known_joystick_can_id,
        known_joystick_center=known_joystick_center,
    )
    print_recognition_summary(recognition)

    notes = f"title={step.title}; " f"replay_source={source_path}; " f"source=replay"

    label = "candidate"

    snippet_path = write_listening_session(
        log_root,
        step_name=step.key,
        label=label,
        lines=lines,
        notes=notes,
    )

    print(f"Saved replay-derived snippet: {snippet_path}")

    return StepResult(
        key=step.key,
        title=step.title,
        status=label,
        notes=[f"Replay mode: loaded saved snippet from {source_path}."],
        observations={
            "source": "replay",
            "replay_source": str(source_path),
            "line_count": len(lines),
            "recognition": recognition,
        },
    )


def build_default_steps() -> list[WizardStep]:
    """Define the main meet-and-greet path."""
    drive_safety = (
        "Use the slowest indoor profile, open space, and a spotter. Release the "
        "joystick immediately if anything feels wrong."
    )
    steps = [
        WizardStep(
            key="baseline_idle",
            title="1. Baseline: chair idle",
            prompt=(
                "Leave the joystick centered. Do not press any buttons. We will learn the "
                "normal idle bus chatter here."
            ),
            timeout_seconds=10.0,
        ),
        WizardStep(
            key="horn",
            title="2. Horn",
            prompt=(
                "When listening starts, honk the horn once, then release. We will look for horn start/stop candidates."
            ),
            timeout_seconds=6.0,
        ),
        WizardStep(
            key="left_indicator",
            title="3. Left indicator",
            prompt=(
                "When listening starts, manually toggle the LEFT indicator on, pause, "
                "then toggle it off."
            ),
            timeout_seconds=8.0,
        ),
        WizardStep(
            key="right_indicator",
            title="4. Right indicator",
            prompt=(
                "When listening starts, manually toggle the RIGHT indicator on, pause, "
                "then toggle it off."
            ),
            timeout_seconds=8.0,
        ),
        WizardStep(
            key="hazard",
            title="5. Hazard lights",
            prompt=(
                "When listening starts, manually toggle HAZARDS on, pause, then toggle "
                "them off."
            ),
            timeout_seconds=8.0,
        ),
        WizardStep(
            key="flood_headlight",
            title="6. Flood / headlight",
            prompt=(
                "When listening starts, manually toggle FLOOD/HEADLIGHT on, pause, "
                "then toggle it off."
            ),
            timeout_seconds=8.0,
        ),
        WizardStep(
            key="profile_discovery",
            title="7. Drive profile discovery",
            prompt=(
                "Start in a known drive profile, preferably the lowest/indoor profile. "
                "When listening starts, repeatedly press the chair's normal profile/mode "
                "button to cycle through every enabled drive profile, then continue until "
                "the starting profile appears again. Pause about one second on each profile. "
                "In comments, record the user-facing profile numbers/names if known; "
                "the recognizer will map wire indices 0-7 to profile numbers 1-8. "
                "If the button enters seating or another non-drive mode, note that in the "
                "comments and return to drive mode using the normal controls."
            ),
            timeout_seconds=18.0,
            safety_note=(
                "Passive recognition only. Do not move the joystick during this step. "
                "Profile names and limits are chair/config specific."
            ),
        ),
        WizardStep(
            key="joystick_calibration",
            title="8. Joystick ID / center / axes",
            prompt=(
                "When listening starts, move the joystick through this pattern slowly:\n"
                "  1. center / hands off\n"
                "  2. forward\n"
                "  3. center\n"
                "  4. reverse\n"
                "  5. center\n"
                "  6. left\n"
                "  7. center\n"
                "  8. right\n"
                "  9. center\n\n"
                "Hold each position briefly and return to center between directions. "
                "The recognizer will use this to infer joystick ID, center, axes, and polarity."
            ),
            timeout_seconds=18.0,
        ),
        WizardStep(
            key="joystick_forward",
            title="9. Joystick forward range",
            prompt=(
                "Prepare a clear, safe path forward. "
                "When listening starts, gently push the joystick forward, hold briefly, "
                "then release back to center."
            ),
            timeout_seconds=8.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="joystick_reverse",
            title="10. Joystick reverse range",
            prompt=(
                "Prepare a clear, safe path behind the chair. "
                "When listening starts, gently pull the joystick backward/reverse, hold briefly, "
                "then release back to center."
            ),
            timeout_seconds=8.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="joystick_left",
            title="11. Joystick left range",
            prompt=(
                "Prepare a clear, safe area for a left turn or left joystick push. "
                "When listening starts, gently push the joystick left, hold briefly, "
                "then release back to center."
            ),
            timeout_seconds=8.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="joystick_right",
            title="12. Joystick right range",
            prompt=(
                "Prepare a clear, safe area for a right turn or right joystick push. "
                "When listening starts, gently push the joystick right, hold briefly, "
                "then release back to center."
            ),
            timeout_seconds=8.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="seating_tilt_validation",
            title="13. Seating validation: tilt",
            prompt=(
                "Using the chair's normal controls, get into TILT seating mode. "
                "When listening starts: tilt more for a bit, return the joystick to center, "
                "tilt less for a bit, then return to center."
            ),
            timeout_seconds=16.0,
            safety_note=(
                "Passive validation only. Make sure the chair has clearance and nothing "
                "can pinch, collide, or destabilize while seating moves."
            ),
        ),
        WizardStep(
            key="seating_elevate_validation",
            title="14. Seating validation: elevate",
            prompt=(
                "Using the chair's normal controls, get into ELEVATE seating mode. "
                "When listening starts: elevate more/raise for a bit, return the joystick "
                "to center, elevate less/lower for a bit, then return to center."
            ),
            timeout_seconds=16.0,
            safety_note=(
                "Passive validation only. Make sure the chair has overhead/under-chair "
                "clearance and is on level ground."
            ),
        ),
        WizardStep(
            key="seating_recline_validation",
            title="15. Seating validation: recline / backrest",
            prompt=(
                "Using the chair's normal controls, get into RECLINE/BACKREST seating mode. "
                "When listening starts: recline more for a bit, return the joystick to center, "
                "recline less for a bit, then return to center."
            ),
            timeout_seconds=16.0,
            safety_note=(
                "Passive validation only. Make sure the chair has rear clearance and the "
                "user is safely positioned."
            ),
        ),
        WizardStep(
            key="seating_legs_validation",
            title="16. Seating validation: legs",
            prompt=(
                "Using the chair's normal controls, get into LEGS seating mode. "
                "When listening starts: raise/extend legs for a bit, return the joystick "
                "to center, lower/retract legs for a bit, then return to center."
            ),
            timeout_seconds=16.0,
            safety_note=(
                "Passive validation only. Make sure the footplates/legrests have clearance "
                "and cannot hit people, walls, furniture, or the ground."
            ),
        ),
    ]

    return steps


def build_profile(args: argparse.Namespace) -> MeetGreetProfile:
    return MeetGreetProfile(
        profile_name=args.profile_name,
        created_at=timestamp(),
        interface=args.interface,
        bustype=args.bustype,
        passive_only=True,
        safety_notes=[
            "Passive listener: receives CAN frames only and does not transmit.",
            "Do not add transmit behavior unless a future explicit confirm mode is added.",
            "Do not test drive movement without open space and a spotter.",
        ],
    )


def save_json_profile(profile: MeetGreetProfile, output_path: Path) -> None:
    """Write current placeholder profile as JSON."""
    serializable = asdict(profile)
    output_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")


def default_summary_output_path(output_path: Path) -> Path:
    """Derive the human-readable summary path from the JSON output path."""
    if output_path.suffix.lower() == ".json":
        return output_path.with_name(f"{output_path.stem}_summary.md")
    return output_path.with_name(f"{output_path.name}_summary.md")


def plain_step_dict(profile_dict: dict[str, Any], key: str) -> dict[str, Any]:
    steps = profile_dict.get("steps") or {}
    step = steps.get(key) or {}
    return step if isinstance(step, dict) else {}


def step_recognition(profile_dict: dict[str, Any], key: str) -> dict[str, Any]:
    step = plain_step_dict(profile_dict, key)
    observations = step.get("observations") or {}
    recognition = observations.get("recognition") or {}
    return recognition if isinstance(recognition, dict) else {}


def md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    text = text.replace("|", "\\|")
    text = text.replace("\n", "<br>")
    return text


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_None found yet._\n"

    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        padded = row + [""] * (len(headers) - len(row))
        out.append("| " + " | ".join(md_cell(value) for value in padded[: len(headers)]) + " |")
    return "\n".join(out) + "\n"


def format_optional_can_id(value: Any) -> str:
    parsed = parse_can_id_value(value)
    if parsed is not None:
        return f"0x{parsed:08X}"
    if value in (None, ""):
        return ""
    return str(value)


def format_range(direction_range: dict[str, Any] | None) -> str:
    if not direction_range:
        return ""
    axis = direction_range.get("axis")
    sign = direction_range.get("sign")
    peak = direction_range.get("signed_peak_from_center")
    abs_peak = direction_range.get("primary_abs_peak")
    delta_min = direction_range.get("primary_delta_min")
    delta_max = direction_range.get("primary_delta_max")
    off_axis = direction_range.get("off_axis")
    off_axis_peak = direction_range.get("off_axis_abs_peak")

    parts = []
    if axis is not None:
        parts.append(f"axis={axis}")
    if sign is not None:
        parts.append(f"sign={sign}")
    if peak is not None:
        parts.append(f"peak={peak}")
    if abs_peak is not None:
        parts.append(f"abs_peak={abs_peak}")
    if delta_min is not None or delta_max is not None:
        parts.append(f"range={delta_min}..{delta_max}")
    if off_axis is not None and off_axis_peak is not None:
        parts.append(f"off_axis_{off_axis}_peak={off_axis_peak}")
    return ", ".join(parts)


def collect_confirmed_ids(profile_dict: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    confirmed = profile_dict.get("confirmed") or {}

    joystick_id = confirmed.get("joystick_can_id") or confirmed.get("joystick_can_id_int")
    if joystick_id is not None:
        center = confirmed.get("joystick_center") or {}
        rows.append([
            "Drive joystick command",
            format_optional_can_id(joystick_id),
            "confirmed",
            f"center X={center.get('x', '')}, Y={center.get('y', '')}",
        ])

    observed_profile_numbers = confirmed.get("observed_profile_numbers") or []
    observed_profile_indices = confirmed.get("observed_profile_indices") or []
    if observed_profile_numbers or observed_profile_indices:
        rows.append([
            "Drive profile control",
            confirmed.get("profile_control_family", "0x050|node"),
            "confirmed",
            (
                f"observed profile numbers={observed_profile_numbers}; "
                f"wire indices={observed_profile_indices}; "
                f"pattern={confirmed.get('profile_request_pattern', '00 PP 00 00')}"
            ),
        ])

    horn = step_recognition(profile_dict, "horn")
    if horn:
        joystick_ev = horn.get("joystick_evidence") or {}
        programmer_ev = horn.get("programmer_evidence") or {}
        if joystick_ev.get("pair_count", 0) or joystick_ev.get("start_count", 0) or joystick_ev.get("stop_count", 0):
            rows.append([
                "Horn start/stop, joystick",
                f"{joystick_ev.get('start_id', '')} -> {joystick_ev.get('stop_id', '')}",
                horn.get("status"),
                f"pairs={joystick_ev.get('pair_count', 0)}, starts={joystick_ev.get('start_count', 0)}, stops={joystick_ev.get('stop_count', 0)}",
            ])
        if programmer_ev.get("pair_count", 0) or programmer_ev.get("start_count", 0) or programmer_ev.get("stop_count", 0):
            rows.append([
                "Horn start/stop, programmer",
                f"{programmer_ev.get('start_id', '')} -> {programmer_ev.get('stop_id', '')}",
                horn.get("status"),
                f"pairs={programmer_ev.get('pair_count', 0)}, starts={programmer_ev.get('start_count', 0)}, stops={programmer_ev.get('stop_count', 0)}",
            ])

    light_steps = [
        ("left_indicator", "Left indicator"),
        ("right_indicator", "Right indicator"),
        ("hazard", "Hazard lights"),
        ("flood_headlight", "Flood/headlight"),
    ]
    for step_key, label in light_steps:
        recog = step_recognition(profile_dict, step_key)
        if not recog:
            continue
        physical_id = recog.get("expected_physical_toggle_id")
        physical_count = recog.get("physical_toggle_count", 0)
        status_bit = recog.get("status_bit") or recog.get("hazard_status_bit")
        status_count = recog.get("status_event_count", 0)
        programmer_count = recog.get("programmer_toggle_count", 0)
        if physical_id or physical_count or status_count or programmer_count:
            rows.append([
                label,
                physical_id,
                recog.get("status"),
                f"physical_toggles={physical_count}, programmer={programmer_count}, status_bit={status_bit}, status_events={status_count}",
            ])

    return rows


def collect_joystick_range_rows(profile_dict: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    confirmed = profile_dict.get("confirmed") or {}
    mapping = confirmed.get("joystick_mapping") or {}

    for direction in ["forward", "reverse", "left", "right"]:
        info = mapping.get(direction) or {}
        phase = info.get("phase") or {}
        if info:
            rows.append([
                f"{direction} (calibration)",
                confirmed.get("joystick_can_id"),
                info.get("axis"),
                info.get("sign"),
                phase.get("signed_peak"),
                max(phase.get("max_abs_dx", 0) or 0, phase.get("max_abs_dy", 0) or 0),
                f"X {phase.get('x_min', '')}..{phase.get('x_max', '')}; Y {phase.get('y_min', '')}..{phase.get('y_max', '')}",
            ])

    for direction in ["forward", "reverse", "left", "right"]:
        step_key = f"joystick_{direction}"
        recog = step_recognition(profile_dict, step_key)
        best = recog.get("best_candidate") or {}
        direction_range = best.get("direction_range") or {}
        if not direction_range:
            continue
        rows.append([
            f"{direction} (range test)",
            best.get("can_id"),
            direction_range.get("axis"),
            direction_range.get("sign"),
            direction_range.get("signed_peak_from_center"),
            direction_range.get("primary_abs_peak"),
            format_range(direction_range),
        ])

    return rows


def collect_drive_response_rows(profile_dict: dict[str, Any], limit_per_step: int = 3) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for step_key in [
        "joystick_calibration",
        "joystick_forward",
        "joystick_reverse",
        "joystick_left",
        "joystick_right",
    ]:
        recog = step_recognition(profile_dict, step_key)
        best = recog.get("best_candidate") or {}
        drive_response = best.get("drive_response_candidates") or {}
        for candidate in (drive_response.get("ranked_candidates") or [])[:limit_per_step]:
            movement_summary = candidate.get("movement_summary") or {}
            common_values = movement_summary.get("most_common_values") or []
            common_text = ", ".join(
                f"{item.get('data_hex')} x{item.get('count')}"
                for item in common_values[:3]
            )
            rows.append([
                step_key,
                candidate.get("can_id"),
                candidate.get("score"),
                candidate.get("movement_changed_fraction"),
                candidate.get("movement_frame_count"),
                common_text,
            ])
    return rows


def collect_step_summary_rows(profile_dict: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for key, step in (profile_dict.get("steps") or {}).items():
        observations = step.get("observations") or {}
        recognition = observations.get("recognition") or {}
        rows.append([
            key,
            step.get("status"),
            step.get("title"),
            recognition.get("summary") or "; ".join(step.get("notes") or []),
        ])
    return rows


def write_human_summary(profile: MeetGreetProfile, summary_path: Path) -> None:
    """Write a human-readable Markdown summary next to the JSON profile."""
    profile_dict = asdict(profile)
    confirmed = profile_dict.get("confirmed") or {}

    lines: list[str] = []
    lines.append(f"# R-Net meet-and-greet summary: {profile.profile_name}")
    lines.append("")
    lines.append("This is a human-readable companion to the JSON profile. It is meant for quick chair-to-chair comparison, not as a complete raw-data archive.")
    lines.append("")
    lines.append("## Session")
    lines.append("")
    lines.append(md_table(
        ["Field", "Value"],
        [
            ["Profile name", profile.profile_name],
            ["Created", profile.created_at],
            ["Interface", profile.interface],
            ["python-can interface", profile.bustype],
            ["Passive only", profile.passive_only],
            ["Confirmed joystick ID", format_optional_can_id(confirmed.get("joystick_can_id") or confirmed.get("joystick_can_id_int"))],
            ["Joystick center", confirmed.get("joystick_center")],
        ],
    ))

    lines.append("## At-a-glance IDs")
    lines.append("")
    lines.append(md_table(
        ["Item", "CAN ID / IDs", "Status", "Evidence"],
        collect_confirmed_ids(profile_dict),
    ))

    baseline = step_recognition(profile_dict, "baseline")
    if baseline:
        lines.append("## Baseline bus traffic")
        lines.append("")
        lines.append(baseline.get("summary", ""))
        lines.append("")
        top_rows = []
        for item in baseline.get("top_ids") or []:
            top_rows.append([
                item.get("can_id"),
                item.get("count"),
                item.get("approx_rate_hz"),
            ])
        lines.append(md_table(["CAN ID", "Count", "Approx Hz"], top_rows))

        idle = baseline.get("joystick_idle_inference") or {}
        idle_rows = []
        for candidate in (idle.get("ranked_candidates") or [])[:6]:
            idle_rows.append([
                candidate.get("can_id"),
                candidate.get("score"),
                candidate.get("rate_hz"),
                candidate.get("zero_fraction"),
                candidate.get("rnet_joystick_family"),
            ])
        if idle_rows:
            lines.append("### Joystick idle candidates")
            lines.append("")
            lines.append(md_table(
                ["CAN ID", "Score", "Rate Hz", "Zero fraction", "R-Net joystick family"],
                idle_rows,
            ))

    lines.append("## Joystick mapping and ranges")
    lines.append("")
    lines.append(md_table(
        ["Direction", "CAN ID", "Axis", "Sign", "Signed peak", "Abs peak", "Range notes"],
        collect_joystick_range_rows(profile_dict),
    ))

    lines.append("## Drive-response candidates")
    lines.append("")
    lines.append("These are non-joystick IDs that changed during joystick movement windows. Treat them as candidates, not confirmed control IDs.")
    lines.append("")
    lines.append(md_table(
        ["Source step", "CAN ID", "Score", "Changed fraction", "Movement frames", "Common movement values"],
        collect_drive_response_rows(profile_dict),
    ))

    lines.append("## Step-by-step recognizer summaries")
    lines.append("")
    lines.append(md_table(
        ["Step", "Status", "Title", "Summary"],
        collect_step_summary_rows(profile_dict),
    ))

    if profile.safety_notes:
        lines.append("## Safety notes")
        lines.append("")
        for note in profile.safety_notes:
            lines.append(f"- {note}")
        lines.append("")

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def save_profile_outputs(
    profile: MeetGreetProfile,
    output_path: Path,
    summary_path: Path | None,
) -> None:
    """Write JSON plus the optional human-readable summary."""
    save_json_profile(profile, output_path)
    if summary_path is not None:
        write_human_summary(profile, summary_path)


def print_summary(profile: MeetGreetProfile) -> None:
    print("\n" + "=" * 78)
    print("Meet & greet placeholder summary")
    print("=" * 78)
    print("Profile:   %s" % profile.profile_name)
    print("Interface: %s" % profile.interface)
    print("Bustype:   %s" % profile.bustype)
    print("Passive:   %s" % profile.passive_only)
    print()
    for key, result in profile.steps.items():
        print("%-22s %-10s %s" % (key, result.status, result.title))
    print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Skeleton R-Net meet-and-greet discovery wizard."
    )
    p.add_argument("--interface", default="can0")
    p.add_argument("--bustype", default="socketcan")
    p.add_argument("--profile-name", default="unnamed-chair")
    p.add_argument(
        "--output",
        default="rnet_meet_greet_profile.json",
        help="path for placeholder JSON profile output",
    )
    p.add_argument(
        "--summary-output",
        default=None,
        help=(
            "Path for human-readable Markdown summary output. "
            "By default, this is derived from --output, e.g. "
            "rnet_meet_greet_profile_summary.md"
        ),
    )
    p.add_argument(
        "--log-snippet-root",
        default=LOG_SNIPPET_ROOT_DEFAULT,
        help=f"Directory where per-step listening snippets are written "
        f"(default: {LOG_SNIPPET_ROOT_DEFAULT})",
    )
    p.add_argument(
        "--listen-seconds",
        type=float,
        default=None,
        help=(
            "Override seconds to listen for every interactive step. "
            "By default, each step uses its own timeout."
        ),
    )
    p.add_argument(
        "--replay-log-root",
        default=None,
        help=(
            "Read existing per-step log snippets instead of listening to live CAN. "
            "Expected structure: ROOT/step_key/*.log"
        ),
    )
    p.add_argument(
        "--replay-pick",
        choices={"ask", "latest", "first"},
        default="ask",
        help="How to choose a snippet when replaying saved logs (default: ask)",
    )
    p.add_argument(
        "--files-root",
        default=MEET_GREET_FILES_ROOT_DEFAULT,
        help=(
            "Parent directory for generated meet-and-greet files "
            f"(default: {MEET_GREET_FILES_ROOT_DEFAULT})"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the interactive flow without opening CAN. Live steps return empty "
            "captures. Replay mode does not need this."
        ),
    )
    p.add_argument(
        "--custom-log",
        action="store_true",
        help=(
            "Run one freeform custom CAN log capture instead of the standard "
            "meet-and-greet step sequence. No recognizers are run."
        ),
    )
    p.add_argument(
        "--custom-log-root",
        default=CUSTOM_LOG_ROOT_DEFAULT,
        help=(
            "Directory for freeform custom logs, relative to --files-root unless absolute "
            f"(default: {CUSTOM_LOG_ROOT_DEFAULT})"
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    files_root = Path(args.files_root)
    files_root.mkdir(parents=True, exist_ok=True)
    output_path = resolve_runtime_path(files_root, args.output)
    if args.summary_output is None:
        summary_output_path = default_summary_output_path(output_path)
    else:
        summary_output_path = resolve_runtime_path(files_root, args.summary_output)
    log_root = ensure_log_root(resolve_runtime_path(files_root, args.log_snippet_root))
    replay_root = resolve_runtime_path(files_root, args.replay_log_root)
    resolved_log_root = resolve_runtime_path(files_root, args.log_snippet_root)
    assert resolved_log_root is not None
    log_root = ensure_log_root(resolved_log_root)

    profile = build_profile(args)
    steps = build_default_steps()

    print("R-Net Meet & Greet")
    print("Passive listener: receives CAN frames only; does not transmit.")
    print("Files root: %s" % files_root)
    print("Output profile: %s" % output_path)
    print("Human summary: %s" % summary_output_path)
    print("Log snippets: %s" % log_root)
    print()

    bus = None
    if replay_root is not None:
        print(f"Replay mode: reading snippets from {replay_root}")
    elif args.dry_run:
        print("Dry-run live mode: no CAN bus will be opened; captures will be empty.")
    else:
        print(f"Live mode: opening {args.interface} using python-can interface={args.bustype}")
        try:
            bus = open_can_bus(args.interface, args.bustype)
        except (OSError, RuntimeError) as exc:
            sys.stderr.write(f"Could not open {args.interface}: {exc}\n")
            sys.stderr.write(
                "Use --replay-log-root for replay mode, or --dry-run to exercise "
                "the flow without CAN.\n"
            )
            return 1

    if args.custom_log:
        if replay_root is not None:
            sys.stderr.write("--custom-log is for live/dry-run captures, not replay mode.\n")
            return 1

        custom_root = resolve_runtime_path(files_root, args.custom_log_root)
        assert custom_root is not None

        seconds = (
            args.listen_seconds
            if args.listen_seconds is not None and args.listen_seconds > 0
            else LISTEN_SECONDS_DEFAULT
        )

        try:
            while True:
                run_custom_log_mode(
                    custom_root=custom_root,
                    listen_seconds=seconds,
                    bus=bus,
                    interface=args.interface,
                    bustype=args.bustype,
                )

                again = ask_choice(
                    "Capture another custom log?",
                    {"y", "n"},
                    default="n",
                )
                if again != "y":
                    break
        except KeyboardInterrupt:
            print("\nCustom log capture interrupted.")
            return 130
        finally:
            close_can_bus(bus)

        return 0
    
    # Run the main wizard steps if not in custom log mode
    try:
        for step in steps:
            known_joystick_can_id = get_confirmed_joystick_can_id(profile)
            known_joystick_center = get_confirmed_joystick_center(profile)

            result = run_step(
                step,
                log_root,
                args.listen_seconds,
                replay_root=replay_root,
                replay_pick=args.replay_pick,
                known_joystick_can_id=known_joystick_can_id,
                known_joystick_center=known_joystick_center,
                bus=bus,
                interface=args.interface,
            )

            profile.steps[result.key] = result
            update_profile_from_step_result(profile, result)

            save_profile_outputs(profile, output_path, summary_output_path)
    except KeyboardInterrupt:
        print("\nWizard interrupted. Saving partial profile...")
        save_profile_outputs(profile, output_path, summary_output_path)
        print_summary(profile)
        return 130
    finally:
        close_can_bus(bus)

    save_profile_outputs(profile, output_path, summary_output_path)
    print_summary(profile)
    print("Saved profile to: %s" % output_path)
    print("Saved human summary to: %s" % summary_output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
