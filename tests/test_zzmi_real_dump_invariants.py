# -*- coding: utf-8 -*-
"""真实 dump 的「结构不变量」回归（只读，不需要 bpy，不需要工作空间）。

夹具（A/B 对，同一场景「叶瞬光01」）：

- A `FrameAnalysis-2026-09-17-184431`（log 8.5 MB / 20312 files，含合并骨架的 fix-9 代 mod）
- B `FrameAnalysis-2026-09-17-185933`（log 3.25 MB / 7409 files，同场景对照，无合并骨架）

gate 沿用仓库既有风格：`log.txt` 不在本机就整类 skip（`skipUnless`）；本机存在 ⇒ 真实跑。

本文件只断言**能从 log.txt 的数据结构推出来**的事实，做法：

1. 解析 D3D 调用轨迹（`<6 位 draw 号> <调用>(...)` + 其后续的属性描述行，如
   `N: resource=0x… hash=…`），重建每个 draw 时刻的关键绑定（vs/ps shader、ib、so0、
   vs/ps/cs 常量缓冲、vs/ps/cs SRV）。
2. 解析 3DMigoto 自己的落盘消息
   `3DMigoto Dumping Buffer <per-draw 文件> -> <deduped/内容文件>`：**左边**给出
   `draw 号 / 槽位名 / log 里的 hash=`，**右边**给出这份内容的**内容标识**（dedup 文件名前缀，
   8 位十六进制）。这样"同一 hash 是否对应同一内容"就是可判定的事实，无需读 8.5 MB 之外的字节。

已断言的不变量（逐条见下面对应用例的 docstring）：

1. 同一网格在 deform 阶段被画了多次时，每次各有一份 palette 内容（既不缺、也**不允许部分共享**）；
2. 同部件多次 draw 之间 `vs-cb0` / `vs-cb2..vs-cb6` 内容逐字节同一（实例无关常量）；
3. `hash=` 允许同值不同内容 ⇒ **hash 不可当内容键**；
4. 同一网格这几次 deform pass 的 SO 目标是同一块，且该区间内没有重新绑定 `SOSetTargets`（记录事实）；
5. deform 阶段的 draw 全是非索引、非实例化的 `Draw(VertexCount:N)`。

关于"实例"的**口径更正**（2026-09-17，逐条复核 log 得出的实测事实）：

- A 侧 deform 的 `000113/000114/000115` 三笔都是
  `Draw(VertexCount:300, StartVertexLocation:0)`——**同一网格在同一帧被画了三次（多次 deform pass）**，
  **不是**一次 `DrawIndexedInstanced(InstanceCount:3)`；**不能**把它们读成"三个实例"。
- 该帧真正的实例化渲染是**另一笔** `DrawIndexedInstanced(... InstanceCount:N>1 ...)`（A 侧实例数上限 35，
  B 侧 19）；实例数还被 3DMigoto 写进了 vb 布局的 dedup 文件名（`…-inst_count=N`）——
  本文件用 `test_dump_inst_count_label_matches_draw_call` 把"标签 N == 该 draw 的 InstanceCount"钉住，
  从而在不写死 draw 号/实例数的前提下确认"该帧确有实例化"。

本文件**无法**断言（诚实边界，勿在此文件里补写）：

- "palette 内容 ⇒ 实例身份"：同一姿态的两个实例会得到同一份 palette 内容，内容键区分的是
  **姿态**而不是**实例**；
- "该网格有几个实例"：113/114/115 是**多次 deform pass**，从中推不出实例个数；实例化证据只来自
  `DrawIndexedInstanced(InstanceCount>1)` 那类调用（本文件只断言"存在且标签一致"，不给实例数结论）；
- 修复链（姿态指纹 / `->spatialhash` / 池写入 / 蒙皮 CS 发布）在真机的行为：A 侧 mod 无
  `SpatialHash`/`ResourceZZPoseKeySrc`/`PoolZZMI` 痕迹，B 侧根本没有合并骨架；
- "SO 指针族按实例分开"：数据**反证**了它（同一网格的几次 deform pass 共用一块 SO，顺序覆盖）；
- 两个 dump 之间的逐 draw 对应关系：两边的部件集合不同，不能把 A 的某次 pass 与 B 的某 draw 配对；
- 顶点步长（`stride=`）之类的具体数值：本文件不做任何 stride 断言（那些属于别的夹具口径）。
"""

