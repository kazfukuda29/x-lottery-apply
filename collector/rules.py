"""段階①②：ルールによる判定と抽出（無料）。

① 抽選と関係ない投稿・懸賞・終了告知を捨てる
② 「店が1つ・締切が1つ・商品名が取れる」単純な投稿はルールで確定する
それ以外（複数店舗入りのまとめ投稿、締切が読めない投稿など）は AI に回す。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .util import JST, keyword_in, normalize, parse_iso, strip_urls

# ---------------------------------------------------------------- 日付の読み取り

_WEEK = r"(?:\s*\((?:[月火水木金土日祝・]|月曜|火曜|水曜|木曜|金曜|土曜|日曜){1,3}\))?"
_TIME = r"(?:\s*(?P<H>[0-2]?\d)\s*(?::|時)\s*(?P<M>[0-5]\d)?\s*分?)?"

DATE_RE = re.compile(
    r"(?<![\d/.])"
    r"(?:(?P<y>20\d{2})\s*[年/.]\s*)?"
    r"(?P<m>1[0-2]|0?[1-9])\s*(?:月|/)\s*(?P<d>3[01]|[12]\d|0?[1-9])(?![\d/])\s*日?"
    + _WEEK
    + _TIME
)
RELATIVE_RE = re.compile(
    r"(?P<rel>本日|今日|明日|あす|あした)(?:\s*中)?"
    r"(?:\s*(?P<H>[0-2]?\d)\s*(?::|時)\s*(?P<M>[0-5]\d)?\s*分?)?"
)
# 範囲の後ろ側（月が省略された日付、または時刻だけ）
DAY_ONLY_RE = re.compile(r"\s*(?P<d>3[01]|[12]\d|0?[1-9])\s*日" + _WEEK + _TIME)
TIME_ONLY_RE = re.compile(r"\s*(?P<H>[0-2]?\d)\s*(?::|時)\s*(?P<M>[0-5]\d)?\s*分?")

RANGE_SEP_RE = re.compile(r"\s*(?:~|-|から)\s*")
AFTER_DEADLINE_RE = re.compile(r"^\s*(?:まで|迄|締切|締め切り|〆切|〆|必着|受付終了|終了)")
BEFORE_DEADLINE_RE = re.compile(r"(?:締切|締め切り|〆切|〆|期限|終了日|~)\s*(?:は|:)?\s*$")


@dataclass
class DateToken:
    start: int
    end: int
    dt: datetime
    has_time: bool


@dataclass
class Window:
    start: datetime | None
    end: datetime | None


def infer_year(month: int, day: int, base: datetime) -> int:
    """年が書かれていない日付の年を、投稿日時から推定する。"""
    year = base.year
    try:
        candidate = datetime(year, month, day, tzinfo=JST)
    except ValueError:
        return year
    if candidate < base - timedelta(days=120):
        return year + 1
    if candidate > base + timedelta(days=240):
        return year - 1
    return year


def _make_dt(y: int | None, m: int, d: int, h: str | None, mi: str | None, base: datetime) -> tuple[datetime, bool] | None:
    year = y or infer_year(m, d, base)
    hour = int(h) if h is not None else None
    minute = int(mi) if mi else 0
    if hour is not None and hour > 24:
        return None
    try:
        day = datetime(year, m, d, tzinfo=JST)
    except ValueError:
        return None
    if hour is None:
        return day, False
    return day + timedelta(hours=hour, minutes=minute), True


def find_date_tokens(text_norm: str, base: datetime) -> list[DateToken]:
    tokens: list[DateToken] = []
    for m in DATE_RE.finditer(text_norm):
        made = _make_dt(
            int(m["y"]) if m["y"] else None, int(m["m"]), int(m["d"]), m["H"], m["M"], base
        )
        if made:
            tokens.append(DateToken(m.start(), m.end(), made[0], made[1]))
    for m in RELATIVE_RE.finditer(text_norm):
        offset = 0 if m["rel"] in ("本日", "今日") else 1
        day = base.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
        if m["H"] is not None and int(m["H"]) <= 24:
            dt = day + timedelta(hours=int(m["H"]), minutes=int(m["M"] or 0))
            tokens.append(DateToken(m.start(), m.end(), dt, True))
        else:
            tokens.append(DateToken(m.start(), m.end(), day, False))
    tokens.sort(key=lambda t: t.start)
    return tokens


def _end_of_day(t: DateToken) -> datetime:
    return t.dt if t.has_time else t.dt.replace(hour=23, minute=59)


# 「当選：12/3」「購入：12/7~12/13」のように、応募期間ではない日付に付くラベル
OTHER_DATE_LABEL_RE = re.compile(
    r"(?:当選|当落|発表|購入|お渡し|引き?換え?|入金|支払い?|発売|注文|受取|受け取り|抽選日|抽選:|販売日|販売期間|入店日|対象日)"
    r"[^\d\n]{0,6}$"
)
ANNOUNCE_LABEL_RE = re.compile(r"(?:当選|当落|発表)[^\d\n]{0,6}$")


@dataclass
class DateAnalysis:
    windows: list[Window]
    other_tokens: int  # 応募期間とも他のラベルとも判断できなかった日付の数
    announce: datetime | None


def parse_windows(text_norm: str, base: datetime) -> list[Window]:
    """本文から受付期間（開始・締切）を読み取る。"""
    return analyze_dates(text_norm, base).windows


def analyze_dates(text_norm: str, base: datetime) -> DateAnalysis:
    tokens = find_date_tokens(text_norm, base)
    by_start = {t.start: t for t in tokens}
    used: set[int] = set()
    windows: list[Window] = []
    announce: datetime | None = None
    other = 0
    for t in tokens:
        if t.start in used:
            continue
        label_area = text_norm[max(0, t.start - 14) : t.start].split("\n")[-1]
        if OTHER_DATE_LABEL_RE.search(label_area):
            # 当選発表日・購入期間などは締切として扱わない（範囲の後ろ側もまとめて除外）
            used.add(t.start)
            sep = RANGE_SEP_RE.match(text_norm, t.end)
            if sep and sep.end() in by_start:
                used.add(sep.end())
            if announce is None and ANNOUNCE_LABEL_RE.search(label_area):
                announce = t.dt
            continue
        sep = RANGE_SEP_RE.match(text_norm, t.end)
        if sep:
            pos = sep.end()
            nxt = by_start.get(pos)
            if nxt is None:
                # 「11/25 ~ 11/27」の間に空白があるとき
                nxt = next((x for x in tokens if x.start > t.end and x.start <= pos + 1), None)
            if nxt is not None:
                used.update({t.start, nxt.start})
                windows.append(Window(t.dt, _end_of_day(nxt)))
                continue
            dm = DAY_ONLY_RE.match(text_norm, pos)
            if dm:
                made = _make_dt(t.dt.year, t.dt.month, int(dm["d"]), dm["H"], dm["M"], base)
                if made:
                    end = made[0] if made[1] else made[0].replace(hour=23, minute=59)
                    if end < t.dt:  # 月またぎ（11/28~3日）
                        end = _add_month(end)
                    used.add(t.start)
                    windows.append(Window(t.dt, end))
                    continue
            tm = TIME_ONLY_RE.match(text_norm, pos)
            if tm and int(tm["H"]) <= 24:
                end = t.dt.replace(hour=0, minute=0) + timedelta(hours=int(tm["H"]), minutes=int(tm["M"] or 0))
                used.add(t.start)
                windows.append(Window(t.dt, end))
                continue
        after = text_norm[t.end : t.end + 8]
        before = text_norm[max(0, t.start - 10) : t.start]
        if AFTER_DEADLINE_RE.match(after) or BEFORE_DEADLINE_RE.search(before):
            used.add(t.start)
            windows.append(Window(None, _end_of_day(t)))
        elif re.match(r"^\s*(?:より|から|~)?\s*(?:受付開始|開始|スタート)", after):
            used.add(t.start)
            windows.append(Window(t.dt, None))
        else:
            other += 1
    # 同じ締切は1つにまとめる
    seen: set = set()
    unique = []
    for w in windows:
        key = (w.start, w.end)
        if key not in seen:
            seen.add(key)
            unique.append(w)
    return DateAnalysis(unique, other, announce)


def _add_month(dt: datetime) -> datetime:
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    try:
        return dt.replace(year=y, month=m)
    except ValueError:
        return dt


# ---------------------------------------------------------------- 店・商品・条件


class RetailerMatcher:
    def __init__(self, retailers: list[dict], entry_platforms: list[str] | None = None):
        self.entries: list[tuple[str, str]] = []  # (正規化した別名, 正式名)
        self.domains: list[tuple[str, str]] = []
        # 店ではないが応募の受付窓口になるサービス（例: livepocket.jp, docs.google.com/forms）
        self.platforms = [p.lower() for p in entry_platforms or []]
        for r in retailers:
            name = r["name"]
            for alias in [name, *(r.get("aliases") or [])]:
                self.entries.append((normalize(alias), name))
            for d in r.get("domains") or []:
                self.domains.append((d.lower(), name))
        # 長い別名から先に照合（「ヨドバシ.com」を「ヨドバシ」より優先）
        self.entries.sort(key=lambda e: -len(e[0]))

    def canonical(self, name: str) -> str:
        """AIが返した店名なども辞書の正式名にそろえる。"""
        n = normalize(name).strip()
        if not n:
            return ""
        for alias, canon in self.entries:
            if n == alias:
                return canon
        for alias, canon in self.entries:
            if len(alias) >= 3 and (alias in n or n in alias):
                return canon
        return unicodedata.normalize("NFKC", name).strip()

    def find(self, text_norm: str, urls: list[str]) -> list[str]:
        found: list[str] = []
        masked = text_norm
        for alias, canon in self.entries:
            if not alias:
                continue
            for m in re.finditer(re.escape(alias), masked):
                prev = masked[m.start() - 1] if m.start() > 0 else ""
                nxt = masked[m.end()] if m.end() < len(masked) else ""
                # 「ライオン」の中の「イオン」、「ビックリ」の中の「ビック」のような誤一致を避ける
                if _is_katakana(alias[0]) and _is_katakana(prev):
                    continue
                if _is_katakana(alias[-1]) and _is_katakana(nxt):
                    continue
                if re.fullmatch(r"[a-z0-9.]+", alias) and re.match(r"[a-z0-9]", prev or " "):
                    continue
                if canon not in found:
                    found.append(canon)
                masked = masked[: m.start()] + " " * len(alias) + masked[m.end() :]
                break
        for url in urls:
            host = urlparse(url).netloc.lower()
            for domain, canon in self.domains:
                if (host == domain or host.endswith("." + domain)) and canon not in found:
                    found.append(canon)
        return found

    def domain_of(self, retailer: str, urls: list[str]) -> str | None:
        """その店の公式ドメインのURL、なければ応募窓口サービスのURLを探す。"""
        doms = [d for d, c in self.domains if c == retailer]
        for url in urls:
            host = urlparse(url).netloc.lower()
            if any(host == d or host.endswith("." + d) for d in doms):
                return url
        return self.platform_url(urls)

    def platform_url(self, urls: list[str]) -> str | None:
        for url in urls:
            u = urlparse(url)
            target = (u.netloc.lower().removeprefix("www.") + u.path.lower())
            if any(target.startswith(p) for p in self.platforms):
                return url
        return None


def _is_katakana(ch: str) -> bool:
    return bool(ch) and ("゠" <= ch <= "ヿ")


_SYMBOLS_RE = re.compile(r"[■□◆◇●○★☆▼▽▲△▶►◀◁→←↑↓※✅✨🔥⚠️🎉📢💡☀-➿\U0001f000-\U0001faff]+")


def extract_product(text: str, lottery_words: list[str], genre_words: list[str]) -> str | None:
    """商品名をルールで取り出す。自信がなければ None（AIに回す）。"""
    t = unicodedata.normalize("NFKC", strip_urls(text))
    genre_norms = {normalize(g) for g in genre_words}
    for pattern in (r"『([^』]{2,60})』", r"「([^」]{2,60})」", r"【([^】]{2,60})】"):
        for m in re.finditer(pattern, t):
            inner = m.group(1).strip()
            low = normalize(inner)
            if any(normalize(w) in low for w in lottery_words) or "情報" in inner or "お知らせ" in inner:
                continue
            if low in genre_norms:  # 「ポケカ」だけでは商品名にならない
                continue
            if pattern.startswith("【") and (
                len(inner) < 6 or not any(keyword_in(g, low) for g in genre_words)
            ):
                continue
            return inner
    return None


def detect_requirements(text_norm: str, mapping: dict[str, str]) -> list[str]:
    masked = text_norm
    labels: list[str] = []
    for word in sorted(mapping, key=len, reverse=True):
        w = normalize(word)
        if w and w in masked:
            label = mapping[word]
            if label not in labels:
                labels.append(label)
            masked = masked.replace(w, " " * len(w))
    return labels


def detect_genre(text_norm: str, genres: dict[str, list[str]]) -> str:
    for genre, words in genres.items():
        if any(keyword_in(w, text_norm) for w in words):
            return genre
    return "other"


# ---------------------------------------------------------------- 判定


@dataclass
class RuleResult:
    kind: str  # "drop" | "simple" | "ai"
    reason: str = ""
    item: dict | None = None
    attach_images: bool = False
    windows: list[Window] = field(default_factory=list)


class RuleEngine:
    def __init__(self, keywords: dict, retailers: list[dict], entry_platforms: list[str] | None = None):
        self.kw = keywords
        self.retailers = RetailerMatcher(retailers, entry_platforms)
        self.genre_words = [w for ws in (keywords.get("genres") or {}).values() for w in ws]

    def _any(self, key: str, text_norm: str) -> bool:
        return any(keyword_in(w, text_norm) for w in self.kw.get(key) or [])

    def classify(self, post: dict, now: datetime) -> RuleResult:
        text = post.get("text", "")
        text_norm = normalize(strip_urls(text))
        base = parse_iso(post.get("created_at")) or now
        has_images = bool(post.get("images"))

        if not self._any("lottery_words", text_norm) and not has_images:
            return RuleResult("drop", "抽選に関係する語がない")

        strong = self._any("strong_lottery_words", text_norm)
        giveaway = self._any("giveaway_words", text_norm)
        closed = self._any("closed_words", text_norm)
        dates = analyze_dates(text_norm, base)
        windows = dates.windows
        future = [w for w in windows if w.end is None or w.end >= now]

        retailers = self.retailers.find(text_norm, post.get("urls") or [])
        if giveaway and not strong:
            return RuleResult("drop", "プレゼント懸賞", windows=windows)
        # 終了・結果の告知。複数店舗のまとめ投稿で一部だけ終了、という場合は残す
        if closed and not any(w.end and w.end >= now for w in windows) and len(retailers) <= 1:
            return RuleResult("drop", "終了・結果の告知", windows=windows)
        if windows and not future:
            return RuleResult("drop", "締切がすべて過ぎている", windows=windows)

        product = extract_product(text, self.kw.get("lottery_words") or [], self.genre_words)

        simple = (
            len(retailers) == 1
            and len(windows) == 1
            and windows[0].end is not None
            and product is not None
            and dates.other_tokens == 0
            and not closed
        )
        if simple:
            w = windows[0]
            retailer = retailers[0]
            urls = post.get("urls") or []
            apply_url = self.retailers.domain_of(retailer, urls)
            detail_url = None if apply_url else (urls[0] if urls else None)
            item = {
                "product": product,
                "retailer": retailer,
                "genre": detect_genre(text_norm, self.kw.get("genres") or {}),
                "apply_start": w.start,
                "apply_end": w.end,
                "apply_url": apply_url,
                "detail_url": detail_url,
                "requirements": detect_requirements(text_norm, self.kw.get("requirements") or {}),
                "announce_date": dates.announce.date().isoformat() if dates.announce else None,
                "same_as_item_id": "",
                "extracted_by": "rule",
            }
            return RuleResult("simple", "単純な告知", item=item, windows=windows)

        attach = has_images and (not windows or len(text_norm) < 80)
        reason = "複数の店/締切" if (len(retailers) > 1 or len(windows) > 1) else "ルールで確定できない"
        return RuleResult("ai", reason, attach_images=attach, windows=windows)
