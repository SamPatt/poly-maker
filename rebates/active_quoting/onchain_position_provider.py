"""
OnChainPositionProvider - Fetches ERC-1155 token balances from the Polygon blockchain.

Phase 1: Read-only on-chain snapshot for logging/comparison.
The provider fetches balances from the Conditional Tokens Framework (CTF) contract
and is used to verify data accuracy against API/WebSocket sources.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from web3 import Web3
from web3.exceptions import Web3Exception

logger = logging.getLogger(__name__)

# Polygon Conditional Tokens Framework contract address
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Default Polygon RPC endpoints (public, may have rate limits)
DEFAULT_RPC_URL = "https://polygon-rpc.com"

# CTF token decimals (same as USDC collateral)
DEFAULT_TOKEN_DECIMALS = 6

# Minimal ABI for balanceOf and balanceOfBatch
CTF_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owners", "type": "address[]"},
            {"name": "ids", "type": "uint256[]"}
        ],
        "name": "balanceOfBatch",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    }
]


@dataclass
class OnChainBalance:
    """Result of an on-chain balance query."""
    token_id: str
    balance: float  # In shares (raw / 10^decimals)
    raw_balance: int  # Raw token amount
    fetched_at: datetime
    block_number: Optional[int] = None


@dataclass
class OnChainSyncResult:
    """Result of a batch balance sync."""
    balances: Dict[str, OnChainBalance]  # token_id -> balance
    success: bool
    error: Optional[str] = None
    duration_ms: float = 0.0
    block_number: Optional[int] = None


class OnChainPositionProvider:
    """
    Fetches ERC-1155 token balances from the Polygon blockchain.

    Uses the Conditional Tokens Framework (CTF) contract to query
    balances for the wallet address that holds trading inventory.

    Phase 1: Read-only - logs balances for comparison with API/WS.
    """

    def __init__(
        self,
        wallet_address: str,
        rpc_url: str = DEFAULT_RPC_URL,
        timeout_seconds: float = 10.0,
        token_decimals: int = DEFAULT_TOKEN_DECIMALS,
    ):
        """
        Initialize the on-chain position provider.

        Args:
            wallet_address: The wallet address that holds inventory (checksummed or not)
            rpc_url: Polygon RPC endpoint URL
            timeout_seconds: Request timeout for RPC calls
            token_decimals: Decimal places for CTF tokens (default 6, same as USDC)
        """
        self._rpc_url = rpc_url
        self._timeout_seconds = timeout_seconds
        self._wallet_address = Web3.to_checksum_address(wallet_address)
        self._token_decimals = token_decimals
        self._decimal_divisor = 10 ** token_decimals

        # Initialize Web3 connection
        self._web3: Optional[Web3] = None
        self._contract = None
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        """
        Lazily initialize Web3 connection.

        Returns:
            True if initialization succeeded, False otherwise
        """
        if self._initialized:
            return True

        try:
            self._web3 = Web3(Web3.HTTPProvider(
                self._rpc_url,
                request_kwargs={'timeout': self._timeout_seconds}
            ))

            # Verify connection
            if not self._web3.is_connected():
                logger.error(f"Failed to connect to RPC: {self._rpc_url}")
                return False

            # Initialize contract
            self._contract = self._web3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                abi=CTF_BALANCE_ABI
            )

            self._initialized = True
            logger.info(
                f"OnChainPositionProvider initialized: "
                f"wallet={self._wallet_address[:10]}..., rpc={self._rpc_url}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize OnChainPositionProvider: {e}")
            return False

    @property
    def is_available(self) -> bool:
        """Check if the provider is available and connected."""
        if not self._initialized:
            return self._ensure_initialized()
        try:
            return self._web3 is not None and self._web3.is_connected()
        except Exception:
            return False

    def fetch_balance(self, token_id: str) -> Optional[OnChainBalance]:
        """
        Fetch the on-chain balance for a single token.

        Args:
            token_id: The ERC-1155 token ID (as string, will be converted to int)

        Returns:
            OnChainBalance if successful, None on error
        """
        if not self._ensure_initialized():
            return None

        try:
            start = datetime.utcnow()

            # Get current block for reference
            block_number = self._web3.eth.block_number

            # Query balance
            raw_balance = self._contract.functions.balanceOf(
                self._wallet_address,
                int(token_id)
            ).call()

            # Convert to shares
            balance = float(raw_balance) / self._decimal_divisor

            result = OnChainBalance(
                token_id=token_id,
                balance=balance,
                raw_balance=raw_balance,
                fetched_at=datetime.utcnow(),
                block_number=block_number,
            )

            duration_ms = (datetime.utcnow() - start).total_seconds() * 1000
            logger.debug(
                f"On-chain balance fetched: {token_id[:20]}... = {balance:.2f} "
                f"(block={block_number}, {duration_ms:.0f}ms)"
            )

            return result

        except Web3Exception as e:
            logger.error(f"Web3 error fetching balance for {token_id[:20]}...: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching balance for {token_id[:20]}...: {e}")
            return None

    def fetch_balances(self, token_ids: List[str]) -> OnChainSyncResult:
        """
        Fetch on-chain balances for multiple tokens using batch call.

        Uses balanceOfBatch for efficiency when querying multiple tokens.

        Args:
            token_ids: List of ERC-1155 token IDs

        Returns:
            OnChainSyncResult with balances dict and status
        """
        if not token_ids:
            return OnChainSyncResult(
                balances={},
                success=True,
                duration_ms=0.0,
            )

        # Deduplicate token_ids to avoid masking issues upstream
        unique_token_ids = list(dict.fromkeys(token_ids))  # Preserves order
        if len(unique_token_ids) < len(token_ids):
            logger.warning(
                f"fetch_balances received {len(token_ids) - len(unique_token_ids)} "
                f"duplicate token_ids, deduplicating to {len(unique_token_ids)}"
            )
            token_ids = unique_token_ids

        if not self._ensure_initialized():
            return OnChainSyncResult(
                balances={},
                success=False,
                error="Provider not initialized",
            )

        start = datetime.utcnow()

        try:
            # Get current block for reference
            block_number = self._web3.eth.block_number

            # Prepare batch call parameters
            # balanceOfBatch takes arrays of owners and ids
            owners = [self._wallet_address] * len(token_ids)
            ids = [int(tid) for tid in token_ids]

            # Query all balances in one call
            raw_balances = self._contract.functions.balanceOfBatch(
                owners,
                ids
            ).call()

            # Build result dict
            now = datetime.utcnow()
            balances: Dict[str, OnChainBalance] = {}

            for token_id, raw_balance in zip(token_ids, raw_balances):
                balance = float(raw_balance) / self._decimal_divisor
                balances[token_id] = OnChainBalance(
                    token_id=token_id,
                    balance=balance,
                    raw_balance=raw_balance,
                    fetched_at=now,
                    block_number=block_number,
                )

            duration_ms = (datetime.utcnow() - start).total_seconds() * 1000

            # Log summary
            non_zero = sum(1 for b in balances.values() if b.balance > 0)
            logger.info(
                f"On-chain batch fetch: {len(token_ids)} tokens, "
                f"{non_zero} non-zero, block={block_number}, {duration_ms:.0f}ms"
            )

            return OnChainSyncResult(
                balances=balances,
                success=True,
                duration_ms=duration_ms,
                block_number=block_number,
            )

        except Web3Exception as e:
            duration_ms = (datetime.utcnow() - start).total_seconds() * 1000
            error_msg = f"Web3 error in batch fetch: {e}"
            logger.error(error_msg)
            return OnChainSyncResult(
                balances={},
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (datetime.utcnow() - start).total_seconds() * 1000
            error_msg = f"Error in batch fetch: {e}"
            logger.error(error_msg)
            return OnChainSyncResult(
                balances={},
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )

    def compare_with_expected(
        self,
        token_id: str,
        expected_balance: float,
        tolerance: float = 0.01,
    ) -> Optional[Dict]:
        """
        Compare on-chain balance with expected value (from API/WS).

        Phase 1 helper for logging discrepancies.

        Args:
            token_id: Token to check
            expected_balance: Expected balance from API/WS
            tolerance: Acceptable difference in shares

        Returns:
            Dict with comparison results, or None on fetch error
        """
        onchain = self.fetch_balance(token_id)
        if onchain is None:
            return None

        diff = onchain.balance - expected_balance
        matches = abs(diff) <= tolerance

        result = {
            "token_id": token_id,
            "onchain_balance": onchain.balance,
            "expected_balance": expected_balance,
            "difference": diff,
            "matches": matches,
            "block_number": onchain.block_number,
            "fetched_at": onchain.fetched_at.isoformat(),
        }

        if not matches:
            logger.warning(
                f"On-chain discrepancy: {token_id[:20]}... "
                f"onchain={onchain.balance:.2f} vs expected={expected_balance:.2f} "
                f"(diff={diff:+.2f})"
            )
        else:
            logger.debug(
                f"On-chain match: {token_id[:20]}... = {onchain.balance:.2f}"
            )

        return result
