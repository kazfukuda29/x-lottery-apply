"""抽出結果を items.json に統合する。

- 「商品 × 店」が同じで締切も同じ（または片方不明）なら1件にまとめる（リマインド投稿・別のまとめアカウントの重複）
- 店が違えば別の抽選（＝別の応募チャンス）として残し、画面では商品ごとにまとめて表示する
- 期限を過ぎたものは片付ける
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from .rules import RetailerMatcher
from .util import JST, iso, norm_key, normalize, parse_iso


def parse_ai_datetime(value: str, *, end: bool) -> datetime | None:
    if not value:
        return None
    dt = parse_iso(value)
    if dt is None:
        return None
    if len(value.strip()) <= 10 and end:  # 日付だけなら締切は 23:59
        dt = dt.replace(hour=23, minute=59)
    return dt


def _url_at(urls: list[str], index: int) -> str | None:
    return urls[index] if isinstance(index, int) and 0 <= index < len(urls) else None


def fields_from_ai(ai: dict, post: dict, retailers: RetailerMatcher) -> dict[str, Any]:
    """AIの出力を item の形に。channel は後で RuleEngine.channel_of で確定する。"""
    urls = post.get("urls") or []
    retailer = retailers.canonical(ai.get("retailer", ""))
    apply_url = _url_at(urls, ai.get("apply_url_index", -1))
    if not apply_url:
        apply_url = retailers.domain_of(retailer, urls) if retailer else retailers.platform_url(urls)
    detail_url = _url_at(urls, ai.get("detail_url_index", -1))
    if detail_url == apply_url:
        detail_url = None
    announce = (ai.get("announce_date") or "")[:10] or None
    return {
        "product": (ai.get("product") or "").strip(),
        "retailer": retailer,
        "genre": ai.get("genre") or "other",
        "channel": ai.get("channel") or "unknown",
        "apply_start": parse_ai_datetime(ai.get("apply_start", ""), end=False),
        "apply_end": parse_ai_datetime(ai.get("apply_end", ""), end=True),
        "apply_url": apply_url,
        "detail_url": detail_url,
        "requirements": [r for r in ai.get("requirements") or [] if r][:6],
        "announce_date": announce,
        "same_as_item_id": ai.get("same_as_item_id") or "",
        "extracted_by": "ai",
    }


def source_of(post: dict) -> dict[str, Any]:
    return {
        "post_id": post["id"],
        "author": post.get("author", ""),
        "url": post.get("url"),
        "created_at": post.get("created_at"),
        "likes": post.get("likes", 0),
        "reposts": post.get("reposts", 0),
    }


class ItemStore:
    def __init__(self, data: dict, generic_words: list[str] | None = None):
        self.items: list[dict] = data.get("items") or []
        self.review: list[dict] = data.get("review") or []
        self.generic = {norm_key(w) for w in generic_words or []}

    # ------------------------------------------------------------ 照合

    def canonical_product(self, name: str) -> str:
        """表記ゆれのある商品名を、既存の商品名にそろえる。"""
        key = norm_key(name)
        if not key:
            return name
        best, best_score = None, 0.0
        for existing in {i["product"] for i in self.items if i.get("product")}:
            ek = norm_key(existing)
            if ek == key:
                return existing
            short, long_ = sorted((key, ek), key=len)
            score = 0.0
            if len(short) >= 5 and short not in self.generic and short in long_:
                score = 0.9 + len(short) / len(long_) / 10
            else:
                ratio = SequenceMatcher(None, key, ek).ratio()
                if ratio >= 0.85:
                    score = ratio
            if score > best_score:
                best, best_score = existing, score
        return best or name

    def rename_product(self, old: str, new: str) -> None:
        for item in self.items:
            if item.get("product") == old:
                item["product"] = new
                item["product_key"] = norm_key(new)

    def find_match(self, fields: dict) -> dict | None:
        same = fields.get("same_as_item_id")
        if same:
            for item in self.items:
                if item["id"] == same:
                    return item
        pkey = norm_key(fields["product"])
        rkey = norm_key(fields["retailer"])
        for item in self.items:
            if item.get("product_key") != pkey or norm_key(item.get("retailer", "")) != rkey:
                continue
            a, b = parse_iso(item.get("apply_end")), fields.get("apply_end")
            if a and b and abs((a - b).total_seconds()) > 36 * 3600:
                continue  # 締切が大きく違えば別の抽選（第2弾など）
            return item
        return None

    # ------------------------------------------------------------ 追加・更新

    def upsert(self, fields: dict, post: dict, now: datetime) -> tuple[dict | None, bool]:
        if not fields.get("product"):
            return None, False
        end = fields.get("apply_end")
        if end and end < now:
            return None, False  # 締切を過ぎた抽選は登録しない
        fields = dict(fields)
        original = fields["product"]
        canonical = self.canonical_product(original)
        if (
            canonical != original
            and fields.get("extracted_by") == "ai"
            and len(norm_key(original)) > len(norm_key(canonical))
        ):
            # ルールで取った短い名前より、AIの説明的な名前を商品名として採用する
            self.rename_product(canonical, original)
            canonical = original
        fields["product"] = canonical
        match = self.find_match(fields)
        if match:
            self._merge(match, fields, post, now)
            return match, False
        item = self._new_item(fields, post, now)
        self.items.append(item)
        return item, True

    def _new_item(self, f: dict, post: dict, now: datetime) -> dict:
        pkey = norm_key(f["product"])
        rkey = norm_key(f["retailer"])
        digest = hashlib.sha1(f"{pkey}|{rkey}|{post['id']}".encode()).hexdigest()[:10]
        src = source_of(post)
        return {
            "id": "i" + digest,
            "product": f["product"],
            "product_key": pkey,
            "genre": f.get("genre") or "other",
            "channel": f.get("channel") or "unknown",
            "retailer": f["retailer"],
            "apply_start": iso(f.get("apply_start")),
            "apply_end": iso(f.get("apply_end")),
            "apply_url": f.get("apply_url"),
            "detail_url": f.get("detail_url"),
            "requirements": f.get("requirements") or [],
            "announce_date": f.get("announce_date"),
            "sources": [src],
            "buzz": src["likes"] + src["reposts"],
            "extracted_by": f.get("extracted_by", "rule"),
            "first_seen": iso(now),
            "updated_at": iso(now),
        }

    def _merge(self, item: dict, f: dict, post: dict, now: datetime) -> None:
        if all(s["post_id"] != post["id"] for s in item["sources"]):
            item["sources"].append(source_of(post))
            item["sources"] = item["sources"][-8:]
        ai_over_rule = item.get("extracted_by") == "rule" and f.get("extracted_by") == "ai"
        for key in ("apply_start", "apply_end"):
            if f.get(key) and (not item.get(key) or ai_over_rule):
                item[key] = iso(f[key])
        for key in ("apply_url", "detail_url", "announce_date", "retailer"):
            if f.get(key) and (not item.get(key) or ai_over_rule):
                item[key] = f[key]
        if item.get("genre") in (None, "other") and f.get("genre"):
            item["genre"] = f["genre"]
        if item.get("channel") in (None, "unknown") and f.get("channel") in ("online", "store"):
            item["channel"] = f["channel"]
        for r in f.get("requirements") or []:
            if r not in item["requirements"]:
                item["requirements"].append(r)
        if ai_over_rule:
            item["extracted_by"] = "ai"
        item["buzz"] = max(s.get("likes", 0) + s.get("reposts", 0) for s in item["sources"])
        item["updated_at"] = iso(now)

    def add_review(self, post: dict, reason: str, now: datetime) -> None:
        if any(r["post_id"] == post["id"] for r in self.review):
            return
        self.review.append(
            {
                "post_id": post["id"],
                "author": post.get("author", ""),
                "url": post.get("url"),
                "created_at": post.get("created_at"),
                "reason": reason,
                "added_at": iso(now),
            }
        )

    # ------------------------------------------------------------ 片付け

    def cleanup(self, now: datetime, keep_days_after_close: int, keep_days_undated: int, keep_days_review: int) -> int:
        before = len(self.items)
        kept = []
        for item in self.items:
            end = parse_iso(item.get("apply_end"))
            announce = parse_iso(item.get("announce_date"))
            if end:
                last = max(end, announce.replace(hour=23, minute=59) if announce else end)
                if now <= last + timedelta(days=keep_days_after_close):
                    kept.append(item)
            else:
                first = parse_iso(item.get("first_seen")) or now
                if now <= first + timedelta(days=keep_days_undated):
                    kept.append(item)
        self.items = kept
        self.review = [
            r for r in self.review
            if now <= (parse_iso(r.get("added_at")) or now) + timedelta(days=keep_days_review)
        ]
        return before - len(self.items)

    def to_dict(self) -> dict:
        def sort_key(i: dict) -> tuple:
            return (i.get("apply_end") or "9999", i.get("product_key", ""))

        return {"items": sorted(self.items, key=sort_key), "review": self.review}


def open_items(items: list[dict], now: datetime) -> list[dict]:
    """受付中または受付前（締切前）のアイテム。"""
    out = []
    for i in items:
        end = parse_iso(i.get("apply_end"))
        if end is None or end >= now:
            out.append(i)
    return out
