import json
import os
import re
import shutil
import struct
import subprocess
import tempfile

import bpy

TOOLSET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "Toolset")

# 内置格式规则：**颜色贴图（DiffuseMap）用 sRGB，其余类型一律线性 bc7_unorm**。
# 表里保留各类型条目只是为了让类型名可读（批量算子的报告、规则列表的「重置默认」都按
# 类型列出），它们的 format 与兜底值相同 —— 要单独给某个类型换格式，改这里就行。
# 注意：把磁盘上本来是 _SRGB 的现有 DDS 按非 sRGB 规则重编码会净压暗一次（实测见
# :func:`_texconv_colorspace_flags` 的说明），所以给某类型降级前先确认那份贴图的来源。
DDS_DEFAULT_RULES = [
    {
        "texture_type": "DiffuseMap",
        "pattern": r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "NormalMap",
        "pattern": r"(?i)(?:^|[_\-. ])NormalMap(?:[_\-. ]|$)",
        "format": "r8g8b8a8_unorm",
    },
    {
        "texture_type": "LightMap",
        "pattern": r"(?i)(?:^|[_\-. ])LightMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "MaterialMap",
        "pattern": r"(?i)(?:^|[_\-. ])MaterialMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "RampMap",
        "pattern": r"(?i)(?:^|[_\-. ])RampMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "HighLightMap",
        "pattern": r"(?i)(?:^|[_\-. ])HighLightMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "WengineFX",
        "pattern": r"(?i)(?:^|[_\-. ])WengineFx(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "Glowmap",
        "pattern": r"(?i)(?:^|[_\-. ])Glowmap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "FXMap",
        "pattern": r"(?i)(?:^|[_\-. ])FXMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "TTLMap",
        "pattern": r"(?i)(?:^|[_\-. ])TTLMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "RoughnessMap",
        "pattern": r"(?i)(?:^|[_\-. ])RoughnessMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "ORMMap",
        "pattern": r"(?i)(?:^|[_\-. ])ORMMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
]

_SUPPORTED_SOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}

# 目标格式名 -> DX10 头里偏移 128 的 DXGI 格式号（大小写不敏感比较）
_DDS_DXGI_FORMAT_IDS = {
    "bc7_unorm": 98,
    "bc7_unorm_srgb": 99,
    "bc6h_uf16": 95,
    "bc6h_sf16": 96,
    "bc1_unorm": 71,
    "bc1_unorm_srgb": 72,
    "bc2_unorm": 74,
    "bc2_unorm_srgb": 75,
    "bc3_unorm": 77,
    "bc3_unorm_srgb": 78,
    "bc4_unorm": 80,
    "bc4_snorm": 81,
    "bc5_unorm": 83,
    "bc5_snorm": 85,
    "r8_unorm": 61,
    "r8g8_unorm": 49,
    "r8g8b8a8_typeless": 27,
    "r16_unorm": 56,
    "r16_float": 54,
    "r16g16b16a16_float": 10,
    "r8g8b8a8_unorm": 28,
    "r8g8b8a8_unorm_srgb": 29,
    "b8g8r8a8_unorm": 87,
    "b8g8r8a8_unorm_srgb": 91,
}

# legacy 头（FOURCC 在偏移 84）-> 等价的非 sRGB 格式名。
# legacy FOURCC 没有 sRGB 变体，所以它们永远匹配不上任何 _srgb 目标。
_DDS_LEGACY_FOURCC_FORMATS = {
    b"DXT1": "bc1_unorm",
    b"DXT2": "bc2_unorm",
    b"DXT3": "bc2_unorm",
    b"DXT4": "bc3_unorm",
    b"DXT5": "bc3_unorm",
    b"ATI1": "bc4_unorm",
    b"BC4U": "bc4_unorm",
    b"ATI2": "bc5_unorm",
    b"BC5U": "bc5_unorm",
}

# DDS magic(4) + DDS_HEADER(124) + DX10 扩展头(20)
_DDS_FULL_HEADER_SIZE = 148


def find_texconv():
    props = bpy.context.scene.texture_tools_props

    local_path = os.path.join(TOOLSET_PATH, "texconv.exe")
    if os.path.exists(local_path):
        return local_path

    if props and props.texconv_path and os.path.exists(props.texconv_path):
        return props.texconv_path

    system_path = shutil.which("texconv")
    if system_path:
        return system_path

    return None


