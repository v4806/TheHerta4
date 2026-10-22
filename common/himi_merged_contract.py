"""Pure validation for the HIMI merged-skeleton import/export contract.

Sibling of :mod:`common.zzmi_merged_contract`; the HIMI shape is intentionally
the same four levels (``ok`` / ``notice`` / ``warning`` / ``error``) so the two
exporters can share their diagnostic vocabulary, with one HIMI-only addition:
the **deform-family gate** (``HIMIDeformGateSafe``).

Why the extra gate
------------------
A HIMI deform pass writes its stream output straight into the part's render VB,
and one deform pass serves *every* render draw of a single index-buffer family
(measured: 39/39 passes have their SO target consumed by exactly one IB family;
18/18 landed families share one SO target).  Rebinding that pass' ``vs-t0`` to
the merged skeleton therefore switches **the whole family** to global bone
indices.  If only some family members take part in the merge, the others would
read global indices against a skeleton that does not describe them.

So a component whose family is not fully covered must be **rejected**, loudly:
the run continues for the remaining parts, but the user is told which family
blocked it and which member was missing.
"""

from __future__ import annotations

NO_MERGED_DATA_NOTICE = (
    "本次导出为普通导出：工作空间里没有 HIMI 合并骨架数据（VGMap/VGCount）。"
    "若你期望合并骨架，请先在‘使用融合统一顶点组’开启的情况下重新导入一次，再导出。"
)

# 门控 G1 拒绝档的固定前缀；测试与产物注释都靠它做稳定匹配。
DEFORM_GATE_REJECT_PREFIX = "变形家族未完整纳入"

#: 「一个部件都没进合并骨架」时给用户的**正确**处置建议。
#:
#: 2026-09 实报修复：旧文案是「请用‘清除骨骼合并VGMap缓存’后重新导入」——它在绝大多数
#: 情况下是**错的**：真正的原因几乎总是「该 IB 家族没有一起导出」（G1 硬要求：同一个
#: deform pass 的家族成员必须一起导出才能合并），此时清除缓存 + 重新导入会白扔完全
#: 正常的合并数据，而且**依然解决不了问题**。
ALL_REJECTED_ADVICE = (
    "本次导出没有任何部件能进入合并骨架。最常见的原因是「IB 家族不完整」："
    "同一个变形 pass 的家族成员必须**一起导出**才能合并 —— 请把该 IB 家族的全部成员"
    "一起导出后重试。只有当上面的诊断里确实出现「合并数据摘要…不一致」时，才说明这些"
    "部件的合并数据来自另一次导入，此时才需要重新导入；**不要**在没有这条诊断时清除"
    "骨骼合并缓存，那会丢掉完全正常的合并数据。"
)

#: 「部分部件没进合并骨架」时给用户的处置建议（warning 档）。
PARTIAL_ADVICE = (
    "这些部件仍按原版局部索引渲染（外观正常，只是没有合并收益）。若要它们也合并："
    "被「变形家族未完整纳入」拒绝的部件，请把对应 IB 家族的全部成员一起导出；"
    "其余原因见上（仅当诊断里出现「合并数据摘要…不一致」时才需要重新导入）。"
)


def _format_diagnostics(diagnostics, limit: int = 6, skip_prefixes=()) -> str:
    """把 ``plan.diagnostics`` 压成用户可读的一段，供契约提示词使用。

    为什么需要它：真实原因（例如「子集导出被误判 stale」「G1 未放行任何部件」）一直只写在
    ``plan.diagnostics`` 里，而提示词此前只读 ``skip_reasons`` / ``gate_rejected_parts``
    ⇒ 用户看到的是一个**没有任何理由**的中止（2026-09 实报）。这里把诊断接进提示词，
    并过滤掉纯规格行（``资源声明：…`` 不是原因）与已由 ``gate_rejected_parts`` 单独给出的
    G1 拒绝行（避免同一段文字在提示词里出现两遍）、限制条数、逐条截断。
    """
    prefixes = tuple(str(item) for item in (skip_prefixes or ()) if str(item))
    lines: list = []
    for raw in diagnostics or ():
        line = " ".join(str(raw).split())
        if not line or line.startswith("资源声明："):
            continue
        if any(prefix and line.startswith(prefix) for prefix in prefixes):
            continue
        if len(line) > 150:
            line = line[:147] + "…"
        lines.append(line)
    if not lines:
        return ""
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 6
    shown = lines[:cap]
    text = "；".join(shown)
    if len(lines) > len(shown):
        text += f"；…（另有 {len(lines) - len(shown)} 条诊断，见导出日志）"
    return text


