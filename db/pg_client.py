"""
PostgreSQL client module for Poly-Maker.
Replaces Google Sheets functionality with local PostgreSQL database.

The database runs on VPS 1 (trading bot) and is accessible from VPS 2 (data updater)
via Tailscale private network.
"""

import os
import pandas as pd
from typing import Tuple, Dict, Any, Optional
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

# Database connection settings
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "polymaker")
DB_USER = os.getenv("DB_USER", "polymaker")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Database availability flag - set via env var to disable DB features
DB_ENABLED = os.getenv("DB_ENABLED", "true").lower() in ("true", "1", "yes")

# Connection pool
_connection_pool = None


def _get_connection_string() -> str:
    """Build PostgreSQL connection string."""
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@contextmanager
def get_db_connection():
    """Get a database connection from the pool."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_db_cursor(commit=True):
    """Get a database cursor with automatic commit/rollback."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    with get_db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


def get_supabase_client():
    """Compatibility wrapper - returns None for PostgreSQL mode."""
    return None


def get_selected_markets() -> pd.DataFrame:
    """
    Get markets selected for trading.

    Returns:
        DataFrame with columns: question, param_type, enabled, event_date, exit_before_event
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("""
            SELECT question, param_type, enabled, event_date, exit_before_event
            FROM selected_markets
            WHERE enabled = true
        """)
        rows = cursor.fetchall()

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=["question", "param_type", "enabled", "event_date", "exit_before_event"])


def get_hyperparameters() -> Dict[str, Dict[str, float]]:
    """
    Get trading hyperparameters grouped by param_type.

    Returns:
        Nested dict: {param_type: {param_name: param_value}}
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("SELECT param_type, param_name, param_value FROM hyperparameters")
        rows = cursor.fetchall()

    params = {}
    for row in rows or []:
        param_type = row["param_type"]
        if param_type not in params:
            params[param_type] = {}
        params[param_type][row["param_name"]] = row["param_value"]

    return params


