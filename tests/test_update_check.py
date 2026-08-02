"""Update notice の挙動テスト (hapbeat-contracts specs/release-feed.md, DEC-053).

とくに大事なのは「黙るべきときに黙る」こと:
  - ``import hapbeat`` ではネットワークに出ない
  - キャッシュが無い回は CLI を待たせず、何も出さない
  - 一度知らせた版は二度と知らせない
  - 取得できないときは無言
"""
import json
import time

import pytest

from hapbeat import update_check as uc

ENTRY = {
    "name": "hapbeat-python-sdk",
    "channel": "pypi",
    "latest": "0.3.0",
    "severity": "info",
    "upgrade": "pip install -U hapbeat-python-sdk",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "config_dir", lambda: tmp_path)
    monkeypatch.delenv("HAPBEAT_NO_UPDATE_CHECK", raising=False)
    yield


def _seed_cache(entry=None, age_s: float = 0.0):
    """feed キャッシュだけ差し替える。

    `notified` / `notified_at` は保持すること — ここで state を丸ごと上書きすると
    「版が変わったから出た」のか「通知記録が消えたから出た」のか区別できなくなり、
    間引きのテストが骨抜きになる。
    """
    p = uc.config_dir() / uc.STATE_FILENAME
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state["entry"] = entry if entry is not None else dict(ENTRY)
    state["checked_at"] = time.time() - age_s
    p.write_text(json.dumps(state), encoding="utf-8")


def test_import_does_not_touch_the_network(monkeypatch):
    """ライブラリ利用 (import) では絶対に外へ出ない。"""
    def _boom():
        raise AssertionError("import path must never hit the network")
    monkeypatch.setattr(uc, "_fetch_entry", _boom)

    import importlib
    import hapbeat
    importlib.reload(hapbeat)  # 再 import しても fetch されないこと


@pytest.mark.parametrize(("raw", "expected"), [
    ("0.2.0", (0, 2, 0)), ("v0.2.0", (0, 2, 0)), ("0.2.0dev1", (0, 2, 0)),
    ("", ()), (None, ()), ("nightly", ()),
])
def test_parse_version(raw, expected):
    assert uc.parse_version(raw) == expected


def test_is_newer():
    assert uc.is_newer("0.3.0", "0.2.0")
    assert uc.is_newer("0.2.10", "0.2.9")
    assert not uc.is_newer("0.2.0", "0.2.0")
    assert not uc.is_newer("0.1.0", "0.2.0")
    assert not uc.is_newer("nightly", "0.2.0")


def test_notifies_then_stays_quiet_for_a_day(capsys):
    """短命 CLI は連続実行され得るので、同じ版は 24h に 1 回まで (§5.1 B)。"""
    _seed_cache()
    uc.notify_cli("0.2.0")
    assert "0.3.0" in capsys.readouterr().err

    uc.notify_cli("0.2.0")
    assert capsys.readouterr().err == ""


def test_notifies_again_after_the_interval(capsys):
    """24h 経てば同じ版でも再度知らせる (見逃したまま放置させない)。"""
    _seed_cache()
    uc.notify_cli("0.2.0")
    capsys.readouterr()

    state = json.loads((uc.config_dir() / uc.STATE_FILENAME).read_text(encoding="utf-8"))
    state["notified_at"] = time.time() - uc.NOTIFY_INTERVAL_S - 60
    (uc.config_dir() / uc.STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    uc.notify_cli("0.2.0")
    assert "0.3.0" in capsys.readouterr().err


def test_newer_release_does_not_wait_for_the_interval(capsys):
    _seed_cache()
    uc.notify_cli("0.2.0")
    capsys.readouterr()

    _seed_cache({**ENTRY, "latest": "0.4.0"})
    uc.notify_cli("0.2.0")
    assert "0.4.0" in capsys.readouterr().err


def test_notice_is_english(capsys):
    """CLI 出力は英語で統一 (この CLI の他の出力に合わせる)。"""
    _seed_cache()
    uc.notify_cli("0.2.0")
    err = capsys.readouterr().err
    assert err and not any("぀" <= ch <= "鿿" for ch in err)


def test_no_cache_means_no_output_this_run(monkeypatch, capsys):
    """初回は裏で取りに行くだけ — コマンドを待たせないし、何も出さない。"""
    started = {"bg": False}
    monkeypatch.setattr(uc, "_refresh_in_background", lambda: started.__setitem__("bg", True))

    uc.notify_cli("0.2.0")
    assert capsys.readouterr().err == ""
    assert started["bg"] is True


def test_stale_cache_is_ignored(monkeypatch, capsys):
    _seed_cache(age_s=uc.CACHE_TTL_S + 60)
    monkeypatch.setattr(uc, "_refresh_in_background", lambda: None)
    uc.notify_cli("0.2.0")
    assert capsys.readouterr().err == ""


def test_quiet_when_up_to_date(capsys):
    _seed_cache()
    uc.notify_cli("0.3.0")
    assert capsys.readouterr().err == ""
    uc.notify_cli("0.9.0")   # feed より先行 (dev checkout)
    assert capsys.readouterr().err == ""


def test_opt_out(monkeypatch, capsys):
    _seed_cache()
    monkeypatch.setenv("HAPBEAT_NO_UPDATE_CHECK", "1")
    uc.notify_cli("0.2.0")
    assert capsys.readouterr().err == ""


def test_broken_cache_is_survivable(capsys):
    (uc.config_dir() / uc.STATE_FILENAME).write_text("{ not json", encoding="utf-8")
    uc.notify_cli("0.2.0")   # must not raise
    assert capsys.readouterr().err == ""


def test_notify_never_raises(monkeypatch, capsys):
    """更新通知が原因で CLI 本体を落とさない。"""
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(uc, "cached_entry", _boom)
    uc.notify_cli("0.2.0")   # must not raise
    assert capsys.readouterr().err == ""
