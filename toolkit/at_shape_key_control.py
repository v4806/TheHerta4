# -*- coding: utf-8 -*-

import bpy
import math
import mathutils
import re
import time
import numpy as np


SEP = "|"  # 分组内形态键名序列化分隔符
DEFAULT_VALUE_MIN = 0.0
DEFAULT_VALUE_MAX = 1.0
EPS = 1e-4

# 刷新/批量写值期间抑制属性回调，避免递归与重复写值
_suppress_update = False

# 「相同前缀 + 连续数字编号」形态键的识别模式，例如 Motion_Key_3
_NAME_PATTERN = re.compile(r'^(.+)_(\d+)$')


# ============================================================
# 名称解析 / 连续形态键分组
# ============================================================

def parse_shape_key_name(name):
    """把 'Motion_Key_3' 解析为 ('Motion_Key', 3)；不匹配返回 None。"""
    match = _NAME_PATTERN.match(str(name or ""))
    if match:
        return match.group(1), int(match.group(2))
    return None


def _flush_group_run(groups, prefix, run):
    """把一段连续编号（成员 >= 2）收进分组结果。"""
    if len(run) < 2:
        return
    names = [name for _index, name in run]
    label = f"{prefix} [{run[0][0]}-{run[-1][0]}]"
    groups.append((prefix, names, label))


def build_continuous_groups(key_names):
    """把「相同前缀 + 连续数字编号」的形态键聚成可整体拖动的组。

    返回 [(prefix, [有序形态键名, ...], 显示标签), ...]；
    只有成员数 >= 2 的连续段才成为分组，断号处自动拆成多段。
    """
    buckets = {}
    for name in key_names:
        parsed = parse_shape_key_name(name)
        if parsed is None:
            continue
        prefix, index = parsed
        buckets.setdefault(prefix, []).append((index, name))

    groups = []
    for prefix in sorted(buckets.keys()):
        ordered = sorted(buckets[prefix], key=lambda pair: pair[0])
        run = [ordered[0]]
        for entry in ordered[1:]:
            if entry[0] == run[-1][0] + 1:
                run.append(entry)
                continue
            _flush_group_run(groups, prefix, run)
            run = [entry]
        _flush_group_run(groups, prefix, run)
    return groups


def split_key_names(serialized):
    return [name for name in str(serialized or "").split(SEP) if name]


def item_key_names(item):
    """列表项对应的形态键名列表（单键一项，分组多项）。"""
    if item.is_group:
        return split_key_names(item.key_names)
    name = item.key_names or item.name
    return [name] if name else []


# ============================================================
# 值域（最小值 / 最大值）
# ============================================================

def clamp01(value):
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return float(value)


def get_value_range(props):
    """当前值域 (min, max)；设置非法（max <= min）时返回 None。"""
    value_min = float(getattr(props, "sk_value_min", DEFAULT_VALUE_MIN))
    value_max = float(getattr(props, "sk_value_max", DEFAULT_VALUE_MAX))
    if value_max <= value_min:
        return None
    return value_min, value_max


def to_normalized(actual_value, props):
    """实际形态键值 -> 分组滑块位置比例 (0~1)。"""
    value_range = get_value_range(props)
    if value_range is None:
        return 0.0
    value_min, value_max = value_range
    return clamp01((float(actual_value) - value_min) / (value_max - value_min))


def to_actual(normalized_value, props):
    """分组滑块位置比例 (0~1) -> 实际形态键值。"""
    value_range = get_value_range(props)
    if value_range is None:
        return 0.0
    value_min, value_max = value_range
    return value_min + clamp01(normalized_value) * (value_max - value_min)


# ============================================================
# 物体 / 键块定位
# ============================================================

def _iter_shape_key_objects(objects):
    for obj in objects or []:
        if obj is None or getattr(obj, "type", None) != 'MESH':
            continue
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        if shape_keys:
            yield obj


def driving_objects(context):
    """统一控制器的驱动集合 = 场景里所有带形态键的网格物体。

    这就是「统一控制所有同名形态键」的全部含义：拖动某一行时，凡是**有这个同名
    形态键**的物体都会被写成同一个值 —— 不看选择、不看活动物体、不看可见性。
    活动物体排在集合最前（仅影响遍历顺序）；取不到场景物体时退回所选物体，
    保证任何上下文都不会退化成空集。
    """
    active_object = getattr(context, "active_object", None)
    ordered = _ordered_objects(getattr(getattr(context, "scene", None), "objects", None), active_object)
    if not ordered:
        ordered = _ordered_objects(getattr(context, "selected_objects", None), active_object)
    return ordered


