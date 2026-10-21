"""工作空间原贴图 DDS 头 → 输出格式：整条判定链的单元测试。

引擎读的是工作空间里那份 DDS。BC5 是两通道格式，换成 BC7 写进去引擎照样只读两个
通道，所以「输出格式 = 原贴图头里的格式」是硬约束，规则表只做兜底。
"""
import importlib.util
import os
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


PKG = "_dds_header_format_test_pkg"
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
)
_install_module("bpy", **_fake_bpy.__dict__)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_dds_conversion.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_dds_conversion", module_path)
dds_conversion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dds_conversion
spec.loader.exec_module(dds_conversion)

PROPS = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])


def _write_dds(path, fourcc=b"DX10", dxgi_format=None, magic=b"DDS ", size=148):
    """写一个只有文件头的 DDS；size=128 时为 legacy（无 DX10 扩展头）。"""
    header = bytearray(size)
    header[0:4] = magic
    struct.pack_into("<I", header, 4, 124)
    if fourcc:
        header[84:88] = fourcc
    if dxgi_format is not None and size >= 132:
        struct.pack_into("<I", header, 128, dxgi_format)
    Path(path).write_bytes(bytes(header))
    return str(path)


class ReadDDSHeaderFormatTests(unittest.TestCase):
    """文件头 → 格式名（这是格式判定的唯一权威来源）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dds_header_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_dx10_srgb_diffuse(self):
        """实测：庄方宜 0f9e1087-156318-0-DiffuseMap.dds 是 DXGI 99"""
        path = _write_dds(self._path("a.dds"), dxgi_format=99)
        self.assertEqual("bc7_unorm_srgb", dds_conversion.read_dds_header_format(path))

    def test_dx10_linear_lightmap(self):
        """实测：同目录 LightMap.dds 是 DXGI 98（线性）"""
        path = _write_dds(self._path("b.dds"), dxgi_format=98)
        self.assertEqual("bc7_unorm", dds_conversion.read_dds_header_format(path))

    def test_legacy_bc5u_normal(self):
        """实测：NormalMap.dds 是传统 FOURCC BC5U —— 两通道，没有 sRGB 概念"""
        path = _write_dds(self._path("c.dds"), fourcc=b"BC5U", size=128)
        self.assertEqual("bc5_unorm", dds_conversion.read_dds_header_format(path))

    def test_legacy_dxt5_maps_to_bc3(self):
        path = _write_dds(self._path("d.dds"), fourcc=b"DXT5", size=128)
        self.assertEqual("bc3_unorm", dds_conversion.read_dds_header_format(path))

    def test_typeless_normalizes_to_unorm(self):
        """typeless 只声明内存布局、不声明语义，物理布局与 UNORM 相同"""
        path = _write_dds(self._path("e.dds"), dxgi_format=27)
        self.assertEqual("r8g8b8a8_unorm", dds_conversion.read_dds_header_format(path))

    def test_unknown_dxgi_id_returns_empty(self):
        path = _write_dds(self._path("f.dds"), dxgi_format=777)
        self.assertEqual("", dds_conversion.read_dds_header_format(path))

    def test_non_dds_magic_returns_empty(self):
        path = _write_dds(self._path("g.dds"), magic=b"PNG ", dxgi_format=99)
        self.assertEqual("", dds_conversion.read_dds_header_format(path))

    def test_short_file_returns_empty(self):
        path = os.path.join(self.temp_dir, "h.dds")
        Path(path).write_bytes(b"DDS short")
        self.assertEqual("", dds_conversion.read_dds_header_format(path))

    def test_missing_file_returns_empty(self):
        self.assertEqual("", dds_conversion.read_dds_header_format(self._path("nope.dds")))

    def test_dxgi_id_reader_is_dx10_only(self):
        self.assertEqual(99, dds_conversion.dds_header_dxgi_format(_write_dds(self._path("i.dds"), dxgi_format=99)))
        self.assertIsNone(dds_conversion.dds_header_dxgi_format(_write_dds(self._path("j.dds"), fourcc=b"BC5U", size=128)))


class ExistingDDSMatchesFormatTests(unittest.TestCase):
    """三态比较：复用前必须先验头，否则规则改了会永久沿用旧格式产物"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dds_match_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_same_format_is_true(self):
        path = _write_dds(os.path.join(self.temp_dir, "a.dds"), dxgi_format=99)
        self.assertIs(True, dds_conversion.existing_dds_matches_format(path, "bc7_unorm_srgb"))

    def test_different_format_is_false(self):
        path = _write_dds(os.path.join(self.temp_dir, "b.dds"), dxgi_format=98)
        self.assertIs(False, dds_conversion.existing_dds_matches_format(path, "bc7_unorm_srgb"))

    def test_unreadable_header_is_false(self):
        """垃圾产物必须重转，不能当成已就绪"""
        path = os.path.join(self.temp_dir, "c.dds")
        Path(path).write_bytes(b"DDS payload")
        self.assertIs(False, dds_conversion.existing_dds_matches_format(path, "bc7_unorm"))

    def test_missing_target_is_false(self):
        self.assertIs(False, dds_conversion.existing_dds_matches_format(os.path.join(self.temp_dir, "d.dds"), "bc7_unorm"))

    def test_table_outside_format_is_none(self):
        """读得出头但不是已知格式：答「不知道」，避免每次导出都重转"""
        path = _write_dds(os.path.join(self.temp_dir, "e.dds"), dxgi_format=777)
        self.assertIsNone(dds_conversion.existing_dds_matches_format(path, "bc7_unorm"))


