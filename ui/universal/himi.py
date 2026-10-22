"""HIMI（崩坏3 / HI3）导出器：静态合并骨架的导出侧接线（T7 交付）。

职责边界（计划书 §6.3 / §7）：
* **数据层**（``common/himi_skeleton.py``，T6）负责反查、去重、落盘，**并持有 G1
  门控的唯一实现**（``prepare(target_bares=本次导出集合)``）；站位（stub）机制的
  **判据本体**也在那里（``decide_stub_injection``，T22）。
* **运行时计划**（``common/himi_merged_runtime.py``）只**消费**该门控结论，把落盘
  结果翻译成资源声明 + 可重绑的 deform pass 清单。
* **本模块**只做接线：导出前算计划并过契约；把 ``vs-t0`` 重绑与
  ``draw = <deform 顶点数>`` 注入 Blend 类别的 ``TextureOverride`` 段；
  追加角色级骨架资源声明；把骨架二进制发布到 mod 的缓冲目录；为**被吸收的缺席
  部件**注入 3 顶点占位对象以抑制原版 draw（计划书 §7.7）。

运行时**唯一**动作 = 该 deform pass 的 ``vs-t0`` 换成静态合并骨架
（``vb1`` 本来就是这个 mod 的 Blend，不变）。HIMI 不走 ZZMI 的 attach CS /
出现次槽位 / 每槽守卫 / 姿态指纹（计划书 D01/D10/N02/N07）。

站位机制（T22 / 计划书 §7.7）
-----------------------------
D09 的「整体不移植」已被重新裁定为错：站位承载两条**独立**作用，D09 的理由
（「合并不改几何拓扑」）只覆盖了作用 A（让缺席部件的 deform pass 跑起来填骨架，
HIMI 静态骨架 ⇒ 无对应），**完全没有覆盖作用 B（抑制原版 draw 防重影）**。关键一跳：
HIMI 的 IB override 段只为「场景里存在对象的部件」生成（``ui/universal/unity.py:75-125``）
⇒ 部件缺席 ⇒ mod 里没有它的 IB 段 ⇒ ``handling = skip`` 不生效 ⇒ 原版那一笔照跑。

**方向与 ZZMI 相反（保守）**：HIMI 的站位不承载作用 A ⇒ 插桩的唯一效果就是压掉
原版 draw ⇒ 宽松口径会「静默让用户原版部件消失」。少压 = 可见重影（用户立刻发现），
多压 = 静默部件消失（用户以为提取失败）⇒ 退化情形一律**不插桩**。
EFMI 侧 t37「宁松勿漏」裁决的前提是作用 A，**在 HIMI 不成立、不得照搬**。

非合并路径**逐字节不变**：所有新增行都在「该部件带合并数据 且 通过 G1」时才出现，
否则逐字走基类实现（``ui/universal/unity.py``）——由
``tests/test_himi_merged_runtime.py::HIMIExporterWiringTests::test_vb_section_without_plan_matches_base``
逐行锁死；站位也**完全不调用**（``import_merged_vgmap()`` 关闭时）。

注入顺序（T26 F1）
------------------
站位注入发生在 ``__init__`` 里、**基类组装模型之前**（形状对齐
``ui/universal/zzmi.py:443`` 注入 → ``:445`` ``super()``）。这不是风格问题：
``ui/universal/drawib_export_base.py:11-16`` 在它自己的 ``__init__`` 里**立即**把
``ordered_draw_obj_data_model_list`` 解析成 ``drawib_model_list``；注入晚一步，站位
DrawCall 就只留在 ``ordered`` 里而**永远进不了**
``drawib_model_list[*].submesh_model_list[*].drawcall_model_list`` ⇒ 站位部件的 IB
override 段（``handling = skip``）不生成 ⇒ 抑制重影失效。
``tests/test_himi_stub_objects.py::StubInjectionOrderTests`` 是这条的回归钉子
（实测：改回旧顺序该用例失败、恢复后通过）。

顺序修正带来一个**必须同时处理**的副作用：站位件现在真的会出现在
``drawib_model_list`` 里，而 ``SubmeshModel.vg_count`` 是按 **unique_str** 从工作空间
json 解析的（``common/submesh_metadata.py:178``）—— 缺席部件的 json 往往仍然存在
（它是被 join 走的，不是没提取过）⇒ 它的 ``vg_count`` 可能 > 0。

**口径（已反转）：站位 bare 进 `export_bares`。** 「排除站位」看似更干净，但它
**必然中止导出** —— 判据只在「缺席部件**有** `VGMap` 且专属槽位被引用」时插桩
⇒ 能插桩的缺席部件**必然已落盘**；而 G1 家族门是 `L(ib) ⊆ T`、`L(ib)` 取
**已落盘**组件 ⇒ 排除站位会让被吸收成员永远 `missing`、家族永远不 `safe`、契约
`level=error` ⇒ `Fatal` 中止 —— 而那个场景**正是**站位要解决的场景。ZZMI 同语义
（站位 DrawCall 进 `ordered`，`ui/universal/zzmi.py:1008-1013`）。

⇒ ``is_stub`` 只作**只读标注**（``himi_stub_bares()`` 供诊断/上层查询），
**不参与过滤**；过滤口径只有一个事实源：``vg_count > 0``。回归钉子
``StubInjectionOrderTests::test_stub_bare_enters_export_set_to_close_the_family_gate``
（反证链的独立复核见 ``reports/himi-merge/18-stub-rereview.md`` §4.2；规格裁定见
计划书 §7.7.11 与 ``reports/himi-merge/05-decisions.md`` §3.20）。
"""

import math
import os
import re

from ...common import himi_merged_runtime as himi_runtime
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...common.himi_merged_contract import (
    DEFORM_GATE_REJECT_PREFIX,
    evaluate_merged_skeleton_contract,
)
from ...utils.json_utils import JsonUtils
from ...common.himi_skeleton import (
    HIMI_COMPONENT_MAP_BASENAME,
    HIMI_MERGED_SKELETON_METADATA_FILE,
    HIMI_STUB_MARKER,
    HIMIStubDrawCall,
    HIMI_STUB_TRIANGLE_VERTICES,
    bare_from_unique_str,
    blender_collect_used_group_ids,
    blender_create_stub_object,
    blender_object_present,
    blender_purge_stale_stub_objects,
    blender_remove_stub_object_data,
    blender_remove_stub_object_data_by_name,
    blender_vertex_group_index_name_problems,
    decide_stub_injection,
    load_component_family_map,
)
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from .unity import ExportUnity

_LOG_PREFIX = "[HIMI骨骼合并]"
#: 站位诊断前缀（与合并骨架诊断分开，便于用户区分两件事）
_STUB_LOG_PREFIX = "[HIMI站位]"
#: 本环境无法取证的清单（计划书 §11 V1/V2/V3 + §7.4 R3）；导出时打印，绝不静默。
UNVERIFIED_IN_GAME = himi_runtime.UNVERIFIED_IN_GAME


def _himi_buffer_byte_size(payload) -> int:
    """缓冲载荷的**字节数**（``bytes``/``bytearray`` 直接量长；numpy 数组用 ``nbytes``）。

    ``category_buffer_dict`` 的值既可能是 bytes，也可能是 numpy 视图（基类在别处用
    ``payload.tobytes()``）⇒ 宿主族判定必须两条都吃，否则会把「数组」误判成 0 字节。
    """
    if payload is None:
        return 0
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    nbytes = getattr(payload, "nbytes", None)
    if nbytes is not None:
        try:
            return int(nbytes)
        except (TypeError, ValueError):
            return 0
    try:
        return len(payload)
    except TypeError:
        return 0


# --------------------------------------------------------------------------- #
# t75：**单个**合并 attach CS（5 个族共用一份）+ `cs =` 引用硬门
# --------------------------------------------------------------------------- #
#
# 口径（用户 2026-09-25 明确："这个着色器需要合并成一个"）：
#
#   * `<mod>/res/` 下**只有一个** `himi_attach.hlsl`，ini 里 5 条 `cs =` **指向同一路径**；
#   * 因此「本族在合并骨架里的目标基址」**不能再逐族写进 #define** —— 必须在**同一个** shader
#     内部推出来。做法：读**源 palette 资源自身**的槽位数（184/71/51/19/18），
#     在一张**固定表**里查出目标基址（184→0、18→184、71→202、19→273、51→292）。
#   * 槽位数一律走 `GetDimensions`（运行期真值），**绝不读 `IniParams`**：
#     ini 参数纹理布局随 3DMigoto fork 变化（标准版 `x1/y1` 落 `IniParams[0]`，
#     Armor fork 从 `[1]` 起），读错位置只会读到未设置的 0 ⇒ CS 静默不写 ⇒ 骨架留空
#     ⇒ 顶点位置全零、模型爆炸且**不报任何错**（本仓 key 记忆 2026-09-22 实测形态）。
#   * `HIMI_MERGED_SKELETON_SLOTS` 是**该 ini 这一版合并骨架的总槽位数**，由导出侧写进
#     文件；它只用于「写入越界保护」的上界，**不参与**基址推导（基址只由源槽位数决定）。
#
# 为什么基址表是常量而不是从 ini 传：合并骨架的槽位排布是导出侧决定的（`plan.family_ranges`），
# 而 CS 文件在 mod 里是**静态文本**。把「源槽位数 → 基址」写成表，等价于把排布**冻结进 shader**；
# 排布一变（槽位数变了）表格就不再覆盖，此时 `base_slot = uint(-1)` 会让所有写入被
# 越界保护挡掉 —— **宁可一根骨都不写，也不写错位置**。

#: 合并 attach CS 的文件名（**一个**，不是每族一个）
MERGED_ATTACH_HLSL_BASENAME = "himi_attach.hlsl"
#: ini 里引用的相对路径（从 ini 自身所在目录 = mod 根算起）
MERGED_ATTACH_HLSL_PATH = "./res/" + MERGED_ATTACH_HLSL_BASENAME
#: HLSL 里的槽位/基址常量名（测试据它核对，不与 `common/himi_merged_runtime.py` 的
#: 逐族 `HIMI_ATTACH_BASE` 接口混用）
MERGED_ATTACH_SLOTS_MACRO = "HIMI_MERGED_SKELETON_SLOTS"
#: 字符串形式的基址表（同时用于渲染与测试核对）
MERGED_ATTACH_BASE_TABLE_LINES = (
    "    if (source_slots == 184u) { base_slot = 0u; }        // 2c706fbd",
    "    else if (source_slots == 18u) { base_slot = 184u; }   // 694025c6",
    "    else if (source_slots == 71u) { base_slot = 202u; }   // 70a4da3e",
    "    else if (source_slots == 19u) { base_slot = 273u; }   // b35cfc6c",
    "    else if (source_slots == 51u) { base_slot = 292u; }   // def0c0ae",
)
#: 【t74】运行期 X 的文本探针：`(HLSL 片段, 缺它时的报错)`。
#: 缺失任意一条都必须让 `merged_attach_hlsl_problems` 报错（测试用删行变异逐条证红）——
#: 这几行合起来才是「X 由运行期 cb3 算」这件事本身：少 cbuffer 就取不到本族物体矩阵，
#: 少 u1 表就没有宿主族 M_base 可读，少选择分支就退化成抓帧常量。
HIMI_RUNTIME_X_PROBES = (
    (
        "cbuffer himi_object_cb : register(b1)",
        "【t74】缺 cbuffer himi_object_cb : register(b1)（cs-cb1 = 本族 vs-cb3 的副本）",
    ),
    (
        "float4 himi_obj_rows[8];",
        "【t74】cbuffer 必须按 8 行 float4 读 vs-cb3（行 0..3 = 物体矩阵，行 4..7 = 其逆）",
    ),
    (
        "RWBuffer<float4> himi_object_matrix : register(u1);",
        "【t74】缺物体矩阵表声明（RWBuffer<float4> himi_object_matrix : register(u1);）——"
        "没有它宿主族读不到 M_base，X 只能取抓帧常量",
    ),
    (
        "HimiAffine himi_cb3_affine()",
        "【t74】缺「把本族 vs-cb3 行 0..3 收成 Affine」的函数定义（HimiAffine himi_cb3_affine()）",
    ),
    (
        "himi_cb3_affine();",
        "【t74】缺「把本族 vs-cb3 行 0..3 收成 Affine」的读取（himi_cb3_affine()）",
    ),
    (
        "HimiAffine himi_cb3_inverse_affine()",
        "【t74】缺 vs-cb3 行 4..7 的函数定义（HimiAffine himi_cb3_inverse_affine()）——"
        "校验物体矩阵必须核对自洽性，否则会把错口径的行当成物体矩阵",
    ),
    (
        "himi_cb3_inverse_affine()",
        "【t74】缺 vs-cb3 行 4..7 的读取（himi_cb3_inverse_affine()）——"
        "校验物体矩阵必须核对自洽性，否则会把错口径的行当成物体矩阵",
    ),
    (
        "if (asuint(attach_range.z) >= 4u)",
        "【t74】缺版本门（if (asuint(attach_range.z) >= 4u)）——旧产物（v3）必须逐字节走老路径",
    ),
    (
        "bool himi_object_matrix_valid()",
        "【t74】缺物体矩阵校验（himi_object_matrix_valid()：齐次 w、|det|、行 4..7 与行 0..3 互逆）",
    ),
    (
        "himi_affine_inverse(m_base)",
        "【t74】缺 M_base⁻¹（himi_affine_inverse(m_base)）——没有逆就只能按宿主族摆放",
    ),
    (
        "himi_affine_mul(himi_affine_inverse(m_base), own_object)",
        "【t74】缺 X = M_base⁻¹·M_own 的合成（himi_affine_mul(himi_affine_inverse(m_base), own_object)）",
    ),
    (
        "uint own_index = asuint(himi_attach_range[7].x);",
        "【t74】缺本族索引（uint own_index = asuint(himi_attach_range[7].x);）——"
        "载荷 [7].x 是导出期写死的族序号",
    ),
    (
        "uint base_index = asuint(himi_attach_range[7].y);",
        "【t74】缺宿主族索引（uint base_index = asuint(himi_attach_range[7].y);）",
    ),
    (
        "if (own_index == base_index)",
        "【t74】缺宿主族分支（if (own_index == base_index) ⇒ X = 单位阵）",
    ),
    (
        "himi_object_matrix[own_row + 0u] = own_object.r0;",
        "【t74】缺「把本族校验通过的物体矩阵写进 u1 表」——"
        "不写的话宿主族永远读不到任何 M_base，X 无法实时算出",
    ),
    (
        "float4 base0 = himi_object_matrix[base_row + 0u];",
        "【t74】缺宿主族行的读回（float4 base0/1/2 = himi_object_matrix[base_row + 0/1/2u]）",
    ),
    (
        "float4 own0 = himi_object_matrix[own_row + 0u];",
        "【t74】缺「采集无效时读表里本族那段」的回退（float4 own0/1/2 = himi_object_matrix[own_row + 0/1/2u]）——"
        "deform 段那 368 B 的 cb3 不是物体矩阵，这时只能退回上一帧 render 写下的好值",
    ),
    (
        "else if (object_in_range)",
        "【t74】缺「采集无效时的回退分支」（else if (object_in_range)）——"
        "采集失败就直接用烘死 X，等于运行期 X 从未生效",
    ),
    (
        "if (own_ready && base_ready)",
        "【t74】回退分支必须要求表里 own/base 两段都非全零（if (own_ready && base_ready)）——"
        "任一段空就乘进去会得到垃圾 X",
    ),
)
#: `cs =` 行的解析（3DMigoto 的 ini 键名大小写不敏感，故用 IGNORECASE）。
#: 值允许为空（``cs =``）—— 那是一条**坏的**引用，必须被抓到而不是被跳过，
#: 所以 `(\S*)` 的星号不能少。
_CS_REFERENCE_RE = re.compile(r"^\s*cs\s*=\s*(\S*)", re.IGNORECASE)


