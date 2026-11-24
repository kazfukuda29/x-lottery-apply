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
    assert {i["retailer"] for i in neon} == {"ヨドバシカメラ", "ビックカメラ", "ジョーシン", "ポケモンセンターオンライン"}
    assert len({i["product_key"] for i in neon}) == 1
    # リマインド投稿は同じ抽選に統合される
    assert len(by_store["ヨドバシカメラ"]["sources"]) == 2
    # 懸賞・終了告知・他人へのリプライは入らない
    assert items["meta"]["last_run"]["dropped"] == 2
    assert items["meta"]["last_run"]["fetched"] == 9
    # 読み取れなかった投稿は「要確認」に
    assert [r["post_id"] for r in items["review"]] == ["1990000000000000009"]
    # 公開ページには投稿本文を載せない
    assert all("text" not in i and all("text" not in s for s in i["sources"]) for i in items["items"])

    # 2回目の実行では同じ投稿を再処理しない
    assert main(["--dry-run", "--data-dir", str(data)]) == 0
    again = json.loads((data / "items.json").read_text(encoding="utf-8"))
    assert again["meta"]["last_run"]["fetched"] == 0
    assert len(again["items"]) == len(items["items"])

    cands = json.loads((data / "candidates.json").read_text(encoding="utf-8"))
    assert [c["handle"] for c in cands["candidates"]] == ["game_chusen_demo"]

    site = tmp_path / "_site"
    build_site(data, site)
    assert (site / "index.html").exists() and (site / "data" / "items.json").exists()
