"""監視アカウントの評価。

ネット抽選（channel が online のもの）を、どのアカウントが伝えたかを期間で記録し、
- アカウントごとの件数と、そのアカウントだけが伝えた件数（単独）
- 取得した投稿数（X APIの費用の目安）
- 少ないアカウントで網羅できる組み合わせ（貪欲法で、網羅率が目標に届くまで1つずつ選ぶ）
を出す。data/account_log.json に保存する。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .util import iso, parse_iso

# これ未満の日数の評価は参考値（見直しの通知もしない）
REVIEW_MIN_DAYS = 7


def update_log(log: dict, items: list[dict], merged: dict[str, str], now: datetime, keep_days: int = 30) -> None:
    """掲載中のアイテムから「ネット抽選ごとに伝えたアカウント」を足し込む。"""
    entries: dict[str, dict] = log.setdefault("items", {})
    # 同じ抽選としてまとめられたものは、まとめ先に寄せる
    for old, new in merged.items():
        if old in entries:
            src = entries.pop(old)
            dst = entries.setdefault(new, {"authors": [], "first_seen": src.get("first_seen"), "apply_end": src.get("apply_end")})
            dst["authors"] = sorted(set(dst["authors"]) | set(src["authors"]))
            dst["first_seen"] = min(filter(None, [dst.get("first_seen"), src.get("first_seen")]), default=None)
    current = {i["id"]: i for i in items}
    for item in items:
        authors = {(s.get("author") or "").lower() for s in item.get("sources") or []} - {""}
        if item.get("channel") != "online":
            entries.pop(item["id"], None)  # 後から店頭・不明と判断し直されたもの
            continue
        e = entries.setdefault(item["id"], {"authors": [], "first_seen": item.get("first_seen")})
        e["authors"] = sorted(set(e["authors"]) | authors)
        e["apply_end"] = item.get("apply_end")
        e["label"] = f"{item.get('retailer') or ''}｜{item.get('product') or ''}"[:60]
    # 締切前なのに一覧から消えたもの（店頭抽選として除外されたなど）は数えない
    for item_id in list(entries):
        if item_id in current:
            continue
        end = parse_iso(entries[item_id].get("apply_end"))
        if end and end > now:
            entries.pop(item_id)
    # 古い記録を片付ける
    limit = now - timedelta(days=keep_days)
    for item_id in list(entries):
        first = parse_iso(entries[item_id].get("first_seen"))
        if first and first < limit:
            entries.pop(item_id)
    log.setdefault("started", iso(now))


def record_fetch(log: dict, posts: list[dict], today: str, keep_days: int = 30) -> None:
    """X APIから取得した投稿数をアカウント別・日別に数える（費用の目安）。"""
    fetched: dict[str, dict[str, int]] = log.setdefault("fetched", {})
    day = fetched.setdefault(today, {})
    for p in posts:
        h = (p.get("author") or "").lower()
        if h:
            day[h] = day.get(h, 0) + 1
    for d in sorted(fetched)[:-keep_days]:
        fetched.pop(d)


def evaluate(log: dict, accounts: list[str], now: datetime, window_days: int = 14, target: float = 0.95) -> dict:
    """直近 window_days 日のネット抽選で、アカウントを評価する。"""
    handles = {a.lower(): a for a in accounts}
    since = now - timedelta(days=window_days)
    lotteries: list[set[str]] = []
    for e in (log.get("items") or {}).values():
        first = parse_iso(e.get("first_seen"))
        if first and first < since:
            continue
        who = set(e.get("authors") or []) & handles.keys()
        if who:
            lotteries.append(who)

    started = parse_iso(log.get("started")) or now
    days_observed = max(1, min(window_days, (now - started).days + 1))
    recent_days = sorted(log.get("fetched") or {})[-window_days:]
    stats = {}
    for h, name in handles.items():
        reported = sum(1 for w in lotteries if h in w)
        only = sum(1 for w in lotteries if w == {h})
        posts = sum((log["fetched"][d] or {}).get(h, 0) for d in recent_days)
        stats[name] = {
            "online": reported,
            "only": only,
            "posts_per_day": round(posts / max(1, len(recent_days)), 1),
        }

    # 少ないアカウントで網羅する組み合わせ（毎回いちばん多く新しく足せるアカウントを選ぶ）
    total = len(lotteries)
    uncovered = list(range(total))
    cover = []
    remaining = set(handles)
    while uncovered and remaining:
        def gain(h: str) -> int:
            return sum(1 for i in uncovered if h in lotteries[i])
        best = max(remaining, key=lambda h: (gain(h), -stats[handles[h]]["posts_per_day"]))
        g = gain(best)
        if g == 0:
            break
        uncovered = [i for i in uncovered if best not in lotteries[i]]
        remaining.discard(best)
        cover.append({"handle": handles[best], "adds": g, "coverage": round((total - len(uncovered)) / total, 3)})

    keep: list[str] = []
    for c in cover:
        keep.append(c["handle"])
        if c["coverage"] >= target:
            break
    drop = [handles[h] for h in handles if handles[h] not in keep]
    return {
        "window_days": window_days,
        "days_observed": days_observed,
        "target": target,
        "total": total,
        "accounts": stats,
        "cover": cover,
        "keep": keep,
        "drop": drop,
        "evaluated_at": iso(now),
    }


def summary_line(ev: dict) -> str | None:
    """毎週のダイジェストに添える1行。"""
    if not ev or not ev.get("total"):
        return None
    keep = "・".join("@" + h for h in ev["keep"])
    cov = ev["cover"][len(ev["keep"]) - 1]["coverage"] if ev["keep"] else 0
    line = f"📊アカウント評価（直近{ev['days_observed']}日・ネット抽選{ev['total']}件）：{keep} の{len(ev['keep'])}つで{cov:.0%}を網羅"
    if ev["drop"]:
        drops = "、".join(f"@{h}（単独{ev['accounts'][h]['only']}件）" for h in ev["drop"])
        line += f"。外す候補：{drops}"
    if ev["days_observed"] < REVIEW_MIN_DAYS:
        line += "（7日未満なので参考値）"
    return line


def weekly_review(ev: dict, history: dict) -> tuple[str, str, int] | None:
    """週1回の評価のあとに、アカウントを見直すタイミングかを判断し、通知（件名・本文・優先度）を返す。

    history は state に保存する記録（前週の外す候補、通知済みのアカウント、初回の通知済みか）。
    - 7日分以上そろった週に、前週に続いて「外す候補」になったアカウントがあれば「見直しのタイミング」。
      同じアカウントで毎週は鳴らさない（外す候補から外れたら、また数え直す）
    - 初めて7日分以上そろった週は、外す候補がなくても結果を知らせる
    """
    if not ev or not ev.get("total"):
        return None  # データがない週は判断しない（全アカウントが外す候補に見えてしまうため）
    drop = list(ev["drop"])
    drop_l = {h.lower() for h in drop}
    prev = set(history.get("last_drop") or [])
    notified = {h for h in history.get("notified") or [] if h in drop_l}
    history["last_drop"] = sorted(drop_l)
    ready = ev["days_observed"] >= REVIEW_MIN_DAYS

    repeat = [h for h in drop if h.lower() in prev]
    keep_line = summary_keep(ev)
    notice = None
    if ready and any(h.lower() not in notified for h in repeat):
        stats = ev["accounts"]
        lines = ["2週続けて「外す候補」になったアカウントがあります。"]
        lines += [f"・@{h}（ネット抽選{stats[h]['online']}件・単独{stats[h]['only']}件）" for h in repeat]
        lines += [keep_line, "", "Claudeに「アカウントを見直して」と伝えると、入れ替え案を出して反映します。"]
        notice = ("🔔アカウント見直しのタイミングです", "\n".join(lines), 4)
        notified |= {h.lower() for h in repeat}
    elif ready and not history.get("ready_notified"):
        lines = [keep_line]
        if drop:
            lines.append("外す候補：" + "、".join(f"@{h}" for h in drop) + "（1週目）。来週も候補なら、見直しのタイミングとしてお知らせします。")
        else:
            lines.append("外す候補はありません。今の組み合わせのままで大丈夫です。")
        notice = ("📊アカウント評価のデータがそろいました", "\n".join(lines), 3)
    if ready:
        history["ready_notified"] = True
    history["notified"] = sorted(notified)
    return notice


def summary_keep(ev: dict) -> str:
    cov = ev["cover"][len(ev["keep"]) - 1]["coverage"] if ev["keep"] else 0
    keep = "・".join("@" + h for h in ev["keep"])
    return f"直近{ev['days_observed']}日のネット抽選{ev['total']}件を、{keep} の{len(ev['keep'])}つで{cov:.0%}網羅。"
