# -*- coding: utf-8 -*-
"""模组每帧命令开销估算（只读，可对任意 SSMT 导出模组运行）。

为什么需要它
------------
3DMigoto 的命令列表由 CPU 逐条解释执行：模组装得越多，**每帧无条件执行的命令数**
就越直接地决定卡顿。本仓库的性能门控优化（见 `docs/analysis/mod-perf-gating.md`）
需要可复现的口径来衡量与回归 —— 这个脚本就是那把尺子。

分类口径
--------
把每个 `[Present]` 段（以及它 `run = CommandList\\X` 调用的命令列表，递归展开）里的
命令按**最严格的外层门控**归类，并区分"是否必然执行"：

* 门控种类
  - ``always`` —— 不在任何已知门控内
  - ``drawn``  —— 在角色门控内（``$active0 == 1`` / ``$active == 1`` / ``$ssmtdrag_drawn_* == 1``）
  - ``armed``  —— 在臂动门控内（``$ssmtdrag_mode_* == 1`` / ``$inputMode == 0``）
* 必然性
  - ``exact``   —— 外层没有"其它条件"（模式/取值分支）：**该场景下必然执行**
  - ``upper``   —— 外层还有其它条件：只在分支被取中时执行，故只计入上界

``if`` 行本身也是命令（要解释条件），一并计数。

用法
----
    python tools/estimate_mod_frame_cost.py [配置表路径] [--mods N] [--ns 100]
"""
import argparse
import os
import re
import sys

DEFAULT_INI = (
    r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\克拉蕾\克拉蕾.ini"
)
SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
RUN_RE = re.compile(
    r"^\s*(?:pre\s+|post\s+)?run\s*=\s*(?:CommandList\\|CommandList)?(.+?)\s*$",
    re.IGNORECASE,
)

CHAR_GATE_PATTERNS = ("$active0 == 1", "$active == 1", "$ssmtdrag_drawn_")
ARMED_GATE_PATTERNS = ("$ssmtdrag_mode_", "$inputmode == 0")
MAX_RUN_DEPTH = 3
GATES = ("always", "drawn", "armed")


def parse_blocks(lines):
    """把 ini 切成块：返回 ([name, start, end], {name: [块序号]})。

    同一个段名可能出现多次（3DMigoto 会把重复段**按文件顺序追加**执行）：
    本模组就有 8 个 `[Present]`。因此必须按"块"遍历，不能按段名去重。
    """
    blocks = []
    by_name = {}
    for index, line in enumerate(lines, 1):
        match = SECTION_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        blocks.append([name, index, len(lines)])
        by_name.setdefault(name, []).append(len(blocks) - 1)

    for position, block in enumerate(blocks):
        block[2] = blocks[position + 1][1] - 1 if position + 1 < len(blocks) else len(lines)
    return blocks, by_name


def classify(condition):
    """把一条 if 条件归到门控种类；非已知门控返回 None（表示"其它条件"）。"""
    text = str(condition or "").lower()
    if any(pattern.lower() in text for pattern in ARMED_GATE_PATTERNS):
        return "armed"
    if any(pattern.lower() in text for pattern in CHAR_GATE_PATTERNS):
        return "drawn"
    return None


def _strictest(gate):
    if gate == "armed":
        return "armed"
    if gate == "drawn":
        return "drawn"
    return "always"


def count_blocks(lines, blocks, block_indexes, inherited_gate="always", inherited_other=False):
    """统计若干块（同名段的所有出现）里的命令：返回 {(gate, exact): 计数} 与要展开的 CommandList。"""
    buckets = {(gate, exact): 0 for gate in GATES for exact in (True, False)}
    runs = []

    for block_index in block_indexes:
        _name, start, end = blocks[block_index]
        stack = []  # 每层: (gate or None, 是否"其它条件")
        for index in range(start, end):  # start 是段头行号（1-based）→ range 从段头之后开始
            stripped = lines[index].strip()
            if not stripped or stripped.startswith(";"):
                continue
            low = stripped.lower()

            if low.startswith("if ") or low.startswith("if("):
                # 条件行本身：按**外层**语境判定必然性（进入外层即必然求值）
                gate, exact = _context(inherited_gate, inherited_other, stack)
                buckets[(gate, exact)] += 1
                stack.append((classify(stripped), classify(stripped) is None))
                continue

            if low == "endif":
                if stack:
                    stack.pop()
                continue

            if low.startswith("else") or low.startswith("elif"):
                if stack and (low.startswith("elif") or low.startswith("else if")):
                    stack[-1] = (classify(stripped), classify(stripped) is None)
                    gate, exact = _context(inherited_gate, inherited_other, stack[:-1])
                    buckets[(gate, exact)] += 1
                continue

            gate, exact = _context(inherited_gate, inherited_other, stack)
            buckets[(gate, exact)] += 1

            run_match = RUN_RE.match(stripped)
            if run_match:
                runs.append((run_match.group(1), gate, not exact))
    return buckets, runs


