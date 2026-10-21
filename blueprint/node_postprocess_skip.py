# -*- coding: utf-8 -*-
"""跳过部件后处理节点。

工作方式（与「条件判断」节点共用同一套结构化圈定机制）
------------------------------------------------------
1. 用 ``BlueprintExportHelper.get_current_blueprint_model()`` 拿到**本次导出**的
   蓝图模型；
2. 交给 ``node_postprocess_condition._collect_target_section_names`` 圈定
   「选中部件 -> draw_ib -> 导出期登记的段名」；
3. 把这些段从导出 ini 里**整段删除**（资源声明段 ``Resource*`` 与检测段
   ``TextureOverrideCheckHash*`` 已在圈定层排除，保持不动）；
4. 从**选中的物体名**解析绘制条目 ``(hash, indexCount, firstIndex)``，按条目追加
   本节点自己的跳过段，并登记进导出段登记
   （``common/export_section_registry``）—— 因此链上后面的「条件判断」节点
   能圈到新段，把 ``handling = skip`` 包进 ``if``：条件不通过时跳过失效，
   效果等同删掉跳过段。

无导出段的 IB（游戏原生绘制）
--------------------------------
若某个 draw_ib 在本次导出里**没有登记段**（该 IB 是游戏原生绘制、不在本 mod 的
接管范围里），节点**不再早退**：直接追加跳过段（完整三元组形式；解析不出三段
时是 ``[TextureOverride_IB_<hash>]`` + ``hash`` + ``handling = skip``）让原生绘制
被跳过（没有段可删，删除步骤为 0 段）。有导出段的 IB 仍走「先删段、再追加」，
删除范围**仍按 draw_ib 集合**（不变）。

跳过段形状（**优先用完整三元组**，来自物体名前缀，hash 统一小写）::

    ; --- AUTO-APPENDED SKIP BLOCK ---
    ; skip <hash>_<indexCount>_<firstIndex>
    [TextureOverride_IB_<hash>_<indexCount>_<firstIndex>]
    hash = <hash>
    match_first_index = <firstIndex>
    match_index_count = <indexCount>
    handling = skip

**为什么要三段**：只写 ``hash`` 的跳过段在部分游戏里不生效（同一 IB hash 被
多次绘制、或需要按 index 区间区分绘制批次），必须同时匹配 ``match_first_index``
与 ``match_index_count`` 才能稳定跳过。三元组来自物体名前缀
（``LOD0.999bff94-6462-17946.前发`` -> ``999bff94`` / ``6462`` / ``17946``），
解析实现见 :func:`_parse_part_draw`。

物体名解析不出三段时（例如老工程里的 ``Body_copy``）**回退为只 hash 的旧形式**::

    ; skip <hash>
    [TextureOverride_IB_<hash>]
    hash = <hash>
    handling = skip

幂等：块由 ``; --- AUTO-APPENDED SKIP BLOCK ---`` 标记。执行时**先剥掉旧块再
删段**，避免第二次执行把旧跳过段当成目标段重复统计；处理完统一重新追加。
``node_postprocess_base.AUTO_APPENDED_SECTION_MARKER_PREFIXES`` 已登记该前缀，
其它后处理节点重写配置表时会把它当尾部内容原样保留。

为什么不再扫描 ini 关键词
--------------------------
段名里的 hash 可能是 VB hash / 贴图 hash（见 ``common/export_section_registry``
模块说明），按关键词反推必然漏段；只读导出期结构化登记才能「准确无漏」地
圈定某个 IB 的全部导出段。
"""

import bpy
import os
import re

from .node_postprocess_base import SSMTNode_PostProcess_Base


NODE_IDNAME = "SSMTNode_PostProcess_Skip"
NODE_LABEL = "跳过"

#: 本节点追加块的标记行；``node_postprocess_base`` 用它的前缀识别「自动追加尾段」。
SKIP_BLOCK_MARKER = "; --- AUTO-APPENDED SKIP BLOCK ---"
SKIP_BLOCK_MARKER_PREFIX = "; --- AUTO-APPENDED SKIP "

