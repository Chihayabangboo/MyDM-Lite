[English]  [English]  [English]  [English]  [English]  [English]  [English]  [English]  [English]  [English] 
 # MyDM-Lite Downloader

A **user-friendly** Windows desktop downloader for those who are completely unfamiliar with technology: single task, multi-threaded chunking, and just one big button on the interface.

> Technology stack: Python 3.9+ / Tkinter + ttk / requests / threading (not using asyncio)
> The interface is in Chinese only. Errors will also pop up in Chinese, with no English exceptions or stack traces.

---

## One, Three-Step Usage for Beginners(New users please note: I have already packaged the ready-made .exe file for you, no need to package it yourself! You can download it directly from the Releases page on the right.)

1. **Copy the download link** (right-click on the download address in the browser and select "Copy link").
2. **Double-click `exe.`**.
3. The link is already filled in for you. **Simply click the big "Start Download" button**, and wait for the progress bar to complete.

After downloading, a pop-up will ask, "Download complete! Do you want to open the folder?" Click "Yes" to directly view the file.

Files are saved by default in the **"Downloads"** folder on your computer, so you don't need to select a folder.

---

## Two, How to Run

### Method 1: Double-click .exe (download from Releases) (recommended for general users)

```
.exe
```

All dependencies are packaged, so you can simply double-click to run.

### Method 2: Double-click

```
run.bat
```

`run.bat` will automatically handle these tasks:

| Situation | Program Behavior |
| --- | --- |
| Python not installed | A pop-up will prompt, "Please install Python 3.9 or above, visit python.org to download," and it won't disappear quickly. |
| Python version below 3.9 | It will prompt to upgrade similarly. |
| Requests not installed | It will automatically `pip install -r requirements.txt`, **only connecting to the internet when this dependency is missing**. |
| Dependency installation fails | A pop-up will prompt, "Dependency installation failed, please check your network," and it won't disappear quickly. |
| Everything is normal | The downloader window will open directly. |

### Method 3: Command Line

```bat
python main.py
```

Or use the py launcher:

```bat
py -3 main.py
```

### Method 4: Run Tests (for developers)

```bat
python -m pip install -r dev-requirements.txt
python -m pytest -v
```

If you don't want to install pytest, you can run the self-check script that comes with it (it will automatically start a local test server and complete the download process):

```bat
python selfcheck.py
```

If all tests pass, the last line will print "Self-check ended: all N tests passed."

---

## Three, Interface Explanation (from top to bottom)

| Position | Description |
| --- | --- |
| Download link | It will automatically read the clipboard at startup, fill in the link if available, and prompt "Link detected, click to download." |
| Save to | The default is the system "Downloads" folder, with a "Change" button on the right. If you don't want to change it, just ignore it. |
| Start Download | A large button, click to start (default 8 threads), no settings required. |
| Cancel | Click to stop the download immediately and clean up temporary files. |
| Progress bar | Displays overall progress. |
| Status | For example, "Connecting to server..." "Downloading (8 threads)" "Automatically adjusted to 4 threads" "Merging files..." "Completed". |
| Speed | Global speed (total bytes added in the last second). |
| Size | Downloaded / Total size. |
| Remaining time | Estimated based on global speed. |
| Advanced options | **Collapsed by default**, when expanded, you can select the number of threads: 1/2/4/8/16/32/64, default 8. |

---

## Four, Working Principle (why it can download fast)

1. First, use `HEAD` to detect file size and whether it supports chunking (timeout 5 seconds).
2. If `HEAD` is rejected or times out, switch to `GET` + `Range: bytes=0-0` for another detection.
3. If it still can't chunk or the server doesn't provide the file size, download it using a **single thread**.
4. If it can chunk, divide the file into several byte intervals, **each thread writes its own temporary file**:
   `movie.mp4.part0`, `movie.mp4.part1`, `movie.mp4.part2`...
   (Absolutely no multiple threads writing to the same file simultaneously to avoid data corruption).
5. After all chunks are downloaded, merge them in order into `movie.mp4`, then delete all `.partN`.
6. If canceled or failed, it will also clean up all `.partN`.

### Thread number automatic adjustment rules (by priority)

1. Server does not support chunking, or file size is unknown → Force **1 thread**.
2. Each chunk is at least 1MB: `N = max(1, min(number of threads you selected, file size ÷ 1MB))`.
3. File size less than 64MB → `N = min(N, 16)` (too many threads for small files will slow it down).
4. Final thread number = N, and it will display "Automatically adjusted to N threads" in the status bar.

### Error retry rules (each thread is responsible for its segment)

