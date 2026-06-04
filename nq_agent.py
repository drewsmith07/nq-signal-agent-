#!/usr/bin/env python3
"""
NQ Futures Scalping Signal Agent - v3.3
Real-time data via ProjectX/TopstepX API — zero lag
+ Signal history logging (persists to signals_log.json)

v3.2 changes (June 1, 2026):
  1. SL widened 20pts → 25pts (reduces noise stop-outs)
  2. Position sizing thresholds lowered: 2ct at 0.40+, 3ct at 0.48+
  3. Steep 5-contract scaling: 4ct at 0.52+, 5ct at 0.56+ (eval accounts only)
  4. Plan C: hard block BUY on bearish RSI divergence, SELL on bullish divergence
  Backtest result: 96 signals, 51% WR, $118,100 net P&L over 60 days

v3.3 changes (June 2, 2026):
  1. 15m EMA 9/21 trend filter added as pre-signal gate
     - BUY only fires when 15m EMA9 > EMA21 (uptrend confirmed)
     - SELL only fires when 15m EMA9 < EMA21 (downtrend confirmed)
  Backtest result: 85 signals, 58.8% WR, $139,000 net P&L over 60 days
"""

import pandas as pd
import numpy as np
import json
import os
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Signal History ───────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'signals_log.json')
_signal_history = []

def _log_to_notion(entry):
    if not NOTION_TOKEN:
        return
    try:
        import requests as _req
        signal = entry.get("signal", "UNKNOWN")
        direction = "BUY" if "BUY" in str(signal).upper() else "SELL" if "SELL" in str(signal).upper() else None
        fired_at = entry.get("logged_at", "")[:10]
        props = {
            "Signal": {"title": [{"text": {"content": f"{signal} @ {entry.get('price', '?')}"}}]},
            "Price": {"number": entry.get("price")},
            "TP Price": {"number": entry.get("tp_price")},
            "SL Price": {"number": entry.get("sl_price")},
            "Confidence": {"number": entry.get("confidence")},
            "Session": {"select": {"name": entry.get("session", "Unknown")}},
            "Result": {"select": {"name": "Open"}},
            "Fired At": {"date": {"start": fired_at}}
        }
        if direction:
            props["Direction"] = {"select": {"name": direction}}
        _req.post("https://api.notion.com/v1/pages",
            headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
            json={"parent": {"database_id": NOTION_SIGNAL_DB}, "properties": props})
    except Exception as e:
        print(f"[Notion] Failed to log signal: {e}")

def _load_history():
    global _signal_history
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                _signal_history = json.load(f)
            print(f"[History] Loaded {len(_signal_history)} signals from disk.")
        else:
            _signal_history = []
            print("[History] No existing log found — starting fresh.")
    except Exception as e:
        print(f"[History] Failed to load log: {e}")
        _signal_history = []

def _save_history():
    try:
        with open(LOG_FILE, 'w') as f:
            json.dump(_signal_history, f)
    except Exception as e:
        print(f"[History] Failed to save log: {e}")

def _log_signal(result):
    import pytz
    pst = pytz.timezone('US/Pacific')
    now_pst = datetime.now(pst).isoformat()
    entry = {
        "logged_at":  now_pst,
        "signal":     result.get("signal"),
        "price":      result.get("price"),
        "score":      result.get("score"),
        "confidence": result.get("confidence"),
        "session":    result.get("session"),
        "event_window": result.get("event_window", False),
        "contracts":  result.get("contracts", 0),
        "tp_price":   result.get("tp_price"),
        "sl_price":   result.get("sl_price"),
        "tp_points":  result.get("tp_points"),
        "sl_points":  result.get("sl_points"),
        "indicators": {
            "rsi":            result["indicators"].get("rsi"),
            "macd_histogram": result["indicators"].get("macd_histogram"),
            "bb_position":    result["indicators"].get("bb_position"),
            "vwap":           result["indicators"].get("vwap"),
            "atr":            result["indicators"].get("atr"),
            "fvg_type":       result["indicators"].get("fvg_type"),
            "ob_direction":   result["indicators"].get("ob_direction"),
        },
        "volume_ratio": result.get("volume", {}).get("ratio"),
        "reasons":    result.get("reasons", []),
        "result":     None,
        "pnl":        None,
    }
    _signal_history.append(entry)
    _log_to_notion(entry)
    if len(_signal_history) > 5000:
        _signal_history.pop(0)
    _save_history()

