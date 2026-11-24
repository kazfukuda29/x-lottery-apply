"""監視アカウントの評価と、発見モードのネット抽選の数え方。"""

from datetime import datetime, timedelta

from collector.discover import is_online_post
from collector.evaluate import evaluate, record_fetch, summary_line, update_log
from collector.rules import RuleEngine
from collector.util import JST, iso, load_yaml

NOW = datetime(2026, 12, 1, 7, 0, tzinfo=JST)


def log_with(reports: dict[str, list[str]]) -> dict:
    """{抽選番号: [伝えたアカウント]} から記録を作る。"""
    first = iso(NOW - timedelta(days=3))
    return {
        "started": iso(NOW - timedelta(days=10)),
        "items": {k: {"authors": v, "first_seen": first} for k, v in reports.items()},
        "fetched": {"2026-11-30": {"a": 10, "b": 4, "c": 2}},
    }


def test_fewest_accounts_that_cover_most_online_lotteries():
    reports = {str(i): ["a"] for i in range(1, 9)}
    reports["1"] = ["a", "b", "c"]
    reports["2"] = ["a", "b", "c"]
    reports["3"] = ["a", "b"]
    reports["9"] = ["b"]
    ev = evaluate(log_with(reports), ["A", "B", "C"], NOW)
    assert ev["total"] == 9 and ev["days_observed"] == 11
    assert [c["handle"] for c in ev["cover"]] == ["A", "B"]
    assert ev["keep"] == ["A", "B"] and ev["drop"] == ["C"]  # A だけだと 8/9 で目標95%に届かない
    assert ev["accounts"]["C"] == {"online": 2, "only": 0, "posts_per_day": 2.0}
    assert ev["accounts"]["B"]["only"] == 1
    line = summary_line(ev)
    assert "@A・@B の2つで100%を網羅" in line and "@C（単独0件）" in line


def test_log_counts_online_items_and_follows_merges():
    log: dict = {}
    end = iso(NOW + timedelta(days=2))
    items = [
        {"id": "x", "channel": "online", "apply_end": end, "first_seen": iso(NOW), "sources": [{"author": "A"}]},
        {"id": "y", "channel": "unknown", "apply_end": end, "first_seen": iso(NOW), "sources": [{"author": "B"}]},
    ]
    update_log(log, items, {}, NOW)
    assert set(log["items"]) == {"x"}  # ネット抽選だけを数える
    # x が z にまとめられたら、伝えたアカウントも z に寄せる
    items = [{"id": "z", "channel": "online", "apply_end": end, "first_seen": iso(NOW), "sources": [{"author": "B"}]}]
    update_log(log, items, {"x": "z"}, NOW)
    assert log["items"]["z"]["authors"] == ["a", "b"] and "x" not in log["items"]
    # 締切前なのに一覧から消えた（店頭抽選として除外された）ものは数えない
    update_log(log, [], {}, NOW)
    assert log["items"] == {}


def test_fetch_counts_are_kept_per_day():
    log: dict = {}
    record_fetch(log, [{"author": "A"}, {"author": "a"}, {"author": "B"}], "2026-12-01")
    assert log["fetched"]["2026-12-01"] == {"a": 2, "b": 1}


def test_discover_counts_online_lottery_posts():
    cfg = load_yaml("retailers.yml")
    engine = RuleEngine(load_yaml("keywords.yml"), cfg["retailers"], cfg["entry_platforms"])
    assert is_online_post({"text": "通販で抽選受付中！送料無料"}, engine)
    assert is_online_post({"text": "プレミアムバンダイで抽選販売"}, engine)
    assert not is_online_post({"text": "店頭で抽選受付、店頭受取のみ"}, engine)
    assert not is_online_post({"text": "抽選受付中"}, engine)
