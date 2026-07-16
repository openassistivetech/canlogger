# R-Net Passive CAN Research Status Update

**Audience:** collaborators who are familiar with the R-Net research direction and want to know what has changed recently.  
**Current scope:** passive observation, chair/config profiling, recognizer design, and safety-conscious research notes.  

**Context:** this work builds on earlier R-Net reverse-engineering work, especially the open-rnet project: https://github.com/redragonx/open-rnet

---

## 1. What changed since the hackathon

At the GitHub hackathon stage, we had a useful but very hardcoded result:

- We knew specific joystick frame IDs and directional values for one observed setup.
- We built a script that watched joystick direction and honked the horn in different patterns depending on driving direction.
- The code worked, but it assumed the chair/config matched the values we had already discovered.

The recent progress is that we are moving from **hardcoded one-chair observations** toward a **repeatable passive profiling workflow**.

### Then vs now

| Area | Hackathon state | Current state |
| --- | --- | --- |
| Joystick ID | Hardcoded from one observed setup | Passively inferred and confirmed per chair/config by `rnet_meet_greet` |
| Joystick direction values | Hardcoded directional thresholds | Calibrated center, axes, polarity, and range from guided movements |
| Horn/lights | Known enough for a demo script | Confirmed as profile findings with recognizer evidence and raw logs |
| Chair differences | Mostly not modeled | Now treated as chair- and configuration-specific |
| Test workflow | Manual, one-off captures | Guided wizard with standard steps, JSON profile, Markdown summary, and raw snippets |
| Research framing | “Can we recognize/use a few known frames?” | “Can we build a passive fingerprint for each chair/config, then compare them?” |

The important lesson from the newest captures is that **configuration matters**. After applying a different but similar Permobil M3 configuration, the active joystick stream changed to a clearly confirmed `0x02000100` profile. That means we should not treat one joystick ID as a universal property of the hardware.

---

## 2. The main practical progress: `rnet_meet_greet`

`rnet_meet_greet` is now the standard passive “hello, who are you?” script for a new chair or a newly written configuration.

It prompts the researcher through a set of simple actions, listens passively on the CAN bus, saves the raw evidence, and produces a profile that can be compared later.

### What the script does today

For each guided step, the script:

1. flushes already-buffered CAN frames before capture;
2. listens passively for a bounded time window;
3. saves a raw log snippet;
4. runs a recognizer for that step;
5. stores results in a JSON profile;
6. writes a human-readable Markdown summary;
7. supports replay mode from saved snippets for offline analysis;
8. supports custom freeform logs with human comments.

The script is designed as a passive discovery wizard. It should remain passive unless a future transmit mode is explicitly designed, reviewed, and separated from this profiling workflow.

### Current standard wizard steps

| Step | Purpose | Current recognizer output |
| --- | --- | --- |
| Baseline idle | Learn ordinary bus chatter and infer joystick-like high-rate IDs | top CAN IDs, frame rates, best joystick idle candidate |
| Horn | Press/release horn | horn start/stop evidence, including joystick and programmer variants |
| Left indicator | Toggle on/off | physical toggle count and optional lamp-status evidence |
| Right indicator | Toggle on/off | physical toggle count and optional lamp-status evidence |
| Hazard lights | Toggle on/off | physical toggle count and optional lamp-status evidence |
| Flood/headlight | Toggle on/off | physical toggle count and optional lamp-status evidence |
| Joystick calibration | center -> forward -> center -> reverse -> center -> left -> center -> right -> center | joystick ID, center, axes, polarity, movement phases |
| Joystick forward range | single forward movement | direction range and drive-response candidates |
| Joystick reverse range | single reverse movement | direction range and drive-response candidates |
| Joystick left range | single left movement | direction range and drive-response candidates |
| Joystick right range | single right movement | direction range and drive-response candidates |

### What it calibrated on the current post-config chair profile

Current profile name used in the meet-and-greet output: **`bumblebee2`**.

