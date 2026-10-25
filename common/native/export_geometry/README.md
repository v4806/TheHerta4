# Lossless export geometry kernels

`../export_geometry.cpp` implements ABI 1 in the adjacent
`herta_export_geometry_windows_x64.dll`. It requires Windows x64, an MSVC runtime,
and no CPU extension beyond the x64 SSE2 baseline. Build out of tree with CMake
and an x64 MSVC C++20 toolchain; retain `/fp:precise`. Never add fast-math flags.

The Python adapter is `common/export_geometry_native.py`. Blender RNA is read
once on the caller's main thread. The owned CSR columns then go to native
sorting, selection, normalization and loss diagnostics (two threads for large
meshes). It preserves descending weight / ascending group tie ordering, rigid
fallback, invalid-loop zero rows, float32 results and the diagnostic samples.
The compensated sum matches the supported Python 3.12+ reference; older Python
uses the existing weight implementation.

`herta_stable_unique` hashes complete prebuilt byte keys, checks collisions by
byte comparison, and returns first-occurrence indices plus the complete inverse
mapping. The caller still constructs the same seam/VertexId/active-column keys.
No key rule is moved or weakened. Missing/disabled native code retains NumPy.

Disable all kernels with `HERTA_EXPORT_GEOMETRY_NATIVE=0`, or only row hashing
with `HERTA_NATIVE_VERTEX_HASH=0`. The backend never reads or writes a disk cache.

On the validated production mesh: 323,996 vertices / 1,227,083 weight records,
weight extraction plus diagnostics 4.90 -> 1.12 seconds; 1,793,634 keys of 104
bytes, stable uniqueness 2.52 -> 0.18 seconds. These are per-kernel comparisons,
not full-export speed ratios. All ten production mesh groups matched the Python
reference weight/index bytes, statistics, first indices and inverse mappings.
