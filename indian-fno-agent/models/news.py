"""
Re-export News models and enums from core.models and core.enums.
"""
from enum import Enum
from core.models import NewsEvent


class NewsSource(str, Enum):
    NEWSAPI = "newsapi"
    ECONOMIC_TIMES = "economic_times"
    MONEYCONTROL = "moneycontrol"
    MANUAL = "manual"


__all__ = ["NewsEvent", "NewsSource"]
