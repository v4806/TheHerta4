"""PR#45 §B3：形态键后处理的段登记 / 蓝图模型消费**只**在直出模式生效。

用户明确要求：普通 / 后台 / 并行导出的形态键行为必须与本次改动之前完全一致，
只有直出（``blueprint/direct_export.py`` 的 ``execute_direct_export``）才登记形态键段、
才消费 ``get_current_blueprint_model()``。判定还必须 fail-safe：摸不到开关时按
「不是直出」处理，即不登记、不消费。

本文件装载**真实**的 ``blueprint/export_helper.py``（运行时开关的真身）与
**真实**的 ``blueprint/node_postprocess_shapekey.py``（门控的真身），只把与判定无关的
重依赖（bpy / 节点基类 / 前处理）用桩替掉。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import common.export_section_registry as _registry

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_sk_section_gate_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__package__ = name
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _install_module(_package_name)

_install_module(
    "bpy",
    types=types.SimpleNamespace(
        PropertyGroup=object, Operator=object, UIList=object, Node=object
    ),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kw: None,
        BoolProperty=lambda **_kw: None,
        IntProperty=lambda **_kw: None,
        FloatProperty=lambda **_kw: None,
        EnumProperty=lambda **_kw: None,
        CollectionProperty=lambda **_kw: None,
        PointerProperty=lambda **_kw: None,
    ),
    data=types.SimpleNamespace(
        objects={}, texts=[], node_groups=types.SimpleNamespace(nodes=[])
    ),
)
_install_module(
    "bpy.props",
    StringProperty=lambda **_kw: None,
    BoolProperty=lambda **_kw: None,
    IntProperty=lambda **_kw: None,
    FloatProperty=lambda **_kw: None,
    EnumProperty=lambda **_kw: None,
    CollectionProperty=lambda **_kw: None,
    PointerProperty=lambda **_kw: None,
)
_install_module(
    "bpy.types",
    PropertyGroup=object,
    Operator=object,
    UIList=object,
    Node=object,
    NodeSocket=object,
)
_install_module(
    "bpy.data", objects={}, texts=[], node_groups=types.SimpleNamespace(nodes=[])
)

# 真实 export_helper 只需要这几个叶子依赖（与 tests/test_zzmi_t80_blockers.py 同构）。
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="", path_generate_mod_folder=lambda: ""),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.m_key", M_Key=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        resolve_source_object_name=lambda name: str(name or "").strip()
    ),
)
_install_module(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=types.SimpleNamespace())

# 登记表的真身：直接复用测试进程里已导入的 common.export_section_registry，
# 这样 node 模块里的 ``_section_registry`` 与被断言的模块是同一个对象
#（tests/_real_modules.py 的 spec_from_file_location 会造出第二个实例，登记表恒空）。
sys.modules[f"{PKG}.common.export_section_registry"] = _registry
setattr(sys.modules[f"{PKG}.common"], "export_section_registry", _registry)


def _load(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


# 真实叶子模块按假包前缀登记（只有 os/shutil/tempfile 依赖，直接装真身）。
_load(f"{PKG}.common.safe_write", "common/safe_write.py")
_load(f"{PKG}.blueprint.shapekey_sparse_index", "blueprint/shapekey_sparse_index.py")

_helper_module = _load(f"{PKG}.blueprint.export_helper", "blueprint/export_helper.py")
BlueprintExportHelper = _helper_module.BlueprintExportHelper

_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)
_install_module(
    f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda self, ctx: None
)
_install_module(f"{PKG}.blueprint.deform_chain")
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda name, **_kw: f"Freq_{name}",
    mark_variable_name_used=lambda *_a, **_kw: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
    cjk_to_ascii=lambda value: str(value or ""),
    is_pinyin_available=lambda **_kwargs: False,
    reset_pinyin_cache=lambda *_args, **_kwargs: None,
    shape_key_base_variable_name=lambda name: f"Freq_{name}",
    get_referenced_variable_names=lambda *_args, **_kwargs: set(),
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_a, **_kw: {},
    derive_shapekey_base_resource_name=lambda *a: "",
    derive_shapekey_freq_resource_name=lambda *a: "",
    derive_shapekey_merged_data_resource_name=lambda *a: "",
    derive_shapekey_merged_map_resource_name=lambda *a: "",
    derive_shapekey_slot_map_resource_name=lambda *a: "",
    derive_shapekey_slot_resource_name=lambda *a: "",
    derive_shapekey_vertex_entry_start_resource_name=lambda *a: "",
    derive_shapekey_vertex_entry_packed_resource_name=lambda *a: "",
    derive_shapekey_vertex_entry_freq_resource_name=lambda *a: "",
    derive_shapekey_weight_resource_name=lambda *a: "ResourceWeightStub",
    ensure_resource_alias_section=lambda *a, **_kw: "",
    resolve_hash_buffer_candidate=lambda *a, **_kw: None,
)

_node_module = _load(
    f"{PKG}.blueprint.node_postprocess_shapekey",
    "blueprint/node_postprocess_shapekey.py",
)
_ShapeKeyNode = _node_module.SSMTNode_PostProcess_ShapeKey


def _fake_model(chain_names):
    chains = [
        types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            original_object_name=name,
            object_name="",
            virtual_object_name="",
            export_object_name_override="",
            rename_history=[],
        )
        for name in chain_names
    ]
    return types.SimpleNamespace(processing_chains=chains)


class DirectExportGateTests(unittest.TestCase):
    def setUp(self):
        _registry.clear()
        BlueprintExportHelper.clear_direct_export_active()
        BlueprintExportHelper.clear_current_blueprint_model()

    def tearDown(self):
        _registry.clear()
        BlueprintExportHelper.clear_direct_export_active()
        BlueprintExportHelper.clear_current_blueprint_model()

    def test_sections_are_not_registered_outside_direct_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "mod.ini"
            registered = _node_module._register_direct_export_sections(
                ("CustomShader_h_Anim", "CustomShaderShapeKeyWeightSync_h"),
                draw_ib="100",
                ini_path=str(ini_path),
            )
        self.assertEqual(registered, 0)
        self.assertEqual(_registry.get_all(), {})

    def test_sections_are_registered_during_direct_export(self):
        BlueprintExportHelper.set_direct_export_active(True)
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "mod.ini"
            registered = _node_module._register_direct_export_sections(
                ("CustomShader_h_Anim", "CustomShaderShapeKeyWeightSync_h"),
                draw_ib="100",
                ini_path=str(ini_path),
            )
            # 同一批段重复登记必须幂等
            _node_module._register_direct_export_sections(
                ("CustomShader_h_Anim", "CustomShaderShapeKeyWeightSync_h"),
                draw_ib="100",
                ini_path=str(ini_path),
            )
            self.assertEqual(registered, 2)
            entries = _registry.get_sections_for_draw_ib("100")
            self.assertEqual(
                [entry["name"] for entry in entries],
                ["CustomShader_h_Anim", "CustomShaderShapeKeyWeightSync_h"],
            )
            self.assertEqual(
                _registry.get_ini_paths(),
                [os.path.normpath(os.path.abspath(str(ini_path)))],
            )

    def test_blueprint_model_is_consumed_only_during_direct_export(self):
        BlueprintExportHelper.set_current_blueprint_model(_fake_model(["Body"]))
        node = types.SimpleNamespace(id_data=None)
        with mock.patch.object(
            BlueprintExportHelper,
            "collect_connected_start_nodes",
            staticmethod(lambda tree: []),
        ):
            self.assertEqual(
                list(_ShapeKeyNode._iter_connected_source_object_names(node)), []
            )
            BlueprintExportHelper.set_direct_export_active(True)
            self.assertEqual(
                list(_ShapeKeyNode._iter_connected_source_object_names(node)), ["Body"]
            )

    def test_gate_fails_safe_without_switch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "mod.ini"

            class _NoSwitchHelper:
                pass

            class _ExplodingHelper:
                @staticmethod
                def is_direct_export_active():
                    raise RuntimeError("boom")

            for helper in (_NoSwitchHelper, _ExplodingHelper):
                with mock.patch.object(_helper_module, "BlueprintExportHelper", helper):
                    self.assertFalse(_node_module._direct_export_runtime_active())
                    self.assertEqual(
                        _node_module._register_direct_export_sections(
                            ("CustomShader_h_Anim",), draw_ib="100", ini_path=str(ini_path)
                        ),
                        0,
                    )
            self.assertEqual(_registry.get_all(), {})

    def test_reset_runtime_state_clears_direct_export_switch(self):
        BlueprintExportHelper.set_direct_export_active(True)
        self.assertTrue(_node_module._direct_export_runtime_active())

        BlueprintExportHelper.reset_direct_export_runtime_state()

        self.assertFalse(BlueprintExportHelper.is_direct_export_active())
        self.assertFalse(_node_module._direct_export_runtime_active())
