"""ntfy でスマホに通知する。

ntfy.sh のトピックは名前を知っていれば誰でも購読できるため、推測されにくいランダムな名前にしてください。

通知の種類（既定）
- 毎朝のダイジェスト：締切が今日・明日・あさって・3日後の抽選を日ごとに分けて1通にまとめる
- 締切が近い新着：見つけた時点で締切が迫っている新着だけ、すぐ知らせる（それ以外の新着は翌朝のダイジェストへ）
"""

from __future__ import annotations

from datetime import datetime, timedelta

import requests

from .util import JST, parse_iso

GENRE_LABEL = {"tcg": "TCG", "game": "ゲーム", "hobby": "ホビー", "goods": "グッズ", "other": "その他"}
DAY_LABEL = {0: "今日", 1: "明日", 2: "あさって"}


def days_until(end: datetime, now: datetime) -> int:
    """締切が今日なら0、明日なら1（日本時間の日付で数える）。"""
    return (end.astimezone(JST).date() - now.astimezone(JST).date()).days


def day_label(days: int) -> str:
    return DAY_LABEL.get(days, f"{days}日後")


def fmt_deadline(value: str | None, now: datetime) -> str:
    end = parse_iso(value)
    if not end:
        return "締切不明"
    end = end.astimezone(JST)
    d = days_until(end, now)
    if d in (0, 1):
        return f"{day_label(d)}{end:%H:%M}まで"
    return f"{end:%m/%d %H:%M}まで"


# 通知は1行が短いほど読みやすいので、長いシリーズ名は略す（一覧ページは正式名のまま）
ABBREV = {
    "ポケモンカードゲーム": "ポケカ",
    "ワンピースカードゲーム": "ワンピカード",
    "ドラゴンボールスーパーカードゲーム": "DBカード",
}


def shorten(text: str, limit: int = 34) -> str:
    for long, short in ABBREV.items():
        text = text.replace(long, short)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def item_line(item: dict, now: datetime, *, time_only: bool = False) -> str:
    """「・23:59｜ヨドバシカメラ｜商品名」の形（締切を先頭にして見比べやすくする）。"""
    shop = item.get("retailer") or "店舗不明"
    end = parse_iso(item.get("apply_end"))
    if end and time_only:
        when = f"{end.astimezone(JST):%H:%M}"
    else:
        when = fmt_deadline(item.get("apply_end"), now).removesuffix("まで")
    return f"・{when}｜{shop}｜{shorten(item['product'])}"


def summarize(items: list[dict], now: datetime, limit: int = 8, *, time_only: bool = False) -> list[str]:
    lines = [item_line(i, now, time_only=time_only) for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"　ほか{len(items) - limit}件")
    return lines


def by_deadline(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: i.get("apply_end") or "9999")


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
        """新着をすべて知らせる（urgent_new_hours: -1 のとき）。"""
        if not items:
            return
        self.send(f"新着の抽選 {len(items)}件", "\n".join(summarize(by_deadline(items), now)))

    def urgent_new_items(self, items: list[dict], now: datetime) -> None:
        """見つけた時点で締切が迫っている新着（翌朝のダイジェストでは間に合わないかもしれないもの）。"""
        if not items:
            return
        self.send(
            f"締切が近い新着 {len(items)}件",
            "\n".join(summarize(by_deadline(items), now, limit=10)),
            priority=4,
            tags=["rotating_light"],
        )

    def closing_soon(self, items: list[dict], now: datetime, hours: int) -> None:
        if not items:
            return
        self.send(
            f"まもなく締切（{hours}時間以内） {len(items)}件",
            "\n".join(summarize(by_deadline(items), now)) + "\n※応募済みなら無視してOK",
            priority=4,
            tags=["alarm_clock"],
        )

    def digest(self, open_items: list[dict], stale_accounts: list[str], now: datetime,
               notes: list[str] | None = None, *, days: int = 3, new_count: int = 0) -> None:
        """毎朝のダイジェスト：締切が今日〜days日後の抽選を日ごとに分けて知らせる。"""
        buckets: dict[int, list[dict]] = {d: [] for d in range(days + 1)}
        for i in open_items:
            end = parse_iso(i.get("apply_end"))
            if end and end >= now and days_until(end, now) <= days:
                buckets[days_until(end, now)].append(i)
        counts = {d: len(v) for d, v in buckets.items()}
        later = sum(n for d, n in counts.items() if d >= 2)

        title = f"締切が近い抽選 {now:%m/%d}｜今日{counts.get(0, 0)}件・明日{counts.get(1, 0)}件"
        if days >= 2:
            title += f"・{'あさって' if days == 2 else f'2〜{days}日後'}{later}件"

        heads = {0: "🔥今日まで", 1: "⚠️明日まで"}
        limits = {0: 12, 1: 10}
        lines: list[str] = []
        for d in range(days + 1):
            items = by_deadline(buckets[d])
            if not items:
                continue
            lines.append(f"{heads.get(d, '📅' + day_label(d) + 'まで')}（{len(items)}件）")
            lines += summarize(items, now, limit=limits.get(d, 6), time_only=d <= 1)
        if not lines:
            lines.append(f"{days}日以内に締切の抽選はありません")
        if new_count:
            lines.append(f"🆕前回のダイジェスト以降の新着 {new_count}件（一覧で確認できます）")
        lines.append(f"受付中 {len(open_items)}件 ※応募済みも含みます")
        if stale_accounts:
            lines.append("⚠ しばらく投稿がない監視アカウント: " + ", ".join("@" + a for a in stale_accounts))
        lines.extend(notes or [])
        urgent = counts.get(0, 0) > 0
        self.send(title, "\n".join(lines), priority=4 if urgent else 3,
                  tags=["rotating_light"] if urgent else ["calendar"])


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


def urgent_new(items: list[dict], now: datetime, hours: int) -> list[dict]:
    """締切が hours 時間以内の新着。"""
    limit = now + timedelta(hours=hours)
    return [i for i in items if (end := parse_iso(i.get("apply_end"))) and now <= end <= limit]


__all__ = ["Notifier", "due_soon", "urgent_new", "fmt_deadline", "days_until", "JST"]
