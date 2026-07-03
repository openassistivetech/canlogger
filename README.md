# CAN bus logging pipeline (goatrnet)

Continuously captures `candump` output from `can0`, writes it in time-bucketed
segments, compresses each segment with **zstd**, and uploads finished segments
to a private cloud remote. The capture is **lossless** — every frame, including
heartbeats, is kept; redundancy is collapsed by compression, not by dropping data.

```
candump -L can0  ──►  canlog-rotate  ──►  /var/spool/canlog/*.zst  ──►  rclone  ──►  cloud
                      (per-session segments,  (finished segments,           (canlog-upload,
                       zstd-compressed)        on the SD card)               every 10 min)
```

## Sessions

The wheelchair's CAN bus is silent while the chair is powered off. `candump`
runs **continuously regardless**, so a power-on handshake is never missed — but
the rotator does not waste files on silence:

- A segment opens **lazily**, when the first frame of activity arrives.
- After `CAN_IDLE` seconds (default 10) of silence, the segment closes and no
  new one opens until traffic resumes. That silence is a **session boundary**.
- A session lasting longer than `CAN_WINDOW` seconds (default 300) is split
  into multiple segments — purely a size cap; the session continues.

Each segment's filename records **why it closed**, so a gap between segments is
never ambiguous:

| reason | meaning                                                              |
|--------|----------------------------------------------------------------------|
| `idle` | bus went silent — the session ended here                             |
| `cont` | hit the `CAN_WINDOW` size cap — the *same* session continues next seg |
| `stop` | the logger was stopped (service restart/shutdown) mid-session        |

So a time gap *after* an `idle` or `stop` segment is an expected session
boundary (the chair was off). A gap with no such marker would indicate a
genuinely missing file.

## Components

| File                   | Role                                                        |
|------------------------|-------------------------------------------------------------|
| `canlog-rotate`     | Reads `candump -L`, writes/rotates/compresses segments      |
| `canlogger.service`    | Runs the capture pipeline under systemd                     |
| `canlog-upload`     | Drains the spool to the cloud via rclone (verified delete)  |
| `canlog-upload.service`| Oneshot unit that runs the uploader                         |
| `canlog-upload.timer`  | Triggers the uploader every 10 minutes                      |

### Directory layout

- **Hot file** — `/run/canlog/` (tmpfs / RAM). The currently-growing segment.
  Lives in RAM to spare the SD card from ~140 writes/sec. Lost on power cut,
  but that's only ever the last ≤5 minutes of an active session.
- **Spool** — `/var/spool/canlog/` (SD card). Finished `.zst` segments waiting
  to upload. On the card so they survive a power cut. Published atomically, so
  the uploader never sees a half-written file.

Segment filenames are `session-<host>-<UTC timestamp>-<reason>.log.zst` (e.g.
`session-goatrnet2-20260525T185437Z-idle.log.zst`). The host tag — the Pi's
hostname, or `CANLOG_HOST` if set — keeps a file self-identifying after it
leaves the Pi and prevents filename collisions when several Pis upload to the
same bucket. The timestamp is when the segment *opened* (UTC).

## One-time setup

### 1. Install the package

Easiest path: build the `.deb` (see *Building the Debian package* at the end of
this README) and install it on the Pi:

```bash
sudo apt install ./canlogger_0.1.0-1_all.deb     # apt resolves runtime deps
```

What that gets you:

- Scripts in `/usr/bin/` (rotator + uploader).
- Systemd units in `/usr/lib/systemd/system/`.
- This README in `/usr/share/doc/canlogger/`.
- **`canlogger.service` enabled and started immediately** — capture begins.
- **`canlog-upload.timer` installed but NOT enabled** — you turn it on after
  configuring rclone in step 2, since uploads can't go anywhere until then.

The runtime dependencies (`can-utils`, `zstd`, `python3`, `rclone`, `bash`) are
declared in the package and pulled in by apt automatically.

> **Manual install fallback** — if you'd rather not build the package, copy
> `canlog-rotate` and `canlog-upload` to `/usr/bin/` (chmod +x),
> copy the three unit files to `/etc/systemd/system/`, `daemon-reload`, then
> `systemctl enable --now canlogger.service`. The package automates exactly
> this; behavior is identical.

### 2. Configure rclone (required before enabling the uploader)

```bash
sudo rclone config             # MUST be sudo — see "rclone config location" below
```

In `rclone config`, create a remote **named `canremote`** (the scripts default
to this name; change `CANLOG_REMOTE` in `canlog-upload.service` if you pick
another).

