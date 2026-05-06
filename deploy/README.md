# Deploy: bind Clawson's lifecycle to the desk PC

Goal: when the desk PC powers on, Clawson is already up (or comes up)
on the robot. When the PC is off, Clawson goes to sleep pose and stops
burning OpenAI tokens. No manual SSH, no manual `start_claw.sh`.

Architecture: always-on watcher controls a fully-stopping main service.

```
desk PC (Windows)                       Reachy Mini (Linux)
┌─────────────────────────┐             ┌────────────────────────────────┐
│  OpenClaw gateway       │ ◀── ws ───  │  clawson-watcher.service       │
│  (Scheduled Task,       │             │   probes PC every 10s          │
│   at logon)             │             │   │                            │
└─────────────────────────┘             │   ├─ host online + clawson off │
        ▲                               │   │   → systemctl start clawson│
        │ Tailscale                     │   │                            │
        ▼                               │   └─ host off ≥60s + active    │
                                        │       → systemctl stop clawson │
                                        │                                │
                                        │  clawson.service (transient)   │
                                        │   on SIGTERM: bow head to      │
                                        │   SDK off-pose, then exit      │
                                        └────────────────────────────────┘
```

When the desk PC is off, the clawson process is fully stopped — no
realtime session, no pollers, no idle CPU. The robot itself stays
powered with its head bowed in the SDK's canonical off pose.

Set up in two halves: robot first, then PC.

---

## 1) Robot — install the systemd unit

Replaces the dev-time `/tmp/start_claw.sh`. Survives reboots, restarts on
crash, logs to `/var/log/clawson/clawson.log` and journald.

```bash
# from your laptop, with the clawson repo checked out:
tailscale ssh pollen@reachy-mini "cd ~/clawbody && git pull --ff-only clawson main"

# main service (transient — watcher will start/stop it):
tailscale ssh pollen@reachy-mini "sudo bash -s" < deploy/install-systemd.sh

# watcher (always-on; owns clawson lifecycle based on host presence):
tailscale ssh pollen@reachy-mini "sudo bash -s" < deploy/install-watcher.sh
tailscale ssh pollen@reachy-mini "sudo systemctl start clawson-watcher"
```

The watcher will start clawson within ~10 s if the desk PC is reachable.

Verify:

```bash
tailscale ssh pollen@reachy-mini "systemctl is-active clawson; tail -n 20 /var/log/clawson/clawson.log"
```

What you'll see when the PC is offline (and that's correct):

```
[INFO] reachy_mini_openclaw.host_presence: host probe failed; grace period begins (60s)
[INFO] reachy_mini_openclaw.host_presence: host presence: unknown → offline
[INFO] reachy_mini_openclaw.main: host offline: pausing realtime + entering sleep pose
[INFO] reachy_mini_openclaw.focus.sleep_animator: sleep animator: entering sleep (host offline)
```

---

## 2) Windows desk PC — autostart OpenClaw at logon

From an **elevated PowerShell** (right-click → Run as Administrator):

```powershell
cd path\to\clawson\deploy\windows
.\Install-OpenClaw-Autostart.ps1 -OpenClawCommand "C:\Users\<you>\AppData\Local\Programs\openclaw\openclaw.exe"
```

(Adjust the path to where OpenClaw is actually installed.)

This registers a Scheduled Task named "OpenClaw Gateway" that:
- starts at any user logon,
- restarts on crash (5 retries, 1 min backoff),
- survives reboots.

Test without logging out:

```powershell
Start-ScheduledTask -TaskName "OpenClaw Gateway"
Get-ScheduledTask -TaskName "OpenClaw Gateway" | Get-ScheduledTaskInfo
```

---

## 3) Point the robot at the desk PC

Once Tailscale is installed on the desk PC and signed into the same
tailnet:

```powershell
tailscale status      # find this machine's Tailscale name
```

Note the FQDN — something like `desk-pc.tail476f78.ts.net`.

Then on the robot:

```bash
tailscale ssh pollen@reachy-mini "sed -i 's|^OPENCLAW_GATEWAY_URL=.*|OPENCLAW_GATEWAY_URL=ws://desk-pc.tail476f78.ts.net:18789|' ~/clawbody/.env"
tailscale ssh pollen@reachy-mini "sudo systemctl restart clawson"
```

Within ~10 s the widget at http://reachy-mini:7860 should show
`host: online` and Clawson will wake from the sleep pose.

---

## How it behaves

| State                | clawson process | OpenAI realtime | Pollers | Robot pose |
|----------------------|-----------------|-----------------|---------|------------|
| desk PC online       | running         | active          | active  | normal     |
| desk PC offline ≥60s | stopped         | n/a             | n/a     | SDK off-pose (head bowed, antennas back) |
| desk PC reappears    | restarted       | reconnects      | resume  | wake animation |

Notes:
- When paused, the mic still listens. Voice commands like "wake up",
  "deep mode", "sleep", "what mode", etc. all still work — they're
  resolved locally, not via the LLM. Conversational chat is what's
  paused.
- Pollers run from the robot directly with their own PATs, so events
  keep accumulating in the queue while the PC is off. They surface on
  the next standup or rollup once you're back.
- The widget shows a `host: online` / `host: offline` pill in the top
  right. When offline, a "Reconnect host" button appears that forces
  an immediate probe (skips the 10 s poll cadence).

---

## Troubleshooting

**Widget shows `host: offline` even though the PC is on.**
Check from the robot whether it can reach the gateway port:

```bash
tailscale ssh pollen@reachy-mini "nc -zv <desk-pc>.tail<net>.ts.net 18789"
```

If `nc` fails, either Tailscale isn't running on the PC, the OpenClaw
task didn't start, or Windows Defender Firewall is blocking inbound
on 18789.

**Clawson doesn't restart after a crash.**
Check the systemd unit state:

```bash
tailscale ssh pollen@reachy-mini "systemctl status clawson; journalctl -u clawson --since='5 min ago'"
```

If it's in `failed` state with `start-limit-hit`, Clawson crashed >10
times in 5 min. Reset with `sudo systemctl reset-failed clawson` and
investigate the underlying error in the log.

**OpenClaw task fires but immediately exits.**
Run the command manually in a regular PowerShell to see the actual
error — Task Scheduler swallows stdout. Once it works manually, the
scheduled task will work too.
