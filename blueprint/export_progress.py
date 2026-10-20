"""后台导出的细粒度进度中心（worker 进程内使用）。

主进程只能通过会话目录里的 ``stage.json`` 观察后台实例，所以 worker 侧
"导出到哪一步了"都要落到这个文件里。本模块只做四件事：

1. 记录当前阶段、当前节点与节点状态表（pending / running / done）；
2. 原子写 ``stage.json``，写失败一律静默 —— 进度绝不能拖垮导出；
3. 提供 ``elapsed()`` 给主进程显示本次后台导出已经跑了多久；
4. 维护**动态步骤计数**（``begin_steps`` / ``advance_stage`` / ``report_node`` /
   ``finish_steps``）：``stage.json`` 的 ``done`` / ``total`` 是"已完成步数 /
   总步数"。

步骤模型（**每个不同名称的子步骤模块各算 1 步**）：

- ``begin_steps(total)``：开始一次导出，``done=0``，``total`` 取传入值（可 0）。
  传入值只是**初始估算**（主进程预算），之后会随实际出现的阶段动态增长。
- ``advance_stage(name)``：``TimerUtils.start_stage`` 的统一入口 —— 进入与上一个
  不同的阶段名时，先把**上一个阶段**记为完成（``done += 1``），再让
  ``total = max(total, done + 1)``（当前阶段占 1 步）。同名阶段重入不计步；
  同名阶段在整次导出里重复出现也只计一次（分段录制里 ``数据哈希预计算`` 等
  子阶段会反复切换，不能重复累加）。
- ``report_stage(name, ...)``：只更新状态栏**显示文本**（如"转换贴图 26/60"），
  **不改 ``done`` / ``total``**；贴图张数这类深层内部进度只做文本提示。
- ``report_node(name, "done")``：后处理节点完成时计 1 步，并按
  ``total = max(total, done + 1)`` 扩张；同名节点只计一次。节点内部最后启动的
  阶段在节点完成时一并记完成（阶段与节点各占 1 步，同名不重复计）。
- ``finish_steps()``：收尾把 ``done`` 补到 ``total``（保证 100%）。

百分比由主进程按 ``done / total`` 计算，并保证单调不回退。

与 ``export_background._write_stage`` 的分工：那边写粗粒度阶段（第 N 轮 /
直出导出 / 完成 / 失败），这边写细粒度阶段与节点。两边写同一份文件，所以本
模块写文件时**先读现有内容再合并**，不会把 ``done`` / ``total`` / ``message``
等旧字段冲掉（旧字段在还没 ``begin_steps`` 时继续沿用）。
"""

import json
import os
import time


STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"

#: 模块级进度状态；``done`` / ``total`` 为 None 表示"沿用 stage.json 里的旧值"
_state = {
    "stage": "",
    "node": "",
    "nodes": {},
    "done": None,
    "total": None,
    "started_at": 0.0,
    #: 计步用的稳定阶段名（最近一次 ``advance_stage`` 的名字；显示文本可能被
    #: ``report_stage`` 改写成"转换贴图 3/60"，不能拿显示文本判断同名）
    "stage_key": "",
    #: 当前阶段是否还没被"下一步"推进过（防止同一阶段被重复计步）
    "stage_pending": False,
    #: 已经计入 done 的计步单元：``("stage", 阶段名)`` / ``("node", 节点键)``。
    #: 同名阶段 / 节点在整次导出里只计一次（基础导出内部会反复切换同名子阶段）
    "counted": set(),
    #: 当前正在执行的节点内部是否启动过阶段（该节点完成时要额外算一步阶段）
    "node_started_stage": False,
    #: 进入当前节点前的阶段显示名（节点结束时还原，避免细分文本漏到下一个节点）
    "node_base_stage": "",
}

_stage_path = ""


def set_stage_file(stage_path: str) -> None:
    """设置本 worker 的进度文件路径，并把状态重置为一次新的导出。"""
    global _stage_path
    _stage_path = str(stage_path or "")
    _state["stage"] = ""
    _state["node"] = ""
    _state["nodes"] = {}
    _state["done"] = None
    _state["total"] = None
    _state["started_at"] = time.time()
    _state["stage_key"] = ""
    _state["stage_pending"] = False
    _state["counted"] = set()
    _state["node_started_stage"] = False
    _state["node_base_stage"] = ""


def clear_stage_file() -> None:
    """解除进度文件（导出结束后调用）；不会删除磁盘上的 stage.json。"""
    global _stage_path
    _stage_path = ""
    _state["stage"] = ""
    _state["node"] = ""
    _state["nodes"] = {}
    _state["done"] = None
    _state["total"] = None
    _state["started_at"] = 0.0
    _state["stage_key"] = ""
    _state["stage_pending"] = False
    _state["counted"] = set()
    _state["node_started_stage"] = False
    _state["node_base_stage"] = ""


