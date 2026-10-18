"""Сторонние файлы Mini App (`miniapp/vendor/`) — ровно те, что проверены.

Lightweight Charts 5.2.1 (TradingView, Apache 2.0): `standalone.production.js`
из npm-пакета (integrity tarball сверена с реестром 15.10.2026), LICENSE из
пакета, NOTICE — из репозитория на теге v5.2.1 (в пакет не входит).

Шрифты (Д.1, 18.10.2026): npm `@fontsource-variable/golos-text`, `@fontsource/ibm-plex-mono`,
`@fontsource/caveat` 5.3.0 (опубликованы 19.07.2026; integrity tarball сверена с реестром),
только woff2 наборов latin и cyrillic, LICENSE (SIL OFL 1.1) из каждого пакета — рядом с
файлами. Версия — в имени папки: vendor/ Caddy отдаёт immutable. `@font-face` — в своём
`miniapp/fonts.css` (вне vendor, no-cache).

Обновление — только осознанно: новый файл, новые хеши здесь, проверка CSP
(docs/miniapp-plan.md §5а, §9а). Байты не меняет autocrlf — `.gitattributes`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MINIAPP = _ROOT / "miniapp"
_VENDOR = _MINIAPP / "vendor"
_LIB = "lightweight-charts-5.2.1.js"
_GOLOS = "fonts/golos-text-5.3.0/"
_PLEX = "fonts/ibm-plex-mono-5.3.0/"
_CAVEAT = "fonts/caveat-5.3.0/"

_SHA256 = {
    _LIB: "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf",
    "LICENSE": "70c9d5382506dd184465425c08a99ad9bd6d9ac1313c252968ba0b585e5ef823",
    "NOTICE": "f76c6afab94884448f0426e30d6e9d555ca7247894cd3484e477d2f87513036e",
    _GOLOS + "LICENSE": "54dcae300688bccf111e43c818a6ee2261d10fc8d7eb5b9c9aa2a1e9b03189c9",
    _GOLOS + "golos-text-cyrillic-wght-normal.woff2":
        "17d048ca05cb1218af3c0d6dcdf882989e6d1cc5dcb598ea50eaf54850ff7229",
    _GOLOS + "golos-text-latin-wght-normal.woff2":
        "9a69d0aa4734c4022224c002a3d944a702e0204972a49d892789f5668b922c2a",
    _PLEX + "LICENSE": "23b0a9d0c6d3f140a0b77e483c5cfa6bba574325ef5cb189ed9f2fec4884533f",
    _PLEX + "ibm-plex-mono-cyrillic-400-normal.woff2":
        "7635422bd10ddbbcb0e0caa1154b928298f5c8319b8c00b30978d4395f4783ce",
    _PLEX + "ibm-plex-mono-cyrillic-500-normal.woff2":
        "1d89462e86cb8e87e7e9d6f6635605bd7c3081baca6caaecee84916bf7b60edc",
    _PLEX + "ibm-plex-mono-cyrillic-600-normal.woff2":
        "2b05a8695c7f9f4abd7178b279646a3b005a8ce57546bef6fd145f25d131e0aa",
    _PLEX + "ibm-plex-mono-latin-400-normal.woff2":
        "08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7",
    _PLEX + "ibm-plex-mono-latin-500-normal.woff2":
        "01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22",
    _PLEX + "ibm-plex-mono-latin-600-normal.woff2":
        "0d1f0b8d0722224e32e9f28261bdc86c79115be73444ae5eceb73976a1bcdf83",
    _CAVEAT + "LICENSE": "163a2b400e16916ad3196296c946c526d0efce6baf22a2164269ffc62fb9f671",
    _CAVEAT + "caveat-cyrillic-600-normal.woff2":
        "f8d027f997b171cd9fc31f8aa5ce3ebe23ba0d0dc58000cd2e635d1d4b4ea4cb",
    _CAVEAT + "caveat-latin-600-normal.woff2":
        "d51e2283010e661d9f3dafdc9ff4b82b2ebcb2f7aa43ca48a105f5f68d46cc32",
}

# Наборы символов — как в пакетах @fontsource (latin, cyrillic).
_RANGES = {
    "latin": "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, "
             "U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, "
             "U+FEFF, U+FFFD",
    "cyrillic": "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116",
}


def _vendor_files() -> set[str]:
    return {p.relative_to(_VENDOR).as_posix() for p in _VENDOR.rglob("*") if p.is_file()}


def _faces() -> list[dict[str, str]]:
    css = (_MINIAPP / "fonts.css").read_text(encoding="utf-8")
    out = []
    for body in re.findall(r"@font-face\s*\{([^}]*)\}", css):
        props = dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+);", body))
        out.append({k: " ".join(v.split()) for k, v in props.items()})
    return out


def _src(face: dict[str, str]) -> str:
    m = re.fullmatch(r'url\(vendor/([^)]+)\) format\("woff2"\)', face["src"])
    assert m, face["src"]
    return m.group(1)


def test_vendor_holds_only_known_files() -> None:
    assert _vendor_files() == set(_SHA256)


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


# --- шрифты ------------------------------------------------------------------------------


def test_every_font_folder_is_versioned_and_carries_ofl() -> None:
    """vendor/ — immutable у Caddy: новая версия шрифта — новая папка, не замена байтов."""
    folders = {name.rsplit("/", 1)[0] for name in _SHA256 if name.startswith("fonts/")}
    assert folders == {_GOLOS[:-1], _PLEX[:-1], _CAVEAT[:-1]}
    for folder in folders:
        assert re.fullmatch(r"fonts/[a-z-]+-\d+\.\d+\.\d+", folder), folder
        text = (_VENDOR / folder / "LICENSE").read_text(encoding="utf-8")
        assert "SIL Open Font License, Version 1.1" in text, folder


def test_font_face_rules_cover_every_woff2_once() -> None:
    srcs = [_src(f) for f in _faces()]
    woff2 = sorted(n for n in _SHA256 if n.endswith(".woff2"))
    assert sorted(srcs) == woff2
    assert len(srcs) == len(set(srcs))


def test_font_families_weights_and_ranges() -> None:
    seen: dict[str, set[str]] = {}
    for face in _faces():
        family = face["font-family"].strip('"')
        src = _src(face)
        subset = "cyrillic" if "-cyrillic-" in src else "latin"
        assert face["unicode-range"] == _RANGES[subset], src
        assert face["font-display"] == "swap", src
        assert face["font-style"] == "normal", src
        seen.setdefault(family, set()).add(f"{face['font-weight']}/{subset}")
    assert seen == {
        "Golos Text": {"400 900/latin", "400 900/cyrillic"},
        "IBM Plex Mono": {f"{w}/{s}" for w in ("400", "500", "600") for s in _RANGES},
        "Caveat": {"600/latin", "600/cyrillic"},
    }


def test_fonts_css_lives_outside_vendor_and_loads_before_style() -> None:
    """fonts.css правится вместе с vendor — сам не в vendor (там immutable)."""
    html = (_MINIAPP / "index.html").read_text(encoding="utf-8")
    sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)">', html)
    assert sheets == ["fonts.css", "style.css"]


def test_preloaded_fonts_are_used_and_cors() -> None:
    """preload шрифта — только с crossorigin (иначе второй запрос) и только того, что стоит на
    body (неиспользованный preload — предупреждение в console, browser_check его ловит)."""
    html = (_MINIAPP / "index.html").read_text(encoding="utf-8")
    preloads = re.findall(r'<link rel="preload"([^>]*)>', html)
    assert preloads
    srcs = {_src(f) for f in _faces()}
    for attrs in preloads:
        flat = " ".join(attrs.split())
        href = re.search(r'href="vendor/([^"]+)"', flat)
        assert href and href.group(1) in srcs, flat
        assert 'as="font"' in flat and 'type="font/woff2"' in flat
        assert re.search(r"\bcrossorigin\b", flat), flat
        assert href.group(1).startswith(_GOLOS), "preload вне Golos — не на body (Д.1)"
    body = (_MINIAPP / "style.css").read_text(encoding="utf-8")
    assert re.search(r'font:\s*[^;]*"Golos Text"', body)


def test_csp_takes_fonts_from_self_only() -> None:
    """font-src не задан — действует default-src 'self': чужие шрифты браузер не загрузит."""
    caddy = (_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    csp = re.search(r'Content-Security-Policy "([^"]+)"', caddy)
    assert csp
    directives = [d.strip() for d in csp.group(1).split(";")]
    assert "default-src 'self'" in directives
    assert not [d for d in directives if d.startswith("font-src")]


def test_miniapp_is_not_in_docker_image() -> None:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "miniapp/" in lines


def test_vendor_bytes_are_not_normalised_by_git() -> None:
    lines = (_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "miniapp/vendor/** -text" in lines
