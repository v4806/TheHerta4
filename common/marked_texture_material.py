"""按 SSMT 子网格贴图标记创建贴图材质（规范材质 + 渲染材质）。

导出 mod 时贴图类型完全由工作空间里 SubmeshJson 的 ``TextureMarkUpInfoList``
决定：``common/m_ini_helper.py`` 按 ``mark_type`` 分流、按 ``mark_filename`` 取
文件、按 ``mark_slot`` 写 ``ps-tN``——所以 Blender 物体上有没有材质都不影响导出，
没标记的类型既不会导出贴图也不会写进 ini。

本模块让**导入**方向使用同一份标记：标记了哪种类型就为哪种类型建材质。

## 材质布局（两套并存）

Blender 里每个面由 ``material_index`` 指向**唯一一个**材质槽，一个面不可能同时
用两个材质（EEVEE/Cycles 都是这个模型，没有真正的跨材质）。所以同一面要同时
显示多种贴图，只能让它们待在同一个材质里；而导出侧又要按「材质名首段」判定类型、
沿材质输出递归只取**第一张**图像纹理，多张图挤进一个材质会让导出认不准。
两者无法用同一个材质满足，于是拆成两套：

- **槽 0 —— 渲染材质** ``IMGPV_<网格名>``：各类型贴图接在同一个原理化 BSDF 的
  不同通道上。导入的网格面 ``material_index`` 都是 0，所以**真正被渲染的是它**。
  它的首段 ``imgpv`` 不是任何贴图类型名，导出侧会完全忽略。
- **槽 1..N —— 规范材质** ``<类型名>_<网格名>``：一材质一张图，首段恰好是类型名，
  专供「材质转资源pro」按类型识别（见 ``blueprint/node_postprocess_material.py``
  的 ``_find_workspace_slot_materials``）。

各通道接法对齐独立插件「快速应用纹理」（MOD 规范）：
  - DiffuseMap  -> 规范材质沿用 TheHerta4 原生透明混合方案；渲染材质走 Base Color + Alpha
  - NormalMap   -> Normal Map 节点；DirectX 法线（G 通道反相）；BC5 缺 Z 时用 R/G 重建
  - LightMap    -> 绝区零（ZZMI）走通道拆分（G->金属度、B->高光），其它走自发光
  - MaterialMap -> 粗糙度
  - BodyMaskMap -> 自发光强度（黑白遮罩，白色发光）
  - 其它类型    -> 规范材质只挂一张图像纹理（保证导出仍能按材质名识别），渲染材质不接
"""

import os

import bpy

from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .logic_name import LogicName
from .submesh_metadata import SubmeshMetadataResolver
from .texture_metadata_helper import TextureMetadataResolver


#: 标记类型（小写）-> 通道接法。
#: 同一个贴图类型在 SSMT 里有多种写法，两种常见命名都要认出来：
#:   4a178546-18468-0-DiffuseMap.dds
#:   LOD0.c209c22b-45087-0.身体_Diffuse.dds
MARK_SOCKET_TYPE_DICT = {
    "diffusemap": "DIFFUSE",
    "diffuse": "DIFFUSE",
    "normalmap": "NORMAL",
    "normal": "NORMAL",
    "lightmap": "LIGHTMAP",
    "light": "LIGHTMAP",
    "hairlightmap": "LIGHTMAP",
    "hairlight": "LIGHTMAP",
    "materialmap": "SURFACE",
    "material": "SURFACE",
    "bodymaskmap": "BODYMASK",
    "bodymask": "BODYMASK",
}

#: 认定「带槽位绑定」的标记类型。与 ``M_IniHelper.is_slot_binding_mark_type`` 同口径：
#: Hash 型标记靠贴图 hash 匹配游戏原有纹理、没有固定文件名与槽位，导入侧不建材质。
SLOT_MARK_TYPES = {"Slot", "SharedSlot"}

#: 渲染材质前缀。首段不是任何贴图类型名，导出侧会忽略它。
#: 与独立插件「快速应用纹理」同前缀（该插件的渲染材质是 ``IMGPV_<物体名>``）。
RENDER_MATERIAL_PREFIX = "IMGPV_"

_ILLEGAL_NAME_CHARS = '\\/:*?"<>|'

#: Blender 数据块名上限 63 字节，留余量避免超长被自动截断后 get() 再也匹配不上。
_MAX_MATERIAL_NAME_BYTES = 60

