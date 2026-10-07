"""把当前工程里的 .dds 图片引用转成同目录下的 .png，并把引用改到新文件。

为什么需要它：Blender 解不了工作空间里常见的 BC7 / BC6H 压缩 DDS，加载时会打
``WARNING DDS image ... is not in a supported GPU compression format`` 并退化成未压缩
读取（BC6H 的 LightMap / MaterialMap 还会 ``failed to load data from file``）。转成
PNG 后 Blender 能正常读，控制台也不再刷这些警告。

三条实测得出的设计约束（数字见 tests/test_dds_to_png.py）：

1. **色彩空间交给 texconv 自己判断**：普通（整型）源只给 ``-ft png``，*不给* ``-f``，
   texconv 会保留输入的 sRGB 语义（``BC7_UNORM_SRGB`` -> ``R8G8B8A8_UNORM_SRGB``，
   ``BC7_UNORM`` -> ``R8G8B8A8_UNORM``）。实测这与手工按格式指定输出逐像素完全一致
   （mean_abs_diff = 0.0）；而手工指定一旦写错，就会让 sRGB 贴图被线性解码一次，
   表现为整片颜色变深。
2. **float/HDR 源（BC6H 等）必须钉死 16bit 输出**：texconv 把 float 写成 8bit PNG 时
   会走 sRGB 编码路径，数值整体非线性提亮（LightMap 实测 0.2172 -> 0.2993）；改成
   ``-f R16G16B16A16_UNORM`` 后逐像素一致（max diff 0.00001）。见 :func:`is_hdr_dds`。
3. **原 .dds 一律保留**（用户明确要求），所以这里不做任何删除动作。
4. **重连只换容器与路径**：沿用图片原有的色彩空间与 Alpha 模式，不改用户已设好的
   配置 —— 与 :mod:`tt_dds_conversion` 的既有约定保持一致。
"""

import os
import struct
import subprocess

import bpy

from .tt_dds_conversion import find_texconv


_AUTO_INTERVAL = 0.005
_LOG_PREFIX = "[DDS转PNG]"

# 需要 16bit 输出的浮点/HDR 源格式（DXGI 格式号，取自 dxgiformat.h）：
# R32G32B32A32_FLOAT / R32G32B32_FLOAT / R16G16B16A16_FLOAT / R32G32_FLOAT /
# R16G16_FLOAT / R32_FLOAT / R16_FLOAT / R9G9B9E5_SHAREDEXP / BC6H_UF16 / BC6H_SF16。
# 判据是"能表示大于 1.0 的数值"，因此 BC6H 这类 HDR 块压缩格式也在内。
_HDR_DXGI_FORMATS = {2, 6, 10, 16, 34, 41, 54, 67, 95, 96}

# 自动转换任务的分帧状态；None 表示当前没有任务在跑。
_AUTO_JOB = {
    "images": None,
    "force": False,
    "converted": 0,
    "skipped": 0,
    "failed": 0,
}


def png_path_for(dds_path):
    """同名替换扩展名：``a/b/X-LightMap.dds`` -> ``a/b/X-LightMap.png``。"""
    return os.path.splitext(dds_path)[0] + ".png"


def is_hdr_dds(dds_path):
    """判断 DDS 是否是浮点/HDR 源（BC6H、R32F 等），这类源必须转 16bit PNG。

    为什么单独对待：float 源写成 **8bit** PNG 时，texconv 会走一条 sRGB 编码路径，
    把数值整体非线性提亮（实测 LightMap：精确 0.2172 -> PNG 0.2993，低端放大 3.8 倍），
    超 1.0 的高光截断并不是主因。改成 16bit 输出后逐像素一致（max diff 0.00001）。

    只有带 DX10 扩展头的 DDS 才带显式 DXGI 格式号；无该头的 legacy 格式
    （DXT1/3/5、A8R8G8B8 等）都是整型，直接当作非 HDR。
    """
    try:
        with open(dds_path, "rb") as handle:
            head = handle.read(148)
    except OSError:
        return False
    if len(head) < 132 or head[:4] != b"DDS " or head[84:88] != b"DX10":
        return False
    (dxgi_format,) = struct.unpack_from("<I", head, 128)
    return dxgi_format in _HDR_DXGI_FORMATS