| Situation | Handling Method |
| --- | --- |
| 429 Server rate limiting | Exponential backoff retry, up to 3 times, **without reducing the number of threads**, backoff period can be interrupted by clicking "Cancel". |
| 403 Forbidden access | Do not retry, directly prompt "Server refused access, please check the link or try again later." |
| Connection timeout / Network error | The thread retries 2 times, if still fails, prompt "Network connection failed, please check your network and try again." |
| 5xx Server error | The thread retries 2 times, if still fails, prompt "Server is temporarily unable to respond, please try again later." |
| 404 Link does not exist | Prompt "Invalid link, please check and paste again." |

When retrying, the chunk file will be cleared and re-downloaded, simple and reliable (does not support breakpoint resuming).

### Where does the file name come from

1. Prioritize the server's returned `Content-Disposition`, supports RFC 5987 Chinese encoding
   (`filename*=UTF-8''%E4%B8%AD%E6%96%87.zip` → `中文.zip`);
2. Otherwise, take from the URL path, also do URL decoding;
3. If none, it will be called `download.bin`;
4. Automatically add `(1)`, `(2)` suffixes for duplicates.

---

## Five, Logging

The program writes `mydm.log` in the system temporary directory (Windows is generally `%TEMP%`):

* Records key events such as startup, download start, thread number adjustment, exceptions, download completion, and cancellation;
* Automatically rotates single files over **5MB**, retains the latest **3** log files;
* If you want to see where the log is, you can set the environment variable `MYDM_LOG_DIR` to specify the directory.

The interface only displays simple Chinese, technical details (exception types, status codes) are written to the log for easy troubleshooting.

---

## Six, Project Structure

```
MyDM-Lite/
├── main.py                     # Interface + Main loop (root.after(100) polls the message queue)
├── downloader.py               # Download core: probe / chunk / threading / retry / merge / clean
├── utils.py                    # Filename parsing, size formatting, default download directory, open folder, log
├── _launcher.py                # run.bat launch assistant (Chinese pop-ups)
├── run.bat                     # Double-click to start (contains Python and dependency checks, pure ASCII)
├── selfcheck.py                # Self-check script (can end-to-end verify without pytest)
├── requirements.txt            # Runtime dependencies: only requests
├── dev-requirements.txt        # Development dependencies: pytest
├── README.md                   # This file
├── conftest.py                   # pytest shared config (adds project root to sys.path)
└── tests/
    ├── test_utils.py           # Pure function / path / mock tests
    └── test_range_server.py    # Local Range server (ThreadingHTTPServer) + integration tests
```

> Tip: `run.bat` must be **pure ASCII** inside (Chinese comments will cause cmd.exe parsing errors),
> so all Chinese prompts are displayed in `_launcher.py` using pop-ups.

---

## Seven, Packaging into exe (Optional)(New users please note: I have already packaged the ready-made .exe file for you, no need to package it yourself! You can download it directly from the Releases page on the right.)

If there are many beginners, you can package it into a single exe for distribution, and the recipient doesn't need to install Python.

```bat
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name MyDM-Lite main.py
```

* The generated program is located in `dist\MyDM-Lite.exe`;
* `--windowed` means no black console window will pop up;
* After packaging, `run.bat` will no longer be needed (exe comes with Python and requests);
* The first time you start the exe, it will be a few seconds slower than starting with Python (to extract the built-in runtime), which is normal.

---

## Eight, Known Limitations (intentionally kept simple)

* **Single task**: Only one file can be downloaded at a time, no queue.
* **Cannot limit speed, cannot set proxy**: Not implemented.
* **No system tray icon, no browser extension**.
* If the server does not support chunking (e.g., some direct links from cloud storage), it will download in a single thread, speed depends on the server.
* Links that require login cannot be obtained (no Cookie/auth implemented), please download using a browser.
* Some websites return 403 on HEAD requests, the program will automatically fall back to `GET Range` detection, if it still fails, download in a single thread.
* Does not support BT / magnet link / m3u8 video streams.
* Downloading large files (>4GB) depends on system and filesystem support, not specially optimized.

---

## Nine, Common Questions

**Q: Double-clicking run.bat prompts "Please install Python 3.9 or above"?**
A: Open <https://www.python.org/downloads/> to download and install, make sure to check "Add python.exe to PATH" in the first step, then restart and double-click `run.bat`.

**Q: Prompt "Dependency installation failed, please check network"?**
A: First confirm that you are online, then manually execute once: `python -m pip install requests`. For slow domestic networks, you can add a mirror:
`python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple requests`

**Q: Clicking "Start Download" has no reaction?**
A: Look at the text in the status bar. If it prompts "Invalid link," it means the link does not start with `http://` or `https://`.

**Q: Speed shows `--`?**
A: In the first few seconds of the initial download, there isn't enough data to calculate the speed, which is normal.

**Q: No pop-up after download?**
A: A pop-up will appear only if the download is successful; failure will prompt a Chinese error message.
---------------------------------------------------------------------------------------------------
[中文]  [中文]  [中文]  [中文]  [中文]  [中文]  [中文]  [中文]  [中文]  [中文]
# MyDM-Lite 下载器

