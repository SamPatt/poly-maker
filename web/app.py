"""
Poly-Maker Web UI
FastAPI application for managing the market making bot.
"""

import os
import subprocess
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# Create FastAPI app
app = FastAPI(
    title="Poly-Maker",
    description="Market Making Bot Admin Panel",
    version="1.0.0"
)

# Setup templates
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Mount static files if directory exists
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def get_db_status() -> dict:
    """Check database connection status."""
    try:
        from db.supabase_client import get_db_cursor
        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT 1")
        return {"connected": True, "error": None}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def get_bot_status() -> dict:
    """Get basic bot status info."""
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    data_source = os.getenv("DATA_SOURCE", "sheets")
    return {
        "dry_run": dry_run,
        "data_source": data_source,
    }


def get_trading_bot_status() -> dict:
    """Check if the trading bot process is running."""
    try:
        # Check for main.py process
        result = subprocess.run(
            ["pgrep", "-f", "python.*main.py"],
            capture_output=True,
            text=True,
            timeout=5
        )
        is_running = result.returncode == 0
        pid = result.stdout.strip().split('\n')[0] if is_running else None

        # Check screen session
        screen_result = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            timeout=5
        )
        has_screen = "trading" in screen_result.stdout

        return {
            "running": is_running,
            "pid": pid,
            "screen_session": has_screen,
            "error": None
        }
    except Exception as e:
        return {
            "running": False,
            "pid": None,
            "screen_session": False,
            "error": str(e)
        }


# ============== Dashboard ==============

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard showing overview of positions and stats."""
    db_status = get_db_status()
    bot_status = get_bot_status()
    trading_bot_status = get_trading_bot_status()

    # Get summary data
    positions = []
    recent_trades = []
    daily_pnl = 0.0
    total_earnings = 0.0

    if db_status["connected"]:
        try:
            from db.supabase_client import get_recent_trades, get_daily_pnl, get_db_cursor

            # Get recent trades
            trades_df = get_recent_trades(limit=10)
            if not trades_df.empty:
                recent_trades = trades_df.to_dict(orient="records")

            # Get daily P&L
            daily_pnl = get_daily_pnl()

            # Get account stats for positions
            with get_db_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT question, answer, position_size, earnings
                    FROM account_stats
                    WHERE position_size > 0
                    ORDER BY position_size DESC
                    LIMIT 20
                """)
                positions = cursor.fetchall() or []

                # Get total earnings
                cursor.execute("SELECT COALESCE(SUM(earnings), 0) FROM account_stats")
                row = cursor.fetchone()
                total_earnings = float(row[0]) if row else 0.0

        except Exception as e:
            print(f"Error fetching dashboard data: {e}")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "page": "dashboard",
        "db_status": db_status,
        "bot_status": bot_status,
        "trading_bot_status": trading_bot_status,
        "positions": positions,
        "recent_trades": recent_trades,
        "daily_pnl": daily_pnl,
        "total_earnings": total_earnings,
        "now": datetime.now(timezone.utc),
    })


# ============== Markets ==============

