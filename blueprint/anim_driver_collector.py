from collections import deque
import re
from typing import List, Dict, Set, Tuple

from .anim_driver_base import SSMTNode_AnimDriver_Base, SSMTSocketAnimDriver

try:  # 引用对齐是可选的发射期增强：裁剪/测试桩环境缺该 API 时退回旧行为
    from .variable_registry import (
        build_shape_key_reference_alias_map,
        rewrite_reference_variables_in_text,
    )
except ImportError:  # pragma: no cover - 仅测试桩/裁剪环境命中
    def build_shape_key_reference_alias_map(context=None):
        return {}

    def rewrite_reference_variables_in_text(text, alias_map=None):
        return text


# ---------------------------------------------------------------------------
# 驱动块内重复 global 声明的归一（同值合并 / 异值 _N 分叉）
# ---------------------------------------------------------------------------
# 每个驱动节点各发自己的 [Constants]，同一个变量仍可能被多个节点重复声明：
#   * 运行时间节点已改为**每节点预分配帧变量**（$anim_frame{auto_index}），不再无条件
#     发共享的 $fps / $swapvar —— 那类重复已从源头消失（连兼容别名也不再发）；
#     只有用户手改成同名时才会重新出现（此时归一负责收尾）；
#   * 同名暂停变量/索引变量在用户手填或节点复制后也可能撞名。
# 同值重复只是噪声（3DMigoto 会告警）；**异值重复是真 bug**：一个变量被赋予
# 多个不同初值，最终生效者取决于解析顺序，且读它的其它段拿到的是谁的值无法预期。
# 归一规则（与用户口径一致）：
#   * 取值相同（含"无初值"不构成取值）→ 合并：保留第一条，删除其余声明；
#   * 取值不同 → 保留第一条，其余按出现顺序分叉成 $name_1 / $name_2 …，
#     并把**该段内**对该名字的引用一并改名（段内自洽，不影响其它段）。
_SECTION_LINE_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
_GLOBAL_DECL_LINE_RE = re.compile(
    r"^\s*global(?:\s+persist)?\s+(\$[A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(.*))?\s*$"
)
_VAR_TOKEN_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
# 段内对某个变量的赋值：`$x = <右侧>`（记录右侧文本，用于判断是否"给了不同的值"）
_ASSIGN_LINE_RE = re.compile(
    r"^\s*(\$[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
)


def _decl_values_equal(left: str, right: str) -> bool:
    """声明取值是否等价：能当数字比就按数字比（60 vs 60.0 视为同值）。"""
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def normalize_driver_shared_declarations(paragraphs: List[Dict]) -> Dict:
    """就地归一驱动块内重复的 ``global`` 声明；返回归一摘要。

    只看 ``[Constants]`` 段内的 ``global`` / ``global persist`` 声明——``[Present]``
    里的 ``global $x = 0`` 是"每帧重置"惯用法，不能当重复声明删掉。
    返回 ``{"merges": [...], "renames": [...]}``（供日志与测试断言）。
    """
    declarations = []  # (paragraph_index, line_index, name, value)
    for paragraph_index, paragraph in enumerate(paragraphs or []):
        in_constants = False
        for line_index, line in enumerate(str(paragraph.get("ini_content") or "").split("\n")):
            section_match = _SECTION_LINE_RE.match(line)
            if section_match:
                in_constants = section_match.group(1).strip().lower() == "constants"
                continue
            if not in_constants:
                continue
            body = line.split(";", 1)[0].rstrip()
            if not body.strip():
                continue
            decl_match = _GLOBAL_DECL_LINE_RE.match(body)
            if not decl_match:
                continue
            declarations.append((
                paragraph_index,
                line_index,
                decl_match.group(1),
                str(decl_match.group(2) or "").strip(),
            ))

    by_name = {}
    for record in declarations:
        by_name.setdefault(record[2], []).append(record)

    drop_lines = set()
    rename_by_paragraph = {}
    merges = []
    renames = []
    conflicts = []
    # 分叉名不得与全文件任何已有声明撞名：否则 $x 分叉成已存在的 $x_1 → 静默覆盖。
    taken_names = {record[2] for record in declarations}

    for name, records in by_name.items():
        if len(records) < 2:
            continue
        # 基准 = 第一条带初值的声明（无初值的声明不构成取值，可安全删除）
        base = next((r for r in records if r[3] != ""), records[0])
        base_value = base[3]
        base_paragraph = base[0]
        # 同段内同名异值的条数：>1 条时该段内对 $name 的引用到底指哪一个无法判定。
        fork_counts_by_paragraph = {}
        for candidate in records:
            if candidate is base or candidate[3] == "":
                continue
            if _decl_values_equal(candidate[3], base_value):
                continue
            fork_counts_by_paragraph[candidate[0]] = (
                fork_counts_by_paragraph.get(candidate[0], 0) + 1
            )
        suffix = 0
        for record in records:
            if record is base:
                continue
            paragraph_index, line_index, _name, value = record
            if value == "" or _decl_values_equal(value, base_value):
                drop_lines.add((paragraph_index, line_index))
                merges.append({
                    "name": name,
                    "value": value,
                    "paragraph_index": paragraph_index,
                })
                continue
            # 分叉必须能「段内自洽」：改名是按段整体替换 $name 的，若基准声明也在
            # 这一段（或本段有多个分叉），基准/分叉会被改成同一个名字 → 产出同名
            # 异值双声明（正是本函数要消灭的形态）。这种情况保留原名，只记提示。
            if (
                paragraph_index == base_paragraph
                or fork_counts_by_paragraph.get(paragraph_index, 0) > 1
            ):
                conflicts.append({
                    "name": name,
                    "value": value,
                    "base_value": base_value,
                    "paragraph_index": paragraph_index,
                })
                continue
            suffix += 1
            new_name = f"{name}_{suffix}"
            while new_name in taken_names:
                suffix += 1
                new_name = f"{name}_{suffix}"
            taken_names.add(new_name)
            rename_by_paragraph.setdefault(paragraph_index, {})[name] = new_name
            renames.append({
                "name": name,
                "new_name": new_name,
                "value": value,
                "base_value": base_value,
                "paragraph_index": paragraph_index,
            })

    # 声明合并后仍只有一份、却被多个段落**写入不同值**的变量：同帧内互相覆盖，
    # 最终生效的是最后执行的那段。这是"一个变量被赋予多个数值"的写入形态，
    # 不能自动分叉（读它的其它段并不知道该读哪一个），只能显式提示。
    # 各段写的是**同一个右侧表达式**时不提示——那只是幂等重复，无歧义。
    # 注意：比较必须在改名**之后**做——分叉会改变写入的右侧（$fps → $fps_1），
    # 那正是"同一个变量被算出不同值"的典型形态。
    surviving_counts = {}
    for name, records in by_name.items():
        dropped = sum(1 for r in records if (r[0], r[1]) in drop_lines)
        surviving_counts[name] = len(records) - dropped

    if drop_lines or rename_by_paragraph:
        for paragraph_index, paragraph in enumerate(paragraphs or []):
            renamed = rename_by_paragraph.get(paragraph_index) or {}
            rebuilt = []
            for line_index, line in enumerate(str(paragraph.get("ini_content") or "").split("\n")):
                if (paragraph_index, line_index) in drop_lines:
                    continue
                if renamed:
                    line = _VAR_TOKEN_RE.sub(
                        lambda match: renamed.get(match.group(0), match.group(0)),
                        line,
                    )
                rebuilt.append(line)
            paragraph["ini_content"] = "\n".join(rebuilt)

    final_assignments = {}
    for paragraph_index, paragraph in enumerate(paragraphs or []):
        for line in str(paragraph.get("ini_content") or "").split("\n"):
            body = line.split(";", 1)[0].rstrip()
            if not body.strip() or _GLOBAL_DECL_LINE_RE.match(body):
                continue
            assign_match = _ASSIGN_LINE_RE.match(body)
            if assign_match:
                final_assignments.setdefault(assign_match.group(1), {}).setdefault(
                    paragraph_index, set()).add(assign_match.group(2))

    shared_writes = []
    for name, surviving in sorted(surviving_counts.items()):
        if surviving != 1:
            continue
        writers = final_assignments.get(name) or {}
        if len(writers) < 2:
            continue
        distinct_values = set()
        for values in writers.values():
            distinct_values |= values
        if len(distinct_values) < 2:
            continue
        shared_writes.append({
            "name": name,
            "paragraphs": sorted(writers),
            "values": sorted(distinct_values),
        })

    summary = {
        "merges": merges,
        "renames": renames,
        "shared_writes": shared_writes,
    }
    return summary