| Item | Result | Current confidence |
| --- | --- | --- |
| Active joystick ID | `0x02000100` | High |
| Joystick center | `02000100#0000`, X=0, Y=0 | High |
| Forward | Y positive, peak about `+100` | High |
| Reverse | Y negative, peak about `-100` | High |
| Left | X negative, peak about `-100` | High |
| Right | X positive, peak about `+98` | High |
| Horn | `0C040100#` then `0C040101#` | High |
| Left indicator | `0C000101#` | High |
| Right indicator | `0C000102#` | High |
| Hazard lights | `0C000103#` | High |
| Flood/headlight | `0C000104#` | High |

### Current implementation status

| Feature | Status |
| --- | --- |
| Passive receive-only logging | Implemented |
| Replay mode from saved snippets | Implemented |
| Custom freeform log mode | Implemented |
| JSON profile output | Implemented |
| Markdown summary output | Implemented |
| Joystick idle inference | Implemented |
| Joystick calibration/range recognition | Implemented |
| Horn/lights recognizers | Implemented |
| Drive-response candidate ranking | Implemented, still exploratory |
| Receive queue flush before capture | Implemented, needs fresh validation with new logs |
| Clean CAN bus shutdown helper | Implemented |
| Seating recognizer | Not yet implemented in the standard wizard |
| Settings/menu recognizer | Not yet implemented |
| Programmer/config recognizer | Not yet implemented |

### Why this matters

The meet-and-greet profile gives us a current chair/config fingerprint before we interpret more complex logs. This is especially important because the newest evidence suggests that a configuration write can change which joystick stream is active.

Working methodological rule:

> Run `rnet_meet_greet` after any meaningful chair/config change. Do not merge findings across configurations unless the profile names and context are explicit.

---

## 3. Current high-confidence findings

### 3.1 Joystick frames after the config update

For the post-config `bumblebee2` profile, the active joystick stream behaves as:

```text
02000100#XXYY
```

Working decode:

| Byte | Interpretation |
| --- | --- |
| `XX` | signed int8 X-axis value |
| `YY` | signed int8 Y-axis value |
| `0000` | centered joystick |
| positive Y | forward / away in drive context |
| negative Y | reverse / toward in drive context |
| negative X | left |
| positive X | right |

The values are analog, not binary. Gentle and partial movements produce intermediate values. Full-range drive and seating captures often saturate near +/-100.

### 3.2 Raw joystick values appear normalized, while chair response changes downstream

Comparing drive profile 1 and profile 2 max-direction tests, the raw joystick stream still reached around +/-100. This suggests that speed/profile settings affect downstream chair response rather than the raw joystick encoding.

Working interpretation:

> The joystick stream reports normalized user input. Drive profile speed/sensitivity is likely applied later by controller/config logic.

### 3.3 Horn and lighting frames are stable in this profile

| Action | Observed frame/pattern | Confidence | Notes |
| --- | --- | --- | --- |
| Horn start | `0C040100#` | High | Seen in horn step. |
| Horn stop | `0C040101#` | High | Seen after horn release. |
| Left indicator | `0C000101#` | High | On/off toggle evidence. |
| Right indicator | `0C000102#` | High | On/off toggle evidence. |
| Hazard lights | `0C000103#` | High | On/off toggle evidence; also appears in short-press settings-button context. |
| Flood/headlight | `0C000104#` | High | On/off toggle evidence. |

These are useful recognition targets. They should not be presented as universal transmit guidance.

### 3.4 Startup and shutdown have structured passive signatures

Startup remains a candidate multi-stage sequence. Useful repeated landmarks include:

| Phase | Candidate frames/pattern | Notes |
| --- | --- | --- |
| Boot marker | `00C#` | Useful alignment point in startup logs. |
| Early startup burst | dense `1F...` family | Appears shortly after boot marker. |
| Startup status/config chatter | `7B3`, `7B1`, `7B0`, `1C...`, `781`, `790` families | Timing relative to `00C#` was consistent across repeats. |
| Joystick active | first repeated joystick stream after boot | Candidate marker that chair is online/ready. |

