"""
broker/delta_exchange.py
========================
Delta Exchange Broker Adapter for Crypto Futures & Options.

Supports both Testnet (Paper Trading) and Live (Production India) environments:
  - Testnet (Paper): https://cdn-ind.testnet.deltaex.org
  - Live (Production India): https://api.india.delta.exchange

Uses HMAC-SHA256 authentication signatures required by Delta Exchange REST API v2.
Normalizes all Delta Exchange responses to internal Pydantic models in ``core.models``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from broker.base import IBrokerAdapter
from core.enums import Exchange, InstrumentType
from core.exceptions import (
    BrokerAuthError,
    BrokerConnectionError,
    InsufficientMarginError,
    OrderPlacementError,
)
from core.models import (
    Candle,
    Instrument,
    MarginInfo,
    OptionChain,
    OptionChainEntry,
    OrderRequest,
    OrderResponse,
    Position,
    Trade,
)

logger = logging.getLogger(__name__)


class DeltaExchangeBroker(IBrokerAdapter):
    """
    Broker adapter for Delta Exchange (Crypto Futures & Options).

    Reads configuration from application settings or environment variables:
      - ``DELTA_API_KEY``
      - ``DELTA_API_SECRET``
      - ``DELTA_ENV`` (``'paper'`` | ``'live'`` - defaults to ``'paper'``)
    """

    broker_name: str = "delta_exchange"

    TESTNET_BASE_URL: str = "https://cdn-ind.testnet.deltaex.org"
    LIVE_BASE_URL: str = "https://api.india.delta.exchange"

    def __init__(self, settings: Any) -> None:
        self._settings = settings

        # Retrieve keys from settings or fallback to direct attribute
        self._api_key: str = getattr(settings, "DELTA_API_KEY", "") or ""
        self._api_secret: str = getattr(settings, "DELTA_API_SECRET", "") or ""

        env = getattr(settings, "DELTA_ENV", "paper") or "paper"
        self._is_live: bool = (env.lower() == "live")

        self.base_url: str = self.LIVE_BASE_URL if self._is_live else self.TESTNET_BASE_URL
        self._authenticated: bool = False
        self._products_cache: Dict[str, Dict[str, Any]] = {}
        self._product_id_map: Dict[str, int] = {}
        self._symbol_map: Dict[int, str] = {}

        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Signature & Authentication Helper
    # ------------------------------------------------------------------

    def _generate_signature(
        self,
        method: str,
        path: str,
        query_string: str = "",
        payload: str = "",
    ) -> Dict[str, str]:
        """
        Generate HMAC-SHA256 headers for Delta Exchange REST API v2.

        Signature string format:
            METHOD + TIMESTAMP + PATH + (?QUERY_STRING) + PAYLOAD
        """
        timestamp = str(int(time.time()))
        query_part = f"?{query_string}" if query_string else ""
        signature_data = method.upper() + timestamp + path + query_part + payload

        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "api-key": self._api_key,
            "signature": signature,
            "timestamp": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """Execute HTTP request to Delta Exchange API with rate limit & error handling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)

        query_string = ""
        if params:
            import urllib.parse
            query_string = urllib.parse.urlencode(params)

        payload_str = ""
        if payload:
            import json
            payload_str = json.dumps(payload)

        headers = {}
        if auth_required:
            if not self._api_key or not self._api_secret:
                raise BrokerAuthError("DELTA_API_KEY and DELTA_API_SECRET must be configured.", broker=self.broker_name)
            headers = self._generate_signature(method, path, query_string, payload_str)
        else:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}

        url = f"{path}?{query_string}" if query_string else path

        try:
            response = await self._client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                content=payload_str if payload else None,
            )
        except Exception as exc:
            logger.exception("Delta Exchange API connection error: %s", exc)
            raise BrokerConnectionError(f"Connection failed: {exc}", broker=self.broker_name) from exc

        if response.status_code == 401 or response.status_code == 403:
            raise BrokerAuthError(f"Authentication failed: {response.text}", broker=self.broker_name)

        if response.status_code == 429:
            logger.warning("Delta Exchange API rate limit hit. Retrying after delay...")
            await asyncio.sleep(1.0)
            return await self._request(method, path, params, payload, auth_required)

        data = response.json()
        if not data.get("success", True):
            error_msg = data.get("error", {}).get("message", response.text)
            code = data.get("error", {}).get("code", "")
            if "INSUFFICIENT_MARGIN" in str(error_msg).upper() or "MARGIN" in str(code).upper():
                raise InsufficientMarginError(f"Delta Exchange margin error: {error_msg}")
            raise OrderPlacementError(f"Delta Exchange API error: {error_msg}")

        return data

    # ------------------------------------------------------------------
    # Session Management
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Authenticate and verify session with Delta Exchange."""
        try:
            res = await self._request("GET", "/v2/wallet/balances", auth_required=True)
            if res.get("success"):
                self._authenticated = True
                logger.info("Successfully connected to Delta Exchange (%s)", "LIVE" if self._is_live else "TESTNET")
                # Pre-fetch products catalog
                await self.get_instruments(Exchange.NSE)
                return True
        except Exception as exc:
            logger.error("Delta Exchange login failed: %s", exc)
            self._authenticated = False

        return False

    async def connect(self) -> bool:
        """Alias for login()."""
        return await self.login()

    async def logout(self) -> bool:
        """Close active session."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._authenticated = False
        logger.info("Disconnected from Delta Exchange")
        return True

    def is_connected(self) -> bool:
        """Check connection state."""
        return self._authenticated

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    async def get_instruments(self, exchange: Exchange = Exchange.NSE) -> List[Instrument]:
        """Fetch and cache product master catalog from Delta Exchange."""
        res = await self._request("GET", "/v2/products", auth_required=False)
        result = res.get("result", [])

        instruments = []
        for prod in result:
            prod_id = prod["id"]
            symbol = prod["symbol"]
            contract_type = prod.get("contract_type", "")

            # Map instrument type
            if contract_type == "call_options":
                itype = InstrumentType.CE
            elif contract_type == "put_options":
                itype = InstrumentType.PE
            elif "futures" in contract_type:
                itype = InstrumentType.FUT
            else:
                itype = InstrumentType.EQ

            expiry_dt = None
            if prod.get("settlement_time"):
                try:
                    expiry_dt = datetime.fromisoformat(prod["settlement_time"].replace("Z", "+00:00")).date()
                except Exception:
                    pass

            import uuid
            inst = Instrument(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, f"delta_{prod_id}"),
                symbol=symbol,
                exchange=Exchange.NSE,  # Map to standard enum
                instrument_type=itype,
                lot_size=max(1, int(float(prod.get("contract_value", 1.0)))),
                tick_size=float(prod.get("tick_size", 0.1)),
                expiry=expiry_dt,
                strike=float(prod["strike_price"]) if prod.get("strike_price") else None,
                option_type="CE" if itype == InstrumentType.CE else ("PE" if itype == InstrumentType.PE else None),
                underlying=prod.get("underlying_asset", {}).get("symbol"),
                token=str(prod_id),
                is_active=prod.get("state") == "live",
            )
            instruments.append(inst)

            # Store mapping
            self._products_cache[symbol] = prod
            self._product_id_map[symbol] = prod_id
            self._symbol_map[prod_id] = symbol

        return instruments

    async def get_ltp(self, tokens: List[str], exchange: Exchange = Exchange.NSE) -> Dict[str, float]:
        """Fetch live ticker prices for requested symbols."""
        res = await self._request("GET", "/v2/tickers", auth_required=False)
        tickers = res.get("result", [])

        ltp_map = {}
        for t in tickers:
            sym = t.get("symbol", "")
            if sym in tokens or str(t.get("product_id")) in tokens:
                price = float(t.get("close", 0.0) or t.get("mark_price", 0.0))
                ltp_map[sym] = price
                ltp_map[str(t.get("product_id"))] = price

        return ltp_map

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch detailed ticker info for a single symbol."""
        res = await self._request("GET", f"/v2/tickers/{symbol}", auth_required=False)
        return res.get("result", {})

    async def get_ohlc(
        self,
        symbol: str,
        exchange: Exchange,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> List[Candle]:
        """Fetch historical candle data from Delta Exchange."""
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}
        resolution = tf_map.get(timeframe, "5m")

        start_ts = int(from_dt.timestamp())
        end_ts = int(to_dt.timestamp())

        params = {
            "symbol": symbol,
            "resolution": resolution,
            "start": start_ts,
            "end": end_ts,
        }
        res = await self._request("GET", "/v2/chart/history", params=params, auth_required=False)
        result = res.get("result", [])

        candles = []
        for c in result:
            candle = Candle(
                time=datetime.fromtimestamp(c["t"], tz=timezone.utc),
                open=float(c["o"]),
                high=float(c["h"]),
                low=float(c["l"]),
                close=float(c["c"]),
                volume=float(c.get("v", 0.0)),
            )
            candles.append(candle)

        return candles

    async def get_option_chain(
        self,
        underlying: str = "BTC",
        expiry: Optional[date] = None,
        exchange: Exchange = Exchange.NSE,
    ) -> Optional[OptionChain]:
        """Fetch option chain with Greeks & IV for underlying (e.g. BTC, ETH)."""
        params = {"underlying_asset": underlying}
        res = await self._request("GET", "/v2/tickers", params=params, auth_required=False)
        tickers = res.get("result", [])

        entries = []
        spot_price = 0.0

        for t in tickers:
            if t.get("contract_type") == "spot" and t.get("symbol") == f"{underlying}USD":
                spot_price = float(t.get("close", 0.0))

            if "options" in t.get("contract_type", ""):
                strike = float(t.get("strike_price", 0.0))
                price = float(t.get("close", 0.0) or t.get("mark_price", 0.0))
                oi = float(t.get("open_interest", 0.0))
                greeks = t.get("greeks", {})

                if t.get("contract_type") == "call_options":
                    entries.append(OptionChainEntry(
                        strike=strike,
                        call_oi=oi,
                        call_oi_change=0.0,
                        call_iv=float(t.get("quotes", {}).get("iv", 0.0)),
                        call_ltp=price,
                        call_greeks=greeks,
                        put_oi=0.0,
                        put_oi_change=0.0,
                        put_iv=0.0,
                        put_ltp=0.0,
                        put_greeks={},
                    ))

        return OptionChain(
            underlying=underlying,
            expiry=expiry or date.today(),
            spot_price=spot_price or 65000.0,
            entries=entries,
            pcr=1.0,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Order Management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        Place an order on Delta Exchange.

        Supports passing ``product_id`` or resolving product_id from ``symbol``.
        """
        prod_id = getattr(order, "product_id", None) or self._product_id_map.get(order.symbol)
        if not prod_id:
            await self.get_instruments(Exchange.NSE)
            prod_id = self._product_id_map.get(order.symbol)

        if not prod_id:
            raise OrderPlacementError(f"Unknown symbol / product_id for {order.symbol}", order_request=order)

        order_type_map = {
            "MARKET": "market_order",
            "LIMIT": "limit_order",
            "SL": "stop_market_order",
            "SL_M": "stop_market_order",
        }
        delta_order_type = order_type_map.get(order.order_type.upper(), "limit_order")

        payload = {
            "product_id": prod_id,
            "size": int(order.quantity),
            "side": order.direction.lower(),  # 'buy' or 'sell'
            "order_type": delta_order_type,
        }

        if delta_order_type == "limit_order" and order.price:
            payload["limit_price"] = str(order.price)

        if order.trigger_price:
            payload["stop_price"] = str(order.trigger_price)

        res = await self._request("POST", "/v2/orders", payload=payload, auth_required=True)
        result = res.get("result", {})

        raw_state = str(result.get("state", "OPEN")).upper()
        order_status = "COMPLETE" if raw_state in ("CLOSED", "FILLED") else raw_state

        return OrderResponse(
            broker_order_id=str(result.get("id")),
            status=order_status,
            message="Order submitted to Delta Exchange",
            timestamp=datetime.now(timezone.utc),
            raw_response=result,
        )

    async def modify_order(self, broker_order_id: str, modifications: Dict[str, Any]) -> OrderResponse:
        """Modify open order limit price or size."""
        prod_id = modifications.get("product_id")
        payload = {
            "id": int(broker_order_id),
            "product_id": prod_id,
        }
        if "price" in modifications:
            payload["limit_price"] = str(modifications["price"])
        if "quantity" in modifications:
            payload["size"] = int(modifications["quantity"])

        res = await self._request("PUT", "/v2/orders", payload=payload, auth_required=True)
        result = res.get("result", {})

        return OrderResponse(
            broker_order_id=str(result.get("id", broker_order_id)),
            status=result.get("state", "OPEN").upper(),
            message="Order modified",
            timestamp=datetime.now(timezone.utc),
            raw_response=result,
        )

    async def cancel_order(self, broker_order_id: str, product_id: Optional[int] = None) -> bool:
        """Cancel order by ID."""
        payload = {"id": int(broker_order_id)}
        if product_id:
            payload["product_id"] = product_id

        res = await self._request("DELETE", "/v2/orders", payload=payload, auth_required=True)
        return res.get("success", False)

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Fetch status of a specific order."""
        res = await self._request("GET", f"/v2/orders/{order_id}", auth_required=True)
        return res.get("result", {})

    # ------------------------------------------------------------------
    # Account & Portfolio
    # ------------------------------------------------------------------

    async def get_positions(self) -> List[Position]:
        """Fetch open margined positions."""
        res = await self._request("GET", "/v2/positions/margined", auth_required=True)
        result = res.get("result", [])

        positions = []
        for p in result:
            size = int(p.get("size", 0))
            if size == 0:
                continue

            entry = float(p.get("entry_price", 0.0))
            mark = float(p.get("mark_price", entry))
            pnl = float(p.get("unrealized_pnl", 0.0))
            prod_id = p.get("product_id")
            sym = self._symbol_map.get(prod_id, f"PRODUCT_{prod_id}")

            pos = Position(
                id=p.get("id"),
                order_id=str(p.get("id")),
                symbol=sym,
                direction="BUY" if size > 0 else "SELL",
                quantity=abs(size),
                entry_price=entry,
                current_price=mark,
                unrealized_pnl=pnl,
                stop_loss=float(p.get("liquidation_price", 0.0)),
                target=0.0,
                opened_at=datetime.now(timezone.utc),
            )
            positions.append(pos)

        return positions

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Fetch raw open orders book."""
        res = await self._request("GET", "/v2/orders", params={"state": "open"}, auth_required=True)
        return res.get("result", [])

    async def get_trade_book(self) -> List[Trade]:
        """Fetch executed trade fills."""
        res = await self._request("GET", "/v2/fills", auth_required=True)
        fills = res.get("result", [])

        trades = []
        for f in fills:
            prod_id = f.get("product_id")
            sym = self._symbol_map.get(prod_id, f"PRODUCT_{prod_id}")

            trade = Trade(
                id=str(f.get("id")),
                order_id=str(f.get("order_id")),
                symbol=sym,
                direction="BUY" if f.get("side") == "buy" else "SELL",
                quantity=int(f.get("size", 0)),
                price=float(f.get("price", 0.0)),
                realized_pnl=float(f.get("realized_pnl", 0.0)),
                charges=float(f.get("fee", 0.0)),
                executed_at=datetime.fromtimestamp(f.get("created_at", time.time()), tz=timezone.utc),
            )
            trades.append(trade)

        return trades

    async def get_margins(self) -> MarginInfo:
        """Fetch wallet balances and margin info."""
        res = await self._request("GET", "/v2/wallet/balances", auth_required=True)
        balances = res.get("result", [])

        total_cash = 0.0
        used_margin = 0.0

        for b in balances:
            balance = float(b.get("balance", 0.0))
            available = float(b.get("available_balance", balance))
            total_cash += balance
            used_margin += (balance - available)

        return MarginInfo(
            available_cash=total_cash,
            used_margin=used_margin,
            available_margin=total_cash - used_margin,
            collateral=0.0,
        )

    async def search_instrument(self, query: str, exchange: Exchange = Exchange.NSE) -> List[Instrument]:
        """Search products catalog for matching symbol query."""
        if not self._products_cache:
            await self.get_instruments(exchange)

        query_upper = query.upper()
        matching = []
        for sym, prod in self._products_cache.items():
            if query_upper in sym:
                matching.append(
                    Instrument(
                        id=str(prod["id"]),
                        symbol=sym,
                        exchange=Exchange.NSE,
                        instrument_type=InstrumentType.FUT if "futures" in prod.get("contract_type", "") else InstrumentType.CE,
                        lot_size=int(prod.get("contract_value", 1)),
                        tick_size=float(prod.get("tick_size", 0.1)),
                        token=str(prod["id"]),
                        is_active=prod.get("state") == "live",
                    )
                )

        return matching
