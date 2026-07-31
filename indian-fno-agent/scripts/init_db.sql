-- ============================================================
-- Indian F&O Trading Agent — Database Schema
-- ============================================================
-- Run this once to initialize the database schema.
-- TimescaleDB extension optional (for OHLCV hypertable).
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text search

-- ──────────────────────────────────────────
-- Instruments Master
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS instruments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol          VARCHAR(50) NOT NULL,
    exchange        VARCHAR(10) NOT NULL,
    instrument_type VARCHAR(10) NOT NULL,
    lot_size        INTEGER NOT NULL DEFAULT 1,
    tick_size       DECIMAL(10,4) NOT NULL DEFAULT 0.05,
    expiry          DATE,
    strike          DECIMAL(12,2),
    option_type     VARCHAR(5),   -- CE or PE
    underlying      VARCHAR(50),
    broker_token    VARCHAR(50),  -- Broker-specific instrument token
    isin            VARCHAR(20),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol, exchange, expiry, strike, option_type)
);
CREATE INDEX idx_instruments_symbol ON instruments(symbol);
CREATE INDEX idx_instruments_underlying ON instruments(underlying);
CREATE INDEX idx_instruments_expiry ON instruments(expiry);

-- ──────────────────────────────────────────
-- Trade Signals
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_name       VARCHAR(100) NOT NULL,
    symbol              VARCHAR(50) NOT NULL,
    exchange            VARCHAR(10) NOT NULL,
    instrument_type     VARCHAR(10) NOT NULL,
    direction           VARCHAR(10) NOT NULL,  -- BUY or SELL
    entry_price         DECIMAL(12,2) NOT NULL,
    stop_loss           DECIMAL(12,2) NOT NULL,
    target              DECIMAL(12,2) NOT NULL,
    quantity            INTEGER NOT NULL,
    lot_size            INTEGER NOT NULL DEFAULT 1,
    confidence_score    DECIMAL(4,3) NOT NULL,  -- 0.000 to 1.000
    regime              VARCHAR(30),
    rationale           JSONB NOT NULL DEFAULT '[]',   -- list of strings
    news_summary        TEXT,
    indicators_snapshot JSONB NOT NULL DEFAULT '{}',
    market_snapshot_id  UUID,  -- FK to market snapshots
    status              VARCHAR(30) NOT NULL DEFAULT 'PENDING_APPROVAL',
    expires_at          TIMESTAMPTZ NOT NULL,
    telegram_message_id BIGINT,
    risk_reward         DECIMAL(6,3),
    -- Agent scores
    regime_score        DECIMAL(4,3),
    technical_score     DECIMAL(4,3),
    options_score       DECIMAL(4,3),
    news_score          DECIMAL(4,3),
    ensemble_score      DECIMAL(4,3)
);
CREATE INDEX idx_signals_status ON signals(status);
CREATE INDEX idx_signals_symbol ON signals(symbol);
CREATE INDEX idx_signals_strategy ON signals(strategy_name);
CREATE INDEX idx_signals_created_at ON signals(created_at DESC);

-- ──────────────────────────────────────────
-- Telegram Approval Actions
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS telegram_actions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id           UUID NOT NULL REFERENCES signals(id),
    chat_id             BIGINT NOT NULL,
    telegram_user_id    BIGINT,
    message_id          BIGINT,
    action              VARCHAR(30) NOT NULL,  -- APPROVED, REJECTED, HALF_SIZE, BLOCKED
    action_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_quantity   INTEGER,
    note                TEXT,
    UNIQUE(signal_id, action)  -- One action per signal
);
CREATE INDEX idx_telegram_actions_signal ON telegram_actions(signal_id);

