"""断点续传（``.download.json``）单元测试 + 本地 Range 服务器集成测试。

覆盖需求里的四块核心逻辑：

1. **元数据读写**：``build_metadata`` / ``write_metadata`` / ``read_metadata``
   的字段、原子写入、损坏数据兜底、``.partN`` 字节数对齐。
2. **分块字节校验**：``inspect_part_files`` 按区间裁剪、``load_resume_state``
   对分块布局 / 总大小 / URL / Range 支持的校验。
3. **断点区间计算**：``plan_chunks_resume`` 算出的 ``resume_bytes`` /
   ``fetch_start`` / ``complete``（用本地服务器验证“只请求缺的那段 Range”）。
4. **不支持 Range 的降级**：保留 ``.partN`` 但服务器不支持 Range 时，
   必须清空临时文件 + 元数据，退回单线程从头下载。

服务器 fixture 直接复用 ``test_range_server.py`` 里的 ``RangeTestServer``。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

import downloader
from downloader import (
    DownloadCancelled,
    DownloadError,
    download,
    download_range,
    make_session,
    plan_chunks,
    plan_chunks_resume,
)
from test_range_server import RangeTestServer, build_payload
from utils import (
    METADATA_SUFFIX,
    build_metadata,
    cleanup_part_files,
    has_resumable_state,
    inspect_part_files,
    load_resume_state,
    metadata_file_path,
    part_file_path,
    read_metadata,
    remove_metadata,
    write_metadata,
)

URL = "http://127.0.0.1:9/payload.bin"


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def make_state(target, size: int, threads: int, part_sizes, url: str = URL,
               server=None, chunks=None, real: bool = False) -> dict:
    """在磁盘上造一个“上次没下完”的现场：元数据 + 指定大小的 .partN。

    ``part_sizes`` 是每个分块已有的字节数。``real=True`` 时必须同时传 ``server``，
    这样写进 ``.partN`` 的是**服务器上真实的字节**（前缀），续传后内容才可能拼得对；
    否则只写零填充（够用来测区间计算，但内容对不上）。
    """
    target = Path(target)
    if chunks is None:
        chunks = plan_chunks(size, threads)
    for position, chunk in enumerate(chunks):
        index = chunk[0]
        part = part_file_path(target, index)
        size_for_part = part_sizes[position] if position < len(part_sizes) else 0
        if not size_for_part:
            if part.exists():
                part.unlink()
            continue
        if real:
            start = chunk[1]
            part.write_bytes(server.expected_bytes(start, start + size_for_part))
        else:
            part.write_bytes(b"\x00" * size_for_part)
    metadata = build_metadata(
        url=url, target=target, total_size=size, chunks=chunks,
        part_sizes=part_sizes, threads=threads, range_supported=True,
    )
    assert write_metadata(target, metadata) is not None
    return metadata


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for buffer in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(buffer)
    return digest.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def resume_server():
    """4MB、支持 Range 的本地服务器（够切成 4 块，跑得快）。"""
    server = RangeTestServer(payload=build_payload(4 * 1024 * 1024))
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def slow_resume_server():
    """带 2 秒延时的服务器：给取消测试留出足够宽的时间窗口。"""
    server = RangeTestServer(payload=build_payload(2 * 1024 * 1024), delay=2.0)
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 1. 元数据读写
# ---------------------------------------------------------------------------


def test_metadata_path_is_file_name_plus_suffix(tmp_path):
    """元数据文件名必须是 ``<文件名>.download.json``，且和目标文件同目录。"""
    target = tmp_path / "movie.mp4"
    assert metadata_file_path(target).name == "movie.mp4" + METADATA_SUFFIX
    assert metadata_file_path(target) == tmp_path / ("movie.mp4.download.json")
    assert metadata_file_path(target).name.endswith(".download.json")


def test_build_metadata_records_required_fields(tmp_path):
    """需求要求的字段一个都不能少：URL、总大小、分块、字节数、线程数、时间戳。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 4)
    metadata = build_metadata(
        url=URL, target=target, total_size=1000, chunks=chunks,
        part_sizes=[250, 0, 100, 250], threads=4, range_supported=True,
    )

    assert metadata["version"] == 1
    assert metadata["url"] == URL
    assert metadata["total_size"] == 1000
    assert metadata["threads"] == 4
    assert metadata["range_supported"] is True
    assert metadata["created_at"] and metadata["updated_at"]

    layout = metadata["chunks"]
    assert [item["index"] for item in layout] == [0, 1, 2, 3]
    assert [item["start"] for item in layout] == [0, 250, 500, 750]
    assert [item["end"] for item in layout] == [249, 499, 749, 999]
    assert [item["expected"] for item in layout] == [250, 250, 250, 250]
    assert [item["downloaded"] for item in layout] == [250, 0, 100, 250]
    assert [item["done"] for item in layout] == [True, False, False, True]
    # 每个分块都要记下自己的 .partN 文件名，恢复时才对得上
    assert layout[0]["file"] == "a.bin.part0"
    assert layout[3]["file"] == "a.bin.part3"


