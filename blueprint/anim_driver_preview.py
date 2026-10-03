"""动画驱动关键帧预览。

把动画驱动蓝图生成的 INI 文本当作**程序**解释执行，得到每个驱动变量在每一帧
的取值，再写回 Blender（形态键权重 / 场景自定义属性）并打成关键帧，让动画可以
在时间轴上直接拖看。

设计要点
--------
* 解释对象是 ``AnimationDriverCollector.collect()`` 的产物 —— 与出货的配置表同源
  （已做重名声明归一与形态键引用对齐）。不读节点字段另算一套语义，因此预览与
  游戏里的实际推进不会漂移。
* 时间基：**1 场景帧 = 1 驱动帧**。运行时间节点生成的 ``$x = (time * fps) // 1``
  被识别成「帧计数器」，每帧直接写入当前帧号 —— 不依赖浮点舍入，也不会因为场景
  帧率与节点帧率不同而错位。``time`` 本身仍按 ``帧 / fps`` 求值。
* ``$active0`` 门控是导出外壳（``SSMTNode_PostProcess_AnimDriver``）在插入配置表时
  才加的，预览假定角色在场，等价于跳过该门控。
* 预览帧数上限 ``ANIM_DRIVER_PREVIEW_MAX_FRAMES``（300 帧）。
* **作用域**：点某个节点的「生成」只写**该节点自己驱动的那几个变量**的关键帧，
  不会把同一蓝图里其它驱动节点的变量一并烘进去。解释范围仍是整份驱动段
  （链上的运行时间、暂停变量、方向变量要参与推进；同一变量被多个节点写时
  也要按"谁最后写谁生效"复现），只有**写关键帧**的范围是节点级的。
* 目前开放预览的节点：索引播放 / 往返播放 / 随机驱动。其余节点的变量仍会被解释
  （链上的运行时间、暂停变量、方向变量都要参与），只是不写关键帧。
"""

from __future__ import annotations

import math
import re
import struct

import bpy
from bpy.props import IntProperty, StringProperty
from bpy.types import Operator

from .anim_driver_base import (
    ANIM_DRIVER_PREVIEW_MAX_FRAMES,
    SSMTNode_AnimDriver_Base,
)
from .anim_driver_collector import AnimationDriverCollector

try:  # 与采集器同口径：裁剪/测试桩环境缺该 API 时退回「无别名」行为
    from .variable_registry import (
        build_shape_key_reference_alias_map,
        normalize_variable_name,
        resolve_reference_variable_name,
    )
except ImportError:  # pragma: no cover - 仅测试桩/裁剪环境命中
    def build_shape_key_reference_alias_map(context=None):
        return {}

    def resolve_reference_variable_name(name, alias_map=None):
        return str(name or "").strip().lstrip("$")

    def normalize_variable_name(name):
        return str(name or "").strip().lstrip("$")


ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES = ANIM_DRIVER_PREVIEW_MAX_FRAMES
ANIM_DRIVER_PREVIEW_START_FRAME = 1
ANIM_DRIVER_PREVIEW_ACTION_PREFIX = "AnimDriverPreview"
ANIM_DRIVER_PREVIEW_PROP_PREFIX = "anim_preview_"

#: 开放「写关键帧」的驱动节点类型。链上的其它节点仍会被解释（帧变量、暂停变量、
#: 方向变量是推进所需的状态），只是不产出关键帧。
PREVIEW_SUPPORTED_NODE_TYPES = (
    'SSMTNode_AnimDriver_ForwardPlay',
    'SSMTNode_AnimDriver_PingPong',
    'SSMTNode_AnimDriver_Random',
)

_RANDOM_NODE_TYPE = 'SSMTNode_AnimDriver_Random'
_RUNTIME_NODE_TYPE = 'SSMTNode_AnimDriver_Runtime'


# ---------------------------------------------------------------------------
# 数值语义：3DMigoto 的变量是 32 位浮点
# ---------------------------------------------------------------------------

def to_float32(value):
    """按 32 位浮点往返一次。

    随机驱动的 LCG 依赖 ``x < 2^24`` 的整数在 float32 下逐位精确；解释器如果
    用 float64 一路算下去，中间结果与出货端一致，但一旦引入除法/取模的舍入
    差异就会分叉。统一在这里过一遍，保证与 3DMigoto 同语义。
    """
    try:
        return struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except (OverflowError, struct.error, TypeError, ValueError):
        return float(value)


# ---------------------------------------------------------------------------
# 表达式：分词 / 解析 / 求值（3DMigoto 命令列表的算术子集）
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<num>\d+\.\d*|\.\d+|\d+)"
    r"|(?P<op>//|&&|\|\||==|!=|<=|>=|[+\-*/%<>()])"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
)

_COMPARISON_OPERATORS = ('==', '!=', '<=', '>=', '<', '>')
_ADDITIVE_OPERATORS = ('+', '-')
_MULTIPLICATIVE_OPERATORS = ('*', '/', '//', '%')


def tokenize_ini_expression(text) -> list:
    tokens = []
    source = str(text or "")
    position = 0
    while position < len(source):
        match = _TOKEN_RE.match(source, position)
        if match is None:
            raise ValueError(f"无法识别的字符 {source[position]!r}（{source!r}）")
        position = match.end()
        if match.lastgroup == "ws":
            continue
        tokens.append((match.lastgroup, match.group()))
    return tokens


