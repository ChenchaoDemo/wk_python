"""学习通题目/测验页分析模块。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List

from playwright.sync_api import Page, Response

from config.config import LOG_DIR
from utils.helper import sanitize_filename
from utils.logger import get_logger

logger = get_logger()


class QuestionManager:
    """负责识别题目页、抓取题目结构和监听疑似题目接口。

    当前模块先做“判断和采集”，不自动提交答案：
    1. 页面里如果有 data-answer / correct / rightAnswer 等字段，会记录出来；
    2. 接口响应里如果出现 answer / correct / rightAnswer 等字段，会记录出来；
    3. 页面题干、选项、控件类型会导出到 logs/question_page_*.json，方便根据真实结构继续适配。
    """

    QUESTION_TITLE_PATTERN = re.compile(r"测验|测试|考试|作业|题目|答题|练习|单选|多选|判断|填空|简答")
    QUESTION_URL_PATTERN = re.compile(
        r"question|quiz|exam|work|homework|test|paper|answer|topic|exercise|selectQuestion|"
        r"getQuestion|questionBank|workQuestion|作业|考试|测验",
        re.IGNORECASE,
    )
    ANSWER_KEY_PATTERN = re.compile(
        r"answer|answers|rightanswer|right_answer|correct|correctanswer|correct_answer|"
        r"standardanswer|standard_answer|trueanswer|true_answer|daan|答案|正确|参考答案|标准答案",
        re.IGNORECASE,
    )
    CAPTURE_ENABLED_PAGE_IDS: set[int] = set()

    def __init__(self, page: Page) -> None:
        self.page = page

    @staticmethod
    def is_question_like_title(text: str) -> bool:
        """根据标题粗略判断是否可能是题目/测验/作业页面。"""

        return bool(QuestionManager.QUESTION_TITLE_PATTERN.search(text or ""))

    def enable_network_capture(self, label: str = "") -> None:
        """监听当前页面的疑似题目接口响应，并把候选答案字段写入日志。

        注意：这里不依赖 F12，也不需要打开开发者工具，因此可以绕开页面里的 debugger 暂停干扰。
        """

        page_id = id(self.page)
        if page_id in self.CAPTURE_ENABLED_PAGE_IDS:
            return

        self.CAPTURE_ENABLED_PAGE_IDS.add(page_id)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "question_network.log"

        def handle_response(response: Response) -> None:
            try:
                url = response.url or ""
                if not self.QUESTION_URL_PATTERN.search(url):
                    return

                headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                content_type = headers.get("content-type", "")
                content_length = int(headers.get("content-length") or "0")
                if content_length and content_length > 500_000:
                    return
                if content_type and not re.search(r"json|text|html|javascript|x-www-form-urlencoded", content_type, re.I):
                    return

                record: Dict[str, Any] = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "label": label,
                    "url": url,
                    "status": response.status,
                    "content_type": content_type,
                }

                body_text = ""
                try:
                    body_text = response.text()
                except Exception as exc:  # noqa: BLE001 - 个别响应无法读取不影响其它接口
                    record["read_error"] = str(exc)

                if body_text:
                    candidates = self._extract_answer_candidates_from_text(body_text)
                    record["body_length"] = len(body_text)
                    record["candidate_count"] = len(candidates)
                    record["candidates"] = candidates[:80]

                    if os.getenv("CHAOXING_QUESTION_DUMP_RAW", "false").lower() in {"1", "true", "yes", "y"}:
                        raw_path = LOG_DIR / f"question_response_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.txt"
                        raw_path.write_text(body_text[:500_000], encoding="utf-8", errors="ignore")
                        record["raw_path"] = str(raw_path)

                with log_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")

                if record.get("candidate_count"):
                    logger.info(
                        "题目接口捕获: %s 候选答案字段=%s",
                        url,
                        record.get("candidate_count"),
                    )
            except Exception as exc:  # noqa: BLE001 - response 监听不能影响主流程
                logger.debug("题目接口监听处理失败: %s", exc)

        self.page.on("response", handle_response)
        logger.info("已开启题目接口监听，日志: %s", log_path)

    def inspect_current_page(self, label: str = "") -> Dict[str, Any]:
        """分析当前页面是否为题目页，并导出题干、选项和候选答案字段。"""

        scan_script = """
            () => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const short = (value, max = 500) => {
                    value = clean(String(value || ''));
                    return value.length > max ? `${value.slice(0, max)}...` : value;
                };
                const cssEscape = (value) => {
                    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
                    return String(value || '').replace(/["\\\\]/g, '\\\\$&');
                };
                const answerNameReg = /(answer|answers|rightAnswer|right_answer|correct|correctAnswer|correct_answer|standardAnswer|standard_answer|trueAnswer|true_answer|daan|答案|正确|参考答案|标准答案)/i;
                const questionTextReg = /(单选题|多选题|判断题|填空题|简答题|第\\s*\\d+\\s*题|题目|答题|提交答案|保存答案|交卷)/;

                const answerAttrsOf = (root) => {
                    const result = [];
                    if (!root) return result;
                    const nodes = [root, ...Array.from(root.querySelectorAll('*')).slice(0, 3000)];
                    nodes.forEach((node) => {
                        if (!node.attributes) return;
                        Array.from(node.attributes).forEach((attr) => {
                            const name = attr.name || '';
                            const value = attr.value || '';
                            if (answerNameReg.test(name) || answerNameReg.test(value)) {
                                result.push({
                                    tag: node.tagName ? node.tagName.toLowerCase() : '',
                                    name,
                                    value: short(value, 300),
                                    text: short(node.innerText || node.textContent || '', 200),
                                });
                            }
                        });
                    });
                    return result;
                };

                const hiddenAnswerInputs = (root) => {
                    return Array.from((root || document).querySelectorAll('input[type="hidden"], input[style*="display:none"], input[style*="display: none"]'))
                        .filter((input) => answerNameReg.test(input.name || '') || answerNameReg.test(input.id || '') || answerNameReg.test(input.value || ''))
                        .map((input) => ({
                            tag: 'input',
                            name: input.name || '',
                            id: input.id || '',
                            value: short(input.value || '', 300),
                        }));
                };

                const scriptAnswerCandidates = () => {
                    const result = [];
                    const patterns = [
                        /(?:rightAnswer|correctAnswer|standardAnswer|trueAnswer|answer|answers|daan)\\s*[:=]\\s*["']?([^"',;\\]}\\n]{1,120})/ig,
                        /(?:正确答案|参考答案|标准答案)\\s*[:：]\\s*([^,;，。\\n]{1,120})/ig,
                    ];
                    Array.from(document.scripts).slice(0, 150).forEach((script) => {
                        const text = script.textContent || '';
                        patterns.forEach((pattern) => {
                            let match;
                            while ((match = pattern.exec(text))) {
                                result.push({ source: 'script', value: short(match[1], 200) });
                                if (result.length >= 100) break;
                            }
                        });
                    });
                    return result;
                };

                const labelTextForInput = (input) => {
                    const id = input.id || '';
                    if (id) {
                        const label = document.querySelector(`label[for="${cssEscape(id)}"]`);
                        if (label) return clean(label.innerText || label.textContent || '');
                    }
                    const label = input.closest('label');
                    if (label) return clean(label.innerText || label.textContent || '');
                    const item = input.closest('li,dd,p,div,span');
                    if (item) return clean(item.innerText || item.textContent || '');
                    return clean(input.value || input.getAttribute('aria-label') || input.title || '');
                };

                const questionContainerOf = (node) => {
                    return node.closest(
                        '.TiMu,.Zy_TItle,.Cy_TItle,.questionLi,.question-item,.question,.questionBox,.questionDiv,' +
                        '.mark_item,.qt-question,.sub-question,.exercise,.work-question,li,dd,.clearfix'
                    ) || node.parentElement || node;
                };

                const containers = new Set();
                const explicitSelectors = [
                    '.TiMu',
                    '.Zy_TItle',
                    '.Cy_TItle',
                    '.questionLi',
                    '.question-item',
                    '.question',
                    '.questionBox',
                    '.questionDiv',
                    '.mark_item',
                    '.qt-question',
                    '.sub-question',
                    '.exercise',
                    '.work-question',
                    '[data-answer]',
                    '[data-correct]',
                    '[data-right-answer]',
                    '[rightAnswer]',
                    '[correctAnswer]',
                    '[standardAnswer]',
                ];
                explicitSelectors.forEach((selector) => {
                    document.querySelectorAll(selector).forEach((node) => containers.add(node));
                });
                document.querySelectorAll('input[type="radio"],input[type="checkbox"],textarea,select,input[type="text"]').forEach((node) => {
                    containers.add(questionContainerOf(node));
                });

                const pageText = short(document.body ? (document.body.innerText || document.body.textContent || '') : '', 3000);
                const questions = [];
                Array.from(containers).slice(0, 200).forEach((container, index) => {
                    const controls = Array.from(container.querySelectorAll('input[type="radio"],input[type="checkbox"],textarea,select,input[type="text"]'));
                    const optionControls = controls.filter((control) => ['radio', 'checkbox'].includes((control.type || '').toLowerCase()));
                    const text = short(container.innerText || container.textContent || '', 1000);
                    const stemNode = container.querySelector('.stem,.question-title,.questionTitle,.qtContent,.mark_name,.Cy_TItle,.Zy_TItle,.title,h1,h2,h3,h4,p');
                    const stem = short((stemNode ? (stemNode.innerText || stemNode.textContent) : text), 500);
                    const answerCandidates = [
                        ...answerAttrsOf(container),
                        ...hiddenAnswerInputs(container),
                    ];

                    let questionType = '';
                    if (optionControls.some((control) => (control.type || '').toLowerCase() === 'checkbox')) questionType = 'multiple';
                    else if (optionControls.some((control) => (control.type || '').toLowerCase() === 'radio')) questionType = 'single';
                    else if (controls.some((control) => (control.tagName || '').toLowerCase() === 'textarea')) questionType = 'text';
                    else if (controls.some((control) => (control.type || '').toLowerCase() === 'text')) questionType = 'fill';
                    else if (/判断题|正确|错误|对|错/.test(text)) questionType = 'judge';

                    const options = optionControls.map((control, optionIndex) => ({
                        index: optionIndex + 1,
                        letter: String.fromCharCode(65 + optionIndex),
                        type: control.type || '',
                        name: control.name || '',
                        id: control.id || '',
                        value: control.value || '',
                        checked: Boolean(control.checked),
                        text: short(labelTextForInput(control), 300),
                    }));

                    const hasQuestionSignal = controls.length > 0
                        || answerCandidates.length > 0
                        || questionTextReg.test(text);
                    if (!hasQuestionSignal) return;

                    questions.push({
                        index: index + 1,
                        type: questionType || 'unknown',
                        stem,
                        text,
                        options,
                        controls_count: controls.length,
                        answer_candidates: answerCandidates.slice(0, 50),
                    });
                });

                const pageAnswerCandidates = [
                    ...answerAttrsOf(document.body).slice(0, 100),
                    ...hiddenAnswerInputs(document).slice(0, 100),
                    ...scriptAnswerCandidates().slice(0, 100),
                ];
                const formControlCount = document.querySelectorAll('input[type="radio"],input[type="checkbox"],textarea,select,input[type="text"]').length;
                const signalCount = questions.length
                    + pageAnswerCandidates.length
                    + ((questionTextReg.test(pageText) && formControlCount > 0) ? 1 : 0);

                return {
                    url: location.href,
                    title: document.title || '',
                    label: '',
                    is_question_page: signalCount > 0,
                    signal_count: signalCount,
                    page_text_sample: pageText,
                    question_count: questions.length,
                    questions,
                    page_answer_candidates: pageAnswerCandidates,
                };
            }
            """

        frame_infos: List[Dict[str, Any]] = []
        for frame in self.page.frames:
            try:
                frame_info = frame.evaluate(scan_script)
                frame_info["frame_url"] = frame.url
                frame_info["frame_name"] = frame.name
                frame_infos.append(frame_info)
            except Exception as exc:  # noqa: BLE001 - 跨域/已销毁 frame 跳过
                logger.debug("题目 frame 扫描失败: %s", exc)

        positive_frames = [frame for frame in frame_infos if frame.get("is_question_page")]
        base_info = positive_frames[0] if positive_frames else (frame_infos[0] if frame_infos else {})
        info: Dict[str, Any] = {
            "url": self.page.url,
            "title": base_info.get("title") or "",
            "label": label,
            "is_question_page": bool(positive_frames),
            "signal_count": sum(int(frame.get("signal_count") or 0) for frame in frame_infos),
            "page_text_sample": base_info.get("page_text_sample") or "",
            "question_count": 0,
            "questions": [],
            "page_answer_candidates": [],
            "frames": [
                {
                    "frame_url": frame.get("frame_url") or frame.get("url") or "",
                    "frame_name": frame.get("frame_name") or "",
                    "is_question_page": bool(frame.get("is_question_page")),
                    "question_count": int(frame.get("question_count") or 0),
                    "page_answer_candidates_count": len(frame.get("page_answer_candidates") or []),
                }
                for frame in frame_infos
            ],
        }

        for frame in positive_frames:
            frame_url = frame.get("frame_url") or frame.get("url") or ""
            frame_name = frame.get("frame_name") or ""
            for question in frame.get("questions") or []:
                question = dict(question)
                question["frame_url"] = frame_url
                question["frame_name"] = frame_name
                info["questions"].append(question)
            for candidate in frame.get("page_answer_candidates") or []:
                candidate = dict(candidate)
                candidate["frame_url"] = frame_url
                candidate["frame_name"] = frame_name
                info["page_answer_candidates"].append(candidate)

        info["question_count"] = len(info["questions"])

        if not info.get("is_question_page"):
            return info

        questions = info.get("questions") or []
        question_answer_count = sum(len(question.get("answer_candidates") or []) for question in questions)
        page_answer_count = len(info.get("page_answer_candidates") or [])
        has_choice_or_answer = (question_answer_count + page_answer_count) > 0 or any(
            question.get("type") in {"single", "multiple", "judge", "fill"}
            and (question.get("options") or question.get("controls_count"))
            for question in questions
        )
        if not self.is_question_like_title(label) and not has_choice_or_answer:
            info["is_question_page"] = False
            return info

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe_label = sanitize_filename(label or "question")[:60]
        dump_path = LOG_DIR / f"question_page_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_label}.json"
        dump_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        info["dump_path"] = str(dump_path)

        question_count = int(info.get("question_count") or 0)
        page_answer_count = len(info.get("page_answer_candidates") or [])
        logger.info(
            "检测到题目/测验页: %s questions=%s page_answer_candidates=%s dump=%s",
            label,
            question_count,
            page_answer_count,
            dump_path,
        )
        for question in (info.get("questions") or [])[:20]:
            logger.info(
                "题目结构 [%s] type=%s stem=%s options=%s answer_candidates=%s",
                question.get("index"),
                question.get("type"),
                str(question.get("stem") or "")[:120],
                len(question.get("options") or []),
                len(question.get("answer_candidates") or []),
            )
        return info

    @classmethod
    def _extract_answer_candidates_from_text(cls, text: str) -> List[Dict[str, Any]]:
        """从接口文本/JSON 里提取疑似答案字段。"""

        candidates: List[Dict[str, Any]] = []

        def shorten(value: Any, max_length: int = 500) -> str:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            value = str(value)
            value = re.sub(r"\s+", " ", value).strip()
            return value[:max_length] + ("..." if len(value) > max_length else "")

        def walk(value: Any, path: str = "$") -> None:
            if len(candidates) >= 200:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    key_text = str(key)
                    current_path = f"{path}.{key_text}"
                    if cls.ANSWER_KEY_PATTERN.search(key_text):
                        candidates.append(
                            {
                                "source": "json",
                                "path": current_path,
                                "key": key_text,
                                "value": shorten(item),
                            }
                        )
                    walk(item, current_path)
            elif isinstance(value, list):
                for index, item in enumerate(value[:500]):
                    walk(item, f"{path}[{index}]")

        try:
            parsed = json.loads(text)
            walk(parsed)
        except Exception:
            pass

        regex_patterns = [
            re.compile(
                r'(?:"|\')?(rightAnswer|correctAnswer|standardAnswer|trueAnswer|answer|answers|daan|correct)(?:"|\')?'
                r'\s*[:=]\s*(?:"([^"]{1,200})"|\'([^\']{1,200})\'|([^,;}\]\n]{1,200}))',
                re.IGNORECASE,
            ),
            re.compile(r"(正确答案|参考答案|标准答案)\s*[:：]\s*([^,;，。\n]{1,200})", re.IGNORECASE),
        ]
        for pattern in regex_patterns:
            for match in pattern.finditer(text[:500_000]):
                value = next((group for group in match.groups()[1:] if group), "")
                candidates.append(
                    {
                        "source": "regex",
                        "key": match.group(1),
                        "value": shorten(value),
                    }
                )
                if len(candidates) >= 200:
                    break
        return candidates