def _ordered_objects(objects, active_object=None):
    ordered = list(_iter_shape_key_objects(objects))
    if active_object is not None and active_object in ordered:
        ordered.remove(active_object)
        ordered.insert(0, active_object)
    return ordered


def get_key_block(obj, key_name):
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    if not shape_keys:
        return None
    return shape_keys.key_blocks.get(key_name)


def collect_shape_key_values(objects):
    """返回 {形态键名: 首个取到的实际值}，跳过 Basis / 参考键。"""
    values = {}
    for obj in _iter_shape_key_objects(objects):
        shape_keys = obj.data.shape_keys
        reference_key = getattr(shape_keys, "reference_key", None)
        for key_block in shape_keys.key_blocks:
            if key_block == reference_key or key_block.name == "Basis":
                continue
            values.setdefault(key_block.name, float(key_block.value))
    return values


def derive_group_position(key_names, key_values, value_range):
    """由当前形态键值反推分组位置 (0~N)：从第一个开始累加，遇到未满的加上它的比例后停止。"""
    value_min, value_max = value_range
    span = value_max - value_min
    total = 0.0
    for name in key_names:
        fraction = clamp01((key_values.get(name, 0.0) - value_min) / span)
        if fraction >= 1.0 - EPS:
            total += 1.0
        elif fraction > EPS:
            total += fraction
            break
        else:
            break
    return total


# ============================================================
# 写值
# ============================================================

def set_key_block_range(key_block, value_min, value_max):
    """把值域写进形态键自身的 slider_min / slider_max（滑块范围随它变化）。"""
    if value_max <= value_min:
        return False
    if (abs(float(key_block.slider_min) - value_min) < 1e-6
            and abs(float(key_block.slider_max) - value_max) < 1e-6):
        return False

    # 先放宽到目标范围，再收紧：避免中途出现 min > max 被 RNA 夹住
    if value_min > float(key_block.slider_max):
        key_block.slider_max = value_max
        key_block.slider_min = value_min
    else:
        key_block.slider_min = value_min
        key_block.slider_max = value_max
    return True


def apply_group_values(context, key_names, group_position, value_range=None):
    """group_position: 0 ~ N；第 i 个键填充 group_position - i（截断到 0~1）。"""
    props = getattr(getattr(context, "scene", None), "atp_props", None)
    if value_range is None:
        value_range = get_value_range(props) if props is not None else None
    if value_range is None or not key_names:
        return

    value_min, value_max = value_range
    span = value_max - value_min

    fractions = {}
    for index, name in enumerate(key_names):
        if group_position >= index + 1:
            fractions[name] = 1.0
        elif group_position > index:
            fractions[name] = group_position - index
        else:
            fractions[name] = 0.0

    for obj in driving_objects(context):
        for name, fraction in fractions.items():
            key_block = get_key_block(obj, name)
            if key_block is None:
                continue
            set_key_block_range(key_block, value_min, value_max)
            key_block.value = value_min + fraction * span


def apply_shape_key_range(context, props):
    """把当前值域写入列表内所有形态键（并夹取越界的实际值）。"""
    global _suppress_update

    value_range = get_value_range(props)
    if value_range is None:
        return False

    value_min, value_max = value_range
    objects = driving_objects(context)
    for item in props.shape_key_list:
        for name in item_key_names(item):
            for obj in objects:
                key_block = get_key_block(obj, name)
                if key_block is None:
                    continue
                set_key_block_range(key_block, value_min, value_max)
                clamped = min(max(float(key_block.value), value_min), value_max)
                if abs(clamped - float(key_block.value)) > 1e-6:
                    key_block.value = clamped

    # 值域只夹取各自的值，不做任何跨物体联动；顺便把列表记录值刷新成夹取后的实际值
    was_suppressed = _suppress_update
    _suppress_update = True
    try:
        for item in props.shape_key_list:
            if item.is_group:
                continue  # 分组行的滑块是 group_value，item.value 与它无关
            names = item_key_names(item)
            for obj in objects:
                key_block = get_key_block(obj, names[0]) if names else None
                if key_block is not None:
                    item.value = float(key_block.value)
                    break
    finally:
        _suppress_update = was_suppressed
    return True


