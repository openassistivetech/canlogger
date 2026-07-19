# R-Net Running Notes

**Working date:** 2026-07-18 / 2026-07-19 UTC  
**Chair/profile context:** Bumblebee / current post-config profile  
**Scope:** passive observation, profile-building, and recognizer design only.  
**Safety boundary:** These notes are for passive analysis of observed CAN traffic. They are not a transmit map for drive, seating, or configuration actions.

---

## Entry 1 - Clean boot sequence with Raspberry Pi listener

**Source log:** `20260718T223804Z_first_boot_with_rpi_candidate.log`  
**Custom log title:** `first boot with rpi`  
**Label:** `candidate`  
**Capture notes from log:**

```text
started listening before plugging in the pi
connected and turned on the chair
```

### Short summary

This appears to be a clean, boot-aligned startup capture. The first actual CAN frame is `00C#`, which makes it a useful alignment point for the full boot sequence. The chair then moves through the familiar startup pattern: repeated `7B3#`, a dense `1F...#` burst, `7B1#`, `7B0#`, early status/config frames, a dense `781/790` negotiation burst, and finally steady joystick traffic on `02000100#0000`.

The active joystick stream first appears approximately **2.216 seconds after `00C#`**.

No `78F` frames were observed in this log, so this Raspberry Pi / CAN listener setup does **not** appear to create the same startup signature as the classic programmer-attached state.

### Timeline relative to first `00C#`

| Relative time | Frame / pattern | Working interpretation |
| ---: | --- | --- |
| T+0.000 s | `00C#` | Boot marker / startup alignment point. |
| T+0.021 to T+0.121 s | repeated `7B3#` with `00E#...` heartbeat | Early module wake-up / startup presence. |
| T+0.139 to T+0.161 s | dense `1F...#` burst, 32 frames | Early startup/config burst. |
| T+0.330 s | `7B1#` | Startup stage transition. |
| T+0.380 s | `7B0#`, repeated | Startup stage transition. |
| T+0.400 to T+0.497 s | `1C0C0000#8A`, `1C2C0100#...`, `1C240101#`, first `03C30F0F#...` | Status/config/display families begin. |
| T+0.640 s | `140C0001#0000` | Drive/status candidate appears. |
| T+0.740 to T+0.767 s | `050...`, `0C280000#00`, `0C140000#C0/C1`, `040#00000000`, start of `781/790` | Start of heavier negotiation/config chatter. |
| T+0.751 to T+1.561 s | many `781/790`, then `782/790`, then `783/790` exchanges | Multi-module startup negotiation/config readout candidate. |
| T+1.584 to T+1.598 s | `040/041/042/043` flip from `00000000` to `80000000`, plus `050...`, `060...`, `061...` | Candidate ready-state/module-state flip. |
| T+1.600 to T+1.610 s | `0C180102#0003`, `0C180000#0101`, `0C180001#200001` | Seating/display/status initialization candidate. Not a seating command by itself. |
| T+2.216 s | first `02000100#0000` | Active joystick stream online. Candidate chair-ready-for-input marker. |
| T+2.216 s onward | steady `02000100#0000`, `00E`, `03C30F0F`, `140C0001`, `14300000`, `1C...` | Normal steady idle state. |

### Proposed phase labels

| Phase label | Relative time | Evidence |
| --- | ---: | --- |
| `boot_marker` | T+0.000 s | `00C#` |
| `early_module_wake` | T+0.021 to T+0.121 s | `7B3#`, `00E#...` |
| `startup_1F_burst` | T+0.139 to T+0.161 s | 32 `1F...#` frames |
| `startup_stage_transition` | T+0.330 to T+0.400 s | `7B1#`, `7B0#`, `1C0C0000#8A` |
| `status_streams_begin` | T+0.423 to T+0.640 s | `1C2C0100`, `1C240101`, `03C30F0F`, `140C0001` |
| `module_negotiation_burst` | T+0.740 to T+1.561 s | `050`, `040/041/042/043`, `781/790`, `782/790`, `783/790` |
| `ready_state_flip` | T+1.584 to T+1.598 s | `040/041/042/043#80000000`, `050`, `060`, `061` |
| `display_or_seating_status_init` | T+1.600 to T+2.131 s | `0C18...`, `14300001` |
| `joystick_online` | T+2.216 s | first `02000100#0000` |
| `steady_idle` | T+2.216 s onward | `02000100`, `00E`, `03C30F0F`, `140C0001`, `14300000`, `1C...` |

