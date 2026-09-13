# -*- coding: utf-8 -*-
"""切换面板按钮图标的优先级与备注文字图标生成。

优先级：单按钮图片 → 全局回退图片 → 备注文字图标（use_remark_as_icon）→ 默认按钮图。
备注文字图标曾在重构中被整段删除，这里把优先级链锁住，避免再次被回退逻辑挤掉。
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PKG = "_test_swap_panel_button_icon"
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
DEFAULT_BUTTON_SIZE = (384, 64)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fake_bpy():
    module = types.ModuleType("bpy")
    module.types = types.SimpleNamespace(Node=object, Operator=object, PropertyGroup=object)
    prop = lambda **_kwargs: None
    props = types.ModuleType("bpy.props")
    for name in (
        "StringProperty", "BoolProperty", "IntProperty", "FloatProperty",
        "EnumProperty", "CollectionProperty", "FloatVectorProperty",
    ):
        setattr(props, name, prop)
    module.props = props
    module.path = types.SimpleNamespace(abspath=lambda path: path)
    return module


def _load_panel():
    fake_bpy = _fake_bpy()
    package = types.ModuleType(PKG + ".blueprint")
    package.__path__ = []
    base = types.ModuleType(PKG + ".blueprint.node_postprocess_base")

    class Base:
        @classmethod
        def split_anim_driver_block_content(cls, content):
            return "", content

        @classmethod
        def split_auto_appended_tail_content(cls, content):
            return content, ""

    base.SSMTNode_PostProcess_Base = Base
    stubs = {
        "bpy": fake_bpy,
        "bpy.props": fake_bpy.props,
        package.__name__: package,
        base.__name__: base,
    }
    with mock.patch.dict(sys.modules, stubs):
        return _load_module(
            PKG + ".blueprint.node_postprocess_swap_panel",
            ROOT / "blueprint" / "node_postprocess_swap_panel.py",
        )


class SwapPanelButtonIconTests(unittest.TestCase):
    """按钮图标优先级：单按钮图片 → 全局回退 → 备注文字图标 → 默认按钮图。"""

    DEFAULTS = {
        "use_remark_as_icon": True,
        "remark_font_family": "msyh.ttc",
        "remark_font_size": 36,
        "remark_text_color": (1.0, 1.0, 1.0),
        "remark_stroke_color": (0.0, 0.0, 0.0),
        "remark_stroke_width": 2,
        "button_image": "",
        "button_border_image": "",
    }

    def setUp(self):
        self.module = _load_panel()
        self.panel = self.module.SSMTNode_PostProcess_SwapPanel()
        for name, value in self.DEFAULTS.items():
            setattr(self.panel, name, value)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.res = self.root / "res"
        self.res.mkdir()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.custom = self.root / "custom.png"
        self._write_png(self.custom, (200, 50))
        self._write_png(self.assets / "0.png", (64, 64))

    @staticmethod
    def _write_png(path, size):
        from PIL import Image

        Image.new("RGBA", size, (255, 0, 0, 255)).save(path)

    @staticmethod
    def _button(**kwargs):
        button = {
            "var_name": "$swapkey0",
            "comment": "",
            "option_count": 2,
            "hotkey": "1",
            "image_path": "",
        }
        button.update(kwargs)
        return button

    def _generate(self, index, button):
        return self.panel._ensure_button_image(
            str(self.res), "ns1", index, str(self.assets), button
        )

    def _size(self, path):
        from PIL import Image

        with Image.open(path) as image:
            return image.size

    def _assert_copied(self, generated, source):
        self.assertTrue(generated and Path(generated).is_file())
        self.assertEqual(Path(generated).read_bytes(), Path(source).read_bytes())

    # ---------------------------------------------------------------- 优先级
    def test_per_button_image_wins_over_everything(self):
        self.panel.button_image = str(self.custom)
        generated = self._generate(1, self._button(image_path=str(self.custom), comment="武器切换"))
        self._assert_copied(generated, self.custom)

    def test_global_fallback_used_when_button_has_no_image(self):
        self.panel.button_image = str(self.custom)
        generated = self._generate(2, self._button(comment="武器切换"))
        self._assert_copied(generated, self.custom)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_remark_text_icon_used_when_no_images_configured(self):
        generated = self._generate(3, self._button(comment="武器切换"))
        self.assertTrue(generated and Path(generated).is_file())
        self.assertNotEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_remark_icon_honours_forced_line_break(self):
        single = self._generate(4, self._button(comment="武器切换"))
        wrapped = self._generate(5, self._button(comment="武器/切换"))
        self.assertGreater(self._size(wrapped)[1], self._size(single)[1])

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_missing_remark_icon_file_falls_back_to_default_button(self):
        generated = self._generate(6, self._button(comment=""))
        self.assertEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_remark_icon_can_be_disabled(self):
        self.panel.use_remark_as_icon = False
        generated = self._generate(7, self._button(comment="武器切换"))
        self.assertEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_invalid_per_button_image_falls_back_to_remark_icon(self):
        missing = str(self.root / "not_here.png")
        generated = self._generate(8, self._button(image_path=missing, comment="武器切换"))
        self.assertTrue(generated and Path(generated).is_file())
        self.assertNotEqual(self._size(generated), DEFAULT_BUTTON_SIZE)


if __name__ == "__main__":
    unittest.main()
