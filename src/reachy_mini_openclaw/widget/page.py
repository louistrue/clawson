"""HTML+JS for the Clawson widget. Inlined so we don't ship static assets."""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Clawson</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #0b0d12;
    --panel: #141821;
    --line: #2a2f3a;
    --fg: #e6e9ef;
    --muted: #7c8595;
    --accent: #ff7a3d;
    --good: #79d18a;
    --bad: #f47672;
    --pending: #f0c674;
  }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--fg);
    margin: 0;
    padding: 24px;
    max-width: 760px;
    margin-inline: auto;
  }
  header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 16px; }
  h1 { font-size: 16px; margin: 0; letter-spacing: 0.04em; }
  .heartbeat { font-size: 11px; color: var(--muted); }
  .heartbeat .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--good); margin-right: 4px; vertical-align: middle; }

  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; margin-bottom: 14px; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; }
  button {
    background: var(--panel);
    color: var(--fg);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 8px 14px;
    font: inherit;
    cursor: pointer;
    transition: border-color 0.1s, background 0.1s;
  }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #1a1208; border-color: var(--accent); font-weight: 600; }
  button.warn { border-color: var(--bad); }
  button.warn:hover { background: var(--bad); color: #1a0a0a; }

  .modecard { display: flex; align-items: center; gap: 12px; }
  .mode { display: inline-block; padding: 4px 14px; border-radius: 16px; background: var(--line); font-size: 13px; font-weight: 600; letter-spacing: 0.02em; text-transform: uppercase; }
  .mode.snoozed { background: var(--bad); color: #1a0a0a; }
  .mode.deep { background: #2a3450; color: #aebed7; }
  .mode.available { background: var(--good); color: #0c1a11; }
  .mode.normal { background: #2c3340; color: var(--fg); }
  .pending-confirm { background: var(--pending); color: #1c1300; padding: 8px 12px; border-radius: 6px; margin-top: 10px; font-weight: 500; }

  .muted { color: var(--muted); font-size: 12px; }
  .small { font-size: 11px; }
  ul { padding-left: 0; list-style: none; margin: 0; }
  li { padding: 6px 0; border-bottom: 1px solid var(--line); }
  li:last-child { border-bottom: none; }
  .ev-fail { border-left: 3px solid var(--bad); padding-left: 8px; }
  .ev-pass { border-left: 3px solid var(--good); padding-left: 8px; }
  .ev-info { border-left: 3px solid var(--line); padding-left: 8px; }
  .ev-crit { border-left: 3px solid var(--accent); padding-left: 8px; background: rgba(255,122,61,0.06); }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 540px) { .grid { grid-template-columns: 1fr; } }
  .voicehints { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 4px 12px; font-size: 12px; color: var(--muted); }
  .voicehints code { color: var(--fg); background: rgba(255,255,255,0.03); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, Menlo, monospace; }
</style>
</head>
<body>
  <header>
    <h1>🦞 CLAWSON</h1>
    <div class="heartbeat"><span class="dot"></span><span id="heartbeat-text">live</span></div>
  </header>

  <div class="panel">
    <div class="modecard">
      <span id="mode" class="mode">…</span>
      <span id="snooze" class="muted small"></span>
      <span style="flex:1"></span>
      <span id="active-hours" class="muted small"></span>
    </div>
    <div id="pending" style="display:none" class="pending-confirm"></div>
    <div class="row" style="margin-top: 12px;">
      <button onclick="post('/api/mode/deep')">Deep</button>
      <button onclick="post('/api/mode/normal')">Normal</button>
      <button onclick="post('/api/mode/available')">Available</button>
      <button onclick="post('/api/mode/cycle')">Cycle</button>
    </div>
    <div class="row" style="margin-top: 6px;">
      <button onclick="snooze(15)">Snooze 15m</button>
      <button onclick="snooze(60)">Snooze 1h</button>
      <button onclick="snooze(240)">Snooze 4h</button>
      <button onclick="post('/api/snooze/cancel')">Un-snooze</button>
    </div>
    <div class="row" style="margin-top: 6px;">
      <button class="primary" onclick="post('/api/standup')">▶ Standup</button>
      <button onclick="post('/api/queue/clear')">Clear queue</button>
      <button class="warn" onclick="confirmRestart()">↻ Restart</button>
    </div>
  </div>

  <div class="grid">
    <div class="panel">
      <div class="muted">QUEUED <span class="small">(silenced — replay later)</span></div>
      <ul id="queue"><li class="muted">empty</li></ul>
    </div>
    <div class="panel">
      <div class="muted">RECENT EVENTS</div>
      <ul id="recent"><li class="muted">empty</li></ul>
    </div>
  </div>

  <div class="panel">
    <div class="muted" style="margin-bottom: 8px;">VOICE COMMANDS</div>
    <div class="voicehints">
      <div><code>deep mode</code> / <code>available</code> / <code>normal</code></div>
      <div><code>snooze fifteen</code> / <code>snooze hour</code> / <code>snooze four hours</code></div>
      <div><code>wake up</code> / <code>cancel snooze</code></div>
      <div><code>standup</code> / <code>rollup</code> / <code>what's queued</code></div>
      <div><code>shut up</code> / <code>be quiet</code></div>
      <div><code>say again</code> / <code>repeat</code></div>
      <div><code>status</code> / <code>are you alive</code></div>
      <div><code>what mode</code> / <code>what time</code></div>
      <div><code>clear queue</code></div>
      <div><code>restart yourself</code></div>
      <div><code>open my PRs</code></div>
      <div><code>what's broken</code></div>
      <div><code>focus on owner/repo</code></div>
      <div><code>unfocus</code></div>
      <div><code>timer 5 minutes</code></div>
      <div><code>draft commit message</code></div>
    </div>
    <div class="muted small" style="margin-top: 10px;">
      Quick yes/no via head gesture: <strong>nod</strong> = yes, <strong>shake</strong> (or twist base) = no.
      Antenna wiggles right for yes detected, left for no detected.
    </div>
  </div>

<script>
  async function post(path, body) {
    await fetch(path, {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: body ? JSON.stringify(body) : null,
    });
    refresh();
  }
  async function snooze(minutes) { await post('/api/snooze', {minutes}); }

  async function confirmRestart() {
    if (!confirm('Restart Clawson? In-flight conversations will drop.')) return;
    await fetch('/api/restart', {method: 'POST'});
    document.getElementById('heartbeat-text').textContent = 'restarting…';
  }

  function fmtEvent(e) {
    let tone = 'ev-info';
    if (e.severity === 'critical') tone = 'ev-crit';
    else if (e.kind.includes('fail')) tone = 'ev-fail';
    else if (e.kind.includes('pass') || e.kind === 'pr_merged') tone = 'ev-pass';
    const link = e.link ? ` <a href="${e.link}" target="_blank">↗</a>` : '';
    const t = e.ts ? new Date(e.ts).toLocaleTimeString() : '';
    return `<li class="${tone}">${e.summary}${link}<div class="muted small">${e.kind} · ${e.source} · ${t}</div></li>`;
  }

  let lastBeat = Date.now();
  async function refresh() {
    try {
      const r = await fetch('/api/state'); if (!r.ok) return;
      const s = await r.json();
      lastBeat = Date.now();
      const modeEl = document.getElementById('mode');
      modeEl.textContent = s.mode;
      modeEl.className = 'mode ' + s.mode;
      document.getElementById('snooze').textContent =
        s.snooze_until ? `until ${new Date(s.snooze_until).toLocaleTimeString()}` : '';
      document.getElementById('active-hours').textContent =
        s.active_hours_now ? 'within active hours' : 'outside active hours — silent';

      // pending confirmation hint
      let pending = null;
      try {
        const p = await fetch('/api/pending_confirmation');
        if (p.ok) pending = (await p.json()).pending;
      } catch {}
      const pendEl = document.getElementById('pending');
      if (pending) {
        pendEl.style.display = 'block';
        pendEl.textContent = '? ' + pending + '  — nod or right antenna for YES, shake or left antenna for NO';
      } else {
        pendEl.style.display = 'none';
      }

      document.getElementById('queue').innerHTML =
        s.queued.length ? s.queued.map(fmtEvent).join('') : '<li class="muted">empty</li>';
      document.getElementById('recent').innerHTML =
        s.recent.length ? s.recent.slice().reverse().map(fmtEvent).join('') : '<li class="muted">empty</li>';
      document.getElementById('heartbeat-text').textContent = 'live';
    } catch (e) {
      const age = Math.round((Date.now() - lastBeat) / 1000);
      document.getElementById('heartbeat-text').textContent = `disconnected ${age}s`;
    }
  }

  refresh();
  setInterval(refresh, 2000);
</script>
</body>
</html>
"""
