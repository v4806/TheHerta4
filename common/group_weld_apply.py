"""「接缝焊接」后处理节点的配置读取与物体名匹配（导出期焊接链路的公共入口）。

职责：
1. ``collect_seam_weld_config``：从蓝图的后处理链里读取「接缝焊接」节点的手动分组
   与距离阈值；没有节点返回 None（= 不焊接，完全保持原导出行为）。
2. 物体名匹配工具（``_normalize_object_name`` / ``_name_match_score`` /
   ``_match_units_for_name``）：节点里填的物体名 → 导出单元。完全相等优先，
   其次互相包含；忽略大小写与 ``_copy`` / ``.001`` 等临时后缀。
3. DrawCall 切换签名（``_collect_drawcall_signature``）与候选名
   （``_drawcall_names``）：只用于历史日志 / 兼容读取，焊接本身不再需要它们
   （seam 驱动方案不按切换签名判断分组）。

真正的焊接执行在 ``common/group_weld_mesh.py``：在 ``SubMeshModel`` join 之后的
临时 mesh 上，只把**用户手动标记的缝合边（seam）顶点**按节点分组交给
``bmesh.ops.remove_doubles``（位置合并与代表顶点全由 Blender 决定），导出缓冲由原有
代码从焊好的 mesh 重新生成。

本模块只依赖 numpy，不导入 bpy（便于单测）。
"""

from __future__ import annotations

import re

import numpy

from .group_weld import DEFAULT_WELD_THRESHOLD

__all__ = [
    "SEAM_WELD_NODE_IDNAME",
    "collect_seam_weld_config",
    "name_match_score",
]

#: 接缝焊接后处理节点的 bl_idname（节点存在才焊接）
SEAM_WELD_NODE_IDNAME = "SSMTNode_PostProcess_SeamWeld"

#: 支持的切换条件运算符（``SSMTNode_ObjectSwap.condition_operator`` 只有这两种）
_SUPPORTED_OPERATORS = ("&&",)


def _warn(message: str) -> None:
    """把警告写到插件日志；无 bpy 环境（单测）时退回 print。"""
    try:
        from ..utils.log_utils import LOG

        LOG.warning(message)
        return
    except Exception:
        pass
    print("[group_weld_apply] " + str(message))


def _log(message: str) -> None:
    """把阶段日志写到插件日志；无 bpy 环境（单测）时退回 print。"""
    try:
        from ..utils.log_utils import LOG

        LOG.info(message)
        return
    except Exception:
        pass
    print("[group_weld_apply] " + str(message))


# ---------------------------------------------------------------------------
# 节点配置读取
# ---------------------------------------------------------------------------