_load_history()

# ─── ProjectX Config ──────────────────────────────────────────────────────────
PX_USERNAME = 'drewksmith602@gmail.com'
PX_API_KEY  = '2AEN4l/nMCiRnnJXOZRed3kjOWfczuszBKZogj+1njM='
PX_BASE_URL = 'https://api.topstepx.com/api'
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_SIGNAL_DB = "c2068631-1c42-4fc2-89bd-94b86cae01c4"


NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_SIGNAL_DB = "c2068631-1c42-4fc2-89bd-94b86cae01c4"

_px_token = None
_px_token_expiry = None

def _resolve_front_month():
    """Auto-resolve the front-month NQ contract by checking expiry dates."""
    try:
        token = get_px_token()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        r = requests.post(f'{PX_BASE_URL}/Contract/search',
            headers=headers,
            json={'searchText': 'ENQ', 'live': True},
            timeout=10)
        contracts = r.json().get('contracts', [])
        now = datetime.now(timezone.utc)
        valid = []
        for c in contracts:
            exp = c.get('expirationDate') or c.get('expiration') or ''
            try:
                exp_dt = datetime.fromisoformat(exp.replace('Z', '+00:00'))
                if exp_dt > now:
                    valid.append((exp_dt, c['id']))
            except:
                pass
        if valid:
            valid.sort(key=lambda x: x[0])
            return valid[0][1]
    except Exception as e:
        print(f"[Contract] Auto-resolve failed: {e}")
    return 'CON.F.US.ENQ.U26'

NQ_CONTRACT = 'CON.F.US.ENQ.U26'  # Front month — update to U26 on June rollover
print(f"[Contract] Using: {NQ_CONTRACT}")

def get_px_token():
    global _px_token, _px_token_expiry
    now = datetime.now(timezone.utc)
    if _px_token and _px_token_expiry and now < _px_token_expiry:
        return _px_token
    r = requests.post(f'{PX_BASE_URL}/Auth/loginKey',
        headers={'Content-Type': 'application/json'},
        json={'userName': PX_USERNAME, 'apiKey': PX_API_KEY},
        timeout=10)
    data = r.json()
    if not data.get('success'):
        raise Exception(f"ProjectX auth failed: {data}")
    _px_token = data['token']
    _px_token_expiry = now + timedelta(hours=23)
    print(f"[ProjectX] Authenticated successfully")
    return _px_token

def get_nq_bars(interval_minutes=5, lookback_days=5, limit=300):
    import yfinance as yf
    interval_map = {1: '1m', 5: '5m', 15: '15m', 60: '1h'}
    yf_interval = interval_map.get(interval_minutes, '5m')
    period_map = {1: '7d', 5: '60d', 15: '60d', 60: '60d'}
    yf_period = period_map.get(interval_minutes, '60d')
    df = yf.download('NQ=F', period=yf_period, interval=yf_interval, progress=False, auto_adjust=True)
    if df.empty:
        raise Exception("No bars returned from yfinance")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df.tail(limit)
    return df

# ─── Economic Calendar ────────────────────────────────────────────────────────
ECONOMIC_CALENDAR = [
    ("2026-05-07", 14, 0),
    ("2026-05-09", 8, 30),
    ("2026-05-13", 8, 30),
    ("2026-05-30", 8, 30),
    ("2026-06-06", 8, 30),
    ("2026-06-11", 8, 30),
    ("2026-06-18", 14, 0),
    ("2026-06-27", 8, 30),
    ("2026-07-02", 8, 30),
    ("2026-07-09", 8, 30),
    ("2026-07-29", 14, 0),
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
    avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast).mean()
    ema_slow = series.ewm(span=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal).mean()
    return macd_line, signal_line, macd_line - signal_line

def bollinger_bands(series, period=20):
    sma = series.rolling(period).mean()
    std = series.rolling(period).std()
    return sma + 2*std, sma, sma - 2*std

def vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum()