#: 段头解析口径（``[SectionName]``）。**条件判断节点直接复用本符号**，名字与
#: 语义不能变：它必须只匹配「整行就是一个段头」的行。
_SECTION_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s*$")

#: 导出时的运行时副本后缀（可重复、可带数字）。
_RUNTIME_SUFFIX_RE = re.compile(r"(_(?:copy|dup\d*|chain\d*|bpe\d*))+$", re.IGNORECASE)

#: 其它节点在配置表尾部追加块的通用标记前缀（用于把本节点块与它们分开）。
_AUTO_APPENDED_OTHER_PREFIX = "; --- AUTO-APPENDED "

#: 物体名前缀里的「三段绘制信息」：``<hash>-<indexCount>-<firstIndex>``。
#: 例：``LOD0.999bff94-6462-17946.前发`` -> ``999bff94`` / ``6462`` / ``17946``。
#: 后面允许跟分隔符（``.`` / ``_`` / ``-``）或字符串结束。
_PART_DRAW_RE = re.compile(r"^([0-9a-fA-F]{8})-(\d+)-(\d+)(?:[._\-]|$)")

#: 物体名可选的 ``LODx.`` 前缀（``LOD0.`` / ``LOD1.`` ...）。
_PART_LOD_PREFIX_RE = re.compile(r"^LOD\d+[.]", re.IGNORECASE)

#: 只 hash 的兜底解析口径（与 ``node_postprocess_condition._parse_hash_from_object_name``
#: 同口径）：先看开头 8 位十六进制，再找前后带分隔符的 8 位十六进制。
_PART_HASH_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8}")
_PART_HASH_FALLBACK_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})(?=[-_.]|$)")


# --------------------------------------------------------------------------- #
# 纯函数（不依赖 bpy，便于 headless 测试）
# --------------------------------------------------------------------------- #

def _strip_runtime_suffixes(name):
    """反复剥掉 ``_copy`` / ``_dup1`` / ``_chain2`` / ``_BPE`` 这类运行时后缀。"""
    current = str(name or "").strip()
    for _ in range(16):
        stripped = _RUNTIME_SUFFIX_RE.sub("", current)
        if stripped == current:
            break
        current = stripped
    return current


def _parse_part_draw(name):
    """从物体名解析 ``(hash, index_count, first_index)``；解析不出三段返回 ``None``。

    物体名格式（SSMT 导出期的部件名前缀）::

        [LODx.]<hash>-<indexCount>-<firstIndex>[.<部件名>[.<序号>]]

    - 去首尾空白；``LOD\\d+.`` 前缀可缺省；
    - 必须**三段齐全**（hash 8 位十六进制 + 两个非负整数）才算完整，否则 ``None``
      —— 缺段的条目由调用方回退成只 hash 的旧形式；
    - hash 统一小写，``index_count`` / ``first_index`` 转 ``int``。

    样例::

        >>> _parse_part_draw("LOD0.999bff94-6462-17946.前发")
        ('999bff94', 6462, 17946)
        >>> _parse_part_draw("LOD0.38b3bd13-4086-0.麻花辫.001")
        ('38b3bd13', 4086, 0)
        >>> _parse_part_draw("随便一个物体") is None
        True
    """
    value = str(name or "").strip()
    if not value:
        return None
    value = _PART_LOD_PREFIX_RE.sub("", value)
    match = _PART_DRAW_RE.match(value)
    if not match:
        return None
    try:
        return (match.group(1).lower(), int(match.group(2)), int(match.group(3)))
    except (TypeError, ValueError):
        return None


def _parse_part_hash(name):
    """只解析物体名里的 8 位 IB hash（三段解析失败时的兜底）；失败返回 ""。"""
    value = str(name or "").strip()
    if not value:
        return ""
    value = _PART_LOD_PREFIX_RE.sub("", value)
    match = _PART_HASH_PREFIX_RE.match(value)
    if match:
        return match.group(0).lower()
    fallback = _PART_HASH_FALLBACK_RE.search(value)
    return fallback.group(1).lower() if fallback else ""


