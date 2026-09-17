"""幂等写文件：内容相同就不碰目标文件。

背景（2026-09-16 实测）
------------------------------------------------------------------

3DMigoto 的自定义着色器编译缓存**按"文件修改时间"配对**，不是按内容哈希：

``DirectX11/CommandList.cpp`` ``CustomShader::compile()`` 的写法是

    swprintf_s(cache_path, MAX_PATH, L"%.*s.%S.%x.bin",
               (int)(ext - wpath), wpath, shaderModel, (UINT)compile_flags);
    GetFileTime(f, NULL, NULL, &timestamp);          // .hlsl 的最后写入时间
    if (load_cached_shader(timestamp, cache_path, ppBytecode))
            return false;                            // 命中 → 完全不编译

而 ``load_cached_shader()`` 的命中条件只有一个：

    CompareFileTime(&hlsl_timestamp, &cache_timestamp) != 0  →  丢弃缓存

也就是说 **``.bin`` 与 ``.hlsl`` 的 mtime 必须精确相等**才算同一代。写缓存时
3DMigoto 会 ``set_file_last_write_time(cache_path, &timestamp)`` 把两者对齐。

后果：只要导出流程在**内容没有任何变化**的情况下重写一次 ``.hlsl``，它就会把
mtime 刷新成"现在"，于是已经对齐好的 ``.bin`` 立刻变成"上一代"，下次进游戏 /
按 F10 就整族重编译（实测单次两分钟量级，``optimization_level3`` + 大 CS）。

实测依据：用户 ``Mods/SSMTGeneratedMod/克拉蕾/res/drag_interaction`` 下，
被 ``open(dest, 'w')`` 重写的着色器 mtime 是导出时刻（``2026-09-16 21:23:17``），
而走 ``shutil.copy2`` 的着色器保留了工具目录模板的原始 mtime
（``2026-08-06 02:37:01``）——受影响的 ``[CustomShader]`` 段共 20 个。

本模块的契约
------------------------------------------------------------------

**内容相同 → 一个字节都不写。目标 mtime 只有两种可能：分毫不动，或被精确地
对齐成源的 mtime（:func:`copy_file_if_changed` 的"同内容回正"，见下）——绝不会
被刷新成"现在"。**

（早前版本这里写的是"mtime 一个纳秒都不动"，与 :func:`copy_file_if_changed` 命中
路径会主动 :func:`sync_mtime` 的行为相冲突，故按实现改写；对齐本身是"往源模板的
时间戳上收"，不会制造新的失配。）

对外函数：

- :func:`write_text_if_changed` / :func:`write_bytes_if_changed` —— 内容不同才
  原地写入。用于着色器源码、ini 这类小文件。
- :func:`write_bytes_if_changed_atomic` —— 同上，但内容不同时走
  "同目录临时文件 + ``os.fsync`` + ``os.replace``"，用于"整块替换、不允许
  半截"的产物。
- :func:`copy_file_if_changed` —— 字节级复制；内容相同则不复制，并把目标
  mtime **纳秒级精确**地对齐成源的（这样重复导出的目标 mtime 恒定等于工具目录
  模板的 mtime，既不会漂移、也不会只差一个 tick）。
- :func:`sync_mtime` —— 单独对齐两个文件的 mtime（``st_mtime_ns`` +
  ``os.utime(ns=...)``，纳秒级）。

全部返回"是否真的写了"，调用方可以据此统计/汇报。

关于"原地写"与"原子替换"的取舍：``os.replace`` 必然给目标换上新的 mtime，
所以**命中路径绝不能走原子替换**（那正是要消灭的行为）。命中判定统一放在
函数入口用内容比较完成，只有确认内容不同才落到写盘路径。
判定用内容而不是时间戳：时间戳会被复制、解压、同步工具改写，内容不会。
"""

import os
import shutil
import tempfile

__all__ = [
	"write_text_if_changed",
	"write_bytes_if_changed",
	"write_bytes_if_changed_atomic",
	"copy_file_if_changed",
	"sync_mtime",
]


def _coerce_bytes(payload):
	"""校验并归一成 ``bytes``（``bytearray``/``memoryview`` 都接受）。"""
	if not isinstance(payload, (bytes, bytearray, memoryview)):
		raise TypeError("payload must be bytes-like")
	return bytes(payload)


def _read_bytes(path):
	"""读整个文件；不存在或读不动时返回 None（调用方按"需要写"处理）。"""
	try:
		with open(path, "rb") as file_obj:
			return file_obj.read()
	except OSError:
		return None


def _normalize_newlines(text):
	"""把三种行尾统一成 ``\\n``，供"逻辑内容是否相同"判定使用。"""
	if "\r" not in text:
		return text
	return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text_if_changed(path, text, encoding="utf-8", newline=None):
	"""``text`` 与目标文件内容相同则不写，返回 False；确实写了返回 True。

	``newline`` 默认 ``None`` = 沿用 ``open(path, "w")`` / ``Path.write_text()``
	的平台换行语义，替换调用点时**不会改动磁盘上的换行**（Windows 下 ``\\n``
	照旧落成 ``\\r\\n``）。

	**为什么比较要归一化行尾**：Windows 上写出的文件是 CRLF，而调用方手里的
	``text`` 是 LF。若直接拿"``text`` 编码后的字节"与磁盘字节比，CRLF 文件会
	被永远判成"内容变了"，幂等形同虚设（已实测）。因此这里比的是**归一化行尾
	后的逻辑内容**：行尾差异不算内容变化（对 HLSL 语义无影响），非行尾差异
	照常判定为变化。

	代价是"纯行尾变化"不会被写盘。这是刻意取舍：本模块的职责是消灭
	"内容没变却重写"，而不是替调用方规范化换行——后者会改动摇滚了许久的
	产物字节，属于另一个决定。
	"""
	existing = _read_bytes(path)
	if existing is not None:
		try:
			current = existing.decode(encoding)
		except UnicodeDecodeError:
			current = None
		if current is not None and _normalize_newlines(current) == _normalize_newlines(text):
			return False

	parent = os.path.dirname(os.path.abspath(path))
	if parent:
		os.makedirs(parent, exist_ok=True)
	with open(path, "w", encoding=encoding, newline=newline) as file_obj:
		file_obj.write(text)
	return True