def _get_match_targets(filename: str) -> list[str]:
    basename = os.path.basename(filename or "")
    stem, _ext = os.path.splitext(basename)
    targets = []
    for value in (stem, basename):
        value = str(value or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


def _pattern_matches(pattern: str, filename: str) -> bool:
    targets = _get_match_targets(filename)
    return any(re.search(pattern, target) for target in targets)


def _validate_custom_rules(props) -> list[str]:
    errors = []
    if not getattr(props, "dds_use_custom_rules", False):
        return errors

    for index, rule in enumerate(getattr(props, "dds_rules", []) or [], start=1):
        if not getattr(rule, "enabled", True):
            continue
        pattern = str(getattr(rule, "pattern", "") or "").strip()
        if not pattern:
            errors.append(f"规则 {index} 缺少正则表达式")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"规则 {index} 正则无效: {exc}")
    return errors


def _format_is_srgb(dds_format: str) -> bool:
    return "_srgb" in str(dds_format or "").strip().lower()


def _texconv_colorspace_flags(dds_format: str) -> list[str]:
    """按输出 DXGI 格式决定 texconv 的色彩空间标志，保证「数值不变、颜色不变」。

    规则：**输出格式与输入格式的 sRGB 属性一致时才恒等**，不一致就会产生一次色彩变换。
    - ``_srgb`` 输出（bc7_unorm_srgb 等）：输入通常是 PNG 这类**没有色彩空间信息**的
      WIC 位图（texconv 默认当线性读），此时必须用 ``--srgb-in`` 让它按 sRGB 解码、
      再由 sRGB 输出编码回去，净效果恒等。
    - 非 sRGB 输出（bc7_unorm、r8g8b8a8_unorm 等）：用 ``--ignore-srgb`` 按原始数值读写。

    **重要边界（2026-09-27 实测澄清）**：``--ignore-srgb`` 只作用于 WIC 输入/输出，
    **管不了 DDS 自身的 ``_SRGB`` 格式语义**——texconv 读 ``BC7_UNORM_SRGB`` 一定解码、
    写它一定编码。因此把「已经是 _SRGB 的 DDS」按非 sRGB 规则重编码，会净压暗一次
    （反向则提亮一次）；用户工作空间里「颜色贴图整体变暗」正是这么来的
    （规则表被改成全 ``bc7_unorm``，而磁盘上的贴图是 ``_SRGB``）。
    要避免它：让规则与现网文件的 sRGB 属性保持一致，而不是指望开关去补偿。"""
    if _format_is_srgb(dds_format):
        return ["--srgb-in"]
    return ["--ignore-srgb"]


def _apply_image_colorspace(image, dds_format: str):
    """按输出格式设置 Blender 显示色彩空间。

    **注意：这已经不是重连路径的常规动作**，只在快照取不到有效值时作为兜底调用
    （见 :func:`_restore_image_display_state`）。b7b1992 曾把「Blender 显示色彩空间
    按输出格式的 _srgb 后缀设置」当成重连后的常规动作，本次（用户 2026-09-27 要求）
    推翻该行为：重连只换容器格式与文件路径，必须沿用原本的纹理节点配置，不得改写
    用户已设好的色彩空间。
    """
    try:
        image.colorspace_settings.name = "sRGB" if _format_is_srgb(dds_format) else "Non-Color"
    except Exception:
        pass


def _snapshot_image_display_state(image) -> dict:
    """重连前记录图片自身的显示配置（色彩空间 / Alpha 模式）。

    为什么只快照这两个字段：`filepath` + `reload()` 只重建 ImBuf，实测（Blender 5.0.1，
    见 .dbg/relink_probe/relink_probe_result.json）不会改动引用该图的
    `ShaderNodeTexImage` 节点的 interpolation / extension / projection，也不动
    `image_user`；本算子同样不写任何节点字段。因此会覆盖用户配置的只有「按格式硬设
    色彩空间」这一条，快照这两个字段即可覆盖全部会被改写的入口。

    取不到值的字段留空串，由恢复侧自行决定是否按格式兜底。
    """
    state = {"colorspace": "", "alpha_mode": ""}
    try:
        state["colorspace"] = str(image.colorspace_settings.name or "")
    except Exception:
        pass
    try:
        state["alpha_mode"] = str(image.alpha_mode or "")
    except Exception:
        pass
    return state


