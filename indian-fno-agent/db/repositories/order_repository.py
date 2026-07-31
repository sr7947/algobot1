"""
Database repository for Orders.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class OrderRepository:
    """Repository for querying and managing orders in PostgreSQL."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, order_id: UUID | str) -> Optional[dict]:
        """Fetch order by ID."""
        return None

    async def get_by_position_id(self, position_id: UUID | str) -> List[dict]:
        """Fetch all orders related to a position."""
        return []