def test_metadata_round_trip(tmp_path):
    """写进去再读出来，内容必须一致（含中文字段）。"""
    target = tmp_path / "中文文件.bin"
    chunks = plan_chunks(4096, 4)
    metadata = build_metadata(
        url="https://example.com/%E4%B8%AD%E6%96%87.bin", target=target,
        total_size=4096, chunks=chunks, part_sizes=[1024, 1024, 0, 0], threads=4,
    )
    path = write_metadata(target, metadata)

    assert path is not None and path.exists()
    loaded = read_metadata(target)
    assert loaded is not None
    assert loaded["url"] == "https://example.com/%E4%B8%AD%E6%96%87.bin"
    assert loaded["total_size"] == 4096
    assert [item["downloaded"] for item in loaded["chunks"]] == [1024, 1024, 0, 0]
    # JSON 必须是人能看懂的中文（ensure_ascii=False），方便用户自己排查
    assert "中文文件.bin" in path.read_text(encoding="utf-8")


def test_write_metadata_is_atomic_and_leaves_no_temp(tmp_path):
    """原子写：不能留下 .tmp 半截文件，且重复写要覆盖旧内容。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)

    write_metadata(target, build_metadata(URL, target, 1000, chunks, [100, 0], 2))
    write_metadata(target, build_metadata(URL, target, 1000, chunks, [500, 500], 2))

    names = sorted(entry.name for entry in tmp_path.iterdir())
    assert names == ["a.bin.download.json"]
    loaded = read_metadata(target)
    assert [item["downloaded"] for item in loaded["chunks"]] == [500, 500]


@pytest.mark.parametrize(
    "content",
    [
        "这不是 JSON",
        "{",
        "[]",
        "null",
        json.dumps({"url": URL}),  # 缺少 chunks
        json.dumps({"url": URL, "chunks": []}),  # 空分块
    ],
)
def test_read_metadata_ignores_broken_files(tmp_path, content):
    """元数据损坏时返回 None（当成没有断点），绝不抛异常。"""
    target = tmp_path / "a.bin"
    metadata_file_path(target).write_text(content, encoding="utf-8")
    assert read_metadata(target) is None


def test_read_metadata_missing_file_returns_none(tmp_path):
    assert read_metadata(tmp_path / "nope.bin") is None


def test_remove_metadata_deletes_file_and_temp(tmp_path):
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)
    write_metadata(target, build_metadata(URL, target, 1000, chunks, [0, 0], 2))
    temp = metadata_file_path(target).with_name(
        metadata_file_path(target).name + ".tmp"
    )
    temp.write_text("half", encoding="utf-8")

    assert remove_metadata(target) is True
    assert not metadata_file_path(target).exists()
    assert not temp.exists()
    # 再删一次不报错
    assert remove_metadata(target) is False


def test_write_metadata_survives_readonly_location(tmp_path, monkeypatch):
    """写不了元数据也不能让下载崩掉，只返回 None。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)
    metadata = build_metadata(URL, target, 1000, chunks, [0, 0], 2)

    def boom(*_args, **_kwargs):
        raise OSError("没有权限")

    monkeypatch.setattr("builtins.open", boom)
    assert write_metadata(target, metadata) is None


# ---------------------------------------------------------------------------
# 2. 分块字节校验
# ---------------------------------------------------------------------------