def _restore_image_display_state(image, snapshot, dds_format: str) -> None:
    """重连后把快照原样写回：转换只换容器格式与文件路径，不改用户既有配置。

    只有快照取不到有效值（读取异常 / 空串）或写回失败时，才退回「按输出格式设置
    色彩空间」的兜底；所有异常都在内部吃掉，绝不中断整个转换循环。
    """
    colorspace = str((snapshot or {}).get("colorspace") or "")
    if not colorspace:
        _apply_image_colorspace(image, dds_format)
    else:
        try:
            if image.colorspace_settings.name != colorspace:
                image.colorspace_settings.name = colorspace
        except Exception:
            _apply_image_colorspace(image, dds_format)

    alpha_mode = str((snapshot or {}).get("alpha_mode") or "")
    if alpha_mode:
        try:
            if image.alpha_mode != alpha_mode:
                image.alpha_mode = alpha_mode
        except Exception:
            pass


_TEXTURE_TYPE_SPLIT_RE = re.compile(r"[_.\-\s]+")


def _first_texture_type_token(texture_type: str) -> str:
    """取类型名的首个 token（按 ``_`` ``.`` ``-`` 空格切分），无有效 token 返回空串。"""
    for token in _TEXTURE_TYPE_SPLIT_RE.split(str(texture_type or "").strip()):
        if token:
            return token
    return ""


def format_for_texture_type(texture_type: str, props) -> str:
    """把材质类型 / 材质名前缀解析成 DDS 格式；未命中返回空串由调用方回退。

    输入可以是 ``DiffuseMap``、``MaterialMap``、``diffusemap_xxx``、
    ``DiffuseMap_VivianGemADiffuse`` 等：取首个 token 判定，大小写不敏感。
    解析顺序：自定义规则（pattern 命中完整类型名）→ 默认规则的 ``texture_type``。
    """
    raw = str(texture_type or "").strip()
    if not raw:
        return ""

    if getattr(props, "dds_use_custom_rules", False):
        for rule in getattr(props, "dds_rules", []) or []:
            if not getattr(rule, "enabled", True):
                continue
            pattern = str(getattr(rule, "pattern", "") or "").strip()
            if not pattern:
                continue
            try:
                if _pattern_matches(pattern, raw):
                    rule_format = str(getattr(rule, "format", "") or "").strip()
                    if rule_format:
                        return rule_format
            except re.error:
                continue

    key = _first_texture_type_token(raw).lower()
    if not key:
        return ""
    for rule in DDS_DEFAULT_RULES:
        if str(rule.get("texture_type", "") or "").strip().lower() == key:
            return str(rule.get("format", "") or "")
    return ""


def _resolve_single_name(name: str, props):
    """按单个候选名匹配规则；未命中返回 None。

    自定义规则优先、默认规则取最早 match 位置 —— 与原 ``resolve_dds_target``
    的匹配语义完全一致，只是抽出来供「源文件名 / hints」逐个重试。
    """
    candidate = str(name or "").strip()
    if not candidate:
        return None

    if getattr(props, "dds_use_custom_rules", False):
        for rule in getattr(props, "dds_rules", []) or []:
            if not getattr(rule, "enabled", True):
                continue
            pattern = str(getattr(rule, "pattern", "") or "").strip()
            if not pattern:
                continue
            try:
                if _pattern_matches(pattern, candidate):
                    rule_format = str(getattr(rule, "format", "") or "").strip()
                    # 自定义规则完全由用户说了算：pattern 只负责识别名称，
                    # format 决定输出格式，不再从文件名推断类型做色彩空间干预
                    return "custom", rule_format or "bc7_unorm", pattern
            except re.error:
                continue

    targets = _get_match_targets(candidate)
    best_rule = None
    best_start = float("inf")
    for rule in DDS_DEFAULT_RULES:
        try:
            pattern = rule["pattern"]
            for target in targets:
                match = re.search(pattern, target)
                if match:
                    if match.start() < best_start:
                        best_start = match.start()
                        best_rule = rule
                    break
        except re.error:
            continue

    if best_rule is not None:
        return best_rule["texture_type"], best_rule["format"], best_rule["texture_type"]
    return None


