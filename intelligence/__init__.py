"""AI analysis layer (Cerebras gpt-oss-120b)."""

from .client import ChatResult, IntelligenceError, chat, chat_json, ping
from .analyst import analyze, heuristic_fallback
from .evidence import filter_evidence
from .research_planner import plan_research

__all__ = [
    "ChatResult",
    "IntelligenceError",
    "chat",
    "chat_json",
    "ping",
    "analyze",
    "heuristic_fallback",
    "filter_evidence",
    "plan_research",
]