-- ──────────────────────────────────────────
-- Orders
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id           UUID REFERENCES signals(id),
    broker              VARCHAR(30) NOT NULL,
    broker_order_id     VARCHAR(100),
    symbol              VARCHAR(50) NOT NULL,
    exchange            VARCHAR(10) NOT NULL,
    order_type          VARCHAR(20) NOT NULL,
    product_type        VARCHAR(20) NOT NULL,
    direction           VARCHAR(10) NOT NULL,
    quantity            INTEGER NOT NULL,
    price               DECIMAL(12,2),
    trigger_price       DECIMAL(12,2),
    fill_price          DECIMAL(12,2),
    fill_quantity       INTEGER DEFAULT 0,
    status              VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    rejection_reason    TEXT,
    placed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    broker_response     JSONB NOT NULL DEFAULT '{}',
    idempotency_key     VARCHAR(200) UNIQUE,  -- Prevents duplicate orders
    is_paper            BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_orders_signal ON orders(signal_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_broker_order_id ON orders(broker_order_id);
CREATE INDEX idx_orders_placed_at ON orders(placed_at DESC);

-- ──────────────────────────────────────────
-- Positions (Open)
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id            UUID REFERENCES orders(id),
    signal_id           UUID REFERENCES signals(id),
    symbol              VARCHAR(50) NOT NULL,
    exchange            VARCHAR(10) NOT NULL,
    direction           VARCHAR(10) NOT NULL,
    quantity            INTEGER NOT NULL,
    entry_price         DECIMAL(12,2) NOT NULL,
    current_price       DECIMAL(12,2),
    unrealized_pnl      DECIMAL(12,2) DEFAULT 0,
    stop_loss           DECIMAL(12,2) NOT NULL,
    target              DECIMAL(12,2) NOT NULL,
    initial_stop_loss   DECIMAL(12,2) NOT NULL,
    trailing_sl         DECIMAL(12,2),
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    exit_price          DECIMAL(12,2),
    exit_reason         VARCHAR(50),  -- SL_HIT, TARGET_HIT, MANUAL, EXPIRY
    is_open             BOOLEAN NOT NULL DEFAULT TRUE,
    is_paper            BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_positions_symbol ON positions(symbol);
CREATE INDEX idx_positions_is_open ON positions(is_open);

-- ──────────────────────────────────────────
-- Closed Trades
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    position_id         UUID REFERENCES positions(id),
    signal_id           UUID REFERENCES signals(id),
    symbol              VARCHAR(50) NOT NULL,
    strategy            VARCHAR(100),
    regime              VARCHAR(30),
    direction           VARCHAR(10) NOT NULL,
    quantity            INTEGER NOT NULL,
    lot_size            INTEGER NOT NULL DEFAULT 1,
    entry_price         DECIMAL(12,2) NOT NULL,
    exit_price          DECIMAL(12,2) NOT NULL,
    gross_pnl           DECIMAL(12,2) NOT NULL,
    brokerage           DECIMAL(10,2) DEFAULT 0,
    stt                 DECIMAL(10,2) DEFAULT 0,
    exchange_charges    DECIMAL(10,2) DEFAULT 0,
    gst                 DECIMAL(10,2) DEFAULT 0,
    stamp_duty          DECIMAL(10,2) DEFAULT 0,
    sebi_charges        DECIMAL(10,2) DEFAULT 0,
    total_charges       DECIMAL(10,2) DEFAULT 0,
    net_pnl             DECIMAL(12,2) NOT NULL,
    win                 BOOLEAN NOT NULL,
    exit_reason         VARCHAR(50),
    opened_at           TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ NOT NULL,
    duration_mins       INTEGER,
    is_paper            BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_strategy ON trades(strategy);
CREATE INDEX idx_trades_closed_at ON trades(closed_at DESC);
CREATE INDEX idx_trades_win ON trades(win);

-- ──────────────────────────────────────────
-- Audit Log
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      VARCHAR(50) NOT NULL,
    entity_type     VARCHAR(50),
    entity_id       VARCHAR(100),
    payload         JSONB NOT NULL DEFAULT '{}',
    actor           VARCHAR(100) DEFAULT 'system',
    severity        VARCHAR(20) DEFAULT 'INFO'
);
CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_event_type ON audit_log(event_type);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp DESC);