def resolve_dds_target(filename: str, props, name_hints=()) -> tuple[str, str, str]:
    """按「源文件名 → name_hints」的顺序推断 DDS 目标格式。

    源文件名匹配保持原有语义；源文件名**一个规则都没命中**时，才依次用
    ``name_hints``（材质名 / 贴图槽类型标签 / 输出文件名 stem 等）重试，
    第一个命中的候选名直接返回。全不命中仍回退 ``default/bc7_unorm``。

    为什么要 hints：Mod 导出时输出名已被改成材质资源名，编辑期的源 PNG 可能
    只含 ``Diffuse`` 而不含 ``DiffuseMap``（实测 ``VivianGemADiffuse.png``），
    只按源文件名匹配就会落到线性默认规则。
    """
    hit = _resolve_single_name(filename, props)
    if hit is not None:
        return hit

    for hint in name_hints or ():
        hit = _resolve_single_name(hint, props)
        if hit is not None:
            return hit

    return "default", "bc7_unorm", "Default"


def resolve_texture_dds_format(
    source_path: str,
    props,
    texture_type: str = "",
    name_hints=(),
    format_override: str = "",
    workspace_source_path: str = "",
    match_name: str | None = None,
) -> str:
    """单文件转换的最终输出格式。

    优先级：显式 ``format_override`` → 工作空间原贴图的 DDS 头 → 材质类型规则
    （:func:`format_for_texture_type`）→ :func:`resolve_dds_target`（源文件名 + hints）。

    为什么把「原贴图的头」放在规则表前面：引擎读的是工作空间里那份 DDS，BC5 是双通道
    格式，换成 BC7 写进去引擎照样只读两个通道 —— 多出来的通道白占显存，还可能因为
    格式不匹配读错。所以能读到原贴图就照抄它的格式，规则表只在这张图不在工作空间里
    （源不是 DDS、也定位不到原贴图）时兜底。
    """
    override = str(format_override or "").strip()
    if override:
        return override

    for candidate in (workspace_source_path, source_path):
        detected = read_dds_header_format(str(candidate or ""))
        if detected:
            return detected

    if props is None:
        try:
            props = bpy.context.scene.texture_tools_props
        except Exception:
            props = None

    type_format = format_for_texture_type(texture_type, props)
    if type_format:
        return type_format

    # 材质资源名（导出侧真正的命名依据）优先于源文件名：源图常是带哈希前缀的中间文件。
    if match_name:
        matched_type, matched_format, _matched_by = resolve_dds_target(str(match_name), props)
        if str(matched_type) != "default":
            return str(matched_format or "bc7_unorm")

    dds_format = resolve_dds_target(
        os.path.basename(str(source_path or "")), props, name_hints=name_hints
    )[1]
    return str(dds_format or "bc7_unorm")


def _read_dds_header(path: str):
    """读 DDS 文件前 148 字节；不存在 / 不是 DDS / 头太短都返回 None。"""
    try:
        with open(path, "rb") as handle:
            header = handle.read(_DDS_FULL_HEADER_SIZE)
    except OSError:
        return None
    if len(header) < 128 or header[:4] != b"DDS ":
        return None
    return header


def dds_header_dxgi_format(path: str):
    """DX10 头返回偏移 128 的 DXGI 格式号；legacy FOURCC 头返回 None。"""
    header = _read_dds_header(path)
    if header is None or len(header) < _DDS_FULL_HEADER_SIZE:
        return None
    if header[84:88] != b"DX10":
        return None
    return struct.unpack_from("<I", header, 128)[0]


def _dxgi_format_name(dxgi_id: int) -> str:
    """DXGI 格式号 -> texconv 格式名；表外返回空串。"""
    for name, value in _DDS_DXGI_FORMAT_IDS.items():
        if value == dxgi_id:
            return name
    return ""


def read_dds_header_format(path: str) -> str:
    """读 DDS 头得出 texconv 认可的格式名；读不到 / 格式不在表里返回空串。

    这是「输出格式必须与工作空间原贴图一致」的唯一权威来源：工作空间里那份 DDS
    就是游戏引擎实际加载的字节，它的头就是答案。两张表（DX10 / legacy FOURCC）
    覆盖工作空间里出现的格式；表外格式返回空串，交给规则表兜底。
    """
    header = _read_dds_header(path)
    if header is None:
        return ""
    fourcc = bytes(header[84:88])
    if fourcc != b"DX10":
        return _DDS_LEGACY_FOURCC_FORMATS.get(fourcc, "")
    if len(header) < _DDS_FULL_HEADER_SIZE:
        return ""
    name = _dxgi_format_name(struct.unpack_from("<I", header, 128)[0])
    if name.endswith("_typeless"):
        # typeless 只是「内存布局未声明语义」：物理布局与 UNORM 完全相同，按 UNORM 输出。
        name = name[: -len("_typeless")] + "_unorm"
    return name