class ResolveFormatTests(unittest.TestCase):
    """优先级：format_override → 原贴图头 → 类型规则 → 源文件名 → 默认"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dds_resolve_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_workspace_source_header_beats_rule_table(self):
        """规则说 DiffuseMap 走 sRGB，原贴图却是 BC5U —— 以原贴图为答案"""
        workspace = _write_dds(self._path("w.dds"), fourcc=b"BC5U", size=128)
        source = self._path("s.png")
        Path(source).write_bytes(b"png payload")
        resolved = dds_conversion.resolve_texture_dds_format(
            source, PROPS, texture_type="DiffuseMap", workspace_source_path=workspace
        )
        self.assertEqual("bc5_unorm", resolved)

    def test_source_dds_header_used_when_source_is_already_dds(self):
        source = _write_dds(self._path("s.dds"), dxgi_format=99)
        self.assertEqual(
            "bc7_unorm_srgb",
            dds_conversion.resolve_texture_dds_format(source, PROPS, texture_type="LightMap"),
        )

    def test_rule_table_fallback_when_no_source_texture(self):
        source = self._path("t.png")
        Path(source).write_bytes(b"png payload")
        self.assertEqual(
            "bc7_unorm_srgb",
            dds_conversion.resolve_texture_dds_format(source, PROPS, texture_type="MaterialMap"),
        )

    def test_format_override_wins_over_everything(self):
        workspace = _write_dds(self._path("w2.dds"), fourcc=b"BC5U", size=128)
        source = self._path("u.png")
        Path(source).write_bytes(b"png payload")
        self.assertEqual(
            "r8g8b8a8_unorm",
            dds_conversion.resolve_texture_dds_format(
                source, PROPS, texture_type="DiffuseMap", workspace_source_path=workspace,
                format_override="r8g8b8a8_unorm",
            ),
        )


class ConvertUsesSourceFormatTests(unittest.TestCase):
    """转换入口真的把原贴图格式喂给 texconv，写不出时原样复制"""

    def setUp(self):
        dds_conversion.clear_dds_conversion_cache()
        self.temp_dir = tempfile.mkdtemp(prefix="dds_convert_src_")
        self.source = os.path.join(self.temp_dir, "c209c22b-45087-0-DiffuseMap.png")
        Path(self.source).write_bytes(b"png payload")
        self.workspace = _write_dds(os.path.join(self.temp_dir, "orig.dds"), fourcc=b"BC5U", size=128)
        self.target = os.path.join(self.temp_dir, "out", "Body_Material.dds")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_texconv_receives_workspace_format(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            out_dir = command[command.index("-o") + 1]
            produced = os.path.join(out_dir, os.path.splitext(os.path.basename(command[-1]))[0] + ".dds")
            Path(produced).write_bytes(Path(self.workspace).read_bytes())
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(dds_conversion, "find_texconv", return_value="texconv.exe"), \
                mock.patch.object(dds_conversion.subprocess, "run", side_effect=fake_run):
            ok, reason = dds_conversion.convert_texture_to_dds_cached(
                self.source, self.target, PROPS, texture_type="DiffuseMap",
                workspace_source_path=self.workspace,
            )

        self.assertTrue(ok, reason)
        self.assertEqual("bc5_unorm", calls[0][calls[0].index("-f") + 1])
        self.assertTrue(os.path.isfile(self.target))

    def test_unwritable_format_copies_original_verbatim(self):
        """texconv 写不出原格式时不换格式重编码，直接复制原贴图"""

        def fake_run(_command, **_kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="unsupported format")

        with mock.patch.object(dds_conversion, "find_texconv", return_value="texconv.exe"), \
                mock.patch.object(dds_conversion.subprocess, "run", side_effect=fake_run):
            ok, reason = dds_conversion.convert_texture_to_dds_cached(
                self.source, self.target, PROPS, texture_type="DiffuseMap",
                workspace_source_path=self.workspace,
            )

        self.assertTrue(ok, reason)
        self.assertEqual("copied-verbatim:bc5_unorm", reason)
        self.assertEqual(Path(self.workspace).read_bytes(), Path(self.target).read_bytes())


if __name__ == "__main__":
    unittest.main()
