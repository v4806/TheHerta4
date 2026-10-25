"""离线生成 `res/himi_attach.hlsl`（HIMI 合并骨架 attach CS）——不开 Blender、不跑导出。

为什么要它：attach CS 是 `ui/universal/himi.py::merged_attach_hlsl_text()` 渲染出来的
**静态文本**，导出时才落盘。想验证「这一版生成器渲染出的 CS 能不能编过」「已经部署的
mod 里的 CS 是不是和当前生成器一致」，都必须能在命令行里单独渲染一份。

它做三件事：
  1. 按 pytest 用的那套假宿主（无 bpy）把 `ui/universal/himi.py` 加载起来；
  2. 用 `build_merged_skeleton_decl(role_key, slots)` 签出声明、渲染 CS（基址表由 plan
     生成；给了 `--base-table-from` 就用部署里那几份身份载荷当 plan），
     并立刻跑 `merged_attach_hlsl_problems()`（生成器/判据一致性硬门）；
  3. `--out` 给了就写文件（与已有内容相同则不重写），否则只打印。

用法（仓库根目录）：
    python tools/himi_attach_cs_offline.py --role-key 06c391d0 --slots 219
    python tools/himi_attach_cs_offline.py --role-key role_06c391d0 --slots 219 \\
        --base-table-from "K:/SSMT-Package-master/3Dmigoto/HI3/Mods/SSMTGeneratedMod/虚构游戏" \\
        --families 077983c9,09c6e58a,1b62725d
    python tools/himi_attach_cs_offline.py --decl REFERENCE,343       # 参考声明（不含任何部署信息）
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "himi_offline_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, leaf = name.rpartition(".")
    if parent in sys.modules and leaf:
        setattr(sys.modules[parent], leaf, module)
    return module


def _load_real_module(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    parent, _, leaf = qualname.rpartition(".")
    if parent in sys.modules and leaf:
        setattr(sys.modules[parent], leaf, module)
    spec.loader.exec_module(module)
    return module


def load_generator():
    """加载 `ui/universal/himi.py`（返回模块本身），副作用是把 fake 宿主登记进 sys.modules。"""
    if REPO_ROOT.as_posix() not in sys.path:
        sys.path.append(REPO_ROOT.as_posix())

    for package_name in (
        PKG,
        f"{PKG}.ui",
        f"{PKG}.ui.universal",
        f"{PKG}.common",
        f"{PKG}.utils",
        f"{PKG}.blueprint",
    ):
        _install_module(package_name).__path__ = []

    for qualname, relpath in (
        (f"{PKG}.utils.json_utils", "utils/json_utils.py"),
        (f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py"),
        (f"{PKG}.utils.format_utils", "utils/format_utils.py"),
        (f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py"),
        (f"{PKG}.common.logic_name", "common/logic_name.py"),
        (f"{PKG}.common.global_key_count_helper", "common/global_key_count_helper.py"),
        (f"{PKG}.common.himi_merged_contract", "common/himi_merged_contract.py"),
        (f"{PKG}.common.himi_skeleton", "common/himi_skeleton.py"),
    ):
        _load_real_module(qualname, relpath)

    state = {"workspace": "", "mod": ""}
    _install_module(
        f"{PKG}.common.global_config",
        GlobalConfig=types.SimpleNamespace(
            path_workspace_folder=lambda: state["workspace"],
            path_generate_mod_folder=lambda: state["mod"],
            path_generatemod_buffer_folder=lambda: (
                os.path.join(state["mod"], "Meshes") if state["mod"] else ""
            ),
            get_workspace_name=lambda: os.path.basename(state["workspace"].rstrip("\\/")),
        ),
    )
    _install_module(
        f"{PKG}.common.global_properties",
        GlobalProterties=types.SimpleNamespace(
            import_merged_vgmap=lambda: True,
            forbid_auto_texture_ini=lambda: False,
            generate_branch_mod_gui=lambda: False,
        ),
    )
    _install_module(
        f"{PKG}.common.m_ini_helper",
        M_IniHelper=types.SimpleNamespace(
            get_drawindexed_str_list=lambda *_a, **_k: [],
            is_slot_binding_mark_type=lambda *_a, **_k: False,
        ),
    )
    _install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
    _install_module(
        f"{PKG}.utils.timer_utils",
        TimerUtils=types.SimpleNamespace(
            start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
        ),
    )
    _install_module(
        f"{PKG}.blueprint.export_helper",
        BlueprintExportHelper=types.SimpleNamespace(
            current_buffer_folder_name="Meshes",
            get_current_buffer_folder_name=lambda: "Meshes",
        ),
    )

    class _FakeDrawIBExportBase:
        def __init__(self, blueprint_model, combine_ib=False):
            self.blueprint_model = blueprint_model
            self.drawib_model_list = []

    _install_module(f"{PKG}.ui.universal.drawib_export_base", DrawIBExportBase=_FakeDrawIBExportBase)

    _load_real_module(f"{PKG}.common.himi_merged_runtime", "common/himi_merged_runtime.py")
    _load_real_module(f"{PKG}.common.himi_family_x", "common/himi_family_x.py")
    _load_real_module(f"{PKG}.ui.universal.unity", "ui/universal/unity.py")

    if importlib.util.find_spec("bpy") is None and "bpy" not in sys.modules:
        _install_module("bpy")
    if "mathutils" not in sys.modules:
        class _FakeVector(tuple):
            def __new__(cls, values=(0.0, 0.0, 0.0)):
                return super().__new__(cls, tuple(values))

        _install_module("mathutils", Vector=_FakeVector)

    return _load_real_module(f"{PKG}.ui.universal.himi", "ui/universal/himi.py")


def _base_table_ranges_from_mod(runtime, mod_root: Path, families) -> dict:
    """从部署的 AttachRange 载荷里读出每族的 ``(base, span)``，拼成 ``plan.family_ranges`` 形状。

    基址表由**导出侧的 plan** 生成（`merged_attach_hlsl_text(decl, ranges=...)`），而不是
    任何冻结常量。``--base-table-from`` 就是把部署里那几份身份载荷读回来当作 plan —— 于是
    离线渲染出的表与「真导出会写出来的那张表」同源，可以直接和部署里的 CS 逐行对照。
    """
    ranges = {}
    folder = runtime.ATTACH_RANGE_DEFAULT_FOLDER
    for family in families:
        path = mod_root / folder / runtime.attach_range_basename(family)
        if not path.is_file():
            raise SystemExit(f"[fatal] 缺身份载荷 {path}")
        info = runtime.decode_attach_range_payload(path.read_bytes())
        if not info.get("ok"):
            raise SystemExit(f"[fatal] 身份载荷 {path} 不合法：{info}")
        ranges[family] = {
            "base": int(info["base"]),
            "slots": int(info["span"]),
            "own_block": True,
        }
    return ranges


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线渲染 HIMI 合并骨架 attach CS")
    parser.add_argument("--role-key", default="", help="合并骨架的角色键（ini 里的 _role_<key>）")
    parser.add_argument("--slots", type=int, default=0, help="合并骨架槽位数（= ini array / 3）")
    parser.add_argument(
        "--decl",
        default="",
        help="ROLE_KEY,SLOTS 简写；省略 --role-key/--slots 时用 REFERENCE,343",
    )
    parser.add_argument("--out", default="", help="写到哪里；不写则只打印摘要")
    parser.add_argument("--print", dest="dump", action="store_true", help="把全文打到 stdout")
    parser.add_argument(
        "--base-table-from",
        default="",
        help="用这个 mod 根下的 Meshes/<fam>-AttachRange.buf 当本次 plan（配合 --families）来生成基址表",
    )
    parser.add_argument(
        "--families", default="", help="逗号分隔的族 ID，顺序即 ini 里的族顺序（配合 --base-table-from）"
    )
    args = parser.parse_args(argv)

    role_key, slots = args.role_key.strip(), int(args.slots)
    if args.decl:
        role_key, _, raw_slots = args.decl.partition(",")
        role_key, slots = role_key.strip(), int(raw_slots or 0)

    himi = load_generator()
    runtime = sys.modules[f"{PKG}.common.himi_merged_runtime"]
    if not role_key or slots <= 0:
        role_key, slots = runtime.REFERENCE_ROLE_KEY, runtime.REFERENCE_SLOT_COUNT
        print(f"[ctx] 未给声明，用参考声明 {role_key}/{slots}")

    decl = runtime.build_merged_skeleton_decl(role_key, slots)
    ranges = None
    if args.base_table_from:
        families = [item.strip() for item in args.families.split(",") if item.strip()]
        if not families:
            print("[fatal] --base-table-from 需要 --families")
            return 2
        ranges = _base_table_ranges_from_mod(runtime, Path(args.base_table_from), families)
        audit = himi.merged_attach_base_table_audit(ranges, slots)
        if audit.get("problems"):
            print("[fatal] 基址表与本次排布矛盾：")
            for item in audit["problems"]:
                print("  -", item)
            return 2
        for item in audit.get("notices") or ():
            print("[notice]", item)

    text = himi.merged_attach_hlsl_text(decl, ranges=ranges)
    problems = himi.merged_attach_hlsl_problems(text)
    print(f"[decl] {decl.machine_line()}")
    print(f"[cs] {len(text)} 字符 / {text.count(chr(10)) + 1} 行 —— {decl.name} slots={decl.slots}")
    for row in himi.merged_attach_base_table_lines_in_text(text):
        print(f"[table] {row.strip()}")
    if problems:
        print("[FAIL] merged_attach_hlsl_problems：")
        for item in problems:
            print("  -", item)
        return 1
    print("[ok] merged_attach_hlsl_problems 全绿")

    if args.dump:
        print("----8<----")
        print(text, end="" if text.endswith("\n") else "\n")
        print("---->8----")

    if args.out:
        out_path = Path(args.out)
        payload = text.encode("utf-8")
        written = True
        if out_path.is_file():
            written = out_path.read_bytes() != payload
        if written:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(payload)
        print(f"[{'write' if written else 'same'}] {out_path} ({len(payload)} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
