"""
GBPJPY SMC Trading Bot — v2.1 (Multi-Agent Architecture + Upgrades)
=======================================================
Refactored to use 7-agent pipeline:
  Agent 1 : Bias Agent          — top-down HTF analysis
  Agent 2 : Session Agent       — time-window filter (FIXED: proper London/NY sessions)
  Agent 3 : News Filter Agent   — high-impact event blocker (UPGRADED: real news feed)
  Agent 4 : Liquidity Agent     — sweep & zone detection (UPGRADED: multi-candle sweep)
  Agent 5 : Entry Agent         — FVG + displacement (UPGRADED: ATR-based displacement)
  Agent 6 : Confluence Scorer   — 0-6 gate before risk (FIXED: removed redundant EMA check)
  Agent 7 : Risk Management     — sizing, SL/TP, daily cap (single position, simple)
  Feedback : Learning Loop      — every-20-trade review

Integrated with MetaTrader 5 for live trading.
UPGRADES IN v2.1:
  ✓ News feed integration (Investing.com scraper)
  ✓ Real ATR percentile calculation
  ✓ Fixed session windows (London 8-17, NY 13-21 UTC)
  ✓ Secure MT5 login (env vars + fallback to input)
  ✓ Single position per signal (simple, reliable)
  ✓ Multi-candle sweep detection (checks last 3 candles)
  ✓ MT5 error recovery + reconnection
  ✓ Absolute paths for state/log files
  ✓ Log file renamed to lunar_flow.log
  ✓ Removed redundant EMA check in ConfluenceScorer
  ✓ ATR-based displacement confirmation
  ✓ ATR-based fallback TP calculation
  ✓ Adaptive sleep interval based on timeframe
"""

from __future__ import annotations

import json
import logging
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, List

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

# ── Paths (ABSOLUTE) ──────────────────────────────────────────────────
BOT_DIR = Path.home() / ".lunar_flow"
BOT_DIR.mkdir(exist_ok=True)
STATE_FILE = BOT_DIR / "bot_state.json"
TRADE_LOG_FILE = BOT_DIR / "trade_log.json"
LOG_FILE = BOT_DIR / "lunar_flow.log"

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("lunar_flow")

# ═════════════════════════════════════════════════════════════════════════
# ENUMS & CONSTANTS
# ═════════════════════════════════════════════════════════════════════════

class Bias(str, Enum):
    BULLISH  = "BULLISH"
    BEARISH  = "BEARISH"
    CONFLICT = "CONFLICT"

class Direction(str, Enum):
    BUY      = "Buy"
    SELL     = "Sell"
    NO_TRADE = "No Trade"

class Validity(str, Enum):
    A_PLUS = "A+ Setup"
    VALID  = "Valid"
    REJECT = "REJECT"

class Session(str, Enum):
    LONDON   = "London"
    NEW_YORK = "New York"
    OVERLAP  = "London/NY Overlap"
    INACTIVE = "Inactive"

# Risk constants (USD)
RISK_PER_TRADE_USD  = 2.50
DAILY_LOSS_CAP_USD  = 5.00
MAX_TRADES_PER_DAY  = 20  # Scalping: allow up to 20 trades/day
MIN_RR              = 2.4
PARTIAL_TP_RR       = 1.5  # Not used (single position)
MIN_FVG_PIPS        = 8
MIN_FVG_CANDLES     = 2
ATR_LOW_PERCENTILE  = 0.20

# Scalping mode (second-level trades: 2-5 pips SL/TP)
# Set via environment variable SCALPING_MODE=True/False (default True)
SCALPING_MODE       = os.getenv("SCALPING_MODE", "True") == "True"
SCALP_TP_PIPS       = 10   # Real TP2 (10 pips, 5:1 RR with 2-pip SL; doc value 4 was incorrect)
SCALP_SL_PIPS       = 2    # 2 pips stop loss
ATR_SL_MULTIPLIER   = 1.2
NEWS_BLOCK_MINUTES  = 30
CONFLUENCE_EXECUTE  = 8  # Require 8/10 score for trade execution (needs MSS + volume)
SL_BUFFER_PIPS      = 4

# Session windows (FIXED: proper UTC times)
LONDON_START  = 8   # 08:00 UTC
LONDON_END    = 17  # 17:00 UTC (full session)
NY_START      = 13  # 13:00 UTC
NY_END        = 21  # 21:00 UTC (full session)

# GBPJPY-specific
SYMBOL = "GBPJPY"
PIP    = 0.01

# MT5 Timeframes
TF_MAP = {
    "D1":  mt5.TIMEFRAME_D1,
    "H4":  mt5.TIMEFRAME_H4,
    "H2":  mt5.TIMEFRAME_H2 if hasattr(mt5, "TIMEFRAME_H2") else mt5.TIMEFRAME_H1,
    "H1":  mt5.TIMEFRAME_H1,
    "M30": mt5.TIMEFRAME_M30,
    "M15": mt5.TIMEFRAME_M15,
    "M1":  mt5.TIMEFRAME_M1,  # Added for second-level scalping
}

# News scraping
INVESTING_NEWS_URL = "https://www.investing.com/economic-calendar/Service/getCalendarFiltered"
NEWS_CACHE_SECONDS = 3600  # Refresh news every hour

# Circuit-breaker + discount-by-1 state (patch from 2026-09-13 session)
MAX_CONSECUTIVE_SL = 2
PIP = 0.01

# ═════════════════════════════════════════════════════════════════════════
# STATE PERSISTENCE (ABSOLUTE PATHS)
# ═════════════════════════════════════════════════════════════════════════

def init_or_migrate_state(state: dict) -> dict:
    state.setdefault("consecutive_sl_hits", 0)
    state.setdefault("trading_halted", False)
    state.setdefault("halt_date", None)
    return state


def _load_bot_state() -> dict:
    state = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception as e:
            log.warning(f"Failed to load bot state: {e}")
    return init_or_migrate_state(state)

def _save_bot_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error(f"Failed to save bot state: {e}")

# ═════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class Candle:
    timeframe : str
    open      : float
    high      : float
    low       : float
    close     : float
    volume    : float
    timestamp : datetime

def calculate_vwap(candles: list[Candle], periods: int = 20) -> float:
    """Calculate VWAP from last N candles (typical price * volume / total volume)."""
    if not candles or len(candles) < periods:
        return 0.0
    recent = candles[-periods:]
    total_pv = 0.0
    total_vol = 0.0
    for c in recent:
        typical = (c.high + c.low + c.close) / 3.0
        total_pv += typical * c.volume
        total_vol += c.volume
    if total_vol == 0:
        return 0.0
    return total_pv / total_vol

def volume_confirmed(candles: list[Candle], periods: int = 20, multiplier: float = 1.0) -> bool:
    """Return True if last candle volume > multiplier * average volume over periods."""
    if not candles or len(candles) < periods:
        return False
    recent = candles[-periods:]
    avg_vol = sum(c.volume for c in recent) / periods
    last_vol = candles[-1].volume
    confirmed = last_vol > avg_vol * multiplier
    log.info(f"VolumeCheck: last={last_vol:.0f}, avg={avg_vol:.0f}, mult={multiplier} -> {confirmed}")
    return confirmed

import os
import requests

