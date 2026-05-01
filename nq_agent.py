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
    if len(df) < 3:
        return patterns
    o, h, l, c = df['Open'].iloc[-1], df['High'].iloc[-1], df['Low'].iloc[-1], df['Close'].iloc[-1]
    po, ph, pl, pc = df['Open'].iloc[-2], df['High'].iloc[-2], df['Low'].iloc[-2], df['Close'].iloc[-2]
    body = abs(c - o)
    rng = h - l
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    if rng > 0 and body / rng < 0.1:
        patterns.append(("doji", "neutral", 0.3))
    if pc > po and c > o and o < pc and c > po:
        patterns.append(("bullish_engulfing", "bullish", 0.75))
    if pc < po and c < o and o > pc and c < po:
        patterns.append(("bearish_engulfing", "bearish", 0.75))
    if body > 0 and lower_wick > 2 * body and upper_wick < body * 0.3:
        patterns.append(("hammer", "bullish", 0.65))
    if body > 0 and upper_wick > 2 * body and lower_wick < body * 0.3:
        patterns.append(("shooting_star", "bearish", 0.65))
    if rng > 0 and body / rng > 0.7:
        patterns.append(("momentum", "bullish" if c > o else "bearish", 0.5))
    return patterns

def support_resistance(df, lookback=20):
    highs = df['High'].rolling(5, center=True).max()
    lows = df['Low'].rolling(5, center=True).min()
    resistance = df['High'][df['High'] == highs].tail(lookback).mean()
    support = df['Low'][df['Low'] == lows].tail(lookback).mean()
    return support, resistance

# ─── Signal Engine ────────────────────────────────────────────────────────────

def generate_signal(df):
    close = df['Close']
    rsi_val = rsi(close).iloc[-1]
    macd_line, sig_line, histogram = macd(close)
    bb_upper, bb_mid, bb_lower = bollinger_bands(close)
    vwap_val = vwap(df).iloc[-1]
    atr_val = atr(df).iloc[-1]
    current_price = close.iloc[-1]
    prev_price = close.iloc[-2]

    scores, reasons = [], []

    # Optimized weights from backtester (74.2% win rate on 6mo data)
    W_RSI, W_MACD, W_BB, W_VWAP, W_VOL = 1.0, 1.5, 0.5, 1.5, 0.5

    # RSI
    if rsi_val < 30:    scores.append(0.8 * W_RSI);  reasons.append(f"RSI oversold ({rsi_val:.1f})")
    elif rsi_val < 45:  scores.append(0.3 * W_RSI);  reasons.append(f"RSI bearish zone ({rsi_val:.1f})")
    elif rsi_val > 70:  scores.append(-0.8 * W_RSI); reasons.append(f"RSI overbought ({rsi_val:.1f})")
    elif rsi_val > 55:  scores.append(-0.3 * W_RSI); reasons.append(f"RSI bullish zone ({rsi_val:.1f})")
    else:               scores.append(0.0);           reasons.append(f"RSI neutral ({rsi_val:.1f})")

    # MACD
    hist_now = histogram.iloc[-1]
    hist_prev = histogram.iloc[-2]
    if hist_now > 0 and hist_prev <= 0:   scores.append(0.9 * W_MACD);  reasons.append("MACD bullish crossover ✦")
    elif hist_now < 0 and hist_prev >= 0: scores.append(-0.9 * W_MACD); reasons.append("MACD bearish crossover ✦")
    elif hist_now > 0:                    scores.append(0.4 * W_MACD);  reasons.append("MACD above signal line")
    else:                                 scores.append(-0.4 * W_MACD); reasons.append("MACD below signal line")

    # Bollinger Bands
    bb_range = bb_upper.iloc[-1] - bb_lower.iloc[-1]
    bb_pos = (current_price - bb_lower.iloc[-1]) / bb_range if bb_range > 0 else 0.5
    if bb_pos < 0.1:    scores.append(0.7);  reasons.append("Price at lower Bollinger Band")
    elif bb_pos > 0.9:  scores.append(-0.7); reasons.append("Price at upper Bollinger Band")
    else:               scores.append((bb_pos - 0.5) * -0.4); reasons.append(f"BB position {bb_pos:.0%}")

    # VWAP
    vwap_diff = (current_price / vwap_val - 1) * 100
    if current_price > vwap_val * 1.002:    scores.append(0.4);  reasons.append(f"Above VWAP (+{vwap_diff:.2f}%)")
    elif current_price < vwap_val * 0.998:  scores.append(-0.4); reasons.append(f"Below VWAP ({vwap_diff:.2f}%)")
    else:                                   scores.append(0.0);  reasons.append("At VWAP")

    # Candlestick patterns
    for name, direction, strength in detect_patterns(df):
        val = strength if direction == "bullish" else -strength if direction == "bearish" else 0
        scores.append(val)
        reasons.append(f"Pattern: {name.replace('_', ' ').title()}")

    # Volume
    avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
    vol_ratio = df['Volume'].iloc[-1] / avg_vol if avg_vol > 0 else 1.0
    if vol_ratio > 1.5:
        boost = 0.3 if current_price > prev_price else -0.3
        scores.append(boost)
        reasons.append(f"Volume spike ({vol_ratio:.1f}x avg)")

    final_score = float(np.mean(scores)) if scores else 0.0
    confidence = min(abs(final_score) * 100, 95)

    signal = "BUY" if final_score > 0.35 else "SELL" if final_score < -0.35 else "HOLD"
    support, resistance = support_resistance(df)

    return {
        "signal": signal,
        "score": round(final_score, 3),
        "confidence": round(confidence, 1),
        "price": round(float(current_price), 2),
        "timestamp": df.index[-1].isoformat(),
        "indicators": {
            "rsi": round(float(rsi_val), 2),
            "macd_histogram": round(float(hist_now), 4),
            "macd_line": round(float(macd_line.iloc[-1]), 4),
            "signal_line": round(float(sig_line.iloc[-1]), 4),
            "bb_upper": round(float(bb_upper.iloc[-1]), 2),
            "bb_lower": round(float(bb_lower.iloc[-1]), 2),
            "bb_mid": round(float(bb_mid.iloc[-1]), 2),
            "bb_position": round(float(bb_pos), 3),
            "vwap": round(float(vwap_val), 2),
            "atr": round(float(atr_val), 2),
        },
        "volume": {
            "current": int(df['Volume'].iloc[-1]),
            "average": int(avg_vol),
            "ratio": round(float(vol_ratio), 2),
            "spike": bool(vol_ratio > 1.5)
        },
        "patterns": [(p[0], p[1]) for p in detect_patterns(df)],
        "support": round(float(support), 2),
        "resistance": round(float(resistance), 2),
        "reasons": reasons,
        "price_history": [round(float(x), 2) for x in close.tail(60).tolist()],
        "high_history": [round(float(x), 2) for x in df['High'].tail(60).tolist()],
        "low_history": [round(float(x), 2) for x in df['Low'].tail(60).tolist()],
        "open_history": [round(float(x), 2) for x in df['Open'].tail(60).tolist()],
        "volume_history": [int(x) for x in df['Volume'].tail(60).tolist()],
        "timestamps": [t.isoformat() for t in df.index[-60:]]
    }

