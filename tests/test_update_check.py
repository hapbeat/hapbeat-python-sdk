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
    (uc.config_dir() / uc.STATE_FILENAME).write_text(json.dumps({
        "entry": entry if entry is not None else dict(ENTRY),
        "checked_at": time.time() - age_s,
    }), encoding="utf-8")


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


def test_notifies_once_then_stays_quiet(capsys):
    _seed_cache()
    uc.notify_cli("0.2.0")
    assert "0.3.0" in capsys.readouterr().err

    uc.notify_cli("0.2.0")
    assert capsys.readouterr().err == ""


def test_newer_release_breaks_the_silence(capsys):
    _seed_cache()
    uc.notify_cli("0.2.0")
    capsys.readouterr()

    _seed_cache({**ENTRY, "latest": "0.4.0"})
    uc.notify_cli("0.2.0")
    assert "0.4.0" in capsys.readouterr().err


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
