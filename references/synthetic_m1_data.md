# Synthetic M1 Data Generation for MT5 Backtesting

## Problem
MT5 brokers may not provide historical M1 data via API (`mt5.copy_rates_from_pos()` returns None/empty). Common for recent data (2026+) or certain broker configs.

## Solution
Generate synthetic M1 candles by splitting M15 candles into 15 M1 candles. Not perfectly accurate but sufficient for scalping backtests.

## Implementation

Add to backtest class:

```python
def _load_synthetic_m1_from_m15(self, days: int) -> bool:
    """Generate synthetic M1 candles from M15 data."""
    import random
    from datetime import timedelta
    
    tf_m15 = mt5.TIMEFRAME_M15
    bars_needed_m15 = days * 96  # 96 M15 bars/day
    
    rates_m15 = mt5.copy_rates_from_pos(SYMBOL, tf_m15, 0, bars_needed_m15)
    if rates_m15 is None or len(rates_m15) == 0:
        return False
    
    self.all_candles = []
    
    for r_m15 in rates_m15:
        m15_open = float(r_m15[1])
        m15_high = float(r_m15[2])
        m15_low = float(r_m15[3])
        m15_close = float(r_m15[4])
        m15_vol = float(r_m15[5])
        m15_ts = datetime.fromtimestamp(r_m15[0], tz=timezone.utc)
        
        for i in range(15):
            if i == 0:
                m1_open = m15_open
            else:
                m1_open = m15_open + (m15_close - m15_open) * ((i - 1) / 14.0)
            
            if i == 14:
                m1_close = m15_close
            else:
                m1_close = m15_open + (m15_close - m15_open) * ((i + 1) / 14.0)
            
            m1_high = max(m1_open, m1_close)
            m1_low = min(m1_open, m1_close)
            
            spread = m15_high - m15_low
            if spread > 0:
                m1_high = min(m1_high + random.uniform(0, spread * 0.3), m15_high)
                m1_low = max(m1_low - random.uniform(0, spread * 0.3), m15_low)
            
            m1_ts = m15_ts + timedelta(minutes=i)
            m1_vol = m15_vol / 15.0
            
            self.all_candles.append(
                Candle("M1", m1_open, m1_high, m1_low, m1_close, m1_vol, m1_ts)
            )
    
    return len(self.all_candles) > 0
```

## Integration

In `load_historical_data()`:

```python
if TIMEFRAME_EXEC == "M1":
    rates = mt5.copy_rates_from_pos(SYMBOL, tf_int, 0, num_bars_needed)
    if rates is None or len(rates) == 0:
        log.warning("M1 API data not available, generating synthetic M1 from M15...")
        if self._load_synthetic_m1_from_m15(days):
            self._load_htf_data(days)
            self._precompute_indicators()
            return True
```

## Limitations
- No real intrabar volatility
- Volume divided equally across 15 bars
- Use only when real M1 data unavailable

## Alternatives
- Download M1 CSV from histdata.com or fxdata.ca
- Use broker's MT5 History Center (File → Open History Center)