def get_all_markets() -> pd.DataFrame:
    """
    Get all available markets from database.

    Returns:
        DataFrame with all market data
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("SELECT * FROM all_markets ORDER BY gm_reward_per_100 DESC NULLS LAST")
        rows = cursor.fetchall()

    if rows:
        df = pd.DataFrame(rows)
        # Rename columns back to match trading code expectations
        column_mapping = {
            "hour_1": "1_hour",
            "hour_3": "3_hour",
            "hour_6": "6_hour",
            "hour_12": "12_hour",
            "hour_24": "24_hour",
            "day_7": "7_day",
            "day_14": "14_day",
            "day_30": "30_day",
            "volatility_reward_ratio": "volatilty/reward",
        }
        df = df.rename(columns=column_mapping)
        return df
    return pd.DataFrame()


def get_sheet_df(read_only: Optional[bool] = None) -> Tuple[pd.DataFrame, Dict[str, Dict[str, float]]]:
    """
    Get market configuration data - drop-in replacement for Google Sheets version.

    Merges selected_markets with all_markets on 'question' field.

    Args:
        read_only: Ignored (kept for API compatibility)

    Returns:
        Tuple of (merged_dataframe, hyperparameters_dict)
    """
    # Get selected markets
    selected_df = get_selected_markets()

    if selected_df.empty:
        print("No markets selected in database")
        return pd.DataFrame(), {}

    # Get all markets data
    all_markets_df = get_all_markets()

    if all_markets_df.empty:
        print("No market data in database - run update_markets.py first")
        return pd.DataFrame(), {}

    # Merge selected with all markets (inner join on question)
    result = selected_df.merge(all_markets_df, on="question", how="inner")

    # Filter out empty rows
    if "question" in result.columns:
        result = result[result["question"] != ""].reset_index(drop=True)

    # Get hyperparameters
    hyperparams = get_hyperparameters()

    # Merge hyperparameters into each row based on param_type
    # This allows trading code to access trade_size, max_size, etc. directly from the row
    for idx, row in result.iterrows():
        # Determine param_type (handle potential column name conflicts from merge)
        param_type = row.get("param_type_x") or row.get("param_type") or "default"
        if param_type not in hyperparams:
            param_type = "default"
        result.at[idx, "param_type"] = param_type

        # Merge all parameters for this param_type into the row
        if param_type in hyperparams:
            for param_name, param_value in hyperparams[param_type].items():
                result.at[idx, param_name] = param_value

    return result, hyperparams


def upsert_all_markets(df: pd.DataFrame) -> None:
    """
    Upsert market data to all_markets table.

    Args:
        df: DataFrame with market data
    """
    if df.empty:
        print("No data to upsert to all_markets")
        return

    # Rename columns to match database schema
    column_mapping = {
        "1_hour": "hour_1",
        "3_hour": "hour_3",
        "6_hour": "hour_6",
        "12_hour": "hour_12",
        "24_hour": "hour_24",
        "7_day": "day_7",
        "14_day": "day_14",
        "30_day": "day_30",
        "volatilty/reward": "volatility_reward_ratio",
        "end_date_iso": "end_date",
    }

    df = df.rename(columns=column_mapping)

    # Convert neg_risk to boolean
    if "neg_risk" in df.columns:
        df["neg_risk"] = df["neg_risk"].apply(lambda x: x == "TRUE" if isinstance(x, str) else bool(x))

    # Get column names that exist in the dataframe
    columns = [
        "question", "answer1", "answer2", "token1", "token2", "condition_id",
        "market_slug", "event_slug", "neg_risk", "spread", "best_bid", "best_ask",
        "rewards_daily_rate", "gm_reward_per_100", "sm_reward_per_100",
        "bid_reward_per_100", "ask_reward_per_100", "volatility_sum",
        "volatility_reward_ratio", "min_size", "hour_1", "hour_3", "hour_6",
        "hour_12", "hour_24", "day_7", "day_14", "day_30", "volatility_price",
        "max_spread", "tick_size", "composite_score", "end_date"
    ]

    available_cols = [c for c in columns if c in df.columns]

    with get_db_cursor() as cursor:
        for _, row in df.iterrows():
            values = {col: row.get(col) for col in available_cols}

            # Handle NaN values
            for key, value in values.items():
                if pd.isna(value):
                    values[key] = None

            # Build upsert query
            cols = list(values.keys())
            placeholders = ", ".join([f"%({c})s" for c in cols])
            updates = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols if c != "question"])

            query = f"""
                INSERT INTO all_markets ({", ".join(cols)})
                VALUES ({placeholders})
                ON CONFLICT (question) DO UPDATE SET {updates}, updated_at = NOW()
            """

            cursor.execute(query, values)

    print(f"Upserted {len(df)} records to all_markets")


def upsert_volatility_markets(df: pd.DataFrame) -> None:
    """
    Upsert filtered volatility market data.

    Args:
        df: DataFrame with volatility market data
    """
    if df.empty:
        print("No data to upsert to volatility_markets")
        return

    # Select only columns that exist in volatility_markets table
    columns = [
        "question", "answer1", "answer2", "token1", "token2",
        "condition_id", "market_slug", "event_slug", "neg_risk", "spread",
        "best_bid", "best_ask", "rewards_daily_rate", "gm_reward_per_100",
        "volatility_sum", "min_size", "max_spread", "tick_size"
    ]

    available_cols = [c for c in columns if c in df.columns]
    df_filtered = df[available_cols].copy()

    # Convert neg_risk to boolean
    if "neg_risk" in df_filtered.columns:
        df_filtered["neg_risk"] = df_filtered["neg_risk"].apply(
            lambda x: x == "TRUE" if isinstance(x, str) else bool(x)
        )

    with get_db_cursor() as cursor:
        # Clear existing and insert new
        cursor.execute("DELETE FROM volatility_markets")

        for _, row in df_filtered.iterrows():
            values = {col: row.get(col) for col in available_cols}

            # Handle NaN values
            for key, value in values.items():
                if pd.isna(value):
                    values[key] = None

            cols = list(values.keys())
            placeholders = ", ".join([f"%({c})s" for c in cols])

            query = f"""
                INSERT INTO volatility_markets ({", ".join(cols)})
                VALUES ({placeholders})
            """

            cursor.execute(query, values)

    print(f"Inserted {len(df_filtered)} records to volatility_markets")


def upsert_account_stats(df: pd.DataFrame) -> None:
    """
    Update account statistics table.

    Args:
        df: DataFrame with account stats
    """
    if df.empty:
        return

    # Rename columns to match schema
    column_mapping = {
        "marketInSelected": "market_in_selected",
    }

    df = df.rename(columns=column_mapping)

    with get_db_cursor() as cursor:
        # Clear existing stats and insert new
        cursor.execute("DELETE FROM account_stats")

        for _, row in df.iterrows():
            values = row.to_dict()

            # Handle NaN values
            for key, value in list(values.items()):
                if pd.isna(value):
                    values[key] = None

            # Only insert columns that exist
            valid_cols = ["question", "answer", "order_size", "position_size",
                         "market_in_selected", "earnings", "earning_percentage"]
            values = {k: v for k, v in values.items() if k in valid_cols}

            if values:
                cols = list(values.keys())
                placeholders = ", ".join([f"%({c})s" for c in cols])

                query = f"""
                    INSERT INTO account_stats ({", ".join(cols)})
                    VALUES ({placeholders})
                """

                cursor.execute(query, values)

    print(f"Updated account_stats with {len(df)} records")


def record_trade(
    token: str,
    side: str,
    price: float,
    size: float,
    market_question: Optional[str] = None,
    pnl: Optional[float] = None,
    source: str = "bot"
) -> None:
    """
    Record a trade to the trade_history table.

    Args:
        token: Token ID
        side: 'BUY' or 'SELL'
        price: Execution price
        size: Trade size
        market_question: Optional market question text
        pnl: Optional P&L for the trade
        source: Source of trade (default: 'bot')
    """
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO trade_history (token, market_question, side, price, size, pnl, source)
            VALUES (%(token)s, %(market_question)s, %(side)s, %(price)s, %(size)s, %(pnl)s, %(source)s)
        """, {
            "token": str(token),
            "market_question": market_question,
            "side": side.upper(),
            "price": float(price),
            "size": float(size),
            "pnl": float(pnl) if pnl is not None else None,
            "source": source,
        })


