"""Render panel previews from temporary exporter output without changing node data."""

import math
import os
import tempfile
import types

from PIL import Image


class PreviewNode:
    """Bind exporter methods to an isolated settings overlay over a Blender node."""

    def __init__(self, source):
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_values", {})
        object.__setattr__(self, "_props", {})

    def __getattr__(self, name):
        if name in self._values:
            return self._values[name]
        value = getattr(self._source, name)
        if isinstance(value, types.MethodType) and value.__self__ is self._source:
            return types.MethodType(value.__func__, self)
        return value

    def __setattr__(self, name, value):
        self._values[name] = value

    def get(self, key, default=None):
        if key in self._props:
            return self._props[key]
        getter = getattr(self._source, "get", None)
        return getter(key, default) if getter is not None else default

    def __getitem__(self, key):
        return self._props[key] if key in self._props else self._source[key]

    def __setitem__(self, key, value):
        self._props[key] = value


def _entry_buttons(node):
    buttons = []
    for entry in node.swap_panel_entries:
        names = [name.strip() for name in (entry.variables or entry.variable_name).split("|")
                 if name.strip()]
        if not names:
            continue
        buttons.append({
            "var_name": names[0], "var_names": names,
            "option_count": entry.option_count,
            "option_counts": [entry.option_count] * len(names),
            "comment": entry.comment, "hotkey": entry.hotkey,
            "image_path": node._button_image_for_source(entry.hotkey, names[0]),
        })
    return buttons


def export_preview(source, root):
    """Export only this panel and its slider source, independent of sibling panels."""
    node = PreviewNode(source)
    node.ini_file_path = ""
    node.last_mod_ini_path = ""
    node._build_button_list = lambda _path: _entry_buttons(node)
    slider_source = source._find_shape_ext_node()
    slider = PreviewNode(slider_source) if slider_source is not None else None
    node._find_shape_ext_node = lambda: slider
    emitted_slider = [False]
    node._find_slider_panel_node = lambda: slider if emitted_slider[0] else None
    if slider is not None:
        slider._find_combined_slider_panel = lambda: node
    os.makedirs(os.path.join(root, "res"), exist_ok=True)
    ini_path = os.path.join(root, "preview.ini")
    with open(ini_path, "w", encoding="utf-8") as handle:
        handle.write("[Constants]\n\n[Present]\n")
    if slider is not None and node.slider_use_slider_panel:
        emitted_slider[0] = bool(slider.execute_postprocess(
            root, _in_place=True, _ini_path=ini_path, _skip_backup=True))
    if _entry_buttons(node):
        if not node.execute_postprocess(root, _in_place=True, _ini_path=ini_path, _skip_backup=True):
            raise RuntimeError("当前节点的切换按钮未能生成，请检查控制台日志")
    elif not emitted_slider[0]:
        raise RuntimeError("当前面板没有可预览的按钮或滑块，请先刷新按钮列表并检查形态键分组")
    return node, ini_path, os.path.join(root, "res")


