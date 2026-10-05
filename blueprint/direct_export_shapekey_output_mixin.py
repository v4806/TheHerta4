import os
import re
from collections import OrderedDict

import numpy as np

from ..common.mod_path_compat import collect_base_position_resource_map
from ..common.mod_path_compat import derive_shapekey_base_resource_name
from ..common.mod_path_compat import derive_shapekey_freq_resource_name
from ..common.mod_path_compat import derive_shapekey_merged_data_resource_name
from ..common.mod_path_compat import derive_shapekey_merged_map_resource_name
from ..common.mod_path_compat import derive_shapekey_slot_map_resource_name
from ..common.mod_path_compat import derive_shapekey_slot_resource_name
from ..common.mod_path_compat import derive_shapekey_weight_resource_name
from ..common.mod_path_compat import ensure_resource_alias_section
from ..utils.log_utils import LOG
from .direct_export_runtime_utils import apply_position_override_in_place
from .direct_export_runtime_utils import assemble_drawib_position_bytes
from .direct_export_runtime_utils import extract_position_bytes_by_indices as _extract_position_bytes_by_indices
from .direct_export_runtime_utils import iter_drawib_models as _iter_drawib_models
from .direct_export_shapekey_shared import ShapeKeyDirectExportError, _buffer_to_bytes, resolve_use_delta


# 形态键权重搬运的跨模块约定：必须与 node_postprocess_shapekey.py 的
# WEIGHT_SYNC_SHADER_NAME / SSMTNode_PostProcess_ShapeKey.WEIGHT_BUFFER_REGISTER
# 一致——两条生成路径写的是同一份 shader、同一张共享表、同一批槽位。
WEIGHT_SYNC_SHADER_NAME = "shapekey_weight_sync.hlsl"
WEIGHT_SYNC_SHADER_REL = "./res/" + WEIGHT_SYNC_SHADER_NAME


