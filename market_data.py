"""
Market Data Module for Claris Platform
======================================

Provides a production-ready market data abstraction layer for the SkyView Investment Advisors
Claris platform. Features include:

- Pluggable architecture supporting multiple data providers (yfinance, Morningstar DWS, etc.)
- In-memory TTL-based caching for quotes, fundamentals, and historical data
- Comprehensive error handling with no external API dependencies required
- Flask Blueprint with REST endpoints for market data consumption
- Thread-safe cache implementation

Dependencies:
- Flask
- yfinance
- Python stdlib: datetime, time, json, logging, threading
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

import yfinance as yf
from flask import Blueprint, jsonify, request

# Configure logging
logger = logging.getLogger(__name__)


# ============================================================================
# TTL Cache Implementation
# ============================================================================

class TTLCache:
    """
    Simple thread-safe, TTL-based cache for market data.

    Stores key-value pairs with expiration times. Automatically removes
    expired entries on access.
    """

    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """
        Store a value with a time-to-live.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Seconds until expiration
        """
        with self._lock:
            self._data[key] = {
                "value": value,
                "expires_at": time.time() + ttl_seconds,
            }

    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve a value if it exists and hasn't expired.

        Args:
            key: Cache key

        Returns:
            Cached value or None if missing/expired
        """
        with self._lock:
            if key not in self._data:
                return None

            entry = self._data[key]
            if time.time() > entry["expires_at"]:
                del self._data[key]
                return None

            return entry["value"]

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._data.clear()

    def cleanup_expired(self) -> None:
        """Remove all expired entries (optional maintenance)."""
        now = time.time()
        with self._lock:
            expired_keys = [
                k for k, v in self._data.items()
                if now > v["expires_at"]
            ]
            for k in expired_keys:
                del self._data[k]


# ============================================================================
# Market Data Client
# ============================================================================