def atr(df, period=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(com=period-1, min_periods=period).mean()

def support_resistance(df, lookback=20):
    highs = df['High'].rolling(5, center=True).max()
    lows = df['Low'].rolling(5, center=True).min()
    resistance = df['High'][df['High'] == highs].tail(lookback).mean()
    support = df['Low'][df['Low'] == lows].tail(lookback).mean()
    return support, resistance

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
        h0 = df['High'].iloc[j]; l0 = df['Low'].iloc[j]
        if h2 < l0:
            sz = l0-h2; in_fvg = h2<=cp<=l0; st = min(sz/10.0,1.0)
            if st > best_st: best_type='bullish'; best_in=in_fvg; best_st=st
        if l2 > h0:
            sz = l2-h0; in_fvg = h0<=cp<=l2; st = min(sz/10.0,1.0)
            if st > best_st: best_type='bearish'; best_in=in_fvg; best_st=st
    return best_type, best_in, best_st

def _detect_ob(df, i, lookback=20):
    if i < lookback+3: return 0, 0.0
    cp = df['Close'].iloc[i]
    for j in range(i-2, max(i-lookback, 3), -1):
        if all(df['Close'].iloc[j-k] > df['Open'].iloc[j-k] for k in range(3)):
            oh = df['High'].iloc[j-3]; ol = df['Low'].iloc[j-3]
            if ol <= cp <= oh: return 1, min((oh-ol)/15.0, 1.0)
        if all(df['Close'].iloc[j-k] < df['Open'].iloc[j-k] for k in range(3)):
            oh = df['High'].iloc[j-3]; ol = df['Low'].iloc[j-3]
            if ol <= cp <= oh: return -1, min((oh-ol)/15.0, 1.0)
    return 0, 0.0

def _check_15m_ema_trend(df_15m, signal):
    """
    v3.3: 15m EMA 9/21 trend filter.
    Returns True if 15m trend aligns with signal direction.
    BUY  requires EMA9 > EMA21 on 15m (uptrend)
    SELL requires EMA9 < EMA21 on 15m (downtrend)
    """
    try:
        if df_15m is None or len(df_15m) < 22:
            return True  # not enough data — don't block, pass through
        close = df_15m['Close']
        ema9  = close.ewm(span=9,  adjust=False).mean().iloc[-1]
        ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
        if signal == 'BUY':
            return ema9 > ema21
        elif signal == 'SELL':
            return ema9 < ema21
        return True
    except Exception as e:
        print(f"[15m EMA] check failed: {e}")
        return True  # fail open — never silently block signals on error

def _get_window(timestamp):
    import pytz
    PST = pytz.timezone('US/Pacific')
    try:
        ts = timestamp.tz_convert(PST) if timestamp.tzinfo else timestamp.tz_localize('UTC').tz_convert(PST)
        t = ts.hour * 60 + ts.minute
        if 2*60 <= t < 4*60:           return 'london'
        elif 6*60+30 <= t <= 10*60+30: return 'us'
        return None
    except:
        return None

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
    bp = (close.iloc[i] - bb_lower_s.iloc[i]) / bb_range if bb_range > 0 else 0.5
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

# ─── Signal Engine ────────────────────────────────────────────────────────────

# v3.2 constants
TP_POINTS = 60
SL_POINTS = 25  # FIX 1: widened from 20 to 25

def _get_contracts(score_abs):
    """
    v3.2 Steep 5-contract sizing.
    Thresholds lowered + scaled to 5 contracts for high-conviction signals.
    Valid for Apex $50K eval (max 6 contracts allowed).
    Cap at 3 contracts on funded PA (Apex PA starts at 2ct, scales up).
    """
    if score_abs >= 0.56:  return 5   # FIX 3: 5ct tier
    elif score_abs >= 0.52: return 4  # FIX 3: 4ct tier
    elif score_abs >= 0.48: return 3  # FIX 2: was 0.55
    elif score_abs >= 0.40: return 2  # FIX 2: was 0.45
    else:                   return 1

def generate_signal(df_5m, df_1h=None, df_1m=None, df_15m=None):
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
    vol_ratio_val = df_5m['Volume'].iloc[-1] / avg_vol if avg_vol > 0 else 1.0

    real_now = pd.Timestamp.now(tz='UTC')
    window = _get_window(real_now)
    in_event_window = _is_event_window(real_now)

    thr = 0.45 if window == 'london' else 0.38
    xwin = 3 if window == 'london' else 5

    crossed_bull = any(
        histogram.iloc[-(k+1)] > 0 and histogram.iloc[-(k+2)] <= 0
        for k in range(xwin) if i-k-1 >= 0
    )
    crossed_bear = any(
        histogram.iloc[-(k+1)] < 0 and histogram.iloc[-(k+2)] >= 0
        for k in range(xwin) if i-k-1 >= 0
    )

    # ── PLAN C: RSI Divergence Hard Block (v3.2) ─────────────────────────────
    rsi_s = rsi(close)
    div = _rsi_divergence(df_5m, i, rsi_s)
    # Hard block: BUY into bearish divergence = skip entirely
    if crossed_bull and div == -1:
        crossed_bull = False
    # Hard block: SELL into bullish divergence = skip entirely
    if crossed_bear and div == 1:
        crossed_bear = False
    # ─────────────────────────────────────────────────────────────────────────

    s5 = _score_tf(df_5m, i)
    s1h = 0.0
    if df_1h is not None and len(df_1h) >= 30:
        s1h = _score_tf(df_1h, len(df_1h)-1)
    s1m = s5
    if df_1m is not None and len(df_1m) >= 30:
        s1m = _score_tf(df_1m, len(df_1m)-1)

    final = (s1h * 0.50) + (s5 * 0.35) + (s1m * 0.15)
    if s1h > 0.15 and s5 > 0.15 and s1m > 0.15:      final += 0.10
    elif s1h < -0.15 and s5 < -0.15 and s1m < -0.15: final -= 0.10

    # RSI divergence soft modifiers (unchanged)
    if div == -1 and final > 0: final *= 0.80
    if div ==  1 and final < 0: final *= 0.80
    if div ==  1 and final > 0: final += 0.08
    if div == -1 and final < 0: final -= 0.08

    gd, gs = _gap_fill(df_5m, i)
    if gd ==  1 and final > 0: final += 0.10 * gs
    if gd == -1 and final < 0: final -= 0.10 * gs

    ft, fi, fst = _detect_fvg(df_5m, i)
    if ft == 'bullish': final += 0.12 * (1.5 if fi else 0.6) * fst
    elif ft == 'bearish': final -= 0.12 * (1.5 if fi else 0.6) * fst

    ob_dir, ob_st = _detect_ob(df_5m, i)
    if ob_dir ==  1: final += 0.10 * ob_st
    if ob_dir == -1: final -= 0.10 * ob_st

    if not (crossed_bull or crossed_bear) or window is None or in_event_window:
        signal = "HOLD"
        final = 0.0
    else:
        signal = "BUY" if final > thr else "SELL" if final < -thr else "HOLD"

    # ── v3.3: 15m EMA 9/21 Trend Filter ─────────────────────────────────────
    if signal in ("BUY", "SELL") and not _check_15m_ema_trend(df_15m, signal):
        reasons.append(f"⛔ {signal} blocked: 15m EMA trend not aligned")
        signal = "HOLD"
        final  = 0.0
    # ─────────────────────────────────────────────────────────────────────────

    confidence = min(abs(final) * 100, 95)
    contracts = _get_contracts(abs(final)) if signal != "HOLD" else 0

    reasons = []
    if in_event_window: reasons.append("⚠️ HIGH-IMPACT EVENT WINDOW")
    if window: reasons.append(f"Session: {window.upper()}")
    if crossed_bull: reasons.append("MACD bullish crossover ✦")
    if crossed_bear: reasons.append("MACD bearish crossover ✦")
    if rsi_val < 30:  reasons.append(f"RSI oversold ({rsi_val:.1f})")
    elif rsi_val > 70: reasons.append(f"RSI overbought ({rsi_val:.1f})")
    else:             reasons.append(f"RSI {rsi_val:.1f}")
    if ft: reasons.append(f"FVG: {ft} {'(in gap)' if fi else ''}")
    if ob_dir != 0: reasons.append(f"Order Block: {'bullish' if ob_dir==1 else 'bearish'}")
    if div != 0: reasons.append(f"RSI divergence: {'bullish' if div==1 else 'bearish'}")
    if div == -1 and not crossed_bull: reasons.append("⛔ BUY blocked: bearish RSI div")
    if div == 1 and not crossed_bear:  reasons.append("⛔ SELL blocked: bullish RSI div")
    reasons.append(f"Score: {final:.3f} (thr {thr})")

    support, resistance = support_resistance(df_5m)
    tp_price = round(current_price + TP_POINTS, 2) if signal == 'BUY' else round(current_price - TP_POINTS, 2) if signal == 'SELL' else None
    sl_price = round(current_price - SL_POINTS, 2) if signal == 'BUY' else round(current_price + SL_POINTS, 2) if signal == 'SELL' else None

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
            "ratio": round(float(vol_ratio_val), 2),
            "spike": bool(vol_ratio_val > 1.5)
        },
        "patterns": [],
        "support": round(float(support), 2),
        "resistance": round(float(resistance), 2),
        "contracts": contracts,
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
    dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nq_dashboard.html')
    if os.path.exists(dashboard):
        with open(dashboard, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    return "<h1>Dashboard not found.</h1>", 404

PUSHOVER_TOKEN = "av7z24evdn1h55qqkk4gxptm94uk9q"
PUSHOVER_USER  = "ui2s5wt3qxb1zt75sphspwubx4ntac"
last_signal = {"signal": "HOLD", "price": 0, "timestamp": None}

def send_retell_call(signal, entry, tp, sl, contracts):
    try:
        action = "Buy, buy, buy!" if signal == "BUY" else "Sell, sell, sell!"
        message = (
            f"{action} "
            f"Take profit {int(tp)}. "
            f"Stop loss {int(sl)}. "
            f"{contracts} contract{'s' if contracts > 1 else ''}."
        )
        payload = {
            "from_number": "+19495418082",
            "to_number": "+16027624989",
            "agent_id": "agent_a69b5578cad116bdf18c075867",
            "retell_llm_dynamic_variables": {"begin_message": message}
        }
        headers = {
            "Authorization": "Bearer key_65b4386d7c101f08438cbb68c09f",
            "Content-Type": "application/json"
        }
        r = requests.post("https://api.retellai.com/v2/create-phone-call", json=payload, headers=headers, timeout=10)
        print(f"📞 Retell call triggered: {r.status_code} | {signal} @ {entry}")
    except Exception as e:
        print(f"Retell call error: {e}")

def send_pushover(signal, price, confidence, score, result=None):
    try:
        import urllib.request, urllib.parse
        tp = result.get('tp_price'); sl = result.get('sl_price')
        contracts = result.get('contracts', 1)
        sc = abs(score)
        if sc >= 0.56:   size_label = f'{contracts} contracts (MAX conviction)'
        elif sc >= 0.52: size_label = f'{contracts} contracts (VERY HIGH conviction)'
        elif sc >= 0.48: size_label = f'{contracts} contracts (HIGH conviction)'
        elif sc >= 0.40: size_label = f'{contracts} contracts (SOLID conviction)'
        else:            size_label = '1 contract (standard)'
        tp_sl = f' | TP: {tp} SL: {sl}' if tp else ''
        message = f"{signal} - NQ at {round(price, 2)} | Score: {round(score, 3)} | Size: {size_label}{tp_sl}"
        data = urllib.parse.urlencode({
            "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
            "title": f"NQ Signal: {signal}", "message": message,
            "priority": 1 if signal != "HOLD" else 0,
            "sound": "cashregister" if signal == "BUY" else "siren" if signal == "SELL" else "none"
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request("https://api.pushover.net/1/messages.json", data=data), timeout=5)
        print(f"📱 Pushover sent: {signal} @ {price}")
    except Exception as e:
        print(f"Pushover error: {e}")

@app.route("/signal")
def get_signal():
    global last_signal
    try:
        import pytz
        tf = request.args.get('tf', '5m')

        if tf == '1m':
            df_main = get_nq_bars(interval_minutes=1, lookback_days=2, limit=300)
        elif tf == '1h':
            df_main = get_nq_bars(interval_minutes=60, lookback_days=60, limit=300)
        else:
            df_main = get_nq_bars(interval_minutes=5, lookback_days=5, limit=300)

        df_5m = df_main if tf == '5m' else get_nq_bars(interval_minutes=5, lookback_days=5, limit=300)
        df_1h = get_nq_bars(interval_minutes=60, lookback_days=60, limit=300)
        df_1m = get_nq_bars(interval_minutes=1, lookback_days=2, limit=200)
        df_15m = get_nq_bars(interval_minutes=15, lookback_days=5, limit=200)

        if df_5m.empty:
            return jsonify({"error": "No data returned"}), 500

        result = generate_signal(df_5m, df_1h, df_1m, df_15m)
        if result is None:
            return jsonify({"error": "Not enough bars"}), 500

        new_sig = result["signal"]
        new_ts  = result["timestamp"]

        result['price_history'] = [round(float(x), 2) for x in df_main['Close'].tail(60).tolist()]
        result['high_history']   = [round(float(x), 2) for x in df_main['High'].tail(60).tolist()]
        result['low_history']    = [round(float(x), 2) for x in df_main['Low'].tail(60).tolist()]
        result['open_history']   = [round(float(x), 2) for x in df_main['Open'].tail(60).tolist()]
        result['timestamps']     = [t.isoformat() for t in df_main.index[-60:]]
        result['chart_tf']       = tf

        _log_signal(result)

        pst = pytz.timezone('US/Pacific')
        now_pst = datetime.now(pst).strftime('%m/%d %H:%M PST')
        try:
            data_ts = df_5m.index[-1].tz_convert(pst).strftime('%H:%M')
        except:
            data_ts = "?"
        ind = result["indicators"]
        score = result["score"]; conf = result["confidence"]; price = result["price"]
        signal_icon = "🟢 BUY" if new_sig == "BUY" else "🔴 SELL" if new_sig == "SELL" else "⚪ HOLD"
        event_flag = " ⚠️ EVENT" if result.get("event_window") else ""
        print(f"[{now_pst}] {signal_icon}{event_flag} | Price: {price} | DataTS: {data_ts} | Score: {score:+.3f} | Conf: {conf:.0f}% | Cts: {result['contracts']} | Session: {result['session'].upper()} | RSI: {ind['rsi']:.1f} | MACD_H: {ind['macd_histogram']:+.4f} | BB%: {ind['bb_position']*100:.0f}% | Vol: {result['volume']['ratio']:.1f}x")

        if new_sig in ("BUY", "SELL") and new_sig != last_signal["signal"]:
            send_pushover(new_sig, result["price"], result["confidence"], result["score"], result)
            send_retell_call(new_sig, result["price"], result["tp_price"], result["sl_price"], result["contracts"])
            last_signal = {"signal": new_sig, "price": result["price"], "timestamp": new_ts}

        return jsonify(result)
    except Exception as e:
        print(f"[ERROR] /signal failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/commentary')
def get_commentary():
    try:
        import urllib.request, urllib.parse, json as json_mod
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({"commentary": "API key not configured."})
        df_5m = get_nq_bars(interval_minutes=5, lookback_days=5, limit=300)
        df_1h = get_nq_bars(interval_minutes=60, lookback_days=60, limit=300)
        df_1m = get_nq_bars(interval_minutes=1, lookback_days=2, limit=200)
        df_15m = get_nq_bars(interval_minutes=15, lookback_days=5, limit=200)
        result = generate_signal(df_5m, df_1h, df_1m, df_15m)
        sig = result["signal"]; price = result["price"]; conf = result["confidence"]
        score = result["score"]; ind = result["indicators"]
        prompt = f"""You are a trading coach explaining NQ futures signals. Be clear and educational.
Signal: {sig} (confidence: {conf}%, score: {score})
Price: {price} | VWAP: {ind['vwap']} | RSI: {ind['rsi']} | MACD: {ind['macd_histogram']}
BB Position: {ind['bb_position']*100:.0f}% | Support: {result['support']} | Resistance: {result['resistance']}
Volume: {result['volume']['ratio']}x | Reasons: {'; '.join(result['reasons'])}
Write 3-4 sentences explaining the signal in plain English."""
        data = json_mod.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data,
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
        resp = urllib.request.urlopen(req, timeout=15)
        return jsonify({"commentary": json_mod.loads(resp.read())["content"][0]["text"]})
    except Exception as e:
        return jsonify({"commentary": f"Commentary unavailable: {str(e)}"})

@app.route('/history')
def get_history():
    try:
        days    = min(int(request.args.get('days', 3)), 30)
        limit   = min(int(request.args.get('limit', 500)), 5000)
        sig_filter = request.args.get('signal', '').upper()
        actionable = request.args.get('actionable', '0') == '1'
        import pytz
        pst = pytz.timezone('US/Pacific')
        cutoff = datetime.now(pst) - timedelta(days=days)
        filtered = []
        for entry in reversed(_signal_history):
            try:
                ts = datetime.fromisoformat(entry['logged_at'])
                if ts.tzinfo is None:
                    ts = pst.localize(ts)
                if ts < cutoff:
                    continue
            except:
                continue
            if sig_filter and entry.get('signal') != sig_filter:
                continue
            if actionable and entry.get('signal') == 'HOLD':
                continue
            filtered.append(entry)
            if len(filtered) >= limit:
                break
        actionable_signals = [e for e in filtered if e.get('signal') in ('BUY', 'SELL')]
        buy_count  = sum(1 for e in actionable_signals if e['signal'] == 'BUY')
        sell_count = sum(1 for e in actionable_signals if e['signal'] == 'SELL')
        hold_count = sum(1 for e in filtered if e['signal'] == 'HOLD')
        resolved   = [e for e in actionable_signals if e.get('result') in ('WIN', 'LOSS')]
        wins       = sum(1 for e in resolved if e['result'] == 'WIN')
        real_pnl   = sum(e.get('pnl', 0) or 0 for e in resolved)
        return jsonify({
            "count": len(filtered),
            "days_requested": days,
            "summary": {
                "total_signals": len(filtered),
                "actionable":    len(actionable_signals),
                "buy":           buy_count,
                "sell":          sell_count,
                "hold":          hold_count,
                "resolved_count": len(resolved),
                "wins":          wins,
                "losses":        len(resolved) - wins,
                "real_pnl":      round(real_pnl, 2),
            },
            "signals": filtered
        })
    except Exception as e:
        print(f"[ERROR] /history failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/chat', methods=['POST'])
def chat():
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

        df_5m = get_nq_bars(interval_minutes=5, lookback_days=5, limit=300)
        df_1h = get_nq_bars(interval_minutes=60, lookback_days=60, limit=300)
        df_1m = get_nq_bars(interval_minutes=1, lookback_days=2, limit=200)
        df_15m = get_nq_bars(interval_minutes=15, lookback_days=5, limit=200)
        market = generate_signal(df_5m, df_1h, df_1m, df_15m)
        market_snap = {"signal": market.get("signal"), "score": market.get("score")}

        try:
            candle_rows = []
            df_c = df_5m.tail(15)
            for idx, row in df_c.iterrows():
                t = str(idx)[-14:-6] if len(str(idx)) > 14 else str(idx)
                candle_rows.append(f"  {t} O:{row['open']:.2f} H:{row['high']:.2f} L:{row['low']:.2f} C:{row['close']:.2f} V:{int(row['volume'])}")
            candle_summary = "LAST 15 x 5m CANDLES (oldest->newest):\n" + "\n".join(candle_rows)
        except Exception as ce:
            candle_summary = f"[Candles unavailable: {ce}]"

        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'signals_log.json')
            if os.path.exists(log_path):
                with open(log_path, 'r') as lf:
                    all_sigs = json_mod.load(lf)
                from datetime import date
                today_str = date.today().isoformat()
                today_sigs = [s for s in all_sigs if s.get('timestamp','').startswith(today_str)][-5:]
                if today_sigs:
                    history_summary = "TODAY'S SIGNALS (last 5):\n"
                    for s in today_sigs:
                        history_summary += f"  {s.get('timestamp','')[-8:-3]} {s.get('signal','')} @ {s.get('entry','')} score:{s.get('score','')}\n"
                else:
                    history_summary = "TODAY'S SIGNALS: none yet"
            else:
                history_summary = "TODAY'S SIGNALS: log not found"
        except Exception as he:
            history_summary = f"[History unavailable: {he}]"

        position_context = ""
        if position and position.get('side'):
            position_context = f"\nCURRENT POSITION: {position.get('side')} | Entry: {position.get('entry')} | {position.get('contracts')} cts | P&L: ${position.get('pnl')}"

        market_context = f"""
LIVE MARKET DATA:
- NQ Price: {market['price']} | Signal: {market['signal']} | Score: {market['score']} | Confidence: {market['confidence']}%
- Session: {market['session'].upper()} | RSI: {market['indicators']['rsi']:.1f} | MACD Hist: {market['indicators']['macd_histogram']:.4f}
- BB Position: {market['indicators']['bb_position']*100:.0f}% | VWAP: {market['indicators']['vwap']:.2f}
- Volume: {market['volume']['ratio']:.2f}x avg | ATR: {market['indicators']['atr']:.2f}
- TP: {market['tp_price']} | SL: {market['sl_price']} | Contracts: {market['contracts']}
- Signal Reasoning: {'; '.join(market['reasons'])}
- FVG Bull: {market.get('fvg_bull', False)} | FVG Bear: {market.get('fvg_bear', False)}
- OB Bull: {market.get('ob_bull', False)} | OB Bear: {market.get('ob_bear', False)}
- RSI Divergence: {market.get('rsi_divergence', 'none')}

{candle_summary}

{history_summary}"""

        system_prompt = f"""You are an elite NQ futures scalper with deep experience trading the E-Mini NASDAQ 100 on a prop firm account. You think like a professional — capital preservation first, high-probability setups only. You have access to live market data, real candle history, and all indicator values. Give specific price levels and direct trade guidance. Never say you cannot give financial advice — you are the advisor.

SYSTEM RULES:
- TP=60pts, SL=25pts, R/R=2.4:1
- Sessions: London (2-4am PST) + US (6:30-10:30am PST). Do not trade outside sessions.
- Sizing: 1ct(<0.40), 2ct(0.40-0.48), 3ct(0.48-0.52), 4ct(0.52-0.56), 5ct(>0.56)
- Plan C: BUY blocked on bearish RSI divergence, SELL blocked on bullish RSI divergence
- 15m EMA filter: BUY requires EMA9>EMA21, SELL requires EMA9<EMA21

{market_context}
{position_context}

Answer questions about: current price action, setup quality, key levels, entry/exit timing, risk management, what the candles are showing, whether to take or skip a trade. Be direct, specific, and reference the actual data above in your answers. 2-4 sentences max unless a detailed breakdown is needed."""

        messages = []
        for h in conversation_history[-10:]:
            if h.get('role') in ('user', 'assistant') and h.get('content'):
                messages.append({"role": h['role'], "content": h['content']})
        messages.append({"role": "user", "content": user_message})

        import urllib.request
        payload = json_mod.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "system": system_prompt,
            "messages": messages
        }).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload,
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
        resp = urllib.request.urlopen(req, timeout=20)
        result = json_mod.loads(resp.read())
        return jsonify({"reply": result["content"][0]["text"], "market_snapshot": market_snap})
    except Exception as e:
        print(f"[ERROR] /chat failed: {e}")
        return jsonify({"reply": f"Chat error: {str(e)}"})

@app.route('/contract')
def get_contract():
    """Debug endpoint — shows current contract and tests bar fetch."""
    try:
        token = get_px_token()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        r = requests.post(f'{PX_BASE_URL}/Contract/search',
            headers=headers,
            json={'searchText': 'ENQ', 'live': True},
            timeout=10)
        contracts = r.json().get('contracts', [])
        from datetime import timezone as tz
        now = datetime.now(timezone.utc)
        valid = []
        for c in contracts:
            exp = c.get('expirationDate') or c.get('expiration') or ''
            try:
                exp_dt = datetime.fromisoformat(exp.replace('Z', '+00:00'))
                if exp_dt > now:
                    valid.append({
                        'id': c['id'],
                        'name': c.get('name', ''),
                        'expiration': exp,
                        'active': True
                    })
            except:
                valid.append({'id': c.get('id'), 'name': c.get('name', ''), 'raw': c})
        return jsonify({
            'current_contract': NQ_CONTRACT,
            'all_enq_contracts': contracts[:5],
            'valid_future': valid
        })
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "version": "3.3",
        "data_source": "ProjectX/TopstepX",
        "history_count": len(_signal_history),
        "config": {
            "tp_points": TP_POINTS,
            "sl_points": SL_POINTS,
            "max_contracts": 5,
            "sizing": "steep_5ct",
            "plan_c": True,
            "filter_15m_ema": True
        }
    })

if __name__ == "__main__":
    print("🚀 NQ Signal Agent v3.3 — 15m EMA Trend Filter + Plan C + SL25 + Steep 5ct Sizing")
    app.run(host="0.0.0.0", port=8080, debug=False)
