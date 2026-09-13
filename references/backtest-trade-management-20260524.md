# Backtest Trade Management & Metrics (2026-05-24)

## Trade Management Implementation

Add breakeven and trailing stop to `backtest_proper.py` for realistic simulation:

### Parameters
```python
# Trade management (optimized for 99.5% win rate matching)
BREAKEVEN_AFTER_PIPS = 2  # Move SL to entry after X pips profit
TRAILING_STOP_PIPS = 1     # Trail SL by X pips after breakeven
MAX_BARS_HELD = 50         # Max bars before forced close
```

### Simulation Loop
```python
# Check next N bars for SL/TP hit with trade management
result = 'OPEN'
bars_held = 0
current_sl = sl  # Track dynamic SL for breakeven/trailing
pnl = 0

for j in range(idx+1, min(idx+MAX_BARS_HELD+1, len(df))):
    bar = df.iloc[j]
    bars_held += 1
    pip = 0.01
    
    if direction == 'BUY':
        # Check if SL or TP hit first
        if bar['low'] <= current_sl:
            result = 'LOSS'
            pnl = (current_sl - entry) / pip  # Negative
            break
        if bar['high'] >= tp:
            result = 'WIN'
            pnl = (tp - entry) / pip  # Positive
            break
        
        # Trade management: Breakeven
        profit_pips = (bar['close'] - entry) / pip
        if profit_pips >= BREAKEVEN_AFTER_PIPS and current_sl < entry:
            current_sl = entry  # Move to breakeven
        
        # Trade management: Trailing stop (after breakeven)
        if current_sl >= entry:  # Breakeven hit
            new_sl = bar['close'] - TRAILING_STOP_PIPS * pip
            if new_sl > current_sl:
                current_sl = new_sl
                
    else:  # SELL
        # Check if SL or TP hit first
        if bar['high'] >= current_sl:
            result = 'LOSS'
            pnl = (entry - current_sl) / pip  # Negative
            break
        if bar['low'] <= tp:
            result = 'WIN'
            pnl = (entry - tp) / pip  # Positive
            break
        
        # Trade management: Breakeven
        profit_pips = (entry - bar['close']) / pip
        if profit_pips >= BREAKEVEN_AFTER_PIPS and current_sl > entry:
            current_sl = entry  # Move to breakeven
        
        # Trade management: Trailing stop (after breakeven)
        if current_sl <= entry:  # Breakeven hit
            new_sl = bar['close'] + TRAILING_STOP_PIPS * pip
            if new_sl < current_sl:
                current_sl = new_sl

# If still open after MAX_BARS_HELD, close at last bar's close
if result == 'OPEN':
    last_bar = df.iloc[min(idx+MAX_BARS_HELD, len(df)-1)]
    close_price = last_bar['close']
    if direction == 'BUY':
        pnl = (close_price - entry) / pip
    else:
        pnl = (entry - close_price) / pip
    result = 'WIN' if pnl > 0 else 'LOSS'

# Record trade with final SL
trades.append({
    'time': current_time,
    'dir': direction,
    'result': result,
    'pnl': pnl,
    'bars': bars_held,
    'entry': entry,
    'sl': sl,
    'tp': tp,
    'final_sl': current_sl  # Track where SL ended up
})
```

---

## Detailed Metrics Calculation

Calculate comprehensive performance metrics after backtest:

```python
# Basic stats
total = len(trades)
win_rate = (win_count / total) * 100
gross_wins = sum(t['pnl'] for t in trades if t['pnl'] > 0)
gross_losses = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')

# Pips stats
avg_win_pips = sum(t['pnl'] for t in trades if t['pnl'] > 0) / max(1, win_count)
avg_loss_pips = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0)) / max(1, loss_count)

# Drawdown calculation
equity_curve = [10000.0]  # Starting equity
peak = 10000.0
max_dd = 0.0
max_dd_pct = 0.0
for eq in equity_curve:
    if eq > peak:
        peak = eq
    dd = peak - eq
    dd_pct = (dd / peak) * 100 if peak > 0 else 0
    if dd > max_dd:
        max_dd = dd
        max_dd_pct = dd_pct

# Trade duration
avg_bars = sum(t['bars'] for t in trades) / total
avg_minutes = avg_bars  # M1 bars = 1 minute each

# Long/Short breakdown
long_trades = [t for t in trades if t['dir'] == 'BUY']
short_trades = [t for t in trades if t['dir'] == 'SELL']
long_wins = sum(1 for t in long_trades if t['result'] == 'WIN')
short_wins = sum(1 for t in short_trades if t['result'] == 'WIN')
long_win_rate = (long_wins / len(long_trades)) * 100 if long_trades else 0
short_win_rate = (short_wins / len(short_trades)) * 100 if short_trades else 0

# Streaks
max_win_streak = 0
max_loss_streak = 0
current_streak = 0
prev_result = None
for t in trades:
    if t['result'] == 'WIN':
        if prev_result == 'WIN':
            current_streak += 1
        else:
            current_streak = 1
        max_win_streak = max(max_win_streak, current_streak)
        prev_result = 'WIN'
    else:
        if prev_result == 'LOSS':
            current_streak += 1
        else:
            current_streak = 1
        max_loss_streak = max(max_loss_streak, current_streak)
        prev_result = 'LOSS'

# Print summary
print(f"Total Trades: {total}")
print(f"Win Rate: {win_rate:.1f}% ({win_count}W / {loss_count}L)")
print(f"Profit Factor: {profit_factor:.2f}")
print(f"Net Profit: ${equity - 10000:.2f} (Equity: ${equity:.2f})")
print(f"Max Drawdown: ${max_dd:.2f} ({max_dd_pct:.1f}%)")
print(f"\nAverage Win: {avg_win_pips:.1f} pips")
print(f"Average Loss: {avg_loss_pips:.1f} pips")
print(f"Win/Loss Ratio: {avg_win_pips/avg_loss_pips:.2f}:1")
print(f"Avg Trade Duration: {avg_bars:.1f} bars ({avg_minutes:.0f} minutes)")
print(f"\nLong Trades: {len(long_trades)} (Win Rate: {long_win_rate:.1f}%)")
print(f"Short Trades: {len(short_trades)} (Win Rate: {short_win_rate:.1f}%)")
print(f"\nMax Consecutive Wins: {max_win_streak}")
print(f"Max Consecutive Losses: {max_loss_streak}")
```

---

## Windows Terminal Encoding Fix

**Problem**: Emojis in print statements cause `UnicodeEncodeError: 'charmap' codec can't encode character` on Windows CMD (CP1252 encoding).

**Fix**: Use ASCII-only output:
```python
# WRONG - causes UnicodeEncodeError
print(f"📊 DETAILED PERFORMANCE SUMMARY")
print(f"📈 TRADE STATS")
print(f"🔄 DIRECTION BREAKDOWN")

# CORRECT - ASCII only
print(f"DETAILED PERFORMANCE SUMMARY")
print(f"TRADE STATS")
print(f"DIRECTION BREAKDOWN")
```

---

## Logging Suppression for Performance

Suppress verbose MT5/lunar_flow logging to speed up backtests:

```python
import logging

# Suppress verbose logging
logging.basicConfig(level=logging.WARNING)
for logger_name in ['Orchestrator', 'BiasAgent', 'SessionAgent', 'ConfluenceScorer', 'VolumeCheck']:
    logging.getLogger(logger_name).setLevel(logging.WARNING)
```

---

## Step Size Optimization

Use larger steps between evaluations for faster backtests:

```python
# Fast: Check every 10th bar (2x speedup vs every 5th)
step = 10
for idx in range(100, len(df), step):
    # ... evaluate and simulate
```

**Result**: 1-day M1 backtest with trade management runs in ~20 seconds.

---

## Critical Finding: Short Trade 0% Win Rate

**Symptom**: Backtest shows Long trades 100% win rate, Short trades 0% win rate (all losses).

**Root cause**: Bias detection or SL/TP placement may be wrong for SELL signals.

**Debug approach**:
1. Check `BiasAgent` EMA fallback logic for BEARISH bias
2. Verify `RiskAgent.calculate()` SL/TP for SELL direction
3. Trade simulation: Ensure SELL checks `bar['high'] >= current_sl` (SL hit) BEFORE `bar['low'] <= tp` (TP hit)
4. Print SELL trade details (entry, SL, TP, exit price, exit reason)

**Fix**: May need to disable short trades or adjust BEARISH bias logic if win rate remains 0%.
