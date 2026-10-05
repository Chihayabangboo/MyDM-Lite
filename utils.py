"""MyDM-Lite 工具模块。

这里只放**纯函数**和“跟界面无关”的小工具，方便单独做单元测试：

* 文件名解析（Content-Disposition / RFC 5987 / URL 路径 / 默认名）
* 重名自动加 (1)、(2) 后缀
* 大小格式化（B/KB/MB/GB）
* 剩余时间格式化
* 默认“下载”目录获取（不存在就创建，创建失败回退用户主目录）
* 跨平台“打开文件夹并选中文件”
* 日志初始化（5MB 轮转，最多保留 3 个日志文件）
* 断点续传元数据（``<文件名>.download.json``）的原子读写与校验
* 小配置（``~/.mydm_lite_config.json``）的原子读写与“保存目录是否可用”校验
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_FILENAME = "download.bin"  # 完全解析不出文件名时的兜底名字
LOG_FILE_NAME = "mydm.log"  # 日志文件名
LOG_MAX_BYTES = 5 * 1024 * 1024  # 单个日志文件最大 5MB，超过就轮转
LOG_BACKUP_COUNT = 3  # 最多保留最近 3 个日志文件
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

CONFIG_FILE_NAME = ".mydm_lite_config.json"  # 配置放在用户主目录下
CONFIG_VERSION = 1
CONFIG_KEY_LAST_SAVE_DIR = "last_save_dir"  # “上次保存到”的目录

# Windows 文件名里不允许出现的字符（含控制字符）
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# .part0 / .part1 / ... 这种临时分块文件
_PART_SUFFIX_RE = re.compile(r"\.part\d+\Z")

# Windows 下不能用作文件名的保留设备名
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 判断一个字符串是不是 http/https 链接
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_LOGGER_INITIALIZED = False

__all__ = [
    "DEFAULT_FILENAME",
    "LOG_FILE_NAME",
    "LOG_MAX_BYTES",
    "LOG_BACKUP_COUNT",
    "CONFIG_FILE_NAME",
    "CONFIG_VERSION",
    "CONFIG_KEY_LAST_SAVE_DIR",
    "get_config_path",
    "load_config",
    "save_config",
    "is_usable_save_dir",
    "resolve_save_dir",
    "parse_content_disposition_filename",
    "filename_from_url",
    "resolve_download_filename",
    "sanitize_filename",
    "unique_path",
    "format_size",
    "format_eta",
    "get_default_download_dir",
    "ensure_dir",
    "open_folder",
    "reveal_in_folder",
    "extract_first_url",
    "cleanup_part_files",
    "part_file_path",
    "metadata_file_path",
    "read_metadata",
    "write_metadata",
    "remove_metadata",
    "build_metadata",
    "inspect_part_files",
    "has_resumable_state",
    "load_resume_state",
    "ResumeState",
    "setup_logging",
]


# ---------------------------------------------------------------------------
# 文件名解析
# ---------------------------------------------------------------------------


def _get_header(headers, name: str) -> Optional[str]:
    """从 requests 的 headers / 普通 dict 里**大小写不敏感**地取一个头。

    没有这个头或者 headers 为 None 时返回 None，绝不抛异常。
    """
    if not headers:
        return None
    try:
        value = headers.get(name)
    except Exception:  # pragma: no cover - 极端情况下 headers 不是 mapping
        return None
    if value is not None:
        return value
    try:
        lowered = name.lower()
        for key, val in headers.items():
            if str(key).lower() == lowered:
                return val
    except Exception:  # pragma: no cover
        return None
    return None


def parse_content_disposition_filename(header_value: Optional[str]) -> Optional[str]:
    """解析 Content-Disposition 里的文件名。

    支持两种写法，优先 RFC 5987 的 ``filename*``：

    * ``attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.zip`` → ``中文.zip``
    * ``attachment; filename="report.pdf"`` → ``report.pdf``

    解析不出来返回 None（调用方继续用 URL 兜底）。
    """
    if not header_value:
        return None
    text = str(header_value)

    # 1) 优先 RFC 5987：filename*=UTF-8''%E4%B8%AD%E6%96%87.zip
    match = re.search(r"filename\*\s*=\s*([^\s;]+)", text, re.IGNORECASE)
    if match:
        raw = match.group(1).strip().strip('"')
        # 形如 charset'lang'percent-encoded-value，取最后一个单引号之后的部分
        if "'" in raw:
            raw = raw.split("'")[-1]
        if raw:
            name = unquote(raw)
            if name.strip():
                return name

    # 2) 普通 filename="xxx" / filename=xxx（也兼容单引号写法）
    match = re.search(r'filename\s*=\s*"([^"]*)"', text, re.IGNORECASE)
    if match and match.group(1).strip():
        return unquote(match.group(1))
    match = re.search(r"filename\s*=\s*'([^']*)'", text, re.IGNORECASE)
    if match and match.group(1).strip():
        return unquote(match.group(1))
    match = re.search(r"filename\s*=\s*([^;\s]+)", text, re.IGNORECASE)
    if match and match.group(1).strip().strip('"'):
        return unquote(match.group(1).strip().strip('"'))
    return None


def filename_from_url(url: Optional[str]) -> Optional[str]:
    """从 URL 路径里猜文件名（会做 unquote，支持中文百分号编码）。"""
    if not url or not isinstance(url, str):
        return None
    try:
        path = urlparse(url.strip()).path
    except Exception:  # pragma: no cover
        return None
    if not path:
        return None
    name = unquote(path.rstrip("/").split("/")[-1])
    name = name.strip()
    return name or None


def resolve_download_filename(
    url: Optional[str] = None,
    headers=None,
    content_disposition: Optional[str] = None,
) -> str:
    """按优先级决定最终文件名：Content-Disposition → URL 路径 → download.bin。

    ``headers`` 可以直接传 requests 的响应头（会自动找 Content-Disposition），
    也可以显式传 ``content_disposition``。
    """
    if content_disposition is None:
        content_disposition = _get_header(headers, "Content-Disposition")

    candidates = [
        parse_content_disposition_filename(content_disposition),
        filename_from_url(url),
    ]
    for candidate in candidates:
        cleaned = sanitize_filename(candidate)
        if cleaned:
            return cleaned
    return DEFAULT_FILENAME


def sanitize_filename(name: Optional[str]) -> str:
    """去掉文件名里非法的字符，返回安全的文件名（可能为空字符串）。"""
    if not name:
        return ""
    # 兼容 Windows 上传来的 C:\path\to\file.zip 这种完整路径
    name = str(name).replace("\\", "/").split("/")[-1]
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip().strip(".")
    if not name:
        return ""
    # 防止 Windows 保留设备名导致保存失败
    stem = name.split(".")[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        name = "_" + name
    return name


def unique_path(path: Path) -> Path:
    """如果文件已存在，自动加 (1)、(2)、(3) 后缀。"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def format_size(num_bytes) -> str:
    """把字节数格式化成人类可读的 B/KB/MB/GB 字符串。"""
    if num_bytes is None:
        return "--"
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "--"
    if value < 0:
        return "--"
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"  # pragma: no cover - 逻辑上不可达