class _ExpressionParser:
    """递归下降解析；AST 用元组表示，避免 eval 任何输入文本。"""

    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0

    def _peek(self):
        if self.index < len(self.tokens):
            return self.tokens[self.index]
        return (None, None)

    def _peek_operator(self):
        kind, text = self._peek()
        return text if kind == "op" else None

    def _accept_operator(self, operator):
        if self._peek_operator() == operator:
            self.index += 1
            return True
        return False

    def parse(self):
        node = self._parse_or()
        if self.index != len(self.tokens):
            kind, text = self._peek()
            raise ValueError(f"表达式尾部有多余内容 {text!r}")
        return node

    def _parse_or(self):
        node = self._parse_and()
        while self._accept_operator("||"):
            node = ("bin", "||", node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_comparison()
        while self._accept_operator("&&"):
            node = ("bin", "&&", node, self._parse_comparison())
        return node

    def _parse_comparison(self):
        node = self._parse_additive()
        while self._peek_operator() in _COMPARISON_OPERATORS:
            operator = self._peek_operator()
            self.index += 1
            node = ("bin", operator, node, self._parse_additive())
        return node

    def _parse_additive(self):
        node = self._parse_multiplicative()
        while self._peek_operator() in _ADDITIVE_OPERATORS:
            operator = self._peek_operator()
            self.index += 1
            node = ("bin", operator, node, self._parse_multiplicative())
        return node

    def _parse_multiplicative(self):
        node = self._parse_unary()
        while self._peek_operator() in _MULTIPLICATIVE_OPERATORS:
            operator = self._peek_operator()
            self.index += 1
            node = ("bin", operator, node, self._parse_unary())
        return node

    def _parse_unary(self):
        if self._accept_operator("-"):
            return ("neg", self._parse_unary())
        if self._accept_operator("+"):
            return self._parse_unary()
        return self._parse_primary()

    def _parse_primary(self):
        kind, text = self._peek()
        if kind == "num":
            self.index += 1
            return ("num", float(text))
        if kind == "var":
            self.index += 1
            return ("var", text)
        if kind == "ident":
            self.index += 1
            if text == "time":
                return ("time",)
            raise ValueError(f"不支持的内建标识符 {text!r}")
        if kind == "op" and text == "(":
            self.index += 1
            node = self._parse_or()
            if not self._accept_operator(")"):
                raise ValueError("括号不匹配")
            return node
        raise ValueError("表达式不完整")


def parse_ini_expression(text):
    tokens = tokenize_ini_expression(text)
    if not tokens:
        raise ValueError("空表达式")
    return _ExpressionParser(tokens).parse()


def is_frame_counter_expression(ast) -> bool:
    """匹配运行时间节点生成的 ``(time * fps) // 1``（也接受 ``/ 1``）。"""
    if not isinstance(ast, tuple) or ast[0] != "bin" or ast[1] not in ("//", "/"):
        return False
    inner, divisor = ast[2], ast[3]
    if not (isinstance(divisor, tuple) and divisor[0] == "num" and divisor[1] == 1.0):
        return False
    if not (isinstance(inner, tuple) and inner[0] == "bin" and inner[1] == "*"):
        return False
    left, right = inner[2], inner[3]
    return (left[0] == "time" and right[0] == "num") or (right[0] == "time" and left[0] == "num")


# ---------------------------------------------------------------------------
# 解释器
# ---------------------------------------------------------------------------

_GLOBAL_DECL_RE = re.compile(
    r"^\s*global(?:\s+persist)?\s+(\$[A-Za-z_][A-Za-z0-9_]*)\s*(?:=(?!=)\s*(.*?))?\s*$"
)
_ASSIGN_RE = re.compile(r"^\s*(\$[A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.*?)\s*$")
_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")


class AnimationDriverSimulationContext:
    """一次模拟的全局状态（3DMigoto 的 global 变量在整份配置表里是全局的）。"""

    def __init__(self):
        self.variables = {}
        self.declared = set()
        self.reads = set()
        self.frame_variables = set()
        self.skipped_commands = []
        self.warnings = []

    def read(self, name):
        self.reads.add(name)
        return self.variables.get(name, 0.0)

    def warn(self, message):
        text = str(message or "")
        if text and text not in self.warnings:
            self.warnings.append(text)

    def skip(self, line):
        text = str(line or "").strip()
        if text and text not in self.skipped_commands:
            self.skipped_commands.append(text)


def strip_ini_comment(line) -> str:
    return str(line or "").split(";", 1)[0]


def split_ini_sections(text):
    """按 ``[段名]`` 切分文本；返回 (小写段名 → 行列表, 出现顺序)。"""
    sections = {}
    order = []
    current = None
    for raw_line in str(text or "").split("\n"):
        match = _SECTION_RE.match(raw_line.strip())
        if match:
            current = match.group(1).strip().lower()
            if current not in sections:
                sections[current] = []
                order.append(current)
            continue
        if current is None:
            continue
        sections[current].append(raw_line)
    return sections, order


def evaluate_expression(ast, context):
    kind = ast[0]
    if kind == "num":
        return ast[1]
    if kind == "var":
        return context.read(ast[1])
    if kind == "time":
        return context.read("time")
    if kind == "neg":
        return to_float32(-evaluate_expression(ast[1], context))

    operator = ast[1]
    left = evaluate_expression(ast[2], context)
    right = evaluate_expression(ast[3], context)

    if operator == "+":
        return to_float32(left + right)
    if operator == "-":
        return to_float32(left - right)
    if operator == "*":
        return to_float32(left * right)
    if operator in ("/", "//", "%"):
        if right == 0:
            context.warn(f"表达式出现除以 0（{operator}），按 0 处理")
            return 0.0
        if operator == "/":
            return to_float32(left / right)
        if operator == "//":
            return to_float32(math.floor(left / right))
        return to_float32(math.fmod(left, right))
    if operator == "==":
        return 1.0 if left == right else 0.0
    if operator == "!=":
        return 1.0 if left != right else 0.0
    if operator == "<":
        return 1.0 if left < right else 0.0
    if operator == ">":
        return 1.0 if left > right else 0.0
    if operator == "<=":
        return 1.0 if left <= right else 0.0
    if operator == ">=":
        return 1.0 if left >= right else 0.0
    if operator == "&&":
        return 1.0 if (left != 0 and right != 0) else 0.0
    if operator == "||":
        return 1.0 if (left != 0 or right != 0) else 0.0
    raise ValueError(f"不支持的运算符 {operator!r}")


def apply_constant_lines(lines, context):
    """执行 ``[Constants]``：这些声明在配置表加载时各执行一次。"""
    for raw_line in lines or []:
        line = strip_ini_comment(raw_line).strip()
        if not line:
            continue
        declaration = _GLOBAL_DECL_RE.match(line)
        if declaration is None:
            # 资源/参数命令（post / x0 = $var / cs-t50 = copy …）与变量推进无关
            context.skip(line)
            continue
        name = declaration.group(1)
        value_text = (declaration.group(2) or "").strip()
        context.declared.add(name)
        if not value_text:
            context.variables.setdefault(name, 0.0)
            continue
        try:
            context.variables[name] = evaluate_expression(parse_ini_expression(value_text), context)
        except ValueError as error:
            context.warn(f"无法解析的初值（按 0 处理）: {line}（{error}）")
            context.variables.setdefault(name, 0.0)


def compile_present_lines(lines, context) -> list:
    """把 ``[Present]`` 编译成线性指令表（if/else/endif 用跳转表达）。"""
    instructions = []
    stack = []

    for raw_line in lines or []:
        line = strip_ini_comment(raw_line).strip()
        if not line:
            continue
        keyword = line.split(None, 1)[0].lower()
        rest = line[len(keyword):].strip()

        if keyword == "if":
            try:
                expression = parse_ini_expression(rest)
            except ValueError as error:
                context.warn(f"无法解析的条件（按恒假处理）: {line}（{error}）")
                expression = ("num", 0.0)
            instructions.append(("jump_if_false", expression, None))
            stack.append({"pending": len(instructions) - 1, "jumps": []})
            continue

        if keyword == "elif" or (keyword == "else" and rest.lower().startswith("if ")):
            if not stack:
                context.warn(f"多余的 {keyword}（没有匹配的 if），已忽略")
                continue
            if keyword == "else":
                rest = rest[3:].strip()
            try:
                expression = parse_ini_expression(rest)
            except ValueError as error:
                context.warn(f"无法解析的条件（按恒假处理）: {line}（{error}）")
                expression = ("num", 0.0)
            top = stack[-1]
            instructions.append(("jump", None))
            top["jumps"].append(len(instructions) - 1)
            if top["pending"] is not None:
                instructions[top["pending"]] = (
                    "jump_if_false",
                    instructions[top["pending"]][1],
                    len(instructions),
                )
            instructions.append(("jump_if_false", expression, None))
            top["pending"] = len(instructions) - 1
            continue

        if keyword == "else":
            if not stack:
                context.warn("多余的 else（没有匹配的 if），已忽略")
                continue
            top = stack[-1]
            instructions.append(("jump", None))
            top["jumps"].append(len(instructions) - 1)
            if top["pending"] is not None:
                instructions[top["pending"]] = (
                    "jump_if_false",
                    instructions[top["pending"]][1],
                    len(instructions),
                )
            top["pending"] = None
            continue

        if keyword == "endif":
            if not stack:
                context.warn("多余的 endif（没有匹配的 if），已忽略")
                continue
            end = len(instructions)
            top = stack.pop()
            if top["pending"] is not None:
                instructions[top["pending"]] = (
                    "jump_if_false",
                    instructions[top["pending"]][1],
                    end,
                )
            for jump_index in top["jumps"]:
                instructions[jump_index] = ("jump", end)
            continue

        if keyword == "store":
            # GPU→CPU 回读（点击计数缓冲），预览没有对应的 Blender 值
            instructions.append(("noop",))
            context.skip("store = …（GPU 回读，预览按不点击处理）")
            continue

        assignment = _ASSIGN_RE.match(line)
        if assignment is not None:
            name = assignment.group(1)
            try:
                expression = parse_ini_expression(assignment.group(2))
            except ValueError as error:
                context.warn(f"无法解析的赋值（已跳过）: {line}（{error}）")
                continue
            if is_frame_counter_expression(expression):
                context.frame_variables.add(name)
                instructions.append(("set_frame", name))
            else:
                instructions.append(("set", name, expression))
            continue

        instructions.append(("noop",))
        context.skip(line)

    if stack:
        context.warn(f"有 {len(stack)} 个 if 缺少 endif，已按段尾闭合处理")
        end = len(instructions)
        while stack:
            top = stack.pop()
            if top["pending"] is not None:
                instructions[top["pending"]] = (
                    "jump_if_false",
                    instructions[top["pending"]][1],
                    end,
                )
            for jump_index in top["jumps"]:
                instructions[jump_index] = ("jump", end)

    return instructions


def execute_instructions(instructions, context, frame_index):
    program_counter = 0
    total = len(instructions)
    while program_counter < total:
        instruction = instructions[program_counter]
        opcode = instruction[0]
        if opcode == "set":
            context.variables[instruction[1]] = evaluate_expression(instruction[2], context)
        elif opcode == "set_frame":
            # 运行时间节点的帧计数器：预览里 1 场景帧 = 1 驱动帧
            context.variables[instruction[1]] = float(frame_index)
        elif opcode == "jump_if_false":
            if evaluate_expression(instruction[1], context) == 0.0:
                program_counter = instruction[2]
                continue
        elif opcode == "jump":
            program_counter = instruction[1]
            continue
        program_counter += 1


def simulate_anim_driver_paragraphs(
    paragraphs,
    frame_count,
    fps=30,
    start_frame=ANIM_DRIVER_PREVIEW_START_FRAME,
):
    """解释驱动段落，返回 ``{变量: [每帧值]}`` 与诊断信息。"""
    context = AnimationDriverSimulationContext()
    programs = []
    for paragraph in paragraphs or []:
        text = str((paragraph or {}).get("ini_content") or "")
        sections, _order = split_ini_sections(text)
        apply_constant_lines(sections.get("constants") or [], context)
        programs.append(compile_present_lines(sections.get("present") or [], context))

    frame_count = max(0, int(frame_count or 0))
    start_frame = int(start_frame)
    fps = float(fps) if fps else 30.0

    history = []
    for offset in range(frame_count):
        context.variables["time"] = offset / fps
        # 驱动帧号从 0 起（与游戏一致）：播放速率是 ``帧 % 速率`` 的相位门控，
        # 若从 1 起，速率=2 这类节点会永远命中不了偶数帧而完全不动。
        # 场景帧 ``start_frame + offset`` 与驱动帧 ``offset`` 一一对应。
        for program in programs:
            execute_instructions(program, context, offset)
        history.append(dict(context.variables))

    names = set()
    for snapshot in history:
        names.update(snapshot.keys())
    names.discard("time")

    variables = {
        name: [snapshot.get(name, 0.0) for snapshot in history]
        for name in sorted(names)
    }
    return {
        "variables": variables,
        "declared": sorted(context.declared),
        "frame_variables": sorted(context.frame_variables),
        "undeclared_reads": sorted(context.reads - context.declared - {"time"}),
        "skipped_commands": list(context.skipped_commands),
        "warnings": list(context.warnings),
        "frame_count": frame_count,
        "start_frame": start_frame,
        "fps": fps,
    }


# ---------------------------------------------------------------------------
# 预览目标：驱动变量 → Blender 可写的属性
# ---------------------------------------------------------------------------

def clamp_preview_frame_count(frame_count) -> int:
    try:
        value = int(frame_count)
    except (TypeError, ValueError):
        value = ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES
    return max(1, min(value, ANIM_DRIVER_PREVIEW_MAX_FRAMES))


def resolve_preview_fps(tree) -> int:
    for node in getattr(tree, "nodes", []) or []:
        if getattr(node, "bl_idname", "") != _RUNTIME_NODE_TYPE:
            continue
        try:
            return max(1, int(getattr(node, "fps", 30) or 30))
        except (TypeError, ValueError):
            return 30
    return 30


def sanitize_preview_name(name) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", str(name or "").lstrip("$")) or "var"


def _resolve_variable_name(raw_name, alias_map) -> str:
    normalized = normalize_variable_name(raw_name)
    if not normalized:
        return ""
    resolved = resolve_reference_variable_name(normalized, alias_map) or normalized
    return f"${resolved}"


def _node_driven_variable_names(node) -> list:
    names = []
    for item in getattr(node, "driven_variable_list", []) or []:
        raw = str(getattr(item, "variable_name", "") or "").strip()
        if raw and raw not in names:
            names.append(raw)
    legacy = str(getattr(node, "driven_variable", "") or "").strip()
    if legacy and not names:
        names.append(legacy)
    return names


def _object_has_shape_key(obj, shape_key_name) -> bool:
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    key_blocks = getattr(shape_keys, "key_blocks", None)
    if not key_blocks:
        return False
    try:
        return key_blocks.get(shape_key_name) is not None
    except Exception:
        return False


def find_shape_key_objects(shape_key_name, preferred_object_name=""):
    result = []
    preferred_name = str(preferred_object_name or "").strip()
    if preferred_name:
        preferred = bpy.data.objects.get(preferred_name)
        if preferred is not None and _object_has_shape_key(preferred, shape_key_name):
            return [preferred]
    for obj in bpy.data.objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        if _object_has_shape_key(obj, shape_key_name):
            result.append(obj)
    return result


def collect_preview_targets(tree, context=None, only_node=None) -> list:
    """收集「驱动变量 → Blender 属性」的写入目标。

    形态键变量走 ``key_blocks[].value``（真正能在视口里看到播放）；没有对应
    形态键的变量（随机抖动的 ``$shape_up`` 等）退回场景自定义属性 —— 曲线在
    曲线编辑器里照样能看，只是不驱动几何。

    ``only_node`` 非空时只收集该节点自己驱动的变量：面板上的「生成」是按节点
    触发的，一个节点不该把整棵蓝图的驱动变量都写进关键帧。
    """
    alias_map = build_shape_key_reference_alias_map(context)
    shape_key_variables = {}
    parent_map = SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map(tree) or {}
    for shape_key_name, variable in parent_map.items():
        variable_name = _resolve_variable_name(variable, alias_map)
        if variable_name:
            shape_key_variables.setdefault(variable_name, str(shape_key_name))

    targets = []
    seen = set()
    for candidate in getattr(tree, "nodes", []) or []:
        if only_node is not None and candidate != only_node:
            continue
        if getattr(candidate, "mute", False):
            continue
        bl_idname = str(getattr(candidate, "bl_idname", "") or "")
        if bl_idname not in PREVIEW_SUPPORTED_NODE_TYPES:
            continue
        source = str(getattr(candidate, "label", "") or "") or str(getattr(candidate, "name", "") or bl_idname)
        interpolation = "CONSTANT" if bl_idname == _RANDOM_NODE_TYPE else "LINEAR"

        entries = []
        if bool(getattr(candidate, "use_continuous_shapekey_mode", False)):
            preferred_object = str(getattr(candidate, "continuous_target_object", "") or "").strip()
            for item in getattr(candidate, "continuous_shape_key_items", []) or []:
                raw = str(getattr(item, "variable_name", "") or "").strip()
                shape_key_name = str(getattr(item, "shape_key_name", "") or "").strip()
                if raw and shape_key_name:
                    entries.append((raw, shape_key_name, preferred_object))
        else:
            for raw in _node_driven_variable_names(candidate):
                entries.append((raw, "", ""))

        for raw, explicit_shape_key, preferred_object in entries:
            variable_name = _resolve_variable_name(raw, alias_map)
            if not variable_name:
                continue
            shape_key_name = explicit_shape_key or shape_key_variables.get(variable_name, "")
            objects = find_shape_key_objects(shape_key_name, preferred_object) if shape_key_name else []
            if objects:
                for obj in objects:
                    key = ("shapekey", variable_name, obj.name, shape_key_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    targets.append({
                        "variable": variable_name,
                        "source": source,
                        "interpolation": interpolation,
                        "kind": "shapekey",
                        "object_name": obj.name,
                        "shape_key_name": shape_key_name,
                    })
                continue
            property_name = f"{ANIM_DRIVER_PREVIEW_PROP_PREFIX}{sanitize_preview_name(variable_name)}"
            key = ("scene_property", variable_name, property_name)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "variable": variable_name,
                "source": source,
                "interpolation": interpolation,
                "kind": "scene_property",
                "property_name": property_name,
                "shape_key_name": shape_key_name,
            })
    return targets