Shutdown appears cleaner than startup. Repeated shutdown logs showed:

- final centered joystick stream;
- `002#` / `004#` cluster;
- `061#40000000`;
- `060#...` sequence;
- `14300000#0000`;
- final repeated `000#` frames.

Working label:

```text
candidate_active_shutdown_signature
```

Important split:

- active shutdown = expected power-off sequence;
- post-shutdown/off-state events = separate, especially when programmer is attached.

### 3.5 Programmer-attached state is visible

Programmer-attached state appears to add traffic that was not present in comparable no-programmer windows.

| Context | Candidate evidence |
| --- | --- |
| Programmer attached during startup | repeated `78F` frames and extra `790` chatter |
| Programmer not attached during startup | no `78F` in comparable startup windows |
| Chair off, programmer attached | rare `1C240F01#` event about every 30.6 seconds |
| Chair off, programmer detached | no comparable off-state traffic in the 300-second control |

Working labels:

```text
candidate_programmer_attached_startup_signature
candidate_off_state_programmer_presence_event
```

---

## 4. Seating-mode findings

Seating is where we now have the strongest “mode-specific recognizer” evidence beyond basic joystick and horn/light actions.

### 4.1 Selected seating function appears in `0C180300` / `0C180301` blocks

The best evidence is the `cycle_all_seating_modes_both_directions` log. The human note for the capture was:

```text
tilt -> elevate -> recline -> legs -> tilt,
then tilt -> legs -> recline -> elevate -> tilt
```

The first code inside repeated `0C180301` blocks follows that order.

Candidate selected-function map for this chair/config:

| Function | Candidate code |
| --- | --- |
| Elevate | `20` |
| Recline / back rest | `21` |
| Tilt | `22` |
| Legs | `23` |

Observed block shape:

```text
0C180300#0202      candidate start-of-seating-function/status block
0C180301#22....    selected function/status item; first byte likely function code
0C180301#....      additional status/position metadata
0C180300#0201      candidate end-of-block marker
```

Important caution:

> Not every `0C180301` payload is a selected-function ID. The first value in the block is the strongest selected-function candidate; other `0C180301` frames likely carry additional status or position metadata.

### 4.2 Seating mode uses current selected function plus joystick direction

In seating mode, X-axis movement changes the selected seating function, while Y-axis movement adjusts the currently selected function.

Function cycling:

| Joystick movement | Candidate result |
| --- | --- |
| X positive | next seating function: `22 -> 20 -> 21 -> 23 -> 22` |
| X negative | previous seating function: `22 -> 23 -> 21 -> 20 -> 22` |

Y-axis more/less depends on selected function:

| Selected function | More action | Less action |
| --- | --- | --- |
| Tilt (`22`) | Y negative / toward user | Y positive / away from user |
| Elevate (`20`) | Y positive / away from user | Y negative / toward user |
| Recline (`21`) | Y negative / toward user | Y positive / away from user |
| Legs (`23`) | Y positive / away from user | Y negative / toward user |

Recognizer implication:

> Do not globally label Y positive as `more` or `less`. Interpret Y direction only after identifying the currently selected seating function.

### 4.3 Seating joystick input is analog

The `legs_half_full_up_down` log shows partial movement values rather than only full-scale +/-100.

Working interpretation:

> Seating joystick input is analog, but many normal holds saturate quickly near +/-100.

---

## 5. Settings, programmer, and config-write findings

These findings are useful, but they should remain more cautiously labeled than the joystick and seating findings.

### 5.1 Settings/menu transition frame

The frame below appears in settings/menu-related captures:

```text
181C0100#0260000000000000
```

Earlier it looked like a settings-entry candidate. Newer controls show it also appears in exit/navigation contexts, so it should be renamed:

```text
candidate_display_menu_navigation_transition
```

