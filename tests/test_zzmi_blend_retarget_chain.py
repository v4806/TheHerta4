"""C-2 覆盖：Blend 布局「重打包」整链（最小且可证伪）。

覆盖分支（`ui/universal/zzmi.py`，**符号名引用**——行号会随改动漂移，本文件不再写数字）：
- `_drawib_category_layout`（`_drawib_blend_layout` 委托到它）元素列表 → {"stride", "elements"}
- `_blend_layout_key`           元素级签名（含 BLENDINDICES UINT/SINT→INT 归并）+ stride 回退
- `_merged_group_layout_catalog` 只统计有 deform pass 的部件，{键: {count, layout}}
- `_drawib_exported_blend_bytes` 载体导出 Blend 字节
- `_build_redirect_blend_retarget`  只允许无损加宽：
    · 源布局元素在锚点布局里找不到同 (semantic, index) 通道 → 拒绝（候选淘汰）
    · **源元素 byte_width > 锚点同通道 byte_width（降宽）→ 拒绝（候选淘汰）**
    · 逐元素 (semantic, index) 映射；锚点有源没有的通道 → 记 `(None, anchor)` 补 0
    · `stride_anchor`（= 锚点 `layout["stride"]`）决定行宽；每行按元素 `offset` 拷贝
- 候选淘汰与锚点选择 `_build_merged_mesh_redirect_plan`
    · 任载体重打包返回 None ⇒ `usable = False`（该布局键被淘汰）
    · `anchor_layout_key = max(candidates, key=(count, _blend_layout_width(key)))`
- `_write_redirect_blend_resources`  写 Meshes/*.buf + 返回 [(资源名, stride, 文件名)]

开关前提：该链在 `zzmi_merged_redirect_enabled` 之下（`_export_impl` 里 `_zzmi_prop_flag`
为假时清空计划）⇒ 本文件 `setUp` **显式把开关置 True**，并有一条用例断言开关门控读得到它。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests import _real_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_blend_retarget_chain_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (
    PKG,
    f"{PKG}.ui",
    f"{PKG}.ui.universal",
    f"{PKG}.common",
    f"{PKG}.utils",
):
    package = _install_module(package_name)
    package.__path__ = []

_real_modules.register_real_common_modules(f"{PKG}.common")


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


class _FakeObject:
    """最小 Blender 对象替身：支持 ``obj["键"]`` 读写（合并工具的账本用）。"""

    def __init__(self, name, object_data=None):
        self.name = name
        self.data = object_data
        self.type = "MESH"
        self._custom = {}

    def __setitem__(self, key, value):
        self._custom[key] = value

    def __getitem__(self, key):
        return self._custom[key]

    def get(self, key, default=None):
        return self._custom.get(key, default)


class _FakeObjectRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name, object_data=None):
        obj = _FakeObject(name=name, object_data=object_data)
        self._items[name] = obj
        return obj

    def get(self, name):
        return self._items.get(str(name))

    def remove(self, obj, do_unlink=False):
        self._items.pop(getattr(obj, "name", ""), None)

    def __iter__(self):
        return iter(list(self._items.values()))


class _FakeMeshRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name=""):
        mesh = types.SimpleNamespace(name=name, vertices=[], loops=[], polygons=[])
        self._items[name] = mesh
        return mesh

    def remove(self, mesh):
        self._items.pop(getattr(mesh, "name", ""), None)


_FAKE_BPY_DATA = types.SimpleNamespace(
    objects=_FakeObjectRegistry(), meshes=_FakeMeshRegistry()
)
_install_module(
    "bpy",
    data=_FAKE_BPY_DATA,
    context=types.SimpleNamespace(
        collection=types.SimpleNamespace(
            objects=types.SimpleNamespace(link=lambda _obj: None)
        )
    ),
)

_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(
    f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py"
)
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")

_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_FAKE_MOD_FOLDER = tempfile.mkdtemp(prefix="zzmi_retarget_chain_")
_FAKE_GLOBAL_CONFIG = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: _FAKE_MOD_FOLDER,
    get_workspace_name=lambda: "ZzmiRetargetChainTest",
    path_workspace_folder=lambda: "",
)
_FAKE_GLOBAL_PROPERTIES = types.SimpleNamespace(
    import_merged_vgmap=lambda: True,
    forbid_auto_texture_ini=lambda: False,
    zzz_use_slot_fix=lambda: False,
    zzmi_merged_redirect_enabled=lambda: True,  # 显式开启实验开关
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=_FAKE_GLOBAL_CONFIG)
_install_module(
    f"{PKG}.common.global_properties", GlobalProterties=_FAKE_GLOBAL_PROPERTIES
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=type("M_IniBuilder", (), {}),
    M_IniSection=type("M_IniSection", (), {}),
    M_SectionType=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace()
)
_install_module(
    f"{PKG}.ui.universal.unity",
    ExportUnity=type("ExportUnity", (), {"__init__": lambda self, model: None}),
)

_module_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", _module_path)
_zzmi_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _zzmi_module
_spec.loader.exec_module(_zzmi_module)

ExportZZMI = _zzmi_module.ExportZZMI
_zzmi_prop_flag = _zzmi_module._zzmi_prop_flag


def _blend_element(semantic, index, fmt, byte_width, category="Blend", slot=""):
    return types.SimpleNamespace(
        Category=category,
        SemanticName=semantic,
        SemanticIndex=index,
        Format=fmt,
        ByteWidth=byte_width,
        ExtractSlot=slot,
    )


# --- 布局定义（与真实 dump 的两种 slot2 形态一致，见 t34 报告）-----------------
ELEMENTS_32B = (
    # e805604d 型：WEIGHTS 4×f32(16B)@0 + INDICES 4×u32(16B)@16 = stride 32
    _blend_element("BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16),
    _blend_element("BLENDINDICES", 0, "R32G32B32A32_UINT", 16),
)
ELEMENTS_16B = (
    # d8224520 型：WEIGHTS 2×f32(8B)@0 + INDICES 2×u32(8B)@8 = stride 16
    _blend_element("BLENDWEIGHTS", 0, "R32G32_FLOAT", 8),
    _blend_element("BLENDINDICES", 0, "R32G32_UINT", 8),
)

# 元素级签名（`_blend_layout_key` 口径：BLENDINDICES 的 UINT/SINT 归并为 INT）
KEY_32B = (
    "elements",
    (
        ("BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, ""),
        ("BLENDINDICES", 0, "R32G32B32A32_INT", 16, ""),
    ),
)
KEY_16B = (
    "elements",
    (
        ("BLENDWEIGHTS", 0, "R32G32_FLOAT", 8, ""),
        ("BLENDINDICES", 0, "R32G32_INT", 8, ""),
    ),
)


class _FakeGameType:
    OrderedCategoryNameList = ["Position", "Texcoord", "Blend"]
    GPU_PreSkinning = True
    CategoryDrawCategoryDict = {
        "Position": "Position",
        "Texcoord": "Texcoord",
        "Blend": "Position",
    }
    CategoryExtractSlotDict = {
        "Position": "vb0",
        "Texcoord": "vb1",
        "Blend": "vb2",
    }
    CategoryStrideDict = {"Position": 40, "Texcoord": 20, "Blend": 32}
    D3D11ElementList = None

    def __init__(self, elements, blend_stride):
        self.CategoryStrideDict = dict(_FakeGameType.CategoryStrideDict)
        self.CategoryStrideDict["Blend"] = blend_stride
        self.D3D11ElementList = list(elements)


class _FakeSubmesh:
    def __init__(self, unique_str, vg_offset=0, vg_count=0, skeleton_group=0,
                 vg_map=None, deform_draw=0, exported_vertex_count=0,
                 match_first_index=0):
        self.unique_str = unique_str
        self.match_first_index = match_first_index
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.skeleton_group = skeleton_group
        self.vg_map = vg_map if vg_map is not None else {
            local: vg_offset + local for local in range(vg_count)
        }
        self.deform_draw_index = deform_draw
        self.original_vertex_count = 0
        self.vertex_count = 0
        self.index_vertex_id_dict = (
            list(range(exported_vertex_count)) if exported_vertex_count else None
        )
        self.category_buffer_dict = {}
        self.drawcall_model_list = []


class _FakeDrawIBModel:
    def __init__(self, draw_ib, submesh_model_list, elements, blend_stride,
                 blend_bytes=None, position_bytes=None):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 4643
        self.vertex_limit_hash = "dd9c8d5e"
        self.d3d11GameType = _FakeGameType(elements, blend_stride)
        self.category_hash_dict = {
            "Position": "122883aa",
            "Texcoord": "5c0fefda",
            "Blend": "bf543990",
        }
        self.submesh_model_list = submesh_model_list
        self.category_buffer_dict = {}
        if blend_bytes is not None:
            self.category_buffer_dict["Blend"] = blend_bytes
        if position_bytes is not None:
            self.category_buffer_dict["Position"] = position_bytes
        self.match_first_index_partname_dict = {}
        self.submesh_ib_dict = {
            submesh.unique_str: b"\x00\x00\x00\x00" for submesh in submesh_model_list
        }
        self.obj_name_draw_offset = {}

    def get_submesh_texture_override_suffix(self, submesh_model):
        return submesh_model.unique_str.replace("-", "_")

    def get_submesh_ib_resource_name(self, submesh_model):
        return "Resource_" + submesh_model.unique_str.replace("-", "_") + "_Index"

    def get_submesh_texture_markup_info_list(self, submesh_model):
        return []


def _make_exporter(drawib_models):
    _FAKE_GLOBAL_PROPERTIES.import_merged_vgmap = lambda: True
    blueprint_model = types.SimpleNamespace(
        cross_ib_info_dict={},
        cross_ib_method_dict={},
        cross_ib_mapping_method={},
        has_cross_ib=False,
        cross_ib_object_names=set(),
        keyname_mkey_dict={},
        ordered_draw_obj_data_model_list=[],
    )
    exporter = ExportZZMI(blueprint_model)
    exporter.drawib_model_list = drawib_models
    return exporter


class _RetargetChainFixture(unittest.TestCase):
    """组 7：carrier 与多数派部件 Blend 布局不同，触发重打包链。"""

    GROUP = 7
    TARGET_IB = "a23aa8a3"
    CARRIER_IB = "b20f90ea"
    SIBLING_IB = "b30db54e"

    def setUp(self):
        _FAKE_BPY_DATA.objects._items.clear()
        _FAKE_BPY_DATA.meshes._items.clear()

    # --- 夹具 -----------------------------------------------------------------
    def _register_obj(self, name, bone_ids, stub=False):
        """fake 对象：顶点 i 的权重挂在顶点组 i，组名 = bone_ids[i]（全局骨骼 id）。

        `_collect_drawib_referenced_bone_ids` 按**组名**取骨骼 id 且要求
        组名是数字 ⇒ 组名必须是 str(bone_id)，不是索引。
        """
        mesh = _FAKE_BPY_DATA.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(groups=[types.SimpleNamespace(group=i, weight=1.0)])
            for i in range(len(bone_ids))
        ]
        obj = _FAKE_BPY_DATA.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = [
            types.SimpleNamespace(name=str(bone_id)) for bone_id in bone_ids
        ]
        if stub:
            obj["ZZMI_STUB"] = 1
        return obj

    def _attach_drawcalls(self, submesh, index_count=0):
        # 用真实 DrawCallModel（`common/draw_call_model.py`）：`get_blender_obj_name()` 决定对象解析
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        draw_call = dcm(obj_name=submesh.unique_str)
        draw_call.index_count = index_count
        submesh.drawcall_model_list = [draw_call]
        return submesh

    def _components(self, carrier_layout_is_16b=True):
        """组 7 三个部件：target(20) / carrier(2) / sibling(8)。"""
        return [
            {
                "draw_ib": self.TARGET_IB, "unique_str": f"LOD0.{self.TARGET_IB}-42759-0",
                "vg_offset": 79, "vg_count": 105, "skeleton_group": self.GROUP,
                "vg_map": {i: 79 + i for i in range(105)}, "deform_draw": 20,
            },
            {
                "draw_ib": self.CARRIER_IB, "unique_str": f"LOD0.{self.CARRIER_IB}-19182-0",
                "vg_offset": 184, "vg_count": 51, "skeleton_group": self.GROUP,
                "vg_map": {i: 184 + i for i in range(51)}, "deform_draw": 2,
            },
            {
                "draw_ib": self.SIBLING_IB, "unique_str": f"LOD0.{self.SIBLING_IB}-7383-0",
                "vg_offset": 235, "vg_count": 14, "skeleton_group": self.GROUP,
                "vg_map": {i: 235 + i for i in range(14)}, "deform_draw": 8,
            },
        ]

    def _build(self, carrier_layout_is_16b=True, carrier_extra_element=False):
        """返回 (exporter, models, source_blend_bytes)。

        carrier 布局：16B（默认，供加宽）或 32B（供"降宽淘汰"用例）；
        多数派 target/sibling 一律 32B。
        """
        self._register_obj(f"LOD0.{self.CARRIER_IB}-19182-0", [79, 88, 105, 229, 248])
        stub = self._register_obj(f"LOD0.{self.TARGET_IB}-42759-0", [79, 79, 79], stub=True)
        self._register_obj(f"LOD0.{self.SIBLING_IB}-7383-0", [235, 236])

        carrier_elements = list(ELEMENTS_16B if carrier_layout_is_16b else ELEMENTS_32B)
        carrier_stride = 16 if carrier_layout_is_16b else 32
        if carrier_extra_element:
            # 源布局多出 INDICES idx1（锚点布局没有该通道）⇒ 重打包必须被拒绝
            carrier_elements.append(
                _blend_element("BLENDINDICES", 1, "R32G32_UINT", 8)
            )
            carrier_stride += 8
        source_bytes = bytes(range(carrier_stride * 3))

        carrier_model = _FakeDrawIBModel(
            self.CARRIER_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.CARRIER_IB}-19182-0", 184, 51,
                skeleton_group=self.GROUP, deform_draw=2,
                exported_vertex_count=18776,
            ), index_count=69612)],
            elements=carrier_elements,
            blend_stride=carrier_stride,
            blend_bytes=source_bytes,
            position_bytes=bytes(40 * 18776),
        )
        target_model = _FakeDrawIBModel(
            self.TARGET_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.TARGET_IB}-42759-0", 79, 105,
                skeleton_group=self.GROUP, deform_draw=20, exported_vertex_count=3,
            ))],
            elements=ELEMENTS_32B,
            blend_stride=32,
        )
        sibling_model = _FakeDrawIBModel(
            self.SIBLING_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.SIBLING_IB}-7383-0", 235, 14,
                skeleton_group=self.GROUP, deform_draw=8,
            ))],
            elements=ELEMENTS_32B,
            blend_stride=32,
        )
        models = [carrier_model, target_model, sibling_model]
        exporter = _make_exporter(models)
        exporter.merged_skeleton_components = self._components()
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(exporter.merged_skeleton_components)
        }
        return exporter, models, source_bytes

    @staticmethod
    def _apply_plan(exporter):
        """复刻 `_export_impl` 的接线：构建计划并写回实例字段。"""
        carrier_map, target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        exporter._redirect_carrier_map = carrier_map
        exporter._redirect_target_map = target_map
        return carrier_map, target_map, unredirected


class ZZMIBlendLayoutContractTests(_RetargetChainFixture):
    """布局元数据 → 元素级签名（`_blend_layout_key`，布局取自 `_drawib_category_layout`）。"""

    def test_layout_parses_elements_and_declared_stride(self):
        exporter, _models, source = self._build(carrier_layout_is_16b=True)
        layout = exporter._drawib_blend_layout(self.CARRIER_IB)
        self.assertEqual(layout["stride"], 16)
        self.assertEqual(
            [(e["semantic"], e["index"], e["format"], e["byte_width"], e["offset"])
             for e in layout["elements"]],
            [
                ("BLENDWEIGHTS", 0, "R32G32_FLOAT", 8, 0),
                ("BLENDINDICES", 0, "R32G32_UINT", 8, 8),
            ],
        )
        # 32B 侧：元素偏移按 ByteWidth 累加（0 / 16）
        wide = exporter._drawib_blend_layout(self.TARGET_IB)
        self.assertEqual(wide["stride"], 32)
        self.assertEqual([e["offset"] for e in wide["elements"]], [0, 16])

    def test_signature_is_element_level_and_normalizes_indices_int(self):
        exporter, _models, _src = self._build(carrier_layout_is_16b=True)
        self.assertEqual(
            exporter._drawib_blend_layout_signature(self.CARRIER_IB), KEY_16B
        )
        self.assertEqual(
            exporter._drawib_blend_layout_signature(self.TARGET_IB), KEY_32B
        )
        # BLENDINDICES 的 UINT/SINT 归并：UINT 与 SINT 布局必须同键（不拆 BI16 挂点）
        sint_layout = exporter._drawib_blend_layout(self.TARGET_IB)
        sint_layout["elements"][1]["format"] = "R32G32B32A32_SINT"
        self.assertEqual(exporter._blend_layout_key(sint_layout), KEY_32B)

    def test_catalog_counts_only_deform_parts(self):
        exporter, _models, _src = self._build(carrier_layout_is_16b=True)
        catalog = exporter._merged_group_layout_catalog(self.GROUP)
        self.assertEqual({k: v["count"] for k, v in catalog.items()},
                         {KEY_32B: 2, KEY_16B: 1})
        self.assertEqual(catalog[KEY_16B]["layout"]["stride"], 16)


class ZZMIRetargetChainTests(_RetargetChainFixture):
    """重打包链本体：加宽 payload + 资源写盘 + 候选淘汰 + 锚点选择。"""

    def test_minority_16b_carrier_is_widened_into_32b_anchor(self):
        exporter, _models, source = self._build(carrier_layout_is_16b=True)
        carrier_map, _target_map, unredirected = self._apply_plan(exporter)
        self.assertEqual(list(carrier_map), [self.CARRIER_IB])
        self.assertEqual(unredirected, {})
        plan = exporter._merged_group_redirect_plan(self.GROUP)
        self.assertIsNotNone(plan)
        # → 锚点 = 多数派 32B 元素级布局
        self.assertEqual(plan["anchor_layout_key"], KEY_32B)
        # 锚点布局的**声明 stride** 是重打包资源步长的来源（32）
        self.assertEqual(
            exporter._drawib_blend_layout(self.TARGET_IB)["stride"], 32
        )
        self.assertEqual(plan["blend_retarget_carriers"], [self.CARRIER_IB])
        self.assertEqual(
            plan["replay_blend_resources"][self.CARRIER_IB],
            f"ResourceZZRedirectBlend_{self.CARRIER_IB}_s32",
        )

        # `_build_redirect_blend_retarget` 的真实产出（不是"没抛异常"）
        name, stride, filename, payload = exporter._redirect_blend_retargets[
            self.CARRIER_IB
        ]
        self.assertEqual(name, f"ResourceZZRedirectBlend_{self.CARRIER_IB}_s32")
        self.assertEqual(stride, 32)
        self.assertEqual(filename, f"zz_redirect_blend_{self.CARRIER_IB}_s32.buf")
        rows = len(source) // 16
        self.assertEqual(rows, 3)
        self.assertEqual(len(payload), rows * 32)
        for row in range(rows):
            src = source[row * 16:(row + 1) * 16]
            out = payload[row * 32:(row + 1) * 32]
            # 权重 8B 照抄到锚点 offset 0；锚点权重通道的剩余 8B 补 0
            self.assertEqual(out[0:8], src[0:8], f"row{row} weights")
            self.assertEqual(out[8:16], b"\x00" * 8, f"row{row} weights pad")
            # 索引 8B 照抄到锚点 offset 16；锚点索引通道剩余 8B 补 0
            self.assertEqual(out[16:24], src[8:16], f"row{row} indices")
            self.assertEqual(out[24:32], b"\x00" * 8, f"row{row} indices pad")

    def test_write_redirect_blend_resources_emits_file_and_definition(self):
        exporter, _models, _source = self._build(carrier_layout_is_16b=True)
        self._apply_plan(exporter)
        definitions = exporter._write_redirect_blend_resources()
        self.assertEqual(
            definitions,
            [(
                f"ResourceZZRedirectBlend_{self.CARRIER_IB}_s32",
                32,
                f"zz_redirect_blend_{self.CARRIER_IB}_s32.buf",
            )],
        )
        path = os.path.join(
            _FAKE_MOD_FOLDER, "Meshes", f"zz_redirect_blend_{self.CARRIER_IB}_s32.buf"
        )
        self.assertTrue(os.path.isfile(path), path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), exporter._redirect_blend_retargets[
                self.CARRIER_IB
            ][3])

    def test_majority_16b_candidate_is_eliminated_when_it_would_narrow(self):
        """候选淘汰（`_build_redirect_blend_retarget` 的降宽拒绝）：多数派反而是 16B，载体是 32B。

        16B 键对 32B 载体需要**降宽**（丢骨骼影响）⇒ 该候选被淘汰；
        锚点只能回落到载体自己的 32B 布局，且不产生任何重打包。
        """
        # 让 target/sibling 变成 16B 多数派：重建模型时交换布局
        self._register_obj(f"LOD0.{self.CARRIER_IB}-19182-0", [79, 88, 105, 229, 248])
        self._register_obj(f"LOD0.{self.TARGET_IB}-42759-0", [79, 79, 79], stub=True)
        self._register_obj(f"LOD0.{self.SIBLING_IB}-7383-0", [235, 236])
        carrier = _FakeDrawIBModel(
            self.CARRIER_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.CARRIER_IB}-19182-0", 184, 51,
                skeleton_group=self.GROUP, deform_draw=2, exported_vertex_count=18776,
            ), index_count=69612)],
            elements=ELEMENTS_32B, blend_stride=32,
            blend_bytes=bytes(96), position_bytes=bytes(40 * 18776),
        )
        target = _FakeDrawIBModel(
            self.TARGET_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.TARGET_IB}-42759-0", 79, 105,
                skeleton_group=self.GROUP, deform_draw=20, exported_vertex_count=3,
            ))],
            elements=ELEMENTS_16B, blend_stride=16,
        )
        sibling = _FakeDrawIBModel(
            self.SIBLING_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.SIBLING_IB}-7383-0", 235, 14,
                skeleton_group=self.GROUP, deform_draw=8,
            ))],
            elements=ELEMENTS_16B, blend_stride=16,
        )
        models = [carrier, target, sibling]
        exporter = _make_exporter(models)
        exporter.merged_skeleton_components = self._components()
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(exporter.merged_skeleton_components)
        }

        catalog = exporter._merged_group_layout_catalog(self.GROUP)
        self.assertEqual({k: v["count"] for k, v in catalog.items()},
                         {KEY_16B: 2, KEY_32B: 1})

        self._apply_plan(exporter)
        plan = exporter._merged_group_redirect_plan(self.GROUP)
        self.assertIsNotNone(plan)
        # 16B 多数派（count=2）被降宽规则淘汰 ⇒ 锚点 = 唯一可用的 32B
        self.assertEqual(plan["anchor_layout_key"], KEY_32B)
        self.assertEqual(plan["blend_retarget_carriers"], [])
        self.assertEqual(
            plan["replay_blend_resources"],
            {self.CARRIER_IB: f"Resource{self.CARRIER_IB}Blend"},
        )
        self.assertEqual(exporter._redirect_blend_retargets, {})

    def test_retarget_rejected_when_carrier_has_element_absent_from_anchor(self):
        """候选淘汰（`_build_redirect_blend_retarget` 的「源有、锚点没有的元素」拒绝）：源多出通道 ⇒ 拒绝重打包。

        载体多出 INDICES idx1（24B 布局）⇒ 加宽到 32B 锚点会**丢真实索引**，必须拒绝；
        该淘汰使 32B 多数派候选整键不可用 ⇒ 锚点只能是载体自己的 24B 布局。
        """
        exporter, _models, _src = self._build(
            carrier_layout_is_16b=True, carrier_extra_element=True
        )
        anchor_layout = exporter._drawib_blend_layout(self.TARGET_IB)
        self.assertIsNone(
            exporter._build_redirect_blend_retarget(self.CARRIER_IB, anchor_layout)
        )
        self._apply_plan(exporter)
        plan = exporter._merged_group_redirect_plan(self.GROUP)
        key_carrier_24b = (
            "elements",
            (
                ("BLENDWEIGHTS", 0, "R32G32_FLOAT", 8, ""),
                ("BLENDINDICES", 0, "R32G32_INT", 8, ""),
                ("BLENDINDICES", 1, "R32G32_INT", 8, ""),
            ),
        )
        self.assertEqual(plan["anchor_layout_key"], key_carrier_24b)
        self.assertEqual(plan["blend_retarget_carriers"], [])
        # 对照：去掉多出元素后同一调用必须成功（证明上面的 None 来自淘汰规则而非其它前提）
        exporter_ok, _m, _s = self._build(carrier_layout_is_16b=True)
        ok = exporter_ok._build_redirect_blend_retarget(
            self.CARRIER_IB, exporter_ok._drawib_blend_layout(self.TARGET_IB)
        )
        self.assertIsNotNone(ok)
        self.assertEqual(ok[1], 32)


class ZZMIRedirectSwitchGateTests(_RetargetChainFixture):
    """链的开关前提：`zzmi_merged_redirect_enabled`（`_export_impl` 里 `_zzmi_prop_flag` 门控）。"""

    def test_switch_reads_true_in_this_fixture(self):
        self.assertTrue(_zzmi_prop_flag("zzmi_merged_redirect_enabled", False))
        previous = _FAKE_GLOBAL_PROPERTIES.zzmi_merged_redirect_enabled
        try:
            _FAKE_GLOBAL_PROPERTIES.zzmi_merged_redirect_enabled = lambda: False
            self.assertFalse(_zzmi_prop_flag("zzmi_merged_redirect_enabled", False))
        finally:
            _FAKE_GLOBAL_PROPERTIES.zzmi_merged_redirect_enabled = previous


if __name__ == "__main__":
    unittest.main()