# ---------------------------------------------------------------------------
# 关键帧写入
# ---------------------------------------------------------------------------

def _preview_action_name(tree, target) -> str:
    """每个数据块一个预览动作。

    同一个物体的所有形态键共用一条 ``Key`` 数据块，一个数据块也只能挂一条动作；
    所以动作按**数据块**命名（而不是按节点），多个节点的曲线共存其中、各写各的
    ``data_path``，互不覆盖。
    """
    tree_name = sanitize_preview_name(getattr(tree, "name", "") or "blueprint")
    if target["kind"] == "shapekey":
        suffix = sanitize_preview_name(target["object_name"])
    else:
        suffix = "scene"
    return f"{ANIM_DRIVER_PREVIEW_ACTION_PREFIX}_{tree_name}_{suffix}"[:63]


def target_datablock_key(target):
    """目标所属数据块的标识（决定它写进哪条预览动作）。"""
    if target["kind"] == "shapekey":
        return ("shapekey", target["object_name"])
    return ("scene",)


def target_datablock(target, scene):
    if target["kind"] == "shapekey":
        obj = bpy.data.objects.get(target["object_name"])
        return getattr(getattr(obj, "data", None), "shape_keys", None)
    return scene


def target_data_path(target) -> str:
    """目标在动作里的曲线路径（节点级作用域就靠它区分"谁写的"）。"""
    if target["kind"] == "shapekey":
        return f'key_blocks["{target["shape_key_name"]}"].value'
    return f'["{target["property_name"]}"]'


