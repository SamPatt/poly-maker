"""
Poly-Maker Web UI
FastAPI application for managing the market making bot.
"""

import os
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

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


# ============== Dashboard ==============

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard showing overview of positions and stats."""
    db_status = get_db_status()
    bot_status = get_bot_status()

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
        "positions": positions,
        "recent_trades": recent_trades,
        "daily_pnl": daily_pnl,
        "total_earnings": total_earnings,
        "now": datetime.now(timezone.utc),
    })


# ============== Markets ==============

@app.get("/markets", response_class=HTMLResponse)
async def markets_list(request: Request, search: str = "", show_selected: bool = False):
    """Browse and select markets for trading."""
    db_status = get_db_status()

    all_markets = []
    selected_questions = set()

    if db_status["connected"]:
        try:
            from db.supabase_client import get_db_cursor

            # Get selected markets
            with get_db_cursor(commit=False) as cursor:
                cursor.execute("SELECT question FROM selected_markets WHERE enabled = true")
                rows = cursor.fetchall()
                selected_questions = {row["question"] for row in rows}

            # Get all markets with optional search
            with get_db_cursor(commit=False) as cursor:
                if search:
                    cursor.execute("""
                        SELECT question, answer1, answer2, best_bid, best_ask,
                               gm_reward_per_100, volatility_sum, min_size, neg_risk
                        FROM all_markets
                        WHERE LOWER(question) LIKE LOWER(%(search)s)
                        ORDER BY gm_reward_per_100 DESC NULLS LAST
                        LIMIT 100
                    """, {"search": f"%{search}%"})
                elif show_selected:
                    cursor.execute("""
                        SELECT m.question, m.answer1, m.answer2, m.best_bid, m.best_ask,
                               m.gm_reward_per_100, m.volatility_sum, m.min_size, m.neg_risk
                        FROM all_markets m
                        INNER JOIN selected_markets s ON m.question = s.question
                        WHERE s.enabled = true
                        ORDER BY m.gm_reward_per_100 DESC NULLS LAST
                    """)
                else:
                    cursor.execute("""
                        SELECT question, answer1, answer2, best_bid, best_ask,
                               gm_reward_per_100, volatility_sum, min_size, neg_risk
                        FROM all_markets
                        ORDER BY gm_reward_per_100 DESC NULLS LAST
                        LIMIT 100
                    """)
                all_markets = cursor.fetchall() or []

        except Exception as e:
            print(f"Error fetching markets: {e}")

    return templates.TemplateResponse("markets.html", {
        "request": request,
        "page": "markets",
        "db_status": db_status,
        "markets": all_markets,
        "selected_questions": selected_questions,
        "search": search,
        "show_selected": show_selected,
        "selected_count": len(selected_questions),
    })


@app.post("/markets/toggle")
async def toggle_market(question: str = Form(...), action: str = Form(...)):
    """Enable or disable a market for trading."""
    try:
        from db.supabase_client import add_selected_market, remove_selected_market

        if action == "enable":
            add_selected_market(question)
        else:
            remove_selected_market(question)

        return RedirectResponse(url="/markets?show_selected=true", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== Parameters ==============

@app.get("/parameters", response_class=HTMLResponse)
async def parameters_list(request: Request):
    """View and edit trading parameters."""
    db_status = get_db_status()

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

    return {
        "database": db_status,
        "bot": bot_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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
