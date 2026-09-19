# -*- coding: utf-8 -*-
"""文本追加后处理节点。

在配置表（导出目录根目录的 ``*.ini``）**最下方**追加用户书写的文本片段。

约束与设计
----------
1. **链尾节点**：只有 ``SSMTSocketPostProcess`` 输入口、没有输出口 —— 结构上
   它必然是后处理链的末端，后面接不了任何东西；
   ``blueprint.model.validate_postprocess_node_constraints`` 另外校验它确实是
   最后执行的节点，并且同一导出链只允许一个。
2. **文本来源两种**：
   * 「节点文本」：节点自带一个专用文本块（首次使用自动创建），在 Blender
     文本编辑器里编辑。Blender 的节点 UI 没有原生多行输入控件，文本编辑器是
     唯一可靠的多行编辑面（支持中文输入法、粘贴、撤销）；
   * 「工程文本」：直接引用工程里已有的任意文本块。
3. **「编辑文本」是开关**：点一下打开编辑区域（优先复用已有文本编辑器，没有就
   把当前节点编辑器一分为二），再点一下关闭 —— 我们分裂出来的区域直接关掉，
   复用的编辑器只还原它原来显示的文本。按钮文字随状态在「编辑文本 / 关闭编辑」
   之间切换。
4. **节点上的大文本框**：随节点宽高放大（宽度按节点宽度折行、行数决定节点
   高度），实时显示将要追加的内容；超过显示上限时截断并提示。
5. **可重复执行**：追加内容带块标记，重复导出/重复执行会先移除上一次追加的
   同一块再重新追加，不会堆叠出多份。
6. **「刷新已导出文本」**：不重新导出整个 Mod，只按当前节点文本重写已导出
   配置表里的追加块；内容为空时清除该块（与「刷新已导出驱动 / 刷新已导出
   面板」语义一致：让已导出的文件追上节点当前状态）。
"""

import hashlib
import os

import bpy
from bpy.props import EnumProperty, StringProperty

from ..common.config_table_backup import find_config_table_files
from ..common.text_width_utils import DEFAULT_NARROW_GLYPH_WIDTH, is_wide_character
from .node_postprocess_base import SSMTNode_PostProcess_Base

NODE_BL_IDNAME = "SSMTNode_PostProcess_TextAppend"

TEXT_SOURCE_NODE = "NODE_TEXT"
TEXT_SOURCE_PROJECT = "PROJECT_TEXT"

# 与 node_postprocess_base.AUTO_APPENDED_SECTION_MARKER_PREFIXES 保持一致：
# 其它后处理节点靠这个前缀识别「这是自动追加的尾部块」，从而原样保留。
BLOCK_MARKER_PREFIX = "; --- AUTO-APPENDED CUSTOM TEXT "
BLOCK_BEGIN_SUFFIX = " BEGIN ---"
BLOCK_END_SUFFIX = " END ---"

BLOCK_SEPARATOR = ";" + "=" * 78

#: 节点文本框最多显示的行数（防止超长文本把节点撑到无法操作）
MAX_DISPLAY_LINES = 18
#: 节点文本框最多预览的字符数（每次重绘都要折行，必须封顶）
MAX_DISPLAY_CHARS = 20000
#: 文本框左右留白（节点宽度减去它才是可用文本宽度）
DISPLAY_HORIZONTAL_PADDING = 44.0
#: 显示列宽相对节点宽度的保守系数（宁可折行，也不让文字被节点裁掉）
DISPLAY_COLUMN_SAFETY_FACTOR = 0.92
#: 节点自带文本块的默认前缀
NODE_TEXT_BLOCK_PREFIX = "文本追加_"
#: 新建工程文本块的默认前缀
PROJECT_TEXT_BLOCK_PREFIX = "文本追加片段"

#: 「编辑文本」开关的会话表：节点键 -> {area_ptr, created, previous_text}。
#: created=True 表示编辑区域是本节点分裂出来的（关闭时直接关掉）；
#: created=False 表示复用了用户已有的文本编辑器（关闭时只还原它原来显示的文本）。
_EDITOR_SESSIONS = {}


