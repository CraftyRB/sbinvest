"""GitHub feed for early patch leaks.

Polls commits on the NotEnoughUpdates-REPO (the community item database) so new
or re-balanced items can be spotted before the wider market reacts. The GitHub
API returns per-file patches; we keep a compact, token-friendly digest.
"""

from __future__ import annotations

import re

import requests

import config
import database

API = "https://api.github.com"
MAX_DIFF_CHARS = 12_000
MAX_FILES = 12
ITEM_PATH = re.compile(r"(?:^|/)items/.*\.json$", re.IGNORECASE)


class GitHubError(RuntimeError):
    pass


def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "skyblock-ai-dashboard/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return headers


def _get(url: str, params: dict | None = None, timeout: int = 25) -> dict | list:
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise GitHubError(f"GitHub request failed: {exc}") from exc
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubError("GitHub rate limit exhausted (set GITHUB_TOKEN in env.txt)")
    if resp.status_code != 200:
        raise GitHubError(f"GitHub returned HTTP {resp.status_code}")
    return resp.json()


def fetch_recent_commits(limit: int = 5) -> list[dict]:
    data = _get(
        f"{API}/repos/{config.GITHUB_REPO}/commits",
        params={"per_page": max(1, min(limit, 30))},
    )
    return data if isinstance(data, list) else []


def fetch_commit_detail(sha: str) -> dict:
    data = _get(f"{API}/repos/{config.GITHUB_REPO}/commits/{sha}")
    return data if isinstance(data, dict) else {}


def _digest_files(detail: dict) -> tuple[list[dict], str, list[str]]:
    files: list[dict] = []
    diff_parts: list[str] = []
    changed_items: list[str] = []

    for f in (detail.get("files") or [])[:MAX_FILES]:
        filename = f.get("filename", "")
        patch = f.get("patch") or ""
        files.append(
            {
                "filename": filename,
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "changes": f.get("changes"),
            }
        )
        if ITEM_PATH.search(filename):
            item_id = filename.rsplit("/", 1)[-1].replace(".json", "")
            changed_items.append(item_id)
        if patch:
            diff_parts.append(f"--- {filename} ({f.get('status')})\n{patch[:3000]}")

    diff = "\n\n".join(diff_parts)
    return files, diff[:MAX_DIFF_CHARS], changed_items


def build_leak(detail: dict) -> dict:
    commit = detail.get("commit") or {}
    author = (commit.get("author") or {}).get("name") or ""
    files, diff, items = _digest_files(detail)
    return {
        "sha": detail.get("sha", ""),
        "ts": (commit.get("author") or {}).get("date"),
        "title": (commit.get("message") or "").split("\n", 1)[0][:200],
        "author": author,
        "files": files,
        "diff": diff,
        "summary": (commit.get("message") or "")[:500],
        "items": items,
    }


def collect_new_leaks(commit_limit: int = 3) -> list[dict]:
    """Return not-yet-seen leak commits, newest first."""
    leaks: list[dict] = []
    for summary in fetch_recent_commits(limit=commit_limit):
        sha = summary.get("sha")
        if not sha or database.saw_leak(sha):
            continue
        detail = fetch_commit_detail(sha)
        leak = build_leak(detail)
        if not leak["diff"]:
            continue
        database.save_leak(leak)
        leaks.append(leak)
    return leaks