#: 取图时优先尝试的扩展名顺序（工作空间里可能同时存在原始 .dds 与转换出的 .png）。
_PREFERRED_TEXTURE_EXTENSIONS = (".png", ".dds")

#: 黑色遮罩 / 空区域的判定阈值。
_BLANK_THRESHOLD = 0.02

#: 身体发光遮罩接自发光强度时的缩放：遮罩是「哪里发光」而不是发光强度，
#: 白色直接当强度会非常刺眼，缩小后只是「比不发光亮一点」。
_BODYMASK_EMISSION_SCALE = 0.35


# 法线贴图 Z 分量探测：DDS 等格式在 Blender 里恒为 4 通道，不能用 channels 判断，
# 只能缩略采样看蓝通道中位数。与独立插件「快速应用纹理」同口径。
_PROBE_SIZE = 128
_Z_MEDIAN_THRESHOLD = 0.8
_PROBE_CACHE = {}


def _truncate_name(name: str) -> str:
    encoded = str(name or "").encode("utf-8")
    if len(encoded) <= _MAX_MATERIAL_NAME_BYTES:
        return str(name or "")
    return encoded[:_MAX_MATERIAL_NAME_BYTES].decode("utf-8", "ignore")


def _safe_name_token(text: str) -> str:
    token = str(text or "").strip()
    for ch in _ILLEGAL_NAME_CHARS:
        token = token.replace(ch, "_")
    return token or "Texture"


def mark_material_name(mark_name: str, mesh_name: str) -> str:
    """拼出 MOD 规范材质名：``<类型名>_<网格名>``。

    首段必须**恰好**是类型名，导出侧才认（``material_name.split('_')[0]``）。
    """
    return _truncate_name(f"{_safe_name_token(mark_name)}_{_safe_name_token(mesh_name)}")


def render_material_name(mesh_name: str) -> str:
    """拼出渲染材质名：``IMGPV_<网格名>``（首段不是贴图类型名）。"""
    return _truncate_name(f"{RENDER_MATERIAL_PREFIX}{_safe_name_token(mesh_name)}")


def mark_socket_type(mark_name: str) -> str:
    """标记名 -> 通道接法；未知类型返回空串（只挂图不接）。"""
    return MARK_SOCKET_TYPE_DICT.get(str(mark_name or "").strip().lower(), "")


#: 标记字段在原始 dict（SubmeshJson 大小写形态）与标记对象上的键名。
_MARK_FIELD_KEYS = {
    "mark_name": ("MarkName", "mark_name"),
    "mark_type": ("MarkType", "mark_type"),
    "mark_hash": ("MarkHash", "mark_hash"),
    "mark_slot": ("MarkSlot", "mark_slot"),
    "mark_filename": ("MarkFileName", "mark_filename"),
}


def get_mark_field(mark, field_name: str, default=""):
    """取标记字段，同时兼容原始 dict 与 normalize 后的标记对象。"""
    if isinstance(mark, dict):
        for key in _MARK_FIELD_KEYS.get(field_name, (field_name,)):
            if key in mark:
                return mark[key]
        return default
    return getattr(mark, field_name, default)


def is_slot_mark(mark) -> bool:
    return str(get_mark_field(mark, "mark_type") or "").strip() in SLOT_MARK_TYPES


def unique_str_from_json_path(json_file_path: str) -> str:
    """由 SubmeshJson 路径反推工作空间身份（``LOD0.xxx-1-0`` 或裸身份）。

    与 ``SSMTImportHelper._build_workspace_unique_str_from_json_path`` 同口径：
    ``<LODx>/<子网格>/TYPE_<类型>/<子网格>.json``。
    """
    json_dir = os.path.dirname(str(json_file_path or ""))
    submesh_dir = os.path.basename(os.path.dirname(json_dir))
    lod_dir = os.path.basename(os.path.dirname(os.path.dirname(json_dir)))

    if lod_dir.upper().startswith("LOD") and lod_dir[3:].isdigit():
        return lod_dir + "." + submesh_dir
    return submesh_dir


