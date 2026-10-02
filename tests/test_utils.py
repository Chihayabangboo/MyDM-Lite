"""utils.py 纯函数测试。

覆盖：文件名解析（Content-Disposition / RFC 5987 中文 / URL / 默认名）、
重名递增、大小格式化、默认下载目录（用临时环境变量 + mock，不依赖真实系统目录）、
日志初始化参数等。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

import pytest

import utils
from utils import (
    DEFAULT_FILENAME,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    cleanup_part_files,
    extract_first_url,
    filename_from_url,
    format_eta,
    format_size,
    get_default_download_dir,
    open_folder,
    parse_content_disposition_filename,
    part_file_path,
    resolve_download_filename,
    reveal_in_folder,
    sanitize_filename,
    setup_logging,
    unique_path,
)


# ---------------------------------------------------------------------------
# Content-Disposition 解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header, expected",
    [
        ('attachment; filename="report.pdf"', "report.pdf"),
        ("attachment; filename=report.pdf", "report.pdf"),
        ("inline; filename='quoted name.zip'", "quoted name.zip"),
        ('attachment; filename="archive.tar.gz"', "archive.tar.gz"),
        ("attachment", None),
        ("", None),
        (None, None),
        (
            "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.zip",
            "中文.zip",
        ),
        (
            "attachment; filename=\"fallback.zip\"; filename*=UTF-8''%E6%B5%8B%E8%AF%95.zip",
            "测试.zip",
        ),
        # 文件名里带分号、空格
        ('attachment; filename="my file (1).mp4"', "my file (1).mp4"),
    ],
)
def test_parse_content_disposition_filename(header, expected):
    assert parse_content_disposition_filename(header) == expected


def test_rfc5987_chinese_filename_decoded():
    """RFC 5987 编码的中文文件名必须能正确解码。"""
    header = "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87%E6%96%87%E4%BB%B6.zip"
    assert parse_content_disposition_filename(header) == "中文文件.zip"


def test_rfc5987_with_charset_and_language():
    header = "attachment; filename*=utf-8'zh-CN'%E6%B5%8B%E8%AF%95.bin"
    assert parse_content_disposition_filename(header) == "测试.bin"


# ---------------------------------------------------------------------------
# URL 解析文件名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/files/movie.mp4", "movie.mp4"),
        ("https://example.com/files/movie.mp4?a=1&b=2", "movie.mp4"),
        ("https://example.com/a/b/c/document.pdf#page=2", "document.pdf"),
        ("https://example.com/%E4%B8%AD%E6%96%87.zip", "中文.zip"),
        ("https://example.com/", None),
        ("https://example.com", None),
        (None, None),
        ("", None),
    ],
)
def test_filename_from_url(url, expected):
    assert filename_from_url(url) == expected


# ---------------------------------------------------------------------------
# 优先级：Content-Disposition → URL → 默认名
# ---------------------------------------------------------------------------


def test_resolve_prefers_content_disposition():
    headers = {"Content-Disposition": 'attachment; filename="from-header.zip"'}
    assert resolve_download_filename("https://example.com/from-url.zip", headers) == "from-header.zip"


def test_resolve_supports_case_insensitive_headers():
    headers = {"content-disposition": "attachment; filename=lower.zip"}
    assert resolve_download_filename("https://example.com/x.zip", headers) == "lower.zip"


def test_resolve_falls_back_to_url():
    assert resolve_download_filename("https://example.com/path/from-url.zip", {}) == "from-url.zip"


def test_resolve_falls_back_to_default_name():
    assert resolve_download_filename("https://example.com", {}) == DEFAULT_FILENAME
    assert resolve_download_filename(None, None) == DEFAULT_FILENAME


def test_resolve_rejects_path_traversal_in_header():
    headers = {"Content-Disposition": 'attachment; filename="../../evil.exe"'}
    assert resolve_download_filename("https://example.com/a.bin", headers) == "evil.exe"


# ---------------------------------------------------------------------------
# 文件名清洗 & 重名递增
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("normal.zip", "normal.zip"),
        ("bad:name?.zip", "bad_name_.zip"),
        ("path/with/slash.zip", "slash.zip"),
        ("C:\\Users\\me\\file.zip", "file.zip"),
        ("  spaced.zip  ", "spaced.zip"),
        ("CON.txt", "_CON.txt"),
        ("", ""),
        (None, ""),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_unique_path_increments(tmp_path):
    first = tmp_path / "file.zip"
    assert unique_path(first) == first

    first.write_bytes(b"x")
    second = unique_path(first)
    assert second.name == "file (1).zip"

    second.write_bytes(b"x")
    third = unique_path(first)
    assert third.name == "file (2).zip"

    third.write_bytes(b"x")
    assert unique_path(first).name == "file (3).zip"


def test_unique_path_keeps_suffix_with_multiple_dots(tmp_path):
    target = tmp_path / "archive.tar.gz"
    target.write_bytes(b"x")
    assert unique_path(target).name == "archive.tar (1).gz"


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0 B"),
        (1, "1 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (int(1.5 * 1024 * 1024), "1.5 MB"),
        (1024 ** 3, "1.0 GB"),
        (int(2.5 * 1024 ** 3), "2.5 GB"),
        (None, "--"),
        (-5, "--"),
        ("abc", "--"),
    ],
)
def test_format_size(value, expected):
    assert format_size(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "--"),
        (0, "0秒"),
        (12, "12秒"),
        (59, "59秒"),
        (60, "01分00秒"),
        (610, "10分10秒"),
        (3600, "1小时00分00秒"),
        (3725, "1小时02分05秒"),
        ("abc", "--"),
        (-1, "--"),
    ],
)
def test_format_eta(value, expected):
    assert format_eta(value) == expected


# ---------------------------------------------------------------------------
# 默认下载目录（不依赖真实系统目录）
# ---------------------------------------------------------------------------


def test_default_download_dir_uses_userprofile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    result = get_default_download_dir()

    assert result == home / "Downloads"
    assert result.is_dir()  # 目录不存在时会自动创建


def test_default_download_dir_creates_missing_directory(tmp_path, monkeypatch):
    home = tmp_path / "brand-new-home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)

    result = get_default_download_dir()

    assert result.name == "Downloads"
    assert result.exists()


def test_default_download_dir_falls_back_to_home_when_mkdir_fails(tmp_path, monkeypatch):
    """连下载目录都创建不出来时，必须回退到用户主目录而不是抛异常。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: (_ for _ in ()).throw(OSError("denied")))

    assert get_default_download_dir() == home


