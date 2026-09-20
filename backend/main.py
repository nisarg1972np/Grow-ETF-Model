"""
Grow ETF Model — Backend API
FastAPI + yfinance + SQLite cache
Serves adjusted close prices for ETF backtesting.
Deploy on Render.com (free tier).
"""

import sqlite3
import json
import time
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import yfinance as yf

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Grow ETF Model API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nisarg1972np.github.io",
        "http://localhost",
        "http://127.0.0.1",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "price_cache.db"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", 14400))  # 4 hours default

ALLOWED_TICKERS = {
    "QQQ", "VOO", "SOXX", "SCHG", "VTI", "SCHD", "VEA",
    "SPY", "IVV", "VUG", "VTV", "JEPI", "JEPQ", "VIG",
    "ARKK", "XLK", "SMH", "BOTZ", "VGT", "IWM", "DIA",
}


def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_cache (
                ticker TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)
        conn.commit()


_init_db()


def fetch_from_yfinance(ticker: str) -> dict:
    tk = yf.Ticker(ticker)
    hist = tk.history(period="max", interval="1mo", auto_adjust=True)

    if hist.empty:
        raise ValueError(f"No data returned for {ticker}")

    # Deduplicate by month (keep last entry per month)
    seen = {}
    for idx, row in hist.iterrows():
        price = round(float(row["Close"]), 2)
        if price > 0:
            ym = idx.strftime("%Y-%m")
            seen[ym] = price

    monthly = sorted(
        [{"ym": k, "price": v} for k, v in seen.items()],
        key=lambda x: x["ym"]
    )

    # Current price from info or last data point
    try:
        info = tk.info
        current_price = info.get("regularMarketPrice") or info.get("previousClose") or 0
    except Exception:
        current_price = monthly[-1]["price"] if monthly else 0

    return {
        "monthly": monthly,
        "currentPrice": round(float(current_price), 2),
        "ticker": ticker,
        "source": "Yahoo Finance (yfinance)",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
def root():
    return {
        "name": "Grow ETF Model API",
        "version": "1.0.0",
        "endpoints": {
            "/api/prices/{ticker}": "Get monthly adjusted close prices",
            "/api/prices/batch": "Get prices for multiple tickers",
            "/api/health": "Health check",
        },
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/prices/batch")
@limiter.limit("5/minute")
def get_batch(request: Request, tickers: str = Query(..., description="Comma-separated tickers")):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]

    if not ticker_list:
        raise HTTPException(400, "No tickers provided")

    if len(ticker_list) > 15:
        raise HTTPException(400, "Max 15 tickers per batch request")

    results = {}
    errors = {}

    for t in ticker_list:
        if t not in ALLOWED_TICKERS:
            errors[t] = "Not in allowed list"
            continue
        try:
            data = _get_ticker_data(t, force_refresh=False)
            results[t] = data
        except Exception as e:
            logger.error(f"Batch fetch failed for {t}: {e}")
            errors[t] = "Data unavailable"

    return JSONResponse({"results": results, "errors": errors})


@app.get("/api/prices/{ticker}")
@limiter.limit("15/minute")
def get_prices(request: Request, ticker: str, force_refresh: bool = Query(False)):
    ticker = ticker.upper().strip()

    if ticker not in ALLOWED_TICKERS:
        raise HTTPException(400, f"Ticker not in allowed list")

    try:
        data = _get_ticker_data(ticker, force_refresh)
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        raise HTTPException(502, f"Failed to fetch data for {ticker}")

    return JSONResponse(data)


def _get_ticker_data(ticker: str, force_refresh: bool) -> dict:
    with sqlite3.connect(DB_PATH) as db:
        # Check cache
        if not force_refresh:
            row = db.execute(
                "SELECT data, fetched_at FROM price_cache WHERE ticker = ?", (ticker,)
            ).fetchone()
            if row:
                age = time.time() - row[1]
                if age < CACHE_TTL:
                    data = json.loads(row[0])
                    data["cached"] = True
                    data["cacheAge"] = round(age)
                    return data

        # Fetch fresh data
        data = fetch_from_yfinance(ticker)

        # Store in cache
        db.execute(
            "INSERT OR REPLACE INTO price_cache (ticker, data, fetched_at) VALUES (?, ?, ?)",
            (ticker, json.dumps(data), time.time()),
        )
        db.commit()

    data["cached"] = False
    return data


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
