#!/usr/bin/env python3
"""
Patches nq_agent.py and nq_dashboard.html with the Claude chat feature.
Run from inside ~/Downloads/nq-signal-agent-/
"""

import re

# ─── PATCH nq_agent.py ────────────────────────────────────────────────────────
print("Patching nq_agent.py...")
with open('nq_agent.py', 'r') as f:
    agent = f.read()

CHAT_ENDPOINT = '''
@app.route('/chat', methods=['POST'])
def chat():
    """
    Live AI trading assistant — knows current market data.
    POST body: { "message": "...", "history": [...], "position": {...} }
    """
    try:
        import json as json_mod
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({"reply": "ANTHROPIC_API_KEY not set in Railway Variables."})

        body = request.get_json(force=True) or {}
        user_message = body.get('message', '').strip()
        conversation_history = body.get('history', [])
        position = body.get('position', {})

        if not user_message:
            return jsonify({"reply": "No message provided."})

        # Pull live market data
        try:
            df_5m = get_nq_bars(interval_minutes=5, lookback_days=2, limit=300)
            df_1h = get_nq_bars(interval_minutes=60, lookback_days=30, limit=300)
            df_1m = get_nq_bars(interval_minutes=1, lookback_days=1, limit=200)
            market = generate_signal(df_5m, df_1h, df_1m)
            market_context = f"""
LIVE MARKET DATA (as of right now):
- NQ Price: {market['price']}
- Signal: {market['signal']} | Score: {market['score']} | Confidence: {market['confidence']}%
- Session: {market['session'].upper()}
- RSI: {market['indicators']['rsi']}
- MACD Histogram: {market['indicators']['macd_histogram']} (\'bullish\' if {market['indicators']['macd_histogram']} > 0 else \'bearish\')
- BB Position: {market['indicators']['bb_position']*100:.0f}% (0%=lower band, 100%=upper band)
- VWAP: {market['indicators']['vwap']} (price is \'ABOVE\' if {market['price']} > {market['indicators']['vwap']} else \'BELOW\' VWAP)
- Volume Ratio: {market['volume']['ratio']}x {'(SPIKE)' if market['volume']['spike'] else ''}
- ATR: {market['indicators']['atr']}
- Support: {market['support']} | Resistance: {market['resistance']}
- FVG: {market['indicators']['fvg_type']} {'(price in gap)' if market['indicators']['fvg_in_gap'] else ''}
- Order Block: {'bullish' if market['indicators']['ob_direction']==1 else 'bearish' if market['indicators']['ob_direction']==-1 else 'none'}
- Event Window Active: {market['event_window']}
- Signal Reasons: {'; '.join(market['reasons'])}
- TP: {market['tp_price']} | SL: {market['sl_price']} | R/R: 3:1
"""
            market_snap = {"price": market.get('price'), "signal": market.get('signal'), "score": market.get('score'), "session": market.get('session')}
        except Exception as e:
            market_context = f"[Market data unavailable: {str(e)}]"
            market_snap = {}

        position_context = ""
        if position and position.get('side'):
            position_context = f"""
CURRENT POSITION:
- Side: {position.get('side')}
- Entry: {position.get('entry')}
- Contracts: {position.get('contracts')}
- Unrealized P&L: ${position.get('pnl')}
- TP: {position.get('tp', 'not set')} | SL: {position.get('sl', 'not set')}
"""

        system_prompt = f"""You are an expert NQ futures scalping assistant embedded in a live trading dashboard. You have real-time access to market data and indicators. Be direct, concise, and actionable. Answer like a sharp trading coach — no fluff.

Your knowledge base:
- Scalping system: TP=60pts, SL=20pts, R/R=3:1
- Sessions: London (2-4am PST) and US (6:30-10:30am PST) only for signals
- Scoring engine uses RSI, MACD, BB, VWAP, FVG, Order Blocks, RSI Divergence
- Signal threshold: 0.38 (US session), 0.45 (London)
- Contract sizing: 1 (score<0.45), 2 (0.45-0.55), 3 (>0.55)
- Baseline: $55,200 net P/L, 50% WR, 78 signals over 60 days

{market_context}
{position_context}

Keep responses under 5 sentences unless detail is needed. Be direct with trade advice."""

        messages = []
        for h in conversation_history[-10:]:
            if h.get('role') in ('user', 'assistant') and h.get('content'):
                messages.append({"role": h['role'], "content": h['content']})
        messages.append({"role": "user", "content": user_message})

        import urllib.request
        payload = json_mod.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 400,
            "system": system_prompt,
            "messages": messages
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            }
        )
        resp = urllib.request.urlopen(req, timeout=20)
        result = json_mod.loads(resp.read())
        reply = result["content"][0]["text"]

        return jsonify({"reply": reply, "market_snapshot": market_snap})

    except Exception as e:
        print(f"[ERROR] /chat failed: {e}")
        return jsonify({"reply": f"Chat error: {str(e)}"})

'''

# Insert before @app.route('/health')
if '/chat' in agent:
    print("  /chat endpoint already exists, skipping.")
