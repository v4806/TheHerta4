"""HIMI (崩坏3 / HI3) 骨骼合并实证探针 —— 一次跑出合并收益与不变量。

只读工具：只读取 K: 盘上的 FrameAnalysis dump 与 WorkSpace 工作空间文件，
从不写入、不修改、不删除任何 dump / 工作空间文件。

用法（仓库根目录）：

    .venv\\Scripts\\python.exe tools\\himi_merged_probe.py
    .venv\\Scripts\\python.exe tools\\himi_merged_probe.py --json reports/himi-merge/04-probe-results.json

输出 = stdout 的确定性文本 +（可选）--json 的结构化全量事实。所有数字都由本脚本
一次跑出，不依赖任何手工录入。

它回答的问题（对应任务 T4 的 ①..⑥）：

  ① 每个角色每个组件的 顶点数 / Blend 资源 hash / 调色板骨骼数 / BLENDINDICES 最大值，
     并验证「调色板骨骼数 == 最大值 + 1」（无填充）。
  ② 调色板按 12 float = 3x4 矩阵解释时旋转行模长是否恒为 1；并排除 16 float 解释。
  ③ 同角色内跨组件按矩阵内容逐位去重后的全局唯一骨骼数、共享槽数，以及哪些组件
     是同一份数据被拆成多个子网格（共享 VGMap/palette）。
  ④ 跨角色之间是否也存在重合骨骼（预期没有）。
  ⑤ 合并后骨骼总数与 252 / 374 这类上限的关系；4 权重 BW16/BI16 是否足以承载
     合并后的全局索引（HIMI 是 R32G32B32A32_SINT，无需升宽）。
  ⑥ deform pass 与工作空间组件的 join 成功率；未 join 的 pass 属于同帧其他角色的判据。

设计要点（踩过的坑，改脚本时别踩回去）：

  * dump 的 **原始** 文件名（`000035-vb1=0d61cd9e-vs=....buf`）**不带** stride/count；
    布局元数据在 **deduped 目标** 文件名里（`<内容hash>-vb1-layout=..-topology=pointlist-stride=32-count=N.txt`）。
    所以 vb0/vb1 的 stride/count 必须从 " -> " 右侧解析。
  * deduped 目录按 **内容 hash** 去重，palette 的 sidecar 名会带一个非法字符
    （`831521e1-?b0.txt`），因此 palette 内容文件固定取同一条 dump 行里 `.buf` 那个目标。
  * palette 是 ring scratch 复用：同一个资源 hash 一帧内被多个 deform pass 重写。
    必须按 **该 draw 的 dump 行** 定位内容文件，绝不能按资源 hash 反查。
  * 工作空间的 `CategoryHash.Blend` 是内容 hash，等于该组件在 deform pass 里 vb1 的
    deduped 内容 hash；join 用 (hash, count) 做键，再用 **字节级相等** 复核。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# 常量：全部来自实证，不猜
# --------------------------------------------------------------------------- #

HI3_ROOT = Path(r"K:\SSMT-Package-master\3Dmigoto\HI3")
WS_ROOT = Path(r"K:\SSMT-Package-master\WorkSpace\HIMI")

BLEND_STRIDE = 32          # BLENDWEIGHTS(4xf32) + BLENDINDICES(4xi32)
POSITION_STRIDE = 44       # P12 N12 C4 TA16
PALETTE_STRIDE = 48        # 12 x float32 = 3x4 矩阵
PALETTE_FLOATS = 12
PALETTE_ALT_FLOATS = 16    # 被否决的候选解释

# ZZMI 参考上限（来自 ZZMI 计划的既成事实，仅作对照标尺，不是本仓库里的硬编码常量）
ZZMI_LIMITS = {
    "zzmi_merged_skeleton_total": 374,
    "zzmi_per_component_peak": 252,
    "bi8_index_ceiling": 255,
    "bi16_index_ceiling": 65535,
}

DUMP_LINE_RE = re.compile(
    r"^(\d{6})\s+3DMigoto Dumping (?:Buffer|Texture2D) .*[\\/]([^\\/]+) -> .*[\\/]([^\\/]+)$"
)
SRC_VB_RE = re.compile(r"^(\d{6})-vb(\d+)=(\w+)-")
SRC_T0_RE = re.compile(r"^(\d{6})-vs-t0=(\w+)-")
DST_VB_RE = re.compile(
    r"-vb(\d+)-layout=(\w+)-topology=(\w+)-stride=(\d+)(?:-first=\d+)?-count=(\d+)\.txt$"
)
DRAWCALL_RE = re.compile(r"^(\d{6})\s+(.*)$")


# --------------------------------------------------------------------------- #
# 解析层
# --------------------------------------------------------------------------- #


def role_dump_map() -> dict[str, str]:
    """角色 -> 该角色 Config/FrameAnalysisPath.json 里记录的 dump 目录名。"""
    out: dict[str, str] = {}
    for role_dir in sorted(p for p in WS_ROOT.iterdir() if p.is_dir()):
        cfg = role_dir / "Config" / "FrameAnalysisPath.json"
        if not cfg.exists():
            continue
        data = json.loads(cfg.read_text(encoding="utf-8"))
        dump = data.get("selectedFrameAnalysis") or ""
        if dump:
            out[role_dir.name] = dump
    return out


def parse_dump_log(fa: Path) -> dict:
    """解析一份 FrameAnalysis 的 log.txt。

    返回 {draw: {"vb": {slot: {res, stride, topology, count, buf}}, "t0": {...},
                 "so": [hash...], "topology": "pointlist"|"trianglelist",
                 "draw_vertex_count": int|None, "index_count": int|None}}
    """
    text = (fa / "log.txt").read_text(encoding="utf-8", errors="replace")
    draws: dict[str, dict] = {}

    for line in text.splitlines():
        m = DUMP_LINE_RE.match(line.rstrip())
        if not m:
            continue
        draw, src, dst = m.group(1), m.group(2), m.group(3)
        entry = draws.setdefault(draw, {})
        mv = SRC_VB_RE.match(src)
        if mv:
            slot = int(mv.group(2))
            rec = entry.setdefault("vb", {}).setdefault(slot, {})
            if dst.lower().endswith(".txt"):
                md = DST_VB_RE.search(dst)
                if md:
                    rec["layout"] = md.group(2)
                    rec["topology"] = md.group(3)
                    rec["stride"] = int(md.group(4))
                    rec["count"] = int(md.group(5))
            else:
                rec["res"] = mv.group(3)
                rec["buf"] = Path(dst).name
            continue
        mt = SRC_T0_RE.match(src)
        if mt:
            rec = entry.setdefault("t0", {})
            if dst.lower().endswith(".txt"):
                rec["sidecar"] = Path(dst).name
            else:
                rec["res"] = mt.group(2)
                rec["buf"] = Path(dst).name

    last_draw: str | None = None
    expecting: str | None = None
    for line in text.splitlines():
        line = line.rstrip()
        m = DRAWCALL_RE.match(line)
        if m:
            draw, rest = m.group(1), m.group(2)
            if rest.startswith("3DMigoto"):
                continue
            last_draw = draw
            entry = draws.setdefault(draw, {})
            if rest.startswith("SOSetTargets"):
                entry.setdefault("so", [])
                expecting = "so"
                continue
            if rest.startswith("IASetVertexBuffers"):
                expecting = "ia"
                continue
            if rest.startswith("VSSetShaderResources"):
                expecting = "srv"
                continue
            mdr = re.match(r"^Draw\(VertexCount:(\d+),", rest)
            if mdr:
                entry["topology"] = "pointlist"
                entry["draw_vertex_count"] = int(mdr.group(1))
                expecting = None
                continue
            mdi = re.match(r"^DrawIndexed\(IndexCount:(\d+),", rest)
            if mdi:
                entry["topology"] = "trianglelist"
                entry["index_count"] = int(mdi.group(1))
                expecting = None
                continue
            expecting = None
            continue

        if last_draw is None or expecting is None:
            continue
        mc = re.match(r"^\s*(\d+):\s*(.*)$", line)
        if not mc:
            continue
        mh = re.search(r"hash=(\w+)", mc.group(2))
        if not mh:
            continue
        if expecting == "so":
            draws[last_draw].setdefault("so", []).append(mh.group(1))
    return draws


def is_deform_pass(entry: dict) -> bool:
    """deform pass 的结构判据（不认 shader hash，hash 随游戏版本变）。

    · 绑了 SO target（SOSetTargets）
    · 非索引 `Draw(VertexCount:N)`（pointlist）
    · vb0 stride 44 + vb1 stride 32，且两者 topology 都是 pointlist
    · 绑了 vs-t0（骨骼 palette SRV）
    """
    if not entry.get("so"):
        return False
    if entry.get("topology") != "pointlist" or not entry.get("draw_vertex_count"):
        return False
    vb = entry.get("vb", {})
    v0, v1 = vb.get(0, {}), vb.get(1, {})
    if v0.get("stride") != POSITION_STRIDE or v1.get("stride") != BLEND_STRIDE:
        return False
    if v0.get("topology") != "pointlist" or v1.get("topology") != "pointlist":
        return False
    return "buf" in entry.get("t0", {})


def scan_workspace(role: str) -> list[dict]:
    """角色工作空间里全部 LOD0 子网格组件。

    目录形状：`LOD0/<component>/<WorkGameType>/<component>.json` + 同名 .buf。
    同一 `<component>` 可以有多份 json（T4/T8 两种 WorkGameType），它们指向**同一批
    .buf**、`CategoryHash` 相同、`VertexLimitVB`（= deform pass 的 SO 目标 hash）相同，
    只有 WorkGameType / 纹理标记不同。因此组件身份用 `<component>`（json stem）去重，
    并把 WorkGameType 列表作为属性保留，避免把「同组件两种 WorkGameType」当成两个部件。
    """
    groups: dict[str, list[Path]] = defaultdict(list)
    for json_path in sorted((WS_ROOT / role / "LOD0").glob("*/*/*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "CategoryBufferList" not in data:
            continue
        groups[json_path.stem].append(json_path)

    comps: list[dict] = []
    for comp, paths in sorted(groups.items()):
        variants: list[dict] = []
        for json_path in paths:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            variants.append({"json": str(json_path), "work_game_type": data.get("WorkGameType", "")})
        first = json.loads(paths[0].read_text(encoding="utf-8"))
        hashes = first.get("CategoryHash", {})
        blend_path = paths[0].parent / f"{comp}-Blend.buf"
        pos_path = paths[0].parent / f"{comp}-Position.buf"
        blend_fmt: dict[str, str] = {}
        for buf in first.get("CategoryBufferList", []):
            if buf.get("FileName", "").endswith("-Blend.buf"):
                for el in buf.get("D3D11ElementList", []):
                    blend_fmt[el.get("SemanticName", "")] = el.get("Format", "")
        comps.append(
            {
                "role": role,
                "component": comp,
                "ib_hash": comp.rsplit("-", 2)[0] if comp.count("-") >= 2 else comp,
                "index_count": int(comp.rsplit("-", 2)[1]) if comp.count("-") >= 2 else None,
                "match_first_index": int(comp.rsplit("-", 2)[2]) if comp.count("-") >= 2 else None,
                "json_variants": variants,
                "json_variant_count": len(variants),
                "submesh": paths[0].parent.name,
                "blend_hash": hashes.get("Blend", ""),
                "position_hash": hashes.get("Position", ""),
                "texcoord_hash": hashes.get("Texcoord", ""),
                "work_game_type": first.get("WorkGameType", ""),
                "gpu_preskinning": bool(first.get("GPU-PreSkinning", False)),
                "blend_path": str(blend_path),
                "position_path": str(pos_path),
                "vertex_count_blend": blend_path.stat().st_size // BLEND_STRIDE
                if blend_path.exists()
                else None,
                "vertex_count_position": pos_path.stat().st_size // POSITION_STRIDE
                if pos_path.exists()
                else None,
                "blend_formats": blend_fmt,
                # HIMI：VertexLimitVB 里放的是该组件的 deform pass **SO 目标 hash**
                # （= 渲染 pass 的 vb0 资源 hash），不是常量缓冲
                "so_target_hash": first.get("VertexLimitVB", ""),
                "vertex_count_json": first.get("VertexCount", 0),
                "index_count_json": first.get("IndexCount", 0),
                "vertex_offset_json": first.get("VertexOffset", 0),
                "index_offset_json": first.get("IndexOffset", 0),
                "ib_file": (first.get("IndexBufferList") or [{}])[0].get("FileName", ""),
            }
        )
    return comps


# --------------------------------------------------------------------------- #
# 数据层：blend / palette
# --------------------------------------------------------------------------- #


def read_blend(path: Path) -> dict:
    """解析 32 字节步长的 BLENDWEIGHTS + BLENDINDICES 记录。"""
    raw = path.read_bytes()
    if len(raw) % BLEND_STRIDE:
        return {"error": f"size {len(raw)} not divisible by {BLEND_STRIDE}"}
    rec = np.frombuffer(raw, dtype=np.uint8).reshape(-1, BLEND_STRIDE)
    weights = rec[:, 0:16].copy().view(np.float32).reshape(-1, 4)
    indices = rec[:, 16:32].copy().view(np.int32).reshape(-1, 4)

    used = weights > 0.0
    used_idx = indices[used]
    all_idx = indices.reshape(-1)
    # 权重为 0 的通道索引在 HIMI 里恒为 0；仍然单独统计以便复核
    zero_weight_idx = indices[~used]
    return {
        "vertices": int(rec.shape[0]),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "index_min": int(all_idx.min()) if all_idx.size else -1,
        "index_max_all": int(all_idx.max()) if all_idx.size else -1,
        "index_max_used": int(used_idx.max()) if used_idx.size else -1,
        "index_max_zero_weight": int(zero_weight_idx.max()) if zero_weight_idx.size else -1,
        "distinct_indices": sorted({int(v) for v in all_idx.tolist()}),
        "distinct_indices_used": sorted({int(v) for v in used_idx.tolist()}),
        "weight_sum_unit_frac": float(np.mean(np.isclose(weights.sum(axis=1), 1.0, atol=1e-3))),
        "weight_sum_abs_err_max": float(np.abs(weights.sum(axis=1) - 1.0).max()),
        "channels_used_hist": {
            str(k): int(v) for k, v in sorted(Counter((weights > 0.0).sum(axis=1).tolist()).items())
        },
        "negative_index_count": int((all_idx < 0).sum()),
    }


def read_palette(path: Path, referenced: list[int] | None = None) -> dict:
    """解析骨骼 palette；同时检验 12-float 与 16-float 两种解释。

    `referenced` = 该组件 BLENDINDICES 里实际出现的槽位。palette 是 ring scratch
    复用，**未被引用的尾部可能是上一笔 draw 的残留**，所以「单位旋转行」这类不变量
    必须同时给出「全体槽位」和「被引用槽位」两个口径。
    """
    raw = path.read_bytes()
    n = len(raw)
    out: dict = {
        "bytes": n,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "divisible_by": {str(s): n // s for s in (16, 32, 48, 64, 96, 128) if n % s == 0},
        "bones_h12": None,
        "h12": None,
        "h16": None,
    }

    if n % PALETTE_STRIDE == 0:
        f = np.frombuffer(raw, dtype=np.float32).reshape(-1, PALETTE_FLOATS)
        rows = f.reshape(-1, 3, 4)
        rot = rows[:, :, 0:3].astype(np.float64)
        trans = rows[:, :, 3].astype(np.float64)
        # 每个骨骼 3 行；先按行取模长，再按骨骼取最大偏差（不要一次 max 到标量，
        # 否则「单位行占比」与「偏差」两个口径会互相打架）。
        row_len = np.linalg.norm(rot, axis=2)
        per_bone_dev = np.abs(row_len - 1.0).max(axis=1)
        # 正交性：3x3 旋转块应满足 R R^T = I
        gram = np.einsum("bij,bkj->bik", rot, rot)
        ortho_err = np.abs(gram - np.eye(3)).max(axis=(1, 2))
        # 均匀缩放检测：3x3 块的奇异值 / 行列式。HIMI 实测旋转块 = 1.6 x 正交矩阵，
        # 即骨骼矩阵里带一个**全局均匀缩放**（见报告 ②）。
        sv = np.linalg.svd(rot, compute_uv=False)
        det = np.linalg.det(rot)
        scale = float(np.median(sv[:, 0]))
        out["bones_h12"] = int(f.shape[0])
        out["h12"] = {
            "rows_total": int(row_len.size),
            "row_len_min": float(row_len.min()),
            "row_len_max": float(row_len.max()),
            "row_len_abs_dev_max": float(np.abs(row_len - 1.0).max()),
            "row_unit_frac": float(np.isclose(row_len, 1.0, atol=1e-3).mean()),
            "bones_with_all_rows_unit_frac": float(
                np.isclose(per_bone_dev, 0.0, atol=1e-3).mean()
            ),
            "bone_dev_max": float(per_bone_dev.max()),
            "orthonormal_frac": float(np.isclose(ortho_err, 0.0, atol=1e-3).mean()),
            "ortho_err_max": float(ortho_err.max()),
            # 归一化后的正交性：把均匀缩放剥掉（A/scale）后是否正交。
            # 注意：直接看未归一化的 `|A A^T - I|` 对 s·R 会得到 s^2-1（1.6 -> 1.56），
            # 那不是「不正交」，而是均匀缩放的必然结果；必须除以 s^2 再判。
            "scale_median": scale,
            "scale_min": float(sv.min()),
            "scale_max": float(sv.max()),
            "uniform_scale_rel_spread": float((sv.max() - sv.min()) / sv.max())
            if sv.max() > 0
            else 0.0,
            "det_min": float(det.min()),
            "det_max": float(det.max()),
            "det_over_scale3_dev_max": float(
                np.abs(det - scale**3).max() / (scale**3) if scale else 0.0
            ),
            "ortho_err_after_descale_max": float(
                np.abs(
                    np.einsum("bij,bkj->bik", rot / scale, rot / scale) - np.eye(3)
                ).max()
                if scale
                else 0.0
            ),
            "trans_abs_max": float(np.abs(trans).max()),
            "trans_len_mean": float(np.linalg.norm(trans, axis=1).mean()),
            "first_bone": [float(x) for x in f[0]],
            "last_bone": [float(x) for x in f[-1]],
        }

    # 16-float 候选：每骨骼 64 字节
    if n % (PALETTE_ALT_FLOATS * 4) == 0:
        g = np.frombuffer(raw, dtype=np.float32).reshape(-1, PALETTE_ALT_FLOATS)
        g4 = g.reshape(-1, 4, 4).astype(np.float64)
        alt_rot = g4[:, 0:3, 0:3]
        alt_len = np.linalg.norm(alt_rot, axis=2)
        out["h16"] = {
            "bones_h16": int(g.shape[0]),
            "col3_len_min": float(np.linalg.norm(g4[:, :, 3], axis=1).min()),
            "col3_len_max": float(np.linalg.norm(g4[:, :, 3], axis=1).max()),
            "col3_abs_max": float(np.abs(g4[:, :, 3]).max()),
            "rot_len_abs_dev_max": float(np.abs(alt_len - 1.0).max()),
            "rot_unit_frac": float(np.isclose(alt_len, 1.0, atol=1e-3).mean()),
        }
    else:
        out["h16"] = {
            "rejected": f"bytes {n} not divisible by {PALETTE_ALT_FLOATS * 4}"
        }

    # 只统计被 BLENDINDICES 真正引用到的槽位
    if referenced and n % PALETTE_STRIDE == 0:
        idx = np.array(sorted(set(referenced)), dtype=np.int64)
        idx = idx[(idx >= 0) & (idx < out["bones_h12"])]
        if idx.size:
            r = rows[idx]
            rl = np.linalg.norm(r[:, :, 0:3].astype(np.float64), axis=2)
            out["h12_referenced"] = {
                "slots": int(idx.size),
                "row_unit_frac": float(np.isclose(rl, 1.0, atol=1e-3).mean()),
                "row_len_abs_dev_max": float(np.abs(rl - 1.0).max()),
                "bones_with_all_rows_unit_frac": float(
                    np.isclose(np.abs(rl - 1.0).max(axis=1), 0.0, atol=1e-3).mean()
                ),
            }
    return out


def bone_signatures(palette_path: Path) -> list[bytes]:
    """每根骨骼的 48 字节内容签名（逐位去重用的 key）。"""
    raw = palette_path.read_bytes()
    if len(raw) % PALETTE_STRIDE:
        return []
    return [raw[i : i + PALETTE_STRIDE] for i in range(0, len(raw), PALETTE_STRIDE)]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def build_facts() -> dict:
    roles = role_dump_map()
    facts: dict = {
        "inputs": {
            "hi3_root": str(HI3_ROOT),
            "workspace_root": str(WS_ROOT),
            "role_dump_map": roles,
            "zzmi_reference_limits": ZZMI_LIMITS,
        },
        "roles": {},
    }

    for role, dump in roles.items():
        fa = HI3_ROOT / dump
        deduped = fa / "deduped"
        draws = parse_dump_log(fa)
        deform = {d: e for d, e in draws.items() if is_deform_pass(e)}
        render = {
            d: e
            for d, e in draws.items()
            if e.get("topology") == "trianglelist" and e.get("index_count")
        }
        comps = scan_workspace(role)

        # ---- join：deform pass 的 vb1 内容 hash + 顶点数 -> 工作空间组件 ----
        by_key: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for c in comps:
            by_key[(c["blend_hash"], c["vertex_count_blend"])].append(c)

        joined: list[dict] = []
        unjoined: list[dict] = []
        for draw in sorted(deform):
            e = deform[draw]
            vb1 = e["vb"][1]
            key = (vb1.get("res", ""), vb1.get("count", -1))
            cands = by_key.get(key, [])
            blend_buf = deduped / vb1.get("buf", "")
            bytes_eq = False
            if cands and blend_buf.exists():
                a = blend_buf.read_bytes()
                bytes_eq = all(Path(c["blend_path"]).read_bytes() == a for c in cands)
            rec = {
                "draw": draw,
                "vertex_count": e["draw_vertex_count"],
                "position_hash": e["vb"][0].get("res", ""),
                "position_dump": e["vb"][0].get("buf", ""),
                "position_count": e["vb"][0].get("count"),
                "blend_hash": vb1.get("res", ""),
                "blend_dump": vb1.get("buf", ""),
                "blend_count": vb1.get("count"),
                "palette_hash": e.get("t0", {}).get("res", ""),
                "palette_dump": e.get("t0", {}).get("buf", ""),
                # D3（T3 实测）：同一 vs-t0 槽在渲染 pass 是 Texture2D(.dds)，
                # 在 deform pass 才是 Buffer(.buf)。is_deform_pass 先按结构判定，
                # 这里再显式记录扩展名，作为「没有把渲染 pass 的 vs-t0 纹理当调色板」的证据。
                "palette_dump_is_buf": str(e.get("t0", {}).get("buf", "")).lower().endswith(".buf"),
                "so_target": (e.get("so") or [""])[0],
                "vs_hash": "",
                "components": [c["component"] for c in cands],
                "blend_bytes_equal": bool(bytes_eq),
            }
            joined.append(rec) if cands and bytes_eq else unjoined.append(rec)

        # 从原始 dump 行里补 vs hash（SRC_VB 只抓到 vb 行；这里再扫一遍 t0 行），
        # 同时收集渲染 draw 的 IB 内容 hash —— G1 家族门控要用它把 SO 消费者归族。
        text = (fa / "log.txt").read_text(encoding="utf-8", errors="replace")
        vs_by_draw: dict[str, str] = {}
        ib_by_draw: dict[str, str] = {}
        for line in text.splitlines():
            m = DUMP_LINE_RE.match(line.rstrip())
            if not m:
                continue
            mv = re.search(r"-vs=(\w+)", m.group(2))
            if mv:
                vs_by_draw.setdefault(m.group(1), mv.group(1))
            mi = re.search(r"-ib=(\w+)-", m.group(2))
            if mi:
                ib_by_draw.setdefault(m.group(1), mi.group(1))
        for rec in joined + unjoined:
            rec["vs_hash"] = vs_by_draw.get(rec["draw"], "")

        # ---- G1 家族门控统计（T5 §2.8；并入 --self-check 的三条断言）----
        so_families: dict[str, set[str]] = defaultdict(set)
        for draw, entry in render.items():
            ib_hash = ib_by_draw.get(draw, "")
            if not ib_hash:
                continue
            for view in entry.get("vb_hashes") or []:
                so_families[view].add(ib_hash)
        gate_passes_multi_family = sorted(
            so for so, fams in so_families.items() if len(fams) > 1
        )
        # 每个 deform pass 的 SO 目标被几个 IB 家族消费
        pass_so_multi_family = [
            rec["draw"]
            for rec in joined + unjoined
            if len(so_families.get(rec["so_target"], set())) > 1
        ]
        # 工作空间每个 IB 家族的组件是否共享同一 SO 目标
        family_so: dict[str, set[str]] = defaultdict(set)
        family_components: dict[str, list[str]] = defaultdict(list)
        for c in comps:
            family_so[c["ib_hash"]].add(c["so_target_hash"])
            family_components[c["ib_hash"]].append(c["component"])
        families_multi_so = sorted(
            fam for fam, targets in family_so.items() if len(targets - {""}) > 1
        )
        families_without_so = sorted(
            fam for fam, targets in family_so.items() if not (targets - {""})
        )
        # 门控放行：族内 SO 唯一（家族全部已落盘组件共享同一 SO 目标）且该 pass 已
        # join。已落盘集合就是本角色工作空间的全部组件，所以「家族全在目标集合内」
        # 在探针口径下恒真；放行数因此等价于「已 join 且家族自洽」的 pass 数。
        component_family = {c["component"]: c["ib_hash"] for c in comps}
        gate_pass_draws = sorted(
            rec["draw"]
            for rec in joined
            if all(
                component_family.get(name, "") not in families_multi_so
                for name in rec["components"]
            )
        )
        facts["gate"] = facts.get("gate") or {}
        facts["gate"][role] = {
            "so_targets_with_multiple_ib_families": gate_passes_multi_family,
            "deform_passes_with_multi_family_so": pass_so_multi_family,
            "families_with_multiple_so_targets": families_multi_so,
            "families_without_so_target": families_without_so,
            "family_count": len(family_so),
            "family_so_targets": {
                fam: sorted(targets - {""}) for fam, targets in sorted(family_so.items())
            },
            "so_consumer_families": {
                so: sorted(fams) for so, fams in sorted(so_families.items())
            },
            "gate_allowed_pass_count": len(gate_pass_draws),
            "gate_allowed_passes": gate_pass_draws,
        }

        # ---- 每个 join 上的组件做 palette / blend 实测 ----
        components: list[dict] = []
        for rec in joined:
            for c in by_key[(rec["blend_hash"], rec["blend_count"])]:
                pal = deduped / rec["palette_dump"]
                blend = read_blend(Path(c["blend_path"]))
                palinfo = (
                    read_palette(pal, blend.get("distinct_indices"))
                    if pal.exists()
                    else {"error": "palette dump missing"}
                )
                bones = palinfo.get("bones_h12")
                imax_all = blend.get("index_max_all", -1)
                imax_used = blend.get("index_max_used", -1)
                distinct = blend.get("distinct_indices", [])
                unreferenced = []
                if bones:
                    referenced = set(distinct)
                    unreferenced = [i for i in range(bones) if i not in referenced]
                components.append(
                    {
                        "role": role,
                        "submesh": c["submesh"],
                        "component": c["component"],
                        "ib_hash": c["ib_hash"],
                        "index_count": c["index_count"],
                        "match_first_index": c["match_first_index"],
                        "json_variant_count": c["json_variant_count"],
                        "json_variants": c["json_variants"],
                        "draw": rec["draw"],
                        "so_target": rec["so_target"],
                        "vs_hash": rec["vs_hash"],
                        "vertex_count_blend": c["vertex_count_blend"],
                        "vertex_count_position": c["vertex_count_position"],
                        "vertex_count_deform": rec["vertex_count"],
                        "vertex_counts_agree": c["vertex_count_blend"] == rec["vertex_count"],
                        "blend_hash": c["blend_hash"],
                        "blend_dump": rec["blend_dump"],
                        "position_hash": c["position_hash"],
                        "palette_hash": rec["palette_hash"],
                        "palette_dump": rec["palette_dump"],
                        "palette_dump_is_buf": rec["palette_dump_is_buf"],
                        "palette_bytes": palinfo.get("bytes"),
                        "palette_bones": bones,
                        "index_max_all": imax_all,
                        "index_max_used": imax_used,
                        "index_max_zero_weight": blend.get("index_max_zero_weight"),
                        "invariant_bones_eq_imax_plus_1": (
                            bones is not None and bones == imax_all + 1
                        ),
                        "unreferenced_slots": len(unreferenced),
                        "unreferenced_slots_sample": unreferenced[:16],
                        "distinct_index_count": len(distinct),
                        "_distinct_indices": distinct,
                        "weight_sum_unit_frac": blend.get("weight_sum_unit_frac"),
                        "weight_sum_abs_err_max": blend.get("weight_sum_abs_err_max"),
                        "channels_used_hist": blend.get("channels_used_hist"),
                        "blend_sha256": blend.get("sha256"),
                        "palette_sha256": palinfo.get("sha256"),
                        "palette_h12": palinfo.get("h12"),
                        "palette_h12_referenced": palinfo.get("h12_referenced"),
                        "palette_h16": palinfo.get("h16"),
                        "palette_divisible_by": palinfo.get("divisible_by"),
                        "blend_formats": c["blend_formats"],
                        "gpu_preskinning": c["gpu_preskinning"],
                        "vertex_limit_vb": c["so_target_hash"],
                        "so_target_hash_matches_workspace_vertex_limit": (
                            c["so_target_hash"] == rec["so_target"]
                        ),
                        "vertex_count_json": c["vertex_count_json"],
                        "index_count_json": c["index_count_json"],
                        "vertex_offset_json": c["vertex_offset_json"],
                        "index_offset_json": c["index_offset_json"],
                        "ib_file": c["ib_file"],
                        "work_game_type": c["work_game_type"],
                    }
                )

        # ---- ③ 同角色内跨组件按矩阵内容逐位去重 ----
        # 关键：HIMI 的 palette 是**角色级骨骼池**，池里含大量本组件不引用的槽。
        # 合并只能对「被引用的槽」做，否则会把池里别人/未用的骨骼也算进全局骨架。
        distinct_by_comp = {
            c["component"]: sorted(set(c.get("_distinct_indices") or []))
            for c in components
        }
        global_slots: dict[bytes, int] = {}
        slot_first: dict[int, str] = {}
        slot_seen_by: dict[int, set[str]] = defaultdict(set)
        per_component_maps: dict[str, dict] = {}
        for rec in joined:
            for c in by_key[(rec["blend_hash"], rec["blend_count"])]:
                pal = deduped / rec["palette_dump"]
                all_sigs = bone_signatures(pal)
                referenced = distinct_by_comp.get(c["component"], [])
                ref_set = set(referenced)
                mapping: dict[int, int] = {}
                for local in referenced:
                    if not (0 <= local < len(all_sigs)):
                        continue
                    sig = all_sigs[local]
                    g = global_slots.setdefault(sig, len(global_slots))
                    if g == len(global_slots) - 1:
                        slot_first[g] = f"{c['component']}#{local}"
                    slot_seen_by[g].add(c["component"])
                    mapping[local] = g
                per_component_maps[c["component"]] = {
                    "draw": rec["draw"],
                    "palette_dump": rec["palette_dump"],
                    "palette_pool_bones": len(all_sigs),
                    "referenced_slot_count": len(referenced),
                    "local_to_global": mapping,
                }

        # 池里未被任何组件引用的槽（不计入全局骨架）；同时判定这些槽是不是「填充」：
        # 若它们的旋转块也是「单位正交 × 同一缩放」（|det| = s³），说明它们是**真实骨骼**，
        # 只是本组件不引用 —— palette 是骨骼池，不是被 padding 的每组件 palette。
        pool_only_bones = 0
        pool_only_valid_bones = 0
        for rec in joined:
            for c in by_key[(rec["blend_hash"], rec["blend_count"])]:
                pal = deduped / rec["palette_dump"]
                raw = pal.read_bytes()
                all_sigs = bone_signatures(pal)
                ref_set = set(distinct_by_comp.get(c["component"], []))
                if len(raw) % PALETTE_STRIDE:
                    continue
                mat = (
                    np.frombuffer(raw, dtype=np.float32)
                    .reshape(-1, PALETTE_FLOATS)
                    .reshape(-1, 3, 4)
                    .astype(np.float64)
                )
                det = np.abs(np.linalg.det(mat[:, :, 0:3]))
                for local, sig in enumerate(all_sigs):
                    if local in ref_set or sig in global_slots:
                        continue
                    pool_only_bones += 1
                    if local < len(det):
                        sv = np.linalg.svd(mat[local, :, 0:3], compute_uv=False)
                        spread = (sv.max() - sv.min()) / sv.max() if sv.max() else 1.0
                        if abs(det[local] - sv.max() ** 3) / (sv.max() ** 3) < 1e-4 and spread < 1e-3:
                            pool_only_valid_bones += 1

        shared_slots = {
            str(s): sorted(v) for s, v in slot_seen_by.items() if len(v) > 1
        }
        # 同一份数据被拆成多个子网格：blend 内容 hash 相同
        by_blend: dict[str, list[str]] = defaultdict(list)
        for c in components:
            by_blend[c["blend_hash"]].append(c["component"])
        split_groups = {
            h: sorted(v) for h, v in by_blend.items() if len(v) > 1
        }
        # palette 内容完全相同的组件组（共享 VGMap 的更强证据）
        by_pal: dict[str, list[str]] = defaultdict(list)
        for c in components:
            by_pal[c["palette_sha256"]].append(c["component"])
        palette_shared_groups = {h: sorted(v) for h, v in by_pal.items() if len(v) > 1}
        # 同一份 deform 输入几何（Position.buf 字节相同）的组件组
        by_pos: dict[str, list[str]] = defaultdict(list)
        for c in components:
            by_pos[c["position_hash"]].append(c["component"])
        position_shared_groups = {h: sorted(v) for h, v in by_pos.items() if len(v) > 1}
        # 同一 SO 输出（VertexLimitVB == deform pass 的 SOSetTargets hash）被几个组件共用
        by_so: dict[str, list[str]] = defaultdict(list)
        for c in components:
            by_so[c["so_target"]].append(c["component"])
        so_shared_groups = {h: sorted(v) for h, v in by_so.items() if len(v) > 1}
        # 同一 IB（组件名前缀）被几个组件共用
        by_ib: dict[str, list[str]] = defaultdict(list)
        for c in components:
            by_ib[c["ib_hash"]].append(c["component"])
        ib_shared_groups = {h: sorted(v) for h, v in by_ib.items() if len(v) > 1}

        facts["roles"][role] = {
            "dump": dump,
            "dump_dir": str(fa),
            "draws_total": len(draws),
            "deform_pass_count": len(deform),
            "render_draw_count": len(render),
            "workspace_component_count": len(comps),
            "workspace_json_count": sum(c["json_variant_count"] for c in comps),
            "joined_components": len(components),
            "join_rate": f"{len(components)}/{len(comps)}",
            "deform_passes_joined": len(joined),
            "deform_passes_unjoined": len(unjoined),
            "components": components,
            "joined_passes": joined,
            "unjoined_passes": unjoined,
            "role_global_unique_bones": len(global_slots),
            "role_pool_only_bones": pool_only_bones,
            "role_pool_only_valid_bones": pool_only_valid_bones,
            "role_pool_only_all_valid": pool_only_bones == pool_only_valid_bones,
            "role_pool_slot_sum": sum(
                (c.get("palette_bones") or 0) for c in components
            ),
            "role_referenced_slot_sum": sum(
                c["distinct_index_count"] for c in components
            ),
            "role_slots_sum": sum(
                (c.get("palette_bones") or 0) for c in components
            ),
            "role_shared_slot_count": len(shared_slots),
            "role_shared_slots": shared_slots,
            "role_slot_first_source": {str(k): v for k, v in sorted(slot_first.items())},
            "per_component_local_to_global": per_component_maps,
            "split_submesh_groups": split_groups,
            "palette_identical_groups": palette_shared_groups,
            "position_identical_groups": position_shared_groups,
            "so_target_shared_groups": so_shared_groups,
            "ib_shared_groups": ib_shared_groups,
            "bone_signature_multiset": [
                hashlib.sha256(sig).hexdigest()[:16] for sig in global_slots
            ],
            # D3 反证用：渲染 pass 里 vs-t0 槽的 dump 扩展名（HIMI 应为 .dds）
            "_render_vst0_ext": {
                d: (
                    ".dds"
                    if str(e.get("t0", {}).get("buf", "")).lower().endswith(".dds")
                    else (".buf" if e.get("t0", {}).get("buf") else "")
                )
                for d, e in render.items()
            },
        }
        # 把签名原文留给跨角色比对用（不落 JSON，太大）
        facts["roles"][role]["_signatures"] = list(global_slots.keys())

    # ---- ④ 跨角色重合骨骼 ----
    sig_owner: dict[bytes, list[str]] = defaultdict(list)
    for role, rd in facts["roles"].items():
        for sig in rd["_signatures"]:
            sig_owner[sig].append(role)
    cross = {k: v for k, v in sig_owner.items() if len(v) > 1}
    # palette 级（整个 buf）跨角色重合
    pal_owner: dict[str, list[str]] = defaultdict(list)
    for role, rd in facts["roles"].items():
        for c in rd["components"]:
            if c.get("palette_sha256"):
                pal_owner[c["palette_sha256"]].append(f"{role}/{c['component']}")
    pal_cross = {k: sorted(v) for k, v in pal_owner.items() if len({x.split('/')[0] for x in v}) > 1}

    facts["cross_role"] = {
        "shared_bone_signature_count": len(cross),
        "shared_bone_signatures": [
            {"sha256_16": hashlib.sha256(k).hexdigest()[:16], "roles": sorted(v)}
            for k, v in list(cross.items())[:32]
        ],
        "shared_palette_buffer_count": len(pal_cross),
        "shared_palette_buffers": list(pal_cross.items())[:16],
    }

    # ---- ⑤ 合并总量与索引宽度 ----
    all_bones = sum(rd["role_global_unique_bones"] for rd in facts["roles"].values())
    union_sigs = len(sig_owner)
    per_role = {r: rd["role_global_unique_bones"] for r, rd in facts["roles"].items()}
    comp_peak = max(
        (c.get("palette_bones") or 0)
        for rd in facts["roles"].values()
        for c in rd["components"]
    )
    imax_all_peak = max(
        (c.get("index_max_all") or -1)
        for rd in facts["roles"].values()
        for c in rd["components"]
    )
    # ①b 关键修正：palette 可能是**多个组件共用同一个池**，此时单组件的
    # 「palette 骨骼数 == BLENDINDICES 最大值 + 1」不成立（池里留有别人用的槽），
    # 但对「组件在池里实际引用的槽」仍然成立（无填充）。两者都要报。
    pool_analysis: dict = {}
    for role, rd in facts["roles"].items():
        by_pal_used: dict[str, list[dict]] = defaultdict(list)
        for c in rd["components"]:
            by_pal_used[c["palette_dump"]].append(c)
        rows = []
        for pal, cs in sorted(by_pal_used.items()):
            pool_size = cs[0]["palette_bones"]
            per_comp = []
            for c in cs:
                idx = sorted(set(c.get("_distinct_indices") or []))
                holes = [i for i in range(0, idx[-1] + 1) if i not in set(idx)] if idx else []
                per_comp.append(
                    {
                        "component": c["component"],
                        "palette_bones": c["palette_bones"],
                        "imax_all": c["index_max_all"],
                        "used_slots": len(idx),
                        "used_min": idx[0] if idx else None,
                        "used_max": idx[-1] if idx else None,
                        "used_max_lt_pool_size": (idx[-1] < pool_size) if idx else True,
                        "interior_holes_below_max": len(holes),
                        "invariant_bones_eq_imax_plus_1": c["invariant_bones_eq_imax_plus_1"],
                        "invariant_used_max_eq_used_count_minus_1": (
                            (idx[-1] + 1 == len(idx)) if idx else True
                        ),
                    }
                )
            used = sorted({i for c in cs for i in (c.get("_distinct_indices") or [])})
            rows.append(
                {
                    "palette_dump": pal,
                    "palette_bones": pool_size,
                    "components": sorted(c["component"] for c in cs),
                    "component_count": len(cs),
                    "pool_used_slot_count": len(used),
                    "pool_max_used_index": used[-1] if used else -1,
                    "pool_exact_fit": pool_size == (used[-1] + 1 if used else -1),
                    "pool_trailing_unused": (
                        pool_size - 1 - used[-1] if used else pool_size
                    ),
                    "shared_pool": len(cs) > 1,
                    "per_component": per_comp,
                    "all_components_used_max_lt_pool_size": all(
                        p["used_max_lt_pool_size"] for p in per_comp
                    ),
                    "all_components_no_interior_holes": all(
                        p["interior_holes_below_max"] == 0 for p in per_comp
                    ),
                }
            )
        pool_analysis[role] = rows

    facts["palette_pool_analysis"] = {
        "per_role": pool_analysis,
        "single_component_pools_all_exact": all(
            r["pool_exact_fit"] for rows in pool_analysis.values() for r in rows
            if not r["shared_pool"]
        ),
        "all_pools_all_components_used_max_lt_pool_size": all(
            r["all_components_used_max_lt_pool_size"]
            for rows in pool_analysis.values()
            for r in rows
        ),
        "all_pools_all_components_no_interior_holes": all(
            r["all_components_no_interior_holes"]
            for rows in pool_analysis.values()
            for r in rows
        ),
        "shared_pools": [
            {
                "role": role,
                "palette_dump": r["palette_dump"],
                "palette_bones": r["palette_bones"],
                "components": r["components"],
                "pool_used_slot_count": r["pool_used_slot_count"],
                "pool_max_used_index": r["pool_max_used_index"],
                "pool_exact_fit": r["pool_exact_fit"],
            }
            for role, rows in pool_analysis.items()
            for r in rows
            if r["shared_pool"]
        ],
        "note": (
            "HIMI 的 vs-t0 是**按角色共享的骨骼池**（同一 deform pass 的多个组件共用同一个"
            " palette buffer；不同 deform pass 之间该 buffer 还会被重写）。因此单组件"
            "「palette 骨骼数 == BLENDINDICES 最大值 + 1」只在「池 = 该组件独占」时成立；"
            "池被多个组件共用、或池是按更大的 pass 尺寸分配时，池里会留下别的骨骼/别的"
            "组件的槽（尾部与中间空洞都实测到）。真正恒成立的不变量是：① 每个组件引用到的"
            "槽集合无填充（max(used) + 1 == |used|，即引用的是 0..max 的连续前缀）；"
            "② 每个组件的 max(BLENDINDICES) < 池大小（不会越界）。"
        ),
    }

    # ---- D3 断言：deform pass 的 vs-t0 必须是 Buffer(.buf)，不是渲染 pass 的 Texture2D(.dds)
    pal_ext = Counter(
        c["palette_dump_is_buf"] for rd in facts["roles"].values() for c in rd["components"]
    )
    facts["palette_slot_kind"] = {
        "palette_dumps_ending_in_buf": pal_ext.get(True, 0),
        "palette_dumps_not_buf": pal_ext.get(False, 0),
        "all_palettes_are_buffers": pal_ext.get(False, 0) == 0,
        "note": (
            "同一 vs-t0 槽在渲染 pass 绑 Texture2D(.dds)、在 deform pass 才绑 Buffer(.buf)。"
            "本探针先按结构判据认定 deform pass（SOSetTargets + 非索引 Draw(VertexCount) + "
            "vb0 stride 44 / vb1 stride 32 pointlist），再取该 pass 的 vs-t0，因此不会把"
            "渲染 pass 的 vs-t0 纹理当调色板。"
        ),
    }
    facts["merged"] = {
        "per_role_unique_bones": per_role,
        "sum_of_role_unique_bones": all_bones,
        "global_union_bones": union_sigs,
        "per_component_palette_peak": comp_peak,
        "index_max_all_peak": imax_all_peak,
        "fits_bi8_255": union_sigs <= 255,
        "fits_bi16_65535": union_sigs <= 65535,
        "fits_r32_sint": union_sigs < 2**31 - 1,
        "index_element_format": sorted(
            {
                c["blend_formats"].get("BLENDINDICES", "")
                for rd in facts["roles"].values()
                for c in rd["components"]
            }
        ),
        "weight_element_format": sorted(
            {
                c["blend_formats"].get("BLENDWEIGHTS", "")
                for rd in facts["roles"].values()
                for c in rd["components"]
            }
        ),
        "max_weights_per_vertex": max(
            (max(int(k) for k in (c.get("channels_used_hist") or {"0": 0}))
             for rd in facts["roles"].values()
             for c in rd["components"]),
            default=0,
        ),
        "vs_zzmi_reference": {
            "merged_total_zzmi": ZZMI_LIMITS["zzmi_merged_skeleton_total"],
            "headroom_to_374": ZZMI_LIMITS["zzmi_merged_skeleton_total"] - union_sigs,
            "per_component_peak_zzmi": ZZMI_LIMITS["zzmi_per_component_peak"],
            "himi_per_component_peak": comp_peak,
        },
        # 单角色口径 vs 五角色口径（ZZMI 的 374 是单角色合并骨架的槽位上限）
        "max_single_role_unique_bones": max(per_role.values()),
        "max_single_role_fits_374": max(per_role.values()) <= ZZMI_LIMITS["zzmi_merged_skeleton_total"],
        "max_single_role_headroom_to_374": ZZMI_LIMITS["zzmi_merged_skeleton_total"] - max(per_role.values()),
        "all_five_roles_in_one_skeleton": union_sigs,
        "all_five_roles_fits_374": union_sigs <= ZZMI_LIMITS["zzmi_merged_skeleton_total"],
        "all_five_roles_headroom_to_374": ZZMI_LIMITS["zzmi_merged_skeleton_total"] - union_sigs,
        "per_component_peak_fits_252": comp_peak <= ZZMI_LIMITS["zzmi_per_component_peak"],
        # D8（T3 实测）核对：索引/权重元素格式在所有组件上是否统一
        "index_format_histogram": dict(
            Counter(
                c["blend_formats"].get("BLENDINDICES", "")
                for rd in facts["roles"].values()
                for c in rd["components"]
            )
        ),
        "weight_format_histogram": dict(
            Counter(
                c["blend_formats"].get("BLENDWEIGHTS", "")
                for rd in facts["roles"].values()
                for c in rd["components"]
            )
        ),
        "component_total": sum(len(rd["components"]) for rd in facts["roles"].values()),
        "negative_index_any": any(
            (c.get("index_max_all") is not None and c["index_max_all"] < 0)
            for rd in facts["roles"].values()
            for c in rd["components"]
        ),
        "all_index_invariant_hold": all(
            c["invariant_bones_eq_imax_plus_1"]
            for rd in facts["roles"].values()
            for c in rd["components"]
        ),
        "all_palettes_are_buffers": facts["palette_slot_kind"]["all_palettes_are_buffers"],
        "no_extra_offset_fields": all(
            c["vertex_offset_json"] == 0
            and c["index_offset_json"] == 0
            and c["vertex_count_json"] == 0
            and c["index_count_json"] == 0
            for rd in facts["roles"].values()
            for c in rd["components"]
        ),
    }

    # ---- ⑥ join 判据证据 ----
    # 未 join 的 pass 属于同帧其他角色的判据：
    #  (a) 同帧内 deform pass 数 > 工作空间组件数（工作空间只提取了一个角色）
    #  (b) 未 join 的 pass 与已 join 的 pass 在 blend 内容 hash 上零交集
    #  (c) 未 join 的 pass 的 vb1 内容 hash 不出现在该角色任何组件的 CategoryHash.Blend
    #  (d) 未 join 的 pass 的 palette 内容 hash 与已 join 的零交集
    #  (e) 未 join 的 pass 的 SO 目标 hash 与已 join 的零交集
    #  (f) 每个 dump 内 deform pass 覆盖的 (顶点数, blend hash) 组合数 > 工作空间组件数
    evidence: dict = {}
    for role, rd in facts["roles"].items():
        joined_blend = {p["blend_hash"] for p in rd["joined_passes"]}
        joined_pal = {p["palette_dump"] for p in rd["joined_passes"]}
        joined_so = {p["so_target"] for p in rd["joined_passes"]}
        unj_blend = {p["blend_hash"] for p in rd["unjoined_passes"]}
        unj_pal = {p["palette_dump"] for p in rd["unjoined_passes"]}
        unj_so = {p["so_target"] for p in rd["unjoined_passes"]}
        ws_blend = {c["blend_hash"] for c in rd["components"]}
        evidence[role] = {
            "deform_passes": rd["deform_pass_count"],
            "joined_passes": rd["deform_passes_joined"],
            "unjoined_passes": rd["deform_passes_unjoined"],
            "workspace_components": rd["workspace_component_count"],
            "passes_exceed_components": rd["deform_pass_count"] > rd["workspace_component_count"],
            "blend_hash_overlap_joined_vs_unjoined": sorted(joined_blend & unj_blend),
            "palette_dump_overlap_joined_vs_unjoined": sorted(joined_pal & unj_pal),
            "so_target_overlap_joined_vs_unjoined": sorted(joined_so & unj_so),
            "unjoined_blend_hashes_in_workspace": sorted(unj_blend & ws_blend),
            "unjoined_vertex_counts": [p["vertex_count"] for p in rd["unjoined_passes"]],
            "unjoined_blend_hashes": sorted(unj_blend),
            "unjoined_palette_dumps": sorted(unj_pal),
            "unjoined_so_targets": sorted(unj_so),
        }
    facts["join_evidence"] = evidence

    # 判据 (g)：未 join 的 pass 的 (顶点数, blend hash) 组合，是否出现在**别的角色**的
    # 工作空间里（若出现，说明该 pass 属于那个角色 —— 同帧多角色的直接证据）
    ws_keys_by_role: dict[str, set[tuple[str, int]]] = {}
    for role, rd in facts["roles"].items():
        ws_keys_by_role[role] = {
            (c["blend_hash"], c["vertex_count_blend"]) for c in rd["components"]
        }
    for role, rd in facts["roles"].items():
        other_keys: set[tuple[str, int]] = set()
        for other, keys in ws_keys_by_role.items():
            if other != role:
                other_keys |= keys
        matches = [
            {
                "draw": p["draw"],
                "blend_hash": p["blend_hash"],
                "vertex_count": p["vertex_count"],
                "matches_other_role_workspace": (p["blend_hash"], p["vertex_count"])
                in other_keys,
            }
            for p in rd["unjoined_passes"]
        ]
        evidence[role]["unjoined_vs_other_roles"] = matches
        evidence[role]["unjoined_matching_other_role_count"] = sum(
            1 for m in matches if m["matches_other_role_workspace"]
        )
        # 同一 dump 内：未 join 的 (vertex_count, blend_hash) 组合与已 join 的是否互斥
        joined_keys = {
            (p["blend_hash"], p["vertex_count"]) for p in rd["joined_passes"]
        }
        unj_keys = {(p["blend_hash"], p["vertex_count"]) for p in rd["unjoined_passes"]}
        evidence[role]["joined_unjoined_key_disjoint"] = not (joined_keys & unj_keys)
        # 渲染 pass 里 vs-t0 的扩展名分布（D3 的反证）
        ext = Counter()
        for d, e in rd.get("_render_vst0_ext", {}).items():
            ext[e] += 1
        evidence[role]["render_pass_vst0_extensions"] = dict(ext)

    # ---- D3 断言（已在上面算好 facts["palette_slot_kind"]）----

    # ---- 共享 SO 输出：同一 deform pass 的 SO 被多个组件（不同 IB）复用 ----
    so_facts: dict = {}
    for role, rd in facts["roles"].items():
        so_facts[role] = {
            "so_targets_used_by_multiple_components": rd["so_target_shared_groups"],
            "ib_families_with_multiple_components": rd["ib_shared_groups"],
            "vertex_limit_vb_mismatches": [
                c["component"]
                for c in rd["components"]
                if not c["so_target_hash_matches_workspace_vertex_limit"]
            ],
            "component_index_segments": {
                ib: sorted(
                    (c["index_count"], c["match_first_index"])
                    for c in rd["components"]
                    if c["ib_hash"] == ib
                )
                for ib in sorted(rd["ib_shared_groups"])
            },
        }
    facts["shared_so_output"] = so_facts

    for rd in facts["roles"].values():
        rd.pop("_signatures", None)
        rd.pop("_render_vst0_ext", None)
        for c in rd["components"]:
            c.pop("_distinct_indices", None)
    return facts


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def _fmt(x, nd=6):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}g}"
    return str(x)


def render_text(f: dict) -> str:
    L: list[str] = []
    p = L.append
    p("=" * 100)
    p("HIMI (HI3) 骨骼合并实证探针 —— 一次跑出的全量数字")
    p("=" * 100)
    p("")
    p("角色 -> dump（取自各角色 Config/FrameAnalysisPath.json）")
    for role, dump in f["inputs"]["role_dump_map"].items():
        p(f"  {role:<22} -> {dump}")
    p("")

    for role, rd in f["roles"].items():
        p("-" * 100)
        p(f"### {role}   dump={rd['dump']}")
        p(
            f"    draws={rd['draws_total']}  deform pass={rd['deform_pass_count']}  "
            f"render draw={rd['render_draw_count']}  工作空间组件={rd['workspace_component_count']}"
            f"（json={rd['workspace_json_count']}）"
        )
        p(
            f"    join: 组件 {rd['join_rate']}  deform pass "
            f"{rd['deform_passes_joined']}/{rd['deform_pass_count']} 已 join"
        )
        p("")
        p("  ① 逐组件实测（组件名 = <ib>-<IndexCount>-<match_first_index>）")
        hdr = (
            f"    {'component':<26} {'j':>2} {'vc':>7} {'blend':>10} {'palette':>14} "
            f"{'bones':>6} {'imax':>5} {'imaxU':>6} {'==b-1':>6} {'unref':>6} "
            f"{'wsum1':>6} {'BIfmt':>20}"
        )
        p(hdr)
        for c in rd["components"]:
            p(
                f"    {c['component'][:26]:<26} {c['json_variant_count']:>2} "
                f"{c['vertex_count_blend']:>7} {c['blend_hash']:>10} {c['palette_dump']:>14} "
                f"{_fmt(c['palette_bones']):>6} {c['index_max_all']:>5} "
                f"{c['index_max_used']:>6} {str(c['invariant_bones_eq_imax_plus_1']):>6} "
                f"{c['unreferenced_slots']:>6} {_fmt(c['weight_sum_unit_frac'], 4):>6} "
                f"{c['blend_formats'].get('BLENDINDICES', ''):>20}"
            )
        p("")
        p("  ①b 调色板池（vs-t0 是共享骨骼池，不是每组件独占）")
        for r in f["palette_pool_analysis"]["per_role"].get(role, []):
            tag = "共享池" if r["shared_pool"] else "独占池"
            p(
                f"     [{tag}] palette={r['palette_dump']} 池大小={r['palette_bones']} "
                f"池引用槽数={r['pool_used_slot_count']} 池maxUsed={r['pool_max_used_index']} "
                f"池精确装下={r['pool_exact_fit']} 池尾部空槽={r['pool_trailing_unused']}"
            )
            for pc in r["per_component"]:
                p(
                    f"          {pc['component']:<26} 池={pc['palette_bones']:>4} "
                    f"imax={pc['imax_all']:>4} used={pc['used_slots']:>4} "
                    f"usedRange=[{pc['used_min']},{pc['used_max']}] "
                    f"越界={not pc['used_max_lt_pool_size']} "
                    f"内部空洞={pc['interior_holes_below_max']:>3} "
                    f"==池-1:{pc['invariant_bones_eq_imax_plus_1']!s:<5} "
                    f"used无填充:{pc['invariant_used_max_eq_used_count_minus_1']}"
                )
        p("")

        h12 = [c["palette_h12"] for c in rd["components"] if c.get("palette_h12")]
        if h12:
            scales = sorted({round(h["scale_median"], 4) for h in h12})
            p("  ② 调色板矩阵解释：12 float = 3x4（48 B/骨骼）")
            p(f"     每骨骼 3x3 块的中位奇异值（均匀缩放因子）= {scales}")
            p(
                f"     旋转行模长 |dev-1| 最大 = {_fmt(max(h['row_len_abs_dev_max'] for h in h12))}"
                f"   单位行占比最低 = {_fmt(min(h['row_unit_frac'] for h in h12))}"
            )
            p(
                f"     3x3 正交性 |A A^T - I| 最大 = {_fmt(max(h['ortho_err_max'] for h in h12))}"
                f"   达标骨骼占比最低 = {_fmt(min(h['orthonormal_frac'] for h in h12))}"
            )
            p(
                f"     剥掉均匀缩放后 |(A/s)(A/s)^T - I| 最大 = "
                f"{_fmt(max(h['ortho_err_after_descale_max'] for h in h12))}"
                f"   缩放相对离散度最大 = "
                f"{_fmt(max(h['uniform_scale_rel_spread'] for h in h12))}"
                f"   det/s^3 相对偏差最大 = "
                f"{_fmt(max(h['det_over_scale3_dev_max'] for h in h12))}"
            )
            p(
                f"     det / scale^3 相对偏差最大 = "
                f"{_fmt(max(h['det_over_scale3_dev_max'] for h in h12))}"
            )
            ref = [c["palette_h12_referenced"] for c in rd["components"]
                   if c.get("palette_h12_referenced")]
            if ref:
                p(
                    f"     仅被引用槽位：单位行占比最低 = "
                    f"{_fmt(min(r['row_unit_frac'] for r in ref))}"
                    f"   |dev-1| 最大 = {_fmt(max(r['row_len_abs_dev_max'] for r in ref))}"
                )
            rej = [c for c in rd["components"] if "rejected" in (c.get("palette_h16") or {})]
            p(f"     16 float = 4x4（64 B/骨骼）解释：{len(rej)}/{len(rd['components'])} 个 palette "
              f"字节数不被 64 整除 -> 结构性否决")
            p("")

        p("  ③ 同角色内跨组件逐位去重（只对**被引用的槽**去重）")
        p(
            f"     组件 palette 池大小之和 = {rd['role_pool_slot_sum']}  "
            f"组件引用槽数之和 = {rd['role_referenced_slot_sum']}  ->  "
            f"全局唯一骨骼 = {rd['role_global_unique_bones']}  "
            f"共享槽 = {rd['role_shared_slot_count']}  "
            f"池内未被任何组件引用的骨骼 = {rd['role_pool_only_bones']}"
            f"（其中也是合法骨骼矩阵的 = {rd['role_pool_only_valid_bones']}，"
            f"全合法 = {rd['role_pool_only_all_valid']}）"
        )
        for g, users in sorted(rd["split_submesh_groups"].items()):
            p(f"     同一份 Blend（hash={g}）被拆成 {len(users)} 个子网格: {', '.join(users)}")
        for h, users in sorted(rd["palette_identical_groups"].items()):
            p(f"     同一份 palette（sha256={h[:12]}）: {', '.join(users)}")
        for h, users in sorted(rd["position_identical_groups"].items()):
            p(f"     同一份 deform 输入几何（Position hash={h}）: {', '.join(users)}")
        for h, users in sorted(rd["so_target_shared_groups"].items()):
            p(f"     同一个 SO 输出（so={h}）被 {len(users)} 个组件共用: {', '.join(users)}")
        for h, users in sorted(rd["ib_shared_groups"].items()):
            p(f"     同一个 IB（ib={h}）被 {len(users)} 个组件分片: {', '.join(users)}")
        p("")

        p("  ⑥ deform pass 明细")
        for rec in rd["joined_passes"]:
            p(
                f"     [joined]   {rec['draw']} vc={rec['vertex_count']:<7} "
                f"vb1={rec['blend_hash']} t0={rec['palette_dump']}"
                f"{'' if rec['palette_dump_is_buf'] else '(!NOT .buf)'} so={rec['so_target']} "
                f"vs={rec['vs_hash']}"
            )
            p(f"                 -> {rec['components']}")
        for rec in rd["unjoined_passes"]:
            p(
                f"     [unjoined] {rec['draw']} vc={rec['vertex_count']:<7} "
                f"vb1={rec['blend_hash']} t0={rec['palette_dump']} so={rec['so_target']} "
                f"vs={rec['vs_hash']}"
            )
        p("")

    p("=" * 100)
    p("④ 跨角色重合骨骼")
    cr = f["cross_role"]
    p(f"   跨角色逐位相同的骨骼矩阵数 = {cr['shared_bone_signature_count']}")
    p(f"   跨角色完全相同的 palette buffer 数 = {cr['shared_palette_buffer_count']}")
    p("")
    p("=" * 100)
    p("⑤ 合并总量与索引宽度")
    m = f["merged"]
    for role, n in m["per_role_unique_bones"].items():
        p(f"   {role:<22} 唯一骨骼 = {n}")
    p(f"   各角色唯一骨骼之和            = {m['sum_of_role_unique_bones']}")
    p(f"   五角色并集（跨角色无重合时相等） = {m['global_union_bones']}")
    p(f"   单组件 palette 峰值            = {m['per_component_palette_peak']}")
    p(f"   BLENDINDICES 最大值（全组件）   = {m['index_max_all_peak']}")
    p(f"   索引元素格式 = {m['index_element_format']}")
    p(f"   权重元素格式 = {m['weight_element_format']}")
    p(f"   每顶点最大有效权重数 = {m['max_weights_per_vertex']}")
    p(f"   适配 8 位索引(<=255)  = {m['fits_bi8_255']}")
    p(f"   适配 16 位索引(<=65535) = {m['fits_bi16_65535']}")
    p(f"   适配 R32_SINT           = {m['fits_r32_sint']}")
    p(
        f"   单角色唯一骨骼最大值 = {m['max_single_role_unique_bones']}"
        f"  与 374 上限关系: 适配={m['max_single_role_fits_374']} "
        f"余量={m['max_single_role_headroom_to_374']}"
    )
    p(
        f"   五角色并集 {m['all_five_roles_in_one_skeleton']} 与 374 上限关系: "
        f"适配={m['all_five_roles_fits_374']} 缺口={-m['all_five_roles_headroom_to_374']}"
    )
    p(
        f"   单组件 palette 峰值 {m['per_component_palette_peak']} 与 252 上限关系: "
        f"适配={m['per_component_peak_fits_252']}"
    )
    p(f"   BLENDINDICES 格式直方图 = {m['index_format_histogram']}")
    p(f"   BLENDWEIGHTS 格式直方图 = {m['weight_format_histogram']}")
    p(f"   组件总数 = {m['component_total']}")
    p(f"   「palette 骨骼数 == imax+1」全组件成立 = {m['all_index_invariant_hold']}")
    p(f"   全部 palette 来自 .buf（非渲染 pass 的 .dds）= {m['all_palettes_are_buffers']}")
    p(f"   Vertex/Index Offset/Count 全为 0（match_first_index 恒 0）= {m['no_extra_offset_fields']}")
    p(f"   ZZMI 对照: {json.dumps(m['vs_zzmi_reference'], ensure_ascii=False)}")
    p("")
    p("=" * 100)
    p("⑥ join 判据证据")
    for role, ev in f["join_evidence"].items():
        p(f"   {role}")
        p(
            f"     deform pass={ev['deform_passes']} joined={ev['joined_passes']} "
            f"unjoined={ev['unjoined_passes']} 工作空间组件={ev['workspace_components']} "
            f"passes>components={ev['passes_exceed_components']}"
        )
        p(
            f"     已 join 与未 join 的 blend hash 交集 = "
            f"{ev['blend_hash_overlap_joined_vs_unjoined']}"
        )
        p(
            f"     已 join 与未 join 的 palette 交集    = "
            f"{ev['palette_dump_overlap_joined_vs_unjoined']}"
        )
        p(
            f"     已 join 与未 join 的 SO 目标交集     = "
            f"{ev['so_target_overlap_joined_vs_unjoined']}"
        )
        p(
            f"     未 join 的 blend hash 出现在工作空间的 = "
            f"{ev['unjoined_blend_hashes_in_workspace']}"
        )
        p(f"     未 join 的顶点数 = {ev['unjoined_vertex_counts']}")
        p(f"     未 join 的 blend hash = {ev['unjoined_blend_hashes']}")
        p(
            f"     未 join 的 (vc, blend) 与已 join 的互斥 = "
            f"{ev['joined_unjoined_key_disjoint']}"
        )
        p(
            f"     未 join 命中**其它角色**工作空间的条数 = "
            f"{ev['unjoined_matching_other_role_count']} / {len(ev['unjoined_vs_other_roles'])}"
        )
        p(
            f"     渲染 pass 里 vs-t0 槽的 dump 扩展名分布 = "
            f"{ev['render_pass_vst0_extensions']}"
        )
    p("")
    p("=" * 100)
    p("D3 调色板槽类型断言")
    pk = f["palette_slot_kind"]
    p(f"   palette dump 以 .buf 结尾的组件数 = {pk['palette_dumps_ending_in_buf']}")
    p(f"   palette dump 非 .buf 的组件数     = {pk['palette_dumps_not_buf']}")
    p(f"   全部是 Buffer（不是渲染 pass 的 Texture2D）= {pk['all_palettes_are_buffers']}")
    p("")
    p("=" * 100)
    p("共享 SO 输出（同一 deform pass 的 SO 被多个组件/IB 复用）")
    for role, so in f["shared_so_output"].items():
        p(f"   {role}")
        for h, users in sorted(so["so_targets_used_by_multiple_components"].items()):
            p(f"     so={h} -> {len(users)} 个组件: {', '.join(users)}")
        for ib, segs in sorted(so["component_index_segments"].items()):
            p(f"     ib={ib} 分片 (IndexCount, matchFirstIndex) = {segs}")
        if so["vertex_limit_vb_mismatches"]:
            p(f"     VertexLimitVB 与 deform SO 不匹配的组件: {so['vertex_limit_vb_mismatches']}")
    p("")
    return "\n".join(L)


def self_check(f: dict) -> tuple[bool, list[str]]:
    """对本报告依赖的不变量做一次机器判定；任一失败即退出码非 0。"""
    checks: list[tuple[str, bool, str]] = []
    m = f["merged"]

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    add(
        "① 每个组件的 max(BLENDINDICES) < palette 池大小（不越界）",
        f["palette_pool_analysis"]["all_pools_all_components_used_max_lt_pool_size"],
    )
    add(
        "① 工作空间顶点数与 deform pass Draw(VertexCount) 逐组件相等",
        all(
            c["vertex_counts_agree"]
            for rd in f["roles"].values()
            for c in rd["components"]
        ),
    )
    add(
        "① 工作空间 VertexLimitVB == deform pass 的 SOSetTargets hash",
        not any(
            c["vertex_limit_vb_mismatches"]
            for c in f["shared_so_output"].values()
        ),
    )
    add(
        "② 全部 palette 字节数被 48 整除（12 float = 3x4 解释）",
        all(
            c["palette_bones"] is not None
            for rd in f["roles"].values()
            for c in rd["components"]
        ),
    )
    add(
        "② 全部 palette 的 3x3 块是「标量 x 正交矩阵」",
        all(
            c["palette_h12"]["ortho_err_after_descale_max"] < 1e-3
            and c["palette_h12"]["uniform_scale_rel_spread"] < 1e-3
            for rd in f["roles"].values()
            for c in rd["components"]
            if c.get("palette_h12")
        ),
    )
    add(
        "② 至少一个 palette 的字节数不被 64 整除（16 float 解释被结构性否决）",
        any(
            "rejected" in (c.get("palette_h16") or {})
            for rd in f["roles"].values()
            for c in rd["components"]
        ),
    )
    add(
        "③ 池内未被引用的槽全部是合法骨骼矩阵（池不是 padding）",
        all(rd["role_pool_only_all_valid"] for rd in f["roles"].values()),
    )
    add(
        "③ 每组件引用槽集合长度 == distinct BLENDINDICES 数",
        all(
            len(rd["per_component_local_to_global"][c["component"]]["local_to_global"])
            == c["distinct_index_count"]
            for rd in f["roles"].values()
            for c in rd["components"]
        ),
    )
    add(
        "④ 跨角色骨骼逐位重合数 == 0",
        f["cross_role"]["shared_bone_signature_count"] == 0,
    )
    add(
        "④ 五角色并集 == 各角色唯一骨骼之和（跨角色无交集）",
        m["global_union_bones"] == m["sum_of_role_unique_bones"],
    )
    add("⑤ 索引/权重元素格式在 34 个组件上统一", len(m["index_format_histogram"]) == 1)
    add("⑤ 每顶点有效权重数 <= 4", m["max_weights_per_vertex"] <= 4)
    add("⑤ 索引全为非负（SINT 语义无符号性风险）", not m["negative_index_any"])
    add("⑤ 合并后总量适配 R32_SINT", m["fits_r32_sint"])
    add(
        "⑤ 单角色合并总量适配 374 槽位上限",
        m["max_single_role_fits_374"],
        f"max={m['max_single_role_unique_bones']} 余量={m['max_single_role_headroom_to_374']}",
    )
    add(
        "⑥ 工作空间组件全部 join 到 deform pass",
        all(
            rd["joined_components"] == rd["workspace_component_count"]
            for rd in f["roles"].values()
        ),
    )
    add(
        "⑥ 未 join 的 pass 与已 join 的在 blend hash / palette / SO 上零交集",
        all(
            not ev["blend_hash_overlap_joined_vs_unjoined"]
            and not ev["palette_dump_overlap_joined_vs_unjoined"]
            and not ev["so_target_overlap_joined_vs_unjoined"]
            for ev in f["join_evidence"].values()
        ),
    )
    add(
        "⑥ 未 join 的 pass 不命中任何角色的工作空间",
        all(
            ev["unjoined_matching_other_role_count"] == 0
            for ev in f["join_evidence"].values()
        ),
    )
    add(
        "D3 deform pass 的 vs-t0 全部是 Buffer(.buf)，不是渲染 pass 的 Texture2D(.dds)",
        f["palette_slot_kind"]["all_palettes_are_buffers"],
    )
    add(
        "补充 Vertex/Index Offset/Count 在全部组件上为 0（match_first_index 恒 0）",
        m["no_extra_offset_fields"],
    )

    # ---- G1 变形家族门控（T5 §2.8 / 计划书 IP-24；T6 并入）----
    gate = f.get("gate") or {}
    family_total = sum(int(entry.get("family_count", 0)) for entry in gate.values())
    allowed_total = sum(int(entry.get("gate_allowed_pass_count", 0)) for entry in gate.values())
    add(
        "G1 每个 deform pass 的 SO 目标只被单一 IB 家族的 render draw 消费（39/39）",
        not any(
            entry.get("deform_passes_with_multi_family_so")
            or entry.get("so_targets_with_multiple_ib_families")
            for entry in gate.values()
        ),
        f"多家族 SO 目标 = {sum(len(e.get('so_targets_with_multiple_ib_families') or []) for e in gate.values())}",
    )
    add(
        "G1 每个工作空间 IB 家族的组件共享同一 SO 目标（家族内 SO 唯一，18/18）",
        family_total == 18
        and not any(
            entry.get("families_with_multiple_so_targets")
            or entry.get("families_without_so_target")
            for entry in gate.values()
        ),
        f"家族数 = {family_total}",
    )
    add(
        "G1 家族独占门控放行 18/39 个 deform pass（= 全部已 join 的 pass）",
        allowed_total == 18
        and allowed_total
        == sum(len(rd.get("joined_passes") or []) for rd in f["roles"].values()),
        f"放行 = {allowed_total}",
    )

    lines = ["", "=" * 100, "自检（不变量判定）"]
    ok_all = True
    for name, ok, detail in checks:
        ok_all &= ok
        lines.append(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    lines.append(f"   总计 {sum(1 for _, ok, _ in checks if ok)}/{len(checks)} 通过")
    return ok_all, lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=None, help="把全量事实写到该 JSON")
    ap.add_argument("--quiet", action="store_true", help="只写 --json，不打印文本")
    ap.add_argument("--self-check", action="store_true", help="只跑不变量自检（退出码 = 是否全过）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    facts = build_facts()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
    if args.self_check:
        ok, lines = self_check(facts)
        print("\n".join(lines))
        return 0 if ok else 1
    if not args.quiet:
        print(render_text(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