def group_targets_by_datablock(targets) -> dict:
    groups = {}
    for target in targets:
        groups.setdefault(target_datablock_key(target), []).append(target)
    return groups


def _ensure_action_slot(animation_data, action):
    """Blender 4.4+ 的 action slot：新动作没有槽位时关键帧可能无处归属。"""
    if not hasattr(animation_data, "action_slot"):
        return
    try:
        if getattr(animation_data, "action_slot", None) is not None:
            return
        slots = getattr(action, "slots", None)
        if slots is None:
            return
        identifier = str(getattr(getattr(animation_data, "id_data", None), "bl_rna", None).identifier or "")
        slot = slots.new(id_type=identifier.upper() or "OBJECT", name=action.name)
        animation_data.action_slot = slot
    except Exception:
        # 拿不到槽位时交给 keyframe_insert 自行处理（Blender 会自动建槽）
        pass


def ensure_preview_action(id_data, action_name):
    """确保 ``id_data`` 上挂着预览动作；返回 (action, 被顶掉的旧动作名)。"""
    animation_data = getattr(id_data, "animation_data", None)
    if animation_data is None:
        animation_data = id_data.animation_data_create()
    current = getattr(animation_data, "action", None)
    if current is not None and str(getattr(current, "name", "") or "").startswith(
        ANIM_DRIVER_PREVIEW_ACTION_PREFIX
    ):
        return current, ""

    previous_name = str(getattr(current, "name", "") or "") if current is not None else ""
    if current is not None:
        # 用户自己的动作不删：打上 fake user 后挂起，避免保存时被当作零用户数据清理
        try:
            current.use_fake_user = True
        except Exception:
            pass

    action = bpy.data.actions.new(action_name)
    action.use_fake_user = True
    animation_data.action = action
    _ensure_action_slot(animation_data, action)
    return action, previous_name