def read_texture_marks(unique_str: str):
    """读取该子网格的贴图标记，返回 ``(标记列表, 贴图源目录)``。

    标记来自工作空间 SubmeshJson 的 ``TextureMarkUpInfoList``——正是导出 mod 时
    ``M_IniHelper`` 使用的那一份。任何解析失败都返回 ``([], "")``，由调用方回退。
    """
    key = str(unique_str or "").strip()
    if not key:
        return [], ""

    cached = _MARK_CACHE.get((GlobalConfig.path_workspace_folder(), key))
    if cached is not None:
        return cached

    marks = []
    extract_folder = ""
    try:
        metadata = SubmeshMetadataResolver.resolve(key)
        # SubmeshMetadata.texture_markup_info_list 里是 SubmeshJson 的原始 dict
        # （大写键 MarkName/MarkType/MarkFileName），导出侧同样先过一次 normalize
        # 才开始用——这里保持同一处理链，否则拿到的是 dict 而不是标记对象。
        marks = TextureMetadataResolver.normalize_texture_markup_info_list(
            list(getattr(metadata, "texture_markup_info_list", []) or [])
        )
        marks = TextureMetadataResolver._dedupe_texture_markup_info_list(marks)
        extract_folder = str(getattr(metadata, "extract_gametype_folder_path", "") or "")
    except Exception as ex:
        print("[贴图标记] 读取失败，回退文件名搜索: " + key + "，原因: " + str(ex))
        marks, extract_folder = [], ""

    result = (marks, extract_folder)
    _MARK_CACHE[(GlobalConfig.path_workspace_folder(), key)] = result
    return result


#: 导入批次内的标记读取缓存（同一身份只解析一次 SubmeshJson）。
_MARK_CACHE = {}


def clear_mark_cache() -> None:
    """工作空间切换 / 重新导入后清空缓存，避免旧工作空间的标记继续生效。"""
    _MARK_CACHE.clear()
    _PROBE_CACHE.clear()


def resolve_mark_texture_path(extract_folder: str, mark) -> str:
    """按标记解析贴图文件路径。

    ``mark_filename`` 指向工作空间里的原始文件（通常是 ``.dds``）。若同目录已经存在
    同名 ``.png``（「导入后自动转PNG」的产物、或用户手工编辑过的无损版本），优先用
    png——这样重复导入不会再让 Blender 去解压排不掉的 BC7/BC6H DDS，也保留用户的编辑。
    """
    filename = str(get_mark_field(mark, "mark_filename") or "").strip()
    if not filename:
        return ""

    stem, extension = os.path.splitext(filename)
    candidates = []
    for preferred in _PREFERRED_TEXTURE_EXTENSIONS:
        if preferred != extension.lower():
            candidates.append(stem + preferred)
    candidates.append(filename)

    for candidate in candidates:
        candidate_path = os.path.join(str(extract_folder or ""), candidate)
        if os.path.isfile(candidate_path):
            return candidate_path
    return ""


