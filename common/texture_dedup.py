"""贴图内容指纹与去重算法（纯 Python，依赖可注入）。

资源合并节点（落盘后去重）与「贴图落盘计划器」（落盘前去重）共用同一套判定：

    MD5  →  像素级（解码后逐像素完全相同）  →  相似度（色块 / 轮廓 / 色差三维取 min）

本模块只负责「文件内容 → 指纹 / 特征」与「两张图是否重复」，不涉及 ini 语义、
不涉及落盘。资源合并节点保留同名薄包装方法，行为与迁移前一致。

调试开关：``DEBUG`` 为真时打印判定细节（前缀 ``[贴图去重]``），默认关闭。
"""

import glob
import hashlib
import io
import os
import re
import sqlite3
import tempfile
import threading
import time

DEBUG = False


def _debug(message):
    if DEBUG:
        print(f"[贴图去重] {message}")


# ---------------------------------------------------------------------------
# MD5
# ---------------------------------------------------------------------------

def compute_md5(file_path, block_size=65536):
    """返回文件 MD5（小写 hex）；不存在或读取失败返回 None。"""
    if not file_path or not os.path.exists(file_path):
        return None
    hasher = hashlib.md5()
    try:
        with open(file_path, "rb") as file_obj:
            while True:
                data = file_obj.read(block_size)
                if not data:
                    break
                hasher.update(data)
    except (OSError, IOError):
        return None
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# 像素读取（DDS 走内置解码，其它格式走 Blender 图像）
# ---------------------------------------------------------------------------

_DDS_MAGIC = b"DDS "


def _decode_with_blender(file_path):
    """用 Blender 图像 API 解码非 DDS 贴图，返回 ``((h, w, 4) uint8, (w, h))``。

    - colorspace 固定 ``Non-Color``：``pixels`` 即原始 0~1 值，不做 sRGB→线性，
      与 DDS 解码出来的原始字节口径一致。
    - Blender 的像素行序是自下而上（bottom-up），这里 ``flipud`` 对齐 DDS 解码的
      自上而下（top-down），否则跨格式比对会把同一张图判成不同。
    拿不到 bpy（fake 宿主 / 裁剪环境）或解码失败时返回 ``(None, None)``。
    """
    try:
        import bpy
        import numpy as np
    except Exception:
        return None, None

    image = None
    try:
        image = bpy.data.images.load(file_path, check_existing=False)
        try:
            image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0:
            return None, None
        buffer = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(buffer)
        rgba = np.rint(np.clip(buffer, 0.0, 1.0) * 255.0).astype(np.uint8)
        rgba = rgba.reshape(height, width, 4)
        return np.ascontiguousarray(np.flipud(rgba)), (width, height)
    except Exception as exc:
        _debug(f"Blender 解码失败 {file_path}: {exc}")
        return None, None
    finally:
        if image is not None:
            try:
                bpy.data.images.remove(image)
            except Exception:
                pass


def load_rgba8(file_path):
    """把贴图文件解码为 ``((h, w, 4) uint8, (w, h))``；失败返回 ``(None, None)``。

    DDS 用 ``common/dds_pixel`` 内置解码（快、无 Blender 依赖）；其它格式交给
    Blender 图像 API。两个入口都返回 top-down 行序。
    """
    if not file_path or not os.path.exists(file_path):
        return None, None

    try:
        with open(file_path, "rb") as file_obj:
            head = file_obj.read(4)
    except OSError:
        return None, None

    if head == _DDS_MAGIC:
        try:
            from . import dds_pixel
        except Exception:
            return None, None
        try:
            with open(file_path, "rb") as file_obj:
                data = file_obj.read()
        except OSError:
            return None, None
        dims = dds_pixel.dds_dimensions(data)
        if dims is None:
            return None, None
        pixels = dds_pixel.decode_dds_rgba8(data)
        if pixels is None:
            return None, None
        return pixels, dims

    if head == b'\x89PNG':
        try:
            from .png_native import analyze_png
            decoded = analyze_png(file_path)
            if decoded is not None:
                return decoded[0], decoded[1]
        except ImportError:
            pass
    return _decode_with_blender(file_path)


def pixel_key(pixels):
    """像素内容键（尺寸 + 像素字节摘要）；``pixels`` 为 None 时返回 None。"""
    if pixels is None:
        return None
    try:
        import numpy as np
    except Exception:
        return None
    contiguous = np.ascontiguousarray(pixels)
    digest = hashlib.sha1(contiguous.tobytes()).hexdigest()
    return (int(contiguous.shape[0]), int(contiguous.shape[1]), digest)


# ---------------------------------------------------------------------------
# 相似度特征（与资源合并节点迁移前的实现逐行等价）
# ---------------------------------------------------------------------------

SIMILARITY_GRID = 64
SIMILARITY_HIST_BINS = 16
SIMILARITY_HEAVY_DIFF = 32
NORMAL_MAP_EXTRA_STRICTNESS = 0.01
# Bump either version when decoded pixels or similarity feature semantics change.
FEATURE_CACHE_VERSION = 1
DDS_DECODE_VERSION = 1
FEATURE_CACHE_MAX_ENTRIES = 512
_FEATURE_CACHE_LOCK = threading.RLock()


def _similarity_pool(pixels, grid):
    """RGBA8 像素图 -> grid×grid RGB 均值池化缩略图（float64, H×W×3）。"""
    import numpy as np

    if pixels is None or getattr(pixels, "ndim", 0) != 3 or pixels.shape[2] < 3:
        return None
    height, width = int(pixels.shape[0]), int(pixels.shape[1])
    if height <= 0 or width <= 0:
        return None
    out_h = min(int(grid), height)
    out_w = min(int(grid), width)
    if out_h <= 0 or out_w <= 0:
        return None
    row_edges = (np.arange(out_h + 1, dtype=np.int64) * height // out_h).astype(np.intp)
    col_edges = (np.arange(out_w + 1, dtype=np.int64) * width // out_w).astype(np.intp)
    counts = np.outer(np.diff(row_edges), np.diff(col_edges)).astype(np.float64)

    rgb = pixels[:, :, :3]
    pooled = np.empty((out_h, out_w, 3), dtype=np.float64)
    source_dtype = np.uint16 if int(np.max(np.diff(col_edges))) * 255 <= 65535 else np.uint32
    for channel in range(3):
        source = rgb[:, :, channel].astype(source_dtype)
        col_sums = np.add.reduceat(source, col_edges[:-1], axis=1).astype(np.uint32)
        block_sums = np.add.reduceat(col_sums, row_edges[:-1], axis=0)
        pooled[:, :, channel] = block_sums.astype(np.float64) / counts
    return pooled


def _pooled_gray(thumb):
    """RGB 缩略图 -> 灰度缩略图（uint16，BT.601 整数权重 77/150/29）。"""
    import numpy as np

    rgb = np.rint(np.clip(thumb, 0.0, 255.0)).astype(np.uint16)
    return (
        rgb[:, :, 0] * np.uint16(77)
        + rgb[:, :, 1] * np.uint16(150)
        + rgb[:, :, 2] * np.uint16(29)
    ) >> 8


def similarity_feature(pixels, grid=32):
    """RGBA8 -> 约 grid×grid 灰度缩略特征（兼容旧接口）。"""
    thumb = _similarity_pool(pixels, grid)
    if thumb is None:
        return None
    return _pooled_gray(thumb).astype("float64")


def similarity_features(pixels, grid=None):
    """RGBA8 -> 三维特征包 ``{thumb, hist, edges}``；无法提取时返回 None。"""
    import numpy as np

    if grid is None:
        grid = SIMILARITY_GRID
    thumb = _similarity_pool(pixels, grid)
    if thumb is None:
        return None
    return similarity_features_from_thumbnail(thumb)


def similarity_features_from_thumbnail(thumb):
    """Shared feature calculation for full decoding and native fused pooling."""
    import numpy as np

    gray = _pooled_gray(thumb).astype(np.float64)

    bins = int(SIMILARITY_HIST_BINS)
    hist = np.zeros((3, bins), dtype=np.float64)
    for channel in range(3):
        values = np.clip(thumb[:, :, channel], 0.0, 255.0)
        counts, _ = np.histogram(values, bins=bins, range=(0.0, 255.0))
        total = float(counts.sum())
        hist[channel] = counts / total if total > 0.0 else 0.0

    if gray.shape[0] >= 2 and gray.shape[1] >= 2:
        grad_y, grad_x = np.gradient(gray)
        edges = np.clip(np.hypot(grad_x, grad_y), 0.0, 255.0)
    else:
        edges = np.zeros_like(gray)

    return {"thumb": thumb, "hist": hist, "edges": edges}


def dds_file_metadata(file_path, include_signature=False):
    """只读 DDS 头与可选的精确压缩块指纹。"""
    try:
        from . import dds_pixel
    except Exception:
        return None
    return dds_pixel.dds_file_metadata(file_path, include_signature=include_signature)


def _analysis_cache_path():
    """缓存放在用户本地缓存目录；缓存损坏或不可写时不影响导出。"""
    override = os.environ.get('HERTA_TEXTURE_ANALYSIS_CACHE_DIR')
    if override:
        return os.path.join(override, 'texture-analysis.sqlite3')
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(root, "TheHerta4", "cache", "texture-analysis.sqlite3")


def analysis_cache_files():
    path = _analysis_cache_path()
    return [path + suffix for suffix in ('', '-wal', '-shm', '-journal')]


def _analysis_cache_enabled():
    try:
        from .export_cache import is_export_cache_enabled
    except ImportError:
        return True
    return is_export_cache_enabled()


def _analysis_cache_key(file_md5):
    feature_spec = (
        FEATURE_CACHE_VERSION,
        DDS_DECODE_VERSION,
        SIMILARITY_GRID,
        SIMILARITY_HIST_BINS,
        SIMILARITY_HEAVY_DIFF,
    )
    return f"{feature_spec!r}:{str(file_md5 or '').lower()}"


def _open_analysis_cache():
    path = _analysis_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS texture_analysis ("
        "cache_key TEXT PRIMARY KEY, width INTEGER NOT NULL, height INTEGER NOT NULL, "
        "pixel_digest TEXT, features BLOB, accessed_at REAL NOT NULL)"
    )
    return connection


def _encode_features(features):
    if not features:
        return None
    try:
        import numpy as np

        buffer = io.BytesIO()
        np.savez_compressed(
            buffer,
            thumb=features["thumb"],
            hist=features["hist"],
            edges=features["edges"],
        )
        return buffer.getvalue()
    except Exception:
        return None


def _decode_features(blob):
    if not blob:
        return None
    try:
        import numpy as np

        with np.load(io.BytesIO(blob), allow_pickle=False) as data:
            return {
                "thumb": data["thumb"],
                "hist": data["hist"],
                "edges": data["edges"],
            }
    except Exception as exc:
        _debug(f"贴图特征缓存读取失败: {exc}")
        return None


def _get_cached_analysis(file_md5):
    if not file_md5 or not _analysis_cache_enabled():
        return None
    key = _analysis_cache_key(file_md5)
    try:
        with _FEATURE_CACHE_LOCK:
            connection = _open_analysis_cache()
            try:
                row = connection.execute(
                    "SELECT width, height, pixel_digest, features "
                    "FROM texture_analysis WHERE cache_key = ?",
                    (key,),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        "UPDATE texture_analysis SET accessed_at = ? WHERE cache_key = ?",
                        (time.time(), key),
                    )
                    connection.commit()
            finally:
                connection.close()
        if row is None:
            return None
        width, height, pixel_digest, features_blob = row
        return {
            "dims": (int(width), int(height)),
            "pixel_key": (
                (int(height), int(width), str(pixel_digest))
                if pixel_digest
                else None
            ),
            "features": _decode_features(features_blob),
        }
    except Exception as exc:
        _debug(f"贴图特征缓存不可用: {exc}")
        return None


def _store_cached_analysis(file_md5, dims, pixel_key=None, features=None):
    if not file_md5 or not dims or len(dims) != 2 or not _analysis_cache_enabled():
        return False
    width, height = int(dims[0]), int(dims[1])
    pixel_digest = str(pixel_key[2]) if pixel_key and len(pixel_key) >= 3 else None
    features_blob = _encode_features(features)
    key = _analysis_cache_key(file_md5)
    try:
        with _FEATURE_CACHE_LOCK:
            connection = _open_analysis_cache()
            try:
                connection.execute(
                    "INSERT INTO texture_analysis "
                    "(cache_key, width, height, pixel_digest, features, accessed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET "
                    "width=excluded.width, height=excluded.height, "
                    "pixel_digest=COALESCE(excluded.pixel_digest, texture_analysis.pixel_digest), "
                    "features=COALESCE(excluded.features, texture_analysis.features), "
                    "accessed_at=excluded.accessed_at",
                    (key, width, height, pixel_digest, features_blob, time.time()),
                )
                connection.execute(
                    "DELETE FROM texture_analysis WHERE cache_key IN ("
                    "SELECT cache_key FROM texture_analysis "
                    "ORDER BY accessed_at DESC LIMIT -1 OFFSET ?)",
                    (FEATURE_CACHE_MAX_ENTRIES,),
                )
                connection.commit()
            finally:
                connection.close()
        return True
    except Exception as exc:
        _debug(f"贴图特征缓存写入失败: {exc}")
        return False


def analyze_file(file_path, file_md5=None, need_pixel_key=False,
                 need_features=False, allow_blender=True):
    """解码并生成可缓存的分析结果；命中内容哈希缓存时跳过像素解码。

    返回 dims=(width,height)、pixel_key、features，以及本次是否实际解码/提取特征。
    DDS 调用不依赖 bpy，可安全放入有限并行任务；非 DDS 的 Blender 解码必须在主线程。
    """
    digest = file_md5 or compute_md5(file_path)
    cached = _get_cached_analysis(digest)
    dims = cached.get("dims") if cached else None
    pixel_key = cached.get("pixel_key") if cached else None
    features = cached.get("features") if cached else None
    pixel_complete = not need_pixel_key or pixel_key is not None
    feature_complete = not need_features or features is not None
    if dims and pixel_complete and feature_complete:
        return {
            "md5": digest,
            "dims": dims,
            "pixel_key": pixel_key if need_pixel_key else None,
            "features": features if need_features else None,
            "decoded": False,
            "decode_seconds": 0.0,
            "feature_seconds": 0.0,
            "cache_hit": True,
        }

    try:
        with open(file_path, 'rb') as stream:
            header = stream.read(4)
    except OSError:
        header = b''
    if need_features and features is None and not need_pixel_key and header == b'\x89PNG':
        try:
            from .png_native import analyze_png
            native = analyze_png(file_path, SIMILARITY_GRID)
            if native is not None:
                thumb, dims, decode_seconds, pool_seconds = native
                feature_started = time.perf_counter()
                features = similarity_features_from_thumbnail(thumb)
                feature_seconds = pool_seconds + time.perf_counter() - feature_started
                _store_cached_analysis(digest, dims, pixel_key, features)
                return {'md5': digest, 'dims': dims, 'pixel_key': None, 'features': features,
                        'decoded': True, 'decode_seconds': decode_seconds,
                        'feature_seconds': feature_seconds, 'cache_hit': False}
        except ImportError:
            pass

    if not allow_blender and header != _DDS_MAGIC:
        return {
            "md5": digest, "dims": dims, "pixel_key": pixel_key,
            "features": features, "decoded": False,
            "decode_seconds": 0.0, "feature_seconds": 0.0, "cache_hit": False,
        }

    pixels = None
    decode_seconds = 0.0
    # Similarity needs only the exact pooled RGB image. The C path accumulates it
    # while decoding each block, avoiding the large intermediate RGBA allocation.
    if need_features and features is None and not need_pixel_key and header == _DDS_MAGIC:
        started = time.perf_counter()
        try:
            from . import dds_pixel
            thumb = None
            if dds_pixel._get_native_library() is not None:
                metadata = dds_pixel.dds_file_metadata(file_path)
                if metadata and metadata.get("kind") == "bc7":
                    with open(file_path, "rb") as source:
                        data = source.read()
                    thumb = dds_pixel.decode_dds_thumbnail(data, SIMILARITY_GRID)
            if thumb is not None:
                dims = dds_pixel.dds_dimensions(data)
                decode_seconds = time.perf_counter() - started
                feature_started = time.perf_counter()
                features = similarity_features_from_thumbnail(thumb)
                feature_seconds = time.perf_counter() - feature_started
                _store_cached_analysis(digest, dims, pixel_key, features)
                return {
                    "md5": digest, "dims": dims, "pixel_key": None, "features": features,
                    "decoded": True, "decode_seconds": decode_seconds,
                    "feature_seconds": feature_seconds, "cache_hit": False,
                }
        except (ImportError, OSError, ValueError):
            pass
    if not dims or (need_pixel_key and pixel_key is None) or (need_features and features is None):
        decode_started = time.perf_counter()
        pixels, decoded_dims = load_rgba8(file_path)
        decode_seconds = time.perf_counter() - decode_started
        if decoded_dims:
            dims = decoded_dims
    decoded = pixels is not None
    feature_seconds = 0.0
    if pixels is not None:
        if need_pixel_key and pixel_key is None:
            pixel_key = globals()["pixel_key"](pixels)
        if need_features and features is None:
            started = time.perf_counter()
            features = similarity_features(pixels)
            feature_seconds = time.perf_counter() - started

    if dims and (pixel_key is not None or features is not None):
        _store_cached_analysis(digest, dims, pixel_key, features)
    return {
        "md5": digest,
        "dims": dims,
        "pixel_key": pixel_key if need_pixel_key else None,
        "features": features if need_features else None,
        "decoded": decoded,
        "decode_seconds": decode_seconds,
        "feature_seconds": feature_seconds,
        "cache_hit": False,
    }


def similarity_scores(feat_a, feat_b):
    """两张图的三维相似度 ``(色块, 轮廓, 色差)``，各 0~1；形状不一致返回 None。"""
    import numpy as np

    thumb_a, thumb_b = feat_a.get("thumb"), feat_b.get("thumb")
    hist_a, hist_b = feat_a.get("hist"), feat_b.get("hist")
    edges_a, edges_b = feat_a.get("edges"), feat_b.get("edges")
    if thumb_a is None or thumb_b is None or thumb_a.shape != thumb_b.shape:
        return None
    if hist_a is None or hist_b is None or hist_a.shape != hist_b.shape:
        return None
    if edges_a is None or edges_b is None or edges_a.shape != edges_b.shape:
        return None

    overlap = 0.5 * np.abs(hist_a - hist_b).sum(axis=1)
    block_score = 1.0 - float(np.mean(overlap))

    contour_score = 1.0 - float(np.mean(np.abs(edges_a - edges_b))) / 255.0

    channel_diff = np.abs(thumb_a - thumb_b)
    pixel_diff = channel_diff.mean(axis=2)
    color_score = 1.0 - float(np.mean(pixel_diff)) / 255.0
    heavy_ratio = float(
        np.count_nonzero(channel_diff.max(axis=2) > SIMILARITY_HEAVY_DIFF)
    ) / float(pixel_diff.size)
    color_score = min(color_score, 1.0 - heavy_ratio)

    return (
        max(0.0, min(1.0, block_score)),
        max(0.0, min(1.0, contour_score)),
        max(0.0, min(1.0, color_score)),
    )


def looks_like_normal_map(filename):
    """按文件名判断是否法线贴图（法线图对方向差异最敏感，阈值额外 +1 个百分点）。"""
    name = os.path.basename(str(filename or "")).lower()
    return "normal" in name or "法线" in name


def required_ratio(threshold, filename, other_filename):
    """本次比较实际使用的相似度门槛（0~1）：用户阈值 + 法线贴图补偿。"""
    try:
        ratio = max(50, min(100, int(threshold))) / 100.0
    except (TypeError, ValueError):
        ratio = 0.90
    if looks_like_normal_map(filename) or looks_like_normal_map(other_filename):
        ratio = min(1.0, ratio + NORMAL_MAP_EXTRA_STRICTNESS)
    return ratio


def similarity_duplicate(scores, required):
    """三维分数取 min 后是否达到门槛（任一维度不达标就不算重复）。"""
    if not scores:
        return False, 0.0
    combined = min(scores)
    return combined >= required, combined


# ---------------------------------------------------------------------------
# ini 的 filename 行重写（转换失败回退 / 代表改名时使用）
# ---------------------------------------------------------------------------

_FILENAME_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t]*filename[ \t]*=[ \t]*)"
    r"(?P<value>.*?)(?P<trailing>[ \t]*)(?P<newline>\r?\n)?$",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
