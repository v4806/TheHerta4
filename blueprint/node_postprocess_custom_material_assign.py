# -*- coding: utf-8 -*-
"""自定义材质指定：仅对用户在节点中指定的部件生成材质/贴图资源引用。

该节点复用“材质转资源”的完整生成逻辑，但处理前会先按目标物体白名单过滤：
未在列表中指定的 TextureOverride 段（即其它部件）不会被触碰，保持默认配置。
"""
import bpy
import json
import re
import time
from collections import OrderedDict

from .node_postprocess_material import (
    MATERIAL_DETECT_PRESETS,
    SSMTNode_PostProcess_MaterialBase,
)


NODE_IDNAME = "SSMTNode_PostProcess_CustomMaterialAssign"
PICK_TIMEOUT_SECONDS = 30.0
RESTORE_MARKER_START = "; --- CustomMaterialAssign Restore Default ---"
RESTORE_MARKER_END = "; --- End CustomMaterialAssign Restore Default ---"

# 吸管拾取状态（与现有 node_obj.py 的 3D 视图吸管风格一致）。
_pick_context = {
    "node_name": "",
    "tree_name": "",
    "item_index": -1,
}

_switch_sync_guard = False


def _sync_switch_variable_fields(group, context):
    """同一切换变量的启用状态、备注与按键保持完全一致（限定在所属蓝图内）。

    一个切换变量在 UI 上只有一套控件、在 INI 里也只有一段 ``[KeySwap_Diffuse_*]``，
    所以这三项必须同变量同值；否则生成侧会被"最后一个组"的值覆盖。
    """
    global _switch_sync_guard
    if _switch_sync_guard:
        return
    variable = str(getattr(group, "switch_variable", "") or "").strip()
    if not variable:
        return
    # 只同步触发变量所属的蓝图树，避免不同蓝图同名变量互相干扰。
    try:
        owner_tree = group.id_data.id_data
    except Exception:
        owner_tree = None
    _switch_sync_guard = True
    try:
        enabled = bool(getattr(group, "enabled", True))
        comment = str(getattr(group, "comment", "") or "")
        hotkey = str(getattr(group, "key", "") or "")
        trees = [owner_tree] if owner_tree is not None else list(bpy.data.node_groups)
        for tree in trees:
            for node in getattr(tree, "nodes", []) or []:
                if getattr(node, "bl_idname", "") != NODE_IDNAME:
                    continue
                collections = [getattr(node, "global_switch_groups", [])]
                collections.extend(
                    getattr(item, "switch_groups", [])
                    for item in getattr(node, "target_items", [])
                )
                for collection in collections:
                    for candidate in collection:
                        if str(getattr(candidate, "switch_variable", "") or "").strip() == variable:
                            if candidate is group:
                                continue
                            candidate.enabled = enabled
                            candidate.comment = comment
                            candidate.key = hotkey
    finally:
        _switch_sync_guard = False


def _pick_variable_control_group(groups):
    """同一变量下的"控制组"：第一个填了按键的组，都没填则取第一个组。

    与 UI 控件组（``_draw_global_switch_panel`` 取 ``entries[0]``）和贴图切换
    面板取其第一个非空 hotkey 的既有口径一致；用户在一个共享框里填的键必须
    落到整个变量上。
    """
    fallback = None
    for group in groups:
        if fallback is None:
            fallback = group
        if str(getattr(group, "key", "") or "").strip():
            return group
    return fallback


def _normalize_switch_variable_metadata(collections):
    """把同一变量的 备注/按键/启用 归一为该变量控制组的值。

    扫描重建后每个部件组各自带着旧值，同伴可能停在默认 ``N``；归一后
    "看到的控件 = 实际生成的 KeySwap 段"，也不会在下一次导出时回退。
    返回每个变量的控制组，供调用方做一次显式跨节点同步。
    """
    ordered = []
    for collection in collections:
        ordered.extend(collection)
    by_variable = OrderedDict()
    for group in ordered:
        variable = str(getattr(group, "switch_variable", "") or "").strip()
        if variable:
            by_variable.setdefault(variable, []).append(group)
    controls = []
    for groups in by_variable.values():
        control = _pick_variable_control_group(groups)
        if control is None:
            continue
        key = str(getattr(control, "key", "") or "")
        comment = str(getattr(control, "comment", "") or "")
        enabled = bool(getattr(control, "enabled", True))
        for group in groups:
            group.key = key
            group.comment = comment
            group.enabled = enabled
        controls.append(control)
    return controls


def _mesh_object_poll(self, obj):
    return bool(getattr(obj, "type", "") == "MESH")


class SSMT_CustomMaterialAssignSwitchGroup(bpy.types.PropertyGroup):
    """某个目标部件的贴图切换控制组（由全局扫描创建）。"""

    object_name: bpy.props.StringProperty(
        name="部件",
        description="属于哪个部件的贴图切换组（全局模式使用）",
        default="",
        options={"HIDDEN"},
    )
    switch_variable: bpy.props.StringProperty(
        name="材质切换变量",
        description="生成到 INI 的控制变量，默认沿用材质转资源的 $swapkey 命名",
        default="",
    )
    key: bpy.props.StringProperty(
        name="切换按键",
        description="控制该部件多套贴图切换的按键；同一切换变量下的部件共享此按键",
        default="N",
        update=_sync_switch_variable_fields,
    )
    state_count: bpy.props.IntProperty(
        name="切换档数",
        default=2,
        min=2,
        max=64,
    )
    enabled: bpy.props.BoolProperty(
        name="启用切换",
        description="关闭后只使用第一套贴图，不写入按键切换",
        default=True,
        update=_sync_switch_variable_fields,
    )
    comment: bpy.props.StringProperty(
        name="备注",
        description="同一切换变量下的部件共享此备注，并写入 KeySwap 段",
        default="",
        update=_sync_switch_variable_fields,
    )
    bindings: bpy.props.StringProperty(
        name="材质绑定",
        description="内部使用：JSON 形式记录参与同一切换的各前缀材质组",
        default="",
        options={"HIDDEN"},
    )
    merge_group_id: bpy.props.StringProperty(
        name="合并组标识",
        default="",
        options={"HIDDEN"},
    )


