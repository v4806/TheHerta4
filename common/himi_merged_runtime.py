"""HIMI（崩坏3 / HI3）合并骨架**运行时资源计划**（导出侧，T7 交付）。

本模块把导入侧数据层（:mod:`common.himi_skeleton`）在工作空间里落盘的东西，翻译
成导出侧真正需要的两件产物：

1. **一条资源声明**（``ResourceHI3MergedSkeleton_<role_key>``）——角色级合并骨架的
   typed 缓冲声明（**不带 ``filename``**：内容是每帧由 attach CS 写入的活资源）；INI 段与
   HLSL 声明**由同一个 :class:`HIMIResourceDecl` 渲染**，
   所以两者不可能互相漂移（``tests/test_himi_merged_runtime.py`` 逐字段比对）。
2. **可重绑的 deform pass 清单**——只有通过 **G1** 的部件才会出现在这里。G1 的判定
   **不在本模块**：它由数据层 ``HIMISkeletonMergeHelper.prepare(target_bares=本次
   导出集合)`` 给出（T9-F4 之后是 **pass 级**：一个 deform pass 被多个 IB 家族共享时，
   有任一家族不完整就整个 pass 不可合并）。本模块只把那份结论搬过来用
   （:func:`prepare_gate_oracle`），dump 不可用时退回导入期缓存口径并**保守收窄**——
   绝不另写一份家族/pass 过滤，避免两边口径漂移。

设计依据：``HIMI骨骼合并计划书.md`` §7（导出/运行时阶段的可交付规格）、
``reports/himi-merge/05-decisions.md``。

为什么骨架不能是静态文件，以及 attach CS 怎么接（D01 / D10 / N02 / N07 的现行口径）
------------------------------------------------------------------------------------
**t51 裁定：静态前提被实机证伪。** 游戏 palette 是**每帧内容**（同一 ``vs-t0`` 资源哈希
在 6 份 dump 里给出 6 种内容；静态场景资源逐字节不变），所以合并骨架**不能**是一份
静态文件。本模块现在的口径是「**映射表 + 每帧 CS 写入**」：

* 每个 deform VB 段按 **①捕获当帧 palette → ②``run`` attach CS** 的顺序发命令
  （``ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null`` → ``run = <该族 attach 段>``），
  随后才是 ``vs-t0 = <合并骨架>`` 的重绑；
* 每族一个 ``[CustomShaderHI3MergedSkeletonAttach_<fam>]`` 段，把当帧 palette 写进骨架的
  家族偏移区间（``x1`` = 全局基址、``y1`` = 该族槽数、``Dispatch = ceil(y1/64)``）；
* ``[Constants]`` / ``[Present]`` 状态机与键池仍然**没有**（HIMI 不需要逐帧计数器）；
* ``Toolset/himi_merged_skeleton_attach.hlsl`` 是该 attach CS 的**冻结副本**（由
  :func:`render_attach_hlsl` 生成），并被每个 ``[CustomShader…]`` 段引用。

**捕获行的方向（写错会污染游戏当帧 palette）**

3DMigoto 的 copy 是**赋值式**：**LHS = 目标**、``copy <槽>`` = 源。正确写法只能是

.. code-block:: ini

   ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null

（逐字核对 ``ui/universal/zzmi.py:3359``、``:5383-5405`` 与
``Toolset/zzmi_merged_skeleton_attach.hlsl:12``。）**顺序**同样固定：捕获 → ``run`` →
``vs-t0 = <骨架>``；反了会把刚绑上的骨架当成当帧 palette 捕获（自反馈）。

与计划书 §7.1 / §7.3 A3 的一处**有意偏离**（已上报给评审）
----------------------------------------------------------
计划书正文写「不需要 ``bind_flags``」。本模块**照样声明**
``bind_flags = shader_resource unordered_access``，理由是：合并骨架要作为 ``vs-t0``
被绑定（= 需要 **SRV**），而项目自己实测过的跨显卡不变量（项目 key 记忆
2026-09-22，ZZMI 实机验证）是——typed ``RWBuffer`` 缺 ``bind_flags`` 时 **SRV 创建
静默失败**、绑 NULL、deform 读全零，且只在两张显卡中的一张上暴露。T7 的验收标准
逐字要求「沿用 ZZMI 跨显卡不变量：typed RWBuffer + format + array = 3 × 槽位数 +
bind_flags = shader_resource unordered_access」，故以验收标准为准；差异记入交付说明。

跨显卡不变量（必须同时满足）
----------------------------
A1 **typed**：``type = RWBuffer`` + ``format = R32G32B32A32_FLOAT`` +
   ``array = 3 × 槽位数``。
A2 **布局**：槽位 ``i`` 占字节 ``i*48``，= typed 元素 ``i*3 .. i*3+2``（3×4 行主序），
   与 dump 里的 palette 逐字节同构。
A3 **绝不加 ``stride``**：加 ``stride`` 会把资源变成结构化声明（声明 48B vs 视图
   16B）→ 落 D3D11 未定义区：AMD 驱动按 SBS(48) 取址，骨号 ``b`` 实际读到槽位
   ``3b`` → 越界读返回精确 0 → 顶点塌到原点。
另外：**palette 形态**的资源必须是 ``type = Buffer`` + ``stride = 48`` 且
**绝不允许** ``format``（加 format 会把 StructureByteStride 改成 16、ByteWidth 缩到
``16 × vg_count``，而 ``copy vs-t0`` 要拷 ``48 × vg_count`` 字节 → 溢出）。HIMI 的每帧
捕获副本 ``ResourceHI3PaletteFam_<fam>`` 正是这一形态（:func:`build_family_palette_decl`），
并由 :func:`validate_resource_decl` 守卫。

**合并骨架不得声明 ``filename``**（t51 裁定）：带 filename 即静态文件，而内容必须每帧由
attach CS 写入。``Meshes/<role>-MergedSkeleton.buf`` 仍会落盘，但只作**离线证据/调试产物**。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field

from ..utils.json_utils import JsonUtils
from .himi_merged_contract import (
    DEFORM_GATE_REJECT_PREFIX,
    format_deform_gate_rejections,
)
from .himi_skeleton import (
    HIMI_DEFORM_GATE_VERSION,
    HIMI_MERGED_SKELETON_METADATA_FILE,
    HIMI_MERGED_SKELETON_SLOT_LIMIT,
    HIMI_VG_MAP_ALGORITHM_VERSION,
    PALETTE_STRIDE_BYTES,
    HIMIBoneMapBuilder,
    bare_from_unique_str,
    derive_role_key,
    resolve_submesh_json_path,
    runtime_cache_path,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 计划 schema 版本（本模块自身；与数据层的 VGMap/Gate 版本独立演进）
HIMI_RUNTIME_PLAN_VERSION = 1

#: 合并骨架资源名前缀（角色级；与 ZZMI 的 ResourceZZMergedSkeleton_* 不同命名空间）
MERGED_SKELETON_RESOURCE_PREFIX = "ResourceHI3MergedSkeleton_"
#: mod 内骨架二进制所在目录（与 VB/IB 同一路径体系；实际用当前 buffer 目录名）
MERGED_SKELETON_DEFAULT_FOLDER = "Meshes"
#: 角色级骨架二进制文件名后缀
MERGED_SKELETON_BASENAME_SUFFIX = "-MergedSkeleton.buf"

#: A1：typed 声明。``type = RWBuffer`` + ``format`` 是 SRV/UAV 双用途的显式形态。
MERGED_SKELETON_INI_TYPE = "RWBuffer"
MERGED_SKELETON_INI_FORMAT = "R32G32B32A32_FLOAT"
#: 缺它会静默丢掉 SRV（跨显卡不变量第 1 条，见模块头）。
MERGED_SKELETON_INI_BIND_FLAGS = "shader_resource unordered_access"
#: 每根骨 = 3 个 typed float4 元素（3×4 行主序矩阵 = 48 字节）
MERGED_SKELETON_ELEMENTS_PER_SLOT = 3
MERGED_SKELETON_STRIDE = PALETTE_STRIDE_BYTES

#: palette 形态守卫：类型/步长固定，且**禁止** ``format``。
PALETTE_RESOURCE_INI_TYPE = "Buffer"
PALETTE_FORBIDDEN_INI_KEYS = ("format", "bind_flags")

#: ``[Resource...]`` 段里 INI 字段的规范顺序（渲染与解析共用，避免两边各写一份）
RESOURCE_INI_FIELD_ORDER = ("type", "format", "stride", "array", "bind_flags", "filename")

#: 参考声明的槽位数 = 五角色**单角色最大**唯一骨骼数（``04-probe-results.md`` §4：
#: 276/343/315/219/252）。``Toolset/himi_merged_skeleton_attach.hlsl`` 用它生成，
#: 但不是任何真实角色的产物 —— 资源名里的 ``REFERENCE`` 是显式占位。
REFERENCE_SLOT_COUNT = 343
REFERENCE_ROLE_KEY = "REFERENCE"

#: 本环境**无法**取证的项（计划书 §11 V1/V2/V3 + §7.4 R3）。既不得阻塞实现，
#: 也不得静默假设成立：它们同时进入 HLSL 抬头与交付说明。
UNVERIFIED_IN_GAME = (
    "V1 骨架新鲜度：**旧静态前提已被 t51 实机证伪** —— 游戏 palette 是每帧内容（同一 "
    "`vs-t0` 资源哈希在 6 份 dump 里给出 6 种内容，而场景静态资源逐字节不变）⇒ 合并骨架"
    "**不能**是一份静态文件。本方案改为「deform 段捕获当帧 palette"
    "（`ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null`）→ attach CS 写进骨架区间」，"
    "逐帧新鲜度由该链保证。实机仍需确认：换姿势后不出现『跟随上一帧骨骼』"
    "（若出现，先查捕获/run 是否排在 `vs-t0 = <骨架>` 之前、再查 Dispatch/槽数）。",
    "V2 deform VS 是否按 typed `palette[3b+m]` 读骨架：HI3 的 d3dx.ini cache_shaders=0、"
    "ShaderFixes/ 为空、无 DXBC，无法静态确认。本模块只承诺『骨架声明与 palette 逐字节"
    "同构』（A1/A2）；读取侧形态待开 shader dump 后核对 dcl_resource 与索引表达式。",
    "V3 mod Blend 的行序是否等于游戏 SO 行序：导出侧顶点顺序是 loop 首次出现序"
    "（计划书 §7.5 O3），需真实工作空间逐顶点比对。",
    "R3 vb1 覆盖 + vs-t0 重绑在**非索引 Draw** 上的实际行为未经实机验证"
    "（本环境无实机条件）。",
)


class HIMIResourceDeclError(ValueError):
    """资源声明违反跨显卡不变量（A1/A2/A3 或 palette 形状守卫）。"""


class HIMIFamilyRangeError(HIMIResourceDeclError):
    """家族槽区间无法确定或不自洽 ⇒ **fail-closed**（t51：不许静默容忍）。

    attach CS 按「族内本地槽 i → 全局槽 ``base+i``」搬运**连续块**，所以区间必须密集、
    互不重叠、落在骨架槽位内；当本次导出覆盖**全部**已落盘带数据部件时（``complete=True``），
    还要求它们从 0 起、首尾相接、且总和等于骨架槽位数。任何一条不满足都会写错位置
    （或留下永不被写的空洞 ⇒ deform 读零 ⇒ 顶点塌到原点）。
    """


def compute_family_ranges(parts, slot_count: int, *, complete: bool = True) -> dict:
    """每族的 attach 块：``{family: {"base", "slots", "own_block"}}``（t51 §4.2 ②）。

    数据源 = 各组件 ``part_from_json_payload`` 里的 ``slot_values``（**数据层注册给它的
    全局槽**）。**fail-closed**，判据分三层：

    **① 专属 / 借位**

    跨部件按矩阵逐位去重会把同一根骨收敛到**一个** canonical 全局槽（数据层的注册循环），
    于是存在这样的家族：它的骨**全部**落在别的家族注册出来的块里（实测真实角色
    ``镇×偃月叩晓``：家族 ``9430996d`` 的 46 个被引用全局槽全部落在 ``2c1f2636`` 的块
    ``[0,148)`` 内）。这类家族**不需要自己的 attach 块**：那些槽由拥有者家族的块写入，
    而矩阵按去重语义**逐位相同** ⇒ 内容等价。判据
    ``exclusive(family) := registered(family) \\ ∪_{other} registered(other)``
    （与 t21 的站位判据同一套词汇）：空 ⇒ ``own_block=False``、``slots=0``。

    **② 布局层（对有专属槽的家族，永远检查）**

    * 族内密集：``len(registered) == max - min + 1``（否则连续块搬运会写错位置）；
    * 越界：块必须落在 ``[0, slot_count)`` 内；
    * 互不重叠：按 base 升序两两不交（重叠 ⇒ 两个 attach 段写同一批槽位、后写的赢）。

    **③ 覆盖层（``complete=True`` 时检查；``complete=False`` = 子集导出，交给调用方诊断）**

    * 首块从 0 起；首尾相接（没有洞）；``∑span == slot_count``（骨架每个槽都有写方）。

    ``complete`` 为什么必须显式给：**覆盖性只有在「本次导出的部件集合 = 已落盘全集」时才
    可判**。按家族局部导出（t20 裁定为合法路径）本来就只带部分家族进场，骨架其余槽位在
    本次导出里天然没有写方 —— 那是**已知的子集语义**（调用方必须写成诊断），用
    ``∑span == slot_count`` 拦它会把合法路径误杀。逐部件的陈旧性由 ``_is_stale_part``
    与 palette 缓存核对独立保证，与这里正交。

    **子集导出的一个边界**：``own_block=False`` 的（借位）家族，其槽位由**别的家族**的块
    写入 ⇒ 只导出它自己时骨架里没有它的写方（``family_block_slots`` 不含这些槽）。
    调用方（``plan_from_metadata``）用 :func:`uncovered_slot_ranges` 把这种空缺写成诊断。
    """
    per_family: dict = {}
    for part in parts or ():
        values = {int(v) for v in (getattr(part, "slot_values", ()) or ()) if int(v) >= 0}
        if not values:
            continue
        family = str(getattr(part, "family_key", "") or "")
        if not family:
            continue
        per_family.setdefault(family, set()).update(values)
    if not per_family:
        raise HIMIFamilyRangeError(
            "无法确定任何家族区间：没有任何带 VGMap 槽值的部件 ⇒ attach 段无法生成"
        )
    total = int(slot_count or 0)
    owners: dict = {}
    borrowed: dict = {}
    for family, values in per_family.items():
        others: set = set()
        for other, other_values in per_family.items():
            if other != family:
                others |= other_values
        exclusive = values - others
        if not exclusive:
            # 借位族：注册槽**全部**落在别的家族注册出来的块里 ⇒ 不生成自己的 attach 段
            # （见 docstring ①：槽由拥有者的块写入，矩阵按逐位去重语义相同）。
            borrowed[family] = {"base": int(min(values)), "slots": 0, "own_block": False}
            continue
        base, top = min(values), max(values)
        span = top - base + 1
        if len(values) != span:
            raise HIMIFamilyRangeError(
                f"{family}: 槽值不密集（distinct={len(values)} span={span}，专属槽 "
                f"{len(exclusive)} 个）⇒ attach CS 的连续块搬运会写错位置"
            )
        if total > 0 and (base < 0 or base + span > total):
            raise HIMIFamilyRangeError(
                f"{family}: 区间 [{base},{base + span}) 越出骨架槽位 [0,{total})"
            )
        owners[family] = {"base": int(base), "slots": int(span), "own_block": True}
    cursor = 0
    out: dict = {}
    for index, (family, info) in enumerate(
        sorted(owners.items(), key=lambda item: (int(item[1]["base"]), str(item[0])))
    ):
        base, span = int(info["base"]), int(info["slots"])
        if base < cursor:
            raise HIMIFamilyRangeError(
                f"{family}: 区间 [{base},{base + span}) 与前一区间（结束于 {cursor}）重叠 ⇒ "
                "两个 attach 段会写同一批槽位（后写的赢）"
            )
        if complete and index == 0 and base != 0:
            raise HIMIFamilyRangeError(
                f"{family}: 区间从 {base} 起，而骨架槽位从 0 开始 ⇒ "
                f"槽 [0,{base}) 没有写方（空洞 ⇒ deform 读零）"
            )
        if complete and base != cursor:
            raise HIMIFamilyRangeError(
                f"{family}: 区间从 {base} 开始，与上一区间（结束于 {cursor}）不接续 ⇒ "
                f"骨架槽 [{cursor},{base}) 没有写方（空洞 ⇒ deform 读零）"
            )
        cursor = base + span
        out[family] = info
    if complete and total > 0 and cursor != total:
        raise HIMIFamilyRangeError(
            f"家族区间总长 {cursor} ≠ 骨架槽数 {total}（∑span 必须等于 ReferencedSlotTotal）"
        )
    out.update(borrowed)
    return out


def family_block_slots(ranges) -> set:
    """所有 ``own_block=True`` 家族覆盖的全局槽（借位族不贡献槽位）。"""
    slots: set = set()
    for info in (ranges or {}).values():
        span = int(info.get("slots", 0) or 0)
        if span > 0:
            base = int(info.get("base", 0) or 0)
            slots.update(range(base, base + span))
    return slots


def uncovered_slot_ranges(ranges, slot_count: int) -> list:
    """``[0, slot_count)`` 里**没有任何 attach 块覆盖**的连续段（升序）。

    子集导出时这些槽在本次导出里没有写方：引用它们的顶点会读到未写入（零）矩阵。它是
    **已知且必须告知**的后果，不是布局错误 —— 由 :func:`plan_from_metadata` 写成诊断
    （计划书 §7.7.7 S3：诊断四条文案之一）。
    """
    total = int(slot_count or 0)
    if total <= 0:
        return []
    covered = sorted(
        (int(info["base"]), int(info["base"]) + int(info["slots"]))
        for info in (ranges or {}).values()
        if int(info.get("slots", 0) or 0) > 0
    )
    gaps: list = []
    cursor = 0
    for start, end in covered:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        gaps.append((cursor, total))
    return gaps



# --------------------------------------------------------------------------- #
# 1) 资源声明：INI 与 HLSL 的**唯一**来源
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HIMIResourceDecl:
    """One ``[Resource...]`` declaration, renderable as INI **and** as HLSL.

    ``kind`` 只有两个取值：

    * ``"merged_skeleton"`` —— 角色级静态合并骨架（typed，A1/A2/A3）；
    * ``"palette"`` —— palette 形态（HIMI 本期不生成，只做形状守卫）。
    """

    name: str
    kind: str
    ini_type: str
    ini_format: str
    array: int
    stride: int
    bind_flags: str
    filename: str
    slots: int

    # -- derived --------------------------------------------------------- #

    @property
    def byte_width(self) -> int:
        """声明的字节数（合并骨架 = 槽位数 × 48）。"""
        if self.kind == "merged_skeleton":
            return int(self.slots) * MERGED_SKELETON_STRIDE
        return int(self.array) * int(self.stride or 0)

    def ini_fields(self) -> dict:
        """Field-name → value for the fields this declaration actually emits.

        缺省不出现的键（例如合并骨架的 ``stride``）**不会**出现在返回值里 ——
        测试正是靠「键集合」断言 ``stride``/``format`` 的有无。
        """
        fields = {"type": self.ini_type}
        if self.ini_format:
            fields["format"] = self.ini_format
        if self.stride:
            fields["stride"] = int(self.stride)
        if self.array:
            fields["array"] = int(self.array)
        if self.bind_flags:
            fields["bind_flags"] = self.bind_flags
        if self.filename:
            fields["filename"] = self.filename
        return fields

    def ini_lines(self) -> list[str]:
        """``[<name>]`` block body (no trailing blank line)."""
        fields = self.ini_fields()
        lines = [f"[{self.name}]"]
        for key in RESOURCE_INI_FIELD_ORDER:
            if key not in fields:
                continue
            lines.append(f"{key} = {fields[key]}")
        # 规范顺序之外的键（未来扩展）也按插入序补在后面。
        for key, value in fields.items():
            if key not in RESOURCE_INI_FIELD_ORDER:
                lines.append(f"{key} = {value}")
        return lines

    def machine_line(self) -> str:
        """机器可读的一行声明（INI 与 HLSL 都写它，供测试逐字段比对）。

        取值里可能含空格（``bind_flags = shader_resource unordered_access``），
        故按 shell 词法加引号；:func:`parse_hlsl_resource_decl` 用 ``shlex`` 还原。
        """
        fields = self.ini_fields()
        parts = [f"name={_quote_token(self.name)}", f"kind={self.kind}"]
        for key in RESOURCE_INI_FIELD_ORDER:
            parts.append(f"{key}={_quote_token(fields.get(key, 'none'))}")
        parts.append(f"slots={int(self.slots)}")
        parts.append(f"bytes={self.byte_width}")
        return "HIMI-RUNTIME-DECL " + " ".join(parts)

    def hlsl_declaration(self) -> str:
        """The HLSL object declaration matching this INI block exactly."""
        if self.kind != "merged_skeleton":
            raise HIMIResourceDeclError(
                "只有 merged_skeleton 形态才有 HLSL typed 声明；"
                f"{self.name} 的 kind={self.kind}"
            )
        return f"RWBuffer<float4> {self.name} : register(u0);"


def validate_resource_decl(decl: HIMIResourceDecl) -> None:
    """Raise :class:`HIMIResourceDeclError` on any invariant violation."""
    if not str(decl.name or "").strip():
        raise HIMIResourceDeclError("资源名为空")
    if decl.kind == "merged_skeleton":
        if decl.ini_type != MERGED_SKELETON_INI_TYPE:
            raise HIMIResourceDeclError(
                f"A1: {decl.name} 的 type 必须是 {MERGED_SKELETON_INI_TYPE}（typed）"
            )
        if decl.ini_format != MERGED_SKELETON_INI_FORMAT:
            raise HIMIResourceDeclError(
                f"A1: {decl.name} 必须声明 format = {MERGED_SKELETON_INI_FORMAT}"
            )
        if decl.stride:
            raise HIMIResourceDeclError(
                f"A3: {decl.name} 绝不能声明 stride（会改成结构化视图 → 跨显卡未定义区）"
            )
        if int(decl.array) != int(decl.slots) * MERGED_SKELETON_ELEMENTS_PER_SLOT:
            raise HIMIResourceDeclError(
                f"A1: {decl.name} 的 array 必须是 3 × 槽位数"
                f"（{int(decl.slots) * MERGED_SKELETON_ELEMENTS_PER_SLOT}），"
                f"实际 {int(decl.array)}"
            )
        if not decl.bind_flags:
            raise HIMIResourceDeclError(
                f"{decl.name} 必须声明 bind_flags：typed RWBuffer 缺它会让 SRV 创建"
                "静默失败（绑 NULL → deform 读全零）"
            )
        if "shader_resource" not in decl.bind_flags:
            raise HIMIResourceDeclError(
                f"{decl.name} 的 bind_flags 必须含 shader_resource（它要作为 vs-t0 绑定）"
            )
        if "unordered_access" not in decl.bind_flags:
            raise HIMIResourceDeclError(
                f"{decl.name} 的 bind_flags 必须含 unordered_access"
                "（typed RWBuffer 的 UAV 侧；与 ZZMI 声明逐字一致避免视图路径分叉）"
            )
        if int(decl.slots) <= 0:
            raise HIMIResourceDeclError(f"{decl.name} 的槽位数必须为正，实际 {decl.slots}")
        if int(decl.slots) > HIMI_MERGED_SKELETON_SLOT_LIMIT:
            raise HIMIResourceDeclError(
                f"{decl.name} 的槽位数 {decl.slots} 超过上限 "
                f"{HIMI_MERGED_SKELETON_SLOT_LIMIT}"
            )
        return

    if decl.kind == "palette":
        if decl.ini_type != PALETTE_RESOURCE_INI_TYPE:
            raise HIMIResourceDeclError(
                f"palette 形态的 {decl.name} 必须是 type = {PALETTE_RESOURCE_INI_TYPE}"
            )
        if int(decl.stride or 0) != MERGED_SKELETON_STRIDE:
            raise HIMIResourceDeclError(
                f"palette 形态的 {decl.name} 必须 stride = {MERGED_SKELETON_STRIDE}"
                f"（48 B/骨），实际 {decl.stride}"
            )
        if decl.ini_format:
            raise HIMIResourceDeclError(
                f"palette 形态的 {decl.name} 绝不能加 format："
                "它会把 StructureByteStride 改成 16、ByteWidth 缩到 16×vg_count，"
                "而 copy 要拷 48×vg_count 字节 → 溢出"
            )
        return

    raise HIMIResourceDeclError(f"未知资源形态 kind={decl.kind!r}")


def merged_skeleton_resource_name(role_key: str) -> str:
    """``ResourceHI3MergedSkeleton_<role_key>``."""
    return MERGED_SKELETON_RESOURCE_PREFIX + str(role_key or "").strip()


def merged_skeleton_basename(role_key: str) -> str:
    """``<role_key>-MergedSkeleton.buf``（纯文件名，无目录）。"""
    return str(role_key or "").strip() + MERGED_SKELETON_BASENAME_SUFFIX


def build_merged_skeleton_decl(
    role_key: str,
    slot_count: int,
    *,
    folder: str = MERGED_SKELETON_DEFAULT_FOLDER,
) -> HIMIResourceDecl:
    """Build (and validate) the merged-skeleton declaration for one plan.

    **t51 裁定：不再带 ``filename``** —— 带 filename 就是静态文件，而游戏 palette 是每帧
    内容（同一资源哈希在 6 份 dump 里 6 种内容）⇒ 骨架内容必须由 attach CS 每帧写入。
    ``folder`` 只用于**离线证据产物**（``Meshes/<role>-MergedSkeleton.buf``）的路径计算，
    不再进 ini 的资源段。
    """
    slots = int(slot_count or 0)
    decl = HIMIResourceDecl(
        name=merged_skeleton_resource_name(role_key),
        kind="merged_skeleton",
        ini_type=MERGED_SKELETON_INI_TYPE,
        ini_format=MERGED_SKELETON_INI_FORMAT,
        array=slots * MERGED_SKELETON_ELEMENTS_PER_SLOT,
        stride=0,
        bind_flags=MERGED_SKELETON_INI_BIND_FLAGS,
        filename="",  # t51：活资源 —— 每帧由 attach CS 填充
        slots=slots,
    )
    validate_resource_decl(decl)
    return decl


def merged_skeleton_evidence_path(role_key: str, folder: str = MERGED_SKELETON_DEFAULT_FOLDER) -> str:
    """离线证据产物路径（**不进 ini**；仅用于 ``_himi_publish_skeleton`` 落盘调试）。"""
    return (
        f"{str(folder or MERGED_SKELETON_DEFAULT_FOLDER)}/"
        f"{merged_skeleton_basename(role_key)}"
    )


#: 每族每帧捕获副本的命名前缀（``copy vs-t0`` 的目标，**不是**源）。
FAMILY_PALETTE_PREFIX = "ResourceHI3PaletteFam_"
#: 每族 attach CS 段名前缀。
ATTACH_SECTION_PREFIX = "CustomShaderHI3MergedSkeletonAttach_"


def family_palette_name(family: str) -> str:
    """``ResourceHI3PaletteFam_<fam>``：该族当帧 palette 的持久副本（copy 的**目标**）。"""
    return FAMILY_PALETTE_PREFIX + str(family or "").strip()


def attach_section_name(family: str) -> str:
    """``CustomShaderHI3MergedSkeletonAttach_<fam>``：该族的 attach CS 段名。"""
    return ATTACH_SECTION_PREFIX + str(family or "").strip()


#: 每族 attach CS 文件名前缀（落在 ``<mod>/res/`` 下）。
FAMILY_ATTACH_HLSL_PREFIX = "himi_attach_"
#: 每族 attach CS 里的目标基址宏名（**接口**：导出侧写它、CS 读它）。
#:
#: 基址走**文件里的 #define** 而不是 ini 参数：ini 参数纹理布局随 3DMigoto fork 变化
#: （标准版 ``x1/y1`` 落 ``IniParams[0]``，Armor fork 从 ``[1]`` 起），读错位置只会读到
#: 未设置的 0 ⇒ CS 静默不写 ⇒ 骨架留空 ⇒ 模型爆炸且不报错（本仓 key 记忆 2026-09-22）。
FAMILY_ATTACH_BASE_DEFINE = "HIMI_ATTACH_BASE"


def family_attach_hlsl_basename(family: str) -> str:
    """``himi_attach_<fam>.hlsl``（每族一个 CS 文件；基址写死在该文件里）。"""
    return f"{FAMILY_ATTACH_HLSL_PREFIX}{str(family or '').strip()}.hlsl"


def family_attach_hlsl_path(family: str) -> str:
    """``./res/himi_attach_<fam>.hlsl``（ini 里的相对路径，从 mod 根算起）。"""
    return f"./res/{family_attach_hlsl_basename(family)}"


def render_family_attach_base_line(base: int) -> str:
    """该族 CS 文件里的基址宏（**模组侧参考实现**逐字形状：``#define HIMI_ATTACH_BASE <base>``）。

    这是导出侧（本模块的 ``family_ranges``）与 CS（t55 落地）之间的**唯一**逻辑参数接口；
    槽数不走参数，由 CS 用 ``GetDimensions`` 读 palette 资源自身维度后 ``/3u`` 还原。
    """
    value = int(base)
    if value < 0:
        raise HIMIResourceDeclError(f"attach 基址不能为负（{value}）")
    return f"#define {FAMILY_ATTACH_BASE_DEFINE} {value}"


def build_family_palette_decl(family: str, slot_count: int) -> HIMIResourceDecl:
    """每族持久副本声明：``Buffer`` + ``stride = 48`` + **无 format**（t51 §4.2 ①）。"""
    return build_palette_decl(family_palette_name(family), int(slot_count or 0))


def render_attach_capture_lines(family: str) -> list[str]:
    """deform VB 段里该族要追加的两行：**①捕获 → ②run**（t51 §4.2 ①③，顺序不可换）。

    * ① ``ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null`` —— LHS 是**目标**
      （3DMigoto 赋值式；写反会把空副本写进游戏当帧 palette，见模块头）。
    * ② ``run = CustomShaderHI3MergedSkeletonAttach_<fam>`` —— 幂等，且必须排在
      ``vs-t0 = <骨架>`` 重绑之前（否则会捕到刚绑上的骨架）。
    """
    return [
        f"{family_palette_name(family)} = copy vs-t0 unless_null",
        f"run = {attach_section_name(family)}",
    ]


def render_attach_section_lines(
    family: str,
    *,
    base: int,
    slots: int,
    skeleton_name: str,
    hlsl_path: str = "",
) -> list[str]:
    """``[CustomShaderHI3MergedSkeletonAttach_<fam>]`` 段体（**模组侧参考实现**的形状）。

    形状以 2026-09-25 用户模组的**逐帧手工实现**
    （``K:\\…\\Mods\\SSGeneratedMod\\嗨♪爱愿妖精♥``，队长落地、离线自检
    ``reports/himi-merge/30-verify-mod-perframe.py`` D 组）为准：

    * **不要 ``x1``/``y1``/``z1``**：家族基址 ``base`` 写死在**该族自己的 CS 文件**里
      （``#define HIMI_ATTACH_BASE <base>``），槽数由 CS 用 palette 资源自身维度还原
      （``GetDimensions`` 单参后 ``/3u``）。整条链**不读 ``IniParams``** —— ini 参数纹理
      布局随 3DMigoto fork 变化（标准版 ``x1/y1`` 落 ``IniParams[0]``，Armor fork 从
      ``[1]`` 起），读错位置只会读到未设置的 0 ⇒ CS 静默不写 ⇒ 骨架留空 ⇒ 模型爆炸且
      **不报任何错**（本仓 key 记忆 2026-09-22）。
    * ``cs = ./res/himi_attach_<fam>.hlsl``：**每族一个 CS 文件**（``cs = ./res/…`` 与仓库
      其它导出器一致：ZZMI ``ui/universal/zzmi.py:5560``、EFMI ``ui/universal/efmi.py:1409``；
      对应的 .hlsl 由导出侧复制到 ``<mod>/res/``）。**不要**写成 ``../res/…``：ini 里的相对
      路径是从 ini 自身所在目录（mod 根）算起的。
    * ``Dispatch = ceil(slots / 64)``（下界 1），段末 ``cs-u0 = null`` 解绑。

    ``base`` 仍必须传：它决定该族 CS 文件里的 ``#define``（写错 = 把本族矩阵写进别的族的槽位），也用于 :func:`family_ranges` 的一致性核对。
    """
    base = int(base or 0)
    slots = int(slots or 0)
    if slots <= 0:
        raise HIMIResourceDeclError(f"{family}: attach 段的槽数（Dispatch 依据）必须 > 0")
    if base < 0:
        raise HIMIResourceDeclError(f"{family}: attach 段的基址不能为负（{base}）")
    path = str(hlsl_path or "").strip() or family_attach_hlsl_path(family)
    dispatch = max(1, -(-slots // 64))
    return [
        f"[{attach_section_name(family)}]",
        "flags = optimization_level3 all_resources_bound skip_validation",
        f"cs = {path}",
        f"cs-t0 = ref {family_palette_name(family)}",
        attach_range_reference_line(family),  # 【F5】本族身份（base/span 权威来源，见 ATTACH_RANGE_* 段）
        f"cs-u0 = ref {skeleton_name}",
        f"Dispatch = {dispatch}, 1, 1",
        "cs-u0 = null",
    ]


def build_palette_decl(name: str, slot_count: int, filename: str = "") -> HIMIResourceDecl:
    """palette **形状守卫**（HIMI 本期不生成 palette 资源，只把形状固定下来）。"""
    decl = HIMIResourceDecl(
        name=str(name),
        kind="palette",
        ini_type=PALETTE_RESOURCE_INI_TYPE,
        ini_format="",
        array=int(slot_count or 0),
        stride=MERGED_SKELETON_STRIDE,
        bind_flags="",
        filename=str(filename or ""),
        slots=int(slot_count or 0),
    )
    validate_resource_decl(decl)
    return decl


def render_resource_ini_lines(
    decl: HIMIResourceDecl,
    *,
    folder: str | None = None,
) -> list[str]:
    """Render the declaration as INI lines (validated first)."""
    if folder is not None:
        decl = _with_folder(decl, folder)
    validate_resource_decl(decl)
    return decl.ini_lines()


def _with_folder(decl: HIMIResourceDecl, folder: str) -> HIMIResourceDecl:
    """Return a copy of *decl* whose ``filename`` sits in *folder*."""
    from dataclasses import replace

    base = os.path.basename(str(decl.filename or ""))
    if not base:
        return decl
    return replace(decl, filename=f"{str(folder or MERGED_SKELETON_DEFAULT_FOLDER)}/{base}")


def parse_ini_resource_block(lines) -> dict:
    """Parse ``[name]`` + ``key = value`` lines into a plain dict.

    测试用：把「INI 产物」与「HLSL 声明」逐字段比对，而不是靠肉眼。
    """
    name = ""
    fields: dict = {}
    for raw in lines:
        line = str(raw).strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    parsed = {"name": name}
    parsed.update(fields)
    return parsed


def _quote_token(value) -> str:
    """Quote one ``machine_line`` value (shell-compatible)."""
    text = str(value)
    if text and all(character not in text for character in ' \t"\'\\'):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_hlsl_resource_decl(text: str) -> dict:
    """Parse the machine-readable declaration line out of generated HLSL."""
    import shlex

    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("/").strip()
        if not line.startswith("HIMI-RUNTIME-DECL "):
            continue
        try:
            tokens = shlex.split(line[len("HIMI-RUNTIME-DECL ") :])
        except ValueError:  # pragma: no cover - 生成侧永远是可解析的
            tokens = line[len("HIMI-RUNTIME-DECL ") :].split()
        fields = {}
        for token in tokens:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key.strip()] = value.strip()
        return fields
    raise HIMIResourceDeclError("HLSL 里没有 HIMI-RUNTIME-DECL 声明行")


# --------------------------------------------------------------------------- #
# 2) 部件 / 计划
# --------------------------------------------------------------------------- #


@dataclass
class HIMIRebindPart:
    """One exported component that may take part in the merge."""

    bare: str
    draw_ib: str
    unique_str: str = ""
    vg_count: int = 0
    original_vertex_count: int = 0
    family: str = ""
    family_members: tuple = ()
    category_hash: str = ""
    join_path: str = ""
    gate_safe: bool = True
    skeleton_digest: str = ""
    #: 该组件的全局槽值集合（json ``VGMap`` 的值）——决定 attach 段的 ``x1``/``y1``。
    slot_values: tuple = ()

    @property
    def family_key(self) -> str:
        return str(self.family or self.draw_ib or "")

    def to_dict(self) -> dict:
        return {
            "bare": self.bare,
            "draw_ib": self.draw_ib,
            "unique_str": self.unique_str,
            "vg_count": int(self.vg_count),
            "original_vertex_count": int(self.original_vertex_count),
            "family": self.family_key,
            "family_members": list(self.family_members),
            "category_hash": self.category_hash,
            "join_path": self.join_path,
            "gate_safe": bool(self.gate_safe),
            "skeleton_digest": self.skeleton_digest,
            "slot_values": [int(value) for value in self.slot_values],
        }


def part_from_json_payload(
    *, unique_str: str, json_payload: dict, referenced_locals=None
) -> HIMIRebindPart:
    """Build a part record from one landed component json (import-side payload).

    ``slot_values`` = 该组件**被引用的骨槽**（attach 块 ``x1``/``y1`` 的输入，见
    :func:`compute_family_ranges`）。三档来源，按精度降序：

    1. ``referenced_locals`` 显式给了（从工作空间 ``<bare>-Blend.buf`` 读出的「权重 > 0
       的局部槽」）⇒ 取它们经 ``VGMap`` 映出的全局槽；
    2. 否则用 json 自带的 **``BoneMatrixSlotCount``**（数据层写回时落的是「被引用局部槽数」，
       ``common/himi_skeleton.py:2679``）：数据层给**未被引用**的局部槽补的 "gap slots"
       满足 ``值 ≥ max(注册值)+1``（``:899-912``）⇒ **gap 值恒大于全部注册值** ⇒ 把 ``VGMap``
       值升序取前 ``BoneMatrixSlotCount`` 个，就是注册槽集合（精确，且不需要额外读盘）；
    3. 两者都没有（旧缓存 / 合成夹具）⇒ 退回 ``VGMap`` **全量值**（区间只会更大 ⇒ 由
       :func:`compute_family_ranges` 的 fail-closed 判据兜住）。

    为什么必须排除 gap slots：它们**不是**该族的槽位（值落在别的族的区间里或区间外），混进来会让块变大 ⇒ attach 段把本族矩阵写进**别的族**的槽位（互相覆盖 ⇒ 蒙皮错），或者造出假洞被 fail-closed 拒绝。
    实测：真实角色 ``镇×偃月叩晓`` 的 ``2c1f2636`` 三个成员，``VGMap`` 全值集 distinct=156 span=401，而注册槽 = ``[0,148)`` 密集 ✓。
    """
    payload = dict(json_payload or {})
    bare = bare_from_unique_str(str(unique_str or ""))
    draw_ib = str(payload.get("HIMIDeformFamilyIB", "") or "").strip()
    if not draw_ib:
        # 与 common/submesh_metadata.py 同口径：DrawIB = bare 的第一段。
        draw_ib = bare.split("-", 1)[0] if "-" in bare else bare
    members = payload.get("HIMIDeformFamilyMembers")
    if not isinstance(members, (list, tuple)) or not members:
        members = (bare,) if bare else ()
    category_hash = str((payload.get("CategoryHash", {}) or {}).get("Blend", "") or "")
    vg_map = {
        int(key): int(value)
        for key, value in (payload.get("VGMap") or {}).items()
        if int(value) >= 0
    }
    ordered = sorted(set(vg_map.values()))
    if referenced_locals is not None:
        slot_values = {
            vg_map[int(local)] for local in referenced_locals if int(local) in vg_map
        }
    else:
        try:
            registered = int(payload.get("BoneMatrixSlotCount", 0) or 0)
        except (TypeError, ValueError):
            registered = 0
        slot_values = set(ordered[:registered]) if 0 < registered <= len(ordered) else set(ordered)
    return HIMIRebindPart(
        bare=bare,
        draw_ib=draw_ib,
        unique_str=str(unique_str or ""),
        vg_count=int(payload.get("VGCount", 0) or 0),
        original_vertex_count=int(payload.get("OriginalVertexCount", 0) or 0),
        family=draw_ib,
        family_members=tuple(str(m) for m in members),
        category_hash=category_hash,
        join_path=str(payload.get("HIMIDeformJoinPath", "") or ""),
        gate_safe=bool(payload.get("HIMIDeformGateSafe", False)),
        skeleton_digest=str(payload.get("HIMIMergedSkeletonDigest", "") or ""),
        slot_values=tuple(sorted(slot_values)),
    )


def referenced_local_slots(workspace_root: str, unique_str: str):
    """该组件在工作空间里**真正被引用**（权重 > 0）的局部槽（升序元组）；不可得 ⇒ ``None``。

    与 palette 缓存顺序契约（:func:`verify_palette_cache_against_skeleton`）用**同一个**
    数据源、同一个函数（``HIMIBoneMapBuilder.referenced_slots``）。``load_workspace_plan``
    现在不需要它（json 的 ``BoneMatrixSlotCount`` 已经足够，见 :func:`part_from_json_payload`）
    —— 保留供**核对/诊断**用：需要「到底是哪些局部槽」时（而不是只知数量）从这里取，不要另写一份解包。
    """
    json_path = resolve_submesh_json_path(str(workspace_root or ""), str(unique_str or ""))
    if not json_path:
        return None
    bare = bare_from_unique_str(str(unique_str or ""))
    blend_path = os.path.join(os.path.dirname(json_path), f"{bare}-Blend.buf")
    if not os.path.isfile(blend_path):
        return None
    try:
        slots = HIMIBoneMapBuilder.referenced_slots(blend_path).get("slots") or []
    except OSError:
        return None
    return tuple(int(slot) for slot in slots)


@dataclass
class HIMIExportRuntimePlan:
    """What the HIMI exporter needs to know about the merged skeleton.

    ``ok=False`` 表示**没有可用的运行时计划** —— 导出必须退回普通路径，并且
    （当工作空间里确实有 VGMap 数据时）由契约判定大声报错，绝不静默降级。
    """

    ok: bool = False
    message: str = ""
    role_key: str = ""
    slot_count: int = 0
    skeleton_basename: str = ""
    skeleton_source_path: str = ""
    skeleton_digest: str = ""
    parts: list = field(default_factory=list)
    rebinds: list = field(default_factory=list)
    rejected: dict = field(default_factory=dict)
    skip_reasons: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    parts_with_data: int = 0
    source: str = ""
    gate_source: str = ""
    palette_cache: dict = field(default_factory=dict)
    #: 每族 ``{family: {"base", "slots"}}``（attach 段的 x1/y1 来源，见 compute_family_ranges）。
    family_ranges: dict = field(default_factory=dict)
    plan_version: int = HIMI_RUNTIME_PLAN_VERSION

    # -- lookups --------------------------------------------------------- #

    @property
    def rebinds_by_draw_ib(self) -> dict:
        return {str(item["draw_ib"]): item for item in self.rebinds}

    def rebind_for(self, draw_ib: str):
        return self.rebinds_by_draw_ib.get(str(draw_ib or ""))

    @property
    def component_count(self) -> int:
        return len(self.rebinds)

    @property
    def has_rebinds(self) -> bool:
        return bool(self.ok and self.rebinds)

    def rejected_detail(self, draw_ib: str) -> str:
        entry = dict(self.rejected.get(str(draw_ib or "")) or {})
        if not entry:
            return str(self.skip_reasons.get(str(draw_ib or "")) or "")
        return format_deform_gate_rejections({str(draw_ib or ""): entry})

    def gate_rejected_parts(self) -> dict:
        return {
            str(draw_ib): dict(entry)
            for draw_ib, entry in (self.rejected or {}).items()
            if dict(entry).get("missing")
        }

    # -- rendering ------------------------------------------------------- #

    def resource(
        self, folder: str = MERGED_SKELETON_DEFAULT_FOLDER
    ) -> HIMIResourceDecl | None:
        if not self.ok or self.slot_count <= 0:
            return None
        return build_merged_skeleton_decl(self.role_key, self.slot_count, folder=folder)

    def resource_ini_lines(self, folder: str = MERGED_SKELETON_DEFAULT_FOLDER) -> list:
        decl = self.resource(folder)
        return decl.ini_lines() if decl is not None else []

    def attach_hlsl(self, folder: str = MERGED_SKELETON_DEFAULT_FOLDER) -> str:
        decl = self.resource(folder)
        return render_attach_hlsl(decl) if decl is not None else ""

    def contract_snapshot(self, *, checkbox_enabled: bool) -> dict:
        """Arguments for ``evaluate_merged_skeleton_contract``.

        ``diagnostics`` 一并传入：真实原因（子集/覆盖性/stale/门控）都写在诊断里，
        不接进提示词就会出现「没有任何理由的中止」（2026-09 实报）。
        """
        return {
            "checkbox_enabled": bool(checkbox_enabled),
            "parts_with_data": int(self.parts_with_data),
            "component_count": int(self.component_count),
            "skip_reasons": dict(self.skip_reasons or {}),
            "gate_rejected_parts": self.gate_rejected_parts(),
            "diagnostics": list(self.diagnostics or []),
        }

    def diagnostics_text(self) -> str:
        return "\n".join(str(line) for line in (self.diagnostics or []) if str(line).strip())

    def uncovered_slot_ranges(self) -> list:
        """本计划**没有写方**的骨架槽位区间（子集导出时非空）。

        全量导出必须为空（``∑span == slot_count`` 由 :func:`compute_family_ranges`
        的布局/覆盖层保证）；子集导出时它列出「本次没有 attach 写方」的槽位，
        引用它们的顶点会读到未写入矩阵。
        """
        return uncovered_slot_ranges(self.family_ranges, int(self.slot_count or 0))

    def to_dict(self) -> dict:
        """JSON-friendly snapshot (tests / probe / user-facing report)."""
        return {
            "PlanVersion": self.plan_version,
            "Ok": bool(self.ok),
            "Message": self.message,
            "Source": self.source,
            "RoleKey": self.role_key,
            "SlotCount": int(self.slot_count),
            "SkeletonFile": self.skeleton_basename,
            "SkeletonDigest": self.skeleton_digest,
            "PartsWithData": int(self.parts_with_data),
            "ComponentCount": int(self.component_count),
            "GateSource": self.gate_source,
            "FamilyRanges": {
                str(family): {
                    "base": int(info["base"]),
                    "slots": int(info["slots"]),
                    "own_block": bool(info.get("own_block", True)),
                }
                for family, info in (self.family_ranges or {}).items()
            },
            "PaletteCache": {
                str(key): dict(value) for key, value in (self.palette_cache or {}).items()
            },
            "Rebinds": [dict(item) for item in self.rebinds],
            "Rejected": {str(k): dict(v) for k, v in (self.rejected or {}).items()},
            "SkipReasons": {str(k): str(v) for k, v in (self.skip_reasons or {}).items()},
            "Diagnostics": list(self.diagnostics),
            "UnverifiedInGame": list(UNVERIFIED_IN_GAME),
        }


def _plan_covers_all_landed(plan: HIMIExportRuntimePlan, landed_data_bares) -> bool:
    """本次导出的部件是否覆盖工作空间里**全部**已落盘带数据部件。

    覆盖性决定骨架的覆盖判据（``compute_family_ranges(complete=...)``）**是否可判**：
    只有全集在场时，「骨架每个槽都有写方」才是一个能证伪的命题。判据与
    :func:`prepare_gate_oracle` 的 ``covers_all``、t20 的子集语义同源（同一个事实：
    *导出的部件集合 vs 工作空间已落盘集合*）。

    ``landed_data_bares is None`` ⇒ 调用方不掌握全集（纯合成/直接调用路径）⇒ 按
    **全量语义**处理：宁可在布局上 fail-closed，也不默认放行未知集合。
    """
    if landed_data_bares is None:
        return True
    landed = {str(name) for name in landed_data_bares if str(name).strip()}
    if not landed:
        return True
    return landed <= {str(part.bare) for part in (plan.parts or ())}


def _subset_coverage_note(ranges, slot_count: int) -> str:
    """子集导出时的覆盖诊断（空缺槽位 = 本次导出里没有 attach 写方）。"""
    gaps = uncovered_slot_ranges(ranges, slot_count)
    if not gaps:
        return ""
    total = int(slot_count or 0)
    unwritten = sum(end - start for start, end in gaps)
    listed = "、".join(f"[{start},{end})" for start, end in gaps[:6])
    if len(gaps) > 6:
        listed += f"…（共 {len(gaps)} 段）"
    return (
        f"子集导出的骨架覆盖：本次只写 {total - unwritten}/{total} 个槽，"
        f"未覆盖 {listed} —— 这些槽在本次导出里**没有 attach 写方**，引用它们的顶点会读到"
        "未写入矩阵（顶点塌到原点/错位）；把相关 IB 家族一起导出可避免。"
    )


def plan_from_metadata(
    metadata: dict,
    parts,
    *,
    target_bares=None,
    checkbox_enabled: bool = True,
    skeleton_source_path: str = "",
    skeleton_bytes: bytes | None = None,
    source: str = "workspace-cache",
    gate_oracle: dict | None = None,
    landed_data_bares=None,
) -> HIMIExportRuntimePlan:
    """Pure core: metadata + parts → runtime plan（不碰磁盘）。

    ``target_bares`` = 本次导出**真正可以合并**的部件（带合并数据、且合并数据与当前
    角色级骨架同源）；它同时是 G1 的**目标集合**（``HIMISkeletonMergeHelper.prepare
    (target_bares=...)`` 的入参）。缺省（``None``）= 不做集合裁剪（导入侧口径）。

    ``gate_oracle`` = 权威 G1 判定（由 :func:`prepare_gate_oracle` 从数据层的
    ``prepare()`` 归一化而来；测试可注入）。缺失/陈旧时退回导入期缓存口径，
    并且**只**在「目标集合覆盖了全部已落盘带数据部件」时才接受该缓存判定。
    ``landed_data_bares`` = 工作空间里全部已落盘且带合并数据的部件（退路用）。
    """
    plan = HIMIExportRuntimePlan(source=str(source or ""))
    metadata = dict(metadata or {})
    role_key = str(metadata.get("RoleKey", "") or "").strip()
    slot_count = int(metadata.get("SlotCount", 0) or 0)
    skeleton_file = str(metadata.get("SkeletonFileName", "") or "").strip()
    digest = str(metadata.get("SkeletonDigest", "") or "").strip()
    plan.role_key = role_key
    plan.slot_count = slot_count
    plan.skeleton_basename = skeleton_file
    plan.skeleton_digest = digest
    plan.skeleton_source_path = str(skeleton_source_path or "")
    plan.parts = [p if isinstance(p, HIMIRebindPart) else HIMIRebindPart(**dict(p)) for p in (parts or [])]
    plan.parts_with_data = int(sum(1 for p in plan.parts if int(p.vg_count or 0) > 0))

    if int(metadata.get("AlgorithmVersion", 0) or 0) != HIMI_VG_MAP_ALGORITHM_VERSION:
        plan.message = (
            "工作空间里的 HIMI 合并骨架元数据版本"
            f"（{metadata.get('AlgorithmVersion')}）与当前实现"
            f"（{HIMI_VG_MAP_ALGORITHM_VERSION}）不符：请重新导入一次。"
        )
        plan.diagnostics.append(plan.message)
        return plan
    if int(metadata.get("GateVersion", 0) or 0) != HIMI_DEFORM_GATE_VERSION:
        plan.message = (
            "工作空间里的 HIMI 变形家族门控版本"
            f"（{metadata.get('GateVersion')}）与当前实现"
            f"（{HIMI_DEFORM_GATE_VERSION}）不符：请重新导入一次。"
        )
        plan.diagnostics.append(plan.message)
        return plan
    if not role_key or slot_count <= 0 or not skeleton_file or not digest:
        plan.message = "HIMI 合并骨架元数据不完整（RoleKey/SlotCount/SkeletonFileName/SkeletonDigest 缺项）"
        plan.diagnostics.append(plan.message)
        return plan

    if not checkbox_enabled:
        # 复选框关闭：绝不消费合并数据（C20 / §4.5「override = None」）。
        plan.ok = False
        plan.message = (
            "HIMI 合并骨架运行时计划未启用：导出时『使用融合统一顶点组』是关闭的。"
        )
        plan.diagnostics.append(plan.message)
        return plan

    # 骨架二进制校验：大小 + 摘要（skeleton_bytes=None 时由调用方保证已校验）。
    if skeleton_bytes is not None:
        expected = slot_count * MERGED_SKELETON_STRIDE
        actual = len(skeleton_bytes)
        if actual != expected:
            plan.message = (
                f"HIMI 角色级骨架字节数不符：期望 {expected}（= {slot_count} 槽 × 48），"
                f"实际 {actual}；拒绝生成合并路径。"
            )
            plan.diagnostics.append(plan.message)
            return plan
        if digest:
            got = hashlib.sha256(skeleton_bytes).hexdigest()
            if got != digest:
                plan.message = (
                    "HIMI 角色级骨架摘要与元数据不符（缓存被改写或损坏）；"
                    f"期望 {digest[:12]}…，实际 {got[:12]}…；拒绝生成合并路径。"
                )
                plan.diagnostics.append(plan.message)
                return plan

    # ---- 资源声明（构造即校验 A1/A2/A3） ---- #
    try:
        decl = build_merged_skeleton_decl(role_key, slot_count)
    except HIMIResourceDeclError as exc:  # pragma: no cover - 只可能在常量被改成非法值时
        plan.message = f"HIMI 合并骨架资源声明非法：{exc}"
        plan.diagnostics.append(plan.message)
        return plan
    plan.diagnostics.append(
        f"资源声明：{decl.name} type={decl.ini_type} format={decl.ini_format} "
        f"array={decl.array}（=3×{slot_count}）bind_flags={decl.bind_flags} "
        f"stride=（不声明）"
    )

    # ---- 陈旧缓存过滤：部件的合并数据必须来自写角色级骨架的那一次导入 ---- #
    data_parts: list = []
    for part in plan.parts:
        if int(part.vg_count or 0) <= 0:
            continue
        if _is_stale_part(part, metadata):
            plan.skip_reasons[part.draw_ib] = (
                "合并数据摘要（HIMIMergedSkeletonDigest "
                f"{part.skeleton_digest[:12]}…）与角色级骨架（{digest[:12]}…）不一致："
                "该部件来自另一次导入，本家族不合并（请重新导入）"
            )
            plan.diagnostics.append(
                f"{part.bare}: {plan.skip_reasons[part.draw_ib]}"
            )
            continue
        data_parts.append(part)

    # ---- G1：**消费**权威判定，绝不在这里另写一份家族/pass 过滤 ---- #
    #
    # 权威 = ``HIMISkeletonMergeHelper.prepare(target_bares=本次导出集合)``（导入侧数据层
    # 的同一个门控实现；T9-F4 之后它的过滤是 **pass 级**的：一个 deform pass 被多个 IB
    # 家族共享时，只要有一个家族不完整，整个 pass 都不可合并）。本模块只做两件事：
    #   ① 主路径：直接用 prepare 返回的 ``writes``（= 通过门控的部件）作为可重绑集合；
    #   ② 退路（dump 被删、prepare 不可用）：沿用导入期缓存的 ``HIMIDeformGateSafe``，
    #      但**必须**本次导出覆盖了全部已落盘带数据部件（否则 pass 级共享无法判定 ⇒
    #      一律不重绑 + 大声诊断）。退路里没有任何家族/pass 推演。
    target = None
    if target_bares is not None:
        target = {str(name).strip() for name in target_bares if str(name).strip()}
    # 目标集合里**同源**（non-stale）的部件才真正可合并；陈旧部件虽然带数据，但它的
    # 顶点组来自另一次导入，把它算进目标会让 G1 放行一个骨架对不上的家族。
    live_bares = {part.bare for part in data_parts}
    effective_target = None if target is None else (target & live_bares)
    if target is not None and effective_target != target:
        plan.diagnostics.append(
            "目标集合里剔除陈旧部件（合并数据与当前骨架不同源）："
            + "、".join(sorted(target - effective_target)[:6])
        )
    # R-T14-1 第二道闸：只采用**自称 pass 级**的 oracle。带 cache-only 证据的 oracle
    # 一律拒绝（附可读诊断），由下面的保守退路接手——绝不把「空 writes」当权威判定。
    adopt_oracle, oracle_refusal = oracle_authority(gate_oracle)
    if gate_oracle and not adopt_oracle:
        plan.diagnostics.append(f"拒绝采用 G1 权威判定：{oracle_refusal}。")
    oracle_used = adopt_oracle
    rejected_all = False
    rebindable: list = []
    if oracle_used:
        plan.gate_source = str(gate_oracle.get("source") or "prepare")
        safe_bares = {str(name) for name in (gate_oracle.get("safe_bares") or ())}
        plan.rejected = {
            str(draw_ib): dict(entry)
            for draw_ib, entry in (gate_oracle.get("rejected") or {}).items()
        }
        rebindable = [part for part in data_parts if part.bare in safe_bares]
        plan.diagnostics.append(
            f"G1 权威判定来自 {plan.gate_source}（pass 级）：放行 {len(safe_bares)} 个部件，"
            f"拒绝 {len(plan.rejected)} 个 deform pass"
        )
        if gate_oracle.get("subset"):
            plan.diagnostics.append(
                "本次是**子集导出**：G1 门控按本次导出集合重算后采用（跳过骨架摘要/槽位数"
                "比对 —— 子集重建的骨架与角色级缓存本就不同）。"
            )
        if not safe_bares:
            # 与 R-T14-1 的陷阱区别开：这里 dump 在场、按本次导出集合真算过 pass 级
            # 门控，结论就是「一个都不放行」。采纳它是对的（退路去重绑才是错的）。
            plan.diagnostics.append(
                "G1 权威判定放行 0 个部件：这是 pass 级结论（dump 在场、按本次导出集合"
                "重算），不是退路失效——本次没有任何部件可以重绑。"
            )
    else:
        plan.gate_source = "cache-only"
        plan.diagnostics.append(
            "G1 无法用 dump 在 pass 级重算"
            + (
                f"（{gate_oracle.get('message')}）"
                if gate_oracle and gate_oracle.get("message")
                else "（未提供权威门控）"
            )
            + "：退回到导入期缓存的 HIMIDeformGateSafe。"
        )
        if landed_data_bares is not None or effective_target is not None:
            landed = (
                set(str(name) for name in landed_data_bares)
                if landed_data_bares is not None
                else {part.bare for part in plan.parts if int(part.vg_count or 0) > 0}
            )
            reference = effective_target if effective_target is not None else landed
            omitted = sorted(str(name) for name in landed if name not in reference)
            if omitted:
                rejected_all = True
                plan.diagnostics.append(
                    f"{DEFORM_GATE_REJECT_PREFIX}（G1）：本次导出只覆盖了部分已落盘部件"
                    "（或因缓存不同源被剔除），而 dump 已不可用、无法在 pass 级重算门控；"
                    "未纳入的部件：" + "、".join(omitted[:6]) + ("…" if len(omitted) > 6 else "")
                    + "。为避免替别人换骨架，本次全部不重绑 vs-t0（请恢复该 FrameAnalysis "
                    "帧后重新导出，或让导出覆盖全部部件）。"
                )
        if not rejected_all:
            rebindable = [part for part in data_parts if bool(part.gate_safe)]
            for part in data_parts:
                if not bool(part.gate_safe):
                    plan.rejected.setdefault(
                        str(part.draw_ib),
                        {
                            "family": str(part.draw_ib),
                            "members": [part.bare],
                            "missing": [],
                        },
                    )

    if plan.rejected:
        plan.diagnostics.append(
            f"{DEFORM_GATE_REJECT_PREFIX}（G1）："
            + format_deform_gate_rejections(plan.rejected)
            + "。这些 deform pass 的 vs-t0 保持原版 palette（整个家族/pass 都不合并）。"
        )

    for part in rebindable:
        if int(part.original_vertex_count or 0) <= 0:
            plan.skip_reasons[part.draw_ib] = (
                "缺少 deform 顶点数（OriginalVertexCount）：无法声明重放 draw 的顶点数"
            )
            continue
        if not part.category_hash:
            plan.skip_reasons[part.draw_ib] = (
                "缺少 CategoryHash.Blend：无法定位该部件的 deform pass VB 段"
            )
            continue
        plan.rebinds.append(
            {
                "bare": part.bare,
                # t18/T5-F2：该键供导出期的「变形 pass 结构」硬门定位部件 json；缺它 ⇒ 门空转。
                "unique_str": part.unique_str,
                "draw_ib": part.draw_ib,
                "family": part.family_key,
                "family_members": [str(m) for m in part.family_members] or [part.bare],
                "category_hash": part.category_hash,
                "vg_count": int(part.vg_count),
                "deform_vertex_count": int(part.original_vertex_count),
                "family_deform_vertex_count": 0,  # 下面按家族汇总（见注释）
                "vs_t0_resource": decl.name,
                "skeleton_file": decl.filename,
                "slot_count": int(slot_count),
                "join_path": part.join_path,
                "gate_safe": True,
            }
        )

    # 家族级 Σ：**t48 起降级为纯诊断字段**（旧实现拿它当 deform 重放 draw 的权威值，
    # t47 实机证伪：同族每成员 OVC 相同 ⇒ Σ 是 N 倍，实测 99475 vs 模组 31066 行）⇒
    # 越界读 Blend（垃圾骨骼索引）+ 写爆角色级 SO 池。
    # 真正的 draw 由 `resolve_deform_draw_count()` 取**模组自己上传的行数**给出。
    family_rows: dict = {}
    for item in plan.rebinds:
        family_rows[str(item["family"])] = family_rows.get(str(item["family"]), 0) + int(
            item["deform_vertex_count"]
        )
    for item in plan.rebinds:
        item["family_deform_vertex_count"] = int(family_rows.get(str(item["family"]), 0))

    # 每族槽区间（attach 段的 x1/y1，t51 §4.2 ②）—— **fail-closed，但只到「抛错」这一层**：
    #
    #  ① 布局层（永远）：族内不密集 / 越界 / 族间重叠 ⇒ :class:`HIMIFamilyRangeError`。
    #     异常**照样冒泡**（与 A/B/C 落盘时的行为一致）：``ui/universal/himi.py`` 的兜底
    #     except 会打印可读原因并退化成「没有运行时计划」。
    #  ② 覆盖层（``complete``）：只有在「本次导出覆盖了全部已落盘带数据部件」时才要求
    #     从 0 起 / 首尾相接 / ∑span == 槽数。按家族局部导出（t20 裁定为合法路径）本来就
    #     只有部分家族在场，骨架其余槽位在本次导出里天然没有写方 ⇒ 必须**明说**（诊断），
    #     但不能中止。
    #
    # 只有「全部部件都没有 VGMap 值」这种旧/合成数据路径才留空并登记诊断（导出侧据此
    # 退回普通路径，不静默生成错段）。
    #
    # ⚠ **已知口径缺口（登记给 t54/t55，不在本任务收口）**：①的异常落到导出器兜底后
    # 只是「打印原因 + 普通导出」，而 local 几何用的是全局骨骼编号 —— 没有可用骨架会整块
    # 不显示。把它升级成「有理由的 Fatal」必须与 ``tests/test_himi_stub_objects.py`` 的
    # 3 个**合成**夹具同批改（那些工作空间自身不自洽：数据层为未引用局部槽补的 gap slots
    # 落在家族区间中间 ⇒ 家族值集带洞），并且要先解决「gap slots 与连续块搬运」的跨层冲突
    # （实测另一真实角色 role 2c1f2636 的家族值集就不密集）。
    if plan.rebinds:
        with_slots = [part for part in plan.parts if getattr(part, "slot_values", ())]
        if not with_slots:
            plan.diagnostics.append(
                "家族槽区间不可得（没有任何部件带 VGMap 值）⇒ 不生成 attach 段；"
                "导出侧必须退回普通路径（不得静默生成错误的 x1/y1）。"
            )
        else:
            complete = _plan_covers_all_landed(plan, landed_data_bares)
            plan.family_ranges = compute_family_ranges(
                plan.parts, int(slot_count), complete=complete
            )
            if not complete:
                note = _subset_coverage_note(plan.family_ranges, int(slot_count))
                if note:
                    plan.diagnostics.append(note)


    plan.rebinds.sort(key=lambda item: (str(item["draw_ib"]), str(item["bare"])))
    plan.ok = True
    plan.message = (
        f"HIMI 合并骨架运行时计划：角色 {role_key}，{slot_count} 槽，"
        f"重绑 {len(plan.rebinds)} 个部件（{len({r['draw_ib'] for r in plan.rebinds})} 个 deform pass），"
        f"G1 拒绝家族 {len(plan.rejected)} 个"
    )
    return plan


def resolve_deform_draw_count(
    family_deform_vertex_count: int, exported_vertex_count: int
) -> dict:
    """deform 重放段 ``draw`` 的顶点数：**权威 = 模组自己上传的行数**（t48）。

    ``drawib_model.draw_number``（= 同产物 ``override_vertex_count``）才是权威：重放把模组
    上传的每一行过一遍，写多会越界读（Blend 越界 = 垃圾骨骼索引）并把角色级 SO 池写爆。
    旧前提「draw = 家族各分片 OVC 之和」已被 t47 实机证伪（同族每成员 OVC 相同 ⇒ N 倍：
    99475 vs 31066 行）⇒ ``exported > 0`` 无条件采用它（Σ 只作 ``note`` 诊断），否则退回
    家族 Σ 兜底。
    完整因果与实机证据见 ``ui/universal/himi.py`` 同名 VB 段方法 docstring。
    """
    exported = int(exported_vertex_count or 0)
    spec = int(family_deform_vertex_count or 0)
    if exported > 0:
        note = ""
        if spec > 0 and spec != exported:
            note = "家族 Σ（**仅诊断**）= %d ≠ 模组行数 %d（Σ 会放大 N 倍）" % (spec, exported)
        return {
            "count": exported,
            "source": "exported",
            "family_deform_vertex_count": spec,
            "note": note,
        }
    if spec > 0:
        return {
            "count": spec,
            "source": "family-sum-fallback",
            "family_deform_vertex_count": spec,
            "note": "模组自身行数不可得（draw_number=0）⇒ 退回家族 Σ（兜底）",
        }
    return {"count": 0, "source": "none", "family_deform_vertex_count": 0, "note": "顶点数不可得"}


# --------------------------------------------------------------------------- #
# 2b) 前置条件核对：mod 的 Blend 是否**真的**带全局骨骼编号
# --------------------------------------------------------------------------- #


def blend_indices_span(elements) -> dict:
    """Locate ``BLENDINDICES`` inside one **Blend** record.

    ``elements`` = the game type's ``D3D11ElementList``（``D3D11Element`` 兼容对象）。
    偏移按元素顺序累加 ``ByteWidth``（与 ``D3D11GameType.CategoryStrideDict`` 同一
    口径）。返回 ``{"offset","width","stride","item_size"}``；``width<=0`` 表示
    ``Category == "Blend"`` 里没有 BLENDINDICES（调用方按「不可核对」处理）。
    """
    offset = 0
    stride = 0
    found = {"offset": 0, "width": 0}
    for element in elements or ():
        if str(getattr(element, "Category", "") or "") != "Blend":
            continue
        width = int(getattr(element, "ByteWidth", 0) or 0)
        if str(getattr(element, "SemanticName", "") or "").upper() == "BLENDINDICES" and width > 0:
            found = {"offset": offset, "width": width}
        offset += width
        stride += width
    return {
        "offset": int(found["offset"]),
        "width": int(found["width"]),
        "stride": int(stride),
        "item_size": 4,
    }


def extract_blend_index_values(payload: bytes, span: dict, *, only_weighted: bool = True) -> set:
    """Distinct BLENDINDICES values in one Blend buffer (32 B/记录：4f + 4i)。

    ``only_weighted=True``（缺省）只取**权重 > 0** 的通道——那才是这个顶点真正引用的
    骨骼；尾部补 0 的通道不参与比较，从而避免「游戏把补位通道填成重复值」被误判成
    「命名空间不同」。某一行若整行权重为 0，则退回该行的全部通道（那种行本身可疑，
    宁可纳入比较）。
    """
    import numpy

    if not payload or not isinstance(span, dict):
        return set()
    stride = int(span.get("stride", 0) or 0)
    offset = int(span.get("offset", 0) or 0)
    width = int(span.get("width", 0) or 0)
    if stride <= 0 or width <= 0 or offset + width > stride:
        return set()
    rows = len(payload) // stride
    if rows <= 0:
        return set()
    record = numpy.frombuffer(payload[: rows * stride], dtype=numpy.uint8).reshape(rows, stride)
    channels = max(1, min(offset, width) // 4)
    indices = numpy.ascontiguousarray(
        record[:, offset : offset + channels * 4]
    ).view(numpy.int32).reshape(rows, channels)
    if not only_weighted:
        return {int(value) for value in indices.reshape(-1).tolist()}
    weights = numpy.ascontiguousarray(record[:, 0 : channels * 4]).view(numpy.float32).reshape(
        rows, channels
    )
    mask = weights > 0.0
    values = indices[mask]
    unweighted_rows = ~mask.any(axis=1)
    if unweighted_rows.any():
        values = numpy.concatenate([values, indices[unweighted_rows].reshape(-1)])
    return {int(value) for value in values.tolist()}


def classify_blend_namespace(
    *, exported_values, local_values, vg_map=None, mapped_values=None
) -> dict:
    """Decide which bone-index namespace the **exported** Blend actually uses.

    * ``global``  —— 导出值集合 == 用导入期 ``VGMap`` 把原局部值映射后的集合
      （重绑 ``vs-t0`` 的前置条件成立）；
    * ``local``   —— 导出值集合 == 原局部值集合**且**该映射不是恒等映射 ⇒ 这次的
      mod Blend 没带全局编号，重绑会让骨骼错位（必须拒绝该家族）；
    * ``indeterminate`` / ``unavailable`` —— 无法判定（例如用户换了几何，导出值集合
      与两者都不等）。**不阻断**，只在日志里说明，与计划书「不假设」一致。

    ``mapped_values`` 允许调用方直接给「已映射」集合（一个 IB 家族的多个部件各有
    自己的 ``VGMap``，局部编号空间互不相通，必须先各自映射再取并集）。
    """
    if exported_values is None or not local_values:
        return {
            "verdict": "unavailable",
            "note": "缺少导出 Blend 或原部件 Blend，无法核对骨骼编号命名空间",
        }
    exported = {int(value) for value in exported_values}
    local = {int(value) for value in local_values}
    if mapped_values is None:
        mapping = {}
        for key, value in dict(vg_map or {}).items():
            try:
                mapping[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        if not mapping:
            return {
                "verdict": "unavailable",
                "note": "工作空间 json 没有 VGMap，无法核对骨骼编号命名空间",
            }
        mapped = {mapping[value] for value in local if value in mapping}
    else:
        mapped = {int(value) for value in mapped_values}
    if exported and exported == mapped:
        return {
            "verdict": "global",
            "note": f"mod Blend 的骨骼编号是全局槽位（{len(exported)} 个不同值）",
        }
    if exported and exported == local and mapped != local:
        return {
            "verdict": "local",
            "note": (
                "mod Blend 里仍然是**局部**骨骼编号（与工作空间原 Blend 逐值相同、"
                "且 VGMap 不是恒等映射）：重绑 vs-t0 会让骨骼错位，"
                "该家族不合并（请用『使用融合统一顶点组』开启的状态重新导入）"
            ),
        }
    return {
        "verdict": "indeterminate",
        "note": (
            "无法判定 mod Blend 的骨骼编号命名空间"
            f"（导出 {len(exported)} 个值 / 原局部 {len(local)} 个值 / 映射后 {len(mapped)} 个值）"
            "；按允许处理（与计划书『不假设』一致）"
        ),
    }


def check_exported_blend_namespace(
    *,
    workspace_root: str,
    unique_strs,
    exported_blend_bytes,
    blend_span: dict,
) -> dict:
    """Best-effort namespace check: exported Blend vs the workspace's original one.

    ``unique_strs`` = 该 DrawIB 的**全部**部件（一个家族的导出 Blend 是各分片顺序拼接，
    每个分片有自己的 ``VGMap`` 与局部编号空间 ⇒ 各自映射后取并集再比较）。

    只读工作空间（``<bare>.json`` 的 ``VGMap`` + ``<bare>-Blend.buf``）。任何缺项都
    降级为 ``unavailable``，绝不因为核对本身失败而阻断导出。
    """
    if not exported_blend_bytes:
        return {"verdict": "unavailable", "note": "没有可核对的导出 Blend 字节"}
    names = [str(name) for name in (unique_strs or []) if str(name).strip()] if not isinstance(
        unique_strs, str
    ) else [str(unique_strs)]
    stride = int((blend_span or {}).get("stride", 0) or 0)
    local_values: set = set()
    mapped_values: set = set()
    considered = 0
    for unique_str in names:
        json_path = resolve_submesh_json_path(workspace_root, unique_str)
        if not json_path:
            continue
        try:
            payload = JsonUtils.LoadFromFile(json_path)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        bare = bare_from_unique_str(unique_str)
        local_path = os.path.join(os.path.dirname(json_path), f"{bare}-Blend.buf")
        if not os.path.isfile(local_path):
            continue
        try:
            size = os.path.getsize(local_path)
        except OSError:
            continue
        if stride <= 0 or size % stride != 0:
            return {
                "verdict": "unavailable",
                "note": (
                    "原部件 Blend 与导出 Blend 的记录步长不一致"
                    f"（{size} 字节不是 {stride} 的整数倍）：布局被数据类型覆盖节点改过？"
                ),
            }
        try:
            with open(local_path, "rb") as handle:
                local_bytes = handle.read()
        except OSError:
            continue
        local = extract_blend_index_values(local_bytes, blend_span)
        local_values |= local
        mapping = {}
        for key, value in dict(payload.get("VGMap") or {}).items():
            try:
                mapping[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        mapped_values |= {mapping[value] for value in local if value in mapping}
        considered += 1
    if considered == 0:
        return {
            "verdict": "unavailable",
            "note": "工作空间里没有可比对的原部件 Blend",
        }
    return classify_blend_namespace(
        exported_values=extract_blend_index_values(exported_blend_bytes, blend_span),
        local_values=local_values,
        mapped_values=mapped_values,
    )


def _unique_str_for_bare(parts, bare: str) -> str:
    """The ``LOD<n>.<bare>`` unique string of one part (needed by file helpers)."""
    for part in parts or ():
        if str(getattr(part, "bare", "")) == str(bare):
            return str(getattr(part, "unique_str", "") or "")
    return ""


def _is_stale_part(part, metadata: dict) -> bool:
    """True when a component's merged data came from a **different** import run."""
    digest = str((metadata or {}).get("SkeletonDigest", "") or "")
    part_digest = str(getattr(part, "skeleton_digest", "") or "")
    return bool(digest and part_digest and part_digest != digest)


