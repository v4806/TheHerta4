"""Pure validation for the ZZMI merged-skeleton import/export contract.

The import side writes global ``VGMap`` metadata into workspace JSON and the
Blender meshes then use those global vertex-group ids.  The export side must
emit the matching runtime merged skeleton whenever that metadata is present;
silently falling back to a normal local-index export makes the resulting mod
render with invalid palettes.  Keeping this decision in a small, bpy-free
module makes it easy to test independently of Blender.
"""


NO_MERGED_DATA_NOTICE = (
    "本次导出为普通导出：工作空间里没有合并骨架数据（VGMap/VGCount）。"
    "若你期望合并骨架，请先在‘使用融合统一顶点组’开启的情况下重新导入一次，再导出。"
)


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


def evaluate_merged_skeleton_contract(
    *,
    checkbox_enabled: bool,
    parts_with_data: int,
    component_count: int,
    skip_reasons=None,
) -> dict:
    """Return a contract decision.

    ``level == 'error'`` means the caller should abort export.  A workspace
    with no merged metadata is a normal, informational case; partial metadata
    is a warning because the rejected parts will not be represented by the
    merged runtime skeleton.
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

    if data_count > 0 and component_count == 0:
        if not checkbox_enabled:
            return {
                "level": "error",
                "message": (
                    f"骨骼合并中止：工作空间里有 {data_count} 个部件带合并骨架数据（VGMap），"
                    "但导出时‘使用融合统一顶点组’是关闭的。这样导出的几何使用全局骨骼编号，"
                    "却没有对应的运行时合并骨架，游戏里会整块不显示。"
                ),
                "hint": (
                    "打开‘使用融合统一顶点组’后重新导出；若确实要走普通模式，"
                    "请关闭该开关后重新导入，让顶点组回到部件局部编号。"
                ),
            }
        return {
            "level": "error",
            "message": (
                f"骨骼合并中止：{data_count} 个部件带合并数据，但全部被导出器拒绝；"
                "继续导出会退化成普通导出（全局骨骼编号在游戏里无法正确蒙皮）。"
            ),
            "hint": (
                (f"被拒原因：{reasons_text}。" if reasons_text else "")
                + "请用‘清除骨骼合并VGMap缓存’后重新导入，或把这条诊断发给开发者。"
            ),
        }

    if component_count > 0 and skip_reasons:
        return {
            "level": "warning",
            "message": (
                f"骨骼合并不完整：{len(dict(skip_reasons))} 个部件未进入合并骨架，"
                "这些部件在游戏里可能不显示，其余部件仍按合并骨架导出。"
            ),
            "hint": f"被拒原因：{reasons_text}。" if reasons_text else "",
        }

    if data_count == 0 and component_count == 0:
        return {"level": "notice", "message": NO_MERGED_DATA_NOTICE, "hint": ""}

    return {"level": "ok", "message": "", "hint": ""}

