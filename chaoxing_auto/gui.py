"""学习通多账号自动化可视化控制台。

运行：
    python gui.py

能力：
    - 多账号管理；
    - 每个账号独立 Chrome Profile 和登录状态；
    - 单个账号可多选课程加入队列；
    - 不同账号并行运行，同一账号内课程按队列顺序执行。
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config import ACCOUNT_STATE_FILE, PASSWORD, PROFILE_DIR, USERNAME
from main import ChaoxingAutomationEngine


@dataclass
class AccountRuntime:
    """GUI 内部账号运行态。"""

    account_id: str
    username: str
    display_name: str
    profile_dir: Path
    storage_state_path: Path
    password: str = ""
    headless: bool = False
    allow_manual_verify: bool = True
    courses: List[Dict[str, str]] = field(default_factory=list)
    command_queue: "queue.Queue[tuple[str, Any]]" = field(default_factory=queue.Queue)
    thread: Optional[threading.Thread] = None
    engine: Optional[ChaoxingAutomationEngine] = None
    status: str = "未登录"
    message: str = ""
    current_course: str = ""
    progress: float = 0.0
    queued_count: int = 0
    running: bool = False
    closing: bool = False


class ChaoxingGUI(tk.Tk):
    """多账号 Tkinter 控制台。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("学习通自动学习 - 多账号控制台")
        self.geometry("1450x860")
        self.minsize(1180, 680)

        self.username_var = tk.StringVar(value=USERNAME)
        self.password_var = tk.StringVar(value=PASSWORD)
        self.visible_browser_var = tk.BooleanVar(value=True)
        self.manual_verify_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="新增账号后获取课程；首次登录可勾选手动登录。")

        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.accounts: Dict[str, AccountRuntime] = {}
        self.selected_account_id: Optional[str] = None
        self.task_counter = 0
        self._last_log_message: Dict[str, str] = {}
        self._selecting_account = False

        self._build_ui()
        self._load_accounts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll_events)

    # ----------------------------- 配置 -----------------------------
    def _load_accounts(self) -> None:
        try:
            data = json.loads(ACCOUNT_STATE_FILE.read_text(encoding="utf-8")) if ACCOUNT_STATE_FILE.exists() else {}
            items = data.get("accounts") if isinstance(data, dict) else []
            items = items if isinstance(items, list) else []
        except Exception:
            items = []

        for item in items:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            account_id = str(item.get("account_id") or "").strip()
            if not username or not account_id or account_id in self.accounts:
                continue
            display_name = username
            runtime = self._create_runtime(account_id, username, display_name)
            self.accounts[account_id] = runtime
            self._upsert_account_row(runtime)

        if self.accounts:
            self._select_account(next(iter(self.accounts)))

    def _save_accounts(self) -> None:
        try:
            ACCOUNT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ACCOUNT_STATE_FILE.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "account_id": runtime.account_id,
                                "username": runtime.username,
                                "display_name": runtime.display_name,
                                "profile_dir": str(runtime.profile_dir),
                            }
                            for runtime in self.accounts.values()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"保存账号配置失败: {exc}")

    @staticmethod
    def _safe_account_id(username: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", username.strip())[:32].strip("_")
        digest = hashlib.sha1(username.encode("utf-8")).hexdigest()[:8]
        return f"{cleaned or 'account'}_{digest}"

    def _create_runtime(self, account_id: str, username: str, display_name: str) -> AccountRuntime:
        base_dir = PROFILE_DIR / account_id
        return AccountRuntime(
            account_id=account_id,
            username=username,
            display_name=display_name,
            profile_dir=base_dir / "chrome",
            storage_state_path=base_dir / "auth.json",
        )

    # ----------------------------- UI -----------------------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        account_frame = ttk.LabelFrame(root, text="账号管理", padding=10)
        account_frame.pack(fill=tk.X)
        for col in (1, 3):
            account_frame.columnconfigure(col, weight=1)

        ttk.Label(account_frame, text="账号:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        ttk.Entry(account_frame, textvariable=self.username_var).grid(row=0, column=1, sticky=tk.EW, padx=(0, 12), pady=4)
        ttk.Label(account_frame, text="密码:").grid(row=0, column=2, sticky=tk.W, padx=(0, 6), pady=4)
        ttk.Entry(account_frame, textvariable=self.password_var).grid(row=0, column=3, sticky=tk.EW, padx=(0, 12), pady=4)
        ttk.Button(account_frame, text="新增/登录并获取课程", command=self._login_or_add_account).grid(row=0, column=4, sticky=tk.EW, pady=4)

        ttk.Checkbutton(account_frame, text="显示浏览器窗口", variable=self.visible_browser_var).grid(row=1, column=1, sticky=tk.W, pady=4)
        ttk.Checkbutton(
            account_frame,
            text="首次/验证码时允许我在浏览器中手动登录",
            variable=self.manual_verify_var,
        ).grid(row=1, column=3, sticky=tk.W, pady=4)
        ttk.Button(account_frame, text="刷新选中账号课程", command=self._refresh_selected_account).grid(row=1, column=4, sticky=tk.EW, pady=4)

        body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.LabelFrame(body, text="账号列表", padding=8)
        body.add(left, weight=1)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.account_tree = ttk.Treeview(
            left,
            columns=("username", "status", "queued", "current", "progress"),
            show="headings",
            selectmode="browse",
        )
        for col, text, width in (
            ("username", "账号", 150),
            ("status", "状态", 90),
            ("queued", "待刷", 55),
            ("current", "当前课程", 180),
            ("progress", "进度", 70),
        ):
            self.account_tree.heading(col, text=text)
            self.account_tree.column(col, width=width, anchor=tk.W if col in {"username", "current"} else tk.CENTER)
        self.account_tree.grid(row=0, column=0, sticky=tk.NSEW)
        self.account_tree.bind("<<TreeviewSelect>>", self._on_account_selected)
        ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.account_tree.yview).grid(row=0, column=1, sticky=tk.NS)

        account_actions = ttk.Frame(left)
        account_actions.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        ttk.Button(account_actions, text="暂停选中账号", command=self._stop_selected_account).pack(side=tk.LEFT)
        ttk.Button(account_actions, text="关闭选中账号浏览器", command=self._close_selected_account).pack(side=tk.LEFT, padx=(8, 0))

        right = ttk.Frame(body)
        body.add(right, weight=3)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_course_frame(right)
        self._build_task_frame(right)
        self._build_status_frame(right)

    def _build_course_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="选中账号的课程列表（支持 Ctrl/Shift 多选）", padding=8)
        frame.grid(row=0, column=0, sticky=tk.NSEW)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        columns = ("index", "name", "course_id", "start_time", "end_time", "exam_status", "task_progress", "url")
        self.course_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "index": "序号",
            "name": "课程名",
            "course_id": "courseId",
            "start_time": "开始时间",
            "end_time": "截止时间",
            "exam_status": "考试",
            "task_progress": "任务进度",
            "url": "链接",
        }
        widths = {"index": 55, "name": 260, "course_id": 130, "start_time": 135, "end_time": 135, "exam_status": 80, "task_progress": 110, "url": 360}
        for col in columns:
            self.course_tree.heading(col, text=headings[col])
            self.course_tree.column(col, width=widths[col], anchor=tk.W if col in {"name", "url"} else tk.CENTER)
        self.course_tree.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.course_tree.yview)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.course_tree.xview)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)
        self.course_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        ttk.Button(actions, text="加入选中课程到刷课列表", command=self._enqueue_selected_courses).pack(side=tk.LEFT)
        ttk.Button(actions, text="全选课程", command=self._select_all_courses).pack(side=tk.LEFT, padx=(8, 0))

    def _build_task_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="刷课列表 / 运行状态", padding=8)
        frame.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = ("account", "course", "status", "progress", "message")
        self.task_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, text, width in (
            ("account", "账号", 150),
            ("course", "课程", 260),
            ("status", "状态", 90),
            ("progress", "进度", 80),
            ("message", "消息", 520),
        ):
            self.task_tree.heading(col, text=text)
            self.task_tree.column(col, width=width, anchor=tk.W if col in {"account", "course", "message"} else tk.CENTER)
        self.task_tree.grid(row=0, column=0, sticky=tk.NSEW)
        ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.task_tree.yview).grid(row=0, column=1, sticky=tk.NS)

    def _build_status_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="状态 / 日志", padding=8)
        frame.grid(row=2, column=0, sticky=tk.NSEW, pady=(10, 0))
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, textvariable=self.status_var, foreground="#0f5132").grid(row=0, column=0, sticky=tk.EW)
        self.log_text = tk.Text(frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=1, column=0, sticky=tk.NSEW, pady=(8, 0))
        log_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky=tk.NS, pady=(8, 0))
        self.log_text.configure(yscrollcommand=log_scroll.set)

    # ----------------------------- 账号动作 -----------------------------
    def _login_or_add_account(self) -> None:
        username = self.username_var.get().strip()
        password = self.password_var.get()
        display_name = username
        if not username:
            messagebox.showwarning("缺少账号", "请输入账号；如果只想手动登录，也需要填一个账号标识用于管理 Profile。")
            return
        if not password and not self.manual_verify_var.get():
            messagebox.showwarning("缺少密码", "未填写密码时请勾选“允许手动登录”。")
            return

        account_id = self._safe_account_id(username)
        runtime = self.accounts.get(account_id)
        if runtime is None:
            runtime = self._create_runtime(account_id, username, display_name)
            self.accounts[account_id] = runtime
        runtime.username = username
        runtime.display_name = display_name
        runtime.password = password
        runtime.headless = not self.visible_browser_var.get()
        runtime.allow_manual_verify = self.manual_verify_var.get()
        self._upsert_account_row(runtime)
        self._save_accounts()
        self._ensure_account_thread(runtime)
        runtime.command_queue.put(("login", None))
        self._select_account(account_id)
        self._append_log(f"[{runtime.display_name}] 已提交登录/获取课程请求")

    def _refresh_selected_account(self) -> None:
        runtime = self._selected_runtime()
        if runtime is None:
            return
        self._ensure_account_thread(runtime)
        runtime.command_queue.put(("login", None))
        self._append_log(f"[{runtime.display_name}] 已提交刷新课程请求")

    def _enqueue_selected_courses(self) -> None:
        runtime = self._selected_runtime()
        if runtime is None:
            return
        selection = self.course_tree.selection()
        if not selection:
            messagebox.showwarning("未选择课程", "请先选择一门或多门课程。")
            return

        courses: List[Dict[str, str]] = []
        task_ids: List[str] = []
        for item_id in selection:
            try:
                course_index = int(str(self.course_tree.set(item_id, "index"))) - 1
                course = dict(runtime.courses[course_index])
            except (ValueError, IndexError):
                continue
            self.task_counter += 1
            task_id = f"task_{self.task_counter}"
            task_ids.append(task_id)
            courses.append(course)
            self.task_tree.insert(
                "",
                tk.END,
                iid=task_id,
                values=(runtime.display_name, course.get("name", ""), "排队中", "0%", "等待账号线程执行"),
            )

        if not courses:
            messagebox.showerror("选择错误", "无法读取选中的课程，请重新选择。")
            return

        runtime.queued_count += len(courses)
        self._upsert_account_row(runtime)
        self._ensure_account_thread(runtime)
        runtime.command_queue.put(("add_courses", {"courses": courses, "task_ids": task_ids}))
        self._append_log(f"[{runtime.display_name}] 已加入 {len(courses)} 门课程到刷课列表")

    def _stop_selected_account(self) -> None:
        runtime = self._selected_runtime()
        if runtime is None:
            return
        if runtime.engine is not None:
            runtime.engine.stop_task()
        runtime.status = "暂停中"
        runtime.message = "已请求暂停"
        self._upsert_account_row(runtime)
        self._append_log(f"[{runtime.display_name}] 已请求暂停")

    def _close_selected_account(self) -> None:
        runtime = self._selected_runtime()
        if runtime is None:
            return
        if not messagebox.askyesno("关闭账号浏览器", f"确定关闭账号 {runtime.display_name} 的浏览器和后台线程吗？"):
            return
        runtime.closing = True
        if runtime.engine is not None:
            runtime.engine.stop_task()
        runtime.command_queue.put(("close", None))
        runtime.status = "关闭中"
        self._upsert_account_row(runtime)

    def _select_all_courses(self) -> None:
        children = self.course_tree.get_children()
        if children:
            self.course_tree.selection_set(children)

    # ----------------------------- 后台线程 -----------------------------
    def _ensure_account_thread(self, runtime: AccountRuntime) -> None:
        if runtime.thread is not None and runtime.thread.is_alive():
            return
        runtime.closing = False
        runtime.thread = threading.Thread(target=self._account_worker, args=(runtime,), daemon=True)
        runtime.thread.start()

    def _create_engine(self, runtime: AccountRuntime) -> ChaoxingAutomationEngine:
        return ChaoxingAutomationEngine(
            headless=runtime.headless,
            username=runtime.username,
            password=runtime.password,
            allow_manual_verify=runtime.allow_manual_verify,
            status_callback=self._make_status_callback(runtime.account_id),
            browser_profile_dir=runtime.profile_dir,
            storage_state_path=runtime.storage_state_path,
        )

    def _account_worker(self, runtime: AccountRuntime) -> None:
        while not runtime.closing:
            command, payload = runtime.command_queue.get()
            try:
                if command == "login":
                    self._worker_login(runtime)
                elif command == "add_courses":
                    payload = payload or {}
                    self._worker_run_courses(runtime, list(payload.get("courses") or []), list(payload.get("task_ids") or []))
                elif command == "close":
                    break
            except Exception as exc:  # noqa: BLE001
                self.events.put(("account_error", {"account_id": runtime.account_id, "error": f"{exc}\n{traceback.format_exc()}"}))

        try:
            if runtime.engine is not None:
                runtime.engine.close()
        finally:
            runtime.engine = None
            runtime.running = False
            runtime.status = "已关闭"
            self.events.put(("account_state", {"account_id": runtime.account_id}))

    def _worker_login(self, runtime: AccountRuntime) -> None:
        runtime.running = True
        runtime.status = "登录中"
        runtime.message = "正在登录并获取课程列表"
        runtime.progress = 0.0
        self.events.put(("account_state", {"account_id": runtime.account_id}))

        if runtime.engine is None:
            runtime.engine = self._create_engine(runtime)
        else:
            runtime.engine.username = runtime.username
            runtime.engine.password = runtime.password
            runtime.engine.allow_manual_verify = runtime.allow_manual_verify
            runtime.engine.set_status_callback(self._make_status_callback(runtime.account_id))

        courses = runtime.engine.login_and_get_courses(username=runtime.username, password=runtime.password)
        runtime.courses = courses
        runtime.status = "已登录"
        runtime.message = f"已获取 {len(courses)} 门课程"
        runtime.running = False
        self.events.put(("courses", {"account_id": runtime.account_id, "courses": courses}))
        self.events.put(("account_state", {"account_id": runtime.account_id}))

    def _worker_run_courses(self, runtime: AccountRuntime, courses: List[Dict[str, str]], task_ids: List[str]) -> None:
        if not courses:
            return
        if runtime.engine is None:
            self._worker_login(runtime)
        if runtime.engine is None:
            raise RuntimeError("账号浏览器未初始化")

        runtime.running = True
        runtime.status = "学习中"
        self.events.put(("account_state", {"account_id": runtime.account_id}))

        for index, course in enumerate(courses):
            task_id = task_ids[index] if index < len(task_ids) else ""
            if runtime.closing:
                break
            course_name = str(course.get("name") or "选中课程")
            runtime.current_course = course_name
            runtime.progress = 0.0
            runtime.message = f"开始学习: {course_name}"
            self.events.put(("task", {"task_id": task_id, "status": "运行中", "progress": 0.0, "message": runtime.message}))
            self.events.put(("account_state", {"account_id": runtime.account_id}))

            final_status = runtime.engine.start_selected_course(course)
            final_state = str(final_status.get("status") or "")
            final_message = str(final_status.get("message") or "")
            runtime.queued_count = max(0, runtime.queued_count - 1)

            if final_state == "success":
                runtime.progress = 100.0
                self.events.put(("task", {"task_id": task_id, "status": "完成", "progress": 100.0, "message": final_message}))
            elif final_state == "stopped":
                self.events.put(("task", {"task_id": task_id, "status": "已暂停", "progress": runtime.progress, "message": final_message}))
                runtime.status = "已暂停"
                runtime.message = final_message
                self.events.put(("account_state", {"account_id": runtime.account_id}))
                break
            else:
                self.events.put(("task", {"task_id": task_id, "status": "失败", "progress": runtime.progress, "message": final_message}))

            runtime.courses = list(runtime.engine.courses or runtime.courses)
            self.events.put(("courses", {"account_id": runtime.account_id, "courses": runtime.courses}))
            self.events.put(("account_state", {"account_id": runtime.account_id}))

        if runtime.status != "已暂停":
            runtime.status = "已登录"
            runtime.current_course = ""
            runtime.message = "队列执行完成，可继续加入课程"
        runtime.running = False
        self.events.put(("account_state", {"account_id": runtime.account_id}))

    def _make_status_callback(self, account_id: str):  # type: ignore[no-untyped-def]
        def callback(status: Dict[str, Any]) -> None:
            self.events.put(("status", {"account_id": account_id, "status": status}))

        return callback

    # ----------------------------- 事件刷新 -----------------------------
    def _poll_events(self) -> None:
        while True:
            try:
                event_name, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event_name == "status":
                self._apply_status(str(payload.get("account_id")), dict(payload.get("status") or {}))
            elif event_name == "courses":
                self._apply_courses(str(payload.get("account_id")), list(payload.get("courses") or []))
            elif event_name == "task":
                self._apply_task_update(dict(payload or {}))
            elif event_name == "account_state":
                runtime = self.accounts.get(str(payload.get("account_id")))
                if runtime is not None:
                    self._upsert_account_row(runtime)
            elif event_name == "account_error":
                self._apply_account_error(str(payload.get("account_id")), str(payload.get("error") or ""))
        self.after(200, self._poll_events)

    def _apply_status(self, account_id: str, status: Dict[str, Any]) -> None:
        runtime = self.accounts.get(account_id)
        if runtime is None:
            return
        runtime.status = str(status.get("status") or runtime.status or "运行中")
        runtime.message = str(status.get("message") or runtime.message or "")
        runtime.progress = float(status.get("progress") or runtime.progress or 0.0)
        course = str(status.get("course_name") or status.get("course") or runtime.current_course or "")
        if course:
            runtime.current_course = course
        self._upsert_account_row(runtime)

        prefix = f"[{runtime.display_name}]"
        self.status_var.set(f"{prefix} {runtime.status} {runtime.progress:.2f}% {runtime.message}")
        if runtime.message and runtime.message != self._last_log_message.get(account_id):
            self._append_log(f"{prefix} {runtime.message}")
            self._last_log_message[account_id] = runtime.message

        current_task = self._find_running_task_for_account(account_id)
        if current_task:
            self._apply_task_update({"task_id": current_task, "status": runtime.status, "progress": runtime.progress, "message": runtime.message})

    def _apply_courses(self, account_id: str, courses: List[Dict[str, str]]) -> None:
        runtime = self.accounts.get(account_id)
        if runtime is None:
            return
        runtime.courses = courses
        self._upsert_account_row(runtime)
        if self.selected_account_id == account_id:
            self._load_courses_for_account(runtime)
        self._append_log(f"[{runtime.display_name}] 课程列表加载完成，共 {len(courses)} 门")

    def _apply_task_update(self, payload: Dict[str, Any]) -> None:
        task_id = str(payload.get("task_id") or "")
        if not task_id or not self.task_tree.exists(task_id):
            return
        values = list(self.task_tree.item(task_id, "values"))
        while len(values) < 5:
            values.append("")
        values[2] = str(payload.get("status") or values[2])
        if payload.get("progress") is not None:
            try:
                values[3] = f"{float(payload.get('progress')):.2f}%"
            except (TypeError, ValueError):
                values[3] = str(payload.get("progress"))
        if payload.get("message") is not None:
            values[4] = str(payload.get("message"))
        self.task_tree.item(task_id, values=values)
        self.task_tree.see(task_id)

    def _apply_account_error(self, account_id: str, error_text: str) -> None:
        runtime = self.accounts.get(account_id)
        display = runtime.display_name if runtime is not None else account_id
        first_line = error_text.strip().splitlines()[0] if error_text.strip() else "未知错误"
        if runtime is not None:
            runtime.status = "失败"
            runtime.message = first_line
            runtime.running = False
            self._upsert_account_row(runtime)
        self._append_log(f"[{display}] 任务失败: {first_line}")
        self._append_log(error_text)
        self.status_var.set(f"[{display}] 任务失败: {first_line}")
        messagebox.showerror("账号任务失败", f"[{display}] {first_line}")

    # ----------------------------- 表格辅助 -----------------------------
    def _upsert_account_row(self, runtime: AccountRuntime) -> None:
        values = (runtime.username, runtime.status, runtime.queued_count, runtime.current_course, f"{runtime.progress:.2f}%")
        if self.account_tree.exists(runtime.account_id):
            self.account_tree.item(runtime.account_id, values=values)
        else:
            self.account_tree.insert("", tk.END, iid=runtime.account_id, values=values)

    def _on_account_selected(self, _event: Any = None) -> None:
        if self._selecting_account:
            return
        selection = self.account_tree.selection()
        if not selection:
            return
        account_id = str(selection[0])
        if account_id == self.selected_account_id:
            return
        self._select_account(account_id)

    def _select_account(self, account_id: str) -> None:
        runtime = self.accounts.get(account_id)
        if runtime is None:
            return
        self.selected_account_id = account_id
        if self.account_tree.exists(account_id):
            current_selection = tuple(str(item) for item in self.account_tree.selection())
            if current_selection != (account_id,):
                self._selecting_account = True
                try:
                    self.account_tree.selection_set(account_id)
                    self.account_tree.focus(account_id)
                finally:
                    self._selecting_account = False
            else:
                self.account_tree.focus(account_id)
        self.username_var.set(runtime.username)
        self.password_var.set(runtime.password)
        self.visible_browser_var.set(not runtime.headless)
        self.manual_verify_var.set(runtime.allow_manual_verify)
        self._load_courses_for_account(runtime)

    def _load_courses_for_account(self, runtime: AccountRuntime) -> None:
        for item in self.course_tree.get_children():
            self.course_tree.delete(item)
        for index, course in enumerate(runtime.courses, start=1):
            self.course_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    index,
                    course.get("name", ""),
                    self._course_value(course, "course_id"),
                    self._course_value(course, "start_time"),
                    self._course_value(course, "end_time"),
                    str(course.get("exam_status") or course.get("has_exam") or "未显示"),
                    self._task_display(course),
                    course.get("url", ""),
                ),
            )

    def _selected_runtime(self) -> Optional[AccountRuntime]:
        account_id = self.selected_account_id
        if not account_id:
            selection = self.account_tree.selection()
            account_id = str(selection[0]) if selection else ""
        runtime = self.accounts.get(account_id or "")
        if runtime is None:
            messagebox.showwarning("未选择账号", "请先选择或新增一个账号。")
            return None
        return runtime

    def _find_running_task_for_account(self, account_id: str) -> str:
        runtime = self.accounts.get(account_id)
        if runtime is None:
            return ""
        for task_id in self.task_tree.get_children():
            values = list(self.task_tree.item(task_id, "values"))
            if len(values) >= 3 and values[0] == runtime.display_name and values[2] in {"运行中", "running", "学习中"}:
                return str(task_id)
        return ""

    @staticmethod
    def _course_value(course: Dict[str, str], key: str) -> str:
        return str(course.get(key) or "未显示")

    @staticmethod
    def _task_display(course: Dict[str, str]) -> str:
        task_progress = course.get("task_progress")
        if task_progress:
            return str(task_progress)
        task_done = course.get("task_done")
        task_total = course.get("task_total")
        if task_done and task_total:
            return f"{task_done}/{task_total}"
        if task_done:
            return f"已完成 {task_done}"
        if task_total:
            return f"共 {task_total}"
        return "未显示"

    def _append_log(self, message: str) -> None:
        if not message:
            return
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message.rstrip()}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        alive = [runtime for runtime in self.accounts.values() if runtime.thread and runtime.thread.is_alive()]
        if alive and not messagebox.askyesno("退出", "仍有账号线程在运行，确定退出并关闭所有浏览器吗？"):
            return
        self._save_accounts()
        for runtime in self.accounts.values():
            runtime.closing = True
            if runtime.engine is not None:
                try:
                    runtime.engine.stop_task()
                    runtime.engine.close()
                except Exception:
                    pass
            try:
                runtime.command_queue.put_nowait(("close", None))
            except Exception:
                pass
        self.destroy()


def main() -> None:
    app = ChaoxingGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