# --------------------------------------------------------------------------- #
# 2b) G1 权威门控（**消费**数据层 prepare，不另写过滤）
# --------------------------------------------------------------------------- #


def prepare_gate_oracle(
    workspace_root: str,
    target_bares=None,
    *,
    expected_digest: str = "",
    expected_slot_count: int = 0,
    all_landed_bares=None,
) -> dict:
    """Run the data layer's ``prepare(target_bares=...)`` as the **G1 authority**.

    返回归一化结果 ``{"available","stale","pass_level","subset","cache_only","source",
    "safe_bares","rejected","message","slot_count","digest","cache_intact"}``。

    为什么用 ``prepare()`` 而不是自己算：门控的语义（一个 deform pass 被多个 IB 家族
    共享 ⇒ 有一个家族不完整就整个 pass 不可合并）由数据层实现并测试（T9-F4 / t12）。
    本模块只把它的结论搬过来用，避免两份口径漂移。

    ``expected_digest`` / ``expected_slot_count`` = 工作空间缓存里的角色级骨架身份。
    **只在 ``covers_all`` 成立时才比对**（``all_landed_bares`` 是工作空间里全部已落盘
    带数据部件）：

    * 覆盖全部已落盘部件（或 ``target_bares is None`` = 导入侧全量语义）⇒ 摘要/槽位数
      不一致说明「反查结果与缓存不是同一次导入」，判 ``stale``（导出几何的顶点组来自
      旧缓存，混用两份数据会错位）；
    * **子集导出** ⇒ ``prepare`` 按**本次导出集合**重建骨架，摘要与槽位数**本来就该不同**
      （缓存是全部部件的并集）。此时比对没有比较意义，只采用它按导出集合重算出的
      **G1 门控结论**（``safe_bares`` / ``rejected``），置 ``subset=True``。部件级陈旧性
      由 ``_is_stale_part``（逐部件摘要）与 palette 缓存逐字节核对独立保证，两道都没放松。

    ``all_landed_bares=None`` 且 ``target_bares`` 非 None（无法判定覆盖性）时按**子集**
    处理：宁可采用 G1 门控结论，也不因为「不知道是否全量」而误杀合法的按家族局部导出。

    **fail-safe（R-T14-1）**：``prepare`` 在 dump 目录缺失时会走 **cache-only 捷径**
    （``cache_only=True`` / ``gate_source="cache-only"``）——那条路径只复现工作空间缓存
    里的结论，**没有**按本次目标集合重算 G1，其 ``writes`` 恒为空。若把这种结果当成
    G1 权威，``safe_bares`` 会是空集 ⇒ 调用方的「沿用缓存判定 + 覆盖性检查」保守退路被
    **静默清空**（表现为「权威判定放行 0 个部件」）。

    因此这里**并用**两条判据（缺一不可）把这种结果判为**非权威**：

    * :func:`_plan_declares_no_recompute` —— 数据层**自己声明**没有重算（cache-only
      位或 ``gate_source`` 前缀）；
    * :func:`_plan_pass_level_evidence` —— **正向物证**：dump 目录非空 + 真跑了解析 +
      产出了角色级元数据。

    任一条不满足 ⇒ ``available=False`` + ``pass_level=False`` + 可读 ``message``，
    调用方据此走自己的保守退路。``pass_level`` 同时是给下游的第二道闸：
    ``plan_from_metadata`` 拒绝任何自称非 pass 级的 oracle。
    """
    result = {
        "available": False,
        "stale": False,
        "pass_level": False,
        "subset": False,
        "source": "prepare",
        "safe_bares": [],
        "rejected": {},
        "message": "",
        "slot_count": 0,
        "digest": "",
        "cache_intact": False,
    }
    root = str(workspace_root or "").strip()
    if not root or not os.path.isdir(root):
        result["message"] = f"工作空间不存在：{root}"
        return result
    try:
        from .himi_skeleton import HIMISkeletonMergeHelper

        plan = HIMISkeletonMergeHelper.prepare(root, None, target_bares=target_bares)
    except Exception as exc:  # noqa: BLE001 - 门控不可用绝不阻断导出（退路有兜底）
        result["message"] = f"prepare 异常：{exc}"
        return result
    if not isinstance(plan, dict) or not plan.get("ok"):
        failed = dict(plan or {})
        result["message"] = str(failed.get("message", "prepare 未返回可用计划"))
        return result

    digest = str(plan.get("metadata", {}).get("SkeletonDigest", "") or "")
    slot_count = int(plan.get("slot_count", 0) or 0)
    declared_no_recompute = _plan_declares_no_recompute(plan)
    pass_level = (not declared_no_recompute) and _plan_pass_level_evidence(plan)
    result.update(
        {
            "slot_count": slot_count,
            "digest": digest,
            "cache_intact": bool(plan.get("cache_intact")),
            "safe_bares": sorted(
                str(entry.get("bare", "")) for entry in (plan.get("writes") or [])
            ),
            "rejected": _normalize_gate_rejections(plan.get("gate_rejected") or []),
            "message": str(plan.get("message", "") or ""),
            "pass_level": pass_level,
        }
    )
    if not pass_level:
        result["available"] = False
        if declared_no_recompute:
            reason = (
                "prepare 声明自己没有做 **pass 级重算**（只复现工作空间缓存结论："
                "FrameAnalysis 目录缺失）"
            )
        else:
            reason = (
                "prepare 未给出 pass 级重算的正向证据（dump 目录为空 / 未跑解析 / "
                "无角色级元数据）"
            )
        result["message"] = (
            f"{reason}：该结果的放行集合恒为空，采纳它会把调用方的保守退路静默清空，"
            "故判为非权威。"
            + (f"（{plan.get('message')}）" if plan.get("message") else "")
        )
        return result
    result["available"] = True
    # 覆盖性：只有「目标集合 ⊇ 全部已落盘带数据部件」（或 None = 导入侧全量语义）时，
    # 骨架摘要/槽位数比对才有比较意义。
    covers_all = target_bares is None or (
        all_landed_bares is not None
        and {str(name) for name in target_bares} >= {str(name) for name in all_landed_bares}
    )
    if not covers_all:
        result["subset"] = True
        result["message"] = (
            "子集导出（目标集合未覆盖全部已落盘带数据部件）：采用 prepare 按本次导出集合"
            "重算出的 G1 门控结论，**跳过骨架摘要/槽位数比对**——子集重建出来的骨架本来"
            "就与角色级缓存（全部部件的并集）不同，把它当『不同源』会误杀合法的按家族局部"
            "导出。部件级陈旧性仍由逐部件摘要 + palette 缓存逐字节核对把关。"
            + (f"（{plan.get('message')}）" if plan.get("message") else "")
        )
        return result
    if expected_digest and digest and digest != expected_digest:
        result["stale"] = True
        result["message"] = (
            "dump 反查结果与工作空间缓存不是同一次导入"
            f"（骨架摘要 {digest[:12]}… vs 缓存 {expected_digest[:12]}…）"
        )
    elif expected_slot_count and slot_count and slot_count != expected_slot_count:
        result["stale"] = True
        result["message"] = (
            f"dump 反查的槽位数 {slot_count} 与缓存 {expected_slot_count} 不一致"
        )
    return result