class SSMT_CustomMaterialAssignTargetItem(bpy.types.PropertyGroup):
    """目标部件：一个可拖入/选择的大纲视图网格物体。"""

    target_object: bpy.props.PointerProperty(
        name="目标部件",
        description="从大纲视图拖入，或点击右侧吸管在视口/大纲中选择",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    switch_groups: bpy.props.CollectionProperty(
        type=SSMT_CustomMaterialAssignSwitchGroup
    )


def _find_node(context, node_name):
    tree = getattr(context.space_data, "edit_tree", None) or getattr(
        context.space_data, "node_tree", None
    )
    if not tree:
        return None
    node = tree.nodes.get(node_name)
    if node and node.bl_idname == NODE_IDNAME:
        return node
    return None


def _find_material_assign_node_from_blueprint(context):
    trees = []
    window = getattr(context, "window", None)
    screens = [getattr(window, "screen", None)] if window else []
    for candidate_window in getattr(getattr(context, "window_manager", None), "windows", []) or []:
        screen = getattr(candidate_window, "screen", None)
        if screen and screen not in screens:
            screens.append(screen)
    for screen in screens:
        for area in getattr(screen, "areas", []) or []:
            if area.type != "NODE_EDITOR":
                continue
            for space in getattr(area, "spaces", []) or []:
                tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
                if getattr(tree, "bl_idname", "") == "SSMTBlueprintTreeType" and tree not in trees:
                    trees.append(tree)
    for tree in bpy.data.node_groups:
        if getattr(tree, "bl_idname", "") == "SSMTBlueprintTreeType" and tree not in trees:
            trees.append(tree)
    for tree in trees:
        for node in getattr(tree, "nodes", []) or []:
            if getattr(node, "bl_idname", "") == NODE_IDNAME:
                return node
    return None


class SSMT_OT_CustomMaterialAssignAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_add_selected"
    bl_label = "将选中物体加入材质转资源"
    bl_description = "将当前选中的所有网格物体加入材质转资源 Pro 节点"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        node = _find_material_assign_node_from_blueprint(context)
        if node is None:
            self.report({"WARNING"}, "没有找到材质转资源节点")
            return {"CANCELLED"}
        if bool(getattr(node, "use_global_assign", False)):
            self.report({"WARNING"}, "全局指定模式下不支持添加")
            return {"CANCELLED"}
        selected = [obj for obj in (getattr(context, "selected_objects", None) or []) if getattr(obj, "type", "") == "MESH"]
        existing = {item.target_object for item in node.target_items if item.target_object}
        added = 0
        for obj in selected:
            if obj in existing:
                continue
            item = next((candidate for candidate in node.target_items if getattr(candidate, "target_object", None) is None), None)
            if item is None:
                item = node.target_items.add()
            item.target_object = obj
            existing.add(obj)
            added += 1
        if added:
            node.active_target_index = len(node.target_items) - 1
        self.report({"INFO"}, f"已加入 {added} 个部件" if added else "选中物体已在材质转资源中")
        return {"FINISHED"}


def _find_custom_node(node):
    """只接受材质转资源pro 节点（独立检测 operator 使用）。"""
    return bool(node is not None and getattr(node, "bl_idname", "") == NODE_IDNAME)


class SSMT_OT_CustomMaterialDetectAddPrefix(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_add_prefix"
    bl_label = "添加前缀"
    bl_description = "按预设顺序添加下一个材质检测前缀（材质转资源pro 独立）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        for preset in MATERIAL_DETECT_PRESETS:
            if preset not in existing:
                new_item = node.material_detect_prefixes.add()
                new_item.prefix = preset
                return {'FINISHED'}

        self.report({'WARNING'}, "所有预设前缀已添加")
        return {'CANCELLED'}


class SSMT_OT_CustomMaterialDetectRemovePrefix(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_remove_prefix"
    bl_label = "移除前缀"
    bl_description = "移除指定索引的材质检测前缀（材质转资源pro 独立）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node:
            if 0 <= self.item_index < len(node.material_detect_prefixes):
                node.material_detect_prefixes.remove(self.item_index)
        return {'FINISHED'}


class SSMT_OT_CustomMaterialDetectAddCustomPrefix(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_add_custom_prefix"
    bl_label = "添加自定义前缀"
    bl_description = "添加手动输入的材质检测前缀（材质转资源pro 独立）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}

        custom = node.temp_prefix_input.strip()
        if not custom:
            self.report({'WARNING'}, "请输入前缀")
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        if custom in existing:
            self.report({'WARNING'}, f"前缀 '{custom}' 已存在")
            return {'CANCELLED'}

        new_item = node.material_detect_prefixes.add()
        new_item.prefix = custom
        node.temp_prefix_input = ""
        return {'FINISHED'}


class SSMT_OT_CustomMaterialDetect(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect"
    bl_label = "Material Detect (Pro)"
    bl_description = "按材质转资源pro 的指定范围检测缺失前缀材质（含嵌套蓝图）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def _collect_target_object_names(self, node):
        """返回本次检测的部件名列表：非全局=白名单部件，全局=蓝图链路部件。"""
        names = []
        seen = set()
        if bool(getattr(node, "use_global_assign", False)):
            tree = getattr(node, "id_data", None)
            for obj_name in _connected_blueprint_object_names(tree) if tree is not None else []:
                if obj_name in seen:
                    continue
                seen.add(obj_name)
                names.append(obj_name)
        else:
            for item in node.target_items:
                obj = getattr(item, "target_object", None)
                if obj is None or getattr(obj, "type", "") != "MESH":
                    continue
                if obj.name in seen:
                    continue
                seen.add(obj.name)
                names.append(obj.name)
        return names

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}

        prefixes = [item.prefix.strip() for item in node.material_detect_prefixes if item.prefix.strip()]
        if not prefixes:
            self.report({'WARNING'}, 'Add at least one material prefix first')
            return {'CANCELLED'}

        unique_object_names = self._collect_target_object_names(node)
        if not unique_object_names:
            self.report({'WARNING'}, '未找到需要检测的部件（非全局模式请先在材质转资源pro节点中添加目标部件）')
            return {'CANCELLED'}

        node.detected_materials.clear()

        missing_count = 0
        for obj_name in unique_object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                continue

            for prefix in prefixes:
                has_prefix_material = False
                for material_slot in obj.material_slots:
                    material = material_slot.material
                    if material and material.name.startswith(prefix):
                        has_prefix_material = True
                        break

                if has_prefix_material:
                    continue

                item = node.detected_materials.add()
                item.object_name = obj_name
                item.missing_prefix = prefix
                missing_count += 1

        node.detect_all_ok = (missing_count == 0)

        if missing_count > 0:
            self.report({'WARNING'}, f'Detection finished: {missing_count} missing entries across {len(unique_object_names)} objects')
        else:
            self.report({'INFO'}, f'Detection finished: all prefixes found across {len(unique_object_names)} objects')
        return {'FINISHED'}


class SSMT_OT_CustomMaterialDetectClear(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_clear"
    bl_label = "清除结果"
    bl_description = "清除材质转资源pro 的检测结果"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node:
            node.detected_materials.clear()
            node.detect_all_ok = False
        return {'FINISHED'}


class SSMT_OT_CustomMaterialAssignAddTarget(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_add_target"
    bl_label = "添加目标部件"
    bl_description = "新增一个部件指定输入框"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        item = node.target_items.add()
        node.active_target_index = len(node.target_items) - 1
        # 新输入框尽量默认为当前活动物体，减少手动操作。
        active = getattr(getattr(context, "view_layer", None), "objects", None)
        active_obj = getattr(active, "active", None)
        if active_obj and active_obj.type == "MESH":
            item.target_object = active_obj
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignRemoveTarget(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_remove_target"
    bl_label = "移除目标部件"
    bl_description = "移除当前目标部件输入框（至少保留一个）"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        if len(node.target_items) <= 1:
            self.report({"WARNING"}, "至少保留一个目标部件输入框")
            return {"CANCELLED"}
        index = self.item_index
        if not 0 <= index < len(node.target_items):
            return {"CANCELLED"}
        # 共享组合并组内第一次点击叉号只解除合并，保留为独立部件窗口。
        item = node.target_items[index]
        if item.switch_groups:
            for group in list(item.switch_groups):
                variable = str(getattr(group, "switch_variable", "") or "").strip()
                merge_id = str(getattr(group, "merge_group_id", "") or "").strip()
                if not merge_id:
                    continue
                peers = []
                for other in node.target_items:
                    if other is item:
                        continue
                    peers.extend(
                        candidate for candidate in other.switch_groups
                        if str(getattr(candidate, "switch_variable", "") or "").strip() == variable
                        and str(getattr(candidate, "merge_group_id", "") or "").strip() == merge_id
                    )
                if peers:
                    group.enabled = peers[0].enabled
                    group.comment = peers[0].comment
                    group.key = peers[0].key
                    group.merge_group_id = ""
                    return {"FINISHED"}
        node.target_items.remove(index)
        node.active_target_index = min(
            max(0, self.item_index - 1),
            max(0, len(node.target_items) - 1),
        )
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignPickTarget(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_pick_target"
    bl_label = "拾取目标部件"
    bl_description = "点击后选择要指定材质的网格部件（可在 3D 视口或大纲中点击）"
    bl_options = {"REGISTER", "INTERNAL"}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        global _pick_context
        tree = getattr(context.space_data, "edit_tree", None) or getattr(
            context.space_data, "node_tree", None
        )
        if not tree:
            self.report({"WARNING"}, "无法获取节点树上下文")
            return {"CANCELLED"}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != NODE_IDNAME:
            self.report({"WARNING"}, "目标节点不存在")
            return {"CANCELLED"}
        if not 0 <= self.item_index < len(node.target_items):
            return {"CANCELLED"}

        _pick_context["node_name"] = self.node_name
        _pick_context["tree_name"] = tree.name
        _pick_context["item_index"] = self.item_index
        self.report({"INFO"}, "请选择要指定材质的部件（网格物体）")
        bpy.ops.ssmt.custom_material_assign_pick_modal("INVOKE_DEFAULT")
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignPickTargetModal(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_pick_modal"
    bl_label = "拾取目标部件"
    bl_options = {"REGISTER", "INTERNAL"}

    def _clear(self, context, status, clear_globals=True):
        global _pick_context
        timer = getattr(self, "_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            self._timer = None
        if clear_globals:
            _pick_context["node_name"] = ""
            _pick_context["tree_name"] = ""
            _pick_context["item_index"] = -1
        return status

    def _current_object(self, context):
        active = getattr(context.view_layer.objects, "active", None)
        if active and active in context.selected_objects:
            return active
        if context.selected_objects:
            return context.selected_objects[0]
        return None

    def _try_apply(self, context):
        global _pick_context
        obj = self._current_object(context)
        if obj is None:
            return None
        if obj == self._last_selected and obj in self._initial_selected:
            return None

        tree = bpy.data.node_groups.get(_pick_context["tree_name"])
        if tree is None:
            self.report({"WARNING"}, "节点树已失效，已取消吸管选择")
            return self._clear(context, {"CANCELLED"})
        node = tree.nodes.get(_pick_context["node_name"])
        if node is None or node.bl_idname != NODE_IDNAME:
            self.report({"WARNING"}, "目标节点已失效，已取消吸管选择")
            return self._clear(context, {"CANCELLED"})
        index = _pick_context["item_index"]
        if not 0 <= index < len(node.target_items):
            return self._clear(context, {"CANCELLED"})

        if obj.type != "MESH":
            if not getattr(self, "_non_mesh_warned", False):
                self._non_mesh_warned = True
                self.report({"WARNING"}, "只能指定网格物体作为部件")
            return None
        node.target_items[index].target_object = obj
        self.report({"INFO"}, f"已指定部件: {obj.name}")
        return self._clear(context, {"FINISHED"})

    def invoke(self, context, event):
        global _pick_context
        if not _pick_context["node_name"]:
            return {"CANCELLED"}
        self._initial_selected = set(context.selected_objects)
        self._last_selected = self._current_object(context)
        self._non_mesh_warned = False
        self._started_at = time.monotonic()
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _pick_context
        if not _pick_context["node_name"]:
            return self._clear(context, {"CANCELLED"}, clear_globals=False)

        if time.monotonic() - self._started_at > PICK_TIMEOUT_SECONDS:
            self.report({"WARNING"}, "拾取超时，已自动取消")
            return self._clear(context, {"CANCELLED"})

        if event.type in {"ESC", "RIGHTMOUSE"}:
            return self._clear(context, {"CANCELLED"})

        if event.type == "TIMER":
            result = self._try_apply(context)
            if result is not None:
                return result
            return {"RUNNING_MODAL"}

        if event.type in {
            "LEFTMOUSE",
            "MIDDLEMOUSE",
            "MOUSEMOVE",
            "WHEELUPMOUSE",
            "WHEELDOWNMOUSE",
            "TRACKPADPAN",
            "TRACKPADZOOM",
        }:
            return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}


def _draw_picking_header(self, context):
    if _pick_context["node_name"]:
        self.layout.label(
            text="请选择要指定材质的部件（网格物体）...",
            icon="EYEDROPPER",
        )


_SWITCH_PREFIX_SET = {
    name.casefold()
    for name in MATERIAL_DETECT_PRESETS
} | {
    "diffusemap",
    "normalmap",
    "lightmap",
    "materialmap",
    "glowmap",
    "fxmap",
    "ttlmap",
    "rampmap",
    "highlightmap",
    "stockingmap",
}


def _parse_switch_var_base(node):
    value = str(getattr(node, "material_switch_var", "") or "").strip()
    if not value:
        value = "$swapkey150"
    match = re.match(r"^(\$\w+?)(\d+)$", value)
    if match:
        return match.group(1), int(match.group(2))
    return "$swapkey", 0


def _collect_switch_prefix_groups(obj):
    """按材质前缀收集同一部件的多套材质。"""
    groups = OrderedDict()
    if obj is None or getattr(obj, "type", "") != "MESH":
        return groups
    for material_slot in getattr(obj, "material_slots", []) or []:
        material = material_slot.material
        if not material:
            continue
        name = str(getattr(material, "name", "") or "")
        parts = name.split("_", 1)
        if len(parts) < 2:
            continue
        prefix = parts[0].strip()
        if not prefix or prefix.casefold() not in _SWITCH_PREFIX_SET:
            continue
        names = groups.setdefault(prefix.casefold(), [])
        if name not in names:
            names.append(name)
    return OrderedDict(
        (prefix, names)
        for prefix, names in groups.items()
        if len(names) > 1
    )


def _connected_blueprint_object_names(tree):
    """仿照物体切换面板：只返回链接到 Mod 输出节点的 Object Info / MultiFile。"""
    OUTPUT_IDS = {
        "SSMTNode_Result_Output",
        "SSMTNode_Result_Output_NTMIModImp", "SSMTNode_VeloExportBridge",
    }
    SOURCE_IDS = {
        "SSMTNode_Object_Info",
        "SSMTNode_MultiFile_Export",
    }

    result_names = []
    seen_trees = set()
    seen_nodes = set()

    def collect_tree(current_tree):
        if current_tree is None or current_tree.name in seen_trees:
            return
        seen_trees.add(current_tree.name)

        output_node = None
        for candidate in current_tree.nodes:
            if candidate.bl_idname in OUTPUT_IDS:
                output_node = candidate
                break
        if output_node is None:
            return

        def is_connected(node):
            if node is None:
                return False
            check_visited = set()

            def check_reverse(current):
                if current is None:
                    return False
                current_key = (
                    getattr(current, "id_data", None).name if getattr(current, "id_data", None) else "",
                    current.name,
                )
                if current_key in check_visited:
                    return False
                check_visited.add(current_key)
                if current == node:
                    return True
                for input_socket in getattr(current, "inputs", []) or []:
                    if not input_socket.is_linked:
                        continue
                    for link in input_socket.links:
                        if check_reverse(link.from_node):
                            return True
                return False

            return check_reverse(output_node)

        for node in current_tree.nodes:
            if node.bl_idname not in SOURCE_IDS or node.mute:
                continue
            node_key = (current_tree.name, node.name)
            if node_key in seen_nodes:
                continue
            if not is_connected(node):
                continue
            seen_nodes.add(node_key)
            if node.bl_idname == "SSMTNode_Object_Info":
                object_name = str(getattr(node, "object_name", "") or "")
                if object_name:
                    result_names.append(object_name)
            elif node.bl_idname == "SSMTNode_MultiFile_Export":
                for item in getattr(node, "object_list", []) or []:
                    object_name = str(getattr(item, "object_name", "") or "")
                    if object_name:
                        result_names.append(object_name)

        for node in current_tree.nodes:
            if node.bl_idname == "SSMTNode_Blueprint_Nest" and not node.mute:
                blueprint_name = str(getattr(node, "blueprint_name", "") or "")
                if blueprint_name and blueprint_name != "NONE":
                    nested = bpy.data.node_groups.get(blueprint_name)
                    if nested and getattr(nested, "bl_idname", "") == "SSMTBlueprintTreeType":
                        collect_tree(nested)

    collect_tree(tree)
    # 去重并保持顺序
    ordered = []
    seen = set()
    for name in result_names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


class SSMT_OT_CustomMaterialScanSwitches(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_scan_switches"
    bl_label = "全局扫描贴图切换"
    bl_description = "扫描所有指定部件的同名前缀材质，为可切换部件创建贴图切换控制组"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    @staticmethod
    def _binding_signature(binding_lists):
        """切换身份 = 这一组材质名清单本身（与顺序无关）。

        对齐原版「材质转资源」的判定：它用 ``tuple(sorted(mat.name ...))`` 作
        ``material_group_to_swapkey`` 的 key，只有**同一套材质**才复用同一个
        ``$swapkeyN``，材质不同就各领新号。套数相同但贴图不同（例如脸部 2 套、
        身体 2 套）必须各自成组 —— 18ab9ba 曾把身份换成"套数形状"
        （``_group_shape``），导致不同贴图因档数相同被并成一组。
        """
        return tuple(sorted(tuple(sorted(names)) for names in binding_lists))

    @classmethod
    def _group_signature(cls, group):
        try:
            bindings = json.loads(str(getattr(group, "bindings", "") or "[]"))
        except Exception:
            bindings = []
        if not isinstance(bindings, list):
            bindings = []
        return cls._binding_signature(
            [names for names in bindings if isinstance(names, (list, tuple))]
        )

    @classmethod
    def _material_group_signature(cls, groups):
        return cls._binding_signature(list(groups.values()))

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}

        prefix, next_number = _parse_switch_var_base(node)
        total_groups = 0
        total_parts = 0

        global_mode = bool(getattr(node, "use_global_assign", False))
        scan_objects = []
        if global_mode:
            try:
                current_tree = getattr(node, "id_data", None)
                scan_object_names = (
                    _connected_blueprint_object_names(current_tree)
                    if current_tree is not None
                    else []
                )
                seen_object_names = set()
                for obj_name in scan_object_names:
                    if obj_name in seen_object_names:
                        continue
                    seen_object_names.add(obj_name)
                    obj = bpy.data.objects.get(obj_name)
                    if obj is not None:
                        scan_objects.append(obj)
            except Exception:
                scan_objects = []
            if not scan_objects:
                self.report(
                    {"WARNING"},
                    "未在链接到 Mod 输出的蓝图链路中找到任何部件，已忽略未连接部件",
                )
                return {"CANCELLED"}
            # 必须先快照纯数据、再清空集合：PropertyGroup 项从集合里移除后，残留的
            # Python 引用不会抛 ReferenceError，而是静默读回属性默认值
            # （state_count→2、bindings/comment/object_name→""、key→"N"、
            # enabled→True；若随后 add 了新项，旧引用还会附身到新项内存上）。
            # 旧实现先 clear 再读字段，导致第二次扫描时每个旧组身份退化成空：
            # 同材质合并与旧变量沿用全部失效（分组结果与首次不同），用户设置的
            # 备注/按键/停用状态也被抹成默认值。
            old_global = {}
            for group in node.global_switch_groups:
                variable = str(getattr(group, "switch_variable", "") or "").strip()
                if not variable:
                    continue
                object_name = str(getattr(group, "object_name", "") or "")
                old_global[(object_name, variable)] = {
                    "variable": variable,
                    "signature": self._group_signature(group),
                    "comment": str(group.comment),
                    "key": str(group.key),
                    "enabled": bool(group.enabled),
                }
            node.global_switch_groups.clear()
        else:
            scan_objects = [
                item.target_object
                for item in node.target_items
            ]

        # 汇总旧组纯数据（全局分支已在清空前取好，非全局分支在上面的循环里取）。
        old_by_object = {}
        old_by_variable = {}
        if global_mode:
            for (object_name, variable), snapshot in old_global.items():
                old_by_object[object_name] = snapshot
                old_by_variable.setdefault(variable, snapshot)
        else:
            for item in node.target_items:
                for old_group in item.switch_groups:
                    if old_group.switch_variable:
                        snapshot = {
                            "variable": str(old_group.switch_variable), "signature": self._group_signature(old_group),
                            "comment": str(old_group.comment), "key": str(old_group.key), "enabled": bool(old_group.enabled),
                        }
                        old_by_object[item.target_object.name if item.target_object else ""] = snapshot
                        old_by_variable.setdefault(snapshot["variable"], snapshot)
            # 重新扫描后按当前材质套件重建，避免已不符合条件的部件残留在旧合并组。
            for item in node.target_items:
                item.switch_groups.clear()

        # signature -> variable：同材质集合的部件共用一个切换变量
        # variable -> signature：一个变量只归属一种材质集合（拆分历史误合并）
        signature_variables = {}
        variable_signatures = {}
        for variable, data in old_by_variable.items():
            signature_variables.setdefault(data["signature"], variable)

        # 旧变量会被沿用，新变量必须从“已占用的最大号 + 1”开始分配：否则链路里
        # 新增 / 改名 / 换贴图套数的部件会领到旧组已占用的号，被错并进同一个切换
        # 变量（KeySwap 段只有一份档数，两个部件会一起切且档数不符）。
        _used_numbers = [
            int(match.group(2))
            for match in (
                re.match(r"^\$?(\w+?)(\d+)$", str(variable))
                for variable in old_by_variable
            )
            if match and f"${match.group(1)}" == prefix
        ]
        if _used_numbers:
            next_number = max(next_number, max(_used_numbers) + 1)

        # 重建期间关掉同变量同步回调：批量重建时每个部件组都要先按自己的旧值落位，
        # 否则同伴默认值（N / 空备注）会在写入瞬间通过 update 回调把用户的键顶掉。
        # 重建完成后由下面的归一 + 一次显式同步统一该变量。
        global _switch_sync_guard
        previous_guard = _switch_sync_guard
        _switch_sync_guard = True
        control_groups = []
        try:
            for obj in scan_objects:
                if obj is None or getattr(obj, "type", "") != "MESH":
                    continue
                groups = _collect_switch_prefix_groups(obj)
                if not groups:
                    continue

                signature = self._material_group_signature(groups)
                old_data = old_by_object.get(obj.name)
                variable = ""
                # 沿用旧变量：材质没变才沿用；且该变量不能被别的材质集合占用
                # （历史版本按"档数形状"合并过，会出现脸部和身体共用 $swapkey150）。
                if old_data is not None and old_data["signature"] == signature:
                    candidate = old_data["variable"]
                    if variable_signatures.get(candidate, signature) == signature:
                        variable = candidate
                if not variable:
                    variable = signature_variables.get(signature, "")
                if not variable:
                    variable = f"{prefix}{next_number}"
                    next_number += 1
                signature_variables.setdefault(signature, variable)
                variable_signatures.setdefault(variable, signature)

                if global_mode:
                    container = node.global_switch_groups
                else:
                    item = next(
                        (candidate for candidate in node.target_items if candidate.target_object == obj),
                        None,
                    )
                    if item is None:
                        continue
                    container = item.switch_groups

                total_parts += 1
                group = container.add()
                group.object_name = obj.name
                group.switch_variable = variable
                group.merge_group_id = variable
                group.state_count = max(len(names) for names in groups.values())
                group.bindings = json.dumps(
                    [sorted(names) for names in groups.values()],
                    ensure_ascii=False,
                )
                metadata = old_data or old_by_variable.get(variable)
                if metadata is not None:
                    group.comment = metadata["comment"]
                    group.key = metadata["key"]
                    group.enabled = metadata["enabled"]
                else:
                    group.comment = ""
                    group.key = "N"
                    group.enabled = True
                total_groups += 1

                counts = {len(names) for names in groups.values()}
                if len(counts) > 1:
                    self.report(
                        {"WARNING"},
                        f"{obj.name} 各前缀贴图档数不一致，按键可能无法完整切换所有贴图",
                    )

            # 同一变量的 备注/按键/启用 归一：一个变量在 UI 上只有一套控件、在 INI 里
            # 只有一段 KeySwap，同伴若停在默认 N，导出时会把用户填的键覆盖掉。
            if global_mode:
                control_groups = _normalize_switch_variable_metadata(
                    [node.global_switch_groups]
                )
            else:
                control_groups = _normalize_switch_variable_metadata(
                    [item.switch_groups for item in node.target_items]
                )
        finally:
            _switch_sync_guard = previous_guard

        # 归一结果同步给同变量的其它节点（本节点各组已直接写好）。
        for control in control_groups:
            _sync_switch_variable_fields(control, None)

        if total_groups:
            self.report(
                {"INFO"},
                f"扫描完成：{total_parts} 个部件需要切换，共 {total_groups} 个切换组",
            )
        else:
            self.report({"INFO"}, "扫描完成：未发现可切换的多套同前缀贴图")
        return {"FINISHED"}


class SSMT_OT_CustomMaterialClearSwitches(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_clear_switches"
    bl_label = "清除贴图切换扫描"
    bl_description = "删除所有已创建的贴图切换控制组"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        cleared = 0
        for item in node.target_items:
            cleared += len(item.switch_groups)
            item.switch_groups.clear()
        cleared += len(node.global_switch_groups)
        node.global_switch_groups.clear()
        self.report({"INFO"}, f"已清除 {cleared} 个贴图切换控制组")
        return {"FINISHED"}


class SSMTNode_PostProcess_CustomMaterialAssign(SSMTNode_PostProcess_MaterialBase):
    bl_idname = NODE_IDNAME
    bl_label = "材质转资源pro"
    bl_description = (
        "仅对指定部件按材质名称生成贴图资源引用；"
        "未指定部件保持默认配置，不受影响"
    )
    bl_icon = "OBJECT_DATA"

    target_items: bpy.props.CollectionProperty(
        type=SSMT_CustomMaterialAssignTargetItem
    )
    active_target_index: bpy.props.IntProperty(name="当前部件", default=0, min=0)
    restore_default_textures_after_draw: bpy.props.BoolProperty(
        name="自动恢复后续部件默认贴图",
        description=(
            "目标部件之后若还有未指定部件，会在这些部件的 mesh 绘制前恢复"
            "该段开头的默认贴图，避免后续部件串用这张自定义贴图"
        ),
        default=True,
    )
    use_global_assign: bpy.props.BoolProperty(
        name="使用全局指定",
        description=(
            "开启后忽略下方指定部件列表，按材质转资源的原始逻辑扫描并"
            "单独处理所有部件"
        ),
        default=False,
    )
    global_switch_groups: bpy.props.CollectionProperty(
        type=SSMT_CustomMaterialAssignSwitchGroup
    )

    def init(self, context):
        super().init(context)
        self.width = 440
        if len(self.target_items) == 0:
            self.target_items.add()

    def _target_object_set(self):
        names = set()
        for item in self.target_items:
            obj = item.target_object
            if obj is not None and getattr(obj, "type", "") == "MESH":
                names.add(obj.name)
        return names

    def _is_primary_switch_group(self, group, collection):
        variable = str(getattr(group, "switch_variable", "") or "").strip()
        if not variable:
            return True
        for candidate in collection:
            if candidate == group:
                return True
            if str(getattr(candidate, "switch_variable", "") or "").strip() == variable:
                return False
        return True

    def _is_custom_target(self, obj):
        return obj is not None and obj.name in self._target_object_set()

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        """让段可以进入，但逐 mesh 时只允许本节点指定的部件匹配。

        材质转资源在一个 TextureOverride 段里会先取“第一条 mesh 注释”作为段对象；
        如果在这里直接按白名单过滤，像 testttt 这种排在 rei/jack 后面的目标部件，
        会因为第一条 mesh 不是目标而把整段跳过。因此无 object_filter 的“段入口
        查找”不限制，后面带材质过滤器的逐 mesh 查找才应用白名单。
        """
        if bool(getattr(self, "use_global_assign", False)):
            return super().find_object_by_mesh_name(mesh_name, object_filter)
        if object_filter is None:
            return super().find_object_by_mesh_name(mesh_name, None)

        if not self._target_object_set():
            return None

        original_filter = object_filter

        def combined_filter(candidate):
            if not self._is_custom_target(candidate):
                return False
            return bool(original_filter(candidate))

        object_filter = combined_filter
        return super().find_object_by_mesh_name(mesh_name, object_filter)

    def find_matching_materials(self, obj, texture_type):
        """TTL/FX 等按 mesh 扫描的入口也只允许指定的部件。"""
        if bool(getattr(self, "use_global_assign", False)):
            materials = super().find_matching_materials(obj, texture_type)
            return self._limit_disabled_switch_materials(materials)
        if obj is None or not self._is_custom_target(obj):
            return []
        materials = super().find_matching_materials(obj, texture_type)
        return self._limit_disabled_switch_materials(materials)

    def _disabled_switch_variables(self):
        """返回被关闭的切换变量；同变量任一组关闭即整体关闭。"""
        disabled = set()
        for spec in self._iter_switch_group_defs():
            if not spec["enabled"]:
                disabled.add(spec["variable"])
        return disabled

    def _limit_disabled_switch_materials(self, materials):
        """禁用切换时只保留材质槽顺序中的第一套材质。

        生成阶段某些纹理类型可能只返回完整切换绑定的一个子集（例如
        DiffuseMap 的三套材质中当前段只命中前两套）。旧逻辑要求名称集合
        与绑定集合完全相等，导致备用材质绕过过滤并被写入 INI。只要当前
        命中的多个材质全部属于同一个已禁用绑定，就应视为该切换组并禁用。
        """
        if not materials or len(materials) < 2:
            return materials
        names = tuple(sorted(str(getattr(material, "name", "") or "") for material in materials))
        name_set = set(names)
        disabled = self._disabled_switch_variables()
        if not disabled:
            return materials
        for spec in self._iter_switch_group_defs():
            if spec["variable"] in disabled and any(
                len(name_set) >= 2
                and name_set.issubset(set(str(name) for name in binding))
                for binding in spec["bindings"]
            ):
                return materials[:1]
        return materials

    def generate_material_lines(self, matching_materials, param_name, texture_type, obj,
                                texture_folder, all_sections,
                                object_to_diffuse_swapkey, material_group_to_swapkey,
                                swap_key_prefix, next_swap_key_num, used_swap_keys,
                                resource_name_provider=None):
        """最终生成入口的防线：禁用切换组时绝不生成备用资源/INI 条件块。"""
        matching_materials = self._limit_disabled_switch_materials(matching_materials)
        return super().generate_material_lines(
            matching_materials, param_name, texture_type, obj,
            texture_folder, all_sections, object_to_diffuse_swapkey,
            material_group_to_swapkey, swap_key_prefix, next_swap_key_num,
            used_swap_keys, resource_name_provider=resource_name_provider,
        )

    def _find_workspace_slot_materials(self, obj, slot_info):
        materials = super()._find_workspace_slot_materials(obj, slot_info)
        return self._limit_disabled_switch_materials(materials)

    def _collect_ps_texture_slot_materials(self, obj):
        slots = super()._collect_ps_texture_slot_materials(obj)
        disabled = self._disabled_switch_variables()
        if not disabled:
            return slots
        for slot_info in slots.values():
            materials = list(slot_info.get("materials", {}).values())
            limited = self._limit_disabled_switch_materials(materials)
            if len(limited) < len(materials):
                slot_info["materials"] = OrderedDict(
                    (self._build_material_signature(material), material)
                    for material in limited
                )
        return slots

    def _iter_switch_group_defs(self):
        global_mode = bool(getattr(self, "use_global_assign", False))
        if global_mode:
            collections = [self.global_switch_groups]
        else:
            collections = [item.switch_groups for item in self.target_items]
        for collection in collections:
            for group in collection:
                variable = str(getattr(group, "switch_variable", "") or "").strip()
                if not variable:
                    continue
                try:
                    bindings = json.loads(
                        str(getattr(group, "bindings", "") or "") or "[]"
                    )
                except Exception:
                    bindings = []
                if not isinstance(bindings, list):
                    bindings = []
                yield {
                    "item": None,
                    "group": group,
                    "enabled": bool(getattr(group, "enabled", True)),
                    "variable": variable,
                    "comment": str(getattr(group, "comment", "") or "").strip(),
                    "key": str(getattr(group, "key", "") or "").strip(),
                    "state_count": max(
                        2,
                        int(getattr(group, "state_count", 2) or 2),
                    ),
                    "bindings": [
                        tuple(names)
                        for names in bindings
                        if isinstance(names, (list, tuple))
                    ],
                }

    def _prepare_material_group_switch_map(self, material_group_to_swapkey):
        for spec in self._iter_switch_group_defs():
            for binding in spec["bindings"]:
                if binding:
                    material_group_to_swapkey.setdefault(
                        tuple(binding),
                        spec["variable"],
                    )

    def _max_ui_switch_number(self):
        prefix, _ = _parse_switch_var_base(self)
        max_number = -1
        for spec in self._iter_switch_group_defs():
            match = re.match(r"^\$?(\w+?)(\d+)$", spec["variable"])
            if not match:
                continue
            if f"${match.group(1)}" != prefix:
                continue
            max_number = max(max_number, int(match.group(2)))
        return max_number

    def _write_keyswap_section(self, sections, spec):
        variable = spec["variable"].lstrip("$")
        if not variable:
            return
        section_name = f"[KeySwap_Diffuse_{variable}]"
        state_count = spec["state_count"]
        values = ",".join(str(index) for index in range(state_count))
        lines = []
        if spec["comment"]:
            lines.append(f"; {spec['comment']}")
        lines.extend(
            [
                f"condition = ${variable} == 0 || ${variable} < {state_count}",
                f"key = {spec['key']}",
                "type = cycle",
                f"${variable} = {values}",
            ]
        )
        sections[section_name] = lines

    def _strip_stale_keyswap_blocks(self, sections):
        """清理旧版本误写入的裸 KeySwap_Diffuse 段头及其内容。"""
        bare_header_re = re.compile(
            r"^KeySwap_Diffuse_[A-Za-z0-9_]+$",
            re.IGNORECASE,
        )
        continuation_re = re.compile(
            r"^(?:condition\s*=|key\s*=|type\s*=|\$[A-Za-z0-9_]+\s*=|\s*;)",
            re.IGNORECASE,
        )
        for section_key, section_lines in list(sections.items()):
            cleaned = []
            index = 0
            while index < len(section_lines):
                line = section_lines[index]
                stripped = str(line or "").strip()
                if bare_header_re.match(stripped):
                    index += 1
                    consumed = 0
                    while (
                        index < len(section_lines)
                        and consumed < 8
                    ):
                        candidate = str(section_lines[index] or "").strip()
                        if not candidate or candidate.startswith(";MARK:"):
                            break
                        if not continuation_re.match(candidate):
                            break
                        index += 1
                        consumed += 1
                    continue
                cleaned.append(line)
                index += 1
            sections[section_key] = cleaned

    def define_swapkeys_in_sections(self, sections, keys_to_define):
        """先声明全局变量，再为已扫描出的切换组写入 KeySwap 按键代码。"""
        self._strip_stale_keyswap_blocks(sections)
        super().define_swapkeys_in_sections(sections, keys_to_define)
        used_keys = set(keys_to_define or [])
        # 一个变量只写一段 [KeySwap_Diffuse_<var>]：同变量的每个部件组都会写出
        # 同名 section，后写覆盖先写，同伴停在默认 N 时会把用户的按键顶掉。
        # 取该变量里第一个填过按键的组为准（与 UI 控件组/贴图面板口径一致）。
        chosen = OrderedDict()
        for spec in self._iter_switch_group_defs():
            if not spec["enabled"]:
                continue
            if spec["variable"] not in used_keys:
                continue
            if not spec["key"]:
                continue
            chosen.setdefault(spec["variable"], spec)
        for spec in chosen.values():
            self._write_keyswap_section(sections, spec)

    def _section_custom_mesh_names(self, lines):
        """返回段内属于本节点指定部件的 mesh 注释名称。"""
        names = []
        seen = set()
        for line in lines:
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            obj = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if obj is not None and mesh_name not in seen:
                seen.add(mesh_name)
                names.append(mesh_name)
        return names

    def _capture_section_default_resources(self, lines):
        """捕获第一条 mesh 注释之前作为默认值的 Resource/ps-t 赋值与 run。"""
        defaults = OrderedDict()
        run_lines = []
        for line in lines:
            if self.extract_mesh_name(line):
                break
            stripped = str(line or "").strip()
            if not stripped:
                continue
            # 只保留后续“重建时会被材质转资源再次识别并清理”的 run，
            # 否则像 CommandListSkinTexture 这类行会残留在 mesh 块中重复累积。
            if stripped.startswith("run = "):
                run_lines.append(stripped)
                continue
            match = re.match(
                r"^(Resource\\[^=\s]+|ps-t\d+)\s*=\s*(.+)$",
                stripped,
                re.IGNORECASE,
            )
            if match:
                defaults.setdefault(match.group(1).strip().casefold(), stripped)
        return defaults, run_lines

    def _insert_restore_blocks(self, lines):
        """在目标 mesh 后面的第一个非指定 mesh 绘制前恢复默认贴图资源。

        不在目标 draw 之后做恢复，是因为当前游戏纹理状态下“draw 后恢复”并不可靠；
        改为在后续默认部件自己的 mesh 注释后插入默认赋值，和材质转资源
        “mesh 前先赋值再 draw”的既有写法一致，也更可靠。
        """
        if not bool(self.restore_default_textures_after_draw):
            return
        defaults, run_lines = self._capture_section_default_resources(lines)
        if not defaults:
            return

        default_block = [
            RESTORE_MARKER_START,
            *list(defaults.values()),
            *run_lines,
            RESTORE_MARKER_END,
        ]
        seen_custom_target = False
        mesh_index = 0
        while mesh_index < len(lines):
            line = lines[mesh_index]
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                mesh_index += 1
                continue
            target = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if target is not None:
                seen_custom_target = True
            elif seen_custom_target:
                lines[mesh_index + 1:mesh_index + 1] = default_block
                mesh_index += len(default_block) + 1
                continue
            mesh_index += 1

    def _strip_generated_material_lines(self, lines, preserved_ps_slots=None):
        """先移除上一轮写入的恢复块，再交给材质转资源清理常规生成行。"""
        cleaned = []
        skipping_restore_block = False
        for line in lines:
            stripped = str(line or "").strip()
            if stripped == RESTORE_MARKER_START:
                skipping_restore_block = True
                continue
            if stripped == RESTORE_MARKER_END:
                skipping_restore_block = False
                continue
            if not skipping_restore_block:
                cleaned.append(line)
        return super()._strip_generated_material_lines(
            cleaned,
            preserved_ps_slots=preserved_ps_slots,
        )

    def _move_top_level_targets_to_end(self, lines):
        """把顶层（不被 if 包裹）的目标 mesh 绘制块移动到本段末尾。

        目标部件最后绘制时，后面不再有默认部件，就不会把自定义贴图状态带到其它
        部件。若目标位于 if/endif 内部，为保证条件块完整暂不移动，仍走默认恢复。
        """
        if_depth = 0
        top_mesh_indices = []
        marker_indices = []

        for index, line in enumerate(lines):
            stripped = str(line or "").strip()
            if not stripped:
                continue
            if re.match(r"^if\s+", stripped, re.IGNORECASE):
                if_depth += 1
                continue
            if stripped.casefold() == "endif":
                if_depth = max(0, if_depth - 1)
                continue
            if if_depth == 0:
                if self.extract_mesh_name(line):
                    top_mesh_indices.append(index)
                elif stripped.startswith(";MARK:"):
                    marker_indices.append(index)

        target_ranges = []
        draw_re = re.compile(r"^\s*drawindexed(?:instanced)?\s*=", re.IGNORECASE)
        for mesh_index in top_mesh_indices:
            line = lines[mesh_index]
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            target = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if target is None:
                continue

            # 目标 mesh 后的材质切换 if 会出现在其 draw 之前，应一起移动；
            # 只有看到目标自己的 draw 之后遇到的下一个顶层 mesh/if/MARK 才是边界。
            next_boundary = len(lines)
            nested_depth = 0
            seen_draw = False
            for scan_index in range(mesh_index + 1, len(lines)):
                scan_line = lines[scan_index]
                stripped = str(scan_line or "").strip()
                if not stripped:
                    continue
                if re.match(r"^if\s+", stripped, re.IGNORECASE):
                    if nested_depth == 0 and seen_draw:
                        next_boundary = scan_index
                        break
                    nested_depth += 1
                    continue
                if stripped.casefold() == "endif":
                    nested_depth = max(0, nested_depth - 1)
                    continue
                if nested_depth == 0:
                    if self.extract_mesh_name(scan_line):
                        next_boundary = scan_index
                        break
                    if stripped.startswith(";MARK:"):
                        next_boundary = scan_index
                        break
                    if draw_re.match(stripped):
                        seen_draw = True

            segment = lines[mesh_index:next_boundary]
            if not any(draw_re.match(str(item or "")) for item in segment):
                continue
            if not any(
                self._is_generated_material_line(str(item or "").strip())
                for item in segment
            ):
                continue
            target_ranges.append((mesh_index, next_boundary))

        if not target_ranges:
            return

        moved_segments = []
        for start, end in reversed(target_ranges):
            moved_segments.insert(0, lines[start:end])
            del lines[start:end]

        insert_at = len(lines)
        for index, line in enumerate(lines):
            stripped = str(line or "").strip()
            if stripped.startswith(";MARK:"):
                insert_at = index
                break
        for segment in moved_segments:
            lines[insert_at:insert_at] = segment
            insert_at += len(segment)

    def _find_flat_region_target_ranges(self, lines, region_start, region_end):
        """查找一段平铺 if 内容中的目标 mesh 范围（支持材质切换 if）。"""
        mesh_indices = []
        for index in range(region_start, region_end):
            if self.extract_mesh_name(lines[index]):
                mesh_indices.append(index)

        ranges = []
        draw_re = re.compile(r"^\s*drawindexed(?:instanced)?\s*=", re.IGNORECASE)
        for mesh_index in mesh_indices:
            line = lines[mesh_index]
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            target = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if target is None:
                continue

            segment_end = region_end
            nested_depth = 0
            seen_draw = False
            for scan_index in range(mesh_index + 1, region_end):
                scan_line = lines[scan_index]
                stripped = str(scan_line or "").strip()
                if not stripped:
                    continue
                if re.match(r"^if\s+", stripped, re.IGNORECASE):
                    nested_depth += 1
                    continue
                if stripped.casefold() == "endif":
                    if nested_depth == 0 and seen_draw:
                        segment_end = scan_index
                        break
                    if nested_depth > 0:
                        nested_depth -= 1
                    continue
                if nested_depth == 0:
                    if self.extract_mesh_name(scan_line):
                        segment_end = scan_index
                        break
                    if stripped.startswith(";MARK:"):
                        segment_end = scan_index
                        break
                    if draw_re.match(stripped):
                        seen_draw = True

            segment = lines[mesh_index:segment_end]
            if not any(draw_re.match(str(item or "")) for item in segment):
                continue
            if not any(
                self._is_generated_material_line(str(item or "").strip())
                for item in segment
            ):
                continue
            ranges.append((mesh_index, segment_end))
        return ranges

    def _move_targets_to_end_of_top_level_if_blocks(self, lines):
        """在每个顶层 if/endif 分支内，把目标 mesh 绘制块移到该分支 endif 前。"""
        top_level_blocks = []
        if_depth = 0
        block_start = -1
        for index, line in enumerate(lines):
            stripped = str(line or "").strip()
            if not stripped:
                continue
            if re.match(r"^if\s+", stripped, re.IGNORECASE):
                if if_depth == 0:
                    block_start = index
                if_depth += 1
            elif stripped.casefold() == "endif":
                if_depth -= 1
                if if_depth == 0 and block_start >= 0:
                    top_level_blocks.append((block_start, index))
                    block_start = -1

        for block_start, block_end in top_level_blocks:
            # 嵌套游戏 if 内部的 mesh 不能安全按平铺块重排，跳过由默认保护处理。
            nested_mesh_found = False
            scan_depth = 0
            for index in range(block_start + 1, block_end):
                stripped = str(lines[index] or "").strip()
                if re.match(r"^if\s+", stripped, re.IGNORECASE):
                    scan_depth += 1
                elif stripped.casefold() == "endif":
                    scan_depth = max(0, scan_depth - 1)
                elif scan_depth > 0 and self.extract_mesh_name(lines[index]):
                    nested_mesh_found = True
            if nested_mesh_found:
                continue

            ranges = self._find_flat_region_target_ranges(
                lines,
                block_start + 1,
                block_end,
            )
            if not ranges:
                continue

            moved_segments = []
            removed_count = 0
            for start, end in reversed(ranges):
                moved_segments.insert(0, lines[start:end])
                del lines[start:end]
                removed_count += end - start
            insert_at = block_end - removed_count
            for segment in moved_segments:
                lines[insert_at:insert_at] = segment
                insert_at += len(segment)

    def process_texture_override_section(
        self,
        section_name,
        all_sections,
        material_group_to_swapkey,
        swap_key_prefix=None,
        next_swap_key_num=None,
        used_swap_keys=None,
        transparency_sections_to_add=None,
    ):
        """只进入包含指定部件的段，并在其绘制后恢复默认贴图。"""
        if used_swap_keys is None or used_swap_keys is Ellipsis:
            used_swap_keys = set()
        if transparency_sections_to_add is None or transparency_sections_to_add is Ellipsis:
            transparency_sections_to_add = OrderedDict()
        if material_group_to_swapkey is None or material_group_to_swapkey is Ellipsis:
            material_group_to_swapkey = {}
        if swap_key_prefix is None or swap_key_prefix is Ellipsis:
            swap_key_prefix = None
        if next_swap_key_num is None or next_swap_key_num is Ellipsis:
            next_swap_key_num = 0

        if bool(getattr(self, "use_global_assign", False)):
            self._prepare_material_group_switch_map(material_group_to_swapkey)
            max_ui_number = self._max_ui_switch_number()
            if max_ui_number >= 0 and next_swap_key_num <= max_ui_number:
                next_swap_key_num = max_ui_number + 1
            return super().process_texture_override_section(
                section_name,
                all_sections,
                material_group_to_swapkey,
                swap_key_prefix=swap_key_prefix,
                next_swap_key_num=next_swap_key_num,
                used_swap_keys=used_swap_keys,
                transparency_sections_to_add=transparency_sections_to_add,
            )

        lines = all_sections.get(section_name, [])
        if not self._section_custom_mesh_names(lines):
            return next_swap_key_num

        self._prepare_material_group_switch_map(material_group_to_swapkey)
        max_ui_number = self._max_ui_switch_number()
        if max_ui_number >= 0 and next_swap_key_num <= max_ui_number:
            next_swap_key_num = max_ui_number + 1

        result = super().process_texture_override_section(
            section_name,
            all_sections,
            material_group_to_swapkey,
            swap_key_prefix=swap_key_prefix,
            next_swap_key_num=next_swap_key_num,
            used_swap_keys=used_swap_keys,
            transparency_sections_to_add=transparency_sections_to_add,
        )
        self._move_targets_to_end_of_top_level_if_blocks(lines)
        self._move_top_level_targets_to_end(lines)
        self._insert_restore_blocks(lines)
        return result

    def draw_material_detection_panel(self, context, layout):
        """材质检测窗口（可折叠），检测范围跟随“使用全局指定”开关。"""
        layout.separator()
        header_row = layout.row(align=True)
        header_row.prop(
            self,
            "show_detect_panel",
            icon="TRIA_DOWN" if self.show_detect_panel else "TRIA_RIGHT",
            text="材质检测",
            emboss=False,
        )
        if not self.show_detect_panel:
            return

        box = layout.box()
        prefix_row = box.row(align=True)
        prefix_row.label(text="检测前缀:", icon="FILTER")
        op = prefix_row.operator("ssmt.custom_material_detect_add_prefix", text="", icon="ADD")
        op.node_name = self.name

        for index, item in enumerate(self.material_detect_prefixes):
            row = box.row(align=True)
            row.label(text=item.prefix, icon="MATERIAL")
            remove = row.operator(
                "ssmt.custom_material_detect_remove_prefix",
                text="",
                icon="X",
            )
            remove.node_name = self.name
            remove.item_index = index

        input_row = box.row(align=True)
        input_row.prop(self, "temp_prefix_input", text="", icon="CONSOLE")
        add_custom = input_row.operator(
            "ssmt.custom_material_detect_add_custom_prefix",
            text="",
            icon="ADD",
        )
        add_custom.node_name = self.name

        btn_row = box.row(align=True)
        detect = btn_row.operator("ssmt.custom_material_detect", text="检测材质", icon="VIEWZOOM")
        detect.node_name = self.name
        clear = btn_row.operator("ssmt.custom_material_detect_clear", text="清除", icon="X")
        clear.node_name = self.name

        if self.detected_materials:
            result_box = box.box()
            result_box.label(
                text=f"缺失材质 ({len(self.detected_materials)} 个)",
                icon="ERROR",
            )
            for item in self.detected_materials:
                row = result_box.row(align=True)
                row.label(text=item.object_name, icon="OBJECT_DATA")
                row.label(text=f"缺少: {item.missing_prefix}", icon="ERROR")
        elif self.detect_all_ok:
            box.label(text="全部正确", icon="CHECKMARK")

    def _draw_target_input_panel(self, context, layout):
        """非全局模式：显示目标部件输入框。"""
        title = layout.box()
        title.label(text="目标部件（仅这些部件会按材质生成）", icon="OBJECT_DATA")
        title.label(
            text="未在此列表中的部件保持默认配置，不会修改",
            icon="INFO",
        )

        rows = layout.box()
        header = rows.row(align=True)
        header.label(text=f"部件输入框（{len(self.target_items)} 个）")
        add = header.operator(
            "ssmt.custom_material_assign_add_target", text="添加", icon="ADD"
        )
        add.node_name = self.name

        grouped = OrderedDict()
        for index, item in enumerate(self.target_items):
            for group in item.switch_groups:
                variable = str(getattr(group, "merge_group_id", "") or "").strip() or f"__single_{index}"
                if variable:
                    grouped.setdefault(variable, []).append((index, item, group))
        merged_groups = {variable: entries for variable, entries in grouped.items() if len(entries) > 1}
        primary_indices = {entries[0][0] for entries in merged_groups.values()}
        merged_member_indices = {entry[0] for entries in merged_groups.values() for entry in entries}

        def draw_target_row(parent, index, item, label):
            row = parent.row(align=True)
            row.prop(item, "target_object", text=label)
            pick = row.operator("ssmt.custom_material_assign_pick_target", text="", icon="EYEDROPPER")
            pick.node_name = self.name
            pick.item_index = index
            remove_row = row.row(align=True)
            remove_row.enabled = len(self.target_items) > 1
            remove = remove_row.operator("ssmt.custom_material_assign_remove_target", text="", icon="X")
            remove.node_name = self.name
            remove.item_index = index
            target = item.target_object
            if target is None:
                parent.label(text="未指定（可拖入大纲物体，或使用吸管）", icon="INFO")
            elif target.type != "MESH":
                parent.label(text="仅支持网格物体", icon="ERROR")
            else:
                parent.label(text=f"物体: {target.name}", icon="MESH_DATA")

        def draw_switch_controls(parent, group, group_index=1):
            header = parent.row(align=True)
            header.prop(
                group,
                "enabled",
                text="",
                icon="CHECKBOX_HLT" if group.enabled else "CHECKBOX_DEHLT",
            )
            header.label(text=f"贴图切换 {group_index}（{group.state_count} 档）", icon="KEYFRAME_HLT")
            parent.prop(group, "switch_variable", text="材质切换变量")
            parent.prop(group, "comment", text="备注")
            parent.prop(group, "key", text="切换按键")

        for index, item in enumerate(self.target_items):
            if index not in primary_indices:
                continue
            # 共享组主窗口：多个部件框在同一窗口内，控件只保留一套。
            matching = [entries for entries in merged_groups.values() if entries[0][0] == index]
            if matching:
                for entries in matching:
                    box = rows.box()
                    first_group = entries[0][2]
                    box.label(text=f"部件指定组（{first_group.state_count} 档）", icon="LINKED")
                    for member_index, member_item, _group in entries:
                        draw_target_row(box, member_index, member_item, f"部件 {member_index + 1}")
                    draw_switch_controls(box, first_group)
                continue
            box = rows.box()
            draw_target_row(box, index, item, f"部件 {index + 1}")
            for group_index, group in enumerate(item.switch_groups, 1):
                draw_switch_controls(box, group, group_index)

        # 未指定或未形成共享组的条目仍保持原有独立窗口。
        for index, item in enumerate(self.target_items):
            if index in merged_member_indices:
                continue
            box = rows.box()
            draw_target_row(box, index, item, f"部件 {index + 1}")
            for group_index, group in enumerate(item.switch_groups, 1):
                draw_switch_controls(box, group, group_index)

    def _draw_global_switch_panel(self, context, layout):
        """全局模式：显示全局贴图切换框。"""
        box = layout.box()
        header = box.row(align=True)
        header.label(
            text=f"贴图切换框（{len(self.global_switch_groups)} 个切换组）",
            icon="KEYFRAME_HLT",
        )
        if not self.global_switch_groups:
            box.label(
                text="尚未扫描：在全局模式下点击上方「全局扫描贴图切换」",
                icon="INFO",
            )

        grouped = OrderedDict()
        for group in self.global_switch_groups:
            variable = str(getattr(group, "merge_group_id", "") or "").strip() or f"__single_{getattr(group, 'object_name', '')}"
            if variable:
                grouped.setdefault(variable, []).append(group)
        for variable, entries in grouped.items():
            group_box = box.box()
            first_group = entries[0]
            if len(entries) > 1:
                group_box.label(text=f"共享切换组（{first_group.state_count} 档）", icon="LINKED")
            else:
                group_box.label(text=f"贴图切换（{first_group.state_count} 档）", icon="KEYFRAME_HLT")
            parts_box = group_box.column(align=True)
            for group in entries:
                row = parts_box.row(align=True)
                row.label(text=str(getattr(group, "object_name", "") or "(未命名部件)"), icon="OBJECT_DATA")
            header = group_box.row(align=True)
            header.prop(first_group, "enabled", text="", icon="CHECKBOX_HLT" if first_group.enabled else "CHECKBOX_DEHLT")
            header.label(text=f"贴图切换 1（{first_group.state_count} 档）", icon="KEYFRAME_HLT")
            group_box.prop(first_group, "switch_variable", text="材质切换变量")
            group_box.prop(first_group, "comment", text="备注")
            group_box.prop(first_group, "key", text="切换按键")

    def draw_buttons(self, context, layout):
        layout.prop(self, "use_global_assign", text="使用全局指定")
        if self.use_global_assign:
            layout.label(
                text="已开启：将忽略下方目标列表，按材质转资源逻辑处理所有部件",
                icon="INFO",
            )
        else:
            layout.label(
                text="仅处理下方列表中指定的部件",
                icon="INFO",
            )

        scan_row = layout.row(align=True)
        scan = scan_row.operator(
            "ssmt.custom_material_scan_switches",
            text="全局扫描贴图切换",
            icon="FILE_REFRESH",
        )
        scan.node_name = self.name
        clear = scan_row.operator(
            "ssmt.custom_material_clear_switches",
            text="清除",
            icon="X",
        )
        clear.node_name = self.name

        if self.use_global_assign:
            self._draw_global_switch_panel(context, layout)
        else:
            self._draw_target_input_panel(context, layout)

        options = layout.box()
        options.label(text="材质转资源选项", icon="MATERIAL")
        options.prop(self, "material_to_resource_override")
        options.prop(self, "restore_default_textures_after_draw")
        options.prop(self, "debug_disable_fx_ttl")

        layout.label(
            text="材质命名前缀沿用材质转资源规则（如 DiffuseMap_xxx）",
            icon="INFO",
        )
        if self.use_global_assign:
            layout.label(
                text="全局指定已开启：未处理单独目标移动/默认恢复逻辑",
                icon="INFO",
            )
        else:
            layout.label(
                text="顶层指定部件会自动移到本段末尾绘制，避免污染后续部件",
                icon="SORT_ASC",
            )
            layout.label(
                text="导出 Mod 时自动执行；未指定部件对应的 INI 段不会被修改",
                icon="TIME",
            )
        self.draw_material_detection_panel(context, layout)

    def execute_postprocess(self, mod_export_path, exporter=None):
        if not bool(getattr(self, "use_global_assign", False)) and not self._target_object_set():
            print("[材质转资源pro] 未指定任何目标部件，跳过")
            return
        return super().execute_postprocess(mod_export_path, exporter=exporter)


LEGACY_MATERIAL_NODE_ID = 'SSMTNode_PostProcess_Material'


def _migrate_legacy_material_nodes():
    """把旧版「材质转资源」节点原位迁移为「材质转资源pro」（幂等）。

    旧类仅保留为注册壳（见 node_postprocess_material），目的是让旧蓝图文件
    能够正常加载、避免未知节点类型被 Blender 静默丢弃；迁移在此之后执行：
    复制全部共享属性、切换为全局指定模式（等价原版全场景行为），并重连
    同名 socket 的连线。迁移完成的壳节点随即删除。
    """
    migrated = 0
    for tree in bpy.data.node_groups:
        if getattr(tree, 'bl_idname', '') != 'SSMTBlueprintTreeType':
            continue
        for node in list(tree.nodes):
            if getattr(node, 'bl_idname', '') != LEGACY_MATERIAL_NODE_ID:
                continue
            try:
                _migrate_legacy_material_node(tree, node)
                migrated += 1
            except Exception as exc:
                print(f"[TheHerta4] 材质转资源节点迁移失败 {node.name}: {exc}")
    if migrated:
        print(f"[TheHerta4] 材质转资源节点迁移完成：{migrated} 个节点已转换为「材质转资源pro」。")
    return migrated


def _migrate_legacy_material_node(tree, node):
    new_node = tree.nodes.new(NODE_IDNAME)
    new_node.location = node.location
    if node.label:
        new_node.label = node.label
    # 原版按场景全部物体处理，等价于 pro 的「使用全局指定」模式。
    new_node.use_global_assign = True
    # 复制共享属性（属性均声明在未注册的 SSMTNode_PostProcess_MaterialBase）。
    for attr in ('material_to_resource_override', 'debug_disable_fx_ttl',
                 'material_switch_var', 'temp_prefix_input',
                 'show_detect_panel', 'detect_all_ok'):
        try:
            setattr(new_node, attr, getattr(node, attr))
        except Exception:
            pass
    for item in node.material_detect_prefixes:
        target = new_node.material_detect_prefixes.add()
        target.prefix = item.prefix
    for item in node.detected_materials:
        target = new_node.detected_materials.add()
        target.object_name = item.object_name
        target.missing_prefix = item.missing_prefix
    # 重连输入/输出 socket（两者的 socket 布局一致：PostProcess Input/Output）。
    for index, output in enumerate(node.outputs):
        for link in list(output.links):
            tree.links.new(new_node.outputs[index], link.to_socket)
    for index, socket_input in enumerate(node.inputs):
        for link in list(socket_input.links):
            tree.links.new(link.from_socket, new_node.inputs[index])
    tree.nodes.remove(node)


def _migrate_legacy_material_nodes_handler():
    try:
        _migrate_legacy_material_nodes()
    except Exception as exc:
        print(f"[TheHerta4] 材质转资源节点迁移失败: {exc}")


classes = (
    SSMT_CustomMaterialAssignSwitchGroup,
    SSMT_CustomMaterialAssignTargetItem,
    SSMT_OT_CustomMaterialDetectAddPrefix,
    SSMT_OT_CustomMaterialDetectRemovePrefix,
    SSMT_OT_CustomMaterialDetectAddCustomPrefix,
    SSMT_OT_CustomMaterialDetect,
    SSMT_OT_CustomMaterialDetectClear,
    SSMT_OT_CustomMaterialAssignAddTarget,
    SSMT_OT_CustomMaterialAssignAddSelected,
    SSMT_OT_CustomMaterialAssignRemoveTarget,
    SSMT_OT_CustomMaterialAssignPickTarget,
    SSMT_OT_CustomMaterialAssignPickTargetModal,
    SSMT_OT_CustomMaterialScanSwitches,
    SSMT_OT_CustomMaterialClearSwitches,
    SSMTNode_PostProcess_CustomMaterialAssign,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(_draw_picking_header)
    if _migrate_legacy_material_nodes_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_migrate_legacy_material_nodes_handler)
    # 立即迁移一次：覆盖「addon 启用时文件已打开」的场景。
    _migrate_legacy_material_nodes_handler()


def unregister():
    bpy.types.VIEW3D_HT_header.remove(_draw_picking_header)
    if _migrate_legacy_material_nodes_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_legacy_material_nodes_handler)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