def existing_dds_matches_format(path: str, dds_format: str):
    """判断磁盘上已有的 DDS 是否已经是 ``dds_format`` 指定的格式。

    只读 148 字节文件头，不 import ``common/dds_pixel.py``（那个模块面向完整
    data 解码、且有循环 import 风险）。

    三态返回：``True`` 一致、``False`` 明确不一致、``None`` 无法判断（表外格式）。
    文件不存在 / 头读不到一律 ``False``。「存在即跳过」会让格式变化对旧产物失效
    （规则被改、工作空间原贴图换了格式），所以复用前必须先验头。
    """
    expected = str(dds_format or "").strip().lower()
    if not expected or not os.path.isfile(path):
        return False

    header = _read_dds_header(path)
    if header is None:
        return False

    actual = read_dds_header_format(path)
    if not actual:
        # 表外格式读不出名字：判成不一致会让每次导出都重转一遍，所以答「不知道」。
        return None
    return actual == expected


# 进程内转换缓存：key = 规范化后的目标绝对路径，value = 转出它时的源文件指纹。
# 一次导出里，同一个目标 DDS 只会被真正转一次。一个材质被 N 个物体（或多个段落）
# 引用时，导出流程会为同一个目标反复走到这里 —— 没有这层缓存就是 N 次 texconv，
# 而单张 4096² 的 BC7 转换约 4 秒，重复转换正是导出耗时的主要来源。
# 生命周期：由 BlueprintExportHelper.clear_postprocess_caches() 在每次导出开始时
# 调 :func:`clear_dds_conversion_cache` 清空，所以「覆盖现有资源」每次导出仍会
# 重新生成一遍，只是不会在一次导出里重复生成。
_dds_conversion_cache: dict = {}


def clear_dds_conversion_cache():
    """清空导出期的 DDS 转换缓存（每次导出开始时调用一次）。"""
    _dds_conversion_cache.clear()


def _source_signature(source_path: str):
    """源文件指纹 ``(规范化路径, mtime_ns, size)``；源不存在时返回 ``None``。"""
    try:
        stat = os.stat(source_path)
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(source_path)), stat.st_mtime_ns, stat.st_size)


def _target_is_current(target_path: str, signature, expected_format: str = "") -> bool:
    """目标文件是否已经是这份源文件、这个格式的最新产物。

    只看 mtime（make 的时间戳规则）不足以复用：源图没变但期望格式变了（规则表被改，
    或工作空间原贴图换了格式）时，磁盘上那份旧格式的产物会被永久沿用。给了
    ``expected_format`` 就再验一次 DDS 头；头里格式无法识别（``None``）时按 mtime 放行。
    """
    try:
        target_stat = os.stat(target_path)
    except OSError:
        return False
    if target_stat.st_mtime_ns < signature[1]:
        return False
    if expected_format:
        return existing_dds_matches_format(target_path, expected_format) is not False
    return True


def _verbatim_copy_source(source_path: str, workspace_source_path: str, dds_format: str) -> str:
    """找出格式正好等于 ``dds_format`` 的原贴图：texconv 写不出该格式时原样复制它。"""
    expected = str(dds_format or "").strip().lower()
    if not expected:
        return ""
    for candidate in (workspace_source_path, source_path):
        candidate = str(candidate or "")
        if candidate and os.path.isfile(candidate):
            if str(read_dds_header_format(candidate)).strip().lower() == expected:
                return candidate
    return ""


