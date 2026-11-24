from datetime import datetime, timedelta
from types import SimpleNamespace

from collector.extract import ClaudeExtractor
from collector.merge import ItemStore, fields_from_ai
from collector.rules import RetailerMatcher
from collector.util import JST, load_yaml
from collector.x_client import Budget, XClient, build_watch_queries, normalize_response

NOW = datetime(2026, 11, 24, 9, 0, tzinfo=JST)
P = "ポケモンカードゲーム MEGA 拡張パック『ネオンストーム』BOX"


def post(pid, urls=()):
    return {"id": pid, "author": "a", "url": f"https://x.com/a/status/{pid}", "created_at": "2026-11-24T08:00+09:00",
            "likes": 10, "reposts": 5, "urls": list(urls)}


def fields(product, retailer, end, by="ai", **kw):
    return {"product": product, "retailer": retailer, "genre": "tcg", "apply_start": None, "apply_end": end,
            "apply_url": None, "detail_url": None, "requirements": [], "announce_date": None,
            "same_as_item_id": "", "extracted_by": by, **kw}


def test_same_store_same_lottery_is_merged():
    store = ItemStore({})
    end = NOW + timedelta(days=2)
    a, new_a = store.upsert(fields(P, "ヨドバシカメラ", end), post("1"), NOW)
    b, new_b = store.upsert(fields(P, "ヨドバシカメラ", end), post("2"), NOW)
    assert new_a and not new_b and a is b
    assert len(a["sources"]) == 2


def test_different_stores_stay_separate_but_share_product():
    store = ItemStore({})
    end = NOW + timedelta(days=2)
    store.upsert(fields(P, "ヨドバシカメラ", end), post("1"), NOW)
    store.upsert(fields(P, "ビックカメラ", end), post("1"), NOW)
    assert len(store.items) == 2
    assert len({i["product_key"] for i in store.items}) == 1


def test_second_round_with_far_deadline_is_new_item():
    store = ItemStore({})
    store.upsert(fields(P, "ヨドバシカメラ", NOW + timedelta(days=2)), post("1"), NOW)
    _, new = store.upsert(fields(P, "ヨドバシカメラ", NOW + timedelta(days=20)), post("2"), NOW)
    assert new and len(store.items) == 2


def test_short_rule_name_is_replaced_by_descriptive_ai_name():
    store = ItemStore({}, generic_words=["ポケカ"])
    end = NOW + timedelta(days=3)
    store.upsert(fields("ネオンストーム", "ポケモンセンターオンライン", end, by="rule"), post("1"), NOW)
    store.upsert(fields(P, "ヨドバシカメラ", end), post("2"), NOW)
    assert {i["product"] for i in store.items} == {P}


def test_generic_word_does_not_merge_products():
    store = ItemStore({}, generic_words=["ポケモンカード"])
    end = NOW + timedelta(days=3)
    store.upsert(fields("ポケモンカード", "ヨドバシカメラ", end, by="rule"), post("1"), NOW)
    store.upsert(fields("ワンピースカード 新弾BOX", "ヨドバシカメラ", end), post("2"), NOW)
    assert len({i["product_key"] for i in store.items}) == 2


def test_past_deadline_is_not_added():
    store = ItemStore({})
    item, _ = store.upsert(fields(P, "ヨドバシカメラ", NOW - timedelta(hours=1)), post("1"), NOW)
    assert item is None and store.items == []


def test_cleanup_keeps_until_announce_date():
    store = ItemStore({})
    store.upsert(fields(P, "ヨドバシカメラ", NOW + timedelta(hours=1), announce_date="2026-12-01"), post("1"), NOW)
    store.cleanup(NOW + timedelta(days=8), 3, 10, 3)
    assert len(store.items) == 1
    store.cleanup(NOW + timedelta(days=11), 3, 10, 3)
    assert store.items == []


