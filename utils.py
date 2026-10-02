"""MyDM-Lite 工具模块。

这里只放**纯函数**和“跟界面无关”的小工具，方便单独做单元测试：

* 文件名解析（Content-Disposition / RFC 5987 / URL 路径 / 默认名）
* 重名自动加 (1)、(2) 后缀
* 大小格式化（B/KB/MB/GB）
* 剩余时间格式化
* 默认“下载”目录获取（不存在就创建，创建失败回退用户主目录）
* 跨平台“打开文件夹并选中文件”
* 日志初始化（5MB 轮转，最多保留 3 个日志文件）
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional
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
