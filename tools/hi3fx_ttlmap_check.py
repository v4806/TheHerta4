# -*- coding: utf-8 -*-
"""HI3FX 运行时的静态自检（FXMap 二值裁切 / TTLMap 抖动半透明）。

这个脚本不依赖游戏，只读 ini 文本，用来在改完 ``Mods\\HI3FX`` 之后核对那些
"漏一行就整个屏幕半透明 / 静默失效" 的成对关系：

* **绑定必须有解绑**：``CommandListBind`` 绑的每个 ``ps-tNN`` 都要有
  ``[Present]`` 的 ``post`` 清零、``CommandListUnbind`` / ``CommandListClean``
  的显式清零，以及**每个读了该槽的 ShaderRegex 组**自己的 ``post`` 清零
  （3DMigoto 绘制后不会恢复纹理绑定，漏一处就会粘到后面每一个绘制上）。
* **别名成对**：``Resource\\HI3FX\\FXMap`` / ``TTLMap`` 必须同时具备
  空段声明、绑定、解绑、``Reset`` 里的 ``= null``。
* **变量复位**：``[Constants]`` 里定义的每个 ``global`` 变量都要在
  ``[CommandListReset]`` 里被写回（少数几个按设计不复位的在 ALLOWED 里列明）。
* **IniParams 行**：新增的 ``w233``（FXMap 二值化阈值）必须同时有
  ``[Constants]`` 默认值与 ``[CommandListCommit]`` 的每帧刷新。
* **汇编结构**：每个 ``*.Pattern.Replace`` 的 ``if_* / endif`` 必须配对，
  引用的 ``tNN`` 必须在同组的 ``InsertDeclarations`` 里 ``dcl`` 过。
* **重复段名**：3DMigoto 只解析第一个重名段，后面的静默失效（会打一条
  ``WARNING: Duplicate section``）。

用法::

    python tools/hi3fx_ttlmap_check.py "K:\\SSMT-Package-master\\3Dmigoto\\HI3\\Mods\\HI3FX"

退出码 0 = 全过，1 = 有失败项（失败项逐条打印）。
"""

import argparse
import os
import re
import sys


# ---------------------------------------------------------------- ini 解析

SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
NAMESPACE_RE = re.compile(r"^\s*namespace\s*=\s*(?P<ns>\S+)\s*$", re.IGNORECASE)
PERSIST_RE = re.compile(r"^global\s+(?:persist\s+)?(?P<var>\$[\w\\]+)\s*=", re.IGNORECASE)
SLOT_RE = re.compile(r"^ps-t(?P<slot>\d+)\s*=\s*(?P<value>.+?)\s*$", re.IGNORECASE)
DCL_RE = re.compile(r"^dcl_resource_texture\w*\s*\([^)]*\)\s*t(?P<slot>\d+)\s*$", re.IGNORECASE)
RUN_RE = re.compile(r"^\s*(?:post\s+)?run\s*=\s*(?P<target>CommandList\\[^\s;]+)\s*$", re.IGNORECASE)
IF_RE = re.compile(r"if_(?:nz|z)\b", re.IGNORECASE)
ENDIF_RE = re.compile(r"endif\b", re.IGNORECASE)
# 注意：替换文本里每条指令之间是**字面**的 ``\n``（反斜杠 + n），所以行首标记
# 前面那个字符是 ``n``（词字符），``\b`` 在这里不成立 —— 上面两个正则故意不加
# 前导词边界。扫描槽位前必须先把 ``${...}`` 替换占位符删掉，否则模式捕获组
# （``${t1:...}`` 这样的命名组）会被当成纹理槽 t1。
SUBST_RE = re.compile(r"\$\{[^}]*\}")
TSLOT_REF_RE = re.compile(r"(?<![\w$])t(?P<slot>\d+)(?![\w.])")

# 按设计不复位的变量（README 第 5.6 章写明了理由），以及 3DMigoto 自己的过滤索引。
RESET_ALLOWED_SKIPS = {
    "$enabled",            # F1 总开关：用户按 F1 关掉后不能被 Reset 悄悄打开
    "$HI3FXMain",          # filter_index 常量
    "$HI3FXHelper",
    "$HI3FXRemap",
}
# 只由 SetTextures 使用、由 Clean 兜底的槽位；不参与"每绘制解绑"的强约束。
REMAP_SLOTS = {50, 51, 52, 53, 54, 55}
# 每绘制绑定的遮罩槽位：必须有 Present post + ShaderRegex post + Unbind + Clean 四重清零。
MASK_SLOTS = (60, 61, 62)


