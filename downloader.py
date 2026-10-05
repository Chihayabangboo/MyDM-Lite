"""MyDM-Lite 下载核心。

职责：
* HEAD / GET Range 探测服务器能力（是否支持 Range、文件多大）
* 按自动调整规则算线程数，把文件切成若干字节区间
* 每个线程写**自己的** ``.partN`` 临时文件（绝不 seek 写同一个文件）
* 每个线程独立负责自己区间的重试（429 指数退避 / 连接超时 / 5xx）
* **断点续传**：把进度写进 ``<文件名>.download.json``，下次接着下
* 全部下载完成后按顺序合并成最终文件，并删除所有 ``.partN`` 和元数据
* 取消（threading.Event）时立刻停止，**保留**分块和元数据以便下次继续

给界面用的时候只需要：``download(url, target, threads, cancel_event, progress_cb)``。
所有面向用户的错误都是 ``DownloadError``，``message`` 是通俗中文。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import requests

from utils import (
    build_metadata,
    cleanup_part_files,
    ensure_dir,
    format_eta,
    get_logger,
    has_resumable_state,
    inspect_part_files,
    load_resume_state,
    metadata_file_path,
    part_file_path,
    remove_metadata,
    resolve_download_filename,
    unique_path,
    write_metadata,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_THREADS = 8
MAX_THREADS = 64
MIN_THREADS = 1
VALID_THREAD_OPTIONS = (1, 2, 4, 8, 16, 32, 64)

MIN_CHUNK_SIZE = 1024 * 1024  # 每块至少 1MB，否则减少线程数
SMALL_FILE_LIMIT = 64 * 1024 * 1024  # 小于 64MB 的文件最多 16 线程
SMALL_FILE_MAX_THREADS = 16

CHUNK_SIZE = 8192  # iter_content 的块大小
PROBE_TIMEOUT = 5  # HEAD 探测的独立短超时（秒）
READ_TIMEOUT = 30  # 正式下载单个请求的超时（秒）
CONNECT_TIMEOUT = 10

MAX_RETRY_429 = 3  # 429 限流：最多重试 3 次，指数退避
MAX_RETRY_TRANSIENT = 2  # 连接超时 / 5xx：最多重试 2 次
MAX_RETRY_OTHER = 1  # 其它网络异常（数据不全等）：最多重试 1 次

# ---- 断点续传检查点：主线程写入节奏
CHECKPOINT_INTERVAL = 1.0  # 至少每 1 秒写一次元数据
CHECKPOINT_BYTES = 1024 * 1024  # 或者每多下载 1MB 写一次（先到哪个算哪个）
CHECKPOINT_POLL = 0.1  # 主线程轮询进度的时间粒度

# 服务器没给文件大小时用的“区间终点”：表示一直读到连接结束。
# 单线程时不会发 Range 头，这个值只是让区间长度计算不至于变成 1 字节。
UNKNOWN_END = 2 ** 62

# 面向用户的通俗中文提示
MSG_NETWORK_FAILED = "网络连接失败，请检查网络后重试"
MSG_FORBIDDEN = "服务器拒绝访问，请检查链接或稍后再试"
MSG_NOT_FOUND = "链接无效，请检查后重新粘贴"
MSG_TOO_MANY_REQUESTS = "服务器太忙（请求过于频繁），请稍后再试"
MSG_SERVER_BUSY = "服务器暂时无法响应，请稍后再试"
MSG_SAVE_FAILED = "保存失败，请检查磁盘空间或换个保存位置"
MSG_INCOMPLETE = "下载不完整，请检查网络后重试"
MSG_CANCELLED = "已取消"
MSG_UNKNOWN = "下载失败，请稍后重试"

# 断点续传相关的状态文案（界面直接显示）
MSG_RESUME_STATUS = "正在恢复断点续传…"
MSG_NO_RANGE_DOWNGRADE = "服务器不支持断点续传，已清空临时文件重新下载"

# 只读事件：服务器信息已经探测完（拿到 content-length / Range 能力），
# 说明这次下载任务已经真正启动。界面收到它之后才会去记“上次保存路径”。
# 这里**只上报事实**，不参与任何下载 / 分块 / 合并 / 取消逻辑。
EVENT_PROBED = "probed"

_LOG = get_logger("mydm.downloader")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    """面向用户的下载错误，message 一定是可以直接显示的中文。"""

    def __init__(self, message: str, detail: str = "", retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.retryable = retryable


class DownloadCancelled(Exception):
    """用户点了取消。"""

    def __init__(self, message: str = MSG_CANCELLED):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 纯函数：线程数调整 & 分块区间（方便单元测试）
# ---------------------------------------------------------------------------


def compute_thread_count(
    content_length: Optional[int],
    range_supported: bool,
    user_threads: int,
    min_chunk_size: int = MIN_CHUNK_SIZE,
    small_file_limit: int = SMALL_FILE_LIMIT,
    small_file_max_threads: int = SMALL_FILE_MAX_THREADS,
    max_threads: int = MAX_THREADS,
) -> int:
    """按需求里的四步规则自动调整线程数。

    1. 不支持 Range 或 content-length 缺失（None/非法）→ 强制 1 线程
    2. ``N = max(1, min(用户选择, content_length // 1MB))``（保证每块至少 1MB）
    3. 文件小于 64MB → ``N = min(N, 16)``
    4. 最终 ``N`` 还会被限制在 1..64
    """
    try:
        user_threads = int(user_threads)
    except (TypeError, ValueError):
        user_threads = DEFAULT_THREADS
    user_threads = max(MIN_THREADS, min(int(max_threads), user_threads))

    # 第一步：不支持 Range / 大小未知，只能单线程
    if not range_supported:
        return 1
    if content_length is None:
        return 1
    try:
        total = int(content_length)
    except (TypeError, ValueError):
        return 1
    if total <= 0:
        return 1

    # 第二步：保证每块至少 1MB
    size_based = total // int(min_chunk_size)
    threads = max(MIN_THREADS, min(user_threads, max(MIN_THREADS, size_based)))

    # 第三步：小文件限制线程数
    if total < small_file_limit:
        threads = min(threads, small_file_max_threads)

    # 第四步：最终线程数
    return max(MIN_THREADS, min(int(max_threads), threads))


def plan_chunks(total_size: int, threads: int) -> List[Tuple[int, int, int]]:
    """把 ``[0, total_size)`` 切成 threads 段，返回 ``[(序号, 起始, 结束含)]``。

    保证：区间连续、不重叠、完整覆盖，最后一块吃掉除不尽的余数。
    序号从 0 开始，跟 ``.partN`` 文件编号一一对应。
    """
    try:
        total = int(total_size)
    except (TypeError, ValueError):
        raise ValueError("total_size 必须是整数")
    if total <= 0:
        raise ValueError("total_size 必须大于 0")

    try:
        thread_count = int(threads)
    except (TypeError, ValueError):
        thread_count = 1
    thread_count = max(1, thread_count) if total > 1 else 1
    thread_count = min(thread_count, total)  # 至少每块 1 字节

    base = total // thread_count
    chunks: List[Tuple[int, int, int]] = []
    start = 0
    for index in range(thread_count):
        if index == thread_count - 1:
            end = total - 1  # 最后一块吃到结尾，保证不漏字节
        else:
            end = start + base - 1
        chunks.append((index, start, end))
        start = end + 1
    return chunks


def plan_chunks_resume(
    chunks: Sequence[Tuple[int, int, int]], part_sizes: Sequence[int]
) -> List[dict]:
    """把“分块布局 + 每个 ``.partN`` 已有字节数”换算成续传计划。

    对每一块给出三件事：

    * ``resume_bytes``：磁盘上已有多少字节（**这就是要跳过的字节数**）
    * ``fetch_start``：本次请求 Range 的起点 = ``原始起点 + resume_bytes``
    * ``complete``：这一块是否已经下完（下完就整块跳过，一个字节都不请求）

    纯函数，不碰磁盘不碰网络，方便单测直接验算区间。
    """
    plan: List[dict] = []
    for position, chunk in enumerate(chunks):
        index, start, end = int(chunk[0]), int(chunk[1]), int(chunk[2])
        expected = end - start + 1
        try:
            existing = int(part_sizes[position]) if position < len(part_sizes) else 0
        except (TypeError, ValueError):
            existing = 0
        # 已有字节不可能超过这一块的长度（脏数据一律按“下满了”处理，交给校验兜底）
        existing = max(0, min(existing, expected))
        plan.append({
            "index": index,
            "start": start,
            "end": end,
            "expected": expected,
            "resume_bytes": existing,
            "fetch_start": start + existing,
            "complete": existing >= expected,
        })
    return plan


def _chunks_from_plan(plan: Sequence) -> List[Tuple[int, int, int]]:
    """从续传计划里取回分块布局（方便复用 merge / 元数据那套逻辑）。

    兼容两种输入：``(index, start, end)`` 元组，或者元数据里带 index/start/end 的字典。
    """
    chunks: List[Tuple[int, int, int]] = []
    for item in plan:
        if isinstance(item, dict):
            chunks.append((int(item["index"]), int(item["start"]), int(item["end"])))
        else:
            chunks.append((int(item[0]), int(item[1]), int(item[2])))
    return chunks


# ---------------------------------------------------------------------------
# 进度统计（全局字节 → 速度 / 剩余时间）
# ---------------------------------------------------------------------------


class ProgressReporter:
    """把“已下载字节”折算成百分比、全局速度、剩余时间。

    速度统计：保存最近 ``window`` 秒内的 ``(时间, 累计字节)`` 采样点，
    用窗口两端的差值算平均速度，避免多线程下数值乱跳。
    """

    def __init__(self, total_bytes: Optional[int], window: float = 1.5, min_interval: float = 0.2):
        self.total_bytes = total_bytes
        self.downloaded = 0
        self.window = float(window)
        self.min_interval = float(min_interval)
        self._samples = deque(maxlen=64)
        self._samples.append((time.monotonic(), 0))
        self._last_emit = 0.0
        self._last_bytes = 0

    def set_total(self, total_bytes: Optional[int]) -> None:
        """探测出总大小后补上（比如 GET Range 探测才知道）。"""
        if total_bytes and total_bytes > 0:
            self.total_bytes = int(total_bytes)

    def add(self, num_bytes: int) -> None:
        """累加已下载字节（每个分块线程下载完自己的区间后调用一次）。"""
        if num_bytes:
            self.downloaded += int(num_bytes)

    def _speed(self, now: float) -> float:
        # 丢掉超出时间窗口的老采样点，但要留最后一个作为基准
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window:
            self._samples.popleft()
        if len(self._samples) >= 2:
            old_time, old_bytes = self._samples[0]
            delta_time = now - old_time
            delta_bytes = self.downloaded - old_bytes
            if delta_time > 0.05 and delta_bytes >= 0:
                return delta_bytes / delta_time
        return 0.0

    def report(self, force: bool = False, status: str = "") -> Optional[dict]:
        """生成一条进度消息；距离上次上报太近且不是强制时返回 None。"""
        now = time.monotonic()
        if not force and (now - self._last_emit) < self.min_interval:
            return None

        self._samples.append((now, self.downloaded))
        speed = self._speed(now)
        total = self.total_bytes

        if total and total > 0:
            percent = min(100.0, self.downloaded * 100.0 / total)
            remaining = max(0, total - self.downloaded)
            eta = (remaining / speed) if speed > 0 else None
        else:
            percent = 0.0
            eta = None

        self._last_emit = now
        self._last_bytes = self.downloaded
        return {
            "type": "progress",
            "downloaded": self.downloaded,
            "total": total or 0,
            "percent": percent,
            "speed": speed,
            "eta": eta,
            "status": status,
        }


# ---------------------------------------------------------------------------
# 服务器能力探测
# ---------------------------------------------------------------------------


class ProbeResult:
    """探测结果：文件多大、支不支持 Range、最终 URL、响应头。"""

    __slots__ = ("content_length", "range_supported", "final_url", "headers")

    def __init__(self, content_length: Optional[int], range_supported: bool,
                 final_url: str = "", headers: Optional[dict] = None):
        self.content_length = content_length
        self.range_supported = bool(range_supported)
        self.final_url = final_url or ""
        self.headers = headers or {}


def needs_range_probe(content_length, accept_ranges=None) -> bool:
    """判断 HEAD 的结果够不够用；不够就再发一个 ``Range: bytes=0-0`` 探测。

    返回 True 表示“需要继续用 GET Range 探测”，返回 False 表示可以收工。
    """
    if accept_ranges is not None:
        try:
            header = str(accept_ranges).lower()
        except Exception:
            header = ""
        if "bytes" in header:
            return False
    if content_length is None:
        return True
    return True  # HEAD 没给出 Accept-Ranges 时必须再用 Range 探测确认


def _int_or_none(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def make_session() -> requests.Session:
    """建一个 requests 会话（带通用请求头，防止某些站点直接 403）。

    注意：requests 默认每个主机只留 10 个连接的连接池，64 线程同时下载时
    后面的线程会排队等连接（服务器那边看起来只有 10 个并发）。这里把连接池
    调到 MAX_THREADS，保证 64 线程是真的并行。
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 MyDM-Lite/1.0"
        ),
        "Accept": "*/*",
        "Connection": "keep-alive",
    })
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_THREADS,
        pool_maxsize=MAX_THREADS,
        max_retries=0,  # 重试逻辑全部由我们自己的线程负责
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def probe_server(
    session: requests.Session,
    url: str,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = PROBE_TIMEOUT,
) -> ProbeResult:
    """探测服务器：先 HEAD（短超时 5 秒），失败再用 ``GET Range: bytes=0-0`` 兜底。

    探测本身永远不抛异常（除了用户取消），失败就当成“不支持 Range、大小未知”，
    让上层退化成单线程下载。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled()

    # ---- 第一步：HEAD，独立短超时
    head_error = ""
    try:
        response = session.head(url, allow_redirects=True, timeout=timeout)
        status = response.status_code
        if status < 400:
            headers = response.headers
            content_length = _int_or_none(headers.get("Content-Length"))
            accept_ranges = headers.get("Accept-Ranges")
            final_url = response.url or url
            try:
                response.close()
            except Exception:
                pass
            if not needs_range_probe(content_length, accept_ranges):
                return ProbeResult(content_length, True, final_url, dict(headers))
            _LOG.info("HEAD 信息不足（length=%s, accept-ranges=%s），改用 GET Range 探测",
                      content_length, accept_ranges)
            return _probe_with_get_range(
                session, url, final_url, dict(headers), content_length, cancel_event, timeout
            )
        head_error = f"HEAD 返回 {status}"
        try:
            response.close()
        except Exception:
            pass
    except DownloadCancelled:
        raise
    except Exception as exc:
        head_error = f"{type(exc).__name__}: {exc}"

    _LOG.info("HEAD 探测失败（%s），改用 GET Range 探测", head_error)

    # ---- 第二步：GET 带 Range: bytes=0-0
    return _probe_with_get_range(session, url, url, {}, None, cancel_event, timeout)


def _probe_with_get_range(
    session,
    url: str,
    final_url: str,
    headers: dict,
    content_length: Optional[int],
    cancel_event: Optional[threading.Event],
    timeout: float,
) -> ProbeResult:
    """用 ``GET`` + ``Range: bytes=0-0`` 探测是否支持分块下载。"""
    try:
        response = session.get(
            url, headers={"Range": "bytes=0-0"}, stream=True,
            allow_redirects=True, timeout=timeout,
        )
    except DownloadCancelled:
        raise
    except Exception as exc:
        _LOG.warning("GET Range 探测也失败（%s: %s），按不支持分块处理", type(exc).__name__, exc)
        return ProbeResult(content_length, False, final_url, headers)

    try:
        status = response.status_code
        response_headers = dict(response.headers)
        if status == 206:
            content_range = response.headers.get("Content-Range") or ""
            total = None
            if "/" in content_range:
                total = _int_or_none(content_range.rsplit("/", 1)[-1])
            if total is None:
                total = _int_or_none(response.headers.get("Content-Length"))
            if total is None:
                total = content_length
            return ProbeResult(total, True, response.url or final_url, response_headers)
        if status == 200:
            # 服务器忽略了 Range，把整个文件返回了 → 不支持分块
            total = _int_or_none(response.headers.get("Content-Length"))
            if total is None:
                total = content_length
            return ProbeResult(total, False, response.url or final_url, response_headers)
        _LOG.warning("GET Range 探测返回 %s，按不支持分块处理", status)
        return ProbeResult(content_length, False, final_url, response_headers)
    finally:
        try:
            response.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 单区间下载（每个线程独立负责 + 独立重试）
# ---------------------------------------------------------------------------


def _backoff_seconds(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """指数退避：1s、2s、4s、8s……上限 cap 秒。"""
    return min(cap, base * (2 ** max(0, attempt)))


def _parse_retry_after(value: Optional[str], attempt: int) -> float:
    """429 优先用响应里的 Retry-After，没有就用指数退避。"""
    if value:
        try:
            seconds = float(str(value).strip())
            if 0 <= seconds <= 120:
                return seconds
        except (TypeError, ValueError):
            pass  # Retry-After 也可能是 HTTP 日期，直接忽略
    return _backoff_seconds(attempt)


def _http_error_message(status: int) -> str:
    """把 HTTP 状态码翻成通俗中文。"""
    if status == 403:
        return MSG_FORBIDDEN
    if status == 404:
        return MSG_NOT_FOUND
    if status == 429:
        return MSG_TOO_MANY_REQUESTS
    if 500 <= status <= 599:
        return MSG_SERVER_BUSY
    return MSG_NETWORK_FAILED


def _open_response(session, url: str, start: int, end: int, single: bool):
    """发一次 GET 请求（支持 Range 时带上 Range 头），返回 Response。"""
    headers = {}
    if not single:
        headers["Range"] = f"bytes={start}-{end}"
    return session.get(
        url, headers=headers or None, stream=True,
        allow_redirects=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )


def download_range(
    session,
    url: str,
    part_path: Path,
    start: int,
    end: int,
    expected_size: Optional[int],
    cancel_event: Optional[threading.Event],
    single: bool = False,
    progress_cb: Optional[Callable[[int], None]] = None,
    resume_from: int = 0,
    total_size: Optional[int] = None,
) -> int:
    """把 ``[start, end]`` 这段字节下载到 ``part_path``。

    重试完全由本函数负责（就是“每个线程负责自己那一段”）。
    返回值是**这一块最终落盘的完整字节数**（含续传前已有的部分）。

    断点续传参数：

    * ``resume_from``：这块磁盘上已经有多少字节，本次只请求剩下的 ``Range``；
      传 0 就是从头下载（并且会把分块文件清空重写）。
    * ``total_size``：整个文件的大小，只在 ``single=True`` 的续传场景用来识别
      “服务器忽略了 Range、返回 200 全量”，从而退回从头下载避免拼出脏数据。

    其它约定：
    * 续传时如果失败，**保留已经落盘的字节**，下次还能接着续（这是续传的意义）；
      从头下载（``resume_from == 0``）失败时同样保留，方便上层下次建立断点。
    * 用户取消抛 ``DownloadCancelled``，失败抛 ``DownloadError``。
    """
    part_path = Path(part_path)
    target_length = int(end) - int(start) + 1
    finish = int(end)
    attempt_429 = 0
    attempt_transient = 0
    attempt_other = 0
    last_detail = ""

    try:
        resume_point = int(resume_from)
    except (TypeError, ValueError):
        resume_point = 0
    # 脏数据保护：磁盘上已有的字节数不可能超过这一块的长度
    resume_point = max(0, min(resume_point, target_length))
    # 已经完整了就一个字节都不用下
    if resume_point >= target_length:
        return target_length

    def part_bytes_on_disk() -> int:
        """分块文件在磁盘上的真实大小（被裁剪到区间长度）。"""
        try:
            size = part_path.stat().st_size if part_path.exists() else 0
        except OSError:
            size = 0
        return max(0, min(int(size), target_length))

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()

        # 本次要从哪里接着写：
        # * resume_from == 0 → 调用方要求从头下载这一块，磁盘上的残留一律作废（必须重写）
        # * resume_from > 0  → 按磁盘实际已有的字节数接着写（磁盘比声称的短就以磁盘为准，
        #   短的部分会在下面按实际起点重新请求 Range 补回来）
        if resume_point <= 0:
            written_before = 0
        else:
            written_before = min(part_bytes_on_disk(), target_length)
        fetch_start = int(start) + written_before
        if fetch_start > finish:
            return target_length

        appended = 0
        # 磁盘上已有 N 字节就必须用 "rb+" 定位到第 N 字节续写，
        # 用 "wb" 会把已经下好的 N 字节截断重来（等于白下）。
        mode = "rb+" if written_before else "wb"
        try:
            # 单线程从头下载时不发 Range（兼容完全不支持 Range 的服务器）
            use_single = bool(single and written_before <= 0)
            response = _open_response(session, url, fetch_start, finish, use_single)
            try:
                status = response.status_code
                if status == 403:
                    raise DownloadError(MSG_FORBIDDEN, f"HTTP 403 ({part_path.name})", retryable=False)
                if status not in (200, 206):
                    raise DownloadError(
                        _http_error_message(status),
                        f"HTTP {status} ({part_path.name})",
                        retryable=status == 429 or 500 <= status <= 599,
                    )

                if status == 206 and not _content_range_matches(
                    response.headers.get("Content-Range"), fetch_start
                ):
                    # 服务器返回的区间起点不是我们要的，绝不能往文件里追加
                    raise DownloadError(
                        MSG_INCOMPLETE,
                        f"服务器返回了错误的区间（{part_path.name}）",
                    )

                if status == 200 and written_before > 0:
                    # 带了 Range 却拿到 200：服务器把整个文件返回了。
                    # 追加会拼出脏数据，所以退回“这一块从头下载”。
                    _LOG.info("分块 %s 的续传请求返回 200，改为从头下载这一块", part_path.name)
                    if single and total_size and int(total_size) > target_length:
                        raise DownloadError(
                            MSG_INCOMPLETE,
                            f"服务器不支持断点续传（{part_path.name}）",
                            retryable=False,
                        )
                    written_before = 0
                    appended = 0
                    mode = "wb"

                with open(part_path, mode) as handle:
                    if mode == "rb+":
                        # 先定位再截断：磁盘上如果多出了脏字节（上次写坏/被别的程序改过），
                        # 必须砍掉，否则合并出来就是坏文件。
                        handle.seek(written_before)
                        handle.truncate()
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        appended += len(chunk)
                        if progress_cb is not None:
                            progress_cb(len(chunk))
            finally:
                try:
                    response.close()
                except Exception:
                    pass

            # 大小校验：expected_size 为 None 表示服务器没给大小（读到多少算多少），
            # 这时绝不能拿“区间长度”去要求字节数——大小未知时区间终点是无限大。
            if expected_size is not None and written_before + appended != int(expected_size):
                raise DownloadError(
                    MSG_INCOMPLETE,
                    f"分块字节数不符：期望 {expected_size}，实际 {written_before + appended}",
                )
            return written_before + appended

        except DownloadCancelled:
            # 取消时保留已经落盘的字节，下次接着续
            raise
        except DownloadError as exc:
            if not exc.retryable:
                raise
            status = _status_from_detail(exc.detail)
            if status == 429:
                if attempt_429 >= MAX_RETRY_429:
                    _LOG.error("分块 %s 连续被限流，放弃", part_path.name)
                    raise
                wait = _parse_retry_after(None, attempt_429)
                attempt_429 += 1
                _LOG.warning("分块 %s 被限流(429)，%.1f 秒后第 %d 次重试",
                             part_path.name, wait, attempt_429)
                if cancel_event is not None and cancel_event.wait(timeout=wait):
                    raise DownloadCancelled()
                continue
            if status is not None and status >= 500:
                if attempt_transient >= MAX_RETRY_TRANSIENT:
                    raise
                attempt_transient += 1
                _LOG.warning("分块 %s 遇到 %s，重试第 %d 次", part_path.name, status, attempt_transient)
                continue
            if attempt_other >= MAX_RETRY_OTHER:
                raise
            attempt_other += 1
            _LOG.warning("分块 %s 出错（%s），重试第 %d 次", part_path.name, exc.detail, attempt_other)
            continue

        except requests.exceptions.RequestException as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
            if attempt_transient >= MAX_RETRY_TRANSIENT:
                _LOG.error("分块 %s 网络异常且重试次数用尽：%s", part_path.name, last_detail)
                raise DownloadError(MSG_NETWORK_FAILED, last_detail) from exc
            attempt_transient += 1
            _LOG.warning("分块 %s 网络异常（%s），重试第 %d 次",
                         part_path.name, last_detail, attempt_transient)
            continue

        except OSError as exc:
            # 磁盘写不进去（空间不足、目录没了……）
            _LOG.error("分块 %s 写文件失败：%s", part_path.name, exc)
            raise DownloadError(MSG_SAVE_FAILED, f"{type(exc).__name__}: {exc}", retryable=False) from exc


def _content_range_matches(header: Optional[str], expected_start: int) -> bool:
    """校验 206 响应的 ``Content-Range`` 起点是否为 ``expected_start``。

    没有这个头或者解析不出来时返回 True（有些服务器不返回它，不该因此判失败）。
    """
    if not header:
        return True
    match = re.search(r"bytes\s+(\d+)\s*-", str(header), re.IGNORECASE)
    if not match:
        return True
    try:
        return int(match.group(1)) == int(expected_start)
    except (TypeError, ValueError):  # pragma: no cover - 正则已保证是数字
        return True



def _status_from_detail(detail: str) -> Optional[int]:
    """从 detail 里把 HTTP 状态码抠出来（detail 形如 ``HTTP 429 (...)``）。"""
    if not detail:
        return None
    match = re.search(r"HTTP (\d{3})", detail)
    if match:
        return int(match.group(1))
    return None


# ---------------------------------------------------------------------------
# 分块线程 & 合并
# ---------------------------------------------------------------------------


def _worker(
    session,
    url: str,
    target: Path,
    chunk: Tuple[int, int, int],
    expected_size: Optional[int],
    cancel_event: threading.Event,
    single: bool,
    reporter: ProgressReporter,
    progress_cb: Callable[[dict], None],
    errors: List[BaseException],
    status_text: str,
    error_lock: threading.Lock,
    start_offset: int = 0,
    total_size: Optional[int] = None,
) -> None:
    """一个分块线程：下载自己的区间（可续传）→ 更新全局进度 → 出错记录异常。

    ``start_offset`` 是这块在磁盘上已经有的字节数（续传时跳过它们），
    非 0 表示这是恢复的那一块，下完要通知界面一次“正在恢复断点续传…”。
    """
    index, start, end = chunk
    part_path = part_file_path(target, index)

    def on_bytes(num_bytes: int) -> None:
        reporter.add(num_bytes)
        message = reporter.report(status=status_text)
        if message is not None:
            progress_cb(message)

    try:
        download_range(
            session=session, url=url, part_path=part_path, start=start, end=end,
            expected_size=expected_size, cancel_event=cancel_event, single=single,
            progress_cb=on_bytes, resume_from=start_offset, total_size=total_size,
        )
        if start_offset > 0:
            # 通知界面：这一块是接着上次下的。main.py 只会把状态标签改个字，
            # 不涉及任何控件创建/布局改动。
            progress_cb({
                "type": "resuming",
                "status": MSG_RESUME_STATUS,
                "resumed_bytes": int(start_offset),
            })
        message = reporter.report(force=True, status=status_text)
        if message is not None:
            progress_cb(message)
    except DownloadCancelled:
        return
    except BaseException as exc:  # noqa: BLE001 - 收集给主线程统一处理
        with error_lock:
            if not errors:
                errors.append(exc)
        # 有线程彻底失败了，通知其它线程尽快收工（它们会以“取消”方式退出）
        cancel_event.set()


def merge_parts(target: Path, chunks: Sequence[Tuple[int, int, int]], expected_total: Optional[int]) -> int:
    """按顺序把 ``.partN`` 合并成最终文件，返回合并后的字节数。

    合并成功后删除所有 ``.partN``；合并失败也会尽量清理。
    元数据（``.download.json``）由调用方在合并成功后删除——
    这里保留它，万一合并中途失败，下次还能接着续。
    """
    target = Path(target)
    total_written = 0
    try:
        # 用 'xb' 独占创建，避免不小心覆盖用户已有文件
        with open(target, "xb") as output:
            for index, _start, _end in chunks:
                part_path = part_file_path(target, index)
                if not part_path.exists():
                    raise DownloadError(MSG_INCOMPLETE, f"缺少分块文件 {part_path.name}")
                with open(part_path, "rb") as source:
                    while True:
                        buffer = source.read(CHUNK_SIZE * 32)
                        if not buffer:
                            break
                        output.write(buffer)
                        total_written += len(buffer)
    except DownloadError:
        cleanup_part_files(target)
        raise
    except Exception as exc:  # noqa: BLE001 - 磁盘/权限问题统一翻成中文提示
        _LOG.error("合并文件失败：%s", exc)
        cleanup_part_files(target)
        raise DownloadError(MSG_SAVE_FAILED, f"合并失败：{type(exc).__name__}: {exc}", retryable=False) from exc

    if expected_total is not None and total_written != expected_total:
        raise DownloadError(
            MSG_INCOMPLETE,
            f"合并后大小不符：期望 {expected_total}，实际 {total_written}",
        )
    cleanup_part_files(target)
    return total_written


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------


def download(
    url: str,
    target,
    threads: int = DEFAULT_THREADS,
    cancel_event: Optional[threading.Event] = None,
    progress_cb: Optional[Callable[[dict], None]] = None,
    session=None,
    logger=None,
    max_threads: int = MAX_THREADS,
    resume: bool = True,
) -> Path:
    """下载 ``url`` 到 ``target``，返回最终文件路径。

    ``progress_cb`` 会被**下载线程**调用，参数是要发给主线程的 dict 消息，
    可能包含：``type=progress`` / ``type=status`` / ``type=resuming``。
    界面层只负责塞进 queue，绝不在子线程里碰 Tkinter 控件。

    ``resume=True``（默认）时启用断点续传：

    * 发现 ``<文件名>.download.json`` + ``.partN`` 就接着下，只请求缺的那段 Range；
    * 下载中主线程每 1 秒或每 1MB 写一次元数据（线程安全，原子替换）；
    * 服务器明确不支持 Range → 清掉临时文件和元数据，退回单线程从头下载；
    * 合并成功 → 删除元数据 + 所有 ``.partN``；
    * 用户取消 → **保留**元数据和 ``.partN``，下次点开始就能继续。
    """
    log = logger or get_logger("mydm.downloader")
    cancel_event = cancel_event or threading.Event()
    target = Path(target)
    if progress_cb is None:
        progress_cb = lambda _msg: None  # noqa: E731

    ensure_dir(target.parent)

    own_session = session is None
    session = session or make_session()

    # 是否已经建立了本次下载的断点（决定取消时是保留还是清理临时文件）
    checkpoint_started = False
    completed = False

    try:
        if cancel_event.is_set():
            raise DownloadCancelled()

        log.info("开始下载：%s → %s（期望线程数 %s）", url, target, threads)
        _emit_status(progress_cb, "正在连接服务器…")

        probe = probe_server(session, url, cancel_event)
        content_length = probe.content_length
        range_supported = probe.range_supported
        log.info("探测结果：大小=%s，支持 Range=%s，最终地址=%s",
                 content_length, range_supported, probe.final_url)

        # 只读事件：任务已经真正启动（已经探测到 content-length / Range 能力）。
        # 界面（main.py）监听到它之后才写“上次保存路径”的配置，
        # 绝不是用户一点“开始下载”就写。这里不改动任何下载行为。
        _emit_state(progress_cb, EVENT_PROBED, {
            "content_length": content_length,
            "range_supported": range_supported,
            "url": probe.final_url or url,
            "target": str(target),
            "directory": str(target.parent),
            "filename": target.name,
        })

        requested = _safe_int(threads, DEFAULT_THREADS)
        actual_threads = compute_thread_count(
            content_length, range_supported, requested, max_threads=max_threads
        )
        if actual_threads != requested:
            _emit_status(progress_cb, f"已自动调整为 {actual_threads} 线程")
            log.info("线程数自动调整：%s → %s", requested, actual_threads)

        # 没有 content-length 时按单线程处理，并把期望大小置空
        if content_length is None or content_length <= 0:
            content_length = None
            actual_threads = 1

        # ---- 断点续传：能续就续，不能续（含服务器不支持 Range）就清掉重来
        chunks: List[Tuple[int, int, int]] = []
        part_sizes: List[int] = []
        resume_state = None
        if resume and content_length:
            if not range_supported and has_resumable_state(target):
                # 需求红线：服务器不支持 Accept-Ranges 时强制放弃续传，
                # 清空临时文件和元数据，退回单线程从头下载。
                log.info("服务器不支持 Range，放弃断点续传并清空临时文件：%s", target)
                _emit_status(progress_cb, MSG_NO_RANGE_DOWNGRADE)
                cleanup_part_files(target)
                remove_metadata(target)
                actual_threads = 1
            elif range_supported:
                resume_state = load_resume_state(target, url=url)

        if resume_state is not None:
            chunks = _chunks_from_plan(resume_state.chunks)
            part_sizes = list(resume_state.part_sizes)
            actual_threads = len(chunks)
            done_before = resume_state.downloaded_bytes
            log.info("发现断点：已下载 %s 字节 / 共 %s 字节，分块 %s 个，接着下",
                     done_before, content_length, len(chunks))
            _emit_status(
                progress_cb,
                f"{MSG_RESUME_STATUS}（已完成 {done_before * 100 // max(1, content_length)}%）",
            )
        else:
            if not chunks:
                if content_length is None:
                    chunks = [(0, 0, UNKNOWN_END)]
                else:
                    chunks = plan_chunks(content_length, actual_threads)
            # 新下载：清掉同名残留（没有元数据的 .partN 无法校验，不能混进来）
            cleanup_part_files(target)
            part_sizes = inspect_part_files(target, chunks)
            if content_length is None:
                _emit_status(progress_cb, "开始下载（1 线程）")
            elif actual_threads == requested:
                _emit_status(progress_cb, f"开始下载（{actual_threads} 线程）")

        single = len(chunks) <= 1
        if content_length is None:
            # 大小未知：只有一个 [0, ∞) 区间，没有区间信息可以拿去算续传，
            # 也不能按“区间长度 = end - start + 1”去校验字节数（那会算成 1 字节）。
            resume_plan = [{
                "index": 0, "start": 0, "end": UNKNOWN_END,
                "expected": None, "resume_bytes": 0, "fetch_start": 0, "complete": False,
            }]
        else:
            resume_plan = plan_chunks_resume(chunks, part_sizes)

        reporter = ProgressReporter(content_length)
        if resume_state is not None:
            # 续传时进度基数 = 磁盘上已经有的字节，进度条才不会从 0 重新跳
            reporter.add(resume_state.downloaded_bytes)
        canceled = threading.Event()
        errors: List[BaseException] = []
        error_lock = threading.Lock()
        status_text = f"正在下载（{len(chunks)} 线程）"
        resumed_any = any(item["resume_bytes"] > 0 for item in resume_plan)

        # ---- 建立/更新元数据：合并前它一直待在磁盘上，中断了下次接着用
        metadata = build_metadata(
            url=url, target=target, total_size=content_length, chunks=chunks,
            part_sizes=part_sizes, threads=len(chunks), range_supported=range_supported,
            final_url=probe.final_url,
        )
        if write_metadata(target, metadata) is not None:
            checkpoint_started = True
        else:
            log.warning("元数据写入失败，本次下载不做断点续传：%s", target)

        worker_threads = []
        for item in resume_plan:
            chunk = (item["index"], item["start"], item["end"])
            expected = None if content_length is None else item["expected"]
            worker = threading.Thread(
                target=_worker,
                kwargs=dict(
                    session=session, url=url, target=target, chunk=chunk,
                    expected_size=expected, cancel_event=cancel_event, single=single,
                    reporter=reporter, progress_cb=progress_cb, errors=errors,
                    status_text=status_text, error_lock=error_lock,
                    start_offset=item["resume_bytes"],
                    total_size=content_length,
                ),
                name=f"mydm-part{item['index']}",
                daemon=True,
            )
            worker.start()
            worker_threads.append(worker)

        # ---- 主线程等待 + 定时写检查点（元数据只由主线程写，天然线程安全）
        last_checkpoint_at = time.monotonic()
        last_checkpoint_bytes = reporter.downloaded
        for worker in worker_threads:
            while worker.is_alive():
                worker.join(timeout=CHECKPOINT_POLL)
                if cancel_event.is_set():
                    canceled.set()
                now = time.monotonic()
                if (now - last_checkpoint_at) >= CHECKPOINT_INTERVAL or \
                        (reporter.downloaded - last_checkpoint_bytes) >= CHECKPOINT_BYTES:
                    _update_checkpoint(target, metadata, reporter.downloaded)
                    last_checkpoint_at = now
                    last_checkpoint_bytes = reporter.downloaded

        # 有线程报错时，错误信息优先于“已取消”
        with error_lock:
            first_error = errors[0] if errors else None
        if first_error is not None:
            # 出错也要把现场存下来，用户再点一次“开始下载”还能接着续
            _update_checkpoint(target, metadata, reporter.downloaded)
            if isinstance(first_error, DownloadError):
                raise first_error
            raise DownloadError(MSG_UNKNOWN, f"{type(first_error).__name__}: {first_error}")

        if cancel_event.is_set() or canceled.is_set():
            _update_checkpoint(target, metadata, reporter.downloaded)
            raise DownloadCancelled()

        actual_total = reporter.downloaded
        if content_length is None:
            content_length = actual_total
        elif actual_total != content_length:
            # 分块加起来不等于总大小：说明磁盘上有脏分块，合并出来一定是坏文件。
            # 这里直接报中文错并清理现场，绝不让用户拿到一个“看起来成功”的坏文件。
            raise DownloadError(
                MSG_INCOMPLETE,
                f"分块总字节数不符：期望 {content_length}，实际 {actual_total}",
            )

        # ---- 合并阶段：通知界面“正在合并文件…”，界面会禁用开始/取消按钮
        progress_cb({"type": "merging", "status": "正在合并文件…", "downloaded": actual_total,
                     "total": content_length or 0, "percent": 100.0, "speed": 0.0, "eta": 0})
        log.info("开始合并 %s 个分块 → %s", len(chunks), target.name)
        merged = merge_parts(target, chunks, content_length if content_length else None)
        cleanup_part_files(target)
        remove_metadata(target)
        completed = True
        log.info("下载完成：%s（%s 字节）%s", target, merged, "（断点续传）" if resumed_any else "")

        progress_cb({
            "type": "done", "status": "已完成", "path": str(target),
            "downloaded": merged, "total": merged, "percent": 100.0,
            "speed": 0.0, "eta": 0,
        })
        return target

    except DownloadCancelled:
        if checkpoint_started:
            # 用户主动取消：保留 .partN 和元数据，下次点“开始下载”直接接着续
            log.info("用户取消下载，已保留断点（%s 个分块 + 元数据）：%s",
                     len(_safe_chunks(chunks)), target)
        else:
            cleanup_part_files(target)
            log.info("用户取消下载，已清理临时分块：%s", target)
        raise
    except DownloadError as exc:
        if checkpoint_started:
            log.error("下载失败：%s（%s）；已保留断点供下次续传", exc.message, exc.detail)
        else:
            cleanup_part_files(target)
            log.error("下载失败：%s（%s）", exc.message, exc.detail)
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不让英文堆栈冒到界面上
        if not checkpoint_started or not completed:
            cleanup_part_files(target)
            remove_metadata(target)
        log.exception("下载出现未预期错误：%s", exc)
        raise DownloadError(MSG_UNKNOWN, f"{type(exc).__name__}: {exc}") from exc
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def _safe_chunks(value) -> list:
    """给日志用：确认 ``chunks`` 是列表，避免日志本身再抛异常。"""
    return value if isinstance(value, list) else []


def _update_checkpoint(target: Path, metadata: Optional[dict], downloaded: int) -> None:
    """按磁盘现状刷新元数据里的每个分块字节数，然后原子写盘。

    只跟 ``.partN`` 的实际大小对齐（不猜），所以即使某个分块文件被截断，
    恢复时也不会读多或读少。写失败只记日志，绝不影响下载。
    """
    if not metadata:
        return
    try:
        chunks = [
            (int(item["index"]), int(item["start"]), int(item["end"]))
            for item in metadata.get("chunks", [])
        ]
        if not chunks:
            return
        sizes = inspect_part_files(target, chunks)
        total = metadata.get("total_size")
        metadata["bytes_downloaded"] = int(downloaded)
        metadata["chunks"] = build_metadata(
            url=str(metadata.get("url") or ""), target=target, total_size=total,
            chunks=chunks, part_sizes=sizes,
            threads=int(metadata.get("threads") or len(chunks)),
            range_supported=bool(metadata.get("range_supported", True)),
            final_url=str(metadata.get("final_url") or ""),
        )["chunks"]
        write_metadata(target, metadata)
    except Exception as exc:  # noqa: BLE001 - 检查点永远不能拖垮下载
        _LOG.warning("刷新断点元数据失败：%s", exc)


def _emit_status(progress_cb: Callable[[dict], None], text: str) -> None:
    """发一条纯状态消息给界面。"""
    progress_cb({"type": "status", "status": text})


def _emit_state(progress_cb: Optional[Callable[[dict], None]], state: str, payload: Optional[dict] = None) -> None:
    """发一条**只读**状态事件给界面（例如 ``state='probed'``）。

    只是把事实塞进队列，不参与任何下载 / 分块 / 合并 / 取消逻辑；
    ``progress_cb`` 为空时安静地什么都不做。
    """
    if progress_cb is None:
        return
    message = {"type": "state", "state": state}
    if payload:
        message.update(payload)
    progress_cb(message)


def _safe_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def download_to_directory(
    url: str,
    directory,
    threads: int = DEFAULT_THREADS,
    cancel_event: Optional[threading.Event] = None,
    progress_cb: Optional[Callable[[dict], None]] = None,
    session=None,
    logger=None,
) -> Path:
    """先探测文件名，再决定最终保存路径，然后开始下载。

    界面里点“开始下载”走的就是这里。

    断点续传的关键一步：如果同名文件旁边已经有 ``.download.json`` + ``.partN``，
    就**沿用这个名字**（不再加 ``(1)`` 后缀），这样才是接着上次继续下。
    """
    log = logger or get_logger("mydm.downloader")
    cancel_event = cancel_event or threading.Event()
    directory = Path(directory)
    ensure_dir(directory)

    own_session = session is None
    session = session or make_session()
    try:
        _emit_status(progress_cb or (lambda _m: None), "正在获取文件信息…")
        probe = probe_server(session, url, cancel_event)
        filename = resolve_download_filename(
            url=probe.final_url or url, headers=probe.headers
        )
        plain_target = directory / filename
        if has_resumable_state(plain_target):
            # 有断点 → 沿用原文件名，走到 download() 里自然会续传
            target = plain_target
            log.info("发现未完成的下载，继续使用原文件名：%s", target)
        else:
            target = unique_path(plain_target)
        log.info("解析文件名：%s → %s", filename, target)
        # 只读事件：服务器信息 + 目标文件名都探测完了，这次下载任务确实已经启动。
        # 界面（main.py）监听到它之后才写“上次保存路径”的配置，
        # 绝不是用户一点“开始下载”就写。这里不改动任何下载行为。
        # （下面的 download() 里还会再发一条同样的状态，界面写配置是幂等的。）
        _emit_state(progress_cb, EVENT_PROBED, {
            "content_length": probe.content_length,
            "range_supported": probe.range_supported,
            "url": probe.final_url or url,
            "target": str(target),
            "directory": str(directory),
            "filename": target.name,
        })
        return download(
            url=url, target=target, threads=threads, cancel_event=cancel_event,
            progress_cb=progress_cb, session=session, logger=log,
        )
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass
