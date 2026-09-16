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

# 原神 ORFix / NNFix 调用行
ORFIX_RUN_RE = re.compile(
    r'^\s*run\s*=\s*CommandList\\global\\ORFix\\(?:ORFix|NNFix)\s*$',
    re.IGNORECASE,
)
# 绘制命令（保留缩进，便于在 if/else 分支内保持缩进）
DRAW_COMMAND_RE = re.compile(
    r'^(\s*)(?:drawindexed|drawindexedinstanced|draw)\s*=',
    re.IGNORECASE,
)
# ORFix 实际读取的三个贴图槽位
TEXTURE_INPUT_RE = re.compile(r'^\s*ps-t[012]\s*=', re.IGNORECASE)


def uses_orfix(lines):
    """判断该段是否使用原神 ORFix 写法。"""
    return any(ORFIX_RUN_RE.match(str(line or "")) for line in lines or [])


def place_orfix_runs(lines):
    """按“每次贴图输入变化后执行一次”重排段内 ORFix 调用位置。

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

    last_draw = max(
        (index for index, line in enumerate(lines) if DRAW_COMMAND_RE.match(str(line))),
        default=-1,
    )
    # 段尾（最后一条绘制之后）原本就带着 ORFix 的（例如材质转资源的恢复块），
    # 重新放置后也要补回段尾，保证段结束时槽位处于“已修正”状态。
    trailing_fix = fix_indexes[-1] > last_draw
    fix_index_set = set(fix_indexes)

    placed_lines = []
    fix_pending = True
    for index, line in enumerate(lines):
        if index in fix_index_set:
            continue
        if TEXTURE_INPUT_RE.match(str(line).strip()):
            # 贴图输入变了：下一次绘制之前必须重新执行一次 ORFix
            fix_pending = True
            placed_lines.append(line)
            continue
        draw_match = DRAW_COMMAND_RE.match(str(line))
        if draw_match and fix_pending:
            placed_lines.append("{}{}".format(draw_match.group(1), fix_text))
            fix_pending = False
        placed_lines.append(line)

    if trailing_fix and fix_pending:
        insert_at = len(placed_lines)
        while insert_at > 0:
            tail = str(placed_lines[insert_at - 1]).strip()
            if not tail or tail.upper().startswith(";MARK:"):
                insert_at -= 1
                continue
            break
        placed_lines.insert(insert_at, fix_text)
    return placed_lines
