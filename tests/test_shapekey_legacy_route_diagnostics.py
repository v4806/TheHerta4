"""R-A 回归：标准（非直出）路线不得静默丢弃形态键。

被测链路（全部真实实现，只有 bpy / 依赖叶子件是 stub）：

    BlueprintExportHelper.detect_standard_route_baked_shapekey_objects
        → M_IniHelper._report_standard_route_baked_shapekeys
            → LOG.warning（stdout / 日志 sink）
            → ini [Present] 里的 `;` 注释（产物注释）

根因（review-reports/t82-shapekey-drag-retest.md §3.3 R-A）：标准前处理
`blueprint/preprocess.py::_apply_shape_keys` 先把副本上的非 Basis 键块烘焙进网格，
于是经典发射器拿到的形态键字典与载荷都为空 —— 旧实现在 `shapekeyname_mkey_dict`
为空时直接 `return`：产物零形态键、零告警。

可证伪性：本文件的用例在修复前必红（修复前 `add_shapekey_ini_sections` 静默 return，
既没有 LOG.warning，也没有任何 ini 段落）。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_shapekey_legacy_route_diag_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeObjects(dict):
    def get(self, name, default=None):
        return super().get(name, default)


class _KeyBlock:
    def __init__(self, name):
        self.name = name


class _ShapeKeyData:
    def __init__(self, *shape_key_names):
        self.key_blocks = [_KeyBlock("Basis")] + [_KeyBlock(name) for name in shape_key_names]


def _mesh_object(name, *shape_key_names):
    return types.SimpleNamespace(
        name=name,
        type="MESH",
        data=types.SimpleNamespace(shape_keys=_ShapeKeyData(*shape_key_names)),
    )


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(objects=_FakeObjects(), node_groups=[], texts=types.SimpleNamespace()),
    types=types.SimpleNamespace(Object=object),
)
_install_module("bpy", **_fake_bpy.__dict__)

_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(get_workspace_name=lambda: ""),
)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_key", M_Key=type("M_Key", (), {}))
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
    ),
)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.workspace_helper", WorkSpaceHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_helper_gui",
    M_IniHelperGUI=types.SimpleNamespace(copy_res_to_mod_folder=lambda: None),
)

# 预处理器 stub：只当作 `original_to_copy_map`（源物体名 → 副本物体名）的载体。
PREPROCESS_HELPER = types.SimpleNamespace(original_to_copy_map={})
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=PREPROCESS_HELPER)

LOG_WARNINGS = []
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda message="": LOG_WARNINGS.append(str(message)),
        debug=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    ),
)


def _load_real(module_name: str, relative_path: str):
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 真实 m_ini_builder（纯标准库）——用它断言产物里的注释行真的落地。
m_ini_builder = _load_real(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
# 真实 export_helper —— 检测器本体。
export_helper = _load_real(f"{PKG}.blueprint.export_helper", "blueprint/export_helper.py")
# 真实 m_ini_helper —— 发射器本体。
m_ini_helper = _load_real(f"{PKG}.common.m_ini_helper", "common/m_ini_helper.py")

BlueprintExportHelper = export_helper.BlueprintExportHelper
M_IniHelper = m_ini_helper.M_IniHelper
M_IniBuilder = m_ini_builder.M_IniBuilder
M_SectionType = m_ini_builder.M_SectionType

DIAGNOSTIC_MARKER = "标准（非直出）路线不导出形态键"
INI_NOTICE_MARKER = "; --- SSMT 形态键导出诊断"


class _EmptyShapeKeyDict:
    """临时把 `get_current_shapekeyname_mkey_dict` 变成返回空字典。"""

    def __enter__(self):
        self._original = BlueprintExportHelper.__dict__.get("get_current_shapekeyname_mkey_dict")
        BlueprintExportHelper.get_current_shapekeyname_mkey_dict = staticmethod(
            lambda context=None: {}
        )
        return self

    def __exit__(self, *_exc_info):
        if self._original is None:
            delattr(BlueprintExportHelper, "get_current_shapekeyname_mkey_dict")
        else:
            BlueprintExportHelper.get_current_shapekeyname_mkey_dict = self._original
        return False


class ShapeKeyBakedObjectDetectorTests(unittest.TestCase):
    """检测器本体：源物体有形态键、副本一个都不剩 ⇒ 上报。"""

    def setUp(self):
        _fake_bpy.data.objects.clear()
        PREPROCESS_HELPER.original_to_copy_map = {}

    def test_reports_source_object_whose_copy_lost_all_shape_keys(self):
        _fake_bpy.data.objects["Body"] = _mesh_object("Body", "blink")
        _fake_bpy.data.objects["Body_copy"] = _mesh_object("Body_copy")

        detected = BlueprintExportHelper.detect_standard_route_baked_shapekey_objects(
            {"Body": "Body_copy"}
        )

        self.assertEqual(detected, ["Body"])

    def test_ignores_copy_that_still_has_shape_keys(self):
        _fake_bpy.data.objects["Body"] = _mesh_object("Body", "blink")
        _fake_bpy.data.objects["Body_copy"] = _mesh_object("Body_copy", "blink")

        self.assertEqual(
            BlueprintExportHelper.detect_standard_route_baked_shapekey_objects(
                {"Body": "Body_copy"}
            ),
            [],
        )

    def test_ignores_source_object_without_exportable_shape_keys(self):
        _fake_bpy.data.objects["Body"] = _mesh_object("Body")
        _fake_bpy.data.objects["Body_copy"] = _mesh_object("Body_copy")

        self.assertEqual(
            BlueprintExportHelper.detect_standard_route_baked_shapekey_objects(
                {"Body": "Body_copy"}
            ),
            [],
        )

    def test_ignores_missing_copy_instead_of_guessing(self):
        _fake_bpy.data.objects["Body"] = _mesh_object("Body", "blink")

        self.assertEqual(
            BlueprintExportHelper.detect_standard_route_baked_shapekey_objects(
                {"Body": "Body_copy"}
            ),
            [],
        )

    def test_reads_preprocess_map_when_called_without_arguments(self):
        _fake_bpy.data.objects["Body"] = _mesh_object("Body", "blink")
        _fake_bpy.data.objects["Body_copy"] = _mesh_object("Body_copy")
        PREPROCESS_HELPER.original_to_copy_map = {"Body": "Body_copy"}

        self.assertEqual(
            BlueprintExportHelper.detect_standard_route_baked_shapekey_objects(),
            ["Body"],
        )

    def test_count_exportable_shapekey_blocks_excludes_basis(self):
        self.assertEqual(
            BlueprintExportHelper.count_exportable_shapekey_blocks(
                _mesh_object("Body", "blink", "smile")
            ),
            2,
        )
        self.assertEqual(
            BlueprintExportHelper.count_exportable_shapekey_blocks(_mesh_object("Body")),
            0,
        )
        self.assertEqual(BlueprintExportHelper.count_exportable_shapekey_blocks(None), 0)


class StandardRouteNotSilentTests(unittest.TestCase):
    """R-A 核心：标准路线丢了形态键时，必须同时落 stdoutsink 与产物注释。"""

    def setUp(self):
        LOG_WARNINGS.clear()
        _fake_bpy.data.objects.clear()
        PREPROCESS_HELPER.original_to_copy_map = {}
        BlueprintExportHelper.set_suppress_shapekey_resource_export(False)

    def tearDown(self):
        LOG_WARNINGS.clear()
        _fake_bpy.data.objects.clear()
        PREPROCESS_HELPER.original_to_copy_map = {}
        BlueprintExportHelper.set_suppress_shapekey_resource_export(False)

    def _arrange_baked_shape_keys(self):
        _fake_bpy.data.objects["Body"] = _mesh_object("Body", "blink")
        _fake_bpy.data.objects["Body_copy"] = _mesh_object("Body_copy")
        PREPROCESS_HELPER.original_to_copy_map = {"Body": "Body_copy"}

    def test_warns_and_writes_ini_notice_when_keys_were_baked_away(self):
        self._arrange_baked_shape_keys()
        ini_builder = M_IniBuilder()

        with _EmptyShapeKeyDict():
            M_IniHelper.add_shapekey_ini_sections(
                ini_builder=ini_builder, drawib_drawibmodel_dict={}
            )

        self.assertEqual(len(LOG_WARNINGS), 1, LOG_WARNINGS)
        self.assertIn(DIAGNOSTIC_MARKER, LOG_WARNINGS[0])
        self.assertIn("Body", LOG_WARNINGS[0])
        self.assertIn("direct_export_mode", LOG_WARNINGS[0])

        notice_lines = [
            line
            for section in ini_builder.ini_section_list
            if section.SectionType == M_SectionType.Present
            for line in section.SectionLineList
        ]
        self.assertTrue(
            any(INI_NOTICE_MARKER in line for line in notice_lines),
            notice_lines,
        )

    def test_saved_ini_contains_the_notice_comment(self):
        self._arrange_baked_shape_keys()
        ini_builder = M_IniBuilder()

        with _EmptyShapeKeyDict():
            M_IniHelper.add_shapekey_ini_sections(
                ini_builder=ini_builder, drawib_drawibmodel_dict={}
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "test.ini")
            ini_builder.save_to_file(ini_path)
            with open(ini_path, "r", encoding="utf-8") as file:
                content = file.read()

        self.assertIn(INI_NOTICE_MARKER, content)
        self.assertIn(DIAGNOSTIC_MARKER, content)

    def test_direct_base_round_suppression_does_not_warn(self):
        """直出基础轮次主动抑制经典发射器 ⇒ 不得误报（否则直出每次导出都会响）。"""
        self._arrange_baked_shape_keys()
        ini_builder = M_IniBuilder()
        BlueprintExportHelper.set_suppress_shapekey_resource_export(True)

        with _EmptyShapeKeyDict():
            M_IniHelper.add_shapekey_ini_sections(
                ini_builder=ini_builder, drawib_drawibmodel_dict={}
            )

        self.assertEqual(LOG_WARNINGS, [])
        self.assertEqual(ini_builder.ini_section_list, [])

    def test_no_shape_keys_baked_anywhere_stays_silent(self):
        """没有形态键被烘焙 ⇒ 不打扰用户（不引入全量导出的噪声）。"""
        _fake_bpy.data.objects["Body"] = _mesh_object("Body")
        _fake_bpy.data.objects["Body_copy"] = _mesh_object("Body_copy")
        PREPROCESS_HELPER.original_to_copy_map = {"Body": "Body_copy"}
        ini_builder = M_IniBuilder()

        with _EmptyShapeKeyDict():
            M_IniHelper.add_shapekey_ini_sections(
                ini_builder=ini_builder, drawib_drawibmodel_dict={}
            )

        self.assertEqual(LOG_WARNINGS, [])
        self.assertEqual(ini_builder.ini_section_list, [])

    def test_non_empty_dict_with_baked_keys_still_warns(self):
        """键仍在树里（字典非空）但副本已被烘焙掉载荷 ⇒ 同样必须可见，不得只发空段。"""
        self._arrange_baked_shape_keys()
        ini_builder = M_IniBuilder()
        m_key = types.SimpleNamespace(
            key_name="$shapekey0", initialize_value=0, initialize_vk_str="", comment="blink"
        )

        with mock.patch.object(
            BlueprintExportHelper,
            "get_current_shapekeyname_mkey_dict",
            staticmethod(lambda context=None: {"blink": m_key}),
        ):
            M_IniHelper.add_shapekey_ini_sections(
                ini_builder=ini_builder, drawib_drawibmodel_dict={}
            )

        self.assertEqual(len(LOG_WARNINGS), 1, LOG_WARNINGS)
        self.assertIn(DIAGNOSTIC_MARKER, LOG_WARNINGS[0])


if __name__ == "__main__":
    unittest.main()
