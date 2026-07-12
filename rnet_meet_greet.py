#!/usr/bin/env python3
"""
rnet_meet_greet_skeleton.py - Passive R-Net chair capability discovery wizard.

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
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal


StepStatus = Literal["not_run", "skipped", "timeout", "candidate", "confirmed", "failed"]

LOG_SNIPPET_ROOT_DEFAULT = "meet_greet_log_snippets"
LISTEN_SECONDS_DEFAULT = 10.0
MEET_GREET_FILES_ROOT_DEFAULT = "meet_greet_files"

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
PROGRAMMER_HAZARD_TOGGLE_ID = 0x0C000F03
PROGRAMMER_LEFT_INDICATOR_TOGGLE_ID = 0x0C000F01
PROGRAMMER_RIGHT_INDICATOR_TOGGLE_ID = 0x0C000F02
PROGRAMMER_FLOOD_HEADLIGHT_TOGGLE_ID = 0x0C000F04

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

# -----------------------------------------------------------------------------
# Future expectation / recognition stubs
# -----------------------------------------------------------------------------

# def open_can_bus(interface: str, bustype: str):
#     """Future: open python-can bus in receive-only/passive mode."""
#     pass

# def close_can_bus(bus) -> None:
#     """Future: cleanly close CAN bus."""
#     pass

# def collect_baseline(bus, duration_seconds: float) -> list:
#     """Future: collect idle frames for comparison."""
#     pass

# def collect_action_window(bus, duration_seconds: float) -> list:
#     """Future: collect frames while the user performs the requested action."""
#     pass

# def rank_candidate_frames(baseline_frames: list, action_frames: list) -> list:
#     """Future: find frames that appear/change during the action but not baseline."""
#     pass

# def recognize_joystick_axis(action_name: str, baseline_frames: list, action_frames: list) -> dict:
#     """Future: infer joystick ID, byte positions, signedness, polarity, min/max."""
#     pass

# def recognize_motor_current(baseline_frames: list, action_frames: list) -> dict:
#     """Future: infer motor-current or motion-state frame candidates."""
#     pass

# def confirm_candidate_by_repetition(candidate: dict, repeated_action_frames: list) -> bool:
#     """Future: confirm that a candidate repeats on a second/third trial."""
#     pass

# def write_yaml_profile(profile: MeetGreetProfile, path: Path) -> None:
#     """Future: write YAML profile once PyYAML or manual serializer is chosen."""
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
                    round(median_interval, 6)
                    if median_interval is not None
                    else None
                ),
                "min_interval_seconds": (
                    round(min_interval, 6)
                    if min_interval is not None
                    else None
                ),
                "max_interval_seconds": (
                    round(max_interval, 6)
                    if max_interval is not None
                    else None
                ),
                "example_lines": [
                    frame["raw"]
                    for frame in frames[:5]
                ],
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
    Recognize a simple R-Net horn start/stop pattern.

    Known-good candidate from prior testing:
      horn start: 0C040100#
      horn stop:  0C040101#

    This recognizer is intentionally conservative:
      - It does not transmit.
      - It does not assume the horn worked physically.
      - It only reports whether the expected start/stop frames appear.
    """
    parsed_frames: list[dict[str, Any]] = []

    for line in lines:
        frame = parse_can_log_line(line)
        if frame is not None:
            parsed_frames.append(frame)

    start_events = [
        frame for frame in parsed_frames
        if frame["can_id"] == HORN_START_ID
    ]

    stop_events = [
        frame for frame in parsed_frames
        if frame["can_id"] == HORN_STOP_ID
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

    if pairs:
        recognition_status = "confirmed"
        summary = (
            f"Found {len(pairs)} horn start/stop pair(s): "
            f"0x{HORN_START_ID:08X} -> 0x{HORN_STOP_ID:08X}."
        )
    elif start_events or stop_events:
        recognition_status = "candidate"
        summary = (
            "Found partial horn evidence: "
            f"{len(start_events)} start frame(s), "
            f"{len(stop_events)} stop frame(s)."
        )
    else:
        recognition_status = "not_observed"
        summary = "No horn start/stop frames observed."

    return {
        "recognizer": "horn_start_stop",
        "implemented": True,
        "status": recognition_status,
        "summary": summary,
        "expected_start_id": f"0x{HORN_START_ID:08X}",
        "expected_stop_id": f"0x{HORN_STOP_ID:08X}",
        "line_count": len(lines),
        "parsed_frame_count": len(parsed_frames),
        "start_count": len(start_events),
        "stop_count": len(stop_events),
        "pair_count": len(pairs),
        "pairs": pairs,
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
        frame for frame in parsed_frames
        if frame["can_id"] == HAZARD_TOGGLE_ID
    ]

    programmer_toggle_events = [
        frame for frame in parsed_frames
        if frame["can_id"] == PROGRAMMER_HAZARD_TOGGLE_ID
    ] if "PROGRAMMER_HAZARD_TOGGLE_ID" in globals() else []

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
        frame for frame in parsed_frames
        if frame["can_id"] == physical_toggle_id
    ]

    if programmer_toggle_id is not None:
        programmer_toggle_events = [
            frame for frame in parsed_frames
            if frame["can_id"] == programmer_toggle_id
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
        frame for frame in parsed_frames
        if frame["can_id"] == FLOOD_HEADLIGHT_TOGGLE_ID
    ]

    programmer_toggle_events = [
        frame for frame in parsed_frames
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
                    round(count / duration_seconds, 3)
                    if duration_seconds > 0
                    else None
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

def recognize_step(step_key: str, lines: list[str]) -> dict[str, Any]:
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

def resolve_runtime_path(files_root: Path, path_value: str | Path | None) -> Path | None:
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
        line
        for line in raw_lines
        if line.strip() and not line.lstrip().startswith("#")
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

def collect_listening_lines_for_step(step_name: str, seconds: float) -> list[str]:
    """
    TODO: Replace this with real CAN collection.

    Future behavior:
      - open python-can bus or use existing bus
      - collect frames for `seconds`
      - format each as candump-style text:
          (timestamp) can0 ID#DATA
      - return list[str]
    """
    print(f"[TODO] Listening for {seconds:.1f}s for step: {step_name}")
    print("[TODO] Real CAN collection will go here later.")
    return []


def format_can_message(msg, interface: str = "can0") -> str:
    """
    Format a python-can Message in candump-like style.

    Example:
      (1783377282.654466) can0 02000200#0000
    """
    arbitration_id = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
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
    return time.strftime("%Y-%m-%d %H:%M:%S local time") + " (" + utc_timestamp_for_filename() + " UTC)"


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
    listen_seconds: float,
    *,
    replay_root: Path | None = None,
    replay_pick: str = "ask",
) -> StepResult:
    if replay_root is not None:
        return run_replay_step(
            step,
            log_root,
            replay_root=replay_root,
            replay_pick=replay_pick,
        )

    return run_live_step(
        step,
        log_root,
        listen_seconds,
    )

