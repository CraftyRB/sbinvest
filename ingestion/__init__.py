"""Multi-source data feeds for the SkyBlock AI Investment Dashboard."""

from .active_search import active_search
from .bazaar_client import BazaarError, fetch_bazaar, normalize_products
from .calendar_mayor import MayorError, collect_mayor_context
from .coflnet_ah import CoflnetError, collect_ah_intel, fetch_lbin
from .forum_scraper import collect_forum_posts
from .github_watcher import GitHubError, collect_new_leaks
from .reddit_scanner import collect_reddit_posts
from .update_calendar import (
    LINKABLE_SECTORS,
    catalyst_for_item,
    collect_update_calendar,
    event_touches,
)
from .wiki_watcher import WikiError, collect_wiki_changes
from .youtube_scanner import collect_youtube_videos

__all__ = [
    "active_search",
    "BazaarError",
    "fetch_bazaar",
    "normalize_products",
    "GitHubError",
    "collect_new_leaks",
    "collect_forum_posts",
    "collect_youtube_videos",
    "collect_reddit_posts",
    "CoflnetError",
    "fetch_lbin",
    "collect_ah_intel",
    "MayorError",
    "collect_mayor_context",
    "WikiError",
    "collect_wiki_changes",
    "collect_update_calendar",
    "catalyst_for_item",
]