一个**给完全不懂技术的人用**的 Windows 桌面下载器：单任务、多线程分块、界面就一个大按钮。

> 技术栈：Python 3.9+ / Tkinter + ttk / requests / threading（不用 asyncio）
> 界面只有中文，出错也只弹中文提示，不会出现英文异常和堆栈。

---

## 一、小白三步使用法（小白用户请注意：我已经帮你打包好了现成的的.exe文件，无需自己打包！直接去右侧Releases页面下载即可）

1. **复制下载链接**（在浏览器里对着下载地址点右键 → 复制链接）。
2. **双击 `exe.`**。
3. 链接已经自动帮你填好了，**直接点那个大按钮“开始下载”**，等进度条走完就行。

下载完成后会弹窗问“下载完成！是否打开所在文件夹？”，点“是”就直接带你看文件。

文件默认存在你电脑的**“下载”文件夹**里，不用你选。

---

## 二、怎么运行

### 方式 1：双击.exe(在Releases下载)（推荐给普通用户）

```
.exe
```

已打包所有依赖库，双击即可运行
### 方式 2：双击

```
run.bat
```

`run.bat` 会自动帮你做完这些事：

| 情况 | 程序的表现 |
| --- | --- |
| 没装 Python | 弹窗提示“请先安装 Python 3.9 或以上版本，访问 python.org 下载”，不会一闪而过 |
| Python 版本低于 3.9 | 同样弹窗提示升级 |
| 没装 requests | 自动 `pip install -r requirements.txt`，**只在这个缺依赖的时候才联网** |
| 依赖安装失败 | 弹窗提示“依赖安装失败，请检查网络”，不会一闪而过 |
| 一切正常 | 直接打开下载器窗口 |

### 方式 3：命令行

```bat
python main.py
```

或者用 py 启动器：

```bat
py -3 main.py
```

### 方式 4：跑测试（开发者）

```bat
python -m pip install -r dev-requirements.txt
python -m pytest -v
```

不想装 pytest 也可以跑自带的自检脚本（会自动起一个本地测试服务器，把下载链路完整走一遍）：

```bat
python selfcheck.py
```

全部通过时最后一行会打印“自检结束：全部 N 项通过”。

---

## 三、界面说明（从上到下）

| 位置 | 说明 |
| --- | --- |
| 下载链接 | 启动时会自动读剪贴板，有链接就自动填好，并提示“已检测到链接，点击下载即可” |
| 保存到 | 默认是系统“下载”文件夹，右边有“更改”按钮，不想改就不用管 |
| 开始下载 | 大号按钮，点一下就开始（默认 8 线程），不用做任何设置 |
| 取消 | 点一下立刻停止下载，并把临时文件清理干净 |
| 进度条 | 显示整体进度 |
| 状态 | 例如“正在连接服务器…”“正在下载（8 线程）”“已自动调整为 4 线程”“正在合并文件…”“已完成” |
| 速度 | 全局速度（所有线程加起来，按最近 1 秒新增字节统计） |
| 大小 | 已下载 / 总大小 |
| 剩余时间 | 按全局速度估算 |
| 高级选项 | **默认折叠**，展开后可以选线程数：1/2/4/8/16/32/64，默认 8 |

---

## 四、工作原理（为什么能下载快）

1. 先用 `HEAD` 探测文件大小和是否支持分块（超时 5 秒）。
2. HEAD 被拒绝或超时，就改用 `GET` + `Range: bytes=0-0` 再探一次。
3. 还不能分块、或者服务器没给文件大小，就老老实实**单线程**下载。
4. 能分块时，把文件切成若干字节区间，**每个线程写自己的临时文件**：
   `电影.mp4.part0`、`电影.mp4.part1`、`电影.mp4.part2`……
   （绝对不让多个线程抢着写同一个文件，避免数据错乱）
5. 所有分块下完，按顺序合并成 `电影.mp4`，然后删掉所有 `.partN`。
6. 取消或者失败，也会把所有 `.partN` 清理掉。

### 线程数自动调整规则（按优先级）

1. 服务器不支持分块，或者文件大小未知 → 强制 **1 线程**。
2. 每块至少 1MB：`N = max(1, min(你选的线程数, 文件大小 ÷ 1MB))`。
3. 文件小于 64MB → `N = min(N, 16)`（小文件开太多线程反而慢）。
4. 最终线程数 = N，并在状态栏显示“已自动调整为 N 线程”。

### 出错重试规则（每个线程只管自己那一段）