def _plan_declares_no_recompute(plan) -> bool:
    """数据层**自己声明**「本次没有做 pass 级重算」的两个信号（任一成立即认定）。

    * ``cache_only`` 布尔位 —— cache-only 捷径的成功与失败结果都带它；
    * ``gate_source`` 以 ``cache-only`` 开头 —— 三种取值 ``cache-only`` /
      ``cache-only-invalid`` / ``cache-only-unavailable``。

    这是「**声明**」；与 :func:`_plan_pass_level_evidence` 的「**物证**」互为补充：
    只看声明会漏掉未来新增的、忘了打标记的捷径，只看物证则把一个明确的自我声明降级成
    推断。两者一起用（``prepare_gate_oracle`` 里取交集）既不漏也不误采纳。
    """
    if not isinstance(plan, dict):
        return False
    if bool(plan.get("cache_only")):
        return True
    return str(plan.get("gate_source", "") or "").startswith("cache-only")


def _plan_pass_level_evidence(plan) -> bool:
    """True only when a data-layer ``prepare`` result **demonstrably** did a pass-level
    dump-based recomputation —— fail-closed。

    三条必须同时成立（都是「真的跑过一次反查」的正向物证）：

    * ``dump_dir`` 非空 —— 本次反查用的 FrameAnalysis 目录；
    * ``resolver`` 不是 ``None`` —— 真跑了 log 解析与 join；
    * ``metadata`` 非空 —— 产出了角色级元数据（Slots / SkeletonDigest 等）。

    dump 目录缺失时数据层只复现缓存结论：这三条全不满足 ⇒ 它的空放行集合不会被误当成
    G1 权威（R-T14-1 的 fail-safe 要点）。反过来，任何**新的**、未做重算的捷径也会因为
    拿不出正向物证而被拒绝（fail-closed 比「只识别某一种捷径」更稳）。
    """
    if not isinstance(plan, dict):
        return False
    if not str(plan.get("dump_dir", "") or "").strip():
        return False
    if plan.get("resolver") is None:
        return False
    return bool(plan.get("metadata"))


