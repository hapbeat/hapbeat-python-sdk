"""Update notice — 「新しい hapbeat-python-sdk が出ています」を 1 回だけ伝える。

情報源は Hapbeat の release feed (``https://devtools.hapbeat.com/releases.json``)。
仕様は hapbeat-contracts ``specs/release-feed.md`` (DEC-053)。feed は GitHub の
タグではなく **PyPI に実際に上がっている版** を載せているので、ここに出る版は
必ず ``pip install -U`` で取得できる。

守っている作法 (spec §5):

* **``import hapbeat`` では絶対にネットワークへ出ない。** チェックするのは
  ``hapbeat`` コマンドを実行したときだけ。ライブラリの import が外部通信を
  するのは、CI・オフライン環境・サンドボックスで有害な副作用になる。
* **1 版につき 1 回だけ**通知する。一度出した版は記録し、より新しい版が出るまで
  黙る (版を固定して開発している人に毎回同じ行を見せない)。
* 取得失敗は**完全にサイレント**。「最新版を確認できませんでした」は出さない。
* CLI の実行を**待たせない**。キャッシュがあればそれを使い、無ければ裏で取りに
  行くだけで、その回は何も出さない (次回の実行で出る)。
* ``HAPBEAT_NO_UPDATE_CHECK=1`` で無効化できる。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

FEED_URL = "https://devtools.hapbeat.com/releases.json"
PRODUCT_ID = "python-sdk"
TIMEOUT_S = 3.0
CACHE_TTL_S = 24 * 60 * 60
STATE_FILENAME = "update-check.json"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / "hapbeat"
    return Path.home() / ".config" / "hapbeat"


def opted_out() -> bool:
    return os.environ.get("HAPBEAT_NO_UPDATE_CHECK", "").strip() not in ("", "0", "false")


# --------------------------------------------------------------------------
# version compare


def parse_version(v: str | None) -> tuple[int, ...]:
    """``0.2.0`` / ``v0.2.0`` / ``0.2.0dev1`` → ``(0, 2, 0)``。解釈不能なら ``()``。"""
    if not v:
        return ()
    s = str(v).strip().lstrip("vV")
    head = s.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for part in head.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return ()
        out.append(int(digits))
    return tuple(out)


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """比較不能なら False (黙る側に倒す)。"""
    a, b = parse_version(candidate), parse_version(baseline)
    if not a or not b:
        return False
    return a > b


# --------------------------------------------------------------------------
# state


def _state_path() -> Path:
    return config_dir() / STATE_FILENAME


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# feed


def _fetch_entry() -> dict[str, Any] | None:
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "hapbeat-python-sdk"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:  # noqa: S310 (fixed https URL)
        feed = json.loads(res.read().decode("utf-8"))
    if feed.get("schema_version") != 1:
        return None
    entry = feed.get("products", {}).get(PRODUCT_ID)
    return entry if isinstance(entry, dict) else None


def cached_entry() -> dict[str, Any] | None:
    """ネットワークを叩かずに、生きているキャッシュだけを返す。"""
    state = _load_state()
    entry = state.get("entry")
    if entry and (time.time() - state.get("checked_at", 0)) < CACHE_TTL_S:
        return entry
    return None


def refresh(*, use_cache: bool = True) -> dict[str, Any] | None:
    """feed を取得 (or キャッシュ) する。取得できなければ None。"""
    if use_cache:
        cached = cached_entry()
        if cached:
            return cached
    try:
        entry = _fetch_entry()
    except Exception:
        return None
    if not entry:
        return None
    state = _load_state()
    state["entry"] = entry
    state["checked_at"] = time.time()
    _save_state(state)
    return entry


def _refresh_in_background() -> None:
    threading.Thread(
        target=lambda: refresh(use_cache=False),
        name="hapbeat-update-check",
        daemon=True,
    ).start()


# --------------------------------------------------------------------------
# public API


def pending_notice(current: str, entry: dict[str, Any] | None = None,
                   *, respect_dismissed: bool = True) -> str | None:
    """通知すべきなら 1 行のメッセージを返す。無ければ None。"""
    if opted_out():
        return None
    if entry is None:
        entry = cached_entry()
    if not entry:
        return None
    latest = entry.get("latest")
    if not is_newer(latest, current):
        return None
    if respect_dismissed:
        notified = _load_state().get("notified")
        if notified and not is_newer(latest, notified):
            return None
    upgrade = entry.get("upgrade") or "pip install -U hapbeat-python-sdk"
    return f"note: hapbeat-python-sdk {latest} が公開されています ({upgrade})"


def mark_notified(latest: str) -> None:
    state = _load_state()
    state["notified"] = latest
    _save_state(state)


def notify_cli(current: str, *, stream=None) -> None:
    """CLI 実行時のフック。**待たせない**のが最優先。

    キャッシュがあればその場で判定して出す。無ければ裏で取りに行くだけで、
    その回は黙る (次に CLI を使ったときに出る)。コマンドの実行を数秒
    ブロックしてまで伝えるほどの用件ではない。
    """
    if opted_out():
        return
    try:
        entry = cached_entry()
        if entry is None:
            _refresh_in_background()
            return
        msg = pending_notice(current, entry)
        if msg:
            print(msg, file=stream if stream is not None else sys.stderr)
            mark_notified(entry["latest"])
    except Exception:
        pass  # 更新通知が原因で CLI を壊さない