def convert_texture_to_dds(
    source_path: str,
    target_path: str,
    props=None,
    texture_type: str = "",
    name_hints=(),
    format_override: str = "",
    workspace_source_path: str = "",
    match_name: str | None = None,
) -> tuple[bool, str]:
    """把单张贴图转成 DDS 写到 ``target_path``，供 Mod 导出流程复用。

    输出格式由 :func:`resolve_texture_dds_format` 决定：能读到工作空间原贴图就照抄它的
    格式，读不到才退回规则表。texconv 写不出该格式时**不换格式重编码**，而是把原贴图
    原样复制过去（返回 ``copied-verbatim:<格式>``）—— 换格式会让引擎按错误的
    通道数 / 色彩空间读贴图，比不转换更糟。

    **不改动源文件**，返回 ``(True, "")`` 或 ``(False, 失败原因)``。
    """
    texconv_executable = find_texconv()
    if not texconv_executable:
        return False, "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。"

    if props is None:
        props = bpy.context.scene.texture_tools_props

    hints = tuple(name_hints or ())

    dds_format = resolve_texture_dds_format(
        source_path,
        props,
        texture_type=texture_type,
        name_hints=hints,
        format_override=format_override,
        workspace_source_path=workspace_source_path,
        match_name=match_name,
    )
    colorspace_flags = _texconv_colorspace_flags(dds_format)

    command = [texconv_executable, "-f", dds_format]
    command.extend(colorspace_flags)

    # 先转到临时目录再搬过去：texconv 的输出名固定是「源文件名词干 + .dds」，而导出侧
    # 要的目标名通常取材质资源名，两者不同；直接 -o 目标目录会写出一个多余的错误文件名。
    # 临时目录必须和目标**同盘**：同卷 rename 是瞬时的，而放到系统临时目录（通常在 C 盘）
    # 再搬到 Mod 目录（常在别的盘）会退化成「复制 21MB + 删除」，纯粹白费时间。
    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    staging = tempfile.mkdtemp(prefix=".herta_dds_staging_", dir=target_dir or None)
    try:
        command.extend(["-o", staging, "-y", source_path])
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as exc:
            return False, f"调用 texconv 失败: {exc}"

        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            verbatim = _verbatim_copy_source(source_path, workspace_source_path, dds_format)
            if verbatim:
                print(
                    f"[DDS转换] texconv 写不出 {dds_format}，原样复制原贴图 "
                    f"{os.path.basename(verbatim)}（保持工作空间原格式）"
                )
                try:
                    shutil.copy2(verbatim, target_path)
                except Exception as exc:
                    return False, f"原样复制原贴图失败: {exc}"
                return True, f"copied-verbatim:{dds_format}"
            return False, detail or f"texconv 退出码 {process.returncode}"

        produced = os.path.join(
            staging, os.path.splitext(os.path.basename(source_path))[0] + ".dds"
        )
        if not os.path.exists(produced):
            return False, f"texconv 未生成 {produced}"

        try:
            shutil.move(produced, target_path)
        except Exception as exc:
            return False, f"写出目标文件失败: {exc}"
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return True, ""


def convert_texture_to_dds_cached(
    source_path: str,
    target_path: str,
    props=None,
    force: bool = False,
    match_name: str | None = None,
    texture_type: str = "",
    name_hints=(),
    format_override: str = "",
    workspace_source_path: str = "",
) -> tuple[bool, str]:
    """带缓存的 :func:`convert_texture_to_dds`：同一目标在一次导出内只真正转一次。

    缓存值是「源文件指纹 + 期望格式」—— 格式也算进去，这样规则变了或原贴图换了格式以后，
    磁盘上那份旧格式的产物不会被当成最新结果复用。

    ``force=True`` 对应节点上的「覆盖现有资源」：表示愿意重新生成，但仅限本次导出的
    第一次 —— 同一张贴图被 N 个物体引用时不会再各转一遍。缓存每次导出开始时清空，
    所以下次导出仍会重新生成。复用磁盘已有产物时同样返回 ``(True, "")``：调用方只
    关心「目标就绪」。

    ``match_name`` 沿用旧签名：材质资源名先查规则，命中就定格式（导出侧真正的命名依据）。
    """
    signature = _source_signature(source_path)
    if signature is None:
        return False, f"源文件不存在: {source_path}"

    hints = tuple(name_hints or ())

    expected_format = resolve_texture_dds_format(
        source_path,
        props,
        texture_type=texture_type,
        name_hints=hints,
        format_override=format_override,
        workspace_source_path=workspace_source_path,
        match_name=match_name,
    )

    cache_key = os.path.normcase(os.path.abspath(target_path))
    cache_value = (signature, expected_format)
    # 命中缓存也要确认产物还在：同一次导出里目标目录可能被清掉（缓存只记「这一轮转过了」，
    # 不负责保管文件），缺了这个判断就会对着不存在的目标返回成功。
    if _dds_conversion_cache.get(cache_key) == cache_value and os.path.exists(target_path):
        return True, ""

    if not force and _target_is_current(target_path, signature, expected_format):
        _dds_conversion_cache[cache_key] = cache_value
        return True, ""

    ok, reason = convert_texture_to_dds(
        source_path,
        target_path,
        props,
        texture_type=texture_type,
        name_hints=hints,
        format_override=format_override,
        workspace_source_path=workspace_source_path,
        match_name=match_name,
    )
    if ok:
        _dds_conversion_cache[cache_key] = cache_value
    return ok, reason