### Current interpretation

This log is a good candidate reference for **clean no-programmer startup with Raspberry Pi listener attached**.

Key findings:

1. The first frame is `00C#`, so the capture appears to start before bus startup rather than after queued traffic.
2. `02000100#0000` first appears about 2.216 seconds after `00C#` and then becomes a steady high-rate joystick stream.
3. No `78F` frames appear in this boot log, so the passive Raspberry Pi logger does not look like the classic programmer-attached startup state.
4. `0C18...` frames appear during startup even without a seating action. Therefore, seeing `0C18...` alone is not enough to infer seating-mode control. These should be treated as display/status initialization candidates unless paired with seating-mode context and joystick movement.
5. The dense `781/790`, `782/790`, and `783/790` sections are good comparison regions for future startup tests with other attached devices.

### Suggested note for status update

```text
The first-boot-with-RPi custom log is a clean boot-aligned capture. The first CAN frame is `00C#`, followed by the familiar early startup sequence: repeated `7B3#`, a dense `1F...#` burst, `7B1#`, `7B0#`, then status/config families including `1C0C0000`, `1C2C0100`, `03C30F0F`, `140C0001`, and a dense `781/790` negotiation burst. The active joystick stream `02000100#0000` first appears about 2.216 seconds after `00C#` and remains steady afterward. No `78F` frames were observed, so this RPi-attached passive logger does not appear to create the same startup signature as the programmer module. Treat the `0C18...` frames during startup as display/status initialization candidates, not seating commands by themselves.
```

### Follow-up tests to compare

| Future test | Why it matters |
| --- | --- |
| Repeat boot with RPi/CAN attached 2-3 times | Confirm timing stability and startup phase consistency. |
| Boot without RPi/CAN attached, if capturable another way | Check whether RPi listener changes the startup sequence. |
| Boot with classic programmer attached | Compare against the `78F` programmer-attached signature. |
| Boot with GOAT dongle attached | Compare compact `781/790` behavior and possible GOAT-specific frames. |
| Boot from different last-used chair state | See whether startup `0C18...` status changes based on prior mode/profile. |

---

## Parking lot / things to add before wrap-up

- Add notes from any additional custom logs captured today.
- Update script TODOs if the boot recognizer should become a standard custom-log analyzer.
- Decide whether `joystick_online` should be promoted as a standard startup landmark in the meet-and-greet report.

## Profile/mode button repeats: indoor drive -> normal drive -> seating -> indoor drive

**Source logs:**

- `20260718T223932Z_profile_mode_changes_candidate.log`
- `20260718T224026Z_profile_mode_changes_2_candidate.log`
- `20260718T224108Z_prodile_mode_changes_3_candidate.log`

**Human note:** chair was already on and in indoor drive mode. The profile/mode button was pressed repeatedly to move through:

```text
indoor drive -> normal drive -> seating mode -> indoor drive
```

### Capture quality

These look like clean custom captures. Each begins with normal centered idle traffic rather than stale action traffic. The steady `02000100#0000` joystick stream remains present throughout, so these mode/profile transitions are not joystick movements.

Approximate parsed capture spans:

| Repeat | Frames | CAN timestamp span | Notes |
| --- | ---: | ---: | --- |
| 1 | 1240 | 8.409 s | user-interrupted before full 10 s |
| 2 | 1118 | 7.589 s | user-interrupted before full 10 s |
| 3 | 1129 | 7.609 s | user-interrupted before full 10 s |

### Repeated transition pattern

Across all three repeats, the action does **not** look like one isolated button-command frame. It looks like a small mode/profile transition sequence with three repeated bursts.

#### Transition 1: indoor drive -> normal drive candidate

Approximate timing by repeat:

| Repeat | Approx time from first frame | Main candidate frames |
| --- | ---: | --- |
| 1 | T+2.330 to T+2.388 s | `051#00010000`, `050#11010002`, `050#20010000`, `050#30010001`, `050#91010000`, `050#81010001`, `0A040100#4B`, `181C0100#0260000000000000` |
| 2 | T+1.601 to T+1.700 s | same pattern |
| 3 | T+1.830 to T+1.924 s | same pattern |

Working label:

```text
candidate_drive_profile_transition
```

Notes:

- `181C0100#0260000000000000` appears again as part of a mode/display transition, not as a settings-only frame.
- This transition does not include the `0C180300#0202` / `0C180301...` seating-status block.
- A single `1C240101#` appears shortly after this first transition in each repeat. This may be related, but it should remain a weak candidate until isolated with better controls.

#### Transition 2: normal drive -> seating mode candidate

Approximate timing by repeat:

| Repeat | Approx time from first frame | Main candidate frames |
| --- | ---: | --- |
| 1 | T+4.170 to T+4.406 s | `061#40100000`, `060#51000002`, `060#60000000`, `060#70000009`, `060#80000010`, `051#00000000`, `050#11000002`, `050#91000000`, `050#81000001`, `061#00010000`, `060#11010002`, `060#20010000`, `063#30010001`, `060#80010080`, `181C0100#0260000000000000`, then `0C180300/0C180301` block |
| 2 | T+2.620 to T+2.866 s | same pattern |
| 3 | T+3.201 to T+3.442 s | same pattern |

The strongest seating-entry evidence is the repeated block:

```text
0C180300#0202
0C180301#220101
0C180301#300101
0C180301#010101
0C180301#030101
0C180301#000101
0C180301#330101
0C180300#0201
```

Working labels:

```text
candidate_enter_seating_mode_transition
candidate_selected_seating_function_22_tilt
```

Notes:

- The first `0C180301` value in the block is consistently `220101` in all three repeats.
- This lines up with the existing candidate function map where `22 = tilt` for this chair/config.
- Joystick remains centered while this happens, so entering seating mode appears to be a display/mode transition, not a nonzero joystick command.
- The repeated `0C180300#0202` start marker and `0C180300#0201` end marker are reinforced by this test.

#### Transition 3: seating mode -> indoor drive candidate

Approximate timing by repeat:

| Repeat | Approx time from first frame | Main candidate frames |
| --- | ---: | --- |
| 1 | T+5.940 to T+6.041 s | `061#40010000`, `060#51010002`, `060#60010000`, `063#70010009`, `060#80010010`, `061#00000000`, `060#11000002`, `060#20000000`, `060#30000001`, `060#80000080`, `0C180000#0101`, `0A040100#4B`, `181C0100#0260000000000000` |
| 2 | T+4.050 to T+4.153 s | same pattern |
| 3 | T+4.750 to T+4.827 s | same pattern |

Working label:

```text
candidate_exit_seating_to_drive_transition
```

Notes:

- This transition does **not** include a new `0C180300#0202` / `0C180301` selected-function block.
- It does include `0C180000#0101` and the same `181C0100#0260000000000000` transition frame.
- This supports treating `181C0100#0260000000000000` as a broad display/mode/navigation transition frame rather than a specific settings-entry marker.

### Recognizer implications

Potential passive recognizer structure:

1. **Mode/profile transition candidate**
   - Look for compact `050/051/060/061/063` transition bursts.
   - Treat `181C0100#0260000000000000` as supporting evidence, not a standalone label.

2. **Enter seating mode candidate**
   - Require a transition burst plus a `0C180300#0202 ... 0C180300#0201` block.
   - Use the first `0C180301#XX...` inside the block as the selected seating function candidate.
   - For these repeats, `XX=22`, consistent with tilt selected.

3. **Exit seating to drive candidate**
   - Look for the reverse-style transition burst plus `0C180000#0101` and `181C0100#0260000000000000`.
   - Do not require a selected-function block on exit.

### Current interpretation

The three repeats strongly support this model:

```text
profile/mode button press 1: indoor drive -> normal drive
profile/mode button press 2: normal drive -> seating mode, selected function appears as 22/tilt
profile/mode button press 3: seating mode -> indoor drive
```