def iter_action_fcurves(action) -> list:
    collected = []
    for collection in iter_action_fcurve_collections(action):
        collected.extend(list(collection))
    return collected


def iter_action_fcurve_collections(action) -> list:
    """动作的曲线容器列表。

    Blender 5.x 移除了 ``Action.fcurves``（动作分层 + 槽位），曲线在
    ``action.layers[].strips[].channelbags[].fcurves`` 里；4.x 及更早则在
    ``action.fcurves``。两种形态都要能读写，否则插值设置与重烘焙清空会静默失效。
    """
    collections = []
    try:
        legacy = action.fcurves
    except Exception:
        legacy = None
    if legacy is not None:
        collections.append(legacy)
        return collections
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for channelbag in getattr(strip, "channelbags", []) or []:
                fcurves = getattr(channelbag, "fcurves", None)
                if fcurves is not None:
                    collections.append(fcurves)
    return collections


def clear_action_fcurves(action):
    """清空动作里的全部曲线（整块数据块的预览都要重来时才用）。"""
    removed = 0
    for collection in iter_action_fcurve_collections(action):
        for fcurve in list(collection):
            try:
                collection.remove(fcurve)
                removed += 1
            except Exception:
                pass
    return removed


def remove_action_data_paths(action, data_paths) -> int:
    """只删指定 ``data_path`` 的曲线。

    节点级作用域的关键：同一个数据块上的预览动作由多个驱动节点共用，重烘焙
    当前节点时只能替换**它自己那几个变量**的曲线，不能把别的节点的预览清掉。
    """
    wanted = {str(path) for path in (data_paths or ()) if path}
    if not wanted:
        return 0
    removed = 0
    for collection in iter_action_fcurve_collections(action):
        for fcurve in list(collection):
            if str(getattr(fcurve, "data_path", "") or "") not in wanted:
                continue
            try:
                collection.remove(fcurve)
                removed += 1
            except Exception:
                pass
    return removed


