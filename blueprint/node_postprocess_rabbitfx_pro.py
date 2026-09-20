"""RabbitFX 贴图后处理 Pro —— 可串联的 FX 参数注入节点。

**本节点不生成任何贴图配置**：发光贴图（``Glowmap_<亮度>_*``）与 FX 贴图
（``FXMap_*``）的材质判定、资源复制、``Resource\\RabbitFX\\Glowmap`` /
``Resource\\RabbitFX\\FXMap`` 绑定以及 ``run = CommandList\\RabbitFX\\Run``
全部由「材质转资源(pro)」完成。本节点只往这些已有绑定上**追加参数**，面板也
按这一点分成两块：

* **发光贴图参数**：``$\\RabbitFX\\H/S/V``、``brightness``、``interpolate``
  的静态值；它下面挂着子项**呼吸灯**，勾选后把这些变量交给三角波动态变换；
* **FX 贴图参数**：子项 **W-Engine 同步**（``$\\rabbitfx\\blendmode`` /
  ``SetFXBuffer`` / 提取源的 ``ERun``）与 **颜色偏移**
  （``run = CommandList\\RabbitFX\\ColorShift``，用 FX 贴图的 R 通道做遮罩）。

与旧的「RabbitFX贴图后处理」(:mod:`node_postprocess_rabbitfx`) 的差别：

* **不生成贴图资源**，因此没有「镂空 FXMap」这类开关——镂空由材质转资源生成，
  本节点管不了也不需要管；
* **可以多个串联**：同一条后处理链上允许存在任意多个本节点，每个节点自带
  物体列表与参数，只会改自己列表里物体所在的 mesh 块；
* **绘制后复位**：按本节点真正写过的参数生成复位行。RabbitFX 的 ``Run`` /
  ``ERun`` / ``ColorShift`` 命令列表末尾本身也会把
  ``$h/$s/$v/$brightness/$interpolate/$BLENDMODE`` 与资源别名清空
  （RabbitFX.ini ``[CommandListRun]`` 结尾），这里再显式写一遍作为双保险，
  同时覆盖「只绑定、不 Run」的异常路径。

INI 语法全部对照官方 mod 说明与随附 demo（v7.8）：
https://gamebanana.com/mods/531649
"""

import bpy
import glob
import json
import os
import re
import uuid
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_rabbitfx import (
    _extract_hash_from_object,
    _extract_resource_suffix,
)

NODE_IDNAME = 'SSMTNode_PostProcess_RabbitFXPro'
MATERIAL_NODE_IDNAME = 'SSMTNode_PostProcess_CustomMaterialAssign'
LEGACY_MATERIAL_NODE_IDNAME = 'SSMTNode_PostProcess_Material'

FX_NAMESPACE = "RabbitFX"
GLOW_REF = r"Resource\RabbitFX\Glowmap"
FXMAP_REF = r"Resource\RabbitFX\FXMap"
SET_FX_BUFFER_REF = r"Resource\RabbitFX\SetFXBuffer"
RUN_LINE = r"run = CommandList\RabbitFX\Run"
COLOR_SHIFT_LINE = r"run = CommandList\RabbitFX\ColorShift"
E_RUN_LINE = r"run = CommandList\RabbitFX\ERun"
UPDATE_BUFFER_LINE = r"pre run = CommandList\RabbitFX\UpdateFXBuffer"

# 本节点写入块的起止标记。清理时按标记成对删除，保证重复导出幂等。
# 标记必须保持纯 ASCII：配置文件清理节点会把 INI 里的非 ASCII 文本改写成
# 确定性字母串，中文标记被改写后就再也匹配不上，重跑会累积重复块。
_PARAM_BEGIN = "; === RabbitFXPro Params ==="
_PARAM_END = "; === End RabbitFXPro Params ==="
_BREATH_BEGIN = "; === RabbitFXPro Breath ==="
_BREATH_END = "; === End RabbitFXPro Breath ==="
_CS_BEGIN = "; === RabbitFXPro ColorShift ==="
_CS_END = "; === End RabbitFXPro ColorShift ==="
_SYNC_BEGIN = "; === RabbitFXPro SyncSource ==="
_SYNC_END = "; === End RabbitFXPro SyncSource ==="
_RESET_BEGIN = "; === RabbitFXPro Reset ==="
_RESET_END = "; === End RabbitFXPro Reset ==="

# 兼容早期中文标记（结束 / End 两种写法都认）。
_MARKER_RE = re.compile(
    r"^;\s*===\s*(?:(?P<end>结束|End)\s+)?RabbitFXPro\b", re.IGNORECASE
)

# 呼吸灯计数变量统一前缀，清理上一轮生成时按前缀整块删除。
_VAR_PREFIX = "$rfxpro_"
_VAR_IF_RE = re.compile(r"^if\s+\$rfxpro_", re.IGNORECASE)

_MESH_LINE_RE = re.compile(r"^\s*\[mesh:(?P<name>.+?)\]\s*$", re.IGNORECASE)
_DRAW_LINE_RE = re.compile(r"^(drawindexed|draw)\s*=", re.IGNORECASE)
_RUN_LINE_RE = re.compile(r"^run\s*=\s*commandlist\\rabbitfx\\run$", re.IGNORECASE)
_IF_LINE_RE = re.compile(r"^if\s+", re.IGNORECASE)
_GLOW_REF_RE = re.compile(
    r"^Resource\\RabbitFX\\Glowmap\s*=\s*(?:ref\s+)?(?P<name>.+?)\s*$", re.IGNORECASE
)
_FXMAP_REF_RE = re.compile(
    r"^Resource\\RabbitFX\\FXMap\s*=\s*(?:ref\s+)?(?P<name>.+?)\s*$", re.IGNORECASE
)


def _mesh_object_poll(_self, obj):
    return bool(getattr(obj, "type", "") == "MESH")


_NTEMIFX_REF_RE = re.compile(
    r"^Resource\\NTEMIFX\\(?:Glowmap|FXMap)\s*=", re.IGNORECASE
)


def _has_ntemifx_bindings(sections):
    """配置表里是否存在 NTEMIFX 命名空间的发光/裁切绑定（NTEMI 逻辑）。"""
    for section_lines in sections.values():
        for line in section_lines:
            if _NTEMIFX_REF_RE.match(str(line).strip()):
                return True
    return False


def _find_material_node_in_tree(node):
    """同一蓝图里的「材质转资源(pro)」节点（本节点的贴图绑定来源）。"""
    tree = getattr(node, "id_data", None)
    for candidate in getattr(tree, "nodes", []) or []:
        if getattr(candidate, "bl_idname", "") in (
            MATERIAL_NODE_IDNAME,
            LEGACY_MATERIAL_NODE_IDNAME,
        ):
            return candidate
    return None


