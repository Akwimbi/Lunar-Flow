# MT5 Data Availability Check

Quick diagnostic to verify MT5 has required timeframe data before running backtests.

## Diagnostic Script

```python
# Save as: C:\Users\akwim\check_mt5_data.py
import MetaTrader5 as mt5
import sys

print("Initializing MT5...")
if not mt5.initialize():
    print(f"Failed to connect: {mt5.last_error()}")
    sys.exit(1)

print(f"MT5 Version: {mt5.version()}")

# Check M1 bars available
print("\nChecking GBPJPY M1 data...")
bars_m1 = mt5.copy_rates_from_pos("GBPJPY", mt5.TIMEFRAME_M1, 0, 100000)
if bars_m1 is None:
    print(f"Error getting M1 bars: {mt5.last_error()}")
else:
    print(f"M1 bars available: {len(bars_m1)}")
    if len(bars_m1) > 0:
        from datetime import datetime, timezone
        first = datetime.fromtimestamp(bars_m1[0][0], tz=timezone.utc)
        last = datetime.fromtimestamp(bars_m1[-1][0], tz=timezone.utc)
        print(f"Date range: {first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')}")

# Check M15 bars
print("\nChecking GBPJPY M15 data...")
bars_m15 = mt5.copy_rates_from_pos("GBPJPY", mt5.TIMEFRAME_M15, 0, 10000)
if bars_m15 is None:
    print(f"Error getting M15 bars: {mt5.last_error()}")
else:
    print(f"M15 bars available: {len(bars_m15)}")
    if len(bars_m15) > 0:
        from datetime import datetime, timezone
        first = datetime.fromtimestamp(bars_m15[0][0], tz=timezone.utc)
        last = datetime.fromtimestamp(bars_m15[-1][0], tz=timezone.utc)
        print(f"Date range: {first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')}")

mt5.shutdown()
print("\nDone.")
```

## Run It

```
C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe C:\Users\akwim\check_mt5_data.py
```

## Interpreting Results

| M1 Bars | M15 Bars | Action |
|----------|----------|--------|
| >100,000 | >10,000 | Ready for scalping (M1) and day trading (M15) |
| 0 or error | >10,000 | M1 unavailable — use synthetic M1 from M15 (see SKILL.md section 43) or run day trading mode (`SCALPING_MODE=False`) |
| 0 or error | 0 or error | MT5 not running, not logged in, or no GBPJPY data |

## Synthetic M1 Fallback

When M1 data unavailable, `backtest_live_like.py` auto-generates synthetic M1 from M15:
- Splits each M15 candle into 15 M1 candles
- Interpolates price progression across the 15 minutes
- Adds randomness to high/low within M15 range
- Divides M15 volume by 15

Updated: 2026-05-23