def set_action_interpolation(action, interpolation):
    for fcurve in iter_action_fcurves(action):
        for point in getattr(fcurve, "keyframe_points", []) or []:
            try:
                point.interpolation = interpolation
            except Exception:
                pass


def _bake_shape_key_target(target, frames, values, action):
    obj = bpy.data.objects.get(target["object_name"])
    if obj is None:
        return False, f"{target['object_name']} 不存在（{target['variable']}）"
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    if shape_keys is None:
        return False, f"{obj.name} 没有形态键（{target['variable']}）"
    key_block = shape_keys.key_blocks.get(target["shape_key_name"])
    if key_block is None:
        return False, f"{obj.name} 上没有形态键 {target['shape_key_name']}（{target['variable']}）"

    for frame, value in zip(frames, values):
        key_block.value = float(value)
        key_block.keyframe_insert(data_path="value", frame=frame, group=target["shape_key_name"])
    set_action_interpolation(action, target["interpolation"])
    return True, f"{obj.name} / 形态键 {target['shape_key_name']} ← {target['variable']}"


def _bake_scene_property_target(target, frames, values, scene, action):
    if scene is None:
        return False, f"{target['variable']}（当前上下文没有场景）"
    property_name = target["property_name"]
    data_path = f'["{property_name}"]'
    for frame, value in zip(frames, values):
        scene[property_name] = float(value)
        scene.keyframe_insert(data_path=data_path, frame=frame)
    set_action_interpolation(action, target["interpolation"])
    return True, f"场景属性 {property_name} ← {target['variable']}"


def _prepare_preview_actions(tree, targets, scene):
    """按数据块准备预览动作，并清掉**这批目标**的旧曲线。

    返回 (``{数据块键: action}``, 被挂起的旧动作名列表)。清的是本批目标对应的
    ``data_path``，不是整条动作 —— 别的驱动节点写进同一条动作的曲线要保留。
    """
    prepared = {}
    stashed = []
    for key, group in group_targets_by_datablock(targets).items():
        datablock = target_datablock(group[0], scene)
        if datablock is None:
            prepared[key] = None
            continue
        action, previous = ensure_preview_action(datablock, _preview_action_name(tree, group[0]))
        remove_action_data_paths(action, {target_data_path(target) for target in group})
        if previous:
            stashed.append(previous)
        prepared[key] = action
    return prepared, stashed