def _extract_mesh_name(line):
    match = _MESH_LINE_RE.match(str(line or ""))
    return match.group("name").strip() if match else ""


def _normalize_suffix(suffix):
    return str(suffix or "").strip().replace("-", "_").lower()


def _mesh_name_matches(mesh_name, hash_val, suffix_pattern):
    """mesh 注释里的物体名是否属于目标物体（哈希相同 + 标识互相包含）。"""
    if not mesh_name:
        return False
    mesh_hash = _extract_hash_from_object(mesh_name).lower()
    if not mesh_hash or mesh_hash != str(hash_val or "").lower():
        return False
    if not suffix_pattern:
        return True
    mesh_suffix = _normalize_suffix(_extract_resource_suffix(mesh_name))
    if not mesh_suffix:
        return False
    return suffix_pattern in mesh_suffix or mesh_suffix in suffix_pattern


def _iter_mesh_blocks(lines):
    """按 ``[mesh:...]`` 切块，产出 (块首, 块尾) —— 块尾不含。"""
    starts = [i for i, line in enumerate(lines) if _extract_mesh_name(line)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        yield start, end


def _marker_kind(line):
    """``start`` / ``end`` / 空串 —— 识别本节点写入块的起止标记。"""
    match = _MARKER_RE.match(str(line or "").strip())
    if match is None:
        return ""
    return "end" if match.group("end") else "start"


def _strip_previous_blocks(lines):
    """删除本节点上一轮写入的标记块与呼吸灯变量块（重跑幂等）。"""
    kept = []
    index = 0
    total = len(lines)
    while index < total:
        stripped = str(lines[index]).strip()
        kind = _marker_kind(stripped)
        if kind == "start":
            # 只有配对成功才删除：孤立标记（截断文件）原样保留，避免吞掉后续内容。
            end_index = next(
                (
                    offset for offset in range(index + 1, total)
                    if _marker_kind(str(lines[offset]).strip()) == "end"
                ),
                None,
            )
            if end_index is None:
                kept.append(lines[index])
                index += 1
                continue
            index = end_index + 1
            continue
        if kind == "end":
            index += 1
            continue
        if stripped.startswith(_VAR_PREFIX):
            index += 1
            continue
        if _VAR_IF_RE.match(stripped):
            depth = 0
            while index < total:
                current = str(lines[index]).strip()
                if _IF_LINE_RE.match(current):
                    depth += 1
                elif current.casefold() == "endif":
                    depth -= 1
                    if depth <= 0:
                        index += 1
                        break
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return kept


def _find_draw_index(lines, start, end):
    """绘制行索引；没有 drawindexed 时退回最后一条 ``run = `` 行。

    着色器替换 / 拖拽交互 / EFMI 合并骨骼会把绘制搬进 ``run =`` 引用的段，
    此时段内只剩 ``run =``，与材质转资源的复位插入口径保持一致。
    """
    for index in range(start, end):
        if _DRAW_LINE_RE.match(str(lines[index]).strip()):
            return index
    fallback = -1
    for index in range(start, end):
        stripped = str(lines[index]).strip().casefold()
        if stripped.startswith("run =") or stripped.startswith("run="):
            fallback = index
    return fallback


def _find_draw_block_end(lines, start, end):
    """绘制语句（含包裹它的 if/endif）之后的插入位置。

    与材质转资源的复位插入口径一致：取绘制行之后最后一个 endif 的下一位，
    没有 endif 时取绘制行的下一位。
    """
    draw_index = _find_draw_index(lines, start, end)
    if draw_index < 0:
        return end
    depth = 0
    last_endif = -1
    for index in range(start, end):
        stripped = str(lines[index]).strip()
        if _IF_LINE_RE.match(stripped):
            depth += 1
        elif stripped.casefold() == "endif":
            if depth > 0:
                depth -= 1
            if index > draw_index:
                last_endif = index
    if last_endif > draw_index:
        return last_endif + 1
    return draw_index + 1


def _collect_existing_refs(lines, start, end):
    glow_ref = ""
    fxmap_ref = ""
    run_index = -1
    for index in range(start, end):
        stripped = str(lines[index]).strip()
        if run_index < 0 and _RUN_LINE_RE.match(stripped):
            run_index = index
        if not glow_ref:
            match = _GLOW_REF_RE.match(stripped)
            if match and match.group("name").strip().casefold() != "null":
                glow_ref = match.group("name").strip()
        if not fxmap_ref:
            match = _FXMAP_REF_RE.match(stripped)
            if match and match.group("name").strip().casefold() != "null":
                fxmap_ref = match.group("name").strip()
    return glow_ref, fxmap_ref, run_index


def _format_number(value):
    """整数写整数、浮点保留有效位，用于呼吸灯里的算式。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number == int(number):
        return str(int(number))
    return repr(round(number, 6))


def _format_float(value):
    """始终带小数点的写法，与官方示例（``brightness = 8.0``）保持一致。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    text = repr(round(number, 6))
    if "." not in text and "e" not in text.lower():
        text += ".0"
    return text


def _insert_block(lines, position, block_lines):
    for offset, line in enumerate(block_lines):
        lines.insert(position + offset, line)


# ────────────────────────── 材质侧探测 ──────────────────────────

class _MaterialProbe:
    """复用材质转资源的材质前缀判定，但不继承它的 Node 类。"""

    @staticmethod
    def _load():
        from .node_postprocess_material import SSMTNode_PostProcess_MaterialBase as base

        return base

    def find_matching_materials(self, obj, texture_type):
        return self._load().find_matching_materials(self, obj, texture_type)

    def _build_material_signature(self, material):
        return self._load()._build_material_signature(material)


_PROBE = _MaterialProbe()


def _probe_marked_slots(obj):
    """导入侧记录的槽位标记（modimp_texture_slots）里是否有 Glowmap / FXMap。"""
    raw = str(obj.get("modimp_texture_slots", "") or "").strip()
    if not raw:
        return False, False
    try:
        slots = json.loads(raw)
    except Exception:
        return False, False
    if not isinstance(slots, dict):
        return False, False
    has_glow = False
    has_fx = False
    for binding in slots.values():
        if not isinstance(binding, dict):
            continue
        mark_name = str(binding.get("mark_name", "") or "").strip().lower()
        if mark_name == "glowmap":
            has_glow = True
        elif mark_name == "fxmap":
            has_fx = True
    return has_glow, has_fx


def _probe_fx_textures(obj):
    """判断物体是否存在发光贴图 / FX 贴图（材质前缀 + 槽位标记）。"""
    has_glow = bool(_PROBE.find_matching_materials(obj, "Glowmap"))
    has_fx = bool(_PROBE.find_matching_materials(obj, "FXMap"))
    if not (has_glow and has_fx):
        slot_glow, slot_fx = _probe_marked_slots(obj)
        has_glow = has_glow or slot_glow
        has_fx = has_fx or slot_fx
    return has_glow, has_fx


class RabbitFXProTargetItem(bpy.types.PropertyGroup):
    """一个受本节点控制的物体（FX 贴图来自材质转资源）。"""

    target_object: bpy.props.PointerProperty(
        name="目标物体",
        description="注入 RabbitFX 参数的物体；其发光/裁切贴图由「材质转资源(pro)」提供",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    has_glow: bpy.props.BoolProperty(
        name="有发光贴图",
        description="扫描结果：该物体的材质里存在 Glowmap 前缀贴图（发光参数会被注入）",
        default=False,
    )
    has_fx: bpy.props.BoolProperty(
        name="有 FX 贴图",
        description="扫描结果：该物体的材质里存在 FXMap 前缀贴图（同步/颜色偏移会用它的绑定）",
        default=False,
    )


class SSMTNode_PostProcess_RabbitFXPro(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = 'RabbitFX贴图后处理pro'
    bl_description = (
        '复用材质转资源写好的 RabbitFX 发光/裁切贴图，注入静态发光(HSV/亮度/插值)、'
        '呼吸灯、W-Engine 同步与颜色偏移参数；可多个串联，每个节点只改自己列表里的物体'
    )

    # ── 目标物体 ──
    target_items: bpy.props.CollectionProperty(type=RabbitFXProTargetItem)
    active_target_index: bpy.props.IntProperty(name="当前目标", default=0, min=-1)

    # ── 静态发光 ──
    enable_glow: bpy.props.BoolProperty(
        name="静态发光", description="写入 $\\RabbitFX\\H/S/V/brightness/interpolate", default=True
    )
    glow_h: bpy.props.FloatProperty(name="H 色相", default=0.0, min=-360, max=360)
    glow_s: bpy.props.FloatProperty(name="S 饱和度", default=0.0, min=-100, max=100)
    glow_v: bpy.props.FloatProperty(name="V 明度", default=0.0, min=-100, max=100)
    glow_brightness: bpy.props.FloatProperty(
        name="发光强度/光晕范围",
        description="$\\RabbitFX\\brightness，越大光晕越强",
        default=8.0,
        min=0,
        max=500,
    )
    glow_interpolate: bpy.props.FloatProperty(
        name="插值",
        description="$\\RabbitFX\\interpolate，0=原色 1=完全偏移",
        default=1.0,
        min=0,
        max=1,
    )

    # ── 呼吸灯 ──
    enable_breath: bpy.props.BoolProperty(
        name="启用呼吸灯",
        description="让发光贴图参数的 H/S/V/亮度/插值随时间变化",
        default=False,
    )
    breath_mode: bpy.props.EnumProperty(
        name="呼吸模式",
        items=[
            ('SINGLE', "单色呼吸", "固定颜色，发光强度周期性脉动"),
            ('RAINBOW', "多彩渐变", "Hue 全色环循环，发光强度固定"),
            ('COMBO', "渐变式呼吸", "Hue 全色环循环 + 发光强度同步脉动"),
        ],
        default='SINGLE',
    )
    breath_fps: bpy.props.IntProperty(
        name="周期(帧)", description="三角波一个来回的帧数", default=200, min=10, max=7200
    )
    breath_step: bpy.props.FloatProperty(
        name="速度", description="每帧给计数器加多少", default=0.3, min=0.01, max=100
    )
    breath_h: bpy.props.FloatProperty(name="H 色相", default=0.0, min=0, max=360)
    breath_s: bpy.props.FloatProperty(name="S 饱和度", default=0.0, min=0, max=100)
    breath_v: bpy.props.FloatProperty(name="V 明度", default=0.0, min=0, max=100)
    breath_brightness_min: bpy.props.FloatProperty(name="最小强度", default=1.0, min=0, max=500)
    breath_brightness_max: bpy.props.FloatProperty(name="最大强度", default=8.0, min=0, max=500)
    breath_interpolate: bpy.props.FloatProperty(name="插值", default=1.0, min=0, max=1)

    # ── FX 贴图参数 ──
    enable_sync: bpy.props.BoolProperty(
        name="W-Engine 同步", description="让发光颜色/亮度跟随 W-Engine 发光", default=False
    )
    sync_blendmode: bpy.props.IntProperty(
        name="混合模式",
        description="$\\rabbitfx\\blendmode；1 = 与引擎发光做比较（只显示更亮的部位）",
        default=1,
        min=0,
        max=1,
    )
    sync_brightness_only: bpy.props.BoolProperty(
        name="仅同步亮度", description="$\\rabbitfx\\syncbrightnessonly = 1", default=False
    )
    sync_source_object: bpy.props.PointerProperty(
        name="提取源物体",
        description="带 W-Engine 发光的部件（通常是头发），在其绘制前运行 ERun 提取引擎发光",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    buffer_mode: bpy.props.EnumProperty(
        name="缓冲写法",
        items=[
            ('COPY', "copy（官方推荐）", "Resource…FXBuffer = copy Resource\\RabbitFX\\FXBuffer，配合 SetFXBuffer"),
            ('RWBUFFER', "自带 RWBuffer（旧版）", "声明 RWBuffer 并用 ps-u4 直绑，兼容 4.5 以前的 RabbitFX"),
        ],
        default='COPY',
    )

    # ── 颜色偏移 ──
    enable_colorshift: bpy.props.BoolProperty(
        name="颜色偏移 ColorShift",
        description="用 FXMap 的 R 通道作遮罩偏移角色贴图颜色",
        default=False,
    )
    cs_h: bpy.props.FloatProperty(name="H 色相偏移", default=0.0, min=-360, max=360)
    cs_s: bpy.props.FloatProperty(name="S 饱和度偏移", default=0.0, min=-100, max=100)
    cs_v: bpy.props.FloatProperty(name="V 明度偏移", default=0.0, min=-100, max=100)

    # ── 复位 ──
    enable_reset: bpy.props.BoolProperty(
        name="绘制后复位",
        description="每个被修改的 mesh 绘制完成后清空资源别名与变量（RabbitFX 命令列表本身也会复位）",
        default=True,
    )

    # ── 内部 ──
    buffer_tag: bpy.props.StringProperty(
        name="缓冲标识",
        description="内部使用：W-Engine 缓冲资源名与呼吸灯变量的唯一后缀",
        default="",
        options={"HIDDEN"},
    )

    # ── UI 折叠 ──
    show_targets: bpy.props.BoolProperty(default=True)
    show_glow: bpy.props.BoolProperty(default=True)
    show_breath: bpy.props.BoolProperty(default=False)
    show_fx: bpy.props.BoolProperty(default=False)
    show_sync: bpy.props.BoolProperty(default=False)
    show_colorshift: bpy.props.BoolProperty(default=False)

    def _breathing(self):
        """呼吸灯是否生效（需勾选呼吸灯；发光贴图参数关掉时不会执行 Run）。"""
        return bool(self.enable_breath)

    def init(self, context):
        super().init(context)
        if not self.buffer_tag:
            self.buffer_tag = uuid.uuid4().hex[:8]

    # ──────────────────────────── UI ────────────────────────────

    def draw_buttons(self, context, layout):
        self._draw_targets(layout)
        self._draw_glow_block(layout)   # 内部再画子项呼吸灯
        self._draw_fx_block(layout)     # 内部再画子项同步/颜色偏移

        layout.separator()
        layout.prop(self, "enable_reset")

    def _draw_glow_block(self, layout):
        """发光贴图参数（一级折叠）→ 组内第一行是「标题 + 启用勾」（二级）。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_glow", text="",
            icon='TRIA_DOWN' if self.show_glow else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="发光贴图参数", icon='LIGHT')

        if not self.show_glow:
            return

        box = layout.box()
        row = box.row(align=True)
        row.label(text="发光贴图参数", icon='LIGHT')
        row.prop(self, "enable_glow", text="")

        breathing = self._breathing()
        static = box.column(align=True)
        static.enabled = self.enable_glow and not breathing
        r = static.row(align=True)
        r.prop(self, "glow_h")
        r.prop(self, "glow_s")
        r.prop(self, "glow_v")
        static.prop(self, "glow_brightness")
        static.prop(self, "glow_interpolate", slider=True)
        if breathing:
            static.label(text="呼吸灯启用中，静态值被覆盖", icon='INFO')

        # 呼吸灯是发光贴图参数的子项，跟着父级一起展开/收起。
        self._draw_breath_block(box)

    def _draw_breath_block(self, layout):
        """呼吸灯（发光贴图参数的子级）：让上面的变量随时间变化。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_breath", text="",
            icon='TRIA_DOWN' if self.show_breath else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="呼吸灯", icon='ANIM')
        row.prop(self, "enable_breath", text="")

        if not self.show_breath:
            return

        col = layout.column(align=True)
        col.enabled = bool(self.enable_breath)
        col.prop(self, "breath_mode", text="模式")
        col.prop(self, "breath_fps")
        col.prop(self, "breath_step")
        if self.breath_mode == 'SINGLE':
            r = col.row(align=True)
            r.prop(self, "breath_h")
            r.prop(self, "breath_s")
            r.prop(self, "breath_v")
            col.prop(self, "breath_brightness_min")
            col.prop(self, "breath_brightness_max")
        elif self.breath_mode == 'RAINBOW':
            r = col.row(align=True)
            r.prop(self, "breath_s")
            r.prop(self, "breath_v")
            col.prop(self, "breath_brightness_max", text="发光强度")
        else:
            r = col.row(align=True)
            r.prop(self, "breath_s")
            r.prop(self, "breath_v")
            col.prop(self, "breath_brightness_min")
            col.prop(self, "breath_brightness_max")
        col.prop(self, "breath_interpolate", slider=True)

        if self._breathing() and not self.enable_glow:
            layout.label(text="需开启「发光贴图参数」才会执行 Run", icon='ERROR')

    def _draw_fx_block(self, layout):
        """FX 贴图参数（父级）：FX 贴图本身由材质转资源生成，这里只加同步与偏移。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_fx", text="",
            icon='TRIA_DOWN' if self.show_fx else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="FX 贴图参数", icon='TEXTURE')

        if not self.show_fx:
            return

        box = layout.box()
        self._draw_sync_block(box)
        box.separator()
        self._draw_colorshift_block(box)

    def _draw_sync_block(self, layout):
        """W-Engine 同步（FX 贴图参数的子级）。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_sync", text="",
            icon='TRIA_DOWN' if self.show_sync else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="W-Engine 同步", icon='DRIVER')
        row.prop(self, "enable_sync", text="")

        if not self.show_sync:
            return

        col = layout.column(align=True)
        col.enabled = bool(self.enable_sync)
        col.prop(self, "sync_blendmode")
        col.prop(self, "sync_brightness_only")
        col.prop(self, "buffer_mode", text="缓冲写法")
        col.prop(self, "sync_source_object", text="提取源")
        if self.enable_sync:
            layout.label(text="提取源 = 带引擎发光的部件（通常是头发）", icon='INFO')

    def _draw_colorshift_block(self, layout):
        """颜色偏移（FX 贴图参数的子级）。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_colorshift", text="",
            icon='TRIA_DOWN' if self.show_colorshift else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="颜色偏移 ColorShift", icon='COLOR')
        row.prop(self, "enable_colorshift", text="")

        if not self.show_colorshift:
            return

        col = layout.column(align=True)
        col.enabled = bool(self.enable_colorshift)
        r = col.row(align=True)
        r.prop(self, "cs_h")
        r.prop(self, "cs_s")
        r.prop(self, "cs_v")
        if self.enable_colorshift:
            layout.label(text="FX 贴图 R 通道作为偏移遮罩", icon='INFO')

    def _draw_targets(self, layout):
        row = layout.row(align=True)
        row.prop(
            self,
            "show_targets",
            text="",
            icon='TRIA_DOWN' if self.show_targets else 'TRIA_RIGHT',
            emboss=False,
        )
        row.label(text=f"目标物体列表 ({len(self.target_items)})", icon='OUTLINER_OB_GROUP_INSTANCE')
        row.operator("ssmt.rabbitfx_pro_scan", text="", icon='VIEWZOOM').node_name = self.name
        row.operator("ssmt.rabbitfx_pro_add", text="", icon='ADD')
        row.operator("ssmt.rabbitfx_pro_remove", text="", icon='REMOVE')

        if not self.show_targets:
            return

        layout.operator("ssmt.rabbitfx_pro_add_selected", icon='RESTRICT_SELECT_OFF')

        if _find_material_node_in_tree(self) is None:
            layout.label(
                text="本蓝图里没有「材质转资源」节点：发光/FX 贴图绑定由它生成",
                icon='ERROR',
            )

        if not self.target_items:
            layout.label(text="点扫描按钮从材质转资源收集 FX 物体", icon='INFO')
            return

        col = layout.column(align=True)
        for index, item in enumerate(self.target_items):
            box = col.box()
            header = box.row(align=True)
            icon = 'TRIA_DOWN' if index == self.active_target_index else 'TRIA_RIGHT'
            op = header.operator("ssmt.rabbitfx_pro_select", text=f"目标 {index + 1}", icon=icon)
            op.index = index
            obj = item.target_object
            if obj is None:
                header.label(text="未选择", icon='ERROR')
                continue
            header.label(text=obj.name, icon='OBJECT_DATA')
            if item.has_glow:
                header.label(text="", icon='LIGHT')
            if item.has_fx:
                header.label(text="", icon='TEXTURE')

            if index == self.active_target_index:
                box.prop(item, "target_object", text="物体")
                box.label(
                    text=(
                        f"哈希 {_extract_hash_from_object(obj.name) or '—'}"
                        f"   标识 {_extract_resource_suffix(obj.name) or '—'}"
                    ),
                    icon='INFO',
                )
                duplicate = _objects_used_by_other_nodes(self, obj)
                if duplicate:
                    box.label(text=f"同一物体也被 {duplicate} 接管", icon='ERROR')

    # ──────────────────────── 生成入口 ────────────────────────

    def execute_postprocess(self, mod_export_path):
        if not self.buffer_tag:
            self.buffer_tag = uuid.uuid4().hex[:8]

        targets = [item for item in self.target_items if item.target_object is not None]
        sync_source = self.sync_source_object if self.enable_sync else None
        if self.enable_sync and sync_source is None:
            print(
                "[RabbitFXPro] 警告: 已开启 W-Engine 同步但未指定「提取源物体」，"
                "引擎发光色不会被提取"
            )
        if not targets and sync_source is None:
            print("[RabbitFXPro] 错误: 未指定任何目标物体")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            return

        for ini_file in ini_files:
            self._create_cumulative_backup(ini_file, mod_export_path)
            self._process_ini_file(ini_file, targets, sync_source)

    @staticmethod
    def _parse_ini(content):
        """切段。``[mesh:...]`` 是段内的 mesh 块标记，不能当成新段。"""
        sections = OrderedDict()
        preamble = []
        current = None
        for line in str(content or "").splitlines():
            stripped = line.strip()
            is_header = (
                stripped.startswith('[')
                and stripped.endswith(']')
                and not _MESH_LINE_RE.match(stripped)
            )
            if is_header:
                current = stripped
                sections[current] = []
            elif current is not None:
                sections[current].append(line.rstrip())
            else:
                preamble.append(line.rstrip())
        # 去掉段首尾的空行，保证重复导出不会一次多出一个空行。
        for section_lines in sections.values():
            while section_lines and not section_lines[0].strip():
                section_lines.pop(0)
            while section_lines and not section_lines[-1].strip():
                section_lines.pop()
        return sections, preamble

    def _process_ini_file(self, ini_file, targets, sync_source):
        with open(ini_file, 'r', encoding='utf-8') as handle:
            content = handle.read()

        preserved_driver, content = self.split_anim_driver_block_content(content)
        content, tail = self.split_auto_appended_tail_content(content)
        sections, preamble = self._parse_ini(content)

        buffer_name = self.buffer_resource_name()
        changed = False
        for item in targets:
            changed |= self._process_target(sections, item, buffer_name)
        if sync_source is not None:
            changed |= self._process_sync_source(sections, sync_source, buffer_name)
        if self.enable_sync and changed:
            changed |= self._ensure_buffer_resource(sections, buffer_name)
        if changed and self.enable_glow and self._breathing():
            changed |= self._ensure_breath_constants(sections)

        if not changed:
            return

        with open(ini_file, 'w', encoding='utf-8') as handle:
            if preserved_driver:
                handle.write(preserved_driver)
                if not preserved_driver.endswith('\n'):
                    handle.write('\n')
                handle.write('\n')
            for line in preamble:
                handle.write(line + '\n')
            for section_name, section_lines in sections.items():
                if section_name.startswith('['):
                    handle.write(f"{section_name}\n")
                for line in section_lines:
                    handle.write(f"{line}\n")
                handle.write("\n")
            if tail:
                handle.write(tail)
        print(f"[RabbitFXPro] 已写入 {os.path.basename(ini_file)}")

    # ──────────────────── 目标物体：参数注入 ────────────────────

    def _process_target(self, sections, item, buffer_name):
        obj = item.target_object
        hash_val = _extract_hash_from_object(obj.name).lower()
        suffix_pattern = _normalize_suffix(_extract_resource_suffix(obj.name))
        if not hash_val:
            print(f"[RabbitFXPro] 跳过 {obj.name}: 无法解析哈希")
            return False

        changed = False
        # 从后往前处理，避免插入行导致后续块的索引位移。
        for section_name, blocks in reversed(self._collect_target_blocks(sections, hash_val, suffix_pattern)):
            section_lines = sections.get(section_name)
            if section_lines is None:
                continue
            for start, end in reversed(blocks):
                changed |= self._patch_mesh_block(section_lines, start, end, obj.name, buffer_name)
        if not changed:
            print(
                f"[RabbitFXPro] 未找到 {obj.name} 的 RabbitFX 贴图绑定，跳过"
                f"（哈希 {hash_val}；确认本节点接在材质转资源之后）"
            )
            if _has_ntemifx_bindings(sections):
                print(
                    "[RabbitFXPro] 提示: 该配置表用的是 NTEMIFX 命名空间（NTEMI 逻辑），"
                    "本节点只注入 RabbitFX 命名空间"
                )
        return changed

    @staticmethod
    def _collect_target_blocks(sections, hash_val, suffix_pattern):
        """返回 [(段名, [(块首, 块尾), ...]), ...]，按段内 mesh 注释匹配目标物体。

        不限定 ``[TextureOverride_*]``：EFMI 合并骨骼把绘制内容放在
        ``[CommandList_*]`` 回调段里，ZZMI 的 TTL 重建也会另开段，材质转资源
        写下的 RabbitFX 绑定跟着 mesh 块走，所以只要段里带这个物体的 mesh 块
        就一起处理。
        """
        result = []
        for section_name, section_lines in sections.items():
            if not section_name.startswith('['):
                continue
            blocks = [
                (start, end)
                for start, end in _iter_mesh_blocks(section_lines)
                if _mesh_name_matches(
                    _extract_mesh_name(section_lines[start]), hash_val, suffix_pattern
                )
            ]
            usable = [
                (start, end)
                for start, end in blocks
                if _find_draw_index(section_lines, start, end) >= 0
                or _collect_existing_refs(section_lines, start, end)[2] >= 0
            ]
            if usable:
                result.append((section_name, usable))
        if result:
            return result

        # 回退：段里没有匹配的 mesh 注释（名字被映射/合并过）时，按 hash + 段名匹配整段。
        for section_name, section_lines in sections.items():
            if not section_name.startswith('[TextureOverride'):
                continue
            if not any(
                str(line).strip().lower().startswith('hash =')
                and str(line).split('=', 1)[1].strip().lower() == hash_val
                for line in section_lines
            ):
                continue
            if suffix_pattern and suffix_pattern not in section_name.replace('-', '_').lower():
                continue
            if _collect_existing_refs(section_lines, 0, len(section_lines))[2] < 0:
                continue
            result.append((section_name, [(0, len(section_lines))]))
        return result

    def _patch_mesh_block(self, lines, start, end, object_label, buffer_name):
        block = _strip_previous_blocks(lines[start:end])
        glow_ref, fxmap_ref, run_index = _collect_existing_refs(block, 0, len(block))
        draw_index = _find_draw_index(block, 0, len(block))
        if draw_index < 0:
            return False
        if not glow_ref and not fxmap_ref and run_index < 0:
            return False

        param_lines, wrote_glow, wrote_sync = self._build_param_lines(
            glow_ref, fxmap_ref, object_label, buffer_name
        )
        cs_lines = self._build_colorshift_lines(fxmap_ref, object_label)
        wrote_colorshift = bool(cs_lines)
        # 只写复位、不写任何参数时不动文件（"只对列表里的物体生成配置"）。
        if not param_lines and not cs_lines:
            return False

        insert_at = run_index if run_index >= 0 else draw_index
        if param_lines and run_index < 0:
            param_lines = [*param_lines, RUN_LINE]
        # ColorShift 必须在 Run 之后（Run 结尾会把 ResourceFXMap 清空）。
        cs_position = (run_index + 1) if run_index >= 0 else (insert_at + len(param_lines))

        # 先插靠后的位置，前面的索引才不会位移。
        if cs_lines:
            _insert_block(block, cs_position, cs_lines)
        if param_lines:
            _insert_block(block, insert_at, param_lines)

        if self.enable_reset:
            reset_lines = self._build_reset_lines(block, wrote_glow, wrote_sync, wrote_colorshift)
            if reset_lines:
                _insert_block(block, _find_draw_block_end(block, 0, len(block)), reset_lines)

        lines[start:end] = block
        return True

    def _build_param_lines(self, glow_ref, fxmap_ref, object_label, buffer_name):
        """返回 (行, 是否写了发光参数, 是否写了 W-Engine 同步参数)。"""
        lines = []
        wrote_glow = False
        wrote_sync = False
        if self.enable_glow and glow_ref:
            breathing = self._breathing()
            lines.extend(
                self._build_breath_lines() if breathing else self._build_static_glow_lines()
            )
            wrote_glow = True
        elif self.enable_glow:
            print(f"[RabbitFXPro] {object_label}: 没有发光贴图绑定，跳过发光参数")
        if self.enable_sync and (glow_ref or fxmap_ref):
            lines.extend(self._build_sync_lines(buffer_name))
            wrote_sync = True
        if not lines:
            return [], False, False
        return [_PARAM_BEGIN, *lines, _PARAM_END], wrote_glow, wrote_sync

    def _build_static_glow_lines(self):
        return [
            f"$\\RabbitFX\\H = {_format_float(self.glow_h)}",
            f"$\\RabbitFX\\S = {_format_float(self.glow_s)}",
            f"$\\RabbitFX\\V = {_format_float(self.glow_v)}",
            f"$\\RabbitFX\\brightness = {_format_float(self.glow_brightness)}",
            f"$\\RabbitFX\\interpolate = {_format_float(self.glow_interpolate)}",
        ]

    def _build_breath_lines(self):
        tag = self._var_tag()
        frame = f"{_VAR_PREFIX}{tag}_frame"
        valve = f"{_VAR_PREFIX}{tag}_valve"
        glow = f"{_VAR_PREFIX}{tag}_glow"
        fps = int(self.breath_fps)
        step = _format_number(self.breath_step)
        lines = [
            _BREATH_BEGIN,
            f"{frame} = {frame} + {step}",
            f"if {frame} >= {fps}",
            f"    {frame} = {frame} - {fps}",
            f"    {valve} = 1 - {valve}",
            "endif",
            f"if {valve} == 0",
            f"    {glow} = {frame}",
            "else",
            f"    {glow} = {fps} - {frame}",
            "endif",
        ]
        bmin = _format_number(self.breath_brightness_min)
        bmax = _format_number(self.breath_brightness_max)
        if self.breath_mode == 'SINGLE':
            lines.extend(
                [
                    f"$\\RabbitFX\\H = {_format_number(self.breath_h)}",
                    f"$\\RabbitFX\\S = {_format_number(self.breath_s)}",
                    f"$\\RabbitFX\\V = {_format_number(self.breath_v)}",
                    f"$\\RabbitFX\\brightness = {bmin} + ({bmax} - {bmin}) * {glow} / {fps}",
                ]
            )
        elif self.breath_mode == 'RAINBOW':
            lines.extend(
                [
                    f"$\\RabbitFX\\H = {glow} * {_format_number(360.0 / fps)}",
                    f"$\\RabbitFX\\S = {_format_number(self.breath_s)}",
                    f"$\\RabbitFX\\V = {_format_number(self.breath_v)}",
                    f"$\\RabbitFX\\brightness = {bmax}",
                ]
            )
        else:  # COMBO
            lines.extend(
                [
                    f"$\\RabbitFX\\H = {glow} * {_format_number(360.0 / fps)}",
                    f"$\\RabbitFX\\S = {_format_number(self.breath_s)}",
                    f"$\\RabbitFX\\V = {_format_number(self.breath_v)}",
                    f"$\\RabbitFX\\brightness = {glow} * {_format_number(float(self.breath_brightness_max) / fps)}",
                ]
            )
        lines.extend(
            [f"$\\RabbitFX\\interpolate = {_format_number(self.breath_interpolate)}", _BREATH_END]
        )
        return lines

    def _build_sync_lines(self, buffer_name):
        lines = [f"$\\rabbitfx\\blendmode = {int(self.sync_blendmode)}"]
        if self.sync_brightness_only:
            lines.append("$\\rabbitfx\\syncbrightnessonly = 1")
        if self.buffer_mode == 'RWBUFFER':
            lines.append(f"ps-u4 = {buffer_name}")
        else:
            lines.append(f"{SET_FX_BUFFER_REF} = ref {buffer_name}")
        return lines

    def _build_colorshift_lines(self, fxmap_ref, object_label):
        if not self.enable_colorshift:
            return []
        if not fxmap_ref:
            print(f"[RabbitFXPro] {object_label}: 没有 FXMap 绑定，跳过 ColorShift")
            return []
        return [
            _CS_BEGIN,
            f"{FXMAP_REF} = ref {fxmap_ref}",
            f"$\\RabbitFX\\H = {_format_float(self.cs_h)}",
            f"$\\RabbitFX\\S = {_format_float(self.cs_s)}",
            f"$\\RabbitFX\\V = {_format_float(self.cs_v)}",
            COLOR_SHIFT_LINE,
            _CS_END,
        ]

    def _build_reset_lines(self, block, wrote_glow, wrote_sync, wrote_colorshift):
        """只复位本节点真正写过的东西；材质转资源已有的复位行不重复写。"""
        candidates = []
        if wrote_glow:
            candidates.append(f"{GLOW_REF} = ref null")
        if wrote_colorshift:
            candidates.append(f"{FXMAP_REF} = ref null")
        if wrote_sync:
            candidates.append(f"{SET_FX_BUFFER_REF} = ref null")
        if wrote_glow or wrote_colorshift:
            candidates.extend(
                [
                    "$\\RabbitFX\\H = 0",
                    "$\\RabbitFX\\S = 0",
                    "$\\RabbitFX\\V = 0",
                ]
            )
        if wrote_glow:
            candidates.extend(
                [
                    "$\\RabbitFX\\brightness = 0",
                    "$\\RabbitFX\\interpolate = 0",
                ]
            )
        if wrote_sync:
            candidates.append("$\\rabbitfx\\blendmode = 0")
            if self.sync_brightness_only:
                candidates.append("$\\rabbitfx\\syncbrightnessonly = 0")
        existing = {str(line).strip().casefold() for line in block}
        lines = [line for line in candidates if line.casefold() not in existing]
        if not lines:
            return []
        return [_RESET_BEGIN, *lines, _RESET_END]

    # ──────────────────── W-Engine 提取源 ────────────────────

    def _process_sync_source(self, sections, obj, buffer_name):
        hash_val = _extract_hash_from_object(obj.name).lower()
        suffix_pattern = _normalize_suffix(_extract_resource_suffix(obj.name))
        if not hash_val:
            print(f"[RabbitFXPro] W-Engine 源 {obj.name}: 无法解析哈希，跳过")
            return False

        changed = False
        for section_name, blocks in reversed(self._collect_target_blocks(sections, hash_val, suffix_pattern)):
            section_lines = sections.get(section_name)
            if section_lines is None:
                continue
            for start, end in reversed(blocks):
                changed |= self._patch_sync_source_block(section_lines, start, end, buffer_name)
        if not changed:
            print(f"[RabbitFXPro] W-Engine 源 {obj.name}: 未找到可注入的绘制段")
        return changed

    def _patch_sync_source_block(self, lines, start, end, buffer_name):
        block = _strip_previous_blocks(lines[start:end])
        draw_index = _find_draw_index(block, 0, len(block))
        if draw_index < 0:
            return False
        run_index = -1
        for index in range(0, draw_index):
            if _RUN_LINE_RE.match(str(block[index]).strip()):
                run_index = index
                break

        block_lines = [_SYNC_BEGIN]
        if self.buffer_mode == 'RWBUFFER':
            # 旧版直绑：UpdateFXBuffer 会解绑 ps-u4，所以这条路径不用 pre run。
            block_lines.append(f"ps-u4 = {buffer_name}")
            block_lines.append(E_RUN_LINE)
        else:
            # 3DMigoto 资源拷贝：自定义资源先声明空段，拷贝语句写在绘制段里执行
            # （官方 Resource Copying 文档）。每帧先按 RabbitFX 自己的 FXBuffer
            # 重建一份，再交给 ERun 写入引擎发光色，目标部件只读同一份资源。
            block_lines.append(f"{buffer_name} = copy Resource\\{FX_NAMESPACE}\\FXBuffer")
            block_lines.append(f"{SET_FX_BUFFER_REF} = ref {buffer_name}")
            block_lines.append(UPDATE_BUFFER_LINE)
            block_lines.append(E_RUN_LINE)
        block_lines.append(_SYNC_END)
        _insert_block(block, run_index if run_index >= 0 else draw_index, block_lines)

        if self.enable_reset:
            reset_line = f"{SET_FX_BUFFER_REF} = ref null"
            existing = {str(line).strip().casefold() for line in block}
            if reset_line.casefold() not in existing:
                _insert_block(
                    block,
                    _find_draw_block_end(block, 0, len(block)),
                    [_RESET_BEGIN, reset_line, _RESET_END],
                )

        lines[start:end] = block
        return True

    # ──────────────────── 资源与变量声明 ────────────────────

    def buffer_resource_name(self):
        return f"ResourceRabbitFXPro_{self._var_tag()}_FXBuffer"

    def _ensure_buffer_resource(self, sections, buffer_name):
        section_name = f"[{buffer_name}]"
        if section_name in sections:
            return False
        if self.buffer_mode == 'RWBUFFER':
            sections[section_name] = ["type = RWBuffer", "format = R32_FLOAT", "array = 100"]
        else:
            # 自定义资源以空段声明；拷贝语句在提取源/目标部件的绘制段里执行。
            sections[section_name] = []
        return True

    def _ensure_breath_constants(self, sections):
        tag = self._var_tag()
        wanted = [
            f"global persist {_VAR_PREFIX}{tag}_frame = 0",
            f"global {_VAR_PREFIX}{tag}_valve = 0",
            f"global {_VAR_PREFIX}{tag}_glow = 0",
        ]
        constants = sections.setdefault('[Constants]', [])
        existing = {str(line).strip() for line in constants}
        added = [line for line in wanted if line not in existing]
        if not added:
            return False
        constants.extend(added)
        return True

    def _var_tag(self):
        tag = re.sub(r"[^0-9a-zA-Z_]", "_", str(self.buffer_tag or "")).strip("_")
        return tag or "0"

    # ──────────────────── 校验 ────────────────────

    def validate_export_configuration(self):
        conflicts = []
        for item in self.target_items:
            obj = item.target_object
            if obj is None:
                continue
            owners = _objects_used_by_other_nodes(self, obj)
            if owners:
                conflicts.append(f"{obj.name}（也被 {owners} 接管）")
        if conflicts:
            raise ValueError(
                "RabbitFX贴图后处理pro：同一物体被多个同类节点重复接管，参数会互相覆盖："
                + "、".join(conflicts)
            )


def _objects_used_by_other_nodes(node, obj):
    """同一蓝图里其它 RabbitFXPro 节点是否也在管这个物体。"""
    tree = getattr(node, "id_data", None)
    if tree is None or obj is None:
        return ""
    owners = []
    for other in getattr(tree, "nodes", []) or []:
        if other == node or getattr(other, "bl_idname", "") != NODE_IDNAME:
            continue
        if any(item.target_object == obj for item in getattr(other, "target_items", []) or []):
            owners.append(other.name)
        elif getattr(other, "sync_source_object", None) == obj:
            owners.append(f"{other.name}(提取源)")
    return "、".join(owners)


# ──────────────────────── 运算符 ────────────────────────

def _active_pro_node(context):
    node = getattr(context, "active_node", None)
    if node is not None and getattr(node, "bl_idname", "") == NODE_IDNAME:
        return node
    return None


def _node_by_name(context, node_name):
    space = getattr(context, "space_data", None)
    tree = None
    if space is not None:
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    if tree is not None and node_name:
        found = tree.nodes.get(node_name)
        if found is not None and getattr(found, "bl_idname", "") == NODE_IDNAME:
            return found
    return _active_pro_node(context)


class SSMT_OT_RabbitFXProScan(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_scan"
    bl_label = "扫描 FX 贴图物体"
    bl_description = (
        "从「材质转资源(pro)」的部件范围里收集所有使用 Glowmap 发光贴图 / FXMap "
        "裁切贴图的物体，填充到本节点的物体列表"
    )
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _node_by_name(context, self.node_name)
        if node is None:
            self.report({"WARNING"}, "未找到 RabbitFX贴图后处理pro 节点")
            return {"CANCELLED"}

        candidates, source_label = _collect_scan_candidates(node)
        if not candidates:
            self.report({"WARNING"}, f"未从{source_label}找到任何部件")
            return {"CANCELLED"}

        found = []
        for obj in candidates:
            has_glow, has_fx = _probe_fx_textures(obj)
            if has_glow or has_fx:
                found.append((obj, has_glow, has_fx))

        node.target_items.clear()
        for obj, has_glow, has_fx in found:
            item = node.target_items.add()
            item.target_object = obj
            item.has_glow = has_glow
            item.has_fx = has_fx
        node.active_target_index = min(node.active_target_index, len(node.target_items) - 1)

        glow_count = sum(1 for _o, has_glow, _f in found if has_glow)
        fx_count = sum(1 for _o, _g, has_fx in found if has_fx)
        self.report(
            {"INFO"},
            f"从{source_label}扫描到 {len(found)} 个 FX 物体"
            f"（发光贴图 {glow_count} / FX 贴图 {fx_count}）",
        )
        return {"FINISHED"}


def _collect_scan_candidates(node):
    """返回 (物体列表, 来源说明)。优先跟随「材质转资源(pro)」的部件范围。"""
    from .node_postprocess_custom_material_assign import _connected_blueprint_object_names

    tree = getattr(node, "id_data", None)
    material_node = None
    if tree is not None:
        for candidate in getattr(tree, "nodes", []) or []:
            if getattr(candidate, "bl_idname", "") in (
                MATERIAL_NODE_IDNAME,
                LEGACY_MATERIAL_NODE_IDNAME,
            ) and not getattr(candidate, "mute", False):
                material_node = candidate
                break

    objects = []
    seen = set()

    def add(obj):
        if obj is None or getattr(obj, "type", "") != "MESH" or obj.name in seen:
            return
        seen.add(obj.name)
        objects.append(obj)

    if material_node is not None:
        if bool(getattr(material_node, "use_global_assign", False)):
            for name in _connected_blueprint_object_names(tree):
                add(bpy.data.objects.get(name))
            return objects, "材质转资源(pro) 全局扫描范围"
        for item in getattr(material_node, "target_items", []) or []:
            add(getattr(item, "target_object", None))
        if objects:
            return objects, "材质转资源(pro) 目标列表"

    if tree is not None:
        for name in _connected_blueprint_object_names(tree):
            add(bpy.data.objects.get(name))
    return objects, "蓝图链路（未找到材质转资源节点）"


class SSMT_OT_RabbitFXProAdd(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_add"
    bl_label = "添加目标"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        node = _active_pro_node(context)
        if node is None:
            return {"CANCELLED"}
        node.target_items.add()
        node.active_target_index = len(node.target_items) - 1
        return {"FINISHED"}


class SSMT_OT_RabbitFXProRemove(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_remove"
    bl_label = "移除目标"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        node = _active_pro_node(context)
        if node is None:
            return {"CANCELLED"}
        index = node.active_target_index
        if 0 <= index < len(node.target_items):
            node.target_items.remove(index)
            node.active_target_index = max(0, min(index, len(node.target_items) - 1))
        return {"FINISHED"}


class SSMT_OT_RabbitFXProSelect(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_select"
    bl_label = "选择目标"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        node = _active_pro_node(context)
        if node is None:
            return {"CANCELLED"}
        node.active_target_index = -1 if node.active_target_index == self.index else self.index
        return {"FINISHED"}


class SSMT_OT_RabbitFXProAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的网格物体加入列表（已在列表中的跳过）"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        node = _active_pro_node(context)
        if node is None:
            return {"CANCELLED"}
        existing = {item.target_object for item in node.target_items if item.target_object}
        added = 0
        for obj in context.selected_objects:
            if getattr(obj, "type", "") != "MESH" or obj in existing:
                continue
            item = node.target_items.add()
            item.target_object = obj
            item.has_glow, item.has_fx = _probe_fx_textures(obj)
            existing.add(obj)
            added += 1
        if added:
            node.active_target_index = len(node.target_items) - 1
        self.report({"INFO"}, f"已加入 {added} 个物体" if added else "选中物体已在列表中")
        return {"FINISHED"}


_classes = (
    RabbitFXProTargetItem,
    SSMTNode_PostProcess_RabbitFXPro,
    SSMT_OT_RabbitFXProScan,
    SSMT_OT_RabbitFXProAdd,
    SSMT_OT_RabbitFXProRemove,
    SSMT_OT_RabbitFXProSelect,
    SSMT_OT_RabbitFXProAddSelected,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