def add_selected_market(question: str, param_type: str = "default") -> bool:
    """
    Add a market to selected_markets.

    Args:
        question: Market question text
        param_type: Parameter type to use (default: 'default')

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO selected_markets (question, param_type, enabled)
                VALUES (%(question)s, %(param_type)s, true)
                ON CONFLICT (question) DO UPDATE SET
                    param_type = EXCLUDED.param_type,
                    enabled = true
            """, {"question": question, "param_type": param_type})
        return True
    except Exception as e:
        print(f"Error adding selected market: {e}")
        return False


def remove_selected_market(question: str) -> bool:
    """
    Remove a market from selected_markets (sets enabled=False).

    Args:
        question: Market question text

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                UPDATE selected_markets SET enabled = false WHERE question = %(question)s
            """, {"question": question})
        return True
    except Exception as e:
        print(f"Error removing selected market: {e}")
        return False


def update_market_event_settings(
    question: str,
    event_date: Optional[str] = None,
    exit_before_event: bool = False
) -> bool:
    """
    Update event date and exit settings for a market.

    Args:
        question: Market question text
        event_date: Event date in YYYY-MM-DD format (or None to clear)
        exit_before_event: Whether to exit before event date

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                UPDATE selected_markets
                SET event_date = %(event_date)s,
                    exit_before_event = %(exit_before_event)s
                WHERE question = %(question)s
            """, {
                "question": question,
                "event_date": event_date if event_date else None,
                "exit_before_event": exit_before_event
            })
        return True
    except Exception as e:
        print(f"Error updating market event settings: {e}")
        return False


