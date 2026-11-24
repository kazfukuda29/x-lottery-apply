"""共通の小道具：日本時間、文字の正規化、ファイル読み書き。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

# 日本は夏時間がないので固定オフセットで十分（Windowsでもtzdata不要）
JST = timezone(timedelta(hours=9), "JST")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

_DASHES = "〜～~‐‑‒–—―－ｰ"  # 範囲を表す記号（長音「ー」は含めない）
_DASH_RE = re.compile(f"[{re.escape(_DASHES)}]")


def now_jst() -> datetime:
    return datetime.now(JST)


def parse_iso(value: str | None) -> datetime | None:
    """ISO形式の文字列を日本時間のdatetimeに。空や不正なら None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(JST).isoformat(timespec="minutes") if dt else None


def normalize(text: str) -> str:
    """全角/半角・大文字/小文字・範囲記号をそろえる。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = _DASH_RE.sub("~", text)
    return text.lower()


def norm_key(text: str) -> str:
    """比較用のキー：記号と空白を除いた正規化文字列。"""
    t = normalize(text)
    return re.sub(r"[\s・,、。.!！?？\"'“”‘’()（）\[\]【】「」『』<>《》〈〉:：;；/／\\|_*#＃~-]", "", t)


def load_yaml(name: str) -> Any:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    """途中で落ちても壊れないよう、一時ファイルに書いてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def keyword_in(keyword: str, text_norm: str) -> bool:
    """語句が含まれるか。短い英数字の語（hg, ps5 など）は単語境界で判定する。"""
    kw = normalize(keyword)
    if not kw:
        return False
    if re.fullmatch(r"[a-z0-9 ]{1,4}", kw):
        return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text_norm) is not None
    return kw in text_norm


URL_RE = re.compile(r"https?://\S+")


def strip_urls(text: str) -> str:
    return URL_RE.sub(" ", text or "")