class MarketDataClient:
    """
    Abstraction layer for market data retrieval with pluggable provider support.

    Provides a clean interface for fetching market quotes, historical data,
    news, and sector performance. Currently uses yfinance as the primary provider.
    Future implementations can swap in Morningstar DWS or other paid APIs.

    Features:
    - TTL-based caching for improved performance
    - Error handling that prevents crashes on API failures
    - Support for configurable cache TTLs
    """

    # Default cache TTLs (seconds)
    QUOTE_CACHE_TTL = 300  # 5 minutes
    HISTORICAL_CACHE_TTL = 3600  # 1 hour
    SECTOR_CACHE_TTL = 900  # 15 minutes
    NEWS_CACHE_TTL = 600  # 10 minutes

    # Standard indices for market snapshot
    MARKET_INDICES = {
        "SPY": "S&P 500",
        "QQQ": "Nasdaq-100",
        "DIA": "Dow Jones",
        "IWM": "Russell 2000",
        "AGG": "Bloomberg Aggregate Bond",
        "GLD": "Gold",
        "TLT": "20+ Year Treasury",
        "^VIX": "VIX (Volatility)",
        "^TNX": "10-Year Yield",
    }

    # Sector ETFs
    SECTOR_ETFS = {
        "XLK": "Technology",
        "XLF": "Financials",
        "XLV": "Healthcare",
        "XLE": "Energy",
        "XLI": "Industrials",
        "XLY": "Consumer Discretionary",
        "XLP": "Consumer Staples",
        "XLU": "Utilities",
        "XLRE": "Real Estate",
        "XLC": "Communication Services",
        "XLB": "Materials",
    }

    # Major indices for movers
    MAJOR_INDICES = ["^GSPC", "^IXIC", "^DJI", "^FTSE", "^N225"]

    def __init__(self):
        self.cache = TTLCache()
        logger.info("MarketDataClient initialized")

    # ========================================================================
    # Core Data Retrieval Methods
    # ========================================================================

    def get_market_snapshot(self) -> Dict[str, Any]:
        """
        Retrieve current market snapshot with key indices.

        Returns:
            Dict with key indices and their current metrics:
            {
                "SPY": {"name": "...", "price": 400.50, "change": 1.25, ...},
                ...
            }
        """
        cache_key = "market_snapshot"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        snapshot = {}
        for ticker, name in self.MARKET_INDICES.items():
            try:
                quote_data = self._fetch_quote_data(ticker)
                if quote_data:
                    snapshot[ticker] = {
                        "name": name,
                        "price": quote_data.get("price"),
                        "change": quote_data.get("change"),
                        "change_pct": quote_data.get("change_pct"),
                        "timestamp": quote_data.get("timestamp"),
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch snapshot for {ticker}: {e}")
                snapshot[ticker] = {
                    "name": name,
                    "error": "Data unavailable",
                }

        self.cache.set(cache_key, snapshot, self.QUOTE_CACHE_TTL)
        return snapshot

    def get_quote(self, ticker: str) -> Dict[str, Any]:
        """
        Retrieve detailed quote for a single security.

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dict with quote details:
            {
                "ticker": "AAPL",
                "name": "Apple Inc.",
                "price": 150.25,
                "change": 2.50,
                "change_pct": 1.69,
                "volume": 45000000,
                "market_cap": 2400000000000,
                "pe_ratio": 28.5,
                "52w_high": 199.62,
                "52w_low": 124.17,
                "dividend_yield": 0.45,
                "sector": "Technology",
                "timestamp": "2026-04-16T14:30:00Z"
            }
        """
        cache_key = f"quote_{ticker.upper()}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        try:
            quote_data = self._fetch_quote_data(ticker)
            if quote_data:
                quote_data["ticker"] = ticker.upper()
                self.cache.set(cache_key, quote_data, self.QUOTE_CACHE_TTL)
                return quote_data
            else:
                return {"error": f"No data found for {ticker}"}
        except Exception as e:
            logger.error(f"Error fetching quote for {ticker}: {e}")
            return {"error": f"Failed to fetch quote: {str(e)}"}

    def get_historical(
        self, ticker: str, period: str = "1M"
    ) -> Dict[str, Any]:
        """
        Retrieve historical OHLCV data for charting.

        Args:
            ticker: Stock ticker symbol
            period: Data period - '1D', '1W', '1M', '3M', '6M', '1Y', '5Y'

        Returns:
            Dict with historical data:
            {
                "ticker": "AAPL",
                "period": "1M",
                "data": [
                    {"date": "2026-04-01", "open": 145.0, "high": 150.0,
                     "low": 144.5, "close": 148.5, "volume": 40000000},
                    ...
                ]
            }
        """
        cache_key = f"historical_{ticker.upper()}_{period}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        # Validate period
        valid_periods = {"1D", "1W", "1M", "3M", "6M", "1Y", "5Y"}
        if period not in valid_periods:
            return {"error": f"Invalid period. Must be one of: {valid_periods}"}

        try:
            # Map period strings to yfinance period parameter
            yf_period_map = {
                "1D": "1d",
                "1W": "5d",
                "1M": "1mo",
                "3M": "3mo",
                "6M": "6mo",
                "1Y": "1y",
                "5Y": "5y",
            }

            ticker_obj = yf.Ticker(ticker)
            hist = ticker_obj.history(period=yf_period_map[period])

            if hist.empty:
                return {"error": f"No historical data found for {ticker}"}

            # Convert to JSON-serializable format
            data = []
            for date, row in hist.iterrows():
                data.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                })

            result = {
                "ticker": ticker.upper(),
                "period": period,
                "data": data,
            }

            self.cache.set(
                cache_key, result, self.HISTORICAL_CACHE_TTL
            )
            return result

        except Exception as e:
            logger.error(f"Error fetching historical data for {ticker}: {e}")
            return {
                "error": f"Failed to fetch historical data: {str(e)}"
            }

    def search_securities(self, query: str) -> Dict[str, Any]:
        """
        Basic ticker search (returns exact match or close matches).

        Args:
            query: Search query (ticker or partial name)

        Returns:
            Dict with search results:
            {
                "query": "AAPL",
                "results": [
                    {"ticker": "AAPL", "name": "Apple Inc."}
                ]
            }

        Note:
            yfinance doesn't provide built-in search. This returns
            what we can verify by ticker lookup. In production, use
            a dedicated search service or Morningstar DWS.
        """
        query_upper = query.upper().strip()

        try:
            # Try direct ticker lookup
            ticker_obj = yf.Ticker(query_upper)
            info = ticker_obj.info

            if info and info.get("symbol"):
                results = [{
                    "ticker": query_upper,
                    "name": info.get("longName", query_upper),
                    "exchange": info.get("exchange", ""),
                }]
                return {"query": query, "results": results}

            # No match
            return {"query": query, "results": []}

        except Exception as e:
            logger.warning(f"Error searching for {query}: {e}")
            return {
                "query": query,
                "results": [],
                "note": "Search requires external service",
            }

    def get_sector_performance(self) -> Dict[str, Any]:
        """
        Retrieve sector ETF performance.

        Returns:
            Dict with sector performance:
            {
                "XLK": {"name": "Technology", "price": 150.0, "change": 2.5, ...},
                ...
            }
        """
        cache_key = "sector_performance"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        sectors = {}
        for ticker, name in self.SECTOR_ETFS.items():
            try:
                quote_data = self._fetch_quote_data(ticker)
                if quote_data:
                    sectors[ticker] = {
                        "name": name,
                        "price": quote_data.get("price"),
                        "change": quote_data.get("change"),
                        "change_pct": quote_data.get("change_pct"),
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch sector data for {ticker}: {e}")
                sectors[ticker] = {"name": name, "error": "Data unavailable"}

        self.cache.set(cache_key, sectors, self.SECTOR_CACHE_TTL)
        return sectors

    def get_market_movers(self) -> Dict[str, Any]:
        """
        Retrieve top gainers and losers from major indices.

        Returns:
            Dict with movers:
            {
                "gainers": [
                    {"ticker": "ABC", "name": "ABC Corp", "change_pct": 5.2},
                    ...
                ],
                "losers": [...]
            }

        Note:
            This is a simplified implementation. In production,
            use a dedicated market data service for real-time movers.
        """
        cache_key = "market_movers"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        # For now, return sector performance as a proxy for movers
        # A real implementation would fetch most active/trending stocks
        sectors = self.get_sector_performance()
        sector_list = [
            {
                "ticker": k,
                "name": v.get("name", k),
                "change_pct": v.get("change_pct", 0),
            }
            for k, v in sectors.items()
            if "error" not in v
        ]

        sector_list.sort(key=lambda x: x["change_pct"], reverse=True)

        gainers = sector_list[:5]
        losers = sector_list[-5:]

        result = {
            "gainers": gainers,
            "losers": losers,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        self.cache.set(cache_key, result, self.QUOTE_CACHE_TTL)
        return result

    def get_news(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve financial news headlines.

        Args:
            ticker: Optional stock ticker for company-specific news.
                    If None, returns general market news.

        Returns:
            Dict with news headlines:
            {
                "ticker": "AAPL",
                "news": [
                    {
                        "title": "Apple announces...",
                        "link": "https://...",
                        "source": "Reuters",
                        "published": "2026-04-16T10:30:00Z"
                    },
                    ...
                ]
            }
        """
        ticker = ticker.upper() if ticker else "general"
        cache_key = f"news_{ticker}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        try:
            if ticker == "general":
                # Use SPY for general market news
                ticker_obj = yf.Ticker("SPY")
            else:
                ticker_obj = yf.Ticker(ticker)

            news_list = ticker_obj.news or []
            news = []

            for item in news_list[:10]:  # Limit to 10 headlines
                try:
                    # Handle both old and new yfinance news formats
                    # Old: {"title":..., "link":..., "source":..., "providerPublishTime":...}
                    # New: {"content": {"title":..., "provider": {"displayName":...}}, "link":...}
                    if "content" in item:
                        content = item.get("content", {})
                        provider = content.get("provider", {})
                        pub_time = content.get("pubDate", "")
                        news.append({
                            "title": content.get("title", ""),
                            "link": content.get("canonicalUrl", {}).get("url", "")
                                    or item.get("link", ""),
                            "source": provider.get("displayName", ""),
                            "published": pub_time,
                        })
                    else:
                        news.append({
                            "title": item.get("title", ""),
                            "link": item.get("link", ""),
                            "source": item.get("source", item.get("publisher", "")),
                            "published": item.get(
                                "providerPublishTime", ""
                            ),
                        })
                except Exception as e:
                    logger.debug(f"Error processing news item: {e}")
                    continue

            result = {
                "ticker": ticker if ticker != "general" else None,
                "news": news,
                "count": len(news),
            }

            self.cache.set(cache_key, result, self.NEWS_CACHE_TTL)
            return result

        except Exception as e:
            logger.error(f"Error fetching news for {ticker}: {e}")
            return {
                "error": f"Failed to fetch news: {str(e)}",
                "news": [],
            }

    # ========================================================================
    # Internal Helper Methods
    # ========================================================================

    def _fetch_quote_data(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Internal method to fetch quote data from yfinance.

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dict with quote data or None if fetch fails
        """
        try:
            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.info

            if not info:
                return None

            # Extract key fields
            # Normalize dividend yield: yfinance sometimes returns as
            # decimal (0.0114) and sometimes as percentage (1.14).
            # We always store as decimal so the frontend can * 100.
            raw_div = info.get("dividendYield")
            if raw_div is not None and raw_div > 1:
                # Likely already a percentage value — convert to decimal
                raw_div = raw_div / 100.0

            quote = {
                "name": info.get("longName", ticker),
                "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "change": self._safe_subtract(
                    info.get("regularMarketPrice"),
                    info.get("regularMarketOpen"),
                ),
                "change_pct": info.get("regularMarketChangePercent"),
                "volume": info.get("volume"),
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "high_52w": info.get("fiftyTwoWeekHigh"),
                "low_52w": info.get("fiftyTwoWeekLow"),
                "dividend_yield": raw_div,
                "sector": info.get("sector"),
                "exchange": info.get("exchange", ""),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

            return quote

        except Exception as e:
            logger.debug(f"Error fetching quote for {ticker}: {e}")
            return None

    @staticmethod
    def _safe_subtract(a: Optional[float], b: Optional[float]) -> Optional[float]:
        """Safely subtract two values, handling None."""
        if a is not None and b is not None:
            return round(a - b, 2)
        return None

    # ========================================================================
    # Morningstar DWS Integration Stub
    # ========================================================================

    def _morningstar_stub(self) -> None:
        """
        Placeholder for Morningstar DWS integration.

        When Morningstar DWS credentials are available, the following
        implementation pattern should be followed:

        TODO: Add Morningstar DWS configuration
        - Store DWS API key in environment variables or secure config
        - Initialize DWS client in __init__() if key is available

        TODO: Implement _fetch_quote_data_morningstar()
        - Call DWS quote endpoint with ticker
        - Map DWS fields to internal quote format
        - Fall back to yfinance if DWS fails

        TODO: Implement _fetch_historical_morningstar()
        - Retrieve historical data from DWS
        - Use DWS data for higher data quality and longer history

        TODO: Add provider selection logic in public methods
        - Check if DWS is available and enabled
        - Use DWS as primary, fall back to yfinance on error
        - Example:
            if self.use_morningstar and self.dws_client:
                data = self._fetch_quote_data_morningstar(ticker)
            else:
                data = self._fetch_quote_data(ticker)

        TODO: Update cache keys to include provider
        - Prevent mixing cached data between providers
        - Example: f"quote_morningstar_{ticker}"

        This pluggable approach allows seamless provider swapping
        without refactoring the rest of the application.
        """
        pass


# ============================================================================
# Flask Blueprint & Endpoints
# ============================================================================

market_bp = Blueprint(
    "market",
    __name__,
    url_prefix="/api/markets",
)

# Global client instance (set by init_market_data)
_market_client: Optional[MarketDataClient] = None


def _get_client() -> MarketDataClient:
    """Get the global market data client."""
    if _market_client is None:
        raise RuntimeError("Market data client not initialized")
    return _market_client


def _add_cors_headers(response):
    """Add CORS headers to response."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def _handle_errors(f):
    """Decorator to handle errors and add CORS headers."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            response = jsonify(f(*args, **kwargs))
            return _add_cors_headers(response)
        except Exception as e:
            logger.error(f"Endpoint error: {e}")
            response = jsonify({"error": str(e)})
            response.status_code = 500
            return _add_cors_headers(response)

    return decorated_function


@market_bp.route("/snapshot", methods=["GET", "OPTIONS"])
@_handle_errors
def snapshot():
    """
    GET /api/markets/snapshot

    Returns market snapshot as {"data": [{ticker, name, price, change, ...}]}
    """
    client = _get_client()
    raw = client.get_market_snapshot()
    # Convert dict-by-ticker to array with ticker field
    data = []
    for ticker_key, values in raw.items():
        if "error" not in values:
            item = {"ticker": ticker_key}
            item.update(values)
            data.append(item)
    return {"data": data}


@market_bp.route("/quote/<ticker>", methods=["GET", "OPTIONS"])
@_handle_errors
def quote(ticker):
    """
    GET /api/markets/quote/<ticker>

    Returns {"data": {ticker, name, price, change, ...}}
    """
    client = _get_client()
    raw = client.get_quote(ticker)
    if "error" in raw:
        return raw
    return {"data": raw}


@market_bp.route("/historical/<ticker>", methods=["GET", "OPTIONS"])
@_handle_errors
def historical(ticker):
    """
    GET /api/markets/historical/<ticker>?period=1M

    Returns {"data": [{date, open, high, low, close, volume}]}
    """
    period = request.args.get("period", "1M").upper()
    client = _get_client()
    raw = client.get_historical(ticker, period)
    if "error" in raw:
        return raw
    # raw already has {"ticker":..., "period":..., "data":[...]}
    # Frontend expects response.data to be the array
    return {"data": raw.get("data", [])}


@market_bp.route("/sectors", methods=["GET", "OPTIONS"])
@_handle_errors
def sectors():
    """
    GET /api/markets/sectors

    Returns {"data": [{ticker, name, price, change, change_pct}]}
    """
    client = _get_client()
    raw = client.get_sector_performance()
    # Convert dict-by-ticker to array
    data = []
    for ticker_key, values in raw.items():
        if "error" not in values:
            item = {"ticker": ticker_key}
            item.update(values)
            data.append(item)
    return {"data": data}


@market_bp.route("/movers", methods=["GET", "OPTIONS"])
@_handle_errors
def movers():
    """
    GET /api/markets/movers

    Returns {"data": {"gainers": [...], "losers": [...]}}
    """
    client = _get_client()
    raw = client.get_market_movers()
    # Add price field to movers (frontend expects it)
    for mover in raw.get("gainers", []):
        if "price" not in mover:
            # Fetch price from sector data
            sector_data = client.get_sector_performance()
            ticker_data = sector_data.get(mover["ticker"], {})
            mover["price"] = ticker_data.get("price", 0)
    for mover in raw.get("losers", []):
        if "price" not in mover:
            sector_data = client.get_sector_performance()
            ticker_data = sector_data.get(mover["ticker"], {})
            mover["price"] = ticker_data.get("price", 0)
    return {"data": raw}


@market_bp.route("/news", methods=["GET", "OPTIONS"])
@_handle_errors
def news():
    """
    GET /api/markets/news?ticker=AAPL

    Returns {"data": [{title, link, publisher, published}]}
    """
    ticker = request.args.get("ticker", None)
    client = _get_client()
    raw = client.get_news(ticker)
    # Transform news array: rename 'source' -> 'publisher', flatten into data
    news_items = raw.get("news", [])
    data = []
    for item in news_items:
        data.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "publisher": item.get("source", ""),
            "published": item.get("published", ""),
        })
    return {"data": data}


@market_bp.route("/search", methods=["GET", "OPTIONS"])
@_handle_errors
def search():
    """
    GET /api/markets/search?q=AAPL

    Returns {"data": [{ticker, name, exchange}]}
    """
    query = request.args.get("q", "").strip()
    if not query:
        return {"error": "Missing query parameter 'q'"}

    client = _get_client()
    raw = client.search_securities(query)
    # Transform results to data array with exchange field
    results = raw.get("results", [])
    data = []
    for item in results:
        data.append({
            "ticker": item.get("ticker", ""),
            "name": item.get("name", ""),
            "exchange": item.get("exchange", "NYSE"),
        })
    return {"data": data}


# ============================================================================
# Initialization
# ============================================================================

def init_market_data(app) -> None:
    """
    Initialize the market data module.

    This function should be called during Flask app initialization:

    Example:
        from market_data import init_market_data

        app = Flask(__name__)
        init_market_data(app)

    Args:
        app: Flask application instance
    """
    global _market_client

    logger.info("Initializing market data module")

    # Create client instance
    _market_client = MarketDataClient()

    # Register Blueprint
    app.register_blueprint(market_bp)

    # Store client on app for access if needed
    app.market_data_client = _market_client

    logger.info("Market data module initialized successfully")


if __name__ == "__main__":
    # Simple test when run directly
    logging.basicConfig(level=logging.INFO)

    client = MarketDataClient()

    print("\n=== Market Snapshot ===")
    print(json.dumps(client.get_market_snapshot(), indent=2, default=str))

    print("\n=== Apple Quote ===")
    print(json.dumps(client.get_quote("AAPL"), indent=2, default=str))

    print("\n=== Sector Performance ===")
    print(
        json.dumps(
            client.get_sector_performance(), indent=2, default=str
        )
    )

    print("\n=== Market Movers ===")
    print(json.dumps(client.get_market_movers(), indent=2, default=str))