def update_hyperparameter(param_type: str, param_name: str, param_value: float) -> bool:
    """
    Update a hyperparameter value.

    Args:
        param_type: Parameter type (e.g., 'default')
        param_name: Parameter name (e.g., 'stop_loss_threshold')
        param_value: New value

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO hyperparameters (param_type, param_name, param_value)
                VALUES (%(param_type)s, %(param_name)s, %(param_value)s)
                ON CONFLICT (param_type, param_name) DO UPDATE SET
                    param_value = EXCLUDED.param_value
            """, {
                "param_type": param_type,
                "param_name": param_name,
                "param_value": float(param_value)
            })
        return True
    except Exception as e:
        print(f"Error updating hyperparameter: {e}")
        return False


def get_recent_trades(limit: int = 50) -> pd.DataFrame:
    """
    Get recent trades from history.

    Args:
        limit: Maximum number of trades to return

    Returns:
        DataFrame with recent trades
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("""
            SELECT * FROM trade_history
            ORDER BY created_at DESC
            LIMIT %(limit)s
        """, {"limit": limit})
        rows = cursor.fetchall()

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame()


def get_daily_pnl() -> float:
    """
    Calculate total P&L for today.

    Returns:
        Total P&L value
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("""
            SELECT COALESCE(SUM(pnl), 0) as total_pnl
            FROM trade_history
            WHERE created_at >= CURRENT_DATE
            AND pnl IS NOT NULL
        """)
        row = cursor.fetchone()

    return float(row["total_pnl"]) if row else 0.0


def init_database() -> None:
    """
    Initialize database tables if they don't exist.
    Run this once during setup.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")

    with open(schema_path, "r") as f:
        schema_sql = f.read()

    with get_db_cursor() as cursor:
        cursor.execute(schema_sql)

    print("Database initialized successfully")


# ============================================
# Rebates Bot Database Functions
# ============================================

def save_rebates_market(
    slug: str,
    question: str,
    condition_id: str,
    up_token: str,
    down_token: str,
    event_start: str,
    up_price: float,
    down_price: float,
    neg_risk: bool = False,
    tick_size: float = 0.01
) -> bool:
    """
    Save a rebates market to the database when orders are placed.

    Args:
        slug: Market slug (unique identifier)
        question: Market question text
        condition_id: Market condition ID for redemption
        up_token: UP token ID
        down_token: DOWN token ID
        event_start: Event start time (ISO format)
        up_price: UP order price
        down_price: DOWN order price
        neg_risk: Whether market uses negative risk
        tick_size: Minimum price increment

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO rebates_markets (
                    slug, question, condition_id, up_token, down_token,
                    event_start, up_price, down_price, neg_risk, tick_size, status
                )
                VALUES (
                    %(slug)s, %(question)s, %(condition_id)s, %(up_token)s, %(down_token)s,
                    %(event_start)s, %(up_price)s, %(down_price)s, %(neg_risk)s, %(tick_size)s, 'UPCOMING'
                )
                ON CONFLICT (slug) DO UPDATE SET
                    up_price = EXCLUDED.up_price,
                    down_price = EXCLUDED.down_price
            """, {
                "slug": slug,
                "question": question,
                "condition_id": condition_id,
                "up_token": up_token,
                "down_token": down_token,
                "event_start": event_start,
                "up_price": up_price,
                "down_price": down_price,
                "neg_risk": neg_risk,
                "tick_size": tick_size
            })
        return True
    except Exception as e:
        print(f"Error saving rebates market: {e}")
        return False


def update_rebates_market_status(slug: str, status: str) -> bool:
    """
    Update the status of a rebates market.

    Args:
        slug: Market slug
        status: New status (UPCOMING, LIVE, RESOLVED, REDEEMED)

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                UPDATE rebates_markets
                SET status = %(status)s
                WHERE slug = %(slug)s
            """, {"slug": slug, "status": status})
        return True
    except Exception as e:
        print(f"Error updating rebates market status: {e}")
        return False


def update_rebates_market_fills(
    slug: str,
    up_filled: Optional[bool] = None,
    down_filled: Optional[bool] = None
) -> bool:
    """
    Update fill status for a rebates market.

    Args:
        slug: Market slug
        up_filled: Whether UP order was filled (or None to leave unchanged)
        down_filled: Whether DOWN order was filled (or None to leave unchanged)

    Returns:
        True if successful
    """
    try:
        updates = []
        params = {"slug": slug}

        if up_filled is not None:
            updates.append("up_filled = %(up_filled)s")
            params["up_filled"] = up_filled
        if down_filled is not None:
            updates.append("down_filled = %(down_filled)s")
            params["down_filled"] = down_filled

        if not updates:
            return True

        with get_db_cursor() as cursor:
            cursor.execute(f"""
                UPDATE rebates_markets
                SET {", ".join(updates)}
                WHERE slug = %(slug)s
            """, params)
        return True
    except Exception as e:
        print(f"Error updating rebates market fills: {e}")
        return False


def get_pending_rebates_markets() -> pd.DataFrame:
    """
    Get rebates markets that are not yet redeemed.

    Returns markets in UPCOMING, LIVE, or RESOLVED status that
    need to be tracked for redemption.

    Returns:
        DataFrame with pending rebates markets
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("""
            SELECT * FROM rebates_markets
            WHERE status != 'REDEEMED'
            ORDER BY event_start ASC
        """)
        rows = cursor.fetchall()

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame()


