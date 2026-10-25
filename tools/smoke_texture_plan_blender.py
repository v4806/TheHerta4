# -*- coding: utf-8 -*-
"""贴图计划器 + 贴图解码的 Blender headless 冒烟（不启动 GUI、不占用户实例）。

验证真实 Blender 环境下：
1. 新增模块能被插件包正常导入（相对导入链完整）；
2. DDS 解码（common/dds_pixel）与 Blender 图像解码（PNG）行序 / 色彩空间一致；
3. TextureExportPlan 的登记 → 去重 → 统一落盘在真机环境跑通。

运行：
  blender.exe --background --factory-startup --addons TheHerta4-main --python <本脚本>
"""

import importlib
import json
import os
import struct
import sys
import tempfile
import traceback

import bpy

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), str(detail)))
    print(f"[SMOKE] {'PASS' if condition else 'FAIL'} {name} {detail}")


def make_dds(width, height, rows_bgra, trailing=b""):
    """构造未压缩 BGRA32 DDS；rows_bgra 是按 top-down 行排列的 bytes 列表。"""
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, 0x1 | 0x2 | 0x4 | 0x1000)  # caps/height/width/pitch
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 20, width * 4)
    struct.pack_into("<I", header, 24, 1)
    struct.pack_into("<I", header, 76, 32)
    struct.pack_into("<I", header, 80, 0x41)  # DDPF_RGB | DDPF_ALPHAPIXELS
    struct.pack_into("<I", header, 88, 32)
    struct.pack_into("<I", header, 92, 0x00FF0000)  # R
    struct.pack_into("<I", header, 96, 0x0000FF00)  # G
    struct.pack_into("<I", header, 100, 0x000000FF)  # B
    struct.pack_into("<I", header, 104, 0xFF000000)  # A
    return bytes(header) + b"".join(rows_bgra) + trailing