def test_default_download_dir_respects_xdg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg-downloads"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(xdg))

    assert get_default_download_dir() == xdg


def test_default_download_dir_real_call_does_not_raise():
    """真实环境调用一次也不能抛异常（不校验具体路径）。"""
    result = get_default_download_dir()
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# 打开文件夹（全部 mock 掉，不真的弹窗口）
# ---------------------------------------------------------------------------


def test_reveal_in_folder_windows_uses_explorer_select(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(utils, "_spawn_detached", lambda args: calls.append(args) or True)

    target = tmp_path / "file.zip"
    target.write_bytes(b"x")

    assert reveal_in_folder(target) is True
    assert calls == [["explorer", f"/select,{target}"]]


def test_reveal_in_folder_macos_uses_open_R(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(utils, "_spawn_detached", lambda args: calls.append(args) or True)

    target = tmp_path / "file.zip"
    target.write_bytes(b"x")

    assert reveal_in_folder(target) is True
    assert calls == [["open", "-R", str(target)]]


def test_reveal_in_folder_linux_uses_xdg_open_directory(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(utils, "_spawn_detached", lambda args: calls.append(args) or True)

    target = tmp_path / "file.zip"
    target.write_bytes(b"x")

    assert reveal_in_folder(target) is True
    assert calls == [["xdg-open", str(tmp_path)]]


def test_reveal_in_folder_falls_back_to_opening_directory(monkeypatch, tmp_path):
    """打开并选中失败时只打开目录，不报错。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(utils, "_spawn_detached", lambda args: False)
    opened = []
    monkeypatch.setattr(utils, "open_folder", lambda path: opened.append(path) or True)

    assert reveal_in_folder(tmp_path / "file.zip") is True
    assert opened == [tmp_path]


def test_open_folder_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "_spawn_detached", lambda args: False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert open_folder(tmp_path) is False


# ---------------------------------------------------------------------------
# 剪贴板文本里挑链接
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com/a.zip", "https://example.com/a.zip"),
        ("看看这个 http://example.com/b.zip 很好", "http://example.com/b.zip"),
        ("https://example.com/a.zip。", "https://example.com/a.zip"),
        ("(https://example.com/a.zip)", "https://example.com/a.zip"),
        ("没有链接的一段话", None),
        ("", None),
        (None, None),
        ("ftp://example.com/a.zip", None),
    ],
)
def test_extract_first_url(text, expected):
    assert extract_first_url(text) == expected


# ---------------------------------------------------------------------------
# 分块临时文件
# ---------------------------------------------------------------------------


def test_part_file_path():
    assert part_file_path(Path("/tmp/movie.mp4"), 3).name == "movie.mp4.part3"


def test_cleanup_part_files_removes_only_matching(tmp_path):
    target = tmp_path / "movie.mp4"
    target.write_bytes(b"final")

    for index in range(4):
        part_file_path(target, index).write_bytes(b"partial")

    other = tmp_path / "movie.mp4.other"
    other.write_bytes(b"keep me")
    lookalike = tmp_path / "movie.mp4.partx"
    lookalike.write_bytes(b"keep me too")

    removed = cleanup_part_files(target)

    assert removed == 4
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "movie.mp4", "movie.mp4.other", "movie.mp4.partx",
    ]


def test_cleanup_part_files_missing_directory_is_safe(tmp_path):
    assert cleanup_part_files(tmp_path / "nope" / "file.bin") == 0


def test_cleanup_part_files_can_keep_indexes(tmp_path):
    target = tmp_path / "movie.mp4"
    for index in range(3):
        part_file_path(target, index).write_bytes(b"x")

    removed = cleanup_part_files(target, keep_indexes=[1])

    assert removed == 2
    assert part_file_path(target, 1).exists()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def test_setup_logging_writes_file_and_rotates(tmp_path):
    utils.reset_logging_for_tests()
    logger = setup_logging(log_dir=tmp_path, force=True)
    logger.info("测试日志：启动")

    for handler in logger.handlers:
        handler.flush()

    log_file = tmp_path / utils.LOG_FILE_NAME
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "测试日志：启动" in content
    assert "[INFO]" in content

    file_handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert file_handlers, "必须挂上 RotatingFileHandler"
    handler = file_handlers[0]
    assert handler.maxBytes == LOG_MAX_BYTES == 5 * 1024 * 1024
    assert handler.backupCount == LOG_BACKUP_COUNT == 3

    utils.reset_logging_for_tests()


def test_setup_logging_is_idempotent(tmp_path):
    utils.reset_logging_for_tests()
    first = setup_logging(log_dir=tmp_path, force=True)
    count = len(first.handlers)
    second = setup_logging(log_dir=tmp_path)
    assert second is first
    assert len(second.handlers) == count
    utils.reset_logging_for_tests()


def test_setup_logging_survives_bad_directory(tmp_path, monkeypatch):
    """日志目录写不了也不能让程序崩掉。"""
    utils.reset_logging_for_tests()
    monkeypatch.setattr(utils, "get_log_directory", lambda: Path(os.devnull) / "nope")
    logger = setup_logging(force=True)
    logger.info("这条日志会被丢掉，但不会抛异常")
    utils.reset_logging_for_tests()


def test_log_rotation_actually_rotates(tmp_path):
    """真的写超过阈值，验证 mydm.log 会轮转出 .1/.2…… 且最多留 3 个备份。"""
    utils.reset_logging_for_tests()
    logger = setup_logging(log_dir=tmp_path, force=True)
    handler = next(
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    # 把阈值改小，避免测试真的写 5MB
    handler.maxBytes = 8192
    handler.backupCount = 3

    for index in range(60):
        logger.info("轮转测试第 %d 条，填充一些内容让它超过阈值 %s", index, "x" * 200)
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass

    main_log = tmp_path / utils.LOG_FILE_NAME
    rotated = sorted(p.name for p in tmp_path.iterdir())
    assert main_log.exists()
    assert any(name.endswith(".1") for name in rotated), f"没有产生轮转文件：{rotated}"
    backups = [name for name in rotated if re.search(r"\.\d+$", name)]
    assert len(backups) <= LOG_BACKUP_COUNT, f"备份文件超过 3 个：{backups}"
    # 最新一条日志必须在主文件里（轮转后老内容进备份）
    assert "轮转测试第 59 条" in main_log.read_text(encoding="utf-8", errors="ignore")
    utils.reset_logging_for_tests()