_ANIM_DRIVER_START = "; --- ANIMATION DRIVER SECTION ---"
_ANIM_DRIVER_END = "; --- END ANIMATION DRIVER SECTION ---"
_AUTO_APPENDED_MARKERS = (
    "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
    "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
    "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---",
)
_AUTO_APPENDED_PREFIXES = (
    "; --- AUTO-APPENDED UI PANEL ",
    "; --- AUTO-APPENDED CUSTOM TEXT ",
    "; --- AUTO-APPENDED SKIP ",
)


def normalize_relative_filename(value):
    """ini 里的相对文件名归一化：斜杠统一、小写、去首尾空白（用于比较）。"""
    return str(value or "").strip().replace("\\", "/").casefold()


def _is_auto_appended_marker(line):
    stripped = str(line or "").strip()
    if stripped in _AUTO_APPENDED_MARKERS:
        return True
    return any(stripped.startswith(prefix) for prefix in _AUTO_APPENDED_PREFIXES)


def _is_resource_section(section_name):
    normalized = str(section_name or "").strip()
    if not (normalized.startswith("[") and normalized.endswith("]")):
        return False
    return normalized[1:-1].lower().startswith("resource")


def _atomic_write_text(path, content):
    folder = os.path.dirname(os.path.abspath(path))
    fd, temporary_path = tempfile.mkstemp(prefix=".texture-plan-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write(content)
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise


def rewrite_ini_filenames(mod_export_path, mapping):
    """把 mod 目录下所有 ini 的 ``[Resource*]`` 段 ``filename`` 行按 ``mapping`` 重写。

    ``mapping`` 的键是旧相对文件名（大小写/斜杠不敏感），值是新的相对文件名。
    动画驱动段与自动追加尾段整块跳过（与资源合并同一口径）。
    返回实际替换的行数；任何单个 ini 读取/写入失败只记调试日志，不抛异常。
    """
    if not mapping:
        return 0
    normalized_mapping = {
        normalize_relative_filename(old): new for old, new in mapping.items()
    }
    if not normalized_mapping:
        return 0

    try:
        import glob
    except Exception:
        return 0

    replaced_total = 0
    for ini_file in glob.glob(os.path.join(mod_export_path, "*.ini")):
        try:
            with open(ini_file, "r", encoding="utf-8", newline="") as file_obj:
                content = file_obj.read()
        except OSError as exc:
            _debug(f"读取 ini 失败 {ini_file}: {exc}")
            continue

        lines = content.splitlines(keepends=True)
        in_driver = False
        in_resource = False
        replaced = 0
        for index, line in enumerate(lines):
            if _ANIM_DRIVER_START in line:
                in_driver = True
                in_resource = False
                continue
            if in_driver:
                if _ANIM_DRIVER_END in line:
                    in_driver = False
                continue
            if _is_auto_appended_marker(line):
                break

            section_match = _SECTION_RE.match(line)
            if section_match:
                in_resource = _is_resource_section(line.strip())
                continue
            if not in_resource:
                continue

            match = _FILENAME_LINE_RE.match(line)
            if match is None:
                continue
            old_value = match.group("value").strip()
            new_value = normalized_mapping.get(normalize_relative_filename(old_value))
            if not new_value or new_value == old_value:
                continue
            lines[index] = (
                match.group("prefix")
                + new_value
                + match.group("trailing")
                + (match.group("newline") or "")
            )
            replaced += 1

        if not replaced:
            continue
        try:
            _atomic_write_text(ini_file, "".join(lines))
        except Exception as exc:
            _debug(f"写入 ini 失败 {ini_file}: {exc}")
            continue
        replaced_total += replaced
        _debug(f"{os.path.basename(ini_file)}: 重写 filename {replaced} 行")

    return replaced_total