class IniFile:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.namespace = ""
        self.sections = {}          # 段名 -> [行]
        self.section_order = []
        self.duplicate_sections = []  # 本文件内重复
        self._parse()

    def _parse(self):
        current = None
        with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n").rstrip("\r")
                match = SECTION_RE.match(line.strip())
                if match:
                    name = match.group("name").strip()
                    if name in self.sections:
                        self.duplicate_sections.append(name)
                    else:
                        self.sections[name] = []
                        self.section_order.append(name)
                    current = name
                    continue
                if current is None:
                    ns = NAMESPACE_RE.match(line)
                    if ns:
                        self.namespace = ns.group("ns")
                    continue
                self.sections[current].append(line.strip())

    def body(self, section):
        return self.sections.get(section, [])


def load_dir(directory):
    files = []
    for entry in sorted(os.listdir(directory)):
        path = os.path.join(directory, entry)
        if not os.path.isfile(path):
            continue
        if not entry.lower().endswith(".ini"):
            continue
        files.append(IniFile(path))
    return files


# ---------------------------------------------------------------- 检查框架

class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def check(self, ok, title, detail=""):
        mark = "PASS" if ok else "FAIL"
        self.lines.append("[{}] {}{}".format(mark, title, "  -- " + detail if detail else ""))
        if not ok:
            self.failures.append(title + ("  -- " + detail if detail else ""))
        return ok

    def info(self, text):
        self.lines.append("       " + text)

    def dump(self):
        for line in self.lines:
            print(line)
        print("")
        if self.failures:
            print("结果: FAIL（{} 项）".format(len(self.failures)))
            for failure in self.failures:
                print("  - " + failure)
        else:
            print("结果: PASS（全部检查通过）")
        return 1 if self.failures else 0


def section_slots(inifile, section):
    """段内出现的 ps-tNN 赋值：{slot: value}。"""
    result = {}
    for line in inifile.body(section):
        match = SLOT_RE.match(line)
        if match:
            result[int(match.group("slot"))] = match.group("value")
    return result


def shader_groups(inifile):
    """{组名: {"post_slots": set, "declared": set, "replace": [...], "asm": str}}

    ``asm`` 是去掉 ``${...}`` 占位符后的替换文本，专门给"结构自检"用：
    不删占位符的话，模式捕获组 ``${t1:...}`` 会被误当成纹理槽 t1。
    """
    groups = {}
    for name in inifile.section_order:
        if not name.startswith("ShaderRegex"):
            continue
        base = name
        for suffix in (".Pattern", ".Pattern.Replace", ".InsertDeclarations"):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                break
        group = groups.setdefault(
            base, {"post_slots": set(), "declared": set(), "replace": [], "insert": [], "asm": ""}
        )
        if name == base:
            for line in inifile.body(name):
                match = re.match(r"^post\s+ps-t(\d+)\s*=\s*null$", line, re.IGNORECASE)
                if match:
                    group["post_slots"].add(int(match.group(1)))
        elif name.endswith(".InsertDeclarations"):
            for line in inifile.body(name):
                match = DCL_RE.match(line)
                if match:
                    group["declared"].add(int(match.group("slot")))
                group["insert"].append(line)
        elif name.endswith(".Pattern.Replace"):
            group["replace"].extend(inifile.body(name))
    for group in groups.values():
        group["asm"] = SUBST_RE.sub("", "".join(group["replace"]))
    return groups


def command_list_targets(inifile, section):
    targets = []
    for line in inifile.body(section):
        match = RUN_RE.match(line)
        if match:
            targets.append(match.group("target").split("\\")[-1])
    return targets


# ---------------------------------------------------------------- 主检查