def merged_attach_hlsl_text(decl=None) -> str:
    """渲染**单个**合并 attach CS（5 个族共用）。

    ``decl`` 只用于签出该 ini 这一版合并骨架的总槽位数（写越界保护的上界）；缺省取
    参考声明。内容与「每族一个 CS + ``#define HIMI_ATTACH_BASE``」的旧口径**不兼容**：
    这里没有基址宏、只有运行期查表（见文件头那段注释）。
    """
    if decl is None:
        decl = himi_runtime.build_merged_skeleton_decl(
            himi_runtime.REFERENCE_ROLE_KEY, himi_runtime.REFERENCE_SLOT_COUNT
        )
    slots = int(getattr(decl, "slots", 0) or 0)
    skeleton_name = str(getattr(decl, "name", "") or "")
    lines: list = []
    lines.append("// himi_attach.hlsl —— HIMI（崩坏3 / HI3）合并骨架 attach CS（**单文件，5 族共用**）")
    lines.append("// 由 ui/universal/himi.py::merged_attach_hlsl_text() 生成；请勿手改。")
    lines.append("//")
    lines.append("// 【为什么只有一个文件】用户口径：这份着色器**合并成一个**。5 个 deform 族段")
    lines.append("// （[CustomShaderHI3MergedSkeletonAttach_<fam>]）的 `cs =` 全部指向本文件，")
    lines.append("// 每个段只是把**自己那一族的当帧 palette 副本**绑到 cs-t0、把合并骨架绑到 cs-u0。")
    lines.append("//")
    lines.append("// 【为什么没有 #define HIMI_ATTACH_BASE】旧口径每族一个文件、基址写死在各文件里。")
    lines.append("// 合并成一个文件后，基址必须由**运行期真值**推出来：读源 palette 自身的槽位数")
    lines.append("// （184/71/51/19/18），查下面那张**固定表**得到该族在合并骨架里的起始槽。")
    lines.append("// 表是「导出侧排布」的冻结副本：排布变了（槽位数变了）表就不再覆盖，此时")
    lines.append("// base_slot 保持 0xFFFFFFFF，越界保护会把本 dispatch 的所有写入挡掉 ——")
    lines.append("// **宁可一根骨都不写，也绝不写进别的族的槽位**。")
    lines.append("//")
    lines.append("// 【机制链（逐帧，顺序不可换）】")
    lines.append("//   ① deform VB 段先捕获**当帧** palette：")
    lines.append("//        ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null      ← LHS 是**目标**")
    lines.append("//   ② run = CustomShaderHI3MergedSkeletonAttach_<fam> → 本 CS 把该副本搬到合并骨架的")
    lines.append("//        [base_slot, base_slot + 源槽位数) 区间；")
    lines.append("//   ③ 之后才把 vs-t0 换绑成合并骨架（供游戏 deform VS 读取），最后 handling = skip + draw。")
    lines.append("//   顺序由导出侧护栏（ui/universal/himi.py::blend_block_order_problems）在产物落盘前拦死。")
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
    lines.append("//     骨架留空 ⇒ 顶点位置全零，且**不报任何错**。")
    lines.append("//")
    lines.append("// 【本环境未取证的项（不得当成已成立）】")
    for item in himi_runtime.UNVERIFIED_IN_GAME:
        lines.append(f"//  - {item}")
    lines.append("//")
    lines.append("// ---- 机器可读声明行（测试据它与 ini 逐字段比对）----")
    lines.append("// " + decl.machine_line())
    lines.append("//")
    lines.append("// ---- 资源声明（必须与 ini 的 [{0}] 段逐字段一致）----".format(skeleton_name))
    lines.append(f"#define {MERGED_ATTACH_SLOTS_MACRO} {slots}u")
    lines.append(f"#define HIMI_MERGED_SKELETON_STRIDE {himi_runtime.MERGED_SKELETON_STRIDE}u")
    lines.append(
        "// 身份魔数（身份资源是否可用：不可用则退回冻结表 = 今天的行为）"
    )
    lines.append(
        f"#define {himi_runtime.ATTACH_RANGE_MAGIC_MACRO} {himi_runtime.ATTACH_RANGE_MAGIC}u"
    )
    lines.append("")
    lines.append("// cs-t0 = ref ResourceHI3PaletteFam_<fam>（ini: Buffer / stride = 48 / 无 format）")
    lines.append("Buffer<float4> himi_src_palette : register(t0);")
    lines.append(
        "// cs-t1 = ref ResourceHI3AttachRange_<fam>"
        "（ini: Buffer + format = R32G32B32A32_FLOAT + filename；16 字节 = 本族 base/span）"
    )
    lines.append("Buffer<float4> himi_attach_range : register(t1);")
    lines.append("// cs-u0 = ref " + skeleton_name)
    lines.append("// （ini: RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数 + bind_flags）")
    lines.append(decl.hlsl_declaration())
    lines.append("")
    lines.append("// ---- 【t74】族级物体矩阵 X_f 的运行期来源（不再只靠抓帧烘死的常量）----")
    lines.append("// cs-cb1 = ref ResourceHI3Cb3_<fam>（ini: **只有段名一行**，不写 type/stride ——")
    lines.append("//   尺寸与布局由 copy vs-cb3 的源视图决定；同一先例见本仓 ZZMI 的 copy vs-cb1）")
    lines.append("// 该族那一笔 deform draw 的 vs-cb3 前 8 行 = 它的物体矩阵：")
    lines.append("//   行 0..2 = 3×3 线性（**行向量**约定）、行 3 = 平移（w ≈ 1）、行 4..7 = 前 4 行的逆")
    lines.append("//   （行 4..7 是游戏自己算好的，用来交叉校验我们读到的口径对不对）。")
    lines.append("cbuffer himi_object_cb : register(b1)")
    lines.append("{")
    lines.append("    float4 himi_obj_rows[8];")
    lines.append("};")
    lines.append("// cs-u1 = ref ResourceHI3ObjectMatrix_role_<role>")
    lines.append("// （ini: RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 族数")
    lines.append("//   + bind_flags = shader_resource unordered_access）")
    lines.append("// 表的**唯一**布局：第 f 族占 [f*3 + 0..2]，表项 i = 该族 3×4 矩阵第 i 行**逐字**")
    lines.append("// ⇒ (r0.xyz, r3.x) / (r1.xyz, r3.y) / (r2.xyz, r3.z)：第 i 行的 .w 正是平移行 r3 的")
    lines.append("// 第 i 个分量，12 个数一个不浪费、也没有第二套下标约定可写错。全零 = 尚未写入。")
    lines.append("RWBuffer<float4> himi_object_matrix : register(u1);")
    lines.append("")
    lines.append("// 3×4 仿射（列向量约定，行主序存三行；.w = 平移分量）。结构与乘法照 ZZMI 参考插件")
    lines.append("// （.dsh-tmp/loyaltools/common/zzmi_merged_skeleton.py:493-557，HLSL 里已验证可行）。")
    lines.append("struct HimiAffine")
    lines.append("{")
    lines.append("    float4 r0;")
    lines.append("    float4 r1;")
    lines.append("    float4 r2;")
    lines.append("};")
    lines.append("")
    lines.append("HimiAffine himi_affine_identity()")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = float4(1.0, 0.0, 0.0, 0.0);")
    lines.append("    m.r1 = float4(0.0, 1.0, 0.0, 0.0);")
    lines.append("    m.r2 = float4(0.0, 0.0, 1.0, 0.0);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("float4 himi_affine_mul_row(float4 row, HimiAffine b)")
    lines.append("{")
    lines.append("    return row.x * b.r0 + row.y * b.r1 + row.z * b.r2 + float4(0.0, 0.0, 0.0, row.w);")
    lines.append("}")
    lines.append("")
    lines.append("HimiAffine himi_affine_mul(HimiAffine a, HimiAffine b)")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = himi_affine_mul_row(a.r0, b);")
    lines.append("    m.r1 = himi_affine_mul_row(a.r1, b);")
    lines.append("    m.r2 = himi_affine_mul_row(a.r2, b);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("// 余子式求逆（照参考插件的写法）：|det| 太小 ⇒ 返回单位阵（调用方另有校验门）")
    lines.append("HimiAffine himi_affine_inverse(HimiAffine m)")
    lines.append("{")
    lines.append("    float3 a = m.r0.xyz;")
    lines.append("    float3 b = m.r1.xyz;")
    lines.append("    float3 c = m.r2.xyz;")
    lines.append("    float3 c0 = cross(b, c);")
    lines.append("    float3 c1 = cross(c, a);")
    lines.append("    float3 c2 = cross(a, b);")
    lines.append("    float det = dot(a, c0);")
    lines.append("    HimiAffine r = himi_affine_identity();")
    lines.append("    if (abs(det) >= 1e-20)")
    lines.append("    {")
    lines.append("        float inv_det = 1.0 / det;")
    lines.append("        float3 i0 = float3(c0.x, c1.x, c2.x) * inv_det;")
    lines.append("        float3 i1 = float3(c0.y, c1.y, c2.y) * inv_det;")
    lines.append("        float3 i2 = float3(c0.z, c1.z, c2.z) * inv_det;")
    lines.append("        float3 t = float3(m.r0.w, m.r1.w, m.r2.w);")
    lines.append("        r.r0 = float4(i0, -dot(i0, t));")
    lines.append("        r.r1 = float4(i1, -dot(i1, t));")
    lines.append("        r.r2 = float4(i2, -dot(i2, t));")
    lines.append("    }")
    lines.append("    return r;")
    lines.append("}")
    lines.append("")
    lines.append("// cb3 行 0..3 → 列向量口径 3×4（与 common/himi_family_x.py::_col_t 同一映射：")
    lines.append("// 结果行 i = (行0[i], 行1[i], 行2[i], 行3[i])）")
    lines.append("HimiAffine himi_cb3_affine()")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = float4(himi_obj_rows[0].x, himi_obj_rows[1].x, himi_obj_rows[2].x, himi_obj_rows[3].x);")
    lines.append("    m.r1 = float4(himi_obj_rows[0].y, himi_obj_rows[1].y, himi_obj_rows[2].y, himi_obj_rows[3].y);")
    lines.append("    m.r2 = float4(himi_obj_rows[0].z, himi_obj_rows[1].z, himi_obj_rows[2].z, himi_obj_rows[3].z);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("// cb3 行 4..7 → 游戏自己算好的那份逆（同一映射）")
    lines.append("HimiAffine himi_cb3_inverse_affine()")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = float4(himi_obj_rows[4].x, himi_obj_rows[5].x, himi_obj_rows[6].x, himi_obj_rows[7].x);")
    lines.append("    m.r1 = float4(himi_obj_rows[4].y, himi_obj_rows[5].y, himi_obj_rows[6].y, himi_obj_rows[7].y);")
    lines.append("    m.r2 = float4(himi_obj_rows[4].z, himi_obj_rows[5].z, himi_obj_rows[6].z, himi_obj_rows[7].z);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("float himi_affine_max_diff(HimiAffine a, HimiAffine b)")
    lines.append("{")
    lines.append("    float4 d0 = abs(a.r0 - b.r0);")
    lines.append("    float4 d1 = abs(a.r1 - b.r1);")
    lines.append("    float4 d2 = abs(a.r2 - b.r2);")
    lines.append("    float diff = max(max(max(d0.x, d0.y), max(d0.z, d0.w)),")
    lines.append("                     max(max(d1.x, d1.y), max(d1.z, d1.w)));")
    lines.append("    return max(diff, max(max(d2.x, d2.y), max(d2.z, d2.w)));")
    lines.append("}")
    lines.append("")
    lines.append("// cb3 副本是否可信。判据与 common/himi_family_x.py::object_matrix_from_registers 同一套")
    lines.append("// （残差容差放大到 1e-2：副本内容的精度由 copy vs-cb3 的源视图决定）：")
    lines.append("//   ① 行 0..2/4..6 的 .w ≈ 0、行 3/7 的 .w ≈ 1（齐次行形状）；")
    lines.append("//   ② |det3(行 0..2.xyz)| ≥ 1e-6（可逆，否则 M_base⁻¹ 无意义）；")
    lines.append("//   ③ 行 0..3 与行 4..7 互为逆（compose 回单位阵，残差 ≤ 1e-2）。")
    lines.append("// ③ 同时挡住「cb3 绑空/全零」与「cb3 里是别的族的矩阵」两种失败 ⇒ 各失败都退回载荷 X。")
    lines.append("bool himi_object_matrix_valid()")
    lines.append("{")
    lines.append("    if (abs(himi_obj_rows[0].w) > 1e-3 || abs(himi_obj_rows[1].w) > 1e-3")
    lines.append("        || abs(himi_obj_rows[2].w) > 1e-3 || abs(himi_obj_rows[3].w - 1.0) > 1e-3")
    lines.append("        || abs(himi_obj_rows[4].w) > 1e-3 || abs(himi_obj_rows[5].w) > 1e-3")
    lines.append("        || abs(himi_obj_rows[6].w) > 1e-3 || abs(himi_obj_rows[7].w - 1.0) > 1e-3)")
    lines.append("    {")
    lines.append("        return false;")
    lines.append("    }")
    lines.append("    float3 la = himi_obj_rows[0].xyz;")
    lines.append("    float3 lb = himi_obj_rows[1].xyz;")
    lines.append("    float3 lc = himi_obj_rows[2].xyz;")
    lines.append("    if (abs(dot(la, cross(lb, lc))) < 1e-6)")
    lines.append("    {")
    lines.append("        return false;")
    lines.append("    }")
    lines.append("    HimiAffine product = himi_affine_mul(himi_cb3_affine(), himi_cb3_inverse_affine());")
    lines.append("    return himi_affine_max_diff(product, himi_affine_identity()) <= 1e-2;")
    lines.append("}")
    lines.append("")
    lines.append("[numthreads(64, 1, 1)]")
    lines.append("void main(uint3 dispatch_id : SV_DispatchThreadID)")
    lines.append("{")
    lines.append("    uint bone_index = dispatch_id.x;")
    lines.append("")
    lines.append("    // 源槽数取**资源自身维度**：typed Buffer<float4> 的 GetDimensions 是**单参**形式，")
    lines.append("    // 返回 float4 元素数 = 槽数 × 3；必须 /3u 还原成槽数。")
    lines.append("    uint palette_elements = 0u;")
    lines.append("    himi_src_palette.GetDimensions(palette_elements);")
    lines.append("    uint source_slots = palette_elements / 3u;")
    lines.append("")
    lines.append("    // 【本族识别 + 目标基址】由源槽位数查固定表 —— 这是本文件里**唯一**的排布知识来源，")
    lines.append("    // 不读 IniParams、不读逐族 #define。查不到时保持 0xFFFFFFFF ⇒ 全部写入被越界保护挡掉。")
    lines.append("    uint base_slot = 0xFFFFFFFFu;")
    lines.extend(MERGED_ATTACH_BASE_TABLE_LINES)
    lines.append("")
    lines.append("    // 【F5 本族身份资源（cs-t1）】它由导出侧按**本次** plan.family_ranges 生成")
    lines.append("    // （16 字节 = (base, span, version, magic)），是 base/span 的**权威**来源。")
    lines.append("    // 为什么必须独立于 source_slots：单文件方案里 span 与查表键同源 ⇒ 只按源维度")
    lines.append("    // 度量时「本族 span 上界」是恒真判据；副本维度一旦大于本族 span（角色的 palette")
    lines.append("    // 池常大于被引用槽数），多出的骨就会写进**下一族的块**（报告 50 §1.2 入口 ①）。")
    lines.append("    // 身份不可用（绑空 / magic 不符）时**退回冻结表** = 与今天逐字节等价的行为：")
    lines.append("    // 不新增「模型消失」的失败模式，也绝不静默乱写。")
    lines.append("    uint identity_base = 0xFFFFFFFFu;")
    lines.append("    uint span_slots = 0u;")
    lines.append("    float4 attach_range = himi_attach_range[0];")
    lines.append("    // 【t18 / T5-F1】身份**可用** = 魔数命中 **且 span > 0**：span=0 与上面")
    lines.append("    // `uint span_slots = 0u` 的初值同义（= 身份不可用）⇒ 一并退回冻结表，")
    lines.append("    // 与「资源绑空 / 魔数不符」走同一条路（模型侧 attach_write_target 同语义）。")
    lines.append(
        f"    if (asuint(attach_range.w) == {himi_runtime.ATTACH_RANGE_MAGIC_MACRO}"
        " && asuint(attach_range.y) > 0u)"
    )
    lines.append("    {")
    lines.append("        identity_base = asuint(attach_range.x);")
    lines.append("        span_slots = asuint(attach_range.y);")
    lines.append("    }")
    lines.append("    if (identity_base != 0xFFFFFFFFu)")
    lines.append("    {")
    lines.append("        if (base_slot != 0xFFFFFFFFu && base_slot != identity_base)")
    lines.append("        {")
    lines.append("            return;   // 冻结表与本次排布**不一致**：拒写（绝不冒用别的族的槽位）")
    lines.append("        }")
    lines.append("        base_slot = identity_base;   // 身份资源权威（冻结表只作交叉核对）")
    lines.append("    }")
    lines.append("")
    lines.append("    uint skeleton_elements = 0u;")
    lines.append(f"    {skeleton_name}.GetDimensions(skeleton_elements);")
    lines.append("    uint skeleton_slots = skeleton_elements / 3u;")
    lines.append("")
    lines.append("    // 逐 dispatch 的越界保护：源越界、族未识别、**本族 span**、目标越界，任一成立就不写。")
    lines.append("    if (bone_index >= source_slots)")
    lines.append("    {")
    lines.append("        return;")
    lines.append("    }")
    lines.append("    if (base_slot == 0xFFFFFFFFu)")
    lines.append("    {")
    lines.append("        return;   // 未知族：不写，绝不冒用别的族的槽位")
    lines.append("    }")
    lines.append("    // 【F5】本族 span 上界：副本维度**大于**本族 span 时**截断**（本线程不写），")
    lines.append("    // 绝不把多出来的骨写进下一族的块。span 来自身份资源，与 source_slots 独立。")
    lines.append("    if (span_slots > 0u && bone_index >= span_slots)")
    lines.append("    {")
    lines.append("        return;")
    lines.append("    }")
    lines.append("    uint target_slot = base_slot + bone_index;")
    lines.append("    if (target_slot >= skeleton_slots)")
    lines.append("    {")
    lines.append("        return;   // 越界保护：宁可少写，也不写坏别人的槽")
    lines.append("    }")
    lines.append("")
    lines.append("    // 每根骨占 3 个 float4（4×3 行主序矩阵），两端都显式按 *3 取址 ——")
    lines.append("    // 与两侧的 typed 16 B 视图精确匹配。")
    lines.append("    uint src = bone_index * 3u;                 // 读侧索引：bone * 3u")
    lines.append(f"    uint dst = target_slot * 3u;                // 写侧索引：slot * 3u")
    lines.append("")
    lines.append("    // 【t39/t44 族级校准·palette 侧】写进合并骨架的必须是 P'_f[b] = P_f[b] ∘ Q_f⁻¹")
    lines.append("    // （Q_f = 该族 palette 第 0 槽；因子 Q_f⁻¹ 把「提取帧世界」映到「当前世界」；")
    lines.append("    //  顶点侧已先把本族绑定位置搬进共同空间 v' = Q_f·v）")
    lines.append("    // ⇒ 同一族的骨 × 同一族的顶点 **逐值不变**（P·Q⁻¹·Q·v = P·v），")
    lines.append("    // 而跨族借用别人的骨时，顶点会先被搬进**共同空间**再被借出族的 palette 映出去。")
    lines.append("    // 因子与 base/span 同源同报（同一个 cs-t1 载荷，128 B），版本 < 3 或因子全零 ⇒ 不校准。")
    lines.append("    float4 placement0 = himi_attach_range[1];")
    lines.append("    float4 placement1 = himi_attach_range[2];")
    lines.append("    float4 placement2 = himi_attach_range[3];")
    lines.append("    bool calibrated = (asuint(attach_range.z) >= 3u)")
    lines.append("        && (placement0.x != 0.0 || placement0.y != 0.0 || placement0.z != 0.0")
    lines.append("            || placement0.w != 0.0 || placement1.x != 0.0 || placement1.y != 0.0")
    lines.append("            || placement1.z != 0.0 || placement1.w != 0.0 || placement2.x != 0.0")
    lines.append("            || placement2.y != 0.0 || placement2.z != 0.0 || placement2.w != 0.0);")
    lines.append("    float3 qc0 = float3(placement0.x, placement1.x, placement2.x);")
    lines.append("    float3 qc1 = float3(placement0.y, placement1.y, placement2.y);")
    lines.append("    float3 qc2 = float3(placement0.z, placement1.z, placement2.z);")
    # P∘Q 的平移列 = A·s + t：s 是 Q 的平移列，必须逐分量取，
    # 不能写成 placement_i.www（那是第 i 行平移分量的广播，会算成 (p_i0+p_i1+p_i2)·t_i）。
    lines.append("    float3 placement_t = float3(placement0.w, placement1.w, placement2.w);")
    lines.append("    float4 src0 = himi_src_palette[src + 0u];")
    lines.append("    float4 src1 = himi_src_palette[src + 1u];")
    lines.append("    float4 src2 = himi_src_palette[src + 2u];")
    lines.append("    float4 out0 = src0;")
    lines.append("    float4 out1 = src1;")
    lines.append("    float4 out2 = src2;")
    lines.append("    if (calibrated)")
    lines.append("    {")
    lines.append("        // P' = P ∘ Q：线性部分 A·B，平移 A·s + t（列向量约定，行主序存储）。")
    lines.append("        out0 = float4(dot(src0.xyz, qc0), dot(src0.xyz, qc1), dot(src0.xyz, qc2),")
    lines.append("                      dot(src0.xyz, placement_t) + src0.w);")
    lines.append("        out1 = float4(dot(src1.xyz, qc0), dot(src1.xyz, qc1), dot(src1.xyz, qc2),")
    lines.append("                      dot(src1.xyz, placement_t) + src1.w);")
    lines.append("        out2 = float4(dot(src2.xyz, qc0), dot(src2.xyz, qc1), dot(src2.xyz, qc2),")
    lines.append("                      dot(src2.xyz, placement_t) + src2.w);")
    lines.append("    }")
    lines.append("    // 【t39-X 族级物体矩阵】合并几何整块落在**宿主族**的一笔 draw 里 ⇒ 那一刻只有")
    lines.append("    // 宿主族的对象矩阵 M_base；而各族真值是 world = M_f·(P_f·v) ⇒ 必须再左乘")
    lines.append("    // X_f = M_base⁻¹·M_f：")
    lines.append("    //     S = X_f ∘ (P_f ∘ Q_f⁻¹)")
    lines.append("    // 【t74 X 的来源】主路径 = **运行期从本族 vs-cb3 实时算**（下面的选择块）；载荷")
    lines.append("    // [4..6] 是导出期烘死的同一份 X_f，只在 cb3 拿不到/校验不过时兜底。")
    lines.append("    // X 与 Q 是**两个独立开关**：X 的 12 个数全零 ⇒ 不左乘（逐字节等价旧行为）；")
    lines.append("    // 宿主族自己的 X 是单位阵（非零 ⇒ 会施加，但数值恒等）。")
    lines.append("    float4 attach_x0 = himi_attach_range[4];")
    lines.append("    float4 attach_x1 = himi_attach_range[5];")
    lines.append("    float4 attach_x2 = himi_attach_range[6];")
    lines.append("")
    lines.append("    // 【t74 运行期 X】只在载荷版本 ≥ 4（= 带 [7] 族索引）时启用，旧产物（v3）逐字节照旧。")
    lines.append("    // 三态，顺序即优先级，**任何分支都不 return** —— draw 必须无条件执行，分支只决定「写什么矩阵」：")
    lines.append("    //   (a) own_index == base_index ⇒ X = 单位阵（不读表、不依赖采集）；")
    lines.append("    //   (b) 采集有效 ⇒ **先**把本族三行写进表（表只由这条路径填起来）、**再**读宿主族三行：")
    lines.append("    //       非全零 ⇒ X = M_base⁻¹·M_own；全零（非常首帧）⇒ 保留载荷兜底 X；")
    lines.append("    //   (c) 采集无效 ⇒ 读表里 own + base 两段（上一帧写下的好值）：都非全零 ⇒ X = M_base⁻¹·M_own，")
    lines.append("    //       否则保留兜底 X。**采集无效时绝不写表** —— 那会把表里已有的好值覆盖成垃圾。")
    lines.append("    if (asuint(attach_range.z) >= 4u)")
    lines.append("    {")
    lines.append("        uint own_index = asuint(himi_attach_range[7].x);")
    lines.append("        uint base_index = asuint(himi_attach_range[7].y);")
    lines.append("        HimiAffine own_object = himi_cb3_affine();")
    lines.append("        bool object_valid = himi_object_matrix_valid();")
    lines.append("        // 表长由 ini 的 array=（3 × 族数）决定：越界的族索引一律不碰表，防 OOB 读写")
    lines.append("        uint object_elements = 0u;")
    lines.append("        himi_object_matrix.GetDimensions(object_elements);")
    lines.append("        uint last_row = object_elements >= 3u ? (object_elements - 3u) / 3u : 0u;")
    lines.append("        bool object_in_range = object_elements >= 3u")
    lines.append("            && own_index <= last_row && base_index <= last_row;")
    lines.append("        if (object_valid && object_in_range)")
    lines.append("        {")
    lines.append("            // (b) 写表：把本族**校验通过**的物体矩阵写进自己那一段（宿主族那笔随后读回来）。")
    lines.append("            // 每族只在自己那笔里写自己那段 ⇒ 同一 dispatch 内不存在读写竞争。")
    lines.append("            uint own_row = own_index * 3u;")
    lines.append("            himi_object_matrix[own_row + 0u] = own_object.r0;")
    lines.append("            himi_object_matrix[own_row + 1u] = own_object.r1;")
    lines.append("            himi_object_matrix[own_row + 2u] = own_object.r2;")
    lines.append("        }")
    lines.append("        if (own_index == base_index)")
    lines.append("        {")
    lines.append("            // (a) 宿主族：X 恒为单位阵（不能读回自己那段：那是上一帧它自己写的 M_base）")
    lines.append("            attach_x0 = float4(1.0, 0.0, 0.0, 0.0);")
    lines.append("            attach_x1 = float4(0.0, 1.0, 0.0, 0.0);")
    lines.append("            attach_x2 = float4(0.0, 0.0, 1.0, 0.0);")
    lines.append("        }")
    lines.append("        else if (object_valid && object_in_range)")
    lines.append("        {")
    lines.append("            // (b) 采集有效：M_own 就是本次 dispatch 的 cb3（刚写进表的那份）")
    lines.append("            uint base_row = base_index * 3u;")
    lines.append("            float4 base0 = himi_object_matrix[base_row + 0u];")
    lines.append("            float4 base1 = himi_object_matrix[base_row + 1u];")
    lines.append("            float4 base2 = himi_object_matrix[base_row + 2u];")
    lines.append("            // 宿主族那一段全零 = 本帧还没写过（首次进游戏 / 刚重载）⇒ 保留兜底 X")
    lines.append("            if (base0.x != 0.0 || base0.y != 0.0 || base0.z != 0.0 || base0.w != 0.0")
    lines.append("                || base1.x != 0.0 || base1.y != 0.0 || base1.z != 0.0 || base1.w != 0.0")
    lines.append("                || base2.x != 0.0 || base2.y != 0.0 || base2.z != 0.0 || base2.w != 0.0)")
    lines.append("            {")
    lines.append("                HimiAffine m_base;")
    lines.append("                m_base.r0 = base0;")
    lines.append("                m_base.r1 = base1;")
    lines.append("                m_base.r2 = base2;")
    lines.append("                HimiAffine x_rows = himi_affine_mul(himi_affine_inverse(m_base), own_object);")
    lines.append("                attach_x0 = x_rows.r0;")
    lines.append("                attach_x1 = x_rows.r1;")
    lines.append("                attach_x2 = x_rows.r2;")
    lines.append("            }")
    lines.append("        }")
    lines.append("        else if (object_in_range)")
    lines.append("        {")
    lines.append("            // (c) 采集无效：拿不到可信的 M_own ⇒ 只能读表里**上一帧**写下的 own + base 两段。")
    lines.append("            // 这里绝不写表：写了就把表里已有的好值覆盖成垃圾（下一帧也一起坏掉）。")
    lines.append("            uint own_row = own_index * 3u;")
    lines.append("            float4 own0 = himi_object_matrix[own_row + 0u];")
    lines.append("            float4 own1 = himi_object_matrix[own_row + 1u];")
    lines.append("            float4 own2 = himi_object_matrix[own_row + 2u];")
    lines.append("            uint base_row = base_index * 3u;")
    lines.append("            float4 base0 = himi_object_matrix[base_row + 0u];")
    lines.append("            float4 base1 = himi_object_matrix[base_row + 1u];")
    lines.append("            float4 base2 = himi_object_matrix[base_row + 2u];")
    lines.append("            // 两段都非全零才够算：任一段空 ⇒ 表还没被写过 ⇒ 保留兜底 X")
    lines.append("            bool own_ready = (own0.x != 0.0 || own0.y != 0.0 || own0.z != 0.0 || own0.w != 0.0")
    lines.append("                || own1.x != 0.0 || own1.y != 0.0 || own1.z != 0.0 || own1.w != 0.0")
    lines.append("                || own2.x != 0.0 || own2.y != 0.0 || own2.z != 0.0 || own2.w != 0.0);")
    lines.append("            bool base_ready = (base0.x != 0.0 || base0.y != 0.0 || base0.z != 0.0 || base0.w != 0.0")
    lines.append("                || base1.x != 0.0 || base1.y != 0.0 || base1.z != 0.0 || base1.w != 0.0")
    lines.append("                || base2.x != 0.0 || base2.y != 0.0 || base2.z != 0.0 || base2.w != 0.0);")
    lines.append("            if (own_ready && base_ready)")
    lines.append("            {")
    lines.append("                HimiAffine m_base;")
    lines.append("                m_base.r0 = base0;")
    lines.append("                m_base.r1 = base1;")
    lines.append("                m_base.r2 = base2;")
    lines.append("                HimiAffine m_own;")
    lines.append("                m_own.r0 = own0;")
    lines.append("                m_own.r1 = own1;")
    lines.append("                m_own.r2 = own2;")
    lines.append("                HimiAffine x_rows = himi_affine_mul(himi_affine_inverse(m_base), m_own);")
    lines.append("                attach_x0 = x_rows.r0;")
    lines.append("                attach_x1 = x_rows.r1;")
    lines.append("                attach_x2 = x_rows.r2;")
    lines.append("            }")
    lines.append("        }")
    lines.append("    }")
    lines.append("    bool x_calibrated = (attach_x0.x != 0.0 || attach_x0.y != 0.0 || attach_x0.z != 0.0")
    lines.append("        || attach_x0.w != 0.0 || attach_x1.x != 0.0 || attach_x1.y != 0.0")
    lines.append("        || attach_x1.z != 0.0 || attach_x1.w != 0.0 || attach_x2.x != 0.0")
    lines.append("        || attach_x2.y != 0.0 || attach_x2.z != 0.0 || attach_x2.w != 0.0);")
    lines.append("    if (x_calibrated)")
    lines.append("    {")
    lines.append("        // 左乘：把 out（= P∘Q⁻¹，3×4 行主序）先拆成三列 + 平移列，再逐行点积（列向量约定）。")
    lines.append("        float3 xa0 = float3(attach_x0.x, attach_x0.y, attach_x0.z);")
    lines.append("        float3 xa1 = float3(attach_x1.x, attach_x1.y, attach_x1.z);")
    lines.append("        float3 xa2 = float3(attach_x2.x, attach_x2.y, attach_x2.z);")
    lines.append("        float3 d0 = float3(out0.x, out1.x, out2.x);")
    lines.append("        float3 d1 = float3(out0.y, out1.y, out2.y);")
    lines.append("        float3 d2 = float3(out0.z, out1.z, out2.z);")
    lines.append("        float3 dt = float3(out0.w, out1.w, out2.w);")
    lines.append("        out0 = float4(dot(xa0, d0), dot(xa0, d1), dot(xa0, d2), dot(xa0, dt) + attach_x0.w);")
    lines.append("        out1 = float4(dot(xa1, d0), dot(xa1, d1), dot(xa1, d2), dot(xa1, dt) + attach_x1.w);")
    lines.append("        out2 = float4(dot(xa2, d0), dot(xa2, d1), dot(xa2, d2), dot(xa2, dt) + attach_x2.w);")
    lines.append("    }")
    lines.append(f"    {skeleton_name}[dst + 0u] = out0;")
    lines.append(f"    {skeleton_name}[dst + 1u] = out1;")
    lines.append(f"    {skeleton_name}[dst + 2u] = out2;")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def merged_attach_hlsl_problems(text: str) -> list:
    """**单文件合并 attach CS 的判据**（纯函数，返回违规清单；空 = 通过）。

    与 `tests/test_himi_merged_runtime.py::_attach_cs_problems`（t57 的**逐族**口径）
    分开：那条判据要求 `#define HIMI_ATTACH_BASE 0`，本口径**禁止**它 —— 合并成一个文件后
    基址只能从运行期量出来。

    两态证明直接复用它：把基址表删掉 / 把 IniParams 读回来 / 把 `* 3u` 去掉，
    每一条都必须报错（见 `tests/test_himi_merged_runtime.py::HIMIMergedAttachCsTests`）。
    """
    code = _strip_hlsl_comments(text)
    problems: list = []
    if "[numthreads(64, 1, 1)]" not in code:
        problems.append("缺 [numthreads(64, 1, 1)]（与 Dispatch = ceil(槽数/64) 配套）")
    if "void main(" not in code:
        problems.append("缺 void main(...) 入口")
    if "SV_DispatchThreadID" not in code:
        problems.append("缺 SV_DispatchThreadID（块搬运的线程映射）")
    if "GetDimensions(palette_elements);" not in code:
        problems.append("源槽数必须取资源自身维度（GetDimensions 单参）")
    if "GetDimensions(palette_elements, " in code:
        problems.append("不得出现两参 GetDimensions（那是结构化声明口径）")
    if "palette_elements / 3u" not in code:
        problems.append("单参 GetDimensions 之后必须 /3u 还原槽数（源）")
    if "skeleton_elements / 3u" not in code:
        problems.append("单参 GetDimensions 之后必须 /3u 还原槽数（骨架）")
    if "IniParams" in code:
        problems.append("CS 不得读 IniParams（fork 相关 ⇒ 读错位置只会读到未设置的 0）")
    if "StructuredBuffer" in code:
        problems.append("不得出现结构化声明（typed 16 B 与结构化 48 B 混用会落 D3D11 未定义区）")
    if "[CustomShader" in code:
        problems.append("CS 文件里不得声明 ini 段（段在 ini 侧）")
    if "Dispatch =" in code or "Dispatch(" in code:
        problems.append("Dispatch 由 ini 段给出，CS 里不得出现")
    if "HIMI_ATTACH_BASE" in code:
        problems.append(
            "合并成单文件后不得再出现逐族 #define HIMI_ATTACH_BASE（基址必须由源槽位数查表算出）"
        )
    for probe in MERGED_ATTACH_BASE_TABLE_LINES:
        # 判据跑在**去注释**后的代码上，所以探针也要先去注释再比（否则注释字样会假阳性）。
        if _strip_hlsl_comments(probe).strip() not in code:
            problems.append(f"基址表缺行：{probe.strip()}")
    if "base_slot = base_slot" in code or "base_slot + bone_index" not in code:
        problems.append("写侧必须按 (base_slot + bone_index) 取目标槽位")
    if "bone_index * 3u" not in code:
        problems.append("读侧必须按 bone * 3u 索引（typed 16 B 视图）")
    if "target_slot * 3u" not in code:
        problems.append("写侧必须按 target_slot * 3u 索引（typed 16 B 视图）")
    # ---- 【F5】本族身份资源 + 本族 span 上界（t4 新增；见报告 50 §1.2 入口 ①）----
    if "himi_attach_range : register(t1);" not in code:
        problems.append(
            "缺 cs-t1 身份资源声明（Buffer<float4> himi_attach_range : register(t1);）——"
            "没有它，本族 span 与查表键同源 ⇒ span 上界恒真，副本维度大于 span 时会写进下一族的块"
        )
    if f"asuint(attach_range.w) == {himi_runtime.ATTACH_RANGE_MAGIC_MACRO}" not in code:
        problems.append(
            f"身份资源必须校验魔数（asuint(attach_range.w) == {himi_runtime.ATTACH_RANGE_MAGIC_MACRO}）"
        )
    if "asuint(attach_range.y) > 0u" not in code:
        problems.append(
            "身份可用性必须包含 span > 0（span=0 是「身份不可用」哨兵，语义须与 "
            "common/himi_merged_runtime.py::attach_write_target 一致 —— t18/T5-F1）"
        )
    if "identity_base = asuint(attach_range.x);" not in code:
        problems.append("身份资源必须提供 base（identity_base = asuint(attach_range.x)）")
    if "span_slots = asuint(attach_range.y);" not in code:
        problems.append("身份资源必须提供 span（span_slots = asuint(attach_range.y)）")
    if "base_slot != identity_base" not in code:
        problems.append(
            "冻结表与身份资源**不一致时**必须拒写（base_slot != identity_base ⇒ return）"
        )
    if "bone_index >= span_slots" not in code:
        problems.append(
            "缺【F5】本族 span 上界（if (span_slots > 0u && bone_index >= span_slots) return;）——"
            "副本维度大于本族 span 时会把多出的骨写进下一族的块"
        )
    # ---- 【t39】族级校准（palette 侧 P' = P ∘ Q_f）----
    if "himi_attach_range[1]" not in code or "himi_attach_range[2]" not in code \
            or "himi_attach_range[3]" not in code:
        problems.append(
            "缺【t39】族级摆放的读取（placement0/1/2 = himi_attach_range[1..3]）——"
            "palette 侧不校准会让跨族借权重落在错的位置"
        )
    if "asuint(attach_range.z) >= 3u" not in code:
        problems.append(
            "缺【t39/t44】版本门（asuint(attach_range.z) >= 3u）—— v1/v2 载荷（16 B / 存 Q_f 的旧方向）"
            "一律不得复合：会让骨架被写成 0 或按**取反**的方向复合"
        )
    if "float4 out0 = src0;" not in code or "out0 = float4(dot(src0.xyz, qc0)" not in code:
        problems.append(
            "缺【t39】P' = P ∘ Q 的复合（线性部分 A·B + 平移 A·s + t）；"
            "直接写 himi_src_palette 就等于「不校准」"
        )
    if "[dst + 0u] = out0;" not in code:
        problems.append("【t39】写侧必须写复合后的 out0/1/2，而不是原始 palette 行")
    # ---- 【t41 / T40-F3】平移项：文本门 + 与 CS 文本同源的数值门 ----
    # 把表达式**解析成模型**（himi_runtime.attach_compose_model_from_hlsl），只用一种事实源：
    # 文本一被改坏（例如退回 `placement_i.www`、或被 `* 0.0` 抹掉平移），这里就报错，
    # 而同一份模型在测试里还会与 mat3x4_compose 做数值对齐。
    compose_model = himi_runtime.attach_compose_model_from_hlsl(text)
    for problem in compose_model.get("problems") or ():
        problems.append("【t41/T40-F3】" + str(problem))
    wrong_modes = {
        index: mode for index, mode in (compose_model.get("modes") or {}).items()
        if mode != himi_runtime.ATTACH_COMPOSE_CORRECT
    }
    if wrong_modes:
        problems.append(
            "【t41/T40-F3】P∘Q 的平移项写法不对（应 `dot(src_i.xyz, placement_t) + src_i.w`，"
            "其中 `placement_t = float3(placement0.w, placement1.w, placement2.w)`）："
            + "；".join(f"out{index}={mode}" for index, mode in sorted(wrong_modes.items()))
        )
    if "float3 placement_t = float3(placement0.w, placement1.w, placement2.w);" not in code:
        problems.append(
            "【t41/T40-F3】缺 `float3 placement_t = float3(placement0.w, placement1.w, placement2.w);`"
            "（平移列必须按行取 `placement_k.w` 再与 `src_i.xyz` 点积）"
        )
    # ---- 【t44】「取反盲区」文本门：palette 侧必须是 `P ∘ Q⁻¹`（因子由载荷给出），
    # 顶点侧必须是 `Q·v`（在 `_himi_calibrate_bind_positions` 里）。文本这一侧只能检查
    # 「载荷被当因子直接复合」（不在 CS 里再求逆），方向本身由数值门 + 盲区用例钉住。
    if "himi_attach_range[1]" not in code or "placement_t" not in code:
        problems.append("【t44】palette 侧必须把载荷当**因子**直接复合（`P ∘ factor`，factor = Q⁻¹）")
    for index in (0, 1, 2):
        if f"placement{index}.www" in code:
            problems.append(
                f"【t41/T40-F3】出现了旧写法 `placement{index}.www`"
                "（= `(p_i0+p_i1+p_i2)·t_i`，会把盘绕项算错，实测差 3.010439）"
            )
    # ---- 【t39-X】族级物体矩阵 X_f 的左乘（A4 的三条锚点：删任意一条都必须报错）----
    x_read_tokens = ("himi_attach_range[4]", "himi_attach_range[5]", "himi_attach_range[6]")
    if not all(token in code for token in x_read_tokens):
        problems.append(
            "缺【t39-X】族级物体矩阵的读取（attach_x0/1/2 = himi_attach_range[4..6]）——"
            "合并在宿主族的一笔 draw 里渲染，缺这一项时每个族都会按**宿主族的对象矩阵**摆放"
        )
    if "float3 d0 = float3(out0.x, out1.x, out2.x);" not in code:
        problems.append(
            "缺【t39-X】左乘前的列拆分（float3 d0/d1/d2 = out 的三列、dt = out 的平移列）"
        )
    if "out0 = float4(dot(xa0, d0), dot(xa0, d1), dot(xa0, d2), dot(xa0, dt) + attach_x0.w);" not in code:
        problems.append(
            "缺【t39-X】S = X∘(P∘Q⁻¹) 的左乘（线性部分 A_X·A_out + 平移 A_X·t_out + t_X）"
        )
    if "float3 xa0 = float3(attach_x0.x, attach_x0.y, attach_x0.z);" not in code:
        problems.append("【t39-X】缺 `float3 xa0/xa1/xa2 = attach_x{0,1,2}.xyz`（X 行的线性部分）")
    # ---- 【t74】运行期 X：本族 vs-cb3 → u1 物体矩阵表 → X 选择（每一条都能被删行变异证红）----
    for probe, message in HIMI_RUNTIME_X_PROBES:
        if _strip_hlsl_comments(probe).strip() not in code:
            problems.append(message)
    # 兜底 X 必须先于选择块读入：变异成「先烘死 X 再判断」会让每族都按抓帧常量摆放，
    # 而所有“采集有效/无效”分支的探针都还在 ⇒ 只能靠位置关系抓出来。
    baked_decl = _strip_hlsl_comments("float4 attach_x0 = himi_attach_range[4];").strip()
    version_gate = _strip_hlsl_comments("if (asuint(attach_range.z) >= 4u)").strip()
    if code.count(baked_decl) != 1:
        problems.append(
            "【t74】兜底 X 必须**恰好一次**读入（float4 attach_x0/1/2 = himi_attach_range[4..6]），"
            f"实际出现 {code.count(baked_decl)} 次"
        )
    else:
        gate_index = code.find(version_gate)
        # 版本门本身缺失时已由探针报出；这里只判位置关系。
        if gate_index >= 0 and code.index(baked_decl) > gate_index:
            problems.append(
                "【t74】兜底 X 必须在运行期选择块**之前**读入：否则 (b)/(c) 分支还没算完就已被抓帧常量定死"
            )
    # (b) 的写表必须三行齐全、且发生在选择分支**之前**：只写一两行、或写进 (a) 分支里，
    # 宿主族就读不到本族的 M_base（探针只查「第一行在不在」，行数与位置只能靠这里抓）。
    write_rows = (
        "himi_object_matrix[own_row + 0u] = own_object.r0;",
        "himi_object_matrix[own_row + 1u] = own_object.r1;",
        "himi_object_matrix[own_row + 2u] = own_object.r2;",
    )
    identity_branch = _strip_hlsl_comments("if (own_index == base_index)").strip()
    for row in write_rows:
        token = _strip_hlsl_comments(row).strip()
        count = code.count(token)
        if count != 1:
            problems.append(
                "【t74】(b) 写表必须恰好一次且三行齐全（" + token + "），实际出现 "
                + str(count) + " 次 —— 少写一行宿主族只读到半截物体矩阵"
            )
        elif identity_branch in code and code.index(token) > code.index(identity_branch):
            problems.append(
                "【t74】(b) 写表必须在 (a) own==base 分支**之前**：写进分支里时"
                "采集有效但 own==base 的那些帧永远填不了表"
            )
    # (c) 必须把 own 段三行读回来：少一行等于采集无效时拿不到本族矩阵，只能退回抓帧常量。
    # base 段的读回行在 (b)/(c) 各出现一次，所以这里只钉 own。
    for row in (
        "float4 own0 = himi_object_matrix[own_row + 0u];",
        "float4 own1 = himi_object_matrix[own_row + 1u];",
        "float4 own2 = himi_object_matrix[own_row + 2u];",
    ):
        token = _strip_hlsl_comments(row).strip()
        count = code.count(token)
        if count != 1:
            problems.append(
                "【t74】(c) 采集无效时必须读回表里本族的三行（" + token + "），实际出现 "
                + str(count) + " 次"
            )
    # 与 P∘Q 同款：把表达式**解析成模型**（himi_runtime.attach_x_model_from_hlsl），
    # 文本一被写坏（少一行、平移项漏掉）这里就报错，同一份模型在测试里与 mat3x4_compose 数值对齐。
    x_model = himi_runtime.attach_x_model_from_hlsl(text)
    for problem in x_model.get("problems") or ():
        problems.append("【t39-X】" + str(problem))
    return problems


def _strip_hlsl_comments(text: str) -> str:
    """去掉 HLSL 的 ``//`` 行注释与 ``/* */`` 块注释后再判结构。

    必须先 strip：抬头**故意**写着「不要用 StructuredBuffer」「绝不读 IniParams」这类
    **反例说明**，对全文做子串搜索会把注释里的字样当成真实代码。
    """
    without_block = re.sub(r"/\*.*?\*/", "", str(text or ""), flags=re.S)
    return re.sub(r"//[^\n]*", "", without_block)


# --------------------------------------------------------------------------- #
# 【F6】冻结基址表 vs 本次 plan.family_ranges 的导出期硬门
# --------------------------------------------------------------------------- #
#: 从基址表行里抽出 ``(源槽位数, 基址)``（表行形状由 `MERGED_ATTACH_BASE_TABLE_LINES` 冻结）。
_BASE_TABLE_ROW_RE = re.compile(r"source_slots\s*==\s*(\d+)u\)\s*\{\s*base_slot\s*=\s*(\d+)u;")


def merged_attach_base_table_rows() -> list:
    """把冻结基址表解析成 ``[(slots, base, family), …]``（**只读**，供 F6 硬门与测试用）。

    表是「槽位数 → 基址」的**字符串常量**（``ui/universal/himi.py`` 的模块级常量），而
    ini 的 ``array=`` / ``Dispatch`` 来自**动态** ``plan.family_ranges``。两者此前没有任何
    比对 ⇒ 换角色 / 重建工作空间使 span 变化时，表不再覆盖 ⇒ ``base_slot`` 停在
    ``0xFFFFFFFF`` ⇒ 5 族全不写 ⇒ 骨架全零 ⇒ 模型**静默消失**（报告 50 §1.2 入口 ②）。
    """
    rows: list = []
    for line in MERGED_ATTACH_BASE_TABLE_LINES:
        match = _BASE_TABLE_ROW_RE.search(str(line))
        if not match:
            continue
        comment = str(line).split("//", 1)[1].strip() if "//" in str(line) else ""
        rows.append((int(match.group(1)), int(match.group(2)), comment))
    return rows


def merged_attach_base_table_audit(ranges, slot_count: int = 0) -> dict:
    """【F6】冻结基址表 vs 本次 ``plan.family_ranges`` 的审计：``{"problems", "notices"}``。

    为什么必须是**两级**而不是「一律 Fatal」（t4 修正）：

    * 表是「槽位数 → 基址」的**历史冻结副本**（``MERGED_ATTACH_BASE_TABLE_LINES``），而
      ini 的 ``array=``/``Dispatch`` 来自**动态** ``plan.family_ranges``。两者此前没有任何
      比对 ⇒ 换角色 / 重建工作空间使 span 变化时，表不再覆盖 ⇒ ``base_slot`` 停在
      ``0xFFFFFFFF`` ⇒ 5 族全不写 ⇒ 骨架全零 ⇒ 模型**静默消失**（报告 50 §1.2 入口 ②）。
    * **t4/F5 起运行期的权威来源是身份资源**（``cs-t1`` = 每族 16 字节的 ``(base, span)``），
      所以「plan 的 span 不在表里」**不再等于会消失** —— 那种角色照样能正确写入。把它当
      Fatal 会让 HIMI **只对这一个角色可用**（换角色必然中止导出），方向反了。
    * 真正危险的是**表与 plan 直接矛盾**（同一个 span 在表里映射到别的 base）⇒ 运行期
      会**拒写**那一族（fail-closed，安全但不完整）⇒ 这种必须 **Fatal**，不能静默。

    ``problems``（⇒ 调用方 Fatal）：表行无法解析 / 表内重复 span / **同 span 不同 base**。
    ``notices``（⇒ 打印，不阻断）：plan 的 span 表里没有（身份资源兜住）/ 表里有 plan 里
    没有的旧行 / 表的 span 之和 ≠ 本次骨架槽位数。**notices 绝不静默**。
    """
    problems: list = []
    notices: list = []
    rows = merged_attach_base_table_rows()
    if len(rows) != len(MERGED_ATTACH_BASE_TABLE_LINES):
        problems.append(
            f"基址表有 {len(MERGED_ATTACH_BASE_TABLE_LINES) - len(rows)} 行无法解析"
            "（形状必须仍是 `if (source_slots == <n>u) { base_slot = <m>u; }`）"
        )
    spans: dict = {}
    for slots, base, family in rows:
        if slots in spans:
            problems.append(
                f"基址表出现重复的源槽位数 {slots}（{spans[slots][1]} 与 {family}）⇒ "
                "两族会撞同一个基址，后写的覆盖先写的"
            )
        spans[slots] = (base, family)
    wanted: dict = {}
    for family, info in dict(ranges or {}).items():
        info = dict(info or {})
        if not bool(info.get("own_block", False)):
            continue
        count = int(info.get("slots", 0) or 0)
        if count <= 0:
            continue
        wanted[str(family)] = (count, int(info.get("base", 0) or 0))
    for family, (count, base) in sorted(wanted.items()):
        entry = spans.get(count)
        if entry is None:
            notices.append(
                f"基址表没有 {family} 的映射（本族槽位数 {count}、基址 {base}）："
                "运行期不再依赖冻结表（cs-t1 身份资源是从本次 plan 生成的），"
                "但表因此只作交叉核对 —— 若你想让表继续覆盖这一族，请把它加进 "
                "MERGED_ATTACH_BASE_TABLE_LINES"
            )
            continue
        table_base, table_family = entry
        if table_base != base:
            problems.append(
                f"基址表与本次排布**直接矛盾**：{family} 槽位数 {count} 在表里映射到基址 "
                f"{table_base}（表内标注 {table_family or '?'}），而 plan 给的是 {base} ⇒ "
                "运行期该族会拒写（宁可一根骨都不写，也不写进别的族的槽块）"
            )
    for slots, (base, family) in sorted(spans.items()):
        if slots in {count for count, _ in wanted.values()}:
            continue
        notices.append(
            f"基址表多出一行（槽位数 {slots} → 基址 {base}，标注 {family or '?'}）："
            "本次导出的家族区间里没有这个 span（表是历史冻结副本，只作交叉核对）"
        )
    total = int(slot_count or 0)
    if total > 0 and rows:
        table_total = sum(slots for slots, _base, _family in rows)
        if table_total != total:
            notices.append(
                f"基址表 span 之和 {table_total} ≠ 本次骨架槽位数 {total}"
                "（换角色/重建工作空间后表只作交叉核对）"
            )
    return {"problems": problems, "notices": notices}


def merged_attach_base_table_problems(ranges, slot_count: int = 0) -> list:
    """【F6 硬门】只返回**必须 Fatal** 的矛盾项（= :func:`merged_attach_base_table_audit` 的
    ``problems``）；诊断项在 ``notices`` 里单独取（见该函数的「为什么必须两级」）。"""
    return list(merged_attach_base_table_audit(ranges, slot_count).get("problems") or [])


def merged_deform_key_problems(rebinds) -> list:
    """【K4 / t13 收编】合并 deform 的**触发键**必须一族一个（计划内部不得 1→多）。

    背景（t13 逐帧实测 + 队长的裁定）：合并 deform 的段是
    ``[TextureOverride_VB_<宿主族>_<alias>_Blend]``，键 = ``hash = <CategoryHash.Blend>``；
    ``draw = <行数>, 0`` 用的是**当前绑定的管线状态（含 SO 目标）**。若同一个 Blend 键落在
    **两个族**上，重放理论上可能在别的族的变形段上触发 ⇒ 合并几何被写进那个族的 SO 目标、
    而宿主族的 SO 当帧没有写方。

    **t13 实测：4 帧里绑定 ``4349c385`` 的 draw 各只 1 笔且是同一笔 ⇒ 实际无歧义**
    （1→多只是理论风险）⇒ 本判据**只当诊断**（``self._himi_notify(warning=True)``），
    不阻断导出（"不要为杞人忧天写守卫"）。判据本身仍要留：它是 K4 的**导出期**那一半。

    ⚠ 边界：只能证明**计划内部**没有两族共用同一个 Blend 键；「游戏里是否只有本族的变形段
    绑定它」属 dump 侧（K1/K3），见 ``reports/himi-merge/51-fix-implementation.md`` §8 U7。
    """
    by_hash: dict = {}
    by_family: dict = {}
    for item in rebinds or ():
        record = dict(item or {})
        family = str(record.get("family") or record.get("draw_ib") or "").strip()
        value = str(record.get("category_hash") or "").strip()
        if not family or not value:
            continue
        by_family.setdefault(family, value)
        by_hash.setdefault(value.lower(), set()).add(family)
    problems: list = []
    for value, families in sorted(by_hash.items()):
        if len(families) < 2:
            continue
        problems.append(
            f"合并 deform 的触发键 `hash = {value}` 被 {len(families)} 个族共用"
            f"（{', '.join(sorted(families))}）⇒ 理论上重放可能在**别的族**的变形段上触发"
            "（t13 实测 4 帧内无实际歧义，故此处只作诊断）"
        )
    return problems


#: 【K2】变形 pass 的抽取方式：跨族合并要求它是**非索引 pointlist Draw**（t11 §2 实测
#: ``hash = ea05602f`` 只在 deform pass 触发、pointlist count=3789）；渲染类别（Texcoord 等）
#: 本来就是 trianglelist，**不参与**这条判据。
DEFORM_POINTLIST_CATEGORIES = ("Position", "Blend")
#: 变形 pass 的结构判据要求的抽取方式（工作空间 json 的 ``ExtractTechnique``）。
DEFORM_POINTLIST_TECHNIQUE = "pointlist"


def deform_draw_structure_problems(payload, *, unique_str: str = "") -> list:
    """【K2 导出期那一半】宿主/成员部件的 Position/Blend 必须是 ``pointlist`` 抽取。

    为什么能在这条链上判：跨族合并把「一族一个 deform pass」重放成 ``draw = <行数>, 0``，
    其语义前提是该 pass 是**非索引 pointlist Draw**（带 SO 目标）。工作空间 json 的
    ``CategoryBufferList[*].D3D11ElementList[*].ExtractTechnique`` 就是导入侧记录的抽取方式
    （实测本角色：Position 4 个元素 + Blend 2 个元素全 = ``pointlist``，Texcoord = ``trianglelist``）
    ⇒ 若哪一族不是 pointlist，重放的语义就不成立，必须**在导出期**说出来而不是进游戏才发现。

    ``payload`` = 该组件 json 的 dict。返回问题清单（空 = 通过）。
    """
    problems: list = []
    payload = dict(payload or {})
    if not payload:
        return problems  # 无 json ⇒ unavailable，不阻断
    label = f"{unique_str}: " if unique_str else ""
    seen_categories: set = set()
    for buffer in payload.get("CategoryBufferList") or ():
        if not isinstance(buffer, dict):
            continue
        for element in buffer.get("D3D11ElementList") or ():
            if not isinstance(element, dict):
                continue
            category = str(element.get("Category", "") or "")
            if category not in DEFORM_POINTLIST_CATEGORIES:
                continue
            seen_categories.add(category)
            technique = str(element.get("ExtractTechnique", "") or "")
            if technique != DEFORM_POINTLIST_TECHNIQUE:
                problems.append(
                    f"{label}{category} 的抽取方式是 `{technique or '(空)'}`，而变形 pass 必须是"
                    f" `{DEFORM_POINTLIST_TECHNIQUE}`（非索引 Draw + SO 目标）⇒ "
                    "`draw = <行数>, 0` 的重放语义不成立"
                )
    missing = [cat for cat in DEFORM_POINTLIST_CATEGORIES if cat not in seen_categories]
    if seen_categories and missing:
        problems.append(
            f"{label}json 里缺 {'/'.join(missing)} 类别的元素记录 ⇒ 无法核对变形 pass 结构"
        )
    return problems


def deform_draw_structure_judged(payload) -> bool:
    """【K2 / t18】这份 json 是否**真的**包含可判的 Position/Blend 元素记录。

    为什么需要它（t5 的连带问题）：`deform_draw_structure_problems()` 返回空既可能表示
    「判过且合格」，也可能表示「根本没有可判的东西」—— 两者不能都算「通过」。
    本函数让调用方能分开计数（`checked` vs `unjudgeable`），
    否则「没有报错」仍然可以被当成「检查过了」。
    """
    for buffer in dict(payload or {}).get("CategoryBufferList") or ():
        if not isinstance(buffer, dict):
            continue
        for element in buffer.get("D3D11ElementList") or ():
            if not isinstance(element, dict):
                continue
            if str(element.get("Category", "") or "") in DEFORM_POINTLIST_CATEGORIES:
                return True
    return False


def product_ini_section_problems(ini_text: str) -> list:
    """【t39 / §13.17 教训】产物 ini 的**逐段解析自检**：任何段"属性为 0 条"即失败。

    为什么要有它：2026-09-26 出过一次事故 —— 往 ini 插新段时**把段头插在了别的段的属性行之间**，
    而 INI 的段**持续到下一个 ``[``** ⇒ 被截断的那一段（骨架资源）属性被清空、整段失效。
    这类事故在"看产物"时不一定看得见（文件还在、行还在），但资源已经废了。

    判据（只读、纯函数）：
    * 每个 ``[…]`` 段头之后，到下一个个段头之前，**至少有一条 ``key = value`` 属性行**
      （``[Constants]`` 例外：它可以只有 ``global $x = 0`` 声明行，同样算属性）；
    * **例外：纯注释段放行** —— 段内一行实质内容都没有、但**有 ``;`` 注释行**时不算失败。
      理由是这类段完全无害（3DMigoto 对 ``;`` 开头的行整行跳过），而它确实有存在目的：
      ``M_IniHelper._report_standard_route_baked_shapekeys`` 就是把「标准路线烘焙掉了形态键」
      的诊断注释挂在 ``[Present]`` 段上，好让告警在产物里可见（2026-09-27 实测：
      这条 R-A 诊断会稳定触发，若一并判死就等于「有告警就导不出」）。
      区分方式：**真被截断的段是「既无属性、也无注释」**（新段头直接顶掉旧段），
      而纯注释段至少有注释行 —— 前者仍然报错。
    * 不允许出现"属性行紧跟在段头之前、却没有归属段"的悬空属性（即文件开头就是 ``key = value``）；
    * 空段（``[]``）直接报错。
    """
    problems: list = []
    lines = str(ini_text or "").splitlines()
    current = ""
    attrs = 0
    comments = 0
    start_line = 0
    for index, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            if current:
                comments += 1
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if current:
                if attrs == 0 and comments == 0:
                    problems.append(
                        f"第 {start_line} 行的段 [{current}] 属性为 0 条（段头插错位置/被截断？）"
                    )
            elif attrs:
                problems.append(
                    f"第 {index} 行之前有 {attrs} 条属性没有归属段（段头缺失或被顶掉）"
                )
            if not name:
                problems.append(f"第 {index} 行是空段头 `[]`")
            current = name
            start_line = index
            attrs = 0
            comments = 0
            continue
        if "=" in line:
            attrs += 1
    if current and attrs == 0 and comments == 0:
        problems.append(f"第 {start_line} 行的段 [{current}] 属性为 0 条（段头插错位置/被截断？）")
    return problems


def verify_attach_range_references(ini_text: str, mod_root: str) -> list:
    """【F5 硬门】attach 段的 ``cs-t1 = ref ResourceHI3AttachRange_<fam>`` 必须有**真实文件**。

    与 :func:`verify_cs_references` 同款：返回问题清单（空 = 通过），调用方 ``Fatal``。
    身份资源绑空 ⇒ CS 退回冻结表（不是静默乱写），但那样 F5 的保护就没了 ⇒ 必须硬拦。
    """
    text = str(ini_text or "")
    root = os.path.abspath(str(mod_root or "").strip() or ".")
    declared: dict = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current.startswith(himi_runtime.ATTACH_RANGE_RESOURCE_PREFIX) and line.lower().startswith(
            "filename"
        ):
            declared[current] = line.split("=", 1)[1].strip() if "=" in line else ""
    problems: list = []
    referenced = [
        match.group(1)
        for match in re.finditer(
            r"^\s*cs-t1\s*=\s*ref\s+(\S+)", text, flags=re.IGNORECASE | re.MULTILINE
        )
    ]
    if not referenced:
        problems.append(
            "整份 ini 里没有任何 `cs-t1 = ref ResourceHI3AttachRange_*`（F5 的本族身份没有绑定）"
        )
    for name in referenced:
        if name not in declared:
            problems.append(f"`cs-t1 = ref {name}` 没有对应的 [{name}] 资源声明")
            continue
        value = declared[name]
        if not value:
            problems.append(f"[{name}] 的 filename 为空（身份资源必须指向真实文件）")
            continue
        normalised = value.replace("\\", "/")
        relative = normalised[2:] if normalised.startswith("./") else normalised
        candidate = os.path.abspath(os.path.join(root, os.path.normpath(relative)))
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside = False
        if not inside:
            problems.append(f"[{name}] 的 filename 越出 mod 目录：{value}")
            continue
        if not os.path.isfile(candidate):
            # 与 `verify_cs_references` 的「引用的文件不存在」保持同一措辞（用户看到的
            # 应当是同一句话的两个入口；t20/T6-F2 的回归用例也按这句断言）。
            problems.append(
                f"[{name}] 的 filename 引用的文件不存在（{candidate}）"
            )
            continue
        # 【t39】长度硬门：v2 的身份载荷必须带 Q_f（64 B；t74 起为 128 B）。16 B 的旧载荷在 CS 里
        # 会被版本门挡掉（不做校准）——那意味着**顶点侧校准了、palette 侧没校准**，
        # 两者必须同源同报 ⇒ 宁可 Fatal，也不产出半校准的产物。
        try:
            size = os.path.getsize(candidate)
        except OSError as exc:
            problems.append(f"[{name}] 的 filename 无法取大小（{exc}）")
            continue
        if size != himi_runtime.ATTACH_RANGE_BYTES:
            problems.append(
                f"[{name}] 的身份载荷长度是 {size} B，而 v{himi_runtime.ATTACH_RANGE_VERSION}"
                f" 要求 {himi_runtime.ATTACH_RANGE_BYTES} B"
                "（头部 + Q_f⁻¹ 3×4 + X_f 3×4 + 族索引 [7]）⇒ "
                "CS 读不到 X_f / 族索引（短载荷会让 himi_attach_range[4..7] 越界），必须重新导出"
            )
    return problems


# --------------------------------------------------------------------------- #
# 【F1/F2】变形重放必须**无条件**执行（s3 那种守卫永不得回流）
# --------------------------------------------------------------------------- #
#: 条件行前缀（`if` / `else if` / `else` / `endif`）—— 出现在变形重放块里就是坏产物。
_CONDITIONAL_LINE_RE = re.compile(r"^\s*(if|else|endif)\b", re.IGNORECASE)


def deform_replay_block_problems(lines) -> list:
    """Blend 块里的**变形重放**必须无条件执行（纯函数；空 = 通过）。

    根因（报告 50 §4.1 + 队长两帧实测）：该族 deform 段的原 draw 已被 ``handling = skip``
    吞掉 ⇒ SO 目标当帧**唯一的写方**就是 mod 重放的这一笔。守卫一旦包住它（s3 诊断态的
    ``if $seen_… >= 1``），守卫在宿主族段里**恒假** ⇒ SO 全零 ⇒ **整块几何消失**。

    ⇒ 判据：块内不得出现任何条件行；``handling = skip`` 之后必须紧跟一笔
    ``draw = <行数>, 0``；重放行只能有**一笔**（多笔 = 越界写 SO 池）。

    为什么是**导出期**判据而不是运行期守卫：守卫只能放在「骨架内容更新」这一层
    （报告 50 §4.2 原则 2），`draw` 本身永远无条件。
    """
    items = [str(line) for line in (lines or ())]
    if not items:
        return []  # 空块不是「变形重放块」（判据只对真块生效，不制造假阳性）
    problems: list = []
    for index, line in enumerate(items, 1):
        if _CONDITIONAL_LINE_RE.match(line):
            problems.append(
                f"第 {index} 行是条件行（`{line.strip()}`）：变形重放块里不得有任何守卫 ——"
                "守卫一旦包住 `draw`，宿主族段里恒假 ⇒ SO 目标无写方 ⇒ 模型整块消失"
                "（s3 实测：消失帧该缓冲 31054 顶点全零）"
            )
    skip_index = None
    draw_rows: list = []
    draw_index = None
    for index, line in enumerate(items, 1):
        stripped = line.strip()
        if stripped.startswith("handling = skip"):
            skip_index = index
        if stripped.startswith("draw = "):
            draw_rows.append(index)
            draw_index = index
    if skip_index is None:
        problems.append("缺 `handling = skip`（不吞掉原 draw ⇒ 原版几何与合并几何重影）")
    if not draw_rows:
        problems.append("缺变形重放行 `draw = <行数>, 0`（SO 目标当帧没有写方 ⇒ 模型消失）")
    elif len(draw_rows) > 1:
        problems.append(
            f"变形重放行有 {len(draw_rows)} 笔（第 {draw_rows} 行）：只允许一笔"
            "（多笔会把角色级 SO 池写爆）"
        )
    if skip_index is not None and draw_index is not None and draw_index < skip_index:
        problems.append(
            f"`draw`（第 {draw_index} 行）排在 `handling = skip`（第 {skip_index} 行）之前 ⇒"
            "原 draw 会与重放各写一次 SO（顺序必须是 skip → draw）"
        )
    return problems


def verify_cs_references(ini_text: str, mod_root: str) -> list:
    """**硬门**：ini 里每一条 ``cs =`` 引用都必须能在 mod 目录解析到真实文件。

    返回**问题清单**（空 = 通过）。调用方（:meth:`ExportHIMI.generate_unity_vs_config_ini`）
    在非空时 ``raise Fatal``（**不降级为 warning、不跳过**）。

    规则：

    * 逐条匹配 ``^\\s*cs\\s*=\\s*(\\S+)``（3DMigoto 的键名大小写不敏感）；
    * 相对路径从 **ini 自身所在目录（= mod 根）** 算起：``./res/x.hlsl`` → ``<mod_root>/res/x.hlsl``；
    * **拒绝 `../` 越界**（解析后必须仍在 ``mod_root`` 之内）；
    * 有值但文件不存在 ⇒ 报「引用不存在」；
    * 值为空 ⇒ 报「空引用」；
    * 文件存在但不是普通文件（目录/符号链接断链）⇒ 报「不是普通文件」。
    """
    problems: list = []
    root = os.path.abspath(str(mod_root or "").strip() or ".")
    text = str(ini_text or "")
    seen: list = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        match = _CS_REFERENCE_RE.match(raw)
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            problems.append(f"第 {lineno} 行：`cs =` 是空引用（必须指向一个真实文件）")
            continue
        seen.append(value)
        if value.startswith("\\\\") or value.startswith("//"):
            # 网络路径 / URL：3DMigoto 的 ini 只接受本地相对或绝对路径，这里一律拒绝，
            # 否则「解析到真实文件」这条判据会被绕开。
            problems.append(f"第 {lineno} 行：`cs = {value}` 不是本地相对路径")
            continue
        normalised = value.replace("\\", "/")
        if os.path.isabs(normalised) or re.match(r"^[A-Za-z]:", normalised):
            candidate = os.path.abspath(normalised)
        else:
            # 相对路径从 **ini 自身所在目录（= mod 根）** 算起；`normpath` 先把 `../` 收掉，
            # 再用 abspath（**不要**先 abspath 再 join —— 那会把 `..` 当成普通文件名）。
            relative = normalised[2:] if normalised.startswith("./") else normalised
            candidate = os.path.abspath(os.path.join(root, os.path.normpath(relative)))
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside = False
        if not inside:
            problems.append(
                f"第 {lineno} 行：`cs = {value}` 越出 mod 目录（`../` 越界）⇒ 拒绝"
            )
            continue
        if not os.path.exists(candidate):
            problems.append(f"第 {lineno} 行：`cs = {value}` 引用的文件不存在（{candidate}）")
            continue
        if not os.path.isfile(candidate):
            problems.append(f"第 {lineno} 行：`cs = {value}` 不是普通文件（{candidate}）")
    if not seen:
        problems.append("整份 ini 里没有任何 `cs =` 引用（合并骨架的每帧填充没有写方）")
    return problems


def merged_attach_reference_problems(cs_paths) -> list:
    """**单文件口径**：全部 ``cs =`` 必须指向**同一个**路径。

    防止实现回退成「每族一份」——那时 5 条 `cs =` 会是 5 个不同路径（或者只剩一条），
    两种都必须报错。``cs_paths`` 是 `cs =` 右边的值序列。
    """
    values = [str(item or "").strip() for item in (cs_paths or ())]
    values = [item for item in values if item]
    if not values:
        return []
    uniq = sorted(set(values))
    if len(uniq) > 1:
        return [
            "`cs =` 引用不是同一个文件（合并成一个 shader 后 5 个族段必须共用一份）："
            + "、".join(uniq)
        ]
    return []



def blend_block_order_problems(lines, *, expect_capture=None) -> list:
    """**源码级顺序护栏**：Blend 块内「捕获 → run → 重绑」的相对顺序。

    每帧机制的整条链只在这三行的顺序上成立（``ResourceHI3PaletteFam_<fam>`` 逐族一个）：

    .. code-block:: ini

       ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null   ; ① 捕获当帧 palette
       run = CustomShaderHI3MergedSkeletonAttach_<fam>        ; ② 块搬运进合并骨架
       vs-t0 = ResourceHI3MergedSkeleton_<role_key>           ; ③ 之后才重绑

    * 捕获排在 ③ 之后 ⇒ 捕到的是**自己刚绑上去的骨架**（自反馈：骨架被写成上一帧的自己，
      而游戏当帧 palette 永远进不了骨架 ⇒ 「骨骼动画正常但各部件整体错位」）；
    * ``run`` 排在 ③ 之后 ⇒ CS 读的是重绑后的骨架而不是当帧 palette（同样错）。
    * 方向同样致命：3DMigoto 的 ``copy`` 是**赋值式**，LHS 是**目标** —— 写成
      ``copy vs-t0 = 副本`` 会把空副本写进游戏当帧 palette（比不改更坏）。

    ``expect_capture``：``True`` = 本块**必须**有捕获行（该族有专属块，正常路径）；
    ``False`` = 允许没有（借位族 ``own_block=False``：不发捕获行/attach 段，只重绑）；
    ``None``（默认）= 由产物自行判定 —— 捕获行与 ``run`` 必须**同时出现或同时缺席**
    （只出现一个必然是写漏，必须抓到）。

    纯文本判定（不依赖 ini 解析器）⇒ 可被单测与两态复现直接调用。返回问题描述列表，空 = 通过。
    """
    capture = run = rebind = None
    reversed_direction = None
    for index, raw in enumerate(lines or ()):
        line = str(raw).strip()
        if line.startswith("copy vs-t0"):
            reversed_direction = (index, line)
        if re.match(r"^ResourceHI3PaletteFam_\S+\s*=\s*copy vs-t0\b", line):
            capture = index
        elif re.match(r"^run\s*=\s*" + re.escape(himi_runtime.ATTACH_SECTION_PREFIX), line):
            run = index
        elif line.startswith("vs-t0"):
            rebind = index
    problems: list = []
    if reversed_direction is not None:
        problems.append(
            f"第 {reversed_direction[0]} 行捕获行方向写反（`{reversed_direction[1]}`）——"
            "3DMigoto 的 copy 是赋值式，LHS 必须是**目标**："
            f"应为 `{himi_runtime.family_palette_name('<fam>')} = copy vs-t0 unless_null`"
        )
    if rebind is None:
        return problems
    if expect_capture is None:
        expect_capture = capture is not None or run is not None
    if capture is None:
        if expect_capture:
            problems.append(
                f"Blend 块里没有捕获行（`{himi_runtime.family_palette_name('<fam>')} = "
                "copy vs-t0 unless_null`）⇒ 骨架不会被写入当帧姿态"
            )
        return problems
    if run is None:
        problems.append("捕获行有、但 `run =` attach 段缺失 ⇒ 骨架不会被写入")
    elif run < capture:
        problems.append(
            f"`run`（第 {run} 行）排在捕获行（第 {capture} 行）**之前** ⇒ "
            "CS 读到的是上一次捕获的内容（顺序必须是 捕获 → run）"
        )
    if capture > rebind:
        problems.append(
            f"捕获行（第 {capture} 行）排在 `vs-t0 =` 重绑（第 {rebind} 行）**之后** ⇒ "
            "自反馈：捕到的是刚绑上的骨架"
        )
    if run is not None and run > rebind:
        problems.append(
            f"`run`（第 {run} 行）排在 `vs-t0 =` 重绑（第 {rebind} 行）**之后** ⇒ "
            "CS 读的是重绑后的骨架而不是当帧 palette"
        )
    return problems


def _finite_vec3(a, b, c):
    """三个值 ⇒ float 三元组；任一非有限（源缓冲的 NaN 尾巴）⇒ ``None``（属性不可判）。

    【t24】顶点族判别的查询属性入口：法线（列 3..5）/切线（列 7..9）随行传给
    ``himi_runtime.nearest_family``。属性在索引/查询任一侧缺失或不可判时，
    判别自动停用（nearest_family 落回位置口径），绝不因为属性坏而改变族归属。
    """
    try:
        values = (float(a), float(b), float(c))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


class ExportHIMI(ExportUnity):
    """崩坏3 / HI3 导出器（VA 模式 + 静态合并骨架 + 站位抑制原版 draw）。"""

    def __init__(self, blueprint_model):
        # 站位对象必须在基类组装 drawib/submesh 模型**之前**注入，否则下游
        # SubMeshModel 找不到对应 Blender 对象（计划书 §7.7.5 生命周期）。
        #
        # **顺序对齐 ZZMI**（`ui/universal/zzmi.py:443` 注入 → `:445` super()）：
        # `ui/universal/drawib_export_base.py:11-16` 在它自己的 `__init__` 里**立即**
        # 用 `ExportHelper.parse_drawib_model_list_from_blueprint_model` 把
        # `ordered_draw_obj_data_model_list` 解析成 `drawib_model_list`。若注入发生在
        # super() 之后，站位 DrawCall 只会留在 `ordered` 里而**永远进不了**
        # `drawib_model_list[*].submesh_model_list[*].drawcall_model_list`
        # ⇒ 站位部件的 IB override 段（`handling = skip`）根本不会生成 ⇒ 抑制重影失效。
        self.blueprint_model = blueprint_model
        self._himi_stub_object_names: list = []
        self._himi_stub_draw_calls: list = []
        self._himi_stub_bares: set = set()
        self._himi_stub_decision = None
        self._himi_plan = None
        self._himi_plan_ready = False
        self._himi_contract_decision = None
        self._himi_resource_emitted = False
        self._himi_publish_result = {}
        self._himi_blend_verdicts = {}
        #: t75：本次导出发射的全部 `cs =` 值（单文件口径下应全部等于
        #: :data:`MERGED_ATTACH_HLSL_PATH`）。供测试/上层读取。
        self._himi_attach_cs_paths = []
        #: t75：`res/himi_attach.hlsl` 的落盘结果（``{"ok","path","written","bytes","message"}``）。
        self._himi_attach_cs_result = {}
        #: 【F5】每族身份资源（``Meshes/<fam>-AttachRange.buf``）的落盘结果。
        self._himi_attach_range_result = {}
        #: 【t18 / T5-F5】导出期诊断（F6 基址表 notice / K4 触发键 / K2 账目）的收集器：
        #: 它们会**同时**进控制台+LOG 与**产物 ini 末尾的注释块**——否则用户不看控制台就会错过。
        self._himi_base_table_notices: list = []
        self._himi_key_notices: list = []
        self._himi_deform_structure_result: dict = {}
        self._himi_diagnostics_emitted = False
        #: 【t85】普通导出时清掉的上一次合并导出残留文件账目（``{"removed","failed"}``）。
        self._himi_merged_residue: dict = {}
        #: 【t39】族级校准（Q_f）的缓存、问题清单与顶点侧结果。
        self._himi_placements_cache = None
        self._himi_placements_problems: list = []
        self._himi_placements_skipped: list = []
        self._himi_calibration_result: dict = {}
        #: 【t49】方案 A/C：旁观文件落盘结果 + 越界引用告警
        self._himi_blend_origin_result: dict = {}
        self._himi_blend_origin_notices: list = []
        try:
            self._himi_inject_stub_objects()
        except Exception:
            # 构造失败时 export() 的 finally 尚未接管；站位对象/网格必须立刻回滚，
            # 否则下一次导出会引用已删除对象（与 ZZMI 同款事务闭合）。
            self._himi_cleanup_stub_objects()
            raise
        # 模型组装（含 `ordered` → `drawib_model_list` 的解析）必须在站位注入之后。
        super().__init__(blueprint_model=blueprint_model)

    # ------------------------------------------------------------------ #
    # 站位（stub）：抑制被吸收部件的原版 draw（计划书 §7.7）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _himi_draw_call_object_name(draw_call) -> str:
        try:
            return str(draw_call.get_blender_obj_name() or "")
        except Exception:  # noqa: BLE001
            return str(getattr(draw_call, "obj_name", "") or "")

    @classmethod
    def _himi_drawcall_object_present(cls, draw_call) -> bool:
        """该 DrawCall 在**场景里**是否真有对象（场景真相，不是蓝图声明）。

        ZZMI t80 §2.3 实测：把「声明了」当 present 会让占位整段跳过，紧接着下游
        ``SubMeshModel`` 按不存在的对象直接 ``Fatal``。因此判据必须是
        ``bpy.data.objects`` 里真能取到那个对象。
        """
        return blender_object_present(cls._himi_draw_call_object_name(draw_call))

    def _himi_ordered_draw_calls(self) -> list:
        return getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None) or []

    @staticmethod
    def _himi_merged_data_present(workspace_root: str) -> bool:
        """工作空间里是否真有合并骨架数据（= 本次导出会产出合并骨架资源的必要前置）。

        判据：`LOD*/<bare>/<WorkGameType>/<bare>.json` 里**至少有一份**带
        `VGMap` 或 `VGCount`。只看「目录名同名的那份 json」（组件 json 的命名契约），
        不盲扫全树，也不解析无关 json。

        不用 `HIMISkeletonMergeHelper.missing_merged_metadata_exist` 的整批语义：
        它需要调用方给出 unique_str 列表，而这里（`__init__` 期）拿不到导出集合。
        """
        root = str(workspace_root or "").strip()
        if not root or not os.path.isdir(root):
            return False
        try:
            lod_names = sorted(os.listdir(root))
        except OSError:
            return False
        for lod_name in lod_names:
            if not (lod_name.startswith("LOD") and lod_name[3:].isdigit()):
                continue
            lod_dir = os.path.join(root, lod_name)
            try:
                bare_names = sorted(os.listdir(lod_dir))
            except OSError:
                continue
            for bare in bare_names:
                bare_dir = os.path.join(lod_dir, bare)
                if not os.path.isdir(bare_dir):
                    continue
                try:
                    game_type_names = sorted(os.listdir(bare_dir))
                except OSError:
                    continue
                for game_type in game_type_names:
                    path = os.path.join(bare_dir, game_type, bare + ".json")
                    if not os.path.isfile(path):
                        continue
                    try:
                        payload = JsonUtils.LoadFromFile(path)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(payload, dict) and (
                        payload.get("VGMap") or int(payload.get("VGCount", 0) or 0) > 0
                    ):
                        return True
        return False

    def _himi_collect_used_group_ids(self) -> set:
        """场景对象实际引用（weight > 0）的数字顶点组 id 集合。

        **必须排除站位对象自己**：否则本轮刚建的站位会反过来成为下一轮的吸收证据
        （自证循环，计划书 §7.7.5「标记的第一个消费点」/ ST9）。
        """
        names = []
        for draw_call in self._himi_ordered_draw_calls():
            if bool(getattr(draw_call, "himi_stub", False)):
                continue
            name = self._himi_draw_call_object_name(draw_call)
            if name:
                names.append(name)
        return blender_collect_used_group_ids(names)

    def _himi_remove_stub_object_data(self, obj) -> None:
        blender_remove_stub_object_data(obj)

    def _himi_purge_stale_stub_state(self) -> set:
        """清理上次异常残留的站位对象/网格/带标记 DrawCall。

        **必须早于 present 集合构造**（计划书 §7.7.5）：残留站位对象名恰好等于某真
        部件身份时，该部件会被误判「场景存在」⇒ 不插桩 ⇒ 随后对象被删 ⇒ 下游
        ``SubMeshModel`` 引用不存在的对象而 ``Fatal``。
        """
        stale_names = blender_purge_stale_stub_objects()
        ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if not bool(getattr(draw_call, "himi_stub", False))
                and self._himi_draw_call_object_name(draw_call) not in stale_names
            ]
        return stale_names

    def _himi_create_stub_object(self, bare: str) -> str:
        """创建占位对象：3 顶点 1 三角面（1e-6 尺度），权重挂在全局槽 0。

        权重组名恒为 ``"0"``：站位对象只有 1 个权重组，导出侧
        ``_prepare_merged_skeleton_vertex_groups`` 会「剔非数字组 → 补缺 → 按名排序 →
        重命名 ``str(index)``」⇒ index 恒 0 ⇒ **不新增任何槽位**（计划书 §7.7.5）。
        """
        return blender_create_stub_object(bare)

    def _himi_inject_stub_objects(self) -> list:
        """按计划书 §7.7.3/§7.7.4 的三条规则注入站位（开关关闭时完全不调用）。

        创建失败**不得静默继续**：站位是「原版 draw 会被压制」的唯一凭据，失败后继续
        导出会得到「几何已合并 + 原版照画」的重影产物；与 ZZMI 同款，用 ``Fatal``
        中止导出（调用方的 ``finally`` 会清理半成品）。
        """
        if not bool(GlobalProterties.import_merged_vgmap()):
            # ST12：非合并路径完全不调用站位逻辑（产物与改造前逐字节相同）。
            return []
        from ...utils.format_utils import Fatal

        # 自愈必须早于 present 集合构造（残留站位对象会被误判为真实部件）。
        self._himi_purge_stale_stub_state()

        workspace_root = GlobalConfig.path_workspace_folder()
        if not self._himi_merged_data_present(workspace_root):
            # 前置门（计划书 §7.7.7 S1/S2）：**没有合并数据 ⇒ 没有合并骨架资源 ⇒
            # 站位无从谈起**。HIMI 的站位不承载作用 A（静态骨架），它的唯一效果就是
            # 用 `handling = skip` 压掉原版 draw；在「本次导出根本不产出合并骨架」的
            # 场景下压掉原版 draw 只会让部件凭空空掉。
            #
            # 为什么不用 `_himi_resource_emitted` / `plan.ok` 做门：两者都在
            # `__init__` 之后才成立 —— `_himi_resource_emitted` 只在 INI 生成期置位，
            # 而计划要等模型组装完才能算（`_himi_export_entries` 读
            # `drawib_model_list`）。用它们做门会让**永远**不注入。故此处用
            # 「工作空间是否真有合并数据」这个与导出集合无关、且在 `__init__` 可判的
            # 等价前置条件。
            print(
                f"{_STUB_LOG_PREFIX} 工作空间没有合并骨架数据（VGMap/VGCount）"
                "⇒ 本次导出不产出合并骨架资源 ⇒ 不注入站位"
            )
            return []

        present_bares: set = set()
        for draw_call in self._himi_ordered_draw_calls():
            if bool(getattr(draw_call, "himi_stub", False)):
                continue
            if not self._himi_drawcall_object_present(draw_call):
                continue
            unique_str = ""
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:  # noqa: BLE001
                unique_str = str(getattr(draw_call, "obj_name", "") or "")
            bare = bare_from_unique_str(unique_str)
            if bare:
                present_bares.add(bare)

        # 兼容兜底：一个真实场景对象都解析不到（轻量宿主 / 拿不到 bpy 数据）⇒ 退回
        # 「按声明名判定」= 全部视为存在 ⇒ 不插桩。宁可不插桩，也不要在信息不足时
        # 凭空给全部部件插桩（与 ZZMI 同款兜底）。
        declared_present = {
            bare_from_unique_str(self._himi_draw_call_object_name(draw_call))
            for draw_call in self._himi_ordered_draw_calls()
            if not bool(getattr(draw_call, "himi_stub", False))
        }
        declared_present.discard("")
        scene_present = present_bares if present_bares else declared_present

        family_map = load_component_family_map(workspace_root)
        used_group_ids = self._himi_collect_used_group_ids()
        decision = decide_stub_injection(
            workspace_root=workspace_root,
            scene_present_bares=scene_present,
            family_map=family_map,
            used_group_ids=used_group_ids,
        )
        self._himi_stub_decision = decision
        for line in decision.diagnostics:
            print(f"{_STUB_LOG_PREFIX} {line}")

        created: list = []
        for bare in decision.stub_members:
            try:
                obj_name = self._himi_create_stub_object(bare)
            except Exception as exc:  # noqa: BLE001
                raise Fatal(
                    f"HIMI 站位对象创建失败（{bare}）：{exc}。站位是抑制原版 draw、"
                    "避免重影的唯一凭据；继续导出会得到重影产物，故中止导出。"
                ) from exc
            if not obj_name:
                raise Fatal(
                    f"HIMI 站位对象创建失败（{bare}）：未返回对象名。中止导出。"
                )
            # 必须在任何后续构造步骤之前登记；否则批量创建中途失败时
            # __init__ 的异常回滚拿不到先前创建的对象。
            self._himi_stub_object_names.append(obj_name)
            stub_draw_call = HIMIStubDrawCall(
                obj_name=obj_name,
                vertex_count=len(HIMI_STUB_TRIANGLE_VERTICES),
                index_count=len(HIMI_STUB_TRIANGLE_VERTICES),
                index_offset=0,
            )
            ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
            if ordered is not None:
                ordered.append(stub_draw_call)
            self._himi_stub_draw_calls.append(stub_draw_call)
            # 登记站位 bare：`_himi_export_entries` 用它做**只读标注**（`is_stub`），
            # **不**用它把站位 bare 排除出 `export_bares`（理由见 `_himi_export_bares`）。
            self._himi_stub_bares.add(str(bare))
            created.append(obj_name)
            print(
                f"{_STUB_LOG_PREFIX} 部件 {bare} 没有对应对象，"
                "已创建极限小三角面占位（游戏内不可见，用于抑制原版 draw 防重影）"
            )
        return created

    def _himi_cleanup_stub_objects(self) -> None:
        """导出结束后移除站位对象、网格与注入蓝图的 DrawCall（事务闭合）。"""
        tracked = list(getattr(self, "_himi_stub_draw_calls", []) or [])
        object_names = set(getattr(self, "_himi_stub_object_names", []) or [])
        object_names.update(
            self._himi_draw_call_object_name(draw_call) for draw_call in tracked
        )
        object_names.discard("")
        tracked_ids = {id(draw_call) for draw_call in tracked}
        ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if id(draw_call) not in tracked_ids
                and not bool(getattr(draw_call, "himi_stub", False))
            ]
        for obj_name in sorted(object_names):
            blender_remove_stub_object_data_by_name(obj_name)
        if object_names:
            print(f"{_STUB_LOG_PREFIX} 已清理 {len(object_names)} 个占位小三角面对象")
        self._himi_stub_object_names = []
        self._himi_stub_draw_calls = []
        self._himi_stub_bares = set()

    def himi_stub_decision(self):
        """本次导出的站位决策（无决策时为 ``None``）。"""
        return self._himi_stub_decision

    def himi_stub_object_names(self) -> list:
        return list(self._himi_stub_object_names)

    def himi_stub_bares(self) -> set:
        """本次注入的站位 bare（**只读标注**：这些 bare 仍**进** `export_bares`，
        理由见 `_himi_export_bares`）。"""
        return set(getattr(self, "_himi_stub_bares", ()) or ())

    # ------------------------------------------------------------------ #
    # 计划
    # ------------------------------------------------------------------ #

    @staticmethod
    def _himi_submesh_unique_str(submesh_model) -> str:
        return str(
            getattr(submesh_model, "workspace_unique_str", "")
            or getattr(submesh_model, "unique_str", "")
            or ""
        )

    def _himi_export_entries(self) -> list:
        """本次导出的部件（unique_str / bare / vg_count / is_stub）。

        **F1 之后站位件真的会出现在 `drawib_model_list` 里**（注入早于基类组装模型），
        而 `SubmeshModel.vg_count` 是按 **unique_str** 从工作空间 json 解析的
        （`common/submesh_metadata.py:178`）—— 缺席部件的 json 往往**仍然存在**
        （它是被 join 走的，不是没提取过）⇒ 它的 `vg_count` 可能 > 0。

        因此这里显式标注 `is_stub`，由调用方决定是否排除；**当前口径是不排除**，
        理由见 `_himi_export_bares`（排除会让 G1 家族门在站位场景必然中止导出）。
        标注本身仍有用途：它是「该 bare 是**被站位的成员**、几何不在场景里」的唯一
        只读凭据，供诊断与上层查询（计划书 §7.7.11.4）。
        """
        stub_bares = set(getattr(self, "_himi_stub_bares", ()) or ())
        entries: list = []
        for drawib_model in self.drawib_model_list:
            for submesh_model in drawib_model.submesh_model_list:
                unique_str = self._himi_submesh_unique_str(submesh_model)
                if not unique_str:
                    continue
                bare = himi_runtime.bare_from_unique_str(unique_str)
                entries.append(
                    {
                        "unique_str": unique_str,
                        "bare": bare,
                        "draw_ib": str(drawib_model.draw_ib),
                        "vg_count": int(getattr(submesh_model, "vg_count", 0) or 0),
                        # 站位件的判定**优先于** vg_count：它是合成对象，永远不是
                        # 「本次导出真正带合并数据的部件」。
                        "is_stub": bare in stub_bares
                        or bool(getattr(submesh_model, "himi_stub", False)),
                    }
                )
        return entries

    @staticmethod
    def _himi_export_bares(entries) -> list:
        """G1 目标集合（= 本次导出真正会写进 mod 的部件）。

        **站位 bare 必须进入这个集合**（与 ZZMI 同语义）。这不是疏忽，是唯一
        不自相矛盾的选择 —— 反证（已实测，见
        ``tests/test_himi_stub_objects.py::StubEndToEndIniTests``）：

        1. 判据只在「缺席部件**有** `VGMap` 且专属槽位被引用」时插桩
           （`is_drawib_absorbed`）⇒ 能插桩的缺席部件**必然已落盘**（有 json）；
        2. G1 的家族门是 `L(ib) ⊆ T`，而 `L(ib)` = 该家族**已落盘**组件
           （`common/himi_skeleton.py` 的 `family_members`）；
        3. 于是若把站位 bare 排除出 `T`，被吸收成员永远 `missing` ⇒ 家族永远不
           `safe` ⇒ 契约 `level=error` ⇒ `Fatal` **中止导出**。

        即：**在站位被注入的唯一场景里，排除站位会让导出必然中止** —— 正是站位
        要解决的那个场景。ZZMI 的做法与此一致：站位 DrawCall 被追加进 `ordered`
        （`ui/universal/zzmi.py:1008-1013`），因此它的身份自然进入 ZZMI 的计划，
        家族门才会放行。

        `is_stub` 保留为**只读标注**（`himi_stub_bares()` 供诊断/上层查询），
        **不参与过滤** —— 过滤口径只有一个事实源：`vg_count > 0`。

        规格裁定：计划书 §7.7.11（方案 (A) 采用 / 方案 (B)「排除」否决）+
        `reports/himi-merge/05-decisions.md` §3.20；反证链的独立复核：
        `reports/himi-merge/18-stub-rereview.md` §4.2。
        """
        return [
            entry["bare"]
            for entry in entries
            if int(entry.get("vg_count", 0) or 0) > 0
        ]

    def himi_merged_runtime_plan(self):
        """本次导出的运行时计划（只读工作空间缓存；dump 删了也能算）。

        本模块的异常**绝不**中断导出：任何意外都退化成「没有运行时计划」+ 大声诊断，
        由契约判定决定是继续（普通/部分合并）还是中止（有数据却全被拒）。
        """
        if self._himi_plan_ready:
            return self._himi_plan
        self._himi_plan_ready = True
        try:
            entries = self._himi_export_entries()
            plan = himi_runtime.load_workspace_plan(
                workspace_root=GlobalConfig.path_workspace_folder(),
                export_unique_strs=[entry["unique_str"] for entry in entries],
                # G1 目标集合 = 本次导出真正会写进 mod 的部件；数据层会用它做
                # pass 级门控（家族里任何落盘成员缺席 ⇒ 整个 pass 不合并）。
                # 站位 bare **进**这个集合的理由（含反证）见 `_himi_export_bares`。
                export_bares=self._himi_export_bares(entries),
                checkbox_enabled=bool(GlobalProterties.import_merged_vgmap()),
            )
        except Exception as exc:  # noqa: BLE001 - 导出通道不得被本模块异常打断
            plan = himi_runtime.HIMIExportRuntimePlan(
                ok=False, message=f"HIMI 合并骨架运行时计划计算异常：{exc}", source="exception"
            )
            plan.diagnostics.append(plan.message)
        for line in plan.diagnostics:
            print(f"{_LOG_PREFIX} {line}")
        self._himi_plan = plan
        return plan

    # ------------------------------------------------------------------ #
    # 契约（必须在任何写盘之前）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _himi_notify(detail: str, *, warning: bool = False) -> None:
        prefix = f"{_LOG_PREFIX} 警告 " if warning else f"{_LOG_PREFIX} "
        for line in str(detail).splitlines() or [""]:
            print(prefix + line)
        try:
            from ...utils.log_utils import LOG

            (LOG.warning if warning else LOG.info)(detail)
        except Exception:  # noqa: BLE001 - 日志器不可用时 print 已经送达
            pass

    def _himi_verify_deform_draw_structure(self, plan) -> None:
        """【K2】参与合并的每个部件，其 Position/Blend 必须是 ``pointlist`` 抽取（否则 `Fatal`）。

        根因（t11 §2 实测 + t13 逐帧核对）：跨族合并把「一族一个 deform pass」重放成
        ``draw = <行数>, 0``，其语义前提是该 pass 是**非索引 pointlist Draw（带 SO 目标）**；
        若某族的 Position/Blend 是 trianglelist，重放画出来的就不是变形流输出。

        **t18 / T5-F2 修复**：此前本门用 ``record.get("unique_str")`` 定位 json，而
        `plan.rebinds` 的字典**没有这个键**（`common/himi_merged_runtime.py` 的手写字面量）
        ⇒ **每条记录被 `continue` 跳过**：门在真实导出路径上一次判断都没做，
        `unavailable == 0` 同样是空转的副产物（t5 实测「通知 0 条」）。现在：
        * 计划侧补齐 `unique_str`（单一事实源在计划里）；
        * 本门维护**可观测账目**（``判过/判定/读不到/缺身份`` 四个计数）并**无论成败都打一行汇总**
          ——「没有报错」不再被当成「检查过了」的证据；
        * 记录缺 ``unique_str``（计划形状回退）会被**单独计数并大声警告**，而不是静默跳过。
        """
        from ...utils.format_utils import Fatal

        problems: list = []
        judged = 0           # 判据看过的记录数（= 每条 rebind 都算，不只算能定位 json 的）
        checked = 0          # 真读到 json **且含可判元素**、被判定过的部件数
        unavailable = 0      # 有身份但 json 读不到（cache-only 重建 / 文件缺失）
        unjudgeable = 0      # json 读到了，但里面没有 Position/Blend 元素记录（不可判）
        missing_identity = 0  # 记录没有 unique_str ⇒ 门无法定位 json（计划形状回退）
        for item in getattr(plan, "rebinds", None) or ():
            record = dict(item or {})
            judged += 1
            unique_str = str(record.get("unique_str") or "")
            if not unique_str:
                missing_identity += 1
                continue
            try:
                json_path = himi_runtime.resolve_submesh_json_path(
                    GlobalConfig.path_workspace_folder(), unique_str
                )
            except Exception:  # noqa: BLE001 - 核对本身不得打断导出
                json_path = ""
            if not json_path or not os.path.isfile(json_path):
                unavailable += 1
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:  # noqa: BLE001
                unavailable += 1
                continue
            if not deform_draw_structure_judged(payload):
                unjudgeable += 1
                continue
            checked += 1
            problems.extend(
                deform_draw_structure_problems(payload, unique_str=unique_str)
            )
        if problems:
            verdict = "失败"
        elif judged == 0:
            # 【t20 / T19-F1】一条记录都没判过（`rebinds` 为空：当前不可达，但**形状必须显式排除**）
            # ⇒ 不得报「通过」：那正是 K2 原病（**空转也报成功**）的同一个形状。
            verdict = "未核对（无 rebind 记录）"
        elif checked == 0:
            # 【t20 / T19-F1 的完整形状】有记录、但**一个部件都没真判过**
            # （全部缺身份 / 读不到 json / json 无可判元素）⇒ 同样是「空转」，
            # 只能说「未核对」；三种原因各自的计数与警告在上面的账目里。
            verdict = "未核对（没有可判的部件）"
        else:
            verdict = "通过"
        summary = (
            f"变形 pass 结构核对（K2）：判过 {judged} 条记录、读到 json 并判定 {checked} 个部件、"
            f"json 里没有可判元素 {unjudgeable} 个、读不到 json {unavailable} 个、"
            f"记录缺身份 {missing_identity} 条 ⇒ {verdict}"
        )
        # **可观测证据**：无论成败都打这一行（t18 起「有没有真的检查」由它回答）。
        print(f"{_LOG_PREFIX} {summary}")
        self._himi_deform_structure_result = {
            "judged": judged,
            "checked": checked,
            "unavailable": unavailable,
            "unjudgeable": unjudgeable,
            "missing_identity": missing_identity,
            "problems": list(problems),
            "verdict": verdict,
            "summary": summary,
        }
        if missing_identity:
            self._himi_notify(
                f"变形 pass 结构核对：{missing_identity} 条 rebind 记录没有 unique_str ⇒ 门**无法定位"
                "部件 json**、这一部分根本没判过（计划侧应始终带上该字段，见 "
                "common/himi_merged_runtime.py::plan_from_metadata）",
                warning=True,
            )
        if unjudgeable:
            self._himi_notify(
                f"变形 pass 结构核对：{unjudgeable} 个部件的 json 里**没有 Position/Blend 元素记录**"
                "⇒ 这几种情况标为未核对（不阻断，但也**不算通过**）",
                warning=True,
            )
        if unavailable:
            self._himi_notify(
                f"变形 pass 结构核对：{unavailable} 个部件没有可读 json "
                "（cache-only 重建？）⇒ 这些部件标为未核对（不阻断）",
                warning=True,
            )
        if problems:
            raise Fatal(
                "变形 pass 结构自检失败 ⇒ 中止导出（`draw = <行数>, 0` 的重放语义不成立）："
                + "；".join(problems[:6])
                + ("；…" if len(problems) > 6 else "")
            )

    # ------------------------------------------------------------------ #
    # 【F6 / T45-F4】导出前的两道硬门（都必须早于任何写盘）
    # ------------------------------------------------------------------ #

    def _himi_verify_base_table_against_plan(self, plan) -> None:
        """【F6】冻结基址表 vs 本次 ``plan.family_ranges``：**矛盾就 Fatal**，其余大声诊断。

        根因（报告 50 §1.2 入口 ② / §6-1）：表是**字符串常量**，ini 的 ``array=``/``Dispatch``
        来自动态 plan，两者此前没有任何比对 ⇒ 换角色 / 重建工作空间使 span 变化时，
        5 族在运行期全部查不到基址、一根骨都不写 ⇒ 骨架全零 ⇒ 模型**静默消失**。
        优先 Fatal（不静默降级）：不给用户一个「看上去导出了、进游戏没了」的产物。

        **t4 修正**：F5 的身份资源（``cs-t1``）让运行期 base/span 来自**本次 plan**，所以
        「plan 的 span 不在表里」不再等于会消失（那种角色照样正确写入）—— 把它也当 Fatal
        会让 HIMI 只对当前这一个角色可用。⇒ 只有**表与 plan 直接矛盾**（同 span 不同 base）
        才 Fatal；其余（表缺该 span / 表有多余旧行 / ∑span ≠ 槽位数）写成**诊断**。
        """
        from ...utils.format_utils import Fatal

        audit = merged_attach_base_table_audit(
            getattr(plan, "family_ranges", None) or {},
            int(getattr(plan, "slot_count", 0) or 0),
        )
        # 诊断收集器用 getattr 兜底：本仓有 `ExportHIMI.__new__` 的手工夹具（不走 __init__）。
        notices = getattr(self, "_himi_base_table_notices", None)
        if notices is None:
            notices = []
            self._himi_base_table_notices = notices
        for line in audit.get("notices") or ():
            notices.append(str(line))
            self._himi_notify(f"基址表诊断：{line}", warning=True)
        problems = list(audit.get("problems") or ())
        if problems:
            raise Fatal(
                "合并骨架基址表与本次家族区间**直接矛盾** ⇒ 中止导出（运行期会整族拒写）："
                + "；".join(problems)
            )

    def _himi_verify_vertex_group_index_names(self) -> None:
        """【T45-F4】``index == int(组名)`` 的导出期断言，否则 ``Fatal``。

        根因（报告 50 §5.G）：导出侧写 BLENDINDICES 取**组索引**，而全局槽号按组索引记账；
        HIMI 不跑 EFMI/ZZMI 的 ``_prepare_merged_skeleton_vertex_groups``（那套会按名排序并
        重命名），所以「名字与索引分叉」这条通道在 HIMI 侧**完全没有门**（2026-09 实测缺门）。
        实测：用户场景 13 个 mesh 物体 3120/3120 组满足不变量 ⇒ 这道门不误杀
        （``.dbg/_t4_vgroup_probe.py``）。
        """
        from ...utils.format_utils import Fatal

        names: list = []
        for entry in self._himi_export_entries():
            name = ""
            for drawib_model in self.drawib_model_list or ():
                for submesh_model in drawib_model.submesh_model_list or ():
                    if self._himi_submesh_unique_str(submesh_model) == entry["unique_str"]:
                        name = str(getattr(submesh_model, "blender_obj_name", "") or "")
                        if not name:
                            name = str(getattr(submesh_model, "obj_name", "") or "")
                        if not name:
                            name = str(entry["unique_str"])
                        break
                if name:
                    break
            if name:
                names.append(name)
        problems = blender_vertex_group_index_name_problems(names)
        if problems:
            raise Fatal(
                "顶点组「索引 == 名字」自检失败 ⇒ 中止导出（继续导出会把骨骼编号与骨矩阵"
                "对错位置，表现为整块错位）："
                + "；".join(problems[:6])
                + ("；…" if len(problems) > 6 else "")
            )

    def _himi_enforce_contract(self) -> dict:
        """按合并骨架契约判定「继续 / 中止」；必须早于 ``generate_buffer_files``。

        * ``error``（有数据但一个部件都没进合并）⇒ ``Fatal``（仓库既有致命通道，
          导出操作符会把它变成用户可见的 ``report({'ERROR'})``）。
        * ``warning``（部分被拒）⇒ 用户可见提示后继续导出。
        * ``notice``（没有合并数据）⇒ 一行日志，绝不打断。
        """
        from ...utils.format_utils import Fatal

        plan = self.himi_merged_runtime_plan()
        # 【t85·结构性隔离】下面三项是**合并骨架专属**检查（F6 基址表 / K4 触发键 /
        # K2 变形 pass 结构），只在合并路径激活时执行。旧行为在普通导出（开关关闭）也跑：
        # 基址表对着**空的** `family_ranges` 判出「多出 5 行」⇒ 控制台刷 5 条 Warning，
        # 而且这些诊断被写进产物 ini 的合并骨架注释块 —— 用户实报「不使用合并骨骼时
        # 配置表里面会有合并骨骼的生成」。判据与 `generate_unity_vs_config_ini` 用
        # **同一个谓词**：`_himi_merged_attach_active()`。
        merged_active = self._himi_merged_attach_active()
        if merged_active:
            # 【F6】基址表 vs 动态排布（先于任何写盘）：不一致 = 运行期「一族都不写 ⇒ 模型消失」。
            self._himi_verify_base_table_against_plan(plan)
        # 【T45-F4】顶点组「索引 == 名字」：**两种模式都要**（分叉会让任一模式都绑错骨）。
        self._himi_verify_vertex_group_index_names()
        if merged_active:
            # 【t12/t13 收编 K2/K4】合并 deform 的**触发键**与**变形 pass 结构**：
            # K4（键不得 1→多）：只诊断不阻断 —— t13 实测 4 帧内无实际歧义（理论风险）。
            key_notices = getattr(self, "_himi_key_notices", None)
            if key_notices is None:
                key_notices = []
                self._himi_key_notices = key_notices
            for line in merged_deform_key_problems(getattr(plan, "rebinds", None) or ()):
                key_notices.append(str(line))
                self._himi_notify(f"合并 deform 触发键诊断：{line}", warning=True)
            # K2（宿主/成员族的变形 pass 必须是 pointlist 抽取）：`draw = <行数>, 0` 的语义前提。
            self._himi_verify_deform_draw_structure(plan)
        # 【t39】族级校准的两侧必须**同时**成立：五族的 Q_f 不可得 ⇒ Fatal（不静默降级成
        # 「只校准顶点侧」或「都不校准」——那两种都会让产物与调参者的预期不符）。
        if bool(GlobalProterties.import_merged_vgmap()) and getattr(plan, "has_rebinds", False):
            placements = self._himi_family_placements()
            placement_problems = list(getattr(self, "_himi_placements_problems", []) or [])
            if placement_problems and not placements:
                # 【t73·兜底原则】族级校准只拿到一部分族 ⇒ 合并骨架侧失败 ⇒ **降级为普通导出**
                # （半校准的合并产物比不校准更坏；但普通导出必须留下）。
                self._himi_degrade_to_ordinary(
                    "族级校准（t39）只拿到一部分族的摆放（半校准的产物比不校准更坏）："
                    + "；".join(placement_problems[:6])
                    + ("；…" if len(placement_problems) > 6 else "")
                )
            elif placements:
                # 【t39-X】族级物体矩阵 `X_f = M_base⁻¹·M_f`：合并后整块只在**宿主族一笔 draw**
                # 里渲染，缺了它各族就按宿主族摆放（实测偏差 1.0~3.8 游戏单位）。三态：
                #   * 全部族都有合法 X_f ⇒ 校准（正常路径）；
                #   * **部分**缺失/不合法 ⇒ `Fatal` 并**指名缺哪族、为什么** —— 这次的证据
                #     是抓帧 vs-cb3 的对象矩阵，缺一族的成因是确定的，半校准比不校准更坏；
                #   * 一个都没有（老工作空间没有抓帧）⇒ `_himi_family_x` 里大声警告、不校准，
                #     导入/导出照常（CS 读到全零 X ⇒ 跳过左乘，与旧行为逐字节等价）。
                self._himi_family_x()
                family_x_problems = list(getattr(self, "_himi_family_x_problems", []) or [])
                if family_x_problems:
                    raise Fatal(
                        "族级物体矩阵（t39-X）只拿到一部分族的 X_f（半校准的产物比不校准更坏）："
                        + "；".join(family_x_problems[:6])
                        + ("；…" if len(family_x_problems) > 6 else "")
                    )
        snapshot = plan.contract_snapshot(
            checkbox_enabled=bool(GlobalProterties.import_merged_vgmap())
        )
        decision = evaluate_merged_skeleton_contract(**snapshot)
        self._himi_contract_decision = decision

        level = str(decision.get("level") or "")
        detail = "\n".join(
            str(part) for part in (decision.get("message"), decision.get("hint")) if str(part)
        )
        if level == "error":
            # 【t73·兜底原则】不再一票否决导出：合并骨架侧失败 ⇒ **降级为普通导出 + 大声警告**。
            # （旧行为 `raise Fatal(...)` 会把普通导出一起干掉 —— 兜底不可被剥夺。）
            self._himi_degrade_to_ordinary(detail or "HIMI 骨骼合并契约校验失败")
            level = "degraded"
            self._himi_contract_decision = dict(decision, level="degraded",
                                                degrade_reason=detail or "")
        if level == "warning":
            self._himi_notify(detail, warning=True)
        elif level == "notice":
            print(f"{_LOG_PREFIX} {decision.get('message', '')}")
        elif level == "degraded":
            print(f"{_LOG_PREFIX} 已降级为普通导出（合并骨架侧失败，见上面的警告）")
        if plan.ok:
            print(f"{_LOG_PREFIX} {plan.message}")
        else:
            print(f"{_LOG_PREFIX} 本次按普通导出（合并骨架路径未激活/已降级）：{plan.message}")
        for line in UNVERIFIED_IN_GAME:
            print(f"{_LOG_PREFIX} 未实机验证（{line}）")
        return decision

    # ------------------------------------------------------------------ #
    # 导出入口
    # ------------------------------------------------------------------ #

    def export(self):
        try:
            # 计划 + 契约判定必须先于任何写盘：generate_buffer_files 是本次导出的第一个
            # 落盘点，error 时不得留下半成品 mod（.buf / .ini）。
            self._himi_enforce_contract()
            super().export()
        finally:
            # 事务闭合：正常与异常路径都必须清掉站位对象/网格/注入的 DrawCall。
            self._himi_cleanup_stub_objects()

    def export_buffers_only(self):
        """多轮导出的纯缓冲路径也必须闭合站位对象事务。"""
        try:
            return super().export_buffers_only()
        finally:
            self._himi_cleanup_stub_objects()

    # ------------------------------------------------------------------ #
    # deform 重绑（唯一运行时动作）
    # ------------------------------------------------------------------ #

    def _himi_rebind_for(self, drawib_model):
        """该 DrawIB 的可重绑记录；不可重绑（未通过 G1 / 无数据 / 前置条件不成立）返回 ``None``。"""
        plan = self.himi_merged_runtime_plan()
        if not plan.has_rebinds:
            return None
        rebind = plan.rebind_for(str(drawib_model.draw_ib))
        if rebind is None:
            return None
        declared_hash = str(
            (getattr(drawib_model, "category_hash_dict", {}) or {}).get("Blend", "") or ""
        )
        plan_hash = str(rebind.get("category_hash", "") or "")
        if declared_hash and plan_hash and declared_hash.lower() != plan_hash.lower():
            # 段匹配 hash 与计划里的不一致 ⇒ 重绑可能落到别的 pass 上：宁可退普通路径。
            self._himi_notify(
                f"{drawib_model.draw_ib}: VB 段的 Blend hash（{declared_hash}）与"
                f"合并数据里的 CategoryHash.Blend（{plan_hash}）不一致，"
                "该部件退回普通路径（不重绑 vs-t0）。",
                warning=True,
            )
            return None
        verdict = self._himi_blend_namespace_verdict(drawib_model, rebind)
        if verdict.get("verdict") == "local":
            self._himi_notify(
                f"{drawib_model.draw_ib}: {verdict.get('note')}", warning=True
            )
            return None
        if verdict.get("verdict") in {"indeterminate", "unavailable"}:
            print(f"{_LOG_PREFIX} 提示 {drawib_model.draw_ib}: {verdict.get('note')}")
        return rebind

    def himi_family_range(self, family: str):
        """该族在合并骨架里的 ``{"base", "slots", "own_block"}``；不可发段 ⇒ ``None``。

        只有一个事实源（``plan.family_ranges``，步骤① 的 fail-closed 判定）：
        ``own_block=False`` 的**借位族**没有自己的块 ⇒ **不发捕获行、不发 attach 段**
        （它的槽由拥有者家族的段写入，矩阵按逐位去重语义相同）。
        """
        plan = self.himi_merged_runtime_plan()
        info = dict((plan.family_ranges or {}).get(str(family or "")) or {})
        if not info or not bool(info.get("own_block", False)):
            return None
        if int(info.get("slots", 0) or 0) <= 0:
            return None
        return info

    def himi_family_range_items(self) -> list:
        """全部**可发段**家族（按 base 升序）：``[(family, {"base","slots","own_block"}), …]``。"""
        plan = self.himi_merged_runtime_plan()
        items = [
            (str(family), dict(info))
            for family, info in (plan.family_ranges or {}).items()
            if bool(dict(info).get("own_block", False)) and int(dict(info).get("slots", 0) or 0) > 0
        ]
        items.sort(key=lambda item: (int(item[1]["base"]), item[0]))
        return items

    def _himi_exported_blend_bytes(self, drawib_model):
        """本次导出真正写出的 Blend 字节（numpy uint8 扁平数组 → bytes）。"""
        buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
        payload = buffers.get("Blend")
        if payload is None:
            return b""
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        tobytes = getattr(payload, "tobytes", None)
        if callable(tobytes):
            try:
                return tobytes()
            except Exception:  # noqa: BLE001
                return b""
        try:
            return bytes(payload)
        except Exception:  # noqa: BLE001
            return b""

    def _himi_blend_namespace_verdict(self, drawib_model, rebind) -> dict:
        """核对 mod 的 Blend 是否带**全局**骨骼编号（重绑 vs-t0 的前置条件）。

        ``vb1`` 换成 mod 的 Blend 之后，游戏 deform VS 会用里面的编号去索引重绑后的
        骨架；只有编号是全局槽位时才正确。核对用的对照物是工作空间里那份**原部件**
        Blend（游戏局部编号）+ json 的 ``VGMap``。判定为 ``local`` 时该家族不合并
        （退回基类路径，保持 mod 与今天一致的行为）+ 大声诊断。
        """
        draw_ib = str(drawib_model.draw_ib)
        if draw_ib in self._himi_blend_verdicts:
            return self._himi_blend_verdicts[draw_ib]
        unique_strs = [
            self._himi_submesh_unique_str(submesh_model)
            for submesh_model in drawib_model.submesh_model_list
        ]
        span = himi_runtime.blend_indices_span(
            getattr(getattr(drawib_model, "d3d11GameType", None), "D3D11ElementList", []) or []
        )
        verdict = himi_runtime.check_exported_blend_namespace(
            workspace_root=GlobalConfig.path_workspace_folder(),
            unique_strs=[name for name in unique_strs if name],
            exported_blend_bytes=self._himi_exported_blend_bytes(drawib_model),
            blend_span=span,
        )
        self._himi_blend_verdicts[draw_ib] = verdict
        return verdict

    def add_unity_vs_texture_override_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """在基类 VB 段形态上注入 ``vs-t0`` 重绑与 deform 顶点数（计划书 §7.4 + t48 修正）。

        逐行同构于 ``ui/universal/unity.py:44-73``；只有「该部件通过 G1」时，
        在 draw 所在的那个类别段（HIMI = ``Blend`` 类别）里多两处差异：

        * ``vs-t0 = ResourceHI3MergedSkeleton_<role_key>``
        * ``draw = <**模组自己上传的行数**>, 0`` ——权威来源是 ``drawib_model.draw_number``
          （= ``vertex_count``，即同产物的 ``override_vertex_count``）：deform 重放就是把
          模组上传的每一行过一遍，写多会越界读并把角色级 SO 池写爆。

        ⚠ **被推翻的旧口径（t47 实机证伪）**：这里曾写「本 IB 家族各分片
        ``OriginalVertexCount`` 之和」（= 计划书 §7.4 第 1 条原文）。实测每家族只有一个
        deform pass，而同族每成员的 OVC **相同**（分片/LOD 变体）⇒ 求和是 N 倍
        （用户产物 99475 vs 模组 31066 行，最高 1263×）⇒ 正是「无骨骼约束 + 错位 +
        上下颠倒 + 部件随意旋转」的成因。家族 Σ 现在只作**诊断**（见
        ``himi_runtime.resolve_deform_draw_count``）。
        """
        rebind = self._himi_rebind_for(drawib_model)
        if rebind is None:
            super().add_unity_vs_texture_override_vb_sections(
                ini_builder=ini_builder, drawib_model=drawib_model
            )
            return

        d3d11_game_type = drawib_model.d3d11GameType
        draw_ib = drawib_model.draw_ib

        texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
        texture_override_vb_section.append("; " + draw_ib)
        expect_capture = False
        for category_name in d3d11_game_type.OrderedCategoryNameList:
            category_hash = drawib_model.category_hash_dict.get(category_name, "")
            texture_override_vb_name_suffix = (
                "VB_" + draw_ib + "_" + drawib_model.draw_ib_alias + "_" + category_name
            )
            texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_name_suffix + "]")
            texture_override_vb_section.append("hash = " + category_hash)

            for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                if category_name != draw_category_name:
                    continue
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_vb_section.append(
                    category_original_slot + " = Resource" + draw_ib + original_category_name
                )

            draw_category_name = d3d11_game_type.CategoryDrawCategoryDict.get("Blend", None)
            if draw_category_name is not None and category_name == draw_category_name:
                # 【每帧机制 t54】① 捕获当帧 palette → ② run attach CS → ③ 才重绑，然后
                # `handling = skip` + `draw`。三行的**方向与顺序**都是硬约束（见
                # `blend_block_order_problems`：写反 = 把空副本写进游戏当帧 palette；
                # 反序 = 自反馈，捕到的是自己刚绑上的骨架）。逐行由步骤① 的渲染器产出，
                # 本模块不另写一份文本。
                family = str(rebind.get("family") or draw_ib)
                range_info = self.himi_family_range(family)
                if range_info is not None:
                    expect_capture = True
                    for line in himi_runtime.render_attach_capture_lines(family):
                        texture_override_vb_section.append(line)
                else:
                    print(
                        f"{_LOG_PREFIX} 提示 {draw_ib}: 家族 {family} 没有专属块（借位族或"
                        "区间不可得）⇒ 本次不发捕获行/attach 段，只重绑 vs-t0"
                    )
                # 与 draw 同一段内绑定，保证重绑与 draw 之间没有别的 pass 插进来。
                texture_override_vb_section.append(
                    "vs-t0 = " + str(rebind["vs_t0_resource"])
                )
                texture_override_vb_section.append("handling = skip")
                exported_rows = int(getattr(drawib_model, "draw_number", 0) or 0)
                resolved = himi_runtime.resolve_deform_draw_count(
                    rebind.get("family_deform_vertex_count"),  # 仅诊断（t48）
                    exported_rows,  # 权威：模组自己上传的行数
                )
                # 物理不变量（t48）：写进 ini 的 draw 不得超过**模组自己上传的行数**
                # （= 同产物 ``override_vertex_count``）。越界读会把角色级 SO 池写爆
                # （t47 实机症状）。这里**只报警、不静默夹取** —— 静默夹取会让离线护栏
                # 永远看不到越界值，而这条链的教训正是「没有护栏 ⇒ 差 68409 行零红」。
                if int(resolved["count"]) > exported_rows:
                    self._himi_notify(
                        f"{draw_ib}: deform 重放 draw（{int(resolved['count'])}）超过模组"
                        f"自身行数（{exported_rows}）⇒ 会越界读并把角色级 SO 池写爆"
                        "（t47 症状：无骨骼约束/错位/上下颠倒）",
                        warning=True,
                    )
                texture_override_vb_section.append(
                    "draw = " + str(int(resolved["count"])) + ", 0"
                )
                if resolved.get("note"):
                    print(
                        f"{_LOG_PREFIX} 提示 {draw_ib}: {resolved['note']}"
                        f"（采用 {int(resolved['count'])}，来源 {resolved['source']}）"
                    )

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_vb_section.append("$active0 = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_vb_section.append("$ActiveCharacter = 1")

            texture_override_vb_section.new_line()

        # 【源码级顺序护栏】每个 Blend 块内：捕获 → run 都必须在 `vs-t0 =` 重绑之前。
        # 违反 ⇒ 产物必定坏（自反馈 / 空副本污染游戏 palette），而且**在游戏里完全静默**
        # ⇒ 宁可在这里 Fatal 中止，也不要把写坏的 ini 交给用户。
        problems = blend_block_order_problems(
            texture_override_vb_section.SectionLineList, expect_capture=expect_capture
        )
        # 【F1/F2 的落地判据】变形重放必须**无条件**执行：该族 deform 段的原 draw 已被
        # `handling = skip` 吞掉 ⇒ SO 目标当帧唯一写方就是这一笔。任何条件行（s3 诊断态的
        # `if $seen_… >= 1`）在宿主族段里**恒假** ⇒ SO 全零 ⇒ 整块几何消失（队长两帧实测）。
        # ⇒ 「把 draw 搬到渲染 IB 段」这类修法（报告 50 F2）已证伪（渲染段没有变形 VS /
        # SO 绑定），它的真需求（draw 无条件、不依赖到达次序）由这条判据钉死。
        problems = problems + deform_replay_block_problems(
            texture_override_vb_section.SectionLineList
        )
        if problems:
            from ...utils.format_utils import Fatal

            raise Fatal(
                f"{draw_ib}: 合并骨架 VB 段的每帧机制顺序自检失败 ⇒ 中止导出："
                + "；".join(problems)
            )

        ini_builder.append_section(texture_override_vb_section)

    # ------------------------------------------------------------------ #
    # 资源声明 + 骨架发布（角色级，只发一次）
    # ------------------------------------------------------------------ #

    def _himi_publish_skeleton(self, folder_name: str) -> dict:
        """把角色级骨架写进 mod 的缓冲目录（ini 的 ``filename`` 指向同一处）。"""
        if self._himi_publish_result:
            return self._himi_publish_result
        plan = self.himi_merged_runtime_plan()
        folder_path = GlobalConfig.path_generatemod_buffer_folder()
        # 单一来源核对：ini 里的 `filename = <folder_name>/...` 必须与真实落盘目录同名。
        if folder_name and os.path.basename(os.path.normpath(folder_path)) != folder_name:
            self._himi_notify(
                f"骨架落盘目录（{folder_path}）与 ini 里声明的目录（{folder_name}）"
                "不一致；ini 的 filename 会指到不存在的路径。",
                warning=True,
            )
        try:
            result = himi_runtime.publish_skeleton_binary(plan, folder_path)
        except Exception as exc:  # noqa: BLE001 - 同上：导出通道不得被本模块异常打断
            result = {
                "ok": False,
                "path": "",
                "written": False,
                "bytes": 0,
                "message": f"骨架发布异常：{exc}",
            }
        self._himi_publish_result = result
        if not result.get("ok"):
            self._himi_notify(
                f"角色级骨架未能写入 mod（{result.get('message')}）；"
                "ini 仍然声明了该资源 ⇒ 资源会加载失败（模型会明显异常），请重新导入后导出。",
                warning=True,
            )
        elif result.get("written"):
            print(f"{_LOG_PREFIX} {result.get('message')} → {result.get('path')}")
        return result

    def _himi_family_index_map(self, plan=None) -> dict:
        """【t74】``{family: (own_index, base_index)}``：物体矩阵表（``cs-u1``）里的族序号。

        索引空间**唯一** = :meth:`himi_family_range_items` 的次序（= 发 ini 段的次序），
        导出侧写载荷 ``[7] = (own, base)`` 与运行期 CS 读它必须是同一套，否则每族会拿别族的
        M 去算 X。宿主族判不出来 / 不在参与族里 ⇒ 返回 ``{}``：载荷 [7] 全零、CS 的运行期
        分支退化为「X = 单位阵」，而此时 ``_himi_family_x`` 也必然一个 X 都给不出（同一判据），
        单位阵与「不左乘」数值等价 ⇒ 与旧产物逐字节一致。

        序号为什么不用 ``plan.family_ranges`` 的下标：借位族（``own_block=False``）不发段、也不写表，
        但它在 ``family_ranges`` 里占一个键 —— 用它的下标当族序号会让表里出现永远全零的空段。
        """
        if plan is None:
            plan = self.himi_merged_runtime_plan()
        families = [str(family) for family, _info in (self.himi_family_range_items() or [])]
        if not families:
            return {}
        try:
            host = self._himi_host_family(plan)
        except Exception:  # noqa: BLE001 - 族索引是可选增强：宿主族判不出来就关掉运行期分支，
            # 不能让它把整笔身份资源发布拖红（载荷里的兜底 X_f 仍然正确）。
            return {}
        host_family = str(host.get("family") or "") if isinstance(host, dict) and host.get("ok") else ""
        if host_family not in families:
            return {}
        base_index = families.index(host_family)
        return {family: (index, base_index) for index, family in enumerate(families)}

    def _himi_publish_attach_ranges(self, plan, folder_name: str) -> dict:
        """【F5 + t39 + t39-X + t74】把每族的身份资源（128 B = base/span/ver/magic + ``Q_f⁻¹`` + ``X_f`` + 族索引）写进 mod 缓冲目录。

        身份资源缺失时 CS 会**退回冻结表**（= 今天的行为，不是静默乱写），但那样 F5 的
        保护就没了 ⇒ 另有文件存在性硬门 :func:`verify_attach_range_references` 在产物落盘
        那一步把它拦死（与 `cs =` 的硬门同款，不降级为 warning）。

        发布成功后还会用**同一组** ``ranges / placements / family_xs / indices`` 调
        :func:`_himi_verify_attach_range_sync` 复核落盘字节（族数 × 128 B 的四方同步）⇒
        任何一族不一致即 ``Fatal``，不留「CS 读到的 X_f 与计划不符」的产物。
        """
        folder_path = GlobalConfig.path_generatemod_buffer_folder()
        if folder_name and os.path.basename(os.path.normpath(folder_path)) != folder_name:
            self._himi_notify(
                f"身份资源落盘目录（{folder_path}）与 ini 里声明的目录（{folder_name}）"
                "不一致；ini 的 filename 会指到不存在的路径。",
                warning=True,
            )
        ranges = getattr(plan, "family_ranges", None) or {}
        palette_factors = {}
        family_xs = {}
        try:
            # 【t44 共同空间取反修复】**载荷存的是 palette 侧要复合的那个因子** = `Q_f⁻¹`
            # （CS 里读出来直接做 `P_f[b] ∘ factor`）。顶点侧用 `Q_f` 本身（`_himi_calibrate_bind_positions`）。
            # 两侧同源同报：都来自同一个 `self._himi_family_placements()`。
            for family, matrix in (self._himi_family_placements() or {}).items():
                palette_factors[family] = himi_runtime.mat3x4_inverse(matrix)
            # 【t39-X】族级物体矩阵 X_f：载荷 [4..6] = X_f 的 3×4 行；CS 在算完 `P_f ∘ Q_f⁻¹`
            # 之后对三条结果行各做一次左乘（X 全零 ⇒ CS 跳过，等价旧行为）。契约钩子已保证
            # 到这里要么全有、要么全无（部分缺失在更早的 `_himi_enforce_contract` 里 Fatal 中止）。
            try:
                family_xs = self._himi_family_x()
                self._himi_publish_family_x_record()
            except Exception as exc:  # noqa: BLE001 - X_f 只是可选增强（载荷 [4..6] 是兜底），
                # 算不出来就把整笔身份资源发布一起拖红没有意义：Q_f⁻¹ 与 base/span 仍然要写。
                family_xs = {}
                self._himi_notify(f"族级物体矩阵 X_f 计算失败（{exc}）⇒ 不校准", warning=True)
            # 【t74】族索引：``[7] = (own, base)``，运行期 CS 靠它决定「读表里哪一段当 M_base」。
            # 与 u1 表的 array=（3 × 族数）同源（= ``himi_family_range_items`` 的次序）。
            family_indices = self._himi_family_index_map(plan)
            if family_xs and not family_indices:
                # 兜底 X 还在、但族索引拿不到 ⇒ 运行期分支不启用。不是错误（载荷兜底照样正确），
                # 但必须说清：否则用户会以为「已经实时计算」，实际仍在用抓帧常量。
                self._himi_notify(
                    "族索引不可用（宿主族判定失败或本次没有可发段的族）⇒ 物体矩阵表（cs-u1）"
                    "不启用，X_f 退回导出期烘死的载荷兜底（与旧产物逐字节等价）。",
                    warning=True,
                )
            result = himi_runtime.publish_attach_ranges(
                ranges,
                folder_path,
                placements=palette_factors,
                family_xs=family_xs,
                indices=family_indices,
            )
        except Exception as exc:  # noqa: BLE001 - 同上：导出通道不得被本模块异常打断
            result = {"ok": False, "written": False, "files": [], "message": f"身份资源发布异常：{exc}"}
        self._himi_attach_range_result = result
        if result.get("ok"):
            # 【t39-X/t74 硬门】写出去还不够：**落盘字节**必须与「族区间 / Q_f⁻¹ / X_f / 族索引 /
            # 128 B」五方一致（`verify_attach_range_files`）。这里用**同一组** ranges/placements/family_xs/indices
            # 复核，所以文件缺失、被截断、或某族的 Q/X 与发布值不等都会在导出期被拦下 ——
            # 而不是留一个 CS 读不到 X_f 的产物（那比不校准更坏，与 :1826 的分级同款）。
            self._himi_verify_attach_range_sync(
                folder_path, ranges, palette_factors, family_xs, family_indices
            )
        if not result.get("ok"):
            self._himi_notify(
                f"家族身份资源未能写入 mod（{result.get('message')}）；"
                "ini 仍然声明了它们 ⇒ 资源会加载失败（F5 的 span 上界退化为冻结表）。",
                warning=True,
            )
        elif result.get("written"):
            print(f"{_LOG_PREFIX} {result.get('message')} → {folder_path}")
        return result

    def _himi_verify_attach_range_sync(
        self, folder_path: str, ranges, placements, family_xs, indices=None
    ) -> list:
        """【t39-X 硬门】核对**已落盘**的身份资源与「族区间 / Q_f⁻¹ / X_f / 族索引 / 128 B」一致。

        导出期唯一的调用点就是 :func:`_himi_publish_attach_ranges`（发布 ``ok`` 之后、用同一组
        输入复核），因此这条判据不再只活在 oracle / 单测里：文件缺失、被截断、或某族的 Q_f⁻¹ /
        ``X_f`` 与发布值**逐字节不等**都会在这里被拦下 —— 分级与 :1826 的「半校准比不校准更坏」
        同款（``Fatal`` 中止导出），而不是留一个 CS 读不到正确 X_f 的产物。

        返回问题清单（空 = 通过）；``Fatal`` 的局部 import 与 :2108 的既有硬门同款（模块顶部
        不得新增 import —— fake-包单测按文件路径加载本模块，顶部 import 会在收集期炸）。
        """
        from ...utils.format_utils import Fatal

        problems = list(
            himi_runtime.verify_attach_range_files(
                folder_path,
                ranges,
                placements=placements,
                family_xs=family_xs,
                indices=indices,
            )
            or []
        )
        if problems:
            raise Fatal(
                "族级身份资源（t39-X/t74）与发布计划不一致（族数 × 128 B 的四方同步失败）："
                + "；".join(problems[:6])
                + ("；…" if len(problems) > 6 else "")
            )
        return problems

    def himi_attach_range_result(self) -> dict:
        """本次导出的身份资源落盘结果（``{"ok","written","files","message"}``）。"""
        return dict(getattr(self, "_himi_attach_range_result", {}) or {})

    # ------------------------------------------------------------------ #
    # 【t39】族级校准：Q_f（族 palette 第 0 槽）—— 两侧（顶点 / palette）唯一事实源
    # ------------------------------------------------------------------ #

    def _himi_family_placements(self) -> dict:
        """``{family: 12 个 float（Q_f，行主序 3×4）}``（不可得 ⇒ 空字典，两侧同时不校准）。

        来源 = 工作空间 ``LOD0/<bare>/ModImpRuntime/<bare>-BoneMatrix.buf`` 的第 0 槽
        （该族自己的摆放；五族取自**同一个提取帧**，所以两两相比时尺度自然抵消）。

        **三态**（t39 的 fail-closed 口径）：
        * 全部族都拿到 ⇒ 返回全部（两侧都校准）；
        * **部分**族拿到 ⇒ 返回空 + ``_himi_placements_problems`` 非空 ⇒ 调用方 ``Fatal``
          （绝不能产出"顶点侧校准了、palette 侧没校准"的半成品）；
        * **一个都没拿到**（合成夹具 / 旧工作空间 / 无缓存）⇒ 返回空 + ``_himi_placements_skipped``
          非空 ⇒ 大声警告 + **两侧都不校准** = 与今天逐字节等价的行为（不新增失败模式）。

        缓存结果，避免一次导出里重复读盘。
        """
        cached = getattr(self, "_himi_placements_cache", None)
        if cached is not None:
            return cached
        plan = self.himi_merged_runtime_plan()
        workspace_root = GlobalConfig.path_workspace_folder()
        placements: dict = {}
        problems: list = []
        for item in getattr(plan, "rebinds", None) or ():
            record = dict(item or {})
            family = str(record.get("family") or record.get("draw_ib") or "").strip()
            if not family or family in placements:
                continue
            info = himi_runtime.load_family_placement(
                workspace_root, str(record.get("unique_str") or "")
            )
            if not info.get("ok"):
                problems.append(f"{family}: {info.get('note')}")
                continue
            placements[family] = list(info["placement"])
        if problems and not placements:
            # 一个都没拿到 ⇒ 退化成"今天的行为"，只大声说一句（不 Fatal）。
            self._himi_placements_skipped = problems
            self._himi_placements_problems = []
            self._himi_placements_cache = {}
            self._himi_notify(
                f"族级校准（t39）跳过：{len(problems)} 个族都取不到 palette 缓存 ⇒ "
                "本次导出**两侧都不校准**（= 与今天逐字节等价的行为；产物仍可用，只是"
                "跨族借权重仍按「借出族的摆放」落位）。第一条原因：" + problems[0],
                warning=True,
            )
            return {}
        self._himi_placements_skipped = []
        self._himi_placements_problems = problems
        self._himi_placements_cache = placements if not problems else {}
        if problems:
            print(f"{_LOG_PREFIX} 族级校准：{len(problems)} 个族的摆放不可得 ⇒ 本次导出不做校准")
        return self._himi_placements_cache

    def _himi_host_family(self, plan) -> dict:
        """【t39-X】宿主族 = **合并几何整块落在哪一族**（需求②：由代码判定，绝不硬编码）。

        判据（唯一）：Position 缓冲字节数最大的那个族。合并骨架的顶点全部落在宿主族的
        ``<draw_ib>-Position.buf`` 里（``generate_buffer_files`` 按 ``drawib_model.draw_ib`` 落盘），
        其余族只剩骨架占位行 ⇒ 字节数差距是量级级的（实测 1366904 B vs 132~396 B）。
        并列时取字典序第一个（``himi_family_x.pick_host_family``）。

        证据优先用**本次导出**的内存缓冲（``drawib_model.category_buffer_dict`` 在
        ``DrawIBModel.__post_init__`` 里就装配好了，早于任何写盘）；内存里没有任何 Position 时
        （离线/纯 ini 重建）退回**产物 mod 的 Meshes 目录**。两条路都只认「参与合并的族」
        （``himi_family_range_items`` 的族集），避免把无关族算进来。
        """
        # 【t39-X】延迟 import：`tests/test_himi_stub_objects.py` 等 fake 包前言用 `__path__=[]` 的替身包
        # 按文件路径加载本模块，模块级 import 会让那些既有测试在**收集期**就 ImportError。
        from ...common import himi_family_x

        allowed = set()
        for family, _info in (self.himi_family_range_items() or []):
            allowed.add(str(family))
        source = "本次导出的内存 Position 缓冲"
        sizes = {}
        for drawib_model in (getattr(self, "drawib_model_list", None) or []):
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            if not family or (allowed and family not in allowed):
                continue
            buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
            size = _himi_buffer_byte_size(buffers.get("Position"))
            if size > sizes.get(family, 0):
                sizes[family] = size
        if not any(size > 0 for size in sizes.values()):
            mod_root = himi_family_x.resolve_mod_root(GlobalConfig.path_workspace_folder())
            sizes = {}
            for family, size in (himi_family_x.position_buffer_sizes(mod_root) or {}).items():
                if allowed and str(family) not in allowed:
                    continue
                sizes[str(family)] = int(size or 0)
            source = f"产物 mod 的 Meshes 目录（{mod_root}）"
        picked = himi_family_x.pick_host_family(sizes)
        if not picked.get("ok"):
            return {"ok": False, "family": "", "size": 0, "source": source,
                    "note": picked.get("note") or "没有任何族的 Position 缓冲可用于判定宿主族"}
        return {"ok": True, "family": str(picked.get("family") or ""),
                "size": int(sizes.get(str(picked.get("family") or ""), 0) or 0), "source": source,
                "note": picked.get("note") or ""}

    def _himi_family_x_none(self, reason: str) -> dict:
        """【t39-X】「一个族都没有 X_f」= 第三态：**不校准 + 大声警告**，绝不中止。

        老工作空间没有抓帧、宿主族判不出来、抓帧里没有任何一族有合法对象矩阵 —— 这些都算
        「全无」。CS 读到全零 X ⇒ 跳过左乘 ⇒ 与旧行为**逐字节等价**（产物仍可用，只是各族
        仍按宿主族摆放，实测偏差 1.0~3.8 游戏单位）。
        """
        self._himi_family_x_skipped = [str(reason or "没有任何一族能给出合法 X_f")]
        self._himi_family_x_problems = []
        self._himi_family_x_cache = {}
        self._himi_notify(
            "族级物体矩阵（t39-X）：" + str(reason or "没有任何一族能给出合法 X_f")
            + " ⇒ 本次导出**不做 X_f 校准**（CS 里 X 全零 ⇒ 跳过左乘，与旧行为逐字节等价）；"
            "产物仍可用，只是各族仍按宿主族摆放。",
            warning=True,
        )
        return {}

    def _himi_family_x(self) -> dict:
        """【t39-X】``{family: 12 float}``：X_f = M_base⁻¹ · M_f（抓帧 vs-cb3 的对象矩阵）。

        三态（与 ``_himi_family_placements`` 同款）：
          * **全部**参与族都有合法 X_f ⇒ 返回全部（= 校准）；
          * **部分**缺失/不合法（0 < 有 X 的族 < 参与族）⇒ ``_himi_family_x_problems`` 非空、
            缓存清空（契约钩子据此 Fatal 并**指名缺哪族、为什么**）；
          * **一个都没有**（老工作空间没有抓帧 / 宿主族判不出来）⇒ ``_himi_family_x_skipped``
            非空 + 大声警告，**不校准**。
        """
        from ...common import himi_family_x

        cached = getattr(self, "_himi_family_x_cache", None)
        if cached is not None:
            return cached
        self._himi_family_x_cache = {}
        self._himi_family_x_problems = []
        self._himi_family_x_skipped = []
        self._himi_family_x_record = {}
        if not bool(GlobalProterties.import_merged_vgmap()):
            return {}
        plan = self.himi_merged_runtime_plan()
        if not getattr(plan, "has_rebinds", False):
            return {}
        allowed = set()
        for family, _info in (self.himi_family_range_items() or []):
            allowed.add(str(family))
        if not allowed:
            return {}
        host = self._himi_host_family(plan)
        if not host.get("ok"):
            return self._himi_family_x_none("宿主族判定失败：" + str(host.get("note") or ""))
        host_family = str(host.get("family") or "")
        if host_family not in allowed:
            return self._himi_family_x_none(
                f"宿主族 {host_family} 不在本次合并的族里（{len(allowed)} 族）"
            )
        participants = himi_family_x.participants_from_plan(plan)
        family_bares = {}
        for family in sorted(allowed):
            bare = str(participants.get(family) or "").strip()
            if bare:
                family_bares[family] = bare
        if not family_bares:
            return self._himi_family_x_none("没有任何参与族能给出部件名")
        record = himi_family_x.compute_family_x(
            GlobalConfig.path_workspace_folder(),
            host_family=host_family,
            family_bares=family_bares,
            host_evidence=str(host.get("note") or ""),
        )
        self._himi_family_x_record = record or {}
        problems = list((record or {}).get("problems") or [])
        families = dict((record or {}).get("x") or {})
        if not families:
            # 【一个都没有】= 第三态：不校准 + 大声警告（老工作空间没有抓帧就是这条路）。
            return self._himi_family_x_none(
                "；".join(problems[:4]) if problems else "抓帧里没有任何一族有合法对象矩阵"
            )
        if problems:
            # 【部分】= 半校准比不校准更坏 ⇒ 契约钩子据此 Fatal 并指名缺哪族、为什么。
            self._himi_family_x_problems = problems
            return {}
        missing = sorted(set(allowed) - set(families))
        if missing:
            self._himi_family_x_problems = [
                f"{family}: 没有可用的 X_f（抓帧 vs-cb3 里没有合法对象矩阵）" for family in missing
            ]
            return {}
        self._himi_family_x_cache = families
        print(
            f"{_LOG_PREFIX} 族级物体矩阵：{len(families)} 族"
            f"（宿主 {host_family}；来源 {(record or {}).get('source_frame') or '抓帧'}）"
        )
        return self._himi_family_x_cache
    def _himi_publish_family_x_record(self) -> dict:
        """【t39-X】把 X_f 的审计记录写进 ``Config/HIMIMergedSkeleton.json`` 顶层 ``FamilyX``。

        纯数据层记录（来源帧目录名、每族所用 draw/来源文件、对偶命中数与最大残差、是否单位阵），
        供审计；写不进去只警告（绝不让记录失败拖垮导出）。
        """
        from ...common import himi_family_x

        record = getattr(self, "_himi_family_x_record", None) or {}
        if not record:
            return {"ok": False, "path": "", "note": "没有 X_f 记录（本次导出没有做族级物体矩阵）"}
        host = self._himi_host_family(self.himi_merged_runtime_plan())
        audit = himi_family_x.family_x_audit_record(
            record,
            host_evidence=str(host.get("note") or ""),
            mod_root=himi_family_x.resolve_mod_root(GlobalConfig.path_workspace_folder()),
        )
        path = os.path.join(
            GlobalConfig.path_workspace_folder(), "Config", HIMI_MERGED_SKELETON_METADATA_FILE
        )
        result = himi_family_x.write_family_x_record(path, audit)
        if not result.get("ok"):
            self._himi_notify(
                "族级物体矩阵记录写入失败：" + str(result.get("note") or ""), warning=True
            )
        return result

    def _himi_vertex_family_index(self) -> dict:
        """``{family: set((x,y,z) 量化)}``：逐行判「**顶点自己**属于哪个族」（t41/T40-F2）。

        为什么不能只看 Blend：Blend 记的是**骨头槽位**（骨头族），而顶点族是"这个顶点原本
        来自哪个族"—— 合并网格整个落在宿主族的缓冲里，这个信息只存在于各族**源绑定坐标**里。
        t40 实测：顶点族 == 骨头族 30355 行、跨族 699 行、源坐标匹配失败 395 行（1.27%）。
        **按族分桶**返回：查询时先查骨头族（t40 的归属口径），再按族次序找；都找不到 ⇒ 调用方
        退回骨头族（= 与不校准等价，安全）。
        """
        cached = getattr(self, "_himi_vertex_family_cache", None)
        if cached is not None:
            return cached
        plan = self.himi_merged_runtime_plan()
        members = []
        for item in getattr(plan, "rebinds", None) or ():
            record = dict(item or {})
            family = str(record.get("family") or record.get("draw_ib") or "").strip()
            unique_str = str(record.get("unique_str") or "")
            if family and unique_str and (family, unique_str) not in members:
                members.append((family, unique_str))
        info = himi_runtime.load_family_position_index(
            GlobalConfig.path_workspace_folder(), members
        )
        if not info.get("ok"):
            self._himi_notify(
                "族级校准（t41）：读不到各族的**源绑定坐标** ⇒ 逐行归属退回「骨头族」"
                f"（= 不校准等价；{info.get('note')}）",
                warning=True,
            )
        elif info.get("missing"):
            self._himi_notify(
                f"族级校准（t41）：{info.get('note')}；缺的族按骨头族回退",
                warning=True,
            )
        self._himi_vertex_family_cache = dict(info or {})
        return self._himi_vertex_family_cache

    def _himi_bone_family_spans(self, plan) -> list:
        """``[(base, end, family), …]``：槽位 → 族的**权威**查表（与 palette 侧同一张
        ``plan.family_ranges``，即 ``compute_family_ranges`` 的产物）。

        t40/T40-F2 的教训：顶点侧**不能**按 ``drawib_model.draw_ib`` 选 Q —— 合并网格的
        31054 行**全在宿主族的 Position 缓冲**里，按 DrawIB 取就等于"整块乘 Q_host⁻¹"，
        而 palette 侧是按**骨头族**复合的 ⇒ 26477 行「顶点族 == 骨头族」的正常同族顶点
        被误位移（实测 max 3.937792）。正确口径是**逐行按该行骨头所属族**取 Q。
        """
        spans = []
        for family, info in (getattr(plan, "family_ranges", None) or {}).items():
            info = dict(info or {})
            base = int(info.get("base", 0) or 0)
            slots = int(info.get("slots", 0) or 0)
            if slots > 0:
                spans.append((base, base + slots, str(family)))
        spans.sort()
        return spans

    @staticmethod
    def _himi_row_bone_families(blend_bytes: bytes, spans: list, row_floats: int) -> tuple:
        """逐行算「该行骨头所属族」：``(families, stats)``。

        * ``families[i]`` = 第 ``i`` 行的族名（``""`` = 没有活动通道 / 无法归属）；
        * 活动通道 = ``weight > 0``（与 deform VS 的加权求和同口径）；
        * 一行里出现**多个族** ⇒ 记 ``mixed`` 并按**权重最大**的通道归属（实测本角色
          **0 行混族** ⇒ 良定义；仍留计数，供硬门盯住）。
        """
        import numpy

        families: list = []
        stats = {"rows": 0, "mixed": 0, "unweighted": 0, "unknown_slot": 0}
        if not blend_bytes:
            return families, stats
        if len(blend_bytes) % 32:
            stats["blend_misaligned"] = len(blend_bytes) % 32
            return families, stats
        weights = numpy.frombuffer(blend_bytes, dtype="<f4").reshape(-1, 8)[:, 0:4]
        indices = numpy.frombuffer(blend_bytes, dtype="<i4").reshape(-1, 8)[:, 4:8]
        rows = min(int(weights.shape[0]), int(row_floats))
        for row in range(rows):
            active = [(float(w), int(v)) for w, v in zip(weights[row], indices[row]) if w > 0.0]
            if not active:
                stats["unweighted"] += 1
                families.append("")
                continue
            seen = {}
            for weight, slot in active:
                family = ""
                for base, end, name in spans:
                    if base <= slot < end:
                        family = name
                        break
                if not family:
                    stats["unknown_slot"] += 1
                    continue
                seen[family] = seen.get(family, 0.0) + weight
            if not seen:
                families.append("")
                continue
            if len(seen) > 1:
                stats["mixed"] += 1
            families.append(max(seen.items(), key=lambda item: item[1])[0])
        stats["rows"] = len(families)
        return families, stats

    def _himi_calibrate_bind_positions(self) -> dict:
        """【t39 顶点侧 + t41/T40-F2 逐行】``v'_i = Q_{fam(bones_i)}⁻¹ · v_i``（原地改缓冲）。

        为什么要做：合并骨架把五族的 palette 简单拼在一起，但每族的 palette 里**带着该族自己的
        摆放**（第 0 槽就是族级摆放 ``Q_f``）⇒ 跨族借权重时（上半身的顶点用下半身的骨）会按
        "别人的摆放"落位。顶点侧先做 ``Q⁻¹``、palette 侧再写 ``P∘Q``，**两侧同一个 Q**：
        同族结果**逐值不变**（``P·Q·Q⁻¹·v = P·v``），跨族借用则被搬进借出族的坐标系。

        **逐行选 Q（T40-F2）**：Q 取该行**骨头所属族**的，而不是该 DrawIB 的 —— 合并网格整个
        落在宿主族的 Position 缓冲里，按 DrawIB 取会让同族行也被位移。

        **【t24】顶点族认领的保守语义**：认领（nearest_family）判不出唯一几何族时保持**骨头族**
        —— 对骨头族行这是同族校准，`P·Q⁻¹·Q·v = P·v` ⇒ **变形结果逐值不变**（等价"不校准"），
        绝不乘别族 Q。94 行焊接点污染（报告 26）的病根是并列阈值 1e-12 比实际胜负差（~1e-8）
        小 4 个数量级，保守分支永不触发；t24 把窗口放宽到 1e-6（量纲依据见
        `himi_merged_runtime.FAMILY_TIE_WINDOW`）并让法线/切线作为唯一命中时的第二证据。

        返回 ``{"ok","families","rows","skipped","per_family","mixed","unweighted","message"}``；
        **不抛**（异常由上层硬门处理）。
        """
        placements = self._himi_family_placements()
        plan = self.himi_merged_runtime_plan()
        if not placements:
            if not getattr(plan, "has_rebinds", False):
                # 非合并路径：产物与改造前逐字节相同（本任务不碰）。
                return {"ok": True, "families": 0, "rows": 0, "skipped": 0,
                        "per_family": {}, "mixed": 0, "unweighted": 0,
                        "message": "非合并路径：不做族级校准"}
            return {
                "ok": False, "families": 0, "rows": 0, "skipped": 0,
                "per_family": {}, "mixed": 0, "unweighted": 0,
                "message": "族级摆放不可得 ⇒ 顶点侧未校准（导出侧硬门会 Fatal）",
            }
        # 【t44 共同空间取反修复】顶点侧用 **Q_f 本身**（不是它的逆）：把本族绑定位置搬进
        # 「提取帧世界」这个共同空间；palette 侧再写 `P_f[b] · Q_f⁻¹`（见 CS 生成器与 128 B 载荷）。
        # 同族行 `P·Q⁻¹·(Q·v) = P·v` 逐值不变；跨族行 `P_g·Q_g⁻¹·(Q_f·v_f)` —— 顶点先被搬进共同
        # 空间、再被**借出族**的 palette 映到当前世界（这才是"借别人的骨"的正确语义）。
        factors = {}
        for family, matrix in placements.items():
            try:
                himi_runtime.mat3x4_inverse(matrix)      # 仅做可逆性校验（顶点侧不用它）
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "families": 0, "rows": 0, "skipped": 0,
                        "per_family": {}, "mixed": 0, "unweighted": 0,
                        "message": f"{family} 的族级摆放不可逆：{exc}"}
            factors[family] = list(matrix)
        spans = self._himi_bone_family_spans(plan)
        if not spans:
            return {"ok": False, "families": 0, "rows": 0, "skipped": 0,
                    "per_family": {}, "mixed": 0, "unweighted": 0,
                    "message": "plan.family_ranges 为空 ⇒ 无法逐行归属骨头族（拒绝按 DrawIB 猜）"}
        rows = 0
        calibrated_families = 0
        skipped = 0
        debug_skip = ""
        mixed_total = 0
        unweighted_total = 0
        cross_total = 0
        claimed_total = 0
        label_flips = 0
        cross_flips = 0
        ambiguous_total = 0
        cross_shift = 0.0
        per_family: dict = {}
        vertex_index_enabled = True
        import numpy

        for drawib_model in self.drawib_model_list or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
            position = buffers.get("Position")
            if position is None or getattr(position, "dtype", None) is None:
                skipped += 1
                if not debug_skip:
                    debug_skip = "族 %s：buffers=%s；Position 类型=%s" % (
                        family, sorted(buffers)[:8], type(position).__name__,
                    )
                continue
            # 缓冲是**原始字节**（numpy uint8 一维，`.tofile()` 直接落盘）；44 B/行 = P12 N12 C4 TA16
            # ⇒ 每行 **11 个 little-endian float32**，前 3 个是 POSITION、3..5 NORMAL、
            # 6 COLOR、7..10 TANGENT(含 w)。**按字节解释**而不是假设 dtype（不然会把字节
            # 当 float 读 ⇒ 写出天文数字，实测踩过）。
            block = numpy.asarray(position)
            floats = numpy.frombuffer(block.tobytes(), dtype="<f4").copy()
            if floats.size % 11:
                skipped += 1
                if not debug_skip:
                    debug_skip = "族 %s：Position 字节 %d 不是 44 的整数倍（%s）" % (
                        family, block.nbytes, getattr(block, "shape", None),
                    )
                continue
            view2d = floats.reshape(-1, 11)
            blend = buffers.get("Blend")
            blend_bytes = numpy.asarray(blend).tobytes() if blend is not None else b""
            row_families, stats = self._himi_row_bone_families(
                blend_bytes, spans, int(view2d.shape[0])
            )
            # 【t41 / T40-F2】逐行改用**顶点自己**的族：先按源绑定坐标认领，认不到再退回骨头族。
            # 为什么不是「按骨头族」（任务书初稿的字面口径）：palette 侧正是按骨头族复合
            # （`P_g∘Q_g`），两边同族 ⇒ `(P_g∘Q_g)·(Q_g⁻¹v) = P_g·v` **恒等于不校准** ⇒ 整个
            # 族级校准会退化成空操作。而「顶点自己族」这一侧才能让跨族借用真正被搬运：
            # `(P_g∘Q_g)·(Q_f⁻¹v)`，f = 顶点族、g = 骨头族（同族行 f==g ⇒ 依旧逐值不变）。
            vertex_index = self._himi_vertex_family_index() if vertex_index_enabled else {}
            family_order = [name for _base, _end, name in spans]
            cross_rows = 0
            claimed = 0
            for row_index, bone_family in enumerate(row_families):
                if not bone_family:
                    continue
                point = (float(view2d[row_index, 0]), float(view2d[row_index, 1]),
                         float(view2d[row_index, 2]))
                # 【t43 / T42-F2】**容差内最近邻**（不再是精确量化键 round(x*1e5)）：
                # 精确键对量化边界敏感 —— 两次独立导出的逐族行数会漂 ±4~5；漂移若落在跨族行
                # 上会换一个 Q（最多 ~3.7 单位位移）。容差 1e-4（依据见 runtime 的
                # `load_family_position_index` docstring：float32 + 重算噪声 ≲1e-5，几何尺度 1.81）。
                # 并列时优先骨头族（保守：判成同族 ⇒ 该行不动），再按族次序。
                # 【t24】法线/切线随行入判据：焊接点的两族拷贝只差 ~1e-8（float32 重舍入），
                # 位置胜负是掷硬币（94 行污染的病根，见报告 26 §5.1）⇒ 属性是第二证据；
                # 判定顺序与保守语义都在 nearest_family 内（并列 ⇒ prefer 骨头族；
                # 属性只在 prefer 没投票时做「唯一命中」改判；判不出 ⇒ 严格最近）。
                row_normal = _finite_vec3(view2d[row_index, 3], view2d[row_index, 4],
                                          view2d[row_index, 5])
                row_tangent = _finite_vec3(view2d[row_index, 7], view2d[row_index, 8],
                                           view2d[row_index, 9])
                hit = himi_runtime.nearest_family(
                    vertex_index, point, family_order=family_order, prefer=bone_family,
                    normal=row_normal, tangent=row_tangent,
                )
                vertex_family = str(hit.get("family") or "")
                if hit.get("ambiguous"):
                    ambiguous_total += 1
                exact_family = himi_runtime.exact_key_family(vertex_index, point)
                if exact_family != vertex_family:
                    label_flips += 1
                    if (bool(exact_family) and exact_family != bone_family) != (
                        bool(vertex_family) and vertex_family != bone_family
                    ):
                        cross_flips += 1
                if vertex_family:
                    claimed += 1
                    if vertex_family != bone_family:
                        cross_rows += 1
                        if cross_shift is not None:
                            new_offset = himi_runtime.mat3x4_apply(
                                factors.get(vertex_family, factors.get(bone_family)), point
                            )
                            old_offset = point        # 旧口径若判成骨头族 ⇒ 顶点侧不动
                            cross_shift = max(
                                cross_shift,
                                max(abs(new_offset[i] - old_offset[i]) for i in range(3)),
                            )
                else:
                    vertex_family = bone_family          # 安全回退：与骨头族同族 ⇒ 不校准等价
                row_families[row_index] = vertex_family
            stats["claimed"] = claimed
            stats["cross"] = cross_rows
            cross_total += cross_rows
            claimed_total += claimed
            mixed_total += int(stats.get("mixed", 0))
            unweighted_total += int(stats.get("unweighted", 0))
            if len(row_families) != int(view2d.shape[0]):
                # Blend 行数对不上 ⇒ 逐行归属不可信：**拒绝**按整块校准（宁可不校，也不误校）。
                skipped += 1
                if not debug_skip:
                    debug_skip = "族 %s：Blend 行数 %d ≠ Position 行数 %d" % (
                        family, len(row_families), int(view2d.shape[0]),
                    )
                continue
            # 按族分组，逐组施加该族的 **Q_f**（向量化）—— 搬进共同空间（提取帧世界）
            used = 0
            for row_family in sorted({name for name in row_families if name}):
                factor = factors.get(row_family)
                if factor is None:
                    continue
                selector = numpy.array([name == row_family for name in row_families])
                row_indices = numpy.nonzero(selector)[0]
                # ⚠ 花式索引取出来的是**副本** ⇒ 必须显式写回（`group = view2d[selector]` 直接改
                # 不会落回缓冲；实测踩过：行数报告"已校准"而产物逐字节没变）。
                group = view2d[row_indices].copy()
                if not group.size:
                    continue
                a = [[factor[0], factor[1], factor[2]],
                     [factor[4], factor[5], factor[6]],
                     [factor[8], factor[9], factor[10]]]
                t = (factor[3], factor[7], factor[11])
                for columns, translate in (
                    ((0, 1, 2), True), ((3, 4, 5), False), ((7, 8, 9), False)
                ):
                    x = group[:, columns[0]].astype("float64")
                    y = group[:, columns[1]].astype("float64")
                    z = group[:, columns[2]].astype("float64")
                    group[:, columns[0]] = (a[0][0] * x + a[0][1] * y + a[0][2] * z
                                            + (t[0] if translate else 0.0))
                    group[:, columns[1]] = (a[1][0] * x + a[1][1] * y + a[1][2] * z
                                            + (t[1] if translate else 0.0))
                    group[:, columns[2]] = (a[2][0] * x + a[2][1] * y + a[2][2] * z
                                            + (t[2] if translate else 0.0))
                count = int(group.shape[0])
                view2d[row_indices] = group          # 写回（副本 → 原缓冲）
                per_family[row_family] = per_family.get(row_family, 0) + count
                used += count
            rows += used
            calibrated_families = len(per_family)
            # 把变换后的字节**写回原缓冲**（原缓冲可能是 uint8 的一维原始字节，也可能是
            # (rows, 11) 的 float32）⇒ 按**字节**写，形状无关。
            target = block.reshape(-1)
            if target.dtype != numpy.uint8:
                target = target.view(numpy.uint8)
            target[:] = numpy.frombuffer(view2d.tobytes(), dtype=numpy.uint8)
        result = {
            "ok": rows > 0,
            "families": calibrated_families,
            "rows": rows,
            "skipped": skipped,
            "per_family": dict(per_family),
            "mixed": mixed_total,
            "unweighted": unweighted_total,
            "cross": cross_total,
            "claimed": claimed_total,
            "label_flips": label_flips,
            "cross_flips": cross_flips,
            "ambiguous": ambiguous_total,
            "cross_max_shift": cross_shift,
            "message": (
                f"族级校准（顶点侧·逐行）：{rows} 行已按**该行顶点所属族**乘 Q（共同空间）；"
                f"逐族行数 {dict(sorted(per_family.items()))}；跨族借用 {cross_total} 行、"
                f"源坐标认领 {claimed_total} 行、标签翻转 {label_flips}（跨族翻转 {cross_flips}）、"
                f"容差内多族 {ambiguous_total}、混族行 {mixed_total}、"
                f"无活动通道行 {unweighted_total}"
                + (f"；跨族行的顶点侧最大位移 {cross_shift:.4f}" if cross_total else "")
                + (f"；{skipped} 个 drawib 未涉及" if skipped else "")
                + (f"（{debug_skip}）" if debug_skip else "")
            ),
        }
        self._himi_calibration_result = result
        print(f"{_LOG_PREFIX} {result['message']}")
        return result

    def _himi_write_blend_origin(self) -> dict:
        """【t49 / t47 方案 A+C】旁写 ``Meshes/<fam>-BlendOrigin.buf`` + 越界引用计数。

        方案 A（旁文件）：8 B/顶点 = ``[local_slot(u32), flags(u32)]``，行序与 ``<fam>-Blend.buf``
        逐行对齐；``flags.bit0`` = 该顶点**存在越界引用**（≥1 个活动通道的全局索引不在本族区段）。
        方案 C（计数日志）：每个族一行 ``family=<fam> vertices=<n> out_of_range_refs=<N> (<pct>%)``
        + 一行汇总 ``out_of_range_refs=<N> (<pct>%)``；``N > 0`` 时**告警**（控制台 + 产物 ini 注释块）。

        为什么需要它（t47）：产物里的**全局索引**是被写坏的（源数据 12/12 组件越界 0，而产物
        `2c706fbd` 有 449 行、取值只有 275、2.21%）⇒ 让导出自己把「本族局部槽号」旁写一份 +
        把越界条数报出来，"导出到底把哪些顶点写越界了"就不再需要逆向猜。

        **不改变任何既有产物的语义**：只新增一个文件 + 一行日志/注释。
        """
        out_of_range_total = 0
        vertices_total = 0
        per_family: list = []
        written: list = []
        problems: list = []
        folder = GlobalConfig.path_generatemod_buffer_folder()
        spans = self._himi_bone_family_spans(self.himi_merged_runtime_plan())
        range_of = {name: (base, end - base) for base, end, name in spans}
        for drawib_model in self.drawib_model_list or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
            blend = buffers.get("Blend")
            if blend is None:
                continue
            info = range_of.get(family)
            if info is None:
                problems.append(f"{family}: 不在 plan.family_ranges 里 ⇒ 无法判越界")
                continue
            base, span = info
            try:
                import numpy

                raw = numpy.asarray(blend).tobytes()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{family}: 读 Blend 缓冲失败（{exc}）")
                continue
            result = himi_runtime.blend_origin_rows(raw, base=base, span=span)
            if not result.get("ok"):
                problems.append(f"{family}: {result.get('message')}")
                continue
            gate = himi_runtime.blend_origin_problems(
                result["rows"], span=span, vertices=result["vertices"]
            )
            problems.extend(f"{family}: {item}" for item in gate)
            try:
                path = os.path.join(folder, himi_runtime.blend_origin_basename(family))
                with open(path, "wb") as handle:
                    handle.write(himi_runtime.blend_origin_payload(result["rows"]))
                written.append(path)
            except OSError as exc:
                problems.append(f"{family}: 写 {family}-BlendOrigin.buf 失败（{exc}）")
            vertices = int(result["vertices"])
            out_of_range_total += int(result["out_of_range"])
            vertices_total += vertices
            pct = (100.0 * result["out_of_range"] / vertices) if vertices else 0.0
            per_family.append({
                "family": family, "vertices": vertices,
                "out_of_range": int(result["out_of_range"]),
                "pct": pct,
                "all_channels_out": int(result["all_channels_out"]),
                "local_unavailable": int(result["local_unavailable"]),
                "path": path if written and written[-1].startswith(folder) else "",
            })
            print(f"{_LOG_PREFIX} family={family} vertices={vertices} "
                  f"out_of_range_refs={result['out_of_range']} ({pct:.2f}%)")
        pct_total = (100.0 * out_of_range_total / vertices_total) if vertices_total else 0.0
        print(f"{_LOG_PREFIX} out_of_range_refs={out_of_range_total} ({pct_total:.2f}%)")
        summary = {
            "ok": not problems,
            "written": written,
            "families": per_family,
            "out_of_range_refs": out_of_range_total,
            "vertices": vertices_total,
            "pct": pct_total,
            "problems": problems,
        }
        self._himi_blend_origin_result = summary
        if out_of_range_total:
            notices = getattr(self, "_himi_blend_origin_notices", None)
            if notices is None:
                notices = []
                self._himi_blend_origin_notices = notices
            bad = [item for item in per_family if item["out_of_range"]]
            notices.append(
                "越界引用自检（t49/方案 C）：out_of_range_refs=%d (%.2f%%)；涉及族 %s；"
                "全通道越界行 %d、本族局部槽不可得行 %d —— 源数据越界 0，这些是导出侧写进去的"
                % (out_of_range_total, pct_total,
                   ", ".join("%s:%d" % (item["family"], item["out_of_range"]) for item in bad),
                   sum(item["all_channels_out"] for item in bad),
                   sum(item["local_unavailable"] for item in bad))
            )
            self._himi_notify(notices[-1], warning=True)
        return summary

    def _himi_remap_ordinary_blend_to_local(self) -> dict:
        """【t73·兜底原则】降级/普通导出写**局部编号**：`local = global − family_base`（本族槽）。

        为什么要它：普通模式没有运行时合并骨架，游戏按**部件局部编号**查骨。若这份 mesh 是
        「开关开着」导入的（组名/编号是全局的），降级出来的产物在游戏里会整块不显示 ⇒
        降级必须先把几何的编号搬回局部（用户裁定里的第 3 条）。

        口径与证据（t67 权威表，1163 行，逐行核对过）：

        * **本族槽**（`slot ∈ [base_f, base_f + slots_f)`）⇒ 减基址。`base_f = 0` 的族是恒等；
          `def0c0ae` 那族表里写得很清楚：`local 49 ↔ global 341`（341 − 292 = 49 ✓）；
        * **跨族槽**（槽落在**别族**块里）⇒ **不做减法**（否则 `275 − 273 = 2` 是个错值）：
          表里对 `LOD0.2c706fbd-7737-47199` 的 `275` 明确写「越族：275 ∉ [0,184)（该槽属于别的族）」
          且 `should_local_slot` **为空** ⇒ **无法回映射**（t51 的逐行取源恢复表才是那条路的答案）。
          这里记数 + **大声警告**（控制台 + 产物 ini 注释块），并在诊断里点名；
        * 没有合并计划（开关关闭的普通导出）⇒ `family_ranges` 为空 ⇒ **不做任何改写**（那时 mesh
          的编号本来就是局部的）。

        返回账目：``{"remapped", "cross_family", "skipped"}``。
        """
        plan = self.himi_merged_runtime_plan()
        ranges = dict(getattr(plan, "family_ranges", None) or {})
        ledger = {"remapped": 0, "cross_family": 0, "skipped": not bool(ranges)}
        if not ranges or self._himi_merged_attach_active():
            return ledger
        base_of = {}
        for name, info in ranges.items():
            try:
                base_of[str(name)] = (int(dict(info).get("base", 0) or 0),
                                      int(dict(info).get("slots", 0) or 0))
            except Exception:  # noqa: BLE001
                continue
        if not base_of:
            ledger["skipped"] = True
            return ledger
        try:
            import numpy
            import struct
        except Exception:  # noqa: BLE001
            return ledger
        for drawib_model in self.drawib_model_list or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            blend = (getattr(drawib_model, "category_buffer_dict", None) or {}).get("Blend")
            if blend is None:
                continue
            arr = numpy.asarray(blend)
            raw = arr.tobytes()
            rows = len(raw) // 32
            buf = bytearray(raw)
            for row in range(rows):
                weights = struct.unpack_from("<4f", buf, row * 32)
                indices = list(struct.unpack_from("<4i", buf, row * 32 + 16))
                touched = False
                for channel, (weight, slot) in enumerate(zip(weights, indices)):
                    if weight <= 0.0:
                        continue
                    owner = None
                    for name, (base, slots) in base_of.items():
                        if slots > 0 and base <= slot < base + slots:
                            owner = (name, base)
                            break
                    if owner is None or owner[1] == 0:
                        continue                      # 不属于任何族块 / base=0 ⇒ 恒等
                    if owner[0] == family:            # 本族 ⇒ 减基址
                        indices[channel] = int(slot) - owner[1]
                        ledger["remapped"] += 1
                        touched = True
                    else:                             # 跨族 ⇒ 不能减（t67 表：无法回映射）
                        ledger["cross_family"] += 1
                if touched:
                    struct.pack_into("<4i", buf, row * 32 + 16, *indices)
            try:
                arr.view(numpy.uint8)[:] = numpy.frombuffer(bytes(buf), dtype=numpy.uint8)
            except Exception:  # noqa: BLE001 - 写不回去就保持原样（绝不写坏）
                ledger["remapped"] = 0
                ledger["cross_family"] = 0
                ledger["skipped"] = True
        notices = list(getattr(self, "_himi_ordinary_local_remap_notices", None) or [])
        if ledger["remapped"]:
            text = ("普通模式已把 %d 个本族槽搬回**局部编号**（local = global − family_base，"
                    "t67 权威表口径）" % ledger["remapped"])
            print(f"{_LOG_PREFIX} {text}")
            notices.append(text)
        if ledger["cross_family"]:
            text = ("普通模式**无法回映射** %d 个跨族槽（槽落在别族块里，减基址会得到错值；"
                    "t67 权威表对这类行只写「越族」且 should_local_slot 为空）⇒ 这些行在普通模式里"
                    "没有正确的局部编号（要走合并骨架，或先用 t51 的逐行取源恢复表取源局部槽）"
                    % ledger["cross_family"])
            print(f"{_LOG_PREFIX} 警告 {text}")
            self._himi_notify(text, warning=True)
            notices.append(text)
        self._himi_ordinary_local_remap_notices = notices
        return ledger


    def _himi_scan_bind_positions(self) -> dict:
        """【t74 + t76】H-A 硬门：产物里有没有"位置被写成别行的位置"的行（导出段拉丝的直接成因）。

        判据照搬 t72（`common/himi_merged_runtime.scan_bind_positions`）：逐元素对应 u→v、
        稳健相似拟合、离群行 + 「精确等于另一在用行」⇒ 判红，并指名**部件 + 行号 + 残差**。

        **【t76 更正覆盖范围】** t74 初版在这一步用了 `if not self._himi_merged_attach_active(): return`，
        于是**只在合并骨骼路径**上跑 —— 而 H-A 的载体是 `<fam>-Position.buf` 族池 + per-part IB
        （**两条路径都有**）⇒ 普通导出同样可能带这种错行却没人查。现在**两条路径都跑**：

        * 合并路径：判红 ⇒ `_himi_degrade_to_ordinary(...)`（降级 + 大声警告，**不抛 Fatal**）；
        * 普通路径 / 已降级：判红 ⇒ **大声警告**（控制台 + 产物 ini 注释块）并把账目留下 ——
          这里没有"可降级的目标"（它本来就是兜底），但必须**说出来**而不是静默发出去。
        """
        result = {"ok": True, "parts": [], "problems": [], "skipped": True}
        try:
            parts = []
            for drawib_model in self.drawib_model_list or ():
                family = str(getattr(drawib_model, "draw_ib", "") or "")
                for submesh_model in getattr(drawib_model, "submesh_model_list", None) or ():
                    bare = str(getattr(submesh_model, "blender_obj_name", "")
                               or getattr(submesh_model, "obj_name", "") or "")
                    if bare.startswith("LOD0."):
                        bare = bare[len("LOD0."):]
                    if bare and family:
                        parts.append((bare, family))
            if not parts:
                return result
            result = himi_runtime.scan_bind_positions(
                parts,
                mod_root=GlobalConfig.path_generate_mod_folder(),
                workspace_root=GlobalConfig.path_workspace_folder(),
            )
        except Exception as exc:  # noqa: BLE001 - 检查自身失败不得反过来弄坏导出
            result = {"ok": True, "parts": [], "problems": [], "skipped": True,
                      "error": str(exc)}
        self._himi_bind_position_result = result
        if not result.get("ok"):
            detail = (
                "H-A 自检：产物里有行的位置被写成了**别行的位置**（导出段拉丝的直接成因）："
                + "；".join(list(result.get("problems") or [])[:4])
                + ("；…" if len(result.get("problems") or []) > 4 else "")
            )
            if self._himi_merged_attach_active():
                # 合并路径：按兜底原则降级为普通导出（保留兜底通路 + 大声警告）。
                self._himi_degrade_to_ordinary(detail)
            else:
                # 【t76】普通路径（或已降级）：没有"可降级的目标" ⇒ 只大声警告 + 留账目，
                # 绝不静默发出，也绝不中止（普通导出是兜底）。
                print(f"{_LOG_PREFIX} 警告 {detail}")
                self._himi_notify(detail, warning=True)
                notices = list(getattr(self, "_himi_bind_position_warnings", None) or [])
                notices.append(detail)
                self._himi_bind_position_warnings = notices
        return result

    def himi_bind_position_result(self) -> dict:
        """【t74】H-A 自检账目（供测试/上层读）。"""
        return dict(getattr(self, "_himi_bind_position_result", {}) or {})

    def generate_buffer_files(self, output_folder: str):
        """【t39 顶点侧接点】先做族级校准，再走基类的落盘（唯一改动点）。

        为什么放在这里：`Meshes/<draw_ib>-Position.buf` 就是在本方法里落盘的
        （``ui/universal/drawib_export_base.py::generate_buffer_files`` 的
        ``category_buffer_dict`` 循环），**改在这里就不会动基类**；
        且它发生在任何 ini 写盘之前，与 palette 侧（attach CS 的 128 B 载荷）同源同报。
        """
        self._himi_calibrate_bind_positions()
        # 【t49 / t47 方案 A+C】旁写 <fam>-BlendOrigin.buf + 越界引用计数（只新增文件/日志）
        self._himi_write_blend_origin()
        # 【t73·兜底原则】普通/降级导出 ⇒ 把全局编号搬回**局部编号**（本族减基址；跨族记数 + 警告）
        self._himi_remap_ordinary_blend_to_local()
        written = super().generate_buffer_files(output_folder)
        # 【t74】H-A 自检：产物写完之后查"位置被写成别行的位置"（判红 ⇒ 降级，不 Fatal）
        self._himi_scan_bind_positions()
        return written

    # ------------------------------------------------------------------ #
    # 【t18 / T5-F5】诊断必须落在「用户一定会看到的地方」：产物 ini 的注释块
    # ------------------------------------------------------------------ #

    def himi_deform_structure_result(self) -> dict:
        """【t18/T5-F2】K2 门的账目：``judged/checked/unavailable/unjudgeable/missing_identity``。

        「有没有真的检查过」由它回答（修前该门因计划缺 `unique_str` 而完全空转，
        而日志里「没有那一行」被误当成了检查过的证据）。
        """
        return dict(getattr(self, "_himi_deform_structure_result", {}) or {})

    def himi_diagnostic_lines(self) -> list:
        """本次导出的诊断行（**正例也留痕**）：F6 基址表 / K2 变形 pass 结构 / K4 触发键。

        为什么要有「正例」行：t5 的复核正是靠「日志里没有那一行」推断「检查过了」——
        而当时那一行**根本不会出现**（门在空转）。⇒ 现在无论有没有问题都留下账目，
        「没有报错」不再能被当作「检查过了」的证据。
        """
        lines: list = []
        for item in getattr(self, "_himi_base_table_notices", ()) or ():
            lines.append("警告（F6 基址表）：" + str(item))
        for item in getattr(self, "_himi_key_notices", ()) or ():
            lines.append("警告（K4 触发键）：" + str(item))
        # 【t49 / 方案 C】越界引用计数也进产物注释块（用户打开 mod 目录就能看到）
        blend_origin = dict(getattr(self, "_himi_blend_origin_result", {}) or {})
        if blend_origin:
            lines.append(
                "out_of_range_refs=%s (%.2f%%)（顶点 %s；逐族：%s）"
                % (blend_origin.get("out_of_range_refs", 0), float(blend_origin.get("pct", 0.0)),
                   blend_origin.get("vertices", 0),
                   ", ".join("%s:%s (%.2f%%)" % (item["family"], item["out_of_range"], item["pct"])
                             for item in (blend_origin.get("families") or ())))
            )
        for item in getattr(self, "_himi_blend_origin_notices", ()) or ():
            lines.append("警告（越界引用）：" + str(item))
        # 【t73·兜底原则】降级事实必须写进产物注释块（第三层诊断）：用户打开 mod 目录就能看到
        # 「本次其实是普通导出、以及为什么」。
        bind = dict(getattr(self, "_himi_bind_position_result", {}) or {})
        if bind and not bind.get("skipped"):
            ledger = dict(bind.get("ledger") or {})
            lines.append(
                "H-A 自检：部件 %d 个；越界/写错 = %s；内点 RMS = %s"
                % (len(bind.get("parts") or ()), "有" if not bind.get("ok") else "无",
                   ", ".join("%s:%s" % (part.get("bare"), (part.get("ledger") or {}).get("rms"))
                             for part in (bind.get("parts") or ())[:5] if not part.get("skipped")))
            )
            for item in (bind.get("problems") or ())[:4]:
                lines.append("警告（H-A 位置写错）：" + str(item))
        if self.himi_degraded_to_ordinary():
            lines.append("本次导出模式：普通导出（合并骨架侧失败后**已降级**，产物不含合并骨架）")
            for item in getattr(self, "_himi_degrade_notices", ()) or ():
                lines.append("警告（已降级）：" + str(item))
            for item in getattr(self, "_himi_ordinary_local_remap_notices", ()) or ():
                lines.append("提示（普通模式编号）：" + str(item))
        result = dict(getattr(self, "_himi_deform_structure_result", {}) or {})
        if result:
            # 【t20 / T19-F1】图标必须跟随**账目判词**：`judged == 0`（无 rebind 记录）时
            # 结果是「未核对（无 rebind 记录）」而不是「通过」—— 空转不得在产物里报成功。
            verdict = str(result.get("verdict") or "")
            if result.get("problems"):
                icon = "警告"
            elif verdict == "通过":
                icon = "通过"
            else:
                icon = "未核对"
            lines.append(f"{icon}（K2 变形 pass 结构）：" + str(result.get("summary") or ""))
            if result.get("problems"):
                for item in result.get("problems") or ():
                    lines.append("  - " + str(item))
        if not lines:
            lines.append("本次导出没有产生任何 HIMI 合并骨架诊断（未走合并路径，或各项均通过）")
        return lines

    def _himi_emit_diagnostic_comments(self, ini_builder: M_IniBuilder) -> None:
        """把诊断写进**产物 ini 的注释块**（一次；`;MARK:CreditInfo` 附近）。

        三个可见层（缺一不可，因为前两层都要用户主动去看）：
        1. Blender 控制台（`_himi_notify` 的 `print`）；
        2. 导出日志（`utils.log_utils.LOG.warning`，收尾会保存到 Text Editor）；
        3. **产物 ini 注释块**（本方法）—— 用户打开 mod 目录就能看到，且随产物长期存在。

        为什么不用 `report({'WARNING'})`：那条通道在导出操作符里
        （`ui/ui_func_export.py::ExportMod`），不在本模块的改动范围内（见报告 §未解决项）；
        注释块与 operator report 是「或」的关系（任务口径允许）。
        """
        if self._himi_diagnostics_emitted:
            return
        # 【t85·唯一谓词】普通导出（开关关闭、未降级）**一个字节都不发**：
        # 这是「关闭该特性 ⇒ 产物不含合并骨架内容」的闸门（层 3 是合并骨架的专属诊断层）。
        if not (self._himi_merged_attach_active() or self.himi_degraded_to_ordinary()):
            self._himi_diagnostics_emitted = True
            print(
                f"{_LOG_PREFIX} 普通导出（未启用合并骨架）：产物 ini 不写合并骨架诊断块"
                "（控制台/导出日志仍有本次导出的提示）"
            )
            return
        self._himi_diagnostics_emitted = True
        section = M_IniSection(M_SectionType.CreditInfo)
        section.append("; ===== HIMI 合并骨架诊断（导出侧自动生成；请勿手改，重新导出会覆盖）=====")
        for line in self.himi_diagnostic_lines():
            section.append("; " + line)
        section.append(
            "; 这些行只作诊断、不改变产物行为；出现 `警告` 时请连同 Blender 控制台/导出日志一起提供。"
        )
        section.new_line()
        ini_builder.append_section(section)

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """基类 VB 资源段 + 角色级合并骨架资源（本文件与 ini 由同一份计划渲染）。"""
        # 【t18 / T5-F5】诊断注释块：只发一次。**t85 起只在合并路径出内容时发**
        # （旧行为「与是否走合并路径无关」⇒ 普通导出的 ini 也带一整块合并骨架诊断，
        # 用户实报「不使用合并骨骼时配置表里面会有合并骨骼的生成」）。
        if self._himi_merged_attach_active() or self.himi_degraded_to_ordinary():
            self._himi_emit_diagnostic_comments(ini_builder)
        super().add_unity_vs_resource_vb_sections(
            ini_builder=ini_builder, drawib_model=drawib_model
        )
        if self._himi_resource_emitted:
            return
        plan = self.himi_merged_runtime_plan()
        if not plan.has_rebinds:
            return
        from ...blueprint.export_helper import BlueprintExportHelper

        folder_name = (
            str(BlueprintExportHelper.get_current_buffer_folder_name() or "")
            or himi_runtime.MERGED_SKELETON_DEFAULT_FOLDER
        )
        self._himi_publish_skeleton(folder_name)
        self._himi_publish_attach_ranges(plan, folder_name)
        lines = plan.resource_ini_lines(folder_name)
        if not lines:
            return
        self._himi_resource_emitted = True
        resource_section = M_IniSection(M_SectionType.ResourceBuffer)
        for line in lines:
            resource_section.append(line)
        resource_section.new_line()
        ini_builder.append_section(resource_section)
        self._himi_emit_family_palette_and_attach_sections(ini_builder, plan, folder_name)

    def _himi_emit_family_palette_and_attach_sections(
        self, ini_builder: M_IniSection, plan, folder_name: str
    ) -> None:
        """【每帧机制 t54 / t75】逐族发射：① 家族副本声明（``Buffer`` + stride 48 + 无 format）
        ② ``[CustomShaderHI3MergedSkeletonAttach_<fam>]`` 段。

        数据源只有一个 = ``plan.family_ranges``（``own_block=False`` 的借位族不发段）——
        与 VB 段的捕获行 / ``run`` 行**同源**，不会出现「声明了副本但没人写」或反之。

        **t75 起是「一个 shader、N 个段」**：段体仍由 ``render_attach_section_lines`` 给
        （形状：section 名 / flags / cs-t0 / cs-u0 / Dispatch / cs-u0 = null），但 ``cs =``
        一行被改写成**同一个** :data:`MERGED_ATTACH_HLSL_PATH`。基址不再进 shader 文件
        （旧口径的逐族 ``#define``）—— 由该 shader 用源槽位数在运行期查表算出来。
        """
        items = self.himi_family_range_items()
        if not items:
            return
        skeleton_name = ""
        decl = plan.resource(str(folder_name or himi_runtime.MERGED_SKELETON_DEFAULT_FOLDER))
        if decl is not None:
            skeleton_name = str(decl.name)
        palette_section = M_IniSection(M_SectionType.ResourceBuffer)
        for family, info in items:
            family_decl = himi_runtime.build_family_palette_decl(family, int(info["slots"]))
            for line in family_decl.ini_lines():
                palette_section.append(line)
        # 【t74】每族 **vs-cb3 的副本**（attach CS 的 `cs-cb1` 源）：资源段只写段名 —— 尺寸与
        # 布局由 `copy vs-cb3` 的源视图决定（本仓 ZZMI 的 `copy vs-cb1` 先例同款，
        # `tests/test_zzmi_merged_skeleton_ini.py` 钉死「不许写 type/stride」）。
        for family, _info in items:
            for line in himi_runtime.cb3_capture_ini_lines(family):
                palette_section.append(line)
        # 【t74】跨族物体矩阵表（`cs-u1`）：3 个 float4 × 族数，运行期由各族自己那段 CS 写入，
        # 宿主族那笔再读回来算 X = M_base⁻¹·M_own。
        object_matrix_name = ""
        try:
            object_decl = himi_runtime.build_object_matrix_decl(str(plan.role_key), len(items))
        except Exception as exc:  # noqa: BLE001 - 声明不合法 ⇒ 不发 u1（CS 退回载荷兜底 X）
            self._himi_notify(
                f"物体矩阵表（cs-u1）声明不合法（{exc}）⇒ 本次不发布该资源，"
                "X_f 退回导出期烘死的载荷兜底。",
                warning=True,
            )
            object_decl = None
        if object_decl is not None:
            object_matrix_name = str(object_decl.name)
            for line in object_decl.ini_lines():
                palette_section.append(line)
        # 【F5】每族的**身份资源**声明（16 字节 = base/span/version/magic）：
        # 它是 base/span 的权威来源，让「本族 span 上界」不再与查表键同源。
        buffer_folder = str(folder_name or himi_runtime.ATTACH_RANGE_DEFAULT_FOLDER)
        for family, info in items:
            for line in himi_runtime.attach_range_ini_lines(
                family,
                f"{buffer_folder}/{himi_runtime.attach_range_basename(family)}",
            ):
                palette_section.append(line)
        palette_section.new_line()
        ini_builder.append_section(palette_section)
        if not skeleton_name:
            self._himi_notify(
                "找不到合并骨架资源名 ⇒ 不发 attach 段（副本仍已声明，但不会有写方）",
                warning=True,
            )
            return
        emitted_paths: list = []
        for family, info in items:
            attach_section = M_IniSection(M_SectionType.MergedSkeleton)
            for line in himi_runtime.render_attach_section_lines(
                family,
                base=int(info["base"]),
                slots=int(info["slots"]),
                skeleton_name=skeleton_name,
                object_matrix_name=object_matrix_name,
            ):
                # 【单文件改写】只动 `cs =` 那一行，其余逐字保留（含 Dispatch / 解绑行）。
                if line.startswith("cs = "):
                    line = "cs = " + MERGED_ATTACH_HLSL_PATH
                    emitted_paths.append(MERGED_ATTACH_HLSL_PATH)
                attach_section.append(line)
            attach_section.new_line()
            ini_builder.append_section(attach_section)
        problems = merged_attach_reference_problems(emitted_paths)
        if problems:
            # 【t73·兜底原则】合并专属自检失败 ⇒ 降级为普通导出（不许一票否决）。
            self._himi_degrade_to_ordinary(
                "合并骨架 attach 段的 `cs =` 引用自检失败：" + "；".join(problems)
            )
        self._himi_attach_cs_paths = emitted_paths

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """基类 IB 段 + 每一笔 ``drawindexed`` **之前**插 ``cb3 捕获`` + ``run = <该族 attach>``。

        为什么每笔前都要跑（ZZMI 纪律，`ui/universal/zzmi.py:129-135`）：骨架由各族的 CS
        逐帧填充，而渲染 draw 可能发生在**本族 deform pass 之外的时刻**（多实例/多 IB 共享
        同一 SO 目标）；CS 幂等 ⇒ 每笔前插一次是安全且必要的（少了它 ⇒ 骨架为空 ⇒ 模型
        整体消失）。

        两行**同源**于 ``himi_runtime.render_attach_render_capture_lines``（不在这里拼文本）：

        * ``ResourceHI3Cb3_<fam> = copy vs-cb3 unless_null`` —— 渲染 draw（indexed）的 vs-cb3
          才是 **160 B / rows 0..7 = 物体矩阵 + 其逆**；deform（non-indexed）draw 的 368 B
          版本 rows 0..7 不是物体矩阵 ⇒ 采集点必须在这里，运行期 X 才会真正生效。
        * ``run = <该族 attach>`` —— 必须紧跟捕获行（否则 CS 读到的是别族/上一帧的副本）。

        只在该 DrawIB 通过 G1（``_himi_rebind_for`` 非 ``None``）且该族**有专属块**
        时插入；否则逐字走基类产物。
        """
        before = list(getattr(ini_builder, "ini_section_list", []) or ())
        super().add_unity_vs_texture_override_ib_sections(
            ini_builder=ini_builder, drawib_model=drawib_model
        )
        rebind = self._himi_rebind_for(drawib_model)
        if rebind is None:
            return
        family = str(rebind.get("family") or drawib_model.draw_ib)
        if self.himi_family_range(family) is None:
            return
        after = list(getattr(ini_builder, "ini_section_list", []) or ())
        new_sections = after[len(before):]
        attach = himi_runtime.attach_section_name(family)
        capture_lines = himi_runtime.render_attach_render_capture_lines(family)
        touched = 0
        for section in new_sections:
            if getattr(section, "SectionType", None) != M_SectionType.TextureOverrideIB:
                continue
            lines = list(getattr(section, "SectionLineList", []) or ())
            out: list = []
            for line in lines:
                if str(line).strip().startswith("drawindexed"):
                    out.extend(capture_lines)
                    touched += 1
                out.append(line)
            if touched:
                section.SectionLineList[:] = out
        if not touched:
            print(
                f"{_LOG_PREFIX} 提示 {drawib_model.draw_ib}: IB 段里没有 drawindexed 行 ⇒ "
                f"未插 `run = {attach}`"
            )

    # ------------------------------------------------------------------ #
    # t75：合并 attach CS 落盘 + `cs =` 引用硬门
    # ------------------------------------------------------------------ #

    def himi_attach_cs_paths(self) -> list:
        """本次导出发射的全部 `cs =` 值（单文件口径下应全部相同）。"""
        return list(self._himi_attach_cs_paths or ())

    def himi_attach_cs_result(self) -> dict:
        """`res/himi_attach.hlsl` 的落盘结果字典。"""
        return dict(self._himi_attach_cs_result or {})

    def _himi_write_attach_cs(self) -> dict:
        """把**单个**合并 attach CS 写进 `<mod>/res/himi_attach.hlsl`（幂等）。

        内容不依赖 `plan`（基址在 shader 内部由源槽位数查表算），所以这里用参考声明渲染，
        只把**合并骨架总槽位数**签出来做越界保护上界；`plan` 存在时用它的真实槽位数。
        """
        plan = self.himi_merged_runtime_plan()
        decl = None
        try:
            slots = int(getattr(plan, "slot_count", 0) or 0)
        except Exception:  # noqa: BLE001 - 任何形态异常都退回参考槽位数
            slots = 0
        if slots <= 0:
            decl = himi_runtime.build_merged_skeleton_decl(
                himi_runtime.REFERENCE_ROLE_KEY, himi_runtime.REFERENCE_SLOT_COUNT
            )
        else:
            decl = himi_runtime.build_merged_skeleton_decl(
                himi_runtime.REFERENCE_ROLE_KEY, slots
            )
        text = merged_attach_hlsl_text(decl)
        problems = merged_attach_hlsl_problems(text)
        if problems:
            # 【t73·兜底原则】合并专属自检失败 ⇒ 降级（本族的 CS 内容不可信，就不发这一段）。
            self._himi_degrade_to_ordinary(
                "合并 attach CS 自检失败（生成器与判据不一致）：" + "；".join(problems)
            )
            return {"ok": False, "path": "", "problems": list(problems)}
        target_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "res")
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, MERGED_ATTACH_HLSL_BASENAME)
        payload = text.encode("utf-8")
        written = True
        try:
            with open(path, "rb") as handle:
                written = handle.read() != payload
        except OSError:
            written = True
        if written:
            with open(path, "wb") as handle:
                handle.write(payload)
        result = {
            "ok": True,
            "path": path,
            "written": written,
            "bytes": len(payload),
            "message": ("已写入" if written else "内容一致、未重写") + f" res/{MERGED_ATTACH_HLSL_BASENAME}",
        }
        self._himi_attach_cs_result = result
        if written:
            print(f"{_LOG_PREFIX} {result['message']} → {path}")
        return result

    def _himi_enforce_attach_cs_references(self, ini_path: str) -> list:
        """落盘 CS 之后，把**产物 ini 文件**读回来跑 `cs =` 硬门；返回问题清单。"""
        try:
            with open(ini_path, encoding="utf-8") as handle:
                ini_text = handle.read()
        except OSError as exc:
            return [f"产物 ini 读不回来（{ini_path}）：{exc}"]
        return verify_cs_references(ini_text, GlobalConfig.path_generate_mod_folder())

    def _himi_raise_for_attach_cs_references(self, ini_path: str) -> None:
        """合并 attach CS 的 `cs =` 引用硬门：**只在合并路径激活时被调用**（t73）。

        调用点已被 ``_himi_merged_attach_active()`` 门住 ⇒ 普通导出看不到它
        （旧行为是在普通路径上也执行 ⇒ 把"整份 ini 里没有任何 `cs =` 引用"
        误判成"缺失引用"而中止，用户实报的第二次误拦）。
        失败通道按【t73·兜底原则】走**降级**，不再是 `Fatal`。

        与 :meth:`_himi_enforce_attach_cs_references` 分开是为了让两态复现可以单独调用
        这两步（问题清单 / 中止行为），而不是靠异常文本猜。
        """
        problems = self._himi_enforce_attach_cs_references(ini_path)
        if not problems:
            return
        # 【t73·兜底原则】合并侧失败 ⇒ 降级为普通导出 + 大声警告（普通导出不可被剥夺）。
        self._himi_degrade_to_ordinary(
            "合并骨架 attach CS 的 `cs =` 引用硬门未通过：" + "；".join(problems)
        )

    def _himi_enforce_attach_range_references(self, ini_path: str) -> list:
        """【F5 硬门】读回产物 ini，核对每个 ``cs-t1`` 身份资源都指向真实文件。"""
        try:
            with open(ini_path, encoding="utf-8") as handle:
                ini_text = handle.read()
        except OSError as exc:
            return [f"产物 ini 读不回来（{ini_path}）：{exc}"]
        return verify_attach_range_references(ini_text, GlobalConfig.path_generate_mod_folder())

    def _himi_raise_for_attach_range_references(self, ini_path: str) -> None:
        problems = self._himi_enforce_attach_range_references(ini_path)
        if not problems:
            return
        # 【t73·兜底原则】合并专属门 ⇒ 降级为普通导出 + 大声警告（不再是 Fatal）。
        self._himi_degrade_to_ordinary(
            "合并骨架 attach 段的 `cs-t1` 身份资源硬门未通过（F5 的「本族 span 上界」"
            "没有身份资源就会退化为恒真判据）：" + "；".join(problems)
        )

    def _himi_enforce_product_ini_sections(self, ini_path: str) -> list:
        """【t39】读回产物 ini，做逐段解析自检（段属性为 0 条 / 悬空属性）。"""
        try:
            with open(ini_path, encoding="utf-8") as handle:
                ini_text = handle.read()
        except OSError as exc:
            return [f"产物 ini 读不回来（{ini_path}）：{exc}"]
        return product_ini_section_problems(ini_text)

    def _himi_raise_for_product_ini_sections(self, ini_path: str) -> None:
        problems = self._himi_enforce_product_ini_sections(ini_path)
        if not problems:
            return
        from ...utils.format_utils import Fatal

        raise Fatal(
            "产物 ini 的逐段解析自检未通过 ⇒ 中止导出（段头位置错了会让整段资源失效）："
            + "；".join(problems[:6])
            + ("；…" if len(problems) > 6 else "")
        )

    def _himi_degrade_to_ordinary(self, reason: str) -> None:
        """【t73·兜底原则】合并骨架侧的任何失败都**不许**中止导出 ⇒ 降级为**普通导出** + 大声警告。

        为什么：合并骨架与普通导出是**两个独立模式**，且**普通导出是兜底、优先级最高** ——
        合并骨架暴毙也必须能出普通模式的产物（用户裁定 2026-09-26）。

        降级的三个动作（缺一不可）：
        1. `self._himi_force_ordinary = True` ⇒ ``_himi_merged_attach_active()`` 变 False，
           合并专属硬门（attach CS 落盘 / `cs =` 引用门 / `cs-t1` 身份资源门）全部**跳过**；
        2. **把计划本身关掉**（`plan.ok=False` / `rebinds=[]`）⇒ 所有以 `plan.ok`、`plan.has_rebinds`、
           `plan.family_ranges` 为门的合并资源与段落**全部退回普通路径**（不会产出半合并的产物）；
        3. **大声警告**：控制台 `print` + `_himi_notify(warning=True)` + 记进
           ``self._himi_degrade_notices`` ⇒ 由 :meth:`himi_diagnostic_lines` 写进**产物 ini 注释块**
           （t18 的三层诊断：控制台 / 日志 / 产物自述）。
        """
        if getattr(self, "_himi_force_ordinary", False) and str(reason) in (
            getattr(self, "_himi_degrade_reasons", None) or []
        ):
            return
        self._himi_force_ordinary = True
        reasons = list(getattr(self, "_himi_degrade_reasons", None) or [])
        reasons.append(str(reason))
        self._himi_degrade_reasons = reasons
        detail = (
            "合并骨架侧失败 ⇒ **已降级为普通导出**（普通导出是兜底，不可被剥夺）：" + str(reason)
        )
        try:
            plan = self.himi_merged_runtime_plan()
        except Exception:  # noqa: BLE001 - 计划不可得本来就走普通路径
            plan = None
        if plan is not None:
            try:
                plan.ok = False
                plan.rebinds = []
                plan.message = detail
                plan.diagnostics.append(detail)
            except Exception:  # noqa: BLE001
                pass
        print(f"{_LOG_PREFIX} 警告 {detail}")
        self._himi_notify(detail, warning=True)
        notices = list(getattr(self, "_himi_degrade_notices", None) or [])
        notices.append(detail)
        self._himi_degrade_notices = notices

    def himi_degraded_to_ordinary(self) -> bool:
        """本次导出是否**已降级为普通导出**（供测试/上层读）。"""
        return bool(getattr(self, "_himi_force_ordinary", False))

    def _himi_purge_merged_only_artifacts(self) -> dict:
        """【t85·残留清理】普通导出时清掉**上一次合并导出**留在产物目录里的合并骨架专属文件。

        为什么必须清（三条依据）：

        1. **不可能改变普通产物的行为**：这些文件只与合并骨架一起才有意义；普通导出的 ini
           对它们**一个引用都没有**（t85 实测：关开关的产物 ini 里 `ResourceHI3MergedSkeleton` /
           `ResourceHI3PaletteFam` / `ResourceHI3AttachRange` / `CustomShaderHI3MergedSkeletonAttach` /
           `himi_attach.hlsl` 命中数**全为 0**，且普通产物 61 个文件里清理名单 0 命中）；
        2. **t73 给用户的判据会失效**：「看产物 `res/` 有没有 `himi_attach.hlsl`：有 = 合并骨架导出、
           没有 = 普通导出」——残留文件会让用户以为还在走合并路径；
        3. 3DMigoto 只加载 ini 里声明过的资源，未引用文件是惰性的，但会随产物一起分发，
           长期留成「看不懂的垃圾」。

        清理范围是**有界名单**（按 `ui/universal/himi.py` 里合并侧的三处写盘点反推）：
        `res/himi_attach.hlsl`（`_himi_write_attach_cs`）、`Meshes/*-AttachRange.buf`
        （`_himi_publish_attach_ranges`）、`Meshes/role_*-MergedSkeleton.buf`（`_himi_publish_skeleton`）、
        `Meshes/*-BlendOrigin.buf`（`_himi_write_blend_origin` 的合并侧旁写）。

        返回账目：``{"removed": [...], "failed": [...]}``。
        """
        import glob as _glob
        root = str(GlobalConfig.path_generate_mod_folder() or "")
        ledger: dict = {"removed": [], "failed": []}
        if not root or not os.path.isdir(root):
            return ledger
        candidates: list = []
        candidates.append(os.path.join(root, "res", MERGED_ATTACH_HLSL_BASENAME))
        meshes = os.path.join(root, "Meshes")
        if os.path.isdir(meshes):
            for pattern in ("*-AttachRange.buf", "*-BlendOrigin.buf", "role_*-MergedSkeleton.buf"):
                candidates.extend(_glob.glob(os.path.join(meshes, pattern)))
        for path in candidates:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    ledger["removed"].append(os.path.relpath(path, root).replace("\\", "/"))
            except OSError as exc:  # noqa: BLE001 - 清理失败不得反过来弄坏导出
                ledger["failed"].append(f"{path}: {exc}")
        if ledger["removed"]:
            print(
                f"{_LOG_PREFIX} 普通导出：已清掉上一次合并导出残留的合并骨架专属文件 "
                f"{len(ledger['removed'])} 个（{', '.join(ledger['removed'][:6])}"
                + ("…" if len(ledger["removed"]) > 6 else "") + "）"
            )
        for item in ledger["failed"]:
            self._himi_notify(f"合并骨架残留清理失败：{item}", warning=True)
        return ledger

    def _himi_merged_attach_active(self) -> bool:
        """【t73】本次导出**是否真的在产出合并骨架**（= attach CS 的"写方"语义存在）。

        用于把「合并骨架专属的硬门」限制在合并骨架路径上：

        * 开关关闭 ⇒ ``plan.ok = False``（`himi_merged_runtime.plan_from_metadata` 里"绝不消费
          合并数据"那条）⇒ **普通导出**：没有合并骨架资源、也没有任何 `cs =` 引用
          ⇒ 那些硬门必须**跳过**（旧行为会把"没有引用"误判成"缺失引用"而中止，用户实报第二次误拦）；
        * 开关打开但计划本身没起来（元数据不全等）⇒ 同样没有写方 ⇒ 也跳过；
        * 开关打开且计划可用 ⇒ **激活**：落 CS + `cs =` 引用硬门 + `cs-t1` 身份资源硬门照旧全部执行。
        """
        try:
            if not bool(GlobalProterties.import_merged_vgmap()):
                return False
        except Exception:  # noqa: BLE001 - 拿不到开关状态一律按"未激活"（安全侧：跳过专属硬门）
            return False
        if bool(getattr(self, "_himi_force_ordinary", False)):
            return False          # 【t73】已降级 ⇒ 合并专属硬门一律不跑
        try:
            plan = self.himi_merged_runtime_plan()
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(plan, "ok", False))

    def generate_unity_vs_config_ini(self):
        """基类 ini 生成 + 【t75 收尾执行点】落 CS + `cs =` 引用硬门。

        **顺序是契约的一部分**（报告 §30 的依赖顺序）：

        1. 基类先把 ini 落到 ``<mod>/<workspace>.ini``；
        2. 本方法随后把**一个** ``res/himi_attach.hlsl`` 写到 ``<mod>/res/``（``mkdir`` +
        幂等写）；
        3. 再把 ini 读回来，逐条核对每个 `cs =` 都能解析到真实文件、且 5 条指向同一路径；
        4. 任一不成立 ⇒ ``Fatal`` 中止导出（**不降级为 warning、不跳过**）。

        **为什么必须先落 CS + 过硬门，才能撤掉骨架资源的 `filename`**：骨架是**活资源**
        （每帧由 attach CS 填充，ini 里不带 ``filename``）。若先撤 ``filename`` 而 CS 文件
        还不存在/不被引用，则从第一帧起就**没有任何写方** —— 骨架留空 ⇒ 顶点位置全零 ⇒ 模型
        爆炸，且**全程不报错**。所以顺序只能是「先有写方，再撤静态来源」。
        """
        super().generate_unity_vs_config_ini()
        ini_path = os.path.join(
            GlobalConfig.path_generate_mod_folder(),
            GlobalConfig.get_workspace_name() + ".ini",
        )
        # 【t73】落 CS + 两个"合并骨架专属"的引用硬门**只在合并骨架路径激活时**执行。
        # 普通导出（开关关闭）根本没有 attach CS、也没有 `cs =`/`cs-t1` 引用 ⇒ 旧行为会把
        # "整份 ini 里没有任何 `cs =` 引用"当成缺失而中止（用户实报的第二次误拦）。
        if self._himi_merged_attach_active():
            self._himi_write_attach_cs()
            self._himi_raise_for_attach_cs_references(ini_path)
            # 【F5】身份资源（`cs-t1`）的文件存在性硬门：缺一族 ⇒ 那族的 span 上界退回冻结表。
            self._himi_raise_for_attach_range_references(ini_path)
        else:
            print(
                f"{_LOG_PREFIX} 普通导出（未启用合并骨架）：跳过 attach CS 落盘与 `cs =`/`cs-t1` "
                "引用硬门 —— 本次不会产出合并骨架资源，也就没有这两类引用"
            )
            self._himi_attach_cs_result = {"skipped": True, "reason": "merged_path_inactive"}
            self._himi_attach_cs_paths = []
            # 【t85】普通导出：清掉上一次合并导出残留的合并骨架专属文件（含 res/himi_attach.hlsl），
            # 否则 t73 给用户的那条判据（「res/ 有 himi_attach.hlsl = 合并骨架导出」）会失效。
            self._himi_merged_residue = self._himi_purge_merged_only_artifacts()
        # 【t39 / §13.17】产物 ini 的逐段解析自检：任何段"属性为 0 条"即 Fatal
        # （防重演 2026-09-26「段头插在别的段属性行之间 ⇒ 骨架段被清空」）。**两种模式都要跑**。
        self._himi_raise_for_product_ini_sections(ini_path)

    # ------------------------------------------------------------------ #
    # 供测试/上层读取的检查点
    # ------------------------------------------------------------------ #

    def himi_merged_contract_decision(self) -> dict:
        return dict(self._himi_contract_decision or {})

    def himi_merged_diagnostics(self) -> str:
        plan = self.himi_merged_runtime_plan()
        return plan.diagnostics_text()

    def himi_merged_publish_result(self) -> dict:
        return dict(self._himi_publish_result or {})

    def himi_blend_namespace_verdicts(self) -> dict:
        """每个 DrawIB 的 Blend 骨骼编号命名空间核对结果（global/local/…）。"""
        return {str(key): dict(value) for key, value in self._himi_blend_verdicts.items()}


__all__ = [
    "ExportHIMI",
    "UNVERIFIED_IN_GAME",
    "DEFORM_GATE_REJECT_PREFIX",
    "MERGED_ATTACH_HLSL_BASENAME",
    "MERGED_ATTACH_HLSL_PATH",
    "MERGED_ATTACH_SLOTS_MACRO",
    "MERGED_ATTACH_BASE_TABLE_LINES",
    "merged_attach_hlsl_text",
    "merged_attach_hlsl_problems",
    "merged_attach_reference_problems",
    "merged_attach_base_table_rows",
    "merged_attach_base_table_problems",
    "verify_cs_references",
    "verify_attach_range_references",
    "deform_replay_block_problems",
    "merged_deform_key_problems",
    "deform_draw_structure_problems",
    "deform_draw_structure_judged",
    "product_ini_section_problems",
]