# ---------------------------------------------------------------------------
# 文本块读写
# ---------------------------------------------------------------------------

def _text_blocks():
    """安全地取 ``bpy.data.texts``（无 bpy / 无 data 时返回 None）。"""
    try:
        return bpy.data.texts
    except Exception:
        return None


def _text_block_exists(name) -> bool:
    blocks = _text_blocks()
    if blocks is None:
        return False
    name = str(name or "").strip()
    if not name:
        return False
    try:
        return blocks.get(name) is not None
    except Exception:
        return False


def _read_text_block(name) -> str:
    """读取文本块内容；文本块不存在或读取失败时返回空串。"""
    blocks = _text_blocks()
    if blocks is None:
        return ""
    name = str(name or "").strip()
    if not name:
        return ""
    try:
        block = blocks.get(name)
    except Exception:
        return ""
    if block is None:
        return ""
    try:
        return str(block.as_string() or "")
    except Exception:
        return ""


def _write_text_block(name, content) -> bool:
    blocks = _text_blocks()
    if blocks is None:
        return False
    name = str(name or "").strip()
    if not name:
        return False
    try:
        block = blocks.get(name)
        if block is None:
            block = blocks.new(name)
        block.clear()
        if content:
            block.write(str(content))
        return True
    except Exception:
        return False


def _get_text_block(name):
    """取已存在的文本块；不存在返回 None。"""
    blocks = _text_blocks()
    if blocks is None:
        return None
    name = str(name or "").strip()
    if not name:
        return None
    try:
        return blocks.get(name)
    except Exception:
        return None


def _ensure_text_block(name):
    """取文本块，不存在则创建；失败返回 None。"""
    blocks = _text_blocks()
    if blocks is None:
        return None
    name = str(name or "").strip()
    if not name:
        return None
    try:
        block = blocks.get(name)
        if block is None:
            block = blocks.new(name)
        return block
    except Exception:
        return None


def _node_text_block_name(node) -> str:
    """节点自带文本块的稳定名称（首次调用生成并缓存到节点上）。

    缓存的意义：节点被重命名后仍指向同一个文本块，内容不会「丢失」到
    另一个名字下。
    """
    stored = str(getattr(node, "own_text_block_name", "") or "").strip()
    if stored:
        return stored

    tree_name = str(getattr(getattr(node, "id_data", None), "name", "") or "")
    node_name = str(getattr(node, "name", "") or "")
    identity = f"{tree_name}\0{node_name}"
    digest = hashlib.blake2s(identity.encode("utf-8"), digest_size=6).hexdigest()
    name = f"{NODE_TEXT_BLOCK_PREFIX}{digest}"
    try:
        node.own_text_block_name = name
    except Exception:
        pass
    return name


def _block_id_for(node) -> str:
    """追加块的稳定标识：由节点自带文本块名派生，重命名节点也不会变。"""
    digest = hashlib.blake2s(
        _node_text_block_name(node).encode("utf-8"), digest_size=6
    ).hexdigest()
    return digest


def _resolve_node(context, node_name):
    """按节点名在当前节点树里取节点（节点算子统一入口）。"""
    space_data = getattr(context, "space_data", None)
    tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
    if tree is None:
        return None
    try:
        return tree.nodes.get(str(node_name or ""))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 追加内容的构造与解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def wrap_display_lines(text, max_columns):
    """按显示列宽折行：宽字符（中日韩全角）算 2 列，其它算 1 列。"""
    try:
        columns = max(4, int(max_columns))
    except (TypeError, ValueError):
        columns = 40

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    display_lines = []
    for raw_line in normalized.split("\n"):
        expanded = raw_line.expandtabs(4)
        if not expanded:
            display_lines.append("")
            continue
        current = ""
        current_width = 0
        for char in expanded:
            char_width = 2 if is_wide_character(char) else 1
            if current and current_width + char_width > columns:
                display_lines.append(current)
                current = ""
                current_width = 0
            current += char
            current_width += char_width
        display_lines.append(current)
    return display_lines


