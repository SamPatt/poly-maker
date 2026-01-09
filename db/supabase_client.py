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
        DataFrame with columns: question, param_type, enabled
    """
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("""
            SELECT question, param_type, enabled
            FROM selected_markets
            WHERE enabled = true
        """)
        rows = cursor.fetchall()

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=["question", "param_type", "enabled"])


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
        return pd.DataFrame(rows)
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

    # Ensure each selected market has a valid param_type
    if "param_type" in result.columns:
        for idx, row in result.iterrows():
            param_type = row.get("param_type_x") or row.get("param_type")
            if param_type not in hyperparams:
                result.at[idx, "param_type"] = "default"

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
    }

    df = df.rename(columns=column_mapping)

    # Convert neg_risk to boolean
    if "neg_risk" in df.columns:
        df["neg_risk"] = df["neg_risk"].apply(lambda x: x == "TRUE" if isinstance(x, str) else bool(x))

    # Get column names that exist in the dataframe
    columns = [
        "question", "answer1", "answer2", "token1", "token2", "condition_id",
        "market_slug", "neg_risk", "spread", "best_bid", "best_ask",
        "rewards_daily_rate", "gm_reward_per_100", "sm_reward_per_100",
        "bid_reward_per_100", "ask_reward_per_100", "volatility_sum",
        "volatility_reward_ratio", "min_size", "hour_1", "hour_3", "hour_6",
        "hour_12", "hour_24", "day_7", "day_14", "day_30", "volatility_price",
        "max_spread", "tick_size", "composite_score"
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
        "condition_id", "market_slug", "neg_risk", "spread",
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
