"""Сторонние файлы Mini App (`miniapp/vendor/`) — ровно те, что проверены.

Lightweight Charts 5.2.1 (TradingView, Apache 2.0): `standalone.production.js`
из npm-пакета (integrity tarball сверена с реестром 15.10.2026), LICENSE из
пакета, NOTICE — из репозитория на теге v5.2.1 (в пакет не входит). Обновление
библиотеки — только осознанно: новый файл, новые хеши здесь, проверка CSP
(docs/miniapp-plan.md §5а). Байты не меняет autocrlf — `.gitattributes`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VENDOR = _ROOT / "miniapp" / "vendor"
_LIB = "lightweight-charts-5.2.1.js"

_SHA256 = {
    _LIB: "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf",
    "LICENSE": "70c9d5382506dd184465425c08a99ad9bd6d9ac1313c252968ba0b585e5ef823",
    "NOTICE": "f76c6afab94884448f0426e30d6e9d555ca7247894cd3484e477d2f87513036e",
}


def test_vendor_holds_only_known_files() -> None:
    present = {p.name for p in _VENDOR.iterdir()}
    assert present == set(_SHA256)


def test_vendor_files_match_recorded_sha256() -> None:
    for name, digest in _SHA256.items():
        actual = hashlib.sha256((_VENDOR / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name}: sha256 {actual}"


def test_library_is_the_recorded_version() -> None:
    text = (_VENDOR / _LIB).read_text(encoding="utf-8")
    assert "TradingView Lightweight Charts™ v5.2.1" in text[:300]
    assert 'version:function(){return"5.2.1"}' in text


def test_notice_and_license_are_apache_attribution() -> None:
    assert "Apache License" in (_VENDOR / "LICENSE").read_text(encoding="utf-8")
    notice = (_VENDOR / "NOTICE").read_text(encoding="utf-8")
    assert "TradingView, Inc." in notice
    assert "https://www.tradingview.com/" in notice


def test_miniapp_is_not_in_docker_image() -> None:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "miniapp/" in lines


def test_vendor_bytes_are_not_normalised_by_git() -> None:
    lines = (_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "miniapp/vendor/** -text" in lines
