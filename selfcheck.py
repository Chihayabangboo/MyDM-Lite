"""MyDM-Lite 自检脚本（不依赖 pytest，直接运行即可）。

它会启动一个本地 Range 测试服务器，然后把下载核心完整跑一遍：

    python selfcheck.py

检查项：
1. HEAD 探测（Content-Length / Accept-Ranges）
2. HEAD 失败时回退到 GET Range 探测
3. 8 线程分块下载 → 合并 → 文件内容校验（SHA256）
4. 64 线程并发（用 Barrier 证明服务器真的同时在处理 64 个请求）
5. 线程数自动调整规则
6. 取消下载后清理所有 .partN
7. 服务器不支持 Range 时退化成单线程
8. 中文文件名（RFC 5987）解析
9. 大小格式化 / 分块区间
10. 日志文件能否写入

全部通过时退出码是 0，有失败时是 1。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

# 保证能 import 到同目录的模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from downloader import (  # noqa: E402
    MAX_RETRY_429,
    MAX_THREADS,
    DownloadCancelled,
    DownloadError,
    compute_thread_count,
    download,
    download_range,
    download_to_directory,
    make_session,
    plan_chunks,
    probe_server,
)
from utils import (  # noqa: E402
    LOG_FILE_NAME,
    cleanup_part_files,
    format_size,
    get_default_download_dir,
    parse_content_disposition_filename,
    resolve_download_filename,
    setup_logging,
    unique_path,
)

CJK_DISPOSITION = "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87%E5%90%8D%E5%AD%97.bin"
CJK_NAME = "中文名字.bin"

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    """记录一条检查结果。"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name} {extra}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def build_payload(size: int) -> bytes:
    block = hashlib.sha256(b"mydm-lite-selfcheck-payload").digest()
    return (block * (size // len(block) + 1))[:size]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for buffer in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(buffer)
    return digest.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_matches(server, path: Path) -> bool:
    """校验下载到的文件跟服务器提供的内容一致（支持大文件的周期数据模式）。"""
    try:
        server.assert_content_matches(path, "自检下载")
        return True
    except AssertionError:
        return False


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------


def check_pure_functions() -> None:
    section("1. 纯函数（文件名 / 大小 / 分块 / 线程数）")

    check("RFC 5987 中文文件名解码",
          parse_content_disposition_filename(CJK_DISPOSITION) == CJK_NAME)
    check("普通 filename 解析",
          parse_content_disposition_filename('attachment; filename="a.zip"') == "a.zip")
    check("URL 中文文件名解码",
          resolve_download_filename("https://x.com/%E6%B5%8B%E8%AF%95.zip", {}) == "测试.zip")
    check("没有任何信息时用 download.bin",
          resolve_download_filename("https://x.com", None) == "download.bin")

    check("大小格式化 B/KB/MB/GB",
          [format_size(v) for v in (0, 1024, 1024 ** 2, 1024 ** 3)]
          == ["0 B", "1.0 KB", "1.0 MB", "1.0 GB"])

    chunks = plan_chunks(1000, 8)
    covered = sum(end - start + 1 for _i, start, end in chunks)
    contiguous = all(chunks[i][1] == chunks[i - 1][2] + 1 for i in range(1, len(chunks)))
    check("分块区间连续、无重叠、全覆盖", covered == 1000 and contiguous and chunks[0][1] == 0
          and chunks[-1][2] == 999)

    check("不支持 Range → 1 线程", compute_thread_count(500 * 1024 ** 2, False, 64) == 1)
    check("content-length 缺失 → 1 线程", compute_thread_count(None, True, 64) == 1)
    check("每块至少 1MB（3MB 最多 3 线程）", compute_thread_count(3 * 1024 ** 2, True, 64) == 3)
    check("小于 64MB 最多 16 线程", compute_thread_count(32 * 1024 ** 2, True, 64) == 16)
    check("大文件可用满 64 线程", compute_thread_count(1024 ** 3, True, 64) == 64)
    check("用户选 8 线程就尊重用户", compute_thread_count(1024 ** 3, True, 8) == 8)

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "dup.zip"
        target.write_bytes(b"x")
        check("重名自动加 (1)", unique_path(target).name == "dup (1).zip")

    check("默认下载目录可用", isinstance(get_default_download_dir(), Path))


def check_logging() -> None:
    section("2. 日志")
    with tempfile.TemporaryDirectory() as tmp:
        logger = setup_logging(log_dir=tmp, force=True)
        logger.info("自检日志：启动")
        for handler in logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
        log_file = Path(tmp) / LOG_FILE_NAME
        content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        check("mydm.log 正常写入", "自检日志：启动" in content)
        from utils import LOG_BACKUP_COUNT, LOG_MAX_BYTES
        check("5MB 轮转 + 保留 3 个", LOG_MAX_BYTES == 5 * 1024 * 1024 and LOG_BACKUP_COUNT == 3)
        setup_logging(log_dir=tempfile.gettempdir(), force=True)


def check_http_pipeline(server) -> None:
    section("3. 本地服务器：探测 + 多线程下载 + 合并")
    session = make_session()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        probe = probe_server(session, server.url)
        check("HEAD 探测出文件大小", probe.content_length == server.size,
              f"（得到 {probe.content_length}，期望 {server.size}）")
        check("HEAD 探测出支持 Range", probe.range_supported is True)

        target = tmp_path / "payload.bin"
        messages = []
        download(server.url, target, threads=8, cancel_event=threading.Event(),
                 progress_cb=messages.append, session=session)
        check("8 线程下载后文件大小一致", target.stat().st_size == server.size)
        check("8 线程下载后内容一致（SHA256）", sha256_of(target) == sha256_of_bytes(server.payload))
        check("所有 .partN 已清理", list(tmp_path.glob("*.part*")) == [])
        check("状态里出现“正在合并文件…”",
              any(m.get("status") == "正在合并文件…" for m in messages))

        # 再跑一次：用户选 64 线程，但文件只有 2MB，应该自动调整并给出提示
        adjusted = download_collect(server, tmp_path / "adjusted.bin", 64, session)
        check("线程数自动调整提示（已自动调整为 N 线程）", any(
            str(m.get("status", "")).startswith("已自动调整为 ") for m in adjusted
        ), f"（状态：{[m.get('status') for m in adjusted][:4]}）")

        # 网络失败的中文提示（连不上的地址）
        try:
            download("http://127.0.0.1:1/nope.bin", tmp_path / "nope.bin", threads=2,
                     cancel_event=threading.Event(), session=session)
            check("连不上时抛出中文错误", False, "（居然没报错）")
        except DownloadError as exc:
            check("连不上时抛出中文错误", "网络连接失败" in exc.message, f"（{exc.message}）")
    session.close()


def download_collect(server, target: Path, threads: int, session) -> list:
    """跑一次下载并把消息收集起来（给上面那条检查用）。"""
    messages: list = []
    download(server.url, target, threads=threads, cancel_event=threading.Event(),
             progress_cb=messages.append, session=session)
    return messages


def check_64_threads(server) -> None:
    section("4. 64 线程并发（Barrier 证明服务器同时在处理 64 个请求）")
    session = make_session()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "big.bin"
        started = time.monotonic()
        download(server.url, target, threads=MAX_THREADS, cancel_event=threading.Event(),
                 session=session)
        elapsed = time.monotonic() - started
        check("64 线程下载内容一致", content_matches(server, target))
        check("服务器收到 64 个 Range 请求",
              server.stats["range_requests"] == MAX_THREADS,
              f"（实际 {server.stats['range_requests']}）")
        check("服务器确实并发了（max_concurrent >= 2）",
              server.stats["max_concurrent"] >= 2,
              f"（实际 {server.stats['max_concurrent']}）")
        print(f"        （64 线程下载 {format_size(server.size)} 耗时 {elapsed:.2f} 秒）")
    session.close()


def check_range_retry(server) -> None:
    section("5. 单区间重试 & 错误翻译")
    session = make_session()
    with tempfile.TemporaryDirectory() as tmp:
        part = Path(tmp) / "retry.part0"
        part.write_bytes(b"stale-data" * 100)
        written = download_range(session, server.url, part, 0, 999, 1000, threading.Event())
        check("单区间下载并覆盖旧内容", written == 1000 and part.read_bytes() == server.payload[:1000])

        class FakeResponse:
            status_code = 403
            headers: dict = {}

            def iter_content(self, chunk_size=8192):
                return iter(())

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                return FakeResponse()

        fake = FakeSession()
        try:
            download_range(fake, "http://example.com/x", Path(tmp) / "f.part0", 0, 9, 10,
                           threading.Event())
            check("403 直接报中文错误", False, "（没有抛错）")
        except DownloadError as exc:
            check("403 直接报中文错误", "服务器拒绝访问" in exc.message, f"（{exc.message}）")
            check("403 不重试（只请求 1 次）", fake.calls == 1, f"（请求了 {fake.calls} 次）")

        # 429：指数退避最多重试 3 次，且能被取消打断
        class Fake429(FakeResponse):
            status_code = 429
            headers = {"Retry-After": "0"}

        class FakeSession429(FakeSession):
            def get(self, url, **kwargs):
                self.calls += 1
                return Fake429()

        fake429 = FakeSession429()
        try:
            download_range(fake429, "http://example.com/x", Path(tmp) / "g.part0", 0, 9, 10,
                           threading.Event())
            check("429 重试到上限后报中文错误", False, "（没有抛错）")
        except DownloadError as exc:
            check("429 重试到上限后报中文错误", "服务器太忙" in exc.message, f"（{exc.message}）")
            check("429 最多重试 3 次", fake429.calls == MAX_RETRY_429 + 1,
                  f"（请求了 {fake429.calls} 次）")

        cancel_event = threading.Event()

        class FakeSession429Cancel(FakeSession):
            def get(self, url, **kwargs):
                self.calls += 1
                cancel_event.set()  # 第一次被限流后立刻取消
                return Fake429()

        fake_cancel = FakeSession429Cancel()
        started = time.monotonic()
        try:
            download_range(fake_cancel, "http://example.com/x", Path(tmp) / "h.part0", 0, 9, 10,
                           cancel_event)
            check("429 退避可被取消打断", False, "（没有抛错）")
        except DownloadCancelled:
            elapsed = time.monotonic() - started
            check("429 退避可被取消打断", elapsed < 2, f"（耗时 {elapsed:.2f} 秒）")
    session.close()


def check_cancel(server_factory) -> None:
    section("6. 取消下载后清理临时文件")
    server = server_factory()
    session = make_session()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        target = tmp_path / "slow.bin"
        cancel_event = threading.Event()
        captured: list = []

        def run():
            try:
                download(server.url, target, threads=4, cancel_event=cancel_event,
                         session=make_session())
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.6)
        cancel_event.set()
        worker.join(timeout=15)
        check("取消后下载线程很快退出", not worker.is_alive())
        check("取消抛的是 DownloadCancelled",
              bool(captured) and isinstance(captured[0], DownloadCancelled),
              f"（{captured}）")
        check("取消后没有残留 .partN", list(tmp_path.glob("*.part*")) == [])
        check("取消后没有生成半成品文件", not target.exists())
    session.close()
    server.stop()


def check_no_range(server_factory) -> None:
    section("7. 服务器不支持 Range → 单线程")
    server = server_factory()
    session = make_session()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "plain.bin"
        probe = probe_server(session, server.url)
        check("探测出不支持 Range", probe.range_supported is False)
        check("不支持 Range 时线程数降为 1", compute_thread_count(probe.content_length, False, 16) == 1)
        download(server.url, target, threads=16, cancel_event=threading.Event(), session=session)
        check("单线程下载内容一致", sha256_of(target) == sha256_of_bytes(server.payload))
        # 只有“GET Range 探测”会带 Range 头（本自检探测一次 + download 内部再探测一次），
        # 正式下载全程没有带 Range。
        check("单线程下载没发过带 Range 的正式请求", server.stats["range_requests"] == 2,
              f"（发了 {server.stats['range_requests']} 次）")
    session.close()
    server.stop()


def check_filename_and_cleanup(server_factory) -> None:
    section("8. 中文文件名落盘 + 重名递增 + 残留清理")
    server = server_factory(content_disposition=CJK_DISPOSITION)
    session = make_session()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        first = download_to_directory(server.url, tmp_path, threads=4,
                                      cancel_event=threading.Event(), session=session)
        second = download_to_directory(server.url, tmp_path, threads=4,
                                       cancel_event=threading.Event(), session=session)
        check("Content-Disposition 中文名落盘", first.name == CJK_NAME, f"（{first.name}）")
        check("重名自动加 (1)", second.name == "中文名字 (1).bin", f"（{second.name}）")

        stale = tmp_path / "残留.bin.part0"
        stale.write_bytes(b"stale-data")
        target = tmp_path / "残留.bin"
        download(server.url, target, threads=4, cancel_event=threading.Event(), session=session)
        check("开始前清掉同名 .partN 残留", not stale.exists() and target.exists())
        check("结束时清理干净", list(tmp_path.glob("*.part*")) == [])
    session.close()
    server.stop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    global FAILED

    # 这里才 import 测试服务器，避免影响主程序
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
    from test_range_server import MAX_THREADS as SERVER_MAX, RangeTestServer, build_payload

    assert SERVER_MAX == MAX_THREADS

    print("MyDM-Lite 自检开始\n" + "=" * 50)

    check_pure_functions()
    check_logging()

    try:
        default_server = RangeTestServer(payload=build_payload(2 * 1024 * 1024))
        try:
            check_http_pipeline(default_server)
        finally:
            default_server.stop()

        big_server = RangeTestServer(
            pattern_size=64 * 1024 * 1024,
            barrier=threading.Barrier(MAX_THREADS),
        )
        try:
            check_64_threads(big_server)
        finally:
            big_server.stop()

        range_server = RangeTestServer(payload=build_payload(1 * 1024 * 1024))
        try:
            check_range_retry(range_server)
        finally:
            range_server.stop()

        check_cancel(lambda: RangeTestServer(payload=build_payload(2 * 1024 * 1024), delay=2.0))
        check_no_range(lambda: RangeTestServer(payload=build_payload(256 * 1024), support_range=False))
        check_filename_and_cleanup(lambda **kwargs: RangeTestServer(
            payload=build_payload(128 * 1024), **kwargs))
    except Exception:
        FAILED += 1
        print("\n[异常] 自检过程中出现未预期错误：")
        traceback.print_exc()

    print("\n" + "=" * 50)
    if FAILED:
        print(f"自检结束：通过 {PASSED} 项，失败 {FAILED} 项")
        return 1
    print(f"自检结束：全部 {PASSED} 项通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
