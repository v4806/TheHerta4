# -*- coding: utf-8 -*-
"""原神（GIMI）ORFix 导出适配。

本模块只负责一件事：把 INI 段里 ``run = CommandList\\global\\ORFix\\ORFix``（或
``NNFix``）的调用位置，从段首移动到“本次绘制真正使用的贴图”绑定之后的第一次绘制前。

为什么需要
----------
ORFix 会读取 ps-t0/ps-t1/ps-t2 并按当前渲染通道重排槽位：

* 导出器把 ORFix 写在段首，而材质转资源把自定义贴图写进 mesh 块内，ORFix 抓到的就是
  旧绑定，表现为角色界面整块通红/发光；
* 反过来，同一套绑定连续执行多次 ORFix，会把槽位反复重排（第二次相当于又转了一圈），
  未指定贴图的部件因此被污染发光。

所以规则是“每次贴图输入变化后只执行一次”。

适用范围
--------
只有在段内检测到原神 ORFix 写法（ORFix/NNFix 调用）时才会生效。绝区零(ZZZ)、崩铁、
NTEMI 等其它游戏的 INI 不存在这种写法，本模块对它们的输出没有任何影响；本模块也不
依赖 bpy 或 GlobalConfig，可以单独测试。
"""
import re

# 原神 ORFix / NNFix 调用行。必须第 0 列：gimi.py 是按段级行输出的
# （M_IniBuilder 直接接 ``line + "\n"``，不加缩进）；缩进的 run 属于 if 分支内部，
# 是作者手写的分支级调用，重排会把它删掉/改语义，因此不参与重排。
ORFIX_RUN_RE = re.compile(
    r'^run\s*=\s*CommandList\\global\\ORFix\\(?:ORFix|NNFix)\s*$',
    re.IGNORECASE,
)
# 绘制命令（裸命令，用于判定“是否顶格绘制”；保留缩进用于分支内原样复刻）
DRAW_COMMAND_BARE_RE = re.compile(
    r'^(?:drawindexed|drawindexedinstanced|draw)\s*=',
    re.IGNORECASE,
)
DRAW_COMMAND_RE = re.compile(
    r'^(\s*)(?:drawindexed|drawindexedinstanced|draw)\s*=',
    re.IGNORECASE,
)
# ORFix 实际读取的三个贴图槽位
TEXTURE_INPUT_RE = re.compile(r'^\s*ps-t[012]\s*=', re.IGNORECASE)
# 控制流关键字：只认顶格（第 0 列）的写法，才能和段内无条件绘制区分开。
# `if`/`while` 后面的标识符边界由 lookahead 保证：``if $x == 1``、``if($x)`` 算条件，
# 而 ``iffy = 1``、``while_x = 1`` 这类变量名不算。`else`/`elseif` 以自身结尾。
CTRL_OPEN_RE = re.compile(r'^(?:if|while)\b(?![A-Za-z0-9_])', re.IGNORECASE)
CTRL_CLOSE_RE = re.compile(r'^(?:endif|endwhile)\b(?![A-Za-z0-9_])', re.IGNORECASE)
CTRL_BRANCH_RE = re.compile(r'^(?:else|elseif)\b(?![A-Za-z0-9_])', re.IGNORECASE)


def uses_orfix(lines):
    """判断该段是否使用原神 ORFix 写法。"""
    return any(ORFIX_RUN_RE.match(str(line or "")) for line in lines or [])


