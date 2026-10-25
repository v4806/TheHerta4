# Geometry hash pack accelerator

`herta_geometry_windows_x64.dll` batches the original hash weight records from
contiguous count/group/float32 arrays. `geometry_pack.c` is the matching source,
GPL-3.0, ABI 1. It preserves record order and all 32-bit payload bits. The x64
build has no CRT/third-party DLL dependencies or internal threads.

The Python loader is `common/geometry_hash.py`. Blender access remains on the
main thread; ctypes packing and hashlib run in independent worker jobs.
Missing/unsupported DLLs fall back to equivalent batched struct packing.
`HERTA_GEOMETRY_NATIVE=0` disables only this native backend.

Benchmark, tests and local build recipe are preserved in the standalone engine
repository at `experiments/geometry_pack/`.