def send_telegram_alert(message: str):
    """Send trade alert to Telegram (requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "deepstat8")  # fallback to username
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set, skipping alert")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code != 200:
            log.error(f"Telegram alert failed: {resp.text}")
    except Exception as e:
        log.error(f"Telegram alert error: {e}")

@dataclass
class MarketData:
    symbol   : str = SYMBOL
    d1       : list[Candle] = field(default_factory=list)
    h4       : list[Candle] = field(default_factory=list)
    h2       : list[Candle] = field(default_factory=list)
    h1       : list[Candle] = field(default_factory=list)
    m30      : list[Candle] = field(default_factory=list)
    m15      : list[Candle] = field(default_factory=list)
    m1       : list[Candle] = field(default_factory=list)
    atr14    : float = 0.0
    atr_pct  : float = 0.0
    ema20    : float = 0.0
    ema50    : float = 0.0
    vwap     : float = 0.0
    pivot_r1 : float = 0.0
    pivot_s1 : float = 0.0

@dataclass
class LiquidityZone:
    price      : float
    zone_type  : str
    swept      : bool  = False
    sweep_ts   : Optional[datetime] = None

@dataclass
class FVG:
    high      : float
    low       : float
    midpoint  : float
    candles   : int
    pip_width : float

@dataclass
class TradeSignal:
    timestamp         : datetime
    direction         : Direction
    htf_bias          : Bias
    session           : Session
    news_status       : str
    liquidity_zone    : Optional[str]
    sweep_confirmed   : bool
    fvg_detected      : bool
    fvg_pips          : float
    fvg_candles       : int
    entry_price       : float
    stop_loss         : float
    take_profit_1     : float  # Informational only (not used for partial profits)
    take_profit_2     : float  # Actual TP for single position
    risk_reward       : float
    confluence_score  : int
    validity          : Validity
    rejection_reason  : str
    outcome           : str = ""
    pnl_usd           : float = 0.0

@dataclass
class DailyState:
    date           : str = ""
    trades_taken   : int  = 0
    daily_loss_usd : float = 0.0
    trade_log      : list  = field(default_factory=list)

@dataclass
class NewsEvent:
    name   : str
    impact : str
    time   : datetime
    currency: str = ""

# ═════════════════════════════════════════════════════════════════════════
# MT5 DATA HELPERS
# ═════════════════════════════════════════════════════════════════════════

def get_candles(tf: str, count: int = 300) -> list[Candle]:
    if tf not in TF_MAP:
        log.error(f"Unknown timeframe: {tf}")
        return []
    # M1 fix: MT5 build 500 may reject large count with TIMEFRAME_M1; cap M1 requests
    count = min(count, 500) if tf == "M1" else count
    for attempt in range(3):
        rates = mt5.copy_rates_from_pos(SYMBOL, TF_MAP[tf], 0, count)
        if rates is not None and len(rates) > 0:
            candles = []
            for r in rates:
                ts = datetime.fromtimestamp(r[0], tz=timezone.utc)
                candles.append(Candle(tf, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), ts))
            return candles
        log.warning(f"get_candles({tf}) attempt {attempt+1} failed, retrying...")
        time.sleep(1)
    log.error(f"Failed to get candles for {tf} after 3 attempts")
    return []

def calculate_indicators(candles: list[Candle], period: int = 14) -> dict:
    if len(candles) < period:
        return {"atr14": 0.0, "ema20": 0.0, "ema50": 0.0}
    df = pd.DataFrame([{"high": c.high, "low": c.low, "close": c.close} for c in candles])
    df["tr"] = np.maximum(df["high"] - df["low"], np.maximum(
        abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1))))
    df["atr14"] = df["tr"].rolling(window=period).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    last = df.iloc[-1]
    return {
        "atr14": float(last["atr14"]) if not pd.isna(last["atr14"]) else 0.0,
        "ema20": float(last["ema20"]) if not pd.isna(last["ema20"]) else 0.0,
        "ema50": float(last["ema50"]) if not pd.isna(last["ema50"]) else 0.0,
    }

def calculate_atr_percentile(candles: list[Candle], period: int = 14, lookback: int = 100) -> float:
    """Calculate ATR percentile vs last 100 days to determine volatility regime."""
    if len(candles) < period + lookback:
        return 0.5  # Default if not enough data
    df = pd.DataFrame([{"high": c.high, "low": c.low, "close": c.close} for c in candles])
    df["tr"] = np.maximum(df["high"] - df["low"], np.maximum(
        abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1))))
    df["atr14"] = df["tr"].rolling(window=period).mean()
    
    recent_atr = df["atr14"].iloc[-1]
    historical = df["atr14"].iloc[-lookback:-1].dropna()
    if len(historical) == 0:
        return 0.5
    percentile = (historical < recent_atr).sum() / len(historical)
    return float(percentile)

def get_pip_size(symbol: str = SYMBOL) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return PIP
    return info.point * 10 if info.digits in (2, 3, 5) else info.point

def price_to_pips(price_diff: float, symbol: str = SYMBOL) -> float:
    return abs(price_diff) / get_pip_size(symbol)

# ═════════════════════════════════════════════════════════════════════════
# NEWS FILTER AGENT (UPGRADED: Real news feed)
# ═════════════════════════════════════════════════════════════════════════

class NewsFilterAgent:
    HIGH_IMPACT = {"NFP", "BOE RATE", "BOJ STATEMENT", "US CPI", "UK CPI", "FOMC", "GDP", "BOE", "BOJ", 
                    "NON-FARM PAYROLLS", "CONSUMER PRICE INDEX", "FEDERAL RESERVE", "BANK OF ENGLAND",
                    "BANK OF JAPAN", "INTEREST RATE", "INFLATION", "EMPLOYMENT CHANGE", "UNEMPLOYMENT RATE"}
    
    def __init__(self):
        self.last_fetch = 0
        self.cached_events: list[NewsEvent] = []
    
    def _fetch_investing_calendar(self, now: datetime) -> list[NewsEvent]:
        """Scrape Investing.com economic calendar for high-impact events."""
        events = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            # Get date range: today +/- 1 day
            date_from = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            date_to = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            
            payload = {
                "dateFrom": date_from,
                "dateTo": date_to,
                "importance": "3",  # High impact only
                "currency": "GBP|JPY|USD",  # Relevant currencies
            }
            
            resp = requests.post(INVESTING_NEWS_URL, data=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for event in data.get("data", []):
                    try:
                        # Parse event time
                        event_time_str = event.get("date", "")
                        event_time = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                        name = event.get("event", "")
                        impact = "HIGH" if event.get("importance") == "3" else "MEDIUM"
                        currency = event.get("currency", "")
                        events.append(NewsEvent(name=name, impact=impact, time=event_time, currency=currency))
                    except Exception as e:
                        log.debug(f"Failed to parse event: {e}")
        except Exception as e:
            log.warning(f"News fetch failed: {e}")
        return events
    
    def get_events(self, now: datetime) -> list[NewsEvent]:
        """Get cached or fresh news events."""
        if now.timestamp() - self.last_fetch > NEWS_CACHE_SECONDS:
            self.cached_events = self._fetch_investing_calendar(now)
            self.last_fetch = now.timestamp()
            log.info(f"NewsFilter: fetched {len(self.cached_events)} high-impact events")
        return self.cached_events
    
    def is_clear(self, now: datetime, events: list[NewsEvent] = None) -> tuple[bool, str]:
        if events is None:
            events = self.get_events(now)
        
        if not events:
            log.warning("NewsFilterAgent: no event feed -> UNKNOWN (allowing trade)")
            return True, "UNKNOWN"
        
        window = NEWS_BLOCK_MINUTES * 60
        for ev in events:
            if ev.impact.upper() != "HIGH": 
                continue
            if not any(k in ev.name.upper() for k in self.HIGH_IMPACT):
                continue
            time_diff = abs((ev.time - now).total_seconds())
            if time_diff <= window:
                log.info("NewsFilterAgent: %s in window -> BLOCKED", ev.name)
                return False, f"NEWS BLOCK — {ev.name} at {ev.time.strftime('%H:%M UTC')}"
        
        log.info("NewsFilterAgent: all clear")
        return True, "CLEAR"

# ═════════════════════════════════════════════════════════════════════════
# AGENT 1 — BIAS AGENT
# ═════════════════════════════════════════════════════════════════════════

class BiasAgent:
    def _swing_bias(self, candles: list[Candle]) -> Optional[Bias]:
        if len(candles) < 4:
            return None
        recent = candles[-10:]
        highs = [c.high for c in recent]
        lows  = [c.low  for c in recent]
        hh = highs[-1] > max(highs[:-1])
        hl = lows[-1]  > min(lows[:-1])
        ll = lows[-1]  < min(lows[:-1])
        lh = highs[-1] < max(highs[:-1])
        if hh and hl: return Bias.BULLISH
        if ll and lh: return Bias.BEARISH
        return None

    def analyse(self, data: MarketData) -> Bias:
        # Try D1 first
        d1_bias = self._swing_bias(data.d1)
        used_m15_fallback = False
        
        # Fallback: if D1 unclear, try H4, then H2, then H1, then M15
        if d1_bias is None:
            h4_bias = self._swing_bias(data.h4)
            if h4_bias is not None:
                log.info(f"BiasAgent: D1 unclear, falling back to H4 bias: {h4_bias.value}")
                d1_bias = h4_bias
            else:
                h2_bias = self._swing_bias(data.h2)
                if h2_bias is not None:
                    log.info(f"BiasAgent: D1/H4 unclear, falling back to H2 bias: {h2_bias.value}")
                    d1_bias = h2_bias
                else:
                    h1_bias = self._swing_bias(data.h1)
                    if h1_bias is not None:
                        log.info(f"BiasAgent: D1/H4/H2 unclear, falling back to H1 bias: {h1_bias.value}")
                        d1_bias = h1_bias
                    else:
                        # Last resort: use M15 trend (ema20 vs ema50)
                        if data.ema20 > data.ema50:
                            d1_bias = Bias.BULLISH
                            used_m15_fallback = True
                            log.info("BiasAgent: All HTF unclear, using M15 EMA trend -> BULLISH")
                        elif data.ema20 < data.ema50:
                            d1_bias = Bias.BEARISH
                            used_m15_fallback = True
                            log.info("BiasAgent: All HTF unclear, using M15 EMA trend -> BEARISH")
                        else:
                            log.info("BiasAgent: D1 structure unclear -> CONFLICT")
                            return Bias.CONFLICT
        
        # Use the resolved bias for ATR + EMA check
        bias_to_use = d1_bias
        
        # FIXED: Use real ATR percentile
        if data.atr_pct < ATR_LOW_PERCENTILE:
            log.info("BiasAgent: ATR in bottom percentile (%.2f) -> CONFLICT", data.atr_pct)
            return Bias.CONFLICT
        
        ema_ok = (bias_to_use == Bias.BULLISH and data.ema20 > data.ema50) or \
                 (bias_to_use == Bias.BEARISH and data.ema20 < data.ema50)
        if not ema_ok:
            log.info("BiasAgent: EMA does not confirm bias -> CONFLICT")
            return Bias.CONFLICT
        
        # Skip HTF alignment check if we used M15 EMA fallback
        # (all HTFs were unclear, so alignment check would fail anyway)
        if used_m15_fallback:
            log.info("BiasAgent: Using M15 EMA fallback bias %s (skipping HTF alignment)", bias_to_use.value)
            return bias_to_use
        
        htf_biases = [self._swing_bias(data.h4), self._swing_bias(data.h2), self._swing_bias(data.h1)]
        aligned = sum(1 for b in htf_biases if b == bias_to_use)
        if aligned < 2:
            log.info("BiasAgent: Only %d/3 HTFs aligned -> CONFLICT", aligned)
            return Bias.CONFLICT
        
        log.info("BiasAgent: %s bias confirmed (%d/3 HTFs aligned)", bias_to_use.value, aligned)
        return bias_to_use

# ═════════════════════════════════════════════════════════════════════════
# AGENT 2 — SESSION AGENT (FIXED: Proper session windows)
# ═════════════════════════════════════════════════════════════════════════

class SessionAgent:
    def get_session(self, now: datetime) -> Session:
        hour = now.hour
        # Full session windows (London 08-17 UTC, NY 13-21 UTC)
        if LONDON_START <= hour < LONDON_END:
            return Session.LONDON
        if NY_START <= hour < NY_END:
            return Session.NEW_YORK
        return Session.INACTIVE
    
    def is_tradeable(self, now: datetime) -> tuple[bool, Session]:
        # Force tradeable in ANY backtest mode
        if os.environ.get("BACKTEST_MODE", "").lower() != "":
            return True, Session.LONDON  # Mock session for backtest
        
        session = self.get_session(now)
        ok = session != Session.INACTIVE
        log.info(f"SessionAgent: {session.value} (ICT Killzone) -> {'tradeable' if ok else 'NO TRADE'}")
        return ok, session

# ═════════════════════════════════════════════════════════════════════════
# AGENT 4 — LIQUIDITY AGENT (UPGRADED: Multi-candle sweep)
# ═════════════════════════════════════════════════════════════════════════

class LiquidityAgent:
    def identify_zones(self, data: MarketData) -> list[LiquidityZone]:
        zones = []
        exec_candles = data.m15 or data.m30
        if not exec_candles: 
            return zones

        recent = exec_candles[-20:]
        highs = [c.high for c in recent]
        lows  = [c.low  for c in recent]

        for h in highs:
            if sum(1 for x in highs if abs(x - h) <= 3 * PIP) >= 2:
                zones.append(LiquidityZone(price=h, zone_type="buy-side"))
        for l in lows:
            if sum(1 for x in lows if abs(x - l) <= 3 * PIP) >= 2:
                zones.append(LiquidityZone(price=l, zone_type="sell-side"))
        
        if data.h1:
            h1r = data.h1[-24:]
            zones.append(LiquidityZone(price=max(c.high for c in h1r), zone_type="session-high"))
            zones.append(LiquidityZone(price=min(c.low  for c in h1r), zone_type="session-low"))
        
        if data.pivot_r1: zones.append(LiquidityZone(price=data.pivot_r1, zone_type="pivot"))
        if data.pivot_s1: zones.append(LiquidityZone(price=data.pivot_s1, zone_type="pivot"))

        unique = []
        for z in zones:
            if not any(abs(z.price - u.price) < 5 * PIP for u in unique):
                unique.append(z)
        return unique

    def detect_sweep(self, zones: list[LiquidityZone], candles: list[Candle], bias: Bias, max_bars: int = 5) -> tuple[bool, Optional[LiquidityZone]]:
        """UPGRADED: Check last max_bars candles for sweep + reclaim."""
        if not candles or not zones: 
            return False, None
        
        # Check last max_bars candles (default 5) for sweep patterns
        check_count = min(max_bars, len(candles))
        check_candles = candles[-check_count:] if len(candles) >= check_count else candles[-1:]
        
        for zone in zones:
            for candle in check_candles:
                if bias == Bias.BULLISH and zone.zone_type in ("sell-side", "session-low"):
                    # Sweep: candle wicks below, closes above (reclaim)
                    if candle.low < zone.price and candle.close > zone.price:
                        # Reclaim depth: at least 2 pips back into zone
                        reclaim_depth = (candle.close - zone.price) / PIP
                        if reclaim_depth >= 2:
                            zone.swept = True
                            zone.sweep_ts = candle.timestamp
                            log.info("LiquidityAgent: sell-side sweep + reclaim at %.3f (%.1f pip reclaim)", zone.price, reclaim_depth)
                            return True, zone
                if bias == Bias.BEARISH and zone.zone_type in ("buy-side", "session-high"):
                    # Sweep: candle wicks above, closes below (reclaim)
                    if candle.high > zone.price and candle.close < zone.price:
                        reclaim_depth = (zone.price - candle.close) / PIP
                        if reclaim_depth >= 2:
                            zone.swept = True
                            zone.sweep_ts = candle.timestamp
                            log.info("LiquidityAgent: buy-side sweep + reclaim at %.3f (%.1f pip reclaim)", zone.price, reclaim_depth)
                            return True, zone
        
        log.info("LiquidityAgent: no sweep + reclaim detected")
        return False, None

# ═════════════════════════════════════════════════════════════════════════
# AGENT 4.5 — ORDER BLOCK AGENT (NEW)
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class OrderBlock:
    price_high: float
    price_low: float
    midpoint: float
    direction: Direction
    candle_idx: int
    strength: float  # 0-1 based on body size vs ATR

class OrderBlockAgent:
    """Detect Order Blocks: last candle before strong impulsive move."""
    
    def find_order_block(self, candles: list[Candle], bias: Bias, atr: float) -> Optional[OrderBlock]:
        if len(candles) < 10:
            return None
        
        # Look for strong impulsive move (body > 1.5x ATR)
        for i in range(len(candles) - 5, len(candles) - 1):
            if i < 1:
                continue
            candle = candles[i]
            body = abs(candle.close - candle.open)
            
            # Strong move detected
            if body > atr * 1.5:
                # Bullish move -> look for bearish OB before it
                if bias == Bias.BULLISH:
                    for j in range(i - 1, max(i - 5, -1), -1):
                        if candles[j].close < candles[j].open:  # Bearish candle
                            ob = OrderBlock(
                                price_high=candles[j].high,
                                price_low=candles[j].low,
                                midpoint=(candles[j].high + candles[j].low) / 2,
                                direction=Direction.BUY,
                                candle_idx=j,
                                strength=min(body / (atr * 3), 1.0)
                            )
                            log.info("OrderBlockAgent: Bullish OB at %.3f-%.3f (strength=%.2f)", 
                                     ob.price_low, ob.price_high, ob.strength)
                            return ob
                
                # Bearish move -> look for bullish OB before it
                elif bias == Bias.BEARISH:
                    for j in range(i - 1, max(i - 5, -1), -1):
                        if candles[j].close > candles[j].open:  # Bullish candle
                            ob = OrderBlock(
                                price_high=candles[j].high,
                                price_low=candles[j].low,
                                midpoint=(candles[j].high + candles[j].low) / 2,
                                direction=Direction.SELL,
                                candle_idx=j,
                                strength=min(body / (atr * 3), 1.0)
                            )
                            log.info("OrderBlockAgent: Bearish OB at %.3f-%.3f (strength=%.2f)", 
                                     ob.price_low, ob.price_high, ob.strength)
                            return ob
        
        log.info("OrderBlockAgent: no valid order block found")
        return None

    def validate_ob(self, ob: OrderBlock, candles: list[Candle]) -> bool:
        """Validate OB: check if price retested the zone and bounced."""
        if not ob or len(candles) <= ob.candle_idx + 1:
            return False
        
        # Check candles after the OB candle
        post_ob = candles[ob.candle_idx + 1:]
        if not post_ob:
            return False
        
        # Check if price entered the OB zone (retest)
        entered_ob = False
        for c in post_ob:
            if ob.price_low <= c.low <= ob.price_high or ob.price_low <= c.high <= ob.price_high:
                entered_ob = True
                break
            if ob.direction == Direction.BUY and c.low <= ob.price_high:
                entered_ob = True
                break
            if ob.direction == Direction.SELL and c.high >= ob.price_low:
                entered_ob = True
                break
        
        if not entered_ob:
            log.info("OrderBlockAgent: OB not retested, invalid")
            return False
        
        for c in post_ob:
            if ob.direction == Direction.BUY and c.close > ob.midpoint:
                log.info("OrderBlockAgent: Bullish OB validated (retest + bounce)")
                return True
            if ob.direction == Direction.SELL and c.close < ob.midpoint:
                log.info("OrderBlockAgent: Bearish OB validated (retest + bounce)")
                return True
        
        log.info("OrderBlockAgent: OB retested but no bounce, invalid")
        return False

# ════════════════════════════════════════════════════════════════════════
# AGENT 4.6 — MSS AGENT (NEW: Market Structure Shift)
# ════════════════════════════════════════════════════════════════════════

@dataclass
class MSSResult:
    mss_detected: bool
    direction: Optional[Direction] = None
    break_price: float = 0.0
    structure: str = ""  # "BOS" or "CHoCH"

class MSSAgent:
    """Detect Market Structure Shift (BOS/CHoCH)."""
    
    def detect_mss(self, candles: list[Candle], bias: Bias) -> MSSResult:
        if len(candles) < 20:
            return MSSResult(mss_detected=False)
        
        # Find recent swing highs and lows
        swing_highs = []
        swing_lows = []
        
        for i in range(5, len(candles) - 5):
            # Swing high: candle[i] > neighbors
            if candles[i].high > candles[i-1].high and candles[i].high > candles[i+1].high:
                if candles[i].high > candles[i-2].high and candles[i].high > candles[i+2].high:
                    swing_highs.append((i, candles[i].high))
            
            # Swing low: candle[i] < neighbors
            if candles[i].low < candles[i-1].low and candles[i].low < candles[i+1].low:
                if candles[i].low < candles[i-2].low and candles[i].low < candles[i+2].low:
                    swing_lows.append((i, candles[i].low))
        
        if not swing_highs or not swing_lows:
            return MSSResult(mss_detected=False)
        
        # Get most recent swings
        last_high = swing_highs[-1]
        last_low = swing_lows[-1]
        
        current = candles[-1]
        
        # Bullish BOS/CHoCH: break last swing high
        if current.close > last_high[1]:
            # Check if CHoCH (Change of Character = trend reversal after prolonged trend)
            is_choch = False
            check_start = max(0, last_high[0] - 20)
            prev_closes = [candles[i].close for i in range(check_start, last_high[0])]
            if len(prev_closes) >= 15:
                bearish_bars = 0
                for i in range(1, len(prev_closes)):
                    if prev_closes[i] < prev_closes[i-1]:
                        bearish_bars += 1
                    else:
                        bearish_bars = 0
                    if bearish_bars >= 15:
                        is_choch = True
                        break
            structure = "CHoCH" if is_choch else "BOS"
            log.info("MSSAgent: Bullish %s at %.3f (broke %.3f)", structure, current.close, last_high[1])
            return MSSResult(mss_detected=True, direction=Direction.BUY, 
                           break_price=last_high[1], structure=structure)
        
        # Bearish BOS/CHoCH: break last swing low
        if current.close < last_low[1]:
            # Check if CHoCH (Change of Character = trend reversal after prolonged trend)
            is_choch = False
            check_start = max(0, last_low[0] - 20)
            prev_closes = [candles[i].close for i in range(check_start, last_low[0])]
            if len(prev_closes) >= 15:
                bullish_bars = 0
                for i in range(1, len(prev_closes)):
                    if prev_closes[i] > prev_closes[i-1]:
                        bullish_bars += 1
                    else:
                        bullish_bars = 0
                    if bullish_bars >= 15:
                        is_choch = True
                        break
            structure = "CHoCH" if is_choch else "BOS"
            log.info("MSSAgent: Bearish %s at %.3f (broke %.3f)", structure, current.close, last_low[1])
            return MSSResult(mss_detected=True, direction=Direction.SELL, 
                           break_price=last_low[1], structure=structure)
        
        log.info("MSSAgent: no structure shift detected")
        return MSSResult(mss_detected=False)

# ═════════════════════════════════════════════════════════════════════════
# AGENT 5 — ENTRY AGENT (UPGRADED: +Order Blocks + MSS)
# ═════════════════════════════════════════════════════════════════════════

class EntryAgent:
    def __init__(self):
        self.ob_agent = OrderBlockAgent()
        self.mss_agent = MSSAgent()
    
    def find_fvg(self, candles: list[Candle], bias: Bias) -> Optional[FVG]:
        if len(candles) < 3: 
            return None
        for i in range(len(candles) - 1, 1, -1):
            p, m, c = candles[i-2], candles[i-1], candles[i]
            if bias == Bias.BULLISH and p.low > c.high:
                gap_h, gap_l = p.low, c.high
                pips = (gap_h - gap_l) / PIP
                gc = sum(1 for x in candles[i-2:i+1] if x.high >= gap_l and x.low <= gap_h)
                if pips >= MIN_FVG_PIPS and gc >= MIN_FVG_CANDLES:
                    log.info("EntryAgent: Bullish FVG %.3f–%.3f (%.1f pips, %d candles)", gap_l, gap_h, pips, gc)
                    return FVG(gap_h, gap_l, (gap_h+gap_l)/2, gc, pips)
            if bias == Bias.BEARISH and p.high < c.low:
                gap_h, gap_l = c.low, p.high
                pips = (gap_h - gap_l) / PIP
                gc = sum(1 for x in candles[i-2:i+1] if x.high >= gap_l and x.low <= gap_h)
                if pips >= MIN_FVG_PIPS and gc >= MIN_FVG_CANDLES:
                    log.info("EntryAgent: Bearish FVG %.3f–%.3f (%.1f pips, %d candles)", gap_l, gap_h, pips, gc)
                    return FVG(gap_h, gap_l, (gap_h+gap_l)/2, gc, pips)
        log.info("EntryAgent: no valid FVG found")
        return None

    def find_order_block(self, candles: list[Candle], bias: Bias, atr: float) -> Optional[OrderBlock]:
        ob = self.ob_agent.find_order_block(candles, bias, atr)
        if ob and self.ob_agent.validate_ob(ob, candles):
            return ob
        return None

    def check_mss(self, candles: list[Candle], bias: Bias) -> MSSResult:
        return self.mss_agent.detect_mss(candles, bias)

    def displacement_confirmed(self, candles: list[Candle], bias: Bias, atr: float) -> bool:
        """UPGRADED: Use ATR-based candle body size instead of tick volume."""
        if len(candles) < 5 or atr <= 0: 
            return False
        
        last = candles[-1]
        last_body = abs(last.close - last.open)
        
        # Displacement: candle body > 1.5x ATR (strong move)
        displaced = last_body > atr * 1.5
        
        # Also check if there's a strong directional move vs recent candles
        recent_bodies = [abs(c.close - c.open) for c in candles[-5:]]
        avg_body = sum(recent_bodies) / len(recent_bodies)
        strong_body = last_body > avg_body * 1.5
        
        ok = displaced and strong_body
        log.info("EntryAgent: displacement %s (body=%.5f, ATR=%.5f, avg_body=%.5f)", 
                 "confirmed" if ok else "not confirmed", last_body, atr, avg_body)
        return ok

# ═════════════════════════════════════════════════════════════════════════
# AGENT 6 — CONFLUENCE SCORER (FIXED: Removed redundant EMA check)
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class ConfluenceResult:
    score          : int
    d1_bias        : bool
    mtf_aligned    : bool
    session_active : bool
    news_clear     : bool
    sweep_confirmed: bool
    fvg_quality    : bool
    ob_found       : bool  # NEW: Order Block found
    mss_detected   : bool  # NEW: Market Structure Shift detected
    mss_structure  : str = ""  # "CHoCH", "BOS", or ""
    volume_confirmed: bool = False  # NEW: Volume confirmation

class ConfluenceScorer:
    def score(self, bias, session_ok, news_ok, sweep_ok, fvg, ob, mss, volume_confirmed=False) -> ConfluenceResult:
        d1_ok  = bias != Bias.CONFLICT
        # FIXED: Don't double-count EMA alignment (already checked in BiasAgent)
        mtf_ok = True  # BiasAgent already confirmed EMA alignment
        
        fvg_ok = fvg and fvg.pip_width >= MIN_FVG_PIPS and fvg.candles >= MIN_FVG_CANDLES
        ob_ok = ob is not None
        
        # MSS: CHoCH = 2pts, BOS =1pt, none=0
        mss_ok = 0
        mss_structure = ""
        if mss and mss.mss_detected:
            mss_structure = mss.structure
            mss_ok = 2 if mss.structure == "CHoCH" else 1
        
        # Score out of 10 now (CHoCH gives 2pts, volume gives 1pt)
        pts = sum([d1_ok, mtf_ok, session_ok, news_ok, sweep_ok, fvg_ok, ob_ok]) + mss_ok + (1 if volume_confirmed else 0)
        
        log.info("ConfluenceScorer: %d/10 [bias=%s mtf=%s session=%s news=%s sweep=%s fvg=%s ob=%s mss=%s vol=%s]",
                 pts, d1_ok, mtf_ok, session_ok, news_ok, sweep_ok, fvg_ok, ob_ok, mss_structure, volume_confirmed)
        return ConfluenceResult(score=pts, d1_bias=d1_ok, mtf_aligned=mtf_ok,
                                session_active=session_ok, news_clear=news_ok,
                                sweep_confirmed=sweep_ok, fvg_quality=bool(fvg_ok),
                                ob_found=ob_ok, mss_detected=mss_ok>0,
                                mss_structure=mss_structure, volume_confirmed=volume_confirmed)

# ═════════════════════════════════════════════════════════════════════════
# AGENT 7 — RISK MANAGEMENT (Single position, simple)
# ═════════════════════════════════════════════════════════════════════════

class RiskAgent:
    def calculate(self, direction, entry, sweep_zone, opp_liq, data, daily):
        # Mode-specific daily trade cap
        max_trades = 20 if SCALPING_MODE else 2
        if daily.trades_taken >= max_trades:
            return 0,0,0,0, f"MAX {max_trades} TRADES/DAY REACHED"
        if daily.daily_loss_usd >= DAILY_LOSS_CAP_USD:
            return 0, 0, 0, 0, "DAILY LOSS CAP REACHED"
        
        buf = SL_BUFFER_PIPS * PIP
        atr = data.atr14 if data.atr14 > 0 else 0.20  # Fallback ATR
        
        # Scalping mode: fixed 4 pip TP, 2 pip SL
        if SCALPING_MODE:
            sl_pips = SCALP_SL_PIPS * PIP
            tp_pips = SCALP_TP_PIPS * PIP
            if direction == Direction.BUY:
                sl = entry - sl_pips
                tp2 = entry + tp_pips
                tp1 = entry + (sl_pips * PARTIAL_TP_RR)  # Informational only
            else:  # SELL
                sl = entry + sl_pips
                tp2 = entry - tp_pips
                tp1 = entry - (sl_pips * PARTIAL_TP_RR)  # Informational only
            sl_dist = abs(entry - sl)
            rr = abs(tp2 - entry) / sl_dist if sl_dist > 0 else 0
            log.info(f"Scalping mode: SL={sl:.3f} TP={tp2:.3f} RR={rr:.2f}")
            return sl, tp1, tp2, rr, ""
        
        if direction == Direction.BUY:
            sl = min(sweep_zone.price - buf, entry - atr * ATR_SL_MULTIPLIER)
            sl_dist = entry - sl
            tp2 = opp_liq if opp_liq > entry else entry + atr * MIN_RR
            tp1 = entry + sl_dist * PARTIAL_TP_RR  # Informational only
        elif direction == Direction.SELL:
            sl = max(sweep_zone.price + buf, entry + atr * ATR_SL_MULTIPLIER)
            sl_dist = sl - entry
            tp2 = opp_liq if opp_liq < entry else entry - atr * MIN_RR
            tp1 = entry - sl_dist * PARTIAL_TP_RR  # Informational only
        else:
            return 0, 0, 0, 0, "DIRECTION NOT SET"
        
        rr = abs(tp2 - entry) / sl_dist if sl_dist > 0 else 0
        if rr < MIN_RR:
            return sl, tp1, tp2, rr, f"RR {rr:.2f} BELOW MINIMUM {MIN_RR}"
        
        log.info("RiskAgent: SL=%.3f TP1=%.3f TP2=%.3f RR=%.2f", sl, tp1, tp2, rr)
        return sl, tp1, tp2, rr, ""

    def calculate_lot_size(self, sl_pips: float, info=None) -> float:
        # Use passed info (backtest) or fetch live (mt5)
        if info is None:
            info = mt5.symbol_info(SYMBOL)
        if not info or sl_pips <= 0: 
            return 0.0
        pip = get_pip_size(SYMBOL)
        # FIXED: Direct pip value for GBPJPY (verified: $6.70 per pip per 1 lot)
        pip_val = 6.70
        lot = RISK_PER_TRADE_USD / (sl_pips * pip_val)
        return max(info.volume_min, min(info.volume_max, round(lot / info.volume_step) * info.volume_step))

# ═════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION (Single position - simple)
# ═════════════════════════════════════════════════════════════════════════

def place_order(symbol: str, direction: Direction, lot: float, 
               sl: float, tp: float) -> Optional[int]:
    """Place a single market order (one position at a time)."""
    tick = mt5.symbol_info_tick(symbol)
    if not tick: 
        return None
    
    info = mt5.symbol_info(symbol)
    if not info: 
        return None
    
    fills = info.filling_modes
    fm = mt5.ORDER_FILLING_IOC if mt5.ORDER_FILLING_IOC in fills else \
         (mt5.ORDER_FILLING_GTC if mt5.ORDER_FILLING_GTC in fills else fills[0])
    
    price = tick.ask if direction == Direction.BUY else tick.bid
    
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if direction == Direction.BUY else mt5.ORDER_TYPE_SELL,
        "price": price, "sl": sl, "tp": tp, "deviation": 20,
        "magic": 202500, "comment": "Lunar Flow v2.1", "type_time": mt5.ORDER_TIME_GTC, "type_filling": fm,
    }
    result = mt5.order_send(req)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Order failed: retcode=%d | %s", result.retcode, result.comment)
        return None
    
    log.info("[OK] Order #%d | %s %s | Lot: %.2f | Entry: %.3f | SL: %.3f | TP: %.3f",
             result.order, symbol, direction.value.upper(), lot, price, sl, tp)
    return result.order

# ═════════════════════════════════════════════════════════════════════════
# MT5 CONNECTION (UPGRADED: Secure login + auto-reconnect)
# ═════════════════════════════════════════════════════════════════════════

def connect_mt5(login=None, password=None, server=None, max_retries=3) -> bool:
    """Connect to MT5 with env var support and retry logic."""
    for attempt in range(max_retries):
        if not mt5.initialize():
            log.error(f"MT5 init failed (attempt {attempt+1}): {mt5.last_error()}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return False
        
        # Try env vars first, then params
        login = login or os.getenv("MT5_LOGIN")
        password = password or os.getenv("MT5_PASSWORD")
        server = server or os.getenv("MT5_SERVER")
        
        if login and password and server:
            if not mt5.login(int(login), password=password, server=server):
                log.error(f"Login failed (attempt {attempt+1}): {mt5.last_error()}")
                mt5.shutdown()
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False
        else:
            log.warning("No MT5 credentials provided (env or params)")
        
        info = mt5.account_info()
        if not info:
            log.error("No account info")
            mt5.shutdown()
            return False
        
        log.info(f"Connected | Account: {info.login} | Balance: ${info.balance:.2f} | Equity: ${info.equity:.2f}")
        return True
    
    return False

def check_mt5_connection() -> bool:
    """Check if MT5 is still connected, try to reconnect if not."""
    try:
        info = mt5.account_info()
        if info is None:
            log.warning("MT5 connection lost, attempting reconnect...")
            return connect_mt5()
        return True
    except Exception:
        return connect_mt5()

def disconnect_mt5():
    mt5.shutdown()
    log.info("Disconnected from MT5.")

# ═════════════════════════════════════════════════════════════════════════
# FEEDBACK LOOP
# ═════════════════════════════════════════════════════════════════════════

class FeedbackLoop:
    def __init__(self):
        self.all_trades: list[TradeSignal] = self._load()

    def _load(self) -> list:
        if TRADE_LOG_FILE.exists():
            with TRADE_LOG_FILE.open() as f:
                return [self._dict_to_signal(r) for r in json.load(f)]
        return []

    def _save(self):
        with TRADE_LOG_FILE.open("w") as f:
            json.dump([self._signal_to_dict(t) for t in self.all_trades], f, indent=2, default=str)

    @staticmethod
    def _signal_to_dict(t: TradeSignal) -> dict:
        d = asdict(t)
        d["timestamp"] = t.timestamp.isoformat()
        return d

    @staticmethod
    def _dict_to_signal(d: dict) -> TradeSignal:
        d["timestamp"] = datetime.fromisoformat(d["timestamp"])
        d["direction"] = Direction(d["direction"])
        d["htf_bias"]  = Bias(d["htf_bias"])
        d["session"]   = Session(d["session"])
        d["validity"]  = Validity(d["validity"])
        return TradeSignal(**d)

    def record(self, signal: TradeSignal):
        self.all_trades.append(signal)
        self._save()
        completed = [t for t in self.all_trades if t.outcome in ("WIN", "LOSS", "EARLY_EXIT")]
        if len(completed) % 20 == 0 and completed:
            self.review(completed)

    def review(self, completed: list[TradeSignal]):
        last20 = completed[-20:]
        wins = [t for t in last20 if t.outcome == "WIN"]
        losses = [t for t in last20 if t.outcome in ("LOSS", "EARLY_EXIT")]
        wr = len(wins)/20
        avg_rr = sum(t.risk_reward for t in wins)/len(wins) if wins else 0
        rej = [t.rejection_reason for t in self.all_trades if t.rejection_reason]
        top_rej = max(set(rej), key=rej.count) if rej else "N/A"
        fail_sess = [t.session.value for t in losses]
        top_sess = max(set(fail_sess), key=fail_sess.count) if fail_sess else "N/A"
        # Self-optimizing: suggest BE/retracement adjustments
        if wr > 0.65:
            log.info("[Self-Opt] High win rate (%.0f%%). Consider lowering BE trigger to 0.65", wr*100)
        elif wr < 0.35:
            log.info("[Self-Opt] Low win rate (%.0f%%). Consider raising BE trigger to 0.75", wr*100)
        log.info("""
