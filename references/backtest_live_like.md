# backtest_live_like.py Reference
## Location
C:\Users\akwim\backtest_live_like.py (WSL path: /mnt/c/Users/akwim/backtest_live_like.py)

## Purpose
Live-like MT5 backtest script for lunar_flow, simulates real trading conditions with spread, slippage, execution delay.

## Modes
- Scalping (SCALPING_MODE=True): M1 data, 1s trades, fixed 2pip SL/4pip TP
- Day Trading (SCALPING_MODE=False): M15 data, ATR-based SL/TP

## Usage (Windows PowerShell)
```powershell
# Scalping mode
$env:SCALPING_MODE="True"
C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe backtest_live_like.py --days=5 --mode=scalp

# Day trading mode
$env:SCALPING_MODE="False"
C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe backtest_live_like.py --days=730 --mode=day --balance=10
```

## Parameters
- --days: Number of historical days to backtest (default 5 for scalping, 730 for day trading)
- --mode: scalp or day
- --balance: Starting account balance (default 10 USD)

## Output
Results saved to ~/.lunar_flow/backtest_live_like.json