else:
    agent = agent.replace("@app.route('/health')", CHAT_ENDPOINT + "@app.route('/health')")
    print("  /chat endpoint added.")

with open('nq_agent.py', 'w') as f:
    f.write(agent)

print("nq_agent.py patched successfully.\n")

# ─── PATCH nq_dashboard.html ─────────────────────────────────────────────────
print("Patching nq_dashboard.html...")
with open('nq_dashboard.html', 'r') as f:
    html = f.read()

# 1. Add Chat tab button
if "switchTab('chat')" in html:
    print("  Chat tab button already exists, skipping.")
else:
    html = html.replace(
        "<button class=\"tab-btn\" onclick=\"switchTab('history')\">HISTORY</button>",
        "<button class=\"tab-btn\" onclick=\"switchTab('history')\">HISTORY</button>\n  <button class=\"tab-btn\" onclick=\"switchTab('chat')\">CHAT</button>"
    )
    print("  Chat tab button added.")

# 2. Add CSS for chat
CHAT_CSS = """
/* Chat */
.quick-prompt {
  background: transparent; border: 1px solid var(--border); color: var(--muted);
  font-family: var(--mono); font-size: 9px; padding: 3px 9px; cursor: pointer;
  letter-spacing: 1px; transition: all 0.2s;
}
.quick-prompt:hover { border-color: var(--accent); color: var(--accent); }
.chat-msg { display: flex; flex-direction: column; gap: 3px; max-width: 85%; }
.chat-msg.user { align-self: flex-end; align-items: flex-end; }
.chat-msg.assistant { align-self: flex-start; align-items: flex-start; }
.chat-bubble { padding: 10px 14px; font-size: 12px; line-height: 1.6; }
.chat-msg.user .chat-bubble { background: rgba(0,212,255,0.08); border: 1px solid rgba(0,212,255,0.2); color: var(--text); font-family: var(--mono); }
.chat-msg.assistant .chat-bubble { background: rgba(255,255,255,0.03); border: 1px solid var(--border); color: var(--text); font-family: var(--sans); }
.chat-meta { font-family: var(--mono); font-size: 9px; color: var(--muted); letter-spacing: 1px; }
.chat-typing { display: flex; gap: 4px; align-items: center; padding: 10px 14px; border: 1px solid var(--border); background: rgba(255,255,255,0.03); width: fit-content; }
.chat-typing span { width: 4px; height: 4px; background: var(--accent); border-radius: 50%; animation: chatdot 1.2s infinite; }
.chat-typing span:nth-child(2) { animation-delay: 0.2s; }
.chat-typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes chatdot { 0%,60%,100%{opacity:0.2;transform:scale(1)} 30%{opacity:1;transform:scale(1.3)} }
"""

if 'chatdot' in html:
    print("  Chat CSS already exists, skipping.")
else:
    html = html.replace("</style>", CHAT_CSS + "\n</style>")
    print("  Chat CSS added.")

# 3. Add Chat tab pane HTML
CHAT_PANE = """
<!-- Chat tab -->
<div class="tab-pane" id="tab-chat">
  <div style="display:flex;flex-direction:column;height:520px;background:var(--panel);border:1px solid var(--border);">
    <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;">
      <div>
        <div style="font-family:var(--mono);font-size:10px;color:var(--accent);letter-spacing:3px;">AI TRADING ASSISTANT</div>
        <div style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-top:2px;letter-spacing:1px;">LIVE MARKET DATA · REAL-TIME INDICATORS</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;">
        <div id="chatMarketSnap" style="font-family:var(--mono);font-size:9px;color:var(--muted);text-align:right;"></div>
        <button onclick="clearChat()" style="background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--mono);font-size:9px;padding:3px 8px;cursor:pointer;letter-spacing:1px;">CLEAR</button>
      </div>
    </div>
    <div style="padding:8px 16px;border-bottom:1px solid var(--border);background:rgba(0,212,255,0.03);">
      <div style="font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:1px;margin-bottom:5px;">POSITION CONTEXT (optional)</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <select id="chatSide" style="background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:10px;padding:3px 6px;">
          <option value="">No Position</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option>
        </select>
        <input id="chatEntry" type="number" placeholder="Entry price" step="0.25" style="background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:10px;padding:3px 8px;width:120px;">
        <input id="chatContracts" type="number" placeholder="Contracts" min="1" style="background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:10px;padding:3px 8px;width:90px;">
        <input id="chatPnl" type="number" placeholder="Unreal P&amp;L $" step="1" style="background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:10px;padding:3px 8px;width:110px;">
        <input id="chatTp" type="number" placeholder="TP" step="0.25" style="background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:10px;padding:3px 8px;width:100px;">
        <input id="chatSl" type="number" placeholder="SL" step="0.25" style="background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:10px;padding:3px 8px;width:100px;">
      </div>
    </div>
    <div id="chatMessages" style="flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px;">
      <div style="text-align:center;font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px;padding:20px 0;">
        Ask anything about the current market, your position, or setup.<br>
        <span style="color:var(--accent);opacity:0.6;">The assistant has live NQ data and all indicators.</span>
      </div>
    </div>
    <div style="padding:8px 16px;border-top:1px solid var(--border);display:flex;gap:6px;flex-wrap:wrap;">
      <button class="quick-prompt" onclick="sendQuick('What does the current setup look like?')">Setup?</button>
      <button class="quick-prompt" onclick="sendQuick('Should I add contracts to my position?')">Add contracts?</button>
      <button class="quick-prompt" onclick="sendQuick('Where should I move my stop loss?')">Move SL?</button>
      <button class="quick-prompt" onclick="sendQuick('Is momentum still bullish or fading?')">Momentum?</button>
      <button class="quick-prompt" onclick="sendQuick('What are the key levels to watch right now?')">Key levels?</button>
    </div>
    <div style="padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px;">
      <input id="chatInput" type="text" placeholder="Ask the trading assistant..."
        style="flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:12px;padding:8px 12px;outline:none;"
        onkeydown="if(event.key==='Enter')sendChat()">
      <button id="chatSendBtn" onclick="sendChat()"
        style="background:var(--accent);border:none;color:var(--bg);font-family:var(--mono);font-size:11px;font-weight:700;padding:8px 20px;cursor:pointer;letter-spacing:1px;">
        SEND
      </button>
    </div>
  </div>
</div>
"""

