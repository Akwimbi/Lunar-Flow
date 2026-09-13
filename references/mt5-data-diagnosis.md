# MT5 Data Diagnosis

Quick diagnostic to check if required timeframe data exists in MT5 before running backtests.

## Diagnostic Script

Save as `check_mt5_data.py` and run in cmd.exe/PowerShell:

```python
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

## Running

**cmd.exe:**
```
C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe C:\Users\akwim\check_mt5_data.py
```

**PowerShell:**
```powershell
C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe C:\Users\akwim\check_mt5_data.py
```

## Interpreting Results

| Scenario | M1 Bars | M15 Bars | Action |
|-----------|----------|----------|--------|
| Both available | >200k for 143 days | >4.5k for 143 days | Run scalping (M1) or day trading (M15) |
| M1 empty, M15 available | 0 or error (-2) | >4.5k | Use `SCALPING_MODE=False` (day trading only) |
| Both empty | 0 | 0 | Download data via MT5 History Center |

## Common Error: `(-2, 'Terminal: Invalid params')`

Means MT5 broker doesn't provide M1 data. Fix:
1. Open MT5 → File → Open History Center (or press F2 if it works)
2. Navigate: GBPJPY → M1 → Download
3. If no History Center available, fall back to M15 (day trading mode)

## Download Script (Alternative to UI)

```python
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

mt5.initialize()
symbol = "GBPJPY"
tf = mt5.TIMEFRAME_M1
start = datetime(2026, 1, 1, tzinfo=timezone.utc)
end = datetime(2026, 5, 23, 23, 59, tzinfo=timezone.utc)

rates = mt5.copy_rates_range(symbol, tf, start, end)
if rates:
    print(f"Downloaded {len(rates)} bars")
else:
    print(f"Failed: {mt5.last_error()}")
mt5.shutdown()
```

**Note:** Most brokers provide M15 by default; M1 often needs manual download.
