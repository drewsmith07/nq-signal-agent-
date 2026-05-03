#!/usr/bin/env python3
"""
NQ Futures Scalping Signal Agent
Run this locally: python nq_agent.py
Requires: pip install yfinance pandas numpy flask flask-cors
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

TICKER = "NQ=F"

# ─── Economic Calendar (suppress signals ±15min around high-impact events) ────
# Times in ET (Eastern Time). Update monthly.
ECONOMIC_CALENDAR = [
    # (date_str, hour_ET, minute_ET)
    ("2026-05-07", 14, 0),   # Fed Decision
    ("2026-05-09", 8, 30),   # NFP
    ("2026-05-13", 8, 30),   # CPI
    ("2026-05-30", 8, 30),   # PCE
    ("2026-06-06", 8, 30),   # NFP
    ("2026-06-11", 8, 30),   # CPI
    ("2026-06-18", 14, 0),   # Fed Decision
    ("2026-06-27", 8, 30),   # PCE
    ("2026-07-02", 8, 30),   # NFP
    ("2026-07-09", 8, 30),   # CPI
    ("2026-07-29", 14, 0),   # Fed Decision
]

def _build_event_ranges(windows, suppress_minutes=15):
    ranges = set()
    for date_str, h, m in windows:
        base = h * 60 + m
        for offset in range(-suppress_minutes, suppress_minutes + 1):
            total = base + offset
            eh, em = total // 60, total % 60
            if 0 <= eh < 24:
                ranges.add((date_str, eh, em))
    return ranges

_EVENT_RANGES = _build_event_ranges(ECONOMIC_CALENDAR)

def _is_event_window(dt):
    """Returns True if dt falls within ±15min of a high-impact economic event."""
    try:
        et = dt.tz_convert('America/New_York')
        return (et.strftime("%Y-%m-%d"), et.hour, et.minute) in _EVENT_RANGES
    except:
        return False

# ─── Indicators ───────────────────────────────────────────────────────────────

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast).mean()
    ema_slow = series.ewm(span=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def bollinger_bands(series, period=20, std_dev=2):
    sma = series.rolling(period).mean()
    std = series.rolling(period).std()
    return sma + std_dev * std, sma, sma - std_dev * std

def vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum()

def atr(df, period=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()

# ─── Price Action ─────────────────────────────────────────────────────────────

def detect_patterns(df):
    patterns = []
    if len(df) < 3: return patterns
    o, h, l, c = df['Open'].iloc[-1], df['High'].iloc[-1], df['Low'].iloc[-1], df['Close'].iloc[-1]
    po, ph, pl, pc = df['Open'].iloc[-2], df['High'].iloc[-2], df['Low'].iloc[-2], df['Close'].iloc[-2]
    body = abs(c - o); rng = h - l
    lower_wick = min(o, c) - l; upper_wick = h - max(o, c)
    if rng > 0 and body / rng < 0.1: patterns.append(("doji", "neutral", 0.3))
    if pc > po and c > o and o < pc and c > po: patterns.append(("bullish_engulfing", "bullish", 0.75))
    if pc < po and c < o and o > pc and c < po: patterns.append(("bearish_engulfing", "bearish", 0.75))
    if body > 0 and lower_wick > 2 * body and upper_wick < body * 0.3: patterns.append(("hammer", "bullish", 0.65))
    if body > 0 and upper_wick > 2 * body and lower_wick < body * 0.3: patterns.append(("shooting_star", "bearish", 0.65))
    if rng > 0 and body / rng > 0.7: patterns.append(("momentum", "bullish" if c > o else "bearish", 0.5))
    return patterns

def support_resistance(df, lookback=20):
    highs = df['High'].rolling(5, center=True).max()
    lows = df['Low'].rolling(5, center=True).min()
    resistance = df['High'][df['High'] == highs].tail(lookback).mean()
    support = df['Low'][df['Low'] == lows].tail(lookback).mean()
    return support, resistance

# ─── Signal Engine (LOCKED BASELINE v14 — 84.7% WR verified) ─────────────────
# Built from: London+US sessions + MACD crossover + FVG + RSI Divergence +
# Gap Fill + Order Blocks. Verified 84.7% WR, 72 signals, 1.2/day.

def _score_tf(df, i):
    close = df['Close']
    rsi_s = rsi(close)
    macd_line_s, sig_line_s, histogram_s = macd(close)
    bb_upper_s, bb_mid_s, bb_lower_s = bollinger_bands(close)
    vwap_val_s = vwap(df)
    avg_vol = df['Volume'].rolling(20).mean()
    vol_ratio = df['Volume'] / (avg_vol + 1e-10)
    W_RSI, W_MACD, W_VWAP = 1.0, 1.5, 1.5
    scores = []
    r = rsi_s.iloc[i]
    if r < 30:    scores.append(0.8 * W_RSI)
    elif r < 45:  scores.append(0.2 * W_RSI)
    elif r > 75:  scores.append(-0.8 * W_RSI)
    elif r > 55:  scores.append(0.3 * W_RSI)
    else:         scores.append(0.0)
    h = histogram_s.iloc[i]; hp = histogram_s.iloc[i-1] if i > 0 else 0
    if h > 0 and hp <= 0:   scores.append(0.9 * W_MACD)
    elif h < 0 and hp >= 0: scores.append(-0.9 * W_MACD)
    elif h > 0:              scores.append(0.4 * W_MACD)
    else:                    scores.append(-0.4 * W_MACD)
    bb_range = bb_upper_s.iloc[i] - bb_lower_s.iloc[i]
    bp = (df['Close'].iloc[i] - bb_lower_s.iloc[i]) / bb_range if bb_range > 0 else 0.5
    if bp < 0.1:   scores.append(0.7)
    elif bp > 0.9: scores.append(0.0)
    else:          scores.append((bp - 0.5) * -0.4)
    price = close.iloc[i]; vv = vwap_val_s.iloc[i]; vd = (price / vv - 1)
    if vd > 0.001:    scores.append(0.4 * W_VWAP)
    elif vd < -0.001: scores.append(-0.4 * W_VWAP)
    else:             scores.append(0.0)
    vr = vol_ratio.iloc[i]
    if vr > 1.3:
        pm = close.iloc[i] - close.iloc[i-1]
        scores.append(0.3 if pm > 0 else -0.3)
    return float(np.mean(scores)) if scores else 0.0

def _rsi_divergence(df, i, rsi_s, lookback=10):
    if i < lookback: return 0
    pn = df['Close'].iloc[i]; pp = df['Close'].iloc[i-lookback]
    rn = rsi_s.iloc[i]; rp = rsi_s.iloc[i-lookback]
    if pn > pp and rn < rp: return -1
    if pn < pp and rn > rp: return 1
    return 0

def _gap_fill(df, i, lookback=50):
    if i < lookback: return 0, 0.0
    cp = df['Close'].iloc[i]
    for j in range(max(1, i-lookback), i):
        pc = df['Close'].iloc[j-1]; co = df['Open'].iloc[j]; gap = co - pc
        if abs(gap) < 5: continue
        if abs(cp - pc) < 3.0:
            return (1 if gap < 0 else -1), min(abs(gap)/20.0, 1.0)
    return 0, 0.0

def _detect_fvg(df, i, lookback=15):
    if i < 2: return None, False, 0.0
    cp = df['Close'].iloc[i]; best_type = None; best_in = False; best_st = 0.0
    for j in range(max(2, i-lookback), i+1):
        if j >= len(df): break
        h2 = df['High'].iloc[j-2]; l2 = df['Low'].iloc[j-2]
        h0 = df['High'].iloc[j];   l0 = df['Low'].iloc[j]
        if h2 < l0:
            sz = l0-h2; in_fvg = h2<=cp<=l0; st = min(sz/10.0,1.0)
            if st > best_st: best_type='bullish'; best_in=in_fvg; best_st=st
        if l2 > h0:
            sz = l2-h0; in_fvg = h0<=cp<=l2; st = min(sz/10.0,1.0)
            if st > best_st: best_type='bearish'; best_in=in_fvg; best_st=st
    return best_type, best_in, best_st

def _detect_ob(df, i, lookback=20):
    if i < lookback + 3: return 0, 0.0
    cp = df['Close'].iloc[i]
    for j in range(i-2, max(i-lookback, 3), -1):
        if all(df['Close'].iloc[j-k] > df['Open'].iloc[j-k] for k in range(3)):
            oh = df['High'].iloc[j-3]; ol = df['Low'].iloc[j-3]
            if ol <= cp <= oh: return 1, min((oh-ol)/15.0, 1.0)
        if all(df['Close'].iloc[j-k] < df['Open'].iloc[j-k] for k in range(3)):
            oh = df['High'].iloc[j-3]; ol = df['Low'].iloc[j-3]
            if ol <= cp <= oh: return -1, min((oh-ol)/15.0, 1.0)
    return 0, 0.0

def _get_window(timestamp):
    import pytz
    PST = pytz.timezone('US/Pacific')
    try:
        if timestamp.tzinfo is None:
            ts = timestamp.tz_localize('UTC').tz_convert(PST)
        else:
            ts = timestamp.tz_convert(PST)
        t = ts.hour * 60 + ts.minute
        if 2*60 <= t < 4*60:           return 'london'
        elif 6*60+30 <= t <= 10*60+30: return 'us'
        return None
    except:
        return None

def generate_signal(df_5m, df_1h=None, df_1m=None):
    """
    LOCKED BASELINE — 84.7% WR verified (nq_ob_verify.py)
    + Economic Calendar filter added (no WR impact, loss prevention)
    """
    i = len(df_5m) - 1
    if i < 30: return None

    close = df_5m['Close']
    current_price = float(close.iloc[-1])
    rsi_val = rsi(close).iloc[-1]
    macd_line, sig_line, histogram = macd(close)
    bb_upper, bb_mid, bb_lower = bollinger_bands(close)
    vwap_val = vwap(df_5m).iloc[-1]
    atr_val = atr(df_5m).iloc[-1]
    avg_vol = df_5m['Volume'].rolling(20).mean().iloc[-1]
    vol_ratio = df_5m['Volume'].iloc[-1] / avg_vol if avg_vol > 0 else 1.0

    ct = df_5m.index[-1]
    window = _get_window(ct)
    thr = 0.45 if window == 'london' else 0.38

    # ─── Economic Calendar Check ──────────────────────────────────────────────
    in_event_window = _is_event_window(ct)

    # MACD crossover check (within 5 candles)
    crossed_bull = any(
        histogram.iloc[-(k+1)] > 0 and histogram.iloc[-(k+2)] <= 0
        for k in range(5) if i-k-1 >= 0
    )
    crossed_bear = any(
        histogram.iloc[-(k+1)] < 0 and histogram.iloc[-(k+2)] >= 0
        for k in range(5) if i-k-1 >= 0
    )

    # Score timeframes
    s5 = _score_tf(df_5m, i)
    s1h = 0.0
    if df_1h is not None and len(df_1h) >= 30:
        s1h = _score_tf(df_1h, len(df_1h)-1)
    s1m = s5
    if df_1m is not None and len(df_1m) >= 30:
        s1m = _score_tf(df_1m, len(df_1m)-1)

    # Weighted composite: 1h=50%, 5m=35%, 1m=15%
    final = (s1h * 0.50) + (s5 * 0.35) + (s1m * 0.15)

    # Alignment bonus
    if s1h > 0.15 and s5 > 0.15 and s1m > 0.15:    final += 0.10
    elif s1h < -0.15 and s5 < -0.15 and s1m < -0.15: final -= 0.10

    # RSI Divergence
    rsi_s = rsi(close)
    div = _rsi_divergence(df_5m, i, rsi_s)
    if div == -1 and final > 0: final *= 0.80
    if div ==  1 and final < 0: final *= 0.80
    if div ==  1 and final > 0: final += 0.08
    if div == -1 and final < 0: final -= 0.08

    # Gap Fill
    gd, gs = _gap_fill(df_5m, i)
    if gd ==  1 and final > 0: final += 0.10 * gs
    if gd == -1 and final < 0: final -= 0.10 * gs

    # FVG
    ft, fi, fst = _detect_fvg(df_5m, i)
    if ft == 'bullish': final += 0.12 * (1.5 if fi else 0.6) * fst
    elif ft == 'bearish': final -= 0.12 * (1.5 if fi else 0.6) * fst

    # Order Blocks
    ob_dir, ob_st = _detect_ob(df_5m, i)
    if ob_dir ==  1: final += 0.10 * ob_st
    if ob_dir == -1: final -= 0.10 * ob_st

    # ─── Signal Decision ──────────────────────────────────────────────────────
    # Force HOLD if: no MACD cross, outside session, OR in event window
    if not (crossed_bull or crossed_bear) or window is None or in_event_window:
        signal = "HOLD"
        final = 0.0
    else:
        signal = "BUY" if final > thr else "SELL" if final < -thr else "HOLD"

    confidence = min(abs(final) * 100, 95)

    # Build reasons
    reasons = []
    if in_event_window:
        reasons.append("⚠️ HIGH-IMPACT EVENT WINDOW — signal suppressed")
    if window:
        reasons.append(f"Session: {window.upper()}")
    if crossed_bull: reasons.append("MACD bullish crossover ✦")
    if crossed_bear: reasons.append("MACD bearish crossover ✦")
    if rsi_val < 30:  reasons.append(f"RSI oversold ({rsi_val:.1f})")
    elif rsi_val > 70: reasons.append(f"RSI overbought ({rsi_val:.1f})")
    else:             reasons.append(f"RSI {rsi_val:.1f}")
    if ft: reasons.append(f"FVG: {ft} {'(in gap)' if fi else ''}")
    if ob_dir != 0: reasons.append(f"Order Block: {'bullish' if ob_dir==1 else 'bearish'} (str {ob_st:.2f})")
    if div != 0: reasons.append(f"RSI divergence: {'bullish' if div==1 else 'bearish'}")
    if gd != 0: reasons.append(f"Gap fill level nearby")
    reasons.append(f"Score: {final:.3f} (thr {thr})")

    support, resistance = support_resistance(df_5m)

    TP_POINTS = 60
    SL_POINTS = 20
    if signal == 'BUY':
        tp_price = round(current_price + TP_POINTS, 2)
        sl_price = round(current_price - SL_POINTS, 2)
    elif signal == 'SELL':
        tp_price = round(current_price - TP_POINTS, 2)
        sl_price = round(current_price + SL_POINTS, 2)
    else:
        tp_price = None
        sl_price = None

    hist_now = float(histogram.iloc[-1])
    bb_range = float(bb_upper.iloc[-1] - bb_lower.iloc[-1])
    bb_pos_val = (current_price - float(bb_lower.iloc[-1])) / bb_range if bb_range > 0 else 0.5

    return {
        "signal": signal,
        "score": round(final, 3),
        "confidence": round(confidence, 1),
        "price": round(float(current_price), 2),
        "timestamp": df_5m.index[-1].isoformat(),
        "session": window or "outside_session",
        "event_window": in_event_window,
        "indicators": {
            "rsi": round(float(rsi_val), 2),
            "macd_histogram": round(float(hist_now), 4),
            "macd_line": round(float(macd_line.iloc[-1]), 4),
            "signal_line": round(float(sig_line.iloc[-1]), 4),
            "bb_upper": round(float(bb_upper.iloc[-1]), 2),
            "bb_lower": round(float(bb_lower.iloc[-1]), 2),
            "bb_mid": round(float(bb_mid.iloc[-1]), 2),
            "bb_position": round(float(bb_pos_val), 3),
            "vwap": round(float(vwap_val), 2),
            "atr": round(float(atr_val), 2),
            "fvg_type": ft or "none",
            "fvg_in_gap": bool(fi),
            "ob_direction": int(ob_dir),
            "ob_strength": round(float(ob_st), 3),
        },
        "volume": {
            "current": int(df_5m['Volume'].iloc[-1]),
            "average": int(avg_vol),
            "ratio": round(float(vol_ratio), 2),
            "spike": bool(vol_ratio > 1.5)
        },
        "patterns": [],
        "support": round(float(support), 2),
        "resistance": round(float(resistance), 2),
        "contracts": (3 if abs(final) >= 0.55 else 2 if abs(final) >= 0.45 else 1) if signal != "HOLD" else 0,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_points": TP_POINTS if signal != "HOLD" else None,
        "sl_points": SL_POINTS if signal != "HOLD" else None,
        "reasons": reasons,
        "price_history": [round(float(x), 2) for x in close.tail(60).tolist()],
        "high_history": [round(float(x), 2) for x in df_5m['High'].tail(60).tolist()],
        "low_history": [round(float(x), 2) for x in df_5m['Low'].tail(60).tolist()],
        "open_history": [round(float(x), 2) for x in df_5m['Open'].tail(60).tolist()],
        "volume_history": [int(x) for x in df_5m['Volume'].tail(60).tolist()],
        "timestamps": [t.isoformat() for t in df_5m.index[-60:]]
    }

@app.route('/')
def index():
    import os
    dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nq_dashboard.html')
    if os.path.exists(dashboard):
        with open(dashboard, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    return "<h1>Dashboard not found.</h1>", 404

PUSHOVER_TOKEN = "av7z24evdn1h55qqkk4gxptm94uk9q"
PUSHOVER_USER  = "ui2s5wt3qxb1zt75sphspwubx4ntac"
last_signal = {"signal": "HOLD", "price": 0}

def send_pushover(signal, price, confidence, score, result=None):
    try:
        import urllib.request, urllib.parse
        tp = result.get('tp_price'); sl = result.get('sl_price')
        sc = abs(score)
        if sc >= 0.55:   size_label = '3 contracts (HIGH conviction)'
        elif sc >= 0.45: size_label = '2 contracts (SOLID conviction)'
        else:            size_label = '1 contract (standard)'
        tp_sl = (f' | TP: {tp} SL: {sl}') if tp else ''
        message = (signal + ' - NQ at ' + str(round(price, 2)) +
                   ' | Score: ' + str(round(score, 3)) +
                   ' | Size: ' + size_label + tp_sl)
        data = urllib.parse.urlencode({
            "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
            "title": f"NQ Signal: {signal}", "message": message,
            "priority": 1 if signal != "HOLD" else 0,
            "sound": "cashregister" if signal == "BUY" else "siren" if signal == "SELL" else "none"
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request("https://api.pushover.net/1/messages.json", data=data),
            timeout=5
        )
        print(f"📱 Pushover sent: {signal} @ {price}")
    except Exception as e:
        print(f"Pushover error: {e}")

@app.route("/signal")
def get_signal():
    global last_signal
    try:
        ticker_obj = yf.Ticker(TICKER)
        df_5m = ticker_obj.history(interval="5m", period="5d")
        df_1h = ticker_obj.history(interval="1h", period="60d")
        df_1m = ticker_obj.history(interval="1m", period="7d")
        if df_5m.empty:
            return jsonify({"error": "No data returned"}), 500
        result = generate_signal(df_5m, df_1h, df_1m)
        new_sig = result["signal"]
        if new_sig != last_signal["signal"] and new_sig in ("BUY", "SELL"):
            send_pushover(new_sig, result["price"], result["confidence"], result["score"], result)
        last_signal = {"signal": new_sig, "price": result["price"]}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/commentary')
def get_commentary():
    try:
        import os, urllib.request, urllib.parse, json as json_mod
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({"commentary": "API key not configured."})
        ticker_obj2 = yf.Ticker(TICKER)
        df_5m = ticker_obj2.history(interval="5m", period="5d")
        df_1h = ticker_obj2.history(interval="1h", period="60d")
        df_1m = ticker_obj2.history(interval="1m", period="7d")
        if df_5m.empty:
            return jsonify({"commentary": "No market data available."})
        result = generate_signal(df_5m, df_1h, df_1m)
        sig = result["signal"]; price = result["price"]; conf = result["confidence"]
        score = result["score"]; rsi_val = result["indicators"]["rsi"]
        macd_hist = result["indicators"]["macd_histogram"]
        bb_pos = result["indicators"]["bb_position"]
        vwap_val = result["indicators"]["vwap"]
        vol_ratio = result["volume"]["ratio"]
        patterns = result["patterns"]; reasons = result["reasons"]
        support = result["support"]; resistance = result["resistance"]
        event_warning = " NOTE: Signal suppressed — high-impact event window active." if result.get("event_window") else ""
        prompt = f"""You are a trading coach explaining NQ futures signals to someone still learning. Be clear, specific, and educational.{event_warning}

