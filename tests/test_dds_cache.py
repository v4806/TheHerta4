# -*- coding: utf-8 -*-
"""DDS 转换结果缓存：命中 → copy2、未命中 → 转换 + 回写；失效 / 清理 / 开关。

测试口径：
- 转换用 fake subprocess.run 代替 texconv，计数即「实际转换次数」；
- 缓存目录指向临时目录（patch ``dds_cache_dir``），绝不碰用户的
  ``%LOCALAPPDATA%\\TheHerta4\\dds_cache``；
- 产物字节是重点：缓存内容必须等于首次转换产物，命中复制出来的字节必须与之相同。
"""
import contextlib
import importlib.util
import io
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_dds_cache_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object, Panel=object),
    props=types.SimpleNamespace(
        BoolProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        StringProperty=lambda **_kwargs: None,
    ),
    context=types.SimpleNamespace(scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace())),
    data=types.SimpleNamespace(images=[]),
    path=types.SimpleNamespace(abspath=lambda value: "" if value == "//" else value),
)
_install_module("bpy", **_fake_bpy.__dict__)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_dds_conversion.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_dds_conversion", module_path)
dds_conversion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dds_conversion
spec.loader.exec_module(dds_conversion)


DXGI_BY_FORMAT = {
    "bc7_unorm": 98,
    "bc7_unorm_srgb": 99,
    "r8g8b8a8_unorm": 28,
    "bc1_unorm": 71,
}


def make_dds_bytes(dds_format, tail=b"\x00" * 32):
    """最小但「头部合法」的 DDS：148 字节 DX10 头 + 任意 payload。

    缓存校验按真实规则（≥148 字节 + ``DDS `` magic + DXGI 格式号）判定，
    所以 fake 产物必须长这样，否则会被当成损坏产物拒绝缓存。
    """
    header = bytearray(148)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    header[84:88] = b"DX10"
    struct.pack_into("<I", header, 128, DXGI_BY_FORMAT[dds_format])
    return bytes(header) + tail