def test_inspect_part_files_reads_actual_sizes(tmp_path):
    """字节数必须来自磁盘上的 .partN，而不是元数据里的自述。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 4)
    part_file_path(target, 0).write_bytes(b"x" * 250)
    part_file_path(target, 2).write_bytes(b"x" * 30)
    # part1 / part3 不存在

    assert inspect_part_files(target, chunks) == [250, 0, 30, 0]


def test_inspect_part_files_clamps_oversized_parts(tmp_path):
    """分块文件比区间还大（脏数据）时按“下满了”处理，绝不返回超界数值。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 4)
    part_file_path(target, 0).write_bytes(b"x" * 9999)  # 远超 250

    sizes = inspect_part_files(target, chunks)
    assert sizes[0] == 250, "超大分块必须被裁剪到区间长度"
    assert sizes == [250, 0, 0, 0]


def test_inspect_part_files_survives_missing_directory(tmp_path):
    target = tmp_path / "no-such-dir" / "a.bin"
    assert inspect_part_files(target, plan_chunks(1000, 2)) == [0, 0]


def test_has_resumable_state_needs_both_metadata_and_parts(tmp_path):
    """只有元数据、没有 .partN 时不算断点（避免空续传）。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)
    write_metadata(target, build_metadata(URL, target, 1000, chunks, [0, 0], 2))
    assert has_resumable_state(target) is False

    part_file_path(target, 0).write_bytes(b"x" * 10)
    assert has_resumable_state(target) is True

    # 只有分块没有元数据 → 也不能续（无法校验布局）
    remove_metadata(target)
    assert has_resumable_state(target) is False


def test_has_resumable_state_ignores_lookalike_files(tmp_path):
    """``.partx`` 这种像但不是的残留不能被当成断点。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)
    write_metadata(target, build_metadata(URL, target, 1000, chunks, [0, 0], 2))
    (tmp_path / "a.bin.partx").write_bytes(b"x")
    assert has_resumable_state(target) is False


def test_load_resume_state_reports_downloaded_bytes(tmp_path):
    target = tmp_path / "a.bin"
    make_state(target, 1000, 4, [250, 250, 0, 100])

    state = load_resume_state(target, url=URL)
    assert state is not None
    assert state.total_size == 1000
    assert state.part_sizes == [250, 250, 0, 100]
    assert state.downloaded_bytes == 600
    assert state.completed_indexes() == [0, 1]


def test_load_resume_state_rejects_changed_url(tmp_path):
    """换了链接就不能沿用旧分块（否则合并出来是垃圾）。"""
    target = tmp_path / "a.bin"
    make_state(target, 1000, 4, [250, 0, 0, 0])

    assert load_resume_state(target, url="https://other.example.com/x.bin") is None
    assert load_resume_state(target, url=URL) is not None