# ---------------------------------------------------------------------------
# 步骤计数（stage.json 的 done/total = 已完成步数 / 总步数）
# ---------------------------------------------------------------------------


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_stage_field(name: str, default: str = "") -> str:
    """读 stage.json 里的单个字段（读不到 / 解析失败都返回 ``default``）。"""
    path = _stage_path
    if not path:
        return default
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError):
        return default
    if not isinstance(payload, dict):
        return default
    return str(payload.get(name) or default)


def begin_steps(total) -> None:
    """开始一次导出的步骤计数：``done=0``，``total`` 取传入值（可 0 = 未知）。

    传入的 total 只是**初始估算**；之后 ``advance_stage`` / ``report_node`` 会
    按实际出现的阶段数把它动态抬高（``total = max(total, done + 1)``）。
    """
    _state["done"] = 0
    _state["total"] = max(0, _as_int(total, 0))
    _state["stage_key"] = ""
    _state["stage_pending"] = False
    _state["counted"] = set()
    _state["node_started_stage"] = False
    _write_stage_file()


def _sync_total() -> None:
    """当前阶段（或刚完成的一步之后的下一个阶段）也占 1 步：total 只增不减。"""
    _state["total"] = max(step_total(), step_done() + 1)


def _complete_stage(stage_name) -> bool:
    """把阶段记为完成；同名阶段在整次导出里只计一次。返回是否真的推进了 done。"""
    name = str(stage_name or "")
    marker = ("stage", name)
    if not name or marker in _state["counted"]:
        return False
    _state["counted"].add(marker)
    _state["done"] = step_done() + 1
    return True


def _complete_node(key) -> bool:
    """把节点记为完成；同名节点只计一次。返回是否真的推进了 done。"""
    name = str(key or "")
    marker = ("node", name)
    if not name or marker in _state["counted"]:
        return False
    _state["counted"].add(marker)
    _state["done"] = step_done() + 1
    return True


def complete_step() -> int:
    """完成一个**无名**步骤（轮次 / 收尾等没有阶段名的推进）。返回当前 done。"""
    _state["done"] = step_done() + 1
    _sync_total()
    _write_stage_file()
    return step_done()


def finish_steps() -> int:
    """收尾：``done = total``（保证 100%）；done 不会因此后退。"""
    total = max(step_total(), step_done())
    _state["total"] = total
    _state["done"] = total
    _state["stage_pending"] = False
    _write_stage_file()
    return total


def step_done() -> int:
    """已完成的步骤数。"""
    return max(0, _as_int(_state["done"], 0) if _state["done"] is not None else 0)


def step_total() -> int:
    """总步骤数（0 表示还没开始 / 未知，进度文本会省略分数）。"""
    return max(0, _as_int(_state["total"], 0) if _state["total"] is not None else 0)


def step_percent() -> int:
    """``done / total`` 的百分比（整数 0~100；total<=0 按 0 处理）。"""
    total = step_total()
    if total <= 0:
        return 0
    return max(0, min(100, int(step_done() * 100 / total)))


def advance_stage(stage: str) -> None:
    """进入新阶段：把**上一个**阶段记为完成，再记录新阶段名。

    - 第一个阶段不推进（它才刚开始，但要占 1 步 → total 至少 1）；
    - 同名阶段重入不推进（直出节点循环里同一步可能多次 ``start_stage``）；
    - 同名阶段在整次导出里重复出现也只计一次（基础导出内部会反复切换
      ``数据哈希预计算`` / ``对象处理与合并`` 等子阶段，不能重复累加）；
    - 显示文本与计步名分开：``report_stage`` 把文本改成"转换贴图 3/60"时，
      计步名仍是最近一次 ``advance_stage`` 的名字；
    - 在某个节点内部启动的阶段会被标记，节点完成时由 ``report_node(done)``
      补记这一阶段（该阶段与节点各算一步）。
    """
    name = str(stage or "")
    if not name:
        return
    previous = str(_state.get("stage_key") or "")
    if previous and previous != name and _state.get("stage_pending"):
        _complete_stage(previous)
    if _state.get("node"):
        _state["node_started_stage"] = True
    if name != previous:
        _state["stage_key"] = name
    _state["stage"] = name
    _state["stage_pending"] = True
    _sync_total()
    _write_stage_file()


def report_stage(stage: str, done=None, total=None) -> None:
    """更新状态栏**显示文本**（不推进步骤，也不改 done/total）。

    贴图转换张数这类更深层的内部进度（"材质转资源pro：转换贴图 26/60"）只做
    文本提示：百分比等这个子步骤整体走完（下一次 ``advance_stage`` / 节点完成）
    才 +1。``done`` / ``total`` 参数仅为兼容旧调用保留，传入值会被忽略。
    """
    name = str(stage or "")
    if name:
        _state["stage"] = name
    _write_stage_file()