def _normalize_skip_entries(entries):
    """把条目整理成 ``[(hash, index_count|None, first_index|None)]``（去重、保序）。

    接受三种写法：``(hash, count, first)`` 三元组（完整段）、``(hash,)`` 单元素、
    以及裸字符串 / ``None``（``index_count`` / ``first_index`` 为 ``None`` 表示
    「只有 hash」的回退条目）。去重键 = ``(hash, count, first)``。
    """
    result = []
    seen = set()
    for item in entries or ():
        if item is None:
            continue
        if isinstance(item, str):
            hash_val, index_count, first_index = item, None, None
        else:
            try:
                values = tuple(item)
            except TypeError:
                continue
            if not values:
                continue
            hash_val = values[0]
            index_count = values[1] if len(values) > 1 else None
            first_index = values[2] if len(values) > 2 else None

        hash_text = str(hash_val or "").strip().lower()
        if not hash_text:
            continue
        try:
            count_value = int(index_count) if index_count is not None else None
            first_value = int(first_index) if first_index is not None else None
        except (TypeError, ValueError):
            count_value = None
            first_value = None
        if count_value is None or first_value is None:
            count_value = None
            first_value = None

        key = (hash_text, count_value, first_value)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _entries_from_names(names):
    """从选中物体名解析跳过条目；三段解析失败时回退为只 hash 条目。

    - 三段齐全的物体 -> ``(hash, index_count, first_index)`` 完整段；
    - 只解析得出 hash 的物体 -> ``(hash, None, None)`` 回退段；
    - 解析不出任何 hash 的名字**不产生条目**（不制造垃圾段）；
    - 若同一个 hash 已经有完整段（另一个物体解析出了三段），**丢弃该 hash 的
      回退段**：只 hash 的段会匹配该 IB 的全部绘制、把精确段的范围覆盖掉。

    顺序与去重：按输入顺序、以 ``(hash, index_count, first_index)`` 去重。
    """
    prepared = []  # [(entry_or_None, hash_or_None)]，保持输入顺序
    full_hashes = set()
    for name in names or ():
        parsed = _parse_part_draw(name)
        if parsed is not None:
            prepared.append(parsed)
            full_hashes.add(parsed[0])
        else:
            prepared.append(_parse_part_hash(name) or None)

    entries = []
    seen = set()
    for item in prepared:
        if item is None:
            continue
        if isinstance(item, str):
            if item in full_hashes:
                continue
            entry = (item, None, None)
        else:
            entry = item
        if entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries


def _hash_only_entries(values):
    """把一组 hash（通常是 draw_ib）整理成只 hash 的条目列表（去重、保序）。"""
    entries = []
    seen = set()
    for value in values or ():
        hash_val = str(value or "").strip().lower()
        if not hash_val or hash_val in seen:
            continue
        seen.add(hash_val)
        entries.append((hash_val, None, None))
    return entries


def _skip_section_name(entry):
    """条目对应的段名：三段齐全用完整名，否则退回 ``TextureOverride_IB_<hash>``。"""
    hash_val, index_count, first_index = entry
    if index_count is None or first_index is None:
        return f"TextureOverride_IB_{hash_val}"
    return f"TextureOverride_IB_{hash_val}_{index_count}_{first_index}"


def _format_skip_entry(entry):
    """条目的日志写法：``hash_count_first`` 或 ``hash``。"""
    hash_val, index_count, first_index = entry
    if index_count is None or first_index is None:
        return str(hash_val)
    return f"{hash_val}_{index_count}_{first_index}"


def _section_ranges(lines):
    """把行列表切成段，返回 ``[(段名, 段头索引, 段体起始, 段体结束)]``。

    口径与 ``node_postprocess_condition._find_sections`` 一致：结束索引独占，
    等于下一个段头索引或文件末尾；第一个段头之前的前导内容不属于任何段。
    """
    lines = list(lines or [])
    headers = []
    for index, raw_line in enumerate(lines):
        match = _SECTION_HEADER_RE.match(str(raw_line or "").strip())
        if match:
            headers.append((match.group(1).strip(), index))

    ranges = []
    for position, (name, header_index) in enumerate(headers):
        body_start = header_index + 1
        body_end = headers[position + 1][1] if position + 1 < len(headers) else len(lines)
        ranges.append((name, header_index, body_start, body_end))
    return ranges