def test_load_resume_state_rejects_metadata_marked_no_range(tmp_path):
    """服务器不支持 Range 时，断点状态直接作废。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)
    part_file_path(target, 0).write_bytes(b"x" * 100)
    metadata = build_metadata(URL, target, 1000, chunks, [100, 0], 2, range_supported=False)
    write_metadata(target, metadata)

    assert load_resume_state(target, url=URL) is None


@pytest.mark.parametrize(
    "chunks, total_size",
    [
        # 有空洞：0-249 之后直接跳到 500
        ([{"index": 0, "start": 0, "end": 249},
          {"index": 1, "start": 500, "end": 999}], 1000),
        # index 不连续
        ([{"index": 0, "start": 0, "end": 499},
          {"index": 5, "start": 500, "end": 999}], 1000),
        # 首块不从 0 开始
        ([{"index": 0, "start": 10, "end": 999}], 1000),
        # 末块没吃到文件结尾
        ([{"index": 0, "start": 0, "end": 500},
          {"index": 1, "start": 501, "end": 900}], 1000),
        # start/end 反了
        ([{"index": 0, "start": 100, "end": 0}], 1000),
    ],
)
def test_load_resume_state_rejects_broken_layout(tmp_path, chunks, total_size):
    """分块布局不自洽时宁可不续传，也不能拼出错文件。"""
    target = tmp_path / "a.bin"
    part_file_path(target, 0).write_bytes(b"x" * 10)
    write_metadata(target, {
        "version": 1, "url": URL, "total_size": total_size,
        "range_supported": True, "threads": len(chunks), "chunks": chunks,
    })
    assert load_resume_state(target, url=URL) is None


def test_load_resume_state_missing_metadata_returns_none(tmp_path):
    assert load_resume_state(tmp_path / "a.bin", url=URL) is None


# ---------------------------------------------------------------------------
# 3. 断点区间计算（纯函数）
# ---------------------------------------------------------------------------


def test_plan_chunks_resume_fresh_download_starts_from_zero():
    chunks = plan_chunks(1000, 4)
    plan = plan_chunks_resume(chunks, [0, 0, 0, 0])

    assert [item["resume_bytes"] for item in plan] == [0, 0, 0, 0]
    assert [item["fetch_start"] for item in plan] == [0, 250, 500, 750]
    assert [item["complete"] for item in plan] == [False, False, False, False]


def test_plan_chunks_resume_skips_downloaded_bytes():
    """核心断言：Range 起点 = 原始起点 + 已有字节数。"""
    chunks = plan_chunks(1000, 4)
    plan = plan_chunks_resume(chunks, [250, 100, 0, 40])

    assert [item["resume_bytes"] for item in plan] == [250, 100, 0, 40]
    assert [item["fetch_start"] for item in plan] == [0 + 250, 250 + 100, 500, 750 + 40]
    assert [item["fetch_start"] for item in plan] == [250, 350, 500, 790]
    # 每块还差多少字节 = 区间长度 - 已有字节
    assert [item["expected"] - item["resume_bytes"] for item in plan] == [0, 150, 250, 210]
    # 第二块虽然没下完，但也只请求 350-499 这一段
    assert plan[1]["complete"] is False
    assert plan[1]["end"] == 499


def test_plan_chunks_resume_marks_finished_chunks_complete():
    chunks = plan_chunks(1000, 4)
    plan = plan_chunks_resume(chunks, [250, 250, 0, 250])

    assert [item["complete"] for item in plan] == [True, True, False, True]
    # 已完成的分块 fetch_start 会越界（等于 250 / 500 / 1000），
    # 上层靠 complete 跳过它们，一个字节都不请求。
    assert plan[0]["fetch_start"] == 250
    assert plan[3]["fetch_start"] == 1000


def test_plan_chunks_resume_handles_garbage_and_oversized_sizes():
    """脏数据（负数 / None / 超大 / 长度不匹配）必须被夹到 [0, 区间长度]。"""
    chunks = plan_chunks(1000, 2)
    plan = plan_chunks_resume(chunks, [-5, 99999])
    assert [item["resume_bytes"] for item in plan] == [0, 500]
    assert [item["complete"] for item in plan] == [False, True]

    # part_sizes 比 chunks 短的时候，缺的部分当 0 处理
    short = plan_chunks_resume(chunks, [100])
    assert [item["resume_bytes"] for item in short] == [100, 0]

    assert plan_chunks_resume(chunks, [None, "abc"])[0]["resume_bytes"] == 0


def test_plan_chunks_resume_preserves_layout():
    """续传计划必须原样保留分块布局（合并顺序靠它）。"""
    chunks = plan_chunks(10 * 1024 * 1024 + 7, 8)
    plan = plan_chunks_resume(chunks, [0] * 8)
    assert [(i["index"], i["start"], i["end"]) for i in plan] == list(chunks)


# ---------------------------------------------------------------------------
# 4. 单区间续传（download_range）
# ---------------------------------------------------------------------------


def test_download_range_resumes_only_missing_bytes(resume_server, tmp_path):
    """续传只请求缺的那段，并且结果是完整的：已有前缀 + 新下载后缀。"""
    part = tmp_path / "a.part0"
    start, end = 0, resume_server.size - 1
    existing = 256 * 1024
    # 先写入正确的“已下载”前缀（真实场景是上次中断留下的）
    part.write_bytes(resume_server.expected_bytes(start, start + existing))

    session = make_session()
    try:
        written = download_range(
            session, resume_server.url, part, start, end, resume_server.size,
            threading.Event(), resume_from=existing,
        )
    finally:
        session.close()

    assert written == resume_server.size
    assert part.stat().st_size == resume_server.size
    assert sha256_of(part) == sha256_of_bytes(resume_server.payload)

    starts = resume_server.range_starts()
    assert existing in starts, f"必须从 {existing} 开始请求，实际 {starts}"
    assert 0 not in starts, "续传时不能从 0 重新下载整块"


def test_download_range_resume_zero_truncates_old_content(tmp_path):
    """``resume_from=0``（从头下载）必须覆盖旧内容，不能追加。"""
    payload = b"0123456789"
    state = {"calls": 0}

    class FakeResponse:
        status_code = 200
        headers = {"Content-Length": str(len(payload))}

        def iter_content(self, chunk_size=8192):
            yield payload

        def close(self):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            state["calls"] += 1
            return FakeResponse()

    part = tmp_path / "a.part0"
    part.write_bytes(b"stale-data" * 10)

    written = download_range(
        FakeSession(), "http://example.com/x", part, 0, len(payload) - 1,
        len(payload), threading.Event(), resume_from=0,
    )
    assert written == len(payload)
    assert part.read_bytes() == payload


def test_download_range_skips_fully_downloaded_chunk(resume_server, tmp_path):
    """已经完整的分块应该一个请求都不发。"""
    part = tmp_path / "a.part0"
    part.write_bytes(resume_server.payload)
    before = resume_server.stats["range_requests"]

    session = make_session()
    try:
        written = download_range(
            session, resume_server.url, part, 0, resume_server.size - 1,
            resume_server.size, threading.Event(), resume_from=resume_server.size,
        )
    finally:
        session.close()

    assert written == resume_server.size
    assert resume_server.stats["range_requests"] == before, "完整分块不该再发请求"


def test_download_range_restarts_chunk_when_server_ignores_range(tmp_path):
    """服务器忽略 Range（返回 200 全量）时，绝不能把全量内容追加到旧数据后面。"""
    payload = b"abcdefghij" * 100
    part = tmp_path / "a.part0"
    part.write_bytes(payload[:50])  # 假装已经下了 50 字节

    class FakeResponse:
        status_code = 200  # 服务器没理 Range
        headers = {"Content-Length": str(len(payload))}

        def iter_content(self, chunk_size=8192):
            yield payload

        def close(self):
            pass

    class FakeSession:
        def __init__(self):
            self.headers = []

        def get(self, url, **kwargs):
            self.headers.append((kwargs.get("headers") or {}).get("Range"))
            return FakeResponse()

    fake = FakeSession()
    written = download_range(
        fake, "http://example.com/x", part, 0, len(payload) - 1,
        len(payload), threading.Event(), resume_from=50,
    )

    assert written == len(payload)
    assert part.read_bytes() == payload, "收到 200 全量时必须整块重写，不能追加"
    assert fake.headers == ["bytes=50-999"], "第一次仍应尝试带 Range 请求"


def test_download_range_rejects_wrong_content_range(tmp_path):
    """206 但区间起点不对（服务器糊弄）时必须重试，绝不能追加错位数据。"""

    class FakeResponse:
        status_code = 206
        headers = {"Content-Range": "bytes 0-9/100"}

        def iter_content(self, chunk_size=8192):
            yield b"0123456789"

        def close(self):
            pass

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return FakeResponse()

    part = tmp_path / "a.part0"
    fake = FakeSession()
    with pytest.raises(DownloadError):
        download_range(
            fake, "http://example.com/x", part, 50, 99, 50,
            threading.Event(), resume_from=10,
        )
    # 首次 + MAX_RETRY_OTHER 次重试
    assert fake.calls == downloader.MAX_RETRY_OTHER + 1


def test_download_range_keeps_bytes_on_cancel(tmp_path):
    """取消时必须保留已经落盘的字节（而且是从已有字节处接着写的），下次还能续。"""
    cancel_event = threading.Event()
    existing = 128
    target_length = 10000  # 本块区间 [0, 9999]
    # 第一次 get 正常返回前 512 字节，之后用户取消
    payload = b"y" * 512

    class FakeResponse:
        status_code = 206
        headers = {"Content-Range": f"bytes {existing}-{target_length - 1}/{target_length}"}

        def iter_content(self, chunk_size=8192):
            yield payload
            cancel_event.set()  # 写完这 512 字节后用户点了取消

        def close(self):
            pass

    class FakeSession:
        def __init__(self):
            self.headers = []

        def get(self, url, **kwargs):
            self.headers.append(kwargs.get("headers"))
            return FakeResponse()

    part = tmp_path / "a.part0"
    part.write_bytes(b"x" * existing)  # 上次已经下好的 128 字节

    fake = FakeSession()
    with pytest.raises(DownloadCancelled):
        download_range(
            fake, "http://example.com/x", part, 0, target_length - 1,
            target_length, cancel_event, resume_from=existing,
        )

    # 断点必须保留：旧字节 + 这次新写的字节都在，且是“接着写”而不是从头覆盖
    assert part.exists(), "取消后分块文件必须保留（断点续传的前提）"
    assert part.stat().st_size == existing + len(payload)
    assert part.read_bytes() == b"x" * existing + payload
    # 而且必须是从已有字节处开始请求的
    assert fake.headers[0] == {"Range": f"bytes={existing}-{target_length - 1}"}


# ---------------------------------------------------------------------------
# 5. 完整下载：断点续传 / 清理 / 降级（本地服务器）
# ---------------------------------------------------------------------------


def test_full_download_writes_and_removes_metadata(resume_server, tmp_path):
    """正常下载：过程中有元数据，合并成功后元数据和 .partN 全部消失。"""
    target = tmp_path / "payload.bin"
    session = make_session()
    try:
        download(
            resume_server.url, target, threads=4,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    resume_server.assert_content_matches(target, "首次完整下载")
    assert not metadata_file_path(target).exists(), "完成后必须删除元数据"
    assert list(tmp_path.glob("*.part*")) == [], "完成后必须删除所有分块"


def test_full_download_resumes_from_saved_parts(resume_server, tmp_path):
    """核心集成用例：造一半真实现场 → 再下载 → 只请求缺的 Range，最终文件正确且干净。"""
    target = tmp_path / "payload.bin"
    threads = 4
    size = resume_server.size
    chunks = plan_chunks(size, threads)
    # 前两块已经下完（内容是服务器上真实的前缀），后两块完全没下
    full = [chunks[index][2] - chunks[index][1] + 1 for index in range(threads)]
    make_state(target, size, threads, [full[0], full[1], 0, 0],
               url=resume_server.url, server=resume_server, chunks=chunks, real=True)

    session = make_session()
    try:
        download(
            resume_server.url, target, threads=threads,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    resume_server.assert_content_matches(target, "断点续传下载")
    # 已经下满的两块不该再被请求
    starts = resume_server.range_starts()
    assert starts, "续传时必须有 Range 请求"
    assert 0 not in starts, f"第一块已下满，不该从 0 再下：{starts}"
    assert chunks[1][1] not in starts, f"第二块已下满，不该从 {chunks[1][1]} 再下：{starts}"
    assert chunks[2][1] in starts or chunks[3][1] in starts, f"没下完的块必须继续下：{starts}"
    # 收尾清理
    assert not metadata_file_path(target).exists()
    assert list(tmp_path.glob("*.part*")) == []


def test_full_download_resumes_partial_chunk(resume_server, tmp_path):
    """分块只下了一半：Range 起点必须等于“原始起点 + 已有一半”。"""
    target = tmp_path / "payload.bin"
    size = resume_server.size
    threads = 2
    chunks = plan_chunks(size, threads)
    chunk_size = chunks[0][2] - chunks[0][1] + 1
    existing = 100 * 1024
    # 第一块下满，第二块只下了 existing 字节（内容是真实前缀，续传才能拼对）
    make_state(target, size, threads, [chunk_size, existing], url=resume_server.url,
               server=resume_server, chunks=chunks, real=True)

    session = make_session()
    try:
        download(
            resume_server.url, target, threads=threads,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    resume_server.assert_content_matches(target, "半块续传")
    starts = resume_server.range_starts()
    assert chunk_size + existing in starts, f"应从 {chunk_size + existing} 续下：{starts}"


def test_download_to_directory_keeps_name_for_resume(resume_server, tmp_path):
    """断点未完成时再次下载必须沿用原文件名（不能变成 xxx (1).bin）。"""
    target = tmp_path / "payload.bin"
    size = resume_server.size
    threads = 4
    chunks = plan_chunks(size, threads)
    full0 = chunks[0][2] - chunks[0][1] + 1
    make_state(target, size, threads, [full0, 0, 0, 0], url=resume_server.url,
               server=resume_server, chunks=chunks, real=True)

    session = make_session()
    try:
        result = downloader.download_to_directory(
            resume_server.url, tmp_path, threads=threads,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    assert result.name == "payload.bin", f"续传不该改名，实际 {result.name}"
    assert not (tmp_path / "payload (1).bin").exists()
    resume_server.assert_content_matches(result, "按目录续传")


def test_cancel_keeps_metadata_and_parts_for_resume(slow_resume_server, tmp_path):
    """取消：保留元数据和 .partN，并且下次能接着下完。"""
    server = slow_resume_server
    target = tmp_path / "slow.bin"
    cancel_event = threading.Event()
    captured: list = []

    def run():
        try:
            download(
                server.url, target, threads=4, cancel_event=cancel_event,
                session=make_session(),
            )
        except BaseException as exc:  # noqa: BLE001
            captured.append(exc)

    worker = threading.Thread(target=run, name="resume-cancel-test", daemon=True)
    worker.start()
    # 等元数据出现（说明断点已经建立），然后立刻取消
    for _ in range(400):
        if metadata_file_path(target).exists():
            break
        time.sleep(0.02)
    cancel_event.set()
    worker.join(timeout=30)

    assert not worker.is_alive(), "取消后下载线程必须很快结束"
    assert captured and isinstance(captured[0], DownloadCancelled), captured

    metadata = read_metadata(target)
    assert metadata is not None, "取消后必须保留元数据，否则没法续传"
    assert has_resumable_state(target), "取消后必须至少留下一个 .partN"

    # 再下一次：应该接着续，并且最终文件完整
    session = make_session()
    try:
        download(
            server.url, target, threads=4,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    server.assert_content_matches(target, "取消后续传完成")
    assert not metadata_file_path(target).exists()
    assert list(tmp_path.glob("*.part*")) == []


def test_no_range_server_discards_resume_and_downloads_single_thread(tmp_path):
    """服务器不支持 Range：清空临时文件 + 元数据，退回单线程从头下载。"""
    payload = build_payload(1024 * 1024)
    server = RangeTestServer(payload=payload, support_range=False)
    target = tmp_path / "plain.bin"
    try:
        # 造一个“上次下了一半”的现场（服务器却不支持 Range）
        make_state(target, len(payload), 4, [len(payload) // 4, 0, 0, 0])
        assert has_resumable_state(target), "前置条件：断点现场应该存在"

        messages = []
        session = make_session()
        try:
            download(
                server.url, target, threads=16, cancel_event=threading.Event(),
                progress_cb=messages.append, session=session,
            )
        finally:
            session.close()

        # 文件必须完整、不能是“旧分块 + 新下载”拼出来的垃圾
        assert target.stat().st_size == len(payload)
        assert sha256_of(target) == sha256_of_bytes(payload)
        # 降级提示必须发给界面
        texts = [str(m.get("status", "")) for m in messages]
        assert any("不支持断点续传" in text for text in texts), texts
        # 临时文件和元数据都必须清掉
        assert list(tmp_path.glob("*.part*")) == []
        assert not metadata_file_path(target).exists()
        # 正式下载没有发过带 Range 的请求：探测 1 次 + 单线程正式下载 0 次
        assert server.stats["range_requests"] == 1, server.stats["range_headers"]
    finally:
        server.stop()


def test_resume_sends_resuming_message_to_ui(resume_server, tmp_path):
    """续传时必须发 ``type=resuming`` 消息，main.py 靠它把状态标签改成“正在恢复断点续传…”。"""
    target = tmp_path / "payload.bin"
    size = resume_server.size
    chunks = plan_chunks(size, 4)
    # 第一块下满、第二块下一半 → 保证确实有块是“接着续”的
    full0 = chunks[0][2] - chunks[0][1] + 1
    make_state(target, size, 4, [full0, 128 * 1024, 0, 0], url=resume_server.url,
               server=resume_server, chunks=chunks, real=True)

    messages = []
    session = make_session()
    try:
        download(
            resume_server.url, target, threads=4, cancel_event=threading.Event(),
            progress_cb=messages.append, session=session,
        )
    finally:
        session.close()

    resuming = [m for m in messages if m.get("type") == "resuming"]
    assert resuming, f"没有发出 resuming 消息：{[m.get('type') for m in messages]}"
    assert resuming[0]["status"] == downloader.MSG_RESUME_STATUS
    assert resuming[0]["status"] == "正在恢复断点续传…"
    assert resuming[0]["resumed_bytes"] > 0, "resuming 消息里要带上“续传跳过了多少字节”"
    # 状态类消息里也要有一次中文提示（界面启动阶段就能看到）
    texts = [str(m.get("status", "")) for m in messages if m.get("type") == "status"]
    assert any("正在恢复断点续传" in text for text in texts), texts


def test_resume_reports_progress_including_saved_bytes(resume_server, tmp_path):
    """续传的进度必须把“已下载的旧字节”算进去，不能从 0 开始。"""
    target = tmp_path / "payload.bin"
    threads = 4
    size = resume_server.size
    chunks = plan_chunks(size, threads)
    # 前两块已下满 → 已经有 50% 的进度
    full = [chunks[index][2] - chunks[index][1] + 1 for index in (0, 1)]
    make_state(target, size, threads, [full[0], full[1], 0, 0],
               url=resume_server.url, server=resume_server, chunks=chunks, real=True)

    messages = []
    session = make_session()
    try:
        download(
            resume_server.url, target, threads=threads,
            cancel_event=threading.Event(), progress_cb=messages.append, session=session,
        )
    finally:
        session.close()

    percents = [m["percent"] for m in messages if m.get("type") == "progress"]
    assert percents, "必须上报进度"
    # 起始进度至少是已保存的 50%
    assert max(percents) >= 99.9
    assert min(percents) >= 50.0, f"续传进度不能从 0 开始：{min(percents)}"


def test_resume_disabled_downloads_from_scratch(resume_server, tmp_path):
    """``resume=False`` 时必须忽略旧断点，从头下载（并清掉旧现场）。"""
    target = tmp_path / "payload.bin"
    chunks = plan_chunks(resume_server.size, 4)
    full0 = chunks[0][2] - chunks[0][1] + 1
    make_state(target, resume_server.size, 4, [full0, 0, 0, 0],
               url=resume_server.url, server=resume_server, chunks=chunks, real=True)

    session = make_session()
    try:
        download(
            resume_server.url, target, threads=4, resume=False,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    resume_server.assert_content_matches(target, "关闭续传后的下载")
    starts = resume_server.range_starts()
    assert 0 in starts, f"关闭续传后必须从 0 重新下载：{starts}"


def test_stale_parts_without_metadata_are_cleaned(resume_server, tmp_path):
    """没有元数据的 .partN 无法校验，必须清掉重下（不能盲目接着写）。"""
    target = tmp_path / "payload.bin"
    part_file_path(target, 0).write_bytes(b"garbage" * 1000)
    part_file_path(target, 1).write_bytes(b"garbage")

    session = make_session()
    try:
        download(
            resume_server.url, target, threads=2,
            cancel_event=threading.Event(), session=session,
        )
    finally:
        session.close()

    resume_server.assert_content_matches(target, "残留清理后的下载")
    assert list(tmp_path.glob("*.part*")) == []


def test_cleanup_part_files_does_not_touch_metadata(tmp_path):
    """``cleanup_part_files`` 只管 .partN，元数据由 remove_metadata 单独负责。"""
    target = tmp_path / "a.bin"
    chunks = plan_chunks(1000, 2)
    write_metadata(target, build_metadata(URL, target, 1000, chunks, [0, 0], 2))
    part_file_path(target, 0).write_bytes(b"x" * 10)

    assert cleanup_part_files(target) == 1
    assert metadata_file_path(target).exists(), "清分块时不该顺手删掉元数据"
