-- Poly-Maker Database Schema for Supabase
-- Run this in Supabase SQL Editor to set up tables

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

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_all_markets_question ON all_markets(question);
CREATE INDEX IF NOT EXISTS idx_all_markets_condition_id ON all_markets(condition_id);
CREATE INDEX IF NOT EXISTS idx_selected_markets_enabled ON selected_markets(enabled);
CREATE INDEX IF NOT EXISTS idx_trade_history_created ON trade_history(created_at);
CREATE INDEX IF NOT EXISTS idx_hyperparameters_type ON hyperparameters(param_type);

-- Insert default hyperparameters (adjust values as needed)
INSERT INTO hyperparameters (param_type, param_name, param_value) VALUES
    ('default', 'stop_loss_threshold', -5.0),
    ('default', 'take_profit_threshold', 3.0),
    ('default', 'volatility_threshold', 50.0),
    ('default', 'spread_threshold', 0.03),
    ('default', 'sleep_period', 6.0)
ON CONFLICT (param_type, param_name) DO NOTHING;