def _delete_sections(content, section_names):
    """删除 ``content`` 中段名属于 ``section_names`` 的**整个段**。

    返回 ``(new_content, deleted_section_names)``：

    - 段名比较**忽略大小写**、必须整段名精确相等（不做前缀 / 关键词匹配）；
    - 只删真实存在于文本里的段；不存在的段名不报错、不计入结果；
    - 删除时顺带吃掉该段尾部的纯空行，避免删除后留下多余双空行；
    - 一个段都没删时原样返回 ``content``（保证「无匹配不写文件」）。
    """
    text = str(content or "")
    if not text:
        return text, []

    wanted = set()
    for name in section_names or ():
        key = str(name or "").strip().casefold()
        if key:
            wanted.add(key)
    if not wanted:
        return text, []

    lines = text.splitlines(keepends=True)
    drop_indexes = set()
    deleted_names = []
    for name, header_index, body_start, body_end in _section_ranges(lines):
        if name.casefold() not in wanted:
            continue
        end = body_end
        while end > body_start and not lines[end - 1].strip():
            # 段体末尾的空行一并删掉：否则删段后会在段与段之间堆积双空行。
            end -= 1
        drop_indexes.update(range(header_index, end))
        deleted_names.append(name)

    if not deleted_names:
        return text, []

    kept = "".join(line for index, line in enumerate(lines) if index not in drop_indexes)
    if not kept.strip():
        return "", deleted_names
    if not kept.endswith(("\n", "\r")):
        kept += "\r\n" if "\r\n" in text else "\n"
    return kept, deleted_names


def _strip_skip_block(content):
    """剥掉本节点上一次追加的整块（含标记行），返回剩下的正文。

    本节点的块永远在文件末尾；块后面若还有别的「自动追加尾段」（文本追加 /
    UI 面板等），从它们自己的标记行开始原样保留，避免连带删掉别的节点的内容。
    块后只有空行（或什么也没有）时，全部视为本块的一部分。
    """
    text = str(content or "")
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"

    start_index = None
    end_index = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start_index is None:
            if stripped.startswith(SKIP_BLOCK_MARKER_PREFIX):
                start_index = index
            continue
        if stripped.startswith(_AUTO_APPENDED_OTHER_PREFIX) and not stripped.startswith(SKIP_BLOCK_MARKER_PREFIX):
            end_index = index
            break

    if start_index is None:
        return text
    if end_index is None:
        end_index = len(lines)

    body = "".join(lines[:start_index]).rstrip("\r\n")
    tail = "".join(lines[end_index:]).lstrip("\r\n")
    parts = [part for part in (body, tail) if part]
    if not parts:
        return ""
    return f"{newline}{newline}".join(parts) + newline


def _build_skip_block(entries, newline="\n"):
    """把跳过条目组装成 SKIP 块文本（没有有效条目时返回 ""）。

    ``entries`` 为 ``[(hash, index_count, first_index)]``；``index_count`` /
    ``first_index`` 为 ``None`` 的条目是「只 hash」回退段，仍用旧形式。
    """
    normalized = _normalize_skip_entries(entries)
    if not normalized:
        return ""

    lines = [SKIP_BLOCK_MARKER]
    for hash_val, index_count, first_index in normalized:
        if index_count is None or first_index is None:
            lines.append(f"; skip {hash_val}")
            lines.append(f"[TextureOverride_IB_{hash_val}]")
            lines.append(f"hash = {hash_val}")
            lines.append("handling = skip")
        else:
            lines.append(f"; skip {hash_val}_{index_count}_{first_index}")
            lines.append(f"[TextureOverride_IB_{hash_val}_{index_count}_{first_index}]")
            lines.append(f"hash = {hash_val}")
            lines.append(f"match_first_index = {first_index}")
            lines.append(f"match_index_count = {index_count}")
            lines.append("handling = skip")
        lines.append("")  # 段之间的空行；块末尾同样以换行结束
    return newline.join(lines)


