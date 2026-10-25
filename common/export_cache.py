"""Shared export-cache policy and UI management; worker calls never read bpy."""
import os
import threading
import time

_enabled = True
_stats_snapshot = None
_stats_at = 0.0
_stats_key = None


def set_export_cache_enabled(value):
    global _enabled
    _enabled = bool(value)


def _scene_cache_enabled():
    try:
        from .global_properties import GlobalProterties
        instance = getattr(GlobalProterties, '_instance', None)
        if instance is not None:
            return bool(instance().enable_preprocess_cache)
        return bool(GlobalProterties.enable_export_cache())
    except (ImportError, AttributeError, RuntimeError):
        return None


def is_export_cache_enabled():
    # Read scene settings only on Blender's main thread. Every export snapshots
    # this before launching native/analysis jobs; the RNA update keeps it fresh.
    if threading.current_thread() is threading.main_thread():
        value = _scene_cache_enabled()
        if value is not None:
            set_export_cache_enabled(value)
    return _enabled


def invalidate_export_cache_stats():
    global _stats_snapshot, _stats_at
    _stats_snapshot, _stats_at = None, 0.0


def get_export_cache_stats(force=False):
    global _stats_snapshot, _stats_at, _stats_key
    from ..blueprint.preprocess_cache import PreProcessCache
    from . import texture_dedup
    from ..toolkit.dds_artifact_cache import DDSArtifactCache
    artifact = DDSArtifactCache()
    key = (PreProcessCache.get_cache_dir(), str(artifact.root), texture_dedup._analysis_cache_path())
    now = time.monotonic()
    if not force and _stats_snapshot is not None and key == _stats_key and now - _stats_at < 2.0:
        return _stats_snapshot
    preprocess = PreProcessCache.get_cache_stats()
    analysis = {'file_count': 0, 'total_size': 0}
    for path in texture_dedup.analysis_cache_files():
        try:
            analysis['total_size'] += os.path.getsize(path)
            analysis['file_count'] += 1
        except OSError:
            pass
    encoded = artifact.get_stats()
    result = {'preprocess': preprocess, 'analysis': analysis, 'dds': encoded}
    result['total'] = {field: sum(item[field] for item in result.values())
                       for field in ('file_count', 'total_size')}
    _stats_snapshot, _stats_at, _stats_key = result, now, key
    return result


def clear_export_caches():
    """Clear current project's preprocessing and local shared texture caches."""
    from ..blueprint.preprocess_cache import PreProcessCache
    from . import texture_dedup
    from ..toolkit.dds_artifact_cache import DDSArtifactCache
    result = {'preprocess': PreProcessCache.clear_cache(), 'analysis': 0, 'dds': 0, 'errors': []}
    PreProcessCache.invalidate_index_cache()
    with texture_dedup._FEATURE_CACHE_LOCK:
        for path in texture_dedup.analysis_cache_files():
            try:
                os.remove(path)
                result['analysis'] += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                result['errors'].append(str(exc))
    encoded = DDSArtifactCache().clear()
    result['dds'] = encoded['file_count']
    result['errors'].extend(encoded['errors'])
    result['total'] = result['preprocess'] + result['analysis'] + result['dds']
    invalidate_export_cache_stats()
    return result
