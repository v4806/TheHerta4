# -*- coding: utf-8 -*-
import bpy

from .node_base import SSMTNodeBase


class SSMTNode_MergeWeld(SSMTNodeBase):
    '''边界焊接节点：导出期把跨分块边界的重合顶点焊接为共享顶点'''
    bl_idname = 'SSMTNode_MergeWeld'
    bl_label = '边界焊接'
    bl_description = '边界焊接：导出期把跨分块边界的重合顶点焊接为共享顶点，避免形变后接缝裂开'
    bl_icon = 'GROUP'
    bl_width_min = 260

    weld_distance: bpy.props.FloatProperty(
        name='合并距离',
        description='判定重合顶点的最大距离',
        default=1e-6,
        min=0.0,
        precision=6,
    ) # type: ignore

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "物体 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 260

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text="边界焊接", icon='GROUP')
        box.prop(self, 'weld_distance')
        try:
            upstream_count = len(self._collect_upstream_object_names())
            direct_count = self._get_connected_object_count()
        except Exception:
            upstream_count = 0
            direct_count = 0
        box.label(text=f"上游物体: {upstream_count}", icon='OBJECT_DATA')
        box.label(text=f"直接连接: {direct_count}", icon='LINKED')
        self._draw_last_export_stats(box)

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"物体 {len(self.inputs) + 1}")

        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
            self.inputs.remove(self.inputs[-1])

    def _collect_upstream_object_names(self):
        """沿输入链接递归向上收集 SSMTNode_Object_Info 的物体名（不依赖导出层）。"""
        names = set()
        try:
            visited = set()
            pending = []

            def _push_from_socket(socket):
                try:
                    links = socket.links
                except Exception:
                    return
                for link in links:
                    try:
                        from_node = getattr(link, 'from_node', None)
                    except Exception:
                        continue
                    if from_node is not None:
                        pending.append(from_node)

            for socket in self.inputs:
                _push_from_socket(socket)

            while pending:
                node = pending.pop()
                if node is None:
                    continue
                tree = getattr(node, 'id_data', None)
                key = (
                    getattr(tree, 'name', '') if tree is not None else '',
                    getattr(node, 'name', '') or str(id(node)),
                )
                if key in visited:
                    continue
                visited.add(key)

                if getattr(node, 'bl_idname', '') == 'SSMTNode_Object_Info':
                    object_name = getattr(node, 'object_name', '')
                    if isinstance(object_name, str) and object_name:
                        names.add(object_name)
                    continue

                try:
                    node_inputs = node.inputs
                except Exception:
                    continue
                for socket in node_inputs:
                    _push_from_socket(socket)
        except Exception:
            return names
        return names

    def _get_connected_object_count(self):
        count = 0
        for socket in self.inputs:
            if socket.is_linked:
                count += 1
        return count

    def _draw_last_export_stats(self, layout):
        """显示上次导出的焊接统计；导出层尚未提供统计接口时静默跳过。"""
        try:
            from .export_helper import BlueprintExportHelper
        except Exception:
            return

        get_stats = getattr(BlueprintExportHelper, 'get_merge_weld_last_stats', None)
        if not callable(get_stats):
            return

        try:
            stats = get_stats()
        except Exception:
            return

        if not isinstance(stats, dict):
            return

        try:
            merged_group_count = int(stats.get('merged_group_count', 0) or 0)
            involved_block_count = int(stats.get('involved_block_count', 0) or 0)
        except Exception:
            return

        layout.separator()
        layout.label(
            text=f"上次导出 合并组: {merged_group_count}  涉及分块: {involved_block_count}",
            icon='INFO',
        )


classes = (
    SSMTNode_MergeWeld,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
