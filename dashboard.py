"""
Policar Dashboard — simple Flask web UI.
Run alongside main.py: python3 dashboard.py
Reads state.json written by the bot; auto-refreshes every 5s.
"""
import json
import os

from flask import Flask, jsonify, Response

import state as st

app   = Flask(__name__)
PORT  = int(os.environ.get("DASHBOARD_PORT", "8080"))

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Policar Dashboard</title>
<style>
  :root {
    --bg:       #0d0f14;
    --surface:  #161b25;
    --border:   #1e2736;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --green:    #22c55e;
    --red:      #ef4444;
    --yellow:   #eab308;
    --blue:     #3b82f6;
    --accent:   #6366f1;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }

  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  header h1 { font-size: 18px; font-weight: 700; color: var(--accent); letter-spacing: 1px; }
  .status-pill {
    padding: 4px 12px; border-radius: 999px; font-size: 11px; font-weight: 600; text-transform: uppercase;
  }
  .status-running { background: rgba(34,197,94,.15); color: var(--green); border: 1px solid rgba(34,197,94,.3); }
  .status-offline { background: rgba(239,68,68,.15); color: var(--red);   border: 1px solid rgba(239,68,68,.3); }

  main { padding: 24px; display: flex; flex-direction: column; gap: 24px; }

  .meta-bar { display: flex; gap: 24px; flex-wrap: wrap; }
  .meta-item { display: flex; flex-direction: column; gap: 4px; }
  .meta-label { font-size: 10px; text-transform: uppercase; color: var(--muted); letter-spacing: .5px; }
  .meta-value { font-size: 13px; color: var(--text); }

  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 12px; }

  .coins-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
  .coin-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px;
    display: flex; flex-direction: column; gap: 14px;
  }
  .coin-header { display: flex; align-items: center; justify-content: space-between; }
  .coin-name { font-size: 18px; font-weight: 700; letter-spacing: 1px; }
  .result-badge {
    padding: 3px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase;
  }
  .result-win  { background: rgba(34,197,94,.15);  color: var(--green); }
  .result-loss { background: rgba(239,68,68,.15);  color: var(--red); }
  .result-none { background: rgba(100,116,139,.15); color: var(--muted); }

  .stat-row { display: flex; justify-content: space-between; }
  .stat-label { color: var(--muted); }
  .stat-value { font-weight: 600; }
  .stat-value.bet    { color: var(--yellow); }
  .stat-value.streak-pos { color: var(--green); }
  .stat-value.streak-neg { color: var(--red); }

  .history-dots { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
  }
  .dot-win  { background: var(--green); }
  .dot-loss { background: var(--red); }

  .log-panel {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px;
  }
  .log-list { display: flex; flex-direction: column; gap: 6px; max-height: 300px; overflow-y: auto; }
  .log-entry { display: flex; gap: 12px; }
  .log-time  { color: var(--muted); min-width: 80px; }
  .log-msg   { color: var(--text); }

  .refresh-note { color: var(--muted); font-size: 11px; text-align: right; }
  #last-updated { color: var(--blue); }
</style>
</head>
<body>
<header>
  <h1>⬡ POLICAR</h1>
  <div style="display:flex;align-items:center;gap:16px;">
    <span id="status-pill" class="status-pill status-offline">offline</span>
  </div>
</header>

<main>
  <div class="meta-bar" id="meta-bar"></div>

  <div>
    <h2>Coins</h2>
    <div class="coins-grid" id="coins-grid"></div>
  </div>

  <div class="log-panel">
    <h2>Activity Log</h2>
    <div class="log-list" id="log-list"></div>
  </div>

  <div class="refresh-note">Auto-refresh every 5s &nbsp;|&nbsp; Last updated: <span id="last-updated">—</span></div>
</main>

<script>
async function refresh() {
  try {
    const data = await fetch('/api/state').then(r => r.json());
    render(data);
    document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('status-pill').textContent = 'offline';
    document.getElementById('status-pill').className = 'status-pill status-offline';
  }
}

function render(data) {
  const bot = data.bot || {};

  // Status pill
  const pill = document.getElementById('status-pill');
  if (bot.status === 'running') {
    pill.textContent = '● running';
    pill.className = 'status-pill status-running';
  } else {
    pill.textContent = 'offline';
    pill.className = 'status-pill status-offline';
  }

  // Meta bar
  const meta = document.getElementById('meta-bar');
  meta.innerHTML = [
    ['Direction', bot.direction || '—'],
    ['Base Bet',  bot.base_bet ? `$${bot.base_bet}` : '—'],
    ['Max Bet',   bot.max_bet  ? `$${bot.max_bet}`  : '—'],
    ['Last Cycle', bot.last_cycle || '—'],
    ['Next Trigger', bot.next_trigger || '—'],
  ].map(([label, value]) => `
    <div class="meta-item">
      <span class="meta-label">${label}</span>
      <span class="meta-value">${value}</span>
    </div>`).join('');

  // Coins
  const grid = document.getElementById('coins-grid');
  const coins = data.coins || {};
  grid.innerHTML = Object.entries(coins).sort().map(([coin, cs]) => {
    const streak = cs.streak || 0;
    const streakClass = streak > 0 ? 'streak-pos' : streak < 0 ? 'streak-neg' : '';
    const streakStr = streak > 0 ? `+${streak} 🔥` : streak < 0 ? `${streak} 🧊` : '—';
    const resultClass = cs.last_result === 'win' ? 'result-win' : cs.last_result === 'loss' ? 'result-loss' : 'result-none';
    const resultLabel = cs.last_result || 'pending';

    const dots = (cs.history || []).slice(0, 15).map(h =>
      `<div class="dot dot-${h.result}" title="${h.slug}: ${h.result} (bet $${h.bet})"></div>`
    ).join('');

    const wl = (cs.wins || 0) + (cs.losses || 0);
    const pct = wl > 0 ? Math.round((cs.wins / wl) * 100) : 0;

    return `
    <div class="coin-card">
      <div class="coin-header">
        <span class="coin-name">${coin.toUpperCase()}</span>
        <span class="result-badge ${resultClass}">${resultLabel}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Next Bet</span>
        <span class="stat-value bet">$${(cs.current_bet || 0).toFixed(2)}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Streak</span>
        <span class="stat-value ${streakClass}">${streakStr}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">W / L</span>
        <span class="stat-value">${cs.wins || 0} / ${cs.losses || 0} &nbsp;<span style="color:var(--muted)">(${pct}%)</span></span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Total Spent</span>
        <span class="stat-value">$${(cs.total_spent || 0).toFixed(2)}</span>
      </div>
      <div class="history-dots">${dots}</div>
    </div>`;
  }).join('');

  // Log
  const logList = document.getElementById('log-list');
  logList.innerHTML = (data.log || []).map(entry =>
    `<div class="log-entry"><span class="log-time">${entry.time}</span><span class="log-msg">${entry.msg}</span></div>`
  ).join('');
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index() -> Response:
    return Response(HTML, mimetype="text/html")


@app.route("/api/state")
def api_state() -> Response:
    return jsonify(st.load())


if __name__ == "__main__":
    print(f"Policar Dashboard → http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
