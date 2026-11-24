"""ntfy でスマホに通知する。

ntfy.sh のトピックは名前を知っていれば誰でも購読できるため、推測されにくいランダムな名前にしてください。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import requests

from .util import JST, parse_iso

GENRE_LABEL = {"tcg": "TCG", "game": "ゲーム", "hobby": "ホビー", "goods": "グッズ", "other": "その他"}


def fmt_deadline(value: str | None, now: datetime) -> str:
    end = parse_iso(value)
    if not end:
        return "締切不明"
    if end.date() == now.date():
        return f"本日{end:%H:%M}まで"
    if end.date() == (now + timedelta(days=1)).date():
        return f"明日{end:%H:%M}まで"
    return f"{end:%m/%d %H:%M}まで"


def item_line(item: dict, now: datetime) -> str:
    shop = item.get("retailer") or "店舗不明"
    return f"・{shop}｜{item['product']}（{fmt_deadline(item.get('apply_end'), now)}）"


def summarize(items: list[dict], now: datetime, limit: int = 8) -> str:
    lines = [item_line(i, now) for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"ほか{len(items) - limit}件")
    return "\n".join(lines)


class Notifier:
    def __init__(self, topic: str | None, site_url: str | None, server: str = "https://ntfy.sh", dry_run: bool = False):
        self.topic = topic
        self.site_url = site_url
        self.server = server.rstrip("/")
        self.dry_run = dry_run
        self.sent: list[dict] = []

    def send(self, title: str, message: str, *, priority: int = 3, tags: list[str] | None = None) -> None:
        body = {
            "topic": self.topic or "",
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or ["tickets"],
        }
        if self.site_url:
            body["click"] = self.site_url
        self.sent.append(body)
        if self.dry_run or not self.topic:
            print(f"[notify] {title}\n{message}\n")
            return
        try:
            resp = requests.post(self.server, json=body, timeout=15)
            if not resp.ok:
                print(f"[notify] ntfyへの送信に失敗: {resp.status_code} {resp.text[:200]}")
        except requests.RequestException as e:
            print(f"[notify] ntfyへの送信に失敗: {e}")

    # -------------------------------------------------------- 通知の種類

    def new_items(self, items: list[dict], now: datetime) -> None:
        if not items:
            return
        items = sorted(items, key=lambda i: i.get("apply_end") or "9999")
        self.send(f"新着の抽選 {len(items)}件", summarize(items, now))

    def closing_soon(self, items: list[dict], now: datetime, hours: int) -> None:
        if not items:
            return
        self.send(
            f"まもなく締切（{hours}時間以内） {len(items)}件",
            summarize(items, now) + "\n※応募済みなら無視してOK",
            priority=4,
            tags=["alarm_clock"],
        )

    def digest(self, open_items: list[dict], stale_accounts: list[str], now: datetime) -> None:
        today = [i for i in open_items if (parse_iso(i.get("apply_end")) or now + timedelta(days=99)) <= now + timedelta(hours=24)]
        today.sort(key=lambda i: i.get("apply_end") or "")
        lines = [f"受付中 {len(open_items)}件 / 24時間以内に締切 {len(today)}件"]
        if today:
            lines.append(summarize(today, now))
        if stale_accounts:
            lines.append("⚠ しばらく投稿がない監視アカウント: " + ", ".join("@" + a for a in stale_accounts))
        self.send(f"抽選ダイジェスト {now:%m/%d}", "\n".join(lines), tags=["calendar"])


def due_soon(items: list[dict], now: datetime, hours: int, alerted: set[str]) -> list[dict]:
    if hours <= 0:
        return []
    limit = now + timedelta(hours=hours)
    out = []
    for i in items:
        end = parse_iso(i.get("apply_end"))
        if end and now <= end <= limit and i["id"] not in alerted:
            out.append(i)
    return out


__all__ = ["Notifier", "due_soon", "fmt_deadline", "JST"]
