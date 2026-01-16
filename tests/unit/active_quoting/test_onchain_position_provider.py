"""
Unit tests for OnChainPositionProvider.
"""
import pytest
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

from rebates.active_quoting.onchain_position_provider import (
    OnChainPositionProvider,
    OnChainBalance,
    OnChainSyncResult,
    CTF_CONTRACT_ADDRESS,
    DEFAULT_RPC_URL,
)


class TestOnChainPositionProviderInit:
    """Test initialization of OnChainPositionProvider."""

    def test_init_stores_wallet_address(self):
        """Should store checksummed wallet address."""
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        # Web3 checksums addresses per EIP-55 (mixed case)
        assert provider._wallet_address.lower() == "0x1234567890123456789012345678901234567890".lower()

    def test_init_stores_rpc_url(self):
        """Should store custom RPC URL."""
        custom_url = "https://custom-rpc.example.com"
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890",
            rpc_url=custom_url,
        )
        assert provider._rpc_url == custom_url

    def test_init_uses_default_rpc_url(self):
        """Should use default RPC URL when not specified."""
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        assert provider._rpc_url == DEFAULT_RPC_URL

    def test_init_stores_timeout(self):
        """Should store custom timeout."""
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890",
            timeout_seconds=30.0,
        )
        assert provider._timeout_seconds == 30.0

    def test_init_not_initialized_immediately(self):
        """Should not initialize Web3 connection until needed."""
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        assert provider._initialized is False
        assert provider._web3 is None
        assert provider._contract is None

    def test_init_default_decimals(self):
        """Should use default 6 decimals for CTF tokens."""
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        assert provider._token_decimals == 6
        assert provider._decimal_divisor == 1_000_000

    def test_init_custom_decimals(self):
        """Should allow custom decimals configuration."""
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890",
            token_decimals=18,
        )
        assert provider._token_decimals == 18
        assert provider._decimal_divisor == 10 ** 18