def _context(inherited_gate, inherited_other, stack):
    """由外层栈 + 继承语境得出 (门控种类, 是否必然执行)。"""
    gate = inherited_gate
    other = inherited_other
    for layer_gate, layer_other in stack:
        if layer_gate is not None:
            gate = _strictest(layer_gate) if gate == "always" else (
                "armed" if "armed" in (gate, layer_gate) else "drawn"
            )
        if layer_other:
            other = True
    return gate, not other


def resolve_block_indexes(by_name, run_name):
    """`run = CommandList\\X` / `run = CommandListX` 与段名的映射。

    两种写法在产物里都存在：材质/拖拽钩子发 `CommandList\\X`（段名保留反斜杠），
    拖拽节点的 Present 段发拼接式 `CommandListX`。都要认。
    """
    candidates = (
        run_name,
        f"CommandList\\{run_name}",
        f"CommandList{run_name}",
        run_name.replace("\\", ""),
    )
    for candidate in candidates:
        if candidate in by_name:
            return by_name[candidate]
    return None


def walk(lines, blocks, by_name):
    totals = {(gate, exact): 0 for gate in GATES for exact in (True, False)}
    visited = set()

    def visit(block_indexes, gate, other, depth):
        if depth > MAX_RUN_DEPTH:
            return
        key = (tuple(block_indexes), gate, other)
        if key in visited:
            return
        visited.add(key)
        buckets, runs = count_blocks(lines, blocks, block_indexes, gate, other)
        for bucket_key, value in buckets.items():
            totals[bucket_key] += value
        for run_name, run_gate, run_other in runs:
            target = resolve_block_indexes(by_name, run_name)
            if target:
                visit(target, run_gate, run_other, depth + 1)

    for block_index in by_name.get("Present", []):
        visit([block_index], "always", False, 0)
    return totals


def main():
    parser = argparse.ArgumentParser(description="估算模组每帧命令开销")
    parser.add_argument("ini", nargs="?", default=DEFAULT_INI)
    parser.add_argument("--mods", type=int, default=1, help="按 N 个同类模组换算")
    parser.add_argument("--ns", type=float, default=100.0, help="单条命令耗时假设（纳秒）")
    args = parser.parse_args()

    if not os.path.isfile(args.ini):
        print(f"[ABORT] 找不到配置表: {args.ini}")
        return 1

    lines = open(args.ini, encoding="utf-8-sig").read().splitlines()
    blocks, by_name = parse_blocks(lines)
    if "Present" not in by_name:
        print(f"[ABORT] 配置表里没有 [Present] 段: {args.ini}")
        return 1

    totals = walk(lines, blocks, by_name)

    def exact(*gates):
        return sum(totals[(gate, True)] for gate in gates)

    def upper(*gates):
        return sum(totals[(gate, False)] for gate in gates)

    def ms(commands):
        return commands * args.mods * args.ns / 1_000_000.0

    print(f"配置表: {args.ini}")
    print(f"  行数 {len(lines)}   段块 {len(blocks)}   模组数假设 {args.mods}   单命令 {args.ns:g}ns\n")

    rows = (
        ("角色不在场", ("always",)),
        ("角色在场（未臂动）", ("always", "drawn")),
        ("角色在场（臂动）", ("always", "drawn", "armed")),
    )
    print(f"{'场景':22} {'必然执行':>10} {'上界':>10} {'必然耗时':>12} {'上界耗时':>12}")
    for label, gates in rows:
        must = exact(*gates)
        cap = must + upper(*gates)
        print(f"{label:22} {must:10} {cap:10} {ms(must):9.3f} ms {ms(cap):9.3f} ms")

    print(
        "\n读法："
        "\n  · 「必然执行」= 该场景下每帧一定会跑的命令数（`if` 行也算，因为要解释条件）；"
        "\n  · 「上界」= 再加上落在模式/取值分支里的命令（只在分支被取中时执行）；"
        "\n  · **角色不在场的「必然执行」是最关键的数**：装 N 个模组时它线性叠加，"
        "\n    正是「大量模组导致卡顿」的来源；在场帧的开销只在角色被绘制时发生。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
