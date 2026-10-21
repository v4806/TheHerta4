"""把 HIMI「合并到一个 IB」导出里的 IB 段改成「每组件画自己那一刀」。

背景（2026-10-20 实机排查）：合并导出把 12 个组件的索引拼进一份池化 IB，
但 ini 只让**一个**组件段画满 101121 个索引、其余 11 段各画 3 个 stub；
于是 12 个部件全部用同一个组件的三张贴图渲染 —— 其余部件的 UV 落在别人
的贴图空间里，画面就是“部件错位/花纹错乱”。

本工具只改 ini：每个组件段都指向池化 IB，画自己那一段（count/start 取自
出厂版里那一大段的 12 条 drawindexed，它们本来就已经算好），并把重复的大段删掉。
delivery 后每段仍各自 `ps-t0/1/2` 保留自己的贴图 ⇒ 部件与贴图重新对上。

用法：
    python tools/himi_pool_ib_fixer.py <mod 目录>            # 就地改（留 .bak-poolib）
    python tools/himi_pool_ib_fixer.py <mod 目录> --check    # 只报告
"""
from __future__ import annotations

import os
import re
import sys

SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
DRAW_RE = re.compile(r"^(\s*)drawindexed\s*=\s*(\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*$")


def parse_sections(text: str):
    """把 ini 切成 [(name, [lines])]，保留注释与空行。"""
    out, name, buf = [], None, []
    for line in text.splitlines():
        m = SECTION_RE.match(line.strip())
        if m:
            if name is not None:
                out.append((name, buf))
            name, buf = m.group(1), []
        if name is not None:
            buf.append(line)
    if name is not None:
        out.append((name, buf))
    return out


def section_get(lines, key):
    for line in lines:
        s = line.strip()
        if s.split("=")[0].strip().lower() == key:
            return s.split("=", 1)[1].split(";")[0].strip()
    return None


def section_set(lines, key, value):
    for i, line in enumerate(lines):
        s = line.strip()
        if s.split("=")[0].strip().lower() == key:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}{key} = {value}"
            return True
    return False


def section_draws(lines):
    out = []
    for i, line in enumerate(lines):
        m = DRAW_RE.match(line)
        if m:
            out.append((i, int(m.group(2)), int(m.group(3)), int(m.group(4))))
    return out


def fix(mod_dir: str, check: bool = False) -> int:
    ini = None
    for name in os.listdir(mod_dir):
        if name.lower().endswith(".ini") and ".bak" not in name and ".diag" not in name:
            ini = os.path.join(mod_dir, name)
            break
    if not ini:
        print("找不到 ini")
        return 2
    with open(ini, "r", encoding="utf-8") as fh:
        text = fh.read()
    sections = parse_sections(text)
    ib_sections = {n: l for n, l in sections if "TextureOverride" in n and "LOD0." in n}
    if not ib_sections:
        print("没有 IB 段")
        return 2

    # 1）找出「画满整池」的那一大段（count 最大，且同一 hash 的段里唯一）
    biggest = max(ib_sections.items(), key=lambda kv: max((d[1] for d in section_draws(kv[1])), default=0))
    big_name, big_lines = biggest
    big_draws = section_draws(big_lines)
    total = sum(d[1] for d in big_draws)
    if len(big_draws) < 2:
        print("大段里只有 %d 条 drawindexed ⇒ 不像合并池化导出，未改动" % len(big_draws))
        return 1

    # 池化 IB 资源名：大段的 ib = ...
    pool_ib = section_get(big_lines, "ib")
    if not pool_ib:
        print("大段没有 ib 行")
        return 2

    # 2）组件身份 = 段名里的「索引数」（`LOD0.<hash>_<count>_<start>`），它唯一且与池化 draw 的 count 对得上
    def name_count(name: str):
        m = re.search(r"_LOD0\.[0-9a-f]+_(\d+)_(\d+)$", name)
        return int(m.group(1)) if m else None

    by_count = {}
    for n, l in ib_sections.items():
        c = name_count(n)
        if c is not None:
            by_count.setdefault(c, []).append(n)

    changed, unmatched = [], []
    for (_li, count, start, bv) in big_draws:
        cands = by_count.get(count) or []
        if not cands:
            unmatched.append(count)
            continue
        name = cands[0]
        lines = ib_sections[name]
        if not check:
            section_set(lines, "ib", pool_ib)
            section_set(lines, "match_first_index", "0")
        draws = section_draws(lines)
        if draws:
            i, c0, s0, b0 = draws[0]
            if (c0, s0) != (count, start):
                changed.append((name, f"{c0},{s0} -> {count},{start}"))
                if not check:
                    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                    lines[i] = f"{indent}drawindexed = {count},{start},{bv}"
            for i, *_ in draws[1:]:
                if not check:
                    lines[i] = ""
        else:
            changed.append((name, "缺 drawindexed"))
            if not check:
                lines.append(f"drawindexed = {count},{start},{bv}")
    if unmatched:
        print(f"有 {len(unmatched)} 条池化 draw 找不到对应组件段（count={unmatched}）⇒ 未改动")
        return 1

    # 4）删掉那一大段（它的内容已被分发）
    if check:
        print(f"[check] 池化 IB = {pool_ib}，总索引 {total}")
        print(f"[check] 待分发 {len(big_draws)} 条池化 draw，需要改 {len(changed)} 个段")
        for n, d in changed:
            print(f"  {n}: {d}")
        return 0

    out_lines = []
    for n, l in sections:
        if n == big_name:
            out_lines.append(f"; [已删] {n} —— 内容已分发到各组件段（tools/himi_pool_ib_fixer.py）")
            continue
        out_lines.extend([x for x in l if x.strip() != ""] or l)
    backup = ini + ".bak-poolib"
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(ini, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    print(f"已改写 {len(changed)} 段，删除大段 {big_name}；备份 {os.path.basename(backup)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(fix(sys.argv[1], check="--check" in sys.argv))
