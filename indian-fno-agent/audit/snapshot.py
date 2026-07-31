"""
Market snapshot capture — freezes the full market context at signal generation time.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from config.settings import get_settings

logger = logging.getLogger(__name__)


class MarketSnapshotService:
    """
    Captures a full market snapshot at the moment a signal is generated.
    This enables post-hoc analysis and replay of trading decisions.
    """

    def __init__(self, db_pool=None, broker_adapter=None, historical_service=None, options_service=None):
        self.settings = get_settings()
        self.db_pool = db_pool
        self.broker = broker_adapter
        self.historical = historical_service
        self.options_service = options_service
        self._data_dir = Path(self.settings.DATA_DIR) / "snapshots"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    async def capture(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
        include_options: bool = True,
        indicators: dict | None = None,
        news_events: list[dict] | None = None,
        risk_state: dict | None = None,
    ) -> dict[str, Any]:
        """
        Capture a full market snapshot for a symbol.

        Args:
            symbol: Trading symbol (e.g. NIFTY, RELIANCE).
            timeframes: List of timeframes to capture (default: 1m, 5m, 15m).
            include_options: Whether to include option chain data.
            indicators: Pre-computed indicator values to include.
            news_events: Recent news events to include.
            risk_state: Current risk state to include.

        Returns:
            dict containing the full snapshot.
        """
        if timeframes is None:
            timeframes = ["1m", "5m", "15m"]

        snapshot: dict[str, Any] = {
            "symbol": symbol,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "candles": {},
            "indicators": indicators or {},
            "option_chain": None,
            "news": news_events or [],
            "risk_state": risk_state or {},
        }

        # Capture OHLCV for each timeframe
        if self.historical:
            for tf in timeframes:
                try:
                    candles = await self.historical.fetch_ohlcv(
                        symbol=symbol,
                        exchange="NFO",
                        timeframe=tf,
                        lookback_days=5,
                    )
                    snapshot["candles"][tf] = [
                        {
                            "time": c.time.isoformat() if hasattr(c.time, "isoformat") else str(c.time),
                            "open": float(c.open),
                            "high": float(c.high),
                            "low": float(c.low),
                            "close": float(c.close),
                            "volume": int(c.volume) if c.volume else 0,
                        }
                        for c in (candles[-50:] if candles else [])  # Last 50 candles
                    ]
                except Exception as e:
                    logger.warning(f"Failed to capture {tf} candles for {symbol}: {e}")
                    snapshot["candles"][tf] = []

        # Capture option chain
        if include_options and self.options_service:
            try:
                chain = await self.options_service.fetch_chain(
                    underlying=symbol, expiry=None, exchange="NFO"
                )
                if chain:
                    snapshot["option_chain"] = {
                        "underlying": chain.underlying,
                        "spot_price": float(chain.spot_price),
                        "pcr": float(chain.pcr),
                        "entries_count": len(chain.entries),
                        "timestamp": chain.timestamp.isoformat() if chain.timestamp else None,
                    }
            except Exception as e:
                logger.warning(f"Failed to capture option chain for {symbol}: {e}")

        # Add LTP if broker available
        if self.broker:
            try:
                ltp_map = await self.broker.get_ltp([symbol], exchange="NFO")
                snapshot["ltp"] = ltp_map.get(symbol)
            except Exception:
                pass

        return snapshot

    async def store(self, signal_id: UUID | str, snapshot: dict) -> None:
        """
        Persist snapshot to PostgreSQL and JSON file.

        Args:
            signal_id: The signal ID this snapshot belongs to.
            snapshot: The captured snapshot dict.
        """
        signal_id_str = str(signal_id)

        # 1. Save to JSON file
        try:
            filepath = self._data_dir / f"snapshot_{signal_id_str}.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save snapshot file: {e}")

        # 2. Save to PostgreSQL
        if self.db_pool:
            try:
                await self.db_pool.execute(
                    """
                    INSERT INTO market_snapshots (signal_id, captured_at, symbol, snapshot_data)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    signal_id_str if not isinstance(signal_id, UUID) else signal_id,
                    datetime.now(timezone.utc),
                    snapshot.get("symbol", ""),
                    json.dumps(snapshot, default=str),
                )
            except Exception as e:
                logger.error(f"Failed to save snapshot to DB: {e}")

    async def retrieve(self, signal_id: UUID | str) -> Optional[dict]:
        """
        Load a previously captured snapshot.
        Tries DB first, falls back to JSON file.
        """
        signal_id_str = str(signal_id)

        # Try DB first
        if self.db_pool:
            try:
                row = await self.db_pool.fetchrow(
                    "SELECT snapshot_data FROM market_snapshots WHERE signal_id = $1",
                    signal_id_str if not isinstance(signal_id, UUID) else signal_id,
                )
                if row:
                    data = row["snapshot_data"]
                    return json.loads(data) if isinstance(data, str) else data
            except Exception as e:
                logger.warning(f"DB snapshot retrieval failed: {e}")

        # Fallback to file
        filepath = self._data_dir / f"snapshot_{signal_id_str}.json"
        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"File snapshot retrieval failed: {e}")

        return None