def format_appended_block(block_id, content, newline="\n"):
    """把用户内容包装成带块标记的追加块（含块首尾标记行）。"""
    body = (
        str(content or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .rstrip("\n")
    )
    lines = [
        f"{BLOCK_MARKER_PREFIX}{block_id}{BLOCK_BEGIN_SUFFIX}",
        BLOCK_SEPARATOR,
    ]
    if body:
        lines.extend(body.split("\n"))
    lines.append(BLOCK_SEPARATOR)
    lines.append(f"{BLOCK_MARKER_PREFIX}{block_id}{BLOCK_END_SUFFIX}")
    return newline.join(lines) + newline


def remove_appended_block(content, block_id):
    """移除同一节点上一次追加的块（含标记行），返回剩余内容。

    块不完整（只有 BEGIN 没有 END）时删到文件尾：本节点永远追加在文件末尾，
    半块之后不可能还有属于原文件的内容。
    """
    text = str(content or "")
    begin_token = f"{BLOCK_MARKER_PREFIX}{block_id}{BLOCK_BEGIN_SUFFIX}"
    end_token = f"{BLOCK_MARKER_PREFIX}{block_id}{BLOCK_END_SUFFIX}"

    lines = text.splitlines(keepends=True)
    start_index = None
    end_index = None
    for index, line in enumerate(lines):
        if start_index is None:
            if begin_token in line:
                start_index = index
            continue
        if end_token in line:
            end_index = index
            break

    if start_index is None:
        return text
    if end_index is None:
        end_index = len(lines) - 1
    return "".join(lines[:start_index] + lines[end_index + 1:])


def build_appended_content(original, content, block_id):
    """把追加块放到 ``original`` 的最下方（先清掉同一块的旧副本）。"""
    original_text = str(original or "")
    newline = "\r\n" if "\r\n" in original_text else "\n"

    stripped = remove_appended_block(original_text, block_id)
    base = stripped.rstrip("\r\n")
    block = format_appended_block(block_id, content, newline=newline)

    if not base:
        return block
    return f"{base}{newline}{newline}{block}"


def strip_appended_block(original, block_id):
    """移除本节点追加的块，并收拢块前多出来的空行（幂等）。

    返回清理后的内容；原内容里没有本节点的块时**原样返回**（不改变换行风格）。
    换行风格跟随原内容：CRLF 文件清理后仍然是纯 CRLF，不会混入裸 LF。
    """
    original_text = str(original or "")
    removed = remove_appended_block(original_text, block_id)
    if removed == original_text:
        return original_text

    newline = "\r\n" if "\r\n" in original_text else "\n"
    body = removed.rstrip("\r\n")
    return f"{body}{newline}" if body else ""


def _describe_files(paths) -> str:
    """把文件路径列表写成简短提示（超过 3 个只报数量，避免撑爆状态栏）。"""
    names = [os.path.basename(str(path)) for path in paths]
    if len(names) > 3:
        return f"{len(names)} 个配置表"
    return ", ".join(names)


# ---------------------------------------------------------------------------
# 算子：编辑文本 / 清空 / 新建工程文本 / 刷新已导出文本
# ---------------------------------------------------------------------------

def _area_keys(screen):
    keys = set()
    for area in getattr(screen, "areas", None) or []:
        try:
            keys.add(area.as_pointer())
        except Exception:
            continue
    return keys


def _bind_text_editor(area, block) -> bool:
    """把文本块绑到区域里的文本编辑器 space 上；没有文本编辑器 space 返回 False。"""
    bound = False
    try:
        for space in area.spaces:
            if space.type == 'TEXT_EDITOR':
                space.text = block
                bound = True
        area.tag_redraw()
    except Exception:
        return False
    return bound


def _iter_areas():
    """遍历当前所有窗口/区域（拿不到时为空）。"""
    window_manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(window_manager, "windows", None) or []:
        screen = getattr(window, "screen", None)
        for area in getattr(screen, "areas", None) or []:
            yield window, area


def _area_by_pointer(pointer):
    for window, area in _iter_areas():
        try:
            if area.as_pointer() == pointer:
                return window, area
        except Exception:
            continue
    return None, None


def _session_key(node) -> str:
    tree_name = str(getattr(getattr(node, "id_data", None), "name", "") or "")
    return f"{tree_name}::{getattr(node, 'name', '')}"


def _prune_sessions():
    """丢掉区域已经不存在（被用户手动关掉）的会话。"""
    live_pointers = set()
    for _window, area in _iter_areas():
        try:
            live_pointers.add(area.as_pointer())
        except Exception:
            continue
    for key in [k for k, s in _EDITOR_SESSIONS.items() if s.get("area_ptr") not in live_pointers]:
        _EDITOR_SESSIONS.pop(key, None)


def is_editor_open(node) -> bool:
    """该节点当前是否已经打开编辑区域（决定按钮显示「编辑文本」还是「关闭编辑」）。"""
    session = _EDITOR_SESSIONS.get(_session_key(node))
    if not session:
        return False
    _window, area = _area_by_pointer(session.get("area_ptr"))
    if area is None or getattr(area, "type", "") != 'TEXT_EDITOR':
        return False
    return True


def _find_text_editor(block):
    """找一个文本编辑器区域：优先已经在显示目标文本块的那个。

    返回 ``(window, area, previous_text_name)``；一个都没有时 area 为 None。
    """
    target_name = str(getattr(block, "name", "") or "")
    fallback = None
    for window, area in _iter_areas():
        if getattr(area, "type", "") != 'TEXT_EDITOR':
            continue
        current_name = ""
        try:
            for space in area.spaces:
                if space.type != 'TEXT_EDITOR':
                    continue
                current_name = str(getattr(getattr(space, "text", None), "name", "") or "")
                break
        except Exception:
            continue
        if target_name and current_name == target_name:
            return window, area, current_name
        if fallback is None:
            fallback = (window, area, current_name)
    return fallback if fallback is not None else (None, None, "")


def _split_text_editor(context, block):
    """把当前节点编辑器一分为二，新区域改成文本编辑器并绑定文本块。

    成功返回新区域对象，失败返回 None。
    """
    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    screen = getattr(window, "screen", None)
    if window is None or area is None or screen is None:
        return None
    if not hasattr(context, "temp_override"):
        return None

    before_keys = _area_keys(screen)
    override = {"window": window, "screen": screen, "area": area}
    region = next(
        (r for r in getattr(area, "regions", None) or [] if getattr(r, "type", "") == 'WINDOW'),
        None,
    )
    if region is not None:
        override["region"] = region

    try:
        with context.temp_override(**override):
            bpy.ops.screen.area_split(direction='HORIZONTAL', factor=0.5)
    except Exception:
        return None

    new_area = next(
        (a for a in screen.areas if a.as_pointer() not in before_keys),
        None,
    )
    if new_area is None:
        return None
    try:
        new_area.type = 'TEXT_EDITOR'
    except Exception:
        return None
    if not _bind_text_editor(new_area, block):
        return None
    return new_area


def _close_area(context, window, area) -> bool:
    """关掉我们分裂出来的编辑区域（等价于右键 Close Area）。"""
    screen = getattr(window, "screen", None)
    if screen is None or not hasattr(context, "temp_override"):
        return False
    override = {"window": window, "screen": screen, "area": area}
    region = next(
        (r for r in getattr(area, "regions", None) or [] if getattr(r, "type", "") == 'WINDOW'),
        None,
    )
    if region is not None:
        override["region"] = region
    try:
        with context.temp_override(**override):
            bpy.ops.screen.area_close()
    except Exception:
        return False
    return True


def _open_editor(context, node, block) -> bool:
    """打开编辑区域：复用已有文本编辑器，没有才分裂出一个。"""
    _prune_sessions()
    key = _session_key(node)

    window, area, previous_text = _find_text_editor(block)
    if area is not None:
        if not _bind_text_editor(area, block):
            return False
        _EDITOR_SESSIONS[key] = {
            "area_ptr": area.as_pointer(),
            "created": False,
            "previous_text": previous_text,
        }
        return True

    new_area = _split_text_editor(context, block)
    if new_area is None:
        return False
    _EDITOR_SESSIONS[key] = {
        "area_ptr": new_area.as_pointer(),
        "created": True,
        "previous_text": "",
    }
    return True


def _close_editor(context, node) -> bool:
    """关闭编辑区域：我们分裂出来的直接关掉，复用的还原它原来显示的文本。

    返回 True 表示本次点击确实完成了关闭（会话已清）；返回 False 表示关闭失败，
    此时**保留会话**，让按钮状态和实际界面保持一致，用户还能再点一次。
    """
    key = _session_key(node)
    session = _EDITOR_SESSIONS.get(key)
    if not session:
        return False

    window, area = _area_by_pointer(session.get("area_ptr"))
    if area is None:
        # 区域已经被用户手动关掉：会话清掉就算关闭完成。
        _EDITOR_SESSIONS.pop(key, None)
        return True

    if session.get("created"):
        if not _close_area(context, window, area):
            return False
        _EDITOR_SESSIONS.pop(key, None)
        return True

    # 复用的是用户自己的文本编辑器：只还原它原来显示的文本，绝不关掉用户的区域。
    previous_name = str(session.get("previous_text", "") or "")
    previous_block = None
    if previous_name:
        blocks = _text_blocks()
        previous_block = blocks.get(previous_name) if blocks is not None else None
    try:
        for space in area.spaces:
            if space.type == 'TEXT_EDITOR':
                space.text = previous_block
        area.tag_redraw()
    except Exception:
        pass
    _EDITOR_SESSIONS.pop(key, None)
    return True


class SSMT_OT_TextAppend_ToggleEditor(bpy.types.Operator):
    """点一下打开编辑区域，再点一下关闭。"""

    bl_idname = "ssmt.text_append_toggle_editor"
    bl_label = "编辑文本 / 关闭编辑"
    bl_description = (
        "点一下在文本编辑器中打开该节点要追加的文本（支持多行/中文输入法/粘贴），"
        "再点一下关闭编辑区域"
    )
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: StringProperty()

    def execute(self, context):
        node = _resolve_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "未找到文本追加节点")
            return {'CANCELLED'}

        if is_editor_open(node):
            if _close_editor(context, node):
                return {'FINISHED'}
            self.report({'WARNING'}, "无法自动关闭编辑区域，请手动右键关闭该区域")
            return {'CANCELLED'}

        block = node.get_text_block(create=True)
        if block is None:
            self.report({'ERROR'}, "无法创建/获取文本块，请检查工程文本选择")
            return {'CANCELLED'}

        if _open_editor(context, node, block):
            return {'FINISHED'}

        self.report(
            {'INFO'},
            f"文本块 '{getattr(block, 'name', '')}' 已就绪：请打开一个文本编辑器(Text Editor)后在其中选择它",
        )
        return {'FINISHED'}