def report_nodes(names) -> None:
    """登记一批节点为 pending（导出开始或某阶段开始时调用）。"""
    for name in names or []:
        key = str(name or "")
        if key:
            _state["nodes"][key] = STATE_PENDING
    _write_stage_file()


def report_node(name: str, status: str, count_step: bool = True) -> None:
    """更新单个节点状态并写 stage.json；节点首次完成时推进一步。

    若该节点执行期间自己启动过导出阶段（例如直出形态键 / 多文件节点），
    完成时把**节点内部最后启动的那个阶段**一并记为完成（该阶段与节点各算
    1 步，同名不重复计）。

    ``count_step=False`` 供"阶段级节点"使用：这些节点只用于节点高亮，
    不参与步骤计数（``done`` / ``total`` 保持不变）。
    """
    key = str(name or "")
    if not key:
        return
    value = str(status or "")
    if not value:
        return
    # 进度键可能是 ``树名/节点名``（嵌套蓝图节点也用同一张表）；节点内部的
    # 细分文本前缀（``<节点名>：…``）只认节点名部分，避免带树名时匹配不上。
    node_label = key.rsplit("/", 1)[-1] or key

    previous = _state["nodes"].get(key)
    _state["nodes"][key] = value
    if value == STATE_RUNNING:
        _state["node"] = key
        # 记住进入节点前状态栏上的阶段名（粗粒度阶段）：节点内部可以把 stage
        # 临时改写成细分文本（如"材质转资源pro：转换贴图 3/26"），节点完成时
        # 按这里记下的名字还原，避免细分文本漏到后面其它节点。
        base_stage = _read_stage_field("stage", "")
        if base_stage and not base_stage.startswith(node_label + "："):
            _state["node_base_stage"] = base_stage
    elif _state["node"] == key:
        # 当前节点跑完了：清掉"当前节点"，避免主进程把已完成节点当成正在执行
        _state["node"] = ""

    if value == STATE_DONE and previous != STATE_DONE:
        # 恢复节点开始前的阶段显示名（只改显示，不动步骤计数）
        base_stage = str(_state.get("node_base_stage") or "")
        if base_stage and str(_state.get("stage") or "").startswith(node_label + "："):
            _state["stage"] = base_stage
        _state["node_base_stage"] = ""
        if count_step:
            # 节点内部最后启动的那个阶段也走完了：先记该阶段，再记节点本身
            if _state.get("node_started_stage") and _state.get("stage_pending"):
                _complete_stage(str(_state.get("stage_key") or ""))
                _state["stage_pending"] = False
            _complete_node(key)
            _sync_total()
        _state["node_started_stage"] = False
        _write_stage_file()
    else:
        _write_stage_file()


def elapsed() -> float:
    """从本次导出开始（``set_stage_file``）到现在经过的秒数。"""
    started_at = _state.get("started_at") or 0.0
    if not started_at:
        return 0.0
    try:
        return max(0.0, time.time() - float(started_at))
    except (TypeError, ValueError):
        return 0.0


def get_state() -> dict:
    """返回状态快照（测试与调试用，调用方不要就地修改）。"""
    return {
        "stage": _state["stage"],
        "node": _state["node"],
        "nodes": dict(_state["nodes"]),
        "done": _state["done"],
        "total": _state["total"],
        "elapsed": elapsed(),
    }


# ---------------------------------------------------------------------------
# 写文件（全部静默）
# ---------------------------------------------------------------------------


def _write_stage_file() -> None:
    """合并写入 stage.json；任何异常都不向外抛。"""
    path = _stage_path
    if not path:
        return

    try:
        payload = {}
        try:
            with open(path, "r", encoding="utf-8") as file:
                existing = json.load(file)
            if isinstance(existing, dict):
                payload.update(existing)
        except (OSError, ValueError):
            pass

        if _state["stage"] or "stage" not in payload:
            payload["stage"] = str(_state["stage"] or "")

        try:
            payload["done"] = int(_state["done"]) if _state["done"] is not None else int(payload.get("done") or 0)
        except (TypeError, ValueError):
            payload["done"] = 0
        try:
            payload["total"] = int(_state["total"]) if _state["total"] is not None else int(payload.get("total") or 0)
        except (TypeError, ValueError):
            payload["total"] = 0

        payload["node"] = str(_state["node"] or "")
        payload["nodes"] = {str(key): str(value) for key, value in _state["nodes"].items()}
        payload["elapsed"] = round(elapsed(), 3)
        payload["started_at"] = float(_state["started_at"] or 0.0)

        _atomic_write(path, payload)
    except Exception:
        pass


def _atomic_write(file_path: str, payload) -> None:
    """先写 ``.tmp`` 再 os.replace，避免主进程读到半截文件；失败静默。"""
    temp_path = file_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, file_path)
        return
    except OSError:
        pass

    try:
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
    except OSError:
        pass
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