class AnimationDriverCollector:
    def __init__(self, node_group):
        self.node_group = node_group
        # 最近一次 collect() 的重复声明归一摘要（供调用方记日志/测试断言）
        self.last_normalization = {"merges": [], "renames": [], "shared_writes": []}

    @staticmethod
    def _node_sort_key(node):
        tree_name = str(getattr(getattr(node, "id_data", None), "name", "") or "")
        node_name = str(getattr(node, "name", "") or "")
        return tree_name.casefold(), node_name.casefold(), tree_name, node_name

    def count_paragraphs(self) -> int:
        """轻量方法：只统计段落数，不调用 generate_ini_segment。
        适用于 draw 回调等只读上下文。"""
        driver_nodes = self._find_animation_driver_nodes()
        if not driver_nodes:
            return 0
        graph, node_set = self._build_graph(driver_nodes)
        paragraphs = self._divide_into_paragraphs(graph, node_set)
        return len(paragraphs)

    def collect(self) -> List[Dict]:
        driver_nodes = self._find_animation_driver_nodes()
        if not driver_nodes:
            return []

        # 引用对齐表（形态键基名 → 该形态键真正分配的变量名）。
        # 驱动节点保存的「驱动变量」是引用，可能是改名前的旧基名；形态键节点若已
        # 拿到 _N 名，这里在发射前把引用改成权威名，避免导出后驱动写一个没人声明
        # 的变量（3DMigoto 当局部变量 → 跨段失效，联动静默死掉）。
        alias_map = build_shape_key_reference_alias_map()

        graph, node_set = self._build_graph(driver_nodes)
        paragraphs = self._divide_into_paragraphs(graph, node_set)

        seen_runtime_segments = set()
        result = []
        for idx, paragraph_nodes in enumerate(paragraphs):
            ordered_nodes = self._topological_sort(paragraph_nodes, graph)
            ini_content_parts = []
            for node in ordered_nodes:
                connected_upstream = list(graph[node]["inputs"]) if node in graph else []
                try:
                    segment = node.generate_ini_segment(connected_nodes=connected_upstream)

                    if hasattr(node, 'fps') and segment:
                        runtime_key = self._build_runtime_segment_key(node, segment)
                        if runtime_key in seen_runtime_segments:
                            segment = ""
                        else:
                            seen_runtime_segments.add(runtime_key)

                    if segment:
                        ini_content_parts.append(segment)
                except Exception as e:
                    raise RuntimeError(
                        f"动画驱动节点 '{node.name}' 生成 INI 失败: {e}"
                    ) from e

            merged = self._merge_paragraph_sections(ini_content_parts)
            merged = rewrite_reference_variables_in_text(merged, alias_map)
            if merged.strip():
                result.append({
                    "paragraph_index": idx,
                    "node_names": [n.name for n in ordered_nodes],
                    "ini_content": merged,
                })

        # 跨段归一：同名 global 声明同值合并、异值分叉（见模块上方注释）
        self.last_normalization = normalize_driver_shared_declarations(result)
        for rename in self.last_normalization["renames"]:
            print(
                f"[AnimDriver][WARNING] 变量 {rename['name']} 在段落 "
                f"#{rename['paragraph_index']} 的初值 {rename['value']} 与首个声明 "
                f"{rename['base_value']} 冲突，已分叉为 {rename['new_name']}"
                "（防止一个变量被赋予多个不同数值）"
            )
        for shared in self.last_normalization["shared_writes"]:
            print(
                f"[AnimDriver][INFO] 变量 {shared['name']} 被 "
                f"{len(shared['paragraphs'])} 个段落写入不同值（段落 "
                f"{shared['paragraphs']}）——同一帧内会互相覆盖，实际生效的是最后"
                "执行的那段；若本意是各自独立的计数，请只保留一个「运行时间」节点"
            )
        return result

    @staticmethod
    def _build_runtime_segment_key(node, segment: str) -> tuple:
        """运行时间节点的段去重键。

        运行时间节点已改为**每节点预分配帧变量**（``$anim_frame{auto_index}``），两个
        节点的段文本天然不同，因此这条去重现在只在「用户把两个节点的帧变量手改成
        同名」时才可能命中（那种情况下两份声明完全相同，去重保留一份即可）。

        键只取**真正影响段内容**的字段：段文本本身 + fps。刻意不含
        ``playback_rate``——它由播放类驱动节点经 ``_compute_play_interval`` 读取，
        并不出现在运行时间节点自己生成的文本里。
        """
        return (
            getattr(node, "bl_idname", ""),
            int(getattr(node, "fps", 0) or 0),
            str(segment or "").strip(),
        )

    def _merge_paragraph_sections(self, segments):
        sections = {}
        section_order = []
        current = None

        for segment in segments:
            for line in segment.split('\n'):
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current = stripped
                    if current not in sections:
                        sections[current] = []
                        section_order.append(current)
                elif current:
                    sections[current].append(line)

        output = []
        for section_name in section_order:
            output.append(section_name)
            output.extend(sections[section_name])

        return '\n'.join(output)

    def _find_animation_driver_nodes(self):
        result = []
        for node in self.node_group.nodes:
            if getattr(node, "mute", False):
                continue
            if hasattr(node, "generate_ini_segment") and callable(node.generate_ini_segment):
                try:
                    if node.bl_idname != SSMTNode_AnimDriver_Base.bl_idname:
                        result.append(node)
                except Exception:
                    continue
        return result

    def _build_graph(self, nodes) -> Tuple[Dict, Set]:
        node_set = set(nodes)
        graph = {node: {"inputs": set(), "outputs": set()} for node in nodes}

        for link in self.node_group.links:
            from_node = link.from_node
            to_node = link.to_node
            if from_node not in node_set or to_node not in node_set:
                continue
            from_socket_type = getattr(link.from_socket, "bl_idname", "")
            to_socket_type = getattr(link.to_socket, "bl_idname", "")
            if from_socket_type == 'SSMTSocketAnimDriver' and to_socket_type == 'SSMTSocketAnimDriver':
                graph[from_node]["outputs"].add(to_node)
                graph[to_node]["inputs"].add(from_node)

        return graph, node_set

    def _divide_into_paragraphs(self, graph: Dict, node_set: Set) -> List[List]:
        visited = set()
        paragraphs = []

        for node in sorted(node_set, key=self._node_sort_key):
            if node in visited:
                continue
            component = self._bfs_component(node, graph, visited)
            paragraphs.append(component)

        return paragraphs

    def _bfs_component(self, start_node, graph: Dict, visited: Set) -> List:
        queue = deque([start_node])
        component = []
        visited.add(start_node)

        while queue:
            node = queue.popleft()
            component.append(node)
            neighbors = graph[node]["inputs"] | graph[node]["outputs"]
            for neighbor in sorted(neighbors, key=self._node_sort_key):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        return component

    def _topological_sort(self, nodes: List, graph: Dict) -> List:
        if len(nodes) <= 1:
            return nodes

        in_degree = {}
        for node in nodes:
            in_degree[node] = len([n for n in graph[node]["inputs"] if n in set(nodes)])

        queue = [n for n in nodes if in_degree[n] == 0]
        queue.sort(key=self._node_sort_key)
        sorted_nodes = []

        while queue:
            node = queue.pop(0)
            sorted_nodes.append(node)
            for neighbor in sorted(graph[node]["outputs"], key=self._node_sort_key):
                if neighbor in in_degree:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
                        queue.sort(key=self._node_sort_key)

        remaining = sorted(
            (n for n in nodes if n not in sorted_nodes),
            key=self._node_sort_key,
        )
        return sorted_nodes + remaining


def register():
    pass


def unregister():
    pass