import collections
import io
import os
import re
import unittest
from functools import lru_cache

# ---------------------------------------------------------------------------
# 夹具与 gate
# ---------------------------------------------------------------------------

DUMP_A = r"K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-09-17-184431"
DUMP_B = r"K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-09-17-185933"
DUMPS = (DUMP_A, DUMP_B)


def _log_path(dump_dir):
    return os.path.join(dump_dir, "log.txt")


def _dump_available(dump_dir):
    return os.path.isfile(_log_path(dump_dir))


# ---------------------------------------------------------------------------
# 协议常量（D3D 调用名 / 槽位名，非 dump 专用魔法值）
# ---------------------------------------------------------------------------

DRAW_KINDS = frozenset(
    ("Draw", "DrawIndexed", "DrawIndexedInstanced", "DrawInstanced",
     "DrawInstancedIndirect", "DrawAuto")
)
DISPATCH_KINDS = frozenset(("Dispatch", "DispatchIndirect"))
INSTANCED_KINDS = frozenset(("DrawIndexedInstanced", "DrawInstanced", "DrawInstancedIndirect"))

# 不变量 2 的比较范围：**实例无关**的每部件常量缓冲。
# 排除 `vs-cb1`：它与 `ps-cb0` 共用同一资源，且实测逐成员会变——
# 这一点由 `test_cb1_varies_so_the_invariant_excludes_it` 单独用数据佐证，
# 而不是在这里默默忽略。
INSTANCE_INVARIANT_CB_SLOTS = ("vs-cb0", "vs-cb2", "vs-cb3", "vs-cb4", "vs-cb5", "vs-cb6")

_IDX = re.compile(r"^(\d{6})\s+(.*)$")
_DESC = re.compile(r"^\s+(\d+):\s*(.*)$")
_CALL = re.compile(r"^([A-Za-z0-9_]+)\((.*)\)\s*(.*)$")
_DUMP_MSG = re.compile(r"^3DMigoto Dumping (Buffer|Texture2D) (.+)$")
# `<draw>-<slot>=<loghash>-vs=<h>-ps=<h>.buf`（cs 轨迹则是 `-cs=<h>`）
_PER_DRAW = re.compile(
    r"^(\d{6})-([A-Za-z0-9\-]+?)=([0-9a-f]{8})-(?:[a-z]{2}=[0-9a-f]+-?)+\.(buf|txt|dds)$"
)
# deduped/<内容标识>[-<载荷标签>].<ext>
_CONTENT = re.compile(r"^([0-9a-f]{8})(?:-(.+?))?\.(buf|txt|dds)$")


