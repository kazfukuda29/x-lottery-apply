"""実行エントリ。

  python -m collector.main                 # 収集（必要なら毎朝のダイジェストと発見モードも）
  python -m collector.main --dry-run       # ダミー投稿で動作確認（APIを呼ばない・課金なし）
  python -m collector.main --mode build    # 公開用の _site フォルダを作る
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from .discover import update_candidates
from .evaluate import evaluate, record_fetch, summary_line, update_log
from .extract import ClaudeExtractor, FixtureExtractor, existing_summary
from .merge import ItemStore, fields_from_ai, open_items
from .notify import Notifier, due_soon, urgent_new
from .rules import RuleEngine
from .util import ROOT, iso, load_json, load_yaml, now_jst, parse_iso, save_json
from .x_client import (
    Budget,
    FixtureXClient,
    XApiError,
    XClient,
    build_watch_queries,
    first_run_start,
    is_own_thread_or_original,
    query_key,
)

FIXTURES = ROOT / "tests" / "fixtures"


def site_url() -> str | None:
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


class Runner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.dry_run = args.dry_run
        self.settings = load_yaml("settings.yml")
        self.keywords = load_yaml("keywords.yml")
        retailer_cfg = load_yaml("retailers.yml") or {}
        self.retailers = retailer_cfg.get("retailers") or []
        self.entry_platforms = retailer_cfg.get("entry_platforms") or []
        self.accounts = (load_yaml("accounts.yml") or {}).get("accounts") or []
        if self.dry_run:
            # 動作確認はダミーのアカウントで行う
            self.accounts = load_json(FIXTURES / "x_search.json", {}).get("accounts") or self.accounts
        default_dir = ROOT / (".dryrun/data" if self.dry_run else "data")
        self.data_dir = Path(args.data_dir) if args.data_dir else default_dir
        self.now = parse_iso(args.now) if args.now else now_jst()
        self.today = self.now.date().isoformat()

        self.state = load_json(self.data_dir / "state.json", {})
        self.items_data = load_json(self.data_dir / "items.json", {})
        self.cand_data = load_json(self.data_dir / "candidates.json", {})
        self.account_log = load_json(self.data_dir / "account_log.json", {})
        genre_words = [w for ws in (self.keywords.get("genres") or {}).values() for w in ws]
        self.store = ItemStore(self.items_data, generic_words=genre_words)
        self.engine = RuleEngine(self.keywords, self.retailers, self.entry_platforms)
        self.budget = Budget(self.state, self.settings["x"]["daily_post_budget"], self.today)
        self.notifier = Notifier(
            os.environ.get("NTFY_TOPIC"),
            site_url(),
            server=os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
            dry_run=self.dry_run,
        )
        self.model = os.environ.get("CLAUDE_MODEL") or self.settings["claude"]["model"]
        # 一覧に載せる受取方法（既定はネット抽選と判断できないもの。店舗抽選は載せない）
        self.channels = set(self.settings["items"].get("channels") or ["online", "unknown", "store"])
        self.stats: dict[str, int] = {
            "fetched": 0, "dropped": 0, "rule": 0, "ai_posts": 0, "ai_failed": 0, "store_skipped": 0, "new_items": 0,
        }
        self.errors: list[str] = []
        self._x = None

    # ------------------------------------------------------------ 依存の生成

    @property
    def x(self):
        if self._x is None:
            if self.dry_run:
                self._x = FixtureXClient(FIXTURES / "x_search.json", self.budget)
            else:
                self._x = XClient(os.environ.get("X_BEARER_TOKEN", ""), self.budget)
        return self._x

    def effort_for(self, model: str) -> str:
        """モデルごとの effort（effort_by_model になければ effort）。"""
        cs = self.settings["claude"]
        for prefix, effort in (cs.get("effort_by_model") or {}).items():
            if model.startswith(prefix):
                return effort
        return cs.get("effort", "low")

    def extractor(self, model: str | None = None):
        if self.dry_run:
            return FixtureExtractor(FIXTURES / "ai_response.json")
        model = model or self.model
        return ClaudeExtractor(model, self.effort_for(model), skip_store="store" not in self.channels)

    def add_item(self, fields: dict, post: dict) -> dict | None:
        """受取方法が対象外（店舗抽選など）なら登録しない。新規なら item を返す。"""
        if fields.get("channel", "unknown") not in self.channels:
            self.stats["store_skipped"] += 1
            return None
        item, is_new = self.store.upsert(fields, post, self.now)
        return item if item and is_new else None

    # ------------------------------------------------------------ 収集

    def fetch_watch_posts(self) -> list[dict]:
        xs = self.settings["x"]
        queries = build_watch_queries(
            self.accounts,
            self.keywords.get("query_words") or [],
            xs["query_max_chars"],
            self.keywords.get("query_exclude") or [],
        )
        since_ids = self.state.setdefault("since_ids", {})
        last_post = self.state.setdefault("last_post_at", {})
        posts: list[dict] = []
        for q in queries:
            key = query_key(q)
            since = since_ids.get(key)
            res = self.x.search(
                q,
                since_id=since,
                start_time=None if since else first_run_start(self.now, xs["first_run_lookback_hours"]),
                max_results=xs["max_results_per_query"],
                max_total=max(10, self.budget.remaining),
            )
            posts.extend(res.posts)
            if res.newest_id:
                since_ids[key] = res.newest_id
            for p in res.posts:
                h = p["author"].lower()
                if p.get("created_at") and (last_post.get(h) or "") < p["created_at"]:
                    last_post[h] = p["created_at"]
            if res.stopped_reason == "daily_budget":
                self.errors.append("1日の取得上限に達したため、残りのクエリは次の日に回しました")
                break
            if res.stopped_reason == "rate_limited":
                self.errors.append("X APIのレート制限に達しました")
                break
        # 使わなくなったクエリの since_id は消す
        current = {query_key(q) for q in queries}
        for k in list(since_ids):
            if k not in current:
                del since_ids[k]
        return posts

    def collect(self) -> None:
        if not self.accounts:
            self.errors.append("config/accounts.yml に監視アカウントがありません")
            return
        try:
            posts = self.fetch_watch_posts()
        except XApiError as e:
            self.errors.append(str(e))
            return
        record_fetch(self.account_log, posts, self.today)
        processed = set(self.state.get("processed_ids") or [])
        unique: dict[str, dict] = {}
        for p in posts:
            if p["id"] in processed or p["id"] in unique or not is_own_thread_or_original(p):
                continue
            unique[p["id"]] = p
        posts = sorted(unique.values(), key=lambda p: int(p["id"]))
        self.stats["fetched"] = len(posts)

        new_items: list[dict] = []
        ai_posts: list[dict] = []
        for p in posts:
            result = self.engine.classify(p, self.now)
            if result.kind == "drop":
                self.stats["dropped"] += 1
            elif result.kind == "simple":
                if result.item["genre"] == "other":
                    result.item["genre"] = self.account_genre(p["author"])
                self.stats["rule"] += 1
                item = self.add_item(result.item, p)
                if item:
                    new_items.append(item)
            else:
                p["_attach_images"] = p.get("images") if result.attach_images else []
                ai_posts.append(p)

        if ai_posts:
            new_items += self.run_ai(ai_posts)

        # 受取方法が不明なものは店名で判定し直す（判定のルールを変えたときに、登録済みの分にも効くように）
        for i in self.store.items:
            if i.get("channel", "unknown") == "unknown":
                i["channel"] = self.engine.channel_of("", i.get("retailer") or "")
        # 後から「店頭受取」とわかったものなど、対象外になったアイテムを外す
        excluded ={i["id"] for i in self.store.items if i.get("channel", "unknown") not in self.channels}
        if excluded:
            self.store.items = [i for i in self.store.items if i["id"] not in excluded]
            new_items = [i for i in new_items if i["id"] not in excluded]

        for p in posts:
            processed.add(p["id"])
            p.pop("_attach_images", None)
        self.state["processed_ids"] = sorted(processed, key=int)[-3000:]
        self.stats["new_items"] = len(new_items)

        # 新着は翌朝のダイジェストにまとめ、締切が迫っているものだけすぐ知らせる
        urgent_hours = int(self.settings["notify"].get("urgent_new_hours", 36))
        if urgent_hours < 0:
            self.notifier.new_items(new_items, self.now)
        elif urgent_hours > 0:
            self.notifier.urgent_new_items(urgent_new(new_items, self.now, urgent_hours), self.now)
        hours = int(self.settings["notify"].get("closing_soon_hours", 0))
        alerted = set(self.state.get("alerted_soon") or [])
        soon = due_soon(self.store.items, self.now, hours, alerted)
        self.notifier.closing_soon(soon, self.now, hours)
        alerted.update(i["id"] for i in soon)
        ids = {i["id"] for i in self.store.items}
        self.state["alerted_soon"] = sorted(a for a in alerted if a in ids)

    def account_genre(self, handle: str) -> str:
        """ジャンルが読み取れないとき、ジャンルが1つだけの監視アカウントならそのジャンルにする。"""
        for a in self.accounts:
            if a["handle"].lower() == (handle or "").lower() and len(a.get("genres") or []) == 1:
                return a["genres"][0]
        return "other"

    def run_ai(self, ai_posts: list[dict]) -> list[dict]:
        cs = self.settings["claude"]
        usage = self.state.setdefault("ai_usage", {})
        if usage.get("date") != self.today:
            usage.clear()
            usage.update({"date": self.today, "calls": 0, "input_tokens": 0, "output_tokens": 0})
        remaining_calls = max(0, cs["max_calls_per_day"] - usage["calls"])
        existing = existing_summary(open_items(self.store.items, self.now))
        try:
            extractor = self.extractor()
        except Exception as e:  # APIキー未設定など
            self.errors.append(f"Claude APIを使えません: {e}")
            for p in ai_posts:
                self.store.add_review(p, "自動で読み取れませんでした", self.now)
            return []
        outcome = extractor.extract(
            ai_posts,
            existing,
            max_posts=cs["max_posts_per_request"],
            max_images=cs["max_images_per_request"],
            max_images_per_post=cs["max_images_per_post"],
            max_calls=remaining_calls,
            now=self.now,
        )
        self.errors.extend(outcome.errors)
        usage["calls"] += outcome.calls
        usage["input_tokens"] += outcome.usage.get("input_tokens", 0)
        usage["output_tokens"] += outcome.usage.get("output_tokens", 0)
        self.stats["ai_posts"] = len(ai_posts)
        self.stats["ai_failed"] = len(outcome.failed_post_ids)

        compare_model = self.compare_target()
        if compare_model and not self.dry_run and outcome.items_by_post:
            self.compare(ai_posts, existing, outcome, compare_model)

        new_items: list[dict] = []
        by_id = {p["id"]: p for p in ai_posts}
        for pid, ai_items in outcome.items_by_post.items():
            post = by_id[pid]
            for ai in ai_items:
                fields = fields_from_ai(ai, post, self.engine.retailers)
                # まとめ投稿は他の店の「店頭受取」も含むため、本文ではなくAIの判断と店の種類で決める
                fields["channel"] = self.engine.channel_of("", fields["retailer"], hint=fields["channel"])
                item = self.add_item(fields, post)
                if item:
                    new_items.append(item)
        for pid in outcome.failed_post_ids:
            self.store.add_review(by_id[pid], "自動で読み取れませんでした", self.now)
        return new_items

    # ------------------------------------------------------------ モデル比較

    @property
    def compare_path(self) -> Path:
        return self.data_dir / "compare_report.json"

    def compare_days(self) -> int:
        return int(self.settings["claude"].get("compare_days", 7))

    def compare_target(self) -> str | None:
        """比較に使うモデル。比較期間（最初の compare_days 日）を過ぎたら None。
        data/compare_report.json を消すと、もう一度比較を始める。"""
        model = os.environ.get("COMPARE_MODEL") or self.settings["claude"].get("compare_model")
        if not model or model == self.model or self.compare_days() <= 0:
            return None
        report = load_json(self.compare_path, {})
        if report.get("base_model") == self.model and report.get("compare_model") == model and report.get("started"):
            end = date.fromisoformat(report["started"]) + timedelta(days=self.compare_days())
            if self.now.date() >= end:
                return None
        return model

    def compare_signature(self, items: list[dict]) -> list[tuple[str, str]]:
        """一覧に載る抽選だけを (店, 締切) にして比べる（店名の表記ゆれはそろえる）。"""
        sig = []
        for i in items:
            shop = self.engine.retailers.canonical(i.get("retailer") or "")
            if self.engine.channel_of("", shop, hint=i.get("channel") or "unknown") not in self.channels:
                continue
            sig.append((shop, (i.get("apply_end") or "")[:16]))
        return sorted(sig)

    def compare(self, posts: list[dict], existing: list[dict], base, compare_model: str) -> None:
        """同じ投稿を別モデルでも読み取り、結果の一致度を data/compare_report.json に記録する。"""
        cs = self.settings["claude"]
        other = self.extractor(compare_model).extract(
            posts, existing,
            max_posts=cs["max_posts_per_request"], max_images=cs["max_images_per_request"],
            max_images_per_post=cs["max_images_per_post"], max_calls=5, now=self.now,
        )
        path = self.compare_path
        report = load_json(path, {})
        if report.get("base_model") != self.model or report.get("compare_model") != compare_model:
            report = {"base_model": self.model, "compare_model": compare_model, "started": self.today, "runs": []}
        diffs = []
        same = 0
        for pid, a_items in base.items_by_post.items():
            b_items = other.items_by_post.get(pid)
            if b_items is None:
                continue
            a_sig = self.compare_signature(a_items)
            b_sig = self.compare_signature(b_items)
            if a_sig == b_sig:
                same += 1
            else:
                url = next((p.get("url") for p in posts if p["id"] == pid), None)
                diffs.append({"post": url, "base": a_sig, "compare": b_sig})
        report["runs"].append({"at": iso(self.now), "posts": len(base.items_by_post), "same": same, "diffs": diffs[:20]})
        report["runs"] = report["runs"][-200:]
        total = sum(r["posts"] for r in report["runs"])
        report["agreement"] = round(sum(r["same"] for r in report["runs"]) / total, 3) if total else None
        save_json(path, report)

    def compare_note(self) -> str | None:
        """毎朝のダイジェストに添える比較結果（比較中と、終わってから3日間）。"""
        report = load_json(self.compare_path, {})
        if report.get("base_model") != self.model or report.get("agreement") is None or not report.get("started"):
            return None
        posts = sum(r["posts"] for r in report.get("runs") or [])
        label = f"{report['compare_model']} の一致率 {report['agreement']:.0%}（{posts}投稿）"
        end = date.fromisoformat(report["started"]) + timedelta(days=self.compare_days())
        today = self.now.date()
        if today < end:
            return f"モデル比較中（{end.month}/{end.day}まで）：{label}"
        if today < end + timedelta(days=3):
            return f"モデル比較が終わりました：{label}。切り替えるかは README の手順で判断してください"
        return None

    # ------------------------------------------------------------ ダイジェスト・発見モード

    def stale_accounts(self) -> list[str]:
        days = int(self.settings["notify"].get("stale_days", 3))
        last_post = self.state.get("last_post_at") or {}
        tracked = self.state.setdefault("tracked_since", {})
        stale = []
        for a in self.accounts:
            h = a["handle"].lower()
            tracked.setdefault(h, iso(self.now))
            if a.get("type", "aggregator") != "aggregator":
                continue
            last = parse_iso(last_post.get(h)) or parse_iso(tracked[h])
            if last and self.now - last > timedelta(days=days):
                stale.append(a["handle"])
        return stale

    def maybe_digest(self, force: bool = False) -> None:
        hour = int(self.settings["notify"].get("digest_hour", 7))
        if not force and (self.now.hour < hour or self.state.get("last_digest_date") == self.today):
            return
        notes = [n for n in (self.compare_note(), self.weekly_account_note()) if n]
        items = open_items(self.store.items, self.now)
        # 前回のダイジェスト以降に載った新着の数（初回は過去24時間）
        since = parse_iso(self.state.get("last_digest_at")) or self.now - timedelta(hours=24)
        new_count = sum(1 for i in items if (parse_iso(i.get("first_seen")) or since) > since)
        self.notifier.digest(
            items, self.stale_accounts(), self.now, notes=notes,
            days=int(self.settings["notify"].get("digest_days", 3)), new_count=new_count,
        )
        self.state["last_digest_date"] = self.today
        self.state["last_digest_at"] = iso(self.now)

    # ------------------------------------------------------------ アカウントの評価

    def account_eval(self) -> dict:
        es = self.settings.get("evaluate") or {}
        update_log(self.account_log, self.store.items, self.store.merged, self.now)
        return evaluate(
            self.account_log, [a["handle"] for a in self.accounts], self.now,
            window_days=int(es.get("window_days", 14)), target=float(es.get("coverage_target", 0.95)),
        )

    def weekly_account_note(self) -> str | None:
        """週に1回（evaluate.weekday の曜日）、ダイジェストにアカウント評価を添える。"""
        weekday = (self.settings.get("evaluate") or {}).get("weekday", 0)
        if weekday is None or self.now.weekday() != int(weekday):
            return None
        return summary_line(self.account_eval())

    def maybe_discover(self, force: bool = False) -> None:
        ds = self.settings.get("discover") or {}
        if not force:
            if not ds.get("enabled"):
                return
            until = str(ds.get("until") or "")
            if until and self.today > until:
                return
            if self.state.get("last_discover_date") == self.today:
                return
        query = " ".join((self.keywords.get("discover_query") or "").split())
        if not query:
            return
        # 監視中のアカウントと広告は除外（取得しない＝課金されない）
        extra = [f"-{w}" for w in self.keywords.get("query_exclude") or []]
        extra += [f"-from:{a['handle']}" for a in self.accounts]
        for part in extra:
            if len(query) + len(part) + 1 > self.settings["x"]["query_max_chars"]:
                break
            query += " " + part
        try:
            res = self.x.search(
                query,
                start_time=self.now - timedelta(hours=24),
                max_results=int(ds.get("max_results", 50)),
                max_total=int(ds.get("max_results", 50)),
            )
        except XApiError as e:
            self.errors.append(f"発見モード: {e}")
            return
        watched = {a["handle"].lower() for a in self.accounts}
        update_candidates(self.cand_data, res.posts, res.users, watched, self.engine, self.now)
        self.state["last_discover_date"] = self.today

    # ------------------------------------------------------------ 保存

    def save(self) -> None:
        # 同じ抽選が別々に載っていればまとめる（店名・商品名の書き方違い。登録済みの分にも効く）
        self.store.dedupe()
        account_eval = self.account_eval()
        self.store.cleanup(
            self.now,
            int(self.settings["items"]["keep_days_after_close"]),
            int(self.settings["items"]["keep_days_undated"]),
            int(self.settings["items"]["keep_days_review"]),
        )
        out = self.store.to_dict()
        out["meta"] = {
            "generated_at": iso(self.now),
            "site_url": site_url(),
            "repo": os.environ.get("GITHUB_REPOSITORY"),
            "model": self.model,
            "last_run": self.stats,
            "errors": self.errors,
            "x_usage_today": self.state.get("x_usage"),
            "ai_usage_today": self.state.get("ai_usage"),
            "accounts": [a["handle"] for a in self.accounts],
            "account_stats": self.account_stats(),
            "account_eval": account_eval,
        }
        save_json(self.data_dir / "items.json", out)
        save_json(self.data_dir / "state.json", self.state)
        save_json(self.data_dir / "account_log.json", self.account_log)
        if self.cand_data:
            save_json(self.data_dir / "candidates.json", self.cand_data)

    def account_stats(self) -> dict[str, dict[str, int]]:
        """監視アカウントごとの貢献度：掲載中の抽選のうち、そのアカウントが伝えた件数と、そのアカウントだけが伝えた件数。"""
        stats = {a["handle"].lower(): {"items": 0, "only": 0} for a in self.accounts}
        for item in self.store.items:
            authors = {(s.get("author") or "").lower() for s in item.get("sources") or []}
            for h in authors & stats.keys():
                stats[h]["items"] += 1
                if len(authors) == 1:
                    stats[h]["only"] += 1
        return {a["handle"]: stats[a["handle"].lower()] for a in self.accounts}

    def report_errors(self) -> None:
        if not self.errors:
            return
        for e in self.errors:
            print(f"::warning::{e}")
        if self.state.get("last_error_notified") != self.today:
            self.notifier.send("収集でエラーが発生しました", "\n".join(self.errors)[:500], priority=4, tags=["warning"])
            self.state["last_error_notified"] = self.today


def build_site(data_dir: Path, out_dir: Path) -> None:
    """site/ と data/ の公開用ファイルを _site にまとめる（GitHub Pagesにアップロードする中身）。"""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(ROOT / "site", out_dir)
    (out_dir / "data").mkdir(exist_ok=True)
    for name in ("items.json", "candidates.json"):
        src = data_dir / name
        if src.exists():
            shutil.copy2(src, out_dir / "data" / name)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    # Windowsのコンソールでも日本語を表示できるようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="X抽選販売ウォッチャー")
    parser.add_argument("--mode", default="auto", choices=["auto", "collect", "digest", "discover", "build"])
    parser.add_argument("--dry-run", action="store_true", help="ダミー投稿で動作確認（API呼び出し・通知なし）")
    parser.add_argument("--data-dir", help="データの保存先（既定: data/、--dry-run時は .dryrun/data/）")
    parser.add_argument("--now", help="現在時刻を上書き（例: 2026-11-24T09:00）")
    parser.add_argument("--out", default=str(ROOT / "_site"), help="--mode build の出力先")
    args = parser.parse_args(argv)

    if args.mode == "build":
        data_dir = Path(args.data_dir) if args.data_dir else ROOT / (".dryrun/data" if args.dry_run else "data")
        build_site(data_dir, Path(args.out))
        print(f"公開用ファイルを作成しました: {args.out}")
        return 0

    if args.dry_run and not args.now:
        args.now = "2026-11-24T09:00"
    runner = Runner(args)
    if args.mode in ("auto", "collect"):
        runner.collect()
    if args.mode == "auto":
        runner.maybe_digest()
        runner.maybe_discover()
    elif args.mode == "digest":
        runner.maybe_digest(force=True)
    elif args.mode == "discover":
        runner.maybe_discover(force=True)
    runner.report_errors()
    runner.save()
    s = runner.stats
    print(
        f"取得 {s['fetched']}件 / 除外 {s['dropped']} / ルール確定 {s['rule']} / AI {s['ai_posts']}"
        f"（失敗 {s['ai_failed']}） / 新着 {s['new_items']}件 / 受付中 {len(open_items(runner.store.items, runner.now))}件"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