def oracle_authority(gate_oracle) -> tuple:
    """May this oracle be adopted as the **G1 authority**? → ``(adopt, reason)``.

    ``reason`` 面向用户，只在 ``adopt=False`` 时有意义。判据（按顺序）：

    1. 没有 oracle / 不是 dict ⇒ 不采用；
    2. ``stale`` ⇒ 不采用（反查结果与工作空间缓存不同源）；
    3. **自称非 pass 级**（``pass_level is False``）或带「没有重算」的证据
       （``cache_only`` 位 / ``gate_source`` 以 ``cache-only`` 开头）⇒ 不采用。
       这是 R-T14-1 的第二道闸：即便有人绕过 :func:`prepare_gate_oracle` 手搓一个
       oracle，只要它带着「未重算」的证据，就不会被当成权威判定；缺这些字段时按旧行为
       采纳，保持对外来/注入 oracle 的兼容；
    4. ``available`` 为假 ⇒ 不采用（附它的 message）。
    """
    if not isinstance(gate_oracle, dict) or not gate_oracle:
        return False, "未提供权威门控"
    if gate_oracle.get("stale"):
        return False, "权威门控结果与工作空间缓存不同源（stale）"
    no_recompute_evidence = _plan_declares_no_recompute(gate_oracle)
    if gate_oracle.get("pass_level") is False or no_recompute_evidence:
        return False, (
            "权威门控自称没有做 pass 级重算（只复现工作空间缓存结论）：它的放行集合恒为空，"
            "采纳它会把保守退路静默清空"
        )
    if not gate_oracle.get("available"):
        return False, str(gate_oracle.get("message") or "权威门控不可用")
    return True, ""


def _normalize_gate_rejections(gate_rejected) -> dict:
    """``prepare`` 的 ``gate_rejected`` 列表 → ``{draw_ib: {family,members,missing}}``。

    只做**搬运**：不推断、不补充 pass/家族关系（那是数据层的事）。
    """
    rejected: dict = {}
    for entry in gate_rejected or ():
        if not isinstance(entry, dict):
            continue
        draw_ib = str(entry.get("draw_ib", "") or entry.get("bare", "") or "?")
        item = rejected.setdefault(
            draw_ib, {"family": draw_ib, "members": [], "missing": []}
        )
        item["members"] = sorted({*item["members"], str(entry.get("bare", ""))} - {""})
        item["missing"] = sorted(
            {*item["missing"], *(str(name) for name in (entry.get("missing") or ()))}
        )
        reason = str(entry.get("reason", "") or "")
        if reason:
            item["reason"] = reason
    return rejected


# --------------------------------------------------------------------------- #
# 2c) 部件级 palette 缓存（ModImpRuntime/<bare>-BoneMatrix.buf）
# --------------------------------------------------------------------------- #


def read_palette_cache(path: str) -> dict:
    """Read ``ModImpRuntime/<bare>-BoneMatrix.buf`` —— **按引用序压缩的槽表**。

    写出侧（``common/himi_skeleton.py::HIMISkeletonMergeManager.prepare``，见
    ``cache_bytes`` 的构造）只保留「被引用的局部槽」，按**局部下标升序**拼接，
    每槽 48 字节。所以：

    * 行 ``j`` = 第 ``j`` 小的被引用局部槽的矩阵（**不是**「下标 ``i`` 在偏移
      ``i*48``」的池镜像）；
    * 想把第 ``j`` 行对回全局槽位，必须先用 ``VGMap`` 把「第 j 小的被引用局部槽」
      映射成全局槽位，再去角色级骨架里取——顺序契约见
      :func:`verify_palette_cache_against_skeleton`。
    """
    with open(str(path), "rb") as handle:
        payload = handle.read()
    if not payload or len(payload) % PALETTE_STRIDE_BYTES:
        return {"ok": False, "rows": [], "row_count": 0, "message": "尺寸不是 48 的整数倍"}
    rows = [
        payload[offset : offset + PALETTE_STRIDE_BYTES]
        for offset in range(0, len(payload), PALETTE_STRIDE_BYTES)
    ]
    return {"ok": True, "rows": rows, "row_count": len(rows), "message": ""}


