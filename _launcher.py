"""run.bat 的弹窗助手（保证 run.bat 本身是纯 ASCII，中文不乱码）。

用法：
    python _launcher.py no-python   # 没装 Python
    python _launcher.py no-deps     # 依赖安装失败
    python _launcher.py too-old     # Python 版本太低
    python _launcher.py start       # 正常启动主程序

这个脚本只用标准库，不需要任何第三方依赖。
"""

from __future__ import annotations

import os
import sys

TITLE = "MyDM-Lite 下载器"

# 统一在这里写中文，方便以后改文案
MESSAGES = {
    "no-python": "请先安装 Python 3.9 或以上版本，访问 python.org 下载",
    "too-old": "请先安装 Python 3.9 或以上版本，访问 python.org 下载",
    "no-deps": "依赖安装失败，请检查网络",
}


def _show_message(text: str) -> None:
    """尽量用弹窗提示；没有图形界面时退回控制台输出。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        messagebox.showwarning(TITLE, text, parent=root)
        root.destroy()
    except Exception:
        try:
            print(f"{TITLE}：{text}")
        except Exception:
            pass


def _start_main() -> int:
    """启动主程序（用完整路径，保证双击 run.bat 时工作目录正确）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    os.chdir(here)
    import main as mydm_main

    return mydm_main.main()


def main(argv) -> int:
    action = (argv[1] if len(argv) > 1 else "start").strip().lower()

    if action in MESSAGES:
        _show_message(MESSAGES[action])
        return 1

    if action in ("start", "selftest"):
        if action == "selftest":
            # 隐藏模式：启动主程序后 3 秒自动关窗，用于自动化冒烟测试
            sys.argv = [sys.argv[0], "--selftest"]
        try:
            return _start_main()
        except ImportError:
            # 主程序里 import requests 失败，说明依赖确实没装上
            _show_message(MESSAGES["no-deps"])
            return 1
        except Exception as exc:  # pragma: no cover - 兜底，避免闪退
            _show_message(f"程序启动失败，请重新双击 run.bat 试试。\n（错误信息：{exc}）")
            return 1

    _show_message(f"未知参数：{action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
