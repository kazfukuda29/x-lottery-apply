"""X API v2 の Recent Search で投稿を集める。

- 監視アカウントを `(from:A OR from:B …)` でまとめて検索し、クエリ長の上限で分割する
- since_id で既に取った分は取り直さない（課金は返ってきた投稿の分だけ）
- 1日の取得上限を超えたら止める
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .util import JST, iso, parse_iso

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

TWEET_FIELDS = (
    "created_at,author_id,entities,public_metrics,in_reply_to_user_id,"
    "conversation_id,attachments,referenced_tweets,note_tweet,lang"
)
EXPANSIONS = "author_id,attachments.media_keys"
USER_FIELDS = "username,name,public_metrics,description"
MEDIA_FIELDS = "url,type,preview_image_url"


class XApiError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class SearchResult:
    posts: list[dict[str, Any]] = field(default_factory=list)
    newest_id: str | None = None
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched: int = 0
    stopped_reason: str | None = None


def query_key(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]


def build_watch_queries(
    accounts: list[dict], query_words: list[str], max_chars: int, exclude: list[str] | tuple = ()
) -> list[str]:
    """監視アカウントを、文字数上限に収まるようにいくつかのクエリへ分ける。"""
    filtered = [a["handle"] for a in accounts if a.get("filter", True)]
    unfiltered = [a["handle"] for a in accounts if not a.get("filter", True)]
    words = " OR ".join(query_words)
    excl = "".join(f" -{w}" for w in exclude)
    queries: list[str] = []
    for handles, suffix in (
        (filtered, (f" ({words})" if words else "") + excl + " -is:retweet"),
        (unfiltered, excl + " -is:retweet"),
    ):
        group: list[str] = []
        for h in handles:
            candidate = group + [h]
            if group and len(_from_clause(candidate) + suffix) > max_chars:
                queries.append(_from_clause(group) + suffix)
                group = [h]
            else:
                group = candidate
        if group:
            queries.append(_from_clause(group) + suffix)
    return queries


def _from_clause(handles: list[str]) -> str:
    parts = [f"from:{h.lstrip('@')}" for h in handles]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def normalize_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """APIの応答を扱いやすい投稿リストに変換する。"""
    includes = payload.get("includes") or {}
    users = {u["id"]: u for u in includes.get("users") or []}
    media = {m["media_key"]: m for m in includes.get("media") or []}
    posts = []
    for t in payload.get("data") or []:
        user = users.get(t.get("author_id"), {})
        handle = user.get("username", "")
        note = t.get("note_tweet") or {}
        text = note.get("text") or t.get("text", "")
        entities = (note.get("entities") if note else None) or t.get("entities") or {}
        urls = []
        for u in entities.get("urls") or []:
            expanded = u.get("unwound_url") or u.get("expanded_url") or u.get("url")
            if not expanded:
                continue
            # 投稿に添付した画像・動画へのリンクは除く
            if "/photo/" in expanded or "/video/" in expanded or u.get("media_key"):
                continue
            if expanded not in urls:
                urls.append(expanded)
        images = []
        for key in (t.get("attachments") or {}).get("media_keys") or []:
            m = media.get(key)
            if m and m.get("type") == "photo" and m.get("url"):
                images.append(m["url"])
        metrics = t.get("public_metrics") or {}
        created = parse_iso(t.get("created_at"))
        posts.append(
            {
                "id": t["id"],
                "author_id": t.get("author_id"),
                "author": handle,
                "author_name": user.get("name", ""),
                "created_at": iso(created),
                "text": text,
                "urls": urls,
                "images": images,
                "likes": metrics.get("like_count", 0),
                "reposts": metrics.get("retweet_count", 0) + metrics.get("quote_count", 0),
                "in_reply_to_user_id": t.get("in_reply_to_user_id"),
                "url": f"https://x.com/{handle or 'i'}/status/{t['id']}",
            }
        )
    return posts, users


def is_own_thread_or_original(post: dict[str, Any]) -> bool:
    """他人へのリプライは除外。自分の投稿への返信（スレッドの続き）は残す。"""
    reply_to = post.get("in_reply_to_user_id")
    return reply_to is None or reply_to == post.get("author_id")


class Budget:
    """1日の取得件数を state に記録して上限を守る。"""

    def __init__(self, state: dict, limit: int, today: str):
        usage = state.setdefault("x_usage", {})
        if usage.get("date") != today:
            usage.clear()
            usage.update({"date": today, "posts": 0, "requests": 0})
        self.usage = usage
        self.limit = limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.usage["posts"])

    def add(self, posts: int) -> None:
        self.usage["posts"] += posts
        self.usage["requests"] += 1


class XClient:
    def __init__(self, bearer_token: str, budget: Budget, session: requests.Session | None = None):
        if not bearer_token:
            raise XApiError("X_BEARER_TOKEN が設定されていません")
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {bearer_token}"
        self.budget = budget

    def search(
        self,
        query: str,
        *,
        since_id: str | None = None,
        start_time: datetime | None = None,
        max_results: int = 50,
        max_total: int = 100,
    ) -> SearchResult:
        result = SearchResult()
        next_token = None
        while True:
            remaining = min(self.budget.remaining, max_total - result.fetched)
            if remaining < 10:
                # Recent Search は1回10件以上が必須。上限に達したら止める
                if self.budget.remaining < 10:
                    result.stopped_reason = "daily_budget"
                break
            params: dict[str, Any] = {
                "query": query,
                "max_results": max(10, min(100, max_results, remaining)),
                "tweet.fields": TWEET_FIELDS,
                "expansions": EXPANSIONS,
                "user.fields": USER_FIELDS,
                "media.fields": MEDIA_FIELDS,
            }
            if since_id:
                params["since_id"] = since_id
            elif start_time:
                params["start_time"] = start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if next_token:
                params["next_token"] = next_token
            resp = self.session.get(SEARCH_URL, params=params, timeout=30)
            if resp.status_code == 429:
                result.stopped_reason = "rate_limited"
                break
            if resp.status_code in (401, 403):
                raise XApiError(
                    f"X APIの認証または権限エラー（{resp.status_code}）。Bearer Tokenとクレジット残高を確認してください: {resp.text[:300]}"
                )
            if resp.status_code == 402:
                raise XApiError(f"X APIのクレジットが不足しています: {resp.text[:300]}")
            if not resp.ok:
                raise XApiError(f"X APIエラー {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            posts, users = normalize_response(payload)
            meta = payload.get("meta") or {}
            self.budget.add(len(posts))
            result.fetched += len(posts)
            result.posts.extend(posts)
            result.users.update(users)
            if meta.get("newest_id") and (
                result.newest_id is None or int(meta["newest_id"]) > int(result.newest_id)
            ):
                result.newest_id = meta["newest_id"]
            next_token = meta.get("next_token")
            if not next_token:
                break
        return result


class FixtureXClient:
    """--dry-run 用。tests/fixtures の応答を返し、課金は発生しない。"""

    def __init__(self, fixture_path: Path, budget: Budget):
        with open(fixture_path, encoding="utf-8") as f:
            self.fixture = json.load(f)
        self.budget = budget

    def search(self, query: str, **_: Any) -> SearchResult:
        # 監視クエリは from: で始まる（発見モードの -from: は除外指定）
        key = "watch" if re.search(r"(?<!-)from:", query) else "discover"
        payload = self.fixture.get(key) or {}
        posts, users = normalize_response(payload)
        self.budget.add(len(posts))
        newest = max((p["id"] for p in posts), key=int, default=None)
        return SearchResult(posts=posts, newest_id=newest, users=users, fetched=len(posts))


def first_run_start(now: datetime, hours: int) -> datetime:
    return (now - timedelta(hours=hours)).astimezone(JST)
