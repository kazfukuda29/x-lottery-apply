"""Opus と Haiku の比較（最初の数日だけ自動で行う）。"""

import argparse
import json

from collector.extract import ExtractOutcome
from collector.main import Runner
from collector.notify import Notifier
from collector.util import JST, load_json, parse_iso

FIXTURE_AI = json.loads(open("tests/fixtures/ai_response.json", encoding="utf-8").read())
P1, P5 = "1990000000000000001", "1990000000000000005"
POSTS = [{"id": P1, "url": "https://x.com/a/status/1"}, {"id": P5, "url": "https://x.com/a/status/5"}]


def make_runner(tmp_path, monkeypatch, now="2026-11-24T09:00", claude_model=None, compare_model=None):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("COMPARE_MODEL", raising=False)
    if claude_model:
        monkeypatch.setenv("CLAUDE_MODEL", claude_model)
    if compare_model:
        monkeypatch.setenv("COMPARE_MODEL", compare_model)
    args = argparse.Namespace(dry_run=True, data_dir=str(tmp_path / "data"), now=now, mode="auto",
                              out=str(tmp_path / "_site"))
    return Runner(args)


def base_outcome():
    """比較の基準（Opus役）。P1 は店名の表記だけ違う、P5 はソニーストアを取りこぼした結果。"""
    p1 = [dict(i) for i in FIXTURE_AI[P1] if i["retailer"] != "ジョーシン"]  # 店頭抽選は一覧に載らないので比較しない
    p1[0]["retailer"] = "ヨドバシカメラ"
    p5 = [i for i in FIXTURE_AI[P5] if i["retailer"] != "ソニーストア"]
    return ExtractOutcome(items_by_post={P1: p1, P5: p5})


def test_haiku_uses_its_own_effort(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    assert r.effort_for("claude-opus-5-5") == "low"
    assert r.effort_for("claude-haiku-5-5") == "medium"


def test_compare_records_agreement_and_stops_after_period(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    assert r.compare_target() == "claude-haiku-5-5"
    r.compare(POSTS, [], base_outcome(), "claude-haiku-5-5")

    report = load_json(tmp_path / "data" / "compare_report.json", {})
    assert report["base_model"] == "claude-opus-5-5" and report["started"] == "2026-11-24"
    assert report["agreement"] == 0.5
    assert [d["post"] for d in report["runs"][0]["diffs"]] == ["https://x.com/a/status/5"]
    assert "モデル比較中（12/1まで）" in r.compare_note() and "50%" in r.compare_note()

    # 7日たったら比較をやめ、3日間だけ結果を知らせる
    later = make_runner(tmp_path, monkeypatch, now="2026-12-01T09:00")
    assert later.compare_target() is None
    assert "モデル比較が終わりました" in later.compare_note()
    assert make_runner(tmp_path, monkeypatch, now="2026-12-04T09:00").compare_note() is None


def test_compare_restarts_for_new_pair_and_skips_same_model(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    r.compare(POSTS, [], base_outcome(), "claude-haiku-5-5")
    # 別のモデルと比べ直すときは、その日から改めて7日間
    sonnet = make_runner(tmp_path, monkeypatch, now="2026-12-05T09:00", compare_model="claude-sonnet-5-5")
    assert sonnet.compare_target() == "claude-sonnet-5-5"
    # Haiku に切り替えた後は比較しない
    switched = make_runner(tmp_path, monkeypatch, now="2026-12-05T09:00", claude_model="claude-haiku-5-5")
    assert switched.compare_target() is None and switched.compare_note() is None


def test_digest_includes_notes():
    n = Notifier(None, None, dry_run=True)
    n.digest([], [], parse_iso("2026-11-24T07:00").astimezone(JST), notes=["モデル比較中：テスト"])
    assert n.sent[0]["message"].endswith("モデル比較中：テスト")
