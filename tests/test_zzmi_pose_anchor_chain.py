"""C-1 覆盖：姿态指纹对齐的**正向激活链**（走真实推导，最小且可证伪）。

覆盖分支（`ui/universal/zzmi.py`，t36 后快照；行号以 AST 为准）：
- `_merged_group_pose_anchor_slot`        L1930-1956  **真实推导**：组内 ≥2 个部件的
    `vg_map` **值集合交集**的最小槽位；L1948-1949 单部件组 → None；L1954-1955 无交集 → None
- `_merged_component_pose_anchor_local`   L1958-1966  该部件里映射到锚点槽位的本地索引
- `_merged_pose_alignment_unavailable_reason` L1968-1986 三个原因码（no_redirect_plan /
    no_shared_canonical_bone / 单部件组 None）
- `_append_merged_pose_key_alignment`     L1988-2059  对齐修正块本体
    · L2002-2004 锚点缺失即返回（负向）
    · L2026-2030 `ResourceZZPoseKeySrc` + `->SpatialHash(f0, f0+4, f0+8, 0.01)`
    · L2031-2040 指纹 → 槽位池写入（`PoolZZMISlotOfKey_*` / `PoolZZMIG_Taken_*`）
    · L2041-2058 `emit_move`：palette 搬运 +（SO owner）SO 重新捕获
- 池段产出 `add_merged_skeleton_sections` L4396-4415（锚点存在才发；`pool_size=16` /
    `pool_index_type=spatial` / `pool_spatial_radius` / `expiration=1`；占用池 `pool_size=4`）

**与既有用例的区别（审计点名的缺口）**：既有测试手工注入 `pose_anchor_slot`，绕过了
真实推导；本文件**不注入**，一律由 `_build_merged_mesh_redirect_plan()` 真实推导出来，
并断言池段真的被执行。
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
PKG = "zzmi_pose_anchor_chain_test_pkg"


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


class _FakeIniSection:
    def __init__(self, section_type):
        self.SectionType = section_type
        self.SectionName = ""
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def new_line(self):
        self.SectionLineList.append("")


class _FakeIniBuilder:
    def __init__(self):
        self.sections = []

    def append_section(self, section):
        self.sections.append(section)

    def save_to_file(self, _path):
        pass


def _all_builder_lines(builder):
    lines = []
    for section in builder.sections:
        if section.SectionName:
            lines.append(f"[{section.SectionName}]")
        lines.extend(section.SectionLineList)
    return lines


class _FakeObject:
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
_load_real_module(f"{PKG}.common.zzmi_channel", "common/zzmi_channel.py")

_FAKE_MOD_FOLDER = tempfile.mkdtemp(prefix="zzmi_pose_anchor_chain_")
_FAKE_GLOBAL_CONFIG = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: _FAKE_MOD_FOLDER,
    get_workspace_name=lambda: "ZzmiPoseAnchorChainTest",
    path_workspace_folder=lambda: "",
)
_FAKE_GLOBAL_PROPERTIES = types.SimpleNamespace(
    import_merged_vgmap=lambda: True,
    forbid_auto_texture_ini=lambda: False,
    zzz_use_slot_fix=lambda: False,
    zzmi_merged_redirect_enabled=lambda: True,
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=_FAKE_GLOBAL_CONFIG)
_install_module(
    f"{PKG}.common.global_properties", GlobalProterties=_FAKE_GLOBAL_PROPERTIES
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=_FakeIniBuilder,
    M_IniSection=_FakeIniSection,
    M_SectionType=types.SimpleNamespace(
        MergedSkeleton="MergedSkeleton",
        Constants="Constants",
        Present="Present",
        TextureOverrideVB="TextureOverrideVB",
        TextureOverrideIB="TextureOverrideIB",
        TextureOverrideVertexLimitRaise="TextureOverrideVertexLimitRaise",
        ResourceBuffer="ResourceBuffer",
    ),
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
    def __init__(self, draw_ib, submesh_model_list):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 4643
        self.vertex_limit_hash = "dd9c8d5e"
        self.d3d11GameType = _FakeGameType()
        self.d3d11GameType.CategoryStrideDict = dict(_FakeGameType.CategoryStrideDict)
        self.category_hash_dict = {
            "Position": "122883aa",
            "Texcoord": "5c0fefda",
            "Blend": "bf543990",
        }
        self.submesh_model_list = submesh_model_list
        self.category_buffer_dict = {}
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


class _PoseAnchorFixture(unittest.TestCase):
    """组 7：三个部件**共享 canonical 槽位 79**（去重借位），姿态锚点应推导为 79。"""

    GROUP = 7
    TARGET_IB = "a23aa8a3"
    CARRIER_IB = "b20f90ea"
    SIBLING_IB = "b30db54e"
    ANCHOR_SLOT = 79

    def setUp(self):
        _FAKE_BPY_DATA.objects._items.clear()
        _FAKE_BPY_DATA.meshes._items.clear()

    def _register_obj(self, name, bone_ids, stub=False):
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
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        draw_call = dcm(obj_name=submesh.unique_str)
        draw_call.index_count = index_count
        submesh.drawcall_model_list = [draw_call]
        return submesh

    def _components(self, shared_anchor=True):
        if shared_anchor:
            maps = {
                self.TARGET_IB: {0: 79, 1: 80, 2: 81},
                self.CARRIER_IB: {0: 79, 1: 83, 2: 84},
                self.SIBLING_IB: {0: 79, 1: 86},
            }
            spans = {
                self.TARGET_IB: (79, 3),
                self.CARRIER_IB: (82, 3),
                self.SIBLING_IB: (85, 2),
            }
        else:
            # 组内**没有**共享骨骼：三段互不相交
            maps = {
                self.TARGET_IB: {0: 79, 1: 80, 2: 81},
                self.CARRIER_IB: {0: 184, 1: 185, 2: 186},
                self.SIBLING_IB: {0: 235, 1: 236},
            }
            spans = {
                self.TARGET_IB: (79, 3),
                self.CARRIER_IB: (184, 3),
                self.SIBLING_IB: (235, 2),
            }
        deform = {self.TARGET_IB: 20, self.CARRIER_IB: 2, self.SIBLING_IB: 8}
        order = [self.TARGET_IB, self.CARRIER_IB, self.SIBLING_IB]
        components = [
            {
                "draw_ib": draw_ib,
                "unique_str": f"LOD0.{draw_ib}-19182-0",
                "vg_offset": spans[draw_ib][0],
                "vg_count": spans[draw_ib][1],
                "skeleton_group": self.GROUP,
                "vg_map": maps[draw_ib],
                "deform_draw": deform[draw_ib],
            }
            for draw_ib in order
        ]
        # t75：通道计划（导入期由 common/zzmi_channel.select_channel_plan 算出）。
        # 夹具用**真实实现**按整组推导，与生产口径同源。
        channel_module = sys.modules[f"{PKG}.common.zzmi_channel"]
        plan = channel_module.select_channel_plan(components)
        for component in components:
            component["channel"] = dict(plan[component["draw_ib"]])
        return components

    def _build(self, shared_anchor=True, single_component=False):
        self._register_obj(
            f"LOD0.{self.CARRIER_IB}-19182-0", [79, 80, 83, 85, 229, 248]
        )
        self._register_obj(
            f"LOD0.{self.TARGET_IB}-42759-0", [79, 79, 79], stub=True
        )
        self._register_obj(f"LOD0.{self.SIBLING_IB}-7383-0", [79, 86])

        carrier_model = _FakeDrawIBModel(
            self.CARRIER_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.CARRIER_IB}-19182-0", 82, 3,
                skeleton_group=self.GROUP, deform_draw=2, exported_vertex_count=18776,
            ), index_count=69612)],
        )
        target_model = _FakeDrawIBModel(
            self.TARGET_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.TARGET_IB}-42759-0", 79, 3,
                skeleton_group=self.GROUP, deform_draw=20, exported_vertex_count=3,
            ))],
        )
        sibling_model = _FakeDrawIBModel(
            self.SIBLING_IB,
            [self._attach_drawcalls(_FakeSubmesh(
                f"LOD0.{self.SIBLING_IB}-7383-0", 85, 2,
                skeleton_group=self.GROUP, deform_draw=8,
            ))],
        )
        models = [carrier_model, target_model, sibling_model]
        exporter = _make_exporter(models)
        components = self._components(shared_anchor=shared_anchor)
        if single_component:
            components = components[1:2]
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        return exporter, models

    @staticmethod
    def _apply_plan(exporter):
        carrier_map, target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        exporter._redirect_carrier_map = carrier_map
        exporter._redirect_target_map = target_map
        return carrier_map, target_map, unredirected

    @staticmethod
    def _vb_text(exporter, models):
        builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        return "\n".join(_all_builder_lines(builder))

    @staticmethod
    def _skeleton_text(exporter, models):
        # 重定向 carrier 的 vb1 对齐缓冲需要真实导出字节（与既有夹具同口径）
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next(
                (m for m in models if m.draw_ib == carrier_ib), None
            )
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {})
                .get("Texcoord", 0)
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get(
                "Texcoord"
            ):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        return "\n".join(_all_builder_lines(builder))


class ZZMIPoseAnchorDerivationTests(_PoseAnchorFixture):
    """真实推导（**不注入** `pose_anchor_slot`）。"""

    def test_anchor_slot_is_intersection_of_group_vgmap_values(self):
        exporter, _models = self._build(shared_anchor=True)
        # t75：三个部件共骨 {79}（同一个连通分量）⇒ 通道槽位 = 79；
        # 三件里槽位 79 都对应本地索引 0 ⇒ HashRegion(0, 48)。
        self.assertEqual(exporter._merged_group_pose_anchor_slot(self.GROUP), 79)
        for component in exporter.merged_skeleton_components:
            self.assertEqual(
                exporter._merged_component_pose_anchor_local(component, 79), 0
            )

    def test_plan_carries_the_derived_anchor_and_no_unavailable_reason(self):
        exporter, _models = self._build(shared_anchor=True)
        self._apply_plan(exporter)
        plan = exporter._merged_group_redirect_plan(self.GROUP)
        self.assertIsNotNone(plan)
        # 计划里的锚点来自真实推导（对照：既有测试在此处手工注入）
        self.assertEqual(plan["pose_anchor_slot"], 79)
        # t75：可达时无原因码
        self.assertIsNone(
            exporter._merged_pose_alignment_unavailable_reason(0, plan)
        )

    def test_no_shared_bone_falls_back_per_connected_component(self):
        """t75：组内不共骨不再让整组放弃——每个连通分量各取自己的通道骨。

        三个部件互不相交 ⇒ 三个分量，各自退化到「本部件最小槽位」；
        因此 `_merged_group_pose_anchor_slot` 返回升序第一个（79），
        每件仍然拿到自己的通道记录（判定可达，不报原因码）。
        """
        exporter, _models = self._build(shared_anchor=False)
        self.assertEqual(exporter._merged_group_pose_anchor_slot(self.GROUP), 79)
        self._apply_plan(exporter)
        plan = exporter._merged_group_redirect_plan(self.GROUP)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["pose_anchor_slot"], 79)
        for component in exporter.merged_skeleton_components:
            record = exporter._merged_component_channel_record(component)
            self.assertIsNotNone(record)
            self.assertEqual(record["channel_reason"], "no_shared_bone_in_component")
            # 退化的通道骨只对本部件两实例可区分 ⇒ 必须有点名诊断
            self.assertTrue(record["channel_diagnostic"])
        self.assertIsNone(
            exporter._merged_pose_alignment_unavailable_reason(0, plan)
        )

    def test_single_component_group_still_gets_its_own_channel(self):
        """t75：单部件组自成一个连通分量，通道骨只用于区分它自己的两实例。"""
        exporter, _models = self._build(shared_anchor=True, single_component=True)
        self.assertEqual(exporter._merged_group_pose_anchor_slot(self.GROUP), 79)
        component = exporter.merged_skeleton_components[0]
        record = exporter._merged_component_channel_record(component)
        self.assertIsNotNone(record)
        self.assertEqual(record["channel_slot"], 79)
        self.assertEqual(record["channel_local"], 0)


class ZZMIPoseAlignmentBlockTests(_PoseAnchorFixture):
    """对齐修正块 + 池段的真实产出。"""

    def test_alignment_block_emitted_with_exact_hash_and_pools(self):
        exporter, models = self._build(shared_anchor=True)
        self._apply_plan(exporter)
        text = self._vb_text(exporter, models)

        # t75：键 = 通道骨的 48 字节矩阵整块哈希（逐字节精确，无容差）。
        # 三件里通道槽位 79 都对应本地索引 0 ⇒ HashRegion(0, 48)。
        self.assertIn("ResourceZZPoseKeySrc = ref vs-t0", text)
        self.assertIn(
            f"$zz_ms_pose_key_{self.GROUP} = ResourceZZPoseKeySrc->HashRegion(0, 48)",
            text,
        )
        self.assertNotIn("SpatialHash", text)
        # 守卫必须 > 0（HashRegion 失败返回 -1/-2/-3，`!= 0` 挡不住）
        self.assertIn(f"if $zz_ms_pose_key_{self.GROUP} > 0", text)
        self.assertIn(
            f"if $PoolZZMISlotOfKey_G{self.GROUP}[$zz_ms_pose_key_{self.GROUP}] == 0",
            text,
        )
        self.assertIn(f"if $PoolZZMIG_Taken_G{self.GROUP}[1] == 0", text)
        self.assertIn(f"$PoolZZMIG_Taken_G{self.GROUP}[1] = 1", text)
        self.assertIn(
            f"$PoolZZMISlotOfKey_G{self.GROUP}[$zz_ms_pose_key_{self.GROUP}] = 1",
            text,
        )
        self.assertIn(f"$PoolZZMIG_Taken_G{self.GROUP}[2] = 1", text)
        self.assertIn(
            f"$PoolZZMISlotOfKey_G{self.GROUP}[$zz_ms_pose_key_{self.GROUP}] = 2",
            text,
        )
        # t75：不再有"出现次 ↔ 键"互搬（emit_move）；捕获直接写在键算出的槽上。
        self.assertIn(
            f"if $PoolZZMISlotOfKey_G{self.GROUP}[$zz_ms_pose_key_{self.GROUP}] == 2",
            text,
        )
        self.assertNotIn(
            f"ResourceZZPalette_{self.CARRIER_IB}_s2 = "
            f"copy ResourceZZPalette_{self.CARRIER_IB}_s1",
            text,
        )
        self.assertIn(
            f"ResourceZZPalette_{self.CARRIER_IB}_s2 = copy vs-t0 unless_null",
            text,
        )

    def test_pool_sections_are_emitted_for_a_derived_channel(self):
        exporter, models = self._build(shared_anchor=True)
        self._apply_plan(exporter)
        text = self._skeleton_text(exporter, models)

        # t75：通道存在才发池段；池必须是 fifo（键是 32 位整数，全位精确匹配）
        self.assertIn(f"[PoolZZMISlotOfKey_G{self.GROUP}]", text)
        # 一个连通分量 × 2 槽 × 2 实例上界 = 4
        self.assertIn("pool_size = 4", text)
        self.assertIn("pool_index_type = fifo", text)
        self.assertNotIn("pool_spatial_radius", text)
        self.assertNotIn("pool_index_type = spatial", text)
        self.assertIn("pool_expiration_timeout_frames = 1", text)
        self.assertIn("pool_expiration_reset_elements = 1", text)
        self.assertIn(f"[PoolZZMIG_Taken_G{self.GROUP}]", text)

    def test_no_channel_skips_block_diagnoses_and_emits_no_pool(self):
        exporter, models = self._build(shared_anchor=False)
        self._apply_plan(exporter)
        vb_text = self._vb_text(exporter, models)
        skeleton_text = self._skeleton_text(exporter, models)

        # 负向：清掉通道记录 = 无通道部件 ⇒ 不发键块、不发池段
        for component in exporter.merged_skeleton_components:
            component["channel"] = {}
        vb_text = self._vb_text(exporter, models)
        skeleton_text = self._skeleton_text(exporter, models)
        self.assertNotIn("ResourceZZPoseKeySrc", vb_text)
        self.assertNotIn("SpatialHash(", vb_text)
        self.assertNotIn(f"[PoolZZMISlotOfKey_G{self.GROUP}]", skeleton_text)
        self.assertNotIn(f"[PoolZZMIG_Taken_G{self.GROUP}]", skeleton_text)
        # 但必须留下机器可读原因码（绝不静默）
        self.assertIn("; ZZMI-MERGE-DIAG POSE_ALIGNMENT_UNAVAILABLE", vb_text)
        self.assertIn("reason=no_channel_plan", vb_text)
        # 供骨必须保留：palette 捕获与 attach run 都还在
        self.assertIn(
            f"ResourceZZPalette_{self.CARRIER_IB}_s1 = copy vs-t0 unless_null", vb_text
        )
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_", vb_text)


if __name__ == "__main__":
    unittest.main()
