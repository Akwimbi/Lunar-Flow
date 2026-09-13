# BiasAgent D1 Fallback Fix (2026-05-23)

## Problem
MT5 API often returns no D1 historical data for recent periods (e.g., 2026+), causing `BiasAgent.analyse()` to return `Bias.CONFLICT` for all candles. This blocked all trades in backtests and live trading.

Log symptom:
```
BiasAgent: D1 structure unclear -> CONFLICT
Orchestrator: NO TRADE — HTF BIAS CONFLICT
```

## Fix: Fallback Chain in `BiasAgent.analyse()`
Update `lunar_flow.py`'s `BiasAgent` class to resolve bias even when D1 data is missing:

```python
def analyse(self, data: MarketData) -> Bias:
    # Try D1 first
    d1_bias = self._swing_bias(data.d1)
    
    # Fallback: if D1 unclear, try H4, then H2, then H1, then M15
    if d1_bias is None:
        h4_bias = self._swing_bias(data.h4)
        if h4_bias is not None:
            log.info(f"BiasAgent: D1 unclear, falling back to H4 bias: {h4_bias.value}")
            d1_bias = h4_bias
        else:
            h2_bias = self._swing_bias(data.h2)
            if h2_bias is not None:
                log.info(f"BiasAgent: D1/H4 unclear, falling back to H2 bias: {h2_bias.value}")
                d1_bias = h2_bias
            else:
                h1_bias = self._swing_bias(data.h1)
                if h1_bias is not None:
                    log.info(f"BiasAgent: D1/H4/H2 unclear, falling back to H1 bias: {h1_bias.value}")
                    d1_bias = h1_bias
                else:
                    # Last resort: use M15 trend (ema20 vs ema50)
                    if data.ema20 > data.ema50:
                        d1_bias = Bias.BULLISH
                        log.info("BiasAgent: All HTF unclear, using M15 EMA trend -> BULLISH")
                    elif data.ema20 < data.ema50:
                        d1_bias = Bias.BEARISH
                        log.info("BiasAgent: All HTF unclear, using M15 EMA trend -> BEARISH")
                    else:
                        log.info("BiasAgent: D1 structure unclear -> CONFLICT")
                        return Bias.CONFLICT
    
    # Use the resolved bias for remaining checks
    bias_to_use = d1_bias
    
    # [Rest of existing checks: ATR guard, EMA confirmation, HTF alignment]
```

## Verification
After applying the fix:
1. Run backtest: `set SCALPING_MODE=True && C:\Users\akwim\AppData\Local\Programs\Python\Python313\python.exe C:\Users\akwim\backtest_live_like.py`
2. Check logs for fallback messages: `BiasAgent: D1 unclear, falling back to H4 bias: BULLISH`
3. Confirm trades execute (look for `[TRADE #1]` in output)

## Coverage
- Applies to both live trading (`lunar_flow.py`) and backtests (`backtest_live_like.py`, `backtest_lunar_flow.py`)
- Works for both scalping (M1) and day trading (M15) modes
- Resolves 0-trade issues caused by missing D1 data