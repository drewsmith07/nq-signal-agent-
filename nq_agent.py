#!/usr/bin/env python3
"""
NQ Futures Scalping Signal Agent - v3.2
Real-time data via ProjectX/TopstepX API — zero lag
+ Signal history logging (persists to signals_log.json)
"""

import pandas as pd
import threading
import numpy as np
import json
import os
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

# ─── Supabase Config ──────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://hzifmrfgimhahmnhddwo.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
_supabase = None

def _get_supabase():
    global _supabase
    if _supabase is None and SUPABASE_KEY:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

# ─── Signal History ───────────────────────────────────────────────────────────
_signal_history = []  # in-memory cache

def _load_history():
    """Load existing history from disk on startup."""
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
    """Persist history to disk."""
    try:
        with open(LOG_FILE, 'w') as f:
            json.dump(_signal_history, f)
    except Exception as e:
        print(f"[History] Failed to save log: {e}")

def _log_signal(result):
    """Log signal to Supabase — BUY/SELL only, no HOLDs."""
    if result.get("signal") not in ("BUY", "SELL"):
        return
    import pytz
    pst = pytz.timezone('US/Pacific')
    now_pst = datetime.now(pst).isoformat()

    ind = result.get("indicators", {})
    entry = {
        "logged_at":    now_pst,
        "signal":       result.get("signal"),
        "price":        result.get("price"),
        "score":        result.get("score"),
        "confidence":   result.get("confidence"),
        "session":      result.get("session"),
        "event_window": result.get("event_window", False),
        "contracts":    result.get("contracts", 0),
        "tp_price":     result.get("tp_price"),
        "sl_price":     result.get("sl_price"),
        "tp_points":    result.get("tp_points"),
        "sl_points":    result.get("sl_points"),
        "rsi":          ind.get("rsi"),
        "macd_histogram": ind.get("macd_histogram"),
        "bb_position":  ind.get("bb_position"),
        "vwap":         ind.get("vwap"),
        "atr":          ind.get("atr"),
        "fvg_type":     ind.get("fvg_type"),
        "ob_direction": ind.get("ob_direction"),
        "volume_ratio": result.get("volume", {}).get("ratio"),
        "reasons":      json.dumps(result.get("reasons", [])),
        "outcome":      None,
        "pnl":          None,
    }

    try:
        sb = _get_supabase()
        if sb:
            sb.table("nq_signals").insert(entry).execute()
            print(f"[Supabase] Logged: {entry['signal']} @ {entry['price']}")
        else:
            print("[Supabase] No client — check SUPABASE_KEY env var")
    except Exception as e:
        print(f"[Supabase] Failed: {e}")

    _signal_history.append(entry)
    if len(_signal_history) > 500:
        _signal_history.pop(0)


# ─── Outcome Tracker ──────────────────────────────────────────────────────────
TP_POINTS = 60.0
SL_POINTS = 20.0

