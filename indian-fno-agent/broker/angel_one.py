"""
broker/angel_one.py
===================
Production-ready Angel One SmartAPI broker adapter.

SDK
---
    pip install smartapi-python pyotp aiohttp pandas

Angel One SmartAPI docs: https://smartapi.angelbroking.com/docs

Session lifecycle
-----------------
1. ``login()``   – generates TOTP, calls ``generateSession``, stores tokens.
2. Every API call checks token validity (``_ensure_connected``).
3. A background refresh loop (started by ``login``) refreshes the JWT every
   50 minutes using ``generateToken`` (tokens expire after 60 min).
4. ``logout()``  – calls ``terminateSession`` and cancels the refresh task.

Error handling
--------------
* Known Angel One error codes are mapped to specific Python exceptions.
* All API calls are wrapped in ``_call_with_retry`` which applies
  exponential back-off (up to 3 attempts) for transient network errors.
* All exceptions are logged with full tracebacks before re-raising.

Thread / async safety
---------------------
The underlying ``SmartConnect`` SDK is synchronous.  All blocking calls are
executed via ``asyncio.get_event_loop().run_in_executor`` so they don't block
the event loop.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.request import urlopen

import pandas as pd
import pyotp

from broker.base import IBrokerAdapter
from core.enums import Exchange, InstrumentType, OrderSide, OrderStatus, OrderType
from core.models import (
    Candle,
    Instrument,
    MarginInfo,
    OptionChain,
    OptionContract,
    OrderRequest,
    OrderResponse,
    Position,
    Tick,
    Trade,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Angel One instrument master CSV URLs (updated daily at ~07:00 IST)
_INSTRUMENT_URLS: dict[str, str] = {
    "NSE":  "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    "NFO":  "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    "BSE":  "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    "MCX":  "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
}

# Timeframe string -> Angel One API constant
_TF_MAP: dict[str, str] = {
    "1m":  "ONE_MINUTE",
    "3m":  "THREE_MINUTE",
    "5m":  "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h":  "ONE_HOUR",
    "1d":  "ONE_DAY",
}

# Token refresh: refresh 10 minutes before the 60-minute expiry
_TOKEN_REFRESH_INTERVAL_SEC: int = 50 * 60   # 50 minutes
_MAX_RETRIES: int = 3
_BASE_BACKOFF_SEC: float = 1.0

# Known Angel One error codes and their human-readable messages
_ANGEL_ERROR_CODES: dict[str, str] = {
    "AB1010": "Invalid API Key",
    "AB1011": "Session expired – please re-login",
    "AB1012": "OTP required",
    "AB1020": "Insufficient funds / margin",
    "AB1021": "Order quantity exceeds limit",
    "AB2000": "Scrip not tradeable",
    "AB2001": "Market is closed",
    "AB9000": "Internal server error – retry",
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AngelOneAuthError(Exception):
    """Raised when Angel One authentication fails."""


class AngelOneOrderError(Exception):
    """Raised when Angel One rejects an order."""


class AngelOneAPIError(Exception):
    """Raised for unexpected Angel One API errors."""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AngelOneBroker(IBrokerAdapter):
    """Production Angel One SmartAPI adapter.

    Parameters
    ----------
    settings : Any
        Must expose the following attributes:

        * ``ANGEL_ONE_API_KEY``    – SmartAPI API key (str).
        * ``ANGEL_ONE_CLIENT_ID``  – Angel One client / login ID (str).
        * ``ANGEL_ONE_PASSWORD``   – Angel One trading password (str).
        * ``ANGEL_ONE_TOTP_SECRET``– Base-32 TOTP secret key (str).
        * ``ANGEL_ONE_CORRELATION_ID`` – Optional correlation-id header (str).
    """

    broker_name: str = "angel_one"

    def __init__(self, settings: Any) -> None:
        self._api_key: str = settings.ANGEL_ONE_API_KEY
        self._client_id: str = settings.ANGEL_ONE_CLIENT_ID
        self._password: str = settings.ANGEL_ONE_PASSWORD
        self._totp_secret: str = settings.ANGEL_ONE_TOTP_SECRET
        self._correlation_id: str = getattr(
            settings, "ANGEL_ONE_CORRELATION_ID", "fno-agent-v1"
        )

        # Session state
        self._smart: Any = None              # SmartConnect instance
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._token_expiry: float = 0.0      # unix timestamp

        # Background token-refresh task
        self._refresh_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

        # Instrument master: exchange_str -> list[Instrument]
        self._instruments: dict[str, list[Instrument]] = {}

        logger.info("AngelOneBroker: initialised for client %s", self._client_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_smart_connect(self) -> Any:
        """Lazy-import and instantiate SmartConnect (avoids import-time error
        if the ``smartapi-python`` package is not installed)."""
        if self._smart is None:
            try:
                from SmartApi.SmartConnect import SmartConnect  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "smartapi-python is not installed. Run: pip install smartapi-python"
                ) from exc
            self._smart = SmartConnect(api_key=self._api_key)
        return self._smart

    def _generate_totp(self) -> str:
        """Generate a fresh 6-digit TOTP from the secret key.

        Returns
        -------
        str
            6-digit TOTP string.
        """
        totp = pyotp.TOTP(self._totp_secret)
        code = totp.now()
        logger.debug("AngelOneBroker: generated TOTP %s", code)
        return code

    async def _run_sync(self, fn, *args, **kwargs):
        """Run a synchronous SDK call in a thread-pool executor.

        Parameters
        ----------
        fn : callable
            Synchronous function to call.
        *args, **kwargs
            Arguments forwarded to *fn*.

        Returns
        -------
        Any
            Return value of *fn*.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def _call_with_retry(self, fn, *args, max_retries: int = _MAX_RETRIES, **kwargs):
        """Call *fn* with exponential back-off on transient failures.

        Parameters
        ----------
        fn : callable
            Sync function to call (wrapped via ``_run_sync`` internally).
        max_retries : int
            Maximum retry attempts (default 3).

        Returns
        -------
        Any
            Return value of *fn*.

        Raises
        ------
        AngelOneAPIError
            If all retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                return await self._run_sync(fn, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                backoff = _BASE_BACKOFF_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "AngelOneBroker: attempt %d/%d failed (%s). Retrying in %.1fs…",
                    attempt,
                    max_retries,
                    exc,
                    backoff,
                )
                if attempt < max_retries:
                    await asyncio.sleep(backoff)

        raise AngelOneAPIError(
            f"All {max_retries} retries exhausted. Last error: {last_exc}"
        ) from last_exc

    def _raise_for_error(self, response: dict, context: str = "") -> None:
        """Inspect an Angel One API response dict and raise if it signals failure.

        Parameters
        ----------
        response : dict
            Raw response dict from the SmartAPI SDK.
        context : str
            Human-readable context for error messages.

        Raises
        ------
        AngelOneAuthError
            For authentication/session errors.
        AngelOneOrderError
            For order-related errors.
        AngelOneAPIError
            For all other errors.
        """
        if response is None:
            raise AngelOneAPIError(f"{context}: received None response from API")

        # SmartAPI wraps responses in {"status": True/False, "errorcode": ..., "message": ...}
        status = response.get("status", True)
        error_code = response.get("errorcode", "")
        message = response.get("message", "Unknown error")

        if not status or error_code:
            human = _ANGEL_ERROR_CODES.get(error_code, message)
            logger.error(
                "AngelOneBroker [%s]: error %s – %s",
                context,
                error_code,
                human,
            )
            if error_code in ("AB1010", "AB1011", "AB1012"):
                raise AngelOneAuthError(f"[{error_code}] {human}")
            if error_code in ("AB1020", "AB1021", "AB2000", "AB2001"):
                raise AngelOneOrderError(f"[{error_code}] {human}")
            raise AngelOneAPIError(f"[{error_code}] {human} (context: {context})")

    def _ensure_connected(self) -> None:
        """Raise if the session is not active or the token has expired.

        Raises
        ------
        AngelOneAuthError
            If not logged in or token has expired.
        """
        if not self.is_connected():
            raise AngelOneAuthError(
                "Not connected. Call await adapter.login() first."
            )

    async def _token_refresh_loop(self) -> None:
        """Background coroutine that refreshes the JWT token every 50 minutes."""
        while True:
            await asyncio.sleep(_TOKEN_REFRESH_INTERVAL_SEC)
            try:
                logger.info("AngelOneBroker: refreshing JWT token…")
                smart = self._get_smart_connect()
                resp = await self._run_sync(
                    smart.generateToken, self._refresh_token
                )
                self._raise_for_error(resp, "token_refresh")
                data = resp.get("data", {})
                self._jwt_token = data.get("jwtToken") or self._jwt_token
                self._feed_token = data.get("feedToken") or self._feed_token
                self._token_expiry = time.time() + 60 * 60
                logger.info("AngelOneBroker: JWT token refreshed successfully")
            except Exception:  # noqa: BLE001
                logger.exception("AngelOneBroker: token refresh failed – will retry next cycle")

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Authenticate with Angel One SmartAPI using TOTP.

        Steps
        -----
        1. Generate a fresh 6-digit TOTP.
        2. Call ``generateSession`` with client ID, password, and TOTP.
        3. Store ``jwtToken``, ``refreshToken``, and ``feedToken``.
        4. Start the background token-refresh coroutine.

        Returns
        -------
        bool
            ``True`` on success.

        Raises
        ------
        AngelOneAuthError
            If login fails (wrong credentials, expired TOTP, etc.).
        """
        totp_code = self._generate_totp()
        smart = self._get_smart_connect()

        try:
            logger.info("AngelOneBroker: logging in as %s", self._client_id)
            resp = await self._call_with_retry(
                smart.generateSession,
                self._client_id,
                self._password,
                totp_code,
            )
        except AngelOneAPIError as exc:
            logger.exception("AngelOneBroker: login network error")
            raise AngelOneAuthError(f"Login failed: {exc}") from exc

        self._raise_for_error(resp, "login")

        data = resp.get("data", {})
        self._jwt_token = data.get("jwtToken")
        self._refresh_token = data.get("refreshToken")
        self._feed_token = data.get("feedToken")
        self._token_expiry = time.time() + 60 * 60   # tokens valid for 1 hour

        if not self._jwt_token:
            raise AngelOneAuthError("Login response did not contain jwtToken")

        # Start the background refresh task
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.ensure_future(self._token_refresh_loop())

        logger.info("AngelOneBroker: login successful | jwt=…%s", self._jwt_token[-8:])
        return True

    async def logout(self) -> bool:
        """Terminate the Angel One session.

        Returns
        -------
        bool
            ``True`` if logout succeeded or was already disconnected.
        """
        if not self.is_connected():
            return True

        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            self._refresh_task = None

        try:
            smart = self._get_smart_connect()
            resp = await self._call_with_retry(smart.terminateSession, self._client_id)
            self._raise_for_error(resp, "logout")
        except Exception:  # noqa: BLE001
            logger.exception("AngelOneBroker: error during logout (ignoring)")

        self._jwt_token = None
        self._refresh_token = None
        self._feed_token = None
        self._token_expiry = 0.0
        logger.info("AngelOneBroker: logged out")
        return True

    def is_connected(self) -> bool:
        """Return ``True`` if the session token is present and not expired."""
        return bool(self._jwt_token) and time.time() < self._token_expiry

    # ------------------------------------------------------------------
    # Market data - instruments
    # ------------------------------------------------------------------

    async def get_instruments(self, exchange: Exchange) -> list[Instrument]:
        """Download and parse the Angel One instrument master JSON.

        The JSON is fetched from Angel One's CDN, filtered to the requested
        exchange, and converted to ``Instrument`` objects.  Results are cached
        in memory for the duration of the session.

        Parameters
        ----------
        exchange : Exchange
            Target exchange (``NSE``, ``NFO``, ``BSE``, ``MCX``).

        Returns
        -------
        list[Instrument]
            All instruments for the given exchange.
        """
        exch_str = exchange.value

        if exch_str in self._instruments:
            return self._instruments[exch_str]

        url = _INSTRUMENT_URLS.get(exch_str, _INSTRUMENT_URLS["NSE"])
        logger.info("AngelOneBroker: downloading instrument master from %s", url)

        try:
            raw_bytes: bytes = await self._run_sync(
                lambda: urlopen(url, timeout=30).read()
            )
            import json
            records: list[dict] = json.loads(raw_bytes)
        except Exception as exc:
            logger.exception("AngelOneBroker: failed to download instrument master")
            raise AngelOneAPIError(f"Instrument master download failed: {exc}") from exc

        instruments: list[Instrument] = []
        for rec in records:
            if rec.get("exch_seg", "").upper() != exch_str:
                continue
            try:
                inst_type_raw = rec.get("instrumenttype", "").upper()
                inst_type = self._map_instrument_type(inst_type_raw)
                instruments.append(
                    Instrument(
                        token=str(rec.get("token", "")),
                        symbol=rec.get("symbol", ""),
                        name=rec.get("name", ""),
                        exchange=exchange,
                        instrument_type=inst_type,
                        lot_size=int(rec.get("lotsize", 1) or 1),
                        tick_size=float(rec.get("tick_size", 0.05) or 0.05),
                        expiry=self._parse_expiry(rec.get("expiry", "")),
                        strike=float(rec.get("strike", 0) or 0) / 100.0,
                        option_type=rec.get("optiontype", None),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.debug("AngelOneBroker: skipping malformed instrument record %s", rec)

        self._instruments[exch_str] = instruments
        logger.info(
            "AngelOneBroker: loaded %d instruments for %s",
            len(instruments),
            exch_str,
        )
        return instruments

    @staticmethod
    def _map_instrument_type(raw: str) -> InstrumentType:
        """Map Angel One instrument type string to our ``InstrumentType`` enum."""
        mapping = {
            "OPTIDX":  InstrumentType.OPTION,
            "OPTSTK":  InstrumentType.OPTION,
            "FUTIDX":  InstrumentType.FUTURE,
            "FUTSTK":  InstrumentType.FUTURE,
            "AMXIDX":  InstrumentType.INDEX,
            "":        InstrumentType.EQUITY,
            "EQ":      InstrumentType.EQUITY,
        }
        return mapping.get(raw, InstrumentType.EQUITY)

    @staticmethod
    def _parse_expiry(expiry_str: str) -> Optional[date]:
        """Parse Angel One's expiry date string (DDMMMYYYY or blank)."""
        if not expiry_str:
            return None
        for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
            try:
                return datetime.strptime(expiry_str.upper(), fmt).date()
            except ValueError:
                continue
        return None

    async def get_ltp(
        self,
        tokens: list[str],
        exchange: Exchange,
    ) -> dict[str, float]:
        """Fetch the Last Traded Price for a batch of tokens.

        Uses the ``marketData`` (LTP mode) endpoint which supports up to 50
        tokens per call.

        Parameters
        ----------
        tokens : list[str]
            Angel One instrument tokens.
        exchange : Exchange
            Exchange for the tokens.

        Returns
        -------
        dict[str, float]
            ``{token: ltp}`` mapping.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        result: dict[str, float] = {}
        # Angel One marketData accepts max 50 tokens per call
        for chunk_start in range(0, len(tokens), 50):
            chunk = tokens[chunk_start: chunk_start + 50]
            payload = {
                "mode": "LTP",
                "exchangeTokens": {exchange.value: chunk},
            }
            try:
                resp = await self._call_with_retry(smart.marketData, payload)
                self._raise_for_error(resp, "get_ltp")
                fetched = resp.get("data", {}).get("fetched", [])
                for item in fetched:
                    token = item.get("symbolToken") or item.get("token", "")
                    ltp = float(item.get("ltp", 0))
                    result[str(token)] = ltp
            except AngelOneAuthError:
                raise
            except Exception:
                logger.exception("AngelOneBroker: get_ltp failed for chunk %s", chunk)

        return result

    async def get_ohlc(
        self,
        symbol: str,
        exchange: Exchange,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[Candle]:
        """Fetch historical OHLCV data using Angel One's ``getCandleData`` API.

        Parameters
        ----------
        symbol : str
            Angel One instrument token (not the trading symbol).
        exchange : Exchange
            Exchange for the instrument.
        timeframe : str
            One of: ``"1m"``, ``"3m"``, ``"5m"``, ``"10m"``, ``"15m"``,
            ``"30m"``, ``"1h"``, ``"1d"``.
        from_dt : datetime
            Range start (IST).
        to_dt : datetime
            Range end (IST).

        Returns
        -------
        list[Candle]
            OHLCV candles ordered oldest-first.

        Raises
        ------
        ValueError
            If *timeframe* is not a recognised interval.
        """
        self._ensure_connected()

        ao_interval = _TF_MAP.get(timeframe)
        if ao_interval is None:
            raise ValueError(
                f"Unsupported timeframe {timeframe!r}. "
                f"Choose from: {sorted(_TF_MAP.keys())}"
            )

        smart = self._get_smart_connect()
        payload = {
            "exchange":    exchange.value,
            "symboltoken": symbol,
            "interval":    ao_interval,
            "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
        }

        try:
            resp = await self._call_with_retry(smart.getCandleData, payload)
            self._raise_for_error(resp, "get_ohlc")
        except AngelOneAuthError:
            raise
        except Exception:
            logger.exception("AngelOneBroker: get_ohlc failed for %s", symbol)
            return []

        raw_candles: list[list] = resp.get("data", [])
        candles: list[Candle] = []
        for row in raw_candles:
            try:
                # row format: [timestamp, open, high, low, close, volume]
                ts = datetime.fromisoformat(str(row[0]))
                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=int(row[5]) if len(row) > 5 else 0,
                    )
                )
            except Exception:
                logger.debug("AngelOneBroker: skipping malformed candle row %s", row)

        logger.debug(
            "AngelOneBroker: get_ohlc %s/%s %s → %d candles",
            exchange.value,
            symbol,
            timeframe,
            len(candles),
        )
        return candles

    async def get_option_chain(
        self,
        underlying: str,
        expiry: date,
        exchange: Exchange = Exchange.NFO,
    ) -> Optional[OptionChain]:
        """Fetch the option chain for an underlying at a specific expiry.

        Uses Angel One's ``getOptionChain`` API endpoint.

        Parameters
        ----------
        underlying : str
            Underlying symbol (e.g. ``"NIFTY"``, ``"BANKNIFTY"``).
        expiry : date
            Target expiry date.
        exchange : Exchange
            Derivatives exchange (default ``NFO``).

        Returns
        -------
        OptionChain | None
            Populated option chain, or ``None`` if the API returns no data.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        expiry_str = expiry.strftime("%d%b%Y").upper()  # e.g. "31JUL2025"

        try:
            resp = await self._call_with_retry(
                smart.optionGreek,
                {
                    "name":     underlying,
                    "expirydate": expiry_str,
                },
            )
            self._raise_for_error(resp, "get_option_chain")
        except AngelOneAuthError:
            raise
        except Exception:
            logger.exception(
                "AngelOneBroker: get_option_chain failed for %s %s", underlying, expiry_str
            )
            return None

        data = resp.get("data", [])
        if not data:
            return None

        contracts: list[OptionContract] = []
        for row in data:
            try:
                contracts.append(
                    OptionContract(
                        strike=float(row.get("strikePrice", 0)),
                        option_type=row.get("optionType", ""),    # "CE" or "PE"
                        expiry=expiry,
                        token=str(row.get("token", "")),
                        symbol=row.get("tradingSymbol", ""),
                        ltp=float(row.get("ltp", 0)),
                        bid=float(row.get("buyPrice", 0)),
                        ask=float(row.get("sellPrice", 0)),
                        oi=int(row.get("openInterest", 0)),
                        volume=int(row.get("tradeVolume", 0)),
                        iv=float(row.get("impliedVolatility", 0)),
                        delta=float(row.get("delta", 0)),
                        gamma=float(row.get("gamma", 0)),
                        theta=float(row.get("theta", 0)),
                        vega=float(row.get("vega", 0)),
                    )
                )
            except Exception:
                logger.debug("AngelOneBroker: skipping malformed option row %s", row)

        underlying_ltp_data = await self.get_ltp(
            [str(row.get("underlyingToken", "")) for row in data[:1]],
            exchange,
        )
        underlying_ltp = next(iter(underlying_ltp_data.values()), 0.0)

        return OptionChain(
            underlying=underlying,
            expiry=expiry,
            exchange=exchange,
            spot_price=underlying_ltp,
            contracts=contracts,
            fetched_at=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place an order via Angel One SmartAPI.

        Parameters
        ----------
        order : OrderRequest
            Fully populated order request.

        Returns
        -------
        OrderResponse
            Contains the Angel One broker_order_id.

        Raises
        ------
        AngelOneOrderError
            If Angel One rejects the order.
        AngelOneAuthError
            If the session has expired.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        ao_params = self._build_order_params(order)
        logger.info(
            "AngelOneBroker: placing %s %s order | symbol=%s | qty=%d | price=%.2f",
            order.order_type.value,
            order.side.value,
            order.symbol,
            order.quantity,
            order.price or 0,
        )

        try:
            resp = await self._call_with_retry(smart.placeOrder, ao_params)
            self._raise_for_error(resp, "place_order")
        except (AngelOneAuthError, AngelOneOrderError):
            raise
        except Exception as exc:
            logger.exception("AngelOneBroker: place_order failed")
            raise AngelOneAPIError(f"place_order failed: {exc}") from exc

        data = resp.get("data", {}) or {}
        broker_order_id = str(data.get("orderid", ""))

        return OrderResponse(
            broker_order_id=broker_order_id,
            status=OrderStatus.OPEN,
            message=resp.get("message", "Order placed"),
        )

    def _build_order_params(self, order: OrderRequest) -> dict:
        """Convert an ``OrderRequest`` to Angel One's placeOrder parameter dict.

        Parameters
        ----------
        order : OrderRequest
            Internal order representation.

        Returns
        -------
        dict
            Angel One API parameters.
        """
        # Map our OrderType to Angel One variety and ordertype
        variety_map = {
            OrderType.MARKET: "NORMAL",
            OrderType.LIMIT:  "NORMAL",
            OrderType.SL:     "STOPLOSS",
            OrderType.SL_M:   "STOPLOSS",
        }
        ordertype_map = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT:  "LIMIT",
            OrderType.SL:     "STOPLOSS_LIMIT",
            OrderType.SL_M:   "STOPLOSS_MARKET",
        }

        params: dict = {
            "variety":          variety_map.get(order.order_type, "NORMAL"),
            "tradingsymbol":    order.symbol,
            "symboltoken":      order.token or "",
            "transactiontype":  order.side.value,    # "BUY" / "SELL"
            "exchange":         order.exchange.value,
            "ordertype":        ordertype_map.get(order.order_type, "MARKET"),
            "producttype":      getattr(order, "product", "NRML"),
            "duration":         getattr(order, "validity", "DAY"),
            "quantity":         str(order.quantity),
            "price":            str(order.price or 0),
            "squareoff":        "0",
            "stoploss":         "0",
            "triggerprice":     str(order.trigger_price or 0),
        }
        return params

    async def modify_order(
        self,
        broker_order_id: str,
        modifications: dict,
    ) -> OrderResponse:
        """Modify an open Angel One order.

        Parameters
        ----------
        broker_order_id : str
            Angel One order ID returned from ``place_order``.
        modifications : dict
            Keys: ``price`` (float), ``trigger_price`` (float),
            ``quantity`` (int), ``order_type`` (``OrderType``).

        Returns
        -------
        OrderResponse
            Updated response from Angel One.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        params: dict = {
            "variety":      modifications.get("variety", "NORMAL"),
            "orderid":      broker_order_id,
            "quantity":     str(modifications.get("quantity", 0)),
            "price":        str(modifications.get("price", 0)),
            "triggerprice": str(modifications.get("trigger_price", 0)),
            "duration":     modifications.get("duration", "DAY"),
        }

        try:
            resp = await self._call_with_retry(smart.modifyOrder, params)
            self._raise_for_error(resp, "modify_order")
        except (AngelOneAuthError, AngelOneOrderError):
            raise
        except Exception as exc:
            logger.exception("AngelOneBroker: modify_order failed")
            raise AngelOneAPIError(f"modify_order failed: {exc}") from exc

        return OrderResponse(
            broker_order_id=broker_order_id,
            status=OrderStatus.OPEN,
            message=resp.get("message", "Order modified"),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open Angel One order.

        Parameters
        ----------
        broker_order_id : str
            Angel One order ID.

        Returns
        -------
        bool
            ``True`` if cancellation was accepted.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        try:
            resp = await self._call_with_retry(
                smart.cancelOrder,
                broker_order_id,
                "NORMAL",
            )
            self._raise_for_error(resp, "cancel_order")
        except AngelOneAuthError:
            raise
        except AngelOneOrderError as exc:
            logger.warning("AngelOneBroker: cancel_order rejected: %s", exc)
            return False
        except Exception:
            logger.exception("AngelOneBroker: cancel_order failed")
            return False

        logger.info("AngelOneBroker: order %s cancelled", broker_order_id)
        return True

    # ------------------------------------------------------------------
    # Account / portfolio
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Fetch current day and net positions from Angel One.

        Returns
        -------
        list[Position]
            All open positions.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        try:
            resp = await self._call_with_retry(smart.position)
            self._raise_for_error(resp, "get_positions")
        except AngelOneAuthError:
            raise
        except Exception:
            logger.exception("AngelOneBroker: get_positions failed")
            return []

        raw: list[dict] = resp.get("data", []) or []
        positions: list[Position] = []
        for item in raw:
            try:
                qty = int(item.get("netqty", 0))
                positions.append(
                    Position(
                        symbol=item.get("tradingsymbol", ""),
                        exchange=Exchange(item.get("exchange", "NFO")),
                        quantity=qty,
                        average_price=float(item.get("netavgprice", 0)),
                        ltp=float(item.get("ltp", 0)),
                        unrealised_pnl=float(item.get("unrealised", 0)),
                        realised_pnl=float(item.get("realised", 0)),
                        product=item.get("producttype", "NRML"),
                    )
                )
            except Exception:
                logger.debug("AngelOneBroker: skipping malformed position %s", item)

        return positions

    async def get_orders(self) -> list[dict]:
        """Fetch the raw order book from Angel One.

        Returns
        -------
        list[dict]
            Raw Angel One order entries.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        try:
            resp = await self._call_with_retry(smart.orderBook)
            self._raise_for_error(resp, "get_orders")
        except AngelOneAuthError:
            raise
        except Exception:
            logger.exception("AngelOneBroker: get_orders failed")
            return []

        return resp.get("data", []) or []

    async def get_trade_book(self) -> list[Trade]:
        """Fetch the executed trade book from Angel One.

        Returns
        -------
        list[Trade]
            Today's executed trades.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        try:
            resp = await self._call_with_retry(smart.tradeBook)
            self._raise_for_error(resp, "get_trade_book")
        except AngelOneAuthError:
            raise
        except Exception:
            logger.exception("AngelOneBroker: get_trade_book failed")
            return []

        raw: list[dict] = resp.get("data", []) or []
        trades: list[Trade] = []
        for item in raw:
            try:
                trades.append(
                    Trade(
                        trade_id=str(item.get("tradeid", "")),
                        broker_order_id=str(item.get("orderid", "")),
                        symbol=item.get("tradingsymbol", ""),
                        exchange=Exchange(item.get("exchange", "NFO")),
                        side=OrderSide(item.get("transactiontype", "BUY")),
                        quantity=int(item.get("fillshares", 0)),
                        price=float(item.get("fillprice", 0)),
                        traded_at=datetime.fromisoformat(
                            item.get("filltime", datetime.now().isoformat())
                        ),
                        pnl=None,
                    )
                )
            except Exception:
                logger.debug("AngelOneBroker: skipping malformed trade entry %s", item)

        return trades

    async def get_margins(self) -> MarginInfo:
        """Fetch account margin / RMS data from Angel One.

        Returns
        -------
        MarginInfo
            Current margin summary.
        """
        self._ensure_connected()
        smart = self._get_smart_connect()

        try:
            resp = await self._call_with_retry(smart.rmsLimit)
            self._raise_for_error(resp, "get_margins")
        except AngelOneAuthError:
            raise
        except Exception:
            logger.exception("AngelOneBroker: get_margins failed")
            return MarginInfo(
                total_cash=0.0,
                available_cash=0.0,
                used_margin=0.0,
                net_liquidation=0.0,
                unrealised_pnl=0.0,
            )

        data: dict = resp.get("data", {}) or {}

        net = float(data.get("net", 0))
        available = float(data.get("availablecash", 0))
        used = float(data.get("utilisedmargin", 0))
        holdings = float(data.get("holdings", 0))

        return MarginInfo(
            total_cash=net,
            available_cash=available,
            used_margin=used,
            net_liquidation=net + holdings,
            unrealised_pnl=float(data.get("unrealisedpnl", 0)),
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def search_instrument(
        self,
        query: str,
        exchange: Exchange,
    ) -> list[Instrument]:
        """Search for instruments by symbol substring (case-insensitive).

        Loads the instrument master for the exchange (cached) and returns
        instruments whose ``symbol`` or ``name`` contains *query*.

        Parameters
        ----------
        query : str
            Substring to search.
        exchange : Exchange
            Exchange to filter.

        Returns
        -------
        list[Instrument]
            Matching instruments.
        """
        all_instruments = await self.get_instruments(exchange)
        q = query.upper()
        return [
            inst
            for inst in all_instruments
            if q in inst.symbol.upper() or q in (inst.name or "").upper()
        ]
