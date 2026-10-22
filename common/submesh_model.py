from dataclasses import dataclass, field
import numpy

from .draw_call_model import DrawCallModel

from ..utils.export_utils import ExportUtils
from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.timer_utils import TimerUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from .logic_name import LogicName
from .merge_weld import compute_weld_groups
from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .d3d11_gametype import D3D11GameType
from .submesh_metadata import SubmeshMetadataResolver
from ..blueprint.export_helper import BlueprintExportHelper


import bpy
import math
import array
import hashlib
import os
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter


# 导出期跨分块焊接的临时分块标记：
# - INT 顶点属性用于 join 后读取分块号（首选）；
# - 临时顶点组作为 join 丢失自定义属性时的退化标记（焊接后删除）。
WELD_BLOCK_ATTR = "_ssmt_weld_block"
WELD_BLOCK_GROUP_PREFIX = "__SSMT_WELD_BLOCK_"


@dataclass
class SubMeshModel:
    # 初始化时需要填入此属性
    drawcall_model_list:list[DrawCallModel] = field(default_factory=list)
    source_obj_unique_str_count:dict[str, int] = field(default_factory=dict, repr=False)
    has_multi_file_export_nodes:bool = False
    # 导出期跨分块边界顶点焊接规格：None = 不焊接
    # {"object_names": set[str], "weld_distance": float}
    weld_spec:dict = field(default=None, repr=False)

    # post_init中计算得到这些属性
    match_draw_ib:str = field(init=False, default="")
    match_first_index:int = field(init=False, default=-1)
    match_index_count:int = field(init=False, default=-1)
    match_cs:str = field(init=False, default="")
    match_uav_bytes:int = field(init=False, default=0)
    unique_str:str = field(init=False, default="")
    workspace_unique_str:str = field(init=False, default="")

    # 调用组合obj并计算ib和vb得到这些属性
    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    # EFMI 骨骼合并升宽标记：BLENDINDICES 已从 8 位升到 16 位（导出 INI 需输出 ElementFormat 行）
    blendindices_widened:bool = field(init=False, default=False)
    # EFMI 骨骼合并元数据（由 SubmeshMetadata 透传）
    vg_offset:int = field(init=False, default=0)
    vg_count:int = field(init=False, default=0)
    vg_map_algorithm_version:int = field(init=False, default=0)
    merged_skeleton_metadata_valid:bool = field(init=False, default=True)
    # EFMI 跨 LOD 对应账本（v9 投影写回；非基准 LOD 子网格记录与基准部件的关系）
    efmi_lod_reference_component:str = field(init=False, default="")
    efmi_lod_correspondence:dict = field(init=False, default_factory=dict)
    efmi_lod_projection:bool = field(init=False, default=False)
    efmi_lod_layout_version:int = field(init=False, default=0)
    # ZZMI 骨架分组号（渲染 cb1 对象变换配对；缺省 0 = 单骨架旧语义）
    skeleton_group:int = field(init=False, default=0)
    # ZZMI 逐部件 VGMap（局部骨骼 id -> 全局槽位；attach CS 按此写合并骨架）
    vg_map:dict = field(init=False, default_factory=dict)
    # ZZMI 导出侧守卫元数据（反查写回；缺省 0）：
    # deform pass draw 序号（合并网格时序校验）+ 原部件顶点数（渲染 vb1 换绑判定）
    deform_draw_index:int = field(init=False, default=0)
    original_vertex_count:int = field(init=False, default=0)

    # 读取工作空间中的 Import.json 选择数据类型目录，再从对应的 SubmeshJson 获取 d3d11GameType
    d3d11_game_type:D3D11GameType = field(init=False,repr=False,default=None)

    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 
    shape_key_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    unique_first_loop_indices:numpy.ndarray = field(init=False,repr=False,default=None)
    object_export_context_map:dict = field(init=False,repr=False,default_factory=dict)

    def __post_init__(self):

        # 因为列表里的每个DrawCallModel的draw_ib,first_index,index_count都是一样的，所以直接取第一个就行了
        if len(self.drawcall_model_list) > 0:
            self.match_draw_ib = self.drawcall_model_list[0].match_draw_ib
            self.match_first_index = self.drawcall_model_list[0].match_first_index
            self.match_index_count = self.drawcall_model_list[0].match_index_count
            self.workspace_unique_str = self.drawcall_model_list[0].get_workspace_unique_str()
            self.unique_str = self.workspace_unique_str or self.drawcall_model_list[0].get_unique_str()

        self.calc_buffer()
    

    def calc_buffer(self):
        folder_name = self.workspace_unique_str or self.unique_str

        submesh_metadata = SubmeshMetadataResolver.resolve(folder_name)
        self.d3d11_game_type = submesh_metadata.d3d11_game_type
        self.match_cs = getattr(submesh_metadata, "match_cs", "") or ""
        self.match_uav_bytes = int(getattr(submesh_metadata, "match_uav_bytes", 0) or 0)
        # EFMI 骨骼合并元数据（反查写回；无则 0/空，合并段输出时据此校验）
        self.vg_offset = int(getattr(submesh_metadata, "vg_offset", 0) or 0)
        self.vg_count = int(getattr(submesh_metadata, "vg_count", 0) or 0)
        self.vg_map_algorithm_version = int(
            getattr(submesh_metadata, "vg_map_algorithm_version", 0) or 0
        )
        # t75 通道计划（唯一判定口径）：导入期算好并缓存，导出侧直接消费。
        self.channel_plan_version = int(
            getattr(submesh_metadata, "channel_plan_version", 0) or 0
        )
        self.channel_plan = dict(getattr(submesh_metadata, "channel_plan", {}) or {})
        self.channel_plan_digest = str(
            getattr(submesh_metadata, "channel_plan_digest", "") or ""
        )
        self.channel_plan_slot_weights = dict(
            getattr(submesh_metadata, "channel_plan_slot_weights", {}) or {}
        )
        self.merged_skeleton_metadata_valid = bool(
            getattr(submesh_metadata, "merged_skeleton_metadata_valid", True)
        )
        # EFMI 跨 LOD 对应账本（v9 投影写回；导出侧据此把 LOD 版本合并进同一逻辑部件）
        self.efmi_lod_reference_component = str(
            getattr(submesh_metadata, "efmi_lod_reference_component", "") or ""
        )
        self.efmi_lod_correspondence = dict(
            getattr(submesh_metadata, "efmi_lod_correspondence", {}) or {}
        )
        self.efmi_lod_projection = bool(
            getattr(submesh_metadata, "efmi_lod_projection", False)
        )
        self.efmi_lod_layout_version = int(
            getattr(submesh_metadata, "efmi_lod_layout_version", 0) or 0
        )
        # ZZMI 骨架分组号（渲染 cb1 对象变换配对；缺省 0 = 单骨架旧语义）
        self.skeleton_group = int(getattr(submesh_metadata, "skeleton_group", 0) or 0)
        # ZZMI 逐部件 VGMap（局部骨骼 id -> 全局槽位；attach CS 按此写合并骨架）
        self.vg_map = dict(getattr(submesh_metadata, "vg_map", {}) or {})
        # ZZMI 导出侧守卫元数据（deform draw 序号 / 原部件顶点数）
        self.deform_draw_index = int(getattr(submesh_metadata, "deform_draw_index", 0) or 0)
        self.original_vertex_count = int(getattr(submesh_metadata, "original_vertex_count", 0) or 0)

        # EFMI 骨骼合并：只有 GPU-PreSkinning 子网格才有统一骨架和
        # BLENDINDICES。CPU-PreSkinning 子网格在 ensure_skeleton_data 阶段
        # 会被明确跳过，必须继续走普通局部组导出，不能因为同一批次开启
        # 复选框就强制要求它存在 BLENDINDICES。
        self.blendindices_widened = False
        if (
            self.d3d11_game_type is not None
            and GlobalConfig.logic_name == LogicName.EFMI
            and GlobalProterties.import_merged_vgmap()
            and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
        ):
            self.blendindices_widened = self.d3d11_game_type.widen_blendindices()
            blend_layouts = self.d3d11_game_type.get_blendindices_layouts()
            if not blend_layouts:
                raise RuntimeError(
                    f"[EFMI骨骼合并] {self.unique_str} 的 GameType 不含 BLENDINDICES"
                )
            narrow_layouts = [
                (semantic_index, fmt)
                for semantic_index, fmt, _extract_slot in blend_layouts
                if str(fmt).upper().startswith("R8")
            ]
            if narrow_layouts:
                raise RuntimeError(
                    f"[EFMI骨骼合并] {self.unique_str} 的 BLENDINDICES 升宽未完成: "
                    f"{narrow_layouts}"
                )

        TimerUtils.start_stage("数据哈希预计算")
        object_hashes, source_obj_list = self._precompute_object_hashes()
        TimerUtils.end_stage("数据哈希预计算")
        
        # 获取每个对象的原始名称（用于判断是否来自同一个物体）
        # 只有来自同一个原始物体的分裂物体才能复用
        original_names = []
        for draw_call_model in self.drawcall_model_list:
            source_name = draw_call_model.source_obj_name
            normalized_source_name = self._normalize_source_name(source_name)
            if normalized_source_name:
                original_names.append(normalized_source_name)
                continue

            fallback_name = draw_call_model.get_blender_obj_name()
            original_names.append(self._normalize_source_name(fallback_name) or fallback_name)

        # 调试输出：打印所有对象的哈希值和原始名称
        print(f"[SubMeshModel] 数据哈希预计算完成: {len(self.drawcall_model_list)} 个对象")
        hash_groups = {}
        none_hash_names = []
        for i, h in enumerate(object_hashes):
            obj_name = self.drawcall_model_list[i].get_blender_obj_name()
            orig_name = original_names[i]
            if h is None:
                none_hash_names.append(obj_name)
                continue
            # 使用 (hash, original_name) 作为分组 key
            group_key = (h, orig_name)
            if group_key not in hash_groups:
                hash_groups[group_key] = []
            hash_groups[group_key].append(obj_name)
        if none_hash_names:
            print(f"  ⚠️ 无法计算哈希（对象未找到）: {', '.join(none_hash_names)}")
        for (h, orig_name), names in hash_groups.items():
            if len(names) > 1:
                print(f"  📋 哈希 {h[:16]}... (原始: {orig_name}) → {len(names)} 个对象可复用: {', '.join(names)}")
            else:
                print(f"  📋 哈希 {h[:16]}... (原始: {orig_name}) → 独立对象: {names[0]}")

        index_offset = 0
        submesh_temp_obj_list = []
        data_hash_cache = {}
        cache_key_to_geometry_record = {}
        cache_key_to_candidate_names = {}

        # 跨分块边界顶点焊接：规格来自蓝图节点（weld_spec），只作用于导出临时副本
        weld_spec = self.weld_spec if isinstance(self.weld_spec, dict) else None
        weld_object_names = set(weld_spec.get("object_names") or ()) if weld_spec else set()
        try:
            weld_distance = (
                float(weld_spec.get("weld_distance", 1e-6)) if weld_spec else 0.0
            )
        except (TypeError, ValueError):
            weld_distance = 0.0
        weld_enabled = bool(weld_object_names) and weld_distance > 0.0
        weld_block_counter = 0
        # block id -> DrawCallModel：焊接后按分块重算 vertex_count 用
        weld_block_drawcalls = {}
        if weld_enabled:
            print(
                f"[合并顶点] {self.unique_str} 启用跨分块边界焊接: "
                f"物体={sorted(weld_object_names)}, 距离={weld_distance}"
            )

        reuse_count = 0
        direct_source_reuse_count = 0
        duplicated_temp_count = 0
        merged_obj_uses_preprocessed_copy = False
        copy_duration = 0.0
        join_duration = 0.0
        normalize_duration = 0.0
        rotate_duration = 0.0
        loop_offset = 0
        preserve_distinct_export_contexts = self._should_preserve_distinct_export_contexts()

        if preserve_distinct_export_contexts:
            print(f"[SubMeshModel] 直出 ShapeKey 场景: 保留独立导出上下文，禁用几何复用 {self.unique_str}")

        temp_collection = CollectionUtils.create_new_collection("TEMP_SUBMESH_COLLECTION_" + self.unique_str)
        bpy.context.scene.collection.children.link(temp_collection)

        TimerUtils.start_stage("对象处理与合并")

        need_normalize = self.d3d11_game_type is not None and "Blend" in self.d3d11_game_type.OrderedCategoryNameList
        need_rotate = ExportUtils.requires_export_space_transform(GlobalConfig.logic_name)

        for i, draw_call_model in enumerate(self.drawcall_model_list):
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = source_obj_list[i]

            obj_hash = object_hashes[i]
            if obj_hash is None:
                obj_hash = f"FALLBACK_{blender_obj_name}"
            original_name = original_names[i]
            cache_key = (obj_hash, original_name)
            # 焊接要求每个分块都有独立临时副本与分块标记，禁止数据哈希复用
            cached_result = (
                None
                if (preserve_distinct_export_contexts or weld_enabled)
                else data_hash_cache.get(cache_key)
            )

            # 【t81-F3 纵深防御】命中后**校验指纹**：几何不同（顶点数/索引数不同）⇒ 视为**未命中**。
            # 即使将来键仍然不足（例如又出现别的"同名同哈希不同几何"），也不会静默把别人的几何写进来。
            # 指纹取 `source_obj` 的真实尺寸（与 `:289-290` 写入缓存时的口径一致）。
            fingerprint_ok = True
            fingerprint_note = ""
            if cached_result is not None and source_obj is not None:
                try:
                    own_vertex_count = len(source_obj.data.vertices)
                    own_index_count = len(source_obj.data.polygons) * 3
                except Exception:  # noqa: BLE001 - 拿不到尺寸就不校验（保守起见按命中处理，但会打日志）
                    own_vertex_count = own_index_count = None
                if own_vertex_count is not None:
                    cached_offset_probe, cached_v_probe, cached_i_probe = cached_result
                    if own_vertex_count != cached_v_probe or own_index_count != cached_i_probe:
                        fingerprint_ok = False
                        fingerprint_note = (
                            f"指纹不符（自身 {own_vertex_count} 顶点/{own_index_count} 索引，"
                            f"缓存 {cached_v_probe}/{cached_i_probe}）⇒ 视为未命中"
                        )

            # 【t81 导出期日志】零假设钉死：命中/未命中 + 键 + 指纹 + 缓存账目（只打日志，不改行为）。
            # **只在 HIMI 打开**：这是 HIMI 合并导出的排查日志，其它逻辑保持改动前：不打。
            if GlobalConfig.logic_name == getattr(LogicName, "HIMI", "HIMI"):
                print(
                    f"[SubMeshModel][t81] draw_call={i} blender_obj='{blender_obj_name}' "
                    f"original='{original_name}' hash={str(obj_hash)[:16]} key={cache_key!r} "
                    f"preserve_distinct={preserve_distinct_export_contexts} "
                    f"hit={'yes' if cached_result is not None else 'no'} "
                    f"fingerprint_ok={fingerprint_ok} "
                    f"cached={cached_result if cached_result is not None else None} "
                    f"{fingerprint_note}"
                )

            if cached_result is not None and fingerprint_ok:
                cached_offset, cached_vertex_count, cached_index_count = cached_result
                draw_call_model.vertex_count = cached_vertex_count
                draw_call_model.index_count = cached_index_count
                draw_call_model.index_offset = cached_offset
                cache_key_to_candidate_names.setdefault(cache_key, set()).update(
                    self._build_drawcall_candidate_names(draw_call_model)
                )
                reuse_count += 1
                print(f"  ♻️复用: '{blender_obj_name}' (原始: {original_name}) → offset={cached_offset}, vertices={cached_vertex_count}, indices={cached_index_count}")
                continue
            if cached_result is not None and not fingerprint_ok:
                print(f"  ⚠️拒绝复用（t81-F3 指纹不符）: '{blender_obj_name}' (原始: {original_name}) "
                      f"→ 改用自己写几何；{fingerprint_note}")

            if source_obj is None:
                from ..utils.ssmt_error_utils import SSMTErrorUtils
                tried_names = [blender_obj_name]
                if draw_call_model.source_obj_name and draw_call_model.source_obj_name != blender_obj_name:
                    tried_names.append(draw_call_model.source_obj_name)
                if draw_call_model.obj_name and draw_call_model.obj_name not in tried_names:
                    tried_names.append(draw_call_model.obj_name)
                SSMTErrorUtils.raise_fatal(
                    f"找不到 Blender 对象: '{blender_obj_name}'"
                    f" (已尝试: {', '.join(tried_names)})"
                    f" — 请检查重命名节点是否正确配置"
                )

            print(f"  🆕 处理: '{blender_obj_name}' (原始: {original_name}, hash={obj_hash[:16]}...)")

            current_weld_block_id = 0
            if weld_enabled and blender_obj_name in weld_object_names:
                weld_block_counter += 1
                current_weld_block_id = weld_block_counter

            # 参与焊接的物体必须使用临时副本，绝不在源物体/源网格上写标记
            if self._should_duplicate_source_for_merge(source_obj) or current_weld_block_id > 0:
                copy_start = perf_counter()
                new_obj = source_obj.copy()
                new_obj.data = source_obj.data.copy()
                new_obj.name = source_obj.name + "_temp"
                temp_collection.objects.link(new_obj)
                temp_obj = new_obj
                duplicated_temp_count += 1
                copy_duration += perf_counter() - copy_start
            else:
                temp_obj = source_obj
                direct_source_reuse_count += 1

            if current_weld_block_id > 0:
                self._tag_weld_block(temp_obj, current_weld_block_id)
                weld_block_drawcalls[current_weld_block_id] = draw_call_model

            draw_call_model.vertex_count = len(temp_obj.data.vertices)
            draw_call_model.index_count = len(temp_obj.data.polygons) * 3
            draw_call_model.index_offset = index_offset
            loop_count = len(temp_obj.data.loops)

            data_hash_cache[cache_key] = (index_offset, draw_call_model.vertex_count, draw_call_model.index_count)
            cache_key_to_candidate_names.setdefault(cache_key, set()).update(
                self._build_drawcall_candidate_names(draw_call_model)
            )
            cache_key_to_geometry_record[cache_key] = {
                "loop_start": loop_offset,
                "loop_count": loop_count,
                "label": self.unique_str,
                "d3d11_game_type": self.d3d11_game_type,
                "preferred_source_name": self._resolve_preferred_source_name(source_obj, draw_call_model),
            }

            index_offset += draw_call_model.index_count
            self.vertex_count += draw_call_model.vertex_count
            self.index_count += draw_call_model.index_count
            loop_offset += loop_count

            submesh_temp_obj_list.append(temp_obj)

        total = len(self.drawcall_model_list)
        if reuse_count > 0:
            print(f"[SubMeshModel] 数据复用统计: {reuse_count}/{total} 个对象复用, {total - reuse_count}/{total} 个对象独立处理")
        else:
            print(f"[SubMeshModel] 数据复用统计: 无复用, 全部 {total} 个对象独立处理")
        print(f"[SubMeshModel] 合并输入统计: 直接复用前处理副本 {direct_source_reuse_count} 个, 额外复制临时物体 {duplicated_temp_count} 个")

        # ------------------------------------------------------------------ #
        # 【t83-A3】位置流插桩（**只打日志、不改行为**）—— **只在 HIMI 打开**：
        # 零假设钉死"行数多出来/位置写错"，是 HIMI 合并导出排查用的一次性诊断；
        # 留在通用路径上会让每个游戏的每次导出都刷屏。其它逻辑保持改动前：不打。
        # ------------------------------------------------------------------ #
        # 三态判据（配合 t82 的"产物 1321 行 = 源 1298 + 23"）：
        #   ① `draw_call_rows_sum != merged_verts` ⇒ **组装期就多/少行了**（位置流的行映射有问题）；
        #   ② 两者相等但产物仍错 ⇒ 错在**落盘之后**（插桩往后挪一层：`drawib_export_base` 写盘处）；
        #   ③ 每行的 `src_v` 与 IB 多数决不一致 ⇒ 写入点就是位置流（再看 `src_v` 是谁提供的）。
        if GlobalConfig.logic_name == getattr(LogicName, "HIMI", "HIMI"):
            try:
                _dc_rows = sum(int(getattr(d, "vertex_count", 0) or 0) for d in self.drawcall_model_list)
                _dc_index = sum(int(getattr(d, "index_count", 0) or 0) for d in self.drawcall_model_list)
                _merged_verts = None
                if submesh_temp_obj_list:
                    try:
                        _merged_verts = len(submesh_temp_obj_list[0].data.vertices)
                    except Exception:  # noqa: BLE001
                        _merged_verts = None
                print(
                    f"[t82-POS] part={self.unique_str} draw_calls={len(self.drawcall_model_list)} "
                    f"draw_call_rows_sum={_dc_rows} draw_call_index_sum={_dc_index} "
                    f"merged_verts={_merged_verts} "
                    f"rows_delta={None if _merged_verts is None else _dc_rows - _merged_verts} "
                    f"reuse={reuse_count} direct_reuse={direct_source_reuse_count} "
                    f"duplicated={duplicated_temp_count} "
                    f"offsets={[int(getattr(d, 'index_offset', -1) or -1) for d in self.drawcall_model_list]}"
                )
                if submesh_temp_obj_list:
                    _obj = submesh_temp_obj_list[0]
                    print(
                        f"[t82-POS]   merged_obj={getattr(_obj, 'name', '?')} "
                        f"verts={len(_obj.data.vertices)} polys={len(_obj.data.polygons)} "
                        f"loops={len(_obj.data.loops)} "
                        f"vgroups={len(getattr(_obj, 'vertex_groups', []) or [])}"
                    )
            except Exception as _exc:  # noqa: BLE001 - 插桩绝不许弄坏导出
                print(f"[t82-POS] 插桩异常（已忽略）：{_exc}")

        join_start = perf_counter()
        if submesh_temp_obj_list:
            valid_temp_obj_list = []
            for temp_obj in submesh_temp_obj_list:
                try:
                    if temp_obj is not None and temp_obj.name in bpy.data.objects:
                        valid_temp_obj_list.append(temp_obj)
                except ReferenceError:
                    continue
            if valid_temp_obj_list:
                if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
                    self._ensure_target_shape_key_union(valid_temp_obj_list[0], valid_temp_obj_list[1:])
                ObjUtils.join_objects_fast(valid_temp_obj_list[0], valid_temp_obj_list[1:])
            submesh_temp_obj_list = valid_temp_obj_list
        join_duration += perf_counter() - join_start

        if not submesh_temp_obj_list:
            from ..utils.ssmt_error_utils import SSMTErrorUtils
            SSMTErrorUtils.raise_fatal(f"SubMesh {self.unique_str} 没有有效的对象可供合并导出")

        submesh_merged_obj = submesh_temp_obj_list[0]
        merged_obj_uses_preprocessed_copy = submesh_merged_obj.name.endswith('_copy')
        if not merged_obj_uses_preprocessed_copy:
            merged_obj_name = "TEMP_SUBMESH_MERGED_" + self.unique_str
            ObjUtils.rename_object(submesh_merged_obj, merged_obj_name)

        # 跨分块边界顶点焊接：join 完成之后、顶点组预处理与写 buffer 之前
        if weld_enabled and weld_block_counter > 0:
            weld_stats = self._weld_cross_block_boundaries(
                submesh_merged_obj, weld_distance
            )
            block_reductions = (
                weld_stats.get("block_vertex_reductions", {}) if weld_stats else {}
            )
            applied_reduction = 0
            for block_id, reduction in sorted(block_reductions.items()):
                target_drawcall = weld_block_drawcalls.get(int(block_id))
                if target_drawcall is None:
                    continue
                reduction = int(reduction)
                if reduction <= 0:
                    continue
                old_count = int(target_drawcall.vertex_count)
                new_count = max(0, old_count - reduction)
                applied_reduction += old_count - new_count
                target_drawcall.vertex_count = new_count
            # 本 SubMesh 顶点数取焊接后 merged mesh 的实际顶点数
            self.vertex_count = int(len(submesh_merged_obj.data.vertices))
            recalculated_sum = sum(
                int(item.vertex_count) for item in self.drawcall_model_list
            )
            print(
                f"[合并顶点] {self.unique_str} 重算后顶点数: "
                f"drawcall 合计={recalculated_sum}, "
                f"merged mesh={self.vertex_count}, "
                f"分块减量合计={applied_reduction}"
            )

        if need_normalize:
            normalize_start = perf_counter()
            self._normalize_temp_obj_for_export(submesh_merged_obj)
            normalize_duration += perf_counter() - normalize_start

        if need_rotate:
            rotate_start = perf_counter()
            self._apply_export_rotation_for_logic(submesh_merged_obj)
            rotate_duration += perf_counter() - rotate_start

        TimerUtils.end_stage("对象处理与合并")

        # 骨骼合并导出前顶点组预处理（对齐参考插件 ObjectMerger 纪律）：
        # 使导出 blend 索引 = 全局骨骼 id（排序后 index == 数字组名）。
        # EFMI 无条件（复选框开即启用）；ZZMI 额外要求反查已写回（vg_count > 0），
        # 复选框关闭或无反查数据时完全保持旧逻辑。
        run_merged_skeleton_preprocess = False
        if GlobalProterties.import_merged_vgmap():
            if (
                GlobalConfig.logic_name == LogicName.EFMI
                and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
            ):
                run_merged_skeleton_preprocess = True
            elif GlobalConfig.logic_name == LogicName.ZZMI and self.vg_count > 0:
                run_merged_skeleton_preprocess = True

        # 双套顶点组导出转换（t9 方案 A：合并与非合并路径统一身份化）：
        # EFMI 去重对象（json 带 VGMap 合并元数据）在任一 EFMI 导出路径（合并骨架
        # 模式或非合并路径）下，先把合并槽更名回「源组中权重强度最强者」的非去重
        # 身份（Z 组更名），使导出 BLENDINDICES 落在原初身份命名空间——运行时全链
        # 本以身份（VGOffset+local）为键（t9 §2.1），VB 直写身份消除「引用 canonical
        # 槽」的跨组件间接层（F1 根治 + F4 族减轻）。
        # 更名只改名不排序；补缺/排序/断言 index==name 由下方各分支统一执行一次。
        dualset_eligible = (
            GlobalConfig.logic_name == LogicName.EFMI
            and self.vg_map_algorithm_version > 0
            and self.merged_skeleton_metadata_valid
            and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
        )
        if run_merged_skeleton_preprocess:
            if GlobalConfig.logic_name == LogicName.ZZMI:
                self._prepare_zzmi_merged_skeleton_vertex_groups(submesh_merged_obj)
            else:
                # EFMI 合并骨架模式：更名回非去重身份后再走既有预处理链。
                if dualset_eligible:
                    self._apply_dualset_export_rename(submesh_merged_obj)
                # 打包级幂等升宽：SubMeshModel 构造时可能因导入/导出状态差异未
                # 执行 widen，但**打包 dtype 必须与 INI 声明的 R16 全池布局一致**
                # ——否则顶点组读回的是全局骨骼 id（>255，如 596/372），被 uint8
                # 打包静默截断（596->84），运行时按 16 位读取 = 索引错位 = 权重爆炸。
                # widen_blendindices 幂等：已 R16 时直接返回 False，不重复修改。
                if (
                    GlobalProterties.import_merged_vgmap()
                    and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
                ):
                    self.d3d11_game_type.widen_blendindices()
                # EFMI 合并骨架的 BLENDINDICES 已统一归一化为 R16 系
                # （widen_blendindices），全局骨骼池必须落在 uint16 可承载范围，
                # 否则导出时 astype(uint16) 会把越界骨骼 id 截断错位。
                # 多 LOD 分段平移（2026-08-27）后，LOD1 部件的组名 = 全局骨骼 id
                # （基准段之后起算），可能远超其自身顶点组数量——上限必须按
                # "最大组名（全局骨骼 id）"判定，不能按组数量判定（本地组数
                # 自洽但全局 id 已越界时，旧检查会漏放并静默截断）。
                numeric_group_ids = [
                    int(vg.name)
                    for vg in submesh_merged_obj.vertex_groups
                    if re.fullmatch(r"[0-9]+", str(vg.name))
                ]
                max_global_bone_id = max(numeric_group_ids, default=-1)
                if max_global_bone_id > 65535:
                    raise RuntimeError(
                        f"[EFMI骨骼合并] {self.unique_str} 全局骨骼 id "
                        f"{max_global_bone_id} 超过 uint16 上限 "
                        "65535，BLENDINDICES 无法以 R16 承载，请拆分骨架或减小骨骼池"
                    )
                self._prepare_merged_skeleton_vertex_groups(submesh_merged_obj)
        elif dualset_eligible:
            # EFMI 去重对象走非合并/其它逻辑导出（原 D2-B 路径保持）：更名回
            # 非去重身份 + 复用预处理链一次成型。更名失败即中止（fail-closed）。
            self._apply_dualset_export_rename(submesh_merged_obj)
            # S2（t20）/ B1 真落地（t22）：非合并路径补 widen——更名后身份
            # ∈ [0, Σvg_count) 可能 >255，而本工作区 Blend 原生 R8G8B8A8_UINT；
            # 不升宽则 _parse_blendindices 的 R8 范围检查抛 Fatal
            # （obj_buffer_helper.py:430-465）→ 导出中断。
            # t22 移除 import_merged_vgmap()/GPU_PreSkinning 门控：elif 可达条件
            # （dualset_eligible 含 GPU_PreSkinning）使二者恒真/恒假（t21 §1.1：
            # elif ⟺ run_merged_skeleton_preprocess=False ⟹ checkbox False——
            # 门控挂复选框 = 死代码，widen 永不触发）。widen 幂等（已 R16 返
            # False），无条件调用多调无害；合并分支行为不变。
            self.d3d11_game_type.widen_blendindices()
            self._prepare_merged_skeleton_vertex_groups(submesh_merged_obj)

        obj_buffer_result = ExportUtils.build_unity_obj_buffer_result(
            obj=submesh_merged_obj,
            d3d11_game_type=self.d3d11_game_type,
        )
        self.ib = obj_buffer_result.ib
        self.category_buffer_dict = obj_buffer_result.category_buffer_dict
        self.index_vertex_id_dict = obj_buffer_result.index_loop_id_dict
        self.unique_first_loop_indices = obj_buffer_result.unique_first_loop_indices
        self.shape_key_buffer_dict = obj_buffer_result.shape_key_buffer_dict
        self.object_export_context_map = self._build_object_export_context_map(
            cache_key_to_geometry_record=cache_key_to_geometry_record,
            cache_key_to_candidate_names=cache_key_to_candidate_names,
        )

        # 计算完成后，删除临时对象
        if not merged_obj_uses_preprocessed_copy:
            bpy.data.objects.remove(submesh_merged_obj, do_unlink=True)

        if temp_collection.name in bpy.data.collections:
            if temp_collection.name in bpy.context.scene.collection.children:
                bpy.context.scene.collection.children.unlink(temp_collection)
            bpy.data.collections.remove(temp_collection)

        if merged_obj_uses_preprocessed_copy:
            print("SubMeshModel: " + self.unique_str + " 计算完成，复用的前处理副本保留到轮次清理阶段")
        else:
            print("SubMeshModel: " + self.unique_str + " 计算完成，临时对象已删除")
        print(
            f"[SubMeshModel] 阶段细分: copy={copy_duration:.3f}s, join={join_duration:.3f}s, "
            f"normalize={normalize_duration:.3f}s, rotate={rotate_duration:.3f}s"
        )

        self._deduplicate_draw_calls()

    def _apply_dualset_export_rename(self, obj):
        """双套顶点组导出转换（M1 挂载点，t3 规格 §2/§6.1）：合并槽更名回运行时身份。

        两边分离契约（t3 设计 §2/§3；身份映射权威实现对照任务书
        ``get_dualset_slot_identity_map`` 别名 = ``build_per_mesh_identity_map``）：
        BL 侧顶点组名 = 合并槽位号（去重池命名空间，**永不直接写盘**）；导出侧
        更名回运行时身份 = 本组件成员身份（``VGOffset + local`` 语义，per-mesh
        覆盖层，含未更名槽），随后写盘 BLENDINDICES 落在自己 attach 必写的
        自属声明段内。

        非合并导出路径（F1 主场景根治）：EFMI 去重对象（json 带 VGMap 合并元数据）
        在本次导出未走合并骨架预处理链时，把临时导出对象上的合并槽顶点组名
        （去重全局 id）更名回本组件成员身份（per-mesh 身份选择主判 = 顶点数
        vertex_count，2026-08-29 用户裁决「顶点组多的那一边」，见
        efmi_skeleton.select_dualset_export_identity），随后复用
        _prepare_merged_skeleton_vertex_groups 的「剔非数字 -> 补缺 -> 排序 ->
        断言 index==name」纪律一次成型。

        - 单源槽恒等（e(s)==s），无实际操作；
        - 合并槽且本组件成员 ≠ 原槽：更名（如槽 371 -> 身份 596）；
        - 身份唯一性定理（t3 §1.3）保证改名不会撞名：e(s)≠s 时 e(s) 必不在
          槽位集合内，也不与其它槽的导出身份重复；
        - 失败即中止导出（fail-closed，A1-A5 + FC-1：槽无本组件成员时
          build_per_mesh_identity_map 直接 RuntimeError，不静默回退槽位直写；
          写盘路径另有 FC-2 产物域断言兜底）；
        - 本方法只做「槽位名→身份名」更名；补缺/排序/断言 index==name 由调用方
          在各自分支统一执行一次（合并/非合并共用同一预处理纪律，防双重执行）。
        """
        if obj is None or obj.type != 'MESH':
            return
        if not obj.vertex_groups:
            return
        # 仅 EFMI 去重对象适用：有 VGMap 合并元数据（算法版本号 > 0）。
        if self.vg_map_algorithm_version <= 0 or not self.merged_skeleton_metadata_valid:
            return
        workspace_root = str(GlobalConfig.path_workspace_folder() or "").strip()
        if not workspace_root:
            return
        from .efmi_skeleton import EFMIBoneMapBuilder

        # S1（t20 容错对称化）：build_dualset_export_table 的表级 fail-closed
        # 断言（A1 VGMap 键集 / A3 identity 单射 / A4 强度可得 / B10 跨 LOD 段）
        # 抛 RuntimeError 时，本调用点保留 fail-closed 语义（不静默回退槽位直写，
        # 与 rekey 侧一致地捕获），但把错误信息包装为带「子网格 / 断言 / 恢复
        # 指引」的可操作文本——避免构造期裸 abort 留下空目录且无指引。
        try:
            component_unique_str = str(
                self.workspace_unique_str or self.unique_str or ""
            ).strip()
            # t25 方向 A（v3）：per-mesh 身份映射——导出身份 = 本组件成员身份，
            # 全量覆盖（含未更名槽）。根治 t24 型「跨组件 canonical 单写者时序
            # 塌陷」：网格引用落回自己 attach 必写的身份域。
            rename_map = EFMIBoneMapBuilder.build_per_mesh_identity_map(
                workspace_root, component_unique_str
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"[EFMI双套导出] {obj.name}（{self.unique_str}）导出转换表构建失败，"
                f"中止导出（fail-closed，不静默回退槽位直写）。\n"
                f"原因: {exc}\n"
                "恢复指引: 请在工作空间执行「骨骼合并反查/重新导入」确保 VGMap "
                "元数据完整后重试；若反复失败请检查是否存在外部进程反复清除工作区 "
                "json（Config/Tabs 或 TYPE_ 目录）"
            ) from exc
        if not rename_map:
            return

        # t31/佩丽卡场景A硬化（fail-closed，不放宽 FC-2；失败点前移 + 可操作指引）：
        # per-mesh 更名表键域 = 本组件当前 json VGMap 引用槽。带权重的数字组名
        # 必须 ⊆ 该域 ∪ 折叠基准段（与 FC-2 的「自属段 ∪ 折叠目标」同口径：
        # same-IB 折叠的 TEMP 合并拷贝会携带基准 LOD0 部件的槽位，属合法折叠
        # 目标）——旧导入/旧缓存场景对象的数字顶点组（如佩丽卡重建前的旧合并
        # 槽 371..647）不在该域内：静默跳过会让其原样直写、到 FC-2 才在产物级
        # 被拦（消息难定位「373..393 从哪来」）。这里在 M1 更名点前置拒绝：
        # 报「场景对象顶点组与工作区骨骼元数据不同期，请重新导入该角色」并
        # 列出具体槽位（不再静默直写）。
        self._assert_dualset_weighted_groups_in_identity_domain(
            obj,
            set(rename_map.keys()),
            workspace_root,
            str(self.unique_str or ""),
        )

        renamed_count = 0
        for vg in list(obj.vertex_groups):
            name = str(vg.name)
            if re.fullmatch(r"[0-9]+", name) is None:
                continue
            slot = int(name)
            target = rename_map.get(slot)
            if target is None or target == slot:
                continue
            existing = None
            try:
                existing = obj.vertex_groups.get(str(target))
            except AttributeError:
                # 测试桩/退化对象：vertex_groups 可能是普通列表
                existing = None
            if existing is None:
                for candidate in obj.vertex_groups:
                    if str(getattr(candidate, "name", "")) == str(target):
                        existing = candidate
                        break
            if existing is None or existing == vg:
                vg.name = str(target)
            else:
                # 目标名已被导入侧的全骨架占位组占用。直接改名会触发 Blender
                # 自动后缀（"54.001"），随后被 _prepare_merged_skeleton_vertex_groups
                # 当作非数字组删除 → 权重陪葬（33b5aa11 单骨组件权重归零 → 导出
                # 报 "Cannot find any valid BLENDINDICES"；多骨组件静默丢权重）。
                # 二段重命名：先把占位组挪到临时非数字名（空组，随后被非数字组
                # 清理移除，无害），再把带权重的源组改名为正式身份名——只动名字
                # 不动顶点分配，权重按 index 跟随组对象。
                if any(
                    g.weight > 0.0 and g.group == existing.index
                    for v in getattr(obj.data, "vertices", ()) or ()
                    for g in getattr(v, "groups", ()) or ()
                ):
                    raise RuntimeError(
                        f"[EFMI双套导出] {obj.name}: 槽 {slot} -> 身份 {target} "
                        "与现有带权重顶点组名冲突（重名两边都有本地权重），无法"
                        "安全重命名，中止导出（fail-closed，不静默覆盖权重）。"
                        "请重新导入该角色"
                    )
                existing.name = f"__TMP_DUALSET_{target}_{slot}"
                vg.name = str(target)
            renamed_count += 1
        if renamed_count == 0:
            return
        print(
            f"[EFMI双套导出] {obj.name}: {renamed_count} 个合并槽更名回非去重身份，"
            "(补缺/排序/断言由调用方预处理链统一执行)"
        )

    @staticmethod
    def _dualset_registered_slots_union(workspace_root: str) -> set[int]:
        """全工作区所有组件 json VGMap 引用槽并集（t16 C-ii，统一骨架/单池语义）。

        骨骼合并后全场共用一副骨架：任一组件引用另一组件的**已注册槽**时，
        被引用组件生成占位符保持该槽注册——跨组件引用是设计内合法状态。故
        放行域 = 当前 json 全组件（LOD0/LOD1…）VGMap 引用槽并集（动态读；
        未注册/旧代槽不在并集内 → 照旧 fail-closed）。json 元数据缺失/被清
        （t15 场景）时并集为空 → 调用方按「元数据缺失」分支报错（与
        「未注册槽」原因区分）。
        """
        slots: set[int] = set()
        ws = str(workspace_root or "")
        if not ws or not os.path.isdir(ws):
            return slots
        try:
            for lod_name in os.listdir(ws):
                lod_root = os.path.join(ws, lod_name)
                if not os.path.isdir(lod_root) or not lod_name.upper().startswith("LOD"):
                    continue
                for bare in os.listdir(lod_root):
                    folder = os.path.join(lod_root, bare)
                    if not os.path.isdir(folder) or bare.startswith("DedupedTextures"):
                        continue
                    for entry in os.listdir(folder):
                        tdir = os.path.join(folder, entry)
                        if not os.path.isdir(tdir) or not entry.startswith("TYPE_"):
                            continue
                        jp = os.path.join(tdir, bare + ".json")
                        if not os.path.isfile(jp):
                            continue
                        try:
                            import json as _json
                            with open(jp, encoding="utf-8") as f:
                                payload = _json.load(f)
                            vg_map = payload.get("VGMap")
                            if isinstance(vg_map, dict):
                                for raw_value in vg_map.values():
                                    # 注意：槽位 0 是合法注册槽，不能用
                                    # `raw_value or ""`（0 会被当假值吞掉）。
                                    text_value = str(raw_value).strip()
                                    if text_value.isdigit():
                                        slots.add(int(text_value))
                        except Exception:
                            continue
                        break
        except OSError:
            pass
        return slots

    @staticmethod
    def _assert_dualset_weighted_groups_in_identity_domain(
        obj, domain: set[int], workspace_root: str = "", component_unique_str: str = ""
    ) -> None:
        """域前置断言：带权重数字组名 ⊆ 本组件身份域 ∪ 全工作区已注册槽并集。

        语义依据（t16 C-ii，用户领域裁决）：骨骼合并后全场共用一副骨架，不同
        前缀物体的顶点组设计上可以互相引用——A 引用 B 的槽时，B 生成占位符
        保持该槽注册（占位符机制的存在理由）。故放行域 = 本组件 VGMap 引用槽
        ∪ **全工作区所有组件已注册槽并集**（动态读当前 json；不再限于 L0 段）。

        fail-closed 牙齿保留：**未注册/旧代槽照旧拦截**（不在并集内，如佩丽卡
        旧时代 373..393）；json 元数据缺失/被清（t15 场景，并集为空）时报
        「元数据缺失」明确消息，与「未注册槽」原因区分。与 FC-2 同口径：
        只查**带权重（weight>0）** 数字组名——无权重组不写盘（0 哨兵/空组
        惰性），skip_empty=False 全量空组工作流不受影响。

        对象无网格数据（单测桩/退化对象）时无法读取权重，退化为检查全部数字
        组名（保守）。
        """
        slot_by_index: dict[int, int] = {}
        for idx, vg in enumerate(obj.vertex_groups):
            name = str(vg.name)
            if re.fullmatch(r"[0-9]+", name) is None:
                continue
            slot_by_index[getattr(vg, "index", idx)] = int(name)

        weighted_slots: set[int] | None = None
        data = getattr(obj, "data", None)
        vertices = getattr(data, "vertices", None) if data is not None else None
        if vertices is not None:
            weighted_slots = set()
            for vertex in vertices:
                for group in getattr(vertex, "groups", ()) or ():
                    if float(getattr(group, "weight", 0.0) or 0.0) > 0.0:
                        slot = slot_by_index.get(getattr(group, "group", -1))
                        if slot is not None:
                            weighted_slots.add(slot)
        if weighted_slots is None:
            weighted_slots = set(slot_by_index.values())
        if not weighted_slots:
            return

        registered_union = (
            SubMeshModel._dualset_registered_slots_union(workspace_root)
            if workspace_root else set()
        )
        allowed = set(domain) | registered_union
        out_of_domain = sorted(weighted_slots - allowed)
        if not out_of_domain:
            return
        preview = out_of_domain[:20]
        suffix = "…" if len(out_of_domain) > 20 else ""
        if not registered_union:
            # 元数据缺失/被清（t15 场景）：全组件 json 均无已注册槽 → 与
            # 「未注册槽」原因区分，给出检查工作区 json / 重新导入指引。
            raise RuntimeError(
                f"[EFMI双套导出/域前置] {getattr(obj, 'name', '?')} 的数字顶点组 "
                f"{preview}{suffix}（共 {len(out_of_domain)} 个）无法判定槽位归属："
                "当前工作区没有任何组件 json 携带 VGMap 已注册槽（骨骼合并元数据"
                "缺失/被清除，或 json 被外部进程改写）。请检查工作区 json 或按当前"
                "工作区 json 重新导入该角色（含合并顶点组开关）后重试"
            )
        raise RuntimeError(
            f"[EFMI双套导出/域前置] {getattr(obj, 'name', '?')} 的数字顶点组 "
            f"{preview}{suffix}（共 {len(out_of_domain)} 个）不在当前工作区已注册"
            "槽集合（本组件 VGMap 引用槽 ∪ 全工作区所有组件已注册槽）内：未注册/"
            "旧代槽（旧导入/旧缓存/外部进程改写 json 遗留）。产物将引用运行时未注册"
            "槽位，中止导出（fail-closed，不静默直写）。请按当前工作区 json 重新导入"
            "该角色后重试"
        )

    @staticmethod
    def _fill_vertex_group_gaps(obj):
        """按数字名补缺顶点组（参考 EFMI-Tools fill_gaps_in_vertex_groups）。

        把 [0, 3, 4, 6] 这样的名字补齐为连续数字名（新增 1、2、5），
        不依赖 active_object，直接操作给定对象。
        """
        existing = set()
        for vg in obj.vertex_groups:
            name = str(vg.name)
            if name.isdigit():
                existing.add(int(name))
        if not existing:
            return
        for i in range(max(existing) + 1):
            if i not in existing:
                obj.vertex_groups.new(name=str(i))
        # 排序由后续紧凑化重命名前的 index 语义处理；此处仅补缺。

    def _prepare_merged_skeleton_vertex_groups(self, obj):
        """骨骼合并导出前顶点组预处理（对齐参考插件 ObjectMerger.finalize_temp_objects_data）。

        EFMI/ZZMI 合并骨架模式共用：组名 = 全局骨骼 id。非数字组不属于骨骼命名
        空间，在临时导出对象上先移除；数字组无条件补缺、排序并验证
        ``vertex_group.index == int(vertex_group.name)``。任一步失败都中止导出，
        禁止退回原样导出后静默重编号全局骨骼。
        """
        if obj is None or obj.type != 'MESH':
            return
        if not obj.vertex_groups:
            raise RuntimeError(f"[骨骼合并] {obj.name} 没有可导出的数字骨骼顶点组")

        non_numeric_groups = [
            vg for vg in obj.vertex_groups
            if re.fullmatch(r"[0-9]+", str(vg.name)) is None
        ]
        for vg in non_numeric_groups:
            obj.vertex_groups.remove(vg)
        if non_numeric_groups:
            print(
                f"[骨骼合并] {obj.name}: 已从临时导出对象移除 "
                f"{len(non_numeric_groups)} 个非数字顶点组"
            )
        if not obj.vertex_groups:
            raise RuntimeError(f"[骨骼合并] {obj.name} 移除非数字组后没有骨骼顶点组")

        numeric_ids = [int(vg.name) for vg in obj.vertex_groups]
        if len(numeric_ids) != len(set(numeric_ids)):
            raise RuntimeError(
                f"[骨骼合并] {obj.name} 存在等价的重复数字顶点组名: {numeric_ids}"
            )

        # 连续全局 id 是合并骨架正确性前提，不受普通导出补组开关控制。
        self._fill_vertex_group_gaps(obj)

        prev_active = bpy.context.view_layer.objects.active
        try:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.vertex_group_sort()
        finally:
            bpy.context.view_layer.objects.active = prev_active

        for vg in obj.vertex_groups:
            if int(vg.name) != int(vg.index):
                raise RuntimeError(
                    f"[骨骼合并] {obj.name} 顶点组排序不变量失败: "
                    f"name={vg.name}, index={vg.index}"
                )
            vg.name = str(vg.index)

    def _prepare_zzmi_merged_skeleton_vertex_groups(self, obj):
        """ZZMI 合并骨架导出前顶点组预处理（组名 = 全局骨骼 id，连续性是关键不变量）。

        与 EFMI 版的差异（修复"删空组→后续部件集体错位消失"）：
        1. **无条件补缺**到最大数字名（不受 export_add_missing_vertex_groups 门控——
           ZZMI 合并模式下组号连续是正确性前提，不是可选项）；
        2. **不剔除任何组**（ignore 组也不删：删任意中间组都会让后续组号 -1 移位，
           全局骨骼 id 全部错位；需要隐藏部件请用其它方式）；
        3. 按名排序 + 重命名 str(index)（补缺+排序后此为安全恒等操作）；
        4. 自检：非数字组名只警告；空组正常（无顶点引用的骨骼，无害）。
        """
        self._prepare_merged_skeleton_vertex_groups(obj)

    @staticmethod
    def _tag_weld_block(temp_obj, block_id):
        """在 join 前的临时副本上写分块标记（INT 属性 + 退化用临时顶点组）。

        仅作用于 calc_buffer 内部创建的导出临时副本；绝不触碰源物体/源网格。
        """
        mesh = temp_obj.data
        vertex_count = len(mesh.vertices)
        if vertex_count <= 0:
            return

        values = numpy.full(vertex_count, int(block_id), dtype=numpy.int32)
        attribute = mesh.attributes.get(WELD_BLOCK_ATTR)
        if (
            attribute is None
            or attribute.domain != 'POINT'
            or attribute.data_type != 'INT'
        ):
            if attribute is not None:
                mesh.attributes.remove(attribute)
            attribute = mesh.attributes.new(WELD_BLOCK_ATTR, 'INT', 'POINT')
        attribute.data.foreach_set("value", values)

        group_name = WELD_BLOCK_GROUP_PREFIX + str(int(block_id))
        vertex_group = temp_obj.vertex_groups.get(group_name)
        if vertex_group is None:
            vertex_group = temp_obj.vertex_groups.new(name=group_name)
        vertex_group.add(list(range(vertex_count)), 1.0, 'REPLACE')

    @staticmethod
    def _compute_weld_block_vertex_reductions(groups, blocks):
        """按分块统计焊接后各 block 的顶点减少量。

        groups 内每个分组的代表索引按 block 升序；remove_doubles 每组使
        顶点总数减少 (代表数 - 1)，保留的是最小 block 的代表，因此其余
        参与分块各减 1（同分块每组至多一个代表，故每 block 每组最多减 1）。
        """
        reductions = {}
        for representative_indices in groups or ():
            # 首项 = 最小 block 的代表，计数不变，只统计其余代表
            for representative_index in representative_indices[1:]:
                block_id = int(blocks[representative_index])
                if block_id <= 0:
                    continue
                reductions[block_id] = reductions.get(block_id, 0) + 1
        return reductions

    def _weld_cross_block_boundaries(self, obj, distance):
        """join 后按分块标记焊接跨分块边界顶点（只作用于导出临时副本）。

        规则见计划书第 3 / 4.1 节：仅候选（block > 0 且边界顶点）参与，
        同格内每个分块只取一个索引最小的代表，只有跨 >= 2 个分块才合并。
        """
        import bmesh

        mesh = obj.data
        loop_count_before = len(mesh.loops)
        face_count_before = len(mesh.polygons)
        vertex_count_before = len(mesh.vertices)
        weld_result = None
        marker_source = "未知"
        block_vertex_reductions = {}

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()

            blocks = self._read_weld_blocks_from_attributes(bm)
            marker_source = "INT 属性"
            if blocks is None:
                blocks = self._read_weld_blocks_from_vertex_groups(bm, obj)
                marker_source = "临时顶点组"
            if blocks is None:
                raise RuntimeError(
                    f"[合并顶点] {self.unique_str} join 后无法读取分块标记"
                    f"（{WELD_BLOCK_ATTR} 属性与临时顶点组均丢失），中止导出"
                )

            boundaries = [False] * len(bm.verts)
            for edge in bm.edges:
                if edge.is_boundary:
                    for vert in edge.verts:
                        boundaries[vert.index] = True

            positions = [(vert.co.x, vert.co.y, vert.co.z) for vert in bm.verts]
            weld_result = compute_weld_groups(
                positions, boundaries, blocks, distance
            )
            block_vertex_reductions = self._compute_weld_block_vertex_reductions(
                weld_result["groups"], blocks
            )

            if weld_result["groups"]:
                bm.verts.index_update()
                # remove_doubles 会重排 bm.verts，先按原始索引快照 BMVert 引用；
                # 每组的代表对象互不重叠，逐组调用不会互相影响。
                ordered_verts = list(bm.verts)
                for representative_indices in weld_result["groups"]:
                    representative_verts = [
                        ordered_verts[index] for index in representative_indices
                    ]
                    if len(representative_verts) < 2:
                        continue
                    # 只传本组代表：同分块其他顶点不在列表内，不会被合并
                    bmesh.ops.remove_doubles(
                        bm, verts=representative_verts, dist=distance
                    )
                bm.verts.ensure_lookup_table()

            block_layer = bm.verts.layers.int.get(WELD_BLOCK_ATTR)
            if block_layer is not None:
                bm.verts.layers.int.remove(block_layer)

            bm.to_mesh(mesh)
        finally:
            bm.free()

        stray_attribute = mesh.attributes.get(WELD_BLOCK_ATTR)
        if stray_attribute is not None:
            mesh.attributes.remove(stray_attribute)
        self._remove_weld_temp_vertex_groups(obj)
        mesh.update()

        if weld_result is None:
            raise RuntimeError(
                f"[合并顶点] {self.unique_str} 焊接未产生结果（内部状态异常），中止导出"
            )

        # 校验一：面数 / loop 数必须与 join 后完全一致，否则 loop 范围会错位
        if len(mesh.polygons) != face_count_before:
            raise RuntimeError(
                f"[合并顶点] {self.unique_str} 焊接后面数变化: "
                f"{face_count_before} -> {len(mesh.polygons)}，中止导出"
            )
        if len(mesh.loops) != loop_count_before:
            raise RuntimeError(
                f"[合并顶点] {self.unique_str} 焊接后 loop 数变化: "
                f"{loop_count_before} -> {len(mesh.loops)}（loop 范围可能错位），中止导出"
            )

        # 校验二：每个形态键的数据长度必须等于焊接后顶点数，防止 buffer 错位
        key_blocks = getattr(getattr(mesh, "shape_keys", None), "key_blocks", None)
        if key_blocks:
            vertex_count_after = len(mesh.vertices)
            for key_block in key_blocks:
                if len(key_block.data) != vertex_count_after:
                    raise RuntimeError(
                        f"[合并顶点] {self.unique_str} 形态键 '{key_block.name}' 数据长度 "
                        f"{len(key_block.data)} != 焊接后顶点数 {vertex_count_after}，中止导出"
                    )

        vertex_count_after = int(len(mesh.vertices))
        recomputed_vertex_count = int(vertex_count_before) - sum(
            block_vertex_reductions.values()
        )
        stats = {
            "merged_group_count": int(weld_result["merged_group_count"]),
            "involved_block_count": int(weld_result["involved_block_count"]),
            "skipped_single_block_count": int(weld_result["skipped_single_block_count"]),
            "excluded_non_boundary_count": int(weld_result["excluded_non_boundary_count"]),
            "block_vertex_reductions": {
                int(block_id): int(reduction)
                for block_id, reduction in block_vertex_reductions.items()
            },
            "vertex_count_before": int(vertex_count_before),
            "vertex_count_after": vertex_count_after,
        }
        print(
            f"[合并顶点] {self.unique_str}: 合并 {stats['merged_group_count']} 组 / "
            f"涉及 {stats['involved_block_count']} 个分块 / "
            f"跳过 {stats['skipped_single_block_count']} 组（单分块）/ "
            f"排除 {stats['excluded_non_boundary_count']} 个非边界顶点；"
            f"标记来源={marker_source}，顶点 {vertex_count_before} -> {vertex_count_after}，"
            f"面 {face_count_before} -> {len(mesh.polygons)}，"
            f"loop {loop_count_before} -> {len(mesh.loops)}，"
            f"重算后顶点数={recomputed_vertex_count}"
            f"（分块减量合计={sum(block_vertex_reductions.values())}）"
        )
        if recomputed_vertex_count != vertex_count_after:
            print(
                f"[合并顶点] {self.unique_str} 警告: 按分块标记重算的顶点数 "
                f"{recomputed_vertex_count} != 焊接后 mesh 实际顶点数 "
                f"{vertex_count_after}（部分代表未被合并），以 mesh 实际为准"
            )
        try:
            from ..blueprint.export_helper import BlueprintExportHelper as _BlueprintExportHelper
            setter = getattr(_BlueprintExportHelper, "set_merge_weld_last_stats", None)
            if callable(setter):
                setter(stats)
        except Exception as error:
            print(f"[合并顶点] 统计写回 BlueprintExportHelper 失败（不影响导出）: {error}")
        return stats

    @staticmethod
    def _read_weld_blocks_from_attributes(bm):
        """读取 join 后的 INT 分块属性；属性丢失或全 0 时返回 None。"""
        layer = bm.verts.layers.int.get(WELD_BLOCK_ATTR)
        if layer is None:
            return None
        blocks = [int(vert[layer]) for vert in bm.verts]
        if not any(block_id > 0 for block_id in blocks):
            return None
        return blocks

    @staticmethod
    def _read_weld_blocks_from_vertex_groups(bm, obj):
        """退化方案：从临时顶点组读取分块号；无临时组时返回 None。"""
        index_to_block = {}
        for vertex_group in obj.vertex_groups:
            name = str(vertex_group.name)
            if not name.startswith(WELD_BLOCK_GROUP_PREFIX):
                continue
            try:
                block_id = int(name[len(WELD_BLOCK_GROUP_PREFIX):])
            except ValueError:
                continue
            index_to_block[int(vertex_group.index)] = block_id
        if not index_to_block:
            return None

        deform_layer = bm.verts.layers.deform.active
        blocks = [0] * len(bm.verts)
        if deform_layer is None:
            return blocks
        for vert in bm.verts:
            for group_index in vert[deform_layer].keys():
                block_id = index_to_block.get(int(group_index))
                if block_id:
                    blocks[vert.index] = block_id
                    break
        return blocks

    @staticmethod
    def _remove_weld_temp_vertex_groups(obj):
        """删除焊接用临时顶点组，避免其进入后续顶点组预处理。"""
        for vertex_group in list(obj.vertex_groups):
            if str(vertex_group.name).startswith(WELD_BLOCK_GROUP_PREFIX):
                obj.vertex_groups.remove(vertex_group)

    def _ensure_target_shape_key_union(self, target_obj: bpy.types.Object, source_objs: list[bpy.types.Object]):
        objects = [
            obj for obj in [target_obj] + list(source_objs or [])
            if obj is not None and getattr(obj, "data", None)
        ]
        if not objects:
            return

        ordered_shape_key_names = []
        seen_names = set()
        for obj in objects:
            key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
            if not key_blocks:
                continue
            for key_index, key_block in enumerate(key_blocks):
                if key_index == 0:
                    continue
                key_name = getattr(key_block, "name", "")
                if key_name and key_name not in seen_names:
                    seen_names.add(key_name)
                    ordered_shape_key_names.append(key_name)

        if not ordered_shape_key_names:
            return

        total_added_count = 0
        for obj in objects:
            target_shape_keys = getattr(obj.data, "shape_keys", None)
            target_key_blocks = getattr(target_shape_keys, "key_blocks", None)
            if not target_key_blocks:
                obj.shape_key_add(name="Basis", from_mix=False)
                target_key_blocks = obj.data.shape_keys.key_blocks

            target_names = {key_block.name for key_block in target_key_blocks}
            added_count = 0
            for key_name in ordered_shape_key_names:
                if key_name in target_names:
                    continue
                obj.shape_key_add(name=key_name, from_mix=False)
                target_names.add(key_name)
                added_count += 1

            total_added_count += added_count

        if total_added_count:
            print(f"[SubMeshModel] ShapeKey 合并前补齐: {self.unique_str} 新增 {total_added_count} 个空形态键槽")

    def _build_drawcall_candidate_names(self, draw_call_model: DrawCallModel) -> set[str]:
        candidate_names = {
            getattr(draw_call_model, "obj_name", "") or "",
            getattr(draw_call_model, "source_obj_name", "") or "",
            draw_call_model.get_blender_obj_name() or "",
        }

        normalized_names = set()
        for name in candidate_names:
            if not name:
                continue
            normalized_names.add(name)
            normalized_name = self._normalize_source_name(name)
            if normalized_name:
                normalized_names.add(normalized_name)
            if name.endswith("_vgtest_unassigned_copy"):
                normalized_names.add(name[:-23])
            if name.endswith("_vgtest_copy"):
                normalized_names.add(name[:-12])
            if name.endswith("_copy"):
                normalized_names.add(name[:-5])

        return {name for name in normalized_names if name}

    def _normalize_source_name(self, name: str) -> str:
        if not name:
            return name

        result = name
        for pattern in (
            r"_chain\d+_dup\d+_vgtest_unassigned_copy$",
            r"_chain\d+_vgtest_unassigned_copy$",
            r"_dup\d+_vgtest_unassigned_copy$",
            r"_vgtest_unassigned_copy$",
            r"_chain\d+_dup\d+_vgtest_copy$",
            r"_chain\d+_vgtest_copy$",
            r"_dup\d+_vgtest_copy$",
            r"_vgtest_copy$",
            r"_chain\d+_dup\d+_copy$",
            r"_chain\d+_copy$",
            r"_dup\d+_copy$",
            r"_copy$",
            r"_chain\d+_dup\d+$",
            r"_chain\d+$",
            r"_dup\d+$",
        ):
            result = re.sub(pattern, "", result)
        return result

    def _resolve_preferred_source_name(self, source_obj: bpy.types.Object, draw_call_model: DrawCallModel) -> str:
        for candidate_name in (
            getattr(source_obj, "name", "") if source_obj is not None else "",
            getattr(draw_call_model, "source_obj_name", "") or "",
            draw_call_model.get_blender_obj_name() or "",
            getattr(draw_call_model, "obj_name", "") or "",
        ):
            normalized_name = self._normalize_source_name(candidate_name)
            if normalized_name:
                return normalized_name
        return ""

    def _build_object_export_context_map(self, cache_key_to_geometry_record: dict, cache_key_to_candidate_names: dict) -> dict:
        if self.unique_first_loop_indices is None:
            return {}

        unique_first_loop_indices = numpy.asarray(self.unique_first_loop_indices, dtype=numpy.int32)
        object_export_context_map = {}

        for cache_key, geometry_record in cache_key_to_geometry_record.items():
            loop_start = int(geometry_record.get("loop_start", 0))
            loop_count = int(geometry_record.get("loop_count", 0))
            if loop_count <= 0:
                continue

            loop_end = loop_start + loop_count
            export_mask = (unique_first_loop_indices >= loop_start) & (unique_first_loop_indices < loop_end)
            export_indices = numpy.flatnonzero(export_mask).astype(numpy.int32)
            if export_indices.size == 0:
                continue

            local_loop_indices = (unique_first_loop_indices[export_mask] - loop_start).astype(numpy.int32)
            context = {
                "cache_key": cache_key,
                "export_indices": export_indices,
                "local_loop_indices": local_loop_indices,
                "vertex_count": int(export_indices.size),
                "label": geometry_record.get("label", self.unique_str),
                "d3d11_game_type": geometry_record.get("d3d11_game_type", self.d3d11_game_type),
                "preferred_source_name": geometry_record.get("preferred_source_name", ""),
            }

            for candidate_name in cache_key_to_candidate_names.get(cache_key, set()):
                if candidate_name:
                    object_export_context_map.setdefault(candidate_name, context)

        return object_export_context_map

    def _should_duplicate_source_for_merge(self, source_obj: bpy.types.Object) -> bool:
        if source_obj is None:
            return True

        if len(self.drawcall_model_list) != 1:
            return True

        if self.has_multi_file_export_nodes:
            return True

        if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
            return True

        if not source_obj.name.endswith('_copy'):
            return True

        unique_str_count = self.source_obj_unique_str_count.get(source_obj.name, 0)
        return unique_str_count > 1

    def _should_preserve_distinct_export_contexts(self) -> bool:
        # Keep the base exporter on the legacy geometry-reuse path.
        # Direct shape key export now supplements missing per-object data later,
        # and forcing distinct export contexts here inflates the base buffers.
        #
        # 【t81-F1】HIMI 合并/join 导出：**禁用几何复用**（`preserve_distinct_export_contexts` 的既有语义）。
        #   为什么：复用键只有 `(obj_hash, original_name)`，而合并/join 会把同一源对象用在不同 draw call 上
        #   （去重 + 紧凑重编号后行序不同）⇒ **键相同、几何不同** ⇒ 命中方不写自己的几何、直接拿
        #   首写方的 offset/count ⇒ 位置被跨行复制（t80 实测 46 行 / 产物段长边 190）。
        #   维护者已有同类先例：直出 ShapeKey 场景就是靠这个开关禁复用（`:227-228` / `:247`）。
        # 【t83-A1】去掉 `import_merged_vgmap()` 这半条件：t82 已确认本缺陷与缓存无关（12/12 `hit=no`），
        #   而这个门本来就不该"只在合并开关打开时"才禁复用 —— **HIMI 的 join 路径两种模式都会走**，
        #   所以普通导出同样禁用；日志里同时打 `logic_name` 与开关实际取值，让"没生效"可判。
        if GlobalConfig.logic_name == LogicName.HIMI:
            print(
                f"[SubMeshModel][t83-A1] HIMI 禁用几何复用 "
                f"(logic_name={GlobalConfig.logic_name!r}, "
                f"import_merged_vgmap={bool(GlobalProterties.import_merged_vgmap())}) "
                f"unique_str={self.unique_str}"
            )
            return True
        return False

    def _deduplicate_draw_calls(self):
        if len(self.drawcall_model_list) <= 1:
            return

        seen_keys = set()
        deduped = []
        for dcm in self.drawcall_model_list:
            condition_str = dcm.get_condition_str()
            draw_key = (dcm.index_offset, dcm.index_count, condition_str)
            if condition_str or draw_key not in seen_keys:
                seen_keys.add(draw_key)
                deduped.append(dcm)

        removed = len(self.drawcall_model_list) - len(deduped)
        if removed > 0:
            print(f"[SubMeshModel] 绘制去重: {self.unique_str} 移除 {removed} 个重复绘制 (原始 {len(self.drawcall_model_list)} → 保留 {len(deduped)})")
            self.drawcall_model_list = deduped

    def _precompute_object_hashes(self) -> tuple:
        """预计算所有源对象的数据哈希，使用多线程并行计算
        
        流程：
        1. 主线程中从 Blender 对象提取原始数据（Blender API 非线程安全）
        2. 多线程并行计算哈希值（纯 Python 计算，线程安全）
        
        Returns:
            (hashes, source_obj_list): 哈希列表和源对象引用列表
        """
        raw_data_list = []
        source_obj_list = []
        for draw_call_model in self.drawcall_model_list:
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = ObjUtils.get_obj_by_name(blender_obj_name)
            source_obj_list.append(source_obj)

            if source_obj is None:
                raw_data_list.append(None)
                continue

            raw_data_list.append(self._extract_object_raw_data(source_obj))

        # 第二步：使用多线程并行计算哈希
        def compute_hash(raw_data):
            if raw_data is None:
                return None
            h = hashlib.md5()
            for item in raw_data:
                if isinstance(item, bytes):
                    h.update(item)
                elif isinstance(item, str):
                    h.update(item.encode('utf-8'))
                elif isinstance(item, int):
                    h.update(struct.pack('i', item))
                elif isinstance(item, float):
                    h.update(struct.pack('f', item))
            return h.hexdigest()

        if len(raw_data_list) > 8:
            try:
                with ThreadPoolExecutor() as executor:
                    hashes = list(executor.map(compute_hash, raw_data_list))
            except Exception:
                hashes = [compute_hash(raw_data) for raw_data in raw_data_list]
        else:
            hashes = [compute_hash(raw_data) for raw_data in raw_data_list]

        return hashes, source_obj_list

    @staticmethod
    def _extract_object_raw_data(obj) -> list:
        """从 Blender 对象提取用于哈希计算的原始数据
        
        包含：顶点位置、顶点组名称和权重、UV 数据
        使用 foreach_get 批量读取，性能远优于逐顶点迭代
        """
        mesh = obj.data
        raw_data = []

        # 1. 顶点位置数据
        vert_count = len(mesh.vertices)
        raw_data.append(vert_count)
        if vert_count > 0:
            coords = array.array('f', [0.0] * (vert_count * 3))
            mesh.vertices.foreach_get('co', coords)
            raw_data.append(coords.tobytes())

        # 2. 顶点组名称（顶点组存储在 Object 层面，不同对象可以不同）
        vg_count = len(obj.vertex_groups)
        raw_data.append(vg_count)
        for vg in obj.vertex_groups:
            raw_data.append(vg.name)

        # 3. 顶点组权重数据
        if vert_count > 0 and vg_count > 0:
            for vi, vert in enumerate(mesh.vertices):
                for group in vert.groups:
                    raw_data.append(vi)
                    raw_data.append(group.group)
                    raw_data.append(group.weight)

        # 4. UV 数据
        uv_layer_count = len(mesh.uv_layers)
        raw_data.append(uv_layer_count)
        for uv_layer in mesh.uv_layers:
            uv_count = len(uv_layer.data)
            raw_data.append(uv_count)
            if uv_count > 0:
                uvs = array.array('f', [0.0] * (uv_count * 2))
                uv_layer.data.foreach_get('uv', uvs)
                raw_data.append(uvs.tobytes())

        return raw_data

    def _normalize_temp_obj_for_export(self, temp_obj: bpy.types.Object):
        if self.d3d11_game_type is None:
            return

        if "Blend" not in self.d3d11_game_type.OrderedCategoryNameList:
            return

        if ObjUtils.is_all_vertex_groups_locked(temp_obj):
            return

        self._normalize_vertex_groups_numpy(temp_obj)

    @staticmethod
    def _normalize_vertex_groups_numpy(obj: bpy.types.Object):
        mesh = obj.data
        vgroups = obj.vertex_groups

        if len(mesh.vertices) == 0 or len(vgroups) == 0:
            return

        for vg in vgroups:
            if vg.lock_weight:
                vg.lock_weight = False

        import bmesh

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            deform_layer = bm.verts.layers.deform.verify()

            changed_count = 0
            for vert in bm.verts:
                deform_data = vert[deform_layer]
                if not deform_data:
                    continue

                total_weight = sum(deform_data.values())
                if total_weight <= 0.0 or abs(total_weight - 1.0) <= 1e-7:
                    continue

                inv_total = 1.0 / total_weight
                for group_index in list(deform_data.keys()):
                    deform_data[group_index] *= inv_total
                changed_count += 1

            if changed_count == 0:
                return

            bm.to_mesh(mesh)
            mesh.update()
        finally:
            bm.free()

        if not obj.data.vertices:
            return

    def _apply_export_rotation_for_logic(self, temp_obj: bpy.types.Object):
        ExportUtils.apply_export_space_transform_to_object(temp_obj, logic_name=GlobalConfig.logic_name)
