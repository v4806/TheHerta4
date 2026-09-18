import bpy
from bpy.props import IntProperty, StringProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)
from .variable_registry import (
    ensure_anim_driver_frame_variable_name,
    normalize_variable_name,
)

#: 兼容别名：手写在自定义文本 / 外部 mod 里的共享帧变量名。
COMPAT_FRAME_VARIABLE_NAME = "swapvar"
COMPAT_FPS_VARIABLE_NAME = "fps"


class SSMTNode_AnimDriver_Runtime(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Runtime'
    bl_label = '运行时间'
    bl_icon = 'TIME'
    bl_description = (
        "按系统时间推进帧计数器；帧变量按节点预分配（$swapvar{序号}），"
        "并由序号最小的节点维护 $swapvar / $fps 兼容别名"
    )

    fps: IntProperty(
        name="固定帧率",
        description="用于计算帧索引的目标帧率",
        default=30,
        min=1,
        max=144,
    )

    playback_rate: IntProperty(
        name="播放速率",
        description="控制帧索引取模的速率（1=每帧，2=每两帧）",
        default=1,
        min=1,
        max=9999,
    )

    custom_frame_variable_name: StringProperty(
        name="帧变量",
        description="当前帧索引变量名；创建时自动填入预分配名，可直接复制或手动修改",
        default="",
    )

    assigned_frame_variable_name: StringProperty(
        name="预分配帧变量",
        description="预分配的唯一帧变量名（自动生成，避免多个运行时间节点声明同名变量）",
        default="",
        options={'HIDDEN'},
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 300
        self._assign_next_available_index()
        # 预分配帧变量名（依赖 auto_index，必须在 _assign_next_available_index 之后）
        self.ensure_frame_variable_name()

    def update(self):
        super().update()
        # 旧蓝图（保存时还没有预分配字段）与复制节点在这里补上名字；已分配的不动
        self.ensure_frame_variable_name()

    # ------------------------------------------------------------------
    # 帧变量（预分配 + 可手改）
    # ------------------------------------------------------------------

    def ensure_frame_variable_name(self) -> str:
        return ensure_anim_driver_frame_variable_name(self)

    def frame_variable_name(self) -> str:
        """最终生效的帧变量名（不含 ``$``）：手改优先，其次预分配名。"""
        custom_name = normalize_variable_name(getattr(self, "custom_frame_variable_name", "") or "")
        if custom_name:
            return custom_name
        assigned_name = normalize_variable_name(getattr(self, "assigned_frame_variable_name", "") or "")
        if assigned_name:
            return assigned_name
        return self.ensure_frame_variable_name()

    def is_compat_alias_owner(self) -> bool:
        """同一个蓝图里只让 ``auto_index`` 最小的运行时间节点发兼容别名。

        别名（``$swapvar`` / ``$fps``）必须是全局唯一的一份声明，否则又回到
        「N 个节点 N 份同名声明」的老问题。
        """
        tree = getattr(self, "id_data", None)
        runtime_nodes = [
            node for node in (getattr(tree, "nodes", None) or [])
            if getattr(node, "bl_idname", "") == 'SSMTNode_AnimDriver_Runtime'
        ] if tree else []
        if not runtime_nodes:
            return True
        owner = min(
            runtime_nodes,
            key=lambda node: (
                int(getattr(node, "auto_index", 0) or 0),
                str(getattr(node, "name", "") or ""),
            ),
        )
        return owner is self

    def draw_buttons(self, context, layout):
        layout.prop(self, "fps")
        layout.prop(self, "playback_rate")
        box = layout.box()
        box.label(text=f"预分配帧变量: ${self.frame_variable_name()}", icon='INFO')
        box.prop(self, "custom_frame_variable_name", text="帧变量")
        if self.is_compat_alias_owner():
            box.label(text="本节点同时维护兼容别名 $swapvar / $fps", icon='CHECKBOX_HLT')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        frame_var = self.frame_variable_name()
        alias_owner = self.is_compat_alias_owner()

        lines = [
            "[Constants]",
            f"global persist ${frame_var} = 0",
            "; 当前帧索引（整数）",
        ]
        if alias_owner:
            # 兼容别名：只声明一份，避免 N 个运行时间节点重复声明 $swapvar / $fps。
            # 手写在自定义文本里的 $swapvar / $fps 引用因此继续有效。
            lines.extend([
                f"global persist ${COMPAT_FRAME_VARIABLE_NAME} = 0",
                f"global persist ${COMPAT_FPS_VARIABLE_NAME} = {self.fps}",
                "; 兼容别名（手写 $swapvar / $fps 仍可用）",
            ])
        lines.extend([
            "[Present]",
            "; 基于系统时间的自动计算（每帧执行）",
            f"${frame_var} = (time * {self.fps}) // 1",
        ])
        if alias_owner:
            lines.append(f"${COMPAT_FRAME_VARIABLE_NAME} = ${frame_var}")
        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _runtime_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_Runtime':
                try:
                    SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                except Exception:
                    pass


classes = (
    SSMTNode_AnimDriver_Runtime,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_runtime_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_runtime_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
