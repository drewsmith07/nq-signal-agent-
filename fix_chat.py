#!/usr/bin/env python3
"""
Fix script — force injects full chat tab into nq_dashboard.html
Run from inside ~/Downloads/nq-signal-agent-/
"""

with open('nq_dashboard.html', 'r') as f:
    html = f.read()

# ── 1. Tab button ─────────────────────────────────────────────────────────────
if "switchTab('chat')" not in html:
    html = html.replace(
        "<button class=\"tab-btn\" onclick=\"switchTab('history')\">HISTORY</button>",
        "<button class=\"tab-btn\" onclick=\"switchTab('history')\">HISTORY</button>\n  <button class=\"tab-btn\" onclick=\"switchTab('chat')\">CHAT</button>"
    )
    print("✓ Tab button added")
else:
    print("- Tab button already present")

# ── 2. CSS ────────────────────────────────────────────────────────────────────
CHAT_CSS = """
/* ── Chat ── */
.quick-prompt{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--mono);font-size:9px;padding:3px 9px;cursor:pointer;letter-spacing:1px;transition:all 0.2s;}
.quick-prompt:hover{border-color:var(--accent);color:var(--accent);}
.chat-msg{display:flex;flex-direction:column;gap:3px;max-width:85%;}
.chat-msg.user{align-self:flex-end;align-items:flex-end;}
.chat-msg.assistant{align-self:flex-start;align-items:flex-start;}
.chat-bubble{padding:10px 14px;font-size:12px;line-height:1.6;}
.chat-msg.user .chat-bubble{background:rgba(0,212,255,0.08);border:1px solid rgba(0,212,255,0.2);color:var(--text);font-family:var(--mono);}
.chat-msg.assistant .chat-bubble{background:rgba(255,255,255,0.03);border:1px solid var(--border);color:var(--text);font-family:var(--sans);}
.chat-meta{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:1px;}
.chat-typing{display:flex;gap:4px;align-items:center;padding:10px 14px;border:1px solid var(--border);background:rgba(255,255,255,0.03);width:fit-content;}
.chat-typing span{width:4px;height:4px;background:var(--accent);border-radius:50%;animation:chatdot 1.2s infinite;}
.chat-typing span:nth-child(2){animation-delay:0.2s;}
.chat-typing span:nth-child(3){animation-delay:0.4s;}
@keyframes chatdot{0%,60%,100%{opacity:0.2;transform:scale(1)}30%{opacity:1;transform:scale(1.3)}}
"""

if 'chatdot' not in html:
    html = html.replace('</style>', CHAT_CSS + '\n</style>')
    print("✓ CSS added")
else:
    print("- CSS already present")

# ── 3. Tab pane ───────────────────────────────────────────────────────────────
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

if 'tab-chat' not in html:
    html = html.replace('<div class="footer"', CHAT_PANE + '\n<div class="footer"')
    print("✓ Tab pane added")
else:
    print("- Tab pane already present")

# ── 4. JS ─────────────────────────────────────────────────────────────────────
CHAT_JS = """
// ── Chat ──────────────────────────────────────────────────────────────────────
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
  div.className = 'chat-msg ' + role;
  div.innerHTML = '<div class="chat-bubble">' + content.replace(/\\n/g, '<br>') + '</div>' + (meta ? '<div class="chat-meta">' + meta + '</div>' : '');
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}
function showTyping() {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'chat-msg assistant'; div.id = 'chatTyping';
  div.innerHTML = '<div class="chat-typing"><span></span><span></span><span></span></div>';
  container.appendChild(div); container.scrollTop = container.scrollHeight;
}
function removeTyping() { const el = document.getElementById('chatTyping'); if (el) el.remove(); }
function updateMarketSnap(snap) {
  if (!snap || !snap.price) return;
  const el = document.getElementById('chatMarketSnap');
  const col = snap.signal === 'BUY' ? 'var(--green)' : snap.signal === 'SELL' ? 'var(--red)' : 'var(--yellow)';
  el.innerHTML = '<span style="color:var(--text)">' + snap.price.toLocaleString() + '</span> &nbsp;<span style="color:' + col + '">' + snap.signal + '</span><br><span style="color:var(--muted)">' + (snap.session||'').toUpperCase() + ' · score ' + snap.score + '</span>';
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
    appendMessage('assistant', 'Error connecting to assistant. Check Railway is online.', '');
  }
  btn.disabled = false; btn.style.opacity = '1';
  document.getElementById('chatInput').focus();
}
function sendQuick(msg) { document.getElementById('chatInput').value = msg; sendChat(); }
function clearChat() {
  chatHistory = [];
  document.getElementById('chatMessages').innerHTML = '<div style="text-align:center;font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px;padding:20px 0;">Ask anything about the current market, your position, or setup.</div>';
}
"""

if 'sendChat' not in html:
    html = html.replace('</script>', CHAT_JS + '\n</script>')
    print("✓ JS added")
else:
    # Force inject before closing script tag
    html = html.replace('</script>', CHAT_JS + '\n</script>', 1)
    print("✓ JS force injected")

with open('nq_dashboard.html', 'w') as f:
    f.write(html)

print("\n✅ Done! Now run:")
print("  git add nq_dashboard.html")
print("  git commit -m 'fix: chat tab full inject'")
print("  git push")
