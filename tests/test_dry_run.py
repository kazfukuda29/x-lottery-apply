"""ダミー投稿で収集〜保存まで通しで動かす。"""

import json

from collector.main import build_site, main


def test_dry_run_end_to_end(tmp_path):
    data = tmp_path / "data"
    assert main(["--dry-run", "--data-dir", str(data)]) == 0
    items = json.loads((data / "items.json").read_text(encoding="utf-8"))
    by_store = {i["retailer"]: i for i in items["items"]}

    # まとめ投稿は店ごとに分かれ、商品名は1つにそろう
    neon = [i for i in items["items"] if "ネオンストーム" in i["product"]]
    assert {i["retailer"] for i in neon} == {"ヨドバシカメラ", "ビックカメラ", "ポケモンセンターオンライン"}
    # 店舗抽選（ジョーシンの店頭抽選、HMVの店頭受取）は載せない
    assert "ジョーシン" not in by_store and "HMV&BOOKS online" not in by_store
    assert items["meta"]["last_run"]["store_skipped"] == 2
    assert by_store["ポケモンセンターオンライン"]["channel"] == "online"
    assert by_store["プレミアムバンダイ"]["channel"] == "online"
    assert by_store["ビックカメラ"]["channel"] == "unknown"
    assert len({i["product_key"] for i in neon}) == 1
    # リマインド投稿は同じ抽選に統合される
    assert len(by_store["ヨドバシカメラ"]["sources"]) == 2
    # 懸賞・終了告知・他人へのリプライは入らない
    assert items["meta"]["last_run"]["dropped"] == 2
    assert items["meta"]["last_run"]["fetched"] == 9
    # 読み取れなかった投稿は「要確認」に
    assert [r["post_id"] for r in items["review"]] == ["1990000000000000009"]
    # アカウントごとの貢献度
    assert items["meta"]["account_stats"] == {
        "chusen_matome_demo": {"items": 3, "only": 3},
        "hobby_lottery_demo": {"items": 3, "only": 3},
    }
    # 公開ページには投稿本文を載せない
    assert all("text" not in i and all("text" not in s for s in i["sources"]) for i in items["items"])

    # 2回目の実行では同じ投稿を再処理しない
    assert main(["--dry-run", "--data-dir", str(data)]) == 0
    again = json.loads((data / "items.json").read_text(encoding="utf-8"))
    assert again["meta"]["last_run"]["fetched"] == 0
    assert len(again["items"]) == len(items["items"])

    # 登録済みで受取方法が不明な「〜店」は、次の実行で店頭扱いになり外れる
    stored = json.loads((data / "items.json").read_text(encoding="utf-8"))
    stored["items"][0]["retailer"], stored["items"][0]["channel"] = "MINT吉祥寺店", "unknown"
    (data / "items.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    assert main(["--dry-run", "--data-dir", str(data)]) == 0
    third = json.loads((data / "items.json").read_text(encoding="utf-8"))
    assert "MINT吉祥寺店" not in {i["retailer"] for i in third["items"]}
    assert len(third["items"]) == len(items["items"]) - 1

    cands = json.loads((data / "candidates.json").read_text(encoding="utf-8"))
    assert [c["handle"] for c in cands["candidates"]] == ["game_chusen_demo"]

    site = tmp_path / "_site"
    build_site(data, site)
    assert (site / "index.html").exists() and (site / "data" / "items.json").exists()
