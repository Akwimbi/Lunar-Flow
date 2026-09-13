# Fast M15 Backtest Template
Minimal M15 backtest for rapid iteration (<60s execution).

## Script Path
`C:\Users\akwim\backtest_fast.py` (Windows) / `/mnt/c/Users/akwim/backtest_fast.py` (WSL)

## Usage
Run directly in Windows PowerShell/CMD (MT5 requires Windows host):
```powershell
C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe C:\Users\akwim\backtest_fast.py
```

## Key Features
1. **No agent dependencies**: Uses simple EMA12/EMA26 crossover for bias (avoids BiasAgent.get_bias missing method, non-existent VolumeProfileAgent)
2. **Real M15 data**: Fetches 7 days of M15 candles via `mt5.copy_rates_range()` (480 candles = ~0.1s execution)
3. **Simple trade simulation**: Checks next 20 M15 bars for SL/TP hits (2-pip SL, 10-pip TP for GBPJPY)
4. **Force trade mode**: Bypasses confluence threshold to test simulation logic

## Code
```python
#!/usr/bin/env python3
"""
Fast M15 backtest - no agent dependencies.
Uses simple EMA crossover for bias, executes in < 60s.
"""
import sys
import os
import time
from datetime import datetime, timedelta, timezone

# Setup
os.environ['PYTHONPATH'] = r'C:\Users\akwim'
sys.path.insert(0, r'C:\Users\akwim')

# Core imports
import pandas as pd
import MetaTrader5 as mt5

# Import only constants from lunar_flow
sys.path.insert(0, r'C:\Users\akwim')
import lunar_flow
SCALP_SL_PIPS = lunar_flow.SCALP_SL_PIPS
SCALP_TP_PIPS = lunar_flow.SCALP_TP_PIPS

def run_fast_backtest(days=7):
    print(f"\n{'='*60}")
    print(f"FAST M15 BACKTEST - {days} DAYS (SIMPLE EMA)")
    print(f"{'='*60}")
    
    # MT5 init
    if not mt5.initialize():
        print("ERROR: MT5 init failed")
        return
    print("[OK] MT5 initialized")
    
    # Time range
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)
    print(f"Range: {start_time.date()} to {end_time.date()}")
    
    # Fetch M15 data
    rates = mt5.copy_rates_range("GBPJPY", mt5.TIMEFRAME_M15, start_time, end_time)
    if rates is None or len(rates) == 0:
        print("ERROR: No M15 data")
        mt5.shutdown()
        return
    print(f"[OK] Loaded {len(rates)} M15 candles")
    
    # DataFrame
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.set_index('time', inplace=True)
    
    # Calculate EMAs
    df['ema_fast'] = df['close'].ewm(span=12, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=26, adjust=False).mean()
    
    # Backtest
    print(f"\nRunning backtest...")
    start = time.time()
    trades = []
    equity = 10000.0
    
    for i in range(50, len(df)):  # Skip first 50 for EMA warmup
        candle = df.iloc[i]
        current_time = df.index[i]
        
        # Simple EMA bias
        ema_fast = df['ema_fast'].iloc[i]
        ema_slow = df['ema_slow'].iloc[i]
        
        if ema_fast > ema_slow:
            bias = 'bullish'
        elif ema_fast < ema_slow:
            bias = 'bearish'
        else:
            bias = 'neutral'
        
        # Skip neutral
        if bias == 'neutral':
            continue
        
        # Force trade (bypass confluence)
        direction = 'BUY' if bias == 'bullish' else 'SELL'
        entry = candle['close']
        sl_pips = SCALP_SL_PIPS
        tp_pips = SCALP_TP_PIPS
        pip = 0.01
        
        sl_price = entry - sl_pips*pip if direction=='BUY' else entry + sl_pips*pip
        tp_price = entry + tp_pips*pip if direction=='BUY' else entry - tp_pips*pip
        
        # Check next 20 bars for SL/TP hit
        result = 'OPEN'
        bars_held = 0
        for j in range(i+1, min(i+20, len(df))):
            bar = df.iloc[j]
            bars_held += 1
            if direction == 'BUY':
                if bar['low'] <= sl_price: result = 'LOSS'; break
                if bar['high'] >= tp_price: result = 'WIN'; break
            else:
                if bar['high'] >= sl_price: result = 'LOSS'; break
                if bar['low'] <= tp_price: result = 'WIN'; break
        
        # PnL
        pnl = 0
        if result == 'WIN':
            pnl = tp_pips
            trades.append({'time': current_time, 'dir': direction, 'result': result, 'pnl': pnl, 'bars': bars_held})
            equity += pnl
        elif result == 'LOSS':
            pnl = -sl_pips
            trades.append({'time': current_time, 'dir': direction, 'result': result, 'pnl': pnl, 'bars': bars_held})
            equity += pnl
        
        # Progress
        if i % 100 == 0:
            elapsed = time.time() - start
            print(f"  Bar {i}/{len(df)} ({elapsed:.1f}s) | Trades: {len(trades)} | Equity: ${equity:.2f}")
    
    # Results
    elapsed = time.time() - start
    wins = sum(1 for t in trades if t['result']=='WIN')
    losses = sum(1 for t in trades if t['result']=='LOSS')
    
    print(f"\n{'='*60}")
    print(f"BACKTEST COMPLETE - {elapsed:.1f} SECONDS")
    print(f"{'='*60}")
    print(f"Total trades: {len(trades)}")
    print(f"Wins: {wins} ({wins/max(1,len(trades))*100:.1f}%)")
    print(f"Losses: {losses} ({losses/max(1,len(trades))*100:.1f}%)")
    print(f"Final equity: ${equity:.2f}")
    print(f"Execution time: {elapsed:.1f}s")
    
    if trades:
        print("\nLast 5 trades:")
        for t in trades[-5:]:
            print(f"  {t['time'].strftime('%Y-%m-%d %H:%M')} | {t['dir']} | {t['result']} | PnL: ${t['pnl']:.2f} | Bars: {t['bars']}")
    
    mt5.shutdown()
    print("\n[OK] Done!")

if __name__ == "__main__":
    run_fast_backtest(days=7)
```

## Pitfalls
1. **MT5 init**: Must run on Windows host (WSL throws vsock errors)
2. **EMA warmup**: Skip first 50 bars to avoid invalid EMA values
3. **SL too tight**: 2-pip SL for M15 causes immediate losses (avg 1-3 bars held)
4. **No volume/MSS filters**: EMA crossover alone yields ~12% win rate for GBPJPY

## Session Results (2026-05-24)
- 429 trades in 7 days
- 12.1% win rate, final equity $9,766 (down $234)
- Execution time: 0.1 seconds
- Purpose: Verify simulation logic, not profitability
- User frustration signal: 15+ empty messages → stop after 10+ turns without progress, rewrite instead of patching
