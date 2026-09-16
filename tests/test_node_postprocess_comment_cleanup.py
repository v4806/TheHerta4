# -*- coding: utf-8 -*-
"""配置文件清理节点（blueprint/node_postprocess_comment_cleanup.py）测试。

覆盖 PR 新增的「原地清理已导出 Mod」能力：`_in_place=True` + `_ini_path`
只清理指定 INI，以及默认导出路径下的发现逻辑与返回值语义。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_comment_cleanup_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


class _Operator:
    def report(self, _kind, _message):
        return None


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=_Operator),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: "",
        BoolProperty=lambda **kwargs: kwargs.get("default", False),
    ),
    utils=types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    ),
)
sys.modules["bpy"] = _fake_bpy


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakePostProcessBase:
    bl_idname = ""


class _FakeMaterialBase:
    @staticmethod
    def _replace_non_ascii_runs(text):
        """与生产实现同形的极简替身：把连续非 ASCII 段折叠成 ASCII 占位。"""
        import re as _re
        return _re.sub(r"[^\x00-\x7f]+", lambda _m: "_", str(text or ""))


sys.modules[f"{PKG}.blueprint.node_postprocess_base"] = _install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=_FakePostProcessBase,
)
sys.modules[f"{PKG}.blueprint.node_postprocess_material"] = _install_module(
    f"{PKG}.blueprint.node_postprocess_material",
    SSMTNode_PostProcess_MaterialBase=_FakeMaterialBase,
)

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_comment_cleanup",
    REPO_ROOT / "blueprint" / "node_postprocess_comment_cleanup.py",
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def _make_node(**attrs):
    node = module.SSMTNode_PostProcess_CommentCleanup()
    node.ini_file_path = attrs.get("ini_file_path", "")
    node.last_mod_ini_path = attrs.get("last_mod_ini_path", "")
    return node


class CommentCleanupClassTests(unittest.TestCase):
    def test_operator_is_registered_with_node(self):
        self.assertIn(module.SSMT_OT_CommentCleanup_Refresh, module.classes)
        self.assertIn(module.SSMTNode_PostProcess_CommentCleanup, module.classes)


class CleanIniFileTests(unittest.TestCase):
    def test_rewrites_non_ascii_and_counts_source_chars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            with open(ini_path, "w", encoding="utf-8") as handle:
                handle.write("; 中文注释A\n[TextureOverride]\nhash = 1\n")

            changed, replaced = module.SSMTNode_PostProcess_CommentCleanup._clean_ini_file(ini_path)

            self.assertTrue(changed)
            self.assertEqual(replaced, 4, "统计的是源文本里的非 ASCII 字符数（中文注释）")
            with open(ini_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _A\n[TextureOverride]\nhash = 1\n")

    def test_ascii_only_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            content = "[TextureOverride]\nhash = 1\n"
            with open(ini_path, "w", encoding="utf-8") as handle:
                handle.write(content)

            changed, replaced = module.SSMTNode_PostProcess_CommentCleanup._clean_ini_file(ini_path)

            self.assertFalse(changed)
            self.assertEqual(replaced, 0)
            with open(ini_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), content)

    def test_missing_file_does_not_raise(self):
        changed, replaced = module.SSMTNode_PostProcess_CommentCleanup._clean_ini_file(
            os.path.join(tempfile.gettempdir(), "definitely_missing_pr15.ini"))
        self.assertFalse(changed)
        self.assertEqual(replaced, 0)


class InPlaceExecuteTests(unittest.TestCase):
    def test_in_place_only_touches_the_target_ini(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "target.ini")
            sibling = os.path.join(temp_dir, "sibling.ini")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("; 目标中文\n")
            with open(sibling, "w", encoding="utf-8") as handle:
                handle.write("; 邻居中文\n")

            node = _make_node()
            ok = node.execute_postprocess(temp_dir, _in_place=True, _ini_path=target)

            self.assertIs(ok, True)
            self.assertEqual(node.last_mod_ini_path, os.path.abspath(target))
            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _\n")
            with open(sibling, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; 邻居中文\n")

    def test_in_place_falls_back_to_ini_file_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "picked.ini")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("中文\n")

            node = _make_node(ini_file_path=target)
            self.assertIs(node.execute_postprocess(temp_dir, _in_place=True), True)
            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "_\n")

    def test_in_place_missing_target_returns_false(self):
        node = _make_node()
        missing = os.path.join(tempfile.gettempdir(), "definitely_missing_pr15.ini")
        self.assertIs(node.execute_postprocess("", _in_place=True, _ini_path=missing), False)

    def test_export_path_uses_backward_compatible_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            nested = os.path.join(temp_dir, "sub")
            os.makedirs(nested)
            root_ini = os.path.join(temp_dir, "root.ini")
            nested_ini = os.path.join(nested, "nested.ini")
            with open(root_ini, "w", encoding="utf-8") as handle:
                handle.write("; 根中文\n")
            with open(nested_ini, "w", encoding="utf-8") as handle:
                handle.write("; 子中文\n")

            node = _make_node()
            # 旧调用形态：只传 mod_export_path
            self.assertIs(node.execute_postprocess(temp_dir), True)
            self.assertEqual(node.last_mod_ini_path, os.path.abspath(root_ini))
            with open(root_ini, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _\n")
            with open(nested_ini, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _\n")

    def test_export_path_without_ini_returns_true(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            node = _make_node()
            self.assertIs(node.execute_postprocess(temp_dir), True)
            self.assertEqual(node.last_mod_ini_path, "")


if __name__ == "__main__":
    unittest.main()
