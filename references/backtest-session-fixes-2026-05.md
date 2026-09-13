# Backtest Session Fixes (2026-05-03)

Critical bug fixes applied to `backtest_lunar_flow.py` during this session, resolving fake 97% win rates, 49K+ trades, and zero-trade scenarios.

## 1. SL Hit Mislabeling (Fake Win Rate)
**Problem**: SL hits were incorrectly marked as wins using conditional logic:
```python
# Buggy code
trade.outcome = "LOSS" if trade.current_sl > entry else "WIN"
```
For BUY trades, SL is always below entry → `trade.current_sl > entry` is always false → outcome = "WIN" (wrong).

**Fix**: Unconditionally mark SL hits as LOSS:
```python
# Fixed code
if direction == Direction.BUY:
    if candle.low <= trade.current_sl:
        trade.exit_price = trade.current_sl
        trade.outcome = "LOSS"  # SL hit = loss, period
        break
elif direction == Direction.SELL:
    if candle.high >= trade.current_sl:
        trade.exit_price = trade.current_sl
        trade.outcome = "LOSS"  # SL hit = loss, period
        break
```

## 2. Rate Fetching Fix (Zero Trades)
**Problem**: `mt5.copy_rates_range()` returned empty data due to timestamp misalignment, causing zero candles loaded.

**Fix**: Use `mt5.copy_rates_from_pos()` to fetch candles by position index (avoids timestamp issues):
```python
# Fixed data loading
rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, num_candles)
```

## 3. EMA NaN Handling
**Problem**: Raw `ewm().mean()` left first N bars as NaN, breaking crossover detection.

**Fix**: Backfill EMA values to eliminate NaNs:
```python
df["ema20"] = df["close"].ewm(span=20, adjust=False).mean().bfill().fillna(0)
df["ema50"] = df["close"].ewm(span=50, adjust=False).mean().bfill().fillna(0)
```

## 4. EMA Crossover Logic (49K Trades)
**Problem**: Checking `ema20 > ema50` (every bar where they differ) instead of actual crossovers generated 49K+ trades in 2 years.

**Fix**: Check for actual EMA20 crossing EMA50:
```python
bullish_cross = (ema20_prev <= ema50_prev) and (ema20_curr > ema50_curr)
bearish_cross = (ema20_prev >= ema50_prev) and (ema20_curr < ema50_curr)
```

## 5. Indentation Fix
**Problem**: Patching caused `if idx % 2000 == 0:` to fall outside the main loop, raising `UnboundLocalError`.

**Fix**: Verify indentation matches context (4 spaces per level) after multiple patches, test with `python -m py_compile script.py`.

## 6. Early Return Guard
**Problem**: No check for `start_idx >= total_candles` caused the backtest loop to never execute.

**Fix**: Add early return:
```python
if start_idx >= total_candles:
    log.error(f"Not enough candles: {total_candles} < {start_idx+1}")
    return self._generate_results()
```

## 7. Lot Size Fix
**Problem**: `calculate_lot_size()` called `mt5.symbol_info()` every trade, returning None in backtest mode (no MT5 connection) → 0 lot size.

**Fix**: Pass cached symbol info from backtest engine:
```python
lot = self.risk_agent.calculate_lot_size(sl_pips_val, self.cached_symbol_info)
```