def format_eta(seconds) -> str:
    """把剩余秒数格式化成“1小时02分03秒 / 05分10秒 / 12秒”这种中文写法。"""
    if seconds is None:
        return "--"
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "--"
    if total < 0:
        return "--"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes:02d}分{secs:02d}秒"
    return f"{secs}秒"


# ---------------------------------------------------------------------------
# 目录 / 路径
# ---------------------------------------------------------------------------


def get_default_download_dir() -> Path:
    """返回默认的“下载”目录，不存在就自动创建，创建失败回退用户主目录。

    优先看 ``XDG_DOWNLOAD_DIR``（Linux 桌面环境会设置它），
    其次用 ``USERPROFILE``/``HOME`` 拼 ``Downloads``（Windows 和 macOS/Linux 都一样）；
    全部失败就用 ``Path.home()``。
    """
    home = _home_dir()
    candidates = []

    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        candidates.append(Path(xdg))
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Downloads")
    candidates.append(home / "Downloads")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if candidate.is_dir():
                return candidate
        except Exception:
            continue
    # 连下载目录都建不出来，就退回用户主目录，保证程序还能用
    return home


def _home_dir() -> Path:
    """尽量稳地拿到用户主目录（Path.home() 出错时用环境变量兜底）。"""
    try:
        return Path.home()
    except Exception:  # pragma: no cover
        for key in ("USERPROFILE", "HOME"):
            value = os.environ.get(key)
            if value:
                return Path(value)
        return Path(os.getcwd())  # pragma: no cover


def ensure_dir(path) -> Path:
    """确保目录存在（已存在不报错），返回 Path 对象。"""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# 小配置（记忆上次保存路径）
