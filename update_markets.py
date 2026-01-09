import os
import time
import pandas as pd
import traceback
from dotenv import load_dotenv

from data_updater.trading_utils import get_clob_client
from data_updater.find_markets import get_sel_df, get_all_markets, get_all_results, get_markets, add_volatility_to_df

load_dotenv()

# Determine data source: 'postgres', 'supabase', or 'sheets' (legacy)
DATA_SOURCE = os.getenv("DATA_SOURCE", "sheets").lower()

print(f"Data source: {DATA_SOURCE}")

# Initialize CLOB client (always needed)
client = get_clob_client()

# Initialize Google Sheets if using sheets mode
if DATA_SOURCE not in ("postgres", "supabase"):
    from data_updater.google_utils import get_spreadsheet
    from gspread_dataframe import set_with_dataframe

    spreadsheet = get_spreadsheet()
    wk_all = spreadsheet.worksheet("All Markets")
    wk_vol = spreadsheet.worksheet("Volatility Markets")
    sel_df = get_sel_df(spreadsheet, "Selected Markets")
else:
    spreadsheet = None
    wk_all = None
    wk_vol = None
    sel_df = pd.DataFrame()


def update_sheet(data, worksheet):
    """Update a Google Sheet with data (legacy mode)."""
    all_values = worksheet.get_all_values()
    existing_num_rows = len(all_values)
    existing_num_cols = len(all_values[0]) if all_values else 0

    num_rows, num_cols = data.shape
    max_rows = max(num_rows, existing_num_rows)
    max_cols = max(num_cols, existing_num_cols)

    # Create a DataFrame with the maximum size and fill it with empty strings
    padded_data = pd.DataFrame('', index=range(max_rows), columns=range(max_cols))

    # Update the padded DataFrame with the original data and its columns
    padded_data.iloc[:num_rows, :num_cols] = data.values
    padded_data.columns = list(data.columns) + [''] * (max_cols - num_cols)

    # Update the sheet with the padded DataFrame, including column headers
    set_with_dataframe(worksheet, padded_data, include_index=False, include_column_header=True, resize=True)


def update_supabase(all_markets_df, volatility_df):
    """Update Supabase database with market data."""
    try:
        from db.supabase_client import upsert_all_markets, upsert_volatility_markets
        upsert_all_markets(all_markets_df)
        upsert_volatility_markets(volatility_df)
        print(f"Updated Supabase with {len(all_markets_df)} markets and {len(volatility_df)} volatility markets")
    except ImportError as e:
        print(f"Supabase module not available: {e}")
    except Exception as e:
        print(f"Error updating Supabase: {e}")
        traceback.print_exc()


def sort_df(df):
    """Sort markets by composite score (reward vs volatility)."""
    # Calculate the mean and standard deviation for each column
    mean_gm = df['gm_reward_per_100'].mean()
    std_gm = df['gm_reward_per_100'].std()

    mean_volatility = df['volatility_sum'].mean()
    std_volatility = df['volatility_sum'].std()

    # Standardize the columns
    df['std_gm_reward_per_100'] = (df['gm_reward_per_100'] - mean_gm) / std_gm
    df['std_volatility_sum'] = (df['volatility_sum'] - mean_volatility) / std_volatility

    # Define a custom scoring function for best_bid and best_ask
    def proximity_score(value):
        if 0.1 <= value <= 0.25:
            return (0.25 - value) / 0.15
        elif 0.75 <= value <= 0.9:
            return (value - 0.75) / 0.15
        else:
            return 0

    df['bid_score'] = df['best_bid'].apply(proximity_score)
    df['ask_score'] = df['best_ask'].apply(proximity_score)

    # Create a composite score (higher is better for rewards, lower is better for volatility)
    df['composite_score'] = (
        df['std_gm_reward_per_100'] -
        df['std_volatility_sum'] +
        df['bid_score'] +
        df['ask_score']
    )

    # Sort by the composite score in descending order
    sorted_df = df.sort_values(by='composite_score', ascending=False)

    # Drop the intermediate columns used for calculation
    sorted_df = sorted_df.drop(columns=['std_gm_reward_per_100', 'std_volatility_sum', 'bid_score', 'ask_score', 'composite_score'])

    return sorted_df