if 'tab-chat' in html:
    print("  Chat tab pane already exists, skipping.")
else:
    html = html.replace('<div class="footer"', CHAT_PANE + '\n<div class="footer"')
    print("  Chat tab pane added.")

# 4. Add Chat JS
CHAT_JS = """
// ─── Chat ─────────────────────────────────────────────────────────────────────
let chatHistory = [];

function getPositionContext() {
  const side = document.getElementById('chatSide').value;
  if (!side) return {};
  return {
    side,
    entry: parseFloat(document.getElementById('chatEntry').value) || null,
    contracts: parseInt(document.getElementById('chatContracts').value) || null,
    pnl: parseFloat(document.getElementById('chatPnl').value) || null,
    tp: parseFloat(document.getElementById('chatTp').value) || null,
    sl: parseFloat(document.getElementById('chatSl').value) || null,
  };
}

function appendMessage(role, content, meta) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  div.innerHTML = `<div class="chat-bubble">${content.replace(/\\n/g, '<br>')}</div>${meta ? `<div class="chat-meta">${meta}</div>` : ''}`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function showTyping() {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'chat-msg assistant'; div.id = 'chatTyping';
  div.innerHTML = '<div class="chat-typing"><span></span><span></span><span></span></div>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function removeTyping() { const el = document.getElementById('chatTyping'); if (el) el.remove(); }

function updateMarketSnap(snap) {
  if (!snap || !snap.price) return;
  const el = document.getElementById('chatMarketSnap');
  const col = snap.signal === 'BUY' ? 'var(--green)' : snap.signal === 'SELL' ? 'var(--red)' : 'var(--yellow)';
  el.innerHTML = `<span style="color:var(--text)">${snap.price?.toLocaleString()}</span> &nbsp;<span style="color:${col}">${snap.signal}</span><br><span style="color:var(--muted)">${(snap.session||'').toUpperCase()} · score ${snap.score}</span>`;
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const btn = document.getElementById('chatSendBtn');
  const msg = input.value.trim();
  if (!msg || btn.disabled) return;
  input.value = ''; btn.disabled = true; btn.style.opacity = '0.4';
  const time = new Date().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'});
  appendMessage('user', msg, time + ' PT');
  showTyping();
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg, history: chatHistory, position: getPositionContext() })
    });
    const data = await res.json();
    removeTyping();
    const reply = data.reply || 'No response.';
    appendMessage('assistant', reply, 'NQ ASSISTANT · ' + time + ' PT');
    chatHistory.push({role: 'user', content: msg});
    chatHistory.push({role: 'assistant', content: reply});
    if (chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
    if (data.market_snapshot) updateMarketSnap(data.market_snapshot);
  } catch(e) {
    removeTyping();
    appendMessage('assistant', 'Error connecting to assistant. Check that Railway is online.', '');
  }
  btn.disabled = false; btn.style.opacity = '1';
  document.getElementById('chatInput').focus();
}

function sendQuick(msg) { document.getElementById('chatInput').value = msg; sendChat(); }

function clearChat() {
  chatHistory = [];
  document.getElementById('chatMessages').innerHTML = '<div style="text-align:center;font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px;padding:20px 0;">Ask anything about the current market, your position, or setup.<br><span style=\\"color:var(--accent);opacity:0.6;\\">The assistant has live NQ data and all indicators.</span></div>';
}
"""

if 'sendChat' in html:
    print("  Chat JS already exists, skipping.")
else:
    html = html.replace('</script>', CHAT_JS + '\n</script>')
    print("  Chat JS added.")

with open('nq_dashboard.html', 'w') as f:
    f.write(html)

print("nq_dashboard.html patched successfully.\n")
print("=" * 50)
print("Done! Now run:")
print("  git add nq_agent.py nq_dashboard.html")
print("  git commit -m 'feat: add Claude chat tab with live market data'")
print("  git push")
print("Railway will auto-deploy in ~60 seconds.")