def place_orfix_runs(lines):
    """按“每次贴图输入变化后执行一次”重排段内 ORFix 调用位置。

    第一次调用必须落在**整个段的第一条绘制之前**，而不是“带缩进的那条绘制之前”：
    段内绘制会按 WorkKey 条件被包进 ``if``/``else``/``endif`` 块（见
    ``common/m_ini_helper.py`` 的 ``get_drawindexed_str_list``），若把 ORFix 插进
    首个 ``if`` 分支内，条件不成立时它会整段不执行——比原来的段首无条件执行更糟。

    返回新的行列表；不满足条件时（段内没有绘制、缺少 ORFix、ORFix 种类不止一种）
    原样返回，保证非原神段落的输出逐字节不变。
    """
    lines = list(lines or [])
    if not any(DRAW_COMMAND_RE.match(str(line)) for line in lines):
        return lines

    fix_indexes = [
        index for index, line in enumerate(lines)
        if ORFIX_RUN_RE.match(str(line))
    ]
    if not fix_indexes:
        return lines
    fix_texts = {str(lines[index]).strip() for index in fix_indexes}
    if len(fix_texts) != 1:
        return lines
    fix_text = fix_texts.pop()
    # fix_text 已 strip 过，重新放置时固定落在第 0 列，与 gimi.py 的输出一致。
    fix_index_set = set(fix_indexes)
    # 段尾（最后一条绘制之后）原本就带着 ORFix 的（例如材质转资源的恢复块），
    # 收尾时要原样补回：它负责让段结束时槽位回到“已修正”状态。
    # 判据是“还有没被重新放置的 ORFix”，而不是猜测尾部语义——
    # 段首那一次只有一个 ORFix 时，它已经放到第一条绘制之前，这里就不会重复补。
    placed_fix_count = 0

    placed_lines = []
    fix_pending = True
    # 段首那一次 ORFix 是否已经放置过（落点 = 第一条顶格控制流/绘制之前）。
    leading_fix_placed = False
    block_depth = 0
    for index, line in enumerate(lines):
        if index in fix_index_set:
            continue
        text = str(line)
        stripped = text.strip()
        is_top_level_draw = bool(DRAW_COMMAND_BARE_RE.match(text))
        is_ctrl_open = bool(CTRL_OPEN_RE.match(text))

        if CTRL_CLOSE_RE.match(text):
            block_depth = max(0, block_depth - 1)

        if TEXTURE_INPUT_RE.match(stripped):
            # 贴图输入变了：下一次绘制之前必须重新执行一次 ORFix
            fix_pending = True
            placed_lines.append(line)
            continue

        if CTRL_BRANCH_RE.match(text):
            if block_depth == 0:
                if not leading_fix_placed:
                    # 首个绘制就在分支结构里：ORFix 必须落在 `if` 之前，
                    # 否则条件不成立时它整段不执行。
                    placed_lines.append(fix_text)
                    fix_pending = False
                    leading_fix_placed = True
                    placed_fix_count += 1
                elif fix_pending:
                    # 上一分支（或段首）的贴图输入在本分支同样生效，补一次。
                    placed_lines.append(fix_text)
                    fix_pending = False
                    placed_fix_count += 1
            placed_lines.append(line)
            block_depth += 1
            continue

        # 段首那一次的无条件落点：第 0 列的 `if`/`while` 或第 0 列的绘制之前。
        if block_depth == 0 and not leading_fix_placed and (is_top_level_draw or is_ctrl_open):
            placed_lines.append(fix_text)
            fix_pending = False
            leading_fix_placed = True
            placed_fix_count += 1

        if is_top_level_draw and fix_pending:
            # 后续顶格绘制前贴图输入变过，同样要补一次
            placed_lines.append(fix_text)
            fix_pending = False
            placed_fix_count += 1

        draw_match = DRAW_COMMAND_RE.match(text)
        if draw_match and not is_top_level_draw and fix_pending:
            # 分支内的绘制：保持缩进原地插入
            placed_lines.append("{}{}".format(draw_match.group(1), fix_text))
            fix_pending = False
            placed_fix_count += 1
        placed_lines.append(line)

        if is_ctrl_open:
            block_depth += 1

    # 还有 ORFix 没被重新放置，说明原本它在最后一条绘制之后（恢复块写法），补回段尾。
    if placed_fix_count < len(fix_indexes):
        placed_lines.append(fix_text)

    return placed_lines