def _parse_log(log_path):
    """把 log.txt 解析成 (draws, so_calls, dump_events)。只读。"""
    draws, so_calls, dump_events = [], [], []
    state = {"so": {}, "vs_cb": {}, "ps_cb": {}, "cs_cb": {},
             "ib": "", "vs": "", "ps": "", "cs": ""}
    pending, cur = None, None
    with io.open(log_path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            indexed = _IDX.match(line)
            if indexed:
                cur, payload = indexed.group(1), indexed.group(2)
                msg = _DUMP_MSG.match(payload)
                if msg:
                    left, _, right = msg.group(2).partition(" -> ")
                    per_draw = _PER_DRAW.match(os.path.basename(left.strip()))
                    if per_draw:
                        content = None
                        label = None
                        if right:
                            deduped = _CONTENT.match(os.path.basename(right.strip()))
                            if deduped:
                                content = deduped.group(1)
                                label = deduped.group(2) or ""
                        dump_events.append({
                            "draw": per_draw.group(1), "slot": per_draw.group(2),
                            "loghash": per_draw.group(3), "ext": per_draw.group(4),
                            "content": content, "label": label,
                        })
                    continue
            else:
                desc = _DESC.match(line)
                if desc and pending:
                    body = desc.group(2)
                    resource = re.search(r"resource=(0x[0-9A-Fa-f]+)", body)
                    hh = re.search(r"hash=([0-9a-f]{8})", body)
                    key = pending
                    if key == "so":
                        state["so"][int(desc.group(1))] = (
                            resource.group(1) if resource else "",
                            hh.group(1) if hh else "",
                        )
                    else:
                        state[key][int(desc.group(1))] = hh.group(1) if hh else ""
                    continue
                continue

            if not payload:
                continue
            call = _CALL.match(payload)
            pending = None
            if not call:
                continue
            name, args, tail = call.group(1), call.group(2), call.group(3)
            if name == "SOSetTargets":
                pending = "so"
                so_calls.append(cur)
            elif name in ("VSSetConstantBuffers", "VSSetConstantBuffers1"):
                pending = "vs_cb"
            elif name in ("PSSetConstantBuffers", "PSSetConstantBuffers1"):
                pending = "ps_cb"
            elif name in ("CSSetConstantBuffers", "CSSetConstantBuffers1"):
                pending = "cs_cb"
            elif name in ("VSSetShader", "PSSetShader", "CSSetShader"):
                found = re.search(r"hash=([0-9a-f]{8,16})", tail + args)
                state[{"V": "vs", "P": "ps", "C": "cs"}[name[0]]] = found.group(1) if found else ""
            elif name == "IASetIndexBuffer":
                found = re.search(r"hash=([0-9a-f]{8})", tail)
                state["ib"] = found.group(1) if found else ""
            elif name.startswith("Draw") or name.startswith("Dispatch"):
                inst = re.search(r"InstanceCount:(\d+)", args)
                vtx = re.search(r"VertexCount:(\d+)", args)
                draws.append({
                    "draw": cur, "kind": name, "args": args,
                    "inst": int(inst.group(1)) if inst else None,
                    "vcount": int(vtx.group(1)) if vtx else None,
                    "vs": state["vs"], "ps": state["ps"], "cs": state["cs"],
                    "ib": state["ib"], "so": dict(state["so"]),
                    "vs_cb": dict(state["vs_cb"]), "ps_cb": dict(state["ps_cb"]),
                })
    return draws, so_calls, dump_events


@lru_cache(maxsize=None)
def _facts(dump_dir):
    """每个 dump 只解析一次（pytest 同一进程内复用）。"""
    draws, so_calls, dump_events = _parse_log(_log_path(dump_dir))
    content_by_slot = collections.defaultdict(dict)
    for event in dump_events:
        if event["content"]:
            content_by_slot[event["draw"]].setdefault(event["slot"], set()).add(event["content"])
    return {
        "draws": draws,
        "so_calls": so_calls,
        "dumps": dump_events,
        "content": content_by_slot,
    }


def _slot_contents(facts, draw, slot):
    return tuple(sorted(facts["content"].get(draw, {}).get(slot, ())))


def _deform_groups(facts):
    """deform 组：非索引 `Draw` + 同 (vs, ps, ib) + 组内每个成员都 dump 了 palette(vs-t0)。

    只按「网格 + 管线」分组的**结构键**，不碰 palette 内容（否则不变量 1 会变成循环论证）。
    返回 [(key, [draw, …])]，按最小 draw 号排序。
    """
    grouped = collections.defaultdict(list)
    for d in facts["draws"]:
        if d["kind"] != "Draw":
            continue
        if not (d["vs"] and d["ps"] and d["ib"]):
            continue
        if not _slot_contents(facts, d["draw"], "vs-t0"):
            continue
        grouped[(d["vs"], d["ps"], d["ib"])].append(d)
    out = [(k, v) for k, v in grouped.items() if len(v) >= 2]
    out.sort(key=lambda kv: min(int(d["draw"]) for d in kv[1]))
    return out


def _instanced_render_groups(facts):
    """渲染组：索引/实例化 draw + 同 (vs, ps, ib) + 组内都有 palette 内容。"""
    grouped = collections.defaultdict(list)
    for d in facts["draws"]:
        if d["kind"] not in ("DrawIndexed", "DrawIndexedInstanced"):
            continue
        if not (d["vs"] and d["ps"] and d["ib"]):
            continue
        if not _slot_contents(facts, d["draw"], "vs-t0"):
            continue
        grouped[(d["vs"], d["ps"], d["ib"])].append(d)
    return [(k, v) for k, v in grouped.items() if len(v) >= 2]


def _gate(dump_dir):
    return unittest.skipUnless(_dump_available(dump_dir), "提取数据不在本机: " + dump_dir)


# ---------------------------------------------------------------------------
# 共用断言（两个 dump 各跑一遍）
# ---------------------------------------------------------------------------


class _DumpInvariantTests:
    DUMP = None

    # -- 解析健全性：防「解析失败 ⇒ 下面的断言全部空转」 --------------------
    def test_parser_yields_structural_facts(self):
        facts = _facts(self.DUMP)
        draws, events = facts["draws"], facts["dumps"]
        self.assertGreaterEqual(len(draws), 100, "解析到的 draw 太少，解析器可能失效")
        self.assertGreaterEqual(len(events), 1000, "解析到的 dump 事件太少")
        self.assertTrue(any(e["content"] for e in events), "没有任何 dedup 内容标识")
        protocol = DRAW_KINDS | DISPATCH_KINDS
        for d in draws:
            self.assertRegex(d["draw"], r"^\d{6}$")
            self.assertIn(d["kind"], protocol)
            self.assertTrue(d["vs"] or d["ps"] or d["cs"] or d["kind"].startswith("Dispatch"),
                            "缺少 shader 绑定：draw %s" % d["draw"])
        for e in events[:500]:
            self.assertRegex(e["slot"], r"^[A-Za-z0-9\-]+$")
            self.assertRegex(e["loghash"], r"^[0-9a-f]{8}$")

    # -- 不变量 5：deform 阶段非索引、非实例化 ------------------------------
    def test_deform_groups_are_non_indexed_single_instance(self):
        facts = _facts(self.DUMP)
        groups = _deform_groups(facts)
        self.assertTrue(groups, "找不到任何 deform 组（非索引 Draw + palette 提取）")
        for key, members in groups:
            self.assertGreaterEqual(len(members), 2)
            for d in members:
                self.assertEqual(d["kind"], "Draw", "deform 组里出现了索引/实例化 draw")
                self.assertIsNone(d["inst"], "非索引 Draw 不应带 InstanceCount：%s" % d["draw"])
        kinds = {d["kind"] for d in facts["draws"]}
        self.assertNotIn(
            "DrawInstanced", kinds,
            "出现了非索引的实例化形式 DrawInstanced：deform 非实例化的结论需要重新审视",
        )
        # 记录事实：本 dump 的实例化只以索引实例化/间接形式出现
        self.assertTrue(kinds & INSTANCED_KINDS, "没有任何实例化 draw：夹具可能取错帧")

    # -- 不变量 2：cb0/cb2..cb6 是实例无关常量 ------------------------------
    def test_constant_buffers_are_instance_invariant(self):
        facts = _facts(self.DUMP)
        compared = 0
        for key, members in _deform_groups(facts):
            for slot in INSTANCE_INVARIANT_CB_SLOTS:
                sets = [_slot_contents(facts, d["draw"], slot) for d in members]
                if any(not s for s in sets):
                    continue  # 该组没提取这个槽 → 不参与比较（不写死）
                compared += 1
                self.assertEqual(
                    len(set(sets)), 1,
                    "%s 组 %s 的 %s 内容不一致：%s" % (
                        os.path.basename(self.DUMP), [d["draw"] for d in members], slot, sets),
                )
        self.assertGreaterEqual(compared, 4, "实际比较的常量缓冲槽太少，不变量 2 可能空转")

    def test_cb1_varies_so_the_invariant_excludes_it(self):
        """记录事实：`vs-cb1`（与 `ps-cb0` 同资源）逐成员会变 ⇒ 不能算实例无关常量。

        这条用例的作用是让不变量 2 的"排除 cb1"**有数据依据**，而不是一句注释。
        """
        facts = _facts(self.DUMP)
        varying = []
        for key, members in _deform_groups(facts):
            sets = [_slot_contents(facts, d["draw"], "vs-cb1") for d in members]
            if any(not s for s in sets):
                continue
            if len(set(sets)) > 1:
                varying.append(([d["draw"] for d in members], sets))
        self.assertTrue(
            varying,
            "本 dump 找不到 vs-cb1 逐成员变化的 deform 组：排除 cb1 的依据缺失（需重看数据）",
        )

    # -- 不变量 1：同一网格的多次 deform pass 各一份 palette -----------------
    def test_palette_content_is_never_partially_shared_between_deform_passes(self):
        """同一网格多次 deform pass 的 palette 内容（且不允许"部分共享"）。

        deform 组按**结构键**（同网格 + 同管线）划分，组内 palette 内容只允许两种形态：

        - 全组同一份内容 ⇒ 同一姿态被重复绘制（多次 pass 同一份骨骼姿态）；
        - 逐成员各不相同 ⇒ 每次 pass 各自一份骨骼姿态（本帧的 113/114/115 就是这种）。

        "部分相同"（两个成员同内容、第三个不同）在这里是要被拦下的病态形态：那正是
        内容型键会撞键的场景，也是合并骨架槽位复用出问题的形态。
        """
        facts = _facts(self.DUMP)
        groups = _deform_groups(facts)
        per_pass_cases = []
        for key, members in groups:
            pals = [_slot_contents(facts, d["draw"], "vs-t0") for d in members]
            self.assertTrue(all(pals), "deform 组内缺少 palette 内容")
            unique = set(pals)
            self.assertIn(
                len(unique), (1, len(members)),
                "%s 组 %s 的 palette 出现部分共享：%s" % (
                    os.path.basename(self.DUMP), [d["draw"] for d in members], pals),
            )
            if len(unique) == len(members) and len(members) >= 3:
                per_pass_cases.append(([d["draw"] for d in members], pals))
        self.assertTrue(
            per_pass_cases,
            "本 dump 找不到 ≥3 成员且 palette 逐 pass 不同的正例：逐 pass 内容区分证据缺失",
        )
        # 正例内部再确认一次：成员数 == 不同内容数
        for draws_, pals in per_pass_cases:
            self.assertEqual(len(set(pals)), len(draws_))

    # -- 更正事实：dump 标签里的 inst_count 必须等于该 draw 的 InstanceCount ---
    def test_dump_inst_count_label_matches_draw_call(self):
        """deform 阶段没有实例化；实例化只由 `DrawIndexedInstanced` 表达，且可与 dump 标签对账。

        3DMigoto 把实例数写进了 vb 布局的 dedup 文件名（`…-inst_count=N`）。这条不变量把
        "标签 N"与"该 draw 调用里的 `InstanceCount:N`"钉在一起：

        - 二者必须逐条相等（不写死任何 draw 号/实例数）；
        - 且必须存在 N>1 的样本 ⇒ 该帧确有实例化渲染（而不是把多次 deform pass 误读成实例）。
        """
        facts = _facts(self.DUMP)
        by_draw = {d["draw"]: d for d in facts["draws"]}
        checked, instanced = 0, []
        for event in facts["dumps"]:
            found = re.search(r"inst_count=(\d+)", event["label"] or "")
            if not found:
                continue
            label_n = int(found.group(1))
            draw = by_draw.get(event["draw"])
            self.assertIsNotNone(draw, "dump 事件 %s 找不到对应 draw" % event["draw"])
            self.assertIsNotNone(
                draw["inst"],
                "draw %s（%s）带 inst_count 标签却没有 InstanceCount 字段" % (
                    draw["draw"], draw["kind"]),
            )
            self.assertEqual(
                label_n, draw["inst"],
                "draw %s 的 dump 标签 inst_count=%d 与调用的 InstanceCount=%d 不一致" % (
                    draw["draw"], label_n, draw["inst"]),
            )
            checked += 1
            if label_n > 1:
                instanced.append((draw["draw"], draw["kind"], label_n))
        self.assertGreater(checked, 0, "没有任何带 inst_count 标签的 dump 事件：夹具或解析异常")
        self.assertTrue(
            instanced,
            "本 dump 找不到 InstanceCount>1 的实例化 draw ⇒ 与「该帧有实例化」的结论矛盾",
        )

    # -- 不变量 3：hash 不可当内容键 ----------------------------------------
    def test_hash_token_does_not_identify_content(self):
        facts = _facts(self.DUMP)
        ambiguous = collections.defaultdict(lambda: collections.defaultdict(set))
        for e in facts["dumps"]:
            if e["content"]:
                ambiguous[e["slot"]][e["loghash"]].add(e["content"])
        pairs = {
            (slot, loghash): sorted(contents)
            for slot, by_hash in ambiguous.items()
            for loghash, contents in by_hash.items()
            if len(contents) > 1
        }
        self.assertTrue(
            pairs,
            "没有任何「同一 hash 对应多份内容」的样本：与黑盒 log 的 hash 语义结论矛盾，"
            "或解析器把内容标识取错了",
        )
        self.assertTrue(
            {h: c for h, c in ambiguous.get("vs-t0", {}).items() if len(c) > 1},
            "palette（vs-t0）没有出现「同 hash 不同内容」，内容键证据不足",
        )
        # 同一个 loghash 在 ≥3 成员的 deform 组里对应多份内容（正例）
        for key, members in _deform_groups(facts):
            if len(members) < 3:
                continue
            pals = [_slot_contents(facts, d["draw"], "vs-t0") for d in members]
            if len(set(pals)) != len(members):
                continue
            hashes_in_log = set()
            for e in facts["dumps"]:
                if e["slot"] == "vs-t0" and e["draw"] in {d["draw"] for d in members}:
                    hashes_in_log.add(e["loghash"])
            self.assertEqual(len(hashes_in_log), 1, "正例内 palette 的 log hash 竟然不同")
            self.assertEqual(len(set(pals)), len(members))
            return
        self.fail("找不到可用的 ≥3 成员 deform 正例来断言「同 hash 多份内容」")

    # -- 不变量 4：多次 deform pass 共用同一块 SO 目标（记录事实） -----------
    def test_so_target_is_shared_across_deform_passes(self):
        """同一网格的多次 deform pass 共用同一块 SO 目标（顺序覆盖），区间内不重绑。

        实测：全帧 `SOSetTargets` 次数很少（A 侧 32 次、索引 25–41；B 侧 32 次、索引 25–48），
        且每个 deform 组区间内 0 次 ⇒ 组内各次 pass 写的是同一块 SO（最后一个写者胜出）。
        这条把"SO 指针族按实例分开"这一错误预期**钉死为反面事实**。
        """
        facts = _facts(self.DUMP)
        checked = 0
        for key, members in _deform_groups(facts):
            bound = [tuple(sorted(d["so"].get(0, ()))) for d in members]
            self.assertTrue(all(bound), "组内 draw 缺 so0 绑定：%s" % [d["draw"] for d in members])
            self.assertEqual(
                len(set(bound)), 1,
                "%s 组 %s 的 so0 不是同一块：%s" % (
                    os.path.basename(self.DUMP), [d["draw"] for d in members], bound),
            )
            draw_numbers = sorted(int(d["draw"]) for d in members)
            inside = [c for c in facts["so_calls"] if draw_numbers[0] < int(c) <= draw_numbers[-1]]
            self.assertEqual(
                inside, [],
                "组 %s 区间内重新绑定过 SOSetTargets：%s" % (draw_numbers, inside),
            )
            # 绑定是在组之前建立的（延续绑定），记录下来
            before = [c for c in facts["so_calls"] if int(c) < draw_numbers[0]]
            self.assertTrue(before, "组之前没有任何 SOSetTargets，绑定来源不明")
            checked += 1
        self.assertGreaterEqual(checked, 1, "没有任何 deform 组可校验 SO 共享")

    # -- A/B 对照的记录事实：渲染阶段实例化共用一份 palette -----------------
    def test_instanced_render_groups_share_one_palette(self):
        """记录事实：索引/实例化 render 组共用同一份 palette 内容。

        与 deform 阶段的"逐 pass 一份"形成对照（本文件不据此断言因果，只记录结构差异）。
        注意：真正带 `InstanceCount>1` 的实例化证据由
        `test_dump_inst_count_label_matches_draw_call` 负责，本条只记录"同网格 render 组共用 palette"。
        """
        facts = _facts(self.DUMP)
        shared, instanced = [], 0
        for key, members in _instanced_render_groups(facts):
            pals = [_slot_contents(facts, d["draw"], "vs-t0") for d in members]
            if len(set(pals)) == 1:
                shared.append(([d["draw"] for d in members][:6], pals[0]))
            instanced += sum(1 for d in members if d["kind"] == "DrawIndexedInstanced")
        self.assertTrue(shared, "找不到共用同一 palette 的索引/实例化 render 组")
        self.assertGreater(instanced, 0, "找不到 DrawIndexedInstanced：夹具可能取错帧")


@_gate(DUMP_A)
class RealDumpInvariants184431(_DumpInvariantTests, unittest.TestCase):
    """A 侧：含合并骨架的 fix-9 代。

    口径更正（实测）：`000113/000114/000115` 是同一网格在 deform 阶段被画了**三次**
    （都是 `Draw(VertexCount:300, StartVertexLocation:0)`），**不是三个实例**；
    该帧的实例化渲染是 `DrawIndexedInstanced(… InstanceCount:N>1 …)` 那一类调用。
    """

    DUMP = DUMP_A


@_gate(DUMP_B)
class RealDumpInvariants185933(_DumpInvariantTests, unittest.TestCase):
    """B 侧：同场景对照（无合并骨架），用于确认不变量不是 A 侧独有。"""

    DUMP = DUMP_B


if __name__ == "__main__":
    unittest.main()
