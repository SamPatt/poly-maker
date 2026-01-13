"""
Unit tests for FillAnalytics - Markout analysis and fill tracking.

Tests:
- Fill recording and tracking
- Markout calculation for buys and sells
- Per-market statistics
- Aggregate statistics
- Fee tracking
- Toxicity scoring
- Pending markout management
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock

from rebates.active_quoting.fill_analytics import (
    FillAnalytics,
    FillRecord,
    MarkoutSample,
    MarketStats,
    AggregateStats,
    MARKOUT_HORIZONS,
)
from rebates.active_quoting.models import Fill, OrderSide


@pytest.fixture
def fill_analytics():
    """Create FillAnalytics instance with default horizons."""
    return FillAnalytics(horizons=[1, 5, 15])


@pytest.fixture
def sample_buy_fill():
    """Create a sample buy fill."""
    return Fill(
        order_id="order_1",
        token_id="token_abc",
        side=OrderSide.BUY,
        price=0.50,
        size=10.0,
        fee=-0.01,  # Negative = rebate
        trade_id="trade_1",
    )


@pytest.fixture
def sample_sell_fill():
    """Create a sample sell fill."""
    return Fill(
        order_id="order_2",
        token_id="token_abc",
        side=OrderSide.SELL,
        price=0.52,
        size=10.0,
        fee=-0.01,  # Negative = rebate
        trade_id="trade_2",
    )


# --- FillRecord Tests ---

class TestFillRecord:
    """Tests for FillRecord dataclass."""

    def test_fill_id_from_trade_id(self, sample_buy_fill):
        """Test fill ID uses trade_id when available."""
        record = FillRecord(fill=sample_buy_fill, mid_price_at_fill=0.50)
        assert record.fill_id == "trade_1"

    def test_fill_id_fallback(self, sample_buy_fill):
        """Test fill ID fallback when no trade_id."""
        sample_buy_fill.trade_id = None
        record = FillRecord(fill=sample_buy_fill, mid_price_at_fill=0.50)
        assert record.fill_id.startswith("order_1_")

    def test_get_markout_empty(self, sample_buy_fill):
        """Test get_markout returns None for unknown horizon."""
        record = FillRecord(fill=sample_buy_fill, mid_price_at_fill=0.50)
        assert record.get_markout(1) is None

    def test_get_markout_with_sample(self, sample_buy_fill):
        """Test get_markout returns markout value."""
        record = FillRecord(fill=sample_buy_fill, mid_price_at_fill=0.50)
        sample = MarkoutSample(
            fill_id="trade_1",
            horizon_seconds=5,
            mid_at_fill=0.50,
            mid_at_horizon=0.52,
            markout=0.02,
        )
        record.markouts[5] = sample
        assert record.get_markout(5) == 0.02


# --- MarketStats Tests ---

class TestMarketStats:
    """Tests for MarketStats dataclass."""

    def test_initial_state(self):
        """Test initial market stats."""
        stats = MarketStats(token_id="test_token")
        assert stats.fill_count == 0
        assert stats.total_volume == 0.0
        assert stats.total_notional == 0.0

    def test_avg_markout_no_data(self):
        """Test avg_markout returns None with no data."""
        stats = MarketStats(token_id="test")
        assert stats.avg_markout(5) is None

    def test_avg_markout_with_data(self):
        """Test avg_markout calculation."""
        stats = MarketStats(token_id="test")
        stats.markout_sums[5] = 0.10
        stats.markout_counts[5] = 5
        assert stats.avg_markout(5) == 0.02

    def test_avg_markout_bps(self):
        """Test avg_markout_bps calculation."""
        stats = MarketStats(token_id="test")
        stats.fill_count = 1
        stats.total_volume = 10.0
        stats.total_notional = 5.0  # avg price = 0.50
        stats.markout_sums[5] = 0.01  # 0.01 / 0.50 = 0.02 = 200 bps
        stats.markout_counts[5] = 1
        bps = stats.avg_markout_bps(5)
        assert bps is not None
        assert abs(bps - 200.0) < 0.01


# --- AggregateStats Tests ---

class TestAggregateStats:
    """Tests for AggregateStats dataclass."""

    def test_initial_state(self):
        """Test initial aggregate stats."""
        stats = AggregateStats()
        assert stats.total_fills == 0
        assert stats.net_fees == 0.0

    def test_avg_markout(self):
        """Test aggregate avg_markout calculation."""
        stats = AggregateStats()
        stats.markout_sums[5] = 0.30
        stats.markout_counts[5] = 3
        assert stats.avg_markout(5) == pytest.approx(0.10)


# --- FillAnalytics Core Tests ---

class TestFillAnalyticsCore:
    """Tests for FillAnalytics core functionality."""

    def test_init_default_horizons(self):
        """Test default horizons are set."""
        analytics = FillAnalytics()
        assert analytics.horizons == MARKOUT_HORIZONS

    def test_init_custom_horizons(self):
        """Test custom horizons."""
        analytics = FillAnalytics(horizons=[1, 2, 3])
        assert analytics.horizons == [1, 2, 3]

    def test_get_market_stats_creates_new(self, fill_analytics):
        """Test get_market_stats creates stats if not exists."""
        stats = fill_analytics.get_market_stats("new_token")
        assert stats.token_id == "new_token"
        assert "new_token" in fill_analytics.market_stats

    def test_get_market_stats_returns_existing(self, fill_analytics):
        """Test get_market_stats returns existing stats."""
        stats1 = fill_analytics.get_market_stats("token_1")
        stats1.fill_count = 5
        stats2 = fill_analytics.get_market_stats("token_1")
        assert stats2.fill_count == 5


# --- Fill Recording Tests ---

class TestFillRecording:
    """Tests for fill recording functionality."""

    def test_record_fill_basic(self, fill_analytics, sample_buy_fill):
        """Test basic fill recording."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        assert record.fill_id == "trade_1"
        assert record.mid_price_at_fill == 0.50
        assert len(record.markouts) == 3  # 1, 5, 15 seconds
        assert "trade_1" in fill_analytics.fills

    def test_record_fill_creates_markout_samples(self, fill_analytics, sample_buy_fill):
        """Test markout samples are created for each horizon."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        for horizon in [1, 5, 15]:
            assert horizon in record.markouts
            sample = record.markouts[horizon]
            assert sample.mid_at_fill == 0.50
            assert sample.horizon_seconds == horizon

    def test_record_fill_updates_market_stats(self, fill_analytics, sample_buy_fill):
        """Test market stats are updated on fill."""
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        stats = fill_analytics.get_market_stats("token_abc")
        assert stats.fill_count == 1
        assert stats.buy_count == 1
        assert stats.total_volume == 10.0
        assert stats.total_notional == 5.0  # 10 * 0.50

    def test_record_fill_updates_aggregate_stats(self, fill_analytics, sample_buy_fill):
        """Test aggregate stats are updated on fill."""
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        agg = fill_analytics.aggregate_stats
        assert agg.total_fills == 1
        assert agg.total_volume == 10.0
        assert agg.total_notional == 5.0

    def test_record_multiple_fills(self, fill_analytics, sample_buy_fill, sample_sell_fill):
        """Test recording multiple fills."""
        fill_analytics.record_fill(fill=sample_buy_fill, mid_price_at_fill=0.50, schedule_markouts=False)
        fill_analytics.record_fill(fill=sample_sell_fill, mid_price_at_fill=0.51, schedule_markouts=False)

        assert len(fill_analytics.fills) == 2
        stats = fill_analytics.get_market_stats("token_abc")
        assert stats.fill_count == 2
        assert stats.buy_count == 1
        assert stats.sell_count == 1

    def test_record_fill_pending_markouts(self, fill_analytics, sample_buy_fill):
        """Test pending markouts are tracked."""
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        pending = fill_analytics.get_pending_markouts()
        assert len(pending) == 3  # One per horizon


# --- Fee Tracking Tests ---

class TestFeeTracking:
    """Tests for fee tracking functionality."""

    def test_rebate_tracking(self, fill_analytics, sample_buy_fill):
        """Test negative fees are tracked as rebates."""
        sample_buy_fill.fee = -0.05  # Rebate
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        stats = fill_analytics.get_market_stats("token_abc")
        assert stats.total_fees_earned == 0.05
        assert stats.total_fees_paid == 0.0

        agg = fill_analytics.aggregate_stats
        assert agg.total_fees_earned == 0.05
        assert agg.net_fees == 0.05

    def test_fee_paid_tracking(self, fill_analytics, sample_buy_fill):
        """Test positive fees are tracked as paid."""
        sample_buy_fill.fee = 0.03  # Taker fee
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        stats = fill_analytics.get_market_stats("token_abc")
        assert stats.total_fees_paid == 0.03
        assert stats.total_fees_earned == 0.0

        agg = fill_analytics.aggregate_stats
        assert agg.total_fees_paid == 0.03
        assert agg.net_fees == -0.03


# --- Markout Capture Tests ---

class TestMarkoutCapture:
    """Tests for markout capture functionality."""

    def test_capture_markout_buy_profit(self, fill_analytics, sample_buy_fill):
        """Test markout capture for profitable buy (price up)."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        # Price went up - good for buy
        sample = fill_analytics.capture_markout(
            fill_id=record.fill_id,
            horizon=5,
            mid_price_now=0.52,
        )

        assert sample is not None
        assert sample.mid_at_horizon == 0.52
        assert sample.markout == pytest.approx(0.02)  # 0.52 - 0.50
        assert sample.markout_bps == pytest.approx(400.0)  # 0.02 / 0.50 * 10000

    def test_capture_markout_buy_loss(self, fill_analytics, sample_buy_fill):
        """Test markout capture for losing buy (price down)."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        # Price went down - bad for buy
        sample = fill_analytics.capture_markout(
            fill_id=record.fill_id,
            horizon=5,
            mid_price_now=0.48,
        )

        assert sample.markout == pytest.approx(-0.02)  # 0.48 - 0.50
        assert sample.markout_bps == pytest.approx(-400.0)

    def test_capture_markout_sell_profit(self, fill_analytics, sample_sell_fill):
        """Test markout capture for profitable sell (price down)."""
        record = fill_analytics.record_fill(
            fill=sample_sell_fill,
            mid_price_at_fill=0.52,
            schedule_markouts=False,
        )

        # Price went down - good for sell
        sample = fill_analytics.capture_markout(
            fill_id=record.fill_id,
            horizon=5,
            mid_price_now=0.50,
        )

        assert sample.markout == pytest.approx(0.02)  # 0.52 - 0.50
        assert sample.markout_bps is not None

    def test_capture_markout_sell_loss(self, fill_analytics, sample_sell_fill):
        """Test markout capture for losing sell (price up)."""
        record = fill_analytics.record_fill(
            fill=sample_sell_fill,
            mid_price_at_fill=0.52,
            schedule_markouts=False,
        )

        # Price went up - bad for sell (adverse selection)
        sample = fill_analytics.capture_markout(
            fill_id=record.fill_id,
            horizon=5,
            mid_price_now=0.55,
        )

        assert sample.markout == pytest.approx(-0.03)  # 0.52 - 0.55
        assert sample.markout_bps < 0

    def test_capture_markout_updates_stats(self, fill_analytics, sample_buy_fill):
        """Test markout capture updates statistics."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        fill_analytics.capture_markout(record.fill_id, 5, 0.52)

        stats = fill_analytics.get_market_stats("token_abc")
        assert stats.markout_counts[5] == 1
        assert stats.markout_sums[5] == pytest.approx(0.02)

        agg = fill_analytics.aggregate_stats
        assert agg.markout_counts[5] == 1
        assert agg.avg_markout(5) == pytest.approx(0.02)

    def test_capture_markout_removes_pending(self, fill_analytics, sample_buy_fill):
        """Test capturing markout removes from pending list."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        pending_before = len(fill_analytics.get_pending_markouts())
        fill_analytics.capture_markout(record.fill_id, 5, 0.52)
        pending_after = len(fill_analytics.get_pending_markouts())

        assert pending_after == pending_before - 1

    def test_capture_all_markouts_sets_captured_flag(self, fill_analytics, sample_buy_fill):
        """Test all markouts captured sets record.captured flag."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        assert record.captured is False

        for horizon in fill_analytics.horizons:
            fill_analytics.capture_markout(record.fill_id, horizon, 0.51)

        assert record.captured is True

    def test_capture_markout_nonexistent_fill(self, fill_analytics):
        """Test capturing markout for nonexistent fill returns None."""
        sample = fill_analytics.capture_markout("nonexistent", 5, 0.50)
        assert sample is None

    def test_capture_markout_already_captured(self, fill_analytics, sample_buy_fill):
        """Test capturing already captured markout returns existing sample."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        sample1 = fill_analytics.capture_markout(record.fill_id, 5, 0.52)
        sample2 = fill_analytics.capture_markout(record.fill_id, 5, 0.55)

        # Should return the original sample, not update
        assert sample2.mid_at_horizon == 0.52  # Not 0.55


# --- Due Markouts Tests ---

class TestDueMarkouts:
    """Tests for due markout detection."""

    def test_get_due_markouts_none_due(self, fill_analytics, sample_buy_fill):
        """Test no markouts are due immediately after recording."""
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        due = fill_analytics.get_due_markouts()
        assert len(due) == 0

    def test_get_due_markouts_some_due(self, fill_analytics):
        """Test markouts become due over time."""
        # Create fill with timestamp in the past
        old_fill = Fill(
            order_id="old_order",
            token_id="token_abc",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            timestamp=datetime.utcnow() - timedelta(seconds=10),
            trade_id="old_trade",
        )

        fill_analytics.record_fill(
            fill=old_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        due = fill_analytics.get_due_markouts()
        # Horizons 1 and 5 should be due (10 seconds have passed)
        assert len(due) >= 2

    def test_process_markout_captures(self, fill_analytics):
        """Test processing due markout captures."""
        # Create fill with old timestamp
        old_fill = Fill(
            order_id="old_order",
            token_id="token_abc",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            timestamp=datetime.utcnow() - timedelta(seconds=20),
            trade_id="old_trade",
        )

        fill_analytics.record_fill(
            fill=old_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        # Mock price lookup
        def get_mid_price(token_id):
            return 0.51

        captured = fill_analytics.process_markout_captures(get_mid_price)
        assert len(captured) > 0


# --- Toxicity Score Tests ---

class TestToxicityScore:
    """Tests for toxicity scoring."""

    def test_toxicity_score_no_data(self, fill_analytics):
        """Test toxicity score with no data returns None."""
        assert fill_analytics.get_toxicity_score() is None

    def test_toxicity_score_positive_markout(self, fill_analytics, sample_buy_fill):
        """Test toxicity score with positive markout (good fills)."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        fill_analytics.capture_markout(record.fill_id, 5, 0.52)

        score = fill_analytics.get_toxicity_score()
        assert score is not None
        assert score == 0.0  # No toxicity for positive markout

    def test_toxicity_score_negative_markout(self, fill_analytics, sample_buy_fill):
        """Test toxicity score with negative markout (adverse selection)."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        fill_analytics.capture_markout(record.fill_id, 5, 0.48)

        score = fill_analytics.get_toxicity_score()
        assert score is not None
        assert score > 0  # Positive toxicity for negative markout

    def test_toxicity_score_per_market(self, fill_analytics, sample_buy_fill):
        """Test toxicity score for specific market."""
        record = fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        fill_analytics.capture_markout(record.fill_id, 5, 0.48)

        score = fill_analytics.get_toxicity_score("token_abc")
        assert score is not None
        assert score > 0


# --- Query Tests ---

class TestQueries:
    """Tests for query functionality."""

    def test_get_fill_record(self, fill_analytics, sample_buy_fill):
        """Test retrieving fill record by ID."""
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )
        record = fill_analytics.get_fill_record("trade_1")
        assert record is not None
        assert record.fill.order_id == "order_1"

    def test_get_fill_record_nonexistent(self, fill_analytics):
        """Test retrieving nonexistent fill record."""
        record = fill_analytics.get_fill_record("nonexistent")
        assert record is None

    def test_get_recent_fills(self, fill_analytics):
        """Test getting recent fills."""
        # Create multiple fills
        for i in range(5):
            fill = Fill(
                order_id=f"order_{i}",
                token_id="token_abc",
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=0.0,
                timestamp=datetime.utcnow() - timedelta(seconds=i),
                trade_id=f"trade_{i}",
            )
            fill_analytics.record_fill(fill, mid_price_at_fill=0.50, schedule_markouts=False)

        recent = fill_analytics.get_recent_fills(limit=3)
        assert len(recent) == 3
        # Should be most recent first
        assert recent[0].fill.trade_id == "trade_0"

    def test_get_recent_fills_by_token(self, fill_analytics, sample_buy_fill, sample_sell_fill):
        """Test getting recent fills filtered by token."""
        fill_analytics.record_fill(sample_buy_fill, mid_price_at_fill=0.50, schedule_markouts=False)

        other_fill = Fill(
            order_id="other",
            token_id="other_token",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            trade_id="other_trade",
        )
        fill_analytics.record_fill(other_fill, mid_price_at_fill=0.50, schedule_markouts=False)

        recent = fill_analytics.get_recent_fills(token_id="token_abc")
        assert len(recent) == 1
        assert recent[0].fill.token_id == "token_abc"


# --- P&L Tracking Tests ---

class TestPnLTracking:
    """Tests for P&L tracking."""

    def test_update_realized_pnl(self, fill_analytics, sample_buy_fill):
        """Test updating realized P&L."""
        fill_analytics.record_fill(sample_buy_fill, mid_price_at_fill=0.50, schedule_markouts=False)
        fill_analytics.update_realized_pnl("token_abc", 10.0)

        stats = fill_analytics.get_market_stats("token_abc")
        assert stats.realized_pnl == 10.0

        agg = fill_analytics.aggregate_stats
        assert agg.realized_pnl == 10.0

    def test_update_realized_pnl_multiple_markets(self, fill_analytics):
        """Test realized P&L aggregation across markets."""
        # Create fills for two markets
        fill1 = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            trade_id="trade_1",
        )
        fill2 = Fill(
            order_id="order_2",
            token_id="token_2",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            trade_id="trade_2",
        )

        fill_analytics.record_fill(fill1, mid_price_at_fill=0.50, schedule_markouts=False)
        fill_analytics.record_fill(fill2, mid_price_at_fill=0.50, schedule_markouts=False)

        fill_analytics.update_realized_pnl("token_1", 5.0)
        fill_analytics.update_realized_pnl("token_2", 8.0)

        agg = fill_analytics.aggregate_stats
        assert agg.realized_pnl == 13.0


# --- Summary Tests ---

class TestSummary:
    """Tests for summary functionality."""

    def test_get_summary_empty(self, fill_analytics):
        """Test summary with no data."""
        summary = fill_analytics.get_summary()
        assert summary["total_fills"] == 0
        assert summary["net_fees"] == 0.0
        assert summary["pending_markouts"] == 0

    def test_get_summary_with_data(self, fill_analytics, sample_buy_fill):
        """Test summary with fill data."""
        fill_analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        summary = fill_analytics.get_summary()
        assert summary["total_fills"] == 1
        assert summary["total_volume"] == 10.0
        assert "token_abc" in summary["markets"]
        assert summary["pending_markouts"] == 3  # One per horizon


# --- Reset Tests ---

class TestReset:
    """Tests for reset functionality."""

    def test_reset_all(self, fill_analytics, sample_buy_fill, sample_sell_fill):
        """Test resetting all analytics."""
        fill_analytics.record_fill(sample_buy_fill, mid_price_at_fill=0.50, schedule_markouts=False)
        fill_analytics.record_fill(sample_sell_fill, mid_price_at_fill=0.51, schedule_markouts=False)

        fill_analytics.reset()

        assert len(fill_analytics.fills) == 0
        assert len(fill_analytics.market_stats) == 0
        assert fill_analytics.aggregate_stats.total_fills == 0

    def test_reset_specific_market(self, fill_analytics):
        """Test resetting specific market."""
        fill1 = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            trade_id="trade_1",
        )
        fill2 = Fill(
            order_id="order_2",
            token_id="token_2",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            trade_id="trade_2",
        )

        fill_analytics.record_fill(fill1, mid_price_at_fill=0.50, schedule_markouts=False)
        fill_analytics.record_fill(fill2, mid_price_at_fill=0.50, schedule_markouts=False)

        fill_analytics.reset("token_1")

        assert "token_1" not in fill_analytics.market_stats
        assert "token_2" in fill_analytics.market_stats


# --- Callback Tests ---

class TestCallbacks:
    """Tests for callback functionality."""

    def test_on_markout_captured_callback(self, sample_buy_fill):
        """Test on_markout_captured callback is triggered."""
        captured_samples = []

        def on_captured(sample):
            captured_samples.append(sample)

        analytics = FillAnalytics(
            horizons=[5],
            on_markout_captured=on_captured,
        )

        record = analytics.record_fill(
            fill=sample_buy_fill,
            mid_price_at_fill=0.50,
            schedule_markouts=False,
        )

        analytics.capture_markout(record.fill_id, 5, 0.52)

        assert len(captured_samples) == 1
        assert captured_samples[0].markout == pytest.approx(0.02)


# --- Async Tests ---

class TestAsync:
    """Tests for async functionality."""

    @pytest.mark.asyncio
    async def test_shutdown(self, fill_analytics):
        """Test clean shutdown."""
        await fill_analytics.shutdown()
        # Should complete without errors
        assert True