def run_checks(directory):
    report = Report()
    inifiles = load_dir(directory)
    report.info("扫描到 {} 个 ini: {}".format(len(inifiles), ", ".join(f.name for f in inifiles)))
    by_name = {f.name: f for f in inifiles}

    core = by_name.get("HI3FX.ini")
    main = by_name.get("HI3FX.Main.ini")
    helper = by_name.get("HI3FX.Helper.ini")
    if not core:
        report.check(False, "HI3FX.ini 存在")
        return report
    mask_files = [f for f in (main, helper) if f]

    # ---- 1. 备份文件不会被加载 ------------------------------------------
    stray = [f.name for f in inifiles if ".bak-" in f.name.lower()]
    report.check(not stray, "目录下没有会被加载的 .bak-* 备份", ", ".join(stray))

    # ---- 2. 重复段名 ------------------------------------------------------
    duplicates = []
    seen = {}
    for inifile in inifiles:
        for name in inifile.duplicate_sections:
            duplicates.append("{}/{}".format(inifile.name, name))
        for name in inifile.section_order:
            # 命名空间展开：段前缀 + \ + namespace + \ + 段名（3DMigoto 的大小写不敏感）
            expanded = name
            prefix_match = re.match(r"^(CommandList|Resource|TextureOverride|ShaderOverride|ShaderRegex|Key)", name)
            if prefix_match and inifile.namespace:
                head, _, tail = name.partition(prefix_match.group(1))
                expanded = "{}{}\\{}{}".format(
                    head, prefix_match.group(1), inifile.namespace, tail[len(prefix_match.group(1)):]
                )
            key = expanded.casefold()
            if key in seen and seen[key][0] != inifile.name:
                duplicates.append("{}:{} 与 {}:{} 展开后同名".format(inifile.name, name, seen[key][0], seen[key][1]))
            else:
                seen.setdefault(key, (inifile.name, name))
    report.check(not duplicates, "没有重复段名（3DMigoto 只解析第一个）", "; ".join(duplicates))

    # ---- 3. 绑定 / 解绑成对 ----------------------------------------------
    bind_slots = section_slots(core, "CommandListBind")
    ref_slots = {slot for slot, value in bind_slots.items() if not value.lower().startswith("null")}
    unbound = section_slots(core, "CommandListUnbind")
    cleaned = section_slots(core, "CommandListClean")
    present_post = set()
    for line in core.body("Present"):
        match = re.match(r"^post\s+ps-t(\d+)\s*=\s*null$", line, re.IGNORECASE)
        if match:
            present_post.add(int(match.group(1)))

    for slot in sorted(ref_slots):
        in_unbind = unbound.get(slot, "").lower().startswith("null")
        in_clean = cleaned.get(slot, "").lower().startswith("null")
        in_present = slot in present_post
        if slot in REMAP_SLOTS:
            report.check(in_clean, "ps-t{}（重映射槽）有 Clean 兜底".format(slot))
            continue
        report.check(
            in_unbind and in_clean and in_present,
            "ps-t{} 绑定了就有三重解绑".format(slot),
            "Unbind={} Clean={} Present.post={}".format(in_unbind, in_clean, in_present),
        )

    # 读了槽的 ShaderRegex 组必须自己 post 清零
    for inifile in mask_files:
        for group_name, group in sorted(shader_groups(inifile).items()):
            text = group["asm"]
            read_slots = {int(m.group("slot")) for m in TSLOT_REF_RE.finditer(text)} & set(MASK_SLOTS)
            for slot in sorted(read_slots):
                report.check(
                    slot in group["post_slots"],
                    "{}/{} 读了 ps-t{} 且 post 清零".format(inifile.name, group_name, slot),
                )

    # ---- 4. 读的槽必须 dcl 过 --------------------------------------------
    for inifile in mask_files:
        for group_name, group in sorted(shader_groups(inifile).items()):
            text = group["asm"]
            read_slots = {int(m.group("slot")) for m in TSLOT_REF_RE.finditer(text)}
            # t120 由 3DMigoto 自己提供，不要求在 InsertDeclarations 里 dcl。
            read_slots.discard(120)
            missing = sorted(read_slots - group["declared"])
            report.check(
                not missing,
                "{}/{} 引用的槽都已 dcl".format(inifile.name, group_name),
                "缺 dcl: {}".format(missing),
            )

    # ---- 5. 汇编结构：if/endif 配对 + 括号式嵌套 --------------------------
    for inifile in mask_files:
        for group_name, group in sorted(shader_groups(inifile).items()):
            text = group["asm"]
            openings = len(IF_RE.findall(text))
            closings = len(ENDIF_RE.findall(text))
            report.check(
                openings == closings,
                "{}/{} 的 if/endif 配对".format(inifile.name, group_name),
                "if={} endif={}".format(openings, closings),
            )
            # 数量相等不代表嵌套正确（多一个早退 endif 也能凑数），用栈再走一遍。
            depth = 0
            bad_at = None
            for match in re.finditer(r"if_(?:nz|z)\b|endif\b", text, re.IGNORECASE):
                if match.group(0).lower().startswith("if_"):
                    depth += 1
                else:
                    depth -= 1
                    if depth < 0 and bad_at is None:
                        bad_at = match.start()
            report.check(
                bad_at is None and depth == 0,
                "{}/{} 的 if/endif 嵌套合法".format(inifile.name, group_name),
                "提前 endif @{} 收尾深度 {}".format(bad_at, depth),
            )

    # ---- 6. 别名成对（FXMap / TTLMap） -----------------------------------
    aliases = ["ResourceFXMap", "ResourceTTLMap"]
    for alias in aliases:
        declared = alias in core.sections
        bound = any(
            line.startswith("if {} !== null".format(alias)) for line in core.body("CommandListBind")
        )
        report.check(
            declared and bound,
            "{} 有空段声明且被 Bind 使用".format(alias),
            "declared={} bound={}".format(declared, bound),
        )
    for alias in aliases:
        report.check(
            "{} = null".format(alias) in core.body("CommandListReset"),
            "{} 在 Reset 里被清空".format(alias),
        )
    ttlmap_alias_present = "ResourceTTLMap" in core.sections
    report.check(ttlmap_alias_present, "HI3FX.ini 声明了 ResourceTTLMap（新版别名）")

    # ---- 7. Reset 覆盖全部变量 -------------------------------------------
    reset_body = core.body("CommandListReset")
    reset_assigned = set()
    for line in reset_body:
        match = re.match(r"^(?P<var>\$[\w\\]+)\s*=", line)
        if match:
            reset_assigned.add(match.group("var"))
    missing_reset = []
    for line in core.body("Constants"):
        match = PERSIST_RE.match(line)
        if not match:
            continue
        var = match.group("var")
        if var in RESET_ALLOWED_SKIPS:
            continue
        if var not in reset_assigned:
            missing_reset.append(var)
    report.check(not missing_reset, "[CommandListReset] 覆盖所有 FX 变量", "缺: {}".format(missing_reset))

    # ---- 8. 新增 IniParams 字段 w233 -------------------------------------
    has_default = "w233 = 0.5" in core.body("Constants")
    has_commit = "w233 = $fx_cutoff" in core.body("CommandListCommit")
    report.check(has_default and has_commit, "w233（FXMap 二值化阈值）有默认值且每帧提交",
                 "Constants={} Commit={}".format(has_default, has_commit))
    persist_cutoff = any("$fx_cutoff" in line for line in core.body("Constants"))
    report.check(persist_cutoff, "[Constants] 定义了 $fx_cutoff")
    for inifile in mask_files:
        for group_name, group in sorted(shader_groups(inifile).items()):
            text = group["asm"]
            if "t61" not in text:
                continue  # 签名预处理组不读遮罩，不适用
            uses_row = "l(233, 0)" in text
            report.check(uses_row, "{}/{} 用了 row233（fx_cutoff）".format(inifile.name, group_name))
            reads_ttl = "t62" in text
            report.check(reads_ttl, "{}/{} 同组也读 t62（TTLMap）".format(inifile.name, group_name))

    # ---- 9. TTLMap 语义：FXMap 侧必须二值化、TTL 侧必须参与 dither --------
    # 注意这里用**未剥离占位符**的原文：这些断言正是要核对 ${mask}/${treg2} 的用法。
    for inifile in mask_files:
        for group_name, group in sorted(shader_groups(inifile).items()):
            raw = "".join(group["replace"])
            if "t61" not in raw:
                continue
            # FXMap 被截断成 0/1：ge + movc 紧跟 FXMap 的读入
            has_truncate = bool(re.search(r"ge\s+\$\{treg2\}\.y,\s*\$\{mask\}\.w,\s*\$\{dsc\}\.w", raw))
            has_movc = bool(re.search(r"movc\s+\$\{mask\}\.w,\s*\$\{treg2\}\.y,\s*l\(1\.000000\),\s*l\(0\.000000\)", raw))
            has_ttl_mul = bool(re.search(r"mul\s+\$\{mask\}\.w,\s*\$\{mask\}\.w,\s*\$\{treg3\}\.w", raw))
            report.check(has_truncate and has_movc, "{}/{} FXMap 二值化（ge+movc）".format(inifile.name, group_name))
            report.check(has_ttl_mul, "{}/{} TTLMap 覆盖率乘进 mask".format(inifile.name, group_name))
            # 二值化必须发生在抖动之前
            truncate_at = raw.find("movc ${mask}.w")
            dither_at = raw.find("if_nz ${opt}.w")
            report.check(
                0 <= truncate_at < dither_at,
                "{}/{} 二值化在抖动之前".format(inifile.name, group_name),
                "truncate@{} dither@{}".format(truncate_at, dither_at),
            )

    # ---- 10. 运行/使用方法未变：Run 仍只做 Commit+Bind -------------------
    run_body = " | ".join(core.body("CommandListRun"))
    report.check(
        "Commit" in run_body and "Bind" in run_body and "Reset" not in run_body,
        "[CommandListRun] 仍是 Commit + Bind（不复位变量）",
        run_body,
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="HI3FX 运行时静态自检")
    parser.add_argument(
        "directory",
        nargs="?",
        default=r"K:\SSMT-Package-master\3Dmigoto\HI3\Mods\HI3FX",
        help="HI3FX mod 目录（含 HI3FX.ini）",
    )
    args = parser.parse_args(argv)
    if not os.path.isdir(args.directory):
        print("目录不存在: {}".format(args.directory))
        return 2
    print("HI3FX 静态自检: {}\n".format(args.directory))
    return run_checks(args.directory).dump()


if __name__ == "__main__":
    sys.exit(main())