def _check_outcomes():
    import pytz, time
    pst = pytz.timezone('US/Pacific')
    while True:
        try:
            sb = _get_supabase()
            if sb:
                open_signals = sb.table("nq_signals") \
                    .select("id,signal,price,tp_price,sl_price,logged_at,contracts") \
                    .is_("outcome", "null") \
                    .in_("signal", ["BUY", "SELL"]) \
                    .execute()
                if open_signals.data:
                    try:
                        df = get_nq_bars(interval_minutes=1, lookback_days=1, limit=200)
                        for sig in open_signals.data:
                            try:
                                sig_id = sig["id"]
                                sig_type = sig["signal"]
                                tp_price = float(sig["tp_price"])
                                sl_price = float(sig["sl_price"])
                                contracts = sig.get("contracts") or 1
                                from datetime import timezone as tz
                                logged_at = datetime.fromisoformat(sig["logged_at"])
                                if logged_at.tzinfo is None:
                                    logged_at = pst.localize(logged_at)
                                logged_utc = logged_at.astimezone(tz.utc)
                                recent = df[df.index > logged_utc]
                                if recent.empty:
                                    continue
                                outcome = None
                                pnl = None
                                for _, candle in recent.iterrows():
                                    high = float(candle["High"])
                                    low = float(candle["Low"])
                                    if sig_type == "BUY":
                                        if low <= sl_price:
                                            outcome = "LOSS"
                                            pnl = round(-SL_POINTS * 20 * contracts, 2)
                                            break
                                        if high >= tp_price:
                                            outcome = "WIN"
                                            pnl = round(TP_POINTS * 20 * contracts, 2)
                                            break
                                    elif sig_type == "SELL":
                                        if high >= sl_price:
                                            outcome = "LOSS"
                                            pnl = round(-SL_POINTS * 20 * contracts, 2)
                                            break
                                        if low <= tp_price:
                                            outcome = "WIN"
                                            pnl = round(TP_POINTS * 20 * contracts, 2)
                                            break
                                if outcome:
                                    sb.table("nq_signals") \
                                        .update({"outcome": outcome, "pnl": pnl}) \
                                        .eq("id", sig_id) \
                                        .execute()
                                    print(f"[Outcome] {sig_type} #{sig_id} -> {outcome} | P&L: ${pnl}")
                            except Exception as e:
                                print(f"[Outcome] Error on signal {sig.get('id')}: {e}")
                    except Exception as e:
                        print(f"[Outcome] Bars error: {e}")
        except Exception as e:
            print(f"[Outcome] Thread error: {e}")
        time.sleep(30)

def _start_outcome_tracker():
    t = threading.Thread(target=_check_outcomes, daemon=True)
    t.start()
    print("[Outcome] Background tracker started.")

# ─── ProjectX Config ──────────────────────────────────────────────────────────
PX_USERNAME = 'drewksmith602@gmail.com'
PX_API_KEY  = '2AEN4l/nMCiRnnJXOZRed3kjOWfczuszBKZogj+1njM='
PX_BASE_URL = 'https://api.topstepx.com/api'
NQ_CONTRACT = 'CON.F.US.ENQ.U26'  # Front month NQ — update on rollover

_px_token = None
_px_token_expiry = None

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

def get_nq_bars(interval_minutes=5, lookback_days=2, limit=300):
    """Fetch real-time NQ bars from ProjectX — zero lag"""
    token = get_px_token()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    r = requests.post(f'{PX_BASE_URL}/History/retrieveBars', headers=headers,
        json={
            'contractId': NQ_CONTRACT,
            'live': False,
            'startTime': start,
            'endTime': end,
            'unit': 2,  # minute
            'unitNumber': interval_minutes,
            'limit': limit,
            'includePartialBar': False
        }, timeout=15)
    data = r.json()
    bars = data.get('bars', [])
    if not bars:
        raise Exception(f"No bars returned from ProjectX: {data}")
    df = pd.DataFrame(bars)
    df.rename(columns={'t':'Datetime','o':'Open','h':'High','l':'Low','c':'Close','v':'Volume'}, inplace=True)
    df['Datetime'] = pd.to_datetime(df['Datetime'], utc=True)
    df = df.sort_values('Datetime').reset_index(drop=True)
    df.set_index('Datetime', inplace=True)
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

