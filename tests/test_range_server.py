"""本地 Range 测试服务器 fixture + 下载核心集成测试。

为什么要自己搭服务器：
* 公开大文件不稳定，还可能被限流；
* 需要精确控制 HEAD / Range / 206 / 429 / 403 等行为；
* 必须验证 64 线程并发是真的并行，所以服务器用
  ``http.server.ThreadingHTTPServer``（不是单线程的 ``HTTPServer``），
  否则 64 个请求会排队、直接超时。
"""

from __future__ import annotations

import hashlib
import http.server
import re
import socket
import threading
import time
from pathlib import Path

import pytest

import downloader
from downloader import (
    DEFAULT_THREADS,
    MAX_THREADS,
    DownloadCancelled,
    DownloadError,
    compute_thread_count,
    download,
    download_range,
    make_session,
    plan_chunks,
    probe_server,
)
from utils import part_file_path

CJK_FILENAME = "中文文件名.bin"
CJK_DISPOSITION = (
    "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87%E6%96%87%E4%BB%B6%E5%90%8D.bin"
)
RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


def build_payload(size: int) -> bytes:
    """生成一段可重复、可校验的伪随机数据。"""
    block = hashlib.sha256(b"mydm-lite-test-payload").digest()
    repeats = size // len(block) + 1
    return (block * repeats)[:size]


