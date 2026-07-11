#!/usr/bin/env python3
"""
rnet_meet_greet_skeleton.py - Passive R-Net chair capability discovery wizard.

Purpose:
  This is the core bones / interaction flow for a future "meet and greet"
  script that helps map chair-specific R-Net frames on a new wheelchair.

Design goals:
  - Passive by default: listen/recognize, do not transmit.
  - User-guided: prompt for one chair action at a time.
  - Skippable: every step can be skipped.
  - Timeout-aware: each step listens for a bounded time window.
  - Evidence-based: later versions should store confirmed/candidate/not-observed.
  - Safety-scoped: do not discover or transmit drive, seating, mode, or config frames.

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
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Literal


StepStatus = Literal["not_run", "skipped", "timeout", "candidate", "confirmed", "failed"]


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

# def recognize_horn_start_stop(baseline_frames: list, action_frames: list) -> dict:
#     """Future: recognize horn start/stop frame candidates."""
#     pass

# def recognize_light_toggle(action_name: str, baseline_frames: list, action_frames: list) -> dict:
#     """Future: recognize physical light toggle and UI/status bitmap candidates."""
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


# -----------------------------------------------------------------------------
# Current runnable skeleton helpers
# -----------------------------------------------------------------------------

def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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


def run_step(step: WizardStep) -> StepResult:
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
        return StepResult(
            key=step.key,
            title=step.title,
            status="skipped",
            notes=["User skipped this step."],
        )

    # Future expectation section would be called here.
    # Example:
    # baseline = collect_baseline(bus, BASELINE_SECONDS)
    # action_frames = collect_action_window(bus, step.timeout_seconds)
    # recognition = recognize_...(baseline, action_frames)

    wait_countdown(step.timeout_seconds)

    print("Recognition is not implemented yet in this skeleton.")
    post = ask_choice(
        "Mark this placeholder step as candidate, timeout, retry, skip, or quit?",
        {"c", "t", "r", "s", "q"},
        default="c",
    )
    if post == "q":
        raise KeyboardInterrupt
    if post == "r":
        return run_step(step)
    if post == "s":
        return StepResult(
            key=step.key,
            title=step.title,
            status="skipped",
            notes=["User skipped after listening window."],
        )
    if post == "t":
        return StepResult(
            key=step.key,
            title=step.title,
            status="timeout",
            notes=["Listening window completed; no recognition logic implemented yet."],
        )
    return StepResult(
        key=step.key,
        title=step.title,
        status="candidate",
        notes=["Placeholder only. Recognition logic not implemented yet."],
        observations={
            "timeout_seconds": step.timeout_seconds,
            "implemented": False,
        },
    )


def build_default_steps(include_drive_tests: bool) -> list[WizardStep]:
    """Define the main meet-and-greet path."""
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
    ]

    if include_drive_tests:
        drive_safety = (
            "Use the slowest indoor profile, open space, and a spotter. Release the "
            "joystick immediately if anything feels wrong."
        )
        steps.extend(
            [
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
        )
    else:
        steps.append(
            WizardStep(
                key="drive_tests_skipped",
                title="8. Drive tests skipped",
                prompt=(
                    "Drive/joystick movement tests are disabled for this run. This is "
                    "the safer mode for bench testing horn/lights/buttons only."
                ),
                timeout_seconds=0.1,
            )
        )

    steps.append(
        WizardStep(
            key="final_review",
            title="Final review",
            prompt=(
                "Review the placeholder results. Future versions will show confirmed, "
                "candidate, and not-observed mappings here."
            ),
            timeout_seconds=0.1,
        )
    )
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
        "--include-drive-tests",
        action="store_true",
        help="include joystick/motor movement prompts; default is horn/lights/buttons only",
    )
    p.add_argument(
        "--output",
        default="rnet_meet_greet_profile.json",
        help="path for placeholder JSON profile output",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    profile = build_profile(args)
    steps = build_default_steps(include_drive_tests=args.include_drive_tests)

    print("R-Net Meet & Greet skeleton")
    print("This version does not read CAN yet. It only exercises the user-guided flow.")
    print("Output profile: %s" % args.output)
    print()

    try:
        for step in steps:
            result = run_step(step)
            profile.steps[result.key] = result
            save_json_profile(profile, Path(args.output))
    except KeyboardInterrupt:
        print("\nWizard interrupted. Saving partial placeholder profile...")
        save_json_profile(profile, Path(args.output))
        print_summary(profile)
        return 130

    save_json_profile(profile, Path(args.output))
    print_summary(profile)
    print("Saved placeholder profile to: %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