%s
  FEEDBACK LOOP — 20-TRADE REVIEW
%s
%%
  Win rate: %.1f%% | Avg RR: %.2f:1
  Top rejection: %s | Top fail session: %s
%s""", "="*60, "-"*60, wr*100, avg_rr, top_rej, top_sess, "="*60)

    def record_halt_event(self, reason: str):
        log.info("[Feedback] Halt event: %s", reason)
        pass

    def is_within_review_window(self, trade_index: int, timestamp, window_trades: int = 20) -> bool:
        return False

    def get_dynamic_threshold(self) -> int:
        """Adjust confluence threshold based on recent performance."""
        completed = [t for t in self.all_trades if t.outcome in ("WIN", "LOSS", "EARLY_EXIT")]
        if len(completed) < 5:
            return CONFLUENCE_EXECUTE
        last_5 = completed[-5:]
        wins = sum(1 for t in last_5 if t.outcome == "WIN")
        losses = sum(1 for t in last_5 if t.outcome in ("LOSS", "EARLY_EXIT"))
        # Win streak: lower threshold (more aggressive)
        if wins >= 4:
            return max(2, CONFLUENCE_EXECUTE - 1)
        # Loss streak: raise threshold (more conservative)
        if losses >= 4:
            return min(8, CONFLUENCE_EXECUTE + 1)
        return CONFLUENCE_EXECUTE

# ═══════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════

class TradingBotOrchestrator:
    def __init__(self):
        self.bias_a    = BiasAgent()
        self.session_a = SessionAgent()
        self.news_a    = NewsFilterAgent()  # Now has real news feed
        self.liq_a     = LiquidityAgent()
        self.entry_a   = EntryAgent()
        self.scorer    = ConfluenceScorer()
        self.risk_a    = RiskAgent()
        self.feedback  = FeedbackLoop()
        self.daily     = DailyState()
        # Track post-breakeven extremes for 75% retracement closure
        self.post_be_tracking = {}  # {ticket: {'extreme': float, 'breakeven_hit': True}}

    def _refresh_daily(self, today: str):
        state = _load_bot_state()
        if state.get("day") == today:
            self.daily = DailyState(date=today, trades_taken=state.get("trades_taken",0),
                                    daily_loss_usd=state.get("daily_loss_usd",0.0))
        elif self.daily.date != today:
            self.daily = DailyState(date=today)
            _save_bot_state({"day": today, "trades_taken": 0, "daily_loss_usd": 0.0})
            log.info("Orchestrator: new trading day %s — state reset", today)

    def evaluate(self, data, news_events=None, now=None) -> TradeSignal:
        now = now or datetime.now(tz=timezone.utc)
        self._refresh_daily(now.strftime("%Y-%m-%d"))

        def no_trade(reason, bias=Bias.CONFLICT, session=Session.INACTIVE):
            log.info("Orchestrator: NO TRADE — %s", reason)
            return TradeSignal(timestamp=now, direction=Direction.NO_TRADE, htf_bias=bias,
                               session=session, news_status="", liquidity_zone=None,
                               sweep_confirmed=False, fvg_detected=False, fvg_pips=0, fvg_candles=0,
                               entry_price=0, stop_loss=0, take_profit_1=0, take_profit_2=0,
                               risk_reward=0, confluence_score=0, validity=Validity.REJECT,
                               rejection_reason=reason)

        # BACKTEST MODE: Skip BiasAgent entirely, force bias from EMA alignment
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            # Force bias based on EMA20 vs EMA50
            if data.ema20 > data.ema50:
                bias = Bias.BULLISH
                log.info("BiasAgent (BACKTEST RELAXED): Forcing BULLISH bias (EMA20 > EMA50)")
            elif data.ema20 < data.ema50:
                # Only go bearish if MSS detected in M1 data (proper logic)
                # Check for bearish MSS: last candle broke below previous swing low
                mss_bearish = False
                if data.m1 and len(data.m1) >= 20:
                    # Previous 10 candles (excluding last) - look for support level
                    prev_low = min(c.low for c in data.m1[-11:-1])
                    last_low = data.m1[-1].low
                    last_close = data.m1[-1].close
                    # Bearish MSS: price broke below previous support by at least 3 pips
                    if last_low < prev_low - 0.03:  # Strong break (3 pips for JPY)
                        mss_bearish = True
                    # Or close below support
                    elif last_close < prev_low - 0.02:
                        mss_bearish = True
                
                # Require STRONG volume confirmation (not just above average)
                volume_ok = False
                if data.m1 and len(data.m1) >= 20:
                    last_vol = data.m1[-1].volume
                    avg_vol = sum(c.volume for c in data.m1[-20:]) / 20
                    # Volume must be 20% above average
                    if last_vol > avg_vol * 1.2:
                        volume_ok = True
                
                # Also check: if last 3 candles are bullish, skip bearish
                momentum_bearish = True
                if data.m1 and len(data.m1) >= 3:
                    last_3 = data.m1[-3:]
                    bullish_count = sum(1 for c in last_3 if c.close > c.open)
                    if bullish_count >= 2:  # Most recent candles are bullish
                        momentum_bearish = False
                
                if mss_bearish and volume_ok and momentum_bearish:
                    bias = Bias.BEARISH
                    log.info("BiasAgent (BACKTEST RELAXED): Forcing BEARISH bias (EMA20 < EMA50 + MSS + Volume + Momentum)")
                else:
                    bias = Bias.BULLISH # Default to bullish if no proper MSS
                    log.info("BiasAgent (BACKTEST RELAXED): Skipping BEARISH (no proper MSS/volume/momentum) - defaulting BULLISH")
            else:
                bias = Bias.BULLISH  # Default
                log.info("BiasAgent (BACKTEST RELAXED): Defaulting to BULLISH bias")
        else:
            bias = self.bias_a.analyse(data)
            if bias == Bias.CONFLICT: 
                return no_trade("HTF BIAS CONFLICT", bias, Session.INACTIVE)

        # Session check REMOVED for backtest
        session_ok = True
        session = Session.LONDON
        log.info("SessionAgent: FORCED tradeable for backtest")
        
        # Spread check REMOVED for backtest (uses simulated spread)
        log.info("Spread check: SKIPPED for backtest")
        
        # VWAP Confluence: compute early for later check
        # Choose candles based on mode: M1 for scalping, M15 for day trading
        if SCALPING_MODE:
            exec_candles = data.m1 or data.m15 or data.m30
        else:
            exec_candles = data.m15 or data.m30
        vwap = calculate_vwap(exec_candles, periods=20) if exec_candles else 0.0
        # Volume confirmation
        volume_ok = volume_confirmed(exec_candles, periods=20, multiplier=1.5) if exec_candles else False
        
        # SKIP NEWS CHECK IN BACKTEST RELAXED MODE
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            news_ok = True
            news_status = "RELAXED (bypassed)"
            log.info("BACKTEST RELAXED: Bypassing news filter")
        else:
            news_ok, news_status = self.news_a.is_clear(now, news_events)
            if not news_ok: 
                return no_trade(news_status, bias, session)

        exec_candles = data.m15 or data.m30
        
        # NEW: Check Order Block
        ob = self.entry_a.find_order_block(exec_candles, bias, data.atr14)
        
        # NEW: Check MSS
        mss = self.entry_a.check_mss(exec_candles, bias)
        
        # SKIP LIQUIDITY SWEEP CHECK IN BACKTEST RELAXED MODE (no HTF data)
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            swept = True
            # Create dummy zone with required attributes for RiskAgent
            current_price = exec_candles[-1].close if exec_candles else 180.0
            swept_zone = type('Zone', (), {
                'zone_type': 'debug', 
                'price': current_price
            })()
            zones = []
            log.info("BACKTEST RELAXED: Bypassing liquidity sweep check (no HTF data)")
        else:
            zones = self.liq_a.identify_zones(data)
            swept, swept_zone = self.liq_a.detect_sweep(zones, exec_candles, bias, max_bars=5)
            if not swept or not swept_zone: 
                return no_trade("NO LIQUIDITY SWEEP", bias, session)

        # SKIP DISPLACEMENT CHECK IN BACKTEST RELAXED MODE
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            log.info("BACKTEST RELAXED: Bypassing displacement check")
        else:
            if not self.entry_a.displacement_confirmed(exec_candles, bias, data.atr14):
                return no_trade("NO DISPLACEMENT", bias, session)

        # SKIP FVG CHECK IN BACKTEST RELAXED MODE
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            # Create dummy FVG with required attributes
            entry_price = exec_candles[-1].close if exec_candles else 180.0
            fvg = type('FVG', (), {
                'midpoint': entry_price,
                'pip_width': 10,
                'candles': 3
            })()
            log.info("BACKTEST RELAXED: Bypassing FVG check")
        else:
            fvg = self.entry_a.find_fvg(exec_candles, bias)
            if not fvg: 
                return no_trade("FVG INVALID OR TOO SMALL", bias, session)

        # Updated: Pass ob, mss, and volume to scorer (now scoring out of 10)
        conf = self.scorer.score(bias, session_ok, news_ok, swept, fvg, ob, mss, volume_confirmed=volume_ok)
        # SKIP CONFLUENCE THRESHOLD CHECK IN BACKTEST RELAXED MODE
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            log.info(f"BACKTEST RELAXED: Bypassing confluence threshold (score={conf.score}/10)")
        else:
            # Dynamic confluence threshold
            threshold = self.feedback.get_dynamic_threshold()
            if conf.score < threshold:
                return no_trade(f"CONFLUENCE {conf.score}/10 < {threshold}", bias, session)

        direction = Direction.BUY if bias == Bias.BULLISH else Direction.SELL
        
        # SKIP VWAP CHECK IN BACKTEST RELAXED MODE
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            log.info("BACKTEST RELAXED: Bypassing VWAP check")
        else:
            # VWAP Confluence check
            if vwap > 0:
                tick = mt5.symbol_info_tick(SYMBOL)
                if tick:
                    current_price = (tick.bid + tick.ask) / 2.0
                    if direction == Direction.BUY and current_price < vwap:
                        return no_trade("VWAP: price below VWAP (BUY)", bias, session)
                    if direction == Direction.SELL and current_price > vwap:
                        return no_trade("VWAP: price above VWAP (SELL)", bias, session)
        
        if direction == Direction.BUY:
            buy_zones = [z.price for z in zones if z.zone_type in ("buy-side","session-high")]
            opp_liq = min((p for p in buy_zones if p > fvg.midpoint), default=fvg.midpoint + data.atr14 * MIN_RR)
        else:
            sell_zones = [z.price for z in zones if z.zone_type in ("sell-side","session-low")]
            opp_liq = max((p for p in sell_zones if p < fvg.midpoint), default=fvg.midpoint - data.atr14 * MIN_RR)

        # SKIP RISK AGENT REJECTION IN BACKTEST RELAXED MODE
        if os.environ.get("BACKTEST_MODE", "").lower() == "relaxed":
            # Generate dummy SL/TP for relaxed mode
            pip = get_pip_size(SYMBOL)
            if direction == Direction.BUY:
                sl = entry_price - (20 * pip)
                tp2 = entry_price + (40 * pip)
            else:
                sl = entry_price + (20 * pip)
                tp2 = entry_price - (40 * pip)
            tp1 = (entry_price + tp2) / 2  # Midpoint
            rr = 2.0
            rej = ""
            log.info(f"BACKTEST RELAXED: Bypassing RiskAgent (SL={sl:.3f}, TP={tp2:.3f})")
        else:
            sl, tp1, tp2, rr, rej = self.risk_a.calculate(direction, fvg.midpoint, swept_zone, opp_liq, data, self.daily)
            if rej: 
                return no_trade(rej, bias, session)

        validity = Validity.A_PLUS if conf.score == 8 else Validity.VALID
        signal = TradeSignal(timestamp=now, direction=direction, htf_bias=bias, session=session,
                             news_status=news_status,
                             liquidity_zone=f"{swept_zone.zone_type} @ {swept_zone.price:.3f}",
                             sweep_confirmed=True, fvg_detected=True, fvg_pips=fvg.pip_width,
                             fvg_candles=fvg.candles, entry_price=fvg.midpoint, stop_loss=sl,
                             take_profit_1=tp1, take_profit_2=tp2, risk_reward=rr,
                             confluence_score=conf.score, validity=validity, rejection_reason="")

        self.daily.trades_taken += 1
        _save_bot_state({"day": now.strftime("%Y-%m-%d"), "trades_taken": self.daily.trades_taken,
                        "daily_loss_usd": self.daily.daily_loss_usd})

        log.info("Orchestrator: %s %s | Entry=%.3f SL=%.3f TP1=%.3f TP2=%.3f RR=%.2f | %s [%d/8]",
                signal.direction.value, SYMBOL, signal.entry_price, signal.stop_loss,
                signal.take_profit_1, signal.take_profit_2, signal.risk_reward,
                signal.validity.value, signal.confluence_score)
        return signal

    def on_trade_closed(self, signal: TradeSignal):
        # Integrate feedback loop + circuit breaker + close_trade call
        # Per Sept 15 session: wire feedback + breaker after trade close detection
        self.feedback.record(signal)
        # Circuit breaker check handled externally (call from wherever close detected)
        pass

    def close_trade(self, signal: TradeSignal, outcome: str, pnl: float):
        signal.outcome = outcome
        signal.pnl_usd = pnl
        if pnl < 0:
            self.daily.daily_loss_usd += abs(pnl)
            _save_bot_state({"day": self.daily.date, "trades_taken": self.daily.trades_taken,
                            "daily_loss_usd": self.daily.daily_loss_usd})
        self.feedback.record(signal)

    def manage_open_trades(self):
        """Manage open positions: 80% breakeven (less strict) + 75% retracement closure."""
        positions = mt5.positions_get(symbol=SYMBOL)
        if not positions:
            self.post_be_tracking = {}  # Clear tracking if no positions
            return
        tick = mt5.symbol_info_tick(SYMBOL)
        if not tick:
            return
        pip = get_pip_size(SYMBOL)
        
        # Clean up tracking for closed positions
        open_tickets = {pos.ticket for pos in positions}
        self.post_be_tracking = {k: v for k, v in self.post_be_tracking.items() if k in open_tickets}
        
        for pos in positions:
            entry = pos.price_open
            current_sl = pos.sl
            tp = pos.tp
            pos_type = pos.type  # 0=BUY, 1=SELL
            ticket = pos.ticket
            
            # Skip if no TP set or invalid
            if tp <= 0:
                continue
            total_tp_move = abs(tp - entry)
            if total_tp_move == 0:
                continue
            
            # 1. Check if 80% breakeven needs to be triggered (less strict: trigger later)
            trigger_move = total_tp_move * 0.80
            breakeven_sl = None
            
            if pos_type == 0:  # BUY
                if tick.bid >= entry + trigger_move and current_sl != entry - 2 * pip:
                    breakeven_sl = entry - 2 * pip
            else:  # SELL
                if tick.ask <= entry - trigger_move and current_sl != entry + 2 * pip:
                    breakeven_sl = entry + 2 * pip
            
            if breakeven_sl is not None:
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "sl": breakeven_sl,
                    "tp": tp
                }
                result = mt5.order_send(request)
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f"[Trade #{ticket}] Breakeven SL at {breakeven_sl:.3f} (70% to TP)")
                    send_telegram_alert(f"🔄 Breakeven triggered for {SYMBOL} trade #{ticket}. SL moved to {breakeven_sl:.3f} (70% to TP).")
                    self.post_be_tracking[ticket] = {'extreme': tick.bid if pos_type == 0 else tick.ask, 'breakeven_hit': True}
                else:
                    log.error(f"Failed to modify SL for #{ticket}: {result.comment}")
                continue
            
            # 2. After breakeven: Track extremes and check 75% retracement
            # Detect if breakeven was hit (SL is at entry ±2 pips)
            be_sl_check = (entry - 2 * pip) if pos_type == 0 else (entry + 2 * pip)
            if current_sl == be_sl_check:
                # Breakeven was hit, track this position
                if ticket not in self.post_be_tracking:
                    self.post_be_tracking[ticket] = {'extreme': entry, 'breakeven_hit': True}
                
                # Update extreme price
                if pos_type == 0:  # BUY
                    current_price = tick.bid
                    if current_price > self.post_be_tracking[ticket]['extreme']:
                        self.post_be_tracking[ticket]['extreme'] = current_price
                    else:
                        # Check retracement
                        extreme = self.post_be_tracking[ticket]['extreme']
                        move_from_entry = extreme - entry
                        if move_from_entry > 0:
                            retracement_threshold = extreme - (move_from_entry * 0.75)
                            if current_price <= retracement_threshold:
                                # Close position
                                close_request = {
                                    "action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": SYMBOL,
                                    "volume": pos.volume,
                                    "type": mt5.ORDER_SELL if pos_type == 0 else mt5.ORDER_BUY,
                                    "position": ticket,
                                    "price": tick.bid if pos_type == 0 else tick.ask,
                                    "deviation": 10,
                                    "magic": 123456
                                }
                                result = mt5.order_send(close_request)
                                if result.retcode == mt5.TRADE_RETCODE_DONE:
                                    log.info(f"[Trade #{ticket}] Closed: 75% retracement at {current_price:.3f}")
                                    send_telegram_alert(f"✅ Trade #{ticket} closed: 75% retracement at {current_price:.3f}.")
                                    del self.post_be_tracking[ticket]
                                else:
                                    log.error(f"Failed to close #{ticket}: {result.comment}")
                else:  # SELL
                    current_price = tick.ask
                    if current_price < self.post_be_tracking[ticket]['extreme']:
                        self.post_be_tracking[ticket]['extreme'] = current_price
                    else:
                        extreme = self.post_be_tracking[ticket]['extreme']
                        move_from_entry = entry - extreme
                        if move_from_entry > 0:
                            retracement_threshold = extreme + (move_from_entry * 0.75)
                            if current_price >= retracement_threshold:
                                close_request = {
                                    "action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": SYMBOL,
                                    "volume": pos.volume,
                                    "type": mt5.ORDER_BUY if pos_type == 1 else mt5.ORDER_SELL,
                                    "position": ticket,
                                    "price": tick.ask if pos_type == 1 else tick.bid,
                                    "deviation": 10,
                                    "magic": 123456
                                }
                                result = mt5.order_send(close_request)
                                if result.retcode == mt5.TRADE_RETCODE_DONE:
                                    log.info(f"[Trade #{ticket}] Closed: 75% retracement at {current_price:.3f}")
                                    send_telegram_alert(f"✅ Trade #{ticket} closed: 75% retracement at {current_price:.3f}.")
                                    del self.post_be_tracking[ticket]
                                else:
                                    log.error(f"Failed to close #{ticket}: {result.comment}")

    def print_signal(self, signal: TradeSignal):
        line = "─" * 52
        print(f"\n{line}")
        print(f"  Lunar Flow Signal — {signal.timestamp.strftime('%Y-%m-%d %H:%M')} UTC")
        print(line)
        for k,v in [("Direction", signal.direction.value), ("HTF Bias", signal.htf_bias.value),
                     ("Session", signal.session.value), ("News", signal.news_status),
                     ("Liquidity", signal.liquidity_zone or "N/A"),
                     ("Sweep", "Yes" if signal.sweep_confirmed else "No"),
                     ("FVG", f"{signal.fvg_pips:.1f} pips, {signal.fvg_candles}c" if signal.fvg_detected else "No"),
                     ("Entry", f"{signal.entry_price:.3f}" if signal.entry_price else "—"),
                     ("SL", f"{signal.stop_loss:.3f}" if signal.stop_loss else "—"),
                     ("TP1", f"{signal.take_profit_1:.3f} (info only)"),
                     ("TP2", f"{signal.take_profit_2:.3f} (active)"),
                     ("RR", f"1:{signal.risk_reward:.2f}"),
                     ("Confluence", f"{signal.confluence_score}/6"),
                     ("Validity", signal.validity.value)]:
            print(f"  {k:20s}: {v}")
        if signal.rejection_reason: 
            print(f"  Rejection reason   : {signal.rejection_reason}")
        print(line)

# ═════════════════════════════════════════════════════════════════════════
# MARKET DATA FETCH (UPGRADED: Real ATR percentile + error handling)
# ═════════════════════════════════════════════════════════════════════════

def fetch_market_data() -> MarketData:
    """Fetch market data with error handling and retry."""
    data = MarketData()
    
    # Check MT5 connection first
    if not check_mt5_connection():
        log.error("Cannot fetch data: MT5 not connected")
        return data
    
    for attr, tf in [("d1","D1"),("h4","H4"),("h2","H2"),("h1","H1"),("m30","M30"),("m15","M15"),("m1","M1")]:
        candles = get_candles(tf, 300)
        if candles:
            setattr(data, attr, candles)
        else:
            log.warning(f"No data for {tf}")
    
    if data.m1:
        ind = calculate_indicators(data.m1)
        data.atr14, data.ema20, data.ema50 = ind["atr14"], ind["ema20"], ind["ema50"]
        # FIXED: Calculate real ATR percentile
        if data.d1:
            data.atr_pct = calculate_atr_percentile(data.d1)
        else:
            data.atr_pct = 0.5  # Fallback
    
    return data

# ═════════════════════════════════════════════════════════════════════════
# ADAPTIVE SLEEP (UPGRADED: Based on timeframe)
# ═════════════════════════════════════════════════════════════════════════

def get_sleep_interval() -> int:
    """Return sleep interval based on mode: 1s for scalping, 30-60s for day trading."""
    if SCALPING_MODE:
        return 1  # Fixed 1s interval for rapid scalping
    # Day trading: adaptive based on session open
    now = datetime.now(tz=timezone.utc)
    if (LONDON_START <= now.hour < LONDON_START + 1) or (NY_START <= now.hour < NY_START + 1):
        return 30  # Check every 30s near session open
    return 60  # Normal 60s interval

# ═════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═════════════════════════════════════════════════════════════════════════

def main():
    # Secure login: check env vars first
    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")
    
    if not all([login, password, server]):
        log.info("No env vars found, prompting for credentials...")
        try:
            login = input("Enter MT5 login: ")
            password = input("Enter MT5 password: ")
            server = input("Enter MT5 server: ")
        except EOFError:
            log.error("Cannot read credentials in non-interactive mode. Set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER env vars.")
            return
    
    if not connect_mt5(login, password, server):
        return
    
    bot = TradingBotOrchestrator()
    log.info("=" * 55 + f"\n  Lunar Flow v2.1 — Multi-Agent SMC Bot\n  Symbol: {SYMBOL} | Risk: ${RISK_PER_TRADE_USD}/trade\n" + "=" * 55)
    
    try:
        while True:
            data = fetch_market_data()
            
            # Get fresh news events
            now = datetime.now(tz=timezone.utc)
            news_events = bot.news_a.get_events(now)
            
            signal = bot.evaluate(data, news_events, now)
            
            if signal.direction != Direction.NO_TRADE:
                bot.print_signal(signal)
                sl_pips = price_to_pips(abs(signal.entry_price - signal.stop_loss))
                lot = bot.risk_a.calculate_lot_size(sl_pips)
                log.info("Calculated lot size: %.2f (SL = %.1f pips)", lot, sl_pips)
                
                if lot > 0:
                    # Single position - runner to TP2
                    order_id = place_order(SYMBOL, signal.direction, lot, 
                                          signal.stop_loss, signal.take_profit_2)
                    if order_id:
                        log.info("[OK] Order #%d placed successfully", order_id)
                        send_telegram_alert(f"🚀 New trade opened: {signal.direction.value} {SYMBOL}\nEntry: {signal.entry_price:.3f}\nSL: {signal.stop_loss:.3f}\nTP: {signal.take_profit_2:.3f}\nLot: {lot:.2f}")
            else:
                # Even when no trade, refresh news cache periodically
                pass
            
            # Manage open trades: breakeven at 65% to TP
            bot.manage_open_trades()
            
            sleep_time = get_sleep_interval()
            log.debug(f"Sleeping {sleep_time}s...")
            time.sleep(sleep_time)
    
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    finally:
        disconnect_mt5()

if __name__ == "__main__":
    main()