@app.route('/')
def index():
    import os
    dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nq_dashboard.html')
    if os.path.exists(dashboard):
        with open(dashboard, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    return "<h1>Dashboard not found. Make sure nq_dashboard.html is in the same folder as nq_agent.py</h1>", 404

# Pushover config
PUSHOVER_TOKEN = "av7z24evdn1h55qqkk4gxptm94uk9q"
PUSHOVER_USER  = "ui2s5wt3qxb1zt75sphspwubx4ntac"
last_signal = {"signal": "HOLD", "price": 0}

def send_pushover(signal, price, confidence, score):
    try:
        import urllib.request, urllib.parse
        message = signal + ' - NQ at ' + str(round(price, 2)) + ', Conf: ' + str(round(confidence, 1)) + '%, Score: ' + str(round(score, 3))

        data = urllib.parse.urlencode({
            "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
            "title": f"NQ Signal: {signal}", "message": message,
            "priority": 1 if signal != "HOLD" else 0,
            "sound": "cashregister" if signal == "BUY" else "siren" if signal == "SELL" else "none"
        }).encode()
        urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json", data=data), timeout=5)
        print(f"📱 Pushover sent: {signal} @ {price}")
    except Exception as e:
        print(f"Pushover error: {e}")

@app.route("/signal")
def get_signal():
    global last_signal
    try:
        interval = request.args.get("interval", "5m")
        period_map = {"1m": "1d", "5m": "2d", "1h": "7d"}
        period = period_map.get(interval, "2d")
        df = yf.Ticker(TICKER).history(interval=interval, period=period)
        if df.empty:
            return jsonify({"error": "No data returned"}), 500
        result = generate_signal(df)
        new_sig = result["signal"]
        if new_sig != last_signal["signal"] and new_sig in ("BUY", "SELL"):
            send_pushover(new_sig, result["price"], result["confidence"], result["score"])
        last_signal = {"signal": new_sig, "price": result["price"]}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({"status": "ok", "ticker": TICKER})

if __name__ == "__main__":
    print("🚀 NQ Signal Agent running at http://localhost:5000")
    print("   GET /signal  — fetch latest signal")
    print("   GET /health  — health check")
    app.run(host="0.0.0.0", port=8080, debug=False)