Important caution:

```text
181C0100#0260000000000000 is not settings-specific. It appears in drive-profile changes, seating entry, and seating exit. It should be labeled as a candidate display/mode/navigation transition frame unless additional context narrows it.
```

### Confidence

| Finding | Confidence | Reason |
| --- | --- | --- |
| Repeats are clean enough to compare | High | Starts with normal on-chair idle traffic and no obvious stale action frames. |
| `181C0100#0260000000000000` is a broad mode/display transition marker | Medium-high | Appears in all three transitions across all three repeats. |
| `0C180300#0202 ... 0C180300#0201` marks a seating status/function block | High for this chair/config | Same block appears in all three seating-entry repeats. |
| First `0C180301#220101` means selected function `22 = tilt` | High for this chair/config | Repeated three times and aligns with prior seating function map. |
| Exact meaning of `050/051/060/061/063` bursts | Low-medium | Very consistent, but not decoded yet. Treat as transition/status/handshake candidates. |

---

## Sequence for legs: drive mode change, seating function cycling, then legs movement

**Source log:** `20260719T001026Z_sequence_for_legs_candidate.log`  
**Capture title:** `sequence for legs`  
**User comments in log:** started in indoor mode, switched to normal drive, entered seating, cycled through several seating functions until legs, then elevated/moved legs.  
**Scope:** passive observation only.

### Why this log matters

This is the cleanest end-to-end validation so far because it combines several previously separate observations in one continuous sequence:

1. drive profile/mode transition,
2. entering seating mode,
3. cycling seating functions using the joystick X axis,
4. reaching the legs function,
5. moving legs using joystick Y.

The log strongly supports the idea that the profile/mode button and seating joystick behavior can be recognized as a sequence of passive state transitions, not as a single isolated button frame.

### High-level timeline

All times below are relative to the first CAN frame in the log, `T+0.000`.

| Time | Event | Working interpretation |
| ---: | --- | --- |
| `T+0.000` onward | Steady `02000100#0000` with normal idle/status traffic | Chair on, centered joystick, starting from drive mode |
| `T+3.401–3.506` | `051/050` burst, `1C240101#`, `0A040100#4B`, `181C0100#0260000000000000` | Candidate drive profile/mode transition: indoor drive to normal drive |
| `T+4.832–5.071` | `061/060/051/050` transition burst, `181C0100#026...`, then `0C180300#0202 ... 0C180300#0201` | Candidate enter seating mode transition |
| `T+4.932` | `0C180301#220101` | First selected seating function after entering seating: candidate `22 = tilt` |
| `T+6.610–6.890` | Joystick X positive, up to about `X=+98`, then seating block | Next seating function selection |
| `T+6.847` | `0C180301#200101` | Candidate `20 = elevate` |
| `T+7.510–7.790` | Joystick X positive again, up to about `X=+98`, then seating block | Next seating function selection |
| `T+7.751` | `0C180301#210101` | Candidate `21 = recline/backrest` |
| `T+8.350–8.650` | Joystick X positive again, up to about `X=+98`, then seating block | Next seating function selection |
| `T+8.589` | `0C180301#230101` | Candidate `23 = legs` |
| `T+9.920–13.340` | Joystick Y positive, sustained near `Y=+100` | Legs movement / legs-more action |
| After `T+13.340` | Joystick returns to centered/idle traffic | Legs movement ended |

### Seating function blocks

The repeated seating status/function block pattern is very clear:

```text
0C180300#0202    candidate start of seating function/status block
0C180301#....    selected function and related status values
0C180300#0201    candidate end of seating function/status block
```

The first `0C180301` value inside each block is still the strongest selected-function candidate:

| Block | Time range | First `0C180301` | Candidate selected function |
| ---: | --- | --- | --- |
| 1 | `T+4.911–5.071` | `220101` | tilt |
| 2 | `T+6.827–6.967` | `200101` | elevate |
| 3 | `T+7.732–7.851` | `210101` | recline/backrest |
| 4 | `T+8.569–8.689` | `230101` | legs |

