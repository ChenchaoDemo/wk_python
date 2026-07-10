# AGENTS.md

本文件给 Codex/自动化开发代理使用，说明本仓库的项目结构、常用命令、编码约定和安全注意事项。

## 适用范围

- 适用于整个仓库：`E:\github\wk_python`
- 当前主要项目位于：`E:\github\wk_python\chaoxing_auto`
- 若子目录以后新增更近层级的 `AGENTS.md`，以更近层级文件为准。

## 项目概览

`chaoxing_auto` 是一个基于 Python + Playwright + Chromium 的网页自动化项目，核心功能包括：

- 登录学习通网页端
- 获取课程列表和章节列表
- 打开章节/章节卡片
- 检测并播放 HTML5 视频
- 处理运行日志、截图、登录状态缓存
- 提供 Tkinter GUI 和命令行入口

主要目录：

```text
chaoxing_auto/
├── main.py                 # 自动化主流程和任务状态接口
├── gui.py                  # Tkinter 可视化界面
├── config/                 # 配置文件
├── browser/                # Playwright 浏览器管理
├── login/                  # 登录逻辑
├── course/                 # 课程、章节、题目处理
├── video/                  # 视频检测和播放
├── utils/                  # 日志、截图、通用工具
├── requirements.txt        # Python 依赖
└── README.md               # 项目说明
```

## 环境和依赖

推荐在 `chaoxing_auto` 目录内使用虚拟环境：

```powershell
cd E:\github\wk_python\chaoxing_auto
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

依赖目前很少，主要是：

```text
playwright>=1.44.0
```

## 常用运行命令

从 `chaoxing_auto` 目录执行：

```powershell
# GUI
python gui.py

# 命令行：有界面模式
python main.py --course "课程名称" --headed

# 命令行：无界面模式
python main.py --course "课程名称" --headless

# 调试模式
python main.py --course "课程名称" --debug
```

账号、密码、课程名可通过环境变量配置：

```powershell
$env:CHAOXING_USERNAME="账号"
$env:CHAOXING_PASSWORD="密码"
$env:CHAOXING_COURSE_NAME="课程名称"
```

## 验证和检查

修改 Python 文件后，至少做语法检查。为了避免生成额外 `.pyc` 文件，优先使用无落盘编译检查：

```powershell
python -c "from pathlib import Path; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in Path('chaoxing_auto').rglob('*.py') if '.venv' not in p.parts]; print('compile ok')"
```

如果使用 `python -m py_compile ...` 生成了 `__pycache__` 或 `.pyc`，提交前清理未跟踪产物。

## 编码与代码风格

- 所有源码按 UTF-8 读写。
- PowerShell 控制台可能把中文显示成乱码；不要因此误判文件编码，必要时用 Python 按 UTF-8 读取确认。
- 优先保持现有模块划分：
  - 浏览器相关放 `browser/`
  - 登录相关放 `login/`
  - 课程、章节、题目相关放 `course/`
  - 视频播放相关放 `video/`
  - 通用工具放 `utils/`
- 避免把大型流程继续堆到 `main.py`，新增功能优先拆到对应模块。
- 选择器和页面结构兼容逻辑要写清楚日志，方便用户根据截图/日志排查。
- 捕获异常时保留原始异常信息，必要时调用现有 `save_screenshot` 辅助定位。

## Git 与文件注意事项

不要提交以下运行产物或敏感/本地文件：

- `chaoxing_auto/.venv/`
- `chaoxing_auto/auth.json`
- `chaoxing_auto/logs/`
- `chaoxing_auto/screenshots/`
- `chaoxing_auto/config/gui_state.json`
- `chaoxing_auto/config/deepseek.local.json`
- `chaoxing_auto/config/question_bank.sqlite3*`
- `__pycache__/`、`*.pyc`

当前 `.gitignore` 已覆盖部分路径；新增运行产物时同步维护 `.gitignore`。

## 修改自动化逻辑时的建议

- Playwright 的 `Locator.click(force=True)` 仍可能因隐藏元素失败；对学习通动态页签、隐藏卡片，可考虑 DOM `evaluate` 方式触发页面自身 JS。
- 页面经常包含 iframe、动态加载、隐藏 tab，改动后要保留充分等待和日志。
- GUI 模式下不要轻易关闭浏览器窗口，现有设计倾向于任务结束后保留或返回课程列表。
- 涉及真实登录、验证码、浏览器可视化运行时，除非用户明确要求，否则优先做静态检查和最小验证。

## 回复用户时

- 默认使用简体中文。
- 简明说明改了哪些文件、验证了什么、是否还有需要用户手动运行确认的步骤。
- 引用文件路径时使用绝对路径，例如：`E:\github\wk_python\chaoxing_auto\course\chapter.py`。
