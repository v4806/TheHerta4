"""EFMI EntryPoint `; [mesh:...]` 聚合注释的发射门控。

该注释是拖拽交互「包含物体」组件级过滤的唯一数据源
（``blueprint/node_postprocess_draginteraction_efmi.py::_zone_component_allowed``
经 ``_locate_component`` 反解 ``comp["mesh_names"]``），也是「物体贴图替换与清理」
在合并骨架布局下唯一能拿到的物体↔段映射。没有消费方时它只是导出器内部元数据，
不该写进 ini——实测一个条目能写出 432 个物体名、20,410 字符的单行。

使用 fake-bpy 加载真实 ``ui/universal/efmi.py``（同
``tests/test_efmi_merge_active_collision.py`` 的骨架），只验证门控本身。
"""

import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_efmi_drag_mesh_comment_gate_test_pkg"


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


for pkg_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.blueprint",
                 f"{PKG}.common", f"{PKG}.utils"):
    _pkg = _install_module(pkg_name)
    _pkg.__path__ = []

# --- 轻量 fake：efmi.py 依赖的其余模块名（绝不实例化重类）---
_install_module("bpy", data=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=_load_real(f"{PKG}.utils.json_utils_", "utils/json_utils.py").JsonUtils,
)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        import_merged_vgmap=lambda: True, forbid_auto_texture_ini=lambda: False
    ),
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_real_ini_builder = _load_real(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
M_IniBuilder = _real_ini_builder.M_IniBuilder
M_IniSection = _real_ini_builder.M_IniSection
M_SectionType = _real_ini_builder.M_SectionType
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
_install_module(f"{PKG}.blueprint.model", BluePrintModel=object)
_install_module(f"{PKG}.common.submesh_model", SubMeshModel=object)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace())