class DDSCacheTestCase(unittest.TestCase):
    """公共装置：临时源图 / 临时缓存目录 / 可计数的 fake 转换。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dds_cache_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cache_dir = os.path.join(self.root, "cache")
        self.source_dir = os.path.join(self.root, "workspace")
        os.makedirs(self.source_dir, exist_ok=True)
        self.source = os.path.join(self.source_dir, "DiffuseMap_Body.png")
        Path(self.source).write_bytes(b"png payload v1")
        self.target = os.path.join(self.root, "Mod", "Textures", "Body.dds")
        self.props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        self.calls = []

        for patcher in (
            mock.patch.object(dds_conversion, "dds_cache_dir", return_value=self.cache_dir),
            mock.patch.object(dds_conversion, "find_texconv", return_value="texconv.exe"),
            mock.patch.object(dds_conversion.subprocess, "run", side_effect=self._fake_texconv),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_texconv(self, command, **_kwargs):
        """假 texconv：按 -f 写出一份头正确的 DDS，并记录调用。"""
        self.calls.append(list(command))
        dds_format = command[command.index("-f") + 1]
        out_dir = command[command.index("-o") + 1]
        produced = os.path.join(
            out_dir, os.path.splitext(os.path.basename(command[-1]))[0] + ".dds"
        )
        Path(produced).write_bytes(make_dds_bytes(dds_format, b"payload-%d" % len(self.calls)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def convert(self, target=None, **kwargs):
        return dds_conversion.convert_texture_to_dds(
            self.source, target or self.target, self.props, **kwargs
        )

    def cache_entries(self):
        if not os.path.isdir(self.cache_dir):
            return []
        return sorted(
            name for name in os.listdir(self.cache_dir) if name.endswith(".dds")
        )


class CacheHitTests(DDSCacheTestCase):
    """首次转换写缓存、第二次命中复制，产物字节一致。"""

    def test_first_conversion_writes_cache_and_second_hit_skips_texconv(self):
        ok, reason = self.convert()
        self.assertTrue(ok, reason)
        self.assertEqual(1, len(self.calls), "首次必须真的转换一次")

        entries = self.cache_entries()
        self.assertEqual(1, len(entries), "首次转换后应写入一个缓存条目")
        self.assertRegex(entries[0], r"^[0-9a-f]{32}\.dds$", "缓存文件名 = sha256 前 32 位")
        first_bytes = Path(self.target).read_bytes()
        self.assertEqual(first_bytes, Path(self.cache_dir, entries[0]).read_bytes())

        # 第二次：目标目录整个删掉，验证命中时能自己建目录、且不再调 texconv。
        shutil.rmtree(os.path.dirname(self.target))
        ok, reason = self.convert()

        self.assertTrue(ok, reason)
        self.assertEqual(1, len(self.calls), "命中缓存时不得再次调用 texconv")
        self.assertEqual(first_bytes, Path(self.target).read_bytes(), "命中产物字节必须与首次一致")

    def test_hit_does_not_depend_on_target_path(self):
        """缓存键是（源图, 格式），换目标文件名/目录仍应命中。"""
        ok, reason = self.convert()
        self.assertTrue(ok, reason)
        first_bytes = Path(self.target).read_bytes()

        other = os.path.join(self.root, "Other", "DiffuseMap_Body.dds")
        ok, reason = self.convert(target=other)

        self.assertTrue(ok, reason)
        self.assertEqual(1, len(self.calls))
        self.assertEqual(first_bytes, Path(other).read_bytes())

    def test_different_target_format_uses_different_key(self):
        """同源不同格式不能互相命中（缓存键含目标格式与色彩空间标志）。"""
        ok, reason = self.convert(format_override="bc7_unorm")
        self.assertTrue(ok, reason)
        linear_bytes = Path(self.target).read_bytes()

        ok, reason = self.convert(target=os.path.join(self.root, "out2", "Body.dds"),
                                  format_override="r8g8b8a8_unorm")
        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "换格式必须重新转换")
        self.assertEqual(2, len(self.cache_entries()), "两种格式各占一个缓存键")
        rgba_bytes = Path(self.root, "out2", "Body.dds").read_bytes()
        self.assertNotEqual(linear_bytes, rgba_bytes)

        # 回到第一种格式：应命中第一份缓存。
        ok, reason = self.convert(target=os.path.join(self.root, "out3", "Body.dds"),
                                  format_override="bc7_unorm")
        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls))
        self.assertEqual(
            linear_bytes, Path(self.root, "out3", "Body.dds").read_bytes()
        )


class CacheInvalidationTests(DDSCacheTestCase):
    """源文件变化 / 缓存损坏 / 缓存缺失 都必须回退重新转换，且不报错。"""

    def test_source_mtime_change_invalidates_cache(self):
        self.convert()
        self.assertEqual(1, len(self.calls))

        stat = os.stat(self.source)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        ok, reason = self.convert()
        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "mtime 变了必须重新转换")
        self.assertEqual(2, len(self.cache_entries()), "失效键不删，另写一个新键")

    def test_source_size_change_invalidates_cache(self):
        self.convert()
        self.assertEqual(1, len(self.calls))

        Path(self.source).write_bytes(b"png payload v2 (longer)")

        ok, reason = self.convert()
        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "size 变了必须重新转换")

    def test_corrupt_cache_falls_back_to_conversion(self):
        self.convert()
        entry = os.path.join(self.cache_dir, self.cache_entries()[0])
        Path(entry).write_bytes(b"DDS " + b"\x00" * 20)  # 24 字节：头都不完整

        ok, reason = self.convert()

        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "损坏的缓存必须被忽略并重转")
        self.assertGreaterEqual(len(Path(self.target).read_bytes()), 148)
        # 重转后缓存被原子覆盖成完好产物
        self.assertTrue(dds_conversion._dds_cache_entry_is_usable(entry, "bc7_unorm_srgb"))

    def test_missing_cache_falls_back_to_conversion(self):
        self.convert()
        os.remove(os.path.join(self.cache_dir, self.cache_entries()[0]))

        ok, reason = self.convert()

        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "缓存缺失必须重新转换")
        self.assertEqual(1, len(self.cache_entries()))

    def test_hit_copy_failure_falls_back_to_conversion(self):
        """命中但复制失败（目标被占用等）不能让导出失败，必须回落重转。"""
        self.convert()
        self.assertEqual(1, len(self.calls))

        with mock.patch.object(dds_conversion.shutil, "copy2", side_effect=OSError("目标被占用")):
            ok, reason = self.convert()

        self.assertTrue(ok, reason)
        # 回落后仍写不出目标文件（copy2 被整体打桩），因此只断言「重试过转换」。
        self.assertEqual(2, len(self.calls))


class CacheSwitchTests(DDSCacheTestCase):
    """``HERTA_DDS_CACHE=0`` 时不读不写缓存。"""

    def test_env_zero_disables_cache_read_and_write(self):
        with mock.patch.dict(os.environ, {"HERTA_DDS_CACHE": "0"}):
            ok, reason = self.convert()
            self.assertTrue(ok, reason)
            ok, reason = self.convert()

        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "关闭后每次都真的转换")
        self.assertFalse(os.path.exists(self.cache_dir), "关闭时不得创建缓存目录")

    def test_env_zero_does_not_use_existing_cache(self):
        self.convert()
        self.assertEqual(1, len(self.calls))
        shutil.rmtree(os.path.dirname(self.target))

        with mock.patch.dict(os.environ, {"HERTA_DDS_CACHE": "0"}):
            ok, reason = self.convert()

        self.assertTrue(ok, reason)
        self.assertEqual(2, len(self.calls), "关闭后即使已有缓存也要重新转换")

    def test_default_is_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERTA_DDS_CACHE", None)
            self.assertTrue(dds_conversion.dds_cache_enabled())
            self.assertEqual(2 * 1024 * 1024 * 1024, dds_conversion._DDS_CACHE_MAX_BYTES)


class CacheTrimTests(DDSCacheTestCase):
    """超过总大小上限：按 mtime 从旧到新删，直到低于上限的 80%。"""

    def _write_entries(self, count=5, size=1000):
        os.makedirs(self.cache_dir, exist_ok=True)
        paths = []
        base = 1_600_000_000_000_000_000  # 固定基准，避免受文件系统当前时间影响
        for index in range(count):
            path = os.path.join(self.cache_dir, f"entry{index}.dds")
            Path(path).write_bytes(b"x" * size)
            os.utime(path, ns=(base + index, base + index))
            paths.append(path)
        return paths

    def test_trim_deletes_oldest_until_below_80_percent(self):
        self._write_entries()  # 5 × 1000B = 5000B

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dds_conversion.trim_dds_cache(self.cache_dir, max_bytes=4000)

        # 5000 > 4000 → 删到 ≤ 3200：删最老 2 个（3000B），剩 3 个
        remaining = sorted(os.listdir(self.cache_dir))
        self.assertEqual(["entry2.dds", "entry3.dds", "entry4.dds"], remaining)
        self.assertIn("[DDS缓存]", out.getvalue())

    def test_trim_keeps_the_entry_just_written(self):
        paths = self._write_entries()

        with contextlib.redirect_stdout(io.StringIO()):
            dds_conversion.trim_dds_cache(
                self.cache_dir, max_bytes=1000, keep_path=paths[-1]
            )

        # 上限 1000 → 目标 800；唯一保留的是刚写入的 entry4（1000B > 800），
        # 其余全删，且 keep_path 绝不被删。
        self.assertEqual(["entry4.dds"], sorted(os.listdir(self.cache_dir)))

    def test_no_trim_below_limit(self):
        self._write_entries(count=2, size=100)

        dds_conversion.trim_dds_cache(self.cache_dir, max_bytes=4000)

        self.assertEqual(2, len(os.listdir(self.cache_dir)))

    def test_store_triggers_trim_when_over_limit(self):
        """store 之后自动清理：旧条目被删、刚写入的那一条保留。"""
        os.makedirs(self.cache_dir, exist_ok=True)
        stale = os.path.join(self.cache_dir, "stale.dds")
        Path(stale).write_bytes(b"y" * 2000)
        os.utime(stale, ns=(1, 1))

        with mock.patch.object(dds_conversion, "_DDS_CACHE_MAX_BYTES", 1500), \
                contextlib.redirect_stdout(io.StringIO()):
            ok, reason = self.convert()

        self.assertTrue(ok, reason)
        self.assertFalse(os.path.exists(stale), "超限时最旧的缓存应被删掉")
        self.assertEqual(1, len(self.cache_entries()))
        self.assertEqual(
            Path(self.target).read_bytes(), Path(self.cache_dir, self.cache_entries()[0]).read_bytes()
        )


if __name__ == "__main__":
    unittest.main()
