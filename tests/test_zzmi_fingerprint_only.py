"""t75（ZZMI 骨骼合并 · 唯一判定口径 = 通道骨精确哈希）**产物级**验收用例。

用户 2026-09-18 拍板后的契约，本文件逐条给出**可证伪**断言：

1. 产物里**没有** `SpatialHash`（旧的 0.01 量化键已彻底移除）。
2. 键是 `ResourceZZPoseKeySrc->HashRegion(48*local, 48)`，守卫写 `> 0`
   （`HashRegion` 失败返回 -1/-2/-3，`!= 0` 挡不住）。
3. 池是 `pool_index_type = fifo` + `pool_variable_default_value = 0`；
   **不得**再出现 `pool_spatial_radius`（0.01 格会把两个实例并键）。
4. **没有出现次双路径**：不得出现 `if $zz_ms_pose_key_* == 0`、不得出现
   出现次分支的 `if $zz_ms_occ_* == 1 → palette_s1`、不得出现
   `ResourceZZPalette_*_sX = copy ResourceZZPalette_*_sY`（emit_move 互搬）。
5. 通道骨按**共享骨图连通分量**选：真实 G0/G2 的 vg_map 走
   `common/zzmi_channel.select_channel_plan`，断言选中槽位与本地下标。
6. **无通道部件**（用户实机确认的硬事实）：任何门控表达式都不得包含它的
   `$zz_ms_seen_<i><k>` / `$zz_ms_prev_<i><k>` 项；但它的
   `ResourceZZPalette_<ib>_s<k> = copy vs-t0` 与 attach `run` **必须仍在**。
7. 单连通组（一个连通分量）产物与旧行为等价：两槽各自被捕获、attach 全发、
   `[Present]` 清场不变。

夹具与 `tests/test_zzmi_pose_anchor_chain.py` 同源（fake bpy 宿主 + 真实
`ui/universal/zzmi.py`），但**不注入**任何判定结果：通道计划一律由
`common/zzmi_channel.select_channel_plan`（真实实现）算出。
"""
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests import _real_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_fingerprint_only_test_pkg"


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
_load_real_module(f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py")
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")
_load_real_module(f"{PKG}.common.zzmi_channel", "common/zzmi_channel.py")

_FAKE_MOD_FOLDER = tempfile.mkdtemp(prefix="zzmi_fingerprint_only_")
_FAKE_GLOBAL_CONFIG = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: _FAKE_MOD_FOLDER,
    get_workspace_name=lambda: "ZzmiFingerprintOnlyTest",
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
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
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
CHANNEL = sys.modules[f"{PKG}.common.zzmi_channel"]

# 真实抓帧的 vg_map 读数（K: 下 `Meshes/zz_vgmap_<ib>.buf` 的槽位集合，
# 见 review-reports/t73-instance-slot-alignment.md §1.1 / t70 §3.1）：
# G0 六件里五件共享槽 0（local 0/0/0/7/2），8c8de427（槽 50..55）与谁都不共骨；
# G2 四件共享槽 185/200，映射到 185 的本地骨分别是 5/14/3/25。
G0_VG_MAPS = {
    "01ef4403": {local: local for local in range(34)},
    "38b3bd13": {0: 0, **{local: 34 + local for local in range(1, 15)}},
    "8c8de427": {local: 50 + local for local in range(6)},
    "9258d5f8": {0: 0, **{local: 56 + local for local in range(1, 23)}},
    "999bff94": {7: 0, **{local: 76 + local for local in range(83) if local != 7}},
    "ae840e72": {2: 0, **{local: 159 + local for local in range(5) if local != 2}},
}
G2_VG_MAPS = {
    # 注意：显式给出的共享槽位必须写在展开**之后**（python dict 字面量里后者覆盖前者），
    # 否则 `{l: 209 + l}` 会把 `14: 185` / `13: 200` 覆盖成 223 / 222 ⇒ 四件不再共骨。
    "3b1b73fe": {**{local: 180 + local for local in range(29)}, 5: 185, 20: 200},
    "4a178546": {**{local: 209 + local for local in range(47)}, 14: 185, 13: 200},
    "869976a3": {**{local: 256 + local for local in range(7)}, 3: 185, 2: 200},
    "c209c22b": {**{local: 263 + local for local in range(135)}, 25: 185, 26: 200},
}


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
    return ExportZZMI(blueprint_model)


class _Fixture(unittest.TestCase):
    """组 7：三件共享槽 79（同一条通道），local 都是 0。"""

    GROUP = 7
    TARGET_IB = "a23aa8a3"
    CARRIER_IB = "b20f90ea"
    SIBLING_IB = "b30db54e"

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

    def _components(self):
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
        self._apply_channels(components)
        return components

    @staticmethod
    def _apply_channels(components):
        plan = CHANNEL.select_channel_plan(components)
        for component in components:
            component["channel"] = dict(plan[component["draw_ib"]])
        return components

    def _build(self):
        self._register_obj(
            f"LOD0.{self.CARRIER_IB}-19182-0", [79, 80, 83, 85, 229, 248]
        )
        self._register_obj(f"LOD0.{self.TARGET_IB}-42759-0", [79, 79, 79], stub=True)
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
        exporter.drawib_model_list = models
        exporter.merged_skeleton_components = self._components()
        exporter.merged_skeleton_component_id_dict = {
            str(c["draw_ib"]): i
            for i, c in enumerate(exporter.merged_skeleton_components)
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
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next((m for m in models if m.draw_ib == carrier_ib), None)
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {})
                .get("Texcoord", 0) or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get("Texcoord"):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        return "\n".join(_all_builder_lines(builder))


# ===========================================================================
# 1~4：判定口径只剩指纹（产物级可证伪）
# ===========================================================================
class ZZMIExactHashOnlyTests(_Fixture):
    def test_product_has_no_spatial_hash_and_uses_hashregion_with_gt_guard(self):
        exporter, models = self._build()
        self._apply_plan(exporter)
        text = self._vb_text(exporter, models)

        self.assertNotIn("SpatialHash", text)
        self.assertIn("ResourceZZPoseKeySrc = ref vs-t0", text)
        # 三件里通道槽位 79 都对应本地索引 0
        self.assertIn(
            f"$zz_ms_pose_key_{self.GROUP} = ResourceZZPoseKeySrc->HashRegion(0, 48)",
            text,
        )
        # 守卫必须 > 0（HashRegion 失败码是 -1/-2/-3）
        self.assertIn(f"if $zz_ms_pose_key_{self.GROUP} > 0", text)
        self.assertNotIn(f"if $zz_ms_pose_key_{self.GROUP} != 0", text)
        self.assertNotIn(f"if $zz_ms_pose_key_{self.GROUP} == 0", text)

    def test_pool_is_fifo_exact_match(self):
        exporter, models = self._build()
        self._apply_plan(exporter)
        text = self._skeleton_text(exporter, models)

        self.assertIn(f"[PoolZZMISlotOfKey_G{self.GROUP}]", text)
        self.assertIn("pool_index_type = fifo", text)
        self.assertNotIn("pool_index_type = spatial", text)
        self.assertNotIn("pool_spatial_radius", text)
        self.assertIn("pool_variable_default_value = 0", text)
        self.assertIn("pool_expiration_timeout_frames = 1", text)
        # 容量 = 连通分量数(1) × 槽位数(2) × 实例上界(2) = 4；不再是写死的 16
        self.assertIn("pool_size = 4", text)
        self.assertNotIn("pool_size = 16", text)

    def test_no_occurrence_dual_path_anywhere(self):
        """出现次双路径必须彻底移除（用户明确：多判据共存只增加开销）。"""
        exporter, models = self._build()
        self._apply_plan(exporter)
        vb_text = self._vb_text(exporter, models)
        skeleton_text = self._skeleton_text(exporter, models)
        combined = "\n".join((vb_text, skeleton_text))

        # ① 不得有 `key == 0` 的出现次回退分支
        self.assertNotIn(f"if $zz_ms_pose_key_{self.GROUP} == 0", combined)
        # ② 不得有 `key <= 0` 以外的出现次捕获分支（键块内只有 >0 / else 诊断）
        self.assertNotIn("$zz_ms_occ_0 == 1\n    ResourceZZPalette", combined)
        # ③ 不得有 emit_move 单向互搬
        self.assertNotRegex(
            combined,
            r"ResourceZZPalette_[0-9a-f]+_s\d = copy ResourceZZPalette_[0-9a-f]+_s\d",
        )
        # ④ 出现次变量仍然存在（它是**到达标记**，不再是判定输入）
        self.assertIn("$zz_ms_occ_0 = $zz_ms_occ_0 + 1", combined)
        self.assertIn("$zz_ms_seen_01 = $zz_ms_seen_01 + ($zz_ms_occ_0 == 1)", combined)

    def test_null_hash_failure_is_an_explicit_diagnosed_path(self):
        """HashRegion 失败（键 ≤ 0）必须是显式诊断路径，**不得**静默退回出现次。"""
        exporter, models = self._build()
        self._apply_plan(exporter)
        text = self._vb_text(exporter, models)
        # else 分支必须只剩下诊断注释（不写 palette、不查出现次）
        self.assertIn("HashRegion 失败", text)
        self.assertIn("**不退回出现次**", text)
        lines = text.splitlines()
        start = lines.index(f"if $zz_ms_pose_key_{self.GROUP} > 0")
        # 守卫级 `else` / `endif` 都写在第 0 列（池内的 else/endif 缩进 4 格），
        # 因此按**整行精确匹配**取出的就是键守卫自己的两条边界行。
        else_index = lines.index("else", start)
        endif_index = lines.index("endif", else_index)
        else_body = lines[else_index + 1 : endif_index]
        self.assertTrue(else_body, "键 ≤ 0 的 else 分支必须有显式诊断语句")
        for line in else_body:
            self.assertNotIn("$zz_ms_occ_", line)
            self.assertNotIn("ResourceZZPalette_", line)


# ===========================================================================
# 5：通道骨按共享骨图连通分量选（真实 vg_map 读数）
# ===========================================================================
class ZZMIChannelSelectionTests(unittest.TestCase):
    def _plan(self, vg_maps):
        components = [
            {
                "draw_ib": draw_ib,
                "vg_map": vg_map,
                "vg_count": len(vg_map),
                "vg_offset": min(vg_map.values()),
                "skeleton_group": 0,
            }
            for draw_ib, vg_map in vg_maps.items()
        ]
        return components, CHANNEL.select_channel_plan(components)

    def test_g0_five_parts_share_channel_slot_zero(self):
        """G0：五件共享槽 0（同一连通分量）⇒ 通道槽位 0，本地下标 0/0/0/7/2。

        旧判据（全组唯一交集）因 8c8de427 与谁都不共骨而整组返回 None ⇒
        六个部件各打一行 `no_shared_canonical_bone` 后静默退回出现次。
        """
        components, plan = self._plan(G0_VG_MAPS)
        partitions = CHANNEL.shared_bone_components(components)
        self.assertEqual(
            sorted(len(members) for members in partitions), [1, 5]
        )
        expected_local = {
            "01ef4403": 0,
            "38b3bd13": 0,
            "9258d5f8": 0,
            "999bff94": 7,
            "ae840e72": 2,
        }
        for draw_ib, local in expected_local.items():
            self.assertEqual(plan[draw_ib]["channel_slot"], 0, draw_ib)
            self.assertEqual(plan[draw_ib]["channel_local"], local, draw_ib)
            self.assertEqual(
                plan[draw_ib]["channel_reason"], "shared_connecting_bone", draw_ib
            )
            self.assertEqual(plan[draw_ib]["channel_diagnostic"], "", draw_ib)
        # 8c8de427 自成一个分量：退化为本部件最小槽位，并必须点名
        self.assertEqual(plan["8c8de427"]["channel_slot"], 50)
        self.assertEqual(
            plan["8c8de427"]["channel_reason"], "no_shared_bone_in_component"
        )
        self.assertTrue(plan["8c8de427"]["channel_diagnostic"])

    def test_g2_channel_prefers_the_most_shared_and_weighted_slot(self):
        """G2：槽 185 与 200 都被四件引用 ⇒ 按「顶点权重合计」决胜，仍可选。"""
        components, plan = self._plan(G2_VG_MAPS)
        partitions = CHANNEL.shared_bone_components(components)
        self.assertEqual(len(partitions), 1)
        self.assertEqual(len(partitions[0]), 4)
        slots = {record["channel_slot"] for record in plan.values()}
        self.assertEqual(len(slots), 1, "同分量必须共用一个通道槽位")
        self.assertIn(slots.pop(), (185, 200))
        expected_local = {"3b1b73fe": 5, "4a178546": 14, "869976a3": 3, "c209c22b": 25}
        for draw_ib, local in expected_local.items():
            self.assertEqual(plan[draw_ib]["channel_local"], local, draw_ib)

    def test_weight_tiebreak_is_deterministic_and_reviewable(self):
        """顶点权重合计是次级排序键：权重高的槽位胜出。"""
        vg_maps = {
            # 两件必须**同时**引用槽 10 与 11（否则 11 只有一件引用 ⇒ 不是共享候选，
            # 权重次级排序键根本没有机会生效）。
            "aaaa1111": {0: 10, 1: 11},
            "bbbb2222": {0: 10, 1: 11, 2: 20},
        }
        components, plan = self._plan(vg_maps)
        # 默认权重 0 ⇒ 按槽位号升序 ⇒ 10
        self.assertEqual({r["channel_slot"] for r in plan.values()}, {10})
        # 给槽 11 加上权重 ⇒ 它必须胜出
        for component in components:
            component["slot_weights"] = {10: 1, 11: 99}
        plan = CHANNEL.select_channel_plan(components)
        self.assertEqual({r["channel_slot"] for r in plan.values()}, {11})

    def test_single_part_component_never_reports_a_shared_bone(self):
        components, plan = self._plan({"aaaa1111": {0: 5, 1: 6}})
        self.assertEqual(plan["aaaa1111"]["channel_slot"], 5)
        self.assertEqual(
            plan["aaaa1111"]["channel_reason"], "no_shared_bone_in_component"
        )
        self.assertEqual(plan["aaaa1111"]["channel_shared_components"], 1)

    def test_real_g0_g2_degenerate_parts_are_never_cross_part(self):
        """t78 #5 + 用户硬事实：``8c8de427`` / ``869976a3`` 与任何件都不共骨。

        它们只能按「无通道件」处置（供骨、不参与判定、不入门控）；其余共骨件
        必须被判为跨部件共享（键驱动）。这是**可证伪**的：任何"按最小槽位兜底
        也能当判定输入"的实现都会在这里返回 True。

        t78 #3：**骨头身份必须按骨名**。这里同时断言"只看槽号"这一弱代理会
        把尾巴 ``869976a3`` 误连（它的槽号与身体件撞号，物理骨却不同名）——
        这就是为什么记录里必须带 ``channel_identity_basis``。
        """
        # G0：五件共享根骨 "root"；8c8de427（发饰）自成一系。
        g0_names = {
            "01ef4403": {0: "root", 1: "b1"},
            "38b3bd13": {0: "root", 1: "b2"},
            "9258d5f8": {0: "root", 1: "b3"},
            "999bff94": {7: "root", 0: "b4"},
            "ae840e72": {2: "root", 0: "b5"},
            "8c8de427": {0: "hair_acc_0", 1: "hair_acc_1"},
        }
        # G2：三件共享 "spine"；869976a3（尾巴）自成一系（槽号 185/200 撞号但不同名）。
        g2_names = {
            "3b1b73fe": {5: "spine", 20: "hip"},
            "4a178546": {14: "spine", 13: "hip"},
            "c209c22b": {25: "spine", 26: "hip"},
            "869976a3": {3: "tail_0", 2: "tail_1"},
        }
        for vg_maps, names in ((G0_VG_MAPS, g0_names), (G2_VG_MAPS, g2_names)):
            # F4（复核发现）口径更正：无骨名时 basis 自述为 `local_slot_map`
            # （槽位号由当帧 palette 矩阵 bitwise 去重派生，不是骨名身份）。
            for basis in ("bone_identity", "local_slot_map"):
                components = [
                    {
                        "draw_ib": draw_ib,
                        "vg_map": vg_map,
                        "vg_count": len(vg_map),
                        "vg_offset": min(vg_map.values()),
                        "skeleton_group": 0,
                    }
                    for draw_ib, vg_map in vg_maps.items()
                ]
                if basis == "bone_identity":
                    for component in components:
                        component["bone_ids"] = dict(names[component["draw_ib"]])
                plan = CHANNEL.select_channel_plan(components)
                for draw_ib, record in plan.items():
                    self.assertEqual(record["channel_identity_basis"], basis, draw_ib)
                    cross_part = CHANNEL.is_cross_part_channel(record)
                    if basis == "bone_identity":
                        # 骨名口径（正确口径）：发饰/尾巴与谁都不共骨 ⇒ 只供骨
                        self.assertFalse(
                            cross_part if draw_ib in ("8c8de427", "869976a3") else not cross_part,
                            f"{draw_ib} 的骨名身份判定与 t78 #5 不符",
                        )
                    else:
                        # 槽号口径（弱代理，导入期暂无骨名时的兜底）：尾巴撞号被误连
                        if draw_ib == "869976a3":
                            self.assertTrue(
                                cross_part,
                                "槽号弱代理本会把尾巴误连——这正是必须记 "
                                "channel_identity_basis 的原因（t78 #3）",
                            )
                if basis == "bone_identity":
                    for draw_ib in ("8c8de427", "869976a3"):
                        if draw_ib not in plan:
                            continue
                        self.assertTrue(
                            plan[draw_ib]["channel_diagnostic"],
                            f"{draw_ib} 必须带退化诊断（绝不静默）",
                        )


class ZZMIBoneIdentityTests(unittest.TestCase):
    """t78 #3：骨骼**身份**指纹与「当帧矩阵」指纹必须分开，且身份不得依赖矩阵。"""

    def test_identity_ignores_the_same_frame_matrix(self):
        vg_map = {0: 79, 1: 83, 7: 0}
        # 同一份身份映射 + 两套**不同**的当帧 palette 字节（= 两次抓帧）
        frame_a = bytes(range(48))
        frame_b = bytes(range(48, 96))
        self.assertEqual(
            CHANNEL.bone_identity_digest(vg_map),
            CHANNEL.bone_identity_digest(dict(vg_map)),
        )
        self.assertNotEqual(
            CHANNEL.same_frame_matrix_digest(frame_a),
            CHANNEL.same_frame_matrix_digest(frame_b),
        )
        # 身份指纹与矩阵指纹**必须**是两个不同的值域：同映射不同帧，身份不变
        self.assertNotEqual(
            CHANNEL.bone_identity_digest(vg_map),
            CHANNEL.same_frame_matrix_digest(frame_a),
        )

    def test_identity_changes_when_the_slot_mapping_changes(self):
        self.assertNotEqual(
            CHANNEL.bone_identity_digest({0: 79, 1: 83}),
            CHANNEL.bone_identity_digest({0: 79, 1: 84}),
        )

    def test_identity_is_order_independent_and_rejects_junk(self):
        self.assertEqual(
            CHANNEL.bone_identity_digest({1: 83, 0: 79}),
            CHANNEL.bone_identity_digest({0: 79, 1: 83}),
        )
        # 非 dict / 不可解析项一律忽略，绝不抛异常（损坏缓存走"显式诊断"路径）
        self.assertTrue(CHANNEL.bone_identity_digest(None))
        self.assertEqual(
            CHANNEL.bone_identity_digest({"x": "y", 0: 79}),
            CHANNEL.bone_identity_digest({0: 79}),
        )


# ===========================================================================
# 6：无通道部件 —— 用户实机确认的硬事实（摘门控、留供骨）
# ===========================================================================
class ZZMINoChannelPartTests(_Fixture):
    """无通道部件 = 缓存里没有通道记录、且组级补算也拿不到通道骨的部件。

    实机结论（用户 2026-09-18）：把它留在门控里会把**有键部件的发布一起拖死**
    （症状 = 骨骼动画直接卡住）。因此它：
    - **不得**出现在任何门控表达式的 `$zz_ms_seen_*` / `$zz_ms_prev_*` 项里；
    - **必须**保留 `ResourceZZPalette_<ib>_s<k> = copy vs-t0` 与 attach `run`（供骨）。
    """

    def _gate_lines(self, text):
        """取出所有门控表达式的行（`if <cond>` 里带 seen/prev 的行）。"""
        return [
            line
            for line in text.split("\n")
            if line.lstrip().startswith("if ")
            and ("$zz_ms_seen_" in line or "$zz_ms_prev_" in line)
        ]

    def _build_without_channel_for(self, draw_ib):
        exporter, models = self._build()
        self._apply_plan(exporter)
        for component in exporter.merged_skeleton_components:
            if component["draw_ib"] == draw_ib:
                component["channel"] = {}
        return exporter, models

    def test_channel_less_part_is_absent_from_every_gate_expression(self):
        exporter, models = self._build_without_channel_for(self.CARRIER_IB)
        component_id = exporter.merged_skeleton_component_id_dict[self.CARRIER_IB]
        vb_text = self._vb_text(exporter, models)
        gates = self._gate_lines(vb_text)
        self.assertTrue(gates, "夹具必须真的产出门控表达式")
        for line in gates:
            self.assertNotIn(f"$zz_ms_seen_{component_id}1", line)
            self.assertNotIn(f"$zz_ms_seen_{component_id}2", line)
            self.assertNotIn(f"$zz_ms_prev_{component_id}1", line)
            self.assertNotIn(f"$zz_ms_prev_{component_id}2", line)
        # 有键部件的门控项仍然在（撤销只针对无通道部件）
        self.assertTrue(
            any("$zz_ms_seen_0" in line or "$zz_ms_seen_2" in line for line in gates)
        )

    def test_channel_less_part_keeps_palette_capture_and_attach(self):
        exporter, models = self._build_without_channel_for(self.CARRIER_IB)
        component_id = exporter.merged_skeleton_component_id_dict[self.CARRIER_IB]
        vb_text = self._vb_text(exporter, models)
        # 供骨：palette 捕获仍在（两槽各一条）
        for slot in (1, 2):
            self.assertIn(
                f"ResourceZZPalette_{self.CARRIER_IB}_s{slot} = copy vs-t0 unless_null",
                vb_text,
            )
        # 供骨：attach 仍在（两槽各一条 run）
        for slot in (1, 2):
            self.assertIn(
                f"run = CustomShaderZZMIMergedSkeletonAttach_C{component_id}_s{slot}",
                vb_text,
            )

    def test_channel_less_part_is_named_in_diagnostics(self):
        exporter, models = self._build_without_channel_for(self.CARRIER_IB)
        vb_text = self._vb_text(exporter, models)
        self.assertIn("; ZZMI-MERGE-DIAG POSE_ALIGNMENT_UNAVAILABLE", vb_text)
        self.assertIn("reason=no_channel_plan", vb_text)
        self.assertIn(f"draw_ib={self.CARRIER_IB}", vb_text)
        codes = [record.get("code") for record in exporter._merged_diag_sink()]
        self.assertIn("POSE_ALIGNMENT_UNAVAILABLE", codes)
        # 无通道部件**自己**不发键块（不参与判定）。其余两件是跨部件共享通道骨
        # ⇒ 它们的键块照旧（所以断言必须限定在本部件自己那一段里）。
        carrier_block = vb_text.split("; a23aa8a3")[0]
        self.assertIn(f"ResourceZZPalette_{self.CARRIER_IB}_s1", carrier_block)
        self.assertNotIn("$zz_ms_pose_key_", carrier_block)
        self.assertNotIn("ResourceZZPoseKeySrc", carrier_block)

    def test_degenerate_channel_part_is_supply_only_like_a_missing_channel(self):
        """人造「无通道部件」：分量内没有共享骨（``8c8de427`` 形态）同样只供骨。

        与「缓存缺失」必须**同样**处置：不发键块、其 `seen/prev` 不出现在任何门控
        表达式里（用户实机确认：把它留在门控里会让有键部件的发布一起拖死），
        但 palette 捕获与 attach `run` 必须保留（它是自己那些槽位的唯一写入者）。
        """
        exporter, models = self._build()
        self._apply_plan(exporter)
        carrier = None
        for component in exporter.merged_skeleton_components:
            if component["draw_ib"] == self.CARRIER_IB:
                # 槽位搬到 900+：与组内任何件都不共骨 ⇒ 退化候选
                component["vg_map"] = {
                    local: 900 + local for local in range(len(component["vg_map"]))
                }
                carrier = component
            component.pop("channel", None)
        plan = CHANNEL.select_channel_plan(exporter.merged_skeleton_components)
        for component in exporter.merged_skeleton_components:
            component["channel"] = dict(plan[component["draw_ib"]])
        self.assertIsNotNone(carrier)
        self.assertEqual(
            carrier["channel"]["channel_reason"], "no_shared_bone_in_component"
        )
        self.assertFalse(CHANNEL.is_cross_part_channel(carrier["channel"]))
        # 其余两件仍然共骨（槽 79）⇒ 仍是键驱动
        self.assertTrue(
            CHANNEL.is_cross_part_channel(
                exporter.merged_skeleton_components[
                    exporter.merged_skeleton_component_id_dict[self.TARGET_IB]
                ]["channel"]
            )
        )

        component_id = exporter.merged_skeleton_component_id_dict[self.CARRIER_IB]
        vb_text = self._vb_text(exporter, models)
        # ① 供骨保留：两槽 palette 捕获 + 两槽 attach run
        for slot in (1, 2):
            self.assertIn(
                f"ResourceZZPalette_{self.CARRIER_IB}_s{slot} = copy vs-t0 unless_null",
                vb_text,
            )
            self.assertIn(
                f"run = CustomShaderZZMIMergedSkeletonAttach_C{component_id}_s{slot}",
                vb_text,
            )
        # ② 判定摘掉：**到达项（期望集合）**里绝不含它 —— 该形态一定带
        # `$zz_ms_prev_<i><k>`；用户实机确认的"卡住"就是这一项造成的。
        # 唯一允许出现的是「SO 别名本槽当帧已刷新」项 `($zz_ms_seen_<i><k> >= 1)`
        #（它不是身份判定，而是别名新鲜度标记；摘掉它会重现 2026-09-17 的
        # "当帧正确写入被丢掉"，见 `_merged_so_ready_capturer_ids`）。
        gates = self._gate_lines(vb_text)
        self.assertTrue(gates)
        for line in gates:
            self.assertNotIn(f"$zz_ms_prev_{component_id}1", line)
            self.assertNotIn(f"$zz_ms_prev_{component_id}2", line)
            if f"$zz_ms_seen_{component_id}1" in line:
                self.assertIn(f"($zz_ms_seen_{component_id}1 >= 1)", line)
            if f"$zz_ms_seen_{component_id}2" in line:
                self.assertIn(f"($zz_ms_seen_{component_id}2 >= 1)", line)
        # ③ 显式点名（绝不静默）
        self.assertIn("; ZZMI-MERGE-DIAG POSE_KEY_HASH_UNAVAILABLE", vb_text)
        self.assertIn(f"draw_ib={self.CARRIER_IB}", vb_text)
        self.assertIn(
            "no_shared_bone_in_component",
            [line for line in vb_text.split("\n") if f"draw_ib={self.CARRIER_IB}" in line][0],
        )
        # ④ 它自己那段不发键块
        carrier_block = vb_text.split("; a23aa8a3")[0]
        self.assertNotIn("$zz_ms_pose_key_", carrier_block)

    def test_gate_is_never_left_empty_when_every_part_has_no_channel(self):
        """退化保护：整组都没有通道时，门控不得变成 `if `（空条件）。"""
        exporter, models = self._build()
        self._apply_plan(exporter)
        for component in exporter.merged_skeleton_components:
            component["channel"] = {}
        vb_text = self._vb_text(exporter, models)
        for line in vb_text.split("\n"):
            self.assertNotEqual(line.rstrip(), "if", "空门控条件必须被兜底")
            self.assertNotEqual(line.rstrip(), "if ")
        self.assertTrue(self._gate_lines(vb_text))


# ===========================================================================
# 7：单连通组（1 个连通分量）与旧行为等价
# ===========================================================================
class ZZMISingleComponentEquivalenceTests(_Fixture):
    """单连通组：旧产物形态（两槽各捕获一次、attach 全发、Present 清场）不变。

    唯一差异是**捕获路径**：旧版按 `occ` 选槽（`if occ == 1 → s1 else → s2`），
    新版按精确键选槽（`if pool[key] == 2 → s2 else → s1`）。两者对"同部件两实例
    分占两槽"给出**同一结果**（一个键落 s1、另一个键落 s2），而新版不再依赖
    出图先后 —— 这正是本次变更的目的。
    """

    def test_two_slots_are_each_captured_once_per_part(self):
        exporter, models = self._build()
        self._apply_plan(exporter)
        text = self._vb_text(exporter, models)
        for draw_ib in (self.TARGET_IB, self.CARRIER_IB, self.SIBLING_IB):
            for slot in (1, 2):
                self.assertEqual(
                    text.count(
                        f"ResourceZZPalette_{draw_ib}_s{slot} = copy vs-t0 unless_null"
                    ),
                    1,
                    f"{draw_ib} s{slot} 必须恰好捕获一次",
                )

    def test_attach_runs_are_unconditional_and_cover_every_component_slot(self):
        exporter, models = self._build()
        self._apply_plan(exporter)
        text = self._vb_text(exporter, models)
        for component_id in range(len(exporter.merged_skeleton_components)):
            for slot in (1, 2):
                self.assertIn(
                    f"run = CustomShaderZZMIMergedSkeletonAttach_C{component_id}_s{slot}",
                    text,
                )

    def test_present_resets_and_sticky_markers_are_intact(self):
        exporter, models = self._build()
        self._apply_plan(exporter)
        skeleton_text = self._skeleton_text(exporter, models)
        constants_text, present_text = skeleton_text.split("[Present]")[0], skeleton_text.split("[Present]")[1]
        for component_id in range(len(exporter.merged_skeleton_components)):
            occ = f"$zz_ms_occ_{component_id}"
            self.assertIn(f"global {occ} = 0", constants_text)
            self.assertIn(f"{occ} = 0", present_text)
            for slot in (1, 2):
                seen = f"$zz_ms_seen_{component_id}{slot}"
                prev = f"$zz_ms_prev_{component_id}{slot}"
                self.assertIn(f"global {seen} = 0", constants_text)
                self.assertIn(f"global {prev} = 1", constants_text)
                self.assertIn(f"{prev} = {seen}", present_text)
                self.assertIn(f"{seen} = 0", present_text)

    def test_guard_predicate_is_monotone_ge_one(self):
        """守卫谓词仍是 `seen >= 1`（帧内单调，最后一次闭合落在最后一个必需部件）。"""
        exporter, models = self._build()
        self._apply_plan(exporter)
        text = self._vb_text(exporter, models)
        self.assertNotRegex(text, r"\$zz_ms_seen_\d+ >= 2")
        self.assertRegex(text, r"\$zz_ms_seen_\d+ >= 1")

    # ---------------------------------------------------------------- v9 基线
    def test_v9_baseline_invariants_survive_the_repair(self):
        """v9 基线（用户实机验证过的重定向路径形态）在本次修复后**逐项不变**。

        复核报告 §5 的读数必须仍然成立（TITLE: `run` 每 (部件,槽) 一条顶层无条件、
        段名集合 26、`override_vertex_count` 与 `so_prefix_rows` 不变、渲染段不覆写
        `vb0`）。本用例是对这些读数的**可执行**版本，防止后续改动悄悄回退。
        """
        exporter, models = self._build()
        self._apply_plan(exporter)
        vb_text = self._vb_text(exporter, models)
        skeleton_text = self._skeleton_text(exporter, models)

        # ① attach run：每 (部件,槽) 在**每份 VB 段**各一条，且全部在**第 0 列**
        #    （顶层无条件）。夹具 3 个模型 × 3 部件 × 2 槽 = 18。
        attach_runs = [
            line
            for line in vb_text.split("\n")
            if line.startswith("run = CustomShaderZZMIMergedSkeletonAttach_")
        ]
        self.assertEqual(
            len(attach_runs),
            18,
            "3 个模型 × 3 部件 × 2 槽 = 18 条顶层无条件 attach run",
        )
        for component_id in range(3):
            for slot in (1, 2):
                self.assertIn(
                    f"run = CustomShaderZZMIMergedSkeletonAttach_C{component_id}_s{slot}",
                    attach_runs,
                )
        # 没有任何 attach run 被缩进（进 if 体的 run 在本 fork 不执行）
        for line in vb_text.split("\n"):
            self.assertFalse(
                line.startswith("    run = CustomShaderZZMIMergedSkeletonAttach_"),
                "attach run 绝不进 if 体",
            )
        # ② 段名集合：per-(部件,槽) attach 段齐备
        for component_id in range(3):
            for slot in (1, 2):
                self.assertIn(
                    f"[CustomShaderZZMIMergedSkeletonAttach_C{component_id}_s{slot}]",
                    skeleton_text,
                )
        # ③ 渲染段绝不覆写 vb0（几何来自合并骨架，覆写会整块错位）
        self.assertNotIn("vb0 = ResourceZZRedirectSO", vb_text)
        self.assertNotIn("vb0 = ResourceZZRedirectSO", skeleton_text)
        # ④ 每个部件在**所有模型**里的 palette 捕获次数受控（槽位语义不变）：
        #    非重定向 target 的部件在它自己的 VB 段里捕获一次；target a23aa8a3
        #    的捕获由它自己的段（或载体转发）承担 —— 只断言"至少 1 次且不重复
        #    成对互搬"（emit_move 已在别处钉住）。
        for draw_ib in (self.TARGET_IB, self.CARRIER_IB, self.SIBLING_IB):
            for slot in (1, 2):
                self.assertGreaterEqual(
                    vb_text.count(
                        f"ResourceZZPalette_{draw_ib}_s{slot} = copy vs-t0 unless_null"
                    ),
                    1,
                    f"{draw_ib} s{slot} 必须至少被捕获一次（供骨不可丢）",
                )


# ===========================================================================
# F1（复核发现·高）：直连路径**不得**发键块
# ===========================================================================
class ZZMIDirectPathNoPoseKeyTests(_Fixture):
    """F1：直连路径（本组无重定向计划）的绘制决策是**按出现次**的
    （`_append_merged_direct_slot_guards` 的自足挂点 `if $zz_ms_occ_<i> == <slot>`
    → 绑本槽骨架）。若还发键块，palette 会被写进 `$PoolZZMISlotOfKey_G<g>[key]`
    算出的槽，而绘制读 `occ` 槽 ⇒ `pool[key] != occ` 时用**另一实例/上一帧**的
    骨架画本部件几何。

    可证伪性：HEAD 直连路径 0 个键块（`group_plan is not None` 挡住整个对齐块）；
    本用例把重定向计划清空后落到同一条直连分支，断言产物同样 0 个键块——
    修复前实测 6 次 `ResourceZZPoseKeySrc` / 1 个 `global $zz_ms_pose_key_` / 1 个池。
    """

    def _direct_path_texts(self):
        exporter, models = self._build()
        # 落到 `group_plan is None` 的直连分支（用户关掉
        # `zzmi_merged_redirect_enabled` 时就是这条路径）。
        exporter._redirect_carrier_map = {}
        exporter._redirect_target_map = {}
        return exporter, models, self._vb_text(exporter, models), self._skeleton_text(
            exporter, models
        )

    def test_direct_path_emits_no_pose_key_block(self):
        exporter, _models, vb_text, skeleton_text = self._direct_path_texts()
        # ① 键块整体不发
        self.assertNotIn("ResourceZZPoseKeySrc", vb_text)
        self.assertNotIn("HashRegion(", vb_text)
        self.assertNotIn("$zz_ms_pose_key_", vb_text)
        # ② 不得留下悬挂的全局键变量 / 空池段（发了没有任何消费者）
        self.assertNotIn("$zz_ms_pose_key_", skeleton_text)
        self.assertNotIn("global $zz_ms_pose_key_", skeleton_text)
        self.assertNotIn("PoolZZMISlotOfKey_", skeleton_text)
        self.assertNotIn("PoolZZMIG_Taken_", skeleton_text)
        # ③ 组内"参与判定"的部件集合为**空** ⇒ 全部按键出现次捕获
        #    （`key_driven_capture` 为假的两条结果路径都发 palette 捕获）
        for draw_ib in (self.TARGET_IB, self.CARRIER_IB, self.SIBLING_IB):
            for slot in (1, 2):
                self.assertIn(
                    f"ResourceZZPalette_{draw_ib}_s{slot} = copy vs-t0 unless_null",
                    vb_text,
                    f"{draw_ib} s{slot} 的按出现次捕获必须保留",
                )
        # ④ 绘制决策仍是出现次（未被改成键驱动）
        self.assertIn("$zz_ms_occ_0 = $zz_ms_occ_0 + 1", vb_text)
        self.assertTrue(
            any(
                line.strip().startswith("if $zz_ms_occ_") and line.rstrip().endswith("== 1")
                for line in vb_text.split("\n")
            ),
            "直连路径的自足挂点必须仍是出现次驱动",
        )
        # ⑤ 显式诊断：不可达原因 = 没有重定向计划（绝不静默）
        self.assertIn("; ZZMI-MERGE-DIAG POSE_ALIGNMENT_UNAVAILABLE", vb_text)
        self.assertIn("reason=no_redirect_plan", vb_text)

    def test_direct_path_pool_is_not_declared_without_key_block(self):
        """键池是「每组一条键变量」的消费者；没有键块就不该有池段。"""
        _exporter, _models, vb_text, skeleton_text = self._direct_path_texts()
        self.assertNotIn("[PoolZZMISlotOfKey_G7]", skeleton_text)
        self.assertNotIn("pool_index_type = fifo", skeleton_text)
        self.assertNotIn("$zz_ms_pose_key_7", vb_text + skeleton_text)


# ===========================================================================
# F2（复核发现·高）：链式连通分量内必须选**同一根**通道骨（或整体退化）
# ===========================================================================
class ZZMIChainedComponentChannelTests(unittest.TestCase):
    """F2：``A-1-B-2-C`` 链式分量（A={slot1}、B={slot1,slot2}、C={slot2}）。

    分量是「共享任意 token」的**传递闭包**，但通道骨按**逐件自己的** token 选。
    旧实现下 A→slot1、C→slot2，两者都 `cross_part=True` ⇒ 键读的是**两根不同
    物理骨** ⇒ 同实例算出不同键 ⇒ 落进不同池槽（＝本次变更要消灭的病）。

    可证伪断言：
    ① 分量内**没有任何一根骨**被全体成员引用 ⇒ 全体必须退化为「只供骨」
       （`is_cross_part_channel` 全为假），不得任何一件 `cross_part=True`；
    ② 退化记录的 `channel_shared_components` 口径一致（= 1，不是槽号统计）；
    ③ 正常形态（G2：一根骨被全体引用）仍必须判为共享 —— 防止「一刀切退化」。
    """

    def _chain(self):
        return [
            {"draw_ib": "aaaa0001", "vg_map": {0: 1, 1: 7}, "vg_count": 2,
             "vg_offset": 1, "skeleton_group": 0},
            {"draw_ib": "bbbb0002", "vg_map": {0: 1, 1: 2}, "vg_count": 2,
             "vg_offset": 1, "skeleton_group": 0},
            {"draw_ib": "cccc0003", "vg_map": {0: 2, 1: 9}, "vg_count": 2,
             "vg_offset": 2, "skeleton_group": 0},
        ]

    def test_chain_component_is_one_partition_with_incomparable_candidates(self):
        components = self._chain()
        partitions = CHANNEL.shared_bone_components(components)
        self.assertEqual(partitions, [[0, 1, 2]], "链式分量必须是一个连通分量")
        # 逐件候选：A 只能选 slot1、C 只能选 slot2 ⇒ 没有任何一根骨覆盖全体
        candidates_a = CHANNEL.member_channel_candidates(components, [0, 1, 2], 0)
        candidates_c = CHANNEL.member_channel_candidates(components, [0, 1, 2], 2)
        self.assertEqual([item[1] for item in candidates_a], [1])
        self.assertEqual([item[1] for item in candidates_c], [2])
        self.assertLess(
            max(item[3] for item in candidates_a), 3,
            "A 的候选引用数必须 < 分量成员数（3）",
        )

    def test_chain_component_members_are_all_supply_only_never_cross_part(self):
        components = self._chain()
        plan = CHANNEL.select_channel_plan(components)
        for draw_ib in ("aaaa0001", "bbbb0002", "cccc0003"):
            record = plan[draw_ib]
            self.assertFalse(
                CHANNEL.is_cross_part_channel(record),
                f"{draw_ib} 在链式分量里不得被判为跨部件共享（键不可比）",
            )
            self.assertEqual(
                record["channel_reason"], "no_shared_bone_in_component", draw_ib
            )
            # F3：口径一致 —— 退化记录的共享件数必须是 1（只对本部件两实例可用）
            self.assertEqual(record["channel_shared_components"], 1, draw_ib)
            self.assertEqual(record["channel_static_root"], False, draw_ib)
            self.assertIn("全体成员", record["channel_diagnostic"], draw_ib)

    def test_fully_covered_component_is_still_shared(self):
        """反向可证伪：一根骨被全体引用时**必须**判共享（不得一刀切退化）。"""
        components = [
            {"draw_ib": "aaaa0001", "vg_map": {0: 1, 1: 7}, "vg_count": 2,
             "vg_offset": 1, "skeleton_group": 0},
            {"draw_ib": "bbbb0002", "vg_map": {0: 1, 1: 7}, "vg_count": 2,
             "vg_offset": 1, "skeleton_group": 0},
            {"draw_ib": "cccc0003", "vg_map": {0: 1, 4: 7}, "vg_count": 2,
             "vg_offset": 1, "skeleton_group": 0},
        ]
        plan = CHANNEL.select_channel_plan(components)
        slots = {record["channel_slot"] for record in plan.values()}
        self.assertEqual(slots, {1}, "全覆盖分量必须选同一个通道槽位")
        for draw_ib in ("aaaa0001", "bbbb0002", "cccc0003"):
            self.assertTrue(CHANNEL.is_cross_part_channel(plan[draw_ib]), draw_ib)
            self.assertEqual(plan[draw_ib]["channel_shared_components"], 3, draw_ib)

    def test_g2_single_channel_invariant_holds_for_all_members(self):
        """不变式：同一连通分量内全体 key-driven 件必须共用一个通道槽位。"""
        components = [
            {"draw_ib": draw_ib, "vg_map": vg_map, "vg_count": len(vg_map),
             "vg_offset": min(vg_map.values()), "skeleton_group": 0}
            for draw_ib, vg_map in G2_VG_MAPS.items()
        ]
        plan = CHANNEL.select_channel_plan(components)
        cross_part_slots = {
            record["channel_slot"]
            for record in plan.values()
            if CHANNEL.is_cross_part_channel(record)
        }
        self.assertEqual(
            len(cross_part_slots), 1, "同分量内 key-driven 件必须共用一个通道槽位"
        )


if __name__ == "__main__":
    unittest.main()
