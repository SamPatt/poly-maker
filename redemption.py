"""
Standalone redemption module for Polymarket positions.

This module handles redeeming winning positions after markets resolve.
It can be called from any bot (trading, rebates, etc.) when a market
resolution is detected.
"""

import subprocess
import threading
from typing import Optional, Callable
from datetime import datetime, timezone


# Default timeout for redemption operations (seconds)
REDEEM_TIMEOUT = 120


def redeem_position(
    condition_id: str,
    on_success: Optional[Callable[[str, str], None]] = None,
    on_error: Optional[Callable[[str, str], None]] = None,
    blocking: bool = False
) -> Optional[str]:
    """
    Redeem winning positions for a resolved market.

    This function calls the poly_merger/redeem.js script to execute
    the redemption on-chain. Can run blocking or non-blocking.

    Args:
        condition_id: The market's condition ID (hex string starting with 0x)
        on_success: Optional callback(condition_id, tx_hash) called on success
        on_error: Optional callback(condition_id, error_msg) called on failure
        blocking: If True, wait for completion. If False, run in background thread.

    Returns:
        If blocking=True: Transaction hash on success, None on failure
        If blocking=False: None (result delivered via callbacks)
    """
    if blocking:
        return _do_redeem(condition_id, on_success, on_error)
    else:
        thread = threading.Thread(
            target=_do_redeem,
            args=(condition_id, on_success, on_error),
            daemon=True
        )
        thread.start()
        return None


def _do_redeem(
    condition_id: str,
    on_success: Optional[Callable[[str, str], None]] = None,
    on_error: Optional[Callable[[str, str], None]] = None
) -> Optional[str]:
    """
    Internal function that performs the actual redemption.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        node_command = f'node poly_merger/redeem.js {condition_id}'
        print(f"[{timestamp}] [REDEMPTION] Starting: {condition_id[:20]}...")

        result = subprocess.run(
            node_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=REDEEM_TIMEOUT
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            print(f"[{timestamp}] [REDEMPTION] Failed: {error_msg[:100]}")
            if on_error:
                on_error(condition_id, error_msg)
            return None

        # Extract transaction hash from output
        tx_hash = result.stdout.strip()
        print(f"[{timestamp}] [REDEMPTION] Success: {condition_id[:20]}... -> {tx_hash[:20] if tx_hash else 'no hash'}...")

        if on_success:
            on_success(condition_id, tx_hash)

        return tx_hash

    except subprocess.TimeoutExpired:
        error_msg = f"Redemption timed out after {REDEEM_TIMEOUT}s (tx may still be pending)"
        print(f"[{timestamp}] [REDEMPTION] Timeout: {condition_id[:20]}...")
        if on_error:
            on_error(condition_id, error_msg)
        return None

    except Exception as e:
        error_msg = str(e)
        print(f"[{timestamp}] [REDEMPTION] Error: {error_msg}")
        if on_error:
            on_error(condition_id, error_msg)
        return None


def redeem_position_async(
    condition_id: str,
    on_success: Optional[Callable[[str, str], None]] = None,
    on_error: Optional[Callable[[str, str], None]] = None
) -> None:
    """
    Convenience function for non-blocking redemption.

    Same as redeem_position(..., blocking=False).
    """
    redeem_position(condition_id, on_success, on_error, blocking=False)
