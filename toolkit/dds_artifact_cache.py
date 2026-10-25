"""Content-addressed DDS artifacts; independent of bpy and overwrite decisions."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time

_MAGIC = b'HRTDDS01'
_CHUNK = 1024 * 1024


def _export_cache_enabled():
    try:
        from ..common.export_cache import is_export_cache_enabled
    except ImportError:
        return True  # Standalone use has no Blender scene setting.
    return is_export_cache_enabled()


def file_sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _valid_dds(header, size):
    if size < 128 or len(header) < 128 or header[:4] != b'DDS ':
        return False
    if struct.unpack_from('<I', header, 4)[0] != 124 or struct.unpack_from('<I', header, 76)[0] != 32:
        return False
    height, width = struct.unpack_from('<II', header, 12)
    if not width or not height:
        return False
    return header[84:88] != b'DX10' or size >= 148


class DDSArtifactCache:
    """One atomically published blob per key, with payload SHA256 verification.

    Cache failures are misses, not export failures. Callers decide whether a
    target may be written before invoking restore(). No hardlinks to artifacts.
    """
    def __init__(self, root=None, max_bytes=None):
        base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
        self.root = Path(root or os.environ.get('HERTA_DDS_CACHE_DIR') or
                         Path(base) / 'TheHerta4' / 'cache' / 'dds-artifacts-v1')
        self.enabled = os.environ.get('HERTA_DDS_CACHE', '1') != '0'
        self.max_bytes = 8 * 1024**3 if max_bytes is None else max(0, int(max_bytes))
        self._maintenance_lock = threading.Lock()
        self._last_maintenance = 0.0

    @property
    def enabled(self):
        return self._enabled and _export_cache_enabled()

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)

    def _owned_entries(self):
        root = self.root.resolve()
        for path in self.root.glob('[0-9a-f][0-9a-f]/*.hdds'):
            key = path.stem
            if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                continue
            if path.parent.name != key[:2] or not path.resolve().is_relative_to(root):
                continue
            yield path

    def get_stats(self):
        result = {'file_count': 0, 'total_size': 0}
        try:
            for path in self._owned_entries():
                try:
                    result['total_size'] += path.stat().st_size
                    result['file_count'] += 1
                except OSError:
                    pass
        except OSError:
            pass
        return result

    def clear(self):
        # Cleaning remains available when caching is disabled. Leave unrelated
        # files and in-flight temporary publications untouched.
        result = {'file_count': 0, 'errors': []}
        try:
            for path in self._owned_entries():
                try:
                    path.unlink()
                    result['file_count'] += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    result['errors'].append(str(exc))
        except OSError as exc:
            result['errors'].append(str(exc))
        self._last_maintenance = 0.0
        return result

    @staticmethod
    def key(source_sha256, recipe):
        canonical = json.dumps({'schema': 1, 'source_sha256': source_sha256, 'recipe': recipe},
                               sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        return hashlib.sha256(canonical).hexdigest()

    def _path(self, key):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid artifact key')
        return self.root / key[:2] / (key + '.hdds')

    def _metadata(self, stream, key):
        if stream.read(8) != _MAGIC:
            raise ValueError('Invalid artifact signature')
        length_bytes = stream.read(4)
        if len(length_bytes) != 4:
            raise ValueError('Truncated artifact')
        length = struct.unpack('<I', length_bytes)[0]
        if not 0 < length <= 4096:
            raise ValueError('Invalid artifact metadata')
        meta = json.loads(stream.read(length).decode('ascii'))
        size = meta['size']
        if meta['key'] != key or not isinstance(size, int) or not 128 <= size <= self.max_bytes:
            raise ValueError('Invalid artifact metadata')
        if os.fstat(stream.fileno()).st_size - stream.tell() != size:
            raise ValueError('Truncated or oversized artifact')
        return meta

    def _touch(self, path):
        try:
            now = time.time()
            if now - path.stat().st_mtime > 60:
                os.utime(path, (now, now))
        except OSError:
            pass

    def get(self, key):
        """Verified DDS bytes, or None. Suitable for an in-memory encoder API."""
        if not self.enabled:
            return None
        try:
            path = self._path(key)
            with path.open('rb') as stream:
                meta = self._metadata(stream, key)
                data = stream.read(meta['size'] + 1)
            if len(data) != meta['size'] or not _valid_dds(data[:148], len(data)):
                return None
            if hashlib.sha256(data).hexdigest() != meta['sha256']:
                return None
            self._touch(path)
            return data
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return None

    def restore(self, key, target, validator=None):
        """Stream, verify, then atomically replace the caller-approved target."""
        if not self.enabled:
            return False
        temporary = None
        try:
            path = self._path(key)
            target = Path(target)
            with path.open('rb') as source:
                meta = self._metadata(source, key)
                target.parent.mkdir(parents=True, exist_ok=True)
                handle, temporary = tempfile.mkstemp(prefix='.herta_cache_', suffix='.dds', dir=target.parent)
                digest = hashlib.sha256()
                remaining = meta['size']
                header = b''
                with os.fdopen(handle, 'wb') as output:
                    while remaining:
                        block = source.read(min(remaining, _CHUNK))
                        if not block:
                            return False
                        if not header:
                            header = block[:148]
                        digest.update(block)
                        output.write(block)
                        remaining -= len(block)
                if source.read(1) or digest.hexdigest() != meta['sha256']:
                    return False
            if not _valid_dds(header, meta['size']) or (validator is not None and not validator(temporary)):
                return False
            os.replace(temporary, target)
            temporary = None
            self._touch(path)
            return True
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def put(self, key, data):
        """Publish a complete DDS using an isolated temp file + atomic replace."""
        if not self.enabled or not _valid_dds(data[:148], len(data)) or len(data) > self.max_bytes:
            return False
        temporary = None
        try:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            metadata = json.dumps({'key': key, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                  separators=(',', ':')).encode('ascii')
            handle, temporary = tempfile.mkstemp(prefix='.pending_', dir=path.parent)
            with os.fdopen(handle, 'wb') as output:
                output.write(_MAGIC)
                output.write(struct.pack('<I', len(metadata)))
                output.write(metadata)
                output.write(data)
            os.replace(temporary, path)
            temporary = None
            self._maintain()
            return True
        except (OSError, ValueError, TypeError, OverflowError):
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def put_file(self, key, path):
        try:
            if os.path.getsize(path) > self.max_bytes:
                return False
            return self.put(key, Path(path).read_bytes())
        except OSError:
            return False

    def _maintain(self):
        # Amortize directory walks; quota is a soft budget between cleanups.
        if time.monotonic() - self._last_maintenance < 30:
            return
        with self._maintenance_lock:
            if time.monotonic() - self._last_maintenance < 30:
                return
            self._last_maintenance = time.monotonic()
            try:
                entries = []
                for path in self.root.glob('[0-9a-f][0-9a-f]/*.hdds'):
                    stat = path.stat()
                    entries.append((stat.st_mtime, stat.st_size, path))
                total = sum(size for _, size, _ in entries)
                for _, size, path in sorted(entries):
                    if total <= self.max_bytes:
                        break
                    try:
                        path.unlink()
                        total -= size
                    except OSError:
                        pass
            except OSError:
                pass
