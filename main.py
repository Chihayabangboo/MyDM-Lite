"""MyDM-Lite 主界面。

设计原则（给小白用）：
* 能自动的全都自动：剪贴板自动填链接、默认存到系统“下载”文件夹、默认 8 线程
* 一级界面上只有“开始下载”和“取消”两个按钮，高级选项默认折叠
* 所有错误提示都是通俗中文，绝不弹英文异常或堆栈

线程安全：子线程**只**往 ``queue.Queue`` 里写消息，主线程用
``root.after(100, ...)`` 轮询队列刷新界面，子线程绝不直接操作 Tkinter 控件。
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from downloader import (
    DEFAULT_THREADS,
    VALID_THREAD_OPTIONS,
    DownloadCancelled,
    DownloadError,
    download_to_directory,
)
from utils import (
    extract_first_url,
    format_eta,
    format_size,
    get_default_download_dir,
    reveal_in_folder,
    setup_logging,
)

WINDOW_TITLE = "MyDM-Lite 下载器"
WINDOW_SIZE = "720x360"
MIN_WINDOW_SIZE = (660, 340)

# 界面用的一套字体（Windows 上是微软雅黑，其它平台自动回退）
FONT_FAMILY = "Microsoft YaHei UI" if sys.platform.startswith("win") else "Helvetica"
FONT_NORMAL = (FONT_FAMILY, 11)
FONT_HINT = (FONT_FAMILY, 11)
FONT_BUTTON = (FONT_FAMILY, 14, "bold")
FONT_CANCEL = (FONT_FAMILY, 11)
FONT_TITLE = (FONT_FAMILY, 13, "bold")


class DownloaderApp:
    """MyDM-Lite 的主窗口。"""

    def __init__(self, root: tk.Tk, logger=None):
        self.root = root
        self.log = logger or setup_logging()
        self.message_queue: "queue.Queue[dict]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.downloading = False
        self.merging = False
        self.default_dir = get_default_download_dir()
        self.save_dir = self.default_dir
        self._poll_job = None

        self.url_var = tk.StringVar(value="")
        self.path_var = tk.StringVar(value=str(self.save_dir))
        self.status_var = tk.StringVar(value="请粘贴下载链接")
        self.speed_var = tk.StringVar(value="速度：--")
        self.size_var = tk.StringVar(value="大小：--")
        self.eta_var = tk.StringVar(value="剩余时间：--")
        self.threads_var = tk.StringVar(value=str(DEFAULT_THREADS))
        self.advanced_open = tk.BooleanVar(value=False)

        self._build_ui()
        self._bind_events()
        self.root.after(150, self._check_clipboard)
        self._schedule_poll()
        self.log.info("程序启动，默认保存目录：%s", self.save_dir)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*MIN_WINDOW_SIZE)

        # 让窗口内容随窗口大小自动拉伸
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = ttk.Frame(self.root, padding=(16, 12, 16, 12))
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        body = ttk.Frame(outer)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)

        # ---- 第 1 行：下载链接
        ttk.Label(body, text="下载链接：", font=FONT_NORMAL).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.url_entry = ttk.Entry(body, textvariable=self.url_var, font=FONT_NORMAL)
        self.url_entry.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=4, pady=(0, 10))

        # ---- 第 2 行：保存位置 + 更改按钮
        ttk.Label(body, text="保存到：", font=FONT_NORMAL).grid(
            row=2, column=0, sticky="w", pady=(0, 4)
        )
        path_row = ttk.Frame(body)
        path_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        path_row.columnconfigure(0, weight=1)
        self.path_label = ttk.Label(
            path_row, textvariable=self.path_var, font=FONT_HINT,
            relief="groove", padding=(8, 4), anchor="w",
        )
        self.path_label.grid(row=0, column=0, sticky="ew")
        self.change_btn = ttk.Button(path_row, text="更改", width=8, command=self.on_change_dir)
        self.change_btn.grid(row=0, column=1, padx=(8, 0))

        # ---- 第 3 行：开始下载 / 取消
        button_row = ttk.Frame(body)
        button_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        button_row.columnconfigure(0, weight=3)
        button_row.columnconfigure(1, weight=1)

        self.start_btn = ttk.Button(
            button_row, text="开始下载", style="Big.TButton", command=self.on_start
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", ipady=8, padx=(0, 8))
        self.cancel_btn = ttk.Button(
            button_row, text="取消", style="Cancel.TButton", command=self.on_cancel
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew", ipady=8)
        self.cancel_btn.state(["disabled"])

        # ---- 大号按钮样式
        style = ttk.Style()
        try:
            style.theme_use("vista")  # Windows 上更好看，其它平台会失败
        except Exception:
            pass
        style.configure("Big.TButton", font=FONT_BUTTON, padding=(10, 6))
        style.configure("Cancel.TButton", font=FONT_CANCEL, padding=(6, 6))

        # ---- 第 4 行：进度条
        self.progress = ttk.Progressbar(body, orient="horizontal", mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        # ---- 第 5 行：状态 / 速度 / 大小 / 剩余时间
        info = ttk.Frame(body)
        info.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        for column in range(4):
            info.columnconfigure(column, weight=1)
        self.status_label = ttk.Label(info, textvariable=self.status_var, font=FONT_TITLE)
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.speed_var, font=FONT_HINT).grid(row=0, column=1, sticky="w")
        ttk.Label(info, textvariable=self.size_var, font=FONT_HINT).grid(row=0, column=2, sticky="w")
        ttk.Label(info, textvariable=self.eta_var, font=FONT_HINT).grid(row=0, column=3, sticky="w")

        # ---- 第 6 行：高级选项（默认折叠）
        self.advanced_check = ttk.Checkbutton(
            body, text="高级选项", variable=self.advanced_open, command=self.on_toggle_advanced
        )
        self.advanced_check.grid(row=7, column=0, sticky="w", pady=(6, 0))

        self.advanced_frame = ttk.Frame(body)
        self.advanced_frame.columnconfigure(2, weight=1)
        ttk.Label(self.advanced_frame, text="下载线程数：", font=FONT_HINT).grid(
            row=0, column=0, sticky="w"
        )
        self.threads_box = ttk.Combobox(
            self.advanced_frame, textvariable=self.threads_var, state="readonly",
            values=[str(value) for value in VALID_THREAD_OPTIONS], width=5, font=FONT_HINT,
        )
        self.threads_box.grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(
            self.advanced_frame,
            text="（不懂就别改，默认 8 线程；大文件用得多，小文件会自动减少）",
            font=FONT_HINT,
        ).grid(row=0, column=2, sticky="w")

    def _bind_events(self) -> None:
        self.url_entry.bind("<Return>", lambda _event: self.on_start())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda _event: self.on_cancel())

    # ------------------------------------------------------------------
    # 剪贴板
    # ------------------------------------------------------------------

    def _read_clipboard_text(self) -> str:
        """安全读取剪贴板：为空 / 被占用 / 不是文本时都返回空串，绝不抛异常。"""
        try:
            text = self.root.clipboard_get()
        except Exception:
            return ""
        if not isinstance(text, str):
            return ""
        return text.strip()

    def _check_clipboard(self) -> None:
        """启动时检查剪贴板，有 http/https 链接就自动填进输入框。"""
        text = self._read_clipboard_text()
        url = extract_first_url(text)
        if url:
            self.url_var.set(url)
            self.status_var.set("已检测到链接，点击下载即可")
            self.log.info("剪贴板检测到链接：%s", url)
        else:
            self.status_var.set("请粘贴下载链接")
            self.log.info("剪贴板没有可用链接")

    # ------------------------------------------------------------------
    # 界面状态
    # ------------------------------------------------------------------

    def _selected_threads(self) -> int:
        try:
            value = int(self.threads_var.get())
        except (TypeError, ValueError):
            return DEFAULT_THREADS
        if value not in VALID_THREAD_OPTIONS:
            return DEFAULT_THREADS
        return value

    def set_running_state(self, running: bool) -> None:
        """下载中禁用输入/开始按钮，只留“取消”可用。"""
        self.downloading = running
        if running:
            self.start_btn.state(["disabled"])
            self.url_entry.state(["disabled"])
            self.change_btn.state(["disabled"])
            self.threads_box.state(["disabled"])
            self.cancel_btn.state(["!disabled"])
        else:
            self.start_btn.state(["!disabled"])
            self.url_entry.state(["!disabled"])
            self.change_btn.state(["!disabled"])
            if self.advanced_open.get():
                self.threads_box.state(["readonly"])
            self.cancel_btn.state(["disabled"])

    def _set_merging(self, merging: bool, status: str = "正在合并文件…") -> None:
        """合并阶段：开始和取消按钮都要禁用，避免删掉正在合并的分块。"""
        self.merging = merging
        if merging:
            self.start_btn.state(["disabled"])
            self.cancel_btn.state(["disabled"])
            self.url_entry.state(["disabled"])
            self.change_btn.state(["disabled"])
            self.status_var.set(status)
            self.progress.configure(value=100)
            self.speed_var.set("速度：--")
            self.eta_var.set("剩余时间：--")
        else:
            self.cancel_btn.state(["disabled"])

    # ------------------------------------------------------------------
    # 交互动作
    # ------------------------------------------------------------------

    def on_toggle_advanced(self) -> None:
        if self.advanced_open.get():
            self.advanced_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            if not self.downloading:
                self.threads_box.state(["readonly"])
        else:
            self.advanced_frame.grid_remove()
            self.threads_box.state(["disabled"])

    def on_change_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择保存文件夹", initialdir=str(self.save_dir)
        )
        if chosen:
            self.save_dir = Path(chosen)
            self.path_var.set(str(self.save_dir))
            self.log.info("用户更改保存目录：%s", self.save_dir)

    def on_start(self) -> None:
        if self.downloading:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("还没填链接", "请先粘贴下载链接，再点“开始下载”。")
            self.url_entry.focus_set()
            return
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("链接无效", "链接无效，请检查后重新粘贴（需要以 http:// 或 https:// 开头）。")
            return

        # 保存目录万一被删了就重新建一个
        try:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.save_dir = get_default_download_dir()
            self.path_var.set(str(self.save_dir))

        threads = self._selected_threads()
        self.cancel_event = threading.Event()
        self.set_running_state(True)
        self.progress.configure(value=0)
        self.status_var.set("正在准备下载…")
        self.speed_var.set("速度：--")
        self.size_var.set("大小：--")
        self.eta_var.set("剩余时间：--")
        self.log.info("点击开始下载：%s（线程数 %s，保存到 %s）", url, threads, self.save_dir)

        self.worker = threading.Thread(
            target=self._download_worker,
            args=(url, self.save_dir, threads),
            name="mydm-download",
            daemon=True,
        )
        self.worker.start()

    def _download_worker(self, url: str, directory: Path, threads: int) -> None:
        """下载线程：只往队列里发消息，绝不碰界面控件。"""
        try:
            path = download_to_directory(
                url=url,
                directory=directory,
                threads=threads,
                cancel_event=self.cancel_event,
                progress_cb=self.message_queue.put,
                logger=self.log,
            )
            self.message_queue.put({"type": "success", "path": str(path), "status": "已完成"})
        except DownloadCancelled:
            self.message_queue.put({"type": "cancelled", "status": "已取消"})
        except DownloadError as exc:
            self.message_queue.put({"type": "error", "message": exc.message, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 - 兜底，保证不会把英文堆栈弹给用户
            self.log.exception("下载线程出现未预期错误：%s", exc)
            self.message_queue.put({
                "type": "error",
                "message": "下载失败，请稍后重试",
                "detail": f"{type(exc).__name__}: {exc}",
            })

    def on_cancel(self) -> None:
        if self.merging:
            # 合并阶段不允许取消，避免删掉正在合并的分块文件
            return
        if not self.downloading:
            return
        self.cancel_event.set()
        self.status_var.set("正在取消…")
        self.cancel_btn.state(["disabled"])
        self.log.info("用户点击取消")

    def on_close(self) -> None:
        if self.downloading and not self.merging:
            if not messagebox.askyesno("正在下载", "下载还没完成，确定要退出吗？"):
                return
            self.cancel_event.set()
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        self.log.info("程序退出")
        self.root.destroy()

    # ------------------------------------------------------------------
    # 主线程轮询队列 → 刷新界面
    # ------------------------------------------------------------------

    def _schedule_poll(self) -> None:
        self._poll_job = self.root.after(100, self._poll_queue)

    def _poll_queue(self) -> None:
        try:
            while True:
                message = self.message_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        self._schedule_poll()

    def _handle_message(self, message: dict) -> None:
        kind = message.get("type")

        if kind == "status":
            self.status_var.set(message.get("status", ""))
            return

        if kind == "resuming":
            # 断点续传：只把状态标签的文本换成“正在恢复断点续传…”，
            # 不动任何控件的创建 / 布局 / 尺寸。
            self.status_var.set("正在恢复断点续传…")
            return

        if kind == "progress":
            total = message.get("total") or 0
            downloaded = message.get("downloaded") or 0
            speed = message.get("speed") or 0.0
            if total:
                self.progress.configure(value=min(100.0, downloaded * 100.0 / total))
                self.size_var.set(f"大小：{format_size(downloaded)} / {format_size(total)}")
            else:
                self.size_var.set(f"大小：{format_size(downloaded)}")
            self.speed_var.set(f"速度：{format_size(speed)}/秒" if speed > 0 else "速度：--")
            eta = message.get("eta")
            self.eta_var.set(f"剩余时间：{format_eta(eta)}" if eta else "剩余时间：--")
            if message.get("status"):
                self.status_var.set(message["status"])
            return

        if kind == "merging":
            self._set_merging(True, message.get("status", "正在合并文件…"))
            return

        if kind == "done":
            self.progress.configure(value=100)
            self.size_var.set(f"大小：{format_size(message.get('downloaded') or 0)} / "
                              f"{format_size(message.get('total') or 0)}")
            # 真正的收尾在 success 消息里做
            return

        if kind == "success":
            self.set_running_state(False)
            self.merging = False
            self.progress.configure(value=100)
            self.status_var.set("已完成")
            self.speed_var.set("速度：--")
            self.eta_var.set("剩余时间：0秒")
            path = message.get("path", "")
            self.log.info("下载完成：%s", path)
            if messagebox.askyesno("下载完成", "下载完成！是否打开所在文件夹？"):
                reveal_in_folder(path)
            return

        if kind == "cancelled":
            self.set_running_state(False)
            self.merging = False
            self.status_var.set("已取消")
            self.speed_var.set("速度：--")
            self.eta_var.set("剩余时间：--")
            self.log.info("下载已取消（临时文件已清理）")
            return

        if kind == "error":
            self.set_running_state(False)
            self.merging = False
            self.status_var.set("下载失败")
            self.speed_var.set("速度：--")
            self.eta_var.set("剩余时间：--")
            text = message.get("message") or "下载失败，请稍后重试"
            detail = message.get("detail") or ""
            if detail:
                self.log.error("界面提示用户：%s（技术细节：%s）", text, detail)
            messagebox.showerror("下载失败", text)
            return


def main() -> int:
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("MyDM-Lite 启动")
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - 没有图形界面时
        logger.exception("无法创建窗口：%s", exc)
        print("无法创建窗口，请确认当前系统能显示图形界面。")
        return 1
    app = DownloaderApp(root, logger=logger)

    # 隐藏参数：启动后自动关闭，仅用于自动化冒烟测试（普通用户用不到）
    if "--selftest" in sys.argv:
        def _auto_close():
            app.on_close()
        root.after(3000, _auto_close)

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