class SSMT_OT_TextAppend_ClearText(bpy.types.Operator):
    """清空节点自带文本块。"""

    bl_idname = "ssmt.text_append_clear_text"
    bl_label = "清空文本"
    bl_description = "清空该节点自带文本块的内容"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        node = _resolve_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "未找到文本追加节点")
            return {'CANCELLED'}
        block = node.get_text_block(create=False)
        if block is None:
            return {'CANCELLED'}
        try:
            block.clear()
        except Exception as exc:
            self.report({'ERROR'}, f"清空文本失败: {exc}")
            return {'CANCELLED'}
        return {'FINISHED'}


class SSMT_OT_TextAppend_NewProjectText(bpy.types.Operator):
    """新建一个工程文本块并选中它。"""

    bl_idname = "ssmt.text_append_new_project_text"
    bl_label = "新建文本块"
    bl_description = "新建一个工程文本块，并把它设为该节点引用的文本"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: StringProperty()

    def execute(self, context):
        node = _resolve_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "未找到文本追加节点")
            return {'CANCELLED'}
        blocks = _text_blocks()
        if blocks is None:
            self.report({'ERROR'}, "无法访问文本块列表")
            return {'CANCELLED'}

        name = PROJECT_TEXT_BLOCK_PREFIX
        index = 1
        try:
            while blocks.get(name) is not None:
                name = f"{PROJECT_TEXT_BLOCK_PREFIX}_{index}"
                index += 1
            blocks.new(name)
        except Exception as exc:
            self.report({'ERROR'}, f"新建文本块失败: {exc}")
            return {'CANCELLED'}

        node.project_text_name = name
        node.text_source = TEXT_SOURCE_PROJECT
        return {'FINISHED'}