def format_skip_reasons(skip_reasons, limit: int = 6) -> str:
    """Format ``{draw_ib: reason}`` diagnostics for a user-facing message."""
    if not skip_reasons:
        return ""
    items = []
    entries = sorted(dict(skip_reasons).items(), key=lambda item: str(item[0]))
    for index, (draw_ib, reason) in enumerate(entries):
        if index >= limit:
            items.append(f"…（另有 {len(entries) - limit} 个部件同类问题）")
            break
        items.append(f"{draw_ib}: {reason}")
    return "；".join(items)


def format_deform_gate_rejections(rejected_parts) -> str:
    """Format the G1 rejection list as ``draw_ib(family; missing=…)`` items.

    ``rejected_parts`` maps ``draw_ib -> {"family": str, "missing": [str, ...]}``
    or ``draw_ib -> reason string``; both shapes are accepted so callers can pass
    either the resolver output or a plain diagnostic.
    """
    if not rejected_parts:
        return ""
    items = []
    for draw_ib, payload in sorted(dict(rejected_parts).items(), key=lambda i: str(i[0])):
        if isinstance(payload, dict):
            family = str(payload.get("family", "") or "")
            missing = sorted(str(m) for m in (payload.get("missing") or []))
            suffix = f"家族={family}" if family else "家族=?"
            if missing:
                suffix += f"；未纳入={', '.join(missing[:4])}"
                if len(missing) > 4:
                    suffix += f"…（共 {len(missing)} 个）"
            items.append(f"{draw_ib}({suffix})")
        else:
            items.append(f"{draw_ib}({payload})")
    return "；".join(items)


def evaluate_merged_skeleton_contract(
    *,
    checkbox_enabled: bool,
    parts_with_data: int,
    component_count: int,
    skip_reasons=None,
    gate_rejected_parts=None,
    diagnostics=None,
) -> dict:
    """Return a contract decision for a HIMI merged-skeleton export.

    ``level == 'error'`` means the caller should abort the export.
    ``gate_rejected_parts`` carries the HIMI-only deform-family gate (G1)
    rejections; they are appended to the warning hint so the user can see which
    family blocked which part without having to read stdout.
    ``diagnostics`` = 导出计划的 ``plan.diagnostics``（真实原因所在）。它们会被接进
    提示词 —— 否则「全部被拒」的错误对用户是**没有理由**的（2026-09 实报）。
    """
    try:
        data_count = int(parts_with_data or 0)
    except (TypeError, ValueError):
        data_count = 0
    try:
        component_count = int(component_count or 0)
    except (TypeError, ValueError):
        component_count = 0
    reasons_text = format_skip_reasons(skip_reasons)
    gate_text = format_deform_gate_rejections(gate_rejected_parts)
    if gate_text:
        gate_text = f"{DEFORM_GATE_REJECT_PREFIX}：{gate_text}"
    diagnostics_text = _format_diagnostics(
        diagnostics,
        skip_prefixes=(DEFORM_GATE_REJECT_PREFIX,) if gate_text else (),
    )

    if data_count > 0 and component_count == 0:
        if not checkbox_enabled:
            # 【t71】开关关闭 = 用户**明确选择普通模式** ⇒ **不得中止导出**（用户实报的误拦）。
            # 旧行为把「工作空间里有**别的**导入留下的合并缓存」误当成「当前几何是全局编号」，
            # 于是把普通导出拦死。为什么现在可以放行：① 导入时开关关着 ⇒ 组名就是**部件局部编号**；
            # ② t66 起写盘按**组名**解析槽号 ⇒ 天然跟随当前 mesh 的编号（不再按列表序号猜）；
            # ③ 真正危险的情形（开关开着导入 ⇒ 全局编号，而本次不产合并骨架）在 hint 里点名。
            return {
                "level": "warning",
                "message": (
                    f"检测到工作空间里有 {data_count} 个部件带合并骨架数据（VGMap），"
                    "但本次导出『使用融合统一顶点组』是关闭的 ⇒ 按**普通模式**继续导出"
                    "（几何按当前 mesh 的顶点组编号写盘）。"
                ),
                "hint": (
                    "普通模式的前提是：当前 mesh 的顶点组是**部件局部编号**"
                    "（即导入时该开关也是关闭的）。若这份 mesh 是「开关开着」导入的，"
                    "组名会是全局编号、而本次不产出运行时合并骨架 ⇒ 游戏里可能整块不显示；"
                    "那种情况请打开该开关重新导出，或关闭开关后重新导入一次。"
                ),
            }
        return {
            "level": "error",
            "message": (
                f"HIMI 骨骼合并中止：{data_count} 个部件带合并数据，但全部被导出器拒绝；"
                "继续导出会退化成普通导出（全局骨骼编号在游戏里无法正确蒙皮）。"
            ),
            "hint": (
                ((f"被拒原因：{reasons_text}。" if reasons_text else "")
                 + (f"{gate_text}。" if gate_text else ""))
                + (f"诊断：{diagnostics_text}。" if diagnostics_text else "")
                + ALL_REJECTED_ADVICE
            ),
        }

    if component_count > 0 and (skip_reasons or gate_rejected_parts):
        hint_parts = []
        if reasons_text:
            hint_parts.append(f"被拒原因：{reasons_text}。")
        if gate_text:
            hint_parts.append(f"{gate_text}。")
        if diagnostics_text:
            hint_parts.append(f"诊断：{diagnostics_text}。")
        hint_parts.append(PARTIAL_ADVICE)
        return {
            "level": "warning",
            "message": (
                "HIMI 骨骼合并不完整："
                f"{len(dict(skip_reasons or {})) + len(dict(gate_rejected_parts or {}))} "
                "个部件未进入合并骨架，这些部件仍按原版局部索引渲染；其余部件按合并骨架导出。"
            ),
            "hint": "".join(hint_parts),
        }

    if data_count == 0 and component_count == 0:
        return {"level": "notice", "message": NO_MERGED_DATA_NOTICE, "hint": ""}

    return {"level": "ok", "message": "", "hint": ""}