# ============================================================
# 属性回调
# ============================================================

def on_group_update(item, context=None):
    """连续形态键分组滑块变动：按顺序整体填充组内形态键。"""
    if _suppress_update:
        return
    context = context if context is not None else bpy.context
    props = getattr(getattr(context, "scene", None), "atp_props", None)
    if props is None:
        return

    names = split_key_names(item.key_names)
    if not names:
        return

    apply_group_values(context, names, item.group_value * len(names))
    _safe_view_update()


def _write_item_value_quiet(item, value):
    """在抑制回调的前提下把值写回列表项，避免递归触发 update 回调。"""
    global _suppress_update
    previous = _suppress_update
    _suppress_update = True
    try:
        item.value = value
    finally:
        _suppress_update = previous


def on_single_value_update(item, context=None):
    """单键滑块变动：把这一行的值写到驱动范围内**所有**同名形态键上。

    走属性 update 回调（而不是 depsgraph 处理器）：只要滑块动了就一定会触发，
    不依赖场景更新事件，也不受动画播放 / 联动开关影响，这是最直接可靠的一条路。
    """
    if _suppress_update:
        return
    context = context if context is not None else bpy.context
    props = getattr(getattr(context, "scene", None), "atp_props", None)
    if props is None:
        return

    names = item_key_names(item)
    if not names:
        return

    value_range = get_value_range(props) or (DEFAULT_VALUE_MIN, DEFAULT_VALUE_MAX)
    value_min, value_max = value_range
    value = min(max(float(item.value), value_min), value_max)
    if abs(value - float(item.value)) > 1e-6:
        # 越界输入回写滑块本身，否则滑块显示的值和实际写进形态键的值会对不上
        _write_item_value_quiet(item, value)

    apply_single_values(context, names, value, value_range)
    _safe_view_update()


def on_value_range_update(props, context=None):
    """最小值/最大值变动：列表内所有形态键的滑块范围与实际值随之变化。"""
    if _suppress_update:
        return
    context = context if context is not None else bpy.context
    if get_value_range(props) is None:
        return

    apply_shape_key_range(context, props)
    _safe_view_update()


def _safe_view_update():
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass


# ============================================================
# 单键写值（一行 -> 驱动范围内所有同名形态键）
# ============================================================

def apply_single_values(context, key_names, value, value_range=None):
    """把驱动范围内所有同名形态键统一写成同一个值，返回实际写到的键数量。

    这是统一控制器的核心语义：一行滑块对应一个形态键名，拖动/输入多少，
    场景里**每一个**有这个同名键的物体就都变成多少。
    """
    props = getattr(getattr(context, "scene", None), "atp_props", None)
    if value_range is None:
        value_range = get_value_range(props) if props is not None else None
    if value_range is None:
        value_range = (DEFAULT_VALUE_MIN, DEFAULT_VALUE_MAX)
    value_min, value_max = value_range

    # 显式夹取到值域，不依赖 RNA 对滑块范围的隐式裁剪
    clamped = min(max(float(value), value_min), value_max)

    written = 0
    for obj in driving_objects(context):
        for name in key_names:
            key_block = get_key_block(obj, name)
            if key_block is None:
                continue
            set_key_block_range(key_block, value_min, value_max)
            key_block.value = clamped
            written += 1
    return written


# ============================================================
# 刷新
# ============================================================

