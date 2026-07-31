# Broker Adapters Guide

The trading agent uses a **broker abstraction layer** — all strategy logic and execution flows through the `IBrokerAdapter` interface. You never need to change strategy code when switching brokers.

---

## Supported Brokers

| Broker | Status | Module | Notes |
|---|---|---|---|
| **Paper Trading** | ✅ Ready | `broker/paper.py` | Simulated execution, default mode |
| **Angel One** | ✅ Ready | `broker/angel_one.py` | SmartAPI integration, TOTP auth |
| **Dhan** | 🔧 Stub | `broker/dhan.py` | Interface ready, implementation pending |
| **Groww** | 🔧 Stub | `broker/groww.py` | Interface ready, implementation pending |

---

## Angel One SmartAPI Setup

### Step 1: Create an Angel One Account

1. Open a trading account at [Angel One](https://www.angelone.in/)
2. Complete KYC verification
3. Activate F&O trading segment

### Step 2: Get API Credentials

1. Go to [SmartAPI Portal](https://smartapi.angelone.in/)
2. Create a new app (select "Trading" as category)
3. Note your:
   - **API Key** (alphanumeric string)
   - **Client ID** (your Angel One client code, e.g., `A12345678`)

### Step 3: Enable TOTP

1. Install a TOTP app (Google Authenticator, Authy, or similar)
2. Go to Angel One app → Settings → Security → Enable TOTP
3. Scan the QR code with your TOTP app
4. Save the **TOTP Secret Key** (base32 string, usually shown during setup)

### Step 4: Configure .env

```env
BROKER=angel_one

# Angel One Credentials
ANGEL_ONE_API_KEY=your_api_key_here
ANGEL_ONE_CLIENT_ID=A12345678
ANGEL_ONE_PASSWORD=your_trading_password
ANGEL_ONE_TOTP_SECRET=your_totp_base32_secret
```

### Step 5: Verify Connection

```bash
# Start in paper mode first to test
TRADING_MODE=PAPER uvicorn api.main:app --port 8000

# Check broker status
curl http://localhost:8000/health
```

---

## Paper Trading Mode

Paper trading is the **default mode** and requires no broker credentials:

```env
TRADING_MODE=PAPER
BROKER=paper
```

Features:
- Virtual capital: ₹5,00,000 (configurable)
- Simulated order fills with configurable slippage
- In-memory order book and position tracking
- Realistic margin calculation (~18% for futures)
- All signals are processed normally, just executed virtually

---

## Switching Brokers

To switch brokers, update `.env` and restart:

```env
# Switch to Angel One
BROKER=angel_one

# Or switch to Paper
BROKER=paper
```

The `BrokerFactory` in `broker/base.py` handles instantiation:

```python
from broker.base import BrokerFactory
from config.settings import get_settings

adapter = BrokerFactory.create(
    broker_name=get_settings().BROKER,
    settings=get_settings()
)
await adapter.login()
```

---

## Adding a New Broker

To add a new broker (e.g., Zerodha Kite):

1. Create `broker/zerodha.py`
2. Implement `IBrokerAdapter` with all abstract methods
3. Add to `BrokerFactory.create()` in `broker/base.py`
4. Add credentials to `config/settings.py` and `.env.example`

Required methods to implement:

| Method | Purpose |
|---|---|
| `login()` | Authenticate with broker |
| `logout()` | Clean disconnect |
| `is_connected()` | Connection health check |
| `get_instruments()` | Fetch instrument master |
| `get_ltp()` | Live price quotes |
| `get_ohlc()` | Historical OHLCV data |
| `get_option_chain()` | Option chain data |
| `place_order()` | Execute trade |
| `modify_order()` | Modify open order |
| `cancel_order()` | Cancel open order |
| `get_positions()` | Current positions |
| `get_orders()` | Order book |
| `get_margins()` | Available margins |