# ---------------------------------------------------------------------------
#
# 设计要点：
# * 配置文件固定放用户主目录下的 ``.mydm_lite_config.json``（``get_config_path()``）；
# * 读配置**绝不抛异常**：文件不存在 / 不是合法 JSON / 内容不是对象 → 返回 ``{}``，
#   界面据此静默回退到默认下载目录，绝不给用户弹报错；
# * 写配置是原子的（先写 ``*.tmp`` 再 ``os.replace``），写一半崩溃也不会留下坏文件；
# * 所有函数都支持 ``path`` 参数注入，测试可以指定临时路径，
#   保证测试不会往真实用户主目录里写任何东西。


def get_config_path() -> Path:
    """配置文件路径：用户主目录下的 ``.mydm_lite_config.json``。

    ``Path.home()`` 出问题时用 ``USERPROFILE``/``HOME`` 兜底（见 ``_home_dir``），
    保证任何环境下都能拿到一个路径而不抛异常。
    """
    return _home_dir() / CONFIG_FILE_NAME


def load_config(path=None) -> dict:
    """读取配置，返回 dict。

    只在“文件存在且是合法 JSON 对象”时返回内容；其它情况（不存在、损坏、
    是数组/字符串、没权限读）一律静默返回空 dict，由调用方回退到默认值。
    """
    config_path = _config_path_from(path)
    try:
        if not config_path.is_file():
            return {}
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_config(config_dict, path=None) -> bool:
    """原子写入配置：先写临时文件，再 ``os.replace`` 覆盖，成功返回 True。

    写入内容一定是 ``dict``（不是 dict 就当成空配置）。
    写失败（没权限 / 磁盘满）只返回 False 并清掉临时文件，绝不抛异常——
    记不住“上次保存路径”最多是下次回到默认目录，不能让主流程崩掉。
    """
    config_path = _config_path_from(path)
    payload = dict(config_dict) if isinstance(config_dict, dict) else {}
    payload.setdefault("version", CONFIG_VERSION)

    temp_path = config_path.with_name(config_path.name + ".tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except Exception:  # pragma: no cover - 某些文件系统不支持
                pass
        os.replace(temp_path, config_path)  # 原子替换，避免半截 JSON
        return True
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        return False


def _config_path_from(path) -> Path:
    """把 ``path`` 参数（None / str / Path）统一成配置文件 Path。"""
    if path is None:
        return get_config_path()
    try:
        return Path(path)
    except Exception:  # pragma: no cover - 非法类型兜底
        return get_config_path()


def is_usable_save_dir(path) -> bool:
    """判断一个保存目录是否可用：存在、是目录、而且**可写**。

    ``os.access(path, os.W_OK)`` 挡不住所有情况（Windows 上 ACL 拒绝时
    它仍可能返回 True），但能挡住最典型的“只读目录 / 被删掉的目录”，
    也是需求里明确要求的检查。
    """
    if path is None:
        return False
    try:
        target = Path(path)
    except Exception:  # pragma: no cover - 非法类型
        return False
    try:
        if not target.exists():
            return False
        if not target.is_dir():
            return False
    except OSError:  # pragma: no cover - 极端路径（超长 / 设备名）
        return False
    try:
        return bool(os.access(target, os.W_OK))
    except Exception:  # pragma: no cover
        return False


def resolve_save_dir(config: Optional[dict] = None, default=None):
    """从配置里取“上次保存路径”，不可用时回退到默认下载目录。

    * 回退**复用** ``get_default_download_dir()``，不重复实现默认目录逻辑，
      避免两处不一致；
    * 校验同时要求 ``os.path.exists`` 和 ``os.access(W_OK)``（见 ``is_usable_save_dir``）；
    * 配置缺失 / 损坏（``config`` 不是 dict）同样静默回退，绝不抛异常。

    返回值是 ``Path``：配置里有效时是配置里的目录，否则是默认下载目录。
    """
    if default is not None:
        fallback = Path(default)
    else:
        fallback = get_default_download_dir()

    if not isinstance(config, dict):
        return fallback
    saved = config.get(CONFIG_KEY_LAST_SAVE_DIR)
    if not saved or not isinstance(saved, str):
        return fallback
    if is_usable_save_dir(saved):
        try:
            return Path(saved)
        except Exception:  # pragma: no cover
            return fallback
    return fallback


# ---------------------------------------------------------------------------
# 跨平台“打开文件夹并选中文件”
# ---------------------------------------------------------------------------


def _spawn_detached(args) -> bool:
    """启动一个跟本程序无关的独立进程（用来打开资源管理器），成功返回 True。"""
    kwargs = {}
    if os.name == "nt":  # pragma: no cover - 只在 Windows 走到
        kwargs["startupinfo"] = subprocess.STARTUPINFO()
        kwargs["close_fds"] = True
    try:
        subprocess.Popen(args, **kwargs)  # noqa: S603 - 参数都是本地路径，用户可控范围有限
        return True
    except Exception:
        return False


def open_folder(path) -> bool:
    """打开一个文件夹（不选中任何文件），跨平台，失败返回 False。"""
    target = Path(path)
    directory = target if target.is_dir() else target.parent
    opened = False

    if sys.platform.startswith("win"):
        # Windows 新建进程无法用 explorer 直接弹目录，用 os.startfile 最稳
        if hasattr(os, "startfile"):
            try:
                os.startfile(str(directory))  # noqa: S606 - 本地目录
                opened = True
            except Exception:
                opened = False
    elif sys.platform == "darwin":
        opened = _spawn_detached(["open", str(directory)])
    else:
        opened = _spawn_detached(["xdg-open", str(directory)])

    if not opened:  # 兜底：换一种方式再试一次，仍然失败也绝不抛异常
        opened = _spawn_detached(
            ["explorer", str(directory)] if sys.platform.startswith("win") else ["xdg-open", str(directory)]
        )
    return opened


def reveal_in_folder(file_path) -> bool:
    """打开所在文件夹并**选中**该文件，跨平台，失败时只打开目录，不报错。

    * Windows：``explorer /select,"文件路径"``
    * macOS：``open -R "文件路径"``
    * Linux：``xdg-open "所在目录"``（Linux 没有统一的高亮选中方式）
    """
    target = Path(file_path)
    parent = target.parent if target.parent != Path("") else Path(".")

    if sys.platform.startswith("win"):
        # 注意 /select, 和路径之间不能有空格，否则 explorer 会当成普通目录打开
        if _spawn_detached(["explorer", f"/select,{target}"]):
            return True
    elif sys.platform == "darwin":
        if _spawn_detached(["open", "-R", str(target)]):
            return True
    else:
        if _spawn_detached(["xdg-open", str(parent)]):
            return True

    return open_folder(parent)


# ---------------------------------------------------------------------------
# 剪贴板文本里找链接
# ---------------------------------------------------------------------------


def extract_first_url(text: Optional[str]) -> Optional[str]:
    """从一段文本里挑出第一个 http/https 链接，没有则返回 None。"""
    if not text or not isinstance(text, str):
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;)]}\u3002")


