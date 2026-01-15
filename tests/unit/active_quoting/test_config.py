"""
Unit tests for ActiveQuotingConfig.
"""
import pytest
import os
from unittest.mock import patch

from rebates.active_quoting.config import ActiveQuotingConfig


class TestActiveQuotingConfigDefaults:
    """Test default configuration values."""

    def test_default_quote_pricing(self):
        """Default quote pricing parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.quote_offset_ticks == 0
        assert config.improve_when_spread_ticks == 4
        assert config.max_spread_ticks == 10

    def test_default_quote_refresh(self):
        """Default quote refresh parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.refresh_threshold_ticks == 2
        assert config.min_refresh_interval_ms == 500
        assert config.global_refresh_cap_per_sec == 10

    def test_default_momentum_detection(self):
        """Default momentum detection parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.momentum_threshold_ticks == 3
        assert config.momentum_window_ms == 500
        assert config.cooldown_seconds == 2.0
        assert config.sweep_depth_threshold == 0.5

    def test_default_inventory_management(self):
        """Default inventory parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.max_position_per_market == 100
        assert config.max_liability_per_market_usdc == 50.0
        assert config.max_total_liability_usdc == 500.0
        assert config.inventory_skew_coefficient == 0.02

    def test_default_risk_management(self):
        """Default risk parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.max_drawdown_per_market_usdc == 20.0
        assert config.max_drawdown_global_usdc == 100.0
        assert config.max_consecutive_errors == 5
        assert config.stale_feed_timeout_seconds == 30.0

    def test_default_order_management(self):
        """Default order management parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.order_size_usdc == 10.0
        assert config.batch_size == 15
        assert config.cancel_on_momentum is True
        assert config.post_only is True

    def test_default_fee_handling(self):
        """Default fee handling parameters should match research doc."""
        config = ActiveQuotingConfig()
        assert config.fee_cache_ttl_seconds == 300

    def test_default_websocket(self):
        """Default WebSocket parameters should be correct."""
        config = ActiveQuotingConfig()
        assert config.market_ws_uri == "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        assert config.user_ws_uri == "wss://ws-subscriptions-clob.polymarket.com/ws/user"
        assert config.ws_ping_interval == 5
        assert config.ws_reconnect_delay_seconds == 5.0
        assert config.ws_max_reconnect_attempts == 10

    def test_default_general(self):
        """Default general parameters should be correct."""
        config = ActiveQuotingConfig()
        assert config.dry_run is True
        assert config.assets == ["btc", "eth", "sol"]
        assert config.log_level == "INFO"