def png_bit_depth(png_path):
    """读 PNG IHDR 的位深（8 / 16），读不到返回 None。

    用作 HDR 源转换后的**结果校验**：确认 texconv 确实产出了 16bit PNG。
    （不能反过来用它判断"既有 png 能不能沿用"—— 实测存在位深 16 但内容被
    sRGB 编码改亮的产物，位深对不代表内容对。）
    """
    try:
        with open(png_path, "rb") as handle:
            head = handle.read(26)
    except OSError:
        return None
    if len(head) < 25 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return head[24]


def iter_dds_images():
    """收集工程里所有指向 .dds 的 FILE 图片，返回 ``[(image, 绝对路径), ...]``。

    先把列表取全再动手，避免边遍历 ``bpy.data.images`` 边改 ``filepath`` 时的歧义；
    同时这也让"本次要处理哪些图"在转换开始前就固定下来。
    """
    found = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        try:
            abs_path = os.path.normpath(bpy.path.abspath(image.filepath_raw))
        except Exception:
            continue
        if os.path.splitext(abs_path)[1].lower() != ".dds":
            continue
        found.append((image, abs_path))
    return found


def convert_dds_to_png(texconv, dds_path):
    """用 texconv 在 dds 同目录生成同名 .png，返回 png 绝对路径。

    不使用 ``-f``：由 texconv 依据输入 DDS 的格式语义挑未压缩输出格式，从而保证
    sRGB 进 sRGB 出、线性进线性出。``-y`` 用于覆盖同名旧文件。
    """
    out_dir = os.path.dirname(dds_path)
    command = [texconv]
    if is_hdr_dds(dds_path):
        # float/HDR 源：钉死 16bit 输出，否则 8bit PNG 会被 sRGB 编码整体提亮
        command += ["-f", "R16G16B16A16_UNORM"]
    command += ["-ft", "png", "-o", out_dir, "-y", dds_path]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise RuntimeError(detail or f"texconv 退出码 {process.returncode}")

    png_path = png_path_for(dds_path)
    if not os.path.exists(png_path):
        raise RuntimeError(f"texconv 未生成 {png_path}")
    return png_path


def relink_image(image, png_path):
    """把图片重连到 png：只换路径与容器格式，显示配置原样保留。"""
    try:
        colorspace = str(image.colorspace_settings.name or "")
    except Exception:
        colorspace = ""
    try:
        alpha_mode = str(image.alpha_mode or "")
    except Exception:
        alpha_mode = ""

    image.filepath = png_path
    image.reload()

    if colorspace:
        try:
            if image.colorspace_settings.name != colorspace:
                image.colorspace_settings.name = colorspace
        except Exception:
            pass
    if alpha_mode:
        try:
            if image.alpha_mode != alpha_mode:
                image.alpha_mode = alpha_mode
        except Exception:
            pass


def process_one(image, dds_path, force=False):
    """处理一张图片，返回 ``(状态, 说明)``，状态取值 converted / skipped / failed。

    ``force=False`` 时：同目录已有比 .dds 更新的 .png 就跳过转换，只重连引用 ——
    重复导入同一个工作空间时不必反复跑 texconv。``force=True`` 则始终重转一遍，
    用于"怀疑磁盘上的 png 不对"时的手动兜底。
    """
    png_path = png_path_for(dds_path)
    hdr_source = is_hdr_dds(dds_path)
    # HDR 源一律重转，不沿用磁盘上任何既有 png。原因：不指定输出格式时 texconv 会把
    # BC6H 这类 float 源写成 sRGB 编码的 PNG，8bit 和 16bit 两种位深都会偏亮（实测同一张
    # LightMap 精确均值 0.2172，磁盘上的 8bit 产物与 16bit 产物都是 0.299）。也就是说
    # **位深对不代表内容对**，只有显式 ``-f R16G16B16A16_UNORM`` 的输出才可信。
    if not force and not hdr_source and os.path.exists(png_path):
        try:
            if os.path.getmtime(png_path) >= os.path.getmtime(dds_path):
                relink_image(image, png_path)
                return "skipped", png_path
        except OSError:
            pass

    texconv = find_texconv()
    if not texconv:
        return "failed", "未找到 texconv.exe"
    try:
        convert_dds_to_png(texconv, dds_path)
    except Exception as exc:
        return "failed", str(exc)

    if hdr_source and png_bit_depth(png_path) != 16:
        # 结果校验：HDR 源没产出 16bit PNG 说明命令没生效，宁可报失败也别留错图
        return "failed", "HDR 源未产出 16bit PNG"

    try:
        relink_image(image, png_path)
    except Exception as exc:
        return "failed", f"重连引用失败: {exc}"
    return "converted", png_path