def write_png(path, top_rows_rgba):
    """用 Blender 写 PNG：top_rows_rgba 是 top-down 行列表（每行 bytes RGBA）。"""
    height = len(top_rows_rgba)
    width = len(top_rows_rgba[0]) // 4
    image = bpy.data.images.new("smoke_png", width=width, height=height, alpha=True)
    try:
        # Blender 的 pixels 是 bottom-up、float 0~1：把 top-down 行反过来逐行填入
        values = []
        for row in reversed(top_rows_rgba):
            for index in range(0, len(row), 4):
                values.extend([row[index] / 255.0, row[index + 1] / 255.0,
                               row[index + 2] / 255.0, row[index + 3] / 255.0])
        image.pixels.foreach_set(values)
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def main():
    dedup = importlib.import_module("TheHerta4-main.common.texture_dedup")
    plan_mod = importlib.import_module("TheHerta4-main.common.texture_export_plan")
    TextureExportPlan = plan_mod.TextureExportPlan

    tmp = tempfile.mkdtemp(prefix="plan_smoke_")
    textures = os.path.join(tmp, "Textures")
    os.makedirs(textures, exist_ok=True)
    source_dir = os.path.join(tmp, "source")
    os.makedirs(source_dir, exist_ok=True)

    # 8x8：上半红、下半蓝（用于验证 DDS 与 PNG 解码的行序对齐）
    red_row = bytes([0, 0, 255, 255]) * 8      # BGRA 红
    blue_row = bytes([255, 0, 0, 255]) * 8     # BGRA 蓝
    dds_path = os.path.join(source_dir, "checker.dds")
    with open(dds_path, "wb") as handle:
        handle.write(make_dds(8, 8, [red_row] * 4 + [blue_row] * 4))

    dds_pixels, dds_dims = dedup.load_rgba8(dds_path)
    check("DDS 解码", dds_pixels is not None and tuple(dds_dims) == (8, 8), str(dds_dims))

    png_path = os.path.join(source_dir, "checker.png")
    png_rows = [bytes([255, 0, 0, 255]) * 8] * 4 + [bytes([0, 0, 255, 255]) * 8] * 4
    write_png(png_path, png_rows)
    png_pixels, png_dims = dedup.load_rgba8(png_path)
    check("PNG 解码（Blender 图像 API）", png_pixels is not None and tuple(png_dims) == (8, 8), str(png_dims))

    same = False
    if dds_pixels is not None and png_pixels is not None:
        try:
            import numpy as np
            same = bool(np.array_equal(dds_pixels, np.asarray(png_pixels, dtype=dds_pixels.dtype)))
        except Exception as exc:
            print(f"[SMOKE] 像素比对异常: {exc}")
    check("DDS/PNG 像素逐位一致（行序与色彩空间对齐）", same)

    # 计划器完整链路：同内容不同字节（尾部填充）→ 像素级去重 → 只落盘一份
    payload_rows = [red_row] * 4 + [blue_row] * 4
    first_source = os.path.join(source_dir, "body_a.dds")
    second_source = os.path.join(source_dir, "body_b.dds")
    with open(first_source, "wb") as handle:
        handle.write(make_dds(8, 8, payload_rows))
    with open(second_source, "wb") as handle:
        handle.write(make_dds(8, 8, payload_rows, trailing=b"\x00padding"))

    TextureExportPlan.reset()
    TextureExportPlan.configure(
        True, pixel_dedup=True, mod_export_path=tmp
    )
    first_name = TextureExportPlan.register(
        first_source, textures, "aaa.dds", owner="material"
    )
    second_name = TextureExportPlan.register(
        second_source, textures, "bbb.dds", owner="material"
    )
    check("同内容复用（不同文件名同像素）", first_name == second_name, f"{first_name} / {second_name}")

    TextureExportPlan.finalize(tmp)
    files = sorted(os.listdir(textures))
    check("只落盘一份", files == [first_name], str(files))
    check("落盘文件存在", os.path.exists(os.path.join(textures, first_name or "")), first_name or "")

    stats = TextureExportPlan.stats()
    check("统计：像素复用", stats.get("reuse_pixel") == 1, str(stats))

    # 模块接线：m_ini_helper 能拿到计划器；资源合并节点新增开关默认开启
    m_ini = importlib.import_module("TheHerta4-main.common.m_ini_helper")
    check("m_ini_helper 已接入计划器", m_ini.TextureExportPlan is not None)

    tree = bpy.data.node_groups.new("SmokePlanTree", "SSMTBlueprintTreeType")
    try:
        merge_node = tree.nodes.new("SSMTNode_PostProcess_ResourceMerge")
        check(
            "资源合并「落盘前联合去重」默认开启",
            bool(getattr(merge_node, "pre_plan_dedup", False)),
        )
        merge_node.use_pixel_dedup = True
        merge_node.similarity_threshold = 97
        TextureExportPlan.configure(
            bool(merge_node.pre_plan_dedup),
            pixel_dedup=bool(merge_node.use_pixel_dedup),
            similarity_dedup=bool(merge_node.use_similarity_dedup),
            threshold=merge_node.similarity_threshold,
            mod_export_path=tmp,
        )
        check(
            "节点开关与去重选项能配置计划器",
            TextureExportPlan.enabled()
            and TextureExportPlan._pixel_dedup
            and TextureExportPlan._threshold == 97,
        )
        merge_node.pre_plan_dedup = False
        TextureExportPlan.configure(
            bool(merge_node.pre_plan_dedup), mod_export_path=tmp
        )
        check("关闭开关后计划器禁用", TextureExportPlan.enabled() is False)

        # 纯贴图节点与材质转资源pro 共用基类贴图路径（copy_texture_file）→ 自动纳入同一套去重
        base_module = importlib.import_module(
            "TheHerta4-main.blueprint.node_postprocess_material"
        )
        pure_node = tree.nodes.new("SSMTNode_PostProcess_PureTexture")
        check(
            "纯贴图节点复用基类 copy_texture_file（走同一计划器）",
            type(pure_node).copy_texture_file
            is base_module.SSMTNode_PostProcess_MaterialBase.copy_texture_file,
        )
        pure_node.export_textures_as_dds = False

        material_a = bpy.data.materials.new("DiffuseMap_PureSmokeA")
        material_b = bpy.data.materials.new("DiffuseMap_PureSmokeB")
        pure_image = bpy.data.images.new("pure_smoke_tex", 8, 8, alpha=True)
        try:
            pure_image.filepath_raw = png_path
            pure_image.file_format = "PNG"
            pure_image.save()

            pure_dir = os.path.join(tmp, "PureTextures")
            os.makedirs(pure_dir, exist_ok=True)
            TextureExportPlan.configure(True, pixel_dedup=True, mod_export_path=tmp)
            name_a = pure_node.copy_texture_file(pure_image, pure_dir, material_a)
            files_after_register = sorted(os.listdir(pure_dir))
            check(
                "纯贴图节点登记后不立即落盘",
                bool(name_a) and files_after_register == [],
                f"{name_a} {files_after_register}",
            )
            name_b = pure_node.copy_texture_file(pure_image, pure_dir, material_b)
            check(
                "纯贴图节点不同材质名同内容 → 复用同一文件",
                name_a == name_b,
                f"{name_a} / {name_b}",
            )
            TextureExportPlan.finalize(tmp)
            pure_files = sorted(os.listdir(pure_dir))
            check(
                "纯贴图节点贴图由 finalize 落盘一次",
                pure_files == [name_a],
                str(pure_files),
            )
        finally:
            bpy.data.images.remove(pure_image)
            bpy.data.materials.remove(material_a)
            bpy.data.materials.remove(material_b)

        # 切换变量避让：纯贴图节点必须避开同蓝图「材质转资源pro」已占用的 $swapkeyN
        switch_tree = bpy.data.node_groups.new("SmokeSwitchTree", "SSMTBlueprintTreeType")
        try:
            material_node = switch_tree.nodes.new(
                "SSMTNode_PostProcess_CustomMaterialAssign"
            )
            material_item = material_node.target_items.add()
            material_group = material_item.switch_groups.add()
            material_group.switch_variable = "$swapkey150"
            material_group.bindings = json.dumps([["DiffuseMap_a", "DiffuseMap_b"]])
            material_group.state_count = 2
            material_node.material_switch_var = "$swapkey150"

            pure_switch = switch_tree.nodes.new("SSMTNode_PostProcess_PureTexture")
            pure_item = pure_switch.target_items.add()
            pure_group = pure_item.switch_groups.add()
            pure_group.switch_variable = "$swapkey150"
            pure_group.bindings = json.dumps([["DiffuseMap_2.002", "DiffuseMap_3.002"]])
            pure_group.state_count = 2
            pure_switch.material_switch_var = "$swapkey150"

            specs = pure_switch._resolve_switch_variables(
                pure_switch.iter_switch_specs()
            )
            resolved = specs[0]["variable"] if specs else None
            check(
                "纯贴图切换变量避让「材质转资源pro」（$swapkey150 -> 新号）",
                resolved is not None and resolved != "$swapkey150",
                str(resolved),
            )
        finally:
            bpy.data.node_groups.remove(switch_tree, do_unlink=True)
    finally:
        TextureExportPlan.reset()
        bpy.data.node_groups.remove(tree, do_unlink=True)

    failed = [name for name, ok, _detail in RESULTS if not ok]
    print(f"[SMOKE] 汇总: {len(RESULTS) - len(failed)} 通过 / {len(failed)} 失败")
    if failed:
        print("[SMOKE] 失败项: " + ", ".join(failed))
    return 1 if failed else 0


try:
    EXIT_CODE = main()
except Exception:
    traceback.print_exc()
    EXIT_CODE = 2
print(f"[SMOKE] EXIT={EXIT_CODE}")