def _needs_z_rebuild(image) -> bool:
    """法线贴图是否缺少 Z（蓝）分量。

    BC5 风格的两通道法线只有 R/G 有效，Blender 却会解出无意义的蓝通道（约 0.5），
    直接使用会让法线退化成切向量、渲染大面积发黑；此时需要用 R/G 重建 Z。
    任何采样失败都返回 False（按原图直连，不做改动）。
    """
    if image is None:
        return False

    key = (getattr(image, "name", ""), tuple(getattr(image, "size", ()) or ()))
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    needs_z = False
    small = None
    try:
        small = image.copy()
        small.scale(_PROBE_SIZE, _PROBE_SIZE)
        data = [0.0] * (_PROBE_SIZE * _PROBE_SIZE * 4)
        small.pixels.foreach_get(data)

        blues = []
        for index in range(0, len(data), 4):
            red = data[index]
            green = data[index + 1]
            blue = data[index + 2]
            if (
                red <= _BLANK_THRESHOLD
                and green <= _BLANK_THRESHOLD
                and blue <= _BLANK_THRESHOLD
            ):
                continue
            blues.append(blue)
        if len(blues) >= (_PROBE_SIZE * _PROBE_SIZE) * 0.05:
            blues.sort()
            needs_z = blues[len(blues) // 2] < _Z_MEDIAN_THRESHOLD
    except Exception as ex:
        print("[贴图标记] 法线 Z 分量探测失败，按原图直连: " + str(ex))
        needs_z = False
    finally:
        if small is not None:
            try:
                bpy.data.images.remove(small)
            except Exception:
                pass

    _PROBE_CACHE[key] = needs_z
    return needs_z


def _ignore_texture_alpha() -> bool:
    """「导入贴图时忽略透明度通道」开关（轻量宿主缺访问器时按关处理）。"""
    try:
        return bool(GlobalProterties.ignore_texture_alpha())
    except Exception:
        return False


# ---------------------------------------------------------------
# 节点基础
# ---------------------------------------------------------------
def _clear_nodes(node_tree) -> None:
    for node in list(node_tree.nodes):
        try:
            node_tree.nodes.remove(node)
        except Exception:
            continue


def _make_principled_material(material):
    """把材质清成「Principled + 输出」的最小结构，返回 (节点树, 原理化BSDF)。"""
    material.use_nodes = True
    node_tree = material.node_tree
    _clear_nodes(node_tree)

    principled = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (0, 0)
    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (320, 0)
    node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    return node_tree, principled


def _new_texture_node(node_tree, image, location):
    tex_image = node_tree.nodes.new("ShaderNodeTexImage")
    tex_image.image = image
    tex_image.location = location
    return tex_image


def _link_texture_image(texture_path: str, colorspace: str):
    image = bpy.data.images.load(texture_path)
    try:
        if image.colorspace_settings.name != colorspace:
            image.colorspace_settings.name = colorspace
    except Exception as ex:
        print("[贴图标记] 设置色彩空间失败 " + colorspace + ": " + str(ex))
    return image


# ---------------------------------------------------------------
# 通道接线（都只往已建好的节点树上加，可在同一材质里累加）
# ---------------------------------------------------------------
def _wire_normal(node_tree, principled, texture_path: str) -> None:
    """法线：Normal Map 节点；DirectX 法线需反相 G；缺 Z 时用 R/G 重建。"""
    image = _link_texture_image(texture_path, "Non-Color")
    tex_image = _new_texture_node(node_tree, image, (-1100, -300))

    normal_map = node_tree.nodes.new("ShaderNodeNormalMap")
    normal_map.location = (-200, -300)
    normal_map.uv_map = "TEXCOORD.xy"
    if hasattr(normal_map, "space"):
        normal_map.space = "TANGENT"
    if "Strength" in normal_map.inputs:
        normal_map.inputs["Strength"].default_value = 1.0
    node_tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    # Blender 的 Normal Map 节点没有 GL/DX 开关：MOD 基于 3DMigoto（DirectX），
    # 法线 G 通道方向与 Blender 默认（OpenGL）相反，必须显式反相。
    separate = node_tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = (-900, -300)
    if hasattr(separate, "mode"):
        separate.mode = "RGB"
    node_tree.links.new(separate.inputs["Color"], tex_image.outputs["Color"])

    combine = node_tree.nodes.new("ShaderNodeCombineColor")
    combine.location = (-620, -300)
    node_tree.links.new(combine.inputs["Red"], separate.outputs["Red"])
    node_tree.links.new(combine.inputs["Blue"], separate.outputs["Blue"])

    invert = node_tree.nodes.new("ShaderNodeMath")
    invert.operation = "SUBTRACT"
    invert.location = (-760, -560)
    invert.inputs[0].default_value = 1.0
    node_tree.links.new(invert.inputs[1], separate.outputs["Green"])
    node_tree.links.new(combine.inputs["Green"], invert.outputs["Value"])

    if _needs_z_rebuild(image):
        for link in list(combine.inputs["Blue"].links):
            node_tree.links.remove(link)
        combine.inputs["Blue"].default_value = 1.0

    node_tree.links.new(normal_map.inputs["Color"], combine.outputs["Color"])


def _wire_emission(node_tree, principled, texture_path: str) -> None:
    """光照贴图（非绝区零）：自发光颜色。

    这里不把自发光颜色复位成黑色：同一材质里可能还挂着身体发光遮罩（它负责
    发光区域），复位成黑会把 LightMap 提供的发光颜色一并抹掉。贴图断开时残留
    自发光的问题只在「单独一个 LightMap 材质」里出现，交给 Emission Strength
    的默认值处理即可。
    """
    image = _link_texture_image(texture_path, "Non-Color")
    tex_image = _new_texture_node(node_tree, image, (-500, -900))

    emission_input = None
    if "Emission Color" in principled.inputs:
        emission_input = principled.inputs["Emission Color"]
    elif "Emission" in principled.inputs:
        emission_input = principled.inputs["Emission"]
    if emission_input is None:
        return

    node_tree.links.new(tex_image.outputs["Color"], emission_input)
    if "Emission Strength" in principled.inputs:
        try:
            principled.inputs["Emission Strength"].default_value = 1.0
        except Exception:
            pass


def _wire_zzz_light(node_tree, principled, texture_path: str) -> None:
    """绝区零 LightMap：按通道语义接入（都是数据通道，不是颜色）。

    R = 阴影/轮廓 ramp 索引 —— Blender 标准节点没有对应输入，不连接；
    G = 金属度 -> Metallic；
    B = 光泽   -> Specular IOR Level。

    直接把这张图当颜色接到自发光会让物体整体泛红（R 通道数值最大）。
    """
    image = _link_texture_image(texture_path, "Non-Color")
    tex_image = _new_texture_node(node_tree, image, (-800, -900))

    metallic = principled.inputs.get("Metallic")
    specular = principled.inputs.get("Specular IOR Level")
    if specular is None:
        specular = principled.inputs.get("Specular")

    separate = node_tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = (-520, -900)
    if hasattr(separate, "mode"):
        separate.mode = "RGB"
    node_tree.links.new(separate.inputs["Color"], tex_image.outputs["Color"])

    if metallic is not None:
        for link in list(metallic.links):
            node_tree.links.remove(link)
        node_tree.links.new(separate.outputs["Green"], metallic)
    if specular is not None:
        for link in list(specular.links):
            node_tree.links.remove(link)
        node_tree.links.new(separate.outputs["Blue"], specular)


def _wire_surface(node_tree, principled, texture_path: str) -> None:
    """质感图（MaterialMap）：粗糙度。"""
    image = _link_texture_image(texture_path, "Non-Color")
    tex_image = _new_texture_node(node_tree, image, (-500, -1200))
    node_tree.links.new(tex_image.outputs["Color"], principled.inputs["Roughness"])


def _wire_bodymask(node_tree, principled, texture_path: str) -> None:
    """身体发光遮罩（绝区零 BodyMaskMap）：接到自发光强度。

    黑白遮罩，白色=该处发光、黑色=不发光。它是「哪里发光」的遮罩而不是发光强度，
    所以接在 Emission Strength 上（与 LightMap 的自发光颜色相乘），并且不把白色
    当成最高亮度——略微高于不发光即可看出该发光的区域。
    """
    image = _link_texture_image(texture_path, "Non-Color")
    tex_image = _new_texture_node(node_tree, image, (-500, -1500))

    strength = principled.inputs.get("Emission Strength")
    if strength is None:
        return

    scale = node_tree.nodes.new("ShaderNodeMath")
    scale.operation = "MULTIPLY"
    scale.location = (-720, -1500)
    scale.inputs[1].default_value = _BODYMASK_EMISSION_SCALE
    node_tree.links.new(scale.inputs[0], tex_image.outputs["Color"])
    node_tree.links.new(scale.outputs["Value"], strength)


def _wire_channel(node_tree, principled, socket_type: str, texture_path: str, logic_name: str) -> None:
    """把一张贴图接到已建好的原理化 BSDF 的对应通道上。"""
    if socket_type == "NORMAL":
        _wire_normal(node_tree, principled, texture_path)
    elif socket_type == "LIGHTMAP":
        if LogicName.is_zzmi_family(logic_name):
            _wire_zzz_light(node_tree, principled, texture_path)
        else:
            _wire_emission(node_tree, principled, texture_path)
    elif socket_type == "SURFACE":
        _wire_surface(node_tree, principled, texture_path)
    elif socket_type == "BODYMASK":
        _wire_bodymask(node_tree, principled, texture_path)


def _wire_render_diffuse(node_tree, principled, texture_path: str) -> None:
    """渲染材质的漫反射：原理化 BSDF 的 Base Color（+ 透明度）。

    和规范材质不同——规范材质的 DiffuseMap 单独占一个材质，可以继续用原来的
    漫反射 BSDF + 透明混合；渲染材质要跟金属度/粗糙度/法线共存，只能用原理化 BSDF。
    透明度沿用导入现有语义：受「导入贴图时忽略透明度通道」约束，忽略时不连 Alpha。
    """
    image = _link_texture_image(texture_path, "sRGB")
    tex_image = _new_texture_node(node_tree, image, (-1100, 200))
    node_tree.links.new(tex_image.outputs["Color"], principled.inputs["Base Color"])

    if _ignore_texture_alpha():
        image.alpha_mode = "NONE"
        return

    image.alpha_mode = "CHANNEL_PACKED"
    alpha_input = principled.inputs.get("Alpha")
    if alpha_input is not None:
        node_tree.links.new(tex_image.outputs["Alpha"], alpha_input)


# ---------------------------------------------------------------
# 材质构建
# ---------------------------------------------------------------
def _apply_diffuse_material(material, texture_path: str, logic_name: str) -> None:
    """规范材质的漫反射：沿用 TheHerta4 原生方案（IdentityV 不透明、其余透明混合）。

    这两个图函数内部自行 load 贴图、设 sRGB、清空节点树，行为与旧的单材质路径
    完全一致，这里不再重复建图。
    """
    from .mesh_create_helper import MeshCreateHelper

    if logic_name == LogicName.IdentityV:
        material.blend_method = "OPAQUE"
        MeshCreateHelper.create_diffuse_material_graph(
            node_tree=material.node_tree,
            texture_path=texture_path,
        )
        return

    material.blend_method = "BLEND"
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = False
    elif hasattr(material, "show_transparent_back"):
        material.show_transparent_back = False
    MeshCreateHelper.create_transparent_material_graph(
        node_tree=material.node_tree,
        texture_path=texture_path,
    )


def _apply_plain_material(material, texture_path: str) -> None:
    """未知类型：只挂一张图像纹理（不接任何输入），保证导出侧仍能按材质名识别。"""
    node_tree, _principled = _make_principled_material(material)
    image = _link_texture_image(texture_path, "Non-Color")
    _new_texture_node(node_tree, image, (-500, 0))


def _apply_material_channel(material, socket_type: str, texture_path: str, logic_name: str) -> None:
    """规范材质：一个材质只接一个通道。"""
    if socket_type == "DIFFUSE":
        _apply_diffuse_material(material, texture_path, logic_name)
        return
    if not socket_type:
        _apply_plain_material(material, texture_path)
        return

    node_tree, principled = _make_principled_material(material)
    _wire_channel(node_tree, principled, socket_type, texture_path, logic_name)


def _build_render_material(material, entries, logic_name: str) -> None:
    """渲染材质：把各类型贴图接在同一个原理化 BSDF 上。

    ``entries`` 是 ``[(通道接法, 贴图路径), ...]``。
    """
    node_tree, principled = _make_principled_material(material)

    has_diffuse = False
    for socket_type, texture_path in entries:
        if socket_type == "DIFFUSE":
            _wire_render_diffuse(node_tree, principled, texture_path)
            has_diffuse = True
        elif socket_type:
            _wire_channel(node_tree, principled, socket_type, texture_path, logic_name)

    if has_diffuse and logic_name != LogicName.IdentityV and not _ignore_texture_alpha():
        material.blend_method = "BLEND"
        if hasattr(material, "use_transparency_overlap"):
            material.use_transparency_overlap = False
        elif hasattr(material, "show_transparent_back"):
            material.show_transparent_back = False
    else:
        material.blend_method = "OPAQUE"


# ---------------------------------------------------------------
# 材质槽分配
# ---------------------------------------------------------------
def _assign_material_slots(
    obj,
    render_material,
    spec_materials,
    keep_leftovers: bool = False,
) -> None:
    """渲染材质放槽 0（导入的网格面 material_index 都是 0），规范材质依次排后。

    ``keep_leftovers``：物体原有、但不属于本次布局的材质是否追加到末尾保留。
    给旧物体补齐时必须保留（不能弄丢用户手工建的材质）；导入新物体时不用，那些
    槽本来就该是本次建出来的。

    用「替换槽位内容」而不是先清空再追加，避免材质槽索引前移导致面的材质索引错位。
    """
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "materials"):
        return

    ordered = []
    seen = set()
    for material in ([render_material] if render_material is not None else []) + list(
        spec_materials
    ):
        if material is None or id(material) in seen:
            continue
        seen.add(id(material))
        ordered.append(material)

    if keep_leftovers:
        for material in list(data.materials):
            if material is None or id(material) in seen:
                continue
            seen.add(id(material))
            ordered.append(material)

    for index, material in enumerate(ordered):
        if index < len(data.materials):
            data.materials[index] = material
        else:
            data.materials.append(material)