def refresh_shape_key_list(scene_props, objects, active_object=None):
    """重建统一控制器列表：连续形态键合成分组行，其余为单键行。

    每一行的滑块都绑定在这一行自己的属性上（单键行 = item.value，分组行 =
    item.group_value），拖动时由属性回调把值写到驱动范围内所有同名形态键。
    刷新时用第一个持有者的实际值回填，所以滑块显示的就是真实值。

    返回找到的全部形态键名集合。
    """
    global _suppress_update

    ordered_objects = _ordered_objects(objects, active_object)
    key_values = collect_shape_key_values(ordered_objects)
    found_keys = set(key_values.keys())

    _suppress_update = True
    try:
        scene_props.shape_key_list.clear()

        value_range = get_value_range(scene_props) or (DEFAULT_VALUE_MIN, DEFAULT_VALUE_MAX)

        grouped_names = set()
        if bool(getattr(scene_props, "sk_use_grouping", True)):
            for prefix, names, label in build_continuous_groups(found_keys):
                item = scene_props.shape_key_list.add()
                item.is_group = True
                item.name = prefix
                item.label = label
                item.key_names = SEP.join(names)
                position = derive_group_position(names, key_values, value_range)
                item.group_value = clamp01(position / len(names))
                grouped_names.update(names)

        for key_name in sorted(found_keys - grouped_names):
            item = scene_props.shape_key_list.add()
            item.is_group = False
            item.name = key_name
            item.label = key_name
            item.key_names = key_name
            item.value = key_values[key_name]

        index = int(getattr(scene_props, "shape_key_list_index", 0) or 0)
        if index >= len(scene_props.shape_key_list):
            scene_props.shape_key_list_index = max(0, len(scene_props.shape_key_list) - 1)
    finally:
        _suppress_update = False

    return found_keys


def refresh_from_context(context):
    """按当前场景刷新统一控制器列表（连续形态键自动合并为一行）。"""
    targets = driving_objects(context)
    return refresh_shape_key_list(
        context.scene.atp_props,
        targets,
        getattr(context, "active_object", None),
    )


# ============================================================
# 操作符
# ============================================================

class ATP_OT_RefreshShapeKeys(bpy.types.Operator):
    bl_idname = "atp.refresh_shape_keys"
    bl_label = "刷新形态键列表"
    bl_description = "扫描场景里所有物体的形态键，重建统一形态键控制列表（连续形态键自动合并为一行）"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return getattr(getattr(context, "scene", None), "atp_props", None) is not None

    def execute(self, context):
        props = context.scene.atp_props
        found_keys = refresh_from_context(context)
        if not found_keys:
            self.report({'INFO'}, "没有找到可控制的形态键。")
            return {'CANCELLED'}

        group_count = len([item for item in props.shape_key_list if item.is_group])
        if group_count > 0:
            self.report({'INFO'}, f"找到 {len(found_keys)} 个唯一形态键，合并为 {group_count} 组连续形态键。")
        else:
            self.report({'INFO'}, f"找到 {len(found_keys)} 个唯一形态键。")
        return {'FINISHED'}


class ATP_OT_ApplyShapeKeyRange(bpy.types.Operator):
    bl_idname = "atp.apply_shape_key_range"
    bl_label = "应用值域"
    bl_description = "把当前最小值/最大值写进列表内所有形态键的滑块范围，并夹取越界的值"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.atp_props.shape_key_list)

    def execute(self, context):
        props = context.scene.atp_props
        if get_value_range(props) is None:
            self.report({'ERROR'}, "最大值必须大于最小值。")
            return {'CANCELLED'}

        apply_shape_key_range(context, props)
        self.report({'INFO'}, f"已应用值域 {props.sk_value_min:g} ~ {props.sk_value_max:g}。")
        return {'FINISHED'}


class ATP_OT_ResetShapeKeyRange(bpy.types.Operator):
    bl_idname = "atp.reset_shape_key_range"
    bl_label = "恢复 0 ~ 1"
    bl_description = "把最小值/最大值恢复为 0 / 1，并重新应用到列表内形态键"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _suppress_update
        props = context.scene.atp_props

        _suppress_update = True
        try:
            props.sk_value_min = DEFAULT_VALUE_MIN
            props.sk_value_max = DEFAULT_VALUE_MAX
        finally:
            _suppress_update = False

        apply_shape_key_range(context, props)
        self.report({'INFO'}, "值域已恢复为 0 ~ 1。")
        return {'FINISHED'}


