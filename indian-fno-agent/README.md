# 🤖 Indian F&O Trading Agent

**AI-powered, semi-automated algo trading system for Indian Futures & Options with a human approval workflow over Telegram.**

> Every trade proposal is sent to Telegram for human approval before execution. This is **not** a fully autonomous bot — you always have the final say.

---

## ✨ Features

| Category | Feature |
|---|---|
| **AI Analysis** | Multi-agent Gemini-powered analysis of technicals, options flow, market regime, and news sentiment |
| **Human-in-the-Loop** | Telegram approval with ✅ Approve, ❌ Reject, 📉 Half Size, 🚫 Block buttons |
| **Risk Management** | Position sizing, daily loss limits, consecutive loss tracking, kill switch |
| **Broker Abstraction** | Angel One SmartAPI (live), Paper trading (built-in), Dhan & Groww (stubs) |
| **4 Strategies** | Trend Breakout, VWAP-RSI Reversal, Options Momentum, Short Premium |
| **Backtesting** | Walk-forward engine with realistic NSE charges |
| **Dashboard** | React + Tailwind real-time dashboard with WebSocket updates |
| **Full Audit Trail** | Every decision logged to PostgreSQL + JSONL files |

---

## 📐 Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Market Data │────▶│  AI Agents   │────▶│  Risk Engine │
│  (Historical │     │  (Technical, │     │  (Position   │
│   + Live)    │     │   Options,   │     │   sizing,    │
└──────────────┘     │   News,      │     │   kill switch│
                     │   Regime)    │     └──────┬───────┘
                     └──────┬───────┘            │
                            │                    │
                     ┌──────▼───────┐     ┌──────▼───────┐
                     │ Orchestrator │────▶│   Telegram   │
                     │  (Signal     │     │   Approval   │
                     │   Pipeline)  │     └──────┬───────┘
                     └──────────────┘            │
                                          ┌──────▼───────┐
                                          │  Execution   │
                                          │   Engine     │
                                          │  (Broker API)│
                                          └──────────────┘
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 15+ (with TimescaleDB extension recommended)
- Redis 7+
- Docker & Docker Compose (optional)

### 1. Clone & Install

```bash
git clone <repo-url> && cd indian-fno-agent
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials (see docs/telegram_setup.md and docs/gemini_setup.md)
```

### 3. Start Infrastructure

```bash
docker-compose up -d postgres redis
python -c "import asyncpg; print('DB ready')"
psql -f scripts/init_db.sql
```

### 4. Run in Paper Mode

```bash
# Start API server
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# In another terminal, start the dashboard
cd dashboard && npm install && npm run dev
```

### 5. Telegram Bot Setup

Follow the guide in [docs/telegram_setup.md](docs/telegram_setup.md).

---

## 📁 Project Structure

```
indian-fno-agent/
├── agents/              # Gemini-powered analysis agents
│   ├── orchestrator.py  # Main trading pipeline
│   ├── technical_agent.py
│   ├── options_agent.py
│   ├── news_agent.py
│   ├── regime_agent.py
│   └── risk_arbiter.py
├── api/                 # FastAPI REST API + WebSocket
├── audit/               # Audit logging & market snapshots
├── backtesting/         # Walk-forward backtesting engine
├── broker/              # Broker adapters (Angel One, Paper, etc.)
├── config/              # Settings, instruments, strategies, risk rules
├── core/                # Enums, models, exceptions, event bus
├── dashboard/           # React + Tailwind real-time UI
├── execution/           # Order management & position tracking
├── indicators/          # Technical indicators & pattern detection
├── market_data/         # Historical data, live feed, options chain
├── news/                # News ingestion, sentiment, events calendar
├── risk/                # Risk engine, kill switch, position sizing
├── scheduler/           # APScheduler job definitions
├── scripts/             # Database init, utilities
├── strategies/          # Trading strategy implementations
├── telegram_bot/        # Telegram bot, handlers, templates
└── tests/               # Unit & integration tests
```

---

## 🔐 Trading Mode

| Mode | Description |
|---|---|
| `PAPER` | Default. Simulates trades with virtual capital. No real orders. |
| `SHADOW` | Analyses market but doesn't even paper-trade. Observation only. |
| `LIVE` | ⚠️ Real money. Requires `.env` change + service restart. |

---

## 📖 Documentation

- [Architecture Deep Dive](docs/architecture.md)
- [Telegram Bot Setup](docs/telegram_setup.md)
- [Gemini API Setup](docs/gemini_setup.md)
- [Broker Adapters Guide](docs/broker_adapters.md)
- [Deployment Guide](docs/deployment.md)
- [Strategy Reference](docs/strategies.md)

---

## ⚠️ Disclaimer

This software is for **educational and research purposes only**. Trading in Futures & Options involves substantial risk of loss. Past performance does not guarantee future results. The authors are not SEBI-registered investment advisors. Always consult a qualified financial advisor before trading with real money.

---

## License

MIT
