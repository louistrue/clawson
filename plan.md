# Clawson — Personal AI Coding/Work Buddy

The canonical plan for this fork. Phase 0 (fork & boot) is in progress; the
brainstorm history lives on branch `claude/reachy-mini-app-ideas-Vd3lP` of
`louistrue/reachy_mini`.

---

## Vision

Extend [tomrikert/clawbody](https://github.com/tomrikert/clawbody) into a
personal embodied assistant that lives on your desk, monitors your work
surfaces, and reacts physically + vocally so you stay in flow without missing
what matters.

**Tagline:** A desk familiar that nods on green CI, slumps on red, and reads
your morning standup aloud while you sip coffee.

## One-line summary

Reachy Mini Wireless + clawbody fork + MCPs (GitHub, Vercel, …)
+ focus modes + gesture-mapped notifications + morning standup TTS.

---

## Decisions locked in

| Decision | Choice |
|---|---|
| Runtime | Laptop alongside the robot (full compute, easy iteration; robot connects over WiFi) |
| v1 integrations | GitHub + Vercel (Todoist / Gmail come later) |
| Voice stack | Keep clawbody's OpenAI Realtime (already wired, sub-second latency, supports interruption) |
| Fork or contribute | Fork — personal config and v1 features may not be upstream-shaped; cherry-pick general bits later |

---

## Foundation (already in clawbody — we don't rebuild this)

- OpenAI Realtime voice loop (mic → STT → LLM → TTS → speaker)
- OpenClaw gateway with MCP support
- Reachy Mini SDK wiring (head, antennas, body)
- Face tracking
- Camera vision
- Gradio web UI (optional, useful for debugging)
- Recorded emotion library

## What we add (the fork's contribution)

```
clawson/                              # additions in the fork
├─ mcp_clients/                       # tool adapters
│   ├─ github.py
│   └─ vercel.py
├─ briefing/                          # cross-source aggregation
│   ├─ poller.py                      # async loops per source
│   ├─ filters.py                     # "what counts as signal"
│   ├─ standup.py                     # morning rollup
│   └─ events.py                      # normalized Event type
├─ focus/                             # state machine
│   ├─ modes.py                       # deep / normal / available / snoozed
│   └─ antenna_handler.py             # physical input (right = cycle, left = snooze)
├─ gestures/                          # source → motion mapping
│   └─ vocabulary.py
├─ persona/                           # speech style / TTS system prompts
│   └─ prompts.py
└─ config/
    └─ schema.py                      # validates ~/.config/clawson/config.toml
```

---

## v1 scope — read-only + snooze

We earn trust before we earn write access.

### Sources

- **GitHub**
  - PRs I authored (status, new comments, review state changes)
  - PRs where I'm a requested reviewer
  - @mentions on issues/PRs
  - CI status on branches I pushed in the last 24h
  - Issues newly assigned to me
- **Vercel**
  - Deploys on watched projects
  - Build failures (state transition only — not every "queued" → "building" → "ready")
  - Preview URLs for new PR deployments

### Focus modes (cycled via right antenna press)

| Mode | Behavior |
|---|---|
| `deep` | Silent. Queue everything. Single soft chirp on **critical** only (CI red on default branch, prod deploy fail). |
| `normal` *(default)* | Filtered events → audio cue + gesture; optional one-line TTS preview. |
| `available` | Same as `normal` but always speaks the full summary on arrival. |
| `snoozed(until)` | Silent for N minutes. Auto-returns to previous mode when timer expires. |

**Antenna controls:**
- Right antenna single tap → cycle mode (deep → normal → available → deep)
- Left antenna single tap → snooze 15min · double → 1hr · hold (>1s) → 4hr
- Both antennas tap → "what's queued?" — TTS rollup of suppressed events

### Per-source filters (the alert-fatigue defense)

- **GitHub PRs:** only PRs where I'm author or requested reviewer; only on status change or new non-bot comment
- **GitHub CI:** only on branches I pushed in the last 24h; mute green-after-green; never repeat same red within 5 min
- **GitHub mentions:** always pass (high signal)
- **Vercel:** only watched projects; only state transitions to terminal state (ready / error); skip per-step build progress

### Gesture vocabulary (motion-as-source-id)

Pair every gesture with a 200ms audio cue so you know what arrived without looking up.

| Source / event | Reaction |
|---|---|
| GitHub PR mention | Head tilt right + curious antenna flick |
| GitHub review requested on me | Head tilt left + small nod |
| GitHub CI fail | Head down, slow body shake, low chirp |
| GitHub CI pass *(after a fail)* | Happy bounce + antenna wiggle |
| GitHub PR merged (mine) | Full-body celebration (recorded `happy` move) |
| Vercel deploy success | Quick nod, head right |
| Vercel deploy fail | Head droop, low chirp, longer than CI fail (rarer event) |
| Anything in `deep` *(critical only)* | Tiny antenna wiggle, no sound, no head movement |

### Morning standup (default 07:30 local, weekdays)

Trigger: scheduled time **or** user pats robot's head (cv face-detect → gesture).

TTS rollup, ~30 seconds:
- Overnight CI status across watched repos (red → details, green → count)
- PRs awaiting your review
- PRs of yours awaiting review
- Vercel deploys overnight (success/fail counts; failures get name + link)
- *(Stretch)* weather + first calendar event via existing OpenClaw tools

Ends with a brief listening window for a follow-up question ("what's the first failure about?").

---

## Data flow

```
   poller.py (per source) ──► Event{source, kind, severity, summary, link, ts}
                                    │
                                    ▼
                              filters.py  (allow / mute)
                                    │
                                    ▼
                          focus.modes  (current-mode gate)
                                    │
                       ┌────────────┴────────────┐
                       ▼                         ▼
                 gesture queue            tts queue (if not `deep`)
                       │                         │
                       ▼                         ▼
                 reachy SDK                OpenAI Realtime
```

Single event bus, debounced 500ms to avoid robot thrashing on bursts.
Snooze state persists to disk so it survives restarts.

---

## Config

Path: `~/.config/clawson/config.toml` *(gitignored, never committed)*

```toml
[github]
token = "ghp_…"
watch_mode = "all_member_repos"     # discovered via /user/repos at startup
refresh_repos_interval_minutes = 60
poll_interval_seconds = 30

[vercel]
token = "…"
# personal account — no team_id
watched_projects = []               # empty = all personal projects
poll_interval_seconds = 60

[focus]
default_mode = "normal"
active_hours = ["09:00", "18:00"]   # robot reacts to events only in this window
                                    # outside: silent (standup is the one exception)
standup_time = "07:30"
standup_days = ["mon", "tue", "wed", "thu", "fri"]
standup_triggers = ["scheduled", "face_detect", "widget", "voice"]

[voice]
provider = "openai_realtime"
persona = "concise_warm"            # short sentences, dry-but-not-cold, small-robot energy

[widget]
host = "127.0.0.1"
port = 7860
mode = "browser_only"               # bookmark http://localhost:7860/widget
```

---

## Desktop widget (laptop control surface)

Antennas alone aren't enough — you want quick visual access to mode, queued
events, and triggers from the laptop without breaking flow.

**Surface:**
- Current focus mode (deep / normal / available / snoozed-until-HH:MM)
- One-click snooze: 15m · 1h · 4h · until tomorrow
- "Trigger standup now" button
- Queued events count + expandable list (when `deep` is suppressing things)
- Repo / project quick-mute toggles
- Clawson status indicator (connected to robot · MCP servers green)
- Optional: live face-track preview thumbnail

**Implementation:**
- Backend: a small FastAPI module inside the clawson process exposing
  `/widget/state`, `/widget/mode`, `/widget/snooze`, `/widget/standup`, `/widget/queue`,
  `/widget/mute`. Same process as the briefing engine — shared state, no IPC.
- Frontend: a single static HTML + vanilla JS panel served at the same origin
  (`http://localhost:7860/widget`). Tailwind via CDN; keep it under 200 lines.
  Auto-refresh state via SSE so mode changes from antenna press are reflected
  live.
- No native wrapper — user bookmarks the URL and keeps the tab open.

**Talks to the robot indirectly:** widget sends commands to the same event bus
the antenna handler uses. Antenna press and widget click are equivalent inputs.

---

## Build phases

| Phase | Goal | Acceptance test |
|---|---|---|
| **0 — Fork & boot** | Fork clawbody, run on laptop, point at robot, confirm voice + face track work | "Hey Clawson" → robot replies, tracks face |
| **1 — Focus modes + antenna input** | State machine + antenna handler; no MCPs yet | Tap right antenna 3× → cycles modes; tap left → snooze + spoken confirmation |
| **2 — GitHub MCP + filters + gestures** | Poller, normalized events, gesture+audio cues; no TTS preview yet | Push a failing CI run → robot droops within 30s |
| **3 — TTS preview + standup** | Per-mode TTS gating, morning standup rollup | At 07:30 robot wakes me up with overnight rollup |
| **4 — Vercel MCP** | Second source proves the abstraction | Trigger a Vercel deploy fail → distinct gesture + cue |
| **5 — Desktop widget** | Tray/popover panel; mode + snooze + standup-now + queue | Click "snooze 1h" in widget → robot goes quiet, returns at the right time |
| **6 — Polish** | Persona prompts, debounce tuning, error recovery, token-refresh, "what's queued?" command | Yank wifi 30s → graceful re-poll, no missed events |

---

## v2 (future, after v1 is trusted)

- **Action mode**: write tools (close issue, comment on PR, redeploy, mark task done) gated by nod-or-antenna confirm
- **More MCPs**: Todoist, Gmail (Superhuman has no public API — would proxy via Gmail with Superhuman-style filters), Linear, Calendar, Notion
- **Wake word** ("hey Clawson") for hands-free trigger
- **Context awareness**: face-track to know if you're at the desk → auto-snooze when you leave; auto-resume when you return
- **Persistent event log + replay**: "what did I miss in the last hour?"
- **Inter-event narration**: chain related events ("your CI just went green and Vercel deployed the preview at …")

---

## Open questions

1. **Repo allowlist for GitHub v1** — list specific repos, or "all repos I'm a member of"?
   - **Answer: all repos I'm a member of.**
   - *Implication:* poll `/user/repos` once at startup to discover the list, refresh hourly. Watch out for rate-limit if the count is high (>100); if so, switch to event-stream API.

2. **Vercel scope** — personal account or a team?
   - **Answer: personal account.** `team_id` omitted from config.

3. **Standup time + days** — default `07:30 weekdays`. Change?
   - **Answer: ok as default.**

4. **Quiet hours** — default `22:00 → 07:30`. Change?
   - **Answer: invert it — robot is active 09:00 → 18:00. Outside that window, silent except for the morning standup.**
   - *Implication:* config field renamed from `quiet_hours` to `active_hours = ["09:00", "18:00"]`. Standup at 07:30 is a special-cased proactive break in the silence (like an alarm); after the standup the robot returns to quiet until 09:00.

5. **Persona / voice style**
   - **Answer: concise but not too dry — "small-robot energy."**
   - *Persona name:* `concise_warm`. System-prompt direction: short sentences, occasional dry humor, never robotic-monotone, leans curious-and-helpful, never sycophantic. No emoji in TTS output.

6. **Where to host the fork** — name `clawson` ok?
   - **Answer: ok.** Fork to user's personal GitHub as `clawson`.

7. **Gesture vocabulary + control surface**
   - **Answer: gesture table is fine, but we also need a desktop widget on the laptop for triggers / mode / snooze.**
   - *Adds:* see new "Desktop widget" section below.

8. **Standup trigger** — scheduled only, head-pat only, or both?
   - **Answer: all four surfaces — scheduled (07:30 weekdays), head-pat / face-detect, widget button, voice command.**
   - *Implication:* one canonical `run_standup()` entry point; each trigger calls it with a `source` tag for logging. Voice command goes through the existing OpenAI Realtime loop matching phrases like "give me the standup" / "morning briefing" / "what did I miss". Face-detect uses clawbody's existing tracker — fires when a face stays in frame for ≥3s after first detection of the day.

9. **Widget style** — tray icon + popover, floating mini-window, or browser tab?
   - **Answer: browser tab only.**
   - *Implication:* no `pywebview` / `pystray` / `rumps` dependency. FastAPI serves a single static panel at `http://localhost:7860/widget`; user bookmarks it. Simplest to build, simplest to debug. Can graduate to a tray wrapper later if it's worth it.

---

## Risks / things to flag

- **OpenClaw vs raw MCP**: clawbody routes tools through OpenClaw. We need to confirm whether OpenClaw exposes raw MCP-server registration or whether tools must go through their gateway. If gateway-only we're partially constrained by their roadmap; we may want to bypass OpenClaw for our two MCPs and call them directly from a side-channel handler.
- **OpenAI Realtime cost**: ~$0.06/min talking. Daily 30s standup ≈ pennies. Conversational use adds up; worth tracking via usage dashboard.
- **GitHub rate limits**: 5000 req/hr on PAT. Polling 5 repos every 30s ≈ ~600/hr — fine. Scales linearly with watched repos.
- **Vercel webhooks vs polling**: webhooks need a public endpoint. Polling is simpler for laptop-only deployment; start there, switch to webhooks if event latency is annoying.
- **Snooze persistence**: must survive restart. Persist to `~/.config/clawson/state.json`.
- **Token rotation**: GitHub PAT can expire; surface via TTS rather than silent failure.
- **Robot busy**: clawbody's tracking loop and our gestures share the robot. Need to coordinate so a CI-fail droop doesn't fight an in-progress face-track. Probably: gestures preempt tracking for ~1s, then tracking resumes.
