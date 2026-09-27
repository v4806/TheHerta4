"""贴图自动重载的「重连 vs 外部改图」区分单测（假 bpy 环境，不依赖 Blender）。

背景：DDS 转换会把 `image.filepath` 从 `.png` 重连到 `.dds`，而 `image.filepath`
参与 `_build_image_signature()`，于是定时器会把「重连」误判成「贴图被外部修改」，
进而通过 `_ensure_srgb_colorspace()` 把用户在 Blender 里设好的色彩空间强制成 sRGB
（表现为「重连没有沿用原本的纹理节点配置」）。

契约：
- 解析路径变了（重连）⇒ 仍 `reload()` 显示新文件，但**不改写 colorspace**；
- 路径不变、mtime/size 变了（外部改图）⇒ 保持既有语义：reload + 确保 sRGB；
- 首次看到某图（缓存未命中）⇒ 只登记，不 reload（既有语义）。
"""

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests import _real_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "texture_auto_reload_relink_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.utils", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []

# 真实 utils/common 子模块按 fake 包前缀登记（空 __path__ 假包解析不了相对导入）
_real_modules.register_real_common_modules(f"{PKG}.common")

# --- 假 bpy：texture_auto_reload 需要 bpy / bpy.app.handlers.persistent / bpy.app.timers ---
_fake_handlers = types.SimpleNamespace(persistent=lambda func: func, load_post=[])
_fake_timers = types.SimpleNamespace(
    is_registered=lambda _handle: False,
    register=lambda *_args, **_kwargs: None,
    unregister=lambda _handle: None,
)
_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Image=object),
    data=types.SimpleNamespace(images=[]),
    # 真实 Blender 里相对路径按工程目录解析；本用例只用绝对路径，原样返回即可
    path=types.SimpleNamespace(abspath=lambda value, library=None: value),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy.app", handlers=_fake_handlers, timers=_fake_timers)
_install_module("bpy.app.handlers", persistent=_fake_handlers.persistent, load_post=_fake_handlers.load_post)
_install_module("bpy.app.timers", **_fake_timers.__dict__)

for _real_module in ("log_utils", "texture_auto_reload"):
    _spec = importlib.util.spec_from_file_location(
        f"{PKG}.utils.{_real_module}", REPO_ROOT / "utils" / f"{_real_module}.py")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)

texture_auto_reload = sys.modules[f"{PKG}.utils.texture_auto_reload"]


class _LogRecorder:
    """接住 LOG.info / LOG.warning，便于断言日志契约。"""

    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, message):
        self.infos.append(str(message))

    def warning(self, message):
        self.warnings.append(str(message))


class _FakeImage:
    """bpy.data.images 元素替身。"""

    def __init__(self, name, filepath, colorspace="Non-Color"):
        self.name = name
        self.source = "FILE"
        self.packed_file = None
        self.is_dirty = False
        self.library = None
        self.tiles = []
        self.filepath = str(filepath)
        self.colorspace_settings = types.SimpleNamespace(name=colorspace)
        self.reload_calls = 0

    def as_pointer(self):
        return id(self)

    def reload(self):
        self.reload_calls += 1


class TextureAutoReloadRelinkTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp(prefix="tar_relink_"))
        self.addCleanup(shutil.rmtree, self._tmpdir, True)

        self.png_a = self._tmpdir / "DiffuseMap_Body.png"
        self.png_b = self._tmpdir / "DiffuseMap_Body.dds"
        self.png_a.write_bytes(b"a" * 32)
        self.png_b.write_bytes(b"b" * 64)

        # 签名缓存是模块级全局状态，逐用例清干净
        texture_auto_reload._image_signature_cache = {}
        self.addCleanup(setattr, texture_auto_reload, "_image_signature_cache", {})

        self.logs = _LogRecorder()
        _original_log = texture_auto_reload.LOG
        texture_auto_reload.LOG = self.logs
        self.addCleanup(setattr, texture_auto_reload, "LOG", _original_log)

    def _use_images(self, *images):
        _fake_bpy.data.images = list(images)

    def test_relink_keeps_colorspace_but_still_reloads(self):
        """路径变化（重连）⇒ 仍 reload，但 Non-Color 保持 Non-Color。"""
        image = _FakeImage("DiffuseMap_Body", self.png_a, colorspace="Non-Color")
        self._use_images(image)
        texture_auto_reload._prime_image_signature_cache()

        image.filepath = str(self.png_b)  # DDS 转换的重连
        reloadable, reloaded = texture_auto_reload._check_and_reload_changed_images()

        self.assertEqual(reloadable, 1)
        self.assertEqual(reloaded, 1)
        self.assertEqual(image.reload_calls, 1)
        self.assertEqual(image.colorspace_settings.name, "Non-Color")
        # 日志要能一眼认出「这是重连，不是外部改图」
        relinked_logs = [line for line in self.logs.infos if "Relinked" in line]
        self.assertEqual(len(relinked_logs), 1)
        self.assertIn("DiffuseMap_Body", relinked_logs[0])
        self.assertIn("colorspace kept: 'Non-Color'", relinked_logs[0])

    def test_external_edit_with_same_path_still_forces_srgb(self):
        """路径不变、size/mtime 变（外部改图）⇒ 既有语义：reload + 强制 sRGB。"""
        image = _FakeImage("DiffuseMap_Body", self.png_a, colorspace="Non-Color")
        self._use_images(image)
        texture_auto_reload._prime_image_signature_cache()

        self.png_a.write_bytes(b"a" * 96)  # 同一个文件被外部改写
        _reloadable, reloaded = texture_auto_reload._check_and_reload_changed_images()

        self.assertEqual(reloaded, 1)
        self.assertEqual(image.reload_calls, 1)
        self.assertEqual(image.colorspace_settings.name, "sRGB")

    def test_first_sighting_only_registers_without_reload(self):
        """缓存未命中（首次看到该图）只登记；无变化时不 reload。"""
        image = _FakeImage("DiffuseMap_Body", self.png_a, colorspace="Non-Color")
        self._use_images(image)

        reloadable, reloaded = texture_auto_reload._check_and_reload_changed_images()
        self.assertEqual(reloadable, 1)
        self.assertEqual(reloaded, 0)
        self.assertEqual(image.reload_calls, 0)

        _reloadable, reloaded = texture_auto_reload._check_and_reload_changed_images()
        self.assertEqual(reloaded, 0)
        self.assertEqual(image.reload_calls, 0)

        # 登记之后再变化才重载
        self.png_a.write_bytes(b"a" * 128)
        _reloadable, reloaded = texture_auto_reload._check_and_reload_changed_images()
        self.assertEqual(reloaded, 1)
        self.assertEqual(image.reload_calls, 1)

    def test_relink_and_external_edit_in_same_tick_are_independent(self):
        """同一次 tick 里：重连的图保留配置、外部改图的图仍被强制 sRGB。"""
        relinked = _FakeImage("Relinked_Tex", self.png_a, colorspace="Non-Color")
        edited_png = self._tmpdir / "LightMap_Hair.png"
        edited_png.write_bytes(b"c" * 16)
        edited = _FakeImage("Edited_Tex", edited_png, colorspace="Non-Color")
        self._use_images(relinked, edited)
        texture_auto_reload._prime_image_signature_cache()

        relinked.filepath = str(self.png_b)     # 重连
        edited_png.write_bytes(b"c" * 48)       # 外部改图
        _reloadable, reloaded = texture_auto_reload._check_and_reload_changed_images()

        self.assertEqual(reloaded, 2)
        self.assertEqual(relinked.colorspace_settings.name, "Non-Color")
        self.assertEqual(edited.colorspace_settings.name, "sRGB")


if __name__ == "__main__":
    unittest.main()