def run_live_step(
    step: WizardStep,
    log_root: Path,
    listen_seconds: float,
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


    lines = collect_listening_lines_for_step(
        step.key,
        seconds=listen_seconds,
    )

    recognition = recognize_step(step.key, lines)
    print_recognition_summary(recognition)
    
    label = ask_user_for_step_result()

    notes = f"title={step.title}; timeout_seconds={step.timeout_seconds}; source=live_or_stub"

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
            listen_seconds
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
            notes=["Listening window completed; no recognition logic implemented yet."],
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

def run_replay_step(
    step: WizardStep,
    log_root: Path,
    replay_root: Path | None = None,
    replay_pick: str = "ask",
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
                "recognition": recognition,
            },
        )

    recognition = recognize_step(step.key, lines)
    print_recognition_summary(recognition)
    
    notes = (
        f"title={step.title}; "
        f"replay_source={source_path}; "
        f"source=replay"
    )

    label="candidate"

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
                "Leave the joystick centered. Do not press buttons. The future version "
                "will learn the normal idle bus chatter here."
            ),
            timeout_seconds=10.0,
        ),
        WizardStep(
            key="horn",
            title="2. Horn",
            prompt=(
                "When listening starts, honk the horn once, then release. The future "
                "version will look for horn start/stop candidates."
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
            key="joystick_center",
            title="7. Joystick center",
            prompt=(
                "Keep the joystick centered. The future version will identify center "
                "values and candidate joystick frames."
            ),
            timeout_seconds=5.0,
        ),
        WizardStep(
            key="joystick_forward",
            title="8. Joystick forward",
            prompt=(
                "When listening starts, gently hold the joystick forward until "
                "the countdown ends, then release."
            ),
            timeout_seconds=5.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="joystick_reverse",
            title="9. Joystick reverse",
            prompt=(
                "When listening starts, gently hold the joystick backward until "
                "the countdown ends, then release."
            ),
            timeout_seconds=5.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="joystick_left",
            title="10. Joystick left",
            prompt=(
                "When listening starts, gently hold the joystick left until "
                "the countdown ends, then release."
            ),
            timeout_seconds=5.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="joystick_right",
            title="11. Joystick right",
            prompt=(
                "When listening starts, gently hold the joystick right until "
                "the countdown ends, then release."
            ),
            timeout_seconds=5.0,
            safety_note=drive_safety,
        ),
        WizardStep(
            key="motor_current",
            title="12. Motor current / motion gate",
            prompt=(
                "When listening starts, perform one small gentle drive movement "
                "and return to center. The future version will look for motor "
                "current or motion-state candidates."
            ),
            timeout_seconds=8.0,
            safety_note=drive_safety,
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
            "Skeleton only; no CAN recognition implemented.",
            "Passive design target: do not transmit unless a future explicit confirm mode is added.",
            "Do not test drive movement without open space and a spotter.",
        ],
    )