Current Signal: {sig} (confidence: {conf}%, score: {score})
Price: {price} | VWAP: {vwap_val}
RSI: {rsi_val} | MACD Histogram: {macd_hist}
BB Position: {bb_pos*100:.0f}% of band
Support: {support} | Resistance: {resistance}
Volume ratio: {vol_ratio}x average
Patterns detected: {', '.join([p[0] for p in patterns]) if patterns else 'none'}
Reasons that fired: {'; '.join(reasons)}

Write 3-4 sentences in plain English:
1. Start with exactly what triggered the {sig} signal
2. Explain what each key indicator is showing right now in simple terms
3. Tell me what to watch for next — what would confirm or cancel this signal
Keep it conversational, no jargon without explanation."""

        data = json_mod.dumps({
            "model": "claude-3-5-haiku-20241022",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=data,
            headers={"Content-Type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        resp_data = json_mod.loads(resp.read())
        commentary = resp_data["content"][0]["text"]
        return jsonify({"commentary": commentary})
    except Exception as e:
        return jsonify({"commentary": f"Commentary unavailable: {str(e)}"})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "ticker": TICKER})

if __name__ == "__main__":
    print("🚀 NQ Signal Agent running at http://localhost:5000")
    print("  GET /signal — fetch latest signal")
    print("  GET /health — health check")
    app.run(host="0.0.0.0", port=8080, debug=False)