def verify_palette_cache_against_skeleton(
    *,
    workspace_root: str,
    unique_str: str,
    json_payload: dict,
    skeleton_bytes,
    slot_count: int,
) -> dict:
    """核对「部件 palette 缓存」与「角色级骨架」逐字节一致（= 计划书 §7.3 A2）。

    这一步同时把**顺序契约**钉死：被引用局部槽按升序取自工作空间里那份原部件
    Blend（与写出侧同一输入、同一算法），第 ``j`` 行必须等于角色级骨架第
    ``VGMap[slots[j]]`` 槽的三行。任何「按池镜像读」或「按全局槽升序重排」的读法
    都会在这里对不上。

    返回 ``{"ok","verdict","checked","rows","reason","mismatches"}``。

    ``verdict`` 三态：``verified``（逐字节一致）/ ``mismatch``（缓存存在但与骨架
    不一致 ⇒ 调用方必须保守排除该部件）/ ``unavailable``（缓存或原 Blend 缺失、
    格式无效 ⇒ **无法核对**，不据此排除，只留诊断）。缺项与「不一致」必须分开：
    旧工作空间没有这个缓存不该让导出失效，但「有缓存且对不上」一定是错的。
    """
    result = {
        "ok": False,
        "verdict": "unavailable",
        "checked": 0,
        "rows": 0,
        "reason": "",
        "mismatches": [],
    }
    json_path = resolve_submesh_json_path(workspace_root, unique_str)
    if not json_path:
        result["reason"] = "找不到该部件的子网格 json"
        return result
    payload = dict(json_payload or {})
    bare = bare_from_unique_str(unique_str)
    json_dir = os.path.dirname(json_path)
    blend_path = os.path.join(json_dir, f"{bare}-Blend.buf")
    if not os.path.isfile(blend_path):
        result["reason"] = f"工作空间里没有原部件 Blend（{bare}-Blend.buf），无法核对缓存顺序"
        return result
    cache_name = str(payload.get("BoneMatrixFileName", "") or "") or f"{bare}-BoneMatrix.buf"
    cache_path = runtime_cache_path(json_path, bare, cache_name)
    if not os.path.isfile(cache_path):
        result["reason"] = f"部件 palette 缓存缺失（{cache_name}），无法核对"
        return result
    try:
        local_slots = list(
            HIMIBoneMapBuilder.referenced_slots(blend_path).get("slots") or []
        )
    except OSError as exc:
        result["reason"] = f"原部件 Blend 读取失败：{exc}"
        return result
    vg_map = dict(payload.get("VGMap") or {})
    cache = read_palette_cache(cache_path)
    if not cache.get("ok"):
        result["reason"] = f"部件 palette 缓存格式无效：{cache.get('message')}"
        return result
    rows = list(cache.get("rows") or [])
    result["rows"] = len(rows)
    if len(rows) != len(local_slots):
        result["verdict"] = "mismatch"
        result["reason"] = (
            f"缓存行数 {len(rows)} != 被引用槽数 {len(local_slots)}"
            "（顺序契约按『被引用局部槽升序』；按池镜像读会在这里对不上）"
        )
        return result
    for index, local in enumerate(local_slots):
        try:
            global_slot = int(vg_map[str(int(local))])
        except (KeyError, TypeError, ValueError):
            result["verdict"] = "mismatch"
            result["reason"] = f"VGMap 没有覆盖被引用局部槽 {local}"
            return result
        if global_slot < 0 or global_slot >= int(slot_count):
            result["verdict"] = "mismatch"
            result["reason"] = (
                f"VGMap[{local}] = {global_slot} 越出角色级骨架（{slot_count} 槽）"
            )
            return result
        expected = skeleton_bytes[
            global_slot * PALETTE_STRIDE_BYTES : (global_slot + 1) * PALETTE_STRIDE_BYTES
        ]
        if rows[index] != expected:
            result["mismatches"].append(
                {
                    "row": index,
                    "local_slot": int(local),
                    "global_slot": global_slot,
                }
            )
    result["checked"] = len(local_slots)
    if result["mismatches"]:
        first = result["mismatches"][0]
        result["verdict"] = "mismatch"
        result["reason"] = (
            f"缓存第 {first['row']} 行（局部槽 {first['local_slot']} → 全局槽 "
            f"{first['global_slot']}）与角色级骨架逐字节不一致"
            f"（共 {len(result['mismatches'])} 行不符）"
        )
        return result
    result["ok"] = True
    result["verdict"] = "verified"
    return result


# --------------------------------------------------------------------------- #
# 3) 工作空间读取（cache-only：dump 删了也能导出）
# --------------------------------------------------------------------------- #


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_unique_strs(workspace_root: str) -> list:
    """All ``LOD<n>.<bare>`` unique strings with a resolvable submesh json."""
    root = str(workspace_root or "").strip()
    found: list = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found
    for lod_name in entries:
        if not (lod_name.startswith("LOD") and lod_name[3:].isdigit()):
            continue
        lod_dir = os.path.join(root, lod_name)
        if not os.path.isdir(lod_dir):
            continue
        try:
            bares = sorted(os.listdir(lod_dir))
        except OSError:
            continue
        for bare in bares:
            if not os.path.isdir(os.path.join(lod_dir, bare)):
                continue
            unique_str = f"{lod_name}.{bare}"
            if resolve_submesh_json_path(root, unique_str):
                found.append(unique_str)
    return found


def _runtime_plan_shell(
    metadata: dict, parts, *, message: str, source: str = ""
) -> HIMIExportRuntimePlan:
    """A **not ok** plan that still reports the shape needed by the contract check.

    ``message`` 里的原因已经足够用户处置；``parts_with_data`` / ``component_count``
    必须照实填，否则「有数据但没有可用运行时计划」会退化成静默的普通导出。
    """
    metadata = dict(metadata or {})
    shell = HIMIExportRuntimePlan(ok=False, message=str(message), source=str(source or ""))
    shell.role_key = str(metadata.get("RoleKey", "") or "")
    shell.slot_count = int(metadata.get("SlotCount", 0) or 0)
    shell.skeleton_basename = str(metadata.get("SkeletonFileName", "") or "")
    shell.skeleton_digest = str(metadata.get("SkeletonDigest", "") or "")
    shell.parts = list(parts or [])
    shell.parts_with_data = int(
        sum(1 for part in shell.parts if int(getattr(part, "vg_count", 0) or 0) > 0)
    )
    return shell


def load_workspace_plan(
    workspace_root: str,
    *,
    export_unique_strs=None,
    export_bares=None,
    checkbox_enabled: bool = True,
    source: str = "workspace-cache",
    gate_oracle=None,
    enable_gate_oracle: bool = True,
) -> HIMIExportRuntimePlan:
    """Build the plan from the workspace cache written by the import side.

    三件事：① 读 ``Config/HIMIMergedSkeleton.json`` + ``Config/<role>-MergedSkeleton.buf``
    + 各组件 json（**cache 是几何顶点组与骨架的共同来源**，两者必须来自同一次导入）；
    ② 用数据层的 ``prepare(target_bares=本次导出集合)`` 作为 G1 的**权威 pass 级判定**
    （``enable_gate_oracle=False`` 或 dump 不可用时退回导入期缓存口径）；
    ③ 归一化成一个 :class:`HIMIExportRuntimePlan`。
    """
    plan = HIMIExportRuntimePlan(source=str(source or ""))
    root = str(workspace_root or "").strip()
    if not root or not os.path.isdir(root):
        plan.message = f"HIMI 工作空间不存在：{root}"
        plan.diagnostics.append(plan.message)
        return plan

    metadata_path = os.path.join(root, "Config", HIMI_MERGED_SKELETON_METADATA_FILE)
    if not os.path.isfile(metadata_path):
        plan.message = (
            "工作空间里没有 HIMI 合并骨架元数据"
            f"（Config/{HIMI_MERGED_SKELETON_METADATA_FILE}）：本次按普通导出处理。"
        )
        return plan

    try:
        metadata = JsonUtils.LoadFromFile(metadata_path)
    except Exception as exc:  # noqa: BLE001 - 读取失败绝不阻断导出
        plan.message = f"HIMI 合并骨架元数据读取失败：{exc}"
        plan.diagnostics.append(plan.message)
        return plan
    if not isinstance(metadata, dict):
        plan.message = "HIMI 合并骨架元数据不是 json 对象"
        plan.diagnostics.append(plan.message)
        return plan

    # 组件：显式列表优先（导出侧传 submesh 的 unique_str），否则扫 LOD 目录。
    unique_strs = [str(u) for u in (export_unique_strs or []) if str(u).strip()]
    if not unique_strs:
        unique_strs = _discover_unique_strs(root)
    parts: list = []
    for unique_str in sorted(set(unique_strs)):
        payload = _load_component_payload(root, unique_str, plan)
        if payload:
            # 精确口径由 `part_from_json_payload` 内部从 json 的 `BoneMatrixSlotCount` 取
            # （= 被引用局部槽数 ⇒ 排除 gap slots），不需要在这里读盘。
            parts.append(part_from_json_payload(unique_str=unique_str, json_payload=payload))
    # ---- G1 目标集合：本次导出里**合并数据与当前骨架同源**的部件 ---- #
    #
    # 「同源」= 该部件 json 记的 HIMIMergedSkeletonDigest 与角色级骨架一致（字段缺失
    # 时按同源处理，兼容早期缓存）。它必须在这里定下来：陈旧部件虽然带数据，但它的
    # 顶点组来自**另一次导入**，把它当目标会让 G1 放行一个骨架对不上的家族。
    export_set = (
        None
        if export_bares is None
        else {str(name).strip() for name in export_bares if str(name).strip()}
    )
    candidate_bares = [
        part.bare
        for part in parts
        if int(part.vg_count or 0) > 0 and not _is_stale_part(part, metadata)
    ]
    target_bares = (
        candidate_bares
        if export_set is None
        else [bare for bare in candidate_bares if bare in export_set]
    )

    skeleton_file = str(metadata.get("SkeletonFileName", "") or "").strip()
    slot_count = int(metadata.get("SlotCount", 0) or 0)
    skeleton_source = os.path.join(root, "Config", skeleton_file) if skeleton_file else ""
    skeleton_bytes = None
    skeleton_missing = ""
    if skeleton_source and os.path.isfile(skeleton_source):
        try:
            with open(skeleton_source, "rb") as handle:
                skeleton_bytes = handle.read()
        except OSError as exc:
            skeleton_missing = f"骨架二进制读取失败：{exc}"
    elif skeleton_file:
        skeleton_missing = f"骨架二进制缺失：{os.path.join('Config', skeleton_file)}"

    if skeleton_missing:
        # 声明了资源却没有文件 ⇒ 加载期资源创建失败、deform 读全零。绝不假装计划可用：
        # ``parts_with_data`` 仍然照实报告，好让契约判定能把它升级成「中止导出」。
        failing = _runtime_plan_shell(
            metadata, parts, message=f"{skeleton_missing}；本次不生成合并骨架路径", source=source
        )
        failing.diagnostics.append(failing.message)
        return failing

    # ---- 部件 palette 缓存核对（顺序契约 + 逐字节同构，计划书 §7.3 A2） ---- #
    #
    # ModImpRuntime/<bare>-BoneMatrix.buf 是**按引用序压缩**的槽表；对回全局槽位必须
    # 走 VGMap。核对只针对本次真正可能重绑的部件（target 候选），成本有界。
    palette_cache: dict = {}
    verified_targets: list = []
    for unique_str in sorted(
        {_unique_str_for_bare(parts, bare) for bare in target_bares} - {""}
    ):
        payload = _load_component_payload(root, unique_str, None)
        if not payload:
            continue
        bare = bare_from_unique_str(unique_str)
        if int(payload.get("VGCount", 0) or 0) <= 0:
            continue
        check = verify_palette_cache_against_skeleton(
            workspace_root=root,
            unique_str=unique_str,
            json_payload=payload,
            skeleton_bytes=skeleton_bytes,
            slot_count=slot_count,
        )
        palette_cache[bare] = check
        if check.get("verdict") == "verified":
            verified_targets.append(bare)
        elif check.get("verdict") == "mismatch":
            plan.diagnostics.append(
                f"{bare}: 部件 palette 缓存与角色级骨架不一致 ⇒ 本家族不合并"
                f"（{check.get('reason')}）；请重新导入。"
            )
        else:
            # 无法核对（缓存/原 Blend 缺失、格式无效）：不据此排除，只留诊断。
            verified_targets.append(bare)
            plan.diagnostics.append(
                f"{bare}: 未能核对部件 palette 缓存顺序（{check.get('reason')}）。"
            )
    if palette_cache:
        mismatched = sorted(
            bare for bare, item in palette_cache.items() if item.get("verdict") == "mismatch"
        )
        plan.diagnostics.append(
            f"部件 palette 缓存核对：{sum(1 for item in palette_cache.values() if item.get('ok'))}"
            f"/{len(palette_cache)} 逐字节一致"
            + (f"；不一致：{'、'.join(mismatched[:6])}" if mismatched else "")
        )
        target_bares = [bare for bare in target_bares if bare in set(verified_targets)]

    # 「工作空间里全部已落盘且带合并数据的部件」：既用于判定「本次是否覆盖全部」
    # （决定骨架摘要比对是否适用 —— 子集导出不该被判 stale），也用于保守退路的覆盖性检查。
    landed_data_bares = set()
    for unique_str in _discover_unique_strs(root):
        payload = _load_component_payload(root, unique_str, None)
        if payload and int(payload.get("VGCount", 0) or 0) > 0:
            landed_data_bares.add(bare_from_unique_str(unique_str))

    # ---- G1 权威判定（数据层 prepare） ---- #
    oracle = gate_oracle
    if oracle is None and enable_gate_oracle:
        oracle = prepare_gate_oracle(
            root,
            target_bares=target_bares,
            expected_digest=str(metadata.get("SkeletonDigest", "") or ""),
            expected_slot_count=slot_count,
            all_landed_bares=sorted(landed_data_bares),
        )
    elif not isinstance(oracle, dict):
        oracle = None

    built = plan_from_metadata(
        metadata,
        parts,
        target_bares=target_bares,
        checkbox_enabled=checkbox_enabled,
        skeleton_source_path=skeleton_source,
        skeleton_bytes=skeleton_bytes,
        source=source,
        gate_oracle=oracle,
        landed_data_bares=landed_data_bares or None,
    )
    built.palette_cache = dict(palette_cache)
    # 角色键一致性（元数据被搬进别的角色工作空间时大声报出来）
    expected_role_key = derive_role_key(os.path.basename(os.path.abspath(root)))
    if built.ok and built.role_key and built.role_key != expected_role_key:
        built.diagnostics.append(
            f"元数据 RoleKey={built.role_key} 与工作空间名推导出的 {expected_role_key} "
            "不一致：骨架文件可能来自另一个角色。"
        )
    for line in plan.diagnostics:
        built.diagnostics.append(line)
    return built


def _load_component_payload(root: str, unique_str: str, plan) -> dict:
    """Read one landed component json; ``plan=None`` 表示只读不要诊断。"""
    json_path = resolve_submesh_json_path(root, unique_str)
    if not json_path:
        if plan is not None:
            plan.diagnostics.append(f"跳过 {unique_str}：找不到子网格 json")
        return {}
    try:
        payload = JsonUtils.LoadFromFile(json_path)
    except Exception as exc:  # noqa: BLE001
        if plan is not None:
            plan.diagnostics.append(f"跳过 {unique_str}：json 读取失败（{exc}）")
        return {}
    if not isinstance(payload, dict) or not payload:
        if plan is not None:
            plan.diagnostics.append(f"跳过 {unique_str}：json 为空")
        return {}
    return payload


# --------------------------------------------------------------------------- #
# 4) 骨架二进制发布（mod 侧）
# --------------------------------------------------------------------------- #


def publish_skeleton_binary(
    plan: HIMIExportRuntimePlan,
    folder_path: str,
    *,
    basename: str = "",
) -> dict:
    """Copy the role skeleton into the mod folder, atomically and idempotently.

    Returns ``{"ok", "path", "written", "bytes", "message"}``.  Never raises for
    expected conditions -- the caller keeps exporting either way (a missing
    skeleton must surface through the contract, not through a traceback).
    """
    name = str(basename or plan.skeleton_basename or "").strip()
    if not plan.ok:
        return {"ok": False, "path": "", "written": False, "bytes": 0, "message": plan.message}
    if not name:
        return {"ok": False, "path": "", "written": False, "bytes": 0, "message": "骨架文件名缺失"}
    source = str(plan.skeleton_source_path or "")
    if not source or not os.path.isfile(source):
        message = f"角色级骨架二进制不存在：{source}"
        return {"ok": False, "path": "", "written": False, "bytes": 0, "message": message}
    try:
        with open(source, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        return {"ok": False, "path": "", "written": False, "bytes": 0, "message": str(exc)}

    expected = int(plan.slot_count) * MERGED_SKELETON_STRIDE
    if len(payload) != expected:
        return {
            "ok": False,
            "path": "",
            "written": False,
            "bytes": len(payload),
            "message": (
                f"角色级骨架字节数不符：期望 {expected}（= {plan.slot_count} 槽 × 48），"
                f"实际 {len(payload)}"
            ),
        }
    if plan.skeleton_digest and _sha256_file(source) != plan.skeleton_digest:
        return {
            "ok": False,
            "path": "",
            "written": False,
            "bytes": len(payload),
            "message": "角色级骨架摘要与元数据不符（缓存被改写？）",
        }

    folder = str(folder_path or "")
    if not folder:
        return {
            "ok": False,
            "path": "",
            "written": False,
            "bytes": 0,
            "message": "mod 缓冲目录为空（BlueprintExportHelper.current_buffer_folder_name 未设置）",
        }
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "path": "", "written": False, "bytes": 0, "message": str(exc)}
    destination = os.path.join(folder, name)
    # 幂等：内容一致就不动文件（多轮导出 / 直出多轮都会调用这里）。
    if os.path.isfile(destination):
        try:
            with open(destination, "rb") as handle:
                if handle.read() == payload:
                    return {
                        "ok": True,
                        "path": destination,
                        "written": False,
                        "bytes": len(payload),
                        "message": "骨架二进制已是最新（内容一致，跳过写入）",
                    }
        except OSError:
            pass

    descriptor = -1
    temp_path = ""
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=folder
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        temp_path = ""
    except OSError as exc:
        return {"ok": False, "path": "", "written": False, "bytes": 0, "message": str(exc)}
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temp_path and os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return {
        "ok": True,
        "path": destination,
        "written": True,
        "bytes": len(payload),
        "message": f"已写出角色级骨架（{plan.slot_count} 槽 / {len(payload)} 字节）",
    }


# --------------------------------------------------------------------------- #
# 5) HLSL：逐帧 attach CS（与 INI 同一份 decl）
# --------------------------------------------------------------------------- #

_HLSL_HEADER = "// {0} —— HIMI（崩坏3 / HI3）合并骨架 attach CS（冻结的规范副本）"
_HLSL_GENERATED = "// 由 common/himi_merged_runtime.py::render_attach_hlsl() 生成；请勿手改。"


