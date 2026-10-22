import numpy as np


SRGB_LINEAR_THRESHOLD = 0.0031308


def get_color_storage_field(color_attribute) -> str:
    """Return the Blender RNA field used to access this color attribute.

    The field decides the value space as well as the accessor name: FLOAT_COLOR
    channels are scene-linear, BYTE_COLOR channels are sRGB-encoded. Callers that
    need one canonical space must convert explicitly (see
    encode_color_for_unorm_target for the export direction).
    """
    if not hasattr(color_attribute, "domain"):
        # Legacy mesh.vertex_colors layers expose MeshLoopColor.color only.
        return "color"
    data_type = str(getattr(color_attribute, "data_type", "") or "")
    if data_type == "FLOAT_COLOR":
        return "color"
    return "color_srgb"


def is_linear_color_storage(color_attribute) -> bool:
    """Return True when this color attribute holds scene-linear channel values."""
    if color_attribute is None or not hasattr(color_attribute, "domain"):
        # Legacy vertex_colors layers are byte layers holding sRGB-encoded values.
        return False
    return str(getattr(color_attribute, "data_type", "") or "") == "FLOAT_COLOR"


def linear_to_srgb(values) -> np.ndarray:
    """Apply the linear-to-sRGB transfer function channel-wise (no clipping).

    Evaluated in float64 and narrowed on return, so 1.0 maps to exactly 1.0
    (float32 arithmetic on 1.055 - 0.055 lands a hair short of it).
    """
    array = np.asarray(values, dtype=np.float64)
    return np.where(
        array <= SRGB_LINEAR_THRESHOLD,
        array * 12.92,
        1.055 * np.power(np.maximum(array, 0.0), 1.0 / 2.4) - 0.055,
    ).astype(np.float32)


def encode_color_for_unorm_target(color_attribute, rgba_data) -> np.ndarray:
    """Return RGBA values in the encoding an 8-bit UNORM color stream stores.

    Byte attributes already hold sRGB-encoded channels, but FLOAT_COLOR
    attributes hold scene-linear ones; quantizing those straight to 8 bits
    darkens the color (sRGB byte 128 sits at linear 0.21586 and lands on byte
    55). Alpha is a mask rather than a color, so it passes through untouched.
    Both are clamped, since an unsigned normalized target has no other range;
    at 1.0 the transfer function round-trips exactly.
    """
    values = np.asarray(rgba_data, dtype=np.float32)
    if values.size == 0 or not is_linear_color_storage(color_attribute):
        return values
    encoded = np.clip(values, 0.0, 1.0)
    encoded[..., :3] = linear_to_srgb(encoded[..., :3])
    return encoded


def read_color_attribute_data(color_attribute, count: int | None = None) -> np.ndarray:
    """Read a Blender color attribute into a float32 RGBA array."""
    if color_attribute is None:
        return np.zeros((0, 4), dtype=np.float32)

    data = getattr(color_attribute, "data", None)
    if data is None:
        return np.zeros((0, 4), dtype=np.float32)

    if count is None:
        count = len(data)

    if count <= 0:
        return np.zeros((0, 4), dtype=np.float32)

    result = np.empty(count * 4, dtype=np.float32)
    data.foreach_get(get_color_storage_field(color_attribute), result)
    return result.reshape(-1, 4)


def write_color_attribute_data(color_attribute, rgba_data) -> None:
    """Write normalized float RGBA data into a Blender color attribute."""
    if color_attribute is None:
        return

    array = np.asarray(rgba_data, dtype=np.float32).reshape(-1, 4)
    color_attribute.data.foreach_set(
        get_color_storage_field(color_attribute),
        array.ravel(),
    )


def sample_color_attribute_to_loops(mesh, color_attribute) -> np.ndarray:
    """Return per-loop RGBA values for either CORNER or POINT color attributes."""
    attr_values = read_color_attribute_data(color_attribute)
    if attr_values.size == 0:
        return attr_values

    domain = str(getattr(color_attribute, "domain", "") or "")
    if domain != "POINT":
        return attr_values

    num_loops = len(getattr(mesh, "loops", ()))
    if num_loops == 0:
        return np.zeros((0, 4), dtype=np.float32)

    loop_vertex_indices = np.empty(num_loops, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loop_vertex_indices)
    return attr_values[loop_vertex_indices]
