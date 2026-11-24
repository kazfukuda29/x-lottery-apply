"""監視アカウントの評価と、発見モードのネット抽選の数え方。"""

import argparse
from datetime import datetime, timedelta

from collector.discover import is_online_post
from collector.evaluate import evaluate, record_fetch, summary_line, update_log, weekly_review
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


def week_eval(days: int, drop: list[str]) -> dict:
    accounts = {h: {"online": 5, "only": 0, "posts_per_day": 1.0} for h in ["A", "B", *drop]}
    return {"total": 10, "days_observed": days, "keep": ["A", "B"], "drop": drop, "accounts": accounts,
            "cover": [{"handle": "A", "adds": 8, "coverage": 0.8}, {"handle": "B", "adds": 2, "coverage": 1.0}]}


def test_review_notice_when_drop_candidate_two_weeks_in_a_row():
    history: dict = {}
    # 1週目（6日分）は参考値なので知らせない
    assert weekly_review(week_eval(6, ["C"]), history) is None
    # 2週目（13日分）も外す候補 → 見直しのタイミング
    title, body, priority = weekly_review(week_eval(13, ["C"]), history)
    assert "見直しのタイミング" in title and priority == 4
    assert "@C（ネット抽選5件・単独0件）" in body and "アカウントを見直して" in body
    assert "@A・@B の2つで100%網羅" in body
    # 同じアカウントで毎週は鳴らさない
    assert weekly_review(week_eval(14, ["C"]), history) is None
    # 外す候補から外れて、また2週続いたら知らせる
    assert weekly_review(week_eval(14, []), history) is None
    assert weekly_review(week_eval(14, ["C"]), history) is None
    assert weekly_review(week_eval(14, ["C"]), history) is not None


def test_first_full_week_is_reported_even_without_drop_candidates():
    history: dict = {"last_drop": ["c"]}
    title, body, priority = weekly_review(week_eval(13, []), history)
    assert "そろいました" in title and priority == 3 and "外す候補はありません" in body
    assert weekly_review(week_eval(14, []), history) is None  # 2回目以降は、2週続いたときだけ
    # データがない週は判断しない
    assert weekly_review({"total": 0, "drop": ["A"]}, history) is None and history["last_drop"] == []


def test_first_full_week_with_new_drop_candidate_says_it_will_follow_up():
    title, body, _ = weekly_review(week_eval(13, ["C"]), {})
    assert "そろいました" in title and "@C（1週目）" in body


def test_monday_digest_sends_review_notice(tmp_path, monkeypatch):
    from collector.main import Runner

    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    args = argparse.Namespace(dry_run=True, data_dir=str(tmp_path / "data"), now="2026-12-07T07:30",
                              mode="auto", out=str(tmp_path / "_site"))
    r = Runner(args)
    handles = [a["handle"] for a in r.accounts]
    first, closed = iso(r.now - timedelta(days=5)), iso(r.now - timedelta(days=1))
    # 先頭のアカウントだけで全部を伝えている（ほかは外す候補）
    r.account_log = {
        "started": iso(r.now - timedelta(days=12)),
        "items": {str(n): {"authors": [handles[0].lower()], "first_seen": first, "apply_end": closed} for n in range(5)},
    }
    r.state["account_review"] = {"last_drop": [h.lower() for h in handles[1:]]}
    r.maybe_digest(force=True)
    titles = [m["title"] for m in r.notifier.sent]
    assert any("見直しのタイミング" in t for t in titles)
    notice = next(m for m in r.notifier.sent if "見直しのタイミング" in m["title"])
    assert f"@{handles[1]}" in notice["message"] and notice["priority"] == 4
