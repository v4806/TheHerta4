"""在**不改游戏**的前提下把 Toolset 里的 HLSL 真编一遍，抓语法/架构错误。

为什么需要它（t89，2026-10-24 实机）：
    `Toolset/himi_merged_skin.hlsl` 里 `dst_rows.GetDimensions(dst_count);` 少写了一个出参
    （`RWStructuredBuffer` 是**双参**：元素数、元素字节数），编译器报
    `error X3013: 'GetDimensions': no matching 1 parameter intrinsic method`，
    **整段 CS 编译失败**。
    3DMigoto 对自定义 shader 的失败是「这个 shader 不存在」，ini 里照旧 dispatch ⇒
    蒙皮缓冲全零 ⇒ 模型消失。而 tests/ 里所有 HIMI 判据都是**源码文本级**的
    （钉 `Copy vs-t0` 的处数、段体行数），钉不住 HLSL 能不能编 —— 这类错只有
    d3d11 日志能看见，代价是启一次游戏。本工具把这个环提前到命令行。

怎么做的：
    ctypes 直接调 `C:\\Windows\\System32\\D3DCompiler_47.dll` 的 `D3DCompile` ——
    与 3DMigoto 同族的编译器（同一个 d3dcompiler），**不需要 fxc.exe 或 Windows SDK**。
    源码原样喂给编译器（UTF-8 中文注释无影响），`pSourceName` 传文件路径，
    报错里就带 `文件(行): error ...`。

范围：
    只适用于**自包含**的 shader（无 `#include`、无 `@@PLACEHOLDER@@` 占位）。
    目前是 `Toolset/himi_merged_skin.hlsl`、`Toolset/drag_interaction/rzm_jiggle_interaction.hlsl`。
    带 `#include` 或占位的（`himi_attach.hlsl` / `himi_merged_skeleton_attach.hlsl` /
    `himi_attach_view.hlsl`）不在此列：它们要 3DMigoto 的 include 体系和导出期替换。

用法（仓库根目录）：
    python tools/check_hlsl_compiles.py Toolset/himi_merged_skin.hlsl
    python tools/check_hlsl_compiles.py <source> <mod 里那份拷贝> ...   # 一次编多份对照
    python tools/check_hlsl_compiles.py --target ps_5_0 --entry main foo.hlsl

退出码：0 = 全部编过；1 = 至少一份失败（错误原文打到 stdout，含行号）。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

# D3DCOMPILE_ENABLE_STRICTNESS（+ shift warnings/errors 让它们一眼能看见）
D3DCOMPILE_ENABLE_STRICTNESS = 0x00000008

D3DCOMPILER_CANDIDATES = (
    r"C:\Windows\System32\D3DCompiler_47.dll",
    r"C:\Windows\SysWOW64\D3DCompiler_47.dll",
)


def _load_compiler():
    last_error = None
    for candidate in D3DCOMPILER_CANDIDATES:
        if not Path(candidate).is_file():
            continue
        try:
            return ctypes.WinDLL(candidate)
        except OSError as exc:  # pragma: no cover - 取决于机器
            last_error = exc
    raise RuntimeError(
        "找不到可用的 D3DCompiler_47.dll（试过："
        + "、".join(D3DCOMPILER_CANDIDATES)
        + "）"
        + (f"；最后一次失败：{last_error}" if last_error else "")
    )


def _vtable_slot(ptr: int, index: int):
    """取 COM 接口 ptr 的 vtable 第 index 个函数地址。"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return vtable[index]


def _blob_payload(ptr: int) -> bytes:
    if not ptr:
        return b""
    get_pointer = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(_vtable_slot(ptr, 3))
    get_size = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(_vtable_slot(ptr, 4))
    size = get_size(ptr)
    data = get_pointer(ptr)
    return ctypes.string_at(data, size) if size and data else b""


def _blob_release(ptr: int) -> None:
    if not ptr:
        return
    release = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(_vtable_slot(ptr, 2))
    release(ptr)


def compile_shader(
    path: Path,
    target: str = "cs_5_0",
    entry: str = "main",
    source_name: str | None = None,
) -> tuple[bool, bytes, str]:
    """编一份 HLSL。返回 (是否通过, 字节码, 编译日志)。"""
    d3d = _load_compiler()
    d3d.D3DCompile.restype = ctypes.c_int32
    d3d.D3DCompile.argtypes = [
        ctypes.c_void_p,  # pSrcData
        ctypes.c_size_t,  # SrcDataSize
        ctypes.c_char_p,  # pSourceName
        ctypes.c_void_p,  # pDefines
        ctypes.c_void_p,  # pInclude
        ctypes.c_char_p,  # pEntrypoint
        ctypes.c_char_p,  # pTarget
        ctypes.c_uint,  # Flags1
        ctypes.c_uint,  # Flags2
        ctypes.POINTER(ctypes.c_void_p),  # ppCode
        ctypes.POINTER(ctypes.c_void_p),  # ppErrorMsgs
    ]

    raw = path.read_bytes()
    source_buffer = ctypes.create_string_buffer(raw, len(raw))
    shown_name = (source_name or str(path)).encode("utf-8")

    code = ctypes.c_void_p()
    errors = ctypes.c_void_p()
    hresult = d3d.D3DCompile(
        ctypes.cast(source_buffer, ctypes.c_void_p),
        len(raw),
        shown_name,
        None,
        None,
        entry.encode("ascii"),
        target.encode("ascii"),
        D3DCOMPILE_ENABLE_STRICTNESS,
        0,
        ctypes.byref(code),
        ctypes.byref(errors),
    )

    log = _blob_payload(errors.value or 0).decode("utf-8", "replace").strip()
    payload = _blob_payload(code.value or 0)
    _blob_release(code.value or 0)
    _blob_release(errors.value or 0)

    if hresult < 0:
        return False, b"", log or f"D3DCompile 返回 0x{hresult & 0xFFFFFFFF:08X}"
    return True, payload, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用 D3DCompiler_47 把自包含的 HLSL 真编一遍（不进游戏）。"
    )
    parser.add_argument("files", nargs="+", help="要编译的 .hlsl 路径（可多份，逐份编）")
    parser.add_argument("--target", default="cs_5_0", help="编译 profile，默认 cs_5_0")
    parser.add_argument("--entry", default="main", help="入口函数名，默认 main")
    args = parser.parse_args(argv)

    failed = 0
    for file_name in args.files:
        path = Path(file_name)
        if not path.is_file():
            print(f"[缺失] {path}")
            failed += 1
            continue
        ok, code, log = compile_shader(path, target=args.target, entry=args.entry)
        if ok:
            print(f"[通过] {path}  ({args.target}/{args.entry}, {len(code)} B 字节码)")
            if log:
                print("  ↳ 编译器警告：")
                for line in log.splitlines():
                    print(f"    {line}")
        else:
            failed += 1
            print(f"[失败] {path}  ({args.target}/{args.entry})")
            for line in log.splitlines():
                print(f"    {line}")

    if failed:
        print(f"\n{failed} 份没编过 —— 这类错进游戏只会表现成「模型消失」，不会报错。")
        return 1
    print("\n全部编过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