class TestOnChainPositionProviderWithMockedWeb3:
    """Test OnChainPositionProvider with mocked Web3."""

    @pytest.fixture
    def mock_web3(self):
        """Create a mock Web3 instance."""
        with patch('rebates.active_quoting.onchain_position_provider.Web3') as mock_web3_class:
            mock_instance = MagicMock()
            mock_instance.is_connected.return_value = True
            mock_instance.eth.block_number = 12345678
            mock_web3_class.return_value = mock_instance
            mock_web3_class.HTTPProvider = MagicMock()
            mock_web3_class.to_checksum_address = lambda x: x
            yield mock_web3_class, mock_instance

    @pytest.fixture
    def provider(self, mock_web3):
        """Create a provider with mocked Web3."""
        mock_class, mock_instance = mock_web3
        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        return provider

    def test_ensure_initialized_connects(self, provider, mock_web3):
        """Should initialize Web3 connection on first use."""
        mock_class, mock_instance = mock_web3

        result = provider._ensure_initialized()

        assert result is True
        assert provider._initialized is True
        mock_instance.is_connected.assert_called()

    def test_ensure_initialized_fails_on_connection_error(self, mock_web3):
        """Should return False if connection fails."""
        mock_class, mock_instance = mock_web3
        mock_instance.is_connected.return_value = False

        provider = OnChainPositionProvider(
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        result = provider._ensure_initialized()

        assert result is False
        assert provider._initialized is False

    def test_is_available_initializes_if_needed(self, provider, mock_web3):
        """is_available should initialize connection if not done."""
        mock_class, mock_instance = mock_web3

        assert provider.is_available is True
        assert provider._initialized is True

    def test_fetch_balance_returns_balance(self, provider, mock_web3):
        """Should fetch and return balance for a token."""
        mock_class, mock_instance = mock_web3
        token_id = "123456789012345678901234567890"

        # Mock contract call - return 100 shares (100 * 1e6)
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 100_000_000
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balance(token_id)

        assert result is not None
        assert result.token_id == token_id
        assert result.balance == 100.0
        assert result.raw_balance == 100_000_000
        assert result.block_number == 12345678

    def test_fetch_balance_returns_zero_for_no_position(self, provider, mock_web3):
        """Should return zero balance for tokens with no position."""
        mock_class, mock_instance = mock_web3
        token_id = "123456789012345678901234567890"

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 0
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balance(token_id)

        assert result is not None
        assert result.balance == 0.0
        assert result.raw_balance == 0

    def test_fetch_balance_handles_decimals(self, provider, mock_web3):
        """Should correctly convert raw balance to shares with decimals."""
        mock_class, mock_instance = mock_web3
        token_id = "123456789012345678901234567890"

        # 50.123456 shares
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 50_123_456
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balance(token_id)

        assert result is not None
        assert abs(result.balance - 50.123456) < 0.000001

    def test_fetch_balance_returns_none_on_error(self, provider, mock_web3):
        """Should return None on Web3 error."""
        mock_class, mock_instance = mock_web3
        token_id = "123456789012345678901234567890"

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.side_effect = Exception("RPC error")
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balance(token_id)

        assert result is None

    def test_fetch_balances_batch(self, provider, mock_web3):
        """Should fetch multiple balances in one batch call."""
        mock_class, mock_instance = mock_web3
        token_ids = ["111111111111111111", "222222222222222222", "333333333333333333"]

        mock_contract = MagicMock()
        # Return different balances for each token
        mock_contract.functions.balanceOfBatch.return_value.call.return_value = [
            100_000_000,  # 100 shares
            50_000_000,   # 50 shares
            0,            # 0 shares
        ]
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balances(token_ids)

        assert result.success is True
        assert len(result.balances) == 3
        assert result.balances["111111111111111111"].balance == 100.0
        assert result.balances["222222222222222222"].balance == 50.0
        assert result.balances["333333333333333333"].balance == 0.0
        assert result.block_number == 12345678

    def test_fetch_balances_empty_list(self, provider):
        """Should handle empty token list."""
        result = provider.fetch_balances([])

        assert result.success is True
        assert len(result.balances) == 0
        assert result.duration_ms == 0.0

    def test_fetch_balances_deduplicates_token_ids(self, provider, mock_web3):
        """Should deduplicate token IDs and log warning."""
        mock_class, mock_instance = mock_web3
        # Duplicate token ID in list
        token_ids = ["111111111111111111", "222222222222222222", "111111111111111111"]

        mock_contract = MagicMock()
        # Only 2 unique tokens, so only 2 balances returned
        mock_contract.functions.balanceOfBatch.return_value.call.return_value = [
            100_000_000,
            50_000_000,
        ]
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balances(token_ids)

        assert result.success is True
        assert len(result.balances) == 2  # Deduplicated

    def test_fetch_balances_returns_error_on_failure(self, provider, mock_web3):
        """Should return error result on batch fetch failure."""
        mock_class, mock_instance = mock_web3
        token_ids = ["111111111111111111", "222222222222222222"]

        mock_contract = MagicMock()
        mock_contract.functions.balanceOfBatch.return_value.call.side_effect = Exception("RPC error")
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.fetch_balances(token_ids)

        assert result.success is False
        assert "RPC error" in result.error
        assert len(result.balances) == 0

    def test_compare_with_expected_matches(self, provider, mock_web3):
        """Should detect matching balances."""
        mock_class, mock_instance = mock_web3

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 100_000_000
        mock_instance.eth.contract.return_value = mock_contract

        result = provider.compare_with_expected("111111111111111111", 100.0)

        assert result is not None
        assert result["matches"] is True
        assert result["difference"] == 0.0

    def test_compare_with_expected_detects_discrepancy(self, provider, mock_web3):
        """Should detect discrepancy between on-chain and expected."""
        mock_class, mock_instance = mock_web3

        mock_contract = MagicMock()
        # On-chain has 100 shares
        mock_contract.functions.balanceOf.return_value.call.return_value = 100_000_000
        mock_instance.eth.contract.return_value = mock_contract

        # Expected 80 shares
        result = provider.compare_with_expected("111111111111111111", 80.0)

        assert result is not None
        assert result["matches"] is False
        assert result["difference"] == 20.0
        assert result["onchain_balance"] == 100.0
        assert result["expected_balance"] == 80.0

    def test_compare_with_expected_uses_tolerance(self, provider, mock_web3):
        """Should use tolerance when comparing balances."""
        mock_class, mock_instance = mock_web3

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 100_005_000  # 100.005 shares
        mock_instance.eth.contract.return_value = mock_contract

        # With default tolerance of 0.01, this should match
        result = provider.compare_with_expected("111111111111111111", 100.0)
        assert result["matches"] is True

        # With tighter tolerance, should not match
        result = provider.compare_with_expected("111111111111111111", 100.0, tolerance=0.001)
        assert result["matches"] is False


class TestOnChainBalance:
    """Test OnChainBalance dataclass."""

    def test_onchain_balance_creation(self):
        """Should create OnChainBalance with all fields."""
        now = datetime.utcnow()
        balance = OnChainBalance(
            token_id="token123",
            balance=100.5,
            raw_balance=100_500_000,
            fetched_at=now,
            block_number=12345678,
        )

        assert balance.token_id == "token123"
        assert balance.balance == 100.5
        assert balance.raw_balance == 100_500_000
        assert balance.fetched_at == now
        assert balance.block_number == 12345678

    def test_onchain_balance_optional_block(self):
        """Block number should be optional."""
        balance = OnChainBalance(
            token_id="token123",
            balance=100.0,
            raw_balance=100_000_000,
            fetched_at=datetime.utcnow(),
        )

        assert balance.block_number is None


class TestOnChainSyncResult:
    """Test OnChainSyncResult dataclass."""

    def test_sync_result_success(self):
        """Should create successful sync result."""
        result = OnChainSyncResult(
            balances={"token1": OnChainBalance(
                token_id="token1",
                balance=100.0,
                raw_balance=100_000_000,
                fetched_at=datetime.utcnow(),
            )},
            success=True,
            duration_ms=50.0,
            block_number=12345678,
        )

        assert result.success is True
        assert len(result.balances) == 1
        assert result.error is None
        assert result.duration_ms == 50.0

    def test_sync_result_failure(self):
        """Should create failure sync result with error."""
        result = OnChainSyncResult(
            balances={},
            success=False,
            error="RPC connection failed",
            duration_ms=100.0,
        )

        assert result.success is False
        assert len(result.balances) == 0
        assert result.error == "RPC connection failed"


class TestOnChainPositionProviderNotInitialized:
    """Test behavior when provider fails to initialize."""

    def test_fetch_balance_returns_none_when_not_initialized(self):
        """Should return None if provider cannot initialize."""
        with patch('rebates.active_quoting.onchain_position_provider.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_instance.is_connected.return_value = False
            mock_web3.return_value = mock_instance
            mock_web3.HTTPProvider = MagicMock()
            mock_web3.to_checksum_address = lambda x: x

            provider = OnChainPositionProvider(
                wallet_address="0x1234567890123456789012345678901234567890"
            )

            result = provider.fetch_balance("token123")
            assert result is None

    def test_fetch_balances_returns_error_when_not_initialized(self):
        """Should return error result if provider cannot initialize."""
        with patch('rebates.active_quoting.onchain_position_provider.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_instance.is_connected.return_value = False
            mock_web3.return_value = mock_instance
            mock_web3.HTTPProvider = MagicMock()
            mock_web3.to_checksum_address = lambda x: x

            provider = OnChainPositionProvider(
                wallet_address="0x1234567890123456789012345678901234567890"
            )

            result = provider.fetch_balances(["token1", "token2"])
            assert result.success is False
            assert "not initialized" in result.error.lower()