def render_attach_hlsl(
    decl: HIMIResourceDecl,
    *,
    folder: str | None = None,
) -> str:
    """Render the per-frame attach CS as HLSL text.

    **t57 起这份文本是真正可挂载的 attach CS**（此前是「声明与索引参考」）：导出侧把它
    按 :func:`family_attach_hlsl_path` 复制成**每族一个文件**（``<mod>/res/himi_attach_<fam>.hlsl``），
    只把基址行改写成该族的全局起始槽（:func:`render_family_attach_base_line`），正文逐字不变。

    抬头写的是真实机制链（① deform 段捕获当帧 palette → ② 本 CS 把副本块搬运进合并骨架区间
    → ③ 之后才 ``vs-t0`` 重绑），正文是 ``[numthreads(64, 1, 1)]`` + ``main`` 的块搬运：
    槽数取**资源自身维度**（``GetDimensions`` 单参后 ``/3u`` 还原）、下标一律 ``*3u``、
    **零 ``IniParams`` 读参**（fork 相关 ⇒ 读错位置只会读到未设置的 0 ⇒ CS 静默不写）。

    渲染结果与随仓库冻结的 ``Toolset/himi_merged_skeleton_attach.hlsl`` **逐字节一致**
    （``tests/test_himi_merged_runtime.py::HIMIIniAndHlslAgreementTests`` 逐字节比对）。
    """
    if folder is not None:
        decl = _with_folder(decl, folder)
    validate_resource_decl(decl)
    if decl.kind != "merged_skeleton":
        raise HIMIResourceDeclError("只有 merged_skeleton 形态能渲染 attach CS")

    lines: list = []
    lines.append(_HLSL_HEADER.format(decl.name))
    lines.append(_HLSL_GENERATED)
    lines.append("//")
    lines.append("// 【这份文件是什么】它是 HIMI 的**逐帧 attach CS**：导出侧为每个 deform 家族复制一份到")
    lines.append("// <mod>/res/himi_attach_<fam>.hlsl，只把基址行改写成该族在合并骨架里的全局起始槽")
    lines.append("// （#define HIMI_ATTACH_BASE <base>），CS 正文逐字不变。每个")
    lines.append("// [CustomShaderHI3MergedSkeletonAttach_<fam>] 段引用自己那一份：")
    lines.append(f"//   cs = {family_attach_hlsl_path('<fam>')} / cs-t0 = 该族当帧副本 / cs-u0 = 合并骨架")
    lines.append("//   Dispatch = ceil(该族槽数 / 64)（与下方 numthreads(64, 1, 1) 匹配），段末 cs-u0 = null 解绑。")
    lines.append("//")
    lines.append("// 【机制链（逐帧，顺序不可换）】")
    lines.append("//   ① deform VB 段（[TextureOverride_VB_<fam>_<fam>_Blend]）先捕获**当帧** palette：")
    lines.append("//        ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null      ← LHS 是**目标**")
    lines.append("//   ② run = CustomShaderHI3MergedSkeletonAttach_<fam> → 本 CS 把该副本块搬运进合并骨架的")
    lines.append("//        [HIMI_ATTACH_BASE, HIMI_ATTACH_BASE + 该族槽数) 区间；")
    lines.append("//   ③ 之后才把 vs-t0 换绑成合并骨架（供游戏 deform VS 读取），最后 handling = skip + draw。")
    lines.append("//   run 必须排在 `vs-t0 = <合并骨架>` **之前**：反序 = 自反馈（把刚绑上去的骨架当成当帧")
    lines.append("//   palette 捕获）；copy 的方向写反 = 把空副本写进游戏的当帧 palette。两处都由导出侧的")
    lines.append("//   顺序护栏（ui/universal/himi.py::blend_block_order_problems）在产物落盘前拦死。")
    lines.append("//")
    lines.append("// 【为什么必须逐帧写】palette 是**每帧姿态数据**：实测同一 vs-t0 资源哈希跨 6 次抓帧给出")
    lines.append("// 6 种不同内容，而同一批 dump 里静态资源（deform 的 Position vb0）逐字节不变")
    lines.append("// （reports/himi-merge/27-static-vs-perframe.md；独立 oracle 的 CF-1 组把这条前提钉住）。")
    lines.append("// 用 filename 加载的静态骨架 = 拿抓帧那一帧的姿态永久蒙皮 ⇒ 合并骨架在 ini 段里**不带")
    lines.append("// filename**（那份 Meshes/<role_key>-MergedSkeleton.buf 仍会落盘，但只作离线证据/调试产物）。")
    lines.append("//")
    lines.append("// 【跨显卡不变量（三条必须同时满足，2026-09-22 ZZMI 实机验证）】")
    lines.append("//  A1 本 palette 必须声明为 Buffer<float4>（typed 16 B 元素），**不是** StructuredBuffer<48B>：")
    lines.append("//     palette 是 copy vs-t0 的目标，加载器会把源视图的 Format 注入进来（SRV 实际是 typed")
    lines.append("//     16 B）。按 48 B 结构化声明会落进 D3D11 未定义区：AMD 按描述符取址 → 骨架 2/3 错位 →")
    lines.append("//     模型爆炸。所以这里手工按 bone*3 取三个 float4，不依赖任何 stride 解释。")
    lines.append("//     ini 侧该副本必须是 type = Buffer / stride = 48 / **绝不允许** format。")
    lines.append("//  A2 合并骨架必须声明为 RWBuffer<float4>（typed 16 B），按 slot*3 写。按结构化（SBS=48）")
    lines.append("//     声明会让 AMD 按 48 取址 → 骨号 b 实际读到槽 3b → 越界读返回精确 0。")
    lines.append("//     ini 侧：type = RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数")
    lines.append("//     + bind_flags = shader_resource unordered_access（缺 bind_flags ⇒ SRV 创建静默失败）。")
    lines.append("//  A3 槽数一律取**资源自身维度**（GetDimensions 单参后 /3u 还原），槽位下标一律 *3u，")
    lines.append("//     **绝不读 IniParams**：ini 参数纹理布局随 3DMigoto fork 变化（标准版 x1/y1 在")
    lines.append("//     IniParams[0]，Armor fork 从 [1] 起），读错位置只会读到未设置的 0 ⇒ CS 静默不写 ⇒")
    lines.append("//     骨架留空 ⇒ 顶点位置全零，且**不报任何错**。唯一逻辑参数是目标基址，由导出侧写进")
    lines.append("//     本文件的 #define（common/himi_merged_runtime.py::render_family_attach_base_line）。")
    lines.append("//")
    lines.append("// 【本环境未取证的项（不得当成已成立）】")
    for item in UNVERIFIED_IN_GAME:
        lines.append(f"//  - {item}")
    lines.append("//")
    lines.append("// ---- 机器可读声明行（测试据它与 ini 逐字段比对）----")
    lines.append("// " + decl.machine_line())
    lines.append("//")
    lines.append("// ---- 资源声明（必须与 ini 的 [{0}] 段逐字段一致）----".format(decl.name))
    lines.append(f"#define HIMI_MERGED_SKELETON_SLOTS {int(decl.slots)}u")
    lines.append(f"#define HIMI_MERGED_SKELETON_STRIDE {MERGED_SKELETON_STRIDE}u")
    lines.append("// 本族在合并骨架里的全局基址（槽）。导出侧逐族改写这一行（render_family_attach_base_line）；")
    lines.append("// 本 REFERENCE 副本用 0。基址不走 ini 参数 —— 见 A3。")
    # 基址是导出侧与 CS 之间**唯一**的逻辑参数接口：写进文件里的 #define（不读 IniParams）。
    # REFERENCE 副本用 0；逐族副本由导出侧改成该族的全局起始槽。
    lines.append(render_family_attach_base_line(0))
    lines.append("")
    lines.append("// cs-t0 = ref ResourceHI3PaletteFam_<fam>（ini: Buffer / stride = 48 / 无 format）")
    lines.append("Buffer<float4> himi_src_palette : register(t0);")
    lines.append("// cs-u0 = ref ResourceHI3MergedSkeleton_<role_key>")
    lines.append("// （ini: RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数 + bind_flags）")
    lines.append(decl.hlsl_declaration())
    lines.append("")
    lines.append("[numthreads(64, 1, 1)]")
    lines.append("void main(uint3 dispatch_id : SV_DispatchThreadID)")
    lines.append("{")
    lines.append("    uint bone_index = dispatch_id.x;")
    lines.append("")
    lines.append("    // 槽数取**资源自身维度**：typed Buffer<float4> 的 GetDimensions 是**单参**形式，")
    lines.append("    // 返回 float4 元素数 = 槽数 × 3；必须 /3u 还原成槽数（与 ZZMI 读取侧同一口径）。")
    lines.append("    uint palette_elements = 0u;")
    lines.append("    himi_src_palette.GetDimensions(palette_elements);")
    lines.append("    uint count = palette_elements / 3u;")
    lines.append("    if (bone_index >= count)")
    lines.append("    {")
    lines.append("        return;")
    lines.append("    }")
    lines.append("")
    lines.append("    uint skeleton_elements = 0u;")
    lines.append(f"    {decl.name}.GetDimensions(skeleton_elements);")
    lines.append("")
    lines.append("    uint src = bone_index * 3u;                        // 读侧索引：bone * 3u")
    lines.append("    uint dst = (HIMI_ATTACH_BASE + bone_index) * 3u;   // 写侧索引：slot * 3u（slot = 基址 + 本族骨号）")
    lines.append("    if (dst + 2u >= skeleton_elements)")
    lines.append("    {")
    lines.append("        return;   // 越界保护：宁可少写，也不写坏别人的槽")
    lines.append("    }")
    lines.append("")
    lines.append("    // 每根骨占 3 个 float4（4×3 行主序矩阵），两端都显式按 *3 取址 ——")
    lines.append("    // 与两侧的 typed 16 B 视图精确匹配。")
    lines.append(f"    {decl.name}[dst + 0u] = himi_src_palette[src + 0u];")
    lines.append(f"    {decl.name}[dst + 1u] = himi_src_palette[src + 1u];")
    lines.append(f"    {decl.name}[dst + 2u] = himi_src_palette[src + 2u];")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def reference_attach_hlsl() -> str:
    """The frozen reference text checked in as ``Toolset/himi_merged_skeleton_attach.hlsl``."""
    decl = build_merged_skeleton_decl(REFERENCE_ROLE_KEY, REFERENCE_SLOT_COUNT)
    return render_attach_hlsl(decl)


# --------------------------------------------------------------------------- #
# 【F5】每族「不可变身份」资源：把本族的 (base, span) 从 plan 带到运行期
# --------------------------------------------------------------------------- #
#
# 为什么必须有它（根因 = `reports/himi-merge/50-code-audit.md` §1.2 入口 ① / §4.3 F5）：
# 合并 attach CS 是**一个文件、5 个段共用**，各段唯一的区别是 `cs-t0` 绑的副本资源。
# 于是 CS 里「这是哪一族」只能从**副本的槽位数**（``GetDimensions``）反推，而那张
# 「槽位数 → 基址」的表也就成了唯一事实源。问题在于：**span 与查表键是同一个量**
# ⇒ `if (bone_index >= span)` 与查表恒同源（恒真判据），一旦副本维度**大于**本族 span
# （不同角色的 palette 池常常大于被引用槽数），多出来的骨就会写进**下一族的块**
# （A 族矩阵落进 B 族槽 = 用户看到的整块错位）。
#
# 修法（t3 §4.3 F5 的原话「给每段加一份不可变身份」）：每族在 ini 里多声明一份 16 字节的
# 身份资源，内容 = ``(base, span, version, magic)``，由**导出侧按本次 `plan.family_ranges`
# 生成**（单一事实源），段里 `cs-t1 = ref` 绑它。CS 于是拿到两份独立信息：
#   * 身份资源 = 本次导出的真实排布（权威）；
#   * 冻结表   = 历史排布（只作**交叉核对**：两者不一致 ⇒ 拒写，绝不冒用别的族的槽）。
# 身份不可用（绑空 / magic 不符）时退回冻结表 = **与今天逐字节等价的行为**（不新增
# 「模型消失」的失败模式）。
ATTACH_RANGE_RESOURCE_PREFIX = "ResourceHI3AttachRange_"
#: typed 16 B 声明：与合并骨架同款（写 ``format``、**不加** ``stride``，见模块头 A2/A3）。
ATTACH_RANGE_INI_FORMAT = "R32G32B32A32_FLOAT"
#: 落盘文件名后缀（与 VB/IB 同一个缓冲目录）。
ATTACH_RANGE_BASENAME_SUFFIX = "-AttachRange.buf"
#: 【t39 / t44】载荷 = 4 个 typed float4 = 64 B：
#:   ``[0]`` 头部 ``(base, span, version, magic)``；``[1..3]`` = **palette 侧要复合的因子**
#:   （t44 起 = ``Q_f⁻¹``：把「提取帧世界」映到「当前世界」；t39–t43 曾存 ``Q_f``，方向是反的）。
#: 为什么扩到 64 B（而不是另开资源）：因子与 ``base/span`` **必须同源同报** ——
#: 它们都由「本次导出的 plan + 该族的 palette 缓存」推导，分两个资源就会有两种真相；
#: 而且 CS 只需要多读 3 个 float4，不新增绑定槽（``cs-t1`` 不变）。
ATTACH_RANGE_BYTES = 64
#: 版本 3 = 载荷带 ``Q_f⁻¹``（t44 共同空间取反修复：palette 侧 `P·Q⁻¹` + 顶点侧 `Q·v`）；
#: 版本 2 = 载荷带 ``Q_f``（t39–t43，**方向取反**，语义已废弃）；版本 1 = 16 B（无因子）。
#: CS 的版本门取 ``>= 3u``：旧产物（v1/v2）一律不复合 ⇒ 退回"不校准"，绝不半校准。
ATTACH_RANGE_VERSION = 3
#: 头部长度（前 4 个 u32）—— 兼容 v1 的 16 B 载荷。
ATTACH_RANGE_HEADER_BYTES = 16
#: 身份魔数（0x48494D52 = "HIMR"）：它是**有限** float，``asuint`` 逐位还原不会碰到 NaN。
ATTACH_RANGE_MAGIC = 0x48494D52
#: CS 里魔数的宏名（生成器与判据共用，避免两处各写一个数）。
ATTACH_RANGE_MAGIC_MACRO = "HIMI_ATTACH_RANGE_MAGIC"
#: ini 侧身份资源的 ``filename`` 目录（与 VB/IB 一致，用当前缓冲目录名）。
ATTACH_RANGE_DEFAULT_FOLDER = MERGED_SKELETON_DEFAULT_FOLDER


def attach_range_resource_name(family: str) -> str:
    """``ResourceHI3AttachRange_<fam>``：该族在合并骨架里的身份资源。"""
    return ATTACH_RANGE_RESOURCE_PREFIX + str(family or "").strip()


def attach_range_basename(family: str) -> str:
    """``<fam>-AttachRange.buf``（64 B：头部 base/span/version/magic + ``Q_f`` 3×4）。"""
    return f"{str(family or '').strip()}{ATTACH_RANGE_BASENAME_SUFFIX}"


def attach_range_payload(
    base: int, span: int, *, placement=None, version: int = ATTACH_RANGE_VERSION
) -> bytes:
    """身份载荷 = 头部 ``(base, span, version, magic)`` + 可选 ``Q_f``（3×4 = 12 float）。

    * ``placement=None`` ⇒ 只写头部 + 12 个 0.0 的 Q 槽（**保留 v1 语义**：CS 不做校准）；
    * ``placement`` 给 12 个数（行主序 3×4）⇒ 写进 ``[1..3]`` 三个 float4。

    纯数据、无副作用：导出侧按 ``plan.family_ranges`` + 该族 palette 缓存生成，
    测试与复算脚本直接读它比对（「产物级复算证据」的入口之一）。
    """
    base = int(base)
    span = int(span)
    if base < 0 or span < 0:
        raise HIMIResourceDeclError(f"身份资源不接受负值（base={base}, span={span}）")
    header = b"".join(
        value.to_bytes(4, "little")
        for value in (base, span, int(version), ATTACH_RANGE_MAGIC)
    )
    if placement is None:
        return header + b"\x00" * (ATTACH_RANGE_BYTES - ATTACH_RANGE_HEADER_BYTES)
    values = [float(v) for v in placement]
    if len(values) != 12:
        raise HIMIResourceDeclError(
            f"族级摆放必须是 12 个数（3×4 行主序），实际 {len(values)}"
        )
    import struct as _struct

    return header + _struct.pack("<12f", *values)


def decode_attach_range_payload(payload: bytes) -> dict:
    """把身份载荷解回 ``{"base","span","version","magic","ok","has_placement","placement"}``。

    兼容三种形态：v1 的 16 B（无 Q）、v2 的 64 B（带 Q）、以及其它长度（``ok=False``）。
    ``has_placement`` 只在 ``version >= 2`` **且** Q 的 12 个数里至少有一个非零时为真
    （全零 = 生成侧没给摆放 ⇒ 语义等同 v1）。
    """
    import struct as _struct

    raw = bytes(payload or b"")
    if len(raw) not in (ATTACH_RANGE_HEADER_BYTES, ATTACH_RANGE_BYTES):
        return {
            "base": -1, "span": -1, "version": -1, "magic": -1,
            "ok": False, "has_placement": False, "placement": [],
        }
    values = [
        int.from_bytes(raw[index * 4 : index * 4 + 4], "little") for index in range(4)
    ]
    placement = []
    if len(raw) == ATTACH_RANGE_BYTES:
        placement = list(_struct.unpack("<12f", raw[ATTACH_RANGE_HEADER_BYTES:]))
    has_placement = bool(values[2] >= 2 and placement and any(v != 0.0 for v in placement))
    return {
        "base": values[0],
        "span": values[1],
        "version": values[2],
        "magic": values[3],
        "ok": values[3] == ATTACH_RANGE_MAGIC,
        "has_placement": has_placement,
        "placement": placement,
    }


# --------------------------------------------------------------------------- #
# 【t39】族级校准的纯数学（顶点侧 v' = Q⁻¹·v ；palette 侧 P' = P ∘ Q）
# --------------------------------------------------------------------------- #
#
# 为什么是这一对（而不是「P' = Q⁻¹·P 且 v' = Q·v」）：不变量要求
#     P'[b] · v' ≡ P[b] · v   （同一族的骨）
# 若取 P' = Q⁻¹·P、v' = Q·v，则左侧 = Q⁻¹·P·Q·v —— 那是**相似变换**，只有 P 与 Q 可交换才相等；
# 实测五族 × 若干槽 × 若干顶点 **最大偏差 0.774**（≫ 1e-5）。取 P' = P∘Q、v' = Q⁻¹·v 时
# 左侧 = P·Q·Q⁻¹·v = P·v **恒等**（实测 1.3e-15）。两者对**跨族借用**都会产生 ~1.3 单位的
# 位移（用户症状量级），但只有后者满足本任务自己的硬门。完整证据：`.dbg/_t39_math_verify.py`。


def mat3x4_compose(a, b):
    """``a ∘ b``（先 ``b`` 后 ``a``）：3×4 行主序，各 12 个 float。"""
    av = [float(v) for v in a]
    bv = [float(v) for v in b]
    out = [0.0] * 12
    for r in range(3):
        for c in range(3):
            out[r * 4 + c] = sum(av[r * 4 + k] * bv[k * 4 + c] for k in range(3))
        out[r * 4 + 3] = sum(av[r * 4 + k] * bv[k * 4 + 3] for k in range(3)) + av[r * 4 + 3]
    return out


def mat3x4_inverse(m):
    """3×4 仿射的逆（3×3 部分求逆 + 平移变换）。行列式退化 ⇒ 抛 ``HIMIResourceDeclError``。"""
    v = [float(x) for x in m]
    a = [[v[0], v[1], v[2]], [v[4], v[5], v[6]], [v[8], v[9], v[10]]]
    det = (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    )
    if abs(det) < 1e-12:
        raise HIMIResourceDeclError(f"族级摆放矩阵不可逆（det={det:.3e}）")
    inv = [
        [(a[1][1] * a[2][2] - a[1][2] * a[2][1]) / det,
         (a[0][2] * a[2][1] - a[0][1] * a[2][2]) / det,
         (a[0][1] * a[1][2] - a[0][2] * a[1][1]) / det],
        [(a[1][2] * a[2][0] - a[1][0] * a[2][2]) / det,
         (a[0][0] * a[2][2] - a[0][2] * a[2][0]) / det,
         (a[0][2] * a[1][0] - a[0][0] * a[1][2]) / det],
        [(a[1][0] * a[2][1] - a[1][1] * a[2][0]) / det,
         (a[0][1] * a[2][0] - a[0][0] * a[2][1]) / det,
         (a[0][0] * a[1][1] - a[0][1] * a[1][0]) / det],
    ]
    t = (v[3], v[7], v[11])
    out = []
    for r in range(3):
        out.extend(inv[r])
        out.append(-sum(inv[r][k] * t[k] for k in range(3)))
    return out


def mat3x4_apply(m, point):
    """``m · [x,y,z,1]``（列向量约定）。"""
    v = [float(x) for x in m]
    x, y, z = (float(point[0]), float(point[1]), float(point[2]))
    return (
        v[0] * x + v[1] * y + v[2] * z + v[3],
        v[4] * x + v[5] * y + v[6] * z + v[7],
        v[8] * x + v[9] * y + v[10] * z + v[11],
    )


def mat3x4_rotate(m, direction):
    """只施加 3×3 线性部分（法线/切线这类方向量）。"""
    v = [float(x) for x in m]
    x, y, z = (float(direction[0]), float(direction[1]), float(direction[2]))
    return (
        v[0] * x + v[1] * y + v[2] * z,
        v[4] * x + v[5] * y + v[6] * z,
        v[8] * x + v[9] * y + v[10] * z,
    )


#: 族 palette 缓存文件名（``BoneMatrixFileName`` 的默认拼法）。
FAMILY_PALETTE_BASENAME_SUFFIX = "-BoneMatrix.buf"


def load_family_placement(workspace_root: str, unique_str: str) -> dict:
    """读该部件所属族的 palette，取**第 0 槽**当 ``Q_f``（族级摆放）。

    返回 ``{"ok","family","placement","palette_path","slot_count","note"}``；任何缺失都
    ``ok=False``（调用方 fail-closed，**不静默降级**）。
    """
    name = str(unique_str or "")
    bare = bare_from_unique_str(name)
    if not bare:
        return {"ok": False, "family": "", "placement": [], "palette_path": "",
                "slot_count": 0, "note": f"部件名无法解析：{name!r}"}
    json_path = resolve_submesh_json_path(workspace_root, name)
    if not json_path:
        return {"ok": False, "family": bare.split("-", 1)[0], "placement": [],
                "palette_path": "", "slot_count": 0,
                "note": f"找不到部件 json（{name}）⇒ 无法定位 palette 缓存"}
    palette_path = runtime_cache_path(
        json_path, bare, bare + FAMILY_PALETTE_BASENAME_SUFFIX
    )
    if not os.path.isfile(palette_path):
        return {"ok": False, "family": bare.split("-", 1)[0], "placement": [],
                "palette_path": palette_path, "slot_count": 0,
                "note": f"没有族 palette 缓存（{palette_path}）⇒ 无法做族级校准"}
    try:
        with open(palette_path, "rb") as handle:
            payload = handle.read(MERGED_SKELETON_STRIDE)
    except OSError as exc:
        return {"ok": False, "family": bare.split("-", 1)[0], "placement": [],
                "palette_path": palette_path, "slot_count": 0, "note": str(exc)}
    if len(payload) < MERGED_SKELETON_STRIDE:
        return {"ok": False, "family": bare.split("-", 1)[0], "placement": [],
                "palette_path": palette_path, "slot_count": 0,
                "note": f"palette 缓存不足 48 字节（{len(payload)}）"}
    import struct as _struct

    placement = list(_struct.unpack("<12f", payload[:MERGED_SKELETON_STRIDE]))
    try:
        mat3x4_inverse(placement)
    except HIMIResourceDeclError as exc:
        return {"ok": False, "family": bare.split("-", 1)[0], "placement": [],
                "palette_path": palette_path, "slot_count": 0, "note": str(exc)}
    try:
        slot_count = os.path.getsize(palette_path) // MERGED_SKELETON_STRIDE
    except OSError:
        slot_count = 0
    return {"ok": True, "family": bare.split("-", 1)[0], "placement": placement,
            "palette_path": palette_path, "slot_count": slot_count, "note": ""}


def attach_range_ini_lines(family: str, filename: str) -> list[str]:
    """身份资源的 ``[Resource…]`` 段体（与 IB 资源同形：``type`` + ``format`` + ``filename``）。"""
    name = attach_range_resource_name(family)
    return [
        f"[{name}]",
        "type = Buffer",
        f"format = {ATTACH_RANGE_INI_FORMAT}",
        f"filename = {str(filename or '').strip()}",
    ]


def attach_range_reference_line(family: str) -> str:
    """attach 段里的 ``cs-t1 = ref <身份资源>`` 行。"""
    return f"cs-t1 = ref {attach_range_resource_name(family)}"


