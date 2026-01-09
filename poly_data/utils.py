import json
import pandas as pd
import os
from dotenv import load_dotenv

load_dotenv()

# Determine data source: 'supabase' or 'sheets' (legacy)
DATA_SOURCE = os.getenv("DATA_SOURCE", "sheets").lower()


def pretty_print(txt, dic):
    print("\n", txt, json.dumps(dic, indent=4))


def get_sheet_df(read_only=None):
    """
    Get market configuration data from configured data source.

    Supports both PostgreSQL (new) and Google Sheets (legacy).
    Set DATA_SOURCE=postgres in .env to use PostgreSQL.

    Args:
        read_only (bool): If None, auto-detects based on credentials availability.
                         Only used for Google Sheets mode.

    Returns:
        Tuple of (merged_dataframe, hyperparameters_dict)
    """
    if DATA_SOURCE in ("supabase", "postgres"):
        return _get_sheet_df_postgres()
    else:
        return _get_sheet_df_google(read_only)


def _get_sheet_df_postgres():
    """Get market data from PostgreSQL database."""
    try:
        from db.supabase_client import get_sheet_df as postgres_get_sheet_df
        return postgres_get_sheet_df()
    except ImportError as e:
        print(f"PostgreSQL module not available: {e}")
        print("Falling back to Google Sheets")
        return _get_sheet_df_google(None)
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}")
        print("Falling back to Google Sheets")
        return _get_sheet_df_google(None)


def _get_sheet_df_google(read_only=None):
    """Get market data from Google Sheets (legacy)."""
    from poly_utils.google_utils import get_spreadsheet

    all_sheet = 'All Markets'
    sel_sheet = 'Selected Markets'

    # Auto-detect read-only mode if not specified
    if read_only is None:
        creds_file = 'credentials.json' if os.path.exists('credentials.json') else '../credentials.json'
        read_only = not os.path.exists(creds_file)
        if read_only:
            print("No credentials found, using read-only mode")

    try:
        spreadsheet = get_spreadsheet(read_only=read_only)
    except FileNotFoundError:
        print("No credentials found, falling back to read-only mode")
        spreadsheet = get_spreadsheet(read_only=True)

    wk = spreadsheet.worksheet(sel_sheet)
    df = pd.DataFrame(wk.get_all_records())
    df = df[df['question'] != ""].reset_index(drop=True)

    wk2 = spreadsheet.worksheet(all_sheet)
    df2 = pd.DataFrame(wk2.get_all_records())
    df2 = df2[df2['question'] != ""].reset_index(drop=True)

    result = df.merge(df2, on='question', how='inner')

    wk_p = spreadsheet.worksheet('Hyperparameters')
    records = wk_p.get_all_records()
    hyperparams, current_type = {}, None

    for r in records:
        # Update current_type only when we have a non-empty type value
        # Handle both string and NaN values from pandas
        type_value = r['type']
        if type_value and str(type_value).strip() and str(type_value) != 'nan':
            current_type = str(type_value).strip()

        # Skip rows where we don't have a current_type set
        if current_type:
            # Convert numeric values to appropriate types
            value = r['value']
            try:
                # Try to convert to float if it's numeric
                if isinstance(value, str) and value.replace('.', '').replace('-', '').isdigit():
                    value = float(value)
                elif isinstance(value, (int, float)):
                    value = float(value)
            except (ValueError, TypeError):
                pass  # Keep as string if conversion fails

            hyperparams.setdefault(current_type, {})[r['param']] = value

    return result, hyperparams