class TT_OT_convert_to_dds(bpy.types.Operator):
    bl_idname = "toolkit.tt_convert_to_dds"
    bl_label = "批量转换为 .dds"
    bl_description = "使用 texconv.exe 将输出目录中的贴图转换或重编码为目标 DDS 格式，并更新图片引用。按输出格式选择正确色彩空间标志（sRGB 输出用 --srgb-in、线性输出用 --ignore-srgb），保证转换前后数值/颜色不变"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({"ERROR"}, "请先设置输出目录")
            return {"CANCELLED"}

        output_dir_abs = os.path.normpath(bpy.path.abspath(props.output_dir))
        if not os.path.isdir(output_dir_abs):
            self.report({"ERROR"}, f"输出目录不存在: {output_dir_abs}")
            return {"CANCELLED"}

        rule_errors = _validate_custom_rules(props)
        if rule_errors:
            self.report({"ERROR"}, "；".join(rule_errors))
            return {"CANCELLED"}

        texconv_executable = find_texconv()
        if not texconv_executable:
            self.report({"ERROR"}, "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。")
            return {"CANCELLED"}

        supported_extensions = set(_SUPPORTED_SOURCE_EXTENSIONS)
        if props.dds_reencode_existing_dds:
            supported_extensions.add(".dds")

        conversion_map = {}
        converted_files_count = 0
        skipped_files_count = 0
        skipped_unmatched_dds_count = 0

        blend_dir = os.path.normpath(bpy.path.abspath("//"))

        for root, _dirs, files in os.walk(output_dir_abs):
            for filename in files:
                name_no_ext, ext = os.path.splitext(filename)
                ext_lower = ext.lower()
                if ext_lower not in supported_extensions:
                    continue

                old_path = os.path.normpath(os.path.join(root, filename))
                if not old_path.startswith(output_dir_abs):
                    self.report({"WARNING"}, f"跳过输出目录外的文件: {filename}")
                    continue

                if old_path.startswith(blend_dir) and not old_path.startswith(output_dir_abs):
                    self.report({"WARNING"}, f"跳过工程目录内的源文件: {filename}")
                    continue

                new_path = os.path.normpath(os.path.join(root, f"{name_no_ext}.dds"))
                texture_type, dds_format, _matched_by = resolve_dds_target(filename, props)

                if ext_lower == ".dds" and texture_type == "default":
                    skipped_unmatched_dds_count += 1
                    continue

                if not dds_format:
                    dds_format = "bc7_unorm"

                command = [texconv_executable, "-f", dds_format]
                command.extend(_texconv_colorspace_flags(dds_format))
                command.extend(["-o", root, "-y", old_path])

                try:
                    process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        check=True,
                        encoding="utf-8",
                        errors="ignore",
                    )
                except subprocess.CalledProcessError as exc:
                    stderr = (exc.stderr or exc.stdout or str(exc)).strip()
                    self.report({"WARNING"}, f"转换文件 {filename} 失败: {stderr}")
                    continue
                except Exception as exc:
                    self.report({"WARNING"}, f"处理文件 {filename} 时出错: {exc}")
                    continue

                if process.returncode != 0:
                    self.report({"WARNING"}, f"转换文件 {filename} 失败: {process.stderr}")
                    continue

                conversion_map[old_path] = (new_path, dds_format)
                converted_files_count += 1

                if props.dds_delete_originals and old_path != new_path:
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
                else:
                    skipped_files_count += 1 if old_path == new_path else 0

        if converted_files_count == 0:
            self.report({"INFO"}, "在输出目录中未找到可转换的贴图文件。")
            return {"CANCELLED"}

        updated_images_count = 0
        for image in bpy.data.images:
            if image.source != "FILE" or not image.filepath:
                continue
            try:
                abs_filepath = os.path.normpath(bpy.path.abspath(image.filepath_raw))
                if abs_filepath in conversion_map:
                    new_path, dds_format = conversion_map[abs_filepath]
                    # 重连只换文件路径与容器格式：先快照用户既有的显示配置，重载后原样写回
                    display_state = _snapshot_image_display_state(image)
                    try:
                        image.filepath = new_path
                        image.reload()
                    finally:
                        # reload 失败也要写回：路径已经换过了，配置同样不该被改写
                        _restore_image_display_state(image, display_state, dds_format)
                    updated_images_count += 1
            except Exception as exc:
                self.report({"WARNING"}, f"更新图片 '{image.name}' 的路径时出错: {exc}")

        self.report(
            {"INFO"},
            f"成功处理 {converted_files_count} 个贴图文件，更新了 {updated_images_count} 个图片引用。"
            + (" 其中部分 DDS 为原地重编码。" if skipped_files_count else ""),
        )
        if skipped_unmatched_dds_count:
            self.report(
                {"INFO"},
                f"跳过了 {skipped_unmatched_dds_count} 个未命中任何 DDS 规则的现有 DDS 文件。",
            )
        return {"FINISHED"}