This sequence validates the earlier candidate order for X-positive seating function cycling:

```text
tilt (22) -> elevate (20) -> recline/backrest (21) -> legs (23)
```

### Joystick interpretation

The joystick stream remains the same current-profile stream:

```text
02000100#XXYY
```

where `XX` and `YY` are signed int8 values, centered at `0000`.

Observed movement clusters:

| Cluster | Time range | Joystick movement | Interpretation |
| ---: | --- | --- | --- |
| 1 | `T+6.610–6.890` | X positive, peak about `+98`, small negative Y drift | seating function next: tilt -> elevate |
| 2 | `T+7.510–7.790` | X positive, peak about `+98`, small negative Y drift | seating function next: elevate -> recline |
| 3 | `T+8.350–8.650` | X positive, peak about `+98`, small negative Y drift | seating function next: recline -> legs |
| 4 | `T+9.920–13.340` | Y positive, sustained near `+100`, small X drift | legs movement / legs-more |

This supports a recognizer strategy where the selected seating function is maintained as state. The script should not infer “legs” from the Y movement alone. It should infer:

1. chair is in seating context,
2. selected function is `23 = legs`, based on the latest `0C180300/0C180301` function block,
3. joystick Y positive while selected function is legs means candidate legs-more / leg-elevation movement.

### Interpretation update

This log strengthens three prior findings:

1. `181C0100#0260000000000000` is a general display/mode/navigation transition candidate, not settings-specific.
2. `0C180300#0202` and `0C180300#0201` are useful start/end candidates for seating function/status blocks.
3. The first `0C180301` inside that block is the best current selected-function candidate.

It also gives a practical recognition sequence:

```text
mode/profile transition -> enter seating -> selected function 22 tilt
X+ -> selected function 20 elevate
X+ -> selected function 21 recline/backrest
X+ -> selected function 23 legs
Y+ while selected function 23 -> legs-more / leg-elevation movement
```

### Caution

This is still passive recognition evidence. The log shows how the chair reports and responds during normal joystick/user control. It does not provide a safe transmit map for seating actuation.

---

## Startup while last-used mode was seating / tilt

**Source log:** `20260719T001311Z_startup_in_non_drive_mode_candidate.log`  
**Title:** `startup in non-drive mode`  
**User notes:** startup into seating; seating in tilt mode.  
**Capture context:** chair had been turned off while in seating mode, then turned back on. On restart, the chair returned to seating mode rather than defaulting to drive mode.

### Summary

This log is a clean startup capture beginning at `00C#`, but unlike the earlier first-boot-with-RPi capture, the chair restores the previous seating context during startup. The early boot sequence is mostly the same as the drive-mode startup, but the mid-boot display/status initialization differs: it includes repeated `0C180300/0C180301` seating function/status blocks, with `0C180301#220101` as the first selected-function value. This supports `22 = tilt` and shows that startup can restore the last-used seating function/mode.

### Timeline relative to `00C#`

| Relative time | Frame / pattern | Working interpretation |
| ---: | --- | --- |
| `T+0.000` | `00C#` | clean boot marker |
| `T+0.021–0.121` | repeated `7B3#` | early startup/module wake sequence |
| `T+0.139–0.161` | dense `1F...#` burst | early startup/config burst, same familiar pattern |
| `T+0.330` | `7B1#` | startup stage transition |
| `T+0.376` | `7B0#` | startup stage transition |
| `T+0.730–1.529` | `050`, `040/041/042/043`, `781/790`, `782/790`, `783/790` | dense module negotiation/config/status burst |
| `T+1.555–1.568` | `040/041/042/043#80000000`, `050`, `061`, `060`, `063` | ready/state flip plus mode-context setup |
| `T+1.570` | `14300001#00` | seating/display position/status candidate begins |
| `T+1.572` | `0C180102#0003` | display/seating status initialization candidate |
| `T+1.574–1.714` | `0C180300#0202 ... 0C180301#220101 ... 0C180300#0201` | seating function/status block; selected function candidate `22 = tilt` |
| `T+1.734–1.814` | second `0C180300` block, first value again `220101` | repeated/confirming tilt selected-function block |
| `T+1.894–2.053` | third `0C180300` block, first value again `220101` | repeated/confirming tilt selected-function block, with additional `330101` status value |
| `T+2.130` | `0C180101#060201`, repeated twice | post-start seating/display status candidate |
| `T+2.216` | first `02000100#0000` | joystick stream comes online, centered |
| `T+3.487 / 4.035 / 4.584` | `0C140300#42`, `#82`, `#C2` with `14300001#19/#32/#64` | later display/status progression; seen in earlier boot too |