**Google Drive** — note it's a *headless* Pi, so when `rclone config` asks
"Use auto config?" answer **No**. It prints a command; run that command on your
**Mac** (which has a browser), complete the Google login, and paste the
resulting token back into the Pi. This is the `rclone authorize` flow — fiddly
once, then permanent. Consider creating a dedicated folder (e.g. `can-logs`) so
the remote path is `canremote:can-logs`.

> **Use `drive.file` scope.** When `rclone config` asks for the Drive `scope`,
> choose `drive.file` rather than full `drive`. This scope limits the token to
> files **rclone itself created** — so a compromised Pi or stolen token cannot
> read or overwrite anything else in the Drive. It could still write *new*
> files elsewhere, but it cannot see or damage pre-existing data. It's a
> capability boundary, not an identity one: revoking it means revoking the
> rclone app for the whole account (acceptable at our scale of 2–3 Pis). One
> consequence — because the token only sees rclone-created files, a `can-logs`
> folder you make by hand in the Drive web UI is invisible to rclone; let
> rclone create its own folder instead.

**Backblaze B2 (no OAuth, easier headless)** — if you'd rather skip the token
dance: create a bucket and an application key in the B2 web console, choose
`b2` as the remote type, and paste the key ID + key. Done.

Either way, test before relying on it — and test **as root**, since that is who
the timer runs as:

```bash
sudo rclone lsd canremote:                                              # lists folders/buckets
echo hi | sudo rclone rcat canremote:goat-rnet-logs/_test.txt && sudo rclone delete canremote:goat-rnet-logs/_test.txt
```

### 3. Enable the uploader timer

```bash
sudo systemctl enable --now canlog-upload.timer
systemctl list-timers canlog-upload.timer      # confirm it's scheduled
journalctl -u canlog-upload.service -f         # watch a run
```

The timer fires the uploader every 10 minutes. To **trigger a run immediately**
(without waiting for the next tick — useful for confirming a fresh config, or
to drain the spool on demand):

```bash
sudo systemctl start canlog-upload.service     # runs once, in the foreground
                                                # of systemd; watch with journalctl
```

This works whether or not the timer is enabled — the service is the actual
work unit, the timer just schedules it. Safe to run alongside a timer-fired
run: the `flock` guard inside the script makes concurrent invocations skip
rather than collide.

> **rclone config location:** `rclone config` writes `~/.config/rclone/rclone.conf`
> for whoever runs it. The timer runs the uploader as **root**, so `rclone config`
> is run with **`sudo`** above — the config lands in `/root/.config/rclone/rclone.conf`,
> which is exactly where `RCLONE_CONFIG=` in `canlog-upload.service` already points.
> No edit to the unit is needed. The trade-off: rclone commands you run by hand to
> inspect the remote must also be prefixed with `sudo`, or they won't see the config.

## Tunable settings

Set as `Environment=` lines in the systemd units.

| Variable             | Default              | Meaning                                  |
|----------------------|----------------------|------------------------------------------|
| `CAN_WINDOW`         | `300`                | Max seconds per segment (size cap)       |
| `CAN_IDLE`           | `10`                 | Silence (s) that ends a session          |
| `CAN_ZSTD_LEVEL`     | `19`                 | zstd level (1–19); 19 = best ratio       |
| `CAN_STAGE`          | `/run/canlog`        | Hot-file dir (keep on tmpfs)             |
| `CAN_SPOOL`          | `/var/spool/canlog`  | Finished-segment dir (keep on SD)        |
| `CANLOG_HOST`        | system hostname      | Host tag in segment filenames            |
| `CANLOG_REMOTE`      | `canremote:goat-rnet-logs` | rclone destination                       |
| `CANLOG_MIN_FREE_MB` | `2048`               | Warn in the journal below this free space|
| `CANLOG_LOCAL_RETAIN`| `14d`                | Keep local copies at least this long after upload |

## Data safety notes

- **Two-phase upload/prune.** The uploader runs in two phases: first `rclone
  copy` uploads any new segments and checksum-verifies them, leaving locals in
  place; then `rclone check` identifies which locals are older than
  `CANLOG_LOCAL_RETAIN` *and* confirmed present on the remote, and only those
  get deleted. A segment that never uploaded successfully (network down for
  weeks, auth broke, etc.) is never deleted — it stays local until an upload
  eventually succeeds. This gives you a working grace period where fresh
  segments are still on disk for local inspection, without giving up the
  "nothing deleted unconfirmed" guarantee.
- **Backlog is bounded by the SD card.** If the network is down for a long
  time, segments accumulate at ~58 MB/day (at the measured frame rate). The
  50 GB free buys well over a year. `CANLOG_MIN_FREE_MB` logs a warning when
  free space gets low — check `journalctl -u canlog-upload` if you suspect a
  stuck uploader. Logging itself never stops.
- **Clock caveat (no RTC fitted).** Timestamps are wall-clock (`CLOCK_REALTIME`).
  On a network-less cold boot the clock is wrong until NTP syncs, then jumps.
  A segment spanning that jump is still lossless but contains one large forward
  time delta. **When parsing, treat a non-monotonic inter-frame delta as a clock
  step, not a bus event** — and if you need true wall-clock time for pre-jump
  frames, re-derive it by counting backward from the first post-jump frame.

---

# Reading the logs: re-merging segmented .zst files

Each `.zst` is one independent time-ordered segment in **candump log format**:

```
(1779730174.490373) can0 00E#B44121B800000000
```

i.e. `(epoch.microseconds) interface ID#DATA`. `R` after the data marks a
remote-transmission-request frame; an ID with no `#data` is a zero-length frame.

Filenames are `session-<host>-YYYYMMDDTHHMMSSZ-<reason>.log.zst` (UTC), and
because the timestamp is fixed-width they **sort chronologically** as plain
text — a glob expands in time order. With logs from several Pis in one place,
glob per host (`session-goatrnet2-*.log.zst`) so you merge one machine's
stream at a time; segments from different Pis are independent captures and
should not be interleaved.

### Segments vs. sessions

Unlike a naive time-sliced log, these segments are **not** contiguous in time —
there are real gaps wherever the chair was powered off. The `<reason>` suffix
(see the Sessions table above) tells you which gaps are boundaries:

- A run of consecutive segments ending in `cont`, `cont`, …, then one `idle`
  (or `stop`) is **one session**. Concatenate that run to rebuild the session.
- A gap *after* an `idle`/`stop` segment is the chair being off — expected.
- A gap that is *not* preceded by an `idle`/`stop` marker means a segment is
  genuinely missing (failed upload, deleted file) — worth investigating.

### A `cont` segment with no successor

Normally a `cont` segment is followed by more segments of the same session.
A `cont` that is the *last* file with no time-adjacent successor has two
possible causes:

1. **The session ended exactly at a `CAN_WINDOW` boundary** — the size cap
   fired, the segment closed `cont`, and the bus then went quiet before the
   next segment opened. Rare, harmless; the timestamp gap in the data confirms
   it.
2. **The Pi lost power, or the rotator was `SIGKILL`ed, mid-session** — there
   was no chance to write a closing `idle`/`stop` segment, and the in-progress
   segment (in tmpfs) was lost with it. Bounded by `CAN_WINDOW`: at most the
   last ~5 minutes of that session.

A *normal chair power-off does NOT produce this* — it produces a clean `idle`
segment, because the Pi runs on its own independent power supply and keeps
logging (the bus simply goes quiet). So a `cont`-with-no-successor specifically
indicates **the Pi** went down, not the chair.

> **Topology assumption:** the above holds while the Pi is on its own power
> supply, separate from the wheelchair. If the Pi is ever moved to an SMPS fed
> from the R-net 24 V bus, this distinction must be re-verified — measure
> whether the 24 V rail (and thus the Pi) stays energised when the chair is
> switched off. Only if it does will `idle` (chair off) and `cont`-no-successor
> (power lost) remain distinguishable.

## Decompress and concatenate into one log

```bash
# Everything from one Pi -> one candump log (gaps between sessions included):
zstdcat session-goatrnet2-*.log.zst > merged.log

# A specific time range — filenames carry a UTC timestamp, so glob the window:
zstdcat session-goatrnet2-20260525T18*.log.zst > afternoon.log

# Just one session — glob its opening timestamp through the closing 'idle':
zstdcat session-goatrnet2-20260525T1854*.log.zst > one-session.log
```

`zstdcat` is just `zstd -dc`. Because every segment is a complete, independent
stream, concatenating their *decompressed* output is all the "merging" needed —
there is no shared dictionary or cross-segment state to reconstruct.

## Pipe straight into a reader (no intermediate file)

```bash
# Wireshark/tshark read the candump text log directly — no conversion needed.
# Decompress to a file and open it:
zstdcat session-goatrnet2-*.log.zst > merged.log      # then File > Open in Wireshark
# or pipe straight in:
zstdcat session-goatrnet2-*.log.zst | wireshark -k -i -

# Replay onto a (virtual) CAN interface:
zstdcat session-goatrnet2-*.log.zst | canplayer -I -  # needs vcan0/can0 up

# grep / awk a single CAN ID across the whole capture without unpacking to disk:
zstdcat session-goatrnet2-*.log.zst | grep ' 00E#'
```

