"""通知：毎朝のダイジェスト（締切日ごと）と、締切が近い新着だけのすぐの通知。"""

from datetime import datetime, timedelta

from collector.notify import Notifier, urgent_new
from collector.util import JST, iso

NOW = datetime(2026, 11, 25, 7, 0, tzinfo=JST)


def item(i, end, shop="ヨドバシカメラ"):
    return {"id": f"i{i}", "product": f"商品{i}", "retailer": shop, "apply_end": iso(end) if end else None}


def at(days, hour=23, minute=59):
    d = NOW + timedelta(days=days)
    return d.replace(hour=hour, minute=minute)


def test_digest_groups_by_deadline_day_and_is_loud_when_something_ends_today():
    items = [item(1, at(0)), item(2, at(1, 10, 0)), item(3, at(2)), item(4, at(3)), item(5, at(5)), item(6, None)]
    n = Notifier(None, None, dry_run=True)
    n.digest(items, [], NOW, new_count=4)
    sent = n.sent[0]
    assert sent["title"] == "締切が近い抽選 11/25｜今日1件・明日1件・2〜3日後2件"
    assert sent["priority"] == 4 and sent["tags"] == ["rotating_light"]
    msg = sent["message"]
    assert msg.index("🔥今日まで（1件）") < msg.index("⚠️明日まで（1件）") < msg.index("📅あさってまで（1件）") < msg.index("📅3日後まで（1件）")
    assert "・23:59｜ヨドバシカメラ｜商品1" in msg and "・10:00｜ヨドバシカメラ｜商品2" in msg
    assert "商品5" not in msg and "商品6" not in msg  # 4日以上先・締切不明は件数だけ
    assert "新着 4件" in msg and "受付中 6件" in msg


def test_digest_is_quiet_when_nothing_ends_today():
    n = Notifier(None, None, dry_run=True)
    n.digest([item(1, at(5))], [], NOW)
    assert n.sent[0]["priority"] == 3
    assert "3日以内に締切の抽選はありません" in n.sent[0]["message"]


def test_only_new_items_ending_soon_are_sent_right_away():
    new = [item(1, NOW + timedelta(hours=20)), item(2, NOW + timedelta(hours=40)), item(3, None)]
    assert [i["id"] for i in urgent_new(new, NOW, 36)] == ["i1"]
    n = Notifier(None, None, dry_run=True)
    n.urgent_new_items(urgent_new(new, NOW, 36), NOW)
    n.urgent_new_items([], NOW)  # 該当がなければ送らない
    assert len(n.sent) == 1 and n.sent[0]["title"] == "締切が近い新着 1件" and n.sent[0]["priority"] == 4
    assert n.sent[0]["message"] == "・明日03:00｜ヨドバシカメラ｜商品1"