def fetch_and_process_data():
    """Fetch market data from Polymarket and update data store."""
    global spreadsheet, client, wk_all, wk_vol, sel_df

    # Reinitialize connections
    client = get_clob_client()

    if DATA_SOURCE not in ("postgres", "supabase"):
        from data_updater.google_utils import get_spreadsheet
        spreadsheet = get_spreadsheet()
        wk_all = spreadsheet.worksheet("All Markets")
        wk_vol = spreadsheet.worksheet("Volatility Markets")
        wk_full = spreadsheet.worksheet("Full Markets")
        sel_df = get_sel_df(spreadsheet, "Selected Markets")
    else:
        # Load selected markets from database
        from db.supabase_client import get_selected_markets
        sel_df = get_selected_markets()
        if sel_df.empty:
            # Create empty DataFrame with required column to avoid KeyError
            sel_df = pd.DataFrame(columns=['question'])

    # Fetch all markets from Polymarket API
    all_df = get_all_markets(client)
    print("Got all Markets")

    all_results = get_all_results(all_df, client)
    print("Got all Results")

    m_data, all_markets = get_markets(all_results, sel_df, maker_reward=0.75)
    print("Got all orderbook")

    print(f'{pd.to_datetime("now")}: Fetched all markets data of length {len(all_markets)}.')

    # Add volatility metrics
    new_df = add_volatility_to_df(all_markets)
    new_df['volatility_sum'] = new_df['24_hour'] + new_df['7_day'] + new_df['14_day']

    new_df = new_df.sort_values('volatility_sum', ascending=True)
    new_df['volatilty/reward'] = ((new_df['gm_reward_per_100'] / new_df['volatility_sum']).round(2)).astype(str)

    # Select and order columns
    new_df = new_df[['question', 'answer1', 'answer2', 'spread', 'rewards_daily_rate',
                     'gm_reward_per_100', 'sm_reward_per_100', 'bid_reward_per_100', 'ask_reward_per_100',
                     'volatility_sum', 'volatilty/reward', 'min_size',
                     '1_hour', '3_hour', '6_hour', '12_hour', '24_hour', '7_day', '30_day',
                     'best_bid', 'best_ask', 'volatility_price', 'max_spread', 'tick_size',
                     'neg_risk', 'market_slug', 'token1', 'token2', 'condition_id']]

    # Create volatility-filtered subset
    volatility_df = new_df.copy()
    volatility_df = volatility_df[new_df['volatility_sum'] < 20]
    volatility_df = volatility_df.sort_values('gm_reward_per_100', ascending=False)

    new_df = new_df.sort_values('gm_reward_per_100', ascending=False)

    print(f'{pd.to_datetime("now")}: Fetched select market of length {len(new_df)}.')

    # Update data store (minimum 50 markets to prevent bad data)
    if len(new_df) > 50:
        if DATA_SOURCE in ("postgres", "supabase"):
            update_supabase(new_df, volatility_df)
        else:
            update_sheet(new_df, wk_all)
            update_sheet(volatility_df, wk_vol)
            update_sheet(m_data, wk_full)
    else:
        print(f'{pd.to_datetime("now")}: Not updating because of insufficient data length {len(new_df)}.')


if __name__ == "__main__":
    print("=" * 60)
    print("POLY-MAKER - Market Data Updater")
    print(f"Data Source: {DATA_SOURCE}")
    print("=" * 60)

    while True:
        try:
            fetch_and_process_data()
            print(f'{pd.to_datetime("now")}: Sleeping for 1 hour...')
            time.sleep(60 * 60)  # Sleep for an hour
        except Exception as e:
            traceback.print_exc()
            print(str(e))
            print("Retrying in 5 minutes...")
            time.sleep(60 * 5)  # Retry after 5 minutes on error