def save_json_profile(profile: MeetGreetProfile, output_path: Path) -> None:
    """Write current placeholder profile as JSON."""
    serializable = asdict(profile)
    output_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")


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
    print("No recognition has been implemented yet; these are interaction-flow results only.")


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
        "--log-snippet-root",
        default=LOG_SNIPPET_ROOT_DEFAULT,
        help=f"Directory where per-step listening snippets are written "
            f"(default: {LOG_SNIPPET_ROOT_DEFAULT})",
    )
    p.add_argument(
        "--listen-seconds",
        type=float,
        default=LISTEN_SECONDS_DEFAULT,
        help=f"Seconds to listen for each interactive step "
            f"(default: {LISTEN_SECONDS_DEFAULT})",
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
    return p.parse_args()


def main() -> int:
    args = parse_args()
    files_root = Path(args.files_root)
    files_root.mkdir(parents=True, exist_ok=True)
    output_path = resolve_runtime_path(files_root, args.output)
    log_root = ensure_log_root(resolve_runtime_path(files_root, args.log_snippet_root))
    replay_root = resolve_runtime_path(files_root, args.replay_log_root)
    resolved_log_root = resolve_runtime_path(files_root, args.log_snippet_root)
    assert resolved_log_root is not None
    log_root = ensure_log_root(resolved_log_root)

    profile = build_profile(args)
    steps = build_default_steps()

    print("R-Net Meet & Greet skeleton")
    print("This version does not read CAN yet. It only exercises the user-guided flow.")
    print("Files root: %s" % files_root)
    print("Output profile: %s" % output_path)
    print("Log snippets: %s" % log_root)
    print()
    if replay_root is not None:
        print(f"Replay mode: reading snippets from {replay_root}")
    else:
        print("Live/stub mode: collecting from current listener")

    try:
        for step in steps:
            result = run_step(
                step,
                log_root,
                args.listen_seconds,
                replay_root=replay_root,
                replay_pick=args.replay_pick,
            )
            profile.steps[result.key] = result
            save_json_profile(profile, output_path)
    except KeyboardInterrupt:
        print("\nWizard interrupted. Saving partial placeholder profile...")
        save_json_profile(profile, output_path)
        print_summary(profile)
        return 130

    save_json_profile(profile, output_path)
    print_summary(profile)
    print("Saved placeholder profile to: %s" % output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
