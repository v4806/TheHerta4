# -*- coding: utf-8 -*-
"""形态键权重值域的唯一事实来源（Python 侧）。

为什么有这个文件：形态键权重原本在三条链路上各自被夹到 0..1 ——

  - shader 同步：Toolset/shapekey_weight_sync.hlsl 把 IniParams 里的强度夹成 0..1；
  - 游戏内滑条：blueprint/node_postprocess_shapekey_ext.py 生成的 Present 代码
    直接把「滑块位置 0..1」赋给 $Freq_xxx；
  - Blender 统一控制器：toolkit/at_shape_key_control.py 在写值前夹到值域
    （默认 0..1）。

于是 ini 里写 `$Freq_xxx = 1.6` 会被压成 1.0，写 `-0.4` 会被压成 0.0。

现在拆成**两个不同概念**，不再混用：

1. 硬边界 ``SHAPEKEY_WEIGHT_MIN`` / ``SHAPEKEY_WEIGHT_MAX``（默认 -4.0 ~ 4.0）
   —— shader 一定会放行的区间。它不是「形态键的合理范围」，只用来挡住
   inf/NaN 和彻底失控的数值。取 ±4.0 的理由：1.6 / -0.4 这类外推在模组里
   是常见幅度，乘 2~5 倍后仍在界内；同时 review-reports/t83-boom-forensics.md
   记录过权重把模型炸穿的事故（anim CS 是 delta * w 线性外推，w 越大位移越
   远），±4 把最坏情况压到「4 倍顶点位移」，界内拖动还能救回来。

2. 滑块窗口 ``SLIDER_WINDOW_MIN`` / ``SLIDER_WINDOW_MAX``（默认 0.0 ~ 1.0）
   —— 游戏内面板的行程。窗口内线性可调；**默认等于放开前的 0..1**，所以
   现有工程的产物逐字节不变。要 >1 / <0 就在节点里把窗口改宽，或者直接在
   ini 里把 $Freq_xxx 写成 1.6（shader 侧的硬边界会放行）。

改这里的数字时必须同步改 5 个 hlsl 文件里的同名 define（shader 没法 import
Python，只能镜像，tests/test_shapekey_weight_range_contract.py 会核对）：

    Toolset/shapekey_weight_sync.hlsl
    Toolset/drag_interaction/rzm_shapekey_var_sync.hlsl
    Toolset/drag_interaction/rzm_shapekey_drive.hlsl
    Toolset/drag_interaction_efmi/efmi_shapekey_var_sync.hlsl
    Toolset/drag_interaction_efmi/efmi_shapekey_drive.hlsl
"""

# 硬边界：shader 放行的绝对值上限（见模块 docstring 的取值理由）
SHAPEKEY_WEIGHT_MIN = -4.0
SHAPEKEY_WEIGHT_MAX = 4.0

# 滑块窗口默认值：与放开前一致，保证老产物不变
SLIDER_WINDOW_MIN = 0.0
SLIDER_WINDOW_MAX = 1.0

# 判断「窗口是否等于旧 0..1」的容差
_WINDOW_EPS = 1e-6


def format_ini_number(value):
    """把数字写成 3DMigoto ini 表达式里的浮点字面量（保证带小数点）。"""
    text = f"{float(value):.6f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return text or "0.0"


def normalize_window(window_min, window_max):
    """返回有效的 (min, max)；非法（max <= min / 非数字）时回落到默认窗口。"""
    try:
        value_min = float(window_min)
        value_max = float(window_max)
    except (TypeError, ValueError):
        return SLIDER_WINDOW_MIN, SLIDER_WINDOW_MAX
    if not (value_max > value_min):
        return SLIDER_WINDOW_MIN, SLIDER_WINDOW_MAX
    return value_min, value_max


def is_identity_window(window_min, window_max):
    """窗口等于 0..1 时为 True：生成端据此保留旧写法，产物逐字节不变。"""
    return (abs(float(window_min)) < _WINDOW_EPS
            and abs(float(window_max) - 1.0) < _WINDOW_EPS)


def weight_from_param_expr(param_var, window_min, window_max):
    """`$Freq_x = <本函数返回值>`：窗口内位置 -> 权重（可越过 0/1）。"""
    value_min, value_max = normalize_window(window_min, window_max)
    if is_identity_window(value_min, value_max):
        return param_var
    span = value_max - value_min
    if value_min < 0.0:
        # 写成 `- 0.5` 而不是 `+ -0.5`：ini 表达式解析器不一定乐意接一元负号
        return (f"{param_var} * {format_ini_number(span)}"
                f" - {format_ini_number(-value_min)}")
    return (f"{param_var} * {format_ini_number(span)}"
            f" + {format_ini_number(value_min)}")


def param_from_weight_expr(weight_var, window_min, window_max):
    """`$paramN = <本函数返回值>`：权重 -> 窗口内位置（可能越出 0..1）。

    越界是**正常**的（权重被写成 1.6 而窗口是 0..1），调用方负责把显示用的
    $rel_x 夹回面板，不要把权重本身夹掉。
    """
    value_min, value_max = normalize_window(window_min, window_max)
    if is_identity_window(value_min, value_max):
        return weight_var
    span = value_max - value_min
    if value_min < 0.0:
        # `- -0.5` 换成 `+ 0.5`，理由同上
        return (f"({weight_var} + {format_ini_number(-value_min)})"
                f" / {format_ini_number(span)}")
    return (f"({weight_var} - {format_ini_number(value_min)})"
            f" / {format_ini_number(span)}")