### Difference from the drive-mode startup

The earlier first-boot-with-RPi log also produced `0C18...` frames during startup, but it did **not** produce `0C180300/0C180301` function blocks at startup. Instead, its early `0C18` startup frames looked more like:

```text
0C180102#0003
0C180000#0101
0C180001#200001
```

In this non-drive startup, the chair instead emits explicit seating function/status blocks:

```text
0C180300#0202
0C180301#220101
0C180301#300101
0C180301#010101
0C180301#030101
0C180301#000101
0C180300#0201
```

and repeats the `220101` block more than once during startup.

This is an important distinction:

- `0C18...` alone is not enough to prove seating action or seating mode.
- `0C180300#0202 ... 0C180301#220101 ... 0C180300#0201` during startup is stronger evidence that the chair restored seating/tilt context.
- The selected-function value `22 = tilt` now appears not only during manual seating mode entry, but also during startup into persisted seating mode.

### Current interpretation

This log supports a “persisted last mode” model:

```text
chair turned off while in seating/tilt
-> next startup follows normal boot sequence
-> startup restores seating context
-> startup broadcasts seating function/status blocks
-> first selected function value is 22 = tilt
-> joystick stream comes online afterward as 02000100#0000
```

The first active joystick stream still appears at about `T+2.216`, almost the same as the earlier drive-mode startup. So the joystick-online timing is stable, but the startup display/seating-status section before it reflects the persisted mode.

### Recognizer implication

A future passive recognizer should treat startup mode as stateful:

1. Watch for clean boot marker `00C#`.
2. Segment the startup phases as before.
3. During the startup status/display section, look for `0C180300/0C180301` blocks.
4. If the first `0C180301` in those blocks is a known seating function code such as `220101`, infer that the chair restored a seating function context at boot.
5. Do **not** require a profile/mode button event to infer seating mode; the chair may already be in seating mode because it persisted from shutdown.

### Confidence

| Claim | Confidence | Notes |
| --- | --- | --- |
| This is a clean boot-aligned capture | High | Starts at `00C#` with no pre-boot queued traffic. |
| Startup restores previous seating mode/context | Medium-high | User context plus repeated startup `0C180300/0C180301` blocks. |
| `22 = tilt` | High for this chair/config | Repeated here and in prior seating-entry/function-cycling logs. |
| Joystick stream still comes online at about `T+2.216` | High | Matches earlier clean boot timing closely. |
| Exact meanings of `300101`, `010101`, `030101`, `000101`, `330101` | Low | Treat as additional seating/display/status values, not decoded yet. |

### Safety / interpretation caution

This is passive evidence of startup state restoration and display/status reporting. It does not imply that the `0C180300/0C180301` frames are actuator commands, and it does not provide a safe transmit map for seating movement.

---

## Shutdown from seating/legs mode

**Source log:** `20260719T001213Z_shutdown_candidate.log`  
**Capture title:** `shutdown`  
**User note:** from seating mode in legs shutdown  
**Capture context:** chair was on, in seating mode with legs context, then shut down.

### Why this log matters

This is a short, clean shutdown capture from a non-drive/seating context. It gives us a compact reference for how the chair transitions from steady centered joystick traffic into final bus silence/power-down frames.

The log begins in normal live centered traffic:

```text
02000100#0000
00E#A40021F200000000
03C30F0F#87878787878787
140C0001#0000 / 14300000#0000
0C140300#C2
1C300004#59720300816A0300
```

The interesting shutdown tail begins when the steady `02000100#0000` joystick stream stops and `002#` / `004#` frames begin.

### Key shutdown window