-- ──────────────────────────────────────────
-- Risk State (daily rolling)
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS risk_state (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    date                    DATE NOT NULL UNIQUE,
    daily_pnl               DECIMAL(12,2) DEFAULT 0,
    daily_trades            INTEGER DEFAULT 0,
    daily_wins              INTEGER DEFAULT 0,
    daily_losses            INTEGER DEFAULT 0,
    consecutive_losses      INTEGER DEFAULT 0,
    max_drawdown_today      DECIMAL(12,2) DEFAULT 0,
    peak_capital_today      DECIMAL(12,2) DEFAULT 0,
    kill_switch_active      BOOLEAN DEFAULT FALSE,
    kill_switch_reason      TEXT,
    kill_switch_activated_at TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ──────────────────────────────────────────
-- News Events
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_events (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source              VARCHAR(100),
    headline            TEXT NOT NULL,
    url                 TEXT,
    published_at        TIMESTAMPTZ,
    summary             TEXT,
    sentiment           VARCHAR(20),
    sentiment_score     DECIMAL(4,3),
    severity            VARCHAR(20),
    symbols_affected    JSONB DEFAULT '[]',
    event_type          VARCHAR(50),    -- EARNINGS, RBI, MACRO, GEOPOLITICAL, SECTOR
    is_blocked_window   BOOLEAN DEFAULT FALSE,
    headline_hash       VARCHAR(64) UNIQUE  -- MD5 for deduplication
);
CREATE INDEX idx_news_ingested_at ON news_events(ingested_at DESC);
CREATE INDEX idx_news_sentiment ON news_events(sentiment);
CREATE INDEX idx_news_severity ON news_events(severity);

-- ──────────────────────────────────────────
-- Market Data (OHLCV)
-- Use TimescaleDB hypertable if available
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_data (
    time            TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(50) NOT NULL,
    timeframe       VARCHAR(10) NOT NULL,
    open            DECIMAL(12,2) NOT NULL,
    high            DECIMAL(12,2) NOT NULL,
    low             DECIMAL(12,2) NOT NULL,
    close           DECIMAL(12,2) NOT NULL,
    volume          BIGINT DEFAULT 0,
    oi              BIGINT DEFAULT 0,
    PRIMARY KEY (time, symbol, timeframe)
);
CREATE INDEX idx_market_data_symbol ON market_data(symbol, timeframe, time DESC);

-- TimescaleDB hypertable (uncomment if TimescaleDB is installed)
-- SELECT create_hypertable('market_data', 'time', if_not_exists => TRUE);

-- ──────────────────────────────────────────
-- Backtest Runs
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_name   VARCHAR(100) NOT NULL,
    from_date       DATE NOT NULL,
    to_date         DATE NOT NULL,
    initial_capital DECIMAL(12,2) NOT NULL DEFAULT 500000,
    config          JSONB NOT NULL DEFAULT '{}',
    metrics         JSONB NOT NULL DEFAULT '{}',
    equity_curve    JSONB NOT NULL DEFAULT '[]',
    total_trades    INTEGER DEFAULT 0,
    duration_secs   DECIMAL(10,2),
    status          VARCHAR(20) DEFAULT 'COMPLETED'
);
CREATE INDEX idx_backtest_strategy ON backtest_runs(strategy_name);
CREATE INDEX idx_backtest_run_at ON backtest_runs(run_at DESC);

-- ──────────────────────────────────────────
-- Market Snapshots
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_snapshots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id       UUID REFERENCES signals(id),
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol          VARCHAR(50) NOT NULL,
    snapshot_data   JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_snapshots_signal ON market_snapshots(signal_id);

-- ──────────────────────────────────────────
-- System Configuration (runtime overrides)
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_config (
    key             VARCHAR(100) PRIMARY KEY,
    value           TEXT NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      VARCHAR(100) DEFAULT 'system'
);

-- Default system config
INSERT INTO system_config (key, value, description) VALUES
    ('trading_mode', 'PAPER', 'Current trading mode: PAPER | LIVE | SHADOW'),
    ('active_broker', 'angel_one', 'Active broker adapter'),
    ('autonomous_mode', 'false', 'Skip Telegram approval if true (DANGEROUS)'),
    ('signal_scan_enabled', 'true', 'Enable/disable signal scanning')
ON CONFLICT (key) DO NOTHING;

-- ──────────────────────────────────────────
-- Triggers: auto-update updated_at
-- ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_instruments_updated_at
    BEFORE UPDATE ON instruments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER set_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER set_risk_state_updated_at
    BEFORE UPDATE ON risk_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
