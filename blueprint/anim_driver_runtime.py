import re

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)
from .variable_registry import (
    ANIM_DRIVER_FRAME_PREFIX,
    ensure_anim_driver_frame_variable_name,
    mark_variable_name_used,
    normalize_variable_name,
)

#: 历史自动生成的帧变量名（``swapvar{序号}``），只用于迁移到当前前缀。
#: 注意：**不匹配** ``swapvar_xxx`` 这类用户自定义名（下划线 + 字母），
#: 那属于用户手改，按「已分配不重分配」口径一律不动。
_LEGACY_FRAME_VARIABLE_RE = re.compile(r"^swapvar(\d+(?:_\d+)?)$")


class SSMTNode_AnimDriver_Runtime(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Runtime'
    bl_label = '运行时间'
    bl_icon = 'TIME'
    bl_description = (
        "按系统时间推进帧计数器；帧变量按节点预分配（$anim_frame{序号}），"
        "创建时会自动填入输入框，可直接复制或手动修改"
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

    frame_var_initialized: BoolProperty(
        name="Frame Variable Initialized",
        default=False,
        options={'HIDDEN'},
    )

    def _ensure_initial_visible_frame_variable_name(self, context=None) -> bool:
        """首次把预分配名**填进输入框**（用户清空后不再回填，与连续索引变量同规则）。"""
        if getattr(self, "frame_var_initialized", False):
            return False

        assigned_name = self.ensure_frame_variable_name(context=context)
        if not assigned_name:
            return False

        if str(getattr(self, "custom_frame_variable_name", "") or "").strip():
            self.frame_var_initialized = True
            return False

        self.frame_var_initialized = True
        self.custom_frame_variable_name = assigned_name
        return True

    def update_frame_variable_name(self, context):
        if self._ensure_initial_visible_frame_variable_name(context=context):
            return
        normalized = normalize_variable_name(self.custom_frame_variable_name)
        if normalized != str(self.custom_frame_variable_name or "").strip().lstrip("$"):
            self.custom_frame_variable_name = normalized
            return
        if normalized:
            mark_variable_name_used(normalized, context=context)
        self.ensure_frame_variable_name(context=context)
        self.update_node_width([
            getattr(self, "custom_frame_variable_name", ""),
            getattr(self, "assigned_frame_variable_name", ""),
        ])

    custom_frame_variable_name: StringProperty(
        name="帧变量",
        description="当前帧索引变量名；创建时会自动填入预分配变量名，可直接复制或手动修改。",
        default="",
        update=update_frame_variable_name,
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
        self._ensure_initial_visible_frame_variable_name(context=context)

    def copy(self, node):
        # 复制节点必须重新取序号与预分配名，否则两个节点会声明同一个帧变量
        self._assign_next_available_index()
        self.custom_frame_variable_name = ""
        self.assigned_frame_variable_name = ""
        self.frame_var_initialized = False
        self._ensure_initial_visible_frame_variable_name()

    def update(self):
        super().update()
        # 旧蓝图（保存时还没有预分配字段）与复制节点在这里补上名字；已分配的不动
        self._ensure_initial_visible_frame_variable_name()

    # ------------------------------------------------------------------
    # 帧变量（预分配 + 可手改）
    # ------------------------------------------------------------------

    def ensure_frame_variable_name(self, context=None) -> str:
        """确保预分配名存在（写回 ``assigned_frame_variable_name``）并返回它。"""
        return ensure_anim_driver_frame_variable_name(self, context=context)

    def frame_variable_name(self) -> str:
        """最终生效的帧变量名（不含 ``$``）：手改优先，其次预分配名。"""
        custom_name = normalize_variable_name(getattr(self, "custom_frame_variable_name", "") or "")
        if custom_name:
            return custom_name
        assigned_name = normalize_variable_name(getattr(self, "assigned_frame_variable_name", "") or "")
        if assigned_name:
            return assigned_name
        return self.ensure_frame_variable_name()

    def draw_buttons(self, context, layout):
        layout.prop(self, "fps")
        layout.prop(self, "playback_rate")

        box = layout.box()
        box.label(text=f"索引: {self._read_safe_index()}", icon='LINENUMBERS_ON')
        row = box.row(align=True)
        row.prop(self, "custom_frame_variable_name", text="帧变量")
        assigned_name = normalize_variable_name(
            getattr(self, "assigned_frame_variable_name", "") or ""
        )
        if not str(getattr(self, "custom_frame_variable_name", "") or "").strip() and assigned_name:
            row.label(text=f"预分配变量: ${assigned_name}", icon='INFO')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        frame_var = self.frame_variable_name()
        return "\n".join([
            "[Constants]",
            f"global persist ${frame_var} = 0",
            "; 当前帧索引（整数）",
            "[Present]",
            "; 基于系统时间的自动计算（每帧执行）",
            f"${frame_var} = (time * {self.fps}) // 1",
        ])


_load_handler_registered = False


def migrate_legacy_frame_variable_name(node) -> bool:
    """把历史自动生成的 ``swapvar{序号}`` / ``swapvar{序号}_n`` 迁到当前前缀。

    只改「形状就是旧前缀自动生成」的名字：用户手改成别的名字（含 ``swapvar_xxx``
    这类自定义名）一律不动 —— 与形态键预分配「已分配不重分配、只做可证明的迁移」
    同一口径。
    """
    changed = False
    for field in ("custom_frame_variable_name", "assigned_frame_variable_name"):
        raw = str(getattr(node, field, "") or "").strip()
        if not raw:
            continue
        has_dollar = raw.startswith("$")
        match = _LEGACY_FRAME_VARIABLE_RE.match(raw.lstrip("$"))
        if not match:
            continue
        new_name = f"{ANIM_DRIVER_FRAME_PREFIX}{match.group(1)}"
        setattr(node, field, f"${new_name}" if has_dollar else new_name)
        changed = True
    return changed


def migrate_existing_runtime_nodes() -> int:
    """补齐/迁移已有运行时间节点；返回被动过的节点数。

    两件事：
      1. 历史预分配名 ``swapvar{序号}`` → ``anim_frame{序号}``（旧前缀已弃用）；
      2. 输入框为空时把预分配名填进去（与暂停变量/连续索引变量同规则）。

    ``load_post``（打开工程）与 ``register``（插件重载/重新启用）都会调用 —— 后者
    是必需的：Reload Scripts / 重新启用插件**不会**触发 ``load_post``，只挂 load_post
    的话「重载后旧节点没被刷新」。
    """
    migrated = 0
    for tree in getattr(bpy.data, "node_groups", []) or []:
        if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
            continue
        for node in getattr(tree, "nodes", []) or []:
            if getattr(node, "bl_idname", "") != 'SSMTNode_AnimDriver_Runtime':
                continue
            try:
                SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                renamed = migrate_legacy_frame_variable_name(node)
                filled = False
                if not str(getattr(node, "custom_frame_variable_name", "") or "").strip():
                    filled = bool(node._ensure_initial_visible_frame_variable_name())
                if renamed or filled:
                    migrated += 1
                    print(
                        f"[AnimDriver] 运行时间节点 '{node.name}' 帧变量 → "
                        f"${node.frame_variable_name()}"
                        f"{'（旧前缀已迁移）' if renamed else '（补填入输入框）'}"
                    )
            except Exception as exc:
                print(f"[AnimDriver][警告] 运行时间节点 '{getattr(node, 'name', '?')}' 迁移失败: {exc}")
    return migrated


@bpy.app.handlers.persistent
def _runtime_load_handler(dummy, *args):
    migrate_existing_runtime_nodes()


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
    # 插件（重新）加载时也要迁移：Reload Scripts / 重新启用不会触发 load_post
    migrate_existing_runtime_nodes()


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_runtime_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
