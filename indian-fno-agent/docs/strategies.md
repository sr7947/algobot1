# Strategy Reference

The agent includes 4 production-ready strategies. Each strategy extends `IStrategy` and can be enabled/disabled independently.

---

## 1. Trend Breakout (`strategies/trend_breakout.py`)

**Type**: Momentum / Breakout  
**Instruments**: Index & Stock Futures  
**Timeframe**: 15-minute primary, 1-hour confirmation

### Logic
- Detects breakouts from support/resistance levels
- Confirms with volume surge (> 1.5x 20-period average)
- Uses EMA stack alignment (9 > 21 > 50 for longs)
- ADX > 25 for trend strength confirmation

### Entry Rules
| Direction | Conditions |
|---|---|
| **BUY** | Price breaks above resistance + Volume spike + EMA aligned bullish + ADX > 25 |
| **SELL** | Price breaks below support + Volume spike + EMA aligned bearish + ADX > 25 |

### Exit Rules
- **Stop Loss**: Below/above the broken S/R level ± ATR buffer
- **Target**: 2x risk (1:2 R:R minimum)
- **Trailing SL**: Moves to breakeven at 1.5x risk, then trails at 0.5x risk behind price

### Best Conditions
- Works best in trending markets (TRENDING_BULL / TRENDING_BEAR regimes)
- Avoid during RANGE_BOUND markets

---

## 2. VWAP-RSI Reversal (`strategies/vwap_rsi_reversal.py`)

**Type**: Mean Reversion  
**Instruments**: Index & Stock Futures  
**Timeframe**: 5-minute primary, 15-minute confirmation

### Logic
- Identifies price deviation from VWAP
- Combines with RSI oversold/overbought zones
- Waits for reversal candlestick pattern at extremes
- Uses Bollinger Band touch as additional confirmation

### Entry Rules
| Direction | Conditions |
|---|---|
| **BUY** | Price < VWAP - 1σ + RSI < 30 + Bullish reversal candle + Near lower BB |
| **SELL** | Price > VWAP + 1σ + RSI > 70 + Bearish reversal candle + Near upper BB |

### Exit Rules
- **Stop Loss**: Beyond the recent swing low/high
- **Target**: VWAP as first target, opposite BB as second
- **Time exit**: Close if still open after 2 hours

### Best Conditions
- Works best in RANGE_BOUND markets
- Avoid during strong trends and news events

---

## 3. Options Momentum (`strategies/options_momentum.py`)

**Type**: Options Directional  
**Instruments**: Index CE/PE options (NIFTY, BANKNIFTY)  
**Timeframe**: 15-minute

### Logic
- Analyses PCR shifts and OI buildup
- Identifies momentum direction from underlying's technical setup
- Selects optimal strike based on IV, delta proximity, and liquidity
- Prefers ATM/slightly OTM options for best risk:reward

### Entry Rules
| Direction | Conditions |
|---|---|
| **BUY CE** | Underlying bullish + PCR < 0.8 (call demand) + Rising CE OI at ATM + Underlying above VWAP |
| **BUY PE** | Underlying bearish + PCR > 1.2 (put demand) + Rising PE OI at ATM + Underlying below VWAP |

### Strike Selection
1. Get ATM strike (nearest to spot)
2. Prefer 1-2 strikes OTM for leverage
3. Check minimum liquidity (OI > 1000 contracts)
4. Verify IV is not extremely elevated (> 2σ)

### Exit Rules
- **Stop Loss**: 30% of premium paid
- **Target**: 50% profit on premium
- **Time decay exit**: Close if < 2 days to expiry

---

## 4. Short Premium (`strategies/short_premium.py`)

**Type**: Options Selling / Theta Decay  
**Instruments**: Index options (NIFTY, BANKNIFTY)  
**Timeframe**: Daily / Swing

### Logic
- Sells far OTM options with high theta
- Creates defined-risk spreads (Iron Condors, Strangles)
- Targets time decay in range-bound markets
- Adjusts positions based on delta and gamma exposure

### Entry Rules
| Setup | Conditions |
|---|---|
| **Short Strangle** | Low IV rank (< 40) + Range-bound regime + Sell options at ±2σ from spot |
| **Iron Condor** | High IV rank (> 60) + Range-bound + Sell 1σ + Buy 2σ for protection |

### Exit Rules
- **Profit target**: 50% of max profit (premium collected)
- **Stop Loss**: 2x premium collected
- **Adjustment**: Roll untested side if breached

### Risk Management
- Max 3 concurrent short premium positions
- Never sell naked options near events (RBI, Budget, etc.)
- Events calendar automatically blocks trades near major events

---

## Configuration

All strategy parameters are configurable via `config/strategies.yaml`:

```yaml
trend_breakout:
  enabled: true
  timeframe: "15m"
  min_confidence: 0.65
  max_risk_per_trade_pct: 1.0
  volume_multiplier: 1.5
  adx_threshold: 25
```

## Performance Tracking

Each strategy's performance is tracked independently:
- Win rate, profit factor, Sharpe ratio
- Performance by market regime
- Monthly P&L breakdown

View via dashboard or `/pnl` Telegram command.