class ATP_OT_CopyShapeKeys(bpy.types.Operator):
    """将活动物体的形态键相对位移复制到其他选中的同拓扑物体上。"""
    bl_idname = "atp.copy_shape_keys"
    bl_label = "复制形态键到选中项"
    bl_description = "将活动物体的全部形态键复制到其他顶点数相同的选中网格物体"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        return (
            active_obj is not None
            and active_obj.type == 'MESH'
            and active_obj.data.shape_keys is not None
            and len(context.selected_objects) > 1
        )

    def execute(self, context):
        source_obj = context.active_object
        all_targets = [obj for obj in context.selected_objects if obj != source_obj]

        if not source_obj.data.shape_keys or not source_obj.data.shape_keys.key_blocks:
            self.report({'ERROR'}, "源物体没有可复制的形态键。")
            return {'CANCELLED'}

        source_mesh = source_obj.data
        source_vtx_count = len(source_mesh.vertices)

        valid_targets = [
            target for target in all_targets
            if target.type == 'MESH' and len(target.data.vertices) == source_vtx_count
        ]
        skipped_count = len(all_targets) - len(valid_targets)

        if not valid_targets:
            self.report({'ERROR'}, f"未找到顶点数为 {source_vtx_count} 的目标物体。")
            return {'CANCELLED'}

        start_time = time.time()

        source_keys = source_mesh.shape_keys
        if not source_keys.reference_key:
            self.report({'ERROR'}, f"源物体 '{source_obj.name}' 缺少 Basis。")
            return {'CANCELLED'}

        basis_key = source_keys.reference_key
        source_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
        basis_key.data.foreach_get("co", source_basis_coords)
        source_basis_coords = source_basis_coords.reshape(-1, 3)

        keys_deltas = {}
        for key_block in source_keys.key_blocks:
            if key_block == basis_key:
                continue

            key_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            key_block.data.foreach_get("co", key_coords)
            key_coords = key_coords.reshape(-1, 3)

            keys_deltas[key_block.name] = {
                "delta": key_coords - source_basis_coords,
                "value": key_block.value,
                "slider_min": key_block.slider_min,
                "slider_max": key_block.slider_max,
                "mute": key_block.mute,
            }

        for target in valid_targets:
            target_mesh = target.data
            if not target_mesh.shape_keys:
                target.shape_key_add(name="Basis")

            target_keys = target_mesh.shape_keys
            target_basis_key = target_keys.reference_key
            if not target_basis_key:
                self.report({'WARNING'}, f"跳过目标 '{target.name}'，因为它缺少 Basis。")
                continue

            target_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            target_basis_key.data.foreach_get("co", target_basis_coords)
            target_basis_coords = target_basis_coords.reshape(-1, 3)

            props = context.scene.atp_props
            if props.copy_sk_use_manual_rotation:
                rot_x = math.radians(props.copy_sk_rotation_x)
                rot_y = math.radians(props.copy_sk_rotation_y)
                rot_z = math.radians(props.copy_sk_rotation_z)

                rot_matrix_x = mathutils.Matrix.Rotation(rot_x, 4, 'X').to_3x3()
                rot_matrix_y = mathutils.Matrix.Rotation(rot_y, 4, 'Y').to_3x3()
                rot_matrix_z = mathutils.Matrix.Rotation(rot_z, 4, 'Z').to_3x3()
                transform_matrix = rot_matrix_z @ rot_matrix_y @ rot_matrix_x
            else:
                source_matrix = source_obj.matrix_world
                target_matrix_inv = target.matrix_world.inverted()
                transform_matrix = (target_matrix_inv @ source_matrix).to_3x3()

            for key_name, key_info in keys_deltas.items():
                target_key_block = target_keys.key_blocks.get(key_name)
                if not target_key_block:
                    target_key_block = target.shape_key_add(name=key_name, from_mix=False)

                transformed_delta = key_info["delta"] @ transform_matrix.transposed()
                new_coords = (target_basis_coords + transformed_delta).reshape(-1)

                target_key_block.data.foreach_set("co", new_coords)
                target_key_block.slider_min = key_info["slider_min"]
                target_key_block.slider_max = key_info["slider_max"]
                target_key_block.value = key_info["value"]
                target_key_block.mute = key_info["mute"]

            target_mesh.update()

        elapsed = time.time() - start_time
        self.report({'INFO'}, f"已复制 {len(keys_deltas)} 个形态键到 {len(valid_targets)} 个物体，耗时 {elapsed:.3f}s。")
        if skipped_count > 0:
            self.report({'WARNING'}, f"跳过了 {skipped_count} 个顶点数不匹配的物体。")

        return {'FINISHED'}


at_shape_key_control_list = (
    ATP_OT_RefreshShapeKeys,
    ATP_OT_ApplyShapeKeyRange,
    ATP_OT_ResetShapeKeyRange,
    ATP_OT_CopyShapeKeys,
)
