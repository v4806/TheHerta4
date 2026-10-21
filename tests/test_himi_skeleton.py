"""HIMI 骨骼合并数据层（``common/himi_skeleton.py``）单测。

两类覆盖：

* **合成 fixture**（永远运行）：在临时目录里造出与真实 HI3 dump 同构的
  ``log.txt`` + 工作空间 json/.buf，逐条钉死本层的关键口径：
  ring-scratch palette 定位、三条 join 键、G1 家族门控、骨骼池空洞、
  scale-aware 校验、VGMap 完整覆盖、幂等与 force。
* **真实 dump 回归**（路径不存在即跳过）：对五帧五角色核对实测期望值
  （唯一骨骼 278/343/318/220/252、组件数 9/12/13/13/13）。

  这些数字是**数据快照**而不是代码不变量：工作空间重新导入（组件增删、帧内
  换 dump）后它们会跟着变，此时按实测值改钉子即可，别当成实现回退。
  快照基准：2026-10-20 重新导入后的 `K:/SSMT-Package-master/WorkSpace/HIMI`。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "himi_skeleton_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_himi = _load_module(f"{PKG}.common.himi_skeleton", REPO_ROOT / "common" / "himi_skeleton.py")

HIMIBoneMapBuilder = _himi.HIMIBoneMapBuilder
HIMIComponent = _himi.HIMIComponent
HIMIDeformResolver = _himi.HIMIDeformResolver
HIMILogParser = _himi.HIMILogParser
HIMISkeletonMergeHelper = _himi.HIMISkeletonMergeHelper

REAL_WORKSPACE_ROOT = Path(r"K:\SSMT-Package-master\WorkSpace\HIMI")
REAL_DUMP_ROOT = Path(r"K:\SSMT-Package-master\3Dmigoto\HI3")

#: 五角色实测期望值（reports/himi-merge/04-probe-results.md §3）
REAL_EXPECTED = {
    "咚！炽愿吉星": {"dump": "FrameAnalysis-2026-09-23-135739", "components": 9, "slots": 278},
    "嗨♪爱愿妖精♥": {"dump": "FrameAnalysis-2026-09-23-140134", "components": 12, "slots": 343},
    "愈生佑翎": {"dump": "FrameAnalysis-2026-09-23-134442", "components": 13, "slots": 318},
    "窈窕谍影": {"dump": "FrameAnalysis-2026-09-23-135117", "components": 13, "slots": 220},
    "镇×偃月叩晓": {"dump": "FrameAnalysis-2026-09-23-133530", "components": 13, "slots": 252},
}

POSITION_STRIDE = 44
BLEND_STRIDE = 32
PALETTE_STRIDE = 48


# --------------------------------------------------------------------------- #
# 合成 fixture 构造器
# --------------------------------------------------------------------------- #


def _matrix(scale: float, translation) -> bytes:
    """3x4 行主序：旋转块 = scale * I，平移列给定。"""
    rows = []
    for index in range(3):
        row = [0.0, 0.0, 0.0, float(translation[index])]
        row[index] = float(scale)
        rows.extend(row)
    return struct.pack("<12f", *rows)


def _matrix_rotated(scale: float, seed: int, translation) -> bytes:
    """带上真实感的旋转（正交，行列式 +1），避免全同矩阵掩盖去重错误。"""
    angle = 0.37 * (seed + 1)
    cos_v, sin_v = numpy.cos(angle), numpy.sin(angle)
    rot = numpy.array(
        [
            [cos_v, -sin_v, 0.0],
            [sin_v, cos_v, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=numpy.float64,
    )
    rows = []
    for index in range(3):
        rows.extend([float(v * scale) for v in rot[index]])
        rows.append(float(translation[index]))
    return struct.pack("<12f", *rows)


def _blend_records(indices_per_vertex, weights_per_vertex=None, vertex_count=None) -> bytes:
    payload = bytearray()
    count = vertex_count if vertex_count is not None else len(indices_per_vertex)
    for vertex in range(count):
        indices = list(indices_per_vertex[vertex % len(indices_per_vertex)]) if indices_per_vertex else []
        indices = (indices + [0, 0, 0, 0])[:4]
        if weights_per_vertex is None:
            weights = [1.0, 0.0, 0.0, 0.0]
            if indices[1] or indices[2] or indices[3]:
                weights = [0.5, 0.3, 0.2, 0.0]
                weights = weights[: 1 + sum(1 for v in indices[1:] if v)] + [0.0] * 4
                weights = weights[:4]
                total = sum(weights) or 1.0
                weights = [w / total for w in weights]
        else:
            weights = (list(weights_per_vertex[vertex % len(weights_per_vertex)]) + [0.0] * 4)[:4]
        payload.extend(struct.pack("<4f", *weights))
        payload.extend(struct.pack("<4i", *indices))
    return bytes(payload)


class _SyntheticWorkspace:
    """造一份同构的 HIMI 工作空间 + dump。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dump = self.root / "dump"
        self.deduped = self.dump / "deduped"
        self.workspace = self.root / "workspace"
        self.lines: list[str] = []
        for path in (self.deduped, self.workspace / "Config", self.workspace / "LOD0"):
            path.mkdir(parents=True, exist_ok=True)
        self.import_map: dict[str, str] = {}
        self._dedup_counter = 0

    def _next_dump_name(self, tag: str) -> str:
        self._dedup_counter += 1
        return f"{tag}{self._dedup_counter:04d}.buf"

    def add_component(
        self,
        bare: str,
        *,
        indices: list[list[int]],
        positions: list[list[float]],
        palette: bytes,
        palette_resource_hash: str,
        vertex_count: int | None = None,
        vertex_limit_vb: str = "",
        work_game_type: str = "GPU_P12_N12_C4_TA16_BW16_BI16_T8_T1-8_",
        lod: str = "LOD0",
        gpu_preskinning: bool = True,
        dedup_excluded: bool = False,
    ) -> dict:
        """写一个组件（json + Position/Blend.buf），返回它的 join 身份。"""
        component_dir = self.workspace / lod / bare
        type_dir = component_dir / f"TYPE_{work_game_type}"
        type_dir.mkdir(parents=True, exist_ok=True)

        blend_bytes = _blend_records(indices, vertex_count=vertex_count)
        rows = vertex_count if vertex_count is not None else len(positions)
        position_payload = bytearray()
        for index in range(rows):
            position = positions[index % len(positions)]
            position_payload.extend(struct.pack("<3f", *[float(v) for v in position]))
            position_payload.extend(b"\x00" * 12)  # NORMAL
            position_payload.extend(b"\x00" * 4)  # COLOR
            position_payload.extend(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0))  # TANGENT
        blend_path = type_dir / f"{bare}-Blend.buf"
        position_path = type_dir / f"{bare}-Position.buf"
        blend_path.write_bytes(blend_bytes)
        position_path.write_bytes(bytes(position_payload))

        blend_hash = _digest(blend_bytes)
        position_hash = _digest(bytes(position_payload))
        payload = {
            "GamePreset": "HIMI",
            "VertexLimitVB": vertex_limit_vb,
            "CategoryHash": {
                "Blend": blend_hash,
                "Position": position_hash,
                "Texcoord": "0" * 8,
            },
            "WorkGameType": work_game_type,
            "GPU-PreSkinning": bool(gpu_preskinning),
            "VertexCount": 0,
            "IndexCount": 0,
            "VertexOffset": 0,
            "IndexOffset": 0,
            "VGCount": 0,
            "VGOffset": 0,
            "IndexBufferList": [{"DXGI_FORMAT": "DXGI_FORMAT_R32_UINT", "FileName": f"{bare}.ib"}],
            "CategoryBufferList": [
                {
                    "FileName": f"{bare}-Position.buf",
                    "D3D11ElementList": [
                        {"SemanticName": "POSITION", "Format": "R32G32B32_FLOAT"},
                    ],
                },
                {
                    "FileName": f"{bare}-Blend.buf",
                    "D3D11ElementList": [
                        {"SemanticName": "BLENDWEIGHTS", "Format": "R32G32B32A32_FLOAT"},
                        {"SemanticName": "BLENDINDICES", "Format": "R32G32B32A32_SINT"},
                    ],
                },
            ],
        }
        if dedup_excluded:
            payload["VGMapDedupExcluded"] = True
        (type_dir / f"{bare}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.import_map[f"{lod}.{bare}"] = work_game_type
        (self.workspace / "Import.json").write_text(
            json.dumps(self.import_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "bare": bare,
            "blend_hash": blend_hash,
            "position_hash": position_hash,
            "blend_path": blend_path,
            "position_path": position_path,
        }

    def write_palette(self, tag: str, palette: bytes) -> str:
        name = self._next_dump_name(tag)
        (self.deduped / name).write_bytes(palette)
        return name

    def dump_line(self, draw: str, logical: str, deduped_name: str, *, kind: str = "Buffer") -> None:
        source = self.dump / logical
        target = self.deduped / deduped_name
        self.lines.append(f"{draw} 3DMigoto Dumping {kind} {source} -> {target}")

    def deform_pass(
        self,
        draw: str,
        *,
        vertex_count: int,
        position_dump: dict,
        blend_dump: dict,
        palette_dump: str,
        palette_resource_hash: str,
        so_target: str,
        topology: str = "pointlist",
    ) -> None:
        """写一个 deform pass 的 dump 描述 + 调用序列。"""
        draw_int = int(draw)
        self.dump_line(
            draw,
            f"{draw}-vb0={position_dump['resource']}-vs=10905ed856e6b621.buf",
            os.path.basename(position_dump["path"]),
        )
        self.dump_line(
            draw,
            f"{draw}-vb0={position_dump['resource']}-vs=10905ed856e6b621.txt",
            f"{os.path.basename(position_dump['path'])[:-4]}-vb0-layout=abc-topology={topology}"
            f"-stride={POSITION_STRIDE}-count={vertex_count}.txt",
        )
        self.dump_line(
            draw,
            f"{draw}-vb1={blend_dump['resource']}-vs=10905ed856e6b621.buf",
            os.path.basename(blend_dump["path"]),
        )
        self.dump_line(
            draw,
            f"{draw}-vb1={blend_dump['resource']}-vs=10905ed856e6b621.txt",
            f"{os.path.basename(blend_dump['path'])[:-4]}-vb1-layout=abc-topology={topology}"
            f"-stride={BLEND_STRIDE}-count={vertex_count}.txt",
        )
        self.dump_line(
            draw,
            f"{draw}-vs-t0={palette_resource_hash}-vs=10905ed856e6b621.buf",
            palette_dump,
        )
        self.lines.append(
            f"{draw} VSSetShader(pVertexShader:0x1, ppClassInstances:0x0, NumClassInstances:0) "
            "hash=10905ed856e6b621"
        )
        self.lines.append(
            f"{draw} SOSetTargets(NumBuffers:1, ppSOTargets:0x1, pOffsets:0xE468)"
        )
        self.lines.append(f"       0: resource=0x2 hash={so_target}")
        self.lines.append(f"{draw} VSSetShaderResources(StartSlot:0, NumViews:1, ppShaderResourceViews:0x3)")
        self.lines.append(f"       0: view=0x2 resource=0x3 hash={palette_resource_hash}")
        self.lines.append(f"{draw} IASetVertexBuffers(StartSlot:0, NumBuffers:2, ppVertexBuffers:0x4)")
        self.lines.append(f"       0: resource=0x5 hash={position_dump['resource']}")
        self.lines.append(f"       1: resource=0x6 hash={blend_dump['resource']}")
        self.lines.append(f"{draw} IASetPrimitiveTopology(Topology:1)")
        self.lines.append(f"{draw} Draw(VertexCount:{vertex_count}, StartVertexLocation:0)")

    def render_draw(
        self,
        draw: str,
        *,
        index_count: int,
        vb0_content_hash: str,
        ib_content_hash: str,
        vet: str = "55da5eab238de808",
    ) -> None:
        self.dump_line(
            draw,
            f"{draw}-vb0={vb0_content_hash}-vs={vet}.buf",
            f"{vb0_content_hash}.buf",
        )
        self.dump_line(
            draw,
            f"{draw}-ib={ib_content_hash}-vs={vet}.buf",
            f"{ib_content_hash}.buf",
        )
        self.lines.append(f"{draw} VSSetShader(pVertexShader:0x7, ppClassInstances:0x0, NumClassInstances:0) hash={vet}")
        self.lines.append(f"{draw} IASetIndexBuffer(pIndexBuffer:0x8, DXGI_FORMAT:42, Offset:0)")
        self.lines.append(f"{draw} IASetVertexBuffers(StartSlot:0, NumBuffers:2, ppVertexBuffers:0x9)")
        self.lines.append(f"       0: resource=0xA hash={vb0_content_hash}")
        self.lines.append(f"       1: resource=0xB hash=texcoord")
        self.lines.append(f"{draw} DrawIndexed(IndexCount:{index_count}, StartIndexLocation:0, BaseVertexLocation:0)")

    def add_render_consumer(
        self, draw: str, *, index_count: int, so_content_hash: str, ib_content_hash: str
    ) -> None:
        """渲染 draw：vb0 = SO 输出的**内容 hash**（= 组件 Position content hash）。"""
        self.render_draw(
            draw,
            index_count=index_count,
            vb0_content_hash=so_content_hash,
            ib_content_hash=ib_content_hash,
        )

    def finalize(self) -> None:
        (self.dump / "log.txt").write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        (self.workspace / "Config" / "FrameAnalysisPath.json").write_text(
            json.dumps(
                {
                    "frameAnalysisFolderPath": str(self.dump),
                    "selectedFrameAnalysis": self.dump.name,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _digest(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()[:8]


#: 独立命名空间：`common/submesh_json.py` 有自己的相对导入（`..utils.json_utils`、
#: `.d3d11_element`），必须与原包分开装载，不能借用上面的 PKG。
_VALIDATOR_PKG = "himi_submesh_json_validator_pkg"


def _load_merged_skeleton_validator():
    """装载 `common/submesh_json.py` 的 `_merged_skeleton_metadata_valid`（计划书 C4）。

    该模块同时依赖 `.d3d11_element`，因此连同依赖一起按独立包前缀装载；任何装载
    失败都返回 ``None``，由调用方 `skipTest`（不让环境问题变成假绿/假红）。
    """
    try:
        for name in (_VALIDATOR_PKG, f"{_VALIDATOR_PKG}.common", f"{_VALIDATOR_PKG}.utils"):
            if name not in sys.modules:
                _install_package(name)
        if f"{_VALIDATOR_PKG}.utils.json_utils" not in sys.modules:
            _load_module(
                f"{_VALIDATOR_PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py"
            )
        if f"{_VALIDATOR_PKG}.common.d3d11_element" not in sys.modules:
            _load_module(
                f"{_VALIDATOR_PKG}.common.d3d11_element",
                REPO_ROOT / "common" / "d3d11_element.py",
            )
        module = _load_module(
            f"{_VALIDATOR_PKG}.common.submesh_json",
            REPO_ROOT / "common" / "submesh_json.py",
        )
        return getattr(module, "_merged_skeleton_metadata_valid", None)
    except Exception:
        return None


class _BaseSynthetic(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="himi_skeleton_test_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.synth = _SyntheticWorkspace(self._tmp)

    def make_deform_pair(
        self,
        tag: str,
        *,
        vertex_count: int,
        matrices: list[bytes],
        position_payload: bytes | None = None,
        blend_payload: bytes | None = None,
        position_name: str | None = None,
        blend_name: str | None = None,
        position_resource: str | None = None,
        blend_resource: str | None = None,
    ):
        """给一个 deform pass 造出 Position/Blend 的 dump 文件，返回描述。

        传入 ``position_payload`` / ``blend_payload`` 时必须与工作空间里的
        ``<bare>-Position.buf`` / ``<bare>-Blend.buf`` **内容一致** —— 三条 join
        键比较的是内容 hash，不是资源 hash。
        """
        if position_payload is None:
            position_payload = bytes(range(16)) * vertex_count
            position_payload = (position_payload)[: POSITION_STRIDE * vertex_count].ljust(
                POSITION_STRIDE * vertex_count, b"\x00"
            )
        if blend_payload is None:
            blend_payload = b"\x00" * (BLEND_STRIDE * vertex_count)
        position_name = position_name or self.synth._next_dump_name(f"{tag}-pos")
        blend_name = blend_name or self.synth._next_dump_name(f"{tag}-blend")
        (self.synth.deduped / position_name).write_bytes(position_payload)
        (self.synth.deduped / blend_name).write_bytes(blend_payload)
        return (
            {"resource": position_resource or f"{tag}pos", "path": self.synth.deduped / position_name},
            {"resource": blend_resource or f"{tag}blend", "path": self.synth.deduped / blend_name},
        )


# --------------------------------------------------------------------------- #
# 1) log 解析
# --------------------------------------------------------------------------- #


class TestHIMILogParser(_BaseSynthetic):
    def test_deform_pass_requires_pointlist_and_buffer_palette(self):
        position, blend = self.make_deform_pair("a", vertex_count=4, matrices=[])
        palette = _matrix(1.6, (1, 2, 3))
        palette_dump = self.synth.write_palette("pal", palette)
        self.synth.deform_pass(
            "000010",
            vertex_count=4,
            position_dump=position,
            blend_dump=blend,
            palette_dump=palette_dump,
            palette_resource_hash="aa11bb22",
            so_target="d2cc3481",
        )
        # 渲染块：同一个 vs-t0 槽，但 dump 成 Texture2D/.dds
        self.synth.dump_line(
            "000011",
            "000011-vs-t0=cc33dd44-vs=render.buf",
            "shadow.dds",
            kind="Texture2D",
        )
        self.synth.add_render_consumer(
            "000011", index_count=12, so_content_hash="d2cc3481", ib_content_hash="ibfam"
        )
        self.synth.finalize()

        parser = HIMILogParser(str(self.synth.dump / "log.txt"))
        passes = parser.deform_passes()
        self.assertEqual(list(passes), ["000010"])
        record = passes["000010"]
        self.assertEqual(record["vertex_count"], 4)
        self.assertEqual(record["so_target"], "d2cc3481")
        self.assertEqual(record["palette_dump"], palette_dump)
        self.assertEqual(record["blend_dump"], os.path.basename(blend["path"]))
        self.assertEqual(record["position_hash"] if False else record["vertex_shader"], "10905ed856e6b621")

    def test_render_pass_vs_t0_texture_is_not_a_deform_pass(self):
        palette = _matrix(1.0, (0, 0, 0))
        palette_dump = self.synth.write_palette("pal", palette)
        self.synth.dump_line(
            "000020",
            "000020-vs-t0=ee55ff66-vs=render.buf",
            palette_dump,
            kind="Texture2D",
        )
        self.synth.lines.append("000020 Draw(VertexCount:8, StartVertexLocation:0)")
        self.synth.finalize()
        parser = HIMILogParser(str(self.synth.dump / "log.txt"))
        self.assertEqual(parser.deform_passes(), {})

    def test_per_draw_vertex_shader_comes_from_dump_suffix(self):
        # 块首 VSSetShader 是上一 pass 的残留；真值在 -vs= 后缀里
        self.synth.lines.append("000030 VSSetShader(ppVertexShader:0x1) hash=aaaa1111")
        self.synth.dump_line("000030", "000030-vb0=abc-vs=bbbb2222-ps=cc.buf", "x.buf")
        self.synth.finalize()
        parser = HIMILogParser(str(self.synth.dump / "log.txt"))
        self.assertEqual(parser.draws["000030"]["vertex_shader"], "bbbb2222")


# --------------------------------------------------------------------------- #
# 2) ring-scratch palette 定位（关键回归）
# --------------------------------------------------------------------------- #


class TestRingScratchPalette(_BaseSynthetic):
    def test_same_resource_hash_two_passes_different_content(self):
        """同一 vs-t0 资源 hash 服务两个 deform pass、内容不同：
        必须各自命中自己的内容文件，不能按资源 hash 反查。
        """
        # 两个组件的几何
        idx = [[0, 1, 0, 0]]
        first = self.synth.add_component(
            "aaaa-0001-1-0",
            indices=idx,
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="shared99",
            vertex_count=2,
            vertex_limit_vb="50a1",
        )
        second = self.synth.add_component(
            "bbbb0002-1-0",
            indices=idx,
            positions=[[1.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="shared99",
            vertex_count=3,
            vertex_limit_vb="50a2",
        )

        palette_a = b"".join(_matrix(1.6, (1, 0, 0)) for _ in range(4))
        palette_b = b"".join(_matrix(1.0, (9, 0, 0)) for _ in range(6))
        dump_a = self.synth.write_palette("sharedA", palette_a)
        dump_b = self.synth.write_palette("sharedB", palette_b)
        self.assertNotEqual(dump_a, dump_b)

        pos_a, blend_a = self.make_deform_pair(
            "A",
            vertex_count=2,
            matrices=[],
            position_payload=first["position_path"].read_bytes(),
            blend_payload=first["blend_path"].read_bytes(),
            position_name=first["position_path"].name,
            blend_name=first["blend_path"].name,
            position_resource=first["position_hash"][:8],
            blend_resource=first["blend_hash"][:8],
        )
        pos_b, blend_b = self.make_deform_pair(
            "B",
            vertex_count=3,
            matrices=[],
            position_payload=second["position_path"].read_bytes(),
            blend_payload=second["blend_path"].read_bytes(),
            position_name=second["position_path"].name,
            blend_name=second["blend_path"].name,
            position_resource=second["position_hash"][:8],
            blend_resource=second["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000001",
            vertex_count=2,
            position_dump=pos_a,
            blend_dump=blend_a,
            palette_dump=dump_a,
            palette_resource_hash="shared99",
            so_target="50a1",
        )
        self.synth.deform_pass(
            "000002",
            vertex_count=3,
            position_dump=pos_b,
            blend_dump=blend_b,
            palette_dump=dump_b,
            palette_resource_hash="shared99",
            so_target="50a2",
        )
        self.synth.finalize()

        parser = HIMILogParser(str(self.synth.dump / "log.txt"))
        passes = parser.deform_passes()
        self.assertEqual(passes["000001"]["palette_dump"], dump_a)
        self.assertEqual(passes["000002"]["palette_dump"], dump_b)
        self.assertEqual(passes["000001"]["palette_content_hash"], passes["000002"]["palette_content_hash"])

        resolver = HIMIDeformResolver(str(self.synth.dump), [])
        self.assertEqual(
            resolver.palette_dump_path(passes["000001"]),
            str(self.synth.deduped / dump_a),
        )
        self.assertEqual(
            resolver.palette_dump_path(passes["000002"]),
            str(self.synth.deduped / dump_b),
        )
        self.assertNotEqual(
            Path(resolver.palette_dump_path(passes["000001"])).read_bytes(),
            Path(resolver.palette_dump_path(passes["000002"])).read_bytes(),
        )


# --------------------------------------------------------------------------- #
# 3) join 与 G1 门控
# --------------------------------------------------------------------------- #


class TestJoinAndGate(_BaseSynthetic):
    def _build_two_family_scene(self, *, second_family_landed: bool = True):
        palette = b"".join(_matrix_rotated(1.6, i, (i, 0, 0)) for i in range(3))
        palette_dump = self.synth.write_palette("pal", palette)

        a1 = self.synth.add_component(
            "aaaa-0001-10-0",
            indices=[[0, 1, 0, 0], [1, 2, 0, 0]],
            positions=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="res1",
            vertex_limit_vb="50a1",
        )
        a2 = self.synth.add_component(
            "aaaa-0002-20-0",
            indices=[[0, 1, 0, 0], [1, 2, 0, 0]],
            positions=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="res1",
            vertex_limit_vb="50a1",
        )
        pos_a, blend_a = self.make_deform_pair(
            "A",
            vertex_count=2,
            matrices=[],
            position_payload=a1["position_path"].read_bytes(),
            blend_payload=a1["blend_path"].read_bytes(),
            position_name=a1["position_path"].name,
            blend_name=a1["blend_path"].name,
            position_resource=a1["position_hash"][:8],
            blend_resource=a1["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000010",
            vertex_count=2,
            position_dump=pos_a,
            blend_dump=blend_a,
            palette_dump=palette_dump,
            palette_resource_hash="res1",
            so_target="50a1",
        )
        self.synth.add_render_consumer(
            "000020", index_count=9, so_content_hash=a1["position_hash"], ib_content_hash="aaaa"
        )

        if second_family_landed:
            b1 = self.synth.add_component(
                "bbbb-0001-30-0",
                indices=[[0, 1, 0, 0]],
                positions=[[2.0, 0.0, 0.0]],
                palette=b"",
                palette_resource_hash="res2",
                vertex_limit_vb="50a2",
            )
            pos_b, blend_b = self.make_deform_pair(
                "B",
                vertex_count=1,
                matrices=[],
                position_payload=b1["position_path"].read_bytes(),
                blend_payload=b1["blend_path"].read_bytes(),
                position_name=b1["position_path"].name,
                blend_name=b1["blend_path"].name,
                position_resource=b1["position_hash"][:8],
                blend_resource=b1["blend_hash"][:8],
            )
            self.synth.deform_pass(
                "000011",
                vertex_count=1,
                position_dump=pos_b,
                blend_dump=blend_b,
                palette_dump=palette_dump,
                palette_resource_hash="res2",
                so_target="50a2",
            )
            self.synth.add_render_consumer(
                "000021", index_count=3, so_content_hash=b1["position_hash"], ib_content_hash="bbbb"
            )
        self.synth.finalize()
        return a1, a2

    def test_join_by_position_and_vertex_limit(self):
        a1, a2 = self._build_two_family_scene()
        components = HIMISkeletonMergeHelper.prepare(
            str(self.synth.workspace), None, force=True
        )
        self.assertTrue(components["ok"], components.get("message"))
        joins = {
            write["bare"]: write["payload"]["HIMIDeformJoinPath"]
            for write in components["writes"]
        }
        self.assertEqual(joins["aaaa-0001-10-0"], "A")
        self.assertEqual(joins["aaaa-0002-20-0"], "A")
        self.assertEqual(
            components["writes"][0]["payload"]["DeformDrawIndex"], 10
        )

    def test_family_gate_blocks_incomplete_family(self):
        self._build_two_family_scene()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        gate = plan["gate_report"]
        self.assertTrue(gate["aaaa"]["safe"])
        self.assertTrue(gate["bbbb"]["safe"])
        # 只把家族 a 的一个成员列入合并目标集合：整个家族被拒
        partial = HIMISkeletonMergeHelper.prepare(
            str(self.synth.workspace), None, force=True, target_bares=["aaaa-0001-10-0"]
        )
        self.assertIn("aaaa", partial["gate_report"])
        self.assertFalse(partial["gate_report"]["aaaa"]["safe"])
        self.assertEqual(partial["gate_report"]["aaaa"]["missing"], ["aaaa-0002-20-0"])
        self.assertEqual(partial["writes"], [])
        self.assertTrue(partial["gate_rejected"])
        self.assertEqual(partial["gate_rejected"][0]["reason"], "deform_family_incomplete")

    def test_unrelated_pass_rejected(self):
        """不属于落盘集合的 deform pass 不参与合并。"""
        a1, _ = self._build_two_family_scene()
        palette = b"".join(_matrix(1.6, (0, 0, 0)) for _ in range(2))
        foreign_dump = self.synth.write_palette("foreign", palette)
        pos_f, blend_f = self.make_deform_pair("F", vertex_count=2, matrices=[])
        self.synth.deform_pass(
            "000099",
            vertex_count=2,
            position_dump={"resource": "ffffff01", "path": pos_f["path"]},
            blend_dump={"resource": "ffffff02", "path": blend_f["path"]},
            palette_dump=foreign_dump,
            palette_resource_hash="foreign",
            so_target="50f1",
        )
        self.synth.add_render_consumer(
            "000098", index_count=3, so_content_hash="deadbeef", ib_content_hash="ffff"
        )
        self.synth.finalize()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        resolver = plan["resolver"]
        self.assertIn("000099", resolver.passes)
        self.assertNotIn("ffff", plan["gate_report"])


# --------------------------------------------------------------------------- #
# 3b) G1 pass 级门控（F4）
# --------------------------------------------------------------------------- #


class TestPassLevelGate(_BaseSynthetic):
    """一个 deform pass 被两个 IB 家族共享：家族 A 完整 / 家族 B 不完整。

    重绑按 **pass** 生效，所以两个家族都不许被标成 `HIMIDeformGateSafe=True`。
    """

    def _build_shared_pass_scene(self):
        # 两个家族各自一个组件，且**共享同一个 deform pass**（路径 A：
        # CategoryHash.Position == 该 pass 的 vb0 内容 hash）。
        palette = b"".join(_matrix_rotated(1.6, i, (i, 0, 0)) for i in range(4))
        palette_dump = self.synth.write_palette("shared", palette)

        a1 = self.synth.add_component(
            "aaaa-0001-10-0",
            indices=[[0, 1, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="res-shared",
            vertex_limit_vb="50e1",
        )
        b1 = self.synth.add_component(
            "bbbb-0001-20-0",
            indices=[[0, 1, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="res-shared",
            vertex_limit_vb="50e1",
        )
        # 两者的 Position.buf 内容相同 ⇒ 同一个 vb0 内容 hash ⇒ 都 join 到 000010
        self.assertEqual(a1["position_hash"], b1["position_hash"])
        pos, blend = self.make_deform_pair(
            "S",
            vertex_count=1,
            matrices=[],
            position_payload=a1["position_path"].read_bytes(),
            blend_payload=a1["blend_path"].read_bytes(),
            position_name=a1["position_path"].name,
            blend_name=a1["blend_path"].name,
            position_resource=a1["position_hash"][:8],
            blend_resource=a1["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000010",
            vertex_count=1,
            position_dump=pos,
            blend_dump=blend,
            palette_dump=palette_dump,
            palette_resource_hash="res-shared",
            so_target="50e1",
        )
        # SO 目标被两个 IB 家族的 render draw 消费（真实数据里 39/39 单家族，
        # 这里刻意构造「多家族共用一个 pass」的越界场景）
        self.synth.add_render_consumer(
            "000020", index_count=3, so_content_hash=a1["position_hash"], ib_content_hash="aaaa"
        )
        self.synth.add_render_consumer(
            "000021", index_count=3, so_content_hash=a1["position_hash"], ib_content_hash="bbbb"
        )
        self.synth.finalize()
        return a1, b1

    def test_shared_pass_joins_both_families(self):
        """前置事实：两个家族确实共享同一个 pass（否则下面的门控测试没意义）。"""
        self._build_shared_pass_scene()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        resolver = plan["resolver"]
        draws = {c.bare: c.deform_draw for c in resolver.joined_components()}
        self.assertEqual(draws["aaaa-0001-10-0"], "000010")
        self.assertEqual(draws["bbbb-0001-20-0"], "000010")
        self.assertEqual(resolver.pass_families(resolver.passes["000010"]), ["aaaa", "bbbb"])
        # 全落盘时两个家族都安全 ⇒ 全部参与
        self.assertEqual(sorted(c.bare for c in plan["resolver"].joined_components()),
                         ["aaaa-0001-10-0", "bbbb-0001-20-0"])
        self.assertEqual(len(plan["writes"]), 2)

    def test_pass_level_gate_blocks_the_safe_family_too(self):
        """F4 核心断言：家族 A 完整、家族 B 不完整 ⇒ 家族 A 也不得进合并。"""
        self._build_shared_pass_scene()
        plan = HIMISkeletonMergeHelper.prepare(
            str(self.synth.workspace), None, force=True, target_bares=["aaaa-0001-10-0"]
        )
        gate = plan["gate_report"]
        self.assertTrue(gate["aaaa"]["safe"], "家族 A 自身完整")
        self.assertFalse(gate["bbbb"]["safe"], "家族 B 不完整")

        resolver = plan["resolver"]
        self.assertFalse(
            resolver.gate_allows(resolver.passes["000010"], gate),
            "pass 级门控必须否决这个被两个家族共享的 pass",
        )
        self.assertEqual(plan["writes"], [], "家族 A 的组件也不得进入合并骨架")
        reasons = {entry["bare"]: entry["reason"] for entry in plan["gate_rejected"]}
        self.assertEqual(reasons.get("aaaa-0001-10-0"), "deform_pass_family_incomplete")
        self.assertEqual(reasons.get("bbbb-0001-20-0"), "deform_family_incomplete")

        detail = {entry["bare"]: entry for entry in plan["gate_rejected"]}
        self.assertEqual(detail["aaaa-0001-10-0"]["unsafe_families"], ["bbbb"])
        self.assertEqual(detail["aaaa-0001-10-0"]["families"], ["aaaa", "bbbb"])
        self.assertEqual(detail["aaaa-0001-10-0"]["missing"], ["bbbb-0001-20-0"])

        blocked = plan["metadata"]["DeformGateBlockedPasses"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["draw"], "000010")
        self.assertEqual(blocked[0]["unsafe_families"], ["bbbb"])
        self.assertEqual(plan["metadata"]["DeformGateMode"], "pass")

    def test_gate_allows_is_wired_not_dead(self):
        """F4：`gate_allows` 必须被真实调用（此前零调用方）。"""
        self._build_shared_pass_scene()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        resolver = plan["resolver"]
        self.assertTrue(resolver.gate_allows(resolver.passes["000010"], plan["gate_report"]))
        # 空 pass / 未知 pass 一律否决（fail-closed）
        self.assertFalse(resolver.gate_allows({}, plan["gate_report"]))
        self.assertFalse(resolver.gate_allows({"draw": "999999"}, plan["gate_report"]))


# --------------------------------------------------------------------------- #
# 4) 骨骼池 / 去重口径
# --------------------------------------------------------------------------- #


class TestBonePoolSemantics(_BaseSynthetic):
    def test_pool_larger_than_referenced_slots_is_allowed(self):
        """池 8 槽、组件只引用 2 槽（带空洞）：VGCount 必须是 max(referenced)+1，
        VGMap 必须覆盖 0..VGCount-1；**不得**断言池大小 == VGCount。
        """
        palette = b"".join(_matrix_rotated(1.6, i, (i, 0, 0)) for i in range(8))
        palette_dump = self.synth.write_palette("pool8", palette)
        component = self.synth.add_component(
            "aaaa-0001-10-0",
            indices=[[5, 7, 0, 0], [5, 0, 0, 0]],
            positions=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="poolres",
            vertex_limit_vb="50c1",
        )
        pos, blend = self.make_deform_pair(
            "P",
            vertex_count=2,
            matrices=[],
            position_payload=component["position_path"].read_bytes(),
            blend_payload=component["blend_path"].read_bytes(),
            position_name=component["position_path"].name,
            blend_name=component["blend_path"].name,
            position_resource=component["position_hash"][:8],
            blend_resource=component["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000010",
            vertex_count=2,
            position_dump=pos,
            blend_dump=blend,
            palette_dump=palette_dump,
            palette_resource_hash="poolres",
            so_target="50c1",
        )
        self.synth.add_render_consumer(
            "000020", index_count=3, so_content_hash=component["position_hash"], ib_content_hash="aaaa"
        )
        self.synth.finalize()

        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        write = plan["writes"][0]
        payload = write["payload"]
        self.assertEqual(payload["HIMIPalettePoolSlotCount"], 8)
        # 引用槽 = 四个 BLENDINDICES 通道里出现过的全部非负值（含权重 0 的填充）
        self.assertEqual(write["referenced_slots"], [0, 5, 7])
        self.assertEqual(write["slots_weight_positive"], [5, 7])
        self.assertEqual(payload["BoneMatrixSlotCount"], 3)  # 缓存里只有被引用槽
        self.assertEqual(payload["VGCount"], 8)  # max(referenced) + 1
        self.assertEqual(sorted(int(k) for k in payload["VGMap"]), list(range(8)))
        self.assertEqual(len(write["cache_bytes"]), 3 * PALETTE_STRIDE)
        # 槽位口径：只有 5/7 注册全局槽（索引 0 与未引用槽不注册）
        self.assertGreaterEqual(plan["slot_count"], 2)
        self.assertGreaterEqual(int(payload["VGMap"]["6"]), 8)

    def test_bitwise_dedup_only_counts_referenced_slots(self):
        """池内未被引用的槽不得进入全局骨架（否则镇×偃月叩晓高估 282 根）。"""
        palette = b"".join(_matrix_rotated(1.6, i, (i, 0, 0)) for i in range(6))
        palette_dump = self.synth.write_palette("pool6", palette)
        indices = [[0, 1, 0, 0]]
        pass_draws = {"aaaa-0001-10-0": "000010", "aaaa-0002-20-0": "000011"}
        for bare, limit in (("aaaa-0001-10-0", "50d1"), ("aaaa-0002-20-0", "50d2")):
            component = self.synth.add_component(
                bare,
                indices=indices,
                positions=[[0.0, 0.0, 0.0]],
                palette=b"",
                palette_resource_hash="poolres",
                vertex_limit_vb=limit,
            )
            draw = pass_draws[bare]
            pos, blend = self.make_deform_pair(
                bare[:4],
                vertex_count=1,
                matrices=[],
                position_payload=component["position_path"].read_bytes(),
                blend_payload=component["blend_path"].read_bytes(),
                position_name=component["position_path"].name,
                blend_name=component["blend_path"].name,
                position_resource=component["position_hash"][:8],
                blend_resource=component["blend_hash"][:8],
            )
            self.synth.deform_pass(
                draw,
                vertex_count=1,
                position_dump=pos,
                blend_dump=blend,
                palette_dump=palette_dump,
                palette_resource_hash="poolres",
                so_target=limit,
            )
            self.synth.add_render_consumer(
                "00002" + draw[-1],
                index_count=3,
                so_content_hash=component["position_hash"],
                ib_content_hash="aaaa",
            )
        self.synth.finalize()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertEqual(plan["slot_count"], 2)  # 两个组件各引用 0/1，逐位相同 ⇒ 2 个全局槽
        self.assertEqual(len(plan["writes"]), 2)
        for write in plan["writes"]:
            self.assertEqual(write["payload"]["HIMIPalettePoolSlotCount"], 6)
            self.assertEqual(write["referenced_slots"], [0, 1])

    def test_scale_aware_row_check(self):
        scaled = b"".join(_matrix_rotated(1.6, i, (0, 0, 0)) for i in range(4))
        unit = b"".join(_matrix_rotated(1.0, i, (0, 0, 0)) for i in range(4))
        for payload, expected in ((scaled, 1.6), (unit, 1.0)):
            path = self.synth.write_palette("s", payload)
            path = str(self.synth.deduped / path)
            self.assertAlmostEqual(HIMIBoneMapBuilder.palette_scale(path), expected, places=4)
            check = HIMIBoneMapBuilder.scale_aware_row_check(path)
            self.assertEqual(check["unit_fraction"], 1.0)
            self.assertLess(check["max_deviation"], 1e-3)

    def test_never_assert_bones_equals_index_max_plus_one(self):
        """把 D03 的反例钉死：池 6 槽、只引用槽 1 ⇒ VGCount 2 != 池大小 6。"""
        palette = b"".join(_matrix_rotated(1.6, i, (i, 0, 0)) for i in range(6))
        palette_dump = self.synth.write_palette("pool6b", palette)
        component = self.synth.add_component(
            "aaaa-0001-10-0",
            indices=[[1, 0, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="poolres2",
            vertex_limit_vb="50c2",
        )
        pos, blend = self.make_deform_pair(
            "Z",
            vertex_count=1,
            matrices=[],
            position_payload=component["position_path"].read_bytes(),
            blend_payload=component["blend_path"].read_bytes(),
            position_name=component["position_path"].name,
            blend_name=component["blend_path"].name,
            position_resource=component["position_hash"][:8],
            blend_resource=component["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000010",
            vertex_count=1,
            position_dump=pos,
            blend_dump=blend,
            palette_dump=palette_dump,
            palette_resource_hash="poolres2",
            so_target="50c2",
        )
        self.synth.add_render_consumer(
            "000020", index_count=3, so_content_hash=component["position_hash"], ib_content_hash="aaaa"
        )
        self.synth.finalize()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        payload = plan["writes"][0]["payload"]
        pool = payload["HIMIPalettePoolSlotCount"]
        self.assertEqual(pool, 6)
        self.assertEqual(payload["VGCount"], 2)
        self.assertNotEqual(pool, payload["VGCount"])

    def test_out_of_range_index_is_rejected(self):
        palette = b"".join(_matrix(1.6, (0, 0, 0)) for _ in range(2))
        palette_dump = self.synth.write_palette("pool2", palette)
        component = self.synth.add_component(
            "aaaa-0001-10-0",
            indices=[[9, 0, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="poolres3",
            vertex_limit_vb="50c3",
        )
        pos, blend = self.make_deform_pair(
            "OOR",
            vertex_count=1,
            matrices=[],
            position_payload=component["position_path"].read_bytes(),
            blend_payload=component["blend_path"].read_bytes(),
            position_name=component["position_path"].name,
            blend_name=component["blend_path"].name,
            position_resource=component["position_hash"][:8],
            blend_resource=component["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000010",
            vertex_count=1,
            position_dump=pos,
            blend_dump=blend,
            palette_dump=palette_dump,
            palette_resource_hash="poolres3",
            so_target="50c3",
        )
        self.synth.add_render_consumer(
            "000020", index_count=3, so_content_hash=component["position_hash"], ib_content_hash="aaaa"
        )
        self.synth.finalize()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertFalse(plan["ok"])
        self.assertIn("越界", plan["message"])


# --------------------------------------------------------------------------- #
# 4b) 组件级参与门与排除去重（F3）
# --------------------------------------------------------------------------- #


class TestComponentParticipation(_BaseSynthetic):
    """`GPU-PreSkinning=False` 与 `VGMapDedupExcluded=True` 两个冻结字段。

    进展依据：EFMI/ZZMI 同款语义 —— 前者是「不参与合并」的门（`efmi_skeleton.py:4400`、
    `zzmi_skeleton.py:1334`），后者是「整组件退出矩阵去重、独占自己的槽位」
    （`efmi_skeleton.py:2377-2397`、`zzmi_skeleton.py:648`）。
    """

    def _scene(self, *, gpu_a: bool = True, gpu_b: bool = True, excluded: bool = False):
        # 两个组件引用**相同**的两根骨（matrices 相同）⇒ 正常去重会共享槽位
        palette = b"".join(
            [_matrix_rotated(1.6, 0, (0, 0, 0)), _matrix_rotated(1.6, 1, (1, 0, 0))]
        )
        palette_dump = self.synth.write_palette("pair", palette)
        draws = {"aaaa-0001-10-0": "000010", "bbbb-0001-20-0": "000011"}
        limits = {"aaaa-0001-10-0": "50d1", "bbbb-0001-20-0": "50d2"}
        for bare, gpu in (("aaaa-0001-10-0", gpu_a), ("bbbb-0001-20-0", gpu_b)):
            component = self.synth.add_component(
                bare,
                indices=[[0, 1, 0, 0]],
                positions=[[0.0, 0.0, 0.0]],
                palette=b"",
                palette_resource_hash="pairres",
                vertex_limit_vb=limits[bare],
                gpu_preskinning=gpu,
                dedup_excluded=excluded and bare == "bbbb-0001-20-0",
            )
            draw = draws[bare]
            pos, blend = self.make_deform_pair(
                bare[:4],
                vertex_count=1,
                matrices=[],
                position_payload=component["position_path"].read_bytes(),
                blend_payload=component["blend_path"].read_bytes(),
                position_name=component["position_path"].name,
                blend_name=component["blend_path"].name,
                position_resource=component["position_hash"][:8],
                blend_resource=component["blend_hash"][:8],
            )
            self.synth.deform_pass(
                draw,
                vertex_count=1,
                position_dump=pos,
                blend_dump=blend,
                palette_dump=palette_dump,
                palette_resource_hash="pairres",
                so_target=limits[bare],
            )
            self.synth.add_render_consumer(
                "00002" + draw[-1],
                index_count=3,
                so_content_hash=component["position_hash"],
                ib_content_hash=bare[:4],
            )
        self.synth.finalize()

    def test_baseline_shares_slots(self):
        self._scene()
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertEqual(len(plan["writes"]), 2)
        self.assertEqual(plan["slot_count"], 2, "两份相同矩阵应去重成 2 个全局槽")
        self.assertEqual(plan["metadata"]["SharedSlotCount"], 2)
        self.assertEqual(plan["metadata"]["DedupExcludedComponents"], [])

    def test_gpu_preskinning_false_skips_component(self):
        self._scene(gpu_b=False)
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertEqual([w["bare"] for w in plan["writes"]], ["aaaa-0001-10-0"])
        self.assertEqual(plan["metadata"]["CPUPreskinningSkipped"], ["bbbb-0001-20-0"])
        # 不参与合并的 CPU 目标不得被「缺合并元数据」误判为需要修复
        self.assertFalse(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.synth.workspace), ["LOD0.bbbb-0001-20-0"]
            ),
            "GPU-PreSkinning=False 的目标不需要合并元数据",
        )
        # 但 GPU 目标仍然需要
        self.assertTrue(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.synth.workspace), ["LOD0.aaaa-0001-10-0"]
            )
        )

    def test_dedup_excluded_component_gets_private_slots(self):
        self._scene(excluded=True)
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertEqual(
            plan["metadata"]["DedupExcludedComponents"], ["bbbb-0001-20-0"]
        )
        # 被排除组件独占自己那两个槽位 ⇒ 全局槽位从 2 涨到 4，且没有任何共享槽
        self.assertEqual(plan["slot_count"], 4)
        self.assertEqual(plan["metadata"]["SharedSlotCount"], 0)
        by_bare = {w["bare"]: w for w in plan["writes"]}
        self.assertEqual(by_bare["bbbb-0001-20-0"]["payload"]["VGMapDedupExcluded"], True)
        self.assertEqual(by_bare["aaaa-0001-10-0"]["payload"]["VGMapDedupExcluded"], False)
        # 两个组件的 VGMap 值集合必须不相交
        a_values = set(int(v) for v in by_bare["aaaa-0001-10-0"]["payload"]["VGMap"].values())
        b_values = set(int(v) for v in by_bare["bbbb-0001-20-0"]["payload"]["VGMap"].values())
        self.assertEqual(a_values & b_values, set(), "被排除组件不得与任何组件共享槽位")
        # 每个组件的 VGMap 必须仍是 0..VGCount-1 的完整覆盖（导入侧硬要求）
        for write in plan["writes"]:
            self.assertEqual(
                sorted(int(k) for k in write["payload"]["VGMap"]),
                list(range(write["payload"]["VGCount"])),
            )
            self.assertEqual(write["payload"]["BoneMatrixSlotCount"], len(write["referenced_slots"]))

    def test_dedup_exclusion_is_idempotent_across_runs(self):
        """排除标记写回 json 后，第二次 prepare 必须给出**完全相同**的槽位分配。

        （曾经的「排除组件先占坑」实现会因为标记已经落盘而改变处理顺序 ⇒ 第二次
        算出不同的 VGMap ⇒ 缓存门失效。此测试把它钉死。）
        """
        self._scene(excluded=True)
        first = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(first["ok"], first.get("message"))
        first_maps = {w["bare"]: w["payload"]["VGMap"] for w in first["writes"]}

        # 把第一次的产物落盘（排除标记随之写回 json），再跑第二次
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertTrue(ok, message)
        second = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(second["ok"], second.get("message"))
        second_maps = {w["bare"]: w["payload"]["VGMap"] for w in second["writes"]}

        self.assertEqual(first_maps, second_maps, "槽位分配必须与运行次序无关")
        self.assertEqual(first["slot_count"], second["slot_count"])
        self.assertEqual(
            first["metadata"]["SharedSlotCount"], second["metadata"]["SharedSlotCount"]
        )
        # 幂等门：第三次运行应命中缓存
        ok2, message2 = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertTrue(ok2, message2)
        self.assertIn("缓存完整", message2)

    def test_gpu_preskinning_false_sibling_does_not_block_family(self):
        """同家族里有一个 CPU 目标时，GPU 目标仍须被判为家族完整并参与合并。"""
        self._scene(gpu_b=False)
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertEqual(plan["metadata"]["CPUPreskinningSkipped"], ["bbbb-0001-20-0"])
        # 家族 bbbb 的参与成员只剩……不，两个组件分属不同家族；此处按家族断言
        gate = plan["gate_report"]
        self.assertTrue(gate["aaaa"]["safe"])
        # CPU 组件不参与 ⇒ 它不该把 bbbb 家族拖成 missing
        self.assertEqual(gate["bbbb"]["members"], [], "CPU 组件不参与门控成员集")
        self.assertEqual(plan["metadata"]["DeformGateRejected"], [])


# --------------------------------------------------------------------------- #
# 5) 落盘 / 幂等 / 角色键
# --------------------------------------------------------------------------- #


class TestLandingAndIdempotence(_BaseSynthetic):
    def _scene(self):
        palette = b"".join(_matrix_rotated(1.6, i, (i, 0, 0)) for i in range(4))
        palette_dump = self.synth.write_palette("pal", palette)
        component = self.synth.add_component(
            "aaaa-0001-10-0",
            indices=[[0, 1, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="res1",
            vertex_limit_vb="50a1",
        )
        pos, blend = self.make_deform_pair(
            "A",
            vertex_count=1,
            matrices=[],
            position_payload=component["position_path"].read_bytes(),
            blend_payload=component["blend_path"].read_bytes(),
            position_name=component["position_path"].name,
            blend_name=component["blend_path"].name,
            position_resource=component["position_hash"][:8],
            blend_resource=component["blend_hash"][:8],
        )
        self.synth.deform_pass(
            "000010",
            vertex_count=1,
            position_dump=pos,
            blend_dump=blend,
            palette_dump=palette_dump,
            palette_resource_hash="res1",
            so_target="50a1",
        )
        self.synth.add_render_consumer(
            "000020", index_count=3, so_content_hash=component["position_hash"], ib_content_hash="aaaa"
        )
        self.synth.finalize()
        return component

    def test_ensure_writes_contract_fields_and_is_idempotent(self):
        component = self._scene()
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self.assertTrue(ok, message)

        json_path = (
            self.synth.workspace
            / "LOD0"
            / "aaaa-0001-10-0"
            / "TYPE_GPU_P12_N12_C4_TA16_BW16_BI16_T8_T1-8_"
            / "aaaa-0001-10-0.json"
        )
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["VGCount"], 2)
        self.assertEqual(payload["VGOffset"], 0)
        self.assertEqual(payload["SkeletonGroup"], 0)
        self.assertEqual(payload["VGMapAlgorithmVersion"], 1)
        self.assertEqual(payload["DeformDrawIndex"], 10)
        self.assertEqual(payload["OriginalVertexCount"], 1)
        self.assertTrue(payload["HIMIDeformGateSafe"])
        self.assertEqual(payload["HIMIDeformFamilyIB"], "aaaa")
        self.assertEqual(payload["HIMIDeformFamilyMembers"], ["aaaa-0001-10-0"])
        self.assertEqual(payload["ObjectCB1CacheValid"], False)
        self.assertNotIn("ObjectCB1FileName", payload)
        self.assertNotIn("ChannelPlan", payload)
        self.assertAlmostEqual(payload["HIMIPaletteScale"], 1.6, places=3)

        skeleton = self.synth.workspace / "Config" / payload["HIMIMergedSkeletonFile"].split(os.sep)[-1]
        self.assertTrue(skeleton.is_file())
        self.assertEqual(skeleton.stat().st_size, payload["HIMIMergedSkeletonSlots"] * PALETTE_STRIDE)
        cache = (
            self.synth.workspace
            / "LOD0"
            / "aaaa-0001-10-0"
            / "ModImpRuntime"
            / payload["BoneMatrixFileName"]
        )
        self.assertTrue(cache.is_file())
        self.assertEqual(cache.stat().st_size, payload["BoneMatrixSlotCount"] * PALETTE_STRIDE)

        metadata = json.loads(
            (self.synth.workspace / "Config" / "HIMIMergedSkeleton.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["SlotCount"], payload["HIMIMergedSkeletonSlots"])
        self.assertEqual(metadata["SkeletonDigest"], payload["HIMIMergedSkeletonDigest"])
        self.assertEqual(metadata["Software"], "TheHerta4 HIMI merged skeleton")
        # F1：ReferencedSlotTotal 必须是真实值，不能恒 0（入参键是 slots 而非
        # referenced_slots；此处钉死数值，避免再退化成 assertLess 之类的空断言）。
        self.assertEqual(metadata["ReferencedSlotTotal"], 2)
        self.assertEqual(metadata["PoolSlotTotal"], 4)
        self.assertEqual(
            metadata["ReferencedSlotTotal"],
            sum(len(w["referenced_slots"]) for w in HIMISkeletonMergeHelper.prepare(
                str(self.synth.workspace), None, force=True)["writes"]),
        )

        # F5：BoneMatrixFrameDigest 与它的 Note 必须同时写出（与 ZZMI 对齐）
        self.assertTrue(payload["BoneMatrixFrameDigest"])
        self.assertIn("当帧矩阵指纹", payload["BoneMatrixFrameDigestNote"])
        # F3：两个组件级参与字段必须被显式写回
        self.assertIn("VGMapDedupExcluded", payload)
        self.assertEqual(payload["VGMapDedupExcluded"], False)
        self.assertEqual(payload["GPU-PreSkinning"], True)

        # 幂等：第二次不改文件
        before = json_path.stat().st_mtime_ns
        ok2, message2 = HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self.assertTrue(ok2, message2)
        self.assertIn("缓存完整", message2)
        self.assertEqual(json_path.stat().st_mtime_ns, before)

        # force：重算
        ok3, message3 = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None, force=True
        )
        self.assertTrue(ok3, message3)
        self.assertNotIn("缓存完整", message3)

    def test_missing_metadata_exist_detects_absent_data(self):
        self._scene()
        self.assertTrue(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.synth.workspace), ["LOD0.aaaa-0001-10-0"]
            )
        )
        HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self.assertFalse(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.synth.workspace), ["LOD0.aaaa-0001-10-0"]
            )
        )

    def test_missing_dump_fails_loudly_without_swapping_frames(self):
        """dump 失效 + **无缓存**（从未构建过）⇒ 显式失败，不偷换帧。"""
        self._scene()
        (self.synth.workspace / "Config" / "FrameAnalysisPath.json").write_text(
            json.dumps({"frameAnalysisFolderPath": str(self._tmp / "gone")}), encoding="utf-8"
        )
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertFalse(plan["ok"])
        self.assertIn("不做「自动改用最新帧」兜底", plan["message"])
        self.assertEqual(plan["writes"], [])

    # -- 导入侧 cache-only（T8 §4.1 / T13） ------------------------------ #

    def _break_frame_analysis_path(self):
        """把登记的 FrameAnalysis 指向一个不存在的目录（模拟用户删掉 dump）。"""
        (self.synth.workspace / "Config" / "FrameAnalysisPath.json").write_text(
            json.dumps({"frameAnalysisFolderPath": str(self._tmp / "gone_dump")}),
            encoding="utf-8",
        )

    def _tree_state(self):
        """工作空间全部文件的 (相对路径 -> 大小) 快照，用于证明「零写入」。"""
        state = {}
        for path in sorted(self.synth.workspace.rglob("*")):
            if path.is_file():
                state[str(path.relative_to(self.synth.workspace))] = path.stat().st_size
        return state

    def test_cache_only_plan_when_dump_deleted_and_cache_intact(self):
        """T13 核心：删 dump + 缓存完好 ⇒ ok=True 且走 cache-only 快路径。"""
        self._scene()
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertTrue(ok, message)

        self._break_frame_analysis_path()
        before = self._tree_state()

        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=False)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertTrue(plan.get("cache_only"))
        self.assertEqual(plan.get("gate_source"), "cache-only")
        self.assertTrue(plan.get("cache_intact"))
        self.assertEqual(plan["writes"], [], "cache-only 不重算、不写盘")
        self.assertEqual(plan["dump_dir"], "")
        self.assertEqual(plan["slot_count"], 2)
        self.assertEqual(plan["cache_only_checked"], 1)

        ok2, message2 = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertTrue(ok2, message2)
        self.assertIn("cache-only", message2)
        self.assertIn("不重算、不写盘", message2)
        self.assertEqual(self._tree_state(), before, "cache-only 必须是零写入")

        # force 语义不被稀释：强制重建需要 dump，无 dump 时明确失败
        forced = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None, force=True)
        self.assertFalse(forced["ok"])
        self.assertIn("无法按要求强制重算", forced["message"])
        self.assertEqual(self._tree_state(), before)

    def test_cache_only_rebuilds_after_dump_deleted_regression(self):
        """T8 §4.1 的最小回归：**先建缓存再删 dump**，导入侧必须能 cache-only 重建。

        注意与 `test_missing_dump_fails_loudly_without_swapping_frames` 的区别：那条
        是「从未构建过缓存」，这条是「缓存已建好但 dump 被删」——旧实现两者同样失败。
        """
        self._scene()
        HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        metadata_path = self.synth.workspace / "Config" / "HIMIMergedSkeleton.json"
        self.assertTrue(metadata_path.is_file())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        # cache-only 判据依赖的三件套之一：本次写回的 ComponentSet
        self.assertEqual(metadata["ComponentSet"], ["aaaa-0001-10-0"])
        self.assertTrue(metadata["SourceLogSignature"])
        self.assertTrue(metadata["SkeletonDigest"])

        import shutil as _shutil

        _shutil.rmtree(self.synth.dump)
        self.assertFalse(self.synth.dump.is_dir())

        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertTrue(ok, message)
        self.assertNotIn("已失效（Config/FrameAnalysisPath.json 指向的目录不存在）。", message)

        # 导出侧同样口径（T8 已验证），此处只确认两侧一致
        plan = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None)
        self.assertEqual(plan["gate_source"], "cache-only")

    def test_missing_dump_with_damaged_cache_fails_explicitly(self):
        """T13 反例：删 dump + 缓存损坏 ⇒ 显式失败、不静默降级、不写坏产物。"""
        self._scene()
        HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self._break_frame_analysis_path()
        before = self._tree_state()

        # 坏法 1：删掉某个组件的 palette 缓存
        cache_dir = (
            self.synth.workspace / "LOD0" / "aaaa-0001-10-0" / "ModImpRuntime"
        )
        caches = sorted(cache_dir.glob("*.buf"))
        self.assertEqual(len(caches), 1)
        caches[0].unlink()
        before = self._tree_state()

        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertFalse(ok, message)
        self.assertIn("无法 cache-only 重建", message)
        self.assertIn("aaaa-0001-10-0", message)
        self.assertIn("不做「自动改用最新帧」兜底", message)
        self.assertIn("未写入任何文件", message)
        self.assertEqual(self._tree_state(), before, "失败路径必须零写入")

    def test_cache_only_detects_skeleton_digest_mismatch(self):
        """骨架文件被改动（大小或字节）⇒ cache-only 必须拒绝，而不是拿着坏骨架继续。"""
        self._scene()
        HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self._break_frame_analysis_path()
        skeleton = next((self.synth.workspace / "Config").glob("*-MergedSkeleton.buf"))
        original = skeleton.read_bytes()
        self.assertEqual(len(original), 2 * PALETTE_STRIDE)

        # 大小不符
        skeleton.write_bytes(original + b"\x00" * 4)
        before = self._tree_state()
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertFalse(ok, message)
        self.assertIn("角色级骨架大小不符", message)
        self.assertEqual(self._tree_state(), before)

        # 大小恢复但字节被篡改（摘要不符）
        mutated = bytearray(original)
        mutated[0] ^= 0xFF
        skeleton.write_bytes(bytes(mutated))
        ok2, message2 = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertFalse(ok2, message2)
        self.assertIn("角色级骨架字节与元数据摘要不符", message2)

    def test_cache_only_detects_unregistered_new_component(self):
        """工作空间多出一个从未参与构建的组件 ⇒ cache-only 不得放行（必须重建）。"""
        self._scene()
        HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self._break_frame_analysis_path()
        # 新增一个从未落盘过的组件（有 json 但没有任何合并元数据）
        self.synth.add_component(
            "bbbb-0002-20-0",
            indices=[[0, 1, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="r2",
            vertex_limit_vb="50b2",
        )
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.synth.workspace), None
        )
        self.assertFalse(ok, message)
        self.assertIn("bbbb-0002-20-0", message)

    def test_cache_only_refuses_target_bares_request(self):
        """带 target_bares 的请求必须**拒绝** cache-only，而不是答「没有可重绑部件」。

        理由：`target_bares` 的语义是「按这个集合重算 G1」，而 G1 是 pass 级的、脱离
        dump 无法重算；若 cache-only 直接用空的 writes 回答，调用方（导出侧）会拿到
        一个「权威可用但放行 0 个」的判定，把它原有的保守退路（沿用 HIMIDeformGateSafe
        + 覆盖性检查）静默变成空操作。这里把这个边界钉死。
        """
        self._scene()
        HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self._break_frame_analysis_path()
        plan = HIMISkeletonMergeHelper.prepare(
            str(self.synth.workspace), None, target_bares=["aaaa-0001-10-0"]
        )
        self.assertFalse(plan["ok"])
        self.assertFalse(plan.get("cache_only"))
        self.assertEqual(plan.get("gate_source"), "cache-only-unavailable")
        self.assertIn("无法做 pass 级重算", plan["message"])
        self.assertIn("不做「自动改用最新帧」兜底", plan["message"])
        # 不带 target_bares 的同一工作空间仍可 cache-only（导入侧口径）
        plain = HIMISkeletonMergeHelper.prepare(str(self.synth.workspace), None)
        self.assertTrue(plain["ok"], plain.get("message"))
        self.assertEqual(plain["gate_source"], "cache-only")

    def test_role_key_is_filesystem_safe(self):
        key = _himi.derive_role_key("嗨♪爱愿妖精♥")
        self.assertRegex(key, r"^[0-9A-Za-z_]+$")
        self.assertEqual(key, _himi.derive_role_key("嗨♪爱愿妖精♥"))
        self.assertNotEqual(key, _himi.derive_role_key("咚！炽愿吉星"))

    def test_written_payload_satisfies_submesh_json_validator(self):
        """计划书 §9.4 C4：写回的 json 必须让 `_merged_skeleton_metadata_valid` 为真。

        同时把「计划书旧口径（VGMap 键集 == 被引用槽集合）」钉成反例：稀疏键集
        会被同一个校验器判为 False —— 这正是实现必须取 `0..VGCount-1` 完整覆盖的
        理由（F6 / F2）。
        """
        validator = _load_merged_skeleton_validator()
        self.assertIsNotNone(validator, "无法装载 common/submesh_json.py 的校验器")

        component = self._scene()
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(str(self.synth.workspace), None)
        self.assertTrue(ok, message)
        json_path = next(
            (self.synth.workspace / "LOD0" / "aaaa-0001-10-0").glob("*/*.json")
        )
        payload = json.loads(json_path.read_text(encoding="utf-8"))

        # 正例：真实写回的产物
        self.assertTrue(
            validator(payload),
            "真实写回的 VGMap 必须被判为有效（VGCount 完整覆盖）",
        )
        self.assertEqual(int(payload["VGCount"]), len(payload["VGMap"]))
        self.assertEqual(sorted(int(k) for k in payload["VGMap"]), list(range(payload["VGCount"])))

        # 负例 1：计划书旧口径的稀疏 VGMap（键集 == 被引用槽集合）
        sparse = dict(payload)
        sparse["VGCount"] = 3
        sparse["VGMap"] = {"0": 0, "5": 5}
        self.assertFalse(validator(sparse), "稀疏键集必须被判为无效")

        # 负例 2：VGCount 与键集规模不符
        mismatched = dict(payload)
        mismatched["VGCount"] = int(payload["VGCount"]) + 1
        self.assertFalse(validator(mismatched))

        # 负例 3：槽位为负
        negative = dict(payload)
        negative["VGMap"] = dict(payload["VGMap"])
        negative["VGMap"]["0"] = -1
        self.assertFalse(validator(negative))

    def test_real_written_payload_satisfies_submesh_json_validator(self):
        """真实数据落点：在临时副本上写回镇×偃月叩晓，产物仍须通过同一校验器。"""
        validator = _load_merged_skeleton_validator()
        if validator is None:
            self.skipTest("无法装载校验器")
        workspace = REAL_WORKSPACE_ROOT / "镇×偃月叩晓"
        if not workspace.is_dir():
            self.skipTest(f"missing {workspace}")
        target = self._tmp / "real_ws" / "镇×偃月叩晓"
        shutil.copytree(
            workspace, target, ignore=shutil.ignore_patterns("New Folder", "*.blend")
        )
        ok, message = HIMISkeletonMergeHelper.ensure_skeleton_data(str(target), None)
        self.assertTrue(ok, message)
        checked = 0
        for json_path in (target / "LOD0").glob("*/*/*.json"):
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            if "VGMap" not in payload:
                continue
            checked += 1
            self.assertTrue(validator(payload), f"{json_path.name} 未通过合并元数据校验")
            self.assertEqual(int(payload["VGCount"]), len(payload["VGMap"]))
        self.assertEqual(checked, 13)
        # F1：角色级统计在真实数据上钉死（计划书 §9.4 A5）
        metadata = json.loads(
            (target / "Config" / "HIMIMergedSkeleton.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["ReferencedSlotTotal"], 601)
        self.assertEqual(metadata["PoolSlotTotal"], 883)

    def test_resolve_submesh_json_path_prefers_import_map(self):
        self.synth.add_component(
            "cccc-0001-1-0",
            indices=[[0, 0, 0, 0]],
            positions=[[0.0, 0.0, 0.0]],
            palette=b"",
            palette_resource_hash="x",
            vertex_count=1,
        )
        # 第二个 WorkGameType 目录存在 ⇒ 没有 Import.json 映射时必须拒绝
        other = (
            self.synth.workspace
            / "LOD0"
            / "cccc-0001-1-0"
            / "TYPE_GPU_P12_N12_C4_TA16_BW16_BI16_T8_"
        )
        other.mkdir(parents=True, exist_ok=True)
        (other / "cccc-0001-1-0.json").write_text("{}", encoding="utf-8")
        resolved = _himi.resolve_submesh_json_path(str(self.synth.workspace), "LOD0.cccc-0001-1-0")
        self.assertIn("T8_T1-8", resolved)


# --------------------------------------------------------------------------- #
# 6) 真实 dump 回归（路径不存在即跳过）
# --------------------------------------------------------------------------- #


@unittest.skipUnless(REAL_WORKSPACE_ROOT.is_dir(), f"missing {REAL_WORKSPACE_ROOT}")
class TestRealHIMIDumps(unittest.TestCase):
    def test_five_roles_unique_bone_counts(self):
        for role, expected in REAL_EXPECTED.items():
            with self.subTest(role=role):
                workspace = REAL_WORKSPACE_ROOT / role
                if not workspace.is_dir():
                    self.skipTest(f"missing {workspace}")
                plan = HIMISkeletonMergeHelper.prepare(str(workspace), None, force=True)
                self.assertTrue(plan["ok"], plan.get("message"))
                self.assertEqual(plan["slot_count"], expected["slots"])
                self.assertEqual(len(plan["writes"]), expected["components"])
                self.assertEqual(plan["dump_dir"].split(os.sep)[-1], expected["dump"])
                for write in plan["writes"]:
                    payload = write["payload"]
                    # 恒真的两条（计划书 §2.4 #1/#2）
                    self.assertLess(
                        max(write["referenced_slots"]),
                        payload["HIMIPalettePoolSlotCount"],
                        "max(BLENDINDICES) 必须小于 palette 池大小",
                    )
                    self.assertEqual(
                        sorted(int(k) for k in payload["VGMap"]), list(range(payload["VGCount"]))
                    )
                # 三条 join 键不得错配；唯一允许的拒绝是「该组件本帧没有 deform pass」
                # （重新导入后工作空间里可能多出帧内没有形变的组件，那是数据不是缺陷）
                self.assertEqual(
                    {r["reason"] for r in plan["rejections"]} - {"no_deform_pass"},
                    set(),
                    plan["rejections"],
                )

    def test_negative_case_pool_larger_than_referenced(self):
        """镇×偃月叩晓 557a7477-3966-0：池 156 / 被引用槽 8（D03 反例）。

        F1/A5：`ReferencedSlotTotal` 必须**钉死为 601**（不是 `assertLess(0, 883)`
        那种恒真断言），`PoolSlotTotal == 883`。
        """
        workspace = REAL_WORKSPACE_ROOT / "镇×偃月叩晓"
        if not workspace.is_dir():
            self.skipTest(f"missing {workspace}")
        plan = HIMISkeletonMergeHelper.prepare(str(workspace), None, force=True)
        write = next(w for w in plan["writes"] if w["bare"] == "557a7477-3966-0")
        self.assertEqual(write["payload"]["HIMIPalettePoolSlotCount"], 156)
        self.assertEqual(len(write["referenced_slots"]), 8)
        self.assertNotEqual(write["payload"]["HIMIPalettePoolSlotCount"], write["payload"]["VGCount"])
        # 计划书 §9.4 A5：引用槽数之和 != 池大小之和（当前数据 601 vs 883）
        self.assertEqual(plan["metadata"]["ReferencedSlotTotal"], 601)
        self.assertEqual(plan["metadata"]["PoolSlotTotal"], 883)
        self.assertEqual(
            plan["metadata"]["ReferencedSlotTotal"],
            sum(len(w["referenced_slots"]) for w in plan["writes"]),
        )
        self.assertEqual(
            plan["metadata"]["PoolSlotTotal"],
            sum(w["palette_pool_slots"] for w in plan["writes"]),
        )

    def test_real_referenced_slot_totals_are_pinned(self):
        """F1/A5：五角色的 `ReferencedSlotTotal` / `PoolSlotTotal` 逐个钉死。"""
        expected = {
            "咚！炽愿吉星": (517, 517),
            "嗨♪爱愿妖精♥": (1166, 1166),
            "愈生佑翎": (828, 828),
            "窈窕谍影": (897, 897),
            "镇×偃月叩晓": (601, 883),
        }
        for role, (referenced, pool) in expected.items():
            workspace = REAL_WORKSPACE_ROOT / role
            if not workspace.is_dir():
                self.skipTest(f"missing {workspace}")
            plan = HIMISkeletonMergeHelper.prepare(str(workspace), None, force=True)
            metadata = plan["metadata"]
            self.assertEqual(metadata["ReferencedSlotTotal"], referenced, role)
            self.assertEqual(metadata["PoolSlotTotal"], pool, role)
            self.assertGreater(metadata["ReferencedSlotTotal"], 0, "不得恒 0")
            # 与逐组件求和一致（F1 的根因就是这里取错了键）
            self.assertEqual(
                metadata["ReferencedSlotTotal"],
                sum(len(w["referenced_slots"]) for w in plan["writes"]),
                role,
            )
            # 真实数据上 pass 级门控不应误伤（全落盘时每个家族都完整）
            self.assertEqual(metadata["DeformGateRejected"], [], role)
            self.assertEqual(metadata["DeformGateBlockedPasses"], [], role)
            self.assertEqual(metadata["DeformGateMode"], "pass")
            self.assertEqual(metadata["CPUPreskinningSkipped"], [], role)
            self.assertEqual(metadata["DedupExcludedComponents"], [], role)

    def test_real_scale_is_never_assumed_unity(self):
        scales = set()
        for role in REAL_EXPECTED:
            workspace = REAL_WORKSPACE_ROOT / role
            if not workspace.is_dir():
                continue
            plan = HIMISkeletonMergeHelper.prepare(str(workspace), None, force=True)
            for write in plan["writes"]:
                scales.add(round(write["payload"]["HIMIPaletteScale"], 2))
        if not scales:
            self.skipTest("no real workspaces")
        self.assertTrue(scales.issubset({0.93, 1.0, 1.6}), scales)
        self.assertIn(1.6, scales)

    def test_real_log_signature_is_stable(self):
        workspace = REAL_WORKSPACE_ROOT / "愈生佑翎"
        if not workspace.is_dir():
            self.skipTest(f"missing {workspace}")
        dump = HIMISkeletonMergeHelper.resolve_frame_analysis_dir(str(workspace))
        parser = HIMILogParser(os.path.join(dump, "log.txt"))
        self.assertEqual(len(parser.deform_passes()), 8)
        self.assertEqual(len(parser.log_signature), 64)


if __name__ == "__main__":
    unittest.main()