class TT_OT_add_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_add_dds_rule"
    bl_label = "添加DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        rule = props.dds_rules.add()
        rule.pattern = ".*"
        rule.format = "bc7_unorm"
        rule.enabled = True
        props.dds_use_custom_rules = True
        return {"FINISHED"}


class TT_OT_remove_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_remove_dds_rule"
    bl_label = "移除DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.remove(self.index)
        return {"FINISHED"}


class TT_OT_reset_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_reset_dds_rules"
    bl_label = "重置DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.clear()
        props.dds_use_custom_rules = True

        for rule_data in DDS_DEFAULT_RULES:
            rule = props.dds_rules.add()
            rule.pattern = rule_data["pattern"]
            rule.format = rule_data["format"]
            rule.enabled = True

        return {"FINISHED"}


class TT_OT_test_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_test_dds_rule"
    bl_label = "测试DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        rule_errors = _validate_custom_rules(props)
        if rule_errors:
            self.report({"ERROR"}, "；".join(rule_errors))
            return {"CANCELLED"}

        test_names = [
            "DiffuseMap_Body.png",
            "Body-DiffuseMap.dds",
            "NormalMap_Face.dds",
            "LightMap_Hair.tga",
            "MaterialMap_Armor.png",
            "Body-ORMMap.png",
            "RampMap_Eye.png",
            "HighLightMap_Hair.png",
            "WengineFX_Leg.bmp",
            "Glowmap_1_Eye.png",
            "FXMap_Body.dds",
            "RoughnessMap_Body.png",
            "UnknownMask.png",
            "VivianGemADiffuse.png",
        ]

        result_lines = ["DDS规则测试结果:"]
        for name in test_names:
            texture_type, matched_format, matched_by = resolve_dds_target(name, props)
            result_lines.append(f"  {name} -> {matched_format} ({texture_type} / {matched_by})")

        self.report({"INFO"}, "\n".join(result_lines))
        return {"FINISHED"}


class TT_OT_save_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_save_dds_rules"
    bl_label = "保存DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.dds_rules_file_path:
            self.report({"ERROR"}, "请先指定规则文件路径")
            return {"CANCELLED"}

        rules_data = []
        for rule in props.dds_rules:
            rules_data.append(
                {
                    "pattern": rule.pattern,
                    "format": rule.format,
                    "enabled": rule.enabled,
                }
            )

        try:
            with open(props.dds_rules_file_path, "w", encoding="utf-8") as file_obj:
                json.dump(rules_data, file_obj, indent=2, ensure_ascii=False)
            self.report({"INFO"}, f"规则已保存到: {props.dds_rules_file_path}")
        except Exception as exc:
            self.report({"ERROR"}, f"保存失败: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


class TT_OT_load_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_load_dds_rules"
    bl_label = "加载DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.dds_rules_file_path:
            self.report({"ERROR"}, "请先指定规则文件路径")
            return {"CANCELLED"}

        if not os.path.exists(props.dds_rules_file_path):
            self.report({"ERROR"}, "规则文件不存在")
            return {"CANCELLED"}

        try:
            with open(props.dds_rules_file_path, "r", encoding="utf-8") as file_obj:
                rules_data = json.load(file_obj)

            props.dds_rules.clear()
            for rule_data in rules_data:
                rule = props.dds_rules.add()
                rule.pattern = rule_data.get("pattern", ".*")
                rule.format = rule_data.get("format", "bc7_unorm")
                rule.enabled = rule_data.get("enabled", True)
            props.dds_use_custom_rules = True

            self.report({"INFO"}, f"已加载 {len(rules_data)} 条规则")
        except Exception as exc:
            self.report({"ERROR"}, f"加载失败: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


tt_dds_conversion_list = (
    TT_OT_convert_to_dds,
    TT_OT_add_dds_rule,
    TT_OT_remove_dds_rule,
    TT_OT_reset_dds_rules,
    TT_OT_test_dds_rule,
    TT_OT_save_dds_rules,
    TT_OT_load_dds_rules,
)