def bake_anim_driver_preview(tree, node=None, frame_count=None, context=None) -> dict:
    """解释驱动段并把**指定节点**的驱动变量烘成关键帧。

    ``node`` 非空时只写该节点自己驱动的变量（面板「生成」就是按节点触发的）；
    为 ``None`` 时写蓝图里全部受支持节点的变量（批量/测试用）。
    """
    context = context if context is not None else getattr(bpy, "context", None)
    try:
        paragraphs = AnimationDriverCollector(tree).collect()
    except Exception as error:
        return {"ok": False, "message": f"生成驱动段落失败: {error}"}
    if not paragraphs:
        return {"ok": False, "message": "动画驱动蓝图中没有可预览的驱动段落"}

    frame_count = clamp_preview_frame_count(
        frame_count if frame_count is not None
        else getattr(node, "preview_frame_count", ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES)
        or getattr(tree, "preview_frame_count", ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES)
    )
    fps = resolve_preview_fps(tree)
    simulation = simulate_anim_driver_paragraphs(paragraphs, frame_count, fps)
    targets = collect_preview_targets(tree, context, only_node=node)
    if not targets:
        node_label = str(getattr(node, "label", "") or "") or str(getattr(node, "name", "") or "")
        if node is not None:
            message = (
                f"节点「{node_label}」没有可预览的驱动变量"
                "（只有索引播放 / 往返播放 / 随机驱动支持关键帧预览）"
            )
        else:
            message = "未找到可写入的预览变量（需要索引播放 / 往返播放 / 随机驱动节点）"
        return {"ok": False, "message": message, "simulation": simulation}

    scene = getattr(context, "scene", None)
    start_frame = simulation["start_frame"]
    frames = list(range(start_frame, start_frame + frame_count))
    prepared, stashed = _prepare_preview_actions(tree, targets, scene)

    written = []
    skipped = []
    for target in targets:
        values = simulation["variables"].get(target["variable"])
        if not values:
            skipped.append(f"{target['variable']}（解释结果里没有该变量）")
            continue
        if target["kind"] == "shapekey":
            action = prepared.get(("shapekey", target["object_name"]))
            ok, detail = _bake_shape_key_target(target, frames, values, action)
        else:
            action = prepared.get(("scene",))
            ok, detail = _bake_scene_property_target(target, frames, values, scene, action)
        (written if ok else skipped).append(detail)

    if scene is not None and written:
        scene.frame_start = start_frame
        scene.frame_end = start_frame + frame_count - 1

    message_parts = [
        f"关键帧预览已生成：{len(written)} 个目标 × {frame_count} 帧"
        f"（{start_frame}–{start_frame + frame_count - 1}，1 场景帧 = 1 驱动帧）"
    ]
    if skipped:
        message_parts.append(f"跳过 {len(skipped)} 项")
    if simulation["undeclared_reads"]:
        message_parts.append(
            f"外部变量 {len(simulation['undeclared_reads'])} 个按 0 起步"
        )
    return {
        "ok": bool(written),
        "message": "；".join(message_parts),
        "written": written,
        "skipped": skipped,
        "stashed_actions": stashed,
        "targets": targets,
        "simulation": simulation,
        "frame_count": frame_count,
        "fps": fps,
    }


def _remove_action(action):
    try:
        action.use_fake_user = False
    except Exception:
        pass
    try:
        bpy.data.actions.remove(action)
    except Exception:
        pass


def _detach_action(animation_data, removed_actions):
    action = getattr(animation_data, "action", None)
    if action is None:
        return None
    if not str(getattr(action, "name", "") or "").startswith(ANIM_DRIVER_PREVIEW_ACTION_PREFIX):
        return None
    removed_actions.append(action.name)
    animation_data.action = None
    _remove_action(action)
    return action


def _remove_preview_properties(scene, names=None):
    removed = []
    if scene is None:
        return removed
    wanted = {str(name) for name in names} if names is not None else None
    for key in list(scene.keys()):
        text = str(key)
        if not text.startswith(ANIM_DRIVER_PREVIEW_PROP_PREFIX):
            continue
        if wanted is not None and text not in wanted:
            continue
        try:
            del scene[key]
            removed.append(text)
        except Exception:
            pass
    return removed


def clear_anim_driver_preview(tree=None, node=None, context=None) -> dict:
    """清除预览关键帧与预览场景属性。

    * ``node`` 非空：只清**该节点**写下的曲线与属性（面板 🗑 就是按节点触发的）；
      曲线清空后动作若已无内容，动作本身一并删除。
    * ``node`` 为空：清掉所有 ``AnimDriverPreview_*`` 动作与 ``anim_preview_*``
      场景属性（节点被删掉后留下的孤儿预览也走这条）。
    """
    context = context if context is not None else getattr(bpy, "context", None)
    scene = getattr(context, "scene", None)
    removed_actions = []
    removed_curves = 0

    if node is not None:
        if tree is None:
            tree = getattr(node, "id_data", None)
        targets = collect_preview_targets(tree, context, only_node=node) if tree is not None else []
        groups = group_targets_by_datablock(targets)
        for key, group in groups.items():
            datablock = target_datablock(group[0], scene)
            if datablock is None:
                continue
            animation_data = getattr(datablock, "animation_data", None)
            action = getattr(animation_data, "action", None) if animation_data is not None else None
            if action is None or not str(getattr(action, "name", "") or "").startswith(
                ANIM_DRIVER_PREVIEW_ACTION_PREFIX
            ):
                continue
            removed_curves += remove_action_data_paths(
                action, {target_data_path(target) for target in group}
            )
            if not iter_action_fcurves(action):
                # 这个数据块上的预览只剩别人的曲线时动作要留着，空了才删
                removed_actions.append(action.name)
                animation_data.action = None
                _remove_action(action)
        removed_properties = _remove_preview_properties(
            scene,
            [
                target["property_name"]
                for target in targets
                if target["kind"] == "scene_property"
            ],
        )
        if removed_curves or removed_actions or removed_properties:
            message = (
                f"已清除该节点的预览：{removed_curves} 条曲线"
                + (f"、{len(removed_actions)} 个空动作" if removed_actions else "")
                + (f"、{len(removed_properties)} 个场景属性" if removed_properties else "")
            )
        else:
            message = "该节点没有生成过关键帧预览"
        return {
            "removed_actions": removed_actions,
            "removed_curves": removed_curves,
            "removed_properties": removed_properties,
            "message": message,
        }

    for shape_keys in list(getattr(bpy.data, "shape_keys", []) or []):
        animation_data = getattr(shape_keys, "animation_data", None)
        if animation_data is not None:
            _detach_action(animation_data, removed_actions)

    if scene is not None:
        animation_data = getattr(scene, "animation_data", None)
        if animation_data is not None:
            _detach_action(animation_data, removed_actions)
    removed_properties = _remove_preview_properties(scene)

    # 兜底：仍带前缀且已无用户的动作
    for action in list(getattr(bpy.data, "actions", []) or []):
        if not str(getattr(action, "name", "") or "").startswith(ANIM_DRIVER_PREVIEW_ACTION_PREFIX):
            continue
        if getattr(action, "users", 0):
            continue
        removed_actions.append(action.name)
        _remove_action(action)

    return {
        "removed_actions": removed_actions,
        "removed_curves": removed_curves,
        "removed_properties": removed_properties,
        "message": (
            f"已清除全部预览：{len(removed_actions)} 个动作"
            + (f"、{len(removed_properties)} 个场景属性" if removed_properties else "")
            if (removed_actions or removed_properties)
            else "没有找到关键帧预览数据"
        ),
    }