# --------------------------------------------------------------------------- #
# 【方案 B / t78】顶点侧不校准（VertexCalibration = "none"）
# --------------------------------------------------------------------------- #
# 语义（唯一事实源，导出侧 / 运行期校验 / oracle / 测试都读这里）：
#
# 写槽矩阵 S = X_f · P_f，顶点缓冲**原样**写盘（不乘任何 Q_f）。
# 为什么去掉顶点侧变换而结果不变：同族 P·Q⁻¹·Q·v ≡ P·v；而真实产物里跨族借用权重行 = 0
# （_himi_calibrate_bind_positions 逐行诊断可复核），所以顶点侧那一步只是把基态搬进
# 「提取帧世界」又原路搬回 —— 净效果恒等，却让**形态键通道**（-Position_merged_packed_pos_delta
# 等，它没有任何 Q 校准）与基态落在两个不同的空间里 ⇒ 运行期 Q·v + δ 而非 Q·(v+δ)。
#
# 身份载荷 [1..3] 原先存这个因子（t39–t43 存 Q_f、t44–t77 存 Q_f⁻¹）。方案 B 起
# **恒为单位阵**：段位与长度保留（CS 的下标、128 B 布局都不动），乘单位阵 = 恒等。
VERTEX_CALIBRATION_NONE = "none"
#: 元数据键名（写进 Config/HIMIMergedSkeleton.json 顶层）。
VERTEX_CALIBRATION_KEY = "VertexCalibration"
#: 单位阵判据的容差：发布/校验两侧共用（float32 round-trip 误差 ~1e-8，1e-6 留 2 个数量级余量）。
ATTACH_PLACEMENT_TOLERANCE = 1e-6
#: 3×4 行主序单位阵（12 个数）—— 载荷 [1..3] 的内容。
ATTACH_PLACEMENT_IDENTITY = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
)


def attach_placement_identity() -> list:
    """单位阵的 12 个数（3×4 行主序）。每次返回**新列表**，调用方可以随意改。"""
    return list(ATTACH_PLACEMENT_IDENTITY)


def attach_placement_deviation(rows) -> float:
    """rows 与单位阵的最大逐元绝对偏差；形状/数值不可用 ⇒ inf。"""
    try:
        values = [float(value) for value in rows]
    except (TypeError, ValueError):
        return float("inf")
    if len(values) != 12:
        return float("inf")
    return max(abs(a - b) for a, b in zip(values, ATTACH_PLACEMENT_IDENTITY))