def build_pattern(size: int) -> bytes:
    """生成一段很大的“周期数据”（默认 8KB 一个周期）。

    这样测试 64MB 这种大文件时，不需要真的在内存里放 64MB：
    服务器按需从这个周期里切片即可，内容依然是确定的、可校验的。
    """
    block = hashlib.sha256(b"mydm-lite-pattern-block").digest()
    return (block * (size // len(block) + 1))[:size]


def pattern_bytes(pattern: bytes, start: int, end: int) -> bytes:
    """取周期数据 ``[start, end)`` 区间的字节。"""
    length = end - start
    if length <= 0:
        return b""
    period = len(pattern)
    offset = start % period
    repeats = (offset + length) // period + 1
    return (pattern * repeats)[offset:offset + length]


# ---------------------------------------------------------------------------
# 测试用 HTTP 服务器
# ---------------------------------------------------------------------------


class _RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    """最小可用的 HTTP 处理器：支持 HEAD / GET / Range / 206 / 可选故障注入。"""

    protocol_version = "HTTP/1.1"
    server_version = "MyDMLiteTestServer/1.0"

    # ---- 日志：测试时保持安静
    def log_message(self, fmt, *args):  # noqa: D102
        return

    # ---- 工具
    @property
    def config(self):
        return self.server.config  # type: ignore[attr-defined]

    def _record(self, method: str, range_header=None, status: int = 0) -> None:
        stats = self.config["stats"]
        with stats["lock"]:
            stats["requests"] += 1
            if method == "HEAD":
                stats["head_requests"] += 1
            else:
                stats["get_requests"] += 1
            if range_header:
                stats["range_requests"] += 1
                stats["range_headers"].append(range_header)
            if status:
                stats["statuses"].append(status)
            stats["active"] += 1
            stats["max_concurrent"] = max(stats["max_concurrent"], stats["active"])
        self._counted_active = True

    def _release(self) -> None:
        stats = self.config["stats"]
        if getattr(self, "_counted_active", False):
            with stats["lock"]:
                stats["active"] -= 1
            self._counted_active = False

    def _record_status(self, status: int) -> None:
        """记一下返回过的状态码，方便测试断言。"""
        stats = self.config["stats"]
        with stats["lock"]:
            stats["statuses"].append(status)

    def _maybe_delay(self, is_probe: bool) -> None:
        config = self.config
        delay = config["delay"]
        if delay and not is_probe:
            time.sleep(delay)
        barrier = config["barrier"]
        # 探测请求（HEAD / Range: bytes=0-0）不参与 Barrier，
        # 否则它自己会一直等到 64 个分块请求到齐才返回，白白超时。
        if barrier is not None and not is_probe:
            try:
                barrier.wait(timeout=20)
            except threading.BrokenBarrierError:  # pragma: no cover
                pass

    def _body_for(self, start: int, end: int) -> bytes:
        """取出 ``[start, end)`` 的响应体（支持周期数据，不必真的占内存）。"""
        pattern = self.config["pattern"]
        if pattern is not None:
            return pattern_bytes(pattern, start, end)
        return self.config["payload"][start:end]

    def _send_payload(self, start: int, end: int, status: int, content_length: int) -> None:
        body = self._body_for(start, end)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes" if self.config["support_range"] else "none")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{content_length}")
        disposition = self.config["content_disposition"]
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- HTTP 方法
    def do_HEAD(self):  # noqa: N802
        self._record("HEAD")
        try:
            config = self.config
            if config["head_status"] != 200:
                self._record_status(config["head_status"])
                self.send_response(config["head_status"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._maybe_delay(is_probe=True)
            self._send_payload(0, config["size"], 200, config["size"])
        finally:
            self._release()

    def do_GET(self):  # noqa: N802
        range_header = self.headers.get("Range")
        self._record("GET", range_header)
        try:
            config = self.config
            if config["get_status"] not in (200, 206):
                self._record_status(config["get_status"])
                self.send_response(config["get_status"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            size = config["size"]
            is_probe = range_header == "bytes=0-0"

            if range_header and config["support_range"]:
                match = RANGE_RE.search(range_header)
                if match:
                    start = int(match.group(1))
                    end_text = match.group(2)
                    end = int(end_text) + 1 if end_text else size
                    if start >= size:
                        self._record_status(416)
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size)
                    self._maybe_delay(is_probe=is_probe)
                    self._send_payload(start, end, 206, size)
                    return

            # 完整响应（不支持 Range，或者客户端没带 Range）
            self._maybe_delay(is_probe=is_probe)
            self._send_payload(0, size, 200, size)
        finally:
            self._release()


class _TestServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer：每个请求一个线程，才能扛住 64 并发。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, handler, config):
        self.config = config
        super().__init__(address, handler)


class RangeTestServer:
    """测试服务器封装：启动 / 停止 / 读取统计信息。"""

    def __init__(
        self,
        payload: bytes = None,
        pattern_size: int = None,
        support_range: bool = True,
        head_status: int = 200,
        get_status: int = 200,
        content_disposition: str = "",
        delay: float = 0.0,
        barrier: threading.Barrier = None,
    ):
        self.pattern = None
        if pattern_size:
            # 大文件用“周期数据”模式：服务器按需切片，不需要真的占 pattern_size 内存
            self.payload = None
            self.pattern = build_pattern(8192)
            self.size_bytes = int(pattern_size)
        else:
            self.payload = payload if payload is not None else build_payload(2 * 1024 * 1024)
            self.size_bytes = len(self.payload)
        self.stats = {
            "lock": threading.Lock(),
            "requests": 0,
            "head_requests": 0,
            "get_requests": 0,
            "range_requests": 0,
            "range_headers": [],
            "max_concurrent": 0,
            "active": 0,
            "statuses": [],
        }
        self.config = {
            "payload": self.payload,
            "pattern": self.pattern,
            "size": self.size_bytes,
            "support_range": support_range,
            "head_status": head_status,
            "get_status": get_status,
            "content_disposition": content_disposition,
            "delay": delay,
            "barrier": barrier,
            "stats": self.stats,
        }
        self.httpd = _TestServer(("127.0.0.1", 0), _RangeRequestHandler, self.config)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="test-http", daemon=True)
        self.thread.start()

    # ---- 生命周期
    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.thread.join(timeout=5)

    # ---- 访问
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/payload.bin"

    @property
    def size(self) -> int:
        return self.size_bytes

    def expected_bytes(self, start: int, end: int) -> bytes:
        """期望下载到的字节（``[start, end)``）。"""
        if self.pattern is not None:
            return pattern_bytes(self.pattern, start, end)
        return self.payload[start:end]

    def assert_content_matches(self, path, label: str) -> None:
        """校验下载到的文件内容跟服务器提供的一模一样。"""
        actual = Path(path).read_bytes()
        assert len(actual) == self.size_bytes, f"{label}：文件大小不对"
        step = self.size_bytes if self.pattern is None else len(self.pattern)
        for start in range(0, self.size_bytes, step):
            end = min(start + step, self.size_bytes)
            assert actual[start:end] == self.expected_bytes(start, end), \
                f"{label}：偏移 {start} 处内容不一致"

    def reset_stats(self) -> None:
        with self.stats["lock"]:
            self.stats["requests"] = 0
            self.stats["head_requests"] = 0
            self.stats["get_requests"] = 0
            self.stats["range_requests"] = 0
            self.stats["range_headers"] = []
            self.stats["max_concurrent"] = 0
            self.stats["statuses"] = []

    @property
    def request_log(self):
        return list(self.stats["range_headers"])


@pytest.fixture
def range_server():
    """默认：2MB 文件、支持 Range 的本地服务器。"""
    server = RangeTestServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def large_range_server():
    """16MB 文件（够 8~16 线程分块），内容按需生成，不占内存。"""
    server = RangeTestServer(pattern_size=16 * 1024 * 1024)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def big_range_server():
    """64MB 文件 + 64 方 Barrier：这是唯一能让自动调整真的给出 64 线程的大小。

    内容用“周期数据”按需生成，所以服务器和测试都不需要真的申请 64MB 内存。
    所有 64 个 Range 请求必须同时到达，否则 Barrier 会超时失败。
    """
    server = RangeTestServer(
        pattern_size=64 * 1024 * 1024,
        barrier=threading.Barrier(MAX_THREADS),
    )
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 线程数自动调整规则（四种分支）
# ---------------------------------------------------------------------------


def test_threads_forced_one_when_range_unsupported():
    """分支一：不支持 Range → 强制 1 线程。"""
    assert compute_thread_count(500 * 1024 * 1024, False, 64) == 1


def test_threads_forced_one_when_content_length_missing():
    """分支一：content-length 缺失 → 强制 1 线程。"""
    assert compute_thread_count(None, True, 64) == 1
    assert compute_thread_count(0, True, 64) == 1


def test_threads_limited_by_one_megabyte_per_chunk():
    """分支二：每块至少 1MB，所以 N 不会超过 content_length // 1MB。"""
    assert compute_thread_count(3 * 1024 * 1024, True, 64) == 3
    assert compute_thread_count(5 * 1024 * 1024, True, 8) == 5
    assert compute_thread_count(1024 * 1024, True, 64) == 1
    assert compute_thread_count(100, True, 64) == 1  # 极小文件至少 1 线程而不是 0


def test_threads_capped_at_16_for_files_smaller_than_64mb():
    """分支三：小于 64MB 的文件最多 16 线程。"""
    assert compute_thread_count(32 * 1024 * 1024, True, 64) == 16
    assert compute_thread_count(63 * 1024 * 1024, True, 64) == 16


def test_threads_allow_full_64_for_large_files():
    """64MB 及以上：用户选多少就是多少（上限 64）。"""
    assert compute_thread_count(64 * 1024 * 1024, True, 64) == 64
    assert compute_thread_count(1024 * 1024 * 1024, True, 64) == 64
    assert compute_thread_count(1024 * 1024 * 1024, True, 128) == 64  # 超过 64 也不给


def test_threads_respect_user_choice_when_smaller():
    assert compute_thread_count(1024 * 1024 * 1024, True, 4) == 4
    assert compute_thread_count(1024 * 1024 * 1024, True, 8) == 8
    assert compute_thread_count(1024 * 1024 * 1024, True, DEFAULT_THREADS) == 8


def test_threads_handles_garbage_input():
    assert compute_thread_count(1024 * 1024 * 1024, True, "abc") == 8
    assert compute_thread_count(1024 * 1024 * 1024, True, -5) == 1
    assert compute_thread_count("abc", True, 8) == 1


# ---------------------------------------------------------------------------
# 分块区间计算
# ---------------------------------------------------------------------------


def test_plan_chunks_covers_everything_without_overlap():
    for total, threads in [(100, 4), (1000, 8), (1024 * 1024 * 10 + 7, 16), (5, 5), (999, 64)]:
        chunks = plan_chunks(total, threads)
        expected_threads = min(threads, total)
        assert len(chunks) == expected_threads
        assert chunks[0][1] == 0
        assert chunks[-1][2] == total - 1
        for position, (index, start, end) in enumerate(chunks):
            assert index == position
            assert start <= end
            if position:
                assert start == chunks[position - 1][2] + 1  # 连续、无空洞
        # 总字节数必须刚好等于文件大小
        assert sum(end - start + 1 for _i, start, end in chunks) == total


def test_plan_chunks_single_thread():
    chunks = plan_chunks(1000, 1)
    assert chunks == [(0, 0, 999)]


def test_plan_chunks_rejects_bad_input():
    with pytest.raises(ValueError):
        plan_chunks(0, 4)
    with pytest.raises(ValueError):
        plan_chunks(-1, 4)


# ---------------------------------------------------------------------------
# 服务器探测
# ---------------------------------------------------------------------------


def test_probe_head_success(range_server):
    session = make_session()
    try:
        probe = probe_server(session, range_server.url)
    finally:
        session.close()

    assert probe.content_length == range_server.size
    assert probe.range_supported is True
    assert range_server.stats["head_requests"] >= 1


def test_probe_falls_back_to_get_range_when_head_fails():
    """HEAD 被拒绝（403）时，必须回退到 GET Range 探测。"""
    server = RangeTestServer(payload=build_payload(512 * 1024), head_status=403)
    try:
        session = make_session()
        try:
            probe = probe_server(session, server.url)
        finally:
            session.close()
        assert probe.content_length == server.size
        assert probe.range_supported is True
        assert server.stats["head_requests"] == 1
        assert "bytes=0-0" in server.stats["range_headers"]
    finally:
        server.stop()


def test_probe_single_thread_when_range_unsupported():
    """服务器忽略 Range（返回 200 全量）时，必须按单线程处理。"""
    server = RangeTestServer(payload=build_payload(256 * 1024), support_range=False)
    try:
        session = make_session()
        try:
            probe = probe_server(session, server.url)
        finally:
            session.close()
        assert probe.range_supported is False
    finally:
        server.stop()


def test_probe_survives_connection_error():
    """连不上的地址不能抛异常，直接当“不支持分块”处理。"""
    with socket.socket() as sock:  # 占一个端口再立刻关掉，保证没人监听
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    session = make_session()
    try:
        probe = probe_server(session, f"http://127.0.0.1:{port}/x.bin", timeout=1)
    finally:
        session.close()
    assert probe.range_supported is False


# ---------------------------------------------------------------------------
# 单区间下载：重试 & 错误
# ---------------------------------------------------------------------------


def test_download_range_single_chunk(range_server, tmp_path):
    session = make_session()
    part = tmp_path / "a.part0"
    try:
        written = download_range(
            session, range_server.url, part, 0, range_server.size - 1,
            range_server.size, threading.Event(),
        )
    finally:
        session.close()

    assert written == range_server.size
    assert part.read_bytes() == range_server.payload


def test_download_range_retries_403_never(range_server, tmp_path, monkeypatch):
    """403 不重试：只允许发一次请求。"""
    calls = {"count": 0}

    class FakeResponse:
        status_code = 403
        headers = {}

        def iter_content(self, chunk_size=8192):
            return iter(())

        def close(self):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            calls["count"] += 1
            return FakeResponse()

    with pytest.raises(DownloadError) as info:
        download_range(
            FakeSession(), "http://example.com/x", tmp_path / "b.part0", 0, 9, 10,
            threading.Event(),
        )
    assert "拒绝访问" in info.value.message
    assert calls["count"] == 1


def test_download_range_retries_429_then_gives_up(tmp_path):
    """429 要重试，但不减少线程数；超过 3 次后报中文提示。"""
    calls = {"count": 0}

    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "0"}

        def iter_content(self, chunk_size=8192):
            return iter(())

        def close(self):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            calls["count"] += 1
            return FakeResponse()

    with pytest.raises(DownloadError) as info:
        download_range(
            FakeSession(), "http://example.com/x", tmp_path / "c.part0", 0, 9, 10,
            threading.Event(),
        )
    assert "服务器太忙" in info.value.message
    assert calls["count"] == downloader.MAX_RETRY_429 + 1  # 首次 + 3 次重试


def test_download_range_429_backoff_can_be_cancelled(tmp_path):
    """429 退避等待必须会被取消事件立刻打断。"""
    cancel_event = threading.Event()
    calls = {"count": 0}

    class FakeResponse:
        status_code = 429
        headers = {}

        def iter_content(self, chunk_size=8192):
            return iter(())

        def close(self):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            calls["count"] += 1
            cancel_event.set()  # 第一次被限流后用户马上点了取消
            return FakeResponse()

    started = time.monotonic()
    with pytest.raises(DownloadCancelled):
        download_range(
            FakeSession(), "http://example.com/x", tmp_path / "d.part0", 0, 9, 10,
            cancel_event,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 2, "退避等待必须用 cancel_event.wait 打断，不能真的睡满退避时间"
    assert calls["count"] == 1


def test_download_range_retries_transient_then_succeeds(tmp_path):
    """连接类异常按线程独立重试，重试时清空分块文件重新下载。"""
    payload = b"0123456789"
    state = {"calls": 0}

    class FakeResponse:
        status_code = 200
        headers = {"Content-Length": str(len(payload))}

        def iter_content(self, chunk_size=8192):
            half = len(payload) // 2
            yield payload[:half]
            yield payload[half:]

        def close(self):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise downloader.requests.exceptions.ConnectionError("boom")
            return FakeResponse()

    part = tmp_path / "e.part0"
    part.write_bytes(b"stale-data" * 10)  # 模拟残留内容，重试时必须被清空

    written = download_range(
        FakeSession(), "http://example.com/x", part, 0, len(payload) - 1,
        len(payload), threading.Event(),
    )

    assert written == len(payload)
    assert part.read_bytes() == payload
    assert state["calls"] == 2


def test_download_range_wraps_network_error_as_chinese(tmp_path):
    class FakeSession:
        def get(self, url, **kwargs):
            raise downloader.requests.exceptions.ConnectTimeout("timeout")

    with pytest.raises(DownloadError) as info:
        download_range(
            FakeSession(), "http://example.com/x", tmp_path / "f.part0", 0, 9, 10,
            threading.Event(),
        )
    assert info.value.message == downloader.MSG_NETWORK_FAILED
    assert "网络连接失败" in info.value.message


def test_download_range_cancel_during_body(tmp_path):
    """下载过程中取消：立刻抛 DownloadCancelled。"""
    cancel_event = threading.Event()

    class FakeResponse:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size=8192):
            for _ in range(100):
                if cancel_event.is_set():
                    return
                yield b"x" * 64

        def close(self):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            cancel_event.set()
            return FakeResponse()

    with pytest.raises(DownloadCancelled):
        download_range(
            FakeSession(), "http://example.com/x", tmp_path / "g.part0", 0, 6399, 6400,
            cancel_event,
        )


# ---------------------------------------------------------------------------
# 完整下载流程（本地服务器）
# ---------------------------------------------------------------------------


def test_full_download_multi_thread(large_range_server, tmp_path):
    """多线程分块下载 → 合并 → 清理 .partN，文件内容必须完全一致。"""
    target = tmp_path / "payload.bin"
    messages = []
    session = make_session()
    try:
        result = download(
            large_range_server.url, target, threads=8, cancel_event=threading.Event(),
            progress_cb=messages.append, session=session,
        )
    finally:
        session.close()

    assert result == target
    large_range_server.assert_content_matches(target, "16MB 多线程下载")
    # 分块临时文件必须全部删掉
    assert list(tmp_path.glob("*.part*")) == []
    # 状态消息里应该出现“正在合并文件…”
    assert any(m.get("status") == "正在合并文件…" for m in messages)
    assert messages[-1]["type"] == "done"
    # 1 次探测 + 每线程一个 Range 请求
    assert large_range_server.stats["range_requests"] >= 8, large_range_server.stats["range_headers"]


def test_download_uses_64_threads_on_big_file(big_range_server, tmp_path):
    """关键用例：64MB 文件真的用 64 线程分块下载，且服务器同时处理 64 个请求。"""
    target = tmp_path / "big.bin"
    session = make_session()
    try:
        probe = probe_server(session, big_range_server.url)
        assert probe.content_length == big_range_server.size
        threads = compute_thread_count(probe.content_length, probe.range_supported, MAX_THREADS)
        assert threads == MAX_THREADS, f"64MB 文件应该允许 64 线程，实际 {threads}"

        download(
            big_range_server.url, target, threads=threads,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    big_range_server.assert_content_matches(target, "64 线程下载")
    # Barrier 要求 64 个请求同时到达，否则会 BrokenBarrier 超时
    assert big_range_server.stats["range_requests"] == MAX_THREADS
    assert big_range_server.stats["max_concurrent"] >= 2, "服务器必须并发处理请求"
    assert list(tmp_path.glob("*.part*")) == []


def test_download_auto_adjusts_threads_and_reports_status(range_server, tmp_path):
    """用户选 64 线程但文件只有 2MB → 自动调整，并给出中文状态提示。"""
    target = tmp_path / "small.bin"
    messages = []
    session = make_session()
    try:
        download(
            range_server.url, target, threads=MAX_THREADS,
            cancel_event=threading.Event(), progress_cb=messages.append, session=session,
        )
    finally:
        session.close()
    assert target.stat().st_size == range_server.size
    texts = [m.get("status", "") for m in messages]
    assert any(text.startswith("已自动调整为 ") for text in texts), texts


def test_download_single_thread_when_server_ignores_range(tmp_path):
    """服务器不支持 Range 时必须退化成单线程，文件依然要完整。"""
    server = RangeTestServer(payload=build_payload(2 * 1024 * 1024), support_range=False)
    target = tmp_path / "plain.bin"
    try:
        session = make_session()
        try:
            download(
                server.url, target, threads=16, cancel_event=threading.Event(), session=session,
            )
        finally:
            session.close()
        server.assert_content_matches(target, "不支持 Range 的单线程下载")
        # 只有那次 GET Range 探测带过 Range 头，正式下载一次都没带
        assert server.stats["range_requests"] == 1, server.stats["range_headers"]
        assert list(tmp_path.glob("*.part*")) == []
    finally:
        server.stop()


def test_download_cleans_leftover_part_files_before_start(large_range_server, tmp_path):
    """开始下载前要清掉同名的 .partN 残留，避免旧数据混进来。"""
    target = tmp_path / "payload.bin"
    stale = part_file_path(target, 0)
    stale.write_bytes(b"stale-data" * 1000)
    stale2 = part_file_path(target, 7)
    stale2.write_bytes(b"stale-data-2")

    session = make_session()
    try:
        download(
            large_range_server.url, target, threads=4,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    large_range_server.assert_content_matches(target, "残留清理后的下载")
    assert list(tmp_path.glob("*.part*")) == []


def test_download_cancel_removes_all_part_files(tmp_path):
    """取消下载后，所有 .partN 必须被清理。"""
    server = RangeTestServer(payload=build_payload(2 * 1024 * 1024), delay=2.0)
    target = tmp_path / "slow.bin"
    cancel_event = threading.Event()
    captured: list = []

    def run_download():
        # download() 会抛 DownloadCancelled，这里捕获后记录，便于断言
        try:
            download(
                server.url, target, threads=4, cancel_event=cancel_event,
                session=make_session(),
            )
        except BaseException as exc:  # noqa: BLE001
            captured.append(exc)

    try:
        worker = threading.Thread(target=run_download, name="cancel-test", daemon=True)
        worker.start()
        time.sleep(0.6)
        cancel_event.set()
        worker.join(timeout=10)
        assert not worker.is_alive(), "取消后下载线程必须很快结束"
        assert captured and isinstance(captured[0], DownloadCancelled), captured
        assert not target.exists()
        assert list(tmp_path.glob("*.part*")) == [], "取消后必须清理所有分块文件"
    finally:
        server.stop()


def test_download_reports_chinese_error_on_403(tmp_path):
    server = RangeTestServer(payload=build_payload(64 * 1024), get_status=403, head_status=403)
    target = tmp_path / "denied.bin"
    try:
        session = make_session()
        try:
            with pytest.raises(DownloadError) as info:
                download(
                    server.url, target, threads=4,
                    cancel_event=threading.Event(), session=session,
                )
        finally:
            session.close()
        assert info.value.message == downloader.MSG_FORBIDDEN
        assert list(tmp_path.glob("*.part*")) == []
    finally:
        server.stop()


def test_download_reports_chinese_error_on_404(tmp_path):
    server = RangeTestServer(payload=build_payload(64 * 1024), head_status=404, get_status=404)
    target = tmp_path / "missing.bin"
    try:
        session = make_session()
        try:
            with pytest.raises(DownloadError) as info:
                download(server.url, target, threads=2, cancel_event=threading.Event(), session=session)
        finally:
            session.close()
        # 必须是通俗中文提示，而且不含任何英文异常名/堆栈
        assert info.value.message == downloader.MSG_NOT_FOUND
        assert not any(token in info.value.message for token in ("Error", "Exception", "Traceback"))
    finally:
        server.stop()


def test_download_to_directory_parses_chinese_filename(tmp_path):
    """Content-Disposition 里的中文文件名要正确落盘。"""
    server = RangeTestServer(
        payload=build_payload(128 * 1024), content_disposition=CJK_DISPOSITION
    )
    try:
        session = make_session()
        try:
            result = downloader.download_to_directory(
                server.url, tmp_path, threads=4, cancel_event=threading.Event(), session=session,
            )
        finally:
            session.close()
        assert result.name == CJK_FILENAME
        assert result.exists()
        assert result.stat().st_size == server.size
    finally:
        server.stop()


def test_download_to_directory_adds_suffix_for_duplicate(tmp_path):
    server = RangeTestServer(
        payload=build_payload(64 * 1024), content_disposition=CJK_DISPOSITION
    )
    try:
        session = make_session()
        try:
            first = downloader.download_to_directory(
                server.url, tmp_path, threads=2, cancel_event=threading.Event(), session=session,
            )
            second = downloader.download_to_directory(
                server.url, tmp_path, threads=2, cancel_event=threading.Event(), session=session,
            )
        finally:
            session.close()
        assert first.name == CJK_FILENAME
        assert second.name == "中文文件名 (1).bin"
    finally:
        server.stop()


def test_download_unknown_content_length_uses_single_thread(tmp_path):
    """content-length 缺失（HEAD/Range 都不给）时，必须单线程且文件完整。"""

    class NoLengthHandler(_RangeRequestHandler):
        def _send_payload(self, start, end, status, content_length):
            body = self._body_for(start, end)
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    payload = build_payload(100 * 1024)
    stats = {
        "lock": threading.Lock(), "requests": 0, "head_requests": 0, "get_requests": 0,
        "range_requests": 0, "range_headers": [], "max_concurrent": 0, "active": 0,
        "statuses": [],
    }
    config = {
        "payload": payload,
        "pattern": None,
        "size": len(payload),
        "support_range": False,
        "head_status": 200,
        "get_status": 200,
        "content_disposition": "",
        "delay": 0.0,
        "barrier": None,
        "stats": stats,
    }
    httpd = _TestServer(("127.0.0.1", 0), NoLengthHandler, config)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/nolength.bin"
    target = tmp_path / "nolength.bin"
    try:
        session = make_session()
        try:
            download(url, target, threads=16, cancel_event=threading.Event(), session=session)
        finally:
            session.close()
        assert target.stat().st_size == len(payload)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()
        assert stats["range_requests"] == 1  # 只有那次探测带了 Range，正式下载没带
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