class TestActiveQuotingConfigFromEnv:
    """Test configuration from environment variables."""

    def test_from_env_quote_pricing(self):
        """Quote pricing params should be read from env."""
        env_vars = {
            "AQ_QUOTE_OFFSET_TICKS": "1",
            "AQ_IMPROVE_WHEN_SPREAD_TICKS": "6",
            "AQ_MAX_SPREAD_TICKS": "15",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = ActiveQuotingConfig.from_env()
            assert config.quote_offset_ticks == 1
            assert config.improve_when_spread_ticks == 6
            assert config.max_spread_ticks == 15

    def test_from_env_momentum(self):
        """Momentum params should be read from env."""
        env_vars = {
            "AQ_MOMENTUM_THRESHOLD_TICKS": "5",
            "AQ_MOMENTUM_WINDOW_MS": "1000",
            "AQ_COOLDOWN_SECONDS": "3.5",
            "AQ_SWEEP_DEPTH_THRESHOLD": "0.7",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = ActiveQuotingConfig.from_env()
            assert config.momentum_threshold_ticks == 5
            assert config.momentum_window_ms == 1000
            assert config.cooldown_seconds == 3.5
            assert config.sweep_depth_threshold == 0.7

    def test_from_env_inventory(self):
        """Inventory params should be read from env."""
        env_vars = {
            "AQ_MAX_POSITION_PER_MARKET": "200",
            "AQ_MAX_LIABILITY_PER_MARKET_USDC": "100.0",
            "AQ_MAX_TOTAL_LIABILITY_USDC": "1000.0",
            "AQ_INVENTORY_SKEW_COEFFICIENT": "0.2",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = ActiveQuotingConfig.from_env()
            assert config.max_position_per_market == 200
            assert config.max_liability_per_market_usdc == 100.0
            assert config.max_total_liability_usdc == 1000.0
            assert config.inventory_skew_coefficient == 0.2

    def test_from_env_order_management(self):
        """Order management params should be read from env."""
        env_vars = {
            "AQ_ORDER_SIZE_USDC": "25.0",
            "AQ_BATCH_SIZE": "10",
            "AQ_CANCEL_ON_MOMENTUM": "false",
            "AQ_POST_ONLY": "true",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = ActiveQuotingConfig.from_env()
            assert config.order_size_usdc == 25.0
            assert config.batch_size == 10
            assert config.cancel_on_momentum is False
            assert config.post_only is True

    def test_from_env_dry_run_false(self):
        """Dry run should be settable to false."""
        env_vars = {"AQ_DRY_RUN": "false", "AQ_ORDER_SIZE_USDC": "10.0"}
        with patch.dict(os.environ, env_vars, clear=False):
            config = ActiveQuotingConfig.from_env()
            assert config.dry_run is False

    def test_from_env_assets(self):
        """Assets should be parsed from comma-separated string."""
        env_vars = {"AQ_ASSETS": "btc,eth"}
        with patch.dict(os.environ, env_vars, clear=False):
            config = ActiveQuotingConfig.from_env()
            assert config.assets == ["btc", "eth"]


class TestActiveQuotingConfigValidation:
    """Test configuration validation."""

    def test_valid_config(self):
        """Valid configuration should not raise."""
        config = ActiveQuotingConfig()
        config.validate()  # Should not raise

    def test_invalid_quote_offset_negative(self):
        """Negative quote_offset_ticks should raise."""
        with pytest.raises(ValueError, match="quote_offset_ticks must be >= 0"):
            ActiveQuotingConfig(quote_offset_ticks=-1)

    def test_invalid_improve_spread_zero(self):
        """improve_when_spread_ticks < 1 should raise."""
        with pytest.raises(ValueError, match="improve_when_spread_ticks must be >= 1"):
            ActiveQuotingConfig(improve_when_spread_ticks=0)

    def test_invalid_max_spread_less_than_improve(self):
        """max_spread_ticks < improve_when_spread_ticks should raise."""
        with pytest.raises(ValueError, match="max_spread_ticks must be >= improve_when_spread_ticks"):
            ActiveQuotingConfig(improve_when_spread_ticks=10, max_spread_ticks=5)

    def test_invalid_refresh_threshold_zero(self):
        """refresh_threshold_ticks < 1 should raise."""
        with pytest.raises(ValueError, match="refresh_threshold_ticks must be >= 1"):
            ActiveQuotingConfig(refresh_threshold_ticks=0)

    def test_invalid_momentum_window_too_small(self):
        """momentum_window_ms < 100 should raise."""
        with pytest.raises(ValueError, match="momentum_window_ms must be >= 100"):
            ActiveQuotingConfig(momentum_window_ms=50)

    def test_invalid_cooldown_negative(self):
        """Negative cooldown_seconds should raise."""
        with pytest.raises(ValueError, match="cooldown_seconds must be >= 0"):
            ActiveQuotingConfig(cooldown_seconds=-1.0)

    def test_invalid_sweep_depth_out_of_range(self):
        """sweep_depth_threshold outside (0, 1] should raise."""
        with pytest.raises(ValueError, match="sweep_depth_threshold must be in"):
            ActiveQuotingConfig(sweep_depth_threshold=0.0)
        with pytest.raises(ValueError, match="sweep_depth_threshold must be in"):
            ActiveQuotingConfig(sweep_depth_threshold=1.5)

    def test_invalid_position_per_market_zero(self):
        """max_position_per_market < 1 should raise."""
        with pytest.raises(ValueError, match="max_position_per_market must be >= 1"):
            ActiveQuotingConfig(max_position_per_market=0)

    def test_invalid_total_liability_less_than_per_market(self):
        """max_total_liability_usdc < max_liability_per_market_usdc should raise."""
        with pytest.raises(ValueError, match="max_total_liability_usdc must be >= max_liability_per_market_usdc"):
            ActiveQuotingConfig(max_liability_per_market_usdc=100.0, max_total_liability_usdc=50.0)

    def test_invalid_order_size_too_small(self):
        """order_size_usdc < 5 should raise in live mode (Polymarket minimum)."""
        with pytest.raises(ValueError, match="order_size_usdc must be >= 5"):
            ActiveQuotingConfig(order_size_usdc=4.0, dry_run=False)

    def test_invalid_batch_size_out_of_range(self):
        """batch_size outside [1, 15] should raise."""
        with pytest.raises(ValueError, match="batch_size must be between 1 and 15"):
            ActiveQuotingConfig(batch_size=0)
        with pytest.raises(ValueError, match="batch_size must be between 1 and 15"):
            ActiveQuotingConfig(batch_size=16)

    def test_invalid_empty_assets(self):
        """Empty assets list should raise."""
        with pytest.raises(ValueError, match="assets list must not be empty"):
            ActiveQuotingConfig(assets=[])

    def test_invalid_stale_feed_timeout_zero(self):
        """stale_feed_timeout_seconds <= 0 should raise."""
        with pytest.raises(ValueError, match="stale_feed_timeout_seconds must be > 0"):
            ActiveQuotingConfig(stale_feed_timeout_seconds=0)

    def test_invalid_max_consecutive_errors_zero(self):
        """max_consecutive_errors < 1 should raise."""
        with pytest.raises(ValueError, match="max_consecutive_errors must be >= 1"):
            ActiveQuotingConfig(max_consecutive_errors=0)

    def test_invalid_ws_ping_interval_zero(self):
        """ws_ping_interval < 1 should raise."""
        with pytest.raises(ValueError, match="ws_ping_interval must be >= 1"):
            ActiveQuotingConfig(ws_ping_interval=0)

    def test_multiple_validation_errors(self):
        """Multiple validation errors should all be reported."""
        with pytest.raises(ValueError) as exc_info:
            ActiveQuotingConfig(
                quote_offset_ticks=-1,
                improve_when_spread_ticks=0,
                order_size_usdc=1.0,
                dry_run=False,  # order_size validation only applies in live mode
            )
        error_message = str(exc_info.value)
        assert "quote_offset_ticks" in error_message
        assert "improve_when_spread_ticks" in error_message
        assert "order_size_usdc" in error_message