def test_fields_from_ai_picks_urls_and_canonical_store():
    m = RetailerMatcher(load_yaml("retailers.yml")["retailers"])
    p = post("1", urls=["https://matome.example.com/a", "https://www.yodobashi.com/product/x"])
    ai = {"product": P, "retailer": "ヨドバシ.com", "genre": "tcg", "apply_start": "", "apply_end": "2026-11-26",
          "apply_url_index": -1, "detail_url_index": 0, "requirements": ["会員登録"], "announce_date": "",
          "same_as_item_id": ""}
    f = fields_from_ai(ai, p, m)
    assert f["retailer"] == "ヨドバシカメラ"
    assert f["apply_url"] == "https://www.yodobashi.com/product/x"  # 公式ドメインのURLを応募先として補う
    assert f["detail_url"] == "https://matome.example.com/a"
    assert f["apply_end"] == datetime(2026, 11, 26, 23, 59, tzinfo=JST)


# ------------------------------------------------------------------ X API


def test_queries_are_split_by_length():
    accounts = [{"handle": f"account_number_{i:02d}"} for i in range(40)]
    qs = build_watch_queries(accounts, ["抽選", "応募"], 200)
    assert len(qs) > 1
    assert all(len(q) <= 200 for q in qs)
    assert sum(q.count("from:") for q in qs) == 40
    assert all(q.endswith("(抽選 OR 応募) -is:retweet") for q in qs)


def test_normalize_response_uses_long_text_and_skips_media_links():
    payload = {
        "data": [{
            "id": "10", "author_id": "u1", "created_at": "2026-11-23T23:00:00.000Z", "text": "短い本文",
            "note_tweet": {"text": "長い本文 https://t.co/a", "entities": {"urls": [
                {"expanded_url": "https://shop.example.com/x"},
                {"expanded_url": "https://x.com/a/status/10/photo/1", "media_key": "3_1"}]}},
            "public_metrics": {"like_count": 3, "retweet_count": 2, "quote_count": 1},
            "attachments": {"media_keys": ["3_1"]},
        }],
        "includes": {"users": [{"id": "u1", "username": "a"}],
                     "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"}]},
    }
    posts, _ = normalize_response(payload)
    p = posts[0]
    assert p["text"].startswith("長い本文")
    assert p["urls"] == ["https://shop.example.com/x"]
    assert p["images"] == ["https://pbs.twimg.com/media/a.jpg"]
    assert p["created_at"] == "2026-11-24T08:00+09:00"
    assert p["reposts"] == 3


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.ok = status < 400
        self.text = ""

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages = list(pages)
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        return self.pages.pop(0)


def page(n, start, next_token=None):
    data = [{"id": str(start + i), "author_id": "u1", "text": "抽選"} for i in range(n)]
    meta = {"newest_id": str(start + n - 1)}
    if next_token:
        meta["next_token"] = next_token
    return FakeResp({"data": data, "includes": {"users": [{"id": "u1", "username": "a"}]}, "meta": meta})


def test_daily_budget_stops_paging():
    state = {}
    budget = Budget(state, limit=30, today="2026-11-24")
    session = FakeSession([page(20, 100, "t1"), page(10, 200, "t2"), page(10, 300)])
    client = XClient("token", budget, session=session)
    res = client.search("from:a", max_results=20, max_total=100)
    assert res.fetched == 30
    assert len(session.calls) == 2
    assert session.calls[1]["max_results"] == 10
    assert res.stopped_reason == "daily_budget"
    assert state["x_usage"]["posts"] == 30


def test_budget_resets_next_day():
    state = {"x_usage": {"date": "2026-11-23", "posts": 300, "requests": 9}}
    assert Budget(state, 300, "2026-11-24").remaining == 300


# ------------------------------------------------------------------ Claude


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeAnthropicClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(stream=self._stream)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        return FakeStream(self.responses.pop(0))


def resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def test_claude_extractor_uses_fallback_and_schema_for_opus():
    fake = FakeAnthropicClient([resp('{"results": [{"post_id": "1", "items": []}]}')])
    ex = ClaudeExtractor("claude-opus-5-5", "low", client=fake)
    out = ex.extract([{"id": "1", "text": "抽選"}], [], max_posts=10, max_images=8, max_images_per_post=2, max_calls=5, now=NOW)
    req = fake.requests[0]
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "low"
    assert out.items_by_post == {"1": []} and out.failed_post_ids == []


