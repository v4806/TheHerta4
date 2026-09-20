# -*- coding: utf-8 -*-
"""配置文件清理节点（blueprint/node_postprocess_comment_cleanup.py）测试。

覆盖「原地清理已导出 Mod」能力：`_in_place=True` + `_ini_path` 只清理指定 INI、
默认导出路径下的发现逻辑与返回值语义，以及**目标配置表从 Generate Mod 输出目录
自动定位**（无需用户手填路径）的消歧规则。
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
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
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


class _FakeGlobalConfig:
    """GlobalConfig 替身：只保留本节点用到的三个入口，值可被测试改写。"""

    workspace_name = ""
    mod_export_path = ""

    @classmethod
    def read_from_main_json_ssmt4(cls):
        return None

    @classmethod
    def get_workspace_name(cls):
        return cls.workspace_name

    @classmethod
    def path_generate_mod_folder(cls):
        return cls.mod_export_path


def _find_config_table_files(mod_export_path):
    """与 common/config_table_backup.find_config_table_files 同形：只认根目录 *.ini。"""
    if not mod_export_path or not os.path.isdir(mod_export_path):
        return []
    return sorted(
        os.path.join(mod_export_path, name)
        for name in os.listdir(mod_export_path)
        if os.path.isfile(os.path.join(mod_export_path, name))
        and name.lower().endswith(".ini")
    )


sys.modules[f"{PKG}.blueprint.node_postprocess_base"] = _install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=_FakePostProcessBase,
)
sys.modules[f"{PKG}.blueprint.node_postprocess_material"] = _install_module(
    f"{PKG}.blueprint.node_postprocess_material",
    SSMTNode_PostProcess_MaterialBase=_FakeMaterialBase,
)
sys.modules[f"{PKG}.common.config_table_backup"] = _install_module(
    f"{PKG}.common.config_table_backup",
    find_config_table_files=_find_config_table_files,
)
sys.modules[f"{PKG}.common.global_config"] = _install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=_FakeGlobalConfig,
)

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_comment_cleanup",
    REPO_ROOT / "blueprint" / "node_postprocess_comment_cleanup.py",
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def _make_node(**attrs):
    node = module.SSMTNode_PostProcess_CommentCleanup()
    node.name = attrs.get("name", "cleanup")
    node.ini_file_path = attrs.get("ini_file_path", "")
    node.last_mod_ini_path = attrs.get("last_mod_ini_path", "")
    return node


def _write_ini(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class _GlobalConfigGuard(unittest.TestCase):
    def setUp(self):
        self._workspace_name = _FakeGlobalConfig.workspace_name
        self._mod_export_path = _FakeGlobalConfig.mod_export_path

    def tearDown(self):
        _FakeGlobalConfig.workspace_name = self._workspace_name
        _FakeGlobalConfig.mod_export_path = self._mod_export_path


class CommentCleanupClassTests(unittest.TestCase):
    def test_operator_is_registered_with_node(self):
        self.assertIn(module.SSMT_OT_CommentCleanup_Refresh, module.classes)
        self.assertIn(module.SSMTNode_PostProcess_CommentCleanup, module.classes)


class CleanIniFileTests(unittest.TestCase):
    def test_rewrites_non_ascii_and_counts_source_chars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            _write_ini(ini_path, "; 中文注释A\n[TextureOverride]\nhash = 1\n")

            changed, replaced = module.SSMTNode_PostProcess_CommentCleanup._clean_ini_file(ini_path)

            self.assertTrue(changed)
            self.assertEqual(replaced, 4, "统计的是源文本里的非 ASCII 字符数（中文注释）")
            with open(ini_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _A\n[TextureOverride]\nhash = 1\n")

    def test_ascii_only_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            content = "[TextureOverride]\nhash = 1\n"
            _write_ini(ini_path, content)

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
            _write_ini(target, "; 目标中文\n")
            _write_ini(sibling, "; 邻居中文\n")

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
            _write_ini(target, "中文\n")

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
            _write_ini(root_ini, "; 根中文\n")
            _write_ini(nested_ini, "; 子中文\n")

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


class FindTargetIniFileTests(_GlobalConfigGuard):
    """导出目录 → 配置表 的自动定位（用户不再需要手填路径）。"""

    def test_single_ini_is_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "Whatever.ini")
            _write_ini(ini_path, "x")

            target, error = module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(temp_dir)

            self.assertEqual(target, ini_path)
            self.assertEqual(error, "")

    def test_workspace_named_ini_wins_among_many(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _FakeGlobalConfig.workspace_name = "MyMod"
            expected = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(expected, "x")
            _write_ini(os.path.join(temp_dir, "Other.ini"), "x")

            target, error = module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(temp_dir)

            self.assertEqual(target, expected)
            self.assertEqual(error, "")

    def test_workspace_prefixed_ini_wins_among_many(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _FakeGlobalConfig.workspace_name = "MyMod"
            expected = os.path.join(temp_dir, "MyMod_extra.ini")
            _write_ini(expected, "x")
            _write_ini(os.path.join(temp_dir, "Other.ini"), "x")

            target, error = module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(temp_dir)

            self.assertEqual(target, expected)
            self.assertEqual(error, "")

    def test_ambiguous_workspace_matches_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _FakeGlobalConfig.workspace_name = "MyMod"
            _write_ini(os.path.join(temp_dir, "MyMod.ini"), "x")
            _write_ini(os.path.join(temp_dir, "MyMod_extra.ini"), "x")

            target, error = module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(temp_dir)

            self.assertEqual(target, "")
            self.assertIn("MyMod.ini", error)
            self.assertIn("MyMod_extra.ini", error)

    def test_nested_ini_is_not_a_config_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.makedirs(os.path.join(temp_dir, "res"))
            _write_ini(os.path.join(temp_dir, "res", "inner.ini"), "x")

            target, error = module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(temp_dir)

            self.assertEqual(target, "")
            self.assertIn("未找到配置表", error)

    def test_missing_directory_is_reported(self):
        target, error = module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(
            os.path.join(tempfile.gettempdir(), "definitely_missing_pr15_dir"))

        self.assertEqual(target, "")
        self.assertIn("导出目录不存在", error)


class ExportPathFallbackTests(_GlobalConfigGuard):
    """导出分支记录的 last_mod_ini_path 必须是配置表本体，供下次原地清理兜底。"""

    def test_workspace_config_table_is_recorded_when_directory_has_many(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _FakeGlobalConfig.workspace_name = "MyMod"
            expected = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(expected, "; 中文\n")
            _write_ini(os.path.join(temp_dir, "Other.ini"), "; 中文\n")

            node = _make_node()
            self.assertIs(node.execute_postprocess(temp_dir), True)

            self.assertEqual(node.last_mod_ini_path, os.path.abspath(expected))


class ResolveInPlaceTargetTests(_GlobalConfigGuard):
    def test_export_dir_is_used_without_manual_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _FakeGlobalConfig.workspace_name = "MyMod"
            ini_path = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(ini_path, "中文\n")

            node = _make_node()
            target, error = node.resolve_in_place_target(temp_dir)

            self.assertEqual(target, os.path.abspath(ini_path))
            self.assertEqual(error, "")

    def test_manual_path_still_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manual = os.path.join(temp_dir, "manual.ini")
            auto = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(manual, "中文\n")
            _write_ini(auto, "中文\n")

            node = _make_node(ini_file_path=manual)
            target, _error = node.resolve_in_place_target(temp_dir)

            self.assertEqual(target, os.path.abspath(manual))

    def test_stale_manual_path_falls_through_to_export_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            auto = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(auto, "中文\n")

            node = _make_node(ini_file_path=os.path.join(temp_dir, "gone.ini"))
            target, error = node.resolve_in_place_target(temp_dir)

            self.assertEqual(target, os.path.abspath(auto))
            self.assertEqual(error, "")

    def test_last_export_path_is_the_final_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            last = os.path.join(temp_dir, "last.ini")
            _write_ini(last, "中文\n")

            node = _make_node(last_mod_ini_path=last)
            target, error = node.resolve_in_place_target("")

            self.assertEqual(target, os.path.abspath(last))
            self.assertEqual(error, "")

    def test_stale_manual_path_is_named_when_nothing_else_resolves(self):
        missing_manual = os.path.join(tempfile.gettempdir(), "definitely_missing_pr15.ini")
        node = _make_node(ini_file_path=missing_manual)

        target, error = node.resolve_in_place_target("")

        self.assertEqual(target, "")
        self.assertIn(missing_manual, error)

    def test_stale_manual_path_still_reports_ambiguous_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _FakeGlobalConfig.workspace_name = "MyMod"
            _write_ini(os.path.join(temp_dir, "MyMod.ini"), "x")
            _write_ini(os.path.join(temp_dir, "MyMod_extra.ini"), "x")

            node = _make_node(ini_file_path=os.path.join(temp_dir, "gone.ini"))
            target, error = node.resolve_in_place_target(temp_dir)

            self.assertEqual(target, "")
            self.assertIn("gone.ini", error)
            self.assertIn("MyMod_extra.ini", error, "过期手填路径不能吞掉「候选多个」这一更有用的信息")

    def test_in_place_without_ini_path_uses_export_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(ini_path, "; 中文\n")

            node = _make_node()
            self.assertIs(node.execute_postprocess(temp_dir, _in_place=True), True)
            self.assertEqual(node.last_mod_ini_path, os.path.abspath(ini_path))
            with open(ini_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _\n")


class OperatorTests(_GlobalConfigGuard):
    """按钮「应用清理到Mod（原地更新）」：不填路径也要能命中输出目录里的配置表。"""

    def _run_operator(self, node, mod_export_path):
        _FakeGlobalConfig.mod_export_path = mod_export_path
        op = module.SSMT_OT_CommentCleanup_Refresh()
        op.node_name = node.name
        reports = []
        op.report = lambda kind, message: reports.append((kind, message))
        context = types.SimpleNamespace(
            space_data=types.SimpleNamespace(
                type='NODE_EDITOR',
                edit_tree=types.SimpleNamespace(nodes={node.name: node}),
            )
        )
        return op.execute(context), reports

    def test_button_cleans_config_table_from_export_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "MyMod.ini")
            _write_ini(ini_path, "; 中文注释\n")

            result, reports = self._run_operator(_make_node(), temp_dir)

            self.assertEqual(result, {'FINISHED'})
            self.assertEqual([kind for kind, _ in reports], [{'INFO'}])
            self.assertIn("MyMod.ini", reports[0][1])
            with open(ini_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "; _\n")

    def test_button_warns_when_export_dir_has_no_config_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result, reports = self._run_operator(_make_node(), temp_dir)

            self.assertEqual(result, {'CANCELLED'})
            self.assertEqual([kind for kind, _ in reports], [{'WARNING'}])
            self.assertIn("未找到配置表", reports[0][1])


if __name__ == "__main__":
    unittest.main()