class SSMT_OT_TextAppend_RefreshExportedText(bpy.types.Operator):
    """不重新导出整个 Mod，只按当前文本重写已导出配置表里的追加块。"""

    bl_idname = "ssmt.text_append_refresh_exported_text"
    bl_label = "刷新已导出文本"
    bl_description = (
        "不重新导出整个Mod，仅按当前节点文本重写已导出配置表中的文本追加块"
        "（内容为空时清除该块）"
    )
    bl_options = {'REGISTER'}

    node_name: StringProperty(
        name="Node Name",
        description="关联的文本追加后处理节点名称",
        default="",
    )

    def execute(self, context):
        node = _resolve_node(context, self.node_name)
        if node is None or getattr(node, "bl_idname", "") != NODE_BL_IDNAME:
            self.report({'ERROR'}, "未找到文本追加后处理节点")
            return {'CANCELLED'}

        from ..common.global_config import GlobalConfig
        GlobalConfig.read_from_main_json_ssmt4()
        mod_export_path = str(GlobalConfig.path_generate_mod_folder() or "").strip()
        if not mod_export_path or not os.path.isdir(mod_export_path):
            self.report({'ERROR'}, "当前导出目录不存在，请先确认Generate Mod输出路径")
            return {'CANCELLED'}

        success, message = node.refresh_exported_text_block(mod_export_path)
        self.report({'INFO'} if success else {'ERROR'}, message)
        return {'FINISHED'} if success else {'CANCELLED'}


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

