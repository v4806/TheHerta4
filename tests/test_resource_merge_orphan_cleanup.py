# -*- coding: utf-8 -*-
"""「资源合并」节点清理未引用贴图测试。

覆盖：
- 勾选后：未被 ini 引用的 Textures/*.dds 被删，被引用的保留
- .buf 文件（无论是否被引用）不处理
- 目录穿越引用（../outside.dds）不被采纳，mod 根外文件不受影响
- 只扫 Textures 这一层，不递归子目录
- 默认关闭：孤儿 .dds 保留（不影响现有导出结果）
- 大小写 / 斜杠不同的引用路径能正确匹配，不误删
- 删除失败静默、计数为 0、流程不中断
- 多 ini：任一 ini 引用的贴图都受保护
- report_stage 用桩验证文本为「资源合并：清理未引用贴图 done/total」
- UI：相似度去重之后显示清理开关，勾选后显示说明行
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_resource_merge_orphan_cleanup_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for pkg_name in (PKG, f"{PKG}.blueprint"):
    _pkg = _install_module(pkg_name)
    _pkg.__path__ = []

# 与既有资源合并测试一致：轻量 fake bpy 只桩 BoolProperty（模块内有 IntProperty 回退）
_install_module(
    "bpy",
    types=types.SimpleNamespace(),
    props=types.SimpleNamespace(BoolProperty=lambda **_k: False),
)


class _StubBase:
    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content)

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content), ""

    def _create_cumulative_backup(self, ini_file, mod_export_path):
        return None


_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=_StubBase)

_node_mod = _load_real(
    f"{PKG}.blueprint.node_postprocess_resource_merge",
    "blueprint/node_postprocess_resource_merge.py",
)
ResourceMergeNode = _node_mod.SSMTNode_PostProcess_ResourceMerge


class OrphanCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orphan_cleanup_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.mod_root = Path(self.tmp) / "mod"
        self.textures = self.mod_root / "Textures"
        self.textures.mkdir(parents=True)
        self.ini = self.mod_root / "Workspace.ini"
        self.addCleanup(self._pop_progress_stub)

    def _pop_progress_stub(self):
        package = sys.modules[f"{PKG}.blueprint"]
        if hasattr(package, "export_progress"):
            delattr(package, "export_progress")
        sys.modules.pop(f"{PKG}.blueprint.export_progress", None)

    def _install_progress_stub(self, reporter):
        module = types.ModuleType(f"{PKG}.blueprint.export_progress")
        module.report_stage = reporter
        sys.modules[module.__name__] = module
        sys.modules[f"{PKG}.blueprint"].export_progress = module
        return module

    def _write(self, relative, data=b"dds-bytes"):
        path = self.mod_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _write_ini(self, resources):
        self.ini.write_text(
            "".join(f"[{section}]\nfilename = {filename}\n\n" for section, filename in resources),
            encoding="utf-8",
        )

    def _node(self, enabled=True):
        node = ResourceMergeNode()
        node.clean_unreferenced_textures = enabled
        return node

    def _run(self, node, resources):
        self._write_ini(resources)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            node.execute_postprocess(str(self.mod_root))
        return buffer.getvalue()

    # -- 基本行为 --------------------------------------------------------
    def test_enabled_deletes_only_unreferenced_textures(self):
        """勾选后只删未被引用的 .dds；.buf（引用或未引用）都不动。"""
        orphan = self._write("Textures/a.dds", b"orphan-dds")
        kept = self._write("Textures/b.dds", b"kept-dds")
        orphan_buf = self._write("Textures/c.buf", b"orphan-buf")
        kept_buf = self._write("Textures/d.buf", b"kept-buf")

        output = self._run(
            self._node(True),
            [("ResourceB", "Textures/b.dds"), ("ResourceD", "Textures/d.buf")],
        )

        self.assertFalse(orphan.exists(), "未引用的孤儿 dds 应被删除")
        self.assertTrue(kept.exists(), "被 ini 引用的 dds 必须保留")
        self.assertTrue(orphan_buf.exists(), "不处理 .buf")
        self.assertTrue(kept_buf.exists(), "不处理被引用的 .buf")
        self.assertIn("清理未引用贴图: 1 个", output)

    def test_default_off_keeps_orphans(self):
        """默认关闭：孤儿 .dds 保留，与改动前的导出一致。"""
        orphan = self._write("Textures/a.dds", b"orphan-dds")
        kept = self._write("Textures/b.dds", b"kept-dds")

        self._run(self._node(False), [("ResourceB", "Textures/b.dds")])

        self.assertTrue(orphan.exists(), "默认关闭不应删除任何贴图")
        self.assertTrue(kept.exists())

    def test_case_and_slash_variants_are_matched(self):
        """引用写成 textures\\mixedcase.dds 也能匹配 Textures/MixedCase.DDS，不误删。"""
        referenced = self._write("Textures/MixedCase.DDS", b"mixed")
        orphan = self._write("Textures/orphan.dds", b"orphan")

        self._run(self._node(True), [("ResourceMixed", "textures\\mixedcase.dds")])

        self.assertTrue(referenced.exists(), "大小写 / 斜杠不同不能误删被引用贴图")
        self.assertFalse(orphan.exists())

    def test_path_traversal_reference_rejected_and_no_recursion(self):
        """../outside.dds 不被采纳，mod 根外文件不受影响；子目录 .dds 不递归清理。"""
        outside = self._write("../outside.dds", b"outside")
        orphan = self._write("Textures/a.dds", b"orphan-dds")
        kept = self._write("Textures/b.dds", b"kept-dds")
        nested = self._write("Textures/Sub/nested.dds", b"nested-dds")

        self._run(
            self._node(True),
            [("ResourceOutside", "../outside.dds"), ("ResourceB", "Textures/b.dds")],
        )

        self.assertTrue(outside.exists(), "mod 根外文件不受影响")
        self.assertTrue(nested.exists(), "只扫 Textures 这一层，不递归子目录")
        self.assertFalse(orphan.exists())
        self.assertTrue(kept.exists())

    def test_other_ini_references_are_respected(self):
        """多 ini 时：任一 ini 引用的贴图都算被引用，不能因另一个 ini 没引用而删除。"""
        kept = self._write("Textures/b.dds", b"kept-dds")
        orphan = self._write("Textures/a.dds", b"orphan-dds")
        (self.mod_root / "One.ini").write_text(
            "[ResourceB]\nfilename = Textures/b.dds\n", encoding="utf-8"
        )
        (self.mod_root / "Two.ini").write_text(
            "[ResourceX]\nfilename = Textures/x.dds\n", encoding="utf-8"
        )

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self._node(True).execute_postprocess(str(self.mod_root))

        self.assertTrue(kept.exists(), "另一个 ini 的引用也要保护贴图")
        self.assertFalse(orphan.exists())

    # -- 失败与进度 ------------------------------------------------------
    def test_delete_failure_is_silent_and_counted(self):
        """删除抛 OSError 时静默、文件保留、计数 0、不中断。"""
        a = self._write("Textures/a.dds", b"a")
        b = self._write("Textures/b.dds", b"b")
        node = self._node(True)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with mock.patch.object(_node_mod.os, "remove", side_effect=OSError("locked")):
                node._orphan_texture_cleanup_phase(str(self.mod_root), set())

        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertNotIn("Traceback", buffer.getvalue())
        self.assertIn("清理未引用贴图: 0 个", buffer.getvalue())

    def test_report_stage_receives_cleanup_progress(self):
        """有进度模块时上报「资源合并：清理未引用贴图 done/total」，只更新文本。"""
        calls = []
        self._install_progress_stub(lambda stage, done=None, total=None: calls.append(stage))
        for name in ("a.dds", "b.dds", "c.dds"):
            self._write(f"Textures/{name}", name.encode())

        self._run(self._node(True), [])

        self.assertTrue(calls, "应至少上报一次清理进度")
        for stage in calls:
            self.assertTrue(stage.startswith("资源合并：清理未引用贴图 "), stage)
            self.assertRegex(stage, r"^\S+ \d+/\d+$")
        self.assertEqual(calls[-1], "资源合并：清理未引用贴图 3/3")

    def test_report_stage_exception_is_swallowed(self):
        """进度桩抛异常时清理照旧。"""
        def _raise(*_args, **_kwargs):
            raise RuntimeError("no progress file")

        self._install_progress_stub(_raise)
        orphan = self._write("Textures/a.dds", b"a")
        kept = self._write("Textures/b.dds", b"b")

        self._run(self._node(True), [("ResourceB", "Textures/b.dds")])

        self.assertFalse(orphan.exists())
        self.assertTrue(kept.exists())

    # -- UI --------------------------------------------------------------
    def test_draw_buttons_shows_cleanup_toggle(self):
        """相似度去重块之后显示清理开关，勾选后显示说明行。"""
        calls = []

        class _Layout:
            def label(self, **kwargs):
                calls.append(("label", kwargs.get("text", "")))

            def prop(self, owner, name, **kwargs):
                calls.append(("prop", name, kwargs))

            def separator(self):
                calls.append(("separator",))

        node = self._node(False)
        node.draw_buttons(None, _Layout())
        prop_names = [c[1] for c in calls if c[0] == "prop"]
        self.assertIn("clean_unreferenced_textures", prop_names)
        self.assertNotIn("清理未被 ini 引用的孤儿 .dds",
                         [c[1] for c in calls if c[0] == "label"])

        calls.clear()
        node.clean_unreferenced_textures = True
        node.draw_buttons(None, _Layout())
        self.assertIn("清理未被 ini 引用的孤儿 .dds",
                      [c[1] for c in calls if c[0] == "label"])


if __name__ == "__main__":
    unittest.main()
