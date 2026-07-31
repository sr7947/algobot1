"""
Symbol screener / watchlist manager for market data service.
"""
from __future__ import annotations

import logging
from typing import Optional

import yaml
from pathlib import Path

from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


class SymbolScreener:
    """
    Manages the active watchlist and provides symbol lookups
    for lot sizes, tick sizes, and sector mapping.
    """

    def __init__(self):
        self._instruments_config: dict = {}
        self._watchlist: list[str] = []
        self._load_config()

    def _load_config(self) -> None:
        """Load instruments.yaml config."""
        config_path = Path(__file__).parent.parent / "config" / "instruments.yaml"
        if config_path.exists():
            with open(config_path, "r") as f:
                self._instruments_config = yaml.safe_load(f) or {}

        # Load watchlist from settings
        if hasattr(settings, "WATCHLIST") and settings.WATCHLIST:
            self._watchlist = [s.strip() for s in settings.WATCHLIST.split(",")]
        else:
            self._watchlist = list(self._instruments_config.get("indices", {}).keys())

    def get_watchlist(self) -> list[str]:
        """Get the current active watchlist."""
        return self._watchlist.copy()

    def set_watchlist(self, symbols: list[str]) -> None:
        """Override the watchlist."""
        self._watchlist = symbols

    def add_symbol(self, symbol: str) -> None:
        """Add a symbol to the watchlist."""
        if symbol not in self._watchlist:
            self._watchlist.append(symbol)

    def remove_symbol(self, symbol: str) -> None:
        """Remove a symbol from the watchlist."""
        if symbol in self._watchlist:
            self._watchlist.remove(symbol)

    def get_lot_size(self, symbol: str) -> int:
        """Get the F&O lot size for a symbol."""
        indices = self._instruments_config.get("indices", {})
        if symbol in indices:
            return indices[symbol].get("lot_size", 1)

        stocks = self._instruments_config.get("stocks", {})
        if symbol in stocks:
            return stocks[symbol].get("lot_size", 1)

        return 1  # Default

    def get_tick_size(self, symbol: str) -> float:
        """Get the tick size for a symbol."""
        for category in ("indices", "stocks"):
            items = self._instruments_config.get(category, {})
            if symbol in items:
                return items[symbol].get("tick_size", 0.05)
        return 0.05

    def get_sector(self, symbol: str) -> Optional[str]:
        """Get the sector for a stock symbol."""
        stocks = self._instruments_config.get("stocks", {})
        if symbol in stocks:
            return stocks[symbol].get("sector")
        if symbol in self._instruments_config.get("indices", {}):
            return "INDEX"
        return None

    def is_index(self, symbol: str) -> bool:
        """Check if symbol is an index."""
        return symbol in self._instruments_config.get("indices", {})

    def get_all_fno_symbols(self) -> list[str]:
        """Get all configured F&O symbols (indices + stocks)."""
        symbols = list(self._instruments_config.get("indices", {}).keys())
        symbols.extend(self._instruments_config.get("stocks", {}).keys())
        return symbols

    def get_symbols_by_sector(self, sector: str) -> list[str]:
        """Get all stocks in a given sector."""
        stocks = self._instruments_config.get("stocks", {})
        return [s for s, config in stocks.items() if config.get("sector") == sector]