def mark_rebates_market_redeemed(slug: str) -> bool:
    """
    Mark a rebates market as redeemed.

    Args:
        slug: Market slug

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                UPDATE rebates_markets
                SET status = 'REDEEMED', redeemed = true
                WHERE slug = %(slug)s
            """, {"slug": slug})
        return True
    except Exception as e:
        print(f"Error marking rebates market redeemed: {e}")
        return False


def cleanup_old_rebates_markets(days: int = 7) -> int:
    """
    Delete old redeemed rebates markets.

    Args:
        days: Delete markets older than this many days

    Returns:
        Number of deleted records
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                DELETE FROM rebates_markets
                WHERE status = 'REDEEMED'
                AND created_at < NOW() - INTERVAL '%s days'
            """, (days,))
            return cursor.rowcount
    except Exception as e:
        print(f"Error cleaning up old rebates markets: {e}")
        return 0


# ============================================
# Active Quoting Bot Database Functions
# ============================================


def save_active_quoting_position(
    token_id: str,
    size: float,
    avg_price: float,
    realized_pnl: float = 0.0,
    total_fees: float = 0.0,
    market_name: Optional[str] = None,
) -> bool:
    """
    Save or update an active quoting position.

    Upserts position on fill - creates if not exists, updates if exists.

    Args:
        token_id: Token ID
        size: Current position size
        avg_price: Average entry price
        realized_pnl: Realized P&L
        total_fees: Total fees paid
        market_name: Optional human-readable market name

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO active_quoting_positions (
                    token_id, market_name, size, avg_price, realized_pnl, total_fees, updated_at
                )
                VALUES (
                    %(token_id)s, %(market_name)s, %(size)s, %(avg_price)s,
                    %(realized_pnl)s, %(total_fees)s, NOW()
                )
                ON CONFLICT (token_id) DO UPDATE SET
                    market_name = COALESCE(EXCLUDED.market_name, active_quoting_positions.market_name),
                    size = EXCLUDED.size,
                    avg_price = EXCLUDED.avg_price,
                    realized_pnl = EXCLUDED.realized_pnl,
                    total_fees = EXCLUDED.total_fees,
                    updated_at = NOW()
            """, {
                "token_id": token_id,
                "market_name": market_name,
                "size": float(size),
                "avg_price": float(avg_price),
                "realized_pnl": float(realized_pnl),
                "total_fees": float(total_fees),
            })
        return True
    except Exception as e:
        print(f"Error saving active quoting position: {e}")
        return False


