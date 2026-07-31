"""
news/ingestion.py
-----------------
News ingestion service for the Indian F&O trading agent.

Aggregates financial news from three sources:
  1. NewsAPI.org  – structured JSON API
  2. Economic Times RSS  – RSS/Atom feed
  3. MoneyControl RSS    – RSS/Atom feed

Articles are deduplicated by MD5 hash of their normalised headline and
filtered to only include items published in the last 24 hours.

Symbol extraction uses keyword matching against a curated list of NSE
index and company names.

Author: F&O Trading Agent
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
import pytz
import xml.etree.ElementTree as ET

from models.news import NewsEvent, NewsSource

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ------------------------------------------------------------------
# Source URLs
# ------------------------------------------------------------------
NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"
NEWSAPI_QUERY = "NSE OR NIFTY OR \"Indian stock market\" OR BSE OR F&O"
ECONOMIC_TIMES_RSS = "https://economictimes.indiatimes.com/markets/rss.cms"
MONEYCONTROL_RSS = "https://www.moneycontrol.com/rss/latestnews.xml"

# ------------------------------------------------------------------
# Symbol keyword mapping  (add more as needed)
# Covers major indices and Nifty 50 / BankNifty constituents
# ------------------------------------------------------------------
SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "NIFTY": ["nifty 50", "nifty50", "nifty index", "nse 50"],
    "BANKNIFTY": ["banknifty", "bank nifty", "nifty bank"],
    "FINNIFTY": ["finnifty", "fin nifty", "nifty financial"],
    "MIDCPNIFTY": ["midcap nifty", "midcpnifty", "nifty midcap"],
    "SENSEX": ["sensex", "bse 30", "bse30"],
    # Large cap equities (partial list – extend as needed)
    "RELIANCE": ["reliance industries", "ril"],
    "TCS": ["tcs", "tata consultancy"],
    "HDFCBANK": ["hdfc bank", "hdfcbank"],
    "INFY": ["infosys", "infy"],
    "ICICIBANK": ["icici bank", "icicibank"],
    "SBIN": ["state bank", "sbi", "sbin"],
    "BHARTIARTL": ["bharti airtel", "airtel"],
    "ITC": [" itc "],
    "KOTAKBANK": ["kotak mahindra bank", "kotak bank"],
    "LT": ["larsen & toubro", "l&t", "larsen and toubro"],
    "HINDUNILVR": ["hindustan unilever", "hul"],
    "AXISBANK": ["axis bank"],
    "WIPRO": ["wipro"],
    "ASIANPAINT": ["asian paints"],
    "MARUTI": ["maruti suzuki", "maruti"],
    "TITAN": ["titan company"],
    "BAJFINANCE": ["bajaj finance"],
    "BAJAJFINSV": ["bajaj finserv"],
    "NTPC": ["ntpc"],
    "POWERGRID": ["power grid"],
    "ONGC": ["ongc", "oil and natural gas"],
    "COALINDIA": ["coal india"],
    "M&M": ["mahindra & mahindra", "m&m"],
    "TATAMOTORS": ["tata motors"],
    "TATASTEEL": ["tata steel"],
    "ADANIPORTS": ["adani ports", "adani enterprises"],
    "SUNPHARMA": ["sun pharma", "sun pharmaceutical"],
    "DRREDDY": ["dr reddy", "dr. reddy"],
    "CIPLA": ["cipla"],
    "HCLTECH": ["hcl tech", "hcltech"],
    "TECHM": ["tech mahindra"],
    "ULTRACEMCO": ["ultratech cement"],
    "JSWSTEEL": ["jsw steel"],
    "INDUSINDBK": ["indusind bank"],
    "DIVISLAB": ["divi's laboratories", "divislab"],
    "BPCL": ["bharat petroleum", "bpcl"],
    "HEROMOTOCO": ["hero motocorp", "hero moto"],
    "EICHERMOT": ["eicher motors"],
    "BRITANNIA": ["britannia"],
    "SHREECEM": ["shree cement"],
    "GRASIM": ["grasim"],
    "APOLLOHOSP": ["apollo hospitals"],
}


class NewsIngestionService:
    """
    Async news ingestion service that aggregates articles from multiple
    financial news sources, deduplicates them via Redis, and extracts
    relevant NSE trading symbols from each article.

    Parameters
    ----------
    news_api_key : str
        API key for NewsAPI.org.
    redis_client : redis.asyncio.Redis
        Async Redis client for headline deduplication.
    http_timeout : int
        HTTP request timeout in seconds (default 15).
    dedup_ttl : int
        TTL in seconds for deduplication keys in Redis (default 86400 = 24 h).
    """

    def __init__(
        self,
        news_api_key: str,
        redis_client: Any,
        http_timeout: int = 15,
        dedup_ttl: int = 86_400,
    ) -> None:
        self._api_key = news_api_key
        self._redis = redis_client
        self._timeout = aiohttp.ClientTimeout(total=http_timeout)
        self._dedup_ttl = dedup_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_latest(
        self, symbols: list[str] | None = None
    ) -> list[NewsEvent]:
        """
        Fetch and aggregate the latest financial news from all sources.

        Runs all three source fetchers concurrently, deduplicates by
        headline hash, filters to the last 24 hours, and extracts
        relevant symbols from each article.

        Parameters
        ----------
        symbols : list[str] | None
            Optional list of NSE symbols to filter by. If None, all
            extracted symbols are returned.

        Returns
        -------
        list[NewsEvent]
            Deduplicated, time-filtered list of news events sorted by
            published date (newest first).
        """
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            results = await asyncio.gather(
                self._fetch_newsapi(session),
                self._fetch_rss(session, ECONOMIC_TIMES_RSS, NewsSource.ECONOMIC_TIMES),
                self._fetch_rss(session, MONEYCONTROL_RSS, NewsSource.MONEYCONTROL),
                return_exceptions=True,
            )

        all_events: list[NewsEvent] = []
        for i, result in enumerate(results):
            source_name = ["NewsAPI", "Economic Times RSS", "MoneyControl RSS"][i]
            if isinstance(result, BaseException):
                logger.error("Failed to fetch from %s: %s", source_name, result)
                continue
            all_events.extend(result)

        # Deduplicate, time-filter, symbol-extract
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        unique_events: list[NewsEvent] = []

        for event in all_events:
            # Time filter
            if event.published_at and event.published_at < cutoff:
                continue

            # Deduplication via Redis
            if await self._is_duplicate(event.headline):
                continue

            # Symbol extraction
            event.symbols = self._extract_symbols(
                f"{event.headline} {event.content or ''}"
            )

            # Apply symbol filter if requested
            if symbols:
                if not any(s in event.symbols for s in symbols):
                    continue

            unique_events.append(event)
            await self._mark_seen(event.headline)

        # Sort newest-first
        unique_events.sort(
            key=lambda e: e.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        logger.info(
            "fetch_latest: %d raw events -> %d unique after dedup/filter",
            len(all_events),
            len(unique_events),
        )
        return unique_events

    # ------------------------------------------------------------------
    # Source fetchers
    # ------------------------------------------------------------------

    async def _fetch_newsapi(self, session: aiohttp.ClientSession) -> list[NewsEvent]:
        """
        Fetch articles from NewsAPI.org.

        Uses the /v2/everything endpoint with a market-focused query,
        sorted by publication date, limited to English-language sources.

        Parameters
        ----------
        session : aiohttp.ClientSession
            Shared HTTP session.

        Returns
        -------
        list[NewsEvent]
            Parsed NewsEvent objects from the API response.
        """
        params = {
            "q": NEWSAPI_QUERY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 50,
            "apiKey": self._api_key,
        }

        logger.debug("Fetching NewsAPI: query=%r", NEWSAPI_QUERY)
        async with session.get(NEWSAPI_BASE_URL, params=params) as resp:
            resp.raise_for_status()
            data: dict = await resp.json()

        articles = data.get("articles", [])
        events: list[NewsEvent] = []

        for article in articles:
            headline = (article.get("title") or "").strip()
            if not headline or headline.lower() == "[removed]":
                continue

            published_at: datetime | None = None
            pub_str = article.get("publishedAt")
            if pub_str:
                try:
                    published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            events.append(
                NewsEvent(
                    headline=headline,
                    content=(article.get("description") or "").strip(),
                    url=article.get("url", ""),
                    source=NewsSource.NEWSAPI,
                    source_name=article.get("source", {}).get("name", "NewsAPI"),
                    published_at=published_at,
                    symbols=[],
                )
            )

        logger.info("NewsAPI: fetched %d articles", len(events))
        return events

    async def _fetch_rss(
        self,
        session: aiohttp.ClientSession,
        url: str,
        source: "NewsSource",
    ) -> list[NewsEvent]:
        """
        Fetch and parse an RSS/Atom feed.

        Parameters
        ----------
        session : aiohttp.ClientSession
            Shared HTTP session.
        url : str
            Full URL of the RSS feed.
        source : NewsSource
            Enum value identifying the source.

        Returns
        -------
        list[NewsEvent]
            Parsed NewsEvent objects from the feed.
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; FNONewsBot/1.0; +https://github.com/fno-agent)"
            )
        }

        logger.debug("Fetching RSS: url=%s", url)
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            raw_bytes = await resp.read()

        # Parse XML safely
        root = ET.fromstring(raw_bytes)

        # Handle both RSS 2.0 and Atom namespace
        # RSS 2.0: channel/item; Atom: feed/entry
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        events: list[NewsEvent] = []
        for item in items:
            # Headline
            title_el = item.find("title") or item.find("atom:title", ns)
            headline = (title_el.text or "").strip() if title_el is not None else ""
            if not headline:
                continue

            # Strip CDATA wrappers if present
            headline = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", headline, flags=re.DOTALL).strip()

            # Description / summary
            desc_el = (
                item.find("description")
                or item.find("atom:summary", ns)
                or item.find("atom:content", ns)
            )
            content = ""
            if desc_el is not None and desc_el.text:
                content = re.sub(r"<[^>]+>", "", desc_el.text).strip()  # Strip HTML tags

            # URL
            link_el = item.find("link") or item.find("atom:link", ns)
            url_str = ""
            if link_el is not None:
                url_str = (link_el.text or link_el.get("href", "")).strip()

            # Published date
            published_at: datetime | None = None
            for tag in ("pubDate", "atom:published", "atom:updated", "dc:date"):
                date_el = item.find(tag, {**ns, "dc": "http://purl.org/dc/elements/1.1/"})
                if date_el is not None and date_el.text:
                    try:
                        published_at = parsedate_to_datetime(date_el.text.strip())
                        break
                    except Exception:  # noqa: BLE001
                        try:
                            published_at = datetime.fromisoformat(date_el.text.strip())
                            break
                        except Exception:  # noqa: BLE001
                            continue

            events.append(
                NewsEvent(
                    headline=headline,
                    content=content,
                    url=url_str,
                    source=source,
                    source_name=source.value,
                    published_at=published_at,
                    symbols=[],
                )
            )

        logger.info("RSS %s: fetched %d items", url, len(events))
        return events

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _headline_hash(headline: str) -> str:
        """
        Compute an MD5 hash of a normalised headline for deduplication.

        Normalisation: lowercase, strip punctuation, collapse whitespace.
        """
        normalised = re.sub(r"[^a-z0-9 ]", "", headline.lower())
        normalised = re.sub(r"\s+", " ", normalised).strip()
        return hashlib.md5(normalised.encode("utf-8")).hexdigest()  # noqa: S324

    async def _is_duplicate(self, headline: str) -> bool:
        """
        Return True if this headline has already been processed today.

        Uses a Redis key ``news_dedup:{hash}`` with a 24-hour TTL.
        """
        key = f"news_dedup:{self._headline_hash(headline)}"
        exists = await self._redis.exists(key)
        return bool(exists)

    async def _mark_seen(self, headline: str) -> None:
        """Mark a headline hash as seen in Redis with the configured TTL."""
        key = f"news_dedup:{self._headline_hash(headline)}"
        await self._redis.set(key, "1", ex=self._dedup_ttl)

    # ------------------------------------------------------------------
    # Symbol extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_symbols(text: str) -> list[str]:
        """
        Extract NSE trading symbols mentioned in a piece of text.

        Uses case-insensitive keyword matching against the
        ``SYMBOL_KEYWORDS`` map.

        Parameters
        ----------
        text : str
            Combined headline + article body text.

        Returns
        -------
        list[str]
            Deduplicated list of NSE symbol strings found in the text.
        """
        text_lower = text.lower()
        found: list[str] = []

        for symbol, keywords in SYMBOL_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    found.append(symbol)
                    break  # Found at least one keyword for this symbol

        return list(dict.fromkeys(found))  # Preserve order, deduplicate
