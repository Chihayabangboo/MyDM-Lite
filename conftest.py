"""pytest 公共配置。

把项目根目录加进 sys.path，这样测试里可以直接 ``import utils`` / ``import downloader``，
不依赖 pytest 的 rootdir 推断结果。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
