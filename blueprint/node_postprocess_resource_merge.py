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

        for ini_file in ini_files:
            self.process_ini_file(ini_file, mod_export_path)

        print("[ResourceMerge] 资源引用合并完成！")

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
        for entry in resource_entries:
            original_filename = entry["filename"]
            primary_filename = self._resolve_primary_filename(
                entry, file_hash_to_first_ref, pixel_primary_filename
            )
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

    @staticmethod
    def _similarity_feature(pixels, grid=32):
        """RGBA8 像素图 -> 约 grid×grid 灰度块均值特征（numpy，4K 图也快）。

        缩略方式为均值池化：按行/列等分切块，用 np.add.reduceat 求块内和再除以块面积，
        不需要 Pillow、不做逐像素 Python 循环。整图灰度用整数权重 77/150/29（≈BT.601）。
        """
        import numpy as np

        if pixels is None or getattr(pixels, "ndim", 0) != 3 or pixels.shape[2] < 3:
            return None
        height, width = int(pixels.shape[0]), int(pixels.shape[1])
        if height <= 0 or width <= 0:
            return None
        # uint16 足够：255*(77+150+29)=65280 < 65535，无溢出
        rgb = pixels[:, :, :3].astype(np.uint16)
        gray = (
            rgb[:, :, 0] * np.uint16(77)
            + rgb[:, :, 1] * np.uint16(150)
            + rgb[:, :, 2] * np.uint16(29)
        ) >> 8

        out_h = min(int(grid), height)
        out_w = min(int(grid), width)
        row_edges = (np.arange(out_h + 1, dtype=np.int64) * height // out_h).astype(np.intp)
        col_edges = (np.arange(out_w + 1, dtype=np.int64) * width // out_w).astype(np.intp)

        # 先按列池化（宽块高度很小，必要时升到 uint32 防溢出），再按行池化
        max_col_block = int(np.max(np.diff(col_edges)))
        source = gray if max_col_block * 255 <= 65535 else gray.astype(np.uint32)
        col_sums = np.add.reduceat(source, col_edges[:-1], axis=1).astype(np.uint32)
        block_sums = np.add.reduceat(col_sums, row_edges[:-1], axis=0)
        counts = np.outer(np.diff(row_edges), np.diff(col_edges)).astype(np.float64)
        return block_sums.astype(np.float64) / counts

    @staticmethod
    def _report_similarity_progress(done, total):
        """只更新进度文本（不推动百分比）；同步导出 / 单测没有进度模块时静默。"""
        try:
            from . import export_progress

            export_progress.report_stage(f"资源合并：相似度去重 {done}/{total}")
        except Exception:
            pass

    def _similarity_dedup_phase(self, resource_entries, files_to_delete, pixel_primary_filename):
        """相似度去重：同尺寸 DDS 的 32×32 灰度特征相似度 ≥ 阈值即视为同一张图。"""
        try:
            from ..common import dds_pixel
            import numpy as np
        except Exception as exc:
            print(f"[ResourceMerge] 相似度去重模块不可用（{exc}），本ini跳过相似度合并")
            return

        threshold = self._similarity_threshold_value()
        ratio = threshold / 100.0

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

        representatives = {}  # dims -> [(norm_path, feature, entry)]
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
                    feature = self._similarity_feature(pixels)
                    if feature is None:
                        skipped += 1
                        print(f"[ResourceMerge] 相似度去重: 跳过无法生成特征的 {os.path.basename(norm_path)}")
                    else:
                        duplicate_of = None
                        similarity = 0.0
                        for rep_path, rep_feature, rep_entry in representatives.setdefault(dims, []):
                            if rep_feature.shape != feature.shape:
                                continue
                            similarity = 1.0 - float(np.mean(np.abs(rep_feature - feature))) / 255.0
                            if similarity >= ratio:
                                duplicate_of = rep_entry
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
                                f"{similarity * 100:.2f}%（阈值 {threshold}%），合并"
                            )
                        else:
                            representatives[dims].append((norm_path, feature, entry))

            done += 1
            if done % 8 == 0 or done == total:
                self._report_similarity_progress(done, total)

        if matched or skipped:
            print(
                f"[ResourceMerge] 相似度去重完成（阈值 {threshold}%）: 合并 {matched} 个, "
                f"无法解码跳过 {skipped} 个"
            )


classes = (
    SSMTNode_PostProcess_ResourceMerge,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
