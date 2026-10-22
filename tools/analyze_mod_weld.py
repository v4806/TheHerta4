#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出产物焊接检查工具（验收用，只读）。

用途：对一个 ZZMI 导出目录里的 `Meshes0000` 产物做量化，判断「同一 IB 内分块接缝顶点
是否被焊接」。可用于改动前后的对比：

    python tools/analyze_mod_weld.py "<mod目录>" --drawib 92cb56c9 --position-stride 40

统计口径：
- 顶点数 = Position.buf 字节数 / Position 行宽
- 三角形数 = Index.buf 字节数 / 4 / 3
- 重复位置顶点数：把所有顶点位置按 epsilon 量化后，位置相同的顶点构成的「多余份数」
  即 sum(份数 - 1)。这个值≈接缝处未焊接的顶点数（同一位置被多个分块各存一份）。
  焊接后该值应显著下降（降到只剩真正重合但不同分块的残留）。

该工具不依赖 numpy / bpy，只用标准库。
"""

import argparse
import os
import struct
from collections import defaultdict


def _read_positions(path: str, stride: int, pos_offset: int = 0):
    """返回 [(x, y, z), ...]"""
    with open(path, "rb") as f:
        data = f.read()
    if stride <= 0:
        raise ValueError("stride 必须大于 0")
    if len(data) % stride != 0:
        raise ValueError(
            f"Position 字节数 {len(data)} 不能被行宽 {stride} 整除，stride 可能不对"
        )
    positions = []
    for i in range(0, len(data), stride):
        x, y, z = struct.unpack_from("<3f", data, i + pos_offset)
        positions.append((x, y, z))
    return positions


def _quant_key(xyz, eps: float):
    return (
        int(round(xyz[0] / eps)),
        int(round(xyz[1] / eps)),
        int(round(xyz[2] / eps)),
    )


def analyze(mod_dir: str, drawib: str, position_stride: int, eps: float, pos_offset: int = 0):
    mesh_dir = os.path.join(mod_dir, "Meshes0000")
    if not os.path.isdir(mesh_dir):
        raise SystemExit(f"找不到产物目录: {mesh_dir}")

    # 找到该 drawib 的索引文件（LOD0.<drawib>-* -Index.buf 可能不止一个）
    index_files = sorted(
        os.path.join(mesh_dir, name)
        for name in os.listdir(mesh_dir)
        if name.startswith(f"LOD0.{drawib}-") and name.endswith("-Index.buf")
    )
    if not index_files:
        raise SystemExit(f"在 {mesh_dir} 中找不到 drawib={drawib} 的 Index.buf")

    # Position 文件名形如 <drawib前缀>-Position.buf；用索引文件名反推前缀更稳
    report = []
    for index_path in index_files:
        stem = os.path.basename(index_path)[: -len("-Index.buf")]
        position_candidates = [
            os.path.join(mesh_dir, drawib + "-Position.buf"),
            os.path.join(mesh_dir, stem + "-Position.buf"),
        ]
        position_path = next((path for path in position_candidates if os.path.isfile(path)), None)
        if position_path is None:
            report.append({"index": os.path.basename(index_path), "error": "缺少 Position.buf"})
            continue

        positions = _read_positions(position_path, position_stride, pos_offset)
        with open(index_path, "rb") as f:
            raw_index = f.read()
        index_count = len(raw_index) // 4
        indices = struct.unpack("<%dI" % index_count, raw_index) if index_count else ()

        buckets = defaultdict(int)
        for xyz in positions:
            buckets[_quant_key(xyz, eps)] += 1
        dup_extra = sum(count - 1 for count in buckets.values() if count > 1)
        dup_positions = sum(1 for count in buckets.values() if count > 1)

        used = len(set(indices)) if indices else 0
        report.append(
            {
                "index_file": os.path.basename(index_path),
                "position_file": os.path.basename(position_path),
                "vertex_count": len(positions),
                "index_count": index_count,
                "triangle_count": index_count // 3,
                "referenced_vertices": used,
                "unreferenced_vertices": len(positions) - used,
                "duplicate_position_groups": dup_positions,
                "duplicate_extra_vertices": dup_extra,
                "max_index": max(indices) if indices else -1,
            }
        )
    return report


def main():
    parser = argparse.ArgumentParser(description="ZZMI 导出产物焊接检查（只读）")
    parser.add_argument("mod_dir", help="mod 目录（含 Meshes0000）")
    parser.add_argument("--drawib", required=True, help="drawib hash 前缀，例如 92cb56c9")
    parser.add_argument(
        "--position-stride",
        type=int,
        required=True,
        help="Position.buf 每顶点字节数（绝区零常见 40）",
    )
    parser.add_argument("--pos-offset", type=int, default=0, help="位置在行内的字节偏移")
    parser.add_argument("--eps", type=float, default=1e-5, help="位置量化阈值（默认 1e-5）")
    args = parser.parse_args()

    result = analyze(args.mod_dir, args.drawib, args.position_stride, args.eps, args.pos_offset)
    for row in result:
        for key, value in row.items():
            print(f"{key}: {value}")
        print("-" * 40)


if __name__ == "__main__":
    main()
