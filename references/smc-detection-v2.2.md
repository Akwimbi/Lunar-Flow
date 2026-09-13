# SMC Detection Logic v2.2 (2026-05-02)

Details of Order Block, MSS (CHoCH/BOS), and Liquidity Sweep detection as implemented in `lunar_flow.py`.

## Order Block Detection (OrderBlockAgent)

### Logic (`find_order_block` method)
1. Scan last 5 candles for **impulsive moves** (body > 1.5× ATR)
2. For **bullish OB**: Find last bearish candle (close < open) before the impulsive bullish move
3. For **bearish OB**: Find last bullish candle (close > open) before the impulsive bearish move
4. Strength scored as: `min(body_size / (atr * 3), 1.0)`

### Validation (`validate_ob` method — NEW in v2.2)
Called by `EntryAgent.find_order_block()` before returning any OB:

```python
def validate_ob(self, ob: OrderBlock, candles: list[Candle]) -> bool:
    # 1. Check if price entered OB zone (retest)
    for c in post_ob_candles:
        if price_enters_ob_zone(c, ob):
            entered_ob = True
            break
    
    # 2. Check for bounce (proof of unfilled orders)
    for c in post_ob_candles:
        if ob.direction == BUY and c.close > ob.midpoint:
            return True  # Bullish bounce
        if ob.direction == SELL and c.close < ob.midpoint:
            return True  # Bearish bounce
    
    return False  # No bounce = invalid OB
```

**Entry only fires if OB passes BOTH retest AND bounce checks.**

---

## Market Structure Shift (MSSAgent)

### Swing Point Detection
- Uses **5-bar lookback** (not 2-bar as commonly documented)
- **Swing High**: `candle[i].high > candles[i-1..-2].high AND candles[i+1..+2].high`
- **Swing Low**: Mirror logic (low < neighbors)

### BOS vs CHoCH Detection (`detect_mss` method — CHoCH NEW in v2.2)

**BOS (Break of Structure)** — Trend continuation:
```python
if current.close > last_swing_high:
    # Check if CHoCH (15+ bars of opposite trend before break)
    is_choch = check_15_bars_opposite_trend(candles, last_high[0], "bearish")
    structure = "CHoCH" if is_choch else "BOS"
```

**CHoCH (Change of Character)** — Trend reversal:
- Detected when BOS occurs after **15+ consecutive bars** of opposite trend
- Example: Bullish CHoCH = break above swing high after 15+ bearish bars

### Confluence Scoring
- **CHoCH** = **2 points** (stronger signal, trend reversal)
- **BOS** = **1 point** (trend continuation)
- Total possible score: **9 points** (up from 8)

---

## Liquidity Sweep Detection (LiquidityAgent)

### Zone Identification (`identify_zones` method)
Scans last 20 M15/M30 candles for:
- **Clustered highs** → `"buy-side"` liquidity (shorts' stop losses)
- **Clustered lows** → `"sell-side"` liquidity (longs' stop losses)
- **Session highs/lows** from H1 (24-bar lookback)
- **Pivot points** (R1/S1)

### Sweep + Reclaim (`detect_sweep` method — UPGRADED in v2.2)

**Before v2.2**: Any sweep (wick beyond zone) counted

**v2.2 Logic**:
```python
def detect_sweep(self, zones, candles, bias, max_bars=5):
    # Check last 5 candles (configurable via max_bars)
    for zone in zones:
        for candle in check_candles:
            if bias == BULLISH and zone.type == "sell-side":
                # Sweep: wick below, BUT MUST CLOSE BACK IN ZONE (reclaim)
                if candle.low < zone.price and candle.close > zone.price:
                    reclaim_depth = (candle.close - zone.price) / PIP
                    if reclaim_depth >= 2:  # ≥2 pips back in zone
                        return True, zone
    return False, None
```

**Key requirements**:
1. Sweep candle must **wick beyond zone** (liquidity grab)
2. Must **close back inside zone** (reclaim)
3. Reclaim depth must be **≥2 pips** (filters weak reclaims)
4. Checked within last **5 bars** (configurable)

---

## Confluence Scoring (ConfluenceScorer v2.2)

| Factor | Points | Notes |
|--------|--------|-------|
| Bias (no conflict) | 1pt | From BiasAgent |
| Multi-timeframe aligned | 1pt | H4/H2/H1 agreement |
| Session active | 1pt | London (8-17) or NY (13-21) |
| News clear | 1pt | No high-impact events ±30min |
| Liquidity sweep + reclaim | 1pt | Must have ≥2 pip reclaim |
| FVG valid | 1pt | ≥8 pips, ≥2 candles |
| Order Block (validated) | 1pt | Must pass retest + bounce |
| **MSS: CHoCH** | **2pts** | Trend reversal (NEW) |
| **MSS: BOS** | **1pt** | Trend continuation |
| **Total Possible** | **9pts** | Up from 8pts |

**Execution threshold**: Configurable via `CONFLUENCE_EXECUTE` (default 3/9 = 33%)

---

## Patched Code Locations (lunar_flow.py)

| Feature | Method | Line (approx) |
|---------|--------|---------------|
| CHoCH detection | `MSSAgent.detect_mss()` | ~615 |
| OB validation | `OrderBlockAgent.validate_ob()` | ~570 |
| Sweep + Reclaim | `LiquidityAgent.detect_sweep()` | ~480 |
| Updated scoring | `ConfluenceScorer.score()` | ~786 |
| OB validation call | `EntryAgent.find_order_block()` | ~728 |