def convert_all(force=False, reporter=None):
    """同步处理工程里全部 .dds 引用，返回统计字典。

    ``reporter`` 为一个接受 ``(类别, 文本)`` 的可调用对象；传 None 时只打印。
    手动按钮用它把结果送进 ``self.report``，自动流程用默认打印。
    """
    def emit(category, message):
        if reporter is not None:
            reporter(category, message)
        else:
            print(f"{_LOG_PREFIX} {message}")

    targets = iter_dds_images()
    if not targets:
        emit("INFO", "工程里没有指向 .dds 的图片引用。")
        return {"total": 0, "converted": 0, "skipped": 0, "failed": 0}

    if not find_texconv():
        emit("ERROR", "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。")
        return {"total": len(targets), "converted": 0, "skipped": 0, "failed": len(targets)}

    stats = {"total": len(targets), "converted": 0, "skipped": 0, "failed": 0}
    for image, dds_path in targets:
        status, detail = process_one(image, dds_path, force=force)
        stats[status] += 1
        if status == "failed":
            emit("WARNING", f"{os.path.basename(dds_path)} 转换失败：{detail}")

    emit(
        "INFO",
        f"共 {stats['total']} 个 DDS 引用：转换 {stats['converted']} 个，"
        f"沿用已有 PNG {stats['skipped']} 个，失败 {stats['failed']} 个。原 .dds 已全部保留。",
    )
    return stats


def _get_props():
    """取 texture_tools_props；timer 里 bpy.context 可能没有 scene，用首个场景兜底。"""
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    if scene is None:
        scene = bpy.data.scenes[0] if len(bpy.data.scenes) else None
    if scene is None:
        return None
    return getattr(scene, "texture_tools_props", None)


def _auto_tick():
    """自动转换的分帧回调：每帧只处理一张，避免长时间阻塞界面。"""
    job = _AUTO_JOB
    images = job["images"]
    if images is None:
        return None

    if not images:
        print(
            f"{_LOG_PREFIX} 自动转换完成：转换 {job['converted']} 个，"
            f"沿用已有 PNG {job['skipped']} 个，失败 {job['failed']} 个。"
        )
        job["images"] = None
        return None

    image, dds_path = images.pop()
    status, detail = process_one(image, dds_path, force=job["force"])
    job[status] += 1
    if status == "failed":
        print(f"{_LOG_PREFIX} {os.path.basename(dds_path)} 转换失败：{detail}")
    return _AUTO_INTERVAL


def schedule_auto_convert(context=None, force=False):
    """导入流程结束后调度一次自动转换；未勾选或已有任务在跑时什么都不做。"""
    props = _get_props()
    if props is None or not getattr(props, "dds_auto_convert_png_after_import", False):
        return False
    if _AUTO_JOB["images"] is not None:
        return False

    targets = iter_dds_images()
    if not targets:
        return False

    _AUTO_JOB.update(
        {
            "images": targets,
            "force": force,
            "converted": 0,
            "skipped": 0,
            "failed": 0,
        }
    )
    print(f"{_LOG_PREFIX} 检测到 {len(targets)} 个 DDS 引用，开始自动转换为 PNG。")
    # 用 timer 而不是当场执行：导入算子还没返回，此时长耗时会卡住整个导入。
    bpy.app.timers.register(_auto_tick, first_interval=0.5)
    return True


class TT_OT_convert_dds_to_png(bpy.types.Operator):
    bl_idname = "toolkit.tt_convert_dds_to_png"
    bl_label = "立即把工程内 DDS 引用转为 PNG"
    bl_description = (
        "扫描当前工程里所有指向 .dds 的图片，用 texconv 在同目录生成同名 .png 并改掉引用。"
        "原 .dds 文件保留不动。已存在的 .png 也会重新转换，用于修正颜色不对的旧文件"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        stats = convert_all(force=True, reporter=self.report)
        if stats["total"] == 0:
            return {"CANCELLED"}
        if stats["failed"] and not stats["converted"] and not stats["skipped"]:
            return {"CANCELLED"}
        return {"FINISHED"}


tt_dds_to_png_list = [
    TT_OT_convert_dds_to_png,
]