def build_marked_materials(
    obj,
    mesh_name: str,
    directory: str,
    logic_name: str | None = None,
) -> int:
    """按该物体的 SSMT 贴图标记建立全部类型的贴图材质（渲染材质 + 规范材质）。

    ``directory`` 是该部件 SubmeshJson 所在的 ``TYPE_<类型>`` 目录，身份
    （``LOD0.xxx-1-0``）由它反推——与导入时写入 ``3DMigoto:WorkspaceUniqueStr``
    的口径同源。

    返回成功建立的**规范材质**数量；返回 0 表示「没有可用标记」，调用方应回退到
    原有的按文件名搜索逻辑（旧工作空间 / 没有标记的部件保持原行为不变）。
    """
    if obj is None:
        return 0

    if logic_name is None:
        logic_name = GlobalConfig.logic_name

    json_file_path = os.path.join(str(directory or ""), str(mesh_name or "") + ".json")
    unique_str = unique_str_from_json_path(json_file_path)
    if not unique_str:
        return 0

    marks, extract_folder = read_texture_marks(unique_str)
    if not marks or not extract_folder:
        return 0

    entries = []
    spec_materials = []
    failures = []
    for mark in marks:
        if not is_slot_mark(mark):
            continue

        mark_name = str(get_mark_field(mark, "mark_name") or "").strip()
        if not mark_name:
            continue

        texture_path = resolve_mark_texture_path(extract_folder, mark)
        if not texture_path:
            failures.append(
                f"{mark_name}({get_mark_field(mark, 'mark_filename')})"
            )
            continue

        socket_type = mark_socket_type(mark_name)
        material = bpy.data.materials.new(name=mark_material_name(mark_name, mesh_name))
        try:
            _apply_material_channel(material, socket_type, texture_path, logic_name)
        except Exception as ex:
            print("[贴图标记] 建立材质失败 " + mark_name + ": " + str(ex))
            failures.append(mark_name)
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass
            continue

        entries.append((socket_type, texture_path))
        spec_materials.append(material)

    if not spec_materials:
        if failures:
            print("[贴图标记] " + unique_str + " 标记的贴图文件均不可用: " + "，".join(failures))
        return 0

    render_material = None
    try:
        render_material = bpy.data.materials.new(name=render_material_name(mesh_name))
        _build_render_material(render_material, entries, logic_name)
    except Exception as ex:
        print("[贴图标记] 建立渲染材质失败，仅保留规范材质: " + str(ex))
        if render_material is not None:
            try:
                bpy.data.materials.remove(render_material)
            except Exception:
                pass
        render_material = None

    _assign_material_slots(obj, render_material, spec_materials)
    print(
        "[贴图标记] "
        + str(mesh_name)
        + " 按标记建立材质 "
        + str(len(spec_materials))
        + " 个（渲染材质: "
        + (render_material.name if render_material is not None else "无")
        + "）: "
        + "，".join(material.name for material in spec_materials)
    )
    if failures:
        print("[贴图标记] " + str(mesh_name) + " 跳过的标记: " + "，".join(failures))
    return len(spec_materials)


