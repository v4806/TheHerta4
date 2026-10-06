"""Pure validation for the ZZMI merged-skeleton import/export contract.

The import side writes global ``VGMap`` metadata into workspace JSON, and when
the ``import_merged_vgmap`` checkbox is on the Blender meshes then use those
global vertex-group ids.  The export side must emit the matching runtime merged
skeleton whenever the meshes actually use global ids; silently falling back to
a normal local-index export in that situation makes the resulting mod render
with invalid palettes.

Note that the checkbox — not the mere presence of cached ``VGMap`` metadata —
decides which id space the meshes live in: ZZMI gates its whole merged-skeleton
preprocess on that checkbox, so a checkbox-off export uses part-local ids even
when the workspace still carries the cache (the import side always generates
that cache, regardless of the checkbox).  Because the cache alone therefore
cannot tell a plain export from a merged one, the caller also reports how many
parts *really* use global ids, read from the meshes' vertex-group id space.
Keeping this decision in a small, bpy-free module makes it easy to test
independently of Blender.
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
    parts_with_global_ids: int = 0,
    skip_reasons=None,
) -> dict:
    """Return a contract decision.

    ``parts_with_global_ids`` is the caller's **geometric** evidence: how many
    parts really carry global bone ids in their vertex groups (the caller
    inspects the meshes, since cached ``VGMap`` metadata alone cannot tell a
    plain export from a merged one).  Defaults to 0 for callers that have no
    such evidence; the checkbox alone then decides.

    ``level == 'error'`` means the caller should abort export.  A workspace
    with no merged metadata is a normal, informational case; so is a workspace
    whose checkbox is off while cached metadata lingers (plain, part-local
    export).  Partial metadata is a warning because the rejected parts will not
    be represented by the merged runtime skeleton.
    """
    try:
        data_count = int(parts_with_data or 0)
    except (TypeError, ValueError):
        data_count = 0
    try:
        component_count = int(component_count or 0)
    except (TypeError, ValueError):
        component_count = 0
    try:
        global_id_count = int(parts_with_global_ids or 0)
    except (TypeError, ValueError):
        global_id_count = 0
    reasons_text = format_skip_reasons(skip_reasons)

    if data_count > 0 and component_count == 0:
        if not checkbox_enabled:
            # 关闭复选框时有两种情形，**缓存本身区分不了**，只能看几何的真实编号
            # 空间（调用方 `ExportZZMI._submesh_uses_global_bone_ids` 读顶点组）：
            # - 几何是部件局部编号 ⇒ 本次就是普通导出。缓存只是导入侧**无条件**
            #   落盘的派生数据（ui/ui_func_import_ssmt.py「生成侧与消费侧分离」），
            #   不参与本次导出 ⇒ notice，绝不中止（旧实现只看「有没有缓存」，
            #   把从未开启过该开关的普通导出误判成致命错误）。
            # - 几何确实是全局骨骼编号（导入时开着开关、导出前被关掉）⇒ 导出会
            #   丢掉运行时合并骨架，游戏里整块不显示 ⇒ 仍然中止。
            if global_id_count > 0:
                return {
                    "level": "error",
                    "message": (
                        f"骨骼合并中止：工作空间里有 {data_count} 个部件带合并骨架数据"
                        f"（VGMap），其中 {global_id_count} 个部件的几何**确实**使用全局骨骼"
                        "编号，但导出时‘使用融合统一顶点组’是关闭的。这样导出的几何"
                        "没有对应的运行时合并骨架，游戏里会整块不显示。"
                    ),
                    "hint": (
                        "打开‘使用融合统一顶点组’后重新导出；若确实要走普通模式，"
                        "请在关闭该开关后重新一键导入，让顶点组回到部件局部编号"
                        "（工作区的合并骨架缓存可以留着，不影响）。"
                    ),
                }
            return {
                "level": "notice",
                "message": (
                    f"本次导出为普通导出：工作空间里留有 {data_count} 个部件的"
                    "合并骨架缓存（VGMap/VGCount），但‘使用融合统一顶点组’是关闭的，"
                    "几何按部件局部顶点组编号导出，这份缓存本次不被消费。"
                ),
                "hint": "",
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

