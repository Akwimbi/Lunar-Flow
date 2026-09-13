# Backtest v2.2 Fixes (2026-05-02)

Specific fixes applied to `backtest_lunar_flow.py` to fix trade simulation bugs.

## 1. Lot Size Cap for Small Accounts

**Problem**: $10 account with 0.02 lot sizes → spread cost ~$9.32/trade. 18 trades = $167.74 costs vs $73.30 gross PnL = -$94.44 net.

**Fix**: Add cap after lot calculation:
```python
lot = self.risk_agent.calculate_lot_size(sl_pips_val)
lot = min(lot, 0.01)  # Cap at 0.01 for small accounts
```

**Impact**: Spread cost drops to ~$4.66/trade. 18 trades save ~$84.

---

## 2. Breakeven Logic Fix (Let Winners Breathe)

**Problem**: Original breakeven logic:
- Trigger: +10 pips profit
- SL moves to: entry ±1 pip
- Same candle triggers breakeven AND hits new SL → trade exits immediately (1 bar held)

**Fix**:
| Parameter | Old | New |
|-----------|-----|-----|
| Trigger | +10 pips | **+15 pips** |
| SL Placement (BUY) | entry +1 pip | **entry -2 pips** |
| SL Placement (SELL) | entry -1 pip | **entry +2 pips** |

```python
if direction == Direction.BUY and candle.high >= entry + 15 * pip:
    trade.current_sl = entry - (2 * pip)  # SL at entry - 2 pips
    trade.breakeven_hit = True
    trade.just_hit_breakeven = True
```

---

## 3. Trade Management Structural Fix (Let Trades Run)

**Problem**: 
- `current_sl` was a local variable reset to `sl` every bar
- Breakeven + trailing updates overwritten each loop iteration
- All 18 trades held exactly 1 bar

**Fix**: 
1. Add fields to `BacktestTrade`:
```python
just_hit_breakeven: bool = False  # Skip exit check on breakeven candle
current_sl: float = 0.0  # Track current SL (persists across bars)
```

2. Initialize `current_sl` once before loop:
```python
if trade.current_sl == 0.0:
    trade.current_sl = sl
```

3. Skip exit checks on breakeven candle:
```python
if not trade.just_hit_breakeven:
    # Check SL/TP exit conditions
    ...
```

4. Reset flag next candle:
```python
if trade.just_hit_breakeven:
    trade.just_hit_breakeven = False
```

**Impact**: Trades now run 10-30+ bars (hours) instead of 1 bar (15 min).

---

## 4. WSL vsock Errors Masking Python Errors

**Problem**: Running MT5 backtests from WSL throws:
```
<3>WSL (PID) ERROR: UtilAcceptVsock:271: accept4 failed 110
```
This masks the real Python error (syntax, import, etc.).

**Workarounds**:
1. **Use `execute_code` with AST**: Check syntax without running MT5:
```python
import ast
with open('/mnt/c/Users/akwim/lunar_flow.py', 'r') as f:
    tree = ast.parse(f.read())
```

2. **Run on Windows directly**: Open CMD/PowerShell, run:
```cmd
"C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe" backtest_lunar_flow.py
```

3. **Capture output to file** (if must run from WSL):
```bash
cd /mnt/c/Users/akwim && python.exe script.py > C:\path\output.txt 2>&1
```

---

## Results Before/After (Expected)

| Metric | Before | After (Est.) |
|--------|--------|---------------|
| Avg Bars Held | 1.0 | **10-30** |
| Breakeven Rate | 100% (false) | **Realistic %** |
| Net PnL | -$94.44 | **Positive** |
| Max Drawdown | 24.38% | **Lower** |
| Spread Cost/Trade | $9.32 | **$4.66** |
