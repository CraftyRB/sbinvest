"""Central configuration for the SkyBlock AI Investment Dashboard.

Every script in this project loads its secrets through this module, which
explicitly reads `env.txt` via python-dotenv. No secrets are hard-coded here.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "env.txt"

# Explicit load from env.txt in the current working directory.
load_dotenv("env.txt", override=False)
# Fallback so scripts still work when launched from a different cwd.
if not os.getenv("CEREBRAS_API_KEY"):
    load_dotenv(ENV_FILE, override=False)

HYPIXEL_API_KEY: str = os.getenv("HYPIXEL_API_KEY", "")
CEREBRAS_API_KEY: str = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_BASE_URL: str = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
CEREBRAS_MODEL: str = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
# Dedicated channel for holdings cash-out alerts: this one is driven by the
# user's own portfolio, not by the AI signal stream.
DISCORD_PORTFOLIO_WEBHOOK_URL: str = os.getenv(
    "DISCORD_PORTFOLIO_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1563930092265148457/vZSqFUxD9mCv5v9HSisKnayfV0PHk0vHs2i-mQv_WMIGp23s6ZRC-pGVvFaXmZNya2VX",
)
PORTFOLIO_TAKE_PROFIT_PCT: float = float(os.getenv("PORTFOLIO_TAKE_PROFIT_PCT", "0.20"))
PORTFOLIO_STRONG_TAKE_PROFIT_PCT: float = float(
    os.getenv("PORTFOLIO_STRONG_TAKE_PROFIT_PCT", "0.35")
)
PORTFOLIO_STOP_LOSS_PCT: float = float(os.getenv("PORTFOLIO_STOP_LOSS_PCT", "0.10"))
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

DB_PATH: Path = Path(os.getenv("DB_PATH", str(BASE_DIR / "investments.db")))

# When set (Streamlit Community Cloud deployment), all queries go to a remote
# Turso database because the cloud container has no durable filesystem.
TURSO_DATABASE_URL: str = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN: str = os.getenv("TURSO_AUTH_TOKEN", "")

HYPIXEL_BAZAAR_URL = "https://api.hypixel.net/v2/skyblock/bazaar"
HYPIXEL_ELECTION_URL = "https://api.hypixel.net/v2/resources/skyblock/election"
GITHUB_REPO = "NotEnoughUpdates/NotEnoughUpdates-REPO"
COFLNET_API_URL = "https://sky.coflnet.com/api"
# The official wiki.hypixel.net API was shut down (it serves a quoted forum post
# instead of JSON), so only the community Fandom MediaWiki API is polled.
WIKI_API_URLS = [
    "https://hypixel-skyblock.fandom.com/api.php",
]

# Confluence scoring (analytics/confluence.py). Weights sum to 1.0.
CONFLUENCE_WEIGHTS = {
    "github": 0.24,
    "forum": 0.17,
    "youtube": 0.13,
    "reddit": 0.07,
    "bazaar": 0.24,
    "wiki": 0.10,
    "web": 0.05,
}
SINGLE_SOURCE_CAP = 0.75     # one external stream only
NO_EXTERNAL_SOURCE_CAP = 0.70  # bazaar-only quant signal
HIGH_CONVICTION_SCORE = 0.65

# Investment terms. SHORT = dip/flip inside a few days; LONG = catalyst-driven
# position sized around an announced or predicted game update.
TERM_SHORT = "SHORT"
TERM_LONG = "LONG"
SHORT_HORIZON_DAYS = 3
LONG_MIN_HORIZON_DAYS = 7
LONG_HORIZON_DAYS = 30
LONG_MAX_CANDIDATES = 8
LONG_EVIDENCE_LOOKBACK_DAYS = 60
LONG_TARGET_MIN_PCT = 0.12
LONG_TARGET_MAX_PCT = 0.45
CATALYST_WINDOW_DAYS = 120

# Evidence acceptance. The gate is asked to keep anything that genuinely
# touches a ticker; the planner's criteria are injected before searching so the
# AI's accept/reject rule matches what is actually collected.
EVIDENCE_MIN_RELEVANCE = 0.30
EVIDENCE_ACCEPT_TARGET = 0.55

# Background daemon.
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
DAEMON_TOP_N = int(os.getenv("DAEMON_TOP_N", "12"))

# Multi-source intelligence feeds.
FORUM_FEEDS = [
    "https://hypixel.net/forums/skyblock-patch-notes.158/index.rss",
    "https://hypixel.net/forums/skyblock-alpha/index.rss",
    "https://hypixel.net/forums/skyblock-general-discussion.157/index.rss",
]
YOUTUBE_CREATORS = [
    handle.strip()
    for handle in os.getenv(
        "YOUTUBE_CREATORS", "@ThirtyVirus,@Refraction,@ModernSoldier"
    ).split(",")
    if handle.strip()
]
YOUTUBE_LOOKBACK_DAYS = int(os.getenv("YOUTUBE_LOOKBACK_DAYS", "21"))
YOUTUBE_MAX_TRANSCRIPTS = int(os.getenv("YOUTUBE_MAX_TRANSCRIPTS", "2"))
REDDIT_SUBREDDIT = os.getenv("REDDIT_SUBREDDIT", "HypixelSkyblock")
REDDIT_POST_LIMIT = int(os.getenv("REDDIT_POST_LIMIT", "25"))
SOURCE_LOOKBACK_DAYS = int(os.getenv("SOURCE_LOOKBACK_DAYS", "14"))
SOURCE_REQUEST_TIMEOUT = int(os.getenv("SOURCE_REQUEST_TIMEOUT", "20"))

# Active per-ticker evidence search (Reddit / YouTube / Fandom wiki).
ACTIVE_SEARCH_ENABLED = os.getenv("ACTIVE_SEARCH_ENABLED", "1") == "1"
ACTIVE_SEARCH_ITEMS = int(os.getenv("ACTIVE_SEARCH_ITEMS", "12"))
ACTIVE_SEARCH_RESULTS = int(os.getenv("ACTIVE_SEARCH_RESULTS", "4"))

# AI research planner: asks Cerebras what to search and what it would accept
# *before* the search runs, so the evidence gate and the queries agree.
RESEARCH_PLANNER_ENABLED = os.getenv("RESEARCH_PLANNER_ENABLED", "1") == "1"
PLANNER_QUERIES_PER_ITEM = int(os.getenv("PLANNER_QUERIES_PER_ITEM", "3"))

# Analytics defaults (overridable per-run from the dashboard).
BASE_BUY_THRESHOLD = 0.95      # price / 7D MA below this => potentially undervalued
INTRADAY_BUY_THRESHOLD = 0.985  # price / same-day MA; intraday flips move less
INTRADAY_Z_BUY = -0.5          # price must also sit this many sigma below its mean
DIP_VOLUME_FLOOR = 100_000_000  # weekly coins required for the dip shortlist
MIN_WEEKLY_VOLUME = 250_000    # coins traded per week; filters illiquid traps
MAX_SIGNALS_PER_SCAN = 10
DEFAULT_PURSE_COINS = 50_000_000  # first-run purse; the sidebar saves the real one
# Quant-only dips land around 0.35-0.45 confidence by construction (no external
# stream can lift confluence), so the floor only has to strip true noise.
AI_CONFIDENCE_FLOOR = 0.35
MODEL_TIMEOUT_SECONDS = 120


def mask(secret: str) -> str:
    """Return a safe representation of a secret for logs/UI."""
    if not secret:
        return "<missing>"
    return f"{secret[:4]}...{secret[-4:]}" if len(secret) > 8 else "<set>"