# ---------------------------------------------------------------------------
# 分块临时文件
# ---------------------------------------------------------------------------


def part_file_path(target, index: int) -> Path:
    """返回第 index 个分块文件的路径：``xxx.zip.part0``。"""
    target = Path(target)
    return target.with_name(f"{target.name}.part{index}")


def cleanup_part_files(target, keep_indexes=None) -> int:
    """删除目标文件对应的所有 ``.partN`` 临时文件，返回删掉的个数。

    ``keep_indexes`` 里的编号会被保留（正常用不到，留给调试）。
    """
    target = Path(target)
    keep = set(keep_indexes or ())
    removed = 0
    if not target.parent.exists():
        return 0
    for entry in target.parent.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.startswith(target.name + ".part"):
            continue
        suffix = name[len(target.name):]
        if not _PART_SUFFIX_RE.match(suffix):
            continue
        index_text = suffix[len(".part"):]
        if index_text.isdigit() and int(index_text) in keep:
            continue
        try:
            entry.unlink()
            removed += 1
        except Exception:
            # 文件被占用 / 没权限，静默跳过，不打断下载流程
            continue
    return removed


# ---------------------------------------------------------------------------
# 断点续传元数据（<文件名>.download.json）
# ---------------------------------------------------------------------------
#
# 设计要点：
# * 元数据只记“事实”：URL、总大小、分块布局、每个 .partN 的字节数、线程数、时间戳；
#   恢复时真正信的是**磁盘上 .partN 的实际大小**，元数据只是对照表，
#   所以即使某个 .partN 被截断也不影响正确性（缺多少补多少）。
# * 写入必须是原子的（先写 .tmp 再 os.replace），否则程序中途挂掉会留下半截 JSON。
# * 所有读取函数都“绝不抛异常”：元数据坏了就当成没有断点，从头下载，绝不让用户看到报错。


METADATA_SUFFIX = ".download.json"
METADATA_VERSION = 1

