import bpy
import os
import glob
import hashlib
import re
import tempfile

from .node_postprocess_base import SSMTNode_PostProcess_Base

# 轻量测试宿主（fake bpy）只桩了 BoolProperty；真实 Blender 一定有 IntProperty。
# 生产环境下这就是 bpy.props.IntProperty，属性声明与行为不变。
_IntProperty = getattr(bpy.props, "IntProperty", bpy.props.BoolProperty)


class SSMTNode_PostProcess_ResourceMerge(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_ResourceMerge'
    bl_label = '资源合并'
    bl_description = '通过计算DDS贴图内容的MD5哈希值，自动合并内容相同的资源引用并删除重复的DDS贴图'

    use_pixel_dedup: bpy.props.BoolProperty(
        name="像素级去重",
        description="开启后除MD5外还会解码DDS像素内容进行比对：像素完全相同的贴图（即使文件格式/编码不同）也判定为重复并合并删除。BC6H等暂不支持的格式自动跳过",
        default=False,
    )  # type: ignore

    use_similarity_dedup: bpy.props.BoolProperty(
        name="相似度去重",
        description="开启后对像素级去重未合并的DDS按缩略特征比对相似度：达到阈值的贴图视为同一张图，合并引用并删除重复文件。适合有损压缩导致像素不完全一致的重复贴图；BC6H等暂不支持的格式自动跳过",
        default=False,
    )  # type: ignore

    similarity_threshold: _IntProperty(
        name="相似度阈值",
        description="两张贴图缩略特征相似度不低于该百分比时视为同一张图并合并（值越大越严格）",
        min=50,
        max=100,
        default=90,
        subtype='PERCENTAGE',
    )  # type: ignore

    clean_unreferenced_textures: bpy.props.BoolProperty(
        name="清理未引用贴图",
        description="勾选后，导出结束前删除 Textures 目录里没有被任何 ini Resource 段引用的 .dds 贴图（清理旧导出的残留孤儿文件）；只处理贴图目录里的 .dds，不动 .buf 和其它目录",
        default=False,
    )  # type: ignore

    def draw_buttons(self, context, layout):
        layout.label(text="计算DDS贴图文件MD5哈希值", icon='FILE_CACHE')
        layout.label(text="合并内容相同的DDS资源引用")
        layout.label(text="自动删除重复的DDS贴图文件")
        layout.separator()
        layout.prop(self, "use_pixel_dedup", icon='TEXTURE')
        if getattr(self, "use_pixel_dedup", False):
            layout.label(text="额外执行像素级去重：", icon='INFO')
            layout.label(text="解码DDS像素内容，像素完全相同（如相同内容")
            layout.label(text="不同格式/编码/压缩的贴图）也合并删除")
        layout.separator()
        layout.prop(self, "use_similarity_dedup", icon='DUPLICATE')
        if getattr(self, "use_similarity_dedup", False):
            layout.prop(self, "similarity_threshold", slider=True)
            layout.label(text="相似度 ≥ 阈值即视为同一张图并合并", icon='INFO')
        layout.separator()
        layout.prop(self, "clean_unreferenced_textures", icon='TRASH')
        if getattr(self, "clean_unreferenced_textures", False):
            layout.label(text="清理未被 ini 引用的孤儿 .dds", icon='INFO')
        layout.separator()
        layout.label(text="执行前会自动备份ini文件", icon='BACK')

    def compute_file_hash(self, file_path, block_size=65536):
        if not os.path.exists(file_path):
            return None
        hasher = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    if not data:
                        break
                    hasher.update(data)
            return hasher.hexdigest()
        except (OSError, IOError):
            return None

    @staticmethod
    def _is_resource_section(section_name: str) -> bool:
        normalized = str(section_name or "").strip()
        if not (normalized.startswith("[") and normalized.endswith("]")):
            return False
        normalized = normalized[1:-1]
        return normalized.lower().startswith("resource")

    @staticmethod
    def _is_dds_filename(filename: str) -> bool:
        return os.path.splitext(str(filename or "").strip())[1].lower() == ".dds"

    @staticmethod
    def _resolve_mod_local_path(mod_export_path: str, filename: str) -> str | None:
        """Resolve an INI filename only when its real path stays inside the mod root."""
        root = os.path.realpath(os.path.abspath(mod_export_path))
        relative = str(filename or "").replace("\\", os.sep).replace("/", os.sep)
        candidate = os.path.realpath(os.path.abspath(os.path.join(root, relative)))
        root_compare = os.path.normcase(root)
        candidate_compare = os.path.normcase(candidate)
        try:
            if os.path.commonpath((root_compare, candidate_compare)) != root_compare:
                return None
        except ValueError:
            # Different Windows drives have no common path.
            return None
        return candidate

    @classmethod
    def _resource_filename_entries(cls, content: str):
        """Return ordered resource filename records without rebuilding the INI.

        Duplicate section names and preamble text deliberately remain represented by
        their original line positions. Animation-driver and auto-appended blocks are
        protected from this postprocessor.
        """
        lines = content.splitlines(keepends=True)
        filename_pattern = re.compile(
            r"^(?P<prefix>[ \t]*filename[ \t]*=[ \t]*)"
            r"(?P<value>.*?)(?P<trailing>[ \t]*)(?P<newline>\r?\n)?$",
            re.IGNORECASE,
        )
        driver_start = getattr(
            cls, "ANIM_DRIVER_SECTION_MARKER_START", "; --- ANIMATION DRIVER SECTION ---"
        )
        driver_end = getattr(
            cls, "ANIM_DRIVER_SECTION_MARKER_END", "; --- END ANIMATION DRIVER SECTION ---"
        )
        tail_detector = getattr(cls, "is_known_auto_appended_marker", None)
        in_driver = False
        current_section = ""
        current_is_resource = False
        current_has_filename = False
        section_count = 0
        entries = []

        for line_index, line in enumerate(lines):
            if driver_start in line:
                in_driver = True
                current_section = ""
                current_is_resource = False
                continue
            if in_driver:
                if driver_end in line:
                    in_driver = False
                continue
            if callable(tail_detector) and tail_detector(line):
                break

            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_count += 1
                current_section = stripped
                current_is_resource = cls._is_resource_section(stripped)
                current_has_filename = False
                continue
            if not current_is_resource or current_has_filename:
                continue
            match = filename_pattern.match(line)
            if match is None:
                continue
            current_has_filename = True
            entries.append({
                "section": current_section,
                "line_index": line_index,
                "filename": match.group("value").strip(),
                "match": match,
            })

        return lines, entries, section_count

    @staticmethod
    def _atomic_write_text(path: str, content: str):
        folder = os.path.dirname(os.path.abspath(path))
        fd, temporary_path = tempfile.mkstemp(prefix=".resource-merge-", suffix=".tmp", dir=folder)
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

    def execute_postprocess(self, mod_export_path):
        print(f"[ResourceMerge] 开始执行，Mod导出路径: {mod_export_path}")
        print(f"[ResourceMerge] 路径是否存在: {os.path.exists(mod_export_path)}")

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        print(f"[ResourceMerge] 找到 {len(ini_files)} 个ini文件: {ini_files}")
        if not ini_files:
            print("[ResourceMerge] 在路径中未找到任何.ini文件，跳过")
            return

        # 先完成所有 ini 的引用合并，再统一清理孤儿贴图：多 ini 时任一 ini 引用即保留
        referenced_paths = set()
        for ini_file in ini_files:
            ini_references = self.process_ini_file(ini_file, mod_export_path)
            if ini_references:
                referenced_paths.update(ini_references)

        print("[ResourceMerge] 资源引用合并完成！")

        if getattr(self, "clean_unreferenced_textures", False):
            self._orphan_texture_cleanup_phase(mod_export_path, referenced_paths)

    def process_ini_file(self, ini_file, mod_export_path):
        print(f"[ResourceMerge] 正在处理ini文件: {ini_file}")
        self._create_cumulative_backup(ini_file, mod_export_path)

        with open(ini_file, 'r', encoding='utf-8', newline='') as f:
            content = f.read()

        lines, resource_entries, section_count = self._resource_filename_entries(content)
        print(
            f"[ResourceMerge] ini中共有 {section_count} 个section，"
            f"其中 {len(resource_entries)} 个Resource section含filename"
        )

        file_hash_to_first_ref = {}
        files_to_delete: dict[str, str] = {}
        pixel_primary_filename = {}

        for entry in resource_entries:
            section_name = entry["section"]
            filename = entry["filename"]
            if not self._is_dds_filename(filename):
                print(f"[ResourceMerge] 跳过 {section_name}: 非DDS贴图 {filename}")
                continue

            file_path = self._resolve_mod_local_path(mod_export_path, filename)
            if file_path is None:
                print(f"[ResourceMerge] 跳过 {section_name}: 路径越出Mod目录 {filename}")
                continue
            entry["file_path"] = file_path
            entry["_norm_path"] = os.path.normcase(os.path.realpath(file_path))
            if not os.path.isfile(file_path):
                print(f"[ResourceMerge] 跳过 {section_name}: 文件不存在 {file_path}")
                continue

            file_hash = self.compute_file_hash(file_path)
            if not file_hash:
                print(f"[ResourceMerge] 跳过 {section_name}: 无法计算哈希")
                continue

            print(f"[ResourceMerge] {section_name} -> {filename} (MD5: {file_hash[:16]}...)")

            if file_hash in file_hash_to_first_ref:
                primary_ref = file_hash_to_first_ref[file_hash]
                primary_path = os.path.normcase(os.path.realpath(primary_ref['file_path']))
                duplicate_path = os.path.normcase(os.path.realpath(file_path))
                if duplicate_path != primary_path:
                    files_to_delete[file_path] = filename
                    print(f"[ResourceMerge]   重复! 与 {primary_ref['section']} 相同")
                else:
                    print(f"[ResourceMerge]   共享文件引用，与 {primary_ref['section']} 指向同一路径，保留文件")
            else:
                file_hash_to_first_ref[file_hash] = {
                    'section': section_name,
                    'filename': filename,
                    'file_path': file_path,
                }
            entry["file_hash"] = file_hash

        if getattr(self, "use_pixel_dedup", False):
            self._pixel_dedup_phase(
                resource_entries,
                files_to_delete,
                pixel_primary_filename,
            )

        if getattr(self, "use_similarity_dedup", False):
            self._similarity_dedup_phase(
                resource_entries,
                files_to_delete,
                pixel_primary_filename,
            )

        print(f"[ResourceMerge] 扫描完成: {len(file_hash_to_first_ref)} 个唯一资源, {len(files_to_delete)} 个重复文件待删除")

        modified = False
        referenced_paths = set()
        for entry in resource_entries:
            original_filename = entry["filename"]
            primary_filename = self._resolve_primary_filename(
                entry, file_hash_to_first_ref, pixel_primary_filename
            )
            # 收集最终 ini 里仍被引用的路径（改写前后的名字都算，供孤儿贴图清理使用）
            for filename in (original_filename, primary_filename):
                resolved = self._resolve_mod_local_path(mod_export_path, filename)
                if resolved is not None:
                    referenced_paths.add(self._normalize_cleanup_path(resolved))
            if original_filename == primary_filename:
                continue
            match = entry["match"]
            lines[entry["line_index"]] = (
                match.group("prefix")
                + primary_filename
                + match.group("trailing")
                + (match.group("newline") or "")
            )
            modified = True
            print(f"[ResourceMerge] 引用替换: {original_filename} -> {primary_filename}")

        if modified:
            print(f"[ResourceMerge] ini文件已修改，正在写入...")
            self._atomic_write_text(ini_file, "".join(lines))
        else:
            print(f"[ResourceMerge] ini文件无需修改")

        for file_path, display_name in files_to_delete.items():
            try:
                os.remove(file_path)
                print(f"[ResourceMerge] 已删除重复文件: {display_name}")
            except OSError as e:
                print(f"[ResourceMerge] 删除文件失败 {file_path}: {e}")

        return referenced_paths

    @staticmethod
    def _resolve_primary_filename(entry, file_hash_to_first_ref, pixel_primary_filename):
        """返回条目应改写成的规范文件名：像素合并结果优先，其次 MD5 同内容组。"""
        norm_path = entry.get("_norm_path")
        if norm_path and norm_path in pixel_primary_filename:
            return pixel_primary_filename[norm_path]
        file_hash = entry.get("file_hash")
        if file_hash and file_hash in file_hash_to_first_ref:
            return file_hash_to_first_ref[file_hash]["filename"]
        return entry["filename"]

    def _pixel_dedup_phase(self, resource_entries, files_to_delete, pixel_primary_filename):
        """像素级去重：解码 DDS 像素，像素完全相同的文件即使格式不同也合并。"""
        try:
            from ..common import dds_pixel
            import numpy as np
        except Exception as exc:
            print(f"[ResourceMerge] 像素去重模块不可用（{exc}），本ini跳过像素合并")
            return

        # 候选：MD5 扫描成功、且未在 MD5 阶段被判定为重复的文件
        unique_entries = {}
        for entry in resource_entries:
            file_hash = entry.get("file_hash")
            norm_path = entry.get("_norm_path")
            if not file_hash or not norm_path:
                continue
            file_path = entry["file_path"]
            if file_path in files_to_delete:
                continue
            unique_entries.setdefault(norm_path, entry)

        if len(unique_entries) < 2:
            return

        # 按像素尺寸分组（尺寸不同不可能像素相同）
        groups = {}
        for norm_path, entry in unique_entries.items():
            try:
                with open(norm_path, 'rb') as f:
                    head = f.read(160)
            except OSError as exc:
                print(f"[ResourceMerge] 像素去重: 读取失败 {norm_path}: {exc}")
                continue
            dims = dds_pixel.dds_dimensions(head)
            if dims is None:
                print(f"[ResourceMerge] 像素去重: 跳过无法解析的 {os.path.basename(norm_path)}（{dds_pixel.last_error}）")
                continue
            groups.setdefault(dims, []).append((norm_path, entry))

        matched = 0
        skipped = 0
        for dims, members in groups.items():
            if len(members) < 2:
                continue
            representatives = []  # [(norm_path, pixels, entry)]
            for norm_path, entry in members:
                try:
                    with open(norm_path, 'rb') as f:
                        data = f.read()
                except OSError as exc:
                    print(f"[ResourceMerge] 像素去重: 读取失败 {norm_path}: {exc}")
                    continue
                pixels = dds_pixel.decode_dds_rgba8(data)
                if pixels is None:
                    skipped += 1
                    print(f"[ResourceMerge] 像素去重: 跳过无法解码的 {os.path.basename(norm_path)}（{dds_pixel.last_error}）")
                    continue
                duplicate_of = None
                for rep_path, rep_pixels, rep_entry in representatives:
                    if np.array_equal(rep_pixels, pixels):
                        duplicate_of = rep_entry
                        break
                if duplicate_of is not None:
                    matched += 1
                    pixel_primary_filename[norm_path] = duplicate_of["filename"]
                    files_to_delete[entry["file_path"]] = entry["filename"]
                    print(
                        f"[ResourceMerge]   像素重复! {os.path.basename(norm_path)} "
                        f"与 {os.path.basename(duplicate_of['file_path'])} 像素完全一致（格式不同），合并"
                    )
                else:
                    representatives.append((norm_path, pixels, entry))

        if matched or skipped:
            print(
                f"[ResourceMerge] 像素去重完成: 合并 {matched} 个, 无法解码跳过 {skipped} 个"
            )


    # ------------------------------------------------------------------
    # 相似度去重（像素级去重之后执行：MD5 / 像素级没有合并掉的剩余文件）
    # ------------------------------------------------------------------
    @staticmethod
    def _rewire_primary_filename(mapping, old_filename, new_filename):
        """代表文件自己被合并掉时，把此前指向它的映射改指到新的代表文件。"""
        if not old_filename or old_filename == new_filename:
            return
        for key, value in list(mapping.items()):
            if value == old_filename:
                mapping[key] = new_filename

    @staticmethod
    def _clamp_similarity_threshold(value):
        """阈值钳制到 50~100；非整数按默认 90。"""
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 90
        return max(50, min(100, number))

    def _similarity_threshold_value(self):
        return self._clamp_similarity_threshold(getattr(self, "similarity_threshold", 90))

    # ---- 相似度特征（色块 / 轮廓 / 色差 三维综合）-------------------------
    SIMILARITY_GRID = 64            # 缩略图边长（小图按实际尺寸）
    SIMILARITY_HIST_BINS = 16       # 每通道直方图 bin 数
    SIMILARITY_HEAVY_DIFF = 32      # 单像素任一通道差 > 32 视为"明显不同"
    NORMAL_MAP_EXTRA_STRICTNESS = 0.01  # 法线贴图额外严格 1 个百分点

    @staticmethod
    def _similarity_pool(pixels, grid):
        """RGBA8 像素图 -> grid×grid RGB 均值池化缩略图（float64, H×W×3）。

        按行/列等分切块，用 np.add.reduceat 求块内和再除以块面积，不做逐像素 Python
        循环，2048²/4K 图也在毫秒级。RGB 三通道分别池化：法线图的方向差异体现在
        RGB，先转灰度会把相反方向（如 158,98 vs 128,128）的差异抹平。
        """
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
        # 列块很宽时才升到 uint32（255*宽 ≤ 65535 用 uint16，省一半内存）；行和用 uint32 防溢出
        source_dtype = np.uint16 if int(np.max(np.diff(col_edges))) * 255 <= 65535 else np.uint32
        for channel in range(3):
            source = rgb[:, :, channel].astype(source_dtype)
            col_sums = np.add.reduceat(source, col_edges[:-1], axis=1).astype(np.uint32)
            block_sums = np.add.reduceat(col_sums, row_edges[:-1], axis=0)
            pooled[:, :, channel] = block_sums.astype(np.float64) / counts
        return pooled

    @staticmethod
    def _pooled_gray(thumb):
        """RGB 缩略图 -> 灰度缩略图（uint16，BT.601 整数权重 77/150/29）。"""
        import numpy as np

        rgb = np.rint(np.clip(thumb, 0.0, 255.0)).astype(np.uint16)
        return (
            rgb[:, :, 0] * np.uint16(77)
            + rgb[:, :, 1] * np.uint16(150)
            + rgb[:, :, 2] * np.uint16(29)
        ) >> 8

    @classmethod
    def _similarity_feature(cls, pixels, grid=32):
        """RGBA8 -> 约 grid×grid 灰度缩略特征（兼容旧接口）。

        先 RGB 均值池化再按 BT.601 整数权重转灰度；均匀图与旧的"先灰度再池化"
        结果完全一致，小图（< grid）按实际尺寸输出。
        """
        thumb = cls._similarity_pool(pixels, grid)
        if thumb is None:
            return None
        return cls._pooled_gray(thumb).astype("float64")

    @classmethod
    def _similarity_features(cls, pixels, grid=None):
        """RGBA8 -> 三维特征包 {thumb, hist, edges}；无法提取时返回 None。

        - thumb: RGB 均值池化缩略图（色差维度用）
        - hist:  每通道 16 bins 归一化直方图（色块/颜色分布维度用）
        - edges: 灰度缩略图的梯度幅值图，截断到 0~255（轮廓维度用）
        """
        import numpy as np

        if grid is None:
            grid = cls.SIMILARITY_GRID
        thumb = cls._similarity_pool(pixels, grid)
        if thumb is None:
            return None
        gray = cls._pooled_gray(thumb).astype(np.float64)

        # 色块：每通道归一化直方图（只在缩略图上统计，抗重采样噪声）
        bins = int(cls.SIMILARITY_HIST_BINS)
        hist = np.zeros((3, bins), dtype=np.float64)
        for channel in range(3):
            values = np.clip(thumb[:, :, channel], 0.0, 255.0)
            counts, _ = np.histogram(values, bins=bins, range=(0.0, 255.0))
            total = float(counts.sum())
            hist[channel] = counts / total if total > 0.0 else 0.0

        # 轮廓：中心差分梯度幅值（与色差同为 0~255 尺度）
        if gray.shape[0] >= 2 and gray.shape[1] >= 2:
            grad_y, grad_x = np.gradient(gray)
            edges = np.clip(np.hypot(grad_x, grad_y), 0.0, 255.0)
        else:
            edges = np.zeros_like(gray)

        return {"thumb": thumb, "hist": hist, "edges": edges}

    @classmethod
    def _similarity_scores(cls, feat_a, feat_b):
        """两张图的三维相似度 (色块, 轮廓, 色差)，各 0~1；形状不一致返回 None。"""
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

        # 色块：每通道直方图半 L1 距离（0=完全重叠，1=完全不重叠），三通道取均值
        overlap = 0.5 * np.abs(hist_a - hist_b).sum(axis=1)
        block_score = 1.0 - float(np.mean(overlap))

        # 轮廓：梯度幅值图平均绝对差 / 255
        contour_score = 1.0 - float(np.mean(np.abs(edges_a - edges_b))) / 255.0

        # 色差：RGB 三通道逐像素平均绝对差 / 255；再用"明显不同像素占比"兜底，
        # 防止大面积轻微差异被平均值摊薄（法线方向不同时单通道差往往远大于灰度差）。
        channel_diff = np.abs(thumb_a - thumb_b)
        pixel_diff = channel_diff.mean(axis=2)
        color_score = 1.0 - float(np.mean(pixel_diff)) / 255.0
        heavy_ratio = float(
            np.count_nonzero(channel_diff.max(axis=2) > cls.SIMILARITY_HEAVY_DIFF)
        ) / float(pixel_diff.size)
        color_score = min(color_score, 1.0 - heavy_ratio)

        return (
            max(0.0, min(1.0, block_score)),
            max(0.0, min(1.0, contour_score)),
            max(0.0, min(1.0, color_score)),
        )

    @staticmethod
    def _looks_like_normal_map(filename):
        """按文件名判断是否法线贴图（法线图对方向差异最敏感，阈值额外 +1 个百分点）。"""
        name = os.path.basename(str(filename or "")).lower()
        return "normal" in name or "法线" in name

    def _similarity_required_ratio(self, threshold, filename, other_filename):
        """本次比较实际使用的相似度门槛（0~1）：用户阈值 + 法线贴图补偿。"""
        ratio = threshold / 100.0
        if self._looks_like_normal_map(filename) or self._looks_like_normal_map(other_filename):
            ratio = min(1.0, ratio + self.NORMAL_MAP_EXTRA_STRICTNESS)
        return ratio

    @staticmethod
    def _report_similarity_progress(done, total):
        """只更新进度文本（不推动百分比）；同步导出 / 单测没有进度模块时静默。"""
        try:
            from . import export_progress

            export_progress.report_stage(f"资源合并：相似度去重 {done}/{total}")
        except Exception:
            pass

    def _similarity_dedup_phase(self, resource_entries, files_to_delete, pixel_primary_filename):
        """相似度去重：同尺寸 DDS 的三维相似度（色块/轮廓/色差）均 ≥ 阈值才合并。

        综合相似度取三项最小值：加权平均会让"色块/轮廓都还高"掩盖色差维度的巨大差异，
        而法线贴图内容不同时往往正是某个通道方向完全不同；取 min 最保守，任一维度不达标
        就不合并（符合"宁可少合并，也不误删贴图"的诉求）。
        法线贴图（文件名含 NormalMap）额外严格 1 个百分点，滑条百分比语义不变。
        """
        try:
            from ..common import dds_pixel
            import numpy as np
        except Exception as exc:
            print(f"[ResourceMerge] 相似度去重模块不可用（{exc}），本ini跳过相似度合并")
            return

        threshold = self._similarity_threshold_value()

        # 候选：MD5 扫描成功、且未被 MD5 / 像素级去重合并掉的文件
        candidates = {}
        for entry in resource_entries:
            file_hash = entry.get("file_hash")
            norm_path = entry.get("_norm_path")
            if not file_hash or not norm_path or norm_path in candidates:
                continue
            if entry["file_path"] in files_to_delete:
                continue
            candidates[norm_path] = entry

        total = len(candidates)
        if total < 2:
            return

        representatives = {}  # dims -> [(norm_path, features, entry)]
        matched = 0
        skipped = 0
        done = 0
        for norm_path, entry in candidates.items():
            try:
                with open(norm_path, 'rb') as f:
                    data = f.read()
            except OSError as exc:
                print(f"[ResourceMerge] 相似度去重: 读取失败 {norm_path}: {exc}")
                done += 1
                if done % 8 == 0 or done == total:
                    self._report_similarity_progress(done, total)
                continue

            dims = dds_pixel.dds_dimensions(data)
            if dims is None:
                skipped += 1
                print(f"[ResourceMerge] 相似度去重: 跳过无法解析的 {os.path.basename(norm_path)}（{dds_pixel.last_error}）")
            else:
                pixels = dds_pixel.decode_dds_rgba8(data)
                if pixels is None:
                    skipped += 1
                    print(f"[ResourceMerge] 相似度去重: 跳过无法解码的 {os.path.basename(norm_path)}（{dds_pixel.last_error}）")
                else:
                    features = self._similarity_features(pixels)
                    if features is None:
                        skipped += 1
                        print(f"[ResourceMerge] 相似度去重: 跳过无法生成特征的 {os.path.basename(norm_path)}")
                    else:
                        duplicate_of = None
                        similarity = 0.0
                        block_score = contour_score = color_score = 0.0
                        required = threshold / 100.0
                        for rep_path, rep_features, rep_entry in representatives.setdefault(dims, []):
                            scores = self._similarity_scores(rep_features, features)
                            if scores is None:
                                continue
                            rep_block, rep_contour, rep_color = scores
                            rep_similarity = min(rep_block, rep_contour, rep_color)
                            rep_required = self._similarity_required_ratio(
                                threshold, entry["filename"], rep_entry["filename"]
                            )
                            if rep_similarity >= rep_required:
                                duplicate_of = rep_entry
                                similarity = rep_similarity
                                block_score, contour_score, color_score = (
                                    rep_block, rep_contour, rep_color
                                )
                                required = rep_required
                                break
                        if duplicate_of is not None:
                            matched += 1
                            # 代表文件若曾在像素级阶段作为主文件，先把它名下的映射改指到新主文件
                            self._rewire_primary_filename(
                                pixel_primary_filename,
                                entry["filename"],
                                duplicate_of["filename"],
                            )
                            pixel_primary_filename[norm_path] = duplicate_of["filename"]
                            files_to_delete[entry["file_path"]] = entry["filename"]
                            print(
                                f"[ResourceMerge]   相似重复! {os.path.basename(norm_path)} "
                                f"与 {os.path.basename(duplicate_of['file_path'])} 相似度 "
                                f"{similarity * 100:.2f}%（色块 {block_score * 100:.2f}% / "
                                f"轮廓 {contour_score * 100:.2f}% / 色差 {color_score * 100:.2f}%，"
                                f"阈值 {required * 100:.2f}%），合并"
                            )
                        else:
                            representatives[dims].append((norm_path, features, entry))

            done += 1
            if done % 8 == 0 or done == total:
                self._report_similarity_progress(done, total)

        if matched or skipped:
            print(
                f"[ResourceMerge] 相似度去重完成（阈值 {threshold}%）：合并 {matched} 个, "
                f"无法解码跳过 {skipped} 个"
            )

    # ------------------------------------------------------------------
    # 清理未引用贴图（所有去重阶段之后执行：只清理 Textures 目录里的孤儿 .dds）
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_cleanup_path(path):
        """清理阶段的路径比较口径：Windows 不区分大小写，/ 与 \\ 统一。"""
        return os.path.normcase(os.path.normpath(os.path.abspath(str(path or ""))))

    @staticmethod
    def _report_cleanup_progress(done, total):
        """只更新进度文本（不推动百分比）；同步导出 / 单测没有进度模块时静默。"""
        try:
            from . import export_progress

            export_progress.report_stage(f"资源合并：清理未引用贴图 {done}/{total}")
        except Exception:
            pass

    def _orphan_texture_cleanup_phase(self, mod_export_path, referenced_paths):
        """删除 Textures 目录里没有被任何 ini Resource 段引用的 .dds 孤儿文件。

        只扫 Textures 这一层、只删常规 .dds 文件；删除前用 _resolve_mod_local_path
        重新确认目标仍在 mod 根内，失败静默跳过。
        """
        textures_dir = os.path.join(mod_export_path, "Textures")
        normalized_refs = {self._normalize_cleanup_path(path) for path in (referenced_paths or ())}
        try:
            dir_entries = list(os.scandir(textures_dir))
        except OSError:
            print("[ResourceMerge] 清理未引用贴图: 0 个")
            return

        orphan_candidates = []
        for dir_entry in dir_entries:
            if not self._is_dds_filename(dir_entry.name):
                continue
            try:
                if not dir_entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            # 解析结果同时确认文件仍在 mod 根内（Textures 是指向根外的符号链接时也跳过）
            resolved = self._resolve_mod_local_path(
                mod_export_path, os.path.join("Textures", dir_entry.name)
            )
            if resolved is None:
                continue
            if self._normalize_cleanup_path(resolved) in normalized_refs:
                continue
            orphan_candidates.append(resolved)

        total = len(orphan_candidates)
        if total == 0:
            print("[ResourceMerge] 清理未引用贴图: 0 个")
            return

        root = os.path.realpath(os.path.abspath(mod_export_path))
        cleaned = 0
        done = 0
        for file_path in orphan_candidates:
            try:
                relative = os.path.relpath(file_path, root)
            except ValueError:
                relative = os.path.basename(file_path)
            checked = self._resolve_mod_local_path(mod_export_path, relative)
            if checked is None or os.path.normcase(checked) != os.path.normcase(file_path):
                done += 1
                self._report_cleanup_progress(done, total)
                continue
            try:
                if os.path.islink(checked) or not os.path.isfile(checked):
                    done += 1
                    self._report_cleanup_progress(done, total)
                    continue
                os.remove(checked)
                cleaned += 1
                print(f"[ResourceMerge] 已删除未引用贴图: {os.path.basename(checked)}")
            except OSError:
                pass
            done += 1
            if done % 8 == 0 or done == total:
                self._report_cleanup_progress(done, total)

        print(f"[ResourceMerge] 清理未引用贴图: {cleaned} 个")


classes = (
    SSMTNode_PostProcess_ResourceMerge,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