def collect_draws(node, values, slider_params):
    """Return texture rectangles in normalized game screen coordinates."""
    draws = []
    ns = node._ensure_namespace()
    has_slider = values.get("base_width0", 0) > 0 and values.get("base_height0", 0) > 0
    independent = values.get(f"{ns}_base_width0", 0) > 0
    slider_x = 0.0

    def add(resource, x, y, width, height):
        if all(math.isfinite(v) for v in (x, y, width, height)) and width > 0 and height > 0:
            draws.append((resource, (x, y, width, height)))

    # Independent panels are placed side by side so both movable panels can be
    # inspected, rather than obscuring each other at their identical default position.
    if independent:
        zoom = values.get(f"{ns}_zoom0", 1.0) or 1.0
        width = values[f"{ns}_base_width0"] * zoom
        height = values[f"{ns}_base_height0"] * zoom
        add(f"ResourceImageToRender0_{ns}", 0.0, 0.0, width, height)
        index = 1
        while f"{ns}_btn_width{index}" in values:
            bw = values[f"{ns}_btn_width{index}"] * zoom
            bh = values[f"{ns}_btn_height{index}"] * zoom
            bx = values[f"{ns}_fixed_rel_x{index}"] * width
            by = values[f"{ns}_fixed_rel_y{index}"] * height - bh * 0.5
            add(f"ResourceSwapButton{index}_{ns}", bx, by, bw, bh)
            index += 1
        slider_x = width + 0.03

    if not has_slider:
        return draws
    zoom = values.get("zoom0", 1.0) or 1.0
    width = values["base_width0"] * zoom
    height = values["base_height0"] * zoom
    add("ResourceImageToRender0", slider_x, 0.0, width, height)
    geometries = []
    index = 1
    modern = "slider_side_padding" in values
    side = values.get("slider_side_padding", 0.0) * zoom
    gap = values.get("slider_label_gap", 0.01) * zoom
    while f"base_width{index}" in values:
        handle_zoom = zoom if modern else (values.get(f"zoom{index}", 1.0) or 1.0)
        hw = values[f"base_width{index}"] * handle_zoom
        hh = values[f"base_height{index}"] * handle_zoom
        label_scale = zoom if modern else 1.0
        lw = values.get(f"btn_base_width{index}", values.get(f"btn_width{index}", 0.0)) * label_scale
        lh = values.get(f"btn_base_height{index}", values.get(f"btn_height{index}", hh)) * label_scale
        left = side + lw + gap if modern else lw + 0.02
        right = width - side if modern else width * 0.95
        maximum = max(left, right - hw)
        param = slider_params[index - 1] if index <= len(slider_params) else 0.0
        hx = slider_x + left + max(0.0, min(1.0, param)) * (maximum - left)
        hy = values.get(f"fixed_rel_y{index}", 0.5) * height - hh * 0.5
        center = hx + hw * 0.5
        geometries.append((index, (hx, hy, hw, hh),
                           (slider_x + left - lw - gap, hy + (hh - lh) * 0.5, lw, lh)))
        add("ResourceLeftBar", slider_x + left, hy + hh * 0.25,
            center - slider_x - left, hh * 0.5)
        add("ResourceRightBar", center, hy + hh * 0.25, slider_x + right - center, hh * 0.5)
        index += 1
    for index, _handle, label in geometries:
        add(f"ResourcePlayPauseButton{index}", *label)
    for _index, handle, _label in geometries:
        add("ResourceSliderHandle", *handle)

    widths, heights = [], []
    while f"{ns}_btn_w{len(widths) + 1}" in values:
        i = len(widths) + 1
        widths.append(values[f"{ns}_btn_w{i}"])
        heights.append(values[f"{ns}_btn_h{i}"])
    if widths:
        attached = node.panel_mode == "ATTACHED_LEFT"
        per_row = max(1, min(int(node.buttons_per_row or 1), len(widths)))
        rows = math.ceil(len(widths) / per_row)
        columns = [max(widths[c::per_row]) for c in range(per_row)]
        row_gap = float(node.button_row_spacing)
        col_gap = float(node.button_column_spacing)
        for i, (bw, bh) in enumerate(zip(widths, heights)):
            row, col = divmod(i, per_row)
            if attached:
                bx = slider_x - 0.02 - bw * zoom
                by = (float(node.button_top_padding) + i * (bh + row_gap)) * zoom
            else:
                bx = slider_x + (0.02 + sum(columns[:col]) + col * col_gap
                                 + (columns[col] - bw) * 0.5) * zoom
                if node.panel_mode == "INTEGRATED_TOP":
                    by = (float(node.button_top_padding) + row * (bh + row_gap)) * zoom
                else:
                    by = height - (rows - row) * (bh + row_gap) * zoom
            add(f"ResourceSwapButton{i + 1}_{ns}", bx, by, bw * zoom, bh * zoom)
    return draws


def paste_clipped(canvas, image, x, y):
    """Skip fully offscreen images before calling Pillow's crop."""
    left, top = max(0, -x), max(0, -y)
    right = min(image.width, canvas.width - x)
    bottom = min(image.height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    canvas.alpha_composite(image.crop((left, top, right, bottom)), (x + left, y + top))


def render_panel_runtime_image(node, ini_path, res_dir):
    from . import node_postprocess_swap_panel as panel
    with open(ini_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    values = panel._panel_runtime_parse_globals(text)
    resources = panel._panel_runtime_parse_resources(text)
    params = panel._panel_runtime_slider_params(node._find_shape_ext_node(), values)
    draws = collect_draws(node, values, params)
    if not draws:
        raise RuntimeError("当前节点未生成可渲染的面板，请检查按钮列表和滑块分组")
    xmin = min(rect[0] for _, rect in draws)
    ymin = min(rect[1] for _, rect in draws)
    xmax = max(rect[0] + rect[2] for _, rect in draws)
    ymax = max(rect[1] + rect[3] for _, rect in draws)
    span_x = (xmax - xmin) * (16.0 / 9.0)
    span_y = ymax - ymin
    density = min(1080.0, 2048.0 / max(span_x, span_y, 1e-8))
    sx, sy = density * (16.0 / 9.0), density
    margin = 24
    canvas = Image.new("RGBA", (max(1, math.ceil(span_x * density)) + margin * 2,
                                 max(1, math.ceil(span_y * density)) + margin * 2), (30, 30, 30, 255))
    for resource, (x, y, width, height) in draws:
        filename = resources.get(resource, "")
        path = filename if os.path.isabs(filename) else os.path.join(res_dir, filename)
        if not filename or not os.path.isfile(path):
            continue
        with Image.open(path) as source:
            image = source.convert("RGBA")
        size = (max(1, round(width * sx)), max(1, round(height * sy)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        paste_clipped(canvas, image, margin + round((x - xmin) * sx), margin + round((y - ymin) * sy))
    return canvas


def generate_preview(node):
    with tempfile.TemporaryDirectory(prefix="ssmt_panel_runtime_preview_") as root:
        proxy, ini_path, res_dir = export_preview(node, root)
        image = render_panel_runtime_image(proxy, ini_path, res_dir)
    # Separate paths prevent sibling previews and image viewers from overwriting
    # or caching another node's result.
    descriptor, output = tempfile.mkstemp(
        prefix=f"ssmt_panel_preview_{proxy._ensure_namespace()}_", suffix=".png")
    os.close(descriptor)
    image.convert("RGB").save(output)
    return output