> **Note on Wireshark:** Wireshark understands the candump log format natively —
> just open a decompressed `merged.log` (or pipe it in). No `log2pcap` or pcap
> conversion step is required. For quick text inspection, `> merged.log` plus
> `grep`/`awk` is often faster anyway.

## Integrity check before trusting a batch

```bash
zstd -t session-goatrnet2-*.log.zst  # verifies every segment's checksum; silent = OK
```

## Sanity-checking time continuity

Within a single session, frames are continuous. *Across* sessions there are
real gaps (chair powered off) — those are expected, not errors. This scan
flags clock jumps and reports gaps, which you then read against the segment
boundaries (a gap at a session boundary is fine; one mid-session is not):

```bash
zstdcat session-goatrnet2-*.log.zst | awk '
  { t = $1; gsub(/[()]/,"",t); t += 0 }
  NR>1 && t < prev   { printf "clock step at line %d: %.6f -> %.6f\n", NR, prev, t }
  NR>1 && t-prev > 1 { printf "gap at line %d: %.3f s\n", NR, t-prev }
  { prev = t }'
```

A backward step is the NTP cold-boot jump described above. A large positive gap
is normally a session boundary (chair powered off) — cross-check it against the
segment filenames: if the gap falls between an `idle`/`stop` segment and the
next session it is expected; if it falls *inside* a run of `cont` segments,
a segment is missing.

---

# Building the Debian package

The source tree is a standard Debian source package: `debian/` directory plus
the files it installs. Building produces a single `.deb` you can drop on any
Pi (or other Debian-derivative system) running an `arm64`/`armhf` userland —
the package is architecture-`all` since everything in it is a script.

## Build host requirements

Tooling (one-time, on whatever host you build from):

```bash
sudo apt install build-essential debhelper dpkg-dev pandoc
```

You need a Debian/Ubuntu-style Linux for this — the Debian build tools
(`dpkg-buildpackage`, `debhelper`, `lintian`) don't exist on macOS and aren't
straightforward to install there. In order of least friction:

- **Build on the Pi itself.** Zero setup: the Pi already runs Raspberry Pi OS.
  `apt install` the tooling above and build in place. dpkg-buildpackage on a
  Pi isn't fast in general, but this package is tiny (a few scripts, one
  pandoc invocation), so a full build takes only tens of seconds. Simplest
  option, and it puts the artifacts right where you'll test them.
- **Build in Docker on macOS.** `docker run --rm -v "$PWD":/src debian:bookworm`,
  install the build deps inside, run `dpkg-buildpackage -us -uc -b`. The
  package is architecture-`all` (pure scripts, no compiled code), so an Apple
  Silicon Mac building a Debian package for an ARM Pi is fine — no
  cross-compilation concerns.
- **Build on any Debian/Ubuntu box, then `scp` the `.deb` to the Pi.** Fastest
  if you already have such a machine handy.

## Build

From the directory containing this README (i.e. the package source root):

```bash
dpkg-buildpackage -us -uc -b
```

Flags:
- `-us -uc` — don't sign the source/changes (no GPG key required).
- `-b` — binary-only build; doesn't try to produce a source tarball.

Output lands in the **parent directory** (Debian convention):

```
../canlogger_0.1.0-1_all.deb
../canlogger_0.1.0-1_amd64.buildinfo
../canlogger_0.1.0-1_amd64.changes
```

The `.deb` is what you ship; the others are build metadata.

## Install on the Pi

If you built on a separate machine, copy the `.deb` over first:

```bash
scp ../canlogger_0.1.5-1_all.deb pi@goatrnet.local:/tmp/
ssh pi@goatrnet.local
sudo apt install /tmp/canlogger_0.1.5-1_all.deb
```

If you built on the Pi itself, the `.deb` is already there — just install
it directly:

```bash
sudo apt install ../canlogger_0.1.5-1_all.deb
```

Using `apt install` (not `dpkg -i`) means apt will resolve and fetch any
missing runtime dependencies. On install you should see `canlogger.service`
get enabled and started; verify with `systemctl status canlogger.service`.

## Iterating

Bump the version in `debian/changelog` (use `dch -i` if you have devscripts
installed, or edit by hand following the existing entry's format) and rebuild.
Reinstalling the same version with `apt install --reinstall` also works for
quick iteration without bumping.

To clean build artifacts:

```bash
dh clean    # or: rm -rf debian/canlogger debian/.debhelper debian/*.log debian/*.substvars
```

## Known policy violations (lintian-not-clean)

This package is deliberately not fully lintian-clean. The remaining issues:

- **No upstream changelog** distinct from the Debian changelog.

None of these affect the package working correctly; they would matter only if
this were ever to go into a real Debian repository.