def attach_write_target(
    *,
    bone_index: int,
    source_slots: int,
    table_base: int,
    identity_base: int,
    identity_span: int,
    skeleton_slots: int,
) -> int:
    """**CS 守卫的模型**（判据与回归用；``-1`` = 本线程不写）。

    ⚠ 这是生成文本里那几行守卫的**可执行镜像**，不是第二份实现：测试同时断言生成文本
    逐条含这些守卫行（``merged_attach_hlsl_problems``），所以两边漂移会被抓。

    顺序与 CS 一致：
    0. **身份可用性**（t18 / T5-F1 统一语义）：身份只在 ``identity_base >= 0 **且** identity_span > 0``
       时可用。`span == 0` 是 CS 里 ``uint span_slots = 0u`` 的**哨兵值**（= 身份不可用），
       此时两处都**退回冻结表**（base 取表值、无本族 span 上界），与「资源绑空 / 魔数不符」
       走同一条路；此前模型把 span=0 当「整族拒写」而 CS 当「不拦」—— 语义分歧已消除；
    1. 身份可用 ⇒ 它权威；冻结表认识这一族且与其不一致 ⇒ **拒写**（-1）；
    2. base 仍不可知 ⇒ 拒写；
    3. ``bone_index >= source_slots`` ⇒ 拒写（不得读副本之外）；
    4. ``bone_index >= identity_span`` ⇒ 拒写（**截断**：副本维度大于本族 span 时
       绝不写进下一族的块 —— F5 的核心）；
    5. 目标槽越出骨架槽数 ⇒ 拒写。
    """
    base = int(table_base)
    identity = int(identity_base)
    span = int(identity_span)
    identity_usable = identity >= 0 and span > 0
    if identity_usable:
        if base >= 0 and base != identity:
            return -1
        base = identity
    if base < 0:
        return -1
    if int(bone_index) >= int(source_slots):
        return -1
    if identity_usable and int(bone_index) >= span:
        return -1
    target = base + int(bone_index)
    if target >= int(skeleton_slots):
        return -1
    return target


def publish_attach_ranges(ranges, folder_path: str, *, placements=None) -> dict:
    """把每族的身份载荷写进 mod 缓冲目录（幂等；返回 ``{"ok","written","files","message"}``）。

    ``placements``：``{family: 12 个 float（Q_f，行主序 3×4）}``（t39 族级校准）。
    给了 ⇒ 载荷 64 B（头部 + Q_f）；没给 ⇒ 只写 16 B 头部（**v1 语义**：CS 不做校准）。

    缺任何一族的文件都会让该族 CS 的 ``cs-t1`` 绑空 ⇒ 退回冻结表（不是静默乱写），
    但导出侧另有**文件存在性硬门**（``ui/universal/himi.py::verify_attach_range_references``）
    把它拦在产物落盘那一步；该硬门同时核对**长度**（v2 必须是 64 B）。
    """
    folder = str(folder_path or "")
    if not folder:
        return {
            "ok": False,
            "written": False,
            "files": [],
            "message": "mod 缓冲目录为空（BlueprintExportHelper.current_buffer_folder_name 未设置）",
        }
    ranges = dict(ranges or {})
    if not ranges:
        return {"ok": True, "written": False, "files": [], "message": "没有家族区间，无需身份资源"}
    placements = dict(placements or {})
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "written": False, "files": [], "message": str(exc)}
    files: list = []
    written = 0
    try:
        for family, info in sorted(
            ranges.items(), key=lambda item: (int(dict(item[1]).get("base", 0)), str(item[0]))
        ):
            info = dict(info or {})
            span = int(info.get("slots", 0) or 0)
            if span <= 0:
                continue  # 借位族没有自己的块 ⇒ 不发身份资源（与「不发 attach 段」同源）
            payload = attach_range_payload(
                int(info.get("base", 0) or 0), span, placement=placements.get(str(family))
            )
            path = os.path.join(folder, attach_range_basename(family))
            files.append(path)
            unchanged = False
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as handle:
                        unchanged = handle.read() == payload
                except OSError:
                    unchanged = False
            if unchanged:
                continue
            with open(path, "wb") as handle:
                handle.write(payload)
            written += 1
    except OSError as exc:
        return {"ok": False, "written": False, "files": files, "message": str(exc)}
    calibrated = len([1 for family in placements if str(family) in ranges])
    return {
        "ok": True,
        "written": written > 0,
        "files": files,
        "message": (
            f"已写出 {len(files)} 个家族身份资源（重写 {written} 个；"
            f"其中 {calibrated} 个带族级摆放 Q_f / {ATTACH_RANGE_BYTES} B）"
        ),
    }


# 说明：`import re` 放在**文件末尾**（而不是顶部）是为了不改动 t43 锚点护栏引用的行号 ——
# 《HIMI骨骼合并计划书》按 `文件:行号 + 相邻符号` 引用了本文件多处 `def`，顶部加一行会让它们
# 整体漂移 +1（实测踩过）。
import re  # noqa: E402  （延迟到文件末尾：见上）


# --------------------------------------------------------------------------- #
# 【t41 / T40-F3】CS 复合表达式的**可执行镜像**（文本门 + 数值门同一事实源）
# --------------------------------------------------------------------------- #
#
# 为什么必须有（t40 的 finding T40-F3，high）：不变量硬门只覆盖 Python 侧的 `mat3x4_compose`，
# 而真正决定游戏里结果的是 **CS 文本里的表达式** —— 实测把平移项改成 `* 0.0`，测试仍
# 166 passed、文本判据对（正确/当前/0.0）三种写法都给 `[]` ⇒ **硬门对交付物零覆盖**。
# 修法（照 `attach_write_target` 之于 CS 守卫的做法）：把 CS 的复合表达式**解析成模型**，
# 再让模型与 `mat3x4_compose` 逐元素对齐 ⇒ 文本一被改坏，数值门立刻变红。
ATTACH_COMPOSE_CORRECT = "correct"   # t_i' = Σ_k p_ik·t_k            （正确）
ATTACH_COMPOSE_OLD = "old"           # t_i' = (p_i0+p_i1+p_i2)·t_i    （旧写法 placement_i.www）
ATTACH_COMPOSE_ZERO = "zero"         # t_i' = 0                       （平移项被 * 0.0 抹掉）
ATTACH_COMPOSE_UNKNOWN = "unknown"   # 解析不出来（判据按失败处理）

#: 逐行的复合表达式正则（行号用 ``(?P<i>)`` 捕获）。
_ATTACH_COMPOSE_LINE = re.compile(
    r"out(?P<i>[0-2])\s*=\s*float4\(\s*dot\(\s*src(?P=i)\.xyz\s*,\s*qc0\s*\)\s*,"
    r"\s*dot\(\s*src(?P=i)\.xyz\s*,\s*qc1\s*\)\s*,\s*dot\(\s*src(?P=i)\.xyz\s*,\s*qc2\s*\)\s*,"
    r"(?P<term>.*?)\)\s*;",
    re.S,
)