@app.get("/markets", response_class=HTMLResponse)
async def markets_list(
    request: Request,
    search: str = "",
    show_selected: bool = False,
    sort: str = "score",
    limit: int = 100,
    max_volatility: Optional[str] = None
):
    """Browse and select markets for trading."""
    # Convert max_volatility from string to float, handling empty strings
    max_vol_float: Optional[float] = None
    if max_volatility and max_volatility.strip():
        try:
            max_vol_float = float(max_volatility)
        except ValueError:
            pass

    db_status = get_db_status()
    trading_bot_status = get_trading_bot_status()

    all_markets = []
    selected_questions = set()

    # Map sort options to SQL
    sort_options = {
        "score": "composite_score DESC NULLS LAST",
        "reward": "gm_reward_per_100 DESC NULLS LAST",
        "volatility": "volatility_sum ASC NULLS LAST",
        "spread": "(best_ask - best_bid) ASC NULLS LAST",
    }
    order_by = sort_options.get(sort, sort_options["score"])

    # Store event settings for selected markets
    selected_event_settings = {}

    if db_status["connected"]:
        try:
            from db.supabase_client import get_db_cursor

            # Get selected markets with event settings
            with get_db_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT question, event_date, exit_before_event
                    FROM selected_markets WHERE enabled = true
                """)
                rows = cursor.fetchall()
                selected_questions = {row["question"] for row in rows}
                # Store event settings keyed by question
                for row in rows:
                    selected_event_settings[row["question"]] = {
                        "event_date": row["event_date"],
                        "exit_before_event": row["exit_before_event"]
                    }

            # Build query with filters
            with get_db_cursor(commit=False) as cursor:
                base_cols = """question, answer1, answer2, best_bid, best_ask,
                               gm_reward_per_100, volatility_sum, min_size, neg_risk,
                               composite_score, market_slug, event_slug, end_date"""

                if search:
                    query = f"""
                        SELECT {base_cols}
                        FROM all_markets
                        WHERE LOWER(question) LIKE LOWER(%(search)s)
                        {'AND volatility_sum <= %(max_vol)s' if max_vol_float else ''}
                        ORDER BY {order_by}
                        LIMIT %(limit)s
                    """
                    cursor.execute(query, {"search": f"%{search}%", "max_vol": max_vol_float, "limit": limit})
                elif show_selected:
                    query = f"""
                        SELECT m.question, m.answer1, m.answer2, m.best_bid, m.best_ask,
                               m.gm_reward_per_100, m.volatility_sum, m.min_size, m.neg_risk,
                               m.composite_score, m.market_slug, m.event_slug,
                               COALESCE(s.event_date, m.end_date) as event_date,
                               s.exit_before_event,
                               m.end_date as api_end_date
                        FROM all_markets m
                        INNER JOIN selected_markets s ON m.question = s.question
                        WHERE s.enabled = true
                        ORDER BY {order_by}
                    """
                    cursor.execute(query)
                else:
                    query = f"""
                        SELECT {base_cols}
                        FROM all_markets
                        WHERE 1=1
                        {'AND volatility_sum <= %(max_vol)s' if max_vol_float else ''}
                        ORDER BY {order_by}
                        LIMIT %(limit)s
                    """
                    cursor.execute(query, {"max_vol": max_vol_float, "limit": limit})
                all_markets = cursor.fetchall() or []

        except Exception as e:
            print(f"Error fetching markets: {e}")

    return templates.TemplateResponse("markets.html", {
        "request": request,
        "page": "markets",
        "db_status": db_status,
        "trading_bot_status": trading_bot_status,
        "markets": all_markets,
        "selected_questions": selected_questions,
        "selected_event_settings": selected_event_settings,
        "search": search,
        "show_selected": show_selected,
        "selected_count": len(selected_questions),
        "sort": sort,
        "limit": limit,
        "max_volatility": max_vol_float,  # Pass float for template comparisons
    })


@app.post("/markets/toggle")
async def toggle_market(
    question: str = Form(...),
    action: str = Form(...),
    return_search: str = Form(""),
    return_sort: str = Form("score"),
    return_limit: str = Form("100"),
    return_max_volatility: str = Form(""),
    return_show_selected: str = Form("")
):
    """Enable or disable a market for trading."""
    try:
        from db.supabase_client import add_selected_market, remove_selected_market

        if action == "enable":
            add_selected_market(question)
        else:
            remove_selected_market(question)

        # Build redirect URL preserving current view state
        params = []
        if return_search:
            params.append(f"search={quote_plus(return_search)}")
        if return_sort and return_sort != "score":
            params.append(f"sort={return_sort}")
        if return_limit and return_limit != "100":
            params.append(f"limit={return_limit}")
        if return_max_volatility:
            params.append(f"max_volatility={return_max_volatility}")
        if return_show_selected == "true":
            params.append("show_selected=true")

        redirect_url = "/markets"
        if params:
            redirect_url += "?" + "&".join(params)

        return RedirectResponse(url=redirect_url, status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/markets/event-settings")
async def update_event_settings(
    request: Request,
    question: str = Form(...),
    event_date: str = Form(""),
    exit_before_event: str = Form("")
):
    """Update event date and exit settings for a market."""
    try:
        from db.supabase_client import update_market_event_settings

        # Convert checkbox value to boolean
        exit_enabled = exit_before_event == "on" or exit_before_event == "true"

        # Handle empty date
        date_value = event_date if event_date.strip() else None

        update_market_event_settings(question, date_value, exit_enabled)

        # Return JSON for AJAX requests, redirect for form submissions
        if request.headers.get("accept", "").startswith("application/json") or \
           "fetch" in request.headers.get("sec-fetch-mode", ""):
            return {"success": True, "question": question}

        return RedirectResponse(url="/markets?show_selected=true", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== Parameters ==============

@app.get("/parameters", response_class=HTMLResponse)
async def parameters_list(request: Request):
    """View and edit trading parameters."""
    db_status = get_db_status()
    trading_bot_status = get_trading_bot_status()

    params_by_type = {}

    if db_status["connected"]:
        try:
            from db.supabase_client import get_hyperparameters
            params_by_type = get_hyperparameters()
        except Exception as e:
            print(f"Error fetching parameters: {e}")

    return templates.TemplateResponse("parameters.html", {
        "request": request,
        "page": "parameters",
        "db_status": db_status,
        "trading_bot_status": trading_bot_status,
        "params_by_type": params_by_type,
    })


@app.post("/parameters/update")
async def update_parameter(
    param_type: str = Form(...),
    param_name: str = Form(...),
    param_value: float = Form(...)
):
    """Update a hyperparameter value."""
    try:
        from db.supabase_client import update_hyperparameter
        update_hyperparameter(param_type, param_name, param_value)
        return RedirectResponse(url="/parameters", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/parameters/add")
async def add_parameter(
    param_type: str = Form(...),
    param_name: str = Form(...),
    param_value: float = Form(...)
):
    """Add a new hyperparameter."""
    try:
        from db.supabase_client import update_hyperparameter
        update_hyperparameter(param_type, param_name, param_value)
        return RedirectResponse(url="/parameters", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== Trades ==============

@app.get("/trades", response_class=HTMLResponse)
async def trades_list(request: Request, limit: int = 50):
    """View trade history."""
    db_status = get_db_status()
    trading_bot_status = get_trading_bot_status()

    trades = []
    total_pnl = 0.0

    if db_status["connected"]:
        try:
            from db.supabase_client import get_recent_trades, get_daily_pnl

            trades_df = get_recent_trades(limit=limit)
            if not trades_df.empty:
                trades = trades_df.to_dict(orient="records")

            total_pnl = get_daily_pnl()
        except Exception as e:
            print(f"Error fetching trades: {e}")

    return templates.TemplateResponse("trades.html", {
        "request": request,
        "page": "trades",
        "db_status": db_status,
        "trading_bot_status": trading_bot_status,
        "trades": trades,
        "total_pnl": total_pnl,
        "limit": limit,
    })


# ============== API Endpoints ==============

@app.get("/api/status")
async def api_status():
    """Get system status as JSON."""
    db_status = get_db_status()
    bot_status = get_bot_status()
    trading_bot_status = get_trading_bot_status()

    return {
        "database": db_status,
        "bot": bot_status,
        "trading_bot": trading_bot_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/bot/restart")
async def restart_bot():
    """Restart the trading bot."""
    try:
        # Get the directory where the bot should run
        bot_dir = Path(__file__).resolve().parent.parent

        # Kill existing bot process
        subprocess.run(
            ["pkill", "-f", "python.*main.py"],
            capture_output=True,
            timeout=10
        )

        # Kill existing screen session
        subprocess.run(
            ["screen", "-S", "trading", "-X", "quit"],
            capture_output=True,
            timeout=5
        )

        # Small delay to ensure cleanup
        import time
        time.sleep(1)

        # Start new bot in screen session
        # Using bash -c to handle the complex command
        start_cmd = f"cd {bot_dir} && source .venv/bin/activate && python -u main.py 2>&1 | tee /tmp/trading.log"
        result = subprocess.run(
            ["screen", "-dmS", "trading", "bash", "-c", start_cmd],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return {"success": False, "error": result.stderr or "Failed to start bot"}

        # Wait a moment and check if it started
        time.sleep(2)
        status = get_trading_bot_status()

        return {
            "success": True,
            "message": "Bot restart initiated",
            "status": status
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/bot/stop")
async def stop_bot():
    """Stop the trading bot."""
    try:
        # Kill bot process
        subprocess.run(
            ["pkill", "-f", "python.*main.py"],
            capture_output=True,
            timeout=10
        )

        # Kill screen session
        subprocess.run(
            ["screen", "-S", "trading", "-X", "quit"],
            capture_output=True,
            timeout=5
        )

        return {"success": True, "message": "Bot stopped"}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/markets")
async def api_markets(limit: int = 100):
    """Get markets as JSON."""
    try:
        from db.supabase_client import get_all_markets
        df = get_all_markets()
        if not df.empty:
            return df.head(limit).to_dict(orient="records")
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/selected")
async def api_selected_markets():
    """Get selected markets as JSON."""
    try:
        from db.supabase_client import get_selected_markets
        df = get_selected_markets()
        if not df.empty:
            return df.to_dict(orient="records")
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/parameters")
async def api_parameters():
    """Get hyperparameters as JSON."""
    try:
        from db.supabase_client import get_hyperparameters
        return get_hyperparameters()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
