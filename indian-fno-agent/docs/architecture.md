# Architecture Deep Dive

## System Overview

The Indian F&O Trading Agent is built on a **modular service-oriented architecture** with clear separation between:
- **Data Acquisition** (market data, news)
- **Analysis** (AI agents, indicators)
- **Decision** (signal generation, risk checks)
- **Approval** (Telegram human-in-the-loop)
- **Execution** (broker adapter, order management)

---

## Data Flow Pipeline

```
Market Data ──▶ Indicators ──▶ AI Agents ──▶ Signal Generation
                                                    │
                                              Risk Engine
                                                    │
                                           ┌────────▼─────────┐
                                           │  Telegram Notify  │
                                           │  (Approve/Reject) │
                                           └────────┬─────────┘
                                                    │ (Human Decision)
                                              Order Execution
                                                    │
                                           Position Tracking
                                                    │
                                              Audit Logging
```

## Component Details

### 1. Core Layer (`core/`)
- **Enums**: All system-wide enumerations (Exchange, OrderType, MarketRegime, etc.)
- **Models**: Pydantic v2 data models — the "contract" between all modules
- **Events**: Async event bus for decoupled communication
- **Exceptions**: Structured error hierarchy

### 2. Market Data Layer (`market_data/`)
- **HistoricalDataService**: Fetches OHLCV via broker, caches in Redis, persists to TimescaleDB
- **LiveMarketFeed**: Polling-based price feed with EventBus integration
- **OptionsChainService**: Option chain analysis — PCR, max pain, IV skew, unusual OI

### 3. Analysis Layer
- **Technical Indicators** (`indicators/technical.py`): EMA, RSI, MACD, VWAP, Bollinger, Supertrend, ADX
- **Pattern Detection** (`indicators/patterns.py`): Candlestick patterns, S/R levels, breakouts, swing structure
- **Options Analysis** (`indicators/options.py`): Greeks, IV surface, OI analysis

### 4. AI Agent Layer (`agents/`)
- **TechnicalAgent**: Analyses multi-timeframe technicals via Gemini
- **OptionsAgent**: Analyses options flow and suggests F&O trades
- **NewsAgent**: Sentiment analysis on Indian financial news
- **RegimeAgent**: Classifies current market regime
- **RiskArbiter**: Final risk assessment before human review
- **Orchestrator**: Coordinates all agents into a unified signal pipeline

### 5. Strategy Layer (`strategies/`)
Four production strategies with configurable parameters:
- Trend Breakout, VWAP-RSI Reversal, Options Momentum, Short Premium

### 6. Risk Layer (`risk/`)
- **RiskEngine**: Pre-trade validation (position sizing, margin, exposure)
- **KillSwitch**: Emergency stop mechanism (manual or automatic)
- **PositionSizer**: Kelly criterion and fixed-risk sizing

### 7. Telegram Layer (`telegram_bot/`)
- Sends formatted trade proposals with inline buttons
- Handles approval/rejection with configurable timeout
- Admin-only commands: /status, /positions, /killswitch, /resume

### 8. Execution Layer (`execution/`)
- **ExecutionEngine**: Translates approved signals into broker orders
- **OrderManager**: Tracks order lifecycle with idempotency
- **PositionTracker**: Monitors SL/target with trailing stop logic

### 9. Broker Adapter Layer (`broker/`)
- **IBrokerAdapter**: Abstract interface — strategies never touch broker SDKs
- **PaperAdapter**: Simulated execution for testing
- **AngelOneAdapter**: Production Angel One SmartAPI integration
- **BrokerFactory**: Runtime adapter selection from config

---

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| Web Framework | FastAPI |
| Database | PostgreSQL + TimescaleDB |
| Cache | Redis |
| AI/LLM | Google Gemini 1.5 Flash |
| Telegram | python-telegram-bot v21 |
| Broker | Angel One SmartAPI |
| Dashboard | React + Vite + Tailwind |
| Scheduling | APScheduler + Celery |
| Containerization | Docker Compose |

---

## Concurrency Model

- All I/O operations are **async** (asyncio)
- EventBus decouples publishers from subscribers
- Redis used for inter-process state (signal status, LTP cache)
- Celery workers handle background tasks (news ingestion, data storage)

---

## Security Considerations

- All credentials via environment variables (never hardcoded)
- Telegram chat ID verification on every command/callback
- TOTP-based broker authentication
- Kill switch can be activated from Telegram or API
- Rate limiting on all external API calls