Avoid calling it:

```text
confirmed_enter_settings
```

The short press of the same button, which did not enter settings, toggled hazards and showed:

```text
0C000103#
```

That aligns with the meet-and-greet hazard finding.

### 5.2 Programmer speed tweaks differ by programmer type

Two programmer types were captured:

- classic/caller programmer;
- GOAT dongle.

They appear to use different traffic styles for the same user-level action.

#### Classic programmer speed setting

The strongest speed-value candidate appears in `78F#208C...` payloads.

Observed displayed speed values and likely hex values:

| Displayed speed | Candidate hex byte |
| ---: | ---: |
| 50 | `0x32` |
| 51 | `0x33` |
| 52 | `0x34` |
| 53 | `0x35` |

This is supported both ways:

- increase log: 50 -> 51 -> 52 -> 53, with `33`, `34`, `35`;
- decrease log: 53 -> 52 -> 51 -> 50, with `34`, `33`, `32`.

Associated repeated passive pattern:

```text
181C0F00#0160800000000000    candidate classic-programmer change/context marker
78F / 790 exchange block       candidate value/status exchange
15000000#01000100             candidate acknowledgement/commit/status marker
```

Do not interpret this as a write recipe. Use it only as passive evidence that the displayed value is present in the programmer exchange.

#### GOAT dongle speed setting

The GOAT dongle logs show a different compact exchange, including `781` / `790` traffic and repeated display/menu transition frames.

Repeated GOAT-related candidates:

| Candidate | Current interpretation |
| --- | --- |
| `781` / `790` compact blocks | GOAT programmer exchange family |
| `181C0100#0260000000000000` | display/menu/selection transition, not speed-specific by itself |
| `1E80000F#` | candidate GOAT dongle presence/idle/event frame |

The absolute speed value is not cleanly decoded for the GOAT dongle yet.

### 5.3 Whole-chair config write looks chunked, not setting-by-setting

The July 13 full config write logs suggest:

- not one huge opaque blob;
- not individual human-visible settings applied one by one;
- most consistent with chunked section/page transfer.

Observed high-level pattern:

| Pattern | Working interpretation |
| --- | --- |
| `1E42...` / `1E3F...` indexed families | candidate first transfer/read-like phase |
| `1E3C...` / `1E41...` indexed families | candidate second transfer/write/commit phase |
| repeated `15000000#...` markers | candidate section/page/object commit or acknowledgement markers |
| repeated `78F#4080000001000001` / `790#2F80000000000000` | polling/status/handshake-like traffic |

The failed write appears to start the process but does not show the fuller successful commit-looking sequence. The successful write shows a longer indexed and sectioned pattern.

Working label:

```text
candidate_chunked_config_page_write
```

---

## 6. What we suspect, but should not yet call confirmed

| Candidate | Confidence | Why it is not confirmed yet |
| --- | --- | --- |
| `02000200` / `02001100` are pre-config or alternate-profile streams | Medium | They appear in older/write logs, but post-config meet-and-greet confirms `02000100`. Need same-chair pre/post controlled repeats. |
| `181C0100#0260000000000000` is a display/menu navigation transition | Medium | Appears in enter/exit/settings/GOAT contexts; not unique enough to name exact action. |
| `181C0F00#0160800000000000` marks classic programmer setting-change context | Medium | Repeats in speed tweak logs, but only one setting type tested. |
| `15000000#01000100` is a commit/ack/status marker | Medium | Repeats after programmer changes and in config-write context, but exact semantics unknown. |
| `1E80000F#` indicates GOAT dongle presence/idle/event | Medium | Repeats in GOAT logs, but not isolated with enough controls yet. |
| `20/21/22/23` seating function IDs generalize beyond Bumblebee2 | Low-medium | Strong on this chair/config, unknown cross-chair. |
| Classic programmer speed byte is displayed value in hex | High for tested values, medium generality | Very strong for 50-53; should test more values and another parameter. |

---