_efmi = _load_real(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")
ExportEFMI = _efmi.ExportEFMI


# ---------------------------------------------------------------------------
# 蓝图替身：只造门控会读的那几层（处理链 → 节点 → zone_objects → 区域设置）
# ---------------------------------------------------------------------------

class _FakeDrawCall:
    def __init__(self, obj_name):
        self.obj_name = obj_name


class _FakeZoneSettings:
    def __init__(self, include_objects=()):
        self.include_objects = list(include_objects)


class _FakeZoneRef:
    def __init__(self, settings):
        if settings is None:
            self.zone_object = None
        else:
            self.zone_object = types.SimpleNamespace(ssmt_drag_zone=settings)


class _FakeDragNode:
    bl_idname = "SSMTNode_PostProcess_DragInteraction"

    def __init__(self, zone_settings=(), mute=False):
        self.zone_objects = [_FakeZoneRef(s) for s in zone_settings]
        self.mute = mute


class _FakeOtherNode:
    bl_idname = "SSMTNode_PostProcess_Glow"

    def __init__(self):
        self.mute = False


class _FakeObjectTextureNode:
    """物体贴图替换与清理：只扫 TextureOverride 段，聚合行是它唯一映射。"""

    bl_idname = "SSMTNode_PostProcess_ObjectTextureAssign"

    def __init__(self, mute=False):
        self.mute = mute


class _FakeChain:
    def __init__(self, nodes):
        self.node_path = list(nodes)


def _make_exporter(chains):
    exporter = object.__new__(ExportEFMI)
    exporter.blueprint_model = types.SimpleNamespace(processing_chains=list(chains))
    return exporter


def _make_submesh(*names):
    return types.SimpleNamespace(
        drawcall_model_list=[_FakeDrawCall(name) for name in names]
    )


def _emit(exporter, submesh):
    section = M_IniSection(M_SectionType.TextureOverrideIB)
    exporter._append_entrypoint_mesh_comment(section, submesh)
    return section.SectionLineList


class DragMeshCommentGateTests(unittest.TestCase):

    def test_no_consumer_node_omits_comment(self):
        """蓝图里没有任何消费方节点 → 不写聚合注释（哪怕组件里有多个物体）。"""
        exporter = _make_exporter([_FakeChain([_FakeOtherNode()])])
        self.assertEqual(_emit(exporter, _make_submesh("A", "B", "C")), [])

    def test_empty_chains_omits_comment(self):
        exporter = _make_exporter([])
        self.assertEqual(_emit(exporter, _make_submesh("A")), [])

    def test_drag_node_without_include_list_omits_comment(self):
        """节点在、但没有任何区域配「包含物体」→ 消费侧不会用到，不写。"""
        node = _FakeDragNode([_FakeZoneSettings(), _FakeZoneSettings([])])
        exporter = _make_exporter([_FakeChain([node])])
        self.assertEqual(_emit(exporter, _make_submesh("A", "B")), [])

    def test_drag_node_with_include_list_emits_sorted_comment(self):
        """区域配了包含物体 → 发射去重排序后的聚合行（消费侧的输入契约）。"""
        node = _FakeDragNode([
            _FakeZoneSettings(),
            _FakeZoneSettings([types.SimpleNamespace(object=object())]),
        ])
        exporter = _make_exporter([_FakeChain([_FakeOtherNode(), node])])
        lines = _emit(exporter, _make_submesh("Zeta", "Alpha", "Alpha"))
        self.assertEqual(lines, ["; [mesh:Alpha,Zeta]"])

    def test_muted_drag_node_omits_comment(self):
        """静音的拖拽节点不参与导出链 → 不写。"""
        node = _FakeDragNode(
            [_FakeZoneSettings([types.SimpleNamespace(object=object())])],
            mute=True,
        )
        exporter = _make_exporter([_FakeChain([node])])
        self.assertEqual(_emit(exporter, _make_submesh("A")), [])

    def test_object_texture_node_emits_comment(self):
        """物体贴图替换与清理在场 → 发射（合并骨架下它是唯一物体↔段映射）。"""
        exporter = _make_exporter([_FakeChain([_FakeOtherNode(), _FakeObjectTextureNode()])])
        self.assertEqual(_emit(exporter, _make_submesh("B", "A")), ["; [mesh:A,B]"])

    def test_muted_object_texture_node_omits_comment(self):
        exporter = _make_exporter([_FakeChain([_FakeObjectTextureNode(mute=True)])])
        self.assertEqual(_emit(exporter, _make_submesh("A")), [])

    def test_material_node_alone_omits_comment(self):
        """材质转资源走 Callback_Component_DrawCustom 跟到 CommandList，
        不读聚合行（_material_target_section_names 文档明示）→ 不写。"""
        material_node = _FakeOtherNode()
        material_node.bl_idname = "SSMTNode_PostProcess_Material"
        exporter = _make_exporter([_FakeChain([material_node])])
        self.assertEqual(_emit(exporter, _make_submesh("A")), [])

    def test_zone_without_settings_omits_comment(self):
        """区域空物体指针悬空（settings 取不到）→ 按「无包含列表」处理，不写。"""
        node = _FakeDragNode([None])
        exporter = _make_exporter([_FakeChain([node])])
        self.assertEqual(_emit(exporter, _make_submesh("A")), [])

    def test_submesh_without_names_omits_comment(self):
        """门控为真但组件没有物体名 → 不写空行。"""
        node = _FakeDragNode(
            [_FakeZoneSettings([types.SimpleNamespace(object=object())])]
        )
        exporter = _make_exporter([_FakeChain([node])])
        self.assertEqual(_emit(exporter, _make_submesh("", None)), [])

    def test_gate_cached_after_first_evaluation(self):
        """门控按导出缓存一次：逐组件重扫处理链没有意义。"""
        exporter = _make_exporter([_FakeChain([_FakeOtherNode()])])
        self.assertFalse(exporter._entrypoint_mesh_comment_enabled())
        # 首次求值后蓝图再出现消费方也不重算（同一导出的稳定输入）
        exporter.blueprint_model.processing_chains.append(
            _FakeChain([_FakeDragNode([_FakeSettingsWithInclude()])])
        )
        self.assertFalse(exporter._entrypoint_mesh_comment_enabled())

    def test_generate_ini_file_emits_via_gated_helper(self):
        """回归护栏：EntryPoint 注释必须走门控助手，不能再内联无条件发射。"""
        source = inspect.getsource(ExportEFMI.generate_ini_file)
        self.assertIn("self._append_entrypoint_mesh_comment(", source)
        self.assertNotIn("_ep_mesh_names", source)


class _FakeSettingsWithInclude(_FakeZoneSettings):
    def __init__(self):
        super().__init__([types.SimpleNamespace(object=object())])


if __name__ == "__main__":
    unittest.main()
