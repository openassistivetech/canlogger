% CANLOGGER(7) canlogger | Miscellaneous Information Manual
%
% July 2026

# NAME

canlogger - CAN bus logger with session detection and cloud upload

# DESCRIPTION

**canlogger** captures **candump(1)** output from *can0* into time-bucketed,
zstd-compressed segments with session detection (silence ends a session,
traffic starts a new one) and ships finished segments to a configurable
**rclone(1)** remote.

# DOCUMENTATION

Full documentation, including setup, environment knobs, log reassembly,
troubleshooting, and package build instructions, is installed at:

    /usr/share/doc/canlogger/README.md

View it with:

    less /usr/share/doc/canlogger/README.md

# COMPONENTS

/usr/bin/canlog-rotate
:   Reads `candump -L can0` on stdin, writes rotated zstd-compressed segments.

/usr/bin/canlog-upload
:   Drains finished segments to a remote via rclone; two-phase upload/prune.

canlogger.service
:   Systemd unit that runs the capture pipeline. Enabled on install.

canlog-upload.service, canlog-upload.timer
:   Systemd unit + timer for the uploader. Installed but NOT enabled by
    default; enable manually after configuring rclone.

# SEE ALSO

**canlog-rotate(1)**, **canlog-upload(1)**, **candump(1)**, **rclone(1)**,
**systemctl(1)**, **journalctl(1)**
