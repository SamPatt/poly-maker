-- Poly-Maker Database Schema for PostgreSQL
-- Run this to set up tables: psql -U polymaker -d polymaker -f db/schema.sql

-- User-configured: which markets to trade
CREATE TABLE IF NOT EXISTS selected_markets (
    id SERIAL PRIMARY KEY,
    question TEXT UNIQUE NOT NULL,
    param_type TEXT DEFAULT 'default',
    enabled BOOLEAN DEFAULT true,
    event_date DATE,
    exit_before_event BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Trading parameters by market type
CREATE TABLE IF NOT EXISTS hyperparameters (
    id SERIAL PRIMARY KEY,
    param_type TEXT NOT NULL,
    param_name TEXT NOT NULL,
    param_value FLOAT NOT NULL,
    UNIQUE(param_type, param_name)
);

-- All available markets (written by data updater)
CREATE TABLE IF NOT EXISTS all_markets (
    id SERIAL PRIMARY KEY,
    question TEXT UNIQUE NOT NULL,
    answer1 TEXT,
    answer2 TEXT,
    token1 TEXT,
    token2 TEXT,
    condition_id TEXT,
    market_slug TEXT,
    event_slug TEXT,
    neg_risk BOOLEAN DEFAULT false,
    spread FLOAT,
    best_bid FLOAT,
    best_ask FLOAT,
    rewards_daily_rate FLOAT,
    gm_reward_per_100 FLOAT,
    sm_reward_per_100 FLOAT,
    bid_reward_per_100 FLOAT,
    ask_reward_per_100 FLOAT,
    volatility_sum FLOAT,
    volatility_reward_ratio TEXT,
    min_size FLOAT,
    hour_1 FLOAT,
    hour_3 FLOAT,
    hour_6 FLOAT,
    hour_12 FLOAT,
    hour_24 FLOAT,
    day_7 FLOAT,
    day_14 FLOAT,
    day_30 FLOAT,
    volatility_price FLOAT,
    max_spread FLOAT,
    tick_size FLOAT,
    end_date DATE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Volatility markets (filtered subset of all_markets)
CREATE TABLE IF NOT EXISTS volatility_markets (
    id SERIAL PRIMARY KEY,
    question TEXT UNIQUE NOT NULL,
    answer1 TEXT,
    answer2 TEXT,
    token1 TEXT,
    token2 TEXT,
    condition_id TEXT,
    market_slug TEXT,
    event_slug TEXT,
    neg_risk BOOLEAN DEFAULT false,
    spread FLOAT,
    best_bid FLOAT,
    best_ask FLOAT,
    rewards_daily_rate FLOAT,
    gm_reward_per_100 FLOAT,
    volatility_sum FLOAT,
    min_size FLOAT,
    max_spread FLOAT,
    tick_size FLOAT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Account statistics
CREATE TABLE IF NOT EXISTS account_stats (
    id SERIAL PRIMARY KEY,
    question TEXT,
    answer TEXT,
    order_size FLOAT DEFAULT 0,
    position_size FLOAT DEFAULT 0,
    market_in_selected BOOLEAN DEFAULT false,
    earnings FLOAT DEFAULT 0,
    earning_percentage FLOAT DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Trade history for P&L tracking and Telegram alerts
CREATE TABLE IF NOT EXISTS trade_history (
    id SERIAL PRIMARY KEY,
    token TEXT NOT NULL,
    market_question TEXT,
    side TEXT NOT NULL,
    price FLOAT NOT NULL,
    size FLOAT NOT NULL,
    pnl FLOAT,
    source TEXT DEFAULT 'bot',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Rebates bot tracked markets (persisted across restarts)
CREATE TABLE IF NOT EXISTS rebates_markets (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    question TEXT NOT NULL,
    condition_id TEXT,
    up_token TEXT,
    down_token TEXT,
    event_start TIMESTAMP WITH TIME ZONE NOT NULL,
    order_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    status TEXT DEFAULT 'UPCOMING',  -- UPCOMING, LIVE, RESOLVED, REDEEMED
    up_filled BOOLEAN DEFAULT false,
    down_filled BOOLEAN DEFAULT false,
    up_price FLOAT,
    down_price FLOAT,
    neg_risk BOOLEAN DEFAULT false,
    tick_size FLOAT DEFAULT 0.01,
    redeemed BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_all_markets_question ON all_markets(question);
CREATE INDEX IF NOT EXISTS idx_all_markets_condition_id ON all_markets(condition_id);
CREATE INDEX IF NOT EXISTS idx_selected_markets_enabled ON selected_markets(enabled);
CREATE INDEX IF NOT EXISTS idx_trade_history_created ON trade_history(created_at);
CREATE INDEX IF NOT EXISTS idx_hyperparameters_type ON hyperparameters(param_type);
CREATE INDEX IF NOT EXISTS idx_rebates_markets_status ON rebates_markets(status);
CREATE INDEX IF NOT EXISTS idx_rebates_markets_slug ON rebates_markets(slug);

-- Insert default hyperparameters (adjust values as needed)
INSERT INTO hyperparameters (param_type, param_name, param_value) VALUES
    ('default', 'stop_loss_threshold', -5.0),
    ('default', 'take_profit_threshold', 3.0),
    ('default', 'volatility_threshold', 50.0),
    ('default', 'spread_threshold', 0.03),
    ('default', 'sleep_period', 6.0)
ON CONFLICT (param_type, param_name) DO NOTHING;

-- ============================================
-- Active Quoting Bot Tables (Phase 6)
-- ============================================

-- Active quoting positions - current positions per market
-- Used to restore state on restart
CREATE TABLE IF NOT EXISTS active_quoting_positions (
    id SERIAL PRIMARY KEY,
    token_id TEXT UNIQUE NOT NULL,
    market_name TEXT,
    size FLOAT NOT NULL DEFAULT 0,
    avg_price FLOAT NOT NULL DEFAULT 0,
    realized_pnl FLOAT DEFAULT 0,
    total_fees FLOAT DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Active quoting fills - fill history for analytics
CREATE TABLE IF NOT EXISTS active_quoting_fills (
    id SERIAL PRIMARY KEY,
    fill_id TEXT UNIQUE NOT NULL,
    token_id TEXT NOT NULL,
    market_name TEXT,
    side TEXT NOT NULL,  -- 'BUY' or 'SELL'
    price FLOAT NOT NULL,
    size FLOAT NOT NULL,
    notional FLOAT NOT NULL,  -- price * size
    fee FLOAT DEFAULT 0,
    mid_at_fill FLOAT,  -- Mid price at time of fill
    order_id TEXT,
    trade_id TEXT,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Active quoting markouts - markout samples for toxicity analysis
CREATE TABLE IF NOT EXISTS active_quoting_markouts (
    id SERIAL PRIMARY KEY,
    fill_id TEXT NOT NULL REFERENCES active_quoting_fills(fill_id) ON DELETE CASCADE,
    horizon_seconds INTEGER NOT NULL,
    mid_at_fill FLOAT NOT NULL,
    mid_at_horizon FLOAT,
    markout FLOAT,  -- Price change in favor/against
    markout_bps FLOAT,  -- Markout in basis points
    captured_at TIMESTAMP WITH TIME ZONE,
    UNIQUE(fill_id, horizon_seconds)
);

-- Active quoting sessions - session metadata for tracking bot runs
CREATE TABLE IF NOT EXISTS active_quoting_sessions (
    id SERIAL PRIMARY KEY,
    session_id TEXT UNIQUE NOT NULL,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    end_time TIMESTAMP WITH TIME ZONE,
    markets TEXT[],  -- Array of token IDs
    config_snapshot JSONB,  -- Snapshot of config at session start
    status TEXT DEFAULT 'RUNNING',  -- RUNNING, STOPPED, CRASHED
    total_fills INTEGER DEFAULT 0,
    total_volume FLOAT DEFAULT 0,
    total_notional FLOAT DEFAULT 0,
    net_fees FLOAT DEFAULT 0,
    realized_pnl FLOAT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for active quoting tables
CREATE INDEX IF NOT EXISTS idx_aq_positions_token ON active_quoting_positions(token_id);
CREATE INDEX IF NOT EXISTS idx_aq_fills_token ON active_quoting_fills(token_id);
CREATE INDEX IF NOT EXISTS idx_aq_fills_timestamp ON active_quoting_fills(timestamp);
CREATE INDEX IF NOT EXISTS idx_aq_markouts_fill ON active_quoting_markouts(fill_id);
CREATE INDEX IF NOT EXISTS idx_aq_markouts_captured ON active_quoting_markouts(captured_at);
CREATE INDEX IF NOT EXISTS idx_aq_sessions_status ON active_quoting_sessions(status);
CREATE INDEX IF NOT EXISTS idx_aq_sessions_start ON active_quoting_sessions(start_time);
