"""接缝焊接的共享常量（seam 驱动方案，2026-10-22 用户最终拍板）。

历史说明：本模块原先实现「按位置自动配对 + 自定义代表顶点规则（优先身体）」的
numpy 焊接核心（``build_weld_representatives`` / ``compress_vertex_buffers`` /
``remap_indices``）。那套判断逻辑已**整体废弃**：

    用户已在 Blender 里手动把需要按距离合并的顶点标记为缝合边（seam edge），
    导出时只按这个标记焊，位置合并完全交给 Blender 原生
    ``bmesh.ops.remove_doubles``，代表顶点也由 Blender 决定。

真正的焊接实现见 :mod:`common.group_weld_mesh`。
"""

from __future__ import annotations

__all__ = ["DEFAULT_WELD_THRESHOLD"]


# 默认焊接阈值：0.0001（= Blender「按距离合并」的默认值）
DEFAULT_WELD_THRESHOLD = 0.0001
