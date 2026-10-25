"""PNG color metadata helpers for 3Dmigoto's shared 2D UI shader."""

import os
import struct
import zlib


def mark_png_as_srgb(path):
    """Mark unchanged PNG pixels as sRGB for the 3Dmigoto texture loader."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as stream:
            data = stream.read()
        signature = b"\x89PNG\r\n\x1a\n"
        if not data.startswith(signature):
            return False

        raw_chunks = []
        offset = len(signature)
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            end = offset + 12 + length
            if end > len(data):
                return False
            chunk_type = data[offset + 4:offset + 8]
            chunk_data = data[offset + 8:offset + 8 + length]
            raw_chunks.append((chunk_type, chunk_data, data[offset:end]))
            offset = end
            if chunk_type == b"IEND":
                break
        srgb_chunks = [chunk_data for kind, chunk_data, _ in raw_chunks if kind == b"sRGB"]
        gamma_chunks = [chunk_data for kind, chunk_data, _ in raw_chunks if kind == b"gAMA"]
        gamma_bytes = struct.pack(">I", 45455)
        if (len(srgb_chunks) == 1 and srgb_chunks[0] == b"\x00"
                and len(gamma_chunks) == 1 and gamma_chunks[0] == gamma_bytes
                and not any(kind in (b"iCCP", b"cHRM") for kind, _, _ in raw_chunks)):
            return False

        chunks = []
        inserted = False
        srgb = b"\x00"
        srgb_crc = zlib.crc32(b"sRGB" + srgb) & 0xFFFFFFFF
        gamma_crc = zlib.crc32(b"gAMA" + gamma_bytes) & 0xFFFFFFFF
        color_metadata = (b"sRGB", b"gAMA", b"iCCP", b"cHRM")
        for kind, chunk_data, original in raw_chunks:
            if kind in color_metadata:
                if not inserted:
                    chunks.append(struct.pack(">I", 1) + b"sRGB" + srgb + struct.pack(">I", srgb_crc))
                    chunks.append(struct.pack(">I", 4) + b"gAMA" + gamma_bytes + struct.pack(">I", gamma_crc))
                    inserted = True
                continue
            chunks.append(original)
            if kind == b"IHDR" and not inserted:
                chunks.append(struct.pack(">I", 1) + b"sRGB" + srgb + struct.pack(">I", srgb_crc))
                chunks.append(struct.pack(">I", 4) + b"gAMA" + gamma_bytes + struct.pack(">I", gamma_crc))
                inserted = True
        if not inserted:
            return False
        with open(path, "wb") as stream:
            stream.write(signature + b"".join(chunks))
        return True
    except Exception as exc:
        print(f"[UI image color] Failed to set PNG sRGB metadata for {path}: {exc}")
        return False