# 合法分块区间的终点上限：任何 end 超过它的元数据都是坏的/是“大小未知”的占位值，
# 一律拒绝续传（防止用哨兵值算出天文数字的区间长度）。
MAX_CHUNK_END = 2 ** 62 - 1


class ResumeState:
    """一次“可以续传”的完整状态：元数据 + 每个 .partN 的有效字节数。

    ``part_sizes`` 已经按分块区间裁剪过（不会超过该块应有的长度），
    所以上层可以直接拿它当“已下载字节数”用。
    """

    __slots__ = ("metadata", "part_sizes", "chunks", "total_size", "url")

    def __init__(self, metadata: dict, part_sizes: List[int], chunks: list,
                 total_size: int, url: str):
        self.metadata = metadata
        self.part_sizes = part_sizes
        self.chunks = chunks
        self.total_size = int(total_size)
        self.url = url or ""

    @property
    def downloaded_bytes(self) -> int:
        """已经落盘的字节总数（恢复后进度条要从这里接着涨）。"""
        return sum(self.part_sizes)

    def completed_indexes(self) -> List[int]:
        """已经下完的分块编号。"""
        return [
            int(chunk[0])
            for chunk, size in zip(self.chunks, self.part_sizes)
            if size >= int(chunk[2]) - int(chunk[1]) + 1
        ]


def metadata_file_path(target) -> Path:
    """返回目标文件对应的元数据路径：``xxx.zip.download.json``。"""
    target = Path(target)
    return target.with_name(f"{target.name}{METADATA_SUFFIX}")


def _now_text() -> str:
    """本地时间字符串，写进元数据方便排查。"""
    try:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # pragma: no cover - 理论上不会失败
        return ""


def build_metadata(
    url: str,
    target,
    total_size: Optional[int],
    chunks,
    part_sizes,
    threads: int,
    range_supported: bool = True,
    final_url: str = "",
) -> dict:
    """组装一份元数据字典（纯函数，方便单测直接断言结构）。"""
    target = Path(target)
    layout = []
    for chunk, size in zip(chunks, part_sizes):
        index, start, end = (int(chunk[0]), int(chunk[1]), int(chunk[2]))
        expected = end - start + 1
        downloaded = max(0, min(int(size), expected))
        layout.append({
            "index": index,
            "start": start,
            "end": end,
            "expected": expected,
            "downloaded": downloaded,
            "file": part_file_path(target, index).name,
            "done": downloaded >= expected,
        })

    return {
        "version": METADATA_VERSION,
        "url": url or "",
        "final_url": final_url or "",
        "target": target.name,
        "total_size": int(total_size) if total_size else None,
        "range_supported": bool(range_supported),
        "threads": max(1, int(threads)),
        "chunks": layout,
        "created_at": _now_text(),
        "updated_at": _now_text(),
    }


def write_metadata(target, metadata: dict) -> Optional[Path]:
    """原子写元数据：先写同名 ``.tmp`` 再 ``os.replace`` 覆盖。

    写失败（没权限 / 磁盘满）只返回 None，绝不打断下载主流程——
    丢一次检查点最多是下次少续一点，不能让整个下载失败。
    """
    path = metadata_file_path(target)
    payload = dict(metadata or {})
    payload.setdefault("version", METADATA_VERSION)
    payload["updated_at"] = _now_text()

    temp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except Exception:  # pragma: no cover - 某些文件系统不支持
                pass
        os.replace(temp_path, path)
        return path
    except Exception:
        # 清掉可能留下的半截临时文件，然后静默失败
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        return None


def read_metadata(target) -> Optional[dict]:
    """读取元数据；不存在 / 损坏 / 不是 JSON 对象时返回 None（当成没有断点）。"""
    path = metadata_file_path(target)
    try:
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    chunks = data.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None
    return data


def remove_metadata(target) -> bool:
    """删除元数据文件（合并成功后调用），顺便清掉可能的 .tmp 残留。"""
    removed = False
    path = metadata_file_path(target)
    for candidate in (path, path.with_name(path.name + ".tmp")):
        try:
            if candidate.exists():
                candidate.unlink()
                removed = True
        except Exception:
            # 文件被占用也不能影响“下载已完成”这个结果
            continue
    return removed


def inspect_part_files(target, chunks) -> List[int]:
    """读取每个 ``.partN`` 的实际大小，并按该块应有的长度裁剪。

    返回 ``[第0块已下载字节, 第1块已下载字节, ...]``；
    文件不存在算 0，超大（磁盘上有脏数据）也算“该块已满”，交给大小校验去发现。
    """
    target = Path(target)
    sizes: List[int] = []
    for chunk in chunks:
        index = int(chunk[0])
        expected = int(chunk[2]) - int(chunk[1]) + 1
        part = part_file_path(target, index)
        try:
            actual = part.stat().st_size if part.is_file() else 0
        except Exception:
            actual = 0
        sizes.append(max(0, min(int(actual), expected)))
    return sizes


