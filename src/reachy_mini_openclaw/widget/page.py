"""HTML+JS for the Clawson widget. Inlined so we don't ship static assets."""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Clawson</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #0e1116;
    --panel: #161a22;
    --line: #2a2f3a;
    --fg: #d8dee9;
    --muted: #7c8595;
    --accent: #ff7a3d;
    --good: #79d18a;
    --bad: #f47672;
  }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--fg);
    margin: 0;
    padding: 24px;
    max-width: 720px;
    margin-inline: auto;
  }
  h1 { font-size: 18px; margin: 0 0 16px; letter-spacing: 0.04em; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  button {
    background: var(--panel);
    color: var(--fg);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 8px 14px;
    font: inherit;
    cursor: pointer;
  }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #1a1208; border-color: var(--accent); }
  .mode { display: inline-block; padding: 2px 8px; border-radius: 12px; background: var(--line); font-size: 12px; }
  .mode.snoozed { background: var(--bad); color: #1a0a0a; }
  .mode.deep { background: #2a3450; }
  .mode.available { background: var(--good); color: #0c1a11; }
  .muted { color: var(--muted); font-size: 12px; }
  ul { padding-left: 0; list-style: none; margin: 0; }
  li { padding: 6px 0; border-bottom: 1px solid var(--line); }
  li:last-child { border-bottom: none; }
  .ev-fail { border-left: 3px solid var(--bad); padding-left: 8px; }
  .ev-pass { border-left: 3px solid var(--good); padding-left: 8px; }
  .ev-info { border-left: 3px solid var(--line); padding-left: 8px; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 540px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <h1>🦞 Clawson</h1>

  <div class="panel">
    <div>Mode: <span id="mode" class="mode">…</span> <span id="snooze" class="muted"></span></div>
    <div class="muted" id="active-hours"></div>
    <div class="row" style="margin-top: 12px;">
      <button onclick="post('/api/mode/cycle')">Cycle mode</button>
      <button onclick="post('/api/mode/deep')">Deep</button>
      <button onclick="post('/api/mode/normal')">Normal</button>
      <button onclick="post('/api/mode/available')">Available</button>
    </div>
    <div class="row" style="margin-top: 8px;">
      <button onclick="snooze(15)">Snooze 15m</button>
      <button onclick="snooze(60)">Snooze 1h</button>
      <button onclick="snooze(240)">Snooze 4h</button>
      <button onclick="post('/api/snooze/cancel')">Un-snooze</button>
    </div>
    <div class="row" style="margin-top: 8px;">
      <button class="primary" onclick="post('/api/standup')">Trigger standup</button>
      <button onclick="post('/api/queue/clear')">Clear queue</button>
    </div>
  </div>

  <div class="grid">
    <div class="panel">
      <div class="muted">Queued (silenced overnight / in deep)</div>
      <ul id="queue"><li class="muted">empty</li></ul>
    </div>
    <div class="panel">
      <div class="muted">Recent events</div>
      <ul id="recent"><li class="muted">empty</li></ul>
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

  function fmtEvent(e) {
    const tone = e.kind.includes('fail') ? 'ev-fail'
               : e.kind.includes('pass') || e.kind === 'pr_merged' ? 'ev-pass'
               : 'ev-info';
    const link = e.link ? ` <a href="${e.link}" target="_blank">↗</a>` : '';
    return `<li class="${tone}">${e.summary}${link}<div class="muted">${e.kind} · ${e.source}</div></li>`;
  }

  async function refresh() {
    const r = await fetch('/api/state'); if (!r.ok) return;
    const s = await r.json();
    const modeEl = document.getElementById('mode');
    modeEl.textContent = s.mode;
    modeEl.className = 'mode ' + s.mode;
    document.getElementById('snooze').textContent =
      s.snooze_until ? `until ${new Date(s.snooze_until).toLocaleTimeString()}` : '';
    document.getElementById('active-hours').textContent =
      s.active_hours_now ? 'within active hours' : 'outside active hours — silent';
    document.getElementById('queue').innerHTML =
      s.queued.length ? s.queued.map(fmtEvent).join('') : '<li class="muted">empty</li>';
    document.getElementById('recent').innerHTML =
      s.recent.length ? s.recent.slice().reverse().map(fmtEvent).join('') : '<li class="muted">empty</li>';
  }

  refresh();
  setInterval(refresh, 3000);
</script>
</body>
</html>
"""