def test_claude_extractor_haiku_has_no_fallback():
    fake = FakeAnthropicClient([resp('{"results": []}')])
    ex = ClaudeExtractor("claude-haiku-5-5", "low", client=fake)
    out = ex.extract([{"id": "1", "text": "抽選"}], [], max_posts=10, max_images=8, max_images_per_post=2, max_calls=5, now=NOW)
    assert "fallbacks" not in fake.requests[0]
    assert out.failed_post_ids == ["1"]  # 結果に含まれなかった投稿は「要確認」へ


def test_claude_extractor_splits_batch_on_max_tokens_and_handles_refusal():
    fake = FakeAnthropicClient([
        resp("", stop="max_tokens"),
        resp('{"results": [{"post_id": "1", "items": []}]}'),
        resp("", stop="refusal"),
    ])
    ex = ClaudeExtractor("claude-opus-5-5", "low", client=fake)
    posts = [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]
    out = ex.extract(posts, [], max_posts=10, max_images=8, max_images_per_post=2, max_calls=5, now=NOW)
    assert out.calls == 3
    assert out.items_by_post == {"1": []}
    assert out.failed_post_ids == ["2"]


def test_claude_extractor_respects_daily_call_limit():
    fake = FakeAnthropicClient([])
    ex = ClaudeExtractor("claude-opus-5-5", "low", client=fake)
    out = ex.extract([{"id": "1", "text": "a"}], [], max_posts=10, max_images=8, max_images_per_post=2, max_calls=0, now=NOW)
    assert fake.requests == [] and out.failed_post_ids == ["1"]


def test_entry_platform_link_is_treated_as_apply_page():
    cfg = load_yaml("retailers.yml")
    m = RetailerMatcher(cfg["retailers"], cfg["entry_platforms"])
    urls = ["https://matome.example.com/a", "https://livepocket.jp/e/abc", "https://docs.google.com/document/x"]
    assert m.domain_of("カードラボ", urls) == "https://livepocket.jp/e/abc"
    assert m.platform_url(["https://docs.google.com/forms/d/e/xyz/viewform"]) is not None
    assert m.platform_url(["https://docs.google.com/document/d/xyz"]) is None


def test_query_excludes_ads():
    qs = build_watch_queries([{"handle": "a"}, {"handle": "b", "filter": False}], ["抽選"], 512, ["#PR"])
    assert qs == ["from:a (抽選) -#PR -is:retweet", "from:b -#PR -is:retweet"]


def test_long_posts_get_their_own_batch():
    from collector.extract import make_batches
    posts = [{"id": "1", "text": "a" * 100}, {"id": "2", "text": "b" * 7000}, {"id": "3", "text": "c" * 100}]
    assert [[p["id"] for p in b] for b in make_batches(posts, 12)] == [["1"], ["2"], ["3"]]
    assert len(make_batches([{"id": str(i), "text": "x"} for i in range(25)], 12)) == 3


def test_store_skip_instruction_is_sent_when_store_is_excluded():
    fake = FakeAnthropicClient([resp('{"results": []}'), resp('{"results": []}')])
    ClaudeExtractor("claude-haiku-5-5", "low", client=fake, skip_store=True).extract(
        [{"id": "1", "text": "a"}], [], max_posts=10, max_images=8, max_images_per_post=2, max_calls=5, now=NOW)
    assert "ネットで完結する抽選だけ" in fake.requests[0]["system"]
    ClaudeExtractor("claude-haiku-5-5", "low", client=fake, skip_store=False).extract(
        [{"id": "1", "text": "a"}], [], max_posts=10, max_images=8, max_images_per_post=2, max_calls=5, now=NOW)
    assert "ネットで完結する抽選だけ" not in fake.requests[1]["system"]
    assert '"channel"' in str(fake.requests[1]["output_config"]["format"]["schema"]).replace("'", '"')
