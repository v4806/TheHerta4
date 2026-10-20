"""后台导出的细粒度进度中心（worker 进程内使用）。

主进程只能通过会话目录里的 ``stage.json`` 观察后台实例，所以 worker 侧
"导出到哪一步了"都要落到这个文件里。本模块只做四件事：

1. 记录当前阶段、当前节点与节点状态表（pending / running / done）；
2. 原子写 ``stage.json``，写失败一律静默 —— 进度绝不能拖垮导出；
3. 提供 ``elapsed()`` 给主进程显示本次后台导出已经跑了多久；
4. 维护**固定步骤计数**（``begin_steps`` / ``advance_stage`` / ``report_node`` /
   ``finish_steps``）：``stage.json`` 的 ``done`` / ``total`` 是"已完成步数 /
   总步数"。

步骤模型（**清单在导出开始时一次算好，整次导出不变**）：

- ``begin_steps(total, plan=None)``：开始一次导出，``done=0``，``total`` 取
  ``len(plan)``（没给清单时才用传入预算 ``total``）。``plan`` 由
  ``blueprint/export_step_plan.py::build_step_plan`` 一次算出，**之后不再增长**。
- ``advance_stage(name)``：``TimerUtils.start_stage`` 的统一入口 —— 进入与上一个
  不同的阶段名时，先把**上一个阶段**记为完成（``done += 1``），但**只在它属于
  计划清单、且整次导出里还没计过时**才推进；清单之外的阶段只更新状态栏文本，
  不计步、也不改 ``total``。同名阶段重入 / 重复出现也只计一次（基础导出内部
  会反复切换 ``数据哈希预计算`` 等子阶段，不能重复累加）。
- ``report_stage(name, ...)``：只更新状态栏**显示文本**（如"转换贴图 26/60"），
  **不改 ``done`` / ``total``**；贴图张数这类深层内部进度只做文本提示。
- ``report_node(name, "done")``：后处理节点完成时计 1 步（每节点只算 1 步）；
  计划内、且同名节点没计过时才推进。节点内部最后启动的阶段若在计划里，节点
  完成时一并记完成（该阶段与节点各 1 步，同名不重复计）。
- ``finish_steps()``：收尾把 ``done`` 补到 ``total``（保证 100%）；``total`` 不变。

百分比由主进程按 ``done / total`` 计算，并保留单调不回退（兜底旧 worker）。

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
    #: 已经计入 done 的计步单元：``("step", 名称)``。同名阶段 / 节点在整次导出里
    #: 只计一次（基础导出内部会反复切换同名子阶段）
    "counted": set(),
    #: 导出开始时一次算好的固定步骤清单（有序）与查找集合；空 = 旧预算模式
    "plan": [],
    "plan_set": set(),
    #: 当前正在执行的节点内部是否启动过阶段（该节点完成时要一并记该阶段）
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
    _state["plan"] = []
    _state["plan_set"] = set()
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
    _state["plan"] = []
    _state["plan_set"] = set()
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


def begin_steps(total, plan=None) -> None:
    """开始一次导出的步骤计数：``done=0``，``total`` 固定为计划清单长度。

    ``plan`` 由 ``export_step_plan.build_step_plan`` 在导出开始时**一次算好**；
    传入清单时 ``total = len(plan)``，整次导出不再变化。没给清单时才退回
    ``total`` 传入值（旧预算 / 兼容旧调用方）。
    """
    plan_list = []
    seen = set()
    for key in plan or []:
        text = str(key or "")
        if not text or text in seen:
            continue
        seen.add(text)
        plan_list.append(text)

    _state["plan"] = plan_list
    _state["plan_set"] = set(plan_list)
    _state["done"] = 0
    _state["total"] = len(plan_list) if plan_list else max(0, _as_int(total, 0))
    _state["stage_key"] = ""
    _state["stage_pending"] = False
    _state["counted"] = set()
    _state["node_started_stage"] = False
    _write_stage_file()


def ensure_plan(plan) -> bool:
    """计划清单还没建立时用 ``plan`` 建立；返回是否真的建立了。

    供 ``direct_export`` 在同步直出（没有后台会话清单）时自建计划；后台导出
    已经由 ``_run_worker`` 建好清单时不会被覆盖。
    """
    if _state["plan"]:
        return False
    keys = [str(key or "") for key in plan or [] if str(key or "")]
    if not keys:
        return False
    begin_steps(len(keys), plan=keys)
    return True


def _complete_step(name) -> bool:
    """把计划清单里的一步记为完成；清单外 / 同名重复一律不计。返回是否推进。"""
    text = str(name or "")
    if not text:
        return False
    marker = ("step", text)
    if marker in _state["counted"]:
        return False
    plan_set = _state["plan_set"]
    if plan_set and text not in plan_set:
        return False
    total = step_total()
    # 没有分母（还没 begin_steps）或计划已满：不推进，避免 done/total 失真
    if total <= 0 or step_done() >= total:
        return False
    _state["counted"].add(marker)
    _state["done"] = step_done() + 1
    return True


def complete_step(key: str = "") -> int:
    """完成计划清单里的一步（``key`` = 清单里的名字）。返回当前 done。

    旧调用方（没有计划清单、只传预算）可以不带 ``key``：按剩余预算推进 1 步。
    有计划清单时无名步骤不推进 —— 避免清单外的动作偷偷涨百分比。
    """
    name = str(key or "")
    if name:
        _complete_step(name)
    elif not _state["plan"]:
        _complete_step(f"__unnamed_{step_done() + 1}__")
    _write_stage_file()
    return step_done()


def finish_steps() -> int:
    """收尾：把 ``done`` 补到 ``total``（保证 100%）；``total`` 不变。"""
    total = step_total()
    if not _state["plan"]:
        # 旧预算模式没有计划清单：done 超出预算时只能抬 total（不再增长不适用）
        total = max(total, step_done())
    _state["total"] = total
    _state["done"] = max(total, step_done())
    _state["stage_pending"] = False
    _write_stage_file()
    return _state["done"]


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

    - 第一个阶段不推进（它才刚开始，完成与否等下一个阶段出现才知道）；
    - 同名阶段重入不推进（直出节点循环里同一步可能多次 ``start_stage``）；
    - 同名阶段在整次导出里重复出现也只计一次（基础导出内部会反复切换
      ``数据哈希预计算`` / ``对象处理与合并`` 等子阶段，不能重复累加）；
    - **只有计划清单内、且没计过的阶段才 ``done += 1``；清单之外的阶段只更新
      状态栏文本，不计步、也不改 ``total``**（``total`` 导出开始时已定死）；
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
        _complete_step(previous)
    if _state.get("node"):
        _state["node_started_stage"] = True
    if name != previous:
        _state["stage_key"] = name
    _state["stage"] = name
    _state["stage_pending"] = True
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

    **每个后处理节点在整次导出里只算 1 步**（计划清单里每个节点一项、同名
    只计一次）；清单之外的节点只更新高亮，不计步、不改 ``total``。

    若该节点执行期间自己启动过导出阶段（例如直出形态键 / 多文件节点），
    完成时把**节点内部最后启动的那个阶段**一并记为完成（该阶段与节点各算
    1 步，同名不重复计、且同样要求它在计划清单内）。

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
                _complete_step(str(_state.get("stage_key") or ""))
                _state["stage_pending"] = False
            # 直出模式节点键是 ``树名/节点名``，普通模式是纯节点名：两种都试
            if not _complete_step(key):
                _complete_step(node_label)
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
        "plan": list(_state["plan"]),
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
        # 固定步骤清单（调试用）：导出开始时一次算好，整次导出不变
        if _state["plan"]:
            payload["plan"] = list(_state["plan"])
            payload["plan_total"] = len(_state["plan"])
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
