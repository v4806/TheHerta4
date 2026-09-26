def _buffer_to_bytes(buffer_data) -> bytes:
    if buffer_data is None:
        return b""
    if isinstance(buffer_data, bytes):
        return buffer_data
    if hasattr(buffer_data, "tobytes"):
        return buffer_data.tobytes()
    return bytes(buffer_data)


class ShapeKeyDirectExportError(RuntimeError):
    pass


def resolve_use_delta(node) -> bool:
    """节点当前是否按增量存储形态键。

    真实节点走三态口径 ``effective_use_delta``（只勾「存储顶点增量」= 仅位置增量，
    只勾「储存全部顶点属性增量」= 全通道增量，两者都不勾 = 绝对坐标）；
    NTMI 适配器没有该方法，沿用自身的 ``store_deltas``，保持它现有的仅位置增量行为。
    """
    resolver = getattr(node, "effective_use_delta", None)
    if callable(resolver):
        return bool(resolver())
    return bool(getattr(node, "store_deltas", True))