def get_active_quoting_positions() -> pd.DataFrame:
    """
    Load all active quoting positions.

    Used on startup to restore position state.

    Returns:
        DataFrame with position data
    """
    try:
        with get_db_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT token_id, market_name, size, avg_price, realized_pnl, total_fees, updated_at
                FROM active_quoting_positions
                WHERE size != 0
                ORDER BY updated_at DESC
            """)
            rows = cursor.fetchall()
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=["token_id", "market_name", "size", "avg_price",
                                      "realized_pnl", "total_fees", "updated_at"])
    except Exception as e:
        print(f"Error getting active quoting positions: {e}")
        return pd.DataFrame()


def delete_active_quoting_position(token_id: str) -> bool:
    """
    Delete an active quoting position from the database.

    Used when a position goes to 0 to prevent stale data.

    Args:
        token_id: Token ID to delete

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                DELETE FROM active_quoting_positions
                WHERE token_id = %(token_id)s
            """, {"token_id": token_id})
        return True
    except Exception as e:
        print(f"Error deleting active quoting position: {e}")
        return False


def clear_all_active_quoting_positions() -> bool:
    """
    Delete all active quoting positions from the database.

    Used for full reset/cleanup.

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM active_quoting_positions")
        return True
    except Exception as e:
        print(f"Error clearing all active quoting positions: {e}")
        return False


def save_active_quoting_fill(
    fill_id: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    fee: float = 0.0,
    mid_at_fill: Optional[float] = None,
    order_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    market_name: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> bool:
    """
    Record a fill for active quoting analytics.

    Args:
        fill_id: Unique fill identifier
        token_id: Token ID
        side: 'BUY' or 'SELL'
        price: Fill price
        size: Fill size in shares
        fee: Fee paid (negative = rebate)
        mid_at_fill: Mid price at time of fill
        order_id: Optional order ID
        trade_id: Optional trade ID
        market_name: Optional market name
        timestamp: Optional timestamp (ISO format)

    Returns:
        True if successful
    """
    try:
        notional = price * size
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO active_quoting_fills (
                    fill_id, token_id, market_name, side, price, size, notional,
                    fee, mid_at_fill, order_id, trade_id, timestamp
                )
                VALUES (
                    %(fill_id)s, %(token_id)s, %(market_name)s, %(side)s, %(price)s,
                    %(size)s, %(notional)s, %(fee)s, %(mid_at_fill)s, %(order_id)s,
                    %(trade_id)s, COALESCE(%(timestamp)s::timestamptz, NOW())
                )
                ON CONFLICT (fill_id) DO NOTHING
            """, {
                "fill_id": fill_id,
                "token_id": token_id,
                "market_name": market_name,
                "side": side.upper(),
                "price": float(price),
                "size": float(size),
                "notional": float(notional),
                "fee": float(fee),
                "mid_at_fill": float(mid_at_fill) if mid_at_fill else None,
                "order_id": order_id,
                "trade_id": trade_id,
                "timestamp": timestamp,
            })
        return True
    except Exception as e:
        print(f"Error saving active quoting fill: {e}")
        return False


def save_active_quoting_markout(
    fill_id: str,
    horizon_seconds: int,
    mid_at_fill: float,
    mid_at_horizon: Optional[float] = None,
    markout: Optional[float] = None,
    markout_bps: Optional[float] = None,
) -> bool:
    """
    Save a markout sample for a fill.

    Args:
        fill_id: Fill ID from active_quoting_fills
        horizon_seconds: Markout horizon (e.g., 1, 5, 15, 30, 60)
        mid_at_fill: Mid price at fill time
        mid_at_horizon: Mid price at horizon time (None if not captured yet)
        markout: Markout value in price terms
        markout_bps: Markout in basis points

    Returns:
        True if successful
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO active_quoting_markouts (
                    fill_id, horizon_seconds, mid_at_fill, mid_at_horizon,
                    markout, markout_bps, captured_at
                )
                VALUES (
                    %(fill_id)s, %(horizon_seconds)s, %(mid_at_fill)s, %(mid_at_horizon)s,
                    %(markout)s, %(markout_bps)s,
                    CASE WHEN %(mid_at_horizon)s IS NOT NULL THEN NOW() ELSE NULL END
                )
                ON CONFLICT (fill_id, horizon_seconds) DO UPDATE SET
                    mid_at_horizon = COALESCE(EXCLUDED.mid_at_horizon, active_quoting_markouts.mid_at_horizon),
                    markout = COALESCE(EXCLUDED.markout, active_quoting_markouts.markout),
                    markout_bps = COALESCE(EXCLUDED.markout_bps, active_quoting_markouts.markout_bps),
                    captured_at = CASE
                        WHEN EXCLUDED.mid_at_horizon IS NOT NULL THEN NOW()
                        ELSE active_quoting_markouts.captured_at
                    END
            """, {
                "fill_id": fill_id,
                "horizon_seconds": int(horizon_seconds),
                "mid_at_fill": float(mid_at_fill),
                "mid_at_horizon": float(mid_at_horizon) if mid_at_horizon else None,
                "markout": float(markout) if markout else None,
                "markout_bps": float(markout_bps) if markout_bps else None,
            })
        return True
    except Exception as e:
        print(f"Error saving active quoting markout: {e}")
        return False


def get_pending_markout_captures() -> pd.DataFrame:
    """
    Get fills that are awaiting markout capture.

    Used on startup to recover pending markout captures.

    Returns:
        DataFrame with fill_id, token_id, horizon_seconds, mid_at_fill, timestamp
    """
    try:
        with get_db_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT
                    m.fill_id, f.token_id, m.horizon_seconds, m.mid_at_fill, f.timestamp,
                    f.side, f.price, f.size
                FROM active_quoting_markouts m
                JOIN active_quoting_fills f ON m.fill_id = f.fill_id
                WHERE m.captured_at IS NULL
                ORDER BY f.timestamp ASC
            """)
            rows = cursor.fetchall()
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame()
    except Exception as e:
        print(f"Error getting pending markout captures: {e}")
        return pd.DataFrame()