| 情况 | 处理方式 |
| --- | --- |
| 429 服务器限流 | 指数退避等待，最多重试 3 次，**不减少线程数**，退避期间点“取消”能立刻打断 |
| 403 禁止访问 | 不重试，直接提示“服务器拒绝访问，请检查链接或稍后再试” |
| 连接超时 / 网络错误 | 该线程重试 2 次，仍失败提示“网络连接失败，请检查网络后重试” |
| 5xx 服务器错误 | 该线程重试 2 次，仍失败提示“服务器暂时无法响应，请稍后再试” |
| 404 链接不存在 | 提示“链接无效，请检查后重新粘贴” |

重试时会把该分块文件清空重下，简单可靠（不做断点续写）。

### 文件名怎么来的

1. 优先看服务器返回的 `Content-Disposition`，支持 RFC 5987 中文编码
   （`filename*=UTF-8''%E4%B8%AD%E6%96%87.zip` → `中文.zip`）；
2. 其次从 URL 路径里取，一样做 URL 解码；
3. 都没有就叫 `download.bin`；
4. 重名自动加 `(1)`、`(2)` 后缀。

---

## 五、日志

程序会在系统临时目录（Windows 一般是 `%TEMP%`）里写 `mydm.log`：

* 记录启动、下载开始、线程数调整、异常、下载完成、取消等关键事件；
* 单个文件超过 **5MB** 自动轮转，最多保留最近 **3** 个日志文件；
* 想看日志在哪儿，可以设环境变量 `MYDM_LOG_DIR` 指定目录。

界面上只显示通俗中文，技术细节（异常类型、状态码）都写到日志里，方便排查。

---

## 六、项目结构

```
MyDM-Lite/
├── main.py                     # 界面 + 主循环（root.after(100) 轮询消息队列）
├── downloader.py               # 下载核心：探测 / 分块 / 线程 / 重试 / 合并 / 清理
├── utils.py                    # 文件名解析、大小格式化、默认下载目录、打开文件夹、日志
├── _launcher.py                # run.bat 的启动助手（中文弹窗）
├── run.bat                     # 双击启动（含 Python 检测 + 依赖检测，纯 ASCII）
├── selfcheck.py                # 自检脚本（不用 pytest 也能端到端验证）
├── requirements.txt            # 运行依赖：只有 requests
├── dev-requirements.txt        # 开发依赖：pytest
├── README.md                   # 本文件
├── conftest.py                   # pytest 公共配置（把项目根目录加入 sys.path）
└── tests/
    ├── test_utils.py           # 纯函数 / 路径 / mock 测试
    └── test_range_server.py    # 本地 Range 服务器（ThreadingHTTPServer）+ 集成测试
```

> 提示：`run.bat` 内部**必须保持纯 ASCII**（中文注释会让 cmd.exe 解析错乱），
> 所以所有中文提示都放在 `_launcher.py` 里用弹窗显示。

---

## 七、打包成 exe（可选）(小白用户请注意：我已经帮你打包好了现成的的.exe文件，无需自己打包！直接去右侧Releases页面下载即可)

小白用户多的话，可以打包成单个 exe 发给对方，对方就不用装 Python 了。

```bat
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name MyDM-Lite main.py
```

* 生成的程序在 `dist\MyDM-Lite.exe`；
* `--windowed` 表示不弹黑色控制台窗口；
* 打包后 `run.bat` 就用不上了（exe 自己带 Python 和 requests）；
* 首次启动 exe 会比 python 启动慢几秒（要解压内置运行时），属正常现象。

---

## 八、已知限制（刻意保持简单）

* **单任务**：同一时间只能下一个文件，没有队列。
* **不能限速、不能设代理**：没做。
* **没有托盘图标、没有浏览器扩展**。
* 服务器不支持分块（比如某些网盘临时直链）时只能单线程，速度取决于服务器。
* 需要登录才能下载的链接拿不到（不做 Cookie/鉴权），请用浏览器下载。
* 某些网站在 HEAD 请求上返回 403，本程序会自动回退到 `GET Range` 探测，若仍失败则单线程下载。
* 不支持 BT / 磁力链 / m3u8 视频流。
* 下载超大文件（>4GB）时依赖系统与文件系统支持，未做专门优化。

---

## 九、常见问题

**Q：双击 run.bat 提示“请先安装 Python 3.9 或以上版本”？**
A：打开 <https://www.python.org/downloads/> 下载安装，安装第一步记得勾选 “Add python.exe to PATH”，装完重新双击 `run.bat`。

**Q：提示“依赖安装失败，请检查网络”？**
A：先确认能上网，然后手动执行一次：`python -m pip install requests`。国内网络慢可以加镜像：
`python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple requests`

**Q：点“开始下载”没反应？**
A：看状态栏文字。如果提示“链接无效”，说明链接不是以 `http://` 或 `https://` 开头的。

**Q：速度显示 `--`？**
A：刚开始下载的前一两秒还没有足够数据算速度，属正常现象。

**Q：下载完没弹窗？**
A：只有下载成功才弹；失败会弹中文错误提示。
