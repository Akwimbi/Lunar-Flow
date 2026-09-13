# Backtest Script — Lunar Flow v2.1

## Overview
Tests the 7-agent SMC/ICT bot (`lunar_flow.py`) on historical GBPJPY data. Generates performance report with win rate, RR, drawdown, etc.

## File Location
`C:\\Users\\akwim\\backtest_lunar_flow.py`

## Usage (Windows Side)

### Prerequisites
1. MT5 terminal running + logged in
2. `Tools → Options → Expert Advisors`:
   - ✅ Allow DLL imports
   - ✅ Allow automated trading
3. Windows host execution only (WSL cannot run MT5 scripts — throws `UtilAcceptVsock:271: accept4 failed 110` errors)

### Run (CMD Preferred)
```cmd
cd C:\Users\akwim
"C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe" backtest_lunar_flow.py
```

### Run (PowerShell)
```powershell
cd C:\Users\akwim
& "C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe" backtest_lunar_flow.py
```

*Note: If `python` command is hijacked by Microsoft Store aliases, disable them via Settings > Apps > Advanced app settings > App execution aliases, or use the full Python path as shown above.*

## What It Does
1. Connects to MT5 (read-only)
2. Downloads 730 days (2 years) of GBPJPY data:
   - M15 (execution timeframe, ~70k+ candles)
   - H1, H4, D1 (HTF bias)
3. Runs the 7-agent pipeline on every candle
4. Simulates trades — walks forward to find SL/TP hit
5. Outputs performance report to console
6. Saves detailed results to `C:\\Users\\akwim\\.lunar_flow\\backtest_results.json`

*Runtime: ~5-10 minutes for 2 years of M15 data*

## Metrics Generated
```
Total Trades | Wins / Losses
Win Rate (%) | Total PnL (USD)
Profit Factor | Max Drawdown (%)
Avg Win RR | Avg Loss RR
Avg Bars Held | Best/Worst Trade (USD)
```

## Key Implementation Notes

### Loading lunar_flow.py (Cross-Platform Fix)
Uses `importlib.util` + `platform.system()` to handle WSL/Windows path differences:
```python
import platform
import importlib.util

if platform.system() == "Windows":
    lunar_flow_path = r"C:\Users\akwim\lunar_flow.py"
else:
    lunar_flow_path = "/mnt/c/Users/akwim/lunar_flow.py"

spec = importlib.util.spec_from_file_location("lunar_flow", lunar_flow_path)
lunar_flow = importlib.util.module_from_spec(spec)
sys.modules["lunar_flow"] = lunar_flow
spec.loader.exec_module(lunar_flow)
```

### Windows Path Unicode Error (Pitfall)
**Problem:** `\\U` in `C:\\Users\\...` causes `SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes`.

**Solution:** 
- Use raw strings (`r\"C:\\...\"`) for Windows paths in Python
- Avoid `\\U`, `\\u`, `\\N` sequences in Python strings when using Windows paths
- The cross-platform fix above handles this automatically

### WSL Execution Error
**Problem:** Running MT5 Python scripts from WSL throws `UtilAcceptVsock:271: accept4 failed 110`.

**Solution:** MT5 Python package requires Windows host execution. Always run backtests from Windows CMD/PowerShell.

### Trade Simulation
- Walks forward through M15 candles after entry
- Checks `candle.low <= SL` (buy) or `candle.high >= SL` (sell) for SL hit
- Checks `candle.high >= TP` (buy) or `candle.low <= TP` (sell) for TP hit
- If reaching end of data, closes at last close price (marked "OPEN")

### PnL Calculation
```python
pip_value = (pip / info.trade_tick_size) * info.trade_tick_value
pnl_pips = price_to_pips(exit_price - entry)  # Buy
pnl_usd = pnl_pips * pip_value * lot_size
```

## Configuration (in script)
```python
BACKTEST_DAYS = 730        # 2 years (730 days) of historical data to test
INITIAL_BALANCE = 1000.0   # Starting balance for simulation
TIMEFRAME_EXEC = "M15"      # Execution timeframe
TIMEFRAME_HTFS = ["D1", "H4", "H1"]  # HTFs for bias
```

## Troubleshooting

### "Failed to load M15 historical data"
- MT5 terminal not running → Start it
- Not logged in → File → Login to Trade Account
- No data for symbol → Check Market Watch has GBPJPY

### SyntaxError with Windows paths
- Use raw strings (`r"..."`) for Windows paths
- Ensure cross-platform path check is in place (see above)

### "Failed to import from lunar_flow.py"
- Check `lunar_flow.py` exists at `C:\Users\akwim\`
- Verify Python env has `MetaTrader5`, `pandas`, `numpy` packages
- Check path uses `platform.system()` check for cross-platform support

### `UtilAcceptVsock` Error
- Running from WSL → Switch to Windows CMD/PowerShell
- MT5 terminal not running → Start it and login

### `python` command not found / Microsoft Store opens
- Disable App Execution Aliases: Settings > Apps > Advanced app settings > App execution aliases → toggle off python.exe/python3.exe
- Use full Python path: `"C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe"`