def save_active_quoting_session(
    session_id: str,
    markets: list,
    config_snapshot: Optional[dict] = None,
) -> bool:
    """
    Create a new active quoting session record.

    Args:
        session_id: Unique session identifier
        markets: List of token IDs being quoted
        config_snapshot: Optional config snapshot as dict

    Returns:
        True if successful
    """
    try:
        import json
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO active_quoting_sessions (
                    session_id, markets, config_snapshot, status, start_time
                )
                VALUES (
                    %(session_id)s, %(markets)s, %(config_snapshot)s, 'RUNNING', NOW()
                )
                ON CONFLICT (session_id) DO UPDATE SET
                    markets = EXCLUDED.markets,
                    config_snapshot = EXCLUDED.config_snapshot,
                    status = 'RUNNING',
                    start_time = NOW()
            """, {
                "session_id": session_id,
                "markets": markets,
                "config_snapshot": json.dumps(config_snapshot) if config_snapshot else None,
            })
        return True
    except Exception as e:
        print(f"Error saving active quoting session: {e}")
        return False


def update_active_quoting_session(
    session_id: str,
    status: Optional[str] = None,
    total_fills: Optional[int] = None,
    total_volume: Optional[float] = None,
    total_notional: Optional[float] = None,
    net_fees: Optional[float] = None,
    realized_pnl: Optional[float] = None,
) -> bool:
    """
    Update an active quoting session's statistics.

    Args:
        session_id: Session ID
        status: Optional new status (RUNNING, STOPPED, CRASHED)
        total_fills: Optional total fills count
        total_volume: Optional total volume
        total_notional: Optional total notional
        net_fees: Optional net fees
        realized_pnl: Optional realized P&L

    Returns:
        True if successful
    """
    try:
        updates = []
        params = {"session_id": session_id}

        if status is not None:
            updates.append("status = %(status)s")
            params["status"] = status
            if status in ("STOPPED", "CRASHED"):
                updates.append("end_time = NOW()")

        if total_fills is not None:
            updates.append("total_fills = %(total_fills)s")
            params["total_fills"] = int(total_fills)

        if total_volume is not None:
            updates.append("total_volume = %(total_volume)s")
            params["total_volume"] = float(total_volume)

        if total_notional is not None:
            updates.append("total_notional = %(total_notional)s")
            params["total_notional"] = float(total_notional)

        if net_fees is not None:
            updates.append("net_fees = %(net_fees)s")
            params["net_fees"] = float(net_fees)

        if realized_pnl is not None:
            updates.append("realized_pnl = %(realized_pnl)s")
            params["realized_pnl"] = float(realized_pnl)

        if not updates:
            return True

        with get_db_cursor() as cursor:
            cursor.execute(f"""
                UPDATE active_quoting_sessions
                SET {", ".join(updates)}
                WHERE session_id = %(session_id)s
            """, params)
        return True
    except Exception as e:
        print(f"Error updating active quoting session: {e}")
        return False


def get_active_quoting_fills(
    token_id: Optional[str] = None,
    limit: int = 100,
    since: Optional[str] = None,
) -> pd.DataFrame:
    """
    Get recent active quoting fills.

    Args:
        token_id: Optional filter by token
        limit: Maximum rows to return
        since: Optional timestamp filter (ISO format)

    Returns:
        DataFrame with fill records
    """
    try:
        params = {"limit": limit}
        where_clauses = []

        if token_id:
            where_clauses.append("token_id = %(token_id)s")
            params["token_id"] = token_id

        if since:
            where_clauses.append("timestamp >= %(since)s::timestamptz")
            params["since"] = since

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        with get_db_cursor(commit=False) as cursor:
            cursor.execute(f"""
                SELECT *
                FROM active_quoting_fills
                {where_sql}
                ORDER BY timestamp DESC
                LIMIT %(limit)s
            """, params)
            rows = cursor.fetchall()

        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame()
    except Exception as e:
        print(f"Error getting active quoting fills: {e}")
        return pd.DataFrame()


def get_active_quoting_markout_stats(
    token_id: Optional[str] = None,
) -> pd.DataFrame:
    """
    Get aggregate markout statistics.

    Args:
        token_id: Optional filter by token

    Returns:
        DataFrame with horizon, count, avg_markout, avg_markout_bps
    """
    try:
        params = {}
        where_sql = ""

        if token_id:
            where_sql = "WHERE f.token_id = %(token_id)s"
            params["token_id"] = token_id

        with get_db_cursor(commit=False) as cursor:
            cursor.execute(f"""
                SELECT
                    m.horizon_seconds,
                    COUNT(*) as count,
                    AVG(m.markout) as avg_markout,
                    AVG(m.markout_bps) as avg_markout_bps
                FROM active_quoting_markouts m
                JOIN active_quoting_fills f ON m.fill_id = f.fill_id
                {where_sql}
                WHERE m.captured_at IS NOT NULL
                GROUP BY m.horizon_seconds
                ORDER BY m.horizon_seconds
            """, params)
            rows = cursor.fetchall()

        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame()
    except Exception as e:
        print(f"Error getting active quoting markout stats: {e}")
        return pd.DataFrame()


def cleanup_old_active_quoting_data(days: int = 30) -> dict:
    """
    Clean up old active quoting data.

    Args:
        days: Delete data older than this many days

    Returns:
        Dict with counts of deleted records
    """
    try:
        deleted = {"fills": 0, "markouts": 0, "sessions": 0}

        with get_db_cursor() as cursor:
            # Delete old fills (markouts will cascade)
            cursor.execute("""
                DELETE FROM active_quoting_fills
                WHERE timestamp < NOW() - INTERVAL '%s days'
            """, (days,))
            deleted["fills"] = cursor.rowcount

            # Delete old sessions
            cursor.execute("""
                DELETE FROM active_quoting_sessions
                WHERE start_time < NOW() - INTERVAL '%s days'
                AND status != 'RUNNING'
            """, (days,))
            deleted["sessions"] = cursor.rowcount

        return deleted
    except Exception as e:
        print(f"Error cleaning up active quoting data: {e}")
        return {"fills": 0, "markouts": 0, "sessions": 0}