def collect_seam_weld_config(blueprint_model) -> dict | None:
    """读取蓝图里的接缝焊接节点配置；没有节点返回 None（= 不焊接）。

    返回 ``{"threshold": float, "groups": [[物体名, ...], ...]}``。节点存在但没配置
    任何组时返回空 groups（调用方据此不焊接）。
    """
    nodes = getattr(blueprint_model, "postprocess_nodes", None) or []
    node = None
    for candidate in nodes:
        if str(getattr(candidate, "bl_idname", "")) == SEAM_WELD_NODE_IDNAME:
            node = candidate
            break
    if node is None:
        return None

    try:
        threshold = float(getattr(node, "weld_threshold", DEFAULT_WELD_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_WELD_THRESHOLD
    if not numpy.isfinite(threshold):
        threshold = DEFAULT_WELD_THRESHOLD

    groups: list = []
    for group in getattr(node, "groups", None) or []:
        names: list = []
        for item in getattr(group, "objects", None) or []:
            name = str(getattr(item, "object_name", "") or "").strip()
            if name and name not in names:
                names.append(name)
        if names:
            groups.append(names)
    return {"threshold": threshold, "groups": groups}


# ---------------------------------------------------------------------------
# DrawCall 候选名 / 切换签名
# ---------------------------------------------------------------------------


def _collect_drawcall_signature(draw_call) -> tuple:
    """取单个 DrawCall 的切换条件签名。

    返回 ``(signature, weldable, reason)``：
    - ``signature``: ``{key_name: tmp_value}``，只含 ``is_swapkey`` 的 M_Key；
      空签名 = 该物体是「常驻」（例如身体），非空 = 可切换单元（脸 / 颈）。
    - ``weldable``: 条件语义是否可判定（``||`` 等拿不准的组合返回 False，不参与焊接）；
    - ``reason``: 不可判定时的中文原因。
    """
    signature: dict = {}
    reason = ""
    weldable = True
    for index, work_key in enumerate(getattr(draw_call, "work_key_list", []) or []):
        if not bool(getattr(work_key, "is_swapkey", False)):
            continue
        key_name = str(getattr(work_key, "key_name", "") or "").strip()
        if not key_name:
            continue
        operator = str(getattr(work_key, "condition_operator", "&&") or "&&").strip()
        if index > 0 and operator not in _SUPPORTED_OPERATORS:
            weldable = False
            reason = f"切换条件运算符 {operator!r} 无法判定互斥关系（仅支持 &&）"
            continue
        try:
            value = int(getattr(work_key, "tmp_value", 0))
        except (TypeError, ValueError):
            weldable = False
            reason = "切换条件的 tmp_value 不是整数"
            continue
        if key_name in signature and signature[key_name] != value:
            weldable = False
            reason = f"同一对象对 {key_name} 声明了冲突取值"
            continue
        signature[key_name] = value
    return signature, weldable, reason


def _drawcall_names(draw_call, fallback: str) -> list:
    """收集单个 DrawCall 可用于匹配节点配置的候选物体名（去重保序）。

    候选：``get_blender_obj_name()`` / ``source_obj_name`` / ``obj_name`` /
    ``comment_alias_name``，与 ``DrawCallModel`` 的字段一一对应。
    """
    names: list = []
    if draw_call is None:
        return [str(fallback)] if fallback else []
    getter = getattr(draw_call, "get_blender_obj_name", None)
    if callable(getter):
        try:
            names.append(str(getter() or ""))
        except Exception:
            pass
    for attribute in ("source_obj_name", "obj_name", "comment_alias_name"):
        names.append(str(getattr(draw_call, attribute, "") or ""))
    if fallback:
        names.append(str(fallback))
    result: list = []
    for name in names:
        name = name.strip()
        if name and name not in result:
            result.append(name)
    return result


# ---------------------------------------------------------------------------
# 物体名匹配
# ---------------------------------------------------------------------------

#: 物体名里的临时后缀：``_copy``、``_copy.001``、``.001``、``.002`` 等。
_TEMP_SUFFIX_PATTERN = re.compile(r"(?:_copy|\.\d+|_\d{3,})+$", re.IGNORECASE)


def _normalize_object_name(name) -> str:
    """物体名归一化：去空白 / 临时后缀，大小写不敏感。"""
    text = str(name or "").strip()
    if not text:
        return ""
    text = _TEMP_SUFFIX_PATTERN.sub("", text).strip()
    return text.casefold()


def _name_match_score(config_name, unit_names) -> int:
    """配置名与单元候选名的匹配分：3=完全相等，2=候选包含配置，1=配置包含候选。"""
    target = _normalize_object_name(config_name)
    if not target:
        return 0
    normalized = [_normalize_object_name(name) for name in (unit_names or [])]
    normalized = [name for name in normalized if name]
    if target in normalized:
        return 3
    score = 0
    for candidate in normalized:
        if target in candidate:
            score = max(score, 2)
        elif candidate in target:
            score = max(score, 1)
    return score


#: 对外的别名（group_weld_mesh 复用同一套匹配规则）。
name_match_score = _name_match_score


def _match_units_for_name(units, config_name) -> list:
    """把一个配置物体名匹配到焊接单元（返回最高分的所有单元，保持单元顺序）。

    单元可以是 ``group_weld_mesh.build_mesh_vertex_ranges`` 产出的区间 dict，
    也可以是任何带 ``names`` 列表的 dict。
    """
    best_score = 0
    hits: list = []
    for unit in units:
        names = unit.get("names") or [unit.get("label", "")]
        score = _name_match_score(config_name, names)
        if score <= 0:
            continue
        if score > best_score:
            best_score = score
            hits = [unit]
        elif score == best_score:
            hits.append(unit)
    return hits