def _append_skip_block(content, entries):
    """把跳过块追加到文本末尾（**不剥旧块**；没有条目时原样返回）。"""
    text = str(content or "")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = _build_skip_block(entries, newline=newline)
    if not block:
        return text

    body = text.rstrip("\r\n")
    if not body:
        return block
    return f"{body}{newline}{newline}{block}"


def _apply_skip_block(content, entries):
    """剥掉旧块后把新块追加到末尾（幂等）；没有条目时只剥不追加。"""
    text = str(content or "")
    stripped = _strip_skip_block(text)
    if not _normalize_skip_entries(entries):
        return stripped
    return _append_skip_block(stripped, entries)


def _get_section_registry():
    """软导入「导出段登记」；不可用时返回 ``None``（不拖垮节点注册 / 导出）。"""
    try:
        from ..common import export_section_registry
    except Exception:
        return None
    return export_section_registry


# --------------------------------------------------------------------------- #
# 节点数据
# --------------------------------------------------------------------------- #

class SSMT_SkipObjectItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore


class SSMT_UL_SKIP_OBJECTS(bpy.types.UIList):
    """已选跳过部件列表（行内 X 按钮按 index 删除对应项）。"""

    bl_idname = "SSMT_UL_SKIP_OBJECTS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=str(item.object_name or ""), icon='OBJECT_DATA')
        op_remove = row.operator("ssmt.skip_parts_remove", text="", icon='X')
        op_remove.node_name = getattr(data, "name", "")
        op_remove.index = index


