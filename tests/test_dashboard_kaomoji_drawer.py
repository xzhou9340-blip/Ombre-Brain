"""颜文字抽屉（移植自 KaomojiDrawerKit）在两份 dashboard.html 里都要在，且保持一致。

仓库里 dashboard.html 和 frontend/dashboard.html 是两份必须同步的拷贝（热更新只
下发 frontend/），所以这里跟其它 dashboard 测试一样，两份一起断言。
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ("dashboard.html", "frontend/dashboard.html")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _bundled(text: str) -> list:
    match = re.search(r"const KM_BUNDLED = (\[.*?\]);\n", text, re.S)
    assert match, "找不到 KM_BUNDLED 字面量"
    return json.loads(match.group(1))


def test_drawer_markup_and_trigger_present() -> None:
    for rel in DASHBOARDS:
        text = _read(rel)

        assert 'id="kaomoji-drawer"' in text
        assert 'id="km-tabs"' in text
        assert 'id="km-body"' in text
        assert 'id="km-count"' in text
        # 写信框旁边的入口
        assert "openKaomojiDrawer('letter-content')" in text
        # 原仓库那三个矢量图标
        for symbol in ("i-kaomoji-face", "i-kaomoji-close", "i-kaomoji-tidy"):
            assert f'id="{symbol}"' in text, (rel, symbol)


def test_bundled_library_matches_upstream_shape() -> None:
    for rel in DASHBOARDS:
        categories = _bundled(_read(rel))

        assert len(categories) == 21
        assert sum(len(c["items"]) for c in categories) == 204
        assert categories[0]["name"] == "抱抱"
        for cat in categories:
            assert isinstance(cat["name"], str) and cat["name"]
            assert cat["items"] and all(isinstance(v, str) and v for v in cat["items"])


def test_both_dashboards_ship_the_same_library() -> None:
    first, second = (_bundled(_read(rel)) for rel in DASHBOARDS)
    assert first == second


def test_storage_key_stays_compatible_with_upstream() -> None:
    # 键名沿用 Swift 版的 UserDefaults key，别改：改了用户自己加的颜文字就丢了
    for rel in DASHBOARDS:
        assert "const KM_STORAGE_KEY = 'KaomojiDrawerKit.library.v1';" in _read(rel)


def test_faces_are_rendered_as_text_not_html() -> None:
    # 颜文字里有 < > & 这些字符，一旦走 innerHTML 就会被当标签吃掉
    for rel in DASHBOARDS:
        text = _read(rel)
        script = text[text.index("// ====== 颜文字抽屉 ======"):]
        script = script[: script.index("</script>")]
        # 注释里提了 innerHTML 一嘴，别把它算成用了
        code = "\n".join(ln.split("//")[0] for ln in script.splitlines())
        assert "innerHTML" not in code
        assert "btn.textContent = face;" in code


def test_attribution_is_kept_in_page() -> None:
    for rel in DASHBOARDS:
        text = _read(rel)
        assert "https://github.com/Pyruslili/KaomojiDrawerKit" in text
        assert "MIT" in text[text.index("id=\"kaomoji-drawer\"") - 1500 : text.index("id=\"kaomoji-drawer\"")]