def write_bytes_if_changed(path, payload):
	"""``payload`` 与目标文件内容相同则不写，返回 False；确实写了返回 True。"""
	payload = _coerce_bytes(payload)
	existing = _read_bytes(path)
	if existing is not None and existing == payload:
		return False

	parent = os.path.dirname(os.path.abspath(path))
	if parent:
		os.makedirs(parent, exist_ok=True)
	with open(path, "wb") as file_obj:
		file_obj.write(payload)
	return True


def write_bytes_if_changed_atomic(path, payload):
	"""同 :func:`write_bytes_if_changed`，但**先比较、再原子发布**。

	内容相同直接返回 False（不碰文件、不动 mtime）；内容不同则走
	"同目录临时文件 + ``os.fsync`` + ``os.replace``"，保证中途失败时磁盘上
	要么是旧产物、要么是新产物，不会留下半截文件。

	适合发布着色器这类"整块替换、不允许半截"的产物。
	"""
	payload = _coerce_bytes(payload)
	existing = _read_bytes(path)
	if existing is not None and existing == payload:
		return False

	directory = os.path.dirname(os.path.abspath(path))
	if directory:
		os.makedirs(directory, exist_ok=True)
	fd, temp_path = tempfile.mkstemp(
		prefix=f".{os.path.basename(path)}.",
		suffix=".tmp",
		dir=directory or None,
	)
	try:
		with os.fdopen(fd, "wb") as temp_file:
			temp_file.write(payload)
			temp_file.flush()
			os.fsync(temp_file.fileno())
		os.replace(temp_path, path)
	except Exception as exc:
		raise RuntimeError(f"原子发布文件失败 {path}: {exc}") from exc
	finally:
		if os.path.exists(temp_path):
			try:
				os.remove(temp_path)
			except OSError:
				pass
	return True


def copy_file_if_changed(source, destination, adopt_source_mtime=True):
	"""把 ``source`` 复制到 ``destination``，内容相同则不动目标。

	同内容时用 ``os.utime`` 把目标的 mtime **纳秒级精确**地对齐成源的 mtime
	（``adopt_source_mtime``；精度细节见 :func:`sync_mtime`），这样重复导出的目标
	mtime 恒定等于工具目录模板的 mtime，3DMigoto 侧的 ``.bin`` 缓存不会逐次失效，
	也不会因为"第 1 次复制精确、第 2 次对齐取整"而多失效一次。

	注意：这里的 mtime 对齐是**保守**选择，不是把目标"修复"成缓存认的时间。
	真正的命中判定交给 3DMigoto：「同源必然同内容，同内容必然同时间戳」。
	若源模板本身被改动，则内容不同 → 正常复制 → 缓存理应失效重编译。

	返回是否真的复制了。
	"""
	expected = _read_bytes(source)
	if expected is None:
		raise FileNotFoundError(source)

	existing = _read_bytes(destination)
	if existing is not None and existing == expected:
		if adopt_source_mtime:
			sync_mtime(source, destination)
		return False

	parent = os.path.dirname(os.path.abspath(destination))
	if parent:
		os.makedirs(parent, exist_ok=True)
	# 用 copy2 而非 copyfile：保留源的时间戳（没有这一步，内容相同时也会换掉
	# 目标 mtime，正是本模块要消灭的行为）。
	shutil.copy2(source, destination)
	return True


def sync_mtime(source, destination):
	"""把 ``destination`` 的 mtime/atime **纳秒级精确**地对齐成 ``source`` 的。

	用 ``st_mtime_ns`` / ``st_atime_ns`` 读取、``os.utime(ns=...)`` 写入，而不是
	``stat().st_mtime`` + ``os.utime((atime, mtime))`` 的浮点秒写法：float64 在
	当前纪元（~1.75e9 s）的分辨率约 2.4e-7 s，**大于 NTFS 的 100ns tick**，浮点
	往返会把 mtime 搬到相邻 tick（实测 0 ~ -200ns）。

	这个偏差在 :func:`copy_file_if_changed` 上表现为"自己和自己不一致"：
	第一次复制（MISS）走 ``shutil.copy2`` 保留源时间戳（精确），第二次起（HIT）
	走本函数对齐——若本函数取整，目标的 mtime 就会在两次导出之间**变化一个
	tick**，于是 3DMigoto 已对齐好的 ``.bin`` 缓存被判成"上一代"，白白多一次
	整族重编译。因此这里必须精确：`同源 → 同 mtime`（``CompareFileTime`` 精确
	相等）不成立的话，"内容没变就不重编译"这条契约就漏了一个洞。
	"""
	try:
		stat = os.stat(source)
	except OSError:
		return False
	try:
		os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
	except OSError:
		return False
	return True