class SSMTNode_PostProcess_Skip(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = NODE_LABEL
    bl_description = (
        "圈定选中部件所属 IB 的全部导出段并从 ini 删除，再追加 handling = skip 跳过段"
        "（跳过段已登记，可被后面的条件判断节点加条件）"
    )

    skip_objects: bpy.props.CollectionProperty(type=SSMT_SkipObjectItem)  # type: ignore
    active_skip_object: bpy.props.IntProperty(default=0)  # type: ignore

    def draw_buttons(self, context, layout):
        layout.label(text="删除该部件所属 IB 的全部导出段，并追加跳过段", icon='HIDE_ON')
        layout.label(text="与条件判断节点同一套 IB 圈定机制（不扫描 ini 关键词）", icon='INFO')

        box = layout.box()
        box.label(text=f"跳过部件 ({len(self.skip_objects)})", icon='OBJECT_DATA')
        if self.skip_objects:
            box.template_list(
                "SSMT_UL_SKIP_OBJECTS", "",
                self, "skip_objects",
                self, "active_skip_object",
                rows=max(2, min(len(self.skip_objects), 6)),
            )
        else:
            box.label(text="列表为空：导出时不会修改 ini", icon='ERROR')

        row = box.row(align=True)
        row.operator("ssmt.skip_parts_add_selected", text="添加选中物体", icon='ADD').node_name = self.name
        op_remove = row.operator("ssmt.skip_parts_remove", text="移除", icon='REMOVE')
        op_remove.node_name = self.name
        op_remove.index = int(self.active_skip_object)
        row.operator("ssmt.skip_parts_clear", text="清空", icon='TRASH').node_name = self.name

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def _selected_part_names(self):
        """收集节点上选定的部件名（去空、去重、保持顺序）。"""
        names = []
        seen = set()
        for item in self.skip_objects:
            name = str(getattr(item, "object_name", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

    def _collect_targets(self, selected_names, target_ini):
        """结构化圈定：返回 ``(model, draw_ibs, target_names)``。

        与条件判断节点共用 ``_collect_target_section_names``；延迟 import 避免
        模块加载期的循环依赖（condition 已经 import 了本模块的两个符号）。
        任何一步不可用时返回 ``(model_or_None, set(), set())``，由调用方打印原因。
        """
        model = None
        try:
            from .export_helper import BlueprintExportHelper

            get_blueprint_model = getattr(BlueprintExportHelper, "get_current_blueprint_model", None)
            model = get_blueprint_model() if callable(get_blueprint_model) else None
        except Exception as exc:
            print(f"跳过部件：读取本次导出的蓝图模型失败（{exc}），跳过")
            return None, set(), set()

        if model is None:
            return None, set(), set()

        try:
            from .node_postprocess_condition import _collect_target_section_names
        except Exception as exc:
            print(f"跳过部件：无法加载条件判断节点的圈定实现（{exc}），跳过")
            return model, set(), set()

        draw_ibs, target_names = _collect_target_section_names(model, selected_names, target_ini)
        return model, draw_ibs, target_names

    def execute_postprocess(self, mod_export_path):
        from .node_postprocess_comment_cleanup import SSMTNode_PostProcess_CommentCleanup

        target_ini, error_message = SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(mod_export_path)
        if not target_ini:
            print(f"跳过部件：{error_message or '未找到目标配置表'}")
            return False

        selected_names = self._selected_part_names()
        if not selected_names:
            print("跳过部件：节点未选择任何部件，跳过")
            return True

        # ---- 结构化圈定（与条件判断节点同一套机制） ----
        model, draw_ibs, target_names = self._collect_targets(selected_names, target_ini)
        if model is None:
            print("跳过部件：未获取到本次导出的蓝图模型（导出期段登记不可用），跳过")
            return True
        if not draw_ibs:
            print("跳过部件：选定的部件未匹配到任何 DrawIB（物体名与导出对象不一致），跳过")
            return True
        # 导出期段登记里没有这些 DrawIB 的段**不再早退**：这类 IB 是游戏原生绘制、
        # 不在本 mod 的接管范围里，没有段可删；用户既然把它加进跳过列表，就应当
        # 直接追加完整跳过段（hash + match_first_index + match_index_count +
        # handling = skip）让原生绘制被跳过（删除步骤自然 0 段）。有导出段的 IB
        # 仍走「先删段、再追加」。

        try:
            with open(target_ini, "rb") as handle:
                raw_bytes = handle.read()
        except OSError as exc:
            print(f"跳过部件：读取配置表失败 {target_ini}: {exc}")
            return False

        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        try:
            content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            print(f"跳过部件：配置表不是 UTF-8，跳过 {target_ini}: {exc}")
            return False

        # ---- 删除 + 追加 ----
        # 追加条目来自**选中物体名的前缀三段**（hash / indexCount / firstIndex）：
        # 只写 hash 的跳过段在部分游戏里不生效，必须带 match_first_index /
        # match_index_count。解析不出三段的名字回退为只 hash 条目；全都解析不出时
        # 再退回 draw_ib 的只 hash 条目（避免整节点变成空操作）。
        entries = _entries_from_names(selected_names)
        if not entries:
            entries = _hash_only_entries(draw_ibs)
            if entries:
                print("跳过部件：物体名里没有可完整解析的 <hash>-<indexCount>-<firstIndex>，回退为只 hash 的跳过段")
        entry_list = ", ".join(_format_skip_entry(entry) for entry in entries)

        # 先剥掉本节点上一次追加的块：否则第二次执行会把旧跳过段当成目标段重复删 / 计数。
        stripped_content = _strip_skip_block(content)
        updated, deleted_names = _delete_sections(stripped_content, target_names)
        updated = _append_skip_block(updated, entries)

        written = updated != content
        if written:
            self._create_cumulative_backup(target_ini, mod_export_path)
            try:
                with open(
                    target_ini,
                    "w",
                    encoding="utf-8-sig" if has_bom else "utf-8",
                    newline="",
                ) as handle:
                    handle.write(updated)
            except (OSError, UnicodeError) as exc:
                print(f"跳过部件：写入配置表失败 {target_ini}: {exc}")
                return False

        # ---- 登记新段：让链上后面的条件判断节点能圈到跳过段并给它加条件 ----
        registry = _get_section_registry()
        if registry is not None:
            for entry in entries:
                hash_val = entry[0]
                try:
                    registry.register_section(
                        _skip_section_name(entry),
                        section_type="TextureOverrideIB",
                        draw_ib=hash_val,
                        ini_path=target_ini,
                    )
                except Exception as exc:
                    print(f"跳过部件：登记导出段失败（{exc}）")

        # ---- 日志：区分「有导出段（删段+追加）」与「无导出段（只追加）」 ----
        ib_list = ", ".join(sorted(str(value) for value in draw_ibs))
        suffix = "" if written else "（配置表已是最新，未改写）"
        if deleted_names:
            message = (
                f"跳过部件：选中 {len(selected_names)} 个部件、圈定 IB {len(draw_ibs)} 个"
                f"（{ib_list}）；"
                f"删除段 {len(deleted_names)} 个（{'、'.join(deleted_names)}）；"
                f"新增跳过段 {len(entries)} 个（{entry_list}）{suffix} -> {target_ini}"
            )
        else:
            message = (
                f"跳过部件：选中 {len(selected_names)} 个部件、圈定 IB {len(draw_ibs)} 个"
                f"（{ib_list}）；"
                f"这些 IB 不在本次导出中（无段可删），只追加跳过段 {len(entries)} 个"
                f"（{entry_list}）{suffix} -> {target_ini}"
            )
        print(message)
        try:
            from . import export_progress
            if deleted_names:
                export_progress.report_stage(
                    f"跳过部件：{len(draw_ibs)} 个IB / 删除 {len(deleted_names)} 段 / "
                    f"新增 {len(entries)} 段（{entry_list}）/ {len(selected_names)} 个部件"
                )
            else:
                export_progress.report_stage(
                    f"跳过部件：{len(draw_ibs)} 个IB / 无导出段可删 / "
                    f"新增 {len(entries)} 段（{entry_list}）/ {len(selected_names)} 个部件"
                )
        except Exception:
            pass
        return True


# --------------------------------------------------------------------------- #
# 算子：添加 / 移除 / 清空
# --------------------------------------------------------------------------- #

def _resolve_skip_node(context, node_name):
    space_data = getattr(context, "space_data", None)
    tree = None
    if space_data is not None:
        tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
    node = tree.nodes.get(node_name) if tree else None
    if node is None or getattr(node, "bl_idname", "") != NODE_IDNAME:
        return None
    return node


class SSMT_OT_PostProcess_AddSkipObject(bpy.types.Operator):
    bl_idname = "ssmt.skip_parts_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的网格物体加入跳过部件列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        objects = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == 'MESH']
        if not objects and getattr(context, "active_object", None) is not None:
            active = context.active_object
            if getattr(active, "type", "") == 'MESH':
                objects = [active]
        if not objects:
            self.report({'WARNING'}, "请先选中要添加的网格物体")
            return {'CANCELLED'}

        existing = {str(item.object_name or "") for item in node.skip_objects}
        added_count = 0
        for obj in objects:
            name = str(getattr(obj, "name", "") or "").strip()
            if not name or name in existing:
                continue
            item = node.skip_objects.add()
            item.object_name = name
            existing.add(name)
            added_count += 1

        self.report({'INFO'}, f"已添加 {added_count} 个部件")
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveSkipObject(bpy.types.Operator):
    bl_idname = "ssmt.skip_parts_remove"
    bl_label = "移除部件"
    bl_description = "从跳过部件列表中移除指定项"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        if 0 <= self.index < len(node.skip_objects):
            node.skip_objects.remove(self.index)
            node.active_skip_object = max(0, min(int(node.active_skip_object), len(node.skip_objects) - 1))
            return {'FINISHED'}

        self.report({'WARNING'}, "列表中没有这一项")
        return {'CANCELLED'}


class SSMT_OT_PostProcess_ClearSkipObjects(bpy.types.Operator):
    bl_idname = "ssmt.skip_parts_clear"
    bl_label = "清空"
    bl_description = "清空跳过部件列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        while len(node.skip_objects) > 0:
            node.skip_objects.remove(len(node.skip_objects) - 1)
        node.active_skip_object = 0
        self.report({'INFO'}, "已清空跳过部件列表")
        return {'FINISHED'}


classes = (
    SSMT_SkipObjectItem,
    SSMT_UL_SKIP_OBJECTS,
    SSMTNode_PostProcess_Skip,
    SSMT_OT_PostProcess_AddSkipObject,
    SSMT_OT_PostProcess_RemoveSkipObject,
    SSMT_OT_PostProcess_ClearSkipObjects,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
