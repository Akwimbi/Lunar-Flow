# 2026-05-24 Lunar Flow Backtest Session Postmortem

## Session Context
15+ turns debugging `lunar_flow.py` backtest (`backtest_live_like.py` / `backtest_simple.py`) with no meaningful progress, leading to user frustration (15+ empty messages sent consecutively).

## Critical Failures & Fixes

### 1. MT5 Returns 0 M15 Candles
- **Command**: `mt5.copy_rates_range("GBPJPY", mt5.TIMEFRAME_M15, from_date, now)` (1-day range test)
- **Symptom**: Returns empty array, no error message, MT5 initializes successfully
- **Root Cause**: Timestamp alignment issues with `copy_rates_range` (requested range not matching candle open times)
- **Fix**: Use `mt5.copy_rates_from_pos()` instead to avoid alignment issues:
  ```python
  # Fetches N candles from most recent position (0 = latest)
  rates = mt5.copy_rates_from_pos("GBPJPY", mt5.TIMEFRAME_M15, 0, 10000)
  ```

### 2. 0% Win Rate (516 Trades, All Losses)
- **Symptom**: All trades hit SL immediately (average 1.0 bars held)
- **Root Cause**: Synthetic M1 data had randomized high/low values, causing invalid price movements that triggered SL instantly
- **Fixes Applied**:
  - Force `SCALPING_MODE=False` to use real M15 data (no synthetic M1)
  - Raise `CONFLUENCE_EXECUTE` from 3 to 8 to filter low-quality trades
  - Lower `volume_confirmed` multiplier from 1.5 to 1.0
  - Raise `SCALP_TP_PIPS` from 4 to 10 (5:1 reward-risk ratio for 2-pip SL)

### 3. Backtest Timeout (600s+)
- **Symptom**: 143-day M1 backtest processes ~205k candles, exceeds 600s timeout
- **Fixes**:
  - Switch to M15 data: ~13.7k candles for 143 days (97% reduction)
  - Set `BACKTEST_DAYS=1` for quick debug tests (<1 minute runtime)

### 4. Python 3.13 Import Error
- **Error**: `AttributeError: 'NoneType' object has no attribute '__dict__'` when loading `lunar_flow.py` as module
- **Root Cause**: Python 3.13 dataclass import behavior requires pre-registering modules in `sys.modules`
- **Fix**: Set `sys.modules["lunar_flow"] = lf` before calling `exec_module()`:
  ```python
  import sys
  import importlib.util
  spec = importlib.util.spec_from_file_location("lunar_flow", "lunar_flow.py")
  lf = importlib.util.module_from_spec(spec)
  sys.modules["lunar_flow"] = lf  # Critical for Python 3.13 dataclasses
  spec.loader.exec_module(lf)
  ```

## User Experience Rule
After 10+ turns of incremental patching with no progress, the user sent 15+ empty messages indicating frustration. **Rule**: If stuck on the same issue for >10 turns, stop patching and rewrite the component (e.g., new minimal backtest script) instead of continuing incremental fixes.

## Key Takeaways
- Never use synthetic M1 data for backtests; always use real M15 data
- Use `copy_rates_from_pos` instead of `copy_rates_range` for MT5 data to avoid timestamp alignment issues
- Python 3.13 requires `sys.modules` pre-setting for dynamic module loads with dataclasses
- Prioritize quick test loops (<1 minute) over long backtests for debugging iterations
