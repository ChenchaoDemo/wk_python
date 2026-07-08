# chaoxing_auto

基于 **Python 3.10+ + Playwright + Chromium** 的学习通网页版自动化执行引擎。当前项目只包含 Python 自动化核心，不包含前端、SpringBoot、数据库，目录和接口已按后续 RPA 服务化扩展进行模块化设计。

## 1. 项目功能

- 启动 Chromium 浏览器
- 支持有界面模式 `headless=False` 和无界面模式 `headless=True`
- 学习通网页登录
- 自动保存和复用登录状态 `auth.json`
- 获取我的课程列表
- 根据课程名称进入课程
- 获取章节列表
- 遍历章节并检测 HTML5 `video` 标签
- 自动播放视频并轮询进度
- 视频进度达到 95% 后认为完成
- 使用 `logging` 输出日志到 `logs/app.log`
- 异常自动截图到 `screenshots/error_xxx.png`
- 支持 Playwright 页面调试暂停模式
- 提供 `start_task()`、`stop_task()`、`get_status()` 预留接口

## 2. 环境安装

建议使用虚拟环境。

```bash
cd chaoxing_auto
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux / macOS：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

## 3. 安装 Playwright 浏览器

首次运行前需要安装 Chromium：

```bash
playwright install chromium
```

如果需要安装 Playwright 所有浏览器：

```bash
playwright install
```

## 4. 配置账号

打开 `config/config.py`，填写：

```python
USERNAME = "你的账号"
PASSWORD = "你的密码"
COURSE_NAME = "人工智能导论"
```

也可以使用环境变量：

```powershell
$env:CHAOXING_USERNAME="你的账号"
$env:CHAOXING_PASSWORD="你的密码"
$env:CHAOXING_COURSE_NAME="人工智能导论"
```

登录地址默认：

```python
LOGIN_URL = "https://passport2.chaoxing.com/login"
```

## 5. 运行方式

有界面模式，便于调试：

```bash
python main.py --course "人工智能导论"
```

无界面模式：

```bash
python main.py --course "人工智能导论" --headless
```

调试模式：

```bash
python main.py --course "人工智能导论" --debug
```

调试模式会调用 Playwright 的 `page.pause()`，可以查看当前页面、定位元素和手动操作。

## 6. 项目结构说明

```text
chaoxing_auto/
├── main.py                 # 程序入口，包含自动学习主流程和预留任务接口
├── config/
│   └── config.py           # 配置文件，账号、URL、浏览器、等待时间等
├── browser/
│   └── browser_manager.py  # 浏览器启动、context、page、storage_state 管理
├── login/
│   └── login.py            # 登录模块，账号密码登录、验证码检测、登录状态判断
├── course/
│   ├── course_manager.py   # 课程列表获取、按课程名进入课程
│   └── chapter.py          # 章节列表获取、打开章节
├── video/
│   └── video_player.py     # HTML5 video 检测、播放、进度轮询
├── utils/
│   ├── logger.py           # logging 日志系统
│   └── helper.py           # 截图、状态对象、调试暂停、通用辅助函数
├── logs/
│   └── app.log             # 运行后生成
├── screenshots/            # 异常截图目录，运行后生成
├── auth.json               # 登录状态文件，首次登录成功后生成
├── requirements.txt
└── README.md
```

## 7. 任务状态接口

`main.py` 中提供 `ChaoxingAutomationEngine`：

```python
from main import ChaoxingAutomationEngine

engine = ChaoxingAutomationEngine(course_name="Python基础", headless=False)

# 启动任务，同步执行
result = engine.start_task()
print(result)

# 获取状态
status = engine.get_status()
print(status)

# 请求停止
engine.stop_task()
```

状态对象格式示例：

```json
{
  "course_name": "Python基础",
  "chapter_name": "第一章",
  "progress": 60,
  "status": "running",
  "message": "章节视频播放中",
  "course": "Python基础",
  "chapter": "第一章"
}
```

## 8. 常见问题

### 8.1 第一次登录后生成 auth.json

首次登录成功后会自动保存 `auth.json`。下次启动时 `BrowserManager` 会自动加载该文件，减少重复登录。

### 8.2 出现验证码怎么办

程序会检测验证码、滑块、安全验证等页面，并保存截图。可使用：

```bash
python main.py --course "人工智能导论" --debug
```

进入调试暂停后，在浏览器中手动处理页面，再继续后续流程。

### 8.3 获取不到课程或章节

学习通页面结构可能调整，优先检查：

- 是否登录成功
- `COURSE_LIST_URL` 是否可访问
- 课程名称是否完整或匹配
- 页面是否出现 iframe 或动态加载
- `course/course_manager.py` 和 `course/chapter.py` 中的 selector 是否需要补充

## 9. 后续扩展方向

- 接入 FastAPI，提供 HTTP 接口启动/停止任务
- 将 `TaskStatus` 写入 Redis 或数据库
- 增加任务队列，例如 Celery、RQ、APScheduler
- 支持多账号、多课程并发执行
- 支持课程任务点、作业、测验等更多模块
- 增加页面元素配置化，减少代码内 selector 硬编码
- 增加断点续学、失败重试、章节黑名单/白名单
- 增加 Docker 镜像和 CI 自动测试