## 7. Current unresolved questions

1. **How config-dependent is the joystick stream ID?**  
   We need controlled pre/post config captures on the same chair and ideally across chairs.

2. **Do `02000200` and `02001100` have a stable role?**  
   They may be old joystick channels, module-specific streams, companion/status frames, or config-write/programmer-context artifacts.

3. **Can we decode GOAT dongle speed values?**  
   The action is visible, but the value encoding is not yet as obvious as classic programmer `78F#208C...`.

4. **What exactly do the config-write sections mean?**  
   We can see chunking/indexing, but not yet map sections to human settings or configuration pages.

5. **Are seating function codes stable across configurations and chairs?**  
   Current evidence is strong for post-config Bumblebee2 only.

6. **Can menu/settings mode be recognized without screen context?**  
   `181C0100#026...` is useful but too generic. We need better controls.

7. **How much of the earlier log contamination disappears with receive-queue flushing?**  
   The script now has a queue flush, but we need a new batch captured after the fix.

---

## 8. Recommended next test set

The next batch should prioritize clean capture boundaries and separating config-specific findings from chair-general findings.

### 8.1 First priority: validate clean logging

Run a short sanity series after the queue-flush update.

| Test | Goal |
| --- | --- |
| Baseline idle, 10 seconds | Confirm capture span matches requested span. |
| Repeat baseline idle, 10 seconds | Confirm no stale frames from previous capture. |
| Custom log, user stops early | Confirm `stop_reason` and timestamps behave as expected. |
| No-action control between actions | Confirm button/action frames do not leak into the next log. |

Acceptance criteria:

- first frame timestamp should be near capture start, not minutes earlier;
- `listen_seconds: 10` should produce around a 10-second CAN span unless user interrupted;
- flushed frame count should be logged;
- no old action frames should appear at the beginning of the next capture.

### 8.2 Run meet-and-greet on each meaningful config state

For each chair/config combination:

1. run full `rnet_meet_greet`;
2. save JSON + summary under a config-specific profile name;
3. do not merge findings across configs without labeling them.

Suggested profile names:

```text
bumblebee_pre_m3_config
bumblebee2_post_m3_like_config
chairname_factory_config
chairname_after_programmer_config
```

### 8.3 Re-run drive tests under clean logging

| Test | Notes |
| --- | --- |
| Profile 1, low speed, max forward/reverse/left/right | Repeat 3x. |
| Profile 2 or normal profile, high speed, max forward/reverse/left/right | Repeat 3x. |
| Gentle forward | Capture intermediate values. |
| Diagonal forward-right | Confirm mixed-axis behavior. |
| Gentle diagonal | Helpful for classifier thresholds. |

Goal:

- confirm raw joystick values still normalize near +/-100;
- document cross-axis drift;
- separate input magnitude from chair speed response.

### 8.4 Re-run seating tests under clean logging

| Test | Notes |
| --- | --- |
| Enter seating from drive | Repeat 3x; note selected function. |
| Idle in each seating function | Tilt/elevate/recline/legs, no joystick movement. |
| Cycle right one step | Each starting function if possible. |
| Cycle left one step | Each starting function if possible. |
| Cycle all functions both directions | Repeat existing best test. |
| More/less for each function | Repeat 3x per function. |
| Partial movement for each function | Especially helpful for analog thresholding. |

Goal:

- confirm `20/21/22/23` function mapping;
- confirm `0C180300` block boundaries;
- confirm Y more/less mapping by function;
- determine whether selected function is continuously broadcast or only during transitions.

### 8.5 Settings/menu tests

| Test | Control value |
| --- | --- |
| Long press into settings | From drive screen. |
| Short press same button | Should not enter settings; may toggle hazards. |
| Settings idle | No movement or button presses. |
| Navigate down/up | Note exact visible selection changes. |
| Select/confirm | Note warning dialogs. |
| Exit settings | Note exact route and visible screen. |

Goal:

- avoid overlabeling `181C0100#026...`;
- distinguish display transition, list navigation, selection, and exit.

### 8.6 Programmer speed/config tests

Keep these passive and well-labeled.

| Test | Notes |
| --- | --- |
| Classic programmer speed +1 only | Known starting value, one increment, stop. |
| Classic programmer speed -1 only | Known starting value, one decrement, stop. |
| GOAT dongle speed +1 only | Known starting value, one increment, stop. |
| GOAT dongle speed -1 only | Known starting value, one decrement, stop. |
| Repeat values across a wider range | Helps verify whether value bytes are decimal-in-hex. |
| Same method on another parameter | Tests whether `78F#208C...` is speed-specific or parameter-general. |

Avoid batching multiple increments unless the goal is to test step sequences. Single-step captures are much easier to interpret.

### 8.7 Whole-config write tests

Only if appropriate and safe for the research setup.

| Test | Notes |
| --- | --- |
| Successful write, longer capture | Start before confirm; continue through visible success. |
| Failed write, longer capture | Capture the same window for comparison. |
| No-write control | Sit on warning dialog or config screen without confirming. |
| Exact config label | Record source config and target chair/config. |

Goal:

- identify which frames are write-specific versus menu/status chatter;
- separate transfer phase, commit phase, and failure/success indication;
- avoid trying to name individual settings prematurely.

---

## 9. Candidate recognizer architecture

Keep recognizers layered and conservative.

Recommended layers:

1. **Capture quality layer**
   - detect stale/buffered traffic;
   - compute timestamp span;
   - compute frame rates and top IDs.

2. **Context layer**
   - chair/config profile name;
   - programmer attached/detached;
   - drive/seating/settings/off state.

3. **Input stream layer**
   - joystick ID;
   - center;
   - X/Y polarity;
   - range and deadzone.

4. **Action layer**
   - horn/lights;
   - seating selected function;
   - seating more/less;
   - settings/menu transitions.

5. **Caution layer**
   - candidate vs confirmed;
   - chair-specific vs cross-chair;
   - config-specific vs stable.

Example passive labels:

| Better label | Avoid overconfident label |
| --- | --- |
| `candidate_display_menu_navigation_transition` | `confirmed_enter_settings` |
| `candidate_programmer_attached_startup_signature` | `programmer protocol decoded` |
| `candidate_chunked_config_page_write` | `settings written individually` |
| `bumblebee2_selected_seating_function_code_22` | `universal tilt command` |

---

## 10. Current working confidence

| Area | Current confidence | Notes |
| --- | --- | --- |
| Post-config joystick ID `02000100` | High | Confirmed by meet-and-greet and later drive/seating logs. |
| Horn/lights IDs | High | Strong repeated meet-and-greet evidence. |
| Seating selected-function codes on Bumblebee2 | High | Cycle test strongly matches human-noted order. |
| Seating analog joystick values | High | Partial movement logs show intermediate values. |
| Profile speed does not change raw joystick range | Medium-high | Multiple tests suggest normalized input; repeat after clean logging. |
| Settings/menu transition frame | Medium | Useful but not action-specific. |
| Classic programmer speed value encoding | High for 50-53, medium generality | Needs more values and maybe other parameters. |
| GOAT dongle speed value encoding | Low | Traffic family visible, value unclear. |
| Whole-config write is chunked sections/pages | Medium-high | Strong shape, unknown section meanings. |
| Cross-chair generality | Low | Most findings are still Bumblebee/Bumblebee2-specific. |

---

## 11. Sharing note for the team

The key research update is not just a new list of frame IDs. The more important progress is methodological:

> We now have the beginnings of a passive per-chair/per-config profiling workflow. A config write can change which joystick stream is active, so each chair/config should be profiled before we interpret custom logs from it.

For the next phase, `rnet_meet_greet` should become the standard pre-test handshake for any new chair or newly written config. Custom logs can then be interpreted against that current profile, rather than against assumptions from a previous chair/config.

