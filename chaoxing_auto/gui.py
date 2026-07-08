"""学习通自动化可视化界面。

运行：
    python gui.py

流程：
    1. 输入账号和密码；
    2. 点击“登录并获取课程”；
    3. 在课程列表中选择一门课程；
    4. 点击“开始学习选中课程”。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

# 支持直接 `python gui.py` 运行。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config import PASSWORD, USERNAME
from main import ChaoxingAutomationEngine

GUI_STATE_FILE = PROJECT_ROOT / "config" / "gui_state.json"


class ChaoxingGUI(tk.Tk):
    """Tkinter 可视化控制台。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("学习通自动学习 - 可视化控制台")
        self.geometry("1280x760")
        self.minsize(1080, 620)

        saved_login = self._load_saved_login()
        self.username_var = tk.StringVar(value=saved_login.get("username", USERNAME))
        self.password_var = tk.StringVar(value=saved_login.get("password", PASSWORD))
        self.visible_browser_var = tk.BooleanVar(value=True)
        self.manual_verify_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="请先输入账号密码，然后点击“登录并获取课程”。")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.course_selection_queue: Optional["queue.Queue[Optional[Dict[str, str]]]"] = None
        self.worker: Optional[threading.Thread] = None
        self.engine: Optional[ChaoxingAutomationEngine] = None
        self.courses: List[Dict[str, str]] = []
        self.phase = "idle"
        self._last_log_message = ""
        self._last_final_state = ""

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll_events)

    @staticmethod
    def _load_saved_login() -> Dict[str, str]:
        """读取 GUI 上一次输入的账号密码。"""

        try:
            if not GUI_STATE_FILE.exists():
                return {}
            data = json.loads(GUI_STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {
                "username": str(data.get("username") or ""),
                "password": str(data.get("password") or ""),
            }
        except Exception:
            return {}

    @staticmethod
    def _save_login(username: str, password: str) -> None:
        """保存 GUI 本次输入的账号密码，供下次启动自动填充。"""

        try:
            GUI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            GUI_STATE_FILE.write_text(
                json.dumps(
                    {
                        "username": username,
                        "password": password,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            # 保存失败不影响本次登录流程。
            pass

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)

        account_frame = ttk.LabelFrame(root, text="账号登录", padding=12)
        account_frame.pack(fill=tk.X)
        account_frame.columnconfigure(1, weight=1)
        account_frame.columnconfigure(3, weight=1)

        ttk.Label(account_frame, text="账号：").grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=5)
        self.username_entry = ttk.Entry(account_frame, textvariable=self.username_var)
        self.username_entry.grid(row=0, column=1, sticky=tk.EW, padx=(0, 14), pady=5)

        ttk.Label(account_frame, text="密码：").grid(row=0, column=2, sticky=tk.W, padx=(0, 6), pady=5)
        self.password_entry = ttk.Entry(account_frame, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=0, column=3, sticky=tk.EW, padx=(0, 14), pady=5)

        self.login_button = ttk.Button(account_frame, text="登录并获取课程", command=self._start_login_and_fetch)
        self.login_button.grid(row=0, column=4, sticky=tk.EW, pady=5)

        self.visible_check = ttk.Checkbutton(account_frame, text="显示浏览器窗口", variable=self.visible_browser_var)
        self.visible_check.grid(row=1, column=1, sticky=tk.W, pady=5)

        self.manual_check = ttk.Checkbutton(
            account_frame,
            text="遇到验证码/滑块时等待我手动完成",
            variable=self.manual_verify_var,
        )
        self.manual_check.grid(row=1, column=3, sticky=tk.W, pady=5)

        course_frame = ttk.LabelFrame(root, text="课程列表", padding=12)
        course_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        course_frame.rowconfigure(0, weight=1)
        course_frame.columnconfigure(0, weight=1)

        columns = (
            "index",
            "name",
            "course_id",
            "start_time",
            "end_time",
            "exam_status",
            "exam_start_time",
            "exam_end_time",
            "task_progress",
            "url",
        )
        self.course_tree = ttk.Treeview(course_frame, columns=columns, show="headings", selectmode="browse")
        self.course_tree.heading("index", text="序号")
        self.course_tree.heading("name", text="课程名")
        self.course_tree.heading("course_id", text="courseId")
        self.course_tree.heading("start_time", text="课程开始时间")
        self.course_tree.heading("end_time", text="课程截止时间")
        self.course_tree.heading("exam_status", text="是否有考试")
        self.course_tree.heading("exam_start_time", text="考试开始时间")
        self.course_tree.heading("exam_end_time", text="考试截止时间")
        self.course_tree.heading("task_progress", text="任务完成数/总数")
        self.course_tree.heading("url", text="课程链接")
        self.course_tree.column("index", width=60, anchor=tk.CENTER, stretch=False)
        self.course_tree.column("name", width=260, anchor=tk.W)
        self.course_tree.column("course_id", width=130, anchor=tk.CENTER)
        self.course_tree.column("start_time", width=150, anchor=tk.W)
        self.course_tree.column("end_time", width=150, anchor=tk.W)
        self.course_tree.column("exam_status", width=110, anchor=tk.CENTER)
        self.course_tree.column("exam_start_time", width=150, anchor=tk.W)
        self.course_tree.column("exam_end_time", width=150, anchor=tk.W)
        self.course_tree.column("task_progress", width=130, anchor=tk.CENTER)
        self.course_tree.column("url", width=420, anchor=tk.W)
        self.course_tree.grid(row=0, column=0, sticky=tk.NSEW)
        self.course_tree.bind("<Double-1>", lambda _event: self._submit_selected_course())

        tree_scroll = ttk.Scrollbar(course_frame, orient=tk.VERTICAL, command=self.course_tree.yview)
        tree_scroll.grid(row=0, column=1, sticky=tk.NS)
        tree_x_scroll = ttk.Scrollbar(course_frame, orient=tk.HORIZONTAL, command=self.course_tree.xview)
        tree_x_scroll.grid(row=1, column=0, sticky=tk.EW)
        self.course_tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_x_scroll.set)

        action_frame = ttk.Frame(root)
        action_frame.pack(fill=tk.X, pady=(12, 0))

        self.start_button = ttk.Button(
            action_frame,
            text="开始学习选中课程",
            command=self._submit_selected_course,
            state=tk.DISABLED,
        )
        self.start_button.pack(side=tk.LEFT)

        self.stop_button = ttk.Button(action_frame, text="暂停任务", command=self._stop_task, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))

        self.progress_bar = ttk.Progressbar(action_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=16)

        self.progress_label = ttk.Label(action_frame, text="0%")
        self.progress_label.pack(side=tk.RIGHT)

        status_frame = ttk.LabelFrame(root, text="状态", padding=12)
        status_frame.pack(fill=tk.BOTH, pady=(12, 0))
        status_frame.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="#0f5132")
        self.status_label.grid(row=0, column=0, sticky=tk.EW)

        log_frame = ttk.Frame(status_frame)
        log_frame.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 0))
        status_frame.rowconfigure(1, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _start_login_and_fetch(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "当前已有任务在运行，请等待或点击暂停。")
            return

        if self.engine is not None:
            # 如果上一次任务是暂停状态，浏览器会被保留；开始新一轮登录前先释放旧浏览器。
            self.engine.close()
            self.engine = None

        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username:
            messagebox.showwarning("缺少账号", "请输入学习通账号。")
            self.username_entry.focus_set()
            return
        if not password:
            messagebox.showwarning("缺少密码", "请输入学习通密码。")
            self.password_entry.focus_set()
            return

        self._save_login(username, password)
        self._clear_courses()
        self._set_controls_for_phase("logging")
        self.progress_var.set(0.0)
        self.progress_label.config(text="0%")
        self.status_var.set("正在登录并获取课程列表...")
        self._append_log("开始登录并获取课程列表")

        self.course_selection_queue = queue.Queue(maxsize=1)
        self.worker = threading.Thread(
            target=self._automation_worker,
            args=(username, password, self.course_selection_queue),
            daemon=True,
        )
        self.worker.start()

    def _automation_worker(
        self,
        username: str,
        password: str,
        selection_queue: "queue.Queue[Optional[Dict[str, str]]]",
    ) -> None:
        self.engine = ChaoxingAutomationEngine(
            headless=not self.visible_browser_var.get(),
            allow_manual_verify=self.manual_verify_var.get(),
            status_callback=self._enqueue_status,
        )

        try:
            courses = self.engine.login_and_get_courses(username=username, password=password)
            self.events.put(("courses", courses))
            if not courses:
                self.engine.close()
                self.events.put(("final", {"status": "failed", "message": "未获取到课程，请检查账号或课程列表页面"}))
                return

            self.events.put(("phase", "waiting_course"))

            selected_course = selection_queue.get()
            if selected_course is None:
                self.engine.close()
                self.events.put(("final", {"status": "stopped", "message": "已取消课程选择"}))
                return

            self.events.put(("phase", "learning"))
            final_status = self.engine.start_selected_course(selected_course)
            self.events.put(("final", final_status))
        except Exception as exc:  # noqa: BLE001 - 需要把异常完整反馈到界面
            self.events.put(("error", f"{exc}\n{traceback.format_exc()}"))
        finally:
            self.events.put(("done", None))

    def _enqueue_status(self, status: Dict[str, Any]) -> None:
        self.events.put(("status", status))

    def _submit_selected_course(self) -> None:
        if self.phase != "waiting_course":
            return
        if self.course_selection_queue is None:
            return

        selection = self.course_tree.selection()
        if not selection:
            messagebox.showwarning("未选择课程", "请先在课程列表里选择一门课程。")
            return

        item_id = selection[0]
        index_text = str(self.course_tree.set(item_id, "index"))
        try:
            course_index = int(index_text) - 1
            selected_course = self.courses[course_index]
        except (ValueError, IndexError):
            messagebox.showerror("选择错误", "无法读取选中的课程，请重新选择。")
            return

        try:
            self.course_selection_queue.put_nowait(selected_course)
        except queue.Full:
            messagebox.showinfo("提示", "课程已经提交，请等待任务开始。")
            return

        self._set_controls_for_phase("learning")
        self.status_var.set(f"已选择课程：{selected_course.get('name', '')}，正在开始学习...")
        self._append_log(f"已选择课程：{selected_course.get('name', '')}")

    def _stop_task(self) -> None:
        if self.engine is not None:
            self.engine.stop_task()

        if self.phase == "waiting_course" and self.course_selection_queue is not None:
            try:
                self.course_selection_queue.put_nowait(None)
            except queue.Full:
                pass

        self.status_var.set("已请求暂停任务，请等待当前步骤停下来；浏览器窗口会保留。")
        self._append_log("已请求暂停任务")
        self.stop_button.config(state=tk.DISABLED)

    def _poll_events(self) -> None:
        while True:
            try:
                event_name, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event_name == "status":
                self._apply_status(payload)
            elif event_name == "courses":
                self._load_courses(payload)
            elif event_name == "phase":
                self._set_controls_for_phase(str(payload))
            elif event_name == "final":
                self._apply_final_status(payload)
            elif event_name == "error":
                self._handle_worker_error(str(payload))
            elif event_name == "done":
                self._handle_worker_done()

        self.after(200, self._poll_events)

    def _apply_status(self, status: Dict[str, Any]) -> None:
        message = str(status.get("message") or "")
        status_text = str(status.get("status") or "")
        progress = float(status.get("progress") or 0.0)
        course = str(status.get("course_name") or status.get("course") or "")
        chapter = str(status.get("chapter_name") or status.get("chapter") or "")

        self.progress_var.set(max(0.0, min(progress, 100.0)))
        self.progress_label.config(text=f"{progress:.2f}%")

        parts = [f"状态：{status_text or 'idle'}"]
        if course:
            parts.append(f"课程：{course}")
        if chapter:
            parts.append(f"章节：{chapter}")
        if message:
            parts.append(message)
        self.status_var.set(" | ".join(parts))

        if message and message != self._last_log_message:
            self._append_log(message)
            self._last_log_message = message

    def _load_courses(self, courses: List[Dict[str, str]]) -> None:
        self._clear_courses()
        self.courses = courses
        for index, course in enumerate(courses, start=1):
            self.course_tree.insert(
                "",
                tk.END,
                values=(
                    index,
                    course.get("name", ""),
                    self._course_value(course, "course_id"),
                    self._course_value(course, "start_time"),
                    self._course_value(course, "end_time"),
                    self._exam_display(course),
                    self._course_value(course, "exam_start_time"),
                    self._course_value(course, "exam_end_time"),
                    self._task_display(course),
                    course.get("url", ""),
                ),
            )

        if courses:
            first_item = self.course_tree.get_children()[0]
            self.course_tree.selection_set(first_item)
            self.course_tree.focus(first_item)
            self._append_log(f"课程列表加载完成，共 {len(courses)} 门")
        else:
            self._append_log("未获取到课程")

    @staticmethod
    def _course_value(course: Dict[str, str], key: str) -> str:
        return str(course.get(key) or "未显示")

    @staticmethod
    def _exam_display(course: Dict[str, str]) -> str:
        return str(course.get("exam_status") or course.get("has_exam") or "未显示")

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

    def _apply_final_status(self, status: Dict[str, Any]) -> None:
        message = str(status.get("message") or "任务结束")
        final_state = str(status.get("status") or "")
        self._last_final_state = final_state
        self._append_log(f"最终状态：{final_state}，{message}")
        self.status_var.set(f"最终状态：{final_state}，{message}")
        if final_state == "success":
            self.progress_var.set(100.0)
            self.progress_label.config(text="100%")

    def _handle_worker_error(self, error_text: str) -> None:
        first_line = error_text.strip().splitlines()[0] if error_text.strip() else "未知错误"
        self.status_var.set(f"任务失败：{first_line}")
        self._append_log(f"任务失败：{first_line}")
        self._append_log(error_text)
        messagebox.showerror("任务失败", first_line)

    def _handle_worker_done(self) -> None:
        if self.phase == "learning" and self._last_final_state == "stopped":
            self._set_controls_for_phase("paused")
            self.status_var.set("任务已暂停，浏览器窗口已保留。需要重新开始时可点击“登录并获取课程”。")
        elif self.phase in {"learning", "logging", "waiting_course"}:
            self._set_controls_for_phase("idle")

    def _set_controls_for_phase(self, phase: str) -> None:
        self.phase = phase

        is_idle = phase == "idle"
        is_logging = phase == "logging"
        is_waiting = phase == "waiting_course"
        is_learning = phase == "learning"
        is_paused = phase == "paused"

        self.login_button.config(state=tk.NORMAL if (is_idle or is_paused) else tk.DISABLED)
        self.username_entry.config(state=tk.NORMAL if (is_idle or is_paused) else tk.DISABLED)
        self.password_entry.config(state=tk.NORMAL if (is_idle or is_paused) else tk.DISABLED)
        self.visible_check.config(state=tk.NORMAL if (is_idle or is_paused) else tk.DISABLED)
        self.manual_check.config(state=tk.NORMAL if (is_idle or is_paused) else tk.DISABLED)
        self.start_button.config(state=tk.NORMAL if is_waiting and bool(self.courses) else tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL if (is_logging or is_learning) else tk.DISABLED)

    def _clear_courses(self) -> None:
        for item in self.course_tree.get_children():
            self.course_tree.delete(item)
        self.courses = []

    def _append_log(self, message: str) -> None:
        if not message:
            return
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("退出", "任务仍在运行或等待选择课程，确定退出吗？"):
                return
            self._stop_task()
        self._save_login(self.username_var.get().strip(), self.password_var.get())
        if self.engine is not None:
            self.engine.close()
        self.destroy()


def main() -> None:
    app = ChaoxingGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
