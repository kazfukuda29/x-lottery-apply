from datetime import datetime

import pytest

from collector.rules import RetailerMatcher, RuleEngine, extract_product, parse_windows
from collector.util import JST, load_yaml, normalize

BASE = datetime(2026, 11, 24, 9, 0, tzinfo=JST)


def dt(m, d, h=23, mi=59, y=2026):
    return datetime(y, m, d, h, mi, tzinfo=JST)


def windows(text, base=BASE):
    return [(w.start, w.end) for w in parse_windows(normalize(text), base)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("11/25(火)23:59まで", [(None, dt(11, 25))]),
        ("11月25日(火) 23時59分まで", [(None, dt(11, 25))]),
        ("～11/26(水)10:00", [(None, dt(11, 26, 10, 0))]),
        ("締切：11/27", [(None, dt(11, 27))]),
        ("締切は11/27(木)です", [(None, dt(11, 27))]),
        ("受付期間 11/24(月)12:00~12/01(月)23:59", [(dt(11, 24, 12, 0), dt(12, 1))]),
        ("11/24 10:00〜23:59", [(dt(11, 24, 10, 0), dt(11, 24, 23, 59))]),
        ("11/28(金)10:00〜3日(水)9:59", [(dt(11, 28, 10, 0), dt(12, 3, 9, 59))]),
        ("本日23:59まで", [(None, dt(11, 24))]),
        ("明日10時まで", [(None, dt(11, 25, 10, 0))]),
        ("11/30 10:00より受付開始", [(dt(11, 30, 10, 0), None)]),
        ("2026年12月1日まで", [(None, dt(12, 1))]),
    ],
)
def test_parse_windows(text, expected):
    assert windows(text) == expected


def test_year_rolls_over_in_december():
    base = datetime(2026, 12, 28, 9, 0, tzinfo=JST)
    assert windows("1/5(月)まで", base) == [(None, dt(1, 5, y=2027))]


def test_scale_notation_is_not_a_date():
    # ガンプラの「1/144」やフィギュアの「1/7スケール」は日付ではない
    assert windows("HG 1/144 ガンダム 抽選") == []
    assert windows("1/7スケールフィギュア 抽選販売") == []


@pytest.fixture(scope="module")
def engine():
    cfg = load_yaml("retailers.yml")
    return RuleEngine(load_yaml("keywords.yml"), cfg["retailers"], cfg["entry_platforms"])


def post(text, urls=(), images=(), created="2026-11-24T08:00+09:00"):
    return {"id": "1", "author": "a", "author_id": "u", "text": text, "urls": list(urls), "images": list(images), "created_at": created}


def test_giveaway_is_dropped(engine):
    r = engine.classify(post("フォロー&リポストで抽選で3名様にプレゼント！締切11/30"), BASE)
    assert r.kind == "drop"


def test_closed_notice_is_dropped(engine):
    r = engine.classify(post("ビックカメラの抽選受付は終了しました"), BASE)
    assert r.kind == "drop"


def test_past_deadline_is_dropped(engine):
    r = engine.classify(post("【抽選販売】ヨドバシ『テスト』11/20まで"), BASE)
    assert r.kind == "drop"


def test_unrelated_post_is_dropped(engine):
    assert engine.classify(post("本日のおすすめ新商品です"), BASE).kind == "drop"


def test_simple_post_is_resolved_by_rules(engine):
    r = engine.classify(
        post("【抽選販売】ポケモンセンターオンラインにて『ネオンストーム』BOXの抽選受付中！ 11/27(木)23:59まで #ポケカ",
             urls=["https://www.pokemoncenter-online.com/lottery/x"]),
        BASE,
    )
    assert r.kind == "simple"
    assert r.item["retailer"] == "ポケモンセンターオンライン"
    assert r.item["product"] == "ネオンストーム"
    assert r.item["apply_end"] == dt(11, 27)
    assert r.item["apply_url"] == "https://www.pokemoncenter-online.com/lottery/x"
    assert r.item["genre"] == "tcg"


def test_multi_store_post_goes_to_ai(engine):
    text = "【ポケカ抽選情報】『ネオンストーム』\n・ヨドバシ 〜11/26 23:59\n・ビックカメラ 〜11/25 10:00\n・ジョーシン 本日23:59まで"
    r = engine.classify(post(text), BASE)
    assert r.kind == "ai"


