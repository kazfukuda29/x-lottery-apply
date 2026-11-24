"""発見モード：監視していないのに抽選情報をよく流すアカウントを集計する。

ネット抽選（通販・オンライン・配送の語や、ネット専業の店名があるもの）の件数を優先して並べる。
"""

from __future__ import annotations

from datetime import datetime

from .rules import RuleEngine
from .util import iso, normalize, strip_urls


def is_online_post(post: dict, engine: RuleEngine) -> bool:
    """本文からネット抽選と判断できるか（店頭の語があれば違う）。AIは使わない。"""
    text_norm = normalize(strip_urls(post.get("text", "")))
    channel = engine.channel_of(text_norm)
    if channel != "unknown":
        return channel == "online"
    shops = engine.retailers.find(text_norm, post.get("urls") or [])
    return any(s in engine.retailers.online_only for s in shops)


def update_candidates(
    data: dict,
    posts: list[dict],
    users: dict[str, dict],
    watched: set[str],
    engine: RuleEngine,
    now: datetime,
) -> int:
    """候補一覧を更新し、今回カウントした投稿数を返す。"""
    by_handle = {c["handle"].lower(): c for c in data.get("candidates") or []}
    counted_ids = set(data.get("counted_post_ids") or [])
    counted = 0
    for post in posts:
        handle = (post.get("author") or "").lower()
        if not handle or handle in watched or post["id"] in counted_ids:
            continue
        if engine.classify(post, now).kind == "drop":
            continue
        user = users.get(post.get("author_id"), {})
        cand = by_handle.setdefault(
            handle,
            {"handle": post["author"], "name": user.get("name", ""), "hits": 0, "first_seen": iso(now)},
        )
        cand["hits"] += 1
        if is_online_post(post, engine):
            cand["online_hits"] = cand.get("online_hits", 0) + 1
        cand["last_seen"] = iso(now)
        cand["sample_url"] = post.get("url")
        cand["followers"] = (user.get("public_metrics") or {}).get("followers_count", cand.get("followers", 0))
        cand["description"] = (user.get("description") or "")[:80]
        counted_ids.add(post["id"])
        counted += 1
    # 監視に追加済みのアカウントは候補から外す
    candidates = [c for h, c in by_handle.items() if h not in watched]
    candidates.sort(key=lambda c: (-c.get("online_hits", 0), -c["hits"], -c.get("followers", 0)))
    data["candidates"] = candidates[:50]
    data["counted_post_ids"] = sorted(counted_ids, key=int)[-1000:]
    data["updated_at"] = iso(now)
    return counted
