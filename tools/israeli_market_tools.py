"""
tools/israeli_market_tools.py — Israeli financial market data fetcher.

Data sources (all free, no API key required):
  • Bank of Israel (boi.org.il) — foreign exchange rates via public XML feed
  • Yahoo Finance API           — TASE stock quotes (symbol + ".TA") and indices

Tools:
  get_exchange_rates()              → ILS rates for major currencies from Bank of Israel
  get_tase_stock_quote(symbol)      → Quote for a TASE-listed stock
  get_tase_index(index_name)        → Current value of a major TASE index
"""

import logging
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_BOI_RATES_URL = "https://www.boi.org.il/currency.xml"
_YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}
_REQUEST_TIMEOUT = 15

# Yahoo Finance symbols for major TASE indices
_TASE_INDEX_SYMBOLS: Dict[str, str] = {
    "TA-35": "^TA35.TA",
    "TA-90": "^TA90.TA",
    "TA-125": "^TA125.TA",
    "TA-SME60": "^TASMC60.TA",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_boi_xml() -> ElementTree.Element:
    """Fetch and parse the Bank of Israel currency XML feed."""
    try:
        resp = requests.get(_BOI_RATES_URL, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Bank of Israel rates: {exc}") from exc

    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"Failed to parse Bank of Israel XML: {exc}") from exc

    return root


def _yf_quote(symbol: str) -> Dict[str, Any]:
    """
    Fetch a single quote from Yahoo Finance chart API.

    Returns a dict with: symbol, name, currency, price, prev_close,
    change, change_pct, day_high, day_low, volume, market_cap, timestamp.
    """
    url = _YF_CHART_URL.format(symbol=symbol)
    try:
        resp = requests.get(
            url,
            headers=_YF_HEADERS,
            params={"interval": "1d", "range": "5d"},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Yahoo Finance data for {symbol}: {exc}") from exc

    data = resp.json()
    result = data.get("chart", {}).get("result")
    if not result:
        error = data.get("chart", {}).get("error") or {}
        raise ValueError(
            f"No data returned for symbol '{symbol}'. "
            f"Yahoo Finance error: {error.get('description', 'unknown')}. "
            "Check that the symbol is correct (TASE stocks use a '.TA' suffix)."
        )

    chart = result[0]
    meta = chart.get("meta", {})
    indicators = chart.get("indicators", {})
    quote_list = indicators.get("quote", [{}])
    closes = quote_list[0].get("close", []) if quote_list else []

    # Use the most recent valid close price
    price = None
    for c in reversed(closes):
        if c is not None:
            price = round(c, 4)
            break

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    regular_price = meta.get("regularMarketPrice") or price

    change = None
    change_pct = None
    if regular_price is not None and prev_close:
        change = round(regular_price - prev_close, 4)
        change_pct = round((change / prev_close) * 100, 2)

    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency", "ILS"),
        "price": regular_price,
        "prev_close": round(prev_close, 4) if prev_close else None,
        "change": change,
        "change_pct": change_pct,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "market_cap": meta.get("marketCap"),
        "exchange": meta.get("exchangeName", "TASE"),
        "market_state": meta.get("marketState"),
    }


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------

@tool
def get_exchange_rates(currencies: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Fetch current foreign exchange rates from the Bank of Israel.

    Returns ILS-based exchange rates published by the Bank of Israel for today.
    All rates express how many ILS one unit of the foreign currency is worth.

    Args:
        currencies: Optional list of ISO currency codes to filter by
                    (e.g. ["USD", "EUR", "GBP"]).
                    If omitted, all available rates are returned.

    Returns:
        A dict with:
          - last_update: date of the rates (YYYY-MM-DD)
          - rates: dict mapping currency code → {name, country, unit, rate, change}
            where `rate` is the ILS value per `unit` of that currency,
            and `change` is the daily percentage change.
    """
    root = _fetch_boi_xml()

    last_update_el = root.find("LAST_UPDATE")
    last_update = last_update_el.text.strip() if last_update_el is not None else None

    filter_set = {c.upper() for c in currencies} if currencies else None

    rates: Dict[str, Any] = {}
    for currency_el in root.findall("CURRENCY"):
        code_el = currency_el.find("CURRENCYCODE")
        if code_el is None:
            continue
        code = (code_el.text or "").strip().upper()
        if filter_set and code not in filter_set:
            continue

        def _text(tag: str) -> Optional[str]:
            el = currency_el.find(tag)
            return el.text.strip() if el is not None and el.text else None

        rate_str = _text("RATE")
        change_str = _text("CHANGE")
        unit_str = _text("UNIT")

        rates[code] = {
            "name": _text("NAME"),
            "country": _text("COUNTRY"),
            "unit": int(unit_str) if unit_str and unit_str.isdigit() else 1,
            "rate_ils": float(rate_str) if rate_str else None,
            "daily_change_pct": float(change_str) if change_str else None,
        }

    return {"last_update": last_update, "rates": rates}


@tool
def get_tase_stock_quote(symbol: str) -> Dict[str, Any]:
    """
    Get the current market quote for a stock traded on the Tel Aviv Stock Exchange (TASE).

    Fetches real-time price data from Yahoo Finance using the TASE symbol convention.
    The '.TA' suffix (e.g. "TEVA.TA") is appended automatically if not already present.

    Args:
        symbol: TASE ticker symbol (e.g. "TEVA", "NICE", "CHKP", "ICL", "BEZQ").
                Case-insensitive. Do NOT include the exchange suffix.

    Returns:
        A dict with:
          - symbol: full ticker (e.g. "TEVA.TA")
          - name: company name
          - currency: trading currency (usually "ILS")
          - price: latest price
          - prev_close: previous close price
          - change: absolute price change today
          - change_pct: percentage change today
          - day_high / day_low: intraday range
          - volume: trading volume
          - market_cap: market capitalisation (if available)
          - exchange: exchange name
          - market_state: "REGULAR", "PRE", "POST", or "CLOSED"
    """
    cleaned = symbol.strip().upper()
    if not cleaned.endswith(".TA"):
        cleaned = cleaned + ".TA"

    return _yf_quote(cleaned)


@tool
def get_tase_index(index_name: str = "TA-35") -> Dict[str, Any]:
    """
    Get the current value of a major Tel Aviv Stock Exchange (TASE) index.

    Args:
        index_name: Index name. Supported values:
                    "TA-35"    — 35 largest companies on TASE (blue-chip)
                    "TA-90"    — next 90 largest companies
                    "TA-125"   — combined TA-35 + TA-90
                    "TA-SME60" — 60 small/mid-cap companies

    Returns:
        A dict with:
          - index: index name
          - symbol: Yahoo Finance ticker used
          - price: current index value
          - prev_close: previous close
          - change: absolute point change
          - change_pct: percentage change
          - day_high / day_low: intraday range
          - market_state: "REGULAR", "PRE", "POST", or "CLOSED"
    """
    key = index_name.upper().replace(" ", "-")
    yf_symbol = _TASE_INDEX_SYMBOLS.get(key)
    if yf_symbol is None:
        supported = ", ".join(_TASE_INDEX_SYMBOLS.keys())
        raise ValueError(
            f"Unknown index '{index_name}'. Supported indices: {supported}."
        )

    quote = _yf_quote(yf_symbol)
    return {
        "index": index_name,
        "symbol": yf_symbol,
        "price": quote["price"],
        "prev_close": quote["prev_close"],
        "change": quote["change"],
        "change_pct": quote["change_pct"],
        "day_high": quote["day_high"],
        "day_low": quote["day_low"],
        "market_state": quote["market_state"],
    }