def test_image_only_post_attaches_images(engine):
    r = engine.classify(post("本日の抽選情報まとめ 画像をご確認ください", images=["https://pbs.twimg.com/media/a.jpg"]), BASE)
    assert r.kind == "ai" and r.attach_images


def test_retailer_matcher_avoids_partial_katakana():
    m = RetailerMatcher(load_yaml("retailers.yml")["retailers"])
    assert m.find(normalize("ライオンのぬいぐるみ"), []) == []
    assert m.find(normalize("ビックリするほど人気"), []) == []
    assert m.find(normalize("イオンで抽選"), []) == ["イオン"]
    assert m.find(normalize("ヨドバシ.comとヨドバシで"), []) == ["ヨドバシカメラ"]
    assert m.find("", ["https://www.biccamera.com/bc/x"]) == ["ビックカメラ"]
    assert m.canonical("ヨドバシ.com") == "ヨドバシカメラ"


def test_extract_product_skips_generic_brackets():
    words = load_yaml("keywords.yml")
    genre_words = [w for ws in words["genres"].values() for w in ws]
    assert extract_product("【ポケカ】抽選販売のお知らせ", words["lottery_words"], genre_words) is None
    assert extract_product("「抽選販売」開始『ネオンストーム』", words["lottery_words"], genre_words) == "ネオンストーム"


def test_announce_and_purchase_dates_are_not_deadlines(engine):
    # 実際の投稿の書き方（応募期間・当選発表・購入期間が並ぶ）
    text = ("【ポケカ抽選情報】 ホビーステーションにてポケモンカード「拡張パック ムニキスゼロ（再販）」の抽選応募受付が開始⚡ "
            "📅応募期間 11月26日(木) 23時59分まで 🎯当選発表 12月4日(金)〜5日予定 🛒購入期間 12月7日(月)〜12月13日(日) "
            "🔗応募ページ https://t.co/x")
    r = engine.classify(post(text, urls=["https://livepocket.jp/e/x1x8-"]), BASE)
    assert r.kind == "simple"
    assert r.item["retailer"] == "ホビーステーション"
    assert r.item["product"] == "拡張パック ムニキスゼロ(再販)"
    assert r.item["apply_end"] == dt(11, 26)
    assert r.item["announce_date"] == "2026-12-04"
    assert r.item["apply_url"] == "https://livepocket.jp/e/x1x8-"


def test_colon_style_post(engine):
    text = ("【抽選受付中‼️】ポケカ🔥 ヤマダデンキで 「MEGA 30th CELEBRATION カードセット（9種セット）」の抽選販売受付中！ "
            "応募：11/25(水)23:59〆 当選：12/9(水)12:00頃 購入：12/12(土)～12/18(金)閉店まで ※店頭受取")
    r = engine.classify(post(text), BASE)
    assert r.kind == "simple"
    assert r.item["retailer"] == "ヤマダデンキ"
    assert r.item["apply_end"] == dt(11, 25)
    assert r.item["announce_date"] == "2026-12-09"
    assert "店頭受取" in r.item["requirements"]


def test_channel_detection(engine):
    n = normalize
    assert engine.channel_of(n("応募：11/25〆 ※店頭受取")) == "store"
    assert engine.channel_of(n("ジョーシンの店頭レジで申込み")) == "store"
    assert engine.channel_of(n("入店抽選の受付を開始")) == "store"
    assert engine.channel_of(n("※郵送受取／送料別途")) == "online"
    assert engine.channel_of(n("（通販・全国）")) == "online"
    # 文面に手がかりがなければ、ネット専業の店はネット、それ以外は不明
    assert engine.channel_of(n("抽選受付中"), "プレミアムバンダイ") == "online"
    assert engine.channel_of(n("抽選受付中"), "ヨドバシカメラ") == "unknown"
    # まとめ投稿ではAIの判断を使う（本文全体の手がかりは使わない）
    assert engine.channel_of("", "ヨドバシカメラ", hint="store") == "store"
    assert engine.channel_of("", "Amazon", hint="unknown") == "online"


def test_store_pickup_post_is_marked_store(engine):
    text = ("【抽選受付中‼️】ポケカ🔥 ヤマダデンキで 「MEGA 30th CELEBRATION カードセット（9種セット）」の抽選販売受付中！ "
            "応募：11/25(水)23:59〆 ※店頭受取")
    assert engine.classify(post(text), BASE).item["channel"] == "store"
