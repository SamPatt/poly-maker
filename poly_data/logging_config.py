"""
Structured logging configuration for Poly-Maker.

Provides consistent logging across all modules with:
- Structured JSON output for machine parsing (file output)
- Human-readable console output with colors
- Context injection (market_id, token, trade_id)
- Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
"""

import logging
import json
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from contextlib import contextmanager


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured log output."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }

        # Add extra context if provided via extra={} parameter
        for key in ["market_id", "token", "trade_id", "error_type", "side", "price", "size"]:
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)

        # Add any custom extras
        if hasattr(record, "extra_context") and record.extra_context:
            log_entry["context"] = record.extra_context

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console formatter with colors."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Build context string from extras
        context_parts = []
        if hasattr(record, "market_id"):
            market_id = str(record.market_id)
            context_parts.append(f"market:{market_id[:16]}...")
        if hasattr(record, "token"):
            token = str(record.token)
            context_parts.append(f"token:{token[:12]}...")
        if hasattr(record, "side"):
            context_parts.append(f"side:{record.side}")

        context = f" [{', '.join(context_parts)}]" if context_parts else ""

        base_msg = f"{color}{timestamp} [{record.levelname}]{context} {record.getMessage()}{self.RESET}"

        # Add exception if present
        if record.exc_info:
            base_msg += f"\n{self.formatException(record.exc_info)}"

        return base_msg


# Cache of configured loggers
_loggers: Dict[str, logging.Logger] = {}


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """
    Get a configured logger for a module.

    Args:
        name: Logger name (typically __name__)
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Returns:
        Configured logger instance
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    # Prevent duplicate handlers
    if not logger.handlers:
        # Console handler with colored output
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(ConsoleFormatter())
        console.setLevel(logging.DEBUG)
        logger.addHandler(console)

    # Prevent propagation to root logger
    logger.propagate = False

    _loggers[name] = logger
    return logger


def add_file_handler(logger: logging.Logger, filepath: str, level: str = "DEBUG") -> None:
    """
    Add a JSON file handler to a logger.

    Args:
        logger: Logger to add handler to
        filepath: Path to log file
        level: Minimum log level for file output
    """
    file_handler = logging.FileHandler(filepath)
    file_handler.setFormatter(StructuredFormatter())
    file_handler.setLevel(getattr(logging, level.upper()))
    logger.addHandler(file_handler)


@contextmanager
def log_context(logger: logging.Logger, **context):
    """
    Context manager for adding structured context to logs.

    Usage:
        with log_context(logger, market_id="abc123", token="xyz"):
            logger.info("Processing trade")  # Will include market_id and token

    Args:
        logger: Logger instance
        **context: Key-value pairs to add to log records
    """
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        for key, value in context.items():
            setattr(record, key, value)
        return record

    logging.setLogRecordFactory(record_factory)
    try:
        yield
    finally:
        logging.setLogRecordFactory(old_factory)


class LoggerAdapter(logging.LoggerAdapter):
    """
    Logger adapter that automatically adds context to all log calls.

    Usage:
        logger = get_logger(__name__)
        ctx_logger = LoggerAdapter(logger, market_id="abc123")
        ctx_logger.info("Trade executed")  # Includes market_id in output
    """

    def __init__(self, logger: logging.Logger, **context):
        super().__init__(logger, context)

    def process(self, msg, kwargs):
        # Add our context to the extra dict
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs
