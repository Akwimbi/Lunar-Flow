"""
Backtest Lunar Flow v2.1 — Historical Data Test
===============================================
Tests the 7-agent SMC/ICT bot on historical GBPJPY data.
Generates performance report with win rate, RR, drawdown, etc.

Usage:
    python backtest_lunar_flow.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List
import importlib.util

# ── Set BACKTEST_MODE BEFORE importing lunar_flow ────────────────────────
# This allows lunar_flow.py to detect backtest mode at runtime
# Default to STRICT for proper backtests; use RELAXED only for debugging
os.environ["BACKTEST_MODE"] = os.environ.get("BACKTEST_MODE", "strict").upper()

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

# ── Load lunar_flow.py (cross-platform path) ────────────────────────
import platform
if platform.system() == "Windows":
    lunar_flow_path = r"C:\Users\akwim\lunar_flow.py"
else:
    lunar_flow_path = "/mnt/c/Users/akwim/lunar_flow.py"

spec = importlib.util.spec_from_file_location("lunar_flow", lunar_flow_path)
if spec is None:
    print(f"[ERROR] Cannot find lunar_flow.py at {lunar_flow_path}")
    sys.exit(1)

lunar_flow = importlib.util.module_from_spec(spec)
sys.modules["lunar_flow"] = lunar_flow
spec.loader.exec_module(lunar_flow)

# Extract components
Direction = lunar_flow.Direction
Bias = lunar_flow.Bias
Session = lunar_flow.Session
Validity = lunar_flow.Validity
Candle = lunar_flow.Candle
MarketData = lunar_flow.MarketData
TradeSignal = lunar_flow.TradeSignal
ConfluenceResult = lunar_flow.ConfluenceResult
DailyState = lunar_flow.DailyState
NewsEvent = lunar_flow.NewsEvent

SYMBOL = lunar_flow.SYMBOL
PIP = lunar_flow.PIP
TF_MAP = lunar_flow.TF_MAP
ATR_LOW_PERCENTILE = lunar_flow.ATR_LOW_PERCENTILE
ATR_SL_MULTIPLIER = lunar_flow.ATR_SL_MULTIPLIER
NEWS_BLOCK_MINUTES = lunar_flow.NEWS_BLOCK_MINUTES
CONFLUENCE_EXECUTE = lunar_flow.CONFLUENCE_EXECUTE
SL_BUFFER_PIPS = lunar_flow.SL_BUFFER_PIPS
MIN_FVG_PIPS = lunar_flow.MIN_FVG_PIPS
MIN_FVG_CANDLES = lunar_flow.MIN_FVG_CANDLES
MIN_RR = lunar_flow.MIN_RR
PARTIAL_TP_RR = lunar_flow.PARTIAL_TP_RR
RISK_PER_TRADE_USD = lunar_flow.RISK_PER_TRADE_USD
DAILY_LOSS_CAP_USD = lunar_flow.DAILY_LOSS_CAP_USD
MAX_TRADES_PER_DAY = lunar_flow.MAX_TRADES_PER_DAY

get_candles = lunar_flow.get_candles
calculate_indicators = lunar_flow.calculate_indicators
calculate_atr_percentile = lunar_flow.calculate_atr_percentile
get_pip_size = lunar_flow.get_pip_size
price_to_pips = lunar_flow.price_to_pips
BiasAgent = lunar_flow.BiasAgent
SessionAgent = lunar_flow.SessionAgent
NewsFilterAgent = lunar_flow.NewsFilterAgent
LiquidityAgent = lunar_flow.LiquidityAgent
EntryAgent = lunar_flow.EntryAgent
ConfluenceScorer = lunar_flow.ConfluenceScorer
RiskAgent = lunar_flow.RiskAgent
TradingBotOrchestrator = lunar_flow.TradingBotOrchestrator
connect_mt5 = lunar_flow.connect_mt5
disconnect_mt5 = lunar_flow.disconnect_mt5
check_mt5_connection = lunar_flow.check_mt5_connection

print("[OK] Loaded lunar_flow.py successfully")

# ── Backtest Config ────────────────────────────────────────────────
# Support BACKTEST_START_DATE (YYYY-MM-DD) for date-range backtesting
# If set, calculates days from that date to today, overriding BACKTEST_DAYS
_start_date_str = os.environ.get("BACKTEST_START_DATE", "")
if _start_date_str:
    try:
        from datetime import date
        _start_date = datetime.strptime(_start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        _today = datetime.now(timezone.utc)
        BACKTEST_DAYS = (_today - _start_date).days + 1  # +1 to include start date
        print(f"[CONFIG] BACKTEST_START_DATE={_start_date_str} -> BACKTEST_DAYS={BACKTEST_DAYS}")
    except Exception as e:
        print(f"[WARNING] Invalid BACKTEST_START_DATE '{_start_date_str}': {e}. Using BACKTEST_DAYS.")
        BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "5"))
else:
    BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "5"))  # Days of history to load (default 5, was 1)
INITIAL_BALANCE    = float(os.environ.get("BACKTEST_BALANCE", "10.0"))  # Starting balance ($10 as corrected by user)
ACCOUNT_CURRENCY   = "USD"
TIMEFRAME_EXEC     = "M15"   # Execution timeframe
TIMEFRAME_HTFS     = ["D1", "H4", "H2", "H1", "M30"]  # HTFs for bias (+ H2, M30)

# Backtest mode: relax filters for more trades
BACKTEST_MODE      = os.environ.get("BACKTEST_MODE", "strict").lower()  # "strict" or "relaxed"
# In relaxed mode: skip ATR percentile check, require only 1/3 HTFs aligned
if BACKTEST_MODE == "relaxed":
    print(f"[CONFIG] Backtest mode: RELAXED (skipping strict bias filters)")
    # Override strict filters
    lunar_flow.ATR_LOW_PERCENTILE = 1.0  # Disable ATR percentile check (always pass)
    # We'll handle HTF alignment in the orchestrator
    
# Override strict filters for more trades (original values too restrictive)
# CONFLUENCE_EXECUTE: 5->3 (out of 8) | ATR_LOW_PERCENTILE: 0.20->0.50
CONFLUENCE_EXECUTE_OVERRIDE = int(os.environ.get("CONFLUENCE_EXECUTE", "3"))  # Lower = more trades
ATR_LOW_PERCENTILE_OVERRIDE = float(os.environ.get("ATR_LOW_PERCENTILE", "0.50"))  # Higher = more trades

# Apply backtest overrides to lunar_flow module
if CONFLUENCE_EXECUTE_OVERRIDE != lunar_flow.CONFLUENCE_EXECUTE:
    print(f"[CONFIG] Overriding CONFLUENCE_EXECUTE: {lunar_flow.CONFLUENCE_EXECUTE} -> {CONFLUENCE_EXECUTE_OVERRIDE}")
    lunar_flow.CONFLUENCE_EXECUTE = CONFLUENCE_EXECUTE_OVERRIDE

if ATR_LOW_PERCENTILE_OVERRIDE != lunar_flow.ATR_LOW_PERCENTILE:
    print(f"[CONFIG] Overriding ATR_LOW_PERCENTILE: {lunar_flow.ATR_LOW_PERCENTILE} -> {ATR_LOW_PERCENTILE_OVERRIDE}")
    lunar_flow.ATR_LOW_PERCENTILE = ATR_LOW_PERCENTILE_OVERRIDE

# Allow command-line override: python backtest_lunar_flow.py --balance 5000 --confluence 3 --atr 0.50
if __name__ == "__main__":
    import sys
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg.startswith("--balance="):
            try:
                INITIAL_BALANCE = float(arg.split("=")[1])
                print(f"[CONFIG] Initial balance set to: ${INITIAL_BALANCE:.2f}")
            except:
                print(f"[WARNING] Invalid --balance value, using default ${INITIAL_BALANCE:.2f}")
        elif arg.startswith("--confluence="):
            try:
                CONFLUENCE_EXECUTE_OVERRIDE = int(arg.split("=")[1])
                print(f"[CONFIG] Confluence threshold set to: {CONFLUENCE_EXECUTE_OVERRIDE} (lower = more trades)")
                # Apply immediately
                lunar_flow.CONFLUENCE_EXECUTE = CONFLUENCE_EXECUTE_OVERRIDE
            except:
                print(f"[WARNING] Invalid --confluence value, using {CONFLUENCE_EXECUTE_OVERRIDE}")
        elif arg.startswith("--atr="):
            try:
                ATR_LOW_PERCENTILE_OVERRIDE = float(arg.split("=")[1])
                print(f"[CONFIG] ATR percentile set to: {ATR_LOW_PERCENTILE_OVERRIDE} (higher = more trades)")
                # Apply immediately
                lunar_flow.ATR_LOW_PERCENTILE = ATR_LOW_PERCENTILE_OVERRIDE
            except:
                print(f"[WARNING] Invalid --atr value, using {ATR_LOW_PERCENTILE_OVERRIDE}")

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for backtest diagnostics
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("Backtest")

# ── Backtest State ────────────────────────────────────────────────────────
@dataclass
class BacktestTrade:
    ticket: int
    timestamp: datetime
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    sl_pips: float
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    outcome: str = ""  # WIN, LOSS, OPEN
    pnl_usd: float = 0.0
    pnl_pips: float = 0.0
    bars_held: int = 0
    # NEW: Trade management
    breakeven_hit: bool = False
    just_hit_breakeven: bool = False  # Skip exit check on breakeven candle
    current_sl: float = 0.0  # Track current SL level (persists across bars)
    highest_price: float = 0.0  # For BUY trailing
    lowest_price: float = 99999.0  # For SELL trailing
    spread_cost_usd: float = 0.0
    swap_cost_usd: float = 0.0

@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    avg_win_rr: float = 0.0
    avg_loss_rr: float = 0.0
    win_rate: float = 0.0
    total_pnl_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    avg_bars_held: float = 0.0
    best_trade_usd: float = 0.0
    worst_trade_usd: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)
    # NEW: Equity curve + cost analysis
    equity_curve: List[tuple] = field(default_factory=list)
    total_costs_usd: float = 0.0
    avg_spread_cost: float = 0.0
    avg_swap_cost: float = 0.0
    breakeven_pct: float = 0.0  # [(timestamp, balance), ...]

class BacktestEngine:
    def __init__(self):
        self.orchestrator = TradingBotOrchestrator()
        self.news_agent = NewsFilterAgent()
        self.risk_agent = RiskAgent()
        self.all_candles_m15: List[Candle] = []
        self.all_candles_htf: dict = {}
        self.trades: List[BacktestTrade] = []
        self.balance = INITIAL_BALANCE
        self.equity_curve: List[tuple] = []
        self.peak_equity = INITIAL_BALANCE
        self.max_dd_pct = 0.0
        self.indicators_cache = {}  # Pre-computed indicators
        
    def load_historical_data(self, days: int = BACKTEST_DAYS) -> bool:
        """Download historical data from MT5 (optimized - single pass)."""
        log.info(f"Loading {days} days of historical GBPJPY data...")
        
        now = datetime.now(tz=timezone.utc)
        from_date = now - timedelta(days=days)  # Needed for HTF loading
        # Calculate how many M15 bars we need: days * 24 hours * 4 bars/hour
        num_bars_needed = days * 96  # 96 M15 bars per day
        
        # Load ONLY M15 (skip HTFs for speed in quick test)
        log.info("Loading M15 candles...")
        # Use copy_rates_from_pos to get the LAST N bars (more reliable than range)
        rates_m15 = mt5.copy_rates_from_pos(SYMBOL, TF_MAP[TIMEFRAME_EXEC], 0, num_bars_needed)
        
        # Debug: log what we got
        log.info(f"[DEBUG] Requested {num_bars_needed} M15 bars, got {len(rates_m15) if rates_m15 is not None else 0}")
        if rates_m15 is not None and len(rates_m15) > 0:
            first_ts = datetime.fromtimestamp(rates_m15[0][0], tz=timezone.utc)
            last_ts = datetime.fromtimestamp(rates_m15[-1][0], tz=timezone.utc)
            log.info(f"[DEBUG] M15 range: {first_ts} to {last_ts}")
        
        if rates_m15 is None or len(rates_m15) == 0:
            log.error("Failed to load M15 historical data")
            return False
        
        self.all_candles_m15 = []
        for r in rates_m15:
            ts = datetime.fromtimestamp(r[0], tz=timezone.utc)
            self.all_candles_m15.append(
                Candle(TIMEFRAME_EXEC, float(r[1]), float(r[2]), 
                       float(r[3]), float(r[4]), float(r[5]), ts)
            )
        log.info(f"Loaded {len(self.all_candles_m15)} M15 candles")
        
        # Load HTF data for bias alignment (essential for proper SMC)
        # Only skip for very short tests (1-2 days) to save time
        if days <= 2:
            self.all_candles_htf = {}
            log.info("Skipping HTF data (short test: {} days)".format(days))
        else:
            log.info("Loading HTF candles for bias alignment...")
            for tf in TIMEFRAME_HTFS:
                log.info(f"  Loading {tf} candles...")
                tf_int = TF_MAP[tf]
                rates = mt5.copy_rates_range(SYMBOL, tf_int, from_date, now)
                if rates is None or len(rates) == 0:
                    log.warning(f"  Failed to load {tf} historical data")
                    self.all_candles_htf[tf] = []
                    continue
                
                candles = []
                for r in rates:
                    ts = datetime.fromtimestamp(r[0], tz=timezone.utc)
                    candles.append(
                        Candle(tf, float(r[1]), float(r[2]), 
                               float(r[3]), float(r[4]), float(r[5]), ts)
                    )
                self.all_candles_htf[tf] = candles
                log.info(f"  Loaded {len(candles)} {tf} candles")
            log.info(f"HTF loading complete: {sum(len(v) for v in self.all_candles_htf.values())} total HTF candles")
        
        # Pre-compute ALL M15 indicators ONCE (vectorized)
        log.info("Pre-computing indicators (vectorized)...")
        self._precompute_indicators()
        
        return len(self.all_candles_m15) > 0
    
    def _precompute_indicators(self):
        """Pre-compute ATR, EMA for all M15 candles (single pass)."""
        if len(self.all_candles_m15) < 14:
            log.warning("Not enough candles for indicators")
            return
        
        # Build DataFrame once
        df = pd.DataFrame([{
            "high": c.high, "low": c.low, "close": c.close,
            "timestamp": c.timestamp
        } for c in self.all_candles_m15])
        
        # True Range
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                abs(df["high"] - df["close"].shift(1)),
                abs(df["low"] - df["close"].shift(1))
            )
        )
        
        # ATR 14
        df["atr14"] = df["tr"].rolling(window=14).mean()
        # Fill NaN: backfill first 13 bars with first valid ATR (no zeros!)
        df["atr14"] = df["atr14"].bfill().fillna(0)
        
        # EMAs
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean().bfill().fillna(0)
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean().bfill().fillna(0)
        
        # ATR percentile (rolling 100-period)
        df["atr_pct"] = 0.0
        for i in range(114, len(df)):
            historical = df["atr14"].iloc[i-114:i].dropna()
            if len(historical) > 0:
                df.loc[i, "atr_pct"] = (historical < df["atr14"].iloc[i]).sum() / len(historical)
        
        # Store computed values for fast lookup
        self.indicators_cache = {
            "atr14": df["atr14"].fillna(0).tolist(),
            "ema20": df["ema20"].fillna(0).tolist(),
            "ema50": df["ema50"].fillna(0).tolist(),
            "atr_pct": df["atr_pct"].tolist(),
            "vwap": []  # Will fill next
        }
        
        # VWAP (20-period rolling)
        vwap_list = [0.0] * len(self.all_candles_m15)
        for i in range(20, len(self.all_candles_m15)):
            window = self.all_candles_m15[max(0, i-19):i+1]  # Last 20 candles
            vwap = lunar_flow.calculate_vwap(window, periods=20)
            vwap_list[i] = vwap
        self.indicators_cache["vwap"] = vwap_list
        
        log.info(f"Pre-computed indicators for {len(self.all_candles_m15)} candles")
    
    def build_market_data(self, candle_idx: int) -> MarketData:
        """Build MarketData object using pre-computed indicators (fast)."""
        m15_data = self.all_candles_m15[:candle_idx+1]
        current_time = self.all_candles_m15[candle_idx].timestamp
        
        htf_data = {}
        for tf in TIMEFRAME_HTFS:
            if tf in self.all_candles_htf:
                htf_data[tf] = [c for c in self.all_candles_htf[tf] 
                                if c.timestamp <= current_time]
        
        # Use pre-computed indicators (vectorized, done once)
        atr14 = self.indicators_cache["atr14"][candle_idx]
        ema20 = self.indicators_cache["ema20"][candle_idx]
        ema50 = self.indicators_cache["ema50"][candle_idx]
        atr_pct = self.indicators_cache["atr_pct"][candle_idx]
        vwap = self.indicators_cache["vwap"][candle_idx] if self.indicators_cache["vwap"] else 0.0
        
        return MarketData(
            symbol=SYMBOL,
            d1=htf_data.get("D1", []),
            h4=htf_data.get("H4", []),
            h2=htf_data.get("H2", []),  # NEW
            h1=htf_data.get("H1", []),
            m30=htf_data.get("M30", []),  # NEW
            m15=m15_data,
            atr14=atr14,
            atr_pct=atr_pct,
            ema20=ema20,
            ema50=ema50,
            vwap=vwap  # NEW: Add VWAP
        )
    
    def simulate_trade(self, signal: TradeSignal, current_idx: int) -> Optional[BacktestTrade]:
        """Simulate a trade with realistic costs + trade management (optimized)."""
        direction = signal.direction
        entry = signal.entry_price
        sl = signal.stop_loss
        tp = signal.take_profit_2
        sl_pips_val = price_to_pips(abs(entry - sl))
        # Pass cached symbol info (backtest) or None (live)
        lot = self.risk_agent.calculate_lot_size(sl_pips_val, self.cached_symbol_info)
        # With 1:3000 leverage, don't cap lot size (high leverage = low margin)
        # Keep minimum 0.01 but allow larger sizes based on risk
        if lot < 0.01:
            lot = 0.01
        
        if lot <= 0:
            log.warning("Invalid lot size, skipping trade")
            return None
        
        trade = BacktestTrade(
            ticket=len(self.trades) + 1,
            timestamp=signal.timestamp,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            lot_size=lot,
            sl_pips=sl_pips_val,
            highest_price=entry if direction == Direction.BUY else 0.0,
            lowest_price=entry if direction == Direction.SELL else 99999.0,
            current_sl=sl,  # Initialize with original SL
        )
        
        # Use CACHED symbol info (huge speed win - no MT5 calls)
        pip = self.cached_pip_size
        pip_value_per_pip = self.cached_pip_value
        spread_pips = self.cached_spread_pips
        
        trade.spread_cost_usd = spread_pips * pip_value_per_pip * lot
        
        # Look forward to find exit with trade management
        for i in range(current_idx + 1, len(self.all_candles_m15)):
            candle = self.all_candles_m15[i]
            trade.bars_held += 1
            # Update trailing prices
            if direction == Direction.BUY:
                trade.highest_price = max(trade.highest_price, candle.high)
            else:  # SELL
                trade.lowest_price = min(trade.lowest_price, candle.low)
            
            # Trade Management: 70% Breakeven + 75% Retracement Closure
            # Use trade.current_sl (persists across bars), initialize if not set
            if trade.current_sl == 0.0:
                trade.current_sl = sl
            
            # Track post-breakeven extremes (for retracement calculation)
            if trade.breakeven_hit:
                if direction == Direction.BUY:
                    trade.highest_price = max(trade.highest_price, candle.high)
                else:  # SELL
                    trade.lowest_price = min(trade.lowest_price, candle.low)
            
            if trade.bars_held >= 1:
                # Breakeven: move SL to entry when 70% of the way to TP
                if not trade.breakeven_hit:
                    total_tp_move = abs(trade.take_profit - entry)
                    trigger_move = total_tp_move * 0.70  # 70% to TP
                    if direction == Direction.BUY:
                        if candle.high >= entry + trigger_move:
                            trade.current_sl = entry - (2 * pip)  # SL at entry - 2 pips
                            trade.breakeven_hit = True
                            trade.just_hit_breakeven = True
                            log.info(f"[Trade #{trade.ticket}] Breakeven SL at {trade.current_sl:.3f} (70% to TP)")
                    else:  # SELL
                        if candle.low <= entry - trigger_move:
                            trade.current_sl = entry + (2 * pip)  # SL at entry + 2 pips
                            trade.breakeven_hit = True
                            trade.just_hit_breakeven = True
                            log.info(f"[Trade #{trade.ticket}] Breakeven SL at {trade.current_sl:.3f} (70% to TP)")
                
                # After breakeven: Close if price retraces 75% back toward entry
                if trade.breakeven_hit and not trade.just_hit_breakeven:
                    if direction == Direction.BUY:
                        # Calculate how far price has moved from entry post-breakeven
                        extreme_price = trade.highest_price
                        move_from_entry = extreme_price - entry
                        if move_from_entry > 0:
                            # 75% retracement back toward entry = extreme - (move * 0.75)
                            retracement_threshold = extreme_price - (move_from_entry * 0.75)
                            if candle.low <= retracement_threshold:
                                trade.exit_price = candle.close
                                trade.outcome = "WIN" if candle.close > entry else "LOSS"
                                log.info(f"[Trade #{trade.ticket}] Closed: 75% retracement to {candle.close:.3f}")
                                break
                    else:  # SELL
                        extreme_price = trade.lowest_price
                        move_from_entry = entry - extreme_price
                        if move_from_entry > 0:
                            retracement_threshold = extreme_price + (move_from_entry * 0.75)
                            if candle.high >= retracement_threshold:
                                trade.exit_price = candle.close
                                trade.outcome = "WIN" if candle.close < entry else "LOSS"
                                log.info(f"[Trade #{trade.ticket}] Closed: 75% retracement to {candle.close:.3f}")
                                break
            
            # Check exit conditions (skip if breakeven just activated)
            if not trade.just_hit_breakeven:
                if direction == Direction.BUY:
                    if candle.low <= trade.current_sl:
                        trade.exit_price = trade.current_sl
                        trade.outcome = "LOSS"  # SL hit = loss, period
                        break
                    if candle.high >= tp:
                        trade.exit_price = tp
                        trade.outcome = "WIN"
                        break
                elif direction == Direction.SELL:
                    if candle.high >= trade.current_sl:
                        trade.exit_price = trade.current_sl
                        trade.outcome = "LOSS"  # SL hit = loss, period
                        break
                    if candle.low <= tp:
                        trade.exit_price = tp
                        trade.outcome = "WIN"
                        break
            
            # Reset the flag after processing
            if trade.just_hit_breakeven:
                trade.just_hit_breakeven = False
            
            # Time Exit: Close after 96 bars (24 hours for M15) if no progress
            if trade.bars_held >= 96:
                trade.exit_price = candle.close
                trade.outcome = "OPEN"  # Time-based exit
                log.info(f"[Trade #{trade.ticket}] Time exit after {trade.bars_held} bars")
                break
            
            if i == len(self.all_candles_m15) - 1:
                trade.exit_price = candle.close
                trade.outcome = "OPEN"
        
        # Calculate PnL (FIXED: explicit sign handling)
        if trade.exit_price > 0:
            # Calculate pips correctly based on direction
            if direction == Direction.BUY:
                trade.pnl_pips = price_to_pips(trade.exit_price - entry)
            else:  # SELL
                trade.pnl_pips = price_to_pips(entry - trade.exit_price)
            
            # pnl_usd = pips * value_per_pip * lot_size
            trade.pnl_usd = trade.pnl_pips * pip_value_per_pip * lot
            
            # Subtract spread cost
            trade.pnl_usd -= trade.spread_cost_usd
            
            # Swap cost (if holding overnight = every 96 bars)
            # Use CACHED swap rates (no MT5 calls)
            swap_pips = self.cached_swap_long if direction == Direction.BUY else self.cached_swap_short
            nights_held = trade.bars_held // 96
            trade.swap_cost_usd = abs(swap_pips) * pip_value_per_pip * lot * nights_held
            trade.pnl_usd -= trade.swap_cost_usd
        
        self.balance += trade.pnl_usd
        
        # Track equity curve
        self.equity_curve.append((trade.timestamp, self.balance))
        
        if self.balance > self.peak_equity:
            self.peak_equity = self.balance
        dd = (self.peak_equity - self.balance) / self.peak_equity * 100
        if dd > self.max_dd_pct:
            self.max_dd_pct = dd
        
        return trade
    
    def generate_relaxed_signal(self, candle, idx, current_time) -> Optional[TradeSignal]:
        """Generate a trade signal directly for relaxed backtest mode (bypasses SMC)."""
        # Need at least 2 bars to check for crossover
        if idx < 1:
            return None
        
        # EMA values for current and previous bar
        ema20_curr = self.indicators_cache["ema20"][idx]
        ema50_curr = self.indicators_cache["ema50"][idx]
        ema20_prev = self.indicators_cache["ema20"][idx - 1]
        ema50_prev = self.indicators_cache["ema50"][idx - 1]
        
        # Debug: print EMA values periodically
        if idx % 100 == 0:
            log.debug(f"[RELAXED] idx={idx} EMA20_curr={ema20_curr:.5f} EMA50_curr={ema50_curr:.5f} EMA20_prev={ema20_prev:.5f} EMA50_prev={ema50_prev:.5f}")
        
        # Check for EMA20 crossing EMA50 (actual crossover, not just difference)
        bullish_cross = (ema20_prev <= ema50_prev) and (ema20_curr > ema50_curr)
        bearish_cross = (ema20_prev >= ema50_prev) and (ema20_curr < ema50_curr)
        
        if idx % 100 == 0:
            log.debug(f"[RELAXED] bullish_cross={bullish_cross} bearish_cross={bearish_cross}")
        
        if bullish_cross:
            direction = Direction.BUY
            bias = lunar_flow.Bias.BULLISH
        elif bearish_cross:
            direction = Direction.SELL
            bias = lunar_flow.Bias.BEARISH
        else:
            return None  # No crossover = no trade
        
        entry = candle.close
        pip = self.cached_pip_size
        
        # Simple SL/TP based on ATR
        atr14 = self.indicators_cache["atr14"][idx]
        sl_distance = max(20 * pip, atr14 * 1.5)  # Min 20 pips or 1.5x ATR
        tp_distance = sl_distance * 2.0  # 2:1 RR
        
        if direction == Direction.BUY:
            sl = entry - sl_distance
            tp = entry + tp_distance
        else:
            sl = entry + sl_distance
            tp = entry - tp_distance
        
        signal = TradeSignal(
            timestamp=current_time,
            direction=direction,
            htf_bias=bias,
            session=lunar_flow.Session.LONDON,
            news_status="RELAXED",
            liquidity_zone="N/A",
            sweep_confirmed=True,
            fvg_detected=True,
            fvg_pips=10,
            fvg_candles=3,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp,
            take_profit_2=tp,
            risk_reward=2.0,
            confluence_score=9,
            validity=lunar_flow.Validity.A_PLUS,
            rejection_reason=""
        )
        log.info(f"RELAXED SIGNAL: {direction.value} @ {entry:.3f} SL={sl:.3f} TP={tp:.3f}")
        return signal

    def run(self) -> BacktestResult:
        """Run the backtest over all historical candles (optimized)."""
        log.info("="*60)
        log.info(f" BACKTEST STARTING")
        log.info(f" Symbol: {SYMBOL} | Timeframe: {TIMEFRAME_EXEC}")
        log.info(f" Period: {BACKTEST_DAYS} days | Initial Balance: ${INITIAL_BALANCE:.2f}")
        log.info("="*60)
        
        if not self.load_historical_data(BACKTEST_DAYS):
            log.error("Failed to load historical data")
            return BacktestResult()
        
        # Cache MT5 symbol info ONCE (huge speed win)
        log.info("Caching MT5 symbol info...")
        self.cached_symbol_info = mt5.symbol_info(SYMBOL)
        if self.cached_symbol_info:
            # Pre-calculate static values
            self.cached_pip_size = get_pip_size(SYMBOL)
            self.cached_pip_value = 6.70  # GBPJPY: $6.70 per pip per 1 lot
            spread_points = self.cached_symbol_info.spread
            self.cached_spread_pips = spread_points / 10.0  # Points → pips
            # Allow override for testing lower spread costs
            override_spread = os.environ.get("BACKTEST_SPREAD_PIPS", "")
            if override_spread:
                try:
                    self.cached_spread_pips = float(override_spread)
                    log.info(f"[OVERRIDE] Using custom spread: {self.cached_spread_pips:.1f} pips (was {spread_points/10.0:.1f})")
                except ValueError:
                    log.warning(f"[WARNING] Invalid BACKTEST_SPREAD_PIPS '{override_spread}', using broker spread")
            # Cache swap rates (per 1 lot per night)
            self.cached_swap_long = self.cached_symbol_info.swap_long
            self.cached_swap_short = self.cached_symbol_info.swap_short
            log.info(f"Cached: spread={self.cached_spread_pips:.1f} pips, pip_value=${self.cached_pip_value}, swap_L={self.cached_swap_long}, swap_S={self.cached_swap_short}")
        
        total_candles = len(self.all_candles_m15)
        # Warmup: max of EMA50 (50), ATR14 (14), so 50 bars minimum
        start_idx = 50  # EMA50 needs 50 bars for stable values
        if start_idx >= total_candles:
            # Dynamic adjustment: use 30% of candles as warmup (min 14 for ATR)
            start_idx = max(14, int(total_candles * 0.3))
            log.warning(f"Reduced start_idx to {start_idx} (only {total_candles} candles loaded)")
        
        # CRITICAL: If still not enough candles, abort with clear message
        if start_idx >= total_candles:
            log.error(f"Not enough candles: {total_candles} loaded but need at least {start_idx + 1}")
            log.error(f"Try increasing BACKTEST_DAYS (currently {BACKTEST_DAYS}) to load more data")
            return self._generate_results()
        
        log.info(f"[DEBUG] BACKTEST_MODE={BACKTEST_MODE} start_idx={start_idx} total_candles={total_candles}")
        
        # Evaluation interval: relaxed checks every bar, strict checks every 4
        if BACKTEST_MODE.lower() == "relaxed":
            evaluation_interval = 1  # Every bar for more trades
            log.info(f"[DEBUG] Relaxed mode: checking every bar (interval=1)")
        else:
            evaluation_interval = 4  # Every 4 bars (SMC setups are rare)
            log.info(f"[DEBUG] Strict mode: checking every 4 bars (interval=4)")
        
        # Counter for debug
        relaxed_calls = 0
        relaxed_signals = 0
        
        # Process ALL loaded candles (no artificial limit)
        # For quick tests, user can set BACKTEST_DAYS=1
        log.info(f"Processing {total_candles - start_idx} candles ({BACKTEST_DAYS} days)...")
        
        # Set BACKTEST_MODE in environ so lunar_flow.py can read it
        import os as os_env
        os_env.putenv("BACKTEST_MODE", BACKTEST_MODE.upper())
        os.environ["BACKTEST_MODE"] = BACKTEST_MODE.upper()
        
        for idx in range(start_idx, total_candles):
            # Skip evaluation on most bars (SMC setups are rare)
            if idx % evaluation_interval != 0:
                continue
            
            candle = self.all_candles_m15[idx]
            current_time = candle.timestamp
            
            # RELAXED MODE: Generate trades directly (bypasses SMC logic)
            if BACKTEST_MODE.lower() == "relaxed":
                relaxed_calls += 1
                log.debug(f"Relaxed check idx={idx}/{total_candles} | EMA20={self.indicators_cache['ema20'][idx]:.3f} EMA50={self.indicators_cache['ema50'][idx]:.3f}")
                signal = self.generate_relaxed_signal(candle, idx, current_time)
                if signal:
                    relaxed_signals += 1
                    log.info(f"RELAXED SIGNAL GENERATED: {signal.direction.value} @ {signal.entry_price:.3f}")
                    trade = self.simulate_trade(signal, idx)
                    if trade:
                        self.trades.append(trade)
                        log.info(f"[TRADE #{trade.ticket}] {trade.direction.value.upper()} | "
                                 f"Entry: {trade.entry_price:.3f} | SL: {trade.stop_loss:.3f} | "
                                 f"TP: {trade.take_profit:.3f} | Outcome: {trade.outcome} | "
                                 f"PnL: ${trade.pnl_usd:.2f}")
                else:
                    log.debug(f"No relaxed signal at idx={idx}")
                continue  # Skip orchestrator path in relaxed mode
            
            # STRICT MODE: Use full SMC logic via orchestrator
            # Mock mt5.symbol_info_tick to return historical candle price (not live)
            original_tick = mt5.symbol_info_tick
            mt5.symbol_info_tick = lambda s: type('Tick', (), {
                'bid': candle.close,
                'ask': candle.close,
                'spread': int(self.cached_spread_pips * 10)
            })()
            
            data = self.build_market_data(idx)
            news_events = []  # Skip news for backtest
            
            # FORCE_TRADE DEBUG (set FORCE_TRADE=1 to test backtest loop)
            if os.environ.get("FORCE_TRADE", "0") == "1":
                # Dummy signal to test backtest simulation
                direction = Direction.BUY if data.ema20 > data.ema50 else Direction.SELL
                entry = candle.close
                pip = self.cached_pip_size
                if direction == Direction.BUY:
                    sl = entry - (20 * pip)  # 20 pip SL
                    tp = entry + (40 * pip)   # 40 pip TP (2:1 RR)
                else:
                    sl = entry + (20 * pip)
                    tp = entry - (40 * pip)
                signal = lunar_flow.TradeSignal(
                    timestamp=current_time,
                    direction=direction,
                    htf_bias=lunar_flow.Bias.BULLISH if direction == Direction.BUY else lunar_flow.Bias.BEARISH,
                    session=lunar_flow.Session.LONDON,
                    news_status="DEBUG",
                    liquidity_zone="DEBUG",
                    sweep_confirmed=True,
                    fvg_detected=True,
                    fvg_pips=10,
                    fvg_candles=3,
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit_1=tp,
                    take_profit_2=tp,
                    risk_reward=2.0,
                    confluence_score=9,
                    validity=lunar_flow.Validity.A_PLUS,
                    rejection_reason=""
                )
                log.info(f"FORCE TRADE DEBUG: {direction.value} @ {entry:.3f}")
            else:
                signal = self.orchestrator.evaluate(data, news_events, current_time)
            
            # Restore original tick function
            mt5.symbol_info_tick = original_tick
            
            if signal.direction != Direction.NO_TRADE:
                trade = self.simulate_trade(signal, idx)
                if trade:
                    self.trades.append(trade)
                    # Record trade to FeedbackLoop for dynamic confluence threshold
                    self.orchestrator.close_trade(signal, trade.outcome, trade.pnl_usd)
                    log.info(f"[TRADE #{trade.ticket}] {trade.direction.value.upper()} | "
                             f"Entry: {trade.entry_price:.3f} | SL: {trade.stop_loss:.3f} | "
                             f"TP: {trade.take_profit:.3f} | Outcome: {trade.outcome} | "
                             f"PnL: ${trade.pnl_usd:.2f}")
            
            if idx % 2000 == 0:  # Less frequent logs = faster
                pct = (idx / total_candles) * 100
                log.info(f"Progress: {pct:.1f}% ({idx}/{total_candles} candles)")
        
        log.info(f"[DEBUG] Backtest complete. relaxed_calls={relaxed_calls} relaxed_signals={relaxed_signals}")
        
        if BACKTEST_MODE.lower() == "relaxed":
            log.info(f"[DEBUG] Relaxed mode summary: {relaxed_signals} signals from {relaxed_calls} calls ({relaxed_signals/max(relaxed_calls,1)*100:.1f}%)")
        
        return self._generate_results()
    
    def _generate_results(self) -> BacktestResult:
        """Generate backtest performance report."""
        result = BacktestResult()
        result.trades = self.trades
        result.total_trades = len(self.trades)
        result.wins = len([t for t in self.trades if t.outcome == "WIN"])
        result.losses = len([t for t in self.trades if t.outcome == "LOSS"])
        result.win_rate = (result.wins / result.total_trades * 100) if result.total_trades > 0 else 0
        result.total_pnl_usd = sum(t.pnl_usd for t in self.trades)
        result.max_drawdown_pct = self.max_dd_pct
        result.avg_bars_held = sum(t.bars_held for t in self.trades) / len(self.trades) if self.trades else 0
        
        # NEW: Equity curve
        result.equity_curve = self.equity_curve
        
        # NEW: Cost analysis
        total_spread = sum(t.spread_cost_usd for t in self.trades)
        total_swap = sum(t.swap_cost_usd for t in self.trades)
        result.total_costs_usd = total_spread + total_swap
        result.avg_spread_cost = total_spread / len(self.trades) if self.trades else 0
        result.avg_swap_cost = total_swap / len(self.trades) if self.trades else 0
        
        # NEW: Trade management stats
        breakeven_hits = sum(1 for t in self.trades if t.breakeven_hit)
        result.breakeven_pct = (breakeven_hits / len(self.trades) * 100) if self.trades else 0
        
        win_trades = [t for t in self.trades if t.outcome == "WIN"]
        loss_trades = [t for t in self.trades if t.outcome == "LOSS"]
        result.avg_win_rr = (sum(abs(t.pnl_pips/t.sl_pips) for t in win_trades) / len(win_trades)) if win_trades else 0
        result.avg_loss_rr = (sum(abs(t.pnl_pips/t.sl_pips) for t in loss_trades) / len(loss_trades)) if loss_trades else 0
        
        gross_profit = sum(t.pnl_usd for t in win_trades)
        gross_loss = abs(sum(t.pnl_usd for t in loss_trades))
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        if self.trades:
            result.best_trade_usd = max(t.pnl_usd for t in self.trades)
            result.worst_trade_usd = min(t.pnl_usd for t in self.trades)
        
        return result


def print_results(result: BacktestResult):
    """Print formatted backtest results."""
    print("\n" + "="*60)
    print("  BACKTEST RESULTS — LUNAR FLOW v2.1")
    print("="*60)
    print(f"\n  Total Trades:     {result.total_trades}")
    print(f"  Wins / Losses:   {result.wins} / {result.losses}")
    print(f"  Win Rate:         {result.win_rate:.1f}%")
    print(f"\n  Total PnL:        ${result.total_pnl_usd:.2f}")
    print(f"  Total Costs:      ${getattr(result, 'total_costs_usd', 0):.2f} (spread+swap)")
    print(f"  Profit Factor:    {result.profit_factor:.2f}")
    print(f"  Max Drawdown:     {result.max_drawdown_pct:.2f}%")
    print(f"\n  Avg Win RR:       {result.avg_win_rr:.2f}:1")
    print(f"  Avg Loss RR:      {result.avg_loss_rr:.2f}:1")
    print(f"  Avg Bars Held:   {result.avg_bars_held:.1f}")
    print(f"\n  Best Trade:       ${result.best_trade_usd:.2f}")
    print(f"  Worst Trade:      ${result.worst_trade_usd:.2f}")
    print(f"\n  Breakeven Hit:    {getattr(result, 'breakeven_pct', 0):.1f}% of trades")
    print(f"  Avg Spread Cost:  ${getattr(result, 'avg_spread_cost', 0):.2f}/trade")
    print(f"  Avg Swap Cost:    ${getattr(result, 'avg_swap_cost', 0):.2f}/trade")
    print("="*60)
    
    output_file = Path.home() / ".lunar_flow" / "backtest_results.json"
    output_file.parent.mkdir(exist_ok=True)
    
    results_dict = {
        "config": {"symbol": SYMBOL, "days": BACKTEST_DAYS, "initial_balance": INITIAL_BALANCE},
        "results": {
            "total_trades": result.total_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate_pct": result.win_rate,
            "total_pnl_usd": result.total_pnl_usd,
            "total_costs_usd": getattr(result, 'total_costs_usd', 0),
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "avg_win_rr": result.avg_win_rr,
            "avg_loss_rr": result.avg_loss_rr,
            "avg_bars_held": result.avg_bars_held,
            "best_trade_usd": result.best_trade_usd,
            "worst_trade_usd": result.worst_trade_usd,
            "breakeven_pct": getattr(result, 'breakeven_pct', 0),
            "avg_spread_cost": getattr(result, 'avg_spread_cost', 0),
            "avg_swap_cost": getattr(result, 'avg_swap_cost', 0)
        },
        "equity_curve": [(ts.isoformat(), bal) for ts, bal in getattr(result, 'equity_curve', [])],
        "trades": [{
            "ticket": t.ticket, "timestamp": t.timestamp.isoformat(),
            "direction": t.direction.value, "entry": t.entry_price,
            "sl": t.stop_loss, "tp": t.take_profit, "lot": t.lot_size,
            "outcome": t.outcome, "pnl_usd": t.pnl_usd,
            "pnl_pips": t.pnl_pips, "bars_held": t.bars_held,
            "breakeven_hit": t.breakeven_hit,
            "spread_cost": t.spread_cost_usd,
            "swap_cost": t.swap_cost_usd
        } for t in result.trades]
    }
    
    with open(output_file, "w") as f:
        json.dump(results_dict, f, indent=2)
    
    print(f"\n  Detailed results saved to: {output_file}")
    print("="*60 + "\n")


def send_telegram_notification(result):
    """Send Telegram notification when backtest completes."""
    import os
    import urllib.request
    import json
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "deepstat8")
    
    if not token:
        print("[INFO] No TELEGRAM_BOT_TOKEN set, skipping Telegram notification")
        return
    
    # Build message
    msg = f"""🔔 BACKTEST COMPLETE

✅ Total Trades: {result.total_trades}
📊 Win Rate: {result.win_rate:.1f}%
💰 Total PnL: ${result.total_pnl_usd:.2f}
📉 Max Drawdown: {result.max_drawdown_pct:.2f}%
💸 Avg Spread Cost: ${getattr(result, 'avg_spread_cost', 0):.4f}/trade

Config: {os.environ.get('BACKTEST_MODE', 'strict').upper()} mode, {os.environ.get('BACKTEST_SPREAD_PIPS', 'broker')} pips spread
"""
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": msg}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[TELEGRAM] Notification sent: {resp.status}")
    except Exception as e:
        print(f"[TELEGRAM] Failed to send: {e}")


def main():
    if not connect_mt5():
        log.error("Failed to connect to MT5. Make sure terminal is running.")
        return
    
    try:
        engine = BacktestEngine()
        result = engine.run()
        print_results(result)
        send_telegram_notification(result)
    except KeyboardInterrupt:
        log.info("Backtest interrupted by user")
    except Exception as e:
        log.error(f"Backtest error: {e}", exc_info=True)
    finally:
        disconnect_mt5()


if __name__ == "__main__":
    main()