def attach_compose_model_from_hlsl(text: str) -> dict:
    """把 CS 里的 ``P' = P∘Q`` 表达式解析成模型：``{"ok","rows","modes","terms","problems"}``。

    ``modes[i]`` ∈ ``correct`` / ``old`` / ``zero`` / ``unknown``；``ok`` 只在三行都解析出
    且都是可识别写法时为真（``unknown`` 会被判据当失败）。
    """
    code = re.sub(r"/\*.*?\*/", "", str(text or ""), flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    modes: dict = {}
    terms: dict = {}
    problems: list = []
    for match in _ATTACH_COMPOSE_LINE.finditer(code):
        index = int(match.group("i"))
        term = " ".join(str(match.group("term") or "").split())
        terms[index] = term
        if re.search(
            r"dot\(\s*src%d\.xyz\s*,\s*placement_t\s*\)\s*\+\s*src%d\.w" % (index, index), term
        ):
            mode = ATTACH_COMPOSE_CORRECT
        elif re.search(r"\*\s*0(?:\.0+)?\b", term):
            mode = ATTACH_COMPOSE_ZERO
        elif re.search(
            r"dot\(\s*src%d\.xyz\s*,\s*placement%d\.www\s*\)" % (index, index), term
        ):
            mode = ATTACH_COMPOSE_OLD
        else:
            mode = ATTACH_COMPOSE_UNKNOWN
        modes[index] = mode
        if mode == ATTACH_COMPOSE_UNKNOWN:
            problems.append(f"out{index} 的平移项无法识别：{term!r}")
    if sorted(modes) != [0, 1, 2]:
        problems.append(f"三行复合表达式没有全部找到（只解析到 {sorted(modes)}）")
    return {
        "ok": not problems,
        "rows": sorted(modes),
        "modes": modes,
        "terms": terms,
        "problems": problems,
    }


def compose_rows_from_model(model: dict, src_rows, placement_rows):
    """按**从 CS 文本解析出来的模型**算 ``P' = P∘Q``（3×4 ∘ 3×4，列向量约定）。

    ``src_rows`` / ``placement_rows``：各 3 行、每行 4 个数（``[x, y, z, w]``，行主序）。
    返回 3 行 4 列。**语义完全由模型（= CS 文本）决定** ⇒ 文本一改，数值就变；
    正确的 ``correct`` 模型与 :func:`mat3x4_compose` 逐元素相等。
    """
    src = [[float(v) for v in row] for row in src_rows]
    q = [[float(v) for v in row] for row in placement_rows]
    modes = dict(model.get("modes") or {})
    out = []
    for i in range(3):
        row = [sum(src[i][k] * q[k][j] for k in range(3)) for j in range(3)]
        mode = modes.get(i, ATTACH_COMPOSE_UNKNOWN)
        if mode == ATTACH_COMPOSE_CORRECT:
            row.append(sum(src[i][k] * q[k][3] for k in range(3)) + src[i][3])
        elif mode == ATTACH_COMPOSE_OLD:
            row.append(sum(src[i][k] for k in range(3)) * q[i][3] + src[i][3])
        elif mode == ATTACH_COMPOSE_ZERO:
            row.append(src[i][3])
        else:
            row.append(float("nan"))
        out.append(row)
    return out


# 说明：`import math as _math` 与 `import re` 一样放在文件末尾，避免让 t43 锚点引用的行号漂移。
import math as _math  # noqa: E402


def load_family_position_index(workspace_root: str, members, *, tolerance: float = 1e-4) -> dict:
    """各族**源绑定坐标**的**容差网格**索引 + 旧口径（精确量化键）的对照表。

    用途（t41/T40-F2 + t43/T42-F2）：合并网格整个落在宿主族的缓冲里，而**顶点自己属于哪个族**
    不在任何缓冲里（Blend 只记骨头槽位）⇒ 只能用坐标回到各族的源 VB 里认领。

    **为什么不再用精确量化键**（t42 的 finding T42-F2）：``round(x*1e5)`` 查表对**量化边界**敏感 ——
    两次独立导出的逐族行数漂 ±4~5（2c706fbd 20637↔20633、def0c0ae 1342↔1347），因为同一坐标在两次
    导出里差 ~1e-6 就会落进不同的桶；漂移若发生在**跨族行**上，该顶点会被换一个 ``Q``（最多 ~3.7 单位）。
    改为**容差内最近邻**：只要在 ``tolerance`` 内找到源点就算命中 ⇒ 稳定。

    **容差 1e-4 的依据**：① 绑定坐标在管线里是 float32（相对精度 ~1e-7、坐标量级 ~2 ⇒ 绝对 ~2e-7）；
    ② 合并/去重会重算坐标，实测两次导出的同一点差 ≲1e-5；③ 几何尺度 = 身高 ~1.81 单位 ⇒ 1e-4 是
    身高的 0.0055%，而误归属的代价是 ~3.7 单位 ⇒ 比噪声大 10 倍、比危险量级小 4 个数量级。

    返回 ``{"ok","grid","cell","tolerance","keys","points","missing","note"}``：
    * ``grid`` = ``{(cx,cy,cz): [(x,y,z,family), …]}``（``cell == tolerance`` ⇒ 半径内的点必在 3×3×3 邻域）；
      **t24 起每条目扩展为** ``(x, y, z, family, nx, ny, nz, tx, ty, tz)`` —— 同一条 44 B 记录里的
      法线（3..5）与切线（7..9）随点入索引。为什么必须带：焊接点在**两个族**的源池里各有一份
      几乎相同的坐标（实测差 ~1e-8，float32 重舍入噪声量级），位置单独不可分；
      属性是判族的第二证据（见 :func:`nearest_family` 的 t24 判定顺序）。
      旧 4 元组形状仍被 :func:`nearest_family` 接受（轻量夹具/旧索引 ⇒ 属性判别自动停用）。
    * ``keys`` = 旧口径的 ``{(round(x*1e5),…): family}``（**只为统计翻转**，族次序先到先得）。
    """
    grid: dict = {}
    keys: dict = {}
    points = 0
    missing: list = []
    cell = float(tolerance) if tolerance > 0 else 1e-4
    for item in members or ():
        try:
            family, unique_str = str(item[0]), str(item[1])
        except (TypeError, IndexError):
            continue
        bare = bare_from_unique_str(unique_str)
        json_path = resolve_submesh_json_path(workspace_root, unique_str) if bare else ""
        if not bare or not json_path:
            missing.append(f"{unique_str}(无 json)")
            continue
        component_dir = os.path.dirname(os.path.dirname(json_path))
        source = ""
        for root, _dirs, files in os.walk(component_dir):
            for name in files:
                if name == bare + "-Position.buf":
                    source = os.path.join(root, name)
                    break
            if source:
                break
        if not source or not os.path.isfile(source):
            missing.append(f"{bare}(无源 Position)")
            continue
        try:
            with open(source, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            missing.append(f"{bare}({exc})")
            continue
        import struct as _struct

        stride = 44
        for offset in range(0, len(payload) - stride + 1, stride):
            # 【t24】整行 11 个 float 一起解包：0..2 位置、3..5 法线、6 COLOR、7..9 切线、10 切线.w。
            # 旧实现只解位置 3 个 float；属性列随点入索引供 nearest_family 的属性判别用
            # （同位置 split-normal 拷贝靠它们分辨）。COLOR/切线.w 不入索引（无判别用途）。
            row = _struct.unpack("<11f", payload[offset : offset + stride])
            x, y, z = row[0], row[1], row[2]
            cell_key = (
                int(_math.floor(x / cell)),
                int(_math.floor(y / cell)),
                int(_math.floor(z / cell)),
            )
            grid.setdefault(cell_key, []).append(
                (x, y, z, family, row[3], row[4], row[5], row[7], row[8], row[9])
            )
            keys.setdefault((round(x, 5), round(y, 5), round(z, 5)), family)
            points += 1
    return {
        "ok": bool(grid),
        "grid": grid,
        "cell": cell,
        "tolerance": float(tolerance),
        "keys": keys,
        "points": points,
        "missing": missing,
        "note": (f"源绑定坐标：{points} 个点、{len(grid)} 个网格单元（cell={cell:g}）；"
                 f"{len(missing)} 个族的源缓冲没读到"),
    }


#: 【t24】近并列（焊接点）判定窗。同一物理点的两份族拷贝经 float32 重舍入后到 query 的
#: 距离差实测 7.45e-09 ~ 8.71e-08（中位 1.86e-08，``reports/himi-merge/200-audit/26-94rows-
#: contamination.md`` §5.1，本仓 `.dbg/_t22_tie_margin.out.txt`）；坐标量级 ~2 ⇒ 单点 float32
#: 重舍入噪声上限 ~√3·2⁻²³·2 ≈ 8.2e-07。窗口取 **1e-06**：≥ 实测胜负差 11×、≥ 噪声上限 1.2×，
#: 且只有认领容差（1e-4）的 1% ⇒ 比它更远的候选仍按严格最近判。
#: 旧值 **1e-12** 比实测胜负差小 4 个数量级 ⇒ 「并列 ⇒ prefer 骨头族」的保守分支对焊接点
#: 永不触发 —— 94 行被乘别族 Q 的病根（t24 修复本体）。
FAMILY_TIE_WINDOW = 1e-6

#: 【t24】属性（法线/切线）判别阈值。单位向量的残差 ≈ 夹角（弧度）：float32 重舍入对
#: 单位法线的噪声 ≤ ~4e-07；导出法线是 Blender 重算值，与源法线的组内最小差实测中位
#: 2.6e-05（t24 探针 ``_t24_probe_groupmin``，全部 31054 行）；焊接点上错误族源法线到
#: 导出行 ≥1.9e-04（94 行全体的实测 min）。取 **1e-04** = 与位置认领容差同一量纲口径
#: （属性一致性不放宽于位置一致性）：高于重舍入噪声 ~250×，低于焊接点错误族实测 min ~2×。
#: 语义是保守的：属性只用于「prefer 没投票时在并列族里**唯一命中**才改判」，判不出 ⇒
#: 落回旧实现的确定性回退（family_order 先到先得），绝不因属性勉强而改判。
FAMILY_ATTRIBUTE_TOLERANCE = 1e-4


def _family_attribute_residual(entry, normal, tangent):
    """一条索引条目的属性 (nx,ny,nz,tx,ty,tz) 与查询属性的最小残差；不可比 ⇒ ``None``。

    * 索引条目是旧 4 元组（无属性）或查询两侧任一缺 ⇒ ``None``（属性判别停用）；
    * 查询给法线 ⇒ 必比法线；另给切线 ⇒ 切线也必须命中（残差取**最大值** = 两者同时达标）；
    * 残差里出现非有限值（源缓冲的 NaN 尾巴）⇒ 该条目不可判（``None``），绝不参与命中。
    """
    if len(entry) < 10:
        return None
    residuals = []
    for query, offset in ((normal, 4), (tangent, 7)):
        if query is None:
            continue
        stored = entry[offset : offset + 3]
        if not all(_math.isfinite(float(v)) for v in stored):
            return None
        if not all(_math.isfinite(float(v)) for v in query):
            return None
        residuals.append(
            _math.dist((float(stored[0]), float(stored[1]), float(stored[2])),
                       (float(query[0]), float(query[1]), float(query[2])))
        )
    if not residuals:
        return None
    return max(residuals)


def _resolve_family_by_attributes(tied, attr, tolerance=FAMILY_ATTRIBUTE_TOLERANCE):
    """并列族里**恰好一个**的属性残差 ≤ ``tolerance`` ⇒ 返回它；其余情形 ⇒ ``""``。

    零命中（属性噪声太大）与多命中（表面平行等真歧义）都返回空 ⇒ 调用方落回
    严格最近/prefer 的保守路径 —— 属性判别只有「唯一命中」这一种改判通道。
    """
    passing = [
        name for name in tied
        if attr.get(name) is not None and attr[name] <= float(tolerance)
    ]
    if len(passing) == 1:
        return passing[0]
    return ""


def nearest_family(info: dict, point, *, family_order=(), prefer: str = "",
                   normal=None, tangent=None) -> dict:
    """**容差内最近邻**归属（t24 起：近并列回 prefer + 属性唯一命中判别）。

    * 搜索 ``point`` 所在单元及其 3×3×3 邻域（``cell == tolerance`` ⇒ 半径内的点必在其中）；
    * **t24 判定顺序**（修复 94 行焊接点被乘别族 Q 的病根）：
      1. 容差内收集每族候选（不变）；同族多条同位置 split-normal 记录 ⇒ 属性残差取**组内最小**；
      2. ``tied`` = 距离 ≤ ``lowest + FAMILY_TIE_WINDOW`` 的族 —— 窗口覆盖焊接点两份拷贝的
         float32 重舍入差（旧值 1e-12 ⇒ prefer 分支对焊接点永不触发）；
      3. ``prefer``（骨头族）在 ``tied`` 里 ⇒ 选它：该行保持骨头族 = 同族校准，对**变形结果**
         逐值不变（``P·Q⁻¹·Q·v = P·v``），等价于"不校准"——这就是保守分支的正确语义；
      4. prefer 没投票（骨头族不在并列集）且索引/查询带属性：并列族里**恰好一个**属性残差
         ≤ ``FAMILY_ATTRIBUTE_TOLERANCE`` ⇒ 选它（借骨行的几何族判定）；
      5. 其余（属性不可用 / 零命中 / 多命中）⇒ 与旧实现相同的确定性回退
         （``family_order`` 先到先得）。
    * **并列时优先 ``prefer``（保守：判成同族 ⇒ 该行不换 Q）**，再按 ``family_order``；
    * 返回 ``{"family","distance","candidates","ambiguous","tied","attr_residual"}``
      （后两项 t24 新增，供诊断；``ambiguous`` 语义不变 = 容差内出现 >1 个族）。
    """
    grid = dict(info or {}).get("grid") or {}
    tolerance = float(dict(info or {}).get("tolerance", 0.0) or 0.0)
    cell = float(dict(info or {}).get("cell", 0.0) or 0.0)
    if not grid or tolerance <= 0.0 or cell <= 0.0:
        return {"family": "", "distance": float("inf"), "candidates": [], "ambiguous": False,
                "tied": [], "attr_residual": None}
    x, y, z = (float(point[0]), float(point[1]), float(point[2]))
    base = (int(_math.floor(x / cell)), int(_math.floor(y / cell)), int(_math.floor(z / cell)))
    best: dict = {}
    attr: dict = {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for entry in grid.get((base[0] + dx, base[1] + dy, base[2] + dz), ()):
                    distance = _math.dist((x, y, z), (entry[0], entry[1], entry[2]))
                    if distance > tolerance:
                        continue
                    family = str(entry[3])
                    if family not in best or distance < best[family]:
                        best[family] = distance
                    residual = _family_attribute_residual(entry, normal, tangent)
                    if residual is not None:
                        current = attr.get(family)
                        if current is None or residual < current:
                            attr[family] = residual
    if not best:
        return {"family": "", "distance": float("inf"), "candidates": [], "ambiguous": False,
                "tied": [], "attr_residual": None}
    lowest = min(best.values())
    tied = [name for name, distance in best.items() if distance <= lowest + FAMILY_TIE_WINDOW]
    chosen = ""
    attr_residual = None
    if prefer and prefer in tied:
        chosen = prefer
    else:
        resolved = _resolve_family_by_attributes(tied, attr)
        if resolved:
            chosen = resolved
            attr_residual = attr.get(resolved)
        else:
            for name in list(family_order) + sorted(tied):
                if name in tied:
                    chosen = name
                    break
    return {
        "family": chosen,
        "distance": best.get(chosen, lowest),
        "candidates": sorted(best),
        "ambiguous": len(best) > 1,
        "tied": sorted(tied),
        "attr_residual": attr_residual,
    }


# --------------------------------------------------------------------------- #
# 【t49 / t47 方案 A + C】导出侧旁写「源局部槽号」+ 越界引用计数
# --------------------------------------------------------------------------- #
#
# 为什么（t47 的关键结论）：产物 Blend 里的**全局索引**是从源数据写坏的 —— 工作空间
# `LOD0/<comp>/TYPE_*/<comp>-Blend.buf` 的局部组号 12/12 组件全部在自己范围内（越界 0），
# 而产物里出现了越界值（实测 `2c706fbd`：449 行、取值只有 275、2.21%）。t47 的话：
# **不要再试图"恢复" `b_A` —— 它在源里是好的，是导出把它丢了。最短路径是让导出把源文件的
# 局部组号原样旁写一份 + 一条越界计数日志。**
#
# 方案 A 的落盘格式（8 B/顶点，行序与 `<fam>-Blend.buf` 逐行对齐）：
#   [0] local_slot(u32)  该顶点**在本族 palette 里的局部槽号**
#                        * 全局索引在范围内 ⇒ 精确推导 `g - base`；
#                        * 全局索引越界、但仍有在范围内的活动通道 ⇒ 用那个通道的推导值；
#                        * 全通道都越界 ⇒ **本族局部槽不可得** ⇒ 写哨兵 `0xFFFFFFFF`（并发 flags.bit1）
#   [1] flags(u32)       bit0 = 存在越界引用（≥1 个活动通道的全局索引不在本族区段）
#                        bit1 = local_slot 是哨兵（本族局部槽不可得）
#                        bit2 = 全部活动通道都越界
BLEND_ORIGIN_BASENAME_SUFFIX = "-BlendOrigin.buf"
BLEND_ORIGIN_BYTES = 8
BLEND_ORIGIN_FLAG_OUT_OF_RANGE = 0x1
BLEND_ORIGIN_FLAG_LOCAL_UNAVAILABLE = 0x2
BLEND_ORIGIN_FLAG_ALL_CHANNELS_OUT = 0x4
BLEND_ORIGIN_LOCAL_UNAVAILABLE = 0xFFFFFFFF
BLEND_ORIGIN_STRIDE = 32


def blend_origin_rows(blend_bytes: bytes, *, base: int, span: int) -> dict:
    """逐行算 ``(local_slot, flags)``（方案 A 的两列）。

    返回 ``{"ok","rows","vertices","out_of_range","all_channels_out","local_unavailable",
    "slots","sample","message"}``；``rows`` = ``[(local_slot, flags), …]``（行序 = Blend 行序）。
    """
    import struct as _struct

    raw = bytes(blend_bytes or b"")
    if not raw or len(raw) % BLEND_ORIGIN_STRIDE:
        return {"ok": False, "rows": [], "vertices": 0, "out_of_range": 0,
                "all_channels_out": 0, "local_unavailable": 0, "slots": {},
                "sample": {}, "message": f"Blend 字节 {len(raw)} 不是 32 的整数倍"}
    rows = []
    vertices = len(raw) // BLEND_ORIGIN_STRIDE
    out_of_range = all_out = unavailable = 0
    slots: dict = {}
    sample = {}
    for index in range(vertices):
        offset = index * BLEND_ORIGIN_STRIDE
        weights = _struct.unpack("<4f", raw[offset : offset + 16])
        indices = _struct.unpack("<4i", raw[offset + 16 : offset + 32])
        active = [(w, i) for w, i in zip(weights, indices) if w > 0.0]
        inside = [i for _w, i in active if base <= i < base + span]
        outside = [i for _w, i in active if not (base <= i < base + span)]
        flags = 0
        if outside:
            flags |= BLEND_ORIGIN_FLAG_OUT_OF_RANGE
            out_of_range += 1
        if outside and not inside:
            flags |= BLEND_ORIGIN_FLAG_ALL_CHANNELS_OUT
            all_out += 1
        if inside:
            local = min(inside) - base
        elif active:
            local = BLEND_ORIGIN_LOCAL_UNAVAILABLE
            flags |= BLEND_ORIGIN_FLAG_LOCAL_UNAVAILABLE
            unavailable += 1
        else:
            local = 0
        slots[index] = local
        if len(sample) < 3:
            sample[index] = {"local_slot": local, "flags": flags,
                             "active": [int(i) for _w, i in active]}
        rows.append((local, flags))
    return {"ok": True, "rows": rows, "vertices": vertices, "out_of_range": out_of_range,
            "all_channels_out": all_out, "local_unavailable": unavailable, "slots": slots,
            "sample": sample, "message": ""}


def blend_origin_payload(rows) -> bytes:
    """``[(local_slot, flags), …]`` ⇒ 8 B/行的 little-endian 载荷。"""
    import struct as _struct

    out = bytearray()
    for local, flags in rows or ():
        out += _struct.pack("<II", int(local) & 0xFFFFFFFF, int(flags) & 0xFFFFFFFF)
    return bytes(out)


def blend_origin_problems(rows, *, span: int, vertices: int) -> list:
    """方案 A 的硬门：条数对得上 + 逐行 ``local_slot < span``（哨兵行单列，**不静默放过**）。"""
    problems: list = []
    rows = list(rows or ())
    if len(rows) != int(vertices):
        problems.append(f"BlendOrigin 行数 {len(rows)} 不等于顶点数 {vertices}")
    bad = [i for i, (local, _f) in enumerate(rows)
           if local != BLEND_ORIGIN_LOCAL_UNAVAILABLE and not (0 <= local < int(span))]
    if bad:
        problems.append(f"{len(bad)} 行的 local_slot 不在 [0,{span})（首行 {bad[:5]}）")
    unavailable = [i for i, (local, _f) in enumerate(rows)
                   if local == BLEND_ORIGIN_LOCAL_UNAVAILABLE]
    if unavailable:
        problems.append(
            f"{len(unavailable)} 行的本族局部槽不可得（哨兵 0xFFFFFFFF、flags.bit1 已标注）"
            "—— 这些行正是「导出侧注入了越界索引」的直接证据"
        )
    return problems


def blend_origin_basename(family: str) -> str:
    """``<fam>-BlendOrigin.buf``（方案 A 的旁文件）。"""
    return f"{str(family or '').strip()}{BLEND_ORIGIN_BASENAME_SUFFIX}"


# --------------------------------------------------------------------------- #
# 【t74】H-A 硬门：「位置被写成别行的位置」的导出侧判据（判据本体照搬 t72）
# --------------------------------------------------------------------------- #
#
# 为什么：导出段"拉丝"的直接成因是**产物里某些行的位置不是它自己的**。t72 用逐元素对应把它变成
# 可逐行算的数：u_k → v_k（工作空间 IB 与产物 per-part IB 字节数相同、索引被重写 ⇒ 同一份三角表），
# 稳健拟合相似变换 (s, R, t) 后：
#   * 正常行：残差 ≈ 1e-7 量级（t72 实测 RMS 2.2e-08…2.9e-07，是 float32 精度级）；
#   * H-A 行：残差 0.5574–0.8238（= 36–54× 中位边），且其位置**精确等于另一"在用"行的位置**（距离 0）。
# ⇒ 两条同时成立就判"位置写错"，并指名到行。
BIND_POSITION_EDGE_FACTOR = 3.0
BIND_POSITION_MIN_THRESHOLD = 1e-5
BIND_POSITION_EXACT_TOLERANCE = 1e-6


def kabsch_similarity(A, B, *, rounds: int = 3, edge_factor: float = BIND_POSITION_EDGE_FACTOR,
                      min_threshold: float = BIND_POSITION_MIN_THRESHOLD):
    """稳健相似变换拟合（照搬 t72 的 `kabsch_similarity` + 3 轮内点）。

    ``A[i] → B[i]``（行向量）。返回 ``{"R", "scale", "t", "inliers", "rms", "residuals",
    "median_edge", "threshold", "rounds"}``。
    """
    import numpy as _np

    A = _np.asarray(A, dtype=_np.float64)
    B = _np.asarray(B, dtype=_np.float64)
    if (A.ndim != 2 or B.ndim != 2 or A.shape[0] != B.shape[0]
            or A.shape[1] != 3 or B.shape[1] != 3):
        # 【t79 防御】形状不匹配/非法输入 ⇒ 返回"退化"结果（门宁可不判，也不许抛异常弄坏导出）。
        rows = int(A.shape[0]) if A.ndim else 0
        return {"R": _np.eye(3), "scale": 1.0, "t": _np.zeros(3),
                "inliers": _np.zeros(rows, bool), "rms": float("inf"),
                "residuals": _np.full(rows, float("inf")), "median_edge": 0.0,
                "threshold": min_threshold, "rounds": 0, "inlier_ratio": 0.0,
                "max_residual": float("inf")}
    out = {"R": _np.eye(3), "scale": 1.0, "t": _np.zeros(3), "inliers": _np.zeros(len(A), bool),
           "rms": float("inf"), "residuals": _np.full(len(A), float("inf")),
           "median_edge": 0.0, "threshold": min_threshold, "rounds": 0}
    if A.shape[0] < 3:
        return out
    # 中位边（用源点的近邻距离估计"几何尺度"，与 t72 的 tri_edges 中位边同量级）
    sample = A if A.shape[0] <= 512 else A[:512]
    d = _np.linalg.norm(sample[:, None, :] - sample[None, :, :], axis=2)
    _np.fill_diagonal(d, _np.inf)
    med = float(_np.median(d.min(axis=1)))
    out["median_edge"] = med
    thr = max(edge_factor * med, min_threshold)
    out["threshold"] = thr

    def _fit(sel):
        if sel.sum() < 3:
            return _np.eye(3), 1.0, _np.zeros(3)
        Ac = A[sel] - A[sel].mean(0)
        Bc = B[sel] - B[sel].mean(0)
        H = Ac.T @ Bc
        U, S, Vt = _np.linalg.svd(H)
        sign = _np.sign(_np.linalg.det(Vt.T @ U.T))
        R = Vt.T @ _np.diag([1.0, 1.0, sign]) @ U.T
        denom = float((Ac ** 2).sum())
        s = float(S.sum() / denom) if denom > 0 else 0.0
        t = B[sel].mean(0) - s * (R @ A[sel].mean(0))
        return R, s, t

    sel = _np.ones(A.shape[0], bool)
    R, s, t = _fit(sel)
    for index in range(int(rounds)):
        res = _np.linalg.norm(s * (A @ R.T) + t - B, axis=1)
        new = res < thr
        out["rounds"] = index + 1
        if _np.array_equal(new, sel) or new.sum() < 3:
            break
        sel = new
        R, s, t = _fit(sel)
    res = _np.linalg.norm(s * (A @ R.T) + t - B, axis=1)
    # 【t76 修】循环里若首轮拟合就退化（`new.sum() < 3`）会 break，**`sel` 仍停在"全 1"**
    # ⇒ 会出现"内点 20300/20300 但残差 0.243"这种自相矛盾的假绿。这里**无条件按阈值重算一次**，
    # 并暴露内点比例供调用方判"这根本不是相似关系"（真门不看假绿）。
    sel = res < thr
    out.update({"R": R, "scale": s, "t": t, "inliers": sel, "residuals": res})
    out["inlier_ratio"] = float(sel.mean()) if sel.size else 0.0
    out["rms"] = float(_np.sqrt((res[sel] ** 2).mean())) if sel.any() else float("inf")
    out["max_residual"] = float(res.max()) if res.size else float("inf")
    return out


def bind_position_mismatch_problems(
    A_points, B_points, *, other_positions=None, source_pool=None,
    pool_tolerance: float = BIND_POSITION_EXACT_TOLERANCE,
    edge_factor: float = BIND_POSITION_EDGE_FACTOR,
    exact_tolerance: float = BIND_POSITION_EXACT_TOLERANCE,
) -> dict:
    """【t74】H-A 判据（纯函数）：``A`` = 源点、``B`` = 产物点（逐元素对应）。

    ``other_positions`` = "在用"的产物行位置（默认就用 ``B`` 自己）；离群行（残差 ≥ 阈值）中
    **位置精确等于 ``other_positions`` 里别的行**的那些 ⇒ 判为"位置被写成别行的"。

    返回 ``{"ok", "problems", "fit", "outlier_rows", "written_from_rows", "ledger"}``。
    """
    import numpy as _np

    A = _np.asarray(A_points, dtype=_np.float64)
    B = _np.asarray(B_points, dtype=_np.float64)
    pool = _np.asarray(other_positions if other_positions is not None else B, dtype=_np.float64)
    fit = kabsch_similarity(A, B, edge_factor=edge_factor)
    ledger = {"rows": int(A.shape[0]), "inliers": int(fit["inliers"].sum()),
              "rms": fit["rms"], "threshold": fit["threshold"],
              "median_edge": fit["median_edge"], "scale": fit["scale"],
              "inlier_ratio": float(fit.get("inlier_ratio", 0.0)),
              "max_residual": float(fit.get("max_residual", float("inf")))}
    problems: list = []
    written_from: list = []
    # 【t76】拟合本身不可信（对应关系根本不是相似变换）⇒ 这是**更严重**的红：
    # 产物几何与源几何不匹配（t72 的两例真实 H-A 的内点比例分别是 93.1% / 99.7% ⇒ 0.5 是安全地板）。
    BIND_FIT_MIN_INLIER_RATIO = 0.5
    if ledger["inlier_ratio"] < BIND_FIT_MIN_INLIER_RATIO:
        problems.append(
            "对应关系不是相似变换（内点比例 %.4f < %.2f、最大残差 %.4g、阈值 %.4g）"
            "⇒ 产物几何与源几何不匹配（不是「某几行写错」而是整段对不上："
            "要么源/产物不是同一姿态，要么产物被整体写坏了）"
            % (ledger["inlier_ratio"], BIND_FIT_MIN_INLIER_RATIO,
               ledger["max_residual"], ledger["threshold"])
        )
    outlier_rows = _np.nonzero(~fit["inliers"])[0]
    for row in outlier_rows.tolist():
        point = B[row]
        if pool.shape[0]:
            d = _np.linalg.norm(pool - point, axis=1)
            d[row] = _np.inf if row < d.shape[0] else d[row]
            match = int(_np.argmin(d))
            best = float(d[match])
        else:
            match, best = -1, float("inf")
        if best <= exact_tolerance:
            written_from.append({"row": int(row), "from_row": match,
                                 "residual": float(fit["residuals"][row]),
                                 "distance": best})
            problems.append(
                f"第 {row} 行的位置**精确等于另一在用行**（第 {match} 行，距离 {best:.3g}）"
                f"且与自身应有位置的残差 {float(fit['residuals'][row]):.4g}"
                f"（阈值 {fit['threshold']:.4g}、= {float(fit['residuals'][row]) / max(fit['threshold'], 1e-30):.1f}×）"
                " ⇒ 位置被写成了别行的位置（导出段拉丝的直接成因）"
            )
    ledger["outliers"] = int(outlier_rows.shape[0])
    ledger["written_from"] = len(written_from)

    # ------------------------------------------------------------------ #
    # 【t79 / t78-B】"反校准后不在源池"判据
    # ------------------------------------------------------------------ #
    # 为什么补它：H-A 的既有档（"位置精确等于另一在用行"）只覆盖"复制了**在用行**的位置"这一类；
    # 若某行的位置既不是自己的、又恰好不等于任何在用行，既有档会漏 ✓ t78-B 要求补上
    # 「反变换回源空间后，**不在源池**里」这一档，并按下列四条排除误报：
    #   ① 源池缺失 ⇒ 跳过（不判红）；② 跨族 ⇒ 源池取**全 12 部件并集**（调用方给的就是并集）；
    #   ③ 站位对象按 marker 排除（调用方不入池）；④ 拟合不可信时**不叠加**本档（避免重复报）。
    out_of_pool: list = []
    if source_pool is not None:
        pool_src = _np.asarray(source_pool, dtype=_np.float64)
        if pool_src.size and ledger["inlier_ratio"] >= 0.5:
            # 有相似变换 ⇒ **先反变换再比**（用拟合出的 (R,s,t) 的逆）。
            def _to_source(points):
                R, s, t = fit["R"], fit["scale"], fit["t"]
                if not _np.isfinite(s) or abs(s) < 1e-30:
                    return points
                return (points - t) @ R / s
            transformed = _to_source(_np.asarray(B_points, dtype=_np.float64))
            hits = 0
            for row in outlier_rows.tolist():
                point = transformed[row]
                d = _np.linalg.norm(pool_src - point, axis=1)
                nearest = int(_np.argmin(d)) if d.size else -1
                best = float(d[nearest]) if d.size else float("inf")
                if best > float(pool_tolerance):
                    hits += 1
                    out_of_pool.append({"row": int(row), "nearest_source_row": nearest,
                                        "nearest_distance": best,
                                        "residual": float(fit["residuals"][row])})
                    if len(out_of_pool) <= 4:
                        problems.append(
                            f"第 {row} 行的位置**反变换回源空间后不在源池里**"
                            f"（最近源行 {nearest}、距离 {best:.4g} > 容差 {pool_tolerance:g}；"
                            f"与自身应有位置的残差 {float(fit['residuals'][row]):.4g}）"
                            " ⇒ 该行位置不是任何一个源顶点的像（导出段写错行）"
                        )
            ledger["out_of_pool"] = hits
    if "out_of_pool" not in ledger:
        ledger["out_of_pool"] = 0
        ledger["out_of_pool_skipped"] = source_pool is None or ledger["inlier_ratio"] < 0.5
    return {"ok": not problems, "problems": problems, "fit": fit,
            "outlier_rows": outlier_rows.tolist(), "written_from_rows": written_from,
            "out_of_pool_rows": out_of_pool, "ledger": ledger}


def scan_part_bind_positions(bare: str, family: str, *, mod_root: str, workspace_root: str,
                             exact_tolerance: float = BIND_POSITION_EXACT_TOLERANCE) -> dict:
    """文件级包装：读 4 个文件 ⇒ :func:`bind_position_mismatch_problems`（只读）。

    读：``<mod>/Meshes/<fam>-Position.buf``（族池）、``<mod>/Meshes/LOD0.<bare>-Index.buf``
    （产物 per-part IB）、``<workspace>/LOD0/<bare>/TYPE_*/<bare>-{Position.buf,<bare>.ib}``（同一个部件在源空间的行与三角表）。
    """
    import numpy as _np

    def _read(path):
        with open(os.path.realpath(path), "rb") as handle:
            return handle.read()

    def _find(*names):
        root = os.path.join(workspace_root, "LOD0", bare)
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name in names:
                    return os.path.join(dirpath, name)
        return ""

    product_pool = os.path.join(mod_root, "Meshes", f"{family}-Position.buf")
    product_ib = os.path.join(mod_root, "Meshes", f"LOD0.{bare}-Index.buf")
    source_pos = _find(f"{bare}-Position.buf")
    source_ib = _find(f"{bare}.ib")
    missing = [p for p in (product_pool, product_ib, source_pos, source_ib) if not p or not os.path.isfile(p)]
    if missing:
        return {"ok": True, "skipped": True, "problems": [], "ledger": {"missing": missing},
                "bare": bare, "family": family}
    pool_raw = _read(product_pool)
    pool = _np.frombuffer(pool_raw[: len(pool_raw) // 44 * 44], dtype="<f4").reshape(-1, 11)[:, 0:3].astype(_np.float64)
    ib_pr = _np.frombuffer(_read(product_ib), dtype=_np.uint32).astype(_np.int64).reshape(-1, 3)
    src_raw = _read(source_pos)
    src = _np.frombuffer(src_raw[: len(src_raw) // 44 * 44], dtype="<f4").reshape(-1, 11)[:, 0:3].astype(_np.float64)
    ib_ws = _np.frombuffer(_read(source_ib), dtype=_np.uint32).astype(_np.int64).reshape(-1, 3)
    if ib_ws.shape != ib_pr.shape:
        return {"ok": True, "skipped": True, "bare": bare, "family": family,
                "problems": [], "ledger": {"ib_shape": [list(ib_ws.shape), list(ib_pr.shape)]}}
    if ib_pr.max() >= pool.shape[0] or ib_ws.max() >= src.shape[0]:
        return {"ok": True, "skipped": True, "bare": bare, "family": family,
                "problems": [], "ledger": {"ib_max": [int(ib_pr.max()), int(ib_ws.max())],
                                           "rows": [int(pool.shape[0]), int(src.shape[0])]}}
    # 逐元素对应：同一个 u 多数决到同一个 v（t72 口径）
    counts: dict = {}
    for k in range(ib_ws.size):
        key = int(ib_ws.ravel()[k])
        counts.setdefault(key, {})
        v = int(ib_pr.ravel()[k])
        counts[key][v] = counts[key].get(v, 0) + 1
    U = sorted(counts)
    V = [max(counts[u].items(), key=lambda kv: kv[1])[0] for u in U]
    A = src[_np.asarray(U, dtype=_np.int64)]
    B = pool[_np.asarray(V, dtype=_np.int64)]
    in_use = pool[_np.unique(ib_pr.ravel())]
    result = bind_position_mismatch_problems(A, B, other_positions=in_use,
                                             exact_tolerance=exact_tolerance)
    result.update({"bare": bare, "family": family, "skipped": False})
    return result


def scan_bind_positions(parts, *, mod_root: str, workspace_root: str) -> dict:
    """扫多个部件（``parts`` = ``[(bare, family), …]``）⇒ 汇总 ``{"ok", "parts", "problems"}``。"""
    reports = []
    problems: list = []
    for bare, family in parts or ():
        try:
            report = scan_part_bind_positions(str(bare), str(family), mod_root=mod_root,
                                              workspace_root=workspace_root)
        except Exception as exc:  # noqa: BLE001 - 检查失败不得反过来弄坏导出
            report = {"ok": True, "skipped": True, "bare": str(bare), "family": str(family),
                      "problems": [], "ledger": {"error": str(exc)}}
        reports.append(report)
        problems.extend(f"{report.get('bare')}: {item}" for item in report.get("problems") or ())
    return {"ok": not problems, "parts": reports, "problems": problems}


def exact_key_family(info: dict, point, *, family_order=()) -> str:
    """**旧口径**（t41 用的精确量化键 ``round(x*1e5)``，族次序先到先得）——只为统计翻转。"""
    keys = dict(info or {}).get("keys") or {}
    if not keys:
        return ""
    key = (round(float(point[0]), 5), round(float(point[1]), 5), round(float(point[2]), 5))
    return str(keys.get(key, "") or "")
