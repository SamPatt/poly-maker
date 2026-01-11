"""
Retry logic for transient failures.

Implements exponential backoff with jitter for:
- API calls (Polymarket, Gamma)
- Database operations
- Blockchain interactions
"""

import time
import random
import functools
import asyncio
from typing import Type, Tuple, Callable, Optional, TypeVar, Union

from poly_data.logging_config import get_logger
from poly_data.exceptions import RateLimitError, PolymarketAPIError, APIError

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable)


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
) -> Callable[[F], F]:
    """
    Decorator for retrying functions with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap
        exponential_base: Base for exponential calculation
        jitter: Add randomness to delay (helps avoid thundering herd)
        retryable_exceptions: Exception types to retry
        on_retry: Optional callback on each retry (receives exception and attempt number)

    Returns:
        Decorated function with retry logic

    Example:
        @retry(max_attempts=3, retryable_exceptions=(ConnectionError,))
        def fetch_data():
            return api.get_data()
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt == max_attempts:
                        logger.error(
                            f"Max retries ({max_attempts}) exceeded for {func.__name__}",
                            extra={"error_type": type(e).__name__, "function": func.__name__},
                        )
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)

                    # Add jitter to prevent thundering herd
                    if jitter:
                        delay = delay * (0.5 + random.random())

                    logger.warning(
                        f"Retry {attempt}/{max_attempts} for {func.__name__} after {delay:.1f}s: {e}",
                        extra={"error_type": type(e).__name__, "attempt": attempt},
                    )

                    if on_retry:
                        on_retry(e, attempt)

                    time.sleep(delay)

            raise last_exception  # type: ignore

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt == max_attempts:
                        logger.error(
                            f"Max retries ({max_attempts}) exceeded for {func.__name__}",
                            extra={"error_type": type(e).__name__, "function": func.__name__},
                        )
                        raise

                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)

                    if jitter:
                        delay = delay * (0.5 + random.random())

                    logger.warning(
                        f"Retry {attempt}/{max_attempts} for {func.__name__} after {delay:.1f}s: {e}",
                        extra={"error_type": type(e).__name__, "attempt": attempt},
                    )

                    if on_retry:
                        on_retry(e, attempt)

                    await asyncio.sleep(delay)

            raise last_exception  # type: ignore

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator


# Pre-configured decorators for common use cases


def retry_api(func: F) -> F:
    """
    Retry decorator for API calls.

    Retries on: PolymarketAPIError, RateLimitError, ConnectionError, TimeoutError
    Max attempts: 3
    Base delay: 1 second
    """
    return retry(
        max_attempts=3,
        base_delay=1.0,
        max_delay=15.0,
        retryable_exceptions=(
            PolymarketAPIError,
            RateLimitError,
            APIError,
            ConnectionError,
            TimeoutError,
        ),
    )(func)


def retry_blockchain(func: F) -> F:
    """
    Retry decorator for blockchain operations.

    Retries on: All exceptions (blockchain can fail many ways)
    Max attempts: 5
    Base delay: 5 seconds
    Max delay: 60 seconds
    """
    return retry(
        max_attempts=5,
        base_delay=5.0,
        max_delay=60.0,
        retryable_exceptions=(Exception,),
    )(func)


def retry_database(func: F) -> F:
    """
    Retry decorator for database operations.

    Retries on: All exceptions
    Max attempts: 3
    Base delay: 0.5 seconds
    """
    return retry(
        max_attempts=3,
        base_delay=0.5,
        max_delay=10.0,
        retryable_exceptions=(Exception,),
    )(func)


class RetryState:
    """
    Helper class for manual retry logic when decorators aren't suitable.

    Usage:
        retry_state = RetryState(max_attempts=3)
        while retry_state.should_retry():
            try:
                result = do_something()
                break
            except Exception as e:
                retry_state.record_failure(e)
                if not retry_state.should_retry():
                    raise
                retry_state.wait()
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.attempt = 0
        self.last_exception: Optional[Exception] = None

    def should_retry(self) -> bool:
        """Check if more attempts are available."""
        return self.attempt < self.max_attempts

    def record_failure(self, exception: Exception) -> None:
        """Record a failed attempt."""
        self.attempt += 1
        self.last_exception = exception
        logger.warning(
            f"Attempt {self.attempt}/{self.max_attempts} failed: {exception}",
            extra={"error_type": type(exception).__name__, "attempt": self.attempt},
        )

    def wait(self) -> None:
        """Wait before next retry with exponential backoff."""
        delay = min(self.base_delay * (self.exponential_base ** (self.attempt - 1)), self.max_delay)
        delay = delay * (0.5 + random.random())  # Add jitter
        time.sleep(delay)

    async def async_wait(self) -> None:
        """Async version of wait."""
        delay = min(self.base_delay * (self.exponential_base ** (self.attempt - 1)), self.max_delay)
        delay = delay * (0.5 + random.random())
        await asyncio.sleep(delay)