def has_resumable_state(target) -> bool:
    """是否存在断点：元数据存在 **且** 至少有一个 .partN 还在。

    只有元数据、没有分块文件时不算断点（那种情况直接按新下载处理更干净）。
    """
    if read_metadata(target) is None:
        return False
    target = Path(target)
    try:
        for entry in target.parent.iterdir():
            if entry.is_file() and entry.name.startswith(target.name + ".part"):
                suffix = entry.name[len(target.name):]
                if _PART_SUFFIX_RE.match(suffix):
                    return True
    except Exception:
        return False
    return False


def load_resume_state(target, url: Optional[str] = None) -> Optional[ResumeState]:
    """读取并校验断点状态，不能续传时返回 None。

    校验规则（任何一条不满足就放弃断点，让上层从头下载）：
    * 元数据存在且分块布局合法（连续、不重叠、有 index/start/end）
    * 元数据里的 URL 跟本次要下载的 URL 一致（换链接了就不能混用旧分块）
    * 服务器支持 Range（不支持则断点续传没有意义）
    """
    target = Path(target)
    metadata = read_metadata(target)
    if metadata is None:
        return None

    saved_url = str(metadata.get("url") or "")
    if url and saved_url and saved_url != url:
        return None

    if not metadata.get("range_supported", True):
        return None

    raw_chunks = metadata.get("chunks") or []
    chunks: List[Tuple[int, int, int]] = []
    for item in raw_chunks:
        if not isinstance(item, dict):
            return None
        try:
            index = int(item.get("index"))
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (TypeError, ValueError):
            return None
        if index < 0 or start < 0 or end < start:
            return None
        if end > MAX_CHUNK_END:
            return None
        chunks.append((index, start, end))
    if not chunks:
        return None
    # 分块必须连续、不重叠、从 0 开始（元数据被改坏的话宁可不续传）
    for position, (index, start, end) in enumerate(chunks):
        if index != position:
            return None
        if position and start != chunks[position - 1][2] + 1:
            return None
    if chunks[0][1] != 0:
        return None

    raw_total = metadata.get("total_size")
    try:
        total_size = int(raw_total)
    except (TypeError, ValueError):
        return None
    if total_size <= 0 or total_size > MAX_CHUNK_END or chunks[-1][2] != total_size - 1:
        return None

    part_sizes = inspect_part_files(target, chunks)
    return ResumeState(metadata, part_sizes, chunks, total_size, saved_url)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def get_log_directory() -> Path:
    """日志目录：优先 MYDM_LOG_DIR，其次系统临时目录，最后当前目录。"""
    candidates = []
    env_dir = os.environ.get("MYDM_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    try:
        candidates.append(Path(tempfile.gettempdir()))
    except Exception:  # pragma: no cover
        pass
    candidates.append(Path.cwd())

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    return Path.cwd()  # pragma: no cover


def setup_logging(log_dir=None, level: int = logging.INFO, force: bool = False) -> logging.Logger:
    """初始化 mydm.log：5MB 轮转，最多保留 3 个日志文件。

    重复调用不会重复添加 handler（除非 force=True）。
    """
    global _LOGGER_INITIALIZED
    logger = logging.getLogger("mydm")
    logger.setLevel(level)
    logger.propagate = False

    if _LOGGER_INITIALIZED and not force:
        return logger

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    directory = Path(log_dir) if log_dir else get_log_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / LOG_FILE_NAME,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        logger.addHandler(handler)
    except Exception:
        # 日志写不了也不能影响主流程，挂一个空 handler 让调用方不用判断
        logger.addHandler(logging.NullHandler())

    _LOGGER_INITIALIZED = True
    return logger


def get_logger(name: str = "mydm") -> logging.Logger:
    """取一个日志器（子模块用 mydm.downloader 这样带前缀的名字）。"""
    return logging.getLogger(name)


def reset_logging_for_tests() -> None:
    """仅供测试使用：重置日志初始化标记。"""
    global _LOGGER_INITIALIZED
    _LOGGER_INITIALIZED = False


# 让类型检查器知道 Callable 被用到了（进度回调签名参考）
ProgressCallback = Callable[[dict], None]