# ---------------------------------------------------------------------------
# 操作符与 UI
# ---------------------------------------------------------------------------

def _find_anim_driver_node(context, node_name):
    tree = getattr(getattr(context, "space_data", None), "edit_tree", None)
    if tree is None:
        return None
    node = tree.nodes.get(node_name) if node_name else tree.nodes.active
    if node is None:
        return None
    if not str(getattr(node, "bl_idname", "") or "").startswith("SSMTNode_AnimDriver_"):
        return None
    return node


class SSMT_OT_AnimDriverPreviewBake(Operator):
    bl_idname = "ssmt.anim_driver_preview_bake"
    bl_label = "生成关键帧预览"
    bl_description = (
        "解释当前动画驱动蓝图生成的配置段，把**该节点驱动的那几个变量**按帧写成 "
        "Blender 关键帧（形态键权重 / 场景属性），用于在时间轴上预览播放"
    )
    bl_options = {'REGISTER', 'UNDO'}

    node_name: StringProperty(name="Node Name", default="")
    frame_count: IntProperty(
        name="预览帧数",
        default=ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES,
        min=1,
        max=ANIM_DRIVER_PREVIEW_MAX_FRAMES,
    )

    def execute(self, context):
        node = _find_anim_driver_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "未找到动画驱动节点")
            return {'CANCELLED'}
        tree = getattr(node, "id_data", None)
        if tree is None:
            self.report({'ERROR'}, "节点不属于任何蓝图")
            return {'CANCELLED'}
        if str(getattr(node, "bl_idname", "") or "") not in PREVIEW_SUPPORTED_NODE_TYPES:
            self.report({'ERROR'}, "该节点类型暂不支持关键帧预览（当前支持：索引播放 / 往返播放 / 随机驱动）")
            return {'CANCELLED'}

        frame_count = self.frame_count or int(
            getattr(node, "preview_frame_count", ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES)
            or ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES
        )
        result = bake_anim_driver_preview(tree, node=node, frame_count=frame_count, context=context)
        if not result.get("ok"):
            self.report({'ERROR'}, result.get("message") or "关键帧预览生成失败")
            return {'CANCELLED'}
        self.report({'INFO'}, result.get("message") or "关键帧预览已生成")
        return {'FINISHED'}


class SSMT_OT_AnimDriverPreviewClear(Operator):
    bl_idname = "ssmt.anim_driver_preview_clear"
    bl_label = "清除关键帧预览"
    bl_description = (
        "删除关键帧预览数据；指定了节点则只删该节点写下的曲线与场景属性，"
        "留空则清掉全部预览（含节点已删除后遗留的孤儿预览）"
    )
    bl_options = {'REGISTER', 'UNDO'}

    node_name: StringProperty(name="Node Name", default="")

    def execute(self, context):
        node = _find_anim_driver_node(context, self.node_name) if self.node_name else None
        tree = getattr(node, "id_data", None) if node is not None else None
        result = clear_anim_driver_preview(tree=tree, node=node, context=context)
        self.report({'INFO'}, result.get("message") or "已清除关键帧预览")
        return {'FINISHED'}


def draw_anim_driver_preview_controls(layout, node):
    frame_count = clamp_preview_frame_count(
        getattr(node, "preview_frame_count", ANIM_DRIVER_PREVIEW_DEFAULT_FRAMES)
    )
    node_name = str(getattr(node, "name", "") or "")
    box = layout.box()
    box.label(text="关键帧预览（仅本节点）", icon='KEYFRAME_HLT')
    row = box.row(align=True)
    row.prop(node, "preview_frame_count", text="帧数")
    bake = row.operator("ssmt.anim_driver_preview_bake", text="生成", icon='KEYFRAME')
    bake.node_name = node_name
    bake.frame_count = frame_count
    clear = row.operator("ssmt.anim_driver_preview_clear", text="", icon='TRASH')
    clear.node_name = node_name
    box.label(
        text=f"范围 {ANIM_DRIVER_PREVIEW_START_FRAME}–{frame_count} 帧"
             f"（上限 {ANIM_DRIVER_PREVIEW_MAX_FRAMES}，1 场景帧 = 1 驱动帧）",
        icon='INFO',
    )
    box.label(text="只写本节点驱动的变量，可在时间轴直接播放", icon='INFO')
    clear_all = box.operator("ssmt.anim_driver_preview_clear", text="清除全部预览", icon='TRASH')
    clear_all.node_name = ""


classes = (
    SSMT_OT_AnimDriverPreviewBake,
    SSMT_OT_AnimDriverPreviewClear,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