class SSMTNode_PostProcess_TextAppend(SSMTNode_PostProcess_Base):
    bl_idname = NODE_BL_IDNAME
    bl_label = "文本追加"
    bl_description = (
        "在配置表最下方追加自定义文本片段；只能接在后处理链最末端（没有输出口）"
    )
    bl_icon = 'TEXT'
    bl_width_min = 360

    text_source: EnumProperty(
        name="文本来源",
        description="追加内容的来源",
        items=(
            (TEXT_SOURCE_NODE, "节点文本", "使用节点自带的文本块（在文本编辑器中编辑）"),
            (TEXT_SOURCE_PROJECT, "工程文本", "引用工程中已有的文本块"),
        ),
        default=TEXT_SOURCE_NODE,
    )
    project_text_name: StringProperty(
        name="工程文本",
        description="引用的工程文本块名称",
        default="",
    )
    own_text_block_name: StringProperty(
        name="节点文本块",
        description="节点自带文本块的名称（自动生成，随节点保存）",
        default="",
        options={'HIDDEN'},
    )

    def init(self, context):
        # 只有输入、没有输出：结构上它必然是后处理链的末端。
        self.inputs.new('SSMTSocketPostProcess', "Input")
        self.width = 420
        # 先定下自带文本块名，重命名节点后仍指向同一份内容。
        _node_text_block_name(self)

    def copy(self, node):
        # 复制节点时另建独立文本块，避免两个节点共享同一份文本互相覆盖。
        source_content = _read_text_block(_node_text_block_name(node)) if node is not None else ""
        self.own_text_block_name = ""
        new_name = _node_text_block_name(self)
        if source_content:
            _write_text_block(new_name, source_content)

    # ------------------------------------------------------------------
    # 文本来源
    # ------------------------------------------------------------------

    def get_source_text(self) -> str:
        """当前将要追加到配置表的内容。"""
        if self.text_source == TEXT_SOURCE_PROJECT:
            return _read_text_block(self.project_text_name)
        return _read_text_block(_node_text_block_name(self))

    def get_text_block(self, create=False):
        """取当前来源对应的文本块对象（create=True 时不存在就创建）。"""
        if self.text_source == TEXT_SOURCE_PROJECT:
            name = str(self.project_text_name or "").strip()
        else:
            name = _node_text_block_name(self)
        if not name:
            return None
        return _ensure_text_block(name) if create else _get_text_block(name)

    def get_block_id(self) -> str:
        return _block_id_for(self)

    def validate_export_configuration(self):
        """后处理链校验时调用：工程文本引用必须有效。"""
        if self.text_source != TEXT_SOURCE_PROJECT:
            return
        name = str(self.project_text_name or "").strip()
        if not name:
            raise ValueError("文本追加节点：请先选择要引用的工程文本块")
        if not _text_block_exists(name):
            raise ValueError(f"文本追加节点：工程文本块 '{name}' 不存在，请重新选择")

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _display_columns(self) -> int:
        try:
            width = float(self.width)
        except (TypeError, ValueError):
            width = 420.0
        available = max(80.0, width - DISPLAY_HORIZONTAL_PADDING)
        columns = int(available / DEFAULT_NARROW_GLYPH_WIDTH * DISPLAY_COLUMN_SAFETY_FACTOR)
        return max(12, columns)

    def _draw_text_box(self, layout):
        """节点上的大文本框：随节点宽度折行、随内容行数撑高节点。"""
        box = layout.box()
        content = self.get_source_text()
        if not str(content or "").strip():
            box.label(text="（空）点「编辑文本」输入要追加的内容", icon='INFO')
            return

        truncated_chars = len(content) > MAX_DISPLAY_CHARS
        if truncated_chars:
            # 重绘频率很高，折行必须封顶，否则长文本会拖慢整个节点编辑器。
            content = content[:MAX_DISPLAY_CHARS]

        display_lines = wrap_display_lines(content, self._display_columns())
        shown_lines = display_lines[:MAX_DISPLAY_LINES]
        for line in shown_lines:
            # 空行也要占一行高度，否则节点高度会跳。
            box.label(text=line if line else " ")
        hidden_count = len(display_lines) - len(shown_lines)
        if hidden_count > 0 or truncated_chars:
            suffix = f"（已截断到前 {MAX_DISPLAY_CHARS} 字符）" if truncated_chars else ""
            box.label(
                text=f"…… 还有 {hidden_count} 行{suffix}，点「编辑文本」查看全部",
                icon='INFO',
            )

    def draw_buttons(self, context, layout):
        header = layout.box()
        header.label(text="导出时追加到配置表最下方", icon='TEXT')
        header.prop(self, "text_source", text="")

        if self.text_source == TEXT_SOURCE_PROJECT:
            row = header.row(align=True)
            row.prop_search(self, "project_text_name", bpy.data, "texts", text="")
            row.operator(
                "ssmt.text_append_new_project_text", text="", icon='ADD'
            ).node_name = self.name
            if not _text_block_exists(self.project_text_name):
                header.label(text="文本块不存在：导出会被拦截", icon='ERROR')
        else:
            header.label(text=f"文本块: {_node_text_block_name(self)}", icon='TEXT')

        row = header.row(align=True)
        editor_open = is_editor_open(self)
        row.operator(
            "ssmt.text_append_toggle_editor",
            text="关闭编辑" if editor_open else "编辑文本",
            icon='X' if editor_open else 'TEXT',
        ).node_name = self.name
        if self.text_source == TEXT_SOURCE_NODE:
            row.operator(
                "ssmt.text_append_clear_text", text="清空", icon='TRASH'
            ).node_name = self.name

        refresh_row = header.row(align=True)
        refresh_op = refresh_row.operator(
            "ssmt.text_append_refresh_exported_text",
            text="刷新已导出文本",
            icon='FILE_REFRESH',
        )
        refresh_op.node_name = self.name

        self._draw_text_box(layout)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def _apply_appended_text(self, mod_export_path, remove_when_empty=False):
        """把当前文本写入导出目录根目录的所有配置表。

        返回 ``(written, unchanged, skipped, failed)`` 四个完整路径列表：
        实际改写的 / 内容已是最新的 / 读取或编码失败跳过的 / 写入失败的。

        ``remove_when_empty=True`` 时，空文本表示「该节点不该在配置表里留下
        任何东西」—— 清掉上一次追加的块（「刷新已导出文本」的语义）；
        默认 False 则空文本是安全 no-op（正式导出时配置表是重新生成的）。
        """
        from ..utils.log_utils import LOG

        content = self.get_source_text()
        has_content = bool(str(content or "").strip())
        block_id = self.get_block_id()

        written, unchanged, skipped, failed = [], [], [], []
        for ini_path in find_config_table_files(mod_export_path):
            try:
                with open(ini_path, "rb") as handle:
                    raw_bytes = handle.read()
            except OSError as exc:
                LOG.warning(f"📄 文本追加节点：读取配置表失败 {ini_path}: {exc}")
                skipped.append(ini_path)
                continue

            # 二进制读取 + 显式解码：既保留原有换行风格，也能原样保留 BOM。
            has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
            try:
                original = raw_bytes.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                LOG.warning(f"📄 文本追加节点：配置表不是 UTF-8，跳过 {ini_path}: {exc}")
                skipped.append(ini_path)
                continue

            if has_content:
                updated = build_appended_content(original, content, block_id)
            elif remove_when_empty:
                updated = strip_appended_block(original, block_id)
            else:
                unchanged.append(ini_path)
                continue

            if updated == original:
                # 重复执行（同一份内容已经在最下方）时保持文件原样。
                unchanged.append(ini_path)
                continue

            self._create_cumulative_backup(ini_path, mod_export_path)
            try:
                with open(
                    ini_path,
                    "w",
                    encoding="utf-8-sig" if has_bom else "utf-8",
                    newline="",
                ) as handle:
                    handle.write(updated)
            except (OSError, UnicodeError) as exc:
                LOG.warning(f"📄 文本追加节点：写入配置表失败 {ini_path}: {exc}")
                failed.append(ini_path)
                continue

            written.append(ini_path)
            if has_content:
                LOG.info(f"📄 文本追加节点：已追加到 {os.path.basename(ini_path)}")
            else:
                LOG.info(f"📄 文本追加节点：已从 {os.path.basename(ini_path)} 清除追加块")

        return written, unchanged, skipped, failed

    def execute_postprocess(self, mod_export_path):
        from ..utils.log_utils import LOG

        content = self.get_source_text()
        if not str(content or "").strip():
            LOG.warning("📄 文本追加节点：内容为空，未向配置表追加任何内容")
            return False

        if not find_config_table_files(mod_export_path):
            LOG.warning(f"📄 文本追加节点：导出目录未找到配置表（*.ini）: {mod_export_path}")
            return False

        written, _unchanged, _skipped, _failed = self._apply_appended_text(mod_export_path)
        if written:
            LOG.info(f"   ✅ 文本追加节点执行完成：{len(written)} 个配置表")
        return bool(written)

    def refresh_exported_text_block(self, mod_export_path):
        """不重新导出整个 Mod，仅按当前文本重写已导出配置表里的追加块。

        返回 ``(success, message)``。内容为空时清除本节点上一次追加的块，
        让已导出文件追上节点当前状态（与刷新已导出驱动/面板的语义一致）。
        """
        try:
            self.validate_export_configuration()
        except ValueError as exc:
            return False, str(exc)

        if not find_config_table_files(mod_export_path):
            return False, f"导出目录中未找到配置表（*.ini）: {mod_export_path}"

        has_content = bool(str(self.get_source_text() or "").strip())
        written, unchanged, skipped, failed = self._apply_appended_text(
            mod_export_path, remove_when_empty=True
        )

        if failed:
            return False, f"写入配置表失败: {_describe_files(failed)}"
        if written:
            if has_content:
                return True, f"已刷新配置表中的文本追加块: {_describe_files(written)}"
            return True, f"文本为空，已清除配置表中的文本追加块: {_describe_files(written)}"
        if skipped and not unchanged:
            return False, f"配置表读取失败或不是 UTF-8，未能刷新: {_describe_files(skipped)}"
        if has_content:
            return True, f"文本追加块已是最新状态: {_describe_files(unchanged)}"
        return True, f"配置表中没有本节点追加的文本块: {_describe_files(unchanged)}"


classes = (
    SSMT_OT_TextAppend_ToggleEditor,
    SSMT_OT_TextAppend_ClearText,
    SSMT_OT_TextAppend_NewProjectText,
    SSMT_OT_TextAppend_RefreshExportedText,
    SSMTNode_PostProcess_TextAppend,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    # 只清会话表，不动用户当前的界面布局。
    _EDITOR_SESSIONS.clear()