```text
(1784419897.371079) can0 02000100#0000
(1784419897.381119) can0 02000100#0000
(1784419897.381510) can0 002#
(1784419897.391820) can0 004#
(1784419897.392643) can0 004#
(1784419897.422518) can0 14300000#0000
(1784419897.434755) can0 002#
(1784419897.435309) can0 1C240101#
(1784419897.445657) can0 004#
(1784419897.446786) can0 004#
(1784419897.491415) can0 002#
(1784419897.503296) can0 004#
(1784419897.515034) can0 061#40010000
(1784419897.515854) can0 060#51010002
(1784419897.516641) can0 060#60010000
(1784419897.517706) can0 063#70010009
(1784419897.518542) can0 060#80010010
(1784419897.544692) can0 002#
(1784419897.564675) can0 004#
(1784419897.610629) can0 002#
(1784419897.624681) can0 004#
(1784419897.671474) can0 002#
(1784419897.702038) can0 000#
(1784419897.702502) can0 000#
(1784419897.702913) can0 000#
(1784419897.703365) can0 000#
(1784419897.703743) can0 000#
```

### Timing landmarks

Using the last centered joystick frame before shutdown as the local reference:

| Event | Timestamp | Delta from last joystick frame |
| --- | ---: | ---: |
| Last observed centered joystick frame | `1784419897.381119` | `0.000 s` |
| First `002#` shutdown marker | `1784419897.381510` | `+0.000391 s` |
| First `004#` marker | `1784419897.391820` | `+0.010701 s` |
| `061/060/063` shutdown-status burst begins | `1784419897.515034` | `+0.133915 s` |
| First final `000#` | `1784419897.702038` | `+0.320919 s` |
| Last final `000#` | `1784419897.703743` | `+0.322624 s` |

### Candidate interpretation

This shutdown log has three visually clear phases:

1. **Steady pre-shutdown idle**  
   High-rate `02000100#0000` continues, with normal background status frames.

2. **Shutdown initiation**  
   The high-rate joystick stream stops and repeated `002#` / `004#` frames appear.

3. **Shutdown/status burst and final bus-off marker**  
   A compact non-drive/seating-looking shutdown burst appears:

   ```text
   061#40010000
   060#51010002
   060#60010000
   063#70010009
   060#80010010
   ```

   Then a few more `002#` / `004#` frames appear, followed by five `000#` frames.

### Relationship to earlier startup/mode findings

The `061/060/063` burst is interesting because it resembles the mode transition families seen during profile/mode changes and seating entry, but here it appears in the shutdown path. The `...0100...` / `...100...` values may reflect that the chair is shutting down from a seating/non-drive context, but this should remain a passive candidate until compared against shutdowns from drive mode and other seating functions.

### Working recognizer idea

A passive shutdown recognizer could look for:

```text
steady 02000100#0000
→ loss of joystick stream
→ repeated 002#/004# frames
→ optional 061/060/063 shutdown-status burst
→ final cluster of 000# frames
```

For this log, the most compact shutdown signature is:

```text
002#
004#
002#
004#
061#40010000
060#51010002
060#60010000
063#70010009
060#80010010
002#
004#
002#
004#
002#
000#
000#
000#
000#
000#
```

### Confidence

| Claim | Confidence | Notes |
| --- | --- | --- |
| This is a clean shutdown capture | High | Short log, no stale preamble, clear end marker. |
| `002#` / `004#` are part of shutdown tail | High | They appear exactly as steady traffic stops and before final `000#`. |
| Final repeated `000#` marks final shutdown/bus-off tail | High | Matches prior shutdown observations. |
| `061/060/063` burst is shutdown-status context | Medium | Appears only during the shutdown tail here, but needs drive-mode shutdown comparison. |
| Values in `061/060/063` indicate seating/non-drive shutdown context | Low-medium | Plausible because user notes seating/legs context; needs repeats and drive-mode comparison. |

### Safety / interpretation caution

This is passive shutdown evidence only. The `002#`, `004#`, `060#`, `061#`, `063#`, and `000#` frames should not be treated as commands to transmit. They are useful as passive markers for recognizing shutdown state and segmenting logs.