class DirectShapeKeyOutputMixin:
    _PRESENT_RUN_BEGIN = "; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---"
    _PRESENT_RUN_END = "; --- SSMT DIRECT SHAPEKEY PRESENT END ---"

    #: 形态键强度签名：每帧把本帧所有强度/开关变量按固定权重求和，与上一帧比较，
    #: 没变化就整段跳过 dispatch。一次 dispatch 要读 ~36MB、写 ~9MB 顶点缓冲
    #: （14033 组 × 16 线程），静止（没有动画、没按 Alt 拖拽）时输入完全没变，
    #: 重算一遍是纯显存带宽浪费 —— 装大量同类模组时这是最大的 GPU 开销。
    #: 权重取互不相同的质数：任意单个强度变化都会改变加权和（避免两项抵消）。
    _SK_SIGNATURE_WEIGHTS = (
        1, 3, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53,
        59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109,
        113, 127, 131, 137, 139, 149, 151, 157, 163, 167, 173,
        179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233,
    )
    #: 单条 INI 表达式里最多塞多少个强度项（3DMigoto 行缓冲有限，超长行会被截断）
    _SK_SIGNATURE_TERMS_PER_LINE = 16

    def _uses_active_guard(self):
        key_map = getattr(getattr(self, "blueprint_model", None), "keyname_mkey_dict", None)
        return bool(key_map)

    @staticmethod
    def _drag_mode_variable_for(drag_drive_resource):
        """从拖拽驱动资源名推出「拖拽系统本帧在臂动」的门控变量。

        资源名形如 ``ResourceDragShapeKeyDrive_{ns}``（拖拽节点命名），对应变量
        ``$ssmtdrag_mode_{ns}``（Alt 按住时为 1）。拖拽驱动缓冲每帧都在变，
        签名看不出来，必须用它强制 dispatch。拿不到就返回空串（不加这一项）。
        """
        text = str(drag_drive_resource or "").strip()
        prefix = "ResourceDragShapeKeyDrive_"
        if not text.startswith(prefix):
            return ""
        namespace = text[len(prefix):].strip()
        return f"$ssmtdrag_mode_{namespace}" if namespace else ""

    def _sk_signature_var_names(self, unique_hashes):
        """签名变量的名字（按 hash 前缀区分，多个形态键节点互不干扰）。"""
        suffix = ""
        for logical_hash in unique_hashes:
            suffix = str(self.node._extract_hash_prefix(logical_hash) or "")
            if suffix:
                break
        safe_suffix = re.sub(r"[^0-9A-Za-z_]", "_", suffix) or "0"
        return f"$ssmt_sk_sig_{safe_suffix}", f"$ssmt_sk_sig_prev_{safe_suffix}"

    def _build_signature_lines(self, signature_var, signature_vars):
        """把签名拆成若干条有界长度的赋值（超长表达式行可能被 3DMigoto 截断）。"""
        params = []
        for name in signature_vars or ():
            text = str(name or "").strip()
            if text and text not in params:
                params.append(text)
        if not params:
            return []

        step = max(1, int(self._SK_SIGNATURE_TERMS_PER_LINE))
        weights = self._SK_SIGNATURE_WEIGHTS
        lines = []
        for chunk_index in range(0, len(params), step):
            chunk = params[chunk_index:chunk_index + step]
            terms = " + ".join(
                f"{param} * {weights[(chunk_index + offset) % len(weights)]}"
                for offset, param in enumerate(chunk)
            )
            if chunk_index == 0:
                lines.append(f"{signature_var} = {terms}")
            else:
                lines.append(f"{signature_var} = {signature_var} + {terms}")
        return lines

    def _build_present_run_block(self, unique_hashes, signature_vars=None, drag_active_var=""):
        """[Present] 里的形态键 dispatch 块。

        带 ``signature_vars`` 时先算签名，只有签名变化（或有拖拽在驱动形态键）才
        真的 dispatch；没有签名变量（旧调用点）时保持原样无条件 dispatch。
        """
        signature_lines = []
        if signature_vars:
            signature_var, _prev_var = self._sk_signature_var_names(unique_hashes)
            signature_lines = self._build_signature_lines(signature_var, signature_vars)

        lines = [self._PRESENT_RUN_BEGIN]
        guard_open = ['if $active0 == 1'] if self._uses_active_guard() else []
        indent = "    " * len(guard_open)

        if signature_lines:
            signature_var, prev_var = self._sk_signature_var_names(unique_hashes)
            condition = f"{signature_var} != {prev_var}"
            if drag_active_var:
                condition = f"{condition} || {drag_active_var} == 1"
            lines.extend(guard_open)
            lines.extend(f"{indent}{line}" for line in signature_lines)
            lines.append(f"{indent}if {condition}")
            # 权重搬运必须先于形态键 CS：把打包窗口搬进 mod 专属权重缓冲并清零，
            # Anim CS 才能从缓冲读到本帧强度（见 shapekey_weight_sync.hlsl）。
            lines.extend(
                f"{indent}    run = CustomShaderShapeKeyWeightSync_{logical_hash}"
                for logical_hash in unique_hashes
            )
            lines.extend(
                f"{indent}    run = CustomShader_{logical_hash}_Anim"
                for logical_hash in unique_hashes
            )
            lines.append(f"{indent}    {prev_var} = {signature_var}")
            lines.append(f"{indent}endif")
            if guard_open:
                lines.append("endif")
        else:
            lines.extend(guard_open)
            lines.extend(
                f"{indent}run = CustomShaderShapeKeyWeightSync_{logical_hash}"
                for logical_hash in unique_hashes
            )
            lines.extend(
                f"{indent}run = CustomShader_{logical_hash}_Anim"
                for logical_hash in unique_hashes
            )
            if guard_open:
                lines.append("endif")
        lines.append(self._PRESENT_RUN_END)
        return lines

    def _delta_stride_for(self, logical_hash, vertex_stride, struct_definition=None):
        """增量资源在 INI 里声明的 stride。

        关闭「存储全部顶点属性增量」时恒为 12（仅位置）；开启时 = 4 × 通道 float 数
        （位置+法线+切线 xyz 即 36）。必须与着色器里的 ``ShapeKeyDelta`` 行宽一致。
        """
        return self.node._resolve_delta_stride(
            hash_val=logical_hash,
            vertex_stride=vertex_stride,
            struct_definition=struct_definition,
        )

    def _delta_channel_columns(self, runtime_info, num_floats_per_vertex, struct_definition=None):
        """增量通道列下标。

        关闭「存储全部顶点属性增量」时恒为 ``[0, 1, 2]``（仅位置），与旧版逐字节一致；
        开启时按顶点数据类型展开（位置 + 法线 + 切线 xyz）。结构体一律按 ``hash_val``
        逐哈希解析——必须与 ``_update_shader_file`` 注入着色器的那个是同一份，
        否则数据列与着色器结构体字段会错位。
        """
        plan = self.node._resolve_delta_channel_plan(
            hash_val=runtime_info.get("logical_hash") if runtime_info else None,
            struct_definition=struct_definition,
            num_floats_per_vertex=num_floats_per_vertex,
        )
        return self.node._channel_plan_columns(plan)

    def _write_slot_files(self, logical_hash, runtime_info, hash_slot_data, slot_position_overrides):
        use_packed = self.node.use_packed_Meshess
        use_delta = resolve_use_delta(self.node)
        actual_hash = runtime_info["actual_hash"]
        base_bytes = runtime_info["base_bytes"]
        struct_definition = self.node._get_vertex_struct_definition()
        slot_maps = {}

        for slot_num, names_data in sorted(hash_slot_data.items()):
            target_bytes = self._compose_slot_bytes(
                logical_hash,
                runtime_info,
                slot_num,
                names_data,
                slot_position_overrides,
            )
            vertex_stride, num_floats_per_vertex, num_vertices = self.node._detect_vertex_format(
                base_bytes,
                target_bytes,
                struct_definition,
                preferred_stride=runtime_info["position_stride"],
            )
            base_data = np.frombuffer(base_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))
            target_data = np.frombuffer(target_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))

            output_prefix = os.path.join(self.meshes_dir, f"{actual_hash}-Position1{slot_num:03d}")
            slot_maps[slot_num] = None

            if use_delta:
                channel_columns = self._delta_channel_columns(
                    runtime_info,
                    num_floats_per_vertex,
                )
                data_to_write = target_data[:, channel_columns] - base_data[:, channel_columns]
                data_to_write[data_to_write == 0] = 0.0
                diff_mask = ~np.isclose(
                    base_data[:, channel_columns],
                    target_data[:, channel_columns],
                    atol=1e-6,
                ).all(axis=1)
                if use_packed:
                    packed_data = data_to_write[diff_mask]
                    map_array = np.full(num_vertices, -1, dtype=np.int32)
                    map_array[diff_mask] = np.arange(np.count_nonzero(diff_mask), dtype=np.int32)
                    with open(f"{output_prefix}_packed_pos_delta.buf", "wb") as file_obj:
                        file_obj.write(packed_data.tobytes())
                    with open(f"{output_prefix}_map.buf", "wb") as file_obj:
                        file_obj.write(map_array.tobytes())
                    slot_maps[slot_num] = map_array
                else:
                    with open(f"{output_prefix}_pos_delta.buf", "wb") as file_obj:
                        file_obj.write(data_to_write.tobytes())
            elif use_packed:
                diff_mask = ~np.isclose(base_data, target_data, atol=1e-6).all(axis=1)
                packed_data = target_data[diff_mask]
                map_array = np.full(num_vertices, -1, dtype=np.int32)
                map_array[diff_mask] = np.arange(np.count_nonzero(diff_mask), dtype=np.int32)
                with open(f"{output_prefix}_packed.buf", "wb") as file_obj:
                    file_obj.write(packed_data.tobytes())
                with open(f"{output_prefix}_map.buf", "wb") as file_obj:
                    file_obj.write(map_array.tobytes())
                slot_maps[slot_num] = map_array
            else:
                with open(f"{output_prefix}.buf", "wb") as file_obj:
                    file_obj.write(target_data.astype(np.float32, copy=False).tobytes())

        return slot_maps

    def _write_merged_slot_files(self, logical_hash, runtime_info, hash_slot_data, slot_position_overrides):
        use_delta = resolve_use_delta(self.node)
        actual_hash = runtime_info["actual_hash"]
        base_bytes = runtime_info["base_bytes"]
        struct_definition = self.node._get_vertex_struct_definition()
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0

        merged_index_map = None
        merged_data_parts = []
        next_global_index = 0
        base_data = None
        # 增量记录每条的 float 宽度：关闭「全部顶点属性增量」时恒为 3（仅位置）
        delta_channel_width = 3

        for slot_num, names_data in sorted(hash_slot_data.items()):
            target_bytes = self._compose_slot_bytes(
                logical_hash,
                runtime_info,
                slot_num,
                names_data,
                slot_position_overrides,
            )
            if base_data is None:
                vertex_stride, num_floats_per_vertex, num_vertices = self.node._detect_vertex_format(
                    base_bytes,
                    target_bytes,
                    struct_definition,
                    preferred_stride=runtime_info["position_stride"],
                )
                base_data = np.frombuffer(base_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))
                merged_index_map = np.full((num_vertices, num_slots), -1, dtype=np.int32)

            target_data = np.frombuffer(target_bytes, dtype=np.float32).reshape(base_data.shape)
            if use_delta:
                channel_columns = self._delta_channel_columns(
                    runtime_info,
                    base_data.shape[1],
                )
                delta_channel_width = len(channel_columns)
                data_to_write = target_data[:, channel_columns] - base_data[:, channel_columns]
                data_to_write[data_to_write == 0] = 0.0
                diff_mask = ~np.isclose(
                    base_data[:, channel_columns],
                    target_data[:, channel_columns],
                    atol=1e-6,
                ).all(axis=1)
            else:
                data_to_write = target_data
                diff_mask = ~np.isclose(base_data, target_data, atol=1e-6).all(axis=1)

            active_count = int(np.count_nonzero(diff_mask))
            if active_count > 0:
                packed_data = data_to_write[diff_mask]
                merged_data_parts.append(packed_data)

                slot_index_map = np.full(base_data.shape[0], -1, dtype=np.int32)
                slot_index_map[diff_mask] = np.arange(next_global_index, next_global_index + active_count, dtype=np.int32)
                merged_index_map[:, slot_num - 1] = slot_index_map
                next_global_index += active_count

        if merged_index_map is None:
            raise ShapeKeyDirectExportError(f"{logical_hash} 未生成任何可用的形态键槽位数据")

        if merged_data_parts:
            merged_data = np.concatenate(merged_data_parts, axis=0)
        else:
            merged_data = np.empty(
                (0, delta_channel_width if use_delta else base_data.shape[1]),
                dtype=np.float32,
            )

        data_suffix = "_merged_packed_pos_delta" if use_delta else "_merged_packed"
        data_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position{data_suffix}.buf")
        map_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_merged_map.buf")
        with open(data_path, "wb") as file_obj:
            file_obj.write(merged_data.tobytes())
        with open(map_path, "wb") as file_obj:
            file_obj.write(merged_index_map.reshape(-1).tobytes())

        return merged_index_map

    def _write_freq_indices(
        self,
        logical_hash,
        actual_hash,
        hash_slot_data,
        unique_names,
        vertex_count,
        calculated_ranges,
        merged_index_map=None,
        slot_index_maps=None,
    ):
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0
        if num_slots <= 0 or vertex_count <= 0:
            return

        name_to_freq_index = {name: index for index, name in enumerate(unique_names)}
        freq_indices = np.full((vertex_count, num_slots), 255, dtype=np.uint32)
        slot_index_maps = slot_index_maps or {}

        for slot_num, names_data in hash_slot_data.items():
            slot_index = slot_num - 1
            index_map = merged_index_map[:, slot_index] if merged_index_map is not None else slot_index_maps.get(slot_num)

            for shapekey_name, objects in names_data.items():
                freq_idx = name_to_freq_index.get(shapekey_name, 255)
                if freq_idx == 255:
                    continue

                for obj_name in objects:
                    range_tuple = calculated_ranges.get(obj_name)
                    if not range_tuple:
                        continue

                    start_v, end_v = range_tuple[:2]
                    start_v = max(0, min(start_v, vertex_count - 1))
                    end_v = max(0, min(end_v, vertex_count - 1))

                    if index_map is not None:
                        valid_local = np.flatnonzero(index_map[start_v:end_v + 1] >= 0)
                        if valid_local.size > 0:
                            valid_vertices = valid_local + start_v
                            freq_indices[valid_vertices, slot_index] = freq_idx
                    else:
                        freq_indices[start_v:end_v + 1, slot_index] = freq_idx

        output_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_freq_indices.buf")
        with open(output_path, "wb") as file_obj:
            file_obj.write(freq_indices.reshape(-1).tobytes())

    def _compose_slot_bytes(self, logical_hash, runtime_info, slot_index, names_data, slot_position_overrides):
        base_bytes = runtime_info["base_bytes"]
        position_stride = runtime_info["position_stride"]
        slot_bytes = bytearray(base_bytes)

        slot_overrides = slot_position_overrides.get(slot_index, {})
        for objects in names_data.values():
            for obj_name in objects:
                override_entry = slot_overrides.get(obj_name)
                if not override_entry:
                    continue

                obj_hash = self.node._extract_hash_from_name(obj_name)
                obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                if obj_prefix != hash_prefix:
                    continue

                export_indices = np.asarray(override_entry.get("export_indices", []), dtype=np.int32)
                position_bytes = override_entry.get("position_bytes", b"")
                if export_indices.size == 0 or not position_bytes:
                    continue

                expected_bytes = export_indices.size * position_stride
                if len(position_bytes) != expected_bytes:
                    raise ShapeKeyDirectExportError(
                        f"物体 '{obj_name}' 的 Position 数据长度异常，期望={expected_bytes}，实际={len(position_bytes)}"
                    )

                try:
                    apply_position_override_in_place(
                        state_bytes=slot_bytes,
                        position_bytes=position_bytes,
                        export_indices=export_indices,
                        position_stride=position_stride,
                    )
                except ValueError as exc:
                    raise ShapeKeyDirectExportError(str(exc)) from exc

        return bytes(slot_bytes)

    def _resolve_drawib_base_position_bytes(
        self, drawib_model, base_path: str, logical_hash: str
    ) -> bytes:
        """取 DrawIB 级基础 Position 字节，与 DrawIB 级 export_indices 同一索引空间。

        优先按子网格顺序拼接 `<unique_str>-Position.buf`（逐子网格写盘的游戏，例如
        EFMI 多 LOD：同一 DrawIB 的 LOD0/LOD1 各有一个文件，而 `export_indices` 是
        DrawIB 级的、第二个子网格的索引从第一个子网格顶点数处开始）——不拼接就会用
        DrawIB 级索引去采样单子网格文件，直接 `IndexError: index N is out of bounds`。

        无法拼接（单个子网格 / 文件缺失 / 步长不一致 / 合并 IB 的游戏）时回退到
        「按哈希解析出的单个文件」，保持既有行为。
        """
        folder_path = os.path.dirname(str(base_path or ""))
        merged_bytes, submesh_count = assemble_drawib_position_bytes(
            folder_path,
            getattr(drawib_model, "submesh_model_list", []) or [],
        )
        if merged_bytes:
            LOG.info(
                f"直出形态键: DrawIB {logical_hash} 基础 Position 由 {submesh_count} 个子网格缓冲"
                f"拼接（{len(merged_bytes)} 字节），与 DrawIB 级顶点索引对齐"
            )
            return merged_bytes

        with open(base_path, "rb") as file_obj:
            return file_obj.read()

    def _build_runtime_infos(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            base_path, actual_hash = self.node._resolve_position_buffer_path(
                self.mod_export_path,
                "Meshes0000",
                logical_hash,
            )
            if not os.path.exists(base_path):
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 文件不存在 {base_path}")
                continue

            drawib_model = self._match_drawib_model(actual_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配基础 DrawIB 模型")
                continue

            base_bytes = self._resolve_drawib_base_position_bytes(
                drawib_model, base_path, logical_hash
            )

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 Position 文件")

        return runtime_infos

    def _build_runtime_infos_from_exporter_buffers(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            base_path, actual_hash = self.node._resolve_position_buffer_path(
                self.mod_export_path,
                "Meshes0000",
                logical_hash,
            )
            if not os.path.exists(base_path):
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 文件不存在 {base_path}")
                continue

            drawib_model = self._match_drawib_model(actual_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配基础 DrawIB 模型")
                continue

            shapekey_buffers = getattr(drawib_model, "shapekey_name_bytelist_dict", {}) or {}
            if not shapekey_buffers:
                continue

            base_bytes = self._resolve_drawib_base_position_bytes(
                drawib_model, base_path, logical_hash
            )

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "shapekey_buffers": shapekey_buffers,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 ShapeKey 数据")

        return runtime_infos

    def _build_runtime_infos_from_exporter_memory(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            drawib_model = self._match_drawib_model(logical_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配内存中的 DrawIB 模型")
                continue

            category_buffer_dict = getattr(drawib_model, "category_buffer_dict", {}) or {}
            position_buffer = category_buffer_dict.get("Position")
            if position_buffer is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 内存中无 Position 缓冲")
                continue

            base_bytes = _buffer_to_bytes(position_buffer)

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            draw_ib = getattr(drawib_model, "draw_ib", logical_hash)
            actual_hash = draw_ib if draw_ib else logical_hash

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": "",
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 Position 数据（内存回退）")

        return runtime_infos

    def _parse_hash_to_base_resources(self, sections):
        return collect_base_position_resource_map(sections, self.node._extract_hash_prefix)

    def _update_ini_sections(
        self,
        sections,
        preserved_tail_content,
        target_ini_file,
        slot_to_name_to_objects,
        unique_hashes,
        hash_to_objects,
        all_unique_names,
        all_unique_objects,
        calculated_ranges,
        hash_to_stride,
        hash_to_actual_file_hash,
        hash_to_vertex_count,
        hash_slot_data_map,
        hash_to_base_resources,
        use_packed,
        use_delta,
        use_optimized,
        merge_slot_files,
        preserved_driver_content="",
        drag_drive_resource=None,
    ):
        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        constants_lines = sections['[Constants]']
        constants_content = "".join(constants_lines)
        vars_to_define = set()

        shapekey_freq_params = {}
        for name in all_unique_names:
            shapekey_freq_params[name] = self.node.get_shape_key_export_variable_name(name)

        intensity_lines = []
        for name, param in shapekey_freq_params.items():
            if param not in constants_content:
                intensity_lines.append(f"; 控制形态键 '{name}' 的强度")
                intensity_lines.append(f"global persist {param} = 0.0")

        if intensity_lines:
            constants_lines.append("\n; --- Auto-generated Shape Key Intensity Controls (Additive Blending) ---")
            constants_lines.extend(intensity_lines)

        vertex_range_vars = {}
        if not use_optimized:
            existing_vertex_range_names = set()
            vertex_range_lines = []
            for obj_name, range_tuple in calculated_ranges.items():
                start_v, end_v = range_tuple[:2]
                if start_v is None:
                    continue
                safe_name = self.node._create_safe_var_name(
                    obj_name.replace("-", "_"),
                    existing_names=existing_vertex_range_names,
                )
                start_var = f"$SV_{safe_name}"
                end_var = f"$EV_{safe_name}"
                vertex_range_vars[obj_name] = (start_var, end_var)
                if start_var not in constants_content:
                    vertex_range_lines.append(f"global {start_var} = {start_v}")
                if end_var not in constants_content:
                    vertex_range_lines.append(f"global {end_var} = {end_v}")

            if vertex_range_lines:
                constants_lines.append("\n; --- Auto-generated Vertex Ranges for Shape Keys ---")
                constants_lines.extend(vertex_range_lines)

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            res_to_post = base_resources if base_resources else [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]
            for res_name in res_to_post:
                ensure_resource_alias_section(
                    sections,
                    res_name,
                    "_0",
                    source_candidates=[res_name],
                )
                if f"post {res_name} = copy_desc" not in constants_content:
                    constants_lines.append(f"post {res_name} = copy_desc {res_name}_0")
            if len(base_resources) > 1:
                # 内部基础网格选择器不可复用 MultiFile 的公开动画帧参数。
                vars_to_define.add("$ssmt_sk_base_mesh")
            if f"post run = CustomShader_{logical_hash}_Anim" not in constants_content:
                constants_lines.append(f"post run = CustomShader_{logical_hash}_Anim")

        base_mesh_switch_lines = []
        for var in sorted(vars_to_define):
            if f"global persist {var}" not in constants_content and f"global {var}" not in constants_content:
                base_mesh_switch_lines.append(f"global persist {var} = 1")
        if base_mesh_switch_lines:
            constants_lines.append("\n; --- Auto-generated Base Mesh Switch Key ---")
            constants_lines.extend(base_mesh_switch_lines)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        present_lines = sections['[Present]']
        rebuilt_present_lines = []
        generated_run_lines = {
            f"run = CustomShader_{logical_hash}_Anim"
            for logical_hash in unique_hashes
        } | {
            f"run = CustomShaderShapeKeyWeightSync_{logical_hash}"
            for logical_hash in unique_hashes
        }
        line_index = 0
        while line_index < len(present_lines):
            stripped_line = present_lines[line_index].strip()
            if stripped_line == self._PRESENT_RUN_BEGIN:
                end_index = line_index + 1
                while end_index < len(present_lines):
                    if present_lines[end_index].strip() == self._PRESENT_RUN_END:
                        break
                    end_index += 1
                if end_index < len(present_lines):
                    line_index = end_index + 1
                    continue
            if stripped_line == 'if $active0 == 1':
                end_index = line_index + 1
                inner_lines = []
                while end_index < len(present_lines):
                    candidate_line = present_lines[end_index]
                    candidate_stripped = candidate_line.strip()
                    if candidate_stripped == 'endif':
                        break
                    inner_lines.append(candidate_stripped)
                    end_index += 1

                if (
                    end_index < len(present_lines)
                    and inner_lines
                    and {line for line in inner_lines if line} == generated_run_lines
                ):
                    line_index = end_index + 1
                    continue

            if stripped_line in generated_run_lines:
                line_index += 1
                continue

            rebuilt_present_lines.append(present_lines[line_index])
            line_index += 1

        # 签名变量：本帧所有形态键强度（+ 基础网格切换开关）。没变化就不 dispatch，
        # 拖拽臂动（Alt）期间强制 dispatch —— 拖拽驱动缓冲每帧都在变，签名看不出来。
        signature_vars = sorted({
            str(param).strip() for param in shapekey_freq_params.values() if str(param).strip()
        })
        if "$ssmt_sk_base_mesh" in vars_to_define:
            signature_vars.append("$ssmt_sk_base_mesh")
        rebuilt_present_lines.extend(self._build_present_run_block(
            unique_hashes,
            signature_vars=signature_vars,
            drag_active_var=self._drag_mode_variable_for(drag_drive_resource),
        ))
        sections['[Present]'] = rebuilt_present_lines

        if signature_vars:
            signature_var, prev_var = self._sk_signature_var_names(unique_hashes)
            for declaration in (
                f"global {signature_var} = 0",
                # 初值 -1：第一帧必然与签名不同 → 至少 dispatch 一次
                f"global {prev_var} = -1",
            ):
                if declaration not in constants_lines:
                    constants_lines.append(declaration)

        compute_blocks_to_add = OrderedDict()
        # 每个 hash 的形态键数量：权重缓冲的 array 容量（供资源段使用）
        hash_weight_counts = {}
        for logical_hash in unique_hashes:
            hash_objects = hash_to_objects.get(logical_hash, [])
            hash_slot_data = hash_slot_data_map.get(logical_hash, {})
            if not hash_slot_data:
                continue

            hash_unique_names = list(
                OrderedDict.fromkeys(name for slot_data in hash_slot_data.values() for name in slot_data.keys())
            )
            hash_unique_objects = list(
                OrderedDict.fromkeys(
                    obj
                    for slot_data in hash_slot_data.values()
                    for name_data in slot_data.values()
                    for obj in name_data
                    if obj in hash_objects
                )
            )

            block_name = f"[CustomShader_{logical_hash}_Anim]"
            # 形态键强度不再直读共享表：写进打包中转窗口 → 权重同步 CS 搬进
            # mod 专属权重缓冲 → 窗口立即清零。与 node_postprocess 路径同格式。
            _weight_h_prefix = self.node._extract_hash_prefix(logical_hash)
            _weight_base_resources = hash_to_base_resources.get(_weight_h_prefix, [])
            _weight_primary_base = (
                _weight_base_resources[0]
                if _weight_base_resources
                else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
            )
            weight_resource = derive_shapekey_weight_resource_name(_weight_primary_base)
            sync_lines = ["\n    ; --- Shape Key Weight Transfer (packed 4 keys per float4) ---"]
            for index, name in enumerate(hash_unique_names):
                freq_param = shapekey_freq_params.get(name)
                if freq_param:
                    sync_lines.append(
                        f"    {'xyzw'[index % 4]}{self.node.INTENSITY_START_INDEX + index // 4} = {freq_param} \n; {name}"
                    )
            sync_lines.extend([
                f"    cs = {WEIGHT_SYNC_SHADER_REL}",
                f"    cs-u0 = {weight_resource}",
                f"    Dispatch = {max(1, (len(hash_unique_names) + 63) // 64)}, 1, 1",
                "    cs-u0 = null",
            ])
            # 搬运后立即清零：共享表里不留形态键值，别的 mod 即使也占用这些槽位，
            # 渲染期读到的也永远是 0。
            for index in range(len(hash_unique_names)):
                sync_lines.append(
                    f"    {'xyzw'[index % 4]}{self.node.INTENSITY_START_INDEX + index // 4} = 0"
                )

            block_lines = []
            if not use_optimized:
                block_lines.append("\n    ; --- Per-Object Vertex Range Controls ---")
                for index, obj_name in enumerate(hash_unique_objects):
                    if obj_name not in calculated_ranges or calculated_ranges[obj_name][0] is None:
                        continue
                    start_var, end_var = vertex_range_vars.get(obj_name, ("$SV_unknown", "$EV_unknown"))
                    block_lines.append(f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2} = {start_var} \n; {obj_name} Start")
                    block_lines.append(f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2 + 1} = {end_var} \n; {obj_name} End")

            t_registers_to_null = []
            slots_for_hash = sorted(hash_slot_data.keys())
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
            if not use_delta:
                block_lines.append(f"\n    cs-t50 = copy {derive_shapekey_base_resource_name(primary_base_resource)}")
                t_registers_to_null.append("cs-t50")

            mode_str = (
                f"紧凑:{'是' if use_packed else '否'}, "
                f"增量:{self.node._describe_delta_scope(use_delta)}, "
                f"优化查找:{'是' if use_optimized else '否'}, "
                f"文件合并:{'是' if merge_slot_files else '否'}"
            )
            block_lines.append(f"\n    ; --- Binding Shape Key Meshess (Mode: {mode_str}) ---")
            if merge_slot_files:
                block_lines.append(f"    cs-t51 = copy {derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}")
                block_lines.append(f"    cs-t52 = copy {derive_shapekey_merged_map_resource_name(primary_base_resource)}")
                t_registers_to_null.extend(["cs-t51", "cs-t52"])

                if use_optimized:
                    block_lines.append(f"    cs-t53 = copy {derive_shapekey_freq_resource_name(primary_base_resource)}")
                    t_registers_to_null.append("cs-t53")
            else:
                res_suffix = "_packed_pos_delta" if use_packed and use_delta else "_pos_delta" if use_delta else "_packed" if use_packed else ""
                for slot_num in slots_for_hash:
                    res_name = derive_shapekey_slot_resource_name(primary_base_resource, slot_num, res_suffix if (use_packed or use_delta) else "")

                    t_reg = 51 + slot_num - 1
                    block_lines.append(f"    cs-t{t_reg} = copy {res_name}")
                    t_registers_to_null.append(f"cs-t{t_reg}")
                    if use_packed:
                        map_reg = 75 + slot_num - 1
                        block_lines.append(
                            f"    cs-t{map_reg} = copy {derive_shapekey_slot_map_resource_name(primary_base_resource, slot_num)}"
                        )
                        t_registers_to_null.append(f"cs-t{map_reg}")

                if use_optimized:
                    block_lines.append(f"    cs-t99 = copy {derive_shapekey_freq_resource_name(primary_base_resource)}")
                    t_registers_to_null.append("cs-t99")

            if drag_drive_resource:
                block_lines.append("\n    ; --- Drag ShapeKey Drive ---")
                block_lines.append(f"    cs-t{self.node.DRAG_DRIVE_REGISTER} = {drag_drive_resource}")
                click_resource = self.node._drag_shapekey_click_count_resource_name(target_ini_file)
                if click_resource:
                    block_lines.append(f"    cs-t{self.node.DRAG_CLICK_COUNT_REGISTER} = {click_resource}")
                t_registers_to_null.append(f"cs-t{self.node.DRAG_DRIVE_REGISTER}")
                t_registers_to_null.append(f"cs-t{self.node.DRAG_CLICK_COUNT_REGISTER}")

            # 权重缓冲：mod 专属资源，由 CustomShaderShapeKeyWeightSync 每帧搬运
            block_lines.append("\n    ; --- Shape Key Weight Buffer ---")
            block_lines.append(f"    cs-t{self.node.WEIGHT_BUFFER_REGISTER} = {weight_resource}")
            t_registers_to_null.append(f"cs-t{self.node.WEIGHT_BUFFER_REGISTER}")

            block_lines.append(f"    cs = ./res/shapekey_anim_{logical_hash}.hlsl")
            res_to_bind = base_resources if base_resources else [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]
            if len(res_to_bind) > 1:
                block_lines.append(f"\n    ; --- Base Mesh Switching ---")
                for index, res_name in enumerate(res_to_bind, 1):
                    ensure_resource_alias_section(
                        sections,
                        res_name,
                        "_0",
                        source_candidates=[res_name],
                    )
                    block_lines.extend([f"    if $ssmt_sk_base_mesh == {index}", f"        cs-u5 = copy {res_name}_0", f"        {res_name} = ref cs-u5", "    endif"])
            else:
                res_name = res_to_bind[0]
                ensure_resource_alias_section(
                    sections,
                    res_name,
                    "_0",
                    source_candidates=[res_name],
                )
                block_lines.extend([f"    cs-u5 = copy {res_name}_0", f"    {res_name} = ref cs-u5"])

            dispatch_count = self.node._compute_dispatch_group_count(
                hash_to_vertex_count.get(hash_prefix, 0),
                threads_per_group=16,
            )
            block_lines.extend([f"    Dispatch = {dispatch_count}, 1, 1", "    cs-u5 = null", *[f"    {reg} = null" for reg in sorted(list(set(t_registers_to_null)))]] )
            compute_blocks_to_add[block_name] = block_lines
            # 权重搬运段：每帧「写打包窗口 → 搬运 → 清零」，必须排在 Anim 之前
            compute_blocks_to_add[f"[CustomShaderShapeKeyWeightSync_{logical_hash}]"] = sync_lines
            hash_weight_counts[logical_hash] = len(hash_unique_names)

        new_resource_lines = []
        generated_section_names = set()

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
            section_name = f"[{derive_shapekey_base_resource_name(primary_base_resource)}]"
            if section_name not in sections and section_name not in generated_section_names:
                stride = hash_to_stride.get(hash_prefix, 40)
                new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = Meshes0000/{actual_file_hash}-Position.buf", ""])
                generated_section_names.add(section_name)

            # 形态键权重缓冲：mod 专属 RWBuffer（资源名即命名空间），由
            # CustomShaderShapeKeyWeightSync 每帧写入。不给 filename 即零初始化。
            weight_section_name = f"[{derive_shapekey_weight_resource_name(primary_base_resource)}]"
            if weight_section_name not in sections and weight_section_name not in generated_section_names:
                weight_capacity = max(1, int(hash_weight_counts.get(logical_hash, 1)))
                new_resource_lines.extend([
                    weight_section_name,
                    "type = RWBuffer",
                    "format = R32_FLOAT",
                    f"array = {weight_capacity}",
                    "",
                ])
                generated_section_names.add(weight_section_name)

        if merge_slot_files:
            for logical_hash in unique_hashes:
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                if not hash_prefix:
                    continue

                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                base_stride = hash_to_stride.get(hash_prefix, 40)
                data_stride = self._delta_stride_for(logical_hash, base_stride) if use_delta else base_stride
                base_resources = hash_to_base_resources.get(hash_prefix, [])
                primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                data_section = f"[{derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}]"
                data_filename = f"Meshes0000/{actual_file_hash}-Position{self.node._get_merged_data_file_suffix(use_delta)}.buf"
                if data_section not in sections and data_section not in generated_section_names:
                    new_resource_lines.extend([data_section, "type = Buffer", f"stride = {data_stride}", f"filename = {data_filename}", ""])
                    generated_section_names.add(data_section)

                map_section = f"[{derive_shapekey_merged_map_resource_name(primary_base_resource)}]"
                map_filename = f"Meshes0000/{actual_file_hash}-Position_merged_map.buf"
                if map_section not in sections and map_section not in generated_section_names:
                    new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = {map_filename}", ""])
                    generated_section_names.add(map_section)
        else:
            for slot_num, name_data in slot_to_name_to_objects.items():
                for obj_name in [obj for _, objects in name_data.items() for obj in objects]:
                    logical_hash = self.node._extract_hash_from_name(obj_name)
                    hash_prefix = self.node._extract_hash_prefix(logical_hash) if logical_hash else None
                    if not hash_prefix:
                        continue
                    actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                    base_stride = hash_to_stride.get(hash_prefix, 40)
                    base_resources = hash_to_base_resources.get(hash_prefix, [])
                    primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                    if use_delta:
                        res_suffix = "_packed_pos_delta" if use_packed else "_pos_delta"
                        stride = self._delta_stride_for(logical_hash, base_stride)
                    elif use_packed:
                        res_suffix = "_packed"
                        stride = base_stride
                    else:
                        res_suffix = ""
                        stride = base_stride

                    if use_delta or use_packed:
                        section_name = f"[{derive_shapekey_slot_resource_name(primary_base_resource, slot_num, res_suffix)}]"
                        filename = f"Meshes0000/{actual_file_hash}-Position1{slot_num:03d}{res_suffix}.buf"
                        if section_name not in sections and section_name not in generated_section_names:
                            new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = {filename}", ""])
                            generated_section_names.add(section_name)

                    if use_packed:
                        map_section = f"[{derive_shapekey_slot_map_resource_name(primary_base_resource, slot_num)}]"
                        if map_section not in sections and map_section not in generated_section_names:
                            new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position1{slot_num:03d}_map.buf", ""])
                            generated_section_names.add(map_section)

        if use_optimized:
            for logical_hash in unique_hashes:
                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                base_resources = hash_to_base_resources.get(hash_prefix, [])
                primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                freq_idx_section = f"[{derive_shapekey_freq_resource_name(primary_base_resource)}]"
                if freq_idx_section not in sections and freq_idx_section not in generated_section_names:
                    new_resource_lines.extend([freq_idx_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position_freq_indices.buf", ""])
                    generated_section_names.add(freq_idx_section)

        if new_resource_lines:
            sections[";; --- Generated Shape Key Meshess ---"] = new_resource_lines

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            for res_name in hash_to_base_resources.get(hash_prefix, [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]):
                ensure_resource_alias_section(
                    sections,
                    res_name,
                    "_0",
                    source_candidates=[res_name],
                )

        sections.update(compute_blocks_to_add)
        self.node._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