# ---------------------------------------------------------------
# 给已有物体补齐材质
# ---------------------------------------------------------------
#: 导入时写在物体上的工作空间身份（``ssmt_import_helper`` 写入）。旧物体靠它
#: 直接查 SSMT 标记，不需要再按目录反推。
WORKSPACE_UNIQUE_STR_PROP = "3DMigoto:WorkspaceUniqueStr"


def bare_name_from_unique_str(unique_str: str) -> str:
    """去掉 ``LODx.`` 前缀，得到与导入时 ``mesh_name`` 同口径的裸身份。"""
    text = str(unique_str or "").strip()
    if text.upper().startswith("LOD") and "." in text:
        head, _, rest = text.partition(".")
        if head[3:].isdigit() and rest:
            return rest
    return text


def object_workspace_unique_str(obj) -> str:
    """取物体的工作空间身份：优先导入时写入的自定义属性，其次退回网格名/物体名。"""
    try:
        raw = str(obj.get(WORKSPACE_UNIQUE_STR_PROP, "") or "").strip()
    except Exception:
        raw = ""
    if raw:
        return raw

    for candidate in (
        getattr(getattr(obj, "data", None), "name", ""),
        getattr(obj, "name", ""),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _existing_material_by_name(obj, name: str):
    """在物体**自己的材质槽**里按精确名字找材质。

    只在同一物体内复用：``mesh_name`` 不含 LOD 前缀，跨 LOD 的同身份部件材质名
    会完全相同，用 ``bpy.data.materials.get`` 复用会让两个物体共享同一个材质、
    指向同一张贴图。
    """
    try:
        slots = list(obj.material_slots)
    except Exception:
        return None
    for slot in slots:
        material = slot.material
        if material is not None and str(material.name) == name:
            return material
    return None


def build_missing_marked_materials(
    obj,
    unique_str: str | None = None,
    mesh_name: str | None = None,
    logic_name: str | None = None,
):
    """给**已有**物体补齐缺失的贴图材质（渲染材质 + 各类型规范材质）。

    与 :func:`build_marked_materials` 的区别是「补齐」语义：物体上已经有同名词同
    类型的材质时**原样保留**（不重建节点，避免抹掉用户的手工修改），只新建缺的
    类型，并保证渲染材质排在槽 0（网格面 ``material_index`` 都是 0，靠它渲染）。

    返回 ``(新建数, 复用数, 警告列表)``；警告列表为空表示全部成功。
    """
    if obj is None:
        return 0, 0, ["物体为空"]

    if logic_name is None:
        logic_name = GlobalConfig.logic_name

    resolved_unique_str = str(unique_str or "").strip() or object_workspace_unique_str(obj)
    if not resolved_unique_str:
        return 0, 0, ["无法确定物体身份（缺少 WorkspaceUniqueStr）"]

    resolved_mesh_name = str(mesh_name or "").strip() or bare_name_from_unique_str(
        resolved_unique_str
    )

    marks, extract_folder = read_texture_marks(resolved_unique_str)
    if not marks or not extract_folder:
        return 0, 0, [f"读不到贴图标记（{resolved_unique_str}）"]

    entries = []
    spec_materials = []
    warnings = []
    created = 0
    reused = 0

    for mark in marks:
        if not is_slot_mark(mark):
            continue

        mark_name = str(get_mark_field(mark, "mark_name") or "").strip()
        if not mark_name:
            continue

        socket_type = mark_socket_type(mark_name)
        target_name = mark_material_name(mark_name, resolved_mesh_name)
        texture_path = resolve_mark_texture_path(extract_folder, mark)

        existing = _existing_material_by_name(obj, target_name)
        if existing is not None:
            # 已有同名词同类型材质：原样保留，只把它纳入渲染材质的接线来源。
            spec_materials.append(existing)
            reused += 1
            if texture_path:
                entries.append((socket_type, texture_path))
            else:
                warnings.append(f"{mark_name} 的贴图文件已不在工作空间，渲染材质缺此通道")
            continue

        if not texture_path:
            warnings.append(f"{mark_name} 的贴图文件不存在，跳过")
            continue

        material = bpy.data.materials.new(name=target_name)
        try:
            _apply_material_channel(material, socket_type, texture_path, logic_name)
        except Exception as ex:
            print("[贴图标记] 补齐材质失败 " + mark_name + ": " + str(ex))
            warnings.append(f"{mark_name} 建材质失败: {ex}")
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass
            continue

        entries.append((socket_type, texture_path))
        spec_materials.append(material)
        created += 1

    render_material = _existing_material_by_name(obj, render_material_name(resolved_mesh_name))
    if render_material is None and entries:
        render_material = bpy.data.materials.new(
            name=render_material_name(resolved_mesh_name)
        )
        try:
            _build_render_material(render_material, entries, logic_name)
        except Exception as ex:
            print("[贴图标记] 补齐渲染材质失败: " + str(ex))
            warnings.append(f"渲染材质建失败: {ex}")
            try:
                bpy.data.materials.remove(render_material)
            except Exception:
                pass
            render_material = None

    if not spec_materials and render_material is None:
        warnings.append("没有可补齐的材质")
        return 0, 0, warnings

    _assign_material_slots(obj, render_material, spec_materials, keep_leftovers=True)
    print(
        "[贴图标记] 补齐 "
        + str(getattr(obj, "name", ""))
        + "：新建 "
        + str(created)
        + " 个、复用 "
        + str(reused)
        + " 个（渲染材质: "
        + (render_material.name if render_material is not None else "无")
        + "）"
    )
    return created, reused, warnings