def generate_signal(df_5m, df_1h=None, df_1m=None):
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
    ct = df_5m.index[-1]
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

    s5 = _score_tf(df_5m, i)
    s1h = 0.0
    if df_1h is not None and len(df_1h) >= 30:
        s1h = _score_tf(df_1h, len(df_1h)-1)
    s1m = s5
    if df_1m is not None and len(df_1m) >= 30:
        s1m = _score_tf(df_1m, len(df_1m)-1)

    final = (s1h * 0.50) + (s5 * 0.35) + (s1m * 0.15)
    if s1h > 0.15 and s5 > 0.15 and s1m > 0.15:     final += 0.10
    elif s1h < -0.15 and s5 < -0.15 and s1m < -0.15: final -= 0.10

    rsi_s = rsi(close)
    div = _rsi_divergence(df_5m, i, rsi_s)
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

    # MACD gate: allow signal on fresh cross OR sustained histogram (3+ candles same sign)
    hist_sustained_bull = all(histogram.iloc[-(k+1)] > 0 for k in range(3))
    hist_sustained_bear = all(histogram.iloc[-(k+1)] < 0 for k in range(3))
    macd_ok = crossed_bull or crossed_bear or hist_sustained_bull or hist_sustained_bear

    if crossed_bull: final += 0.08
    elif crossed_bear: final -= 0.08

    if not macd_ok or window is None or in_event_window:
        signal = "HOLD"
        final = 0.0
    else:
        signal = "BUY" if final > thr else "SELL" if final < -thr else "HOLD"

    confidence = min(abs(final) * 100, 95)

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
    reasons.append(f"Score: {final:.3f} (thr {thr})")

    support, resistance = support_resistance(df_5m)
    TP_POINTS = 60; SL_POINTS = 20
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
    dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nq_dashboard.html')
    if os.path.exists(dashboard):
        with open(dashboard, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    return "<h1>Dashboard not found.</h1>", 404

PUSHOVER_TOKEN = "av7z24evdn1h55qqkk4gxptm94uk9q"
PUSHOVER_USER  = "ui2s5wt3qxb1zt75sphspwubx4ntac"
last_signal = {"signal": "HOLD", "price": 0, "timestamp": None}

def send_retell_call(signal, entry, tp, sl, contracts):
    """Call Andrew's phone via Retell AI when a BUY/SELL signal fires."""
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
            "retell_llm_dynamic_variables": {
                "begin_message": message
            }
        }
        headers = {
            "Authorization": "Bearer key_a21bca454a3cd876862e6b391ac3",
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
        sc = abs(score)
        size_label = '3 contracts (HIGH conviction)' if sc >= 0.55 else '2 contracts (SOLID conviction)' if sc >= 0.45 else '1 contract (standard)'
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
            df_main = get_nq_bars(interval_minutes=1, lookback_days=1, limit=300)
        elif tf == '1h':
            df_main = get_nq_bars(interval_minutes=60, lookback_days=30, limit=300)
        else:
            df_main = get_nq_bars(interval_minutes=5, lookback_days=2, limit=300)

        df_5m = df_main if tf == '5m' else get_nq_bars(interval_minutes=5, lookback_days=2, limit=300)
        df_1h = get_nq_bars(interval_minutes=60, lookback_days=30, limit=300)
        df_1m = get_nq_bars(interval_minutes=1, lookback_days=1, limit=200)

        if df_5m.empty:
            return jsonify({"error": "No data returned"}), 500

        result = generate_signal(df_5m, df_1h, df_1m)
        new_sig = result["signal"]
        new_ts  = result["timestamp"]

        result['price_history'] = [round(float(x), 2) for x in df_main['Close'].tail(60).tolist()]
        result['high_history']   = [round(float(x), 2) for x in df_main['High'].tail(60).tolist()]
        result['low_history']    = [round(float(x), 2) for x in df_main['Low'].tail(60).tolist()]
        result['open_history']   = [round(float(x), 2) for x in df_main['Open'].tail(60).tolist()]
        result['timestamps']     = [t.isoformat() for t in df_main.index[-60:]]
        result['chart_tf']       = tf

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
        print(f"[{now_pst}] {signal_icon}{event_flag} | Price: {price} | DataTS: {data_ts} | Score: {score:+.3f} | Conf: {conf:.0f}% | Session: {result['session'].upper()} | RSI: {ind['rsi']:.1f} | MACD_H: {ind['macd_histogram']:+.4f} | BB%: {ind['bb_position']*100:.0f}% | Vol: {result['volume']['ratio']:.1f}x")

        thr = 0.45 if result["session"] == "london" else 0.38
        if new_sig == "HOLD" and abs(score) > 0 and abs(score) >= thr * 0.6:
            gap = thr - abs(score)
            direction = "BUY" if score > 0 else "SELL"
            print(f"  ↳ NEAR-MISS: {direction} signal {gap:.3f} pts below threshold ({thr})")

        if new_sig in ("BUY", "SELL") and new_sig != last_signal["signal"]:
            _log_signal(result)
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
        df_5m = get_nq_bars(interval_minutes=5, lookback_days=2, limit=300)
        df_1h = get_nq_bars(interval_minutes=60, lookback_days=30, limit=300)
        df_1m = get_nq_bars(interval_minutes=1, lookback_days=1, limit=200)
        result = generate_signal(df_5m, df_1h, df_1m)
        sig = result["signal"]; price = result["price"]; conf = result["confidence"]
        score = result["score"]; ind = result["indicators"]
        prompt = f"""You are a trading coach explaining NQ futures signals. Be clear and educational.
Signal: {sig} (confidence: {conf}%, score: {score})
Price: {price} | VWAP: {ind['vwap']} | RSI: {ind['rsi']} | MACD: {ind['macd_histogram']}
BB Position: {ind['bb_position']*100:.0f}% | Support: {result['support']} | Resistance: {result['resistance']}
Volume: {result['volume']['ratio']}x | Reasons: {'; '.join(result['reasons'])}
Write 3-4 sentences explaining the signal in plain English."""
        data = json_mod.dumps({"model": "claude-haiku-4-5", "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data,
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
        resp = urllib.request.urlopen(req, timeout=15)
        return jsonify({"commentary": json_mod.loads(resp.read())["content"][0]["text"]})
    except Exception as e:
        return jsonify({"commentary": f"Commentary unavailable: {str(e)}"})

# ─── History Endpoint ─────────────────────────────────────────────────────────

@app.route('/history')
def get_history():
    """
    Returns logged signals from Supabase — persists across deploys.
    Query params:
      ?days=N       — last N days (default 7, max 90)
      ?actionable=1 — only return BUY/SELL signals
    """
    try:
        days       = min(int(request.args.get('days', 7)), 90)
        actionable = request.args.get('actionable', '0') == '1'

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        sb = _get_supabase()
        if not sb:
            return jsonify({"error": "Supabase not configured"}), 500

        query = sb.table("nq_signals") \
            .select("*") \
            .gte("logged_at", cutoff) \
            .order("logged_at", desc=True) \
            .limit(500)

        if actionable:
            query = query.in_("signal", ["BUY", "SELL"])

        result = query.execute()
        filtered = result.data or []

        actionable_signals = [e for e in filtered if e.get('signal') in ('BUY', 'SELL')]
        buy_count  = sum(1 for e in actionable_signals if e['signal'] == 'BUY')
        sell_count = sum(1 for e in actionable_signals if e['signal'] == 'SELL')
        hold_count = sum(1 for e in filtered if e['signal'] == 'HOLD')

        resolved   = [e for e in actionable_signals if e.get('outcome') in ('WIN', 'LOSS')]
        real_pnl   = sum(e.get('pnl', 0) or 0 for e in resolved)
        win_count  = sum(1 for e in resolved if e['outcome'] == 'WIN')
        loss_count = sum(1 for e in resolved if e['outcome'] == 'LOSS')

        return jsonify({
            "count": len(filtered),
            "days_requested": days,
            "summary": {
                "total_signals":  len(filtered),
                "actionable":     len(actionable_signals),
                "buy":            buy_count,
                "sell":           sell_count,
                "hold":           hold_count,
                "wins":           win_count,
                "losses":         loss_count,
                "real_pnl":       real_pnl,
                "resolved_count": len(resolved),
            },
            "signals": filtered
        })
    except Exception as e:
        print(f"[ERROR] /history failed: {e}")
        return jsonify({"error": str(e)}), 500


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
- MACD Histogram: {market['indicators']['macd_histogram']} ('bullish' if {market['indicators']['macd_histogram']} > 0 else 'bearish')
- BB Position: {market['indicators']['bb_position']*100:.0f}% (0%=lower band, 100%=upper band)
- VWAP: {market['indicators']['vwap']} (price is 'ABOVE' if {market['price']} > {market['indicators']['vwap']} else 'BELOW' VWAP)
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
            "model": "claude-sonnet-4-5",
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

@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "version": "3.2",
        "data_source": "ProjectX/TopstepX",
        "history_count": len(_signal_history)
    })

if __name__ == "__main__":
    _start_outcome_tracker()
    print("🚀 NQ Signal Agent v3.2 — Real-time ProjectX data + Signal History")
    app.run(host="0.0.0.0", port=8080, debug=False)