def attach_placement_problem(
    rows, *, label: str = "族级摆放段", tolerance: float = ATTACH_PLACEMENT_TOLERANCE
) -> str:
    """非空 = 这一段不是单位阵（方案 B 的硬门：那说明产物会被二次缩放/旋转）。

    返回中文原因（直接拼进 Fatal 文案）；空串 = 通过。
    """
    deviation = attach_placement_deviation(rows)
    limit = float(tolerance)
    if deviation <= limit:
        return ""
    return (
        f"{label}不是单位阵（最大偏差 {deviation:.6g} > {limit:g}）："
        "方案 B 已取消顶点侧校准，palette 侧只写 S = X_f·P_f ⇒ 载荷 [1..3] 必须是单位阵；"
        "非单位阵意味着产物会被二次缩放/旋转（t39–t77 的旧口径）"
    )


# --------------------------------------------------------------------------- #
# 【B3 硬门】合并骨架 + 非 Basis 形态键块 + 形态键节点未勾直出 ⇒ 拒绝导出
# --------------------------------------------------------------------------- #
SHAPEKEY_DIRECT_MODE_HINT = (
    "请把**全部**形态键节点（SSMTNode_PostProcess_ShapeKey）的「直出模式 / direct_export_mode」"
    "勾上（新节点默认已勾选）后重新导出；若本次不需要形态键，请在导出前删掉这些形态键块。"
    "不要靠关掉形态键块来绕过：标准路线的槽位轮次会先落盘 Meshes1001+，而合并骨架的 Position "
    "缓冲与全局顶点组编号只对得上 Meshes0000。"
)


def shapekey_direct_mode_problem(
    *, merged_active: bool, non_basis_blocks: int, needs_standard_rounds: bool
) -> str:
    """【B3】返回中文原因；空串 = 放行。

    * merged_active：本次导出是否真的产出了合并骨架（_himi_merged_attach_active()）；
    * non_basis_blocks：本次导出涉及的非 Basis 形态键块个数（count_exportable_shapekey_blocks）；
    * needs_standard_rounds：是否有形态键节点**没有**勾直出（= 会走标准槽位轮次）。

    三者同时成立才会互相踩：合并骨架只在 Meshes0000 里重建了骨骼编号，标准路线的形态键轮次
    会重新烘焙 Position/顶点组，两边对不上 ⇒ 游戏里形态键一开就整块撕裂。
    """
    if not merged_active:
        return ""
    try:
        blocks = int(non_basis_blocks or 0)
    except (TypeError, ValueError):
        blocks = 0
    if blocks <= 0:
        return ""
    if not needs_standard_rounds:
        return ""
    return (
        f"HIMI 骨骼合并与「标准（非直出）形态键路线」不能同时使用：本次导出产出了合并骨架，"
        f"同时检测到 {blocks} 个非 Basis 形态键块，且有形态键节点**没有**勾选 direct_export_mode ⇒ "
        "标准路线会先落盘 Meshes1001+ 的形态键槽位，而合并骨架的 Position 缓冲/全局骨骼编号"
        "只对得上 Meshes0000，两者混在一起会让形态键一开就整块撕裂。"
        + SHAPEKEY_DIRECT_MODE_HINT
    )


# --------------------------------------------------------------------------- #
# 【方案 B / 第 4 项】离线读口径：Config/HIMIMergedSkeleton.json 里的 VertexCalibration
# --------------------------------------------------------------------------- #
def read_vertex_calibration(payload) -> str:
    """从合并骨架元数据（或单看 ``FamilyX`` 段）里读出方案 B 标注。

    导出侧 :func:`ui.universal.himi.ExportHIMI._himi_publish_family_x_record` 把它写进
    ``FamilyX`` 段；顶层也认，方便旧产物/手写记录。返回空串 = 没有记录（旧产物）。
    """
    if not isinstance(payload, dict):
        return ""
    direct = payload.get(VERTEX_CALIBRATION_KEY)
    if isinstance(direct, str) and direct:
        return str(direct)
    section = payload.get("FamilyX")
    if isinstance(section, dict):
        value = section.get(VERTEX_CALIBRATION_KEY)
        if isinstance(value, str) and value:
            return str(value)
    return ""
