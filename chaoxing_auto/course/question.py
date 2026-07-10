"""学习通题目/测验页分析模块。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import struct
import time
from datetime import datetime
from html import unescape
from typing import Any, Dict, List, Optional

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
    CXSECRET_FONT_PATTERN = re.compile(
        r"@font-face\s*\{[^}]*font-family\s*:\s*['\"]?font-cxsecret['\"]?[^}]*?"
        r"base64,([A-Za-z0-9+/=\s]+)",
        re.IGNORECASE | re.DOTALL,
    )
    CXSECRET_DECODE_CACHE: Dict[str, Dict[str, str]] = {}
    CXSECRET_REFERENCE_CACHE: Dict[str, List[Any]] = {}
    CXSECRET_REFERENCE_FONTS = (
        r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    )
    # 学习通题目页经常把真实中文映射到“font-cxsecret”私有字体。
    # 这里用于视觉匹配时做轻微偏置，避免把“常/酸/钾/体”等常用字误判成形近生僻字。
    CXSECRET_PREFERRED_CHARS = set(
        "宋体列为衡的性因原毒高呕休腹尿常见于反酸代谢低钾糖气析血者应中大吸要易患清少脏节式包方"
        "碱钠钙镁氢氨肾病诊断泻吐克抽搐减少交换抑制包括平"
    )
    CXSECRET_KNOWN_MAPS: Dict[str, Dict[str, str]] = {
        "23c03736657cd14722344e69664e43456c30c06f": {
            "仏": "后",
            "僥": "括",
            "徂": "发",
            "徃": "某",
            "徆": "禁",
            "徇": "最",
            "徉": "易",
            "後": "容",
            "徍": "体",
            "徎": "高",
            "徏": "宋",
            "徑": "血",
            "従": "低",
            "徔": "请",
            "徖": "大",
            "徚": "充",
            "徛": "能",
            "徜": "烈",
            "徠": "男",
            "復": "问",
            "徫": "性",
            "徭": "脱",
            "徯": "等",
            "徰": "水",
            "徱": "中",
            "徲": "指",
            "徳": "液",
            "徴": "过",
            "徵": "的",
            "徶": "是",
            "徸": "肿",
            "徺": "生",
            "徻": "机",
            "徾": "制",
            "徿": "响",
            "忀": "症",
            "忁": "脏",
            "忂": "影",
            "揗": "渗",
        },
        "7b0d4ff5ae8c559640cecf7ec77f5b113ce95861": {
            "宪": "碱",
            "忆": "为",
            "恳": "衡",
            "憙": "宋",
            "憛": "的",
            "憝": "列",
            "憞": "性",
            "憟": "因",
            "憠": "原",
            "憡": "毒",
            "憢": "高",
            "憣": "体",
            "憥": "呕",
            "憦": "休",
            "憧": "腹",
            "憨": "尿",
            "憩": "常",
            "憪": "见",
            "憬": "于",
            "憭": "反",
            "憯": "酸",
            "憰": "代",
            "憱": "谢",
            "憳": "低",
            "憴": "钾",
            "憵": "糖",
            "憸": "气",
            "憹": "析",
            "憺": "血",
            "憻": "者",
            "憼": "应",
            "憽": "中",
            "憾": "大",
            "憿": "吸",
            "懀": "要",
            "懁": "易",
            "懄": "患",
            "懅": "清",
            "懆": "少",
            "應": "脏",
            "懋": "节",
            "懍": "式",
            "澥": "包",
            "襖": "方",
        },
        "6b21e3669c08a7b6ef82ca6334fb4b91aed17f8a": {
            "屿": "或",
            "嶅": "静",
            "嶆": "氧",
            "嶈": "进",
            "嶉": "继",
            "嶊": "色",
            "嶋": "口",
            "嶌": "餐",
            "嶍": "缺",
            "嶎": "性",
            "嶏": "低",
            "嶐": "张",
            "嶑": "血",
            "嶒": "液",
            "嶓": "组",
            "嶔": "织",
            "嶕": "循",
            "嶖": "环",
            "嶘": "合",
            "嶙": "并",
            "嶚": "时",
            "嶛": "化",
            "嶜": "气",
            "嶝": "量",
            "嶞": "不",
            "嶟": "含",
            "嶡": "和",
            "嶢": "饱",
            "嶣": "变",
            "嶤": "差",
            "嶥": "增",
            "嶦": "动",
            "嶨": "常",
            "嶩": "正",
            "嶪": "于",
            "嶫": "分",
            "嶬": "降",
            "嶭": "脉",
            "嶮": "压",
            "嶯": "体",
            "嶰": "是",
            "嶱": "宋",
            "嶲": "念",
            "嶳": "入",
            "嶵": "减",
            "嶶": "中",
            "嶷": "的",
            "嶹": "容",
            "嶻": "障",
            "巀": "因",
            "巁": "引",
            "巂": "绀",
            "巃": "起",
            "巄": "源",
            "巆": "亚",
            "巇": "酸",
            "巈": "硝",
            "巉": "毒",
            "巊": "物",
            "巌": "氰",
            "巎": "肿",
            "巏": "失",
            "巐": "肠",
            "巑": "道",
            "巓": "菌",
            "巔": "血",
            "巕": "蛋",
            "巗": "危",
            "巘": "对",
            "巙": "白",
            "帋": "紫",
            "捳": "用",
            "玪": "足",
            "蘶": "硫",
        },
        "bde1210f4d1923693880e28ef64ccfc09cda7fdf": {
            "凄": "发",
            "厝": "列",
            "參": "过",
            "悷": "哪",
            "悹": "质",
            "悺": "热",
            "悻": "节",
            "悼": "种",
            "悾": "正",
            "悿": "酸",
            "惀": "氨",
            "惁": "压",
            "惂": "素",
            "惃": "加",
            "惄": "激",
            "惆": "制",
            "惈": "胞",
            "惉": "黑",
            "惋": "蛋",
            "惌": "白",
            "惍": "环",
            "惎": "腺",
            "惏": "苷",
            "惐": "是",
            "惑": "的",
            "惒": "宋",
            "惓": "通",
            "惕": "中",
            "惖": "外",
            "惗": "体",
            "惘": "原",
            "惙": "致",
            "惚": "生",
            "惛": "前",
            "惝": "羟",
            "惞": "胺",
            "惠": "磷",
            "惢": "点",
            "惣": "谢",
            "惤": "特",
            "惥": "期",
            "惦": "超",
            "惧": "散",
            "惪": "平",
            "惫": "衡",
            "惮": "相",
            "惴": "对",
            "惵": "与",
            "惸": "显",
            "惼": "流",
            "惽": "明",
            "惾": "下",
            "惿": "属",
            "愀": "于",
            "愂": "亢",
            "愃": "能",
            "愄": "进",
            "愅": "状",
            "愆": "功",
            "愇": "甲",
            "愊": "炎",
            "愋": "缺",
            "愌": "乏",
            "愎": "汗",
            "愐": "先",
            "愑": "已",
            "愒": "知",
            "愓": "有",
            "懲": "方",
            "揄": "暑",
            "敦": "色",
            "湣": "天",
            "瑆": "少",
            "瑝": "高",
            "蠢": "减",
            "驚": "脂",
        },
    }
    DEFAULT_QUESTION_BANK: List[Dict[str, Any]] = [
        {"keywords": ["术后禁食3天"], "answers": ["低血钾"]},
        {"keywords": ["烈日", "大量出汗", "补充了1000ml水"], "answers": ["低渗性脱水"]},
        {"keywords": ["水肿", "过多", "液体聚集"], "answers": ["体腔内", "组织间隙"]},
        {"keywords": ["水肿的发生机制"], "answers": ["体内外液体交换失衡", "血管内外液体交换失衡"]},
        {"keywords": ["高钾血症对心脏"], "answers": ["轻度高钾血症致心肌兴奋性升高", "自律性降低", "心肌收缩性降低", "传导性降低"]},
        {"keywords": ["不是代谢性酸中毒的原因"], "answers": ["呕吐"]},
        {"keywords": ["反常性酸性尿"], "answers": ["低钾性碱中毒"]},
        {"keywords": ["糖尿病患者", "pH7.3", "HCO", "16mmol"], "answers": ["AG 增大性代谢性酸中毒"]},
        {"keywords": ["碱中毒患者", "手足抽搐"], "answers": ["血清Ca"]},
        {"keywords": ["肾脏调节酸碱平衡"], "answers": ["氢-钠交换", "产氨", "主动泌氢", "钾-钠交换"]},
        {"keywords": ["朋友在外进餐", "头昏", "口唇青灰", "缺氧类型"], "answers": ["血液性缺氧"]},
        {"keywords": ["低张性缺氧时血气变化"], "answers": ["动脉血氧分压降低"]},
        {"keywords": ["缺氧概念"], "answers": ["供氧不足或用氧障碍"]},
        {"keywords": ["肠源性紫绀"], "answers": ["亚硝酸盐中毒"]},
        {"keywords": ["碳氧血红蛋白", "危害"], "answers": ["抑制红细胞糖酵解", "本身无携氧能力", "使氧解离曲线左移"]},
        {"keywords": ["发热中枢正调节介质"], "answers": ["环磷酸腺苷"]},
        {"keywords": ["发热的发生机制", "共同的中介环节"], "answers": ["内生致热原"]},
        {"keywords": ["体温上升期", "热代谢特点"], "answers": ["产热超过散热"]},
        {"keywords": ["体温升高属于发热"], "answers": ["肺炎"]},
        {"keywords": ["内生致热原"], "answers": ["IL-1", "IFN", "IL-6", "MIP-1"]},
    ]
    CAPTURE_ENABLED_PAGE_IDS: set[int] = set()

    def __init__(self, page: Page) -> None:
        self.page = page
        self.last_question_snapshot: Dict[str, Any] = {}
        self.last_question_snapshot_time = 0.0

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
                is_likely_main_question_url = bool(
                    re.search(r"/work/doHomeWork|/work/.*Work|/exam/|/question|/test|/quiz|homework", url, re.I)
                )
                max_length = 2_000_000 if is_likely_main_question_url else 500_000
                if content_length and content_length > max_length:
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

                    is_main_question_response = self._is_main_question_response(url, content_type, body_text)
                    if is_main_question_response:
                        snapshot = self._dump_question_response_snapshot(
                            label=label,
                            url=url,
                            content_type=content_type,
                            body_text=body_text,
                            candidates=candidates,
                        )
                        record["snapshot_path"] = snapshot.get("dump_path", "")
                        record["raw_path"] = snapshot.get("raw_path", "")
                        self.last_question_snapshot = snapshot
                        self.last_question_snapshot_time = time.time()
                    elif os.getenv("CHAOXING_QUESTION_DUMP_RAW", "false").lower() in {"1", "true", "yes", "y"}:
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

    def get_recent_question_snapshot(self, max_age_seconds: int = 60) -> Dict[str, Any]:
        """返回最近一次题目主接口快照。"""

        if not self.last_question_snapshot:
            return {}
        if time.time() - self.last_question_snapshot_time > max_age_seconds:
            return {}
        return dict(self.last_question_snapshot)

    @classmethod
    def _is_main_question_response(cls, url: str, content_type: str, body_text: str) -> bool:
        """判断响应是否是题目主页面/主数据，而不是普通 css/js 资源。"""

        url_text = url or ""
        if re.search(r"\.(?:css|js|png|jpg|jpeg|gif|svg|ico|woff2?|ttf)(?:\?|$)", url_text, re.I):
            return False
        if re.search(r"/work/doHomeWork|/work/.*Work|/exam/|/question|/test|/quiz|homework", url_text, re.I):
            return True
        if "html" in (content_type or "").lower() and re.search(
            r"单选题|多选题|判断题|填空题|简答题|提交答案|交卷|doHomeWork|questionId|题目",
            body_text[:100_000],
        ):
            return True
        return False

    def _dump_question_response_snapshot(
        self,
        *,
        label: str,
        url: str,
        content_type: str,
        body_text: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """把题目接口响应保存成 question_page_*.json，避免页面跳转时抓不到 DOM。"""

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_label = sanitize_filename(label or "network_question")[:60]
        suffix = ".html" if re.search(r"html|text", content_type or "", re.I) else ".txt"
        raw_path = LOG_DIR / f"question_response_{timestamp}_{safe_label}{suffix}"
        raw_path.write_text(body_text[:500_000], encoding="utf-8", errors="ignore")

        cxsecret_decode_map = self._build_cxsecret_decode_map(body_text)
        parsed_body_text = self._decode_cxsecret_html_text(body_text, cxsecret_decode_map)
        questions = self._extract_questions_from_html_text(parsed_body_text)
        page_text_sample = self._apply_cxsecret_text_fixes(self._plain_text_from_html(parsed_body_text))[:3000]
        decoded_candidates = self._decode_snapshot_value(candidates, cxsecret_decode_map)
        snapshot: Dict[str, Any] = {
            "source": "network_response",
            "label": label,
            "url": url,
            "content_type": content_type,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_question_page": True,
            "raw_path": str(raw_path),
            "body_length": len(body_text),
            "question_count": len(questions),
            "questions": questions,
            "page_answer_candidates": decoded_candidates[:120] if isinstance(decoded_candidates, list) else candidates[:120],
            "page_text_sample": page_text_sample,
            "cxsecret_decoded": bool(cxsecret_decode_map),
            "cxsecret_map_size": len(cxsecret_decode_map),
            "cxsecret_map": dict(list(cxsecret_decode_map.items())[:120]),
        }
        dump_path = LOG_DIR / f"question_page_{timestamp}_{safe_label}_network.json"
        dump_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot["dump_path"] = str(dump_path)

        logger.info(
            "题目主接口已导出: questions=%s candidates=%s dump=%s raw=%s",
            len(questions),
            len(candidates),
            dump_path,
            raw_path,
        )
        return snapshot

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
                    cxsecret_style: short(Array.from(document.querySelectorAll('style'))
                        .map((style) => style.textContent || '')
                        .find((text) => text.includes('font-cxsecret')) || '', 200000),
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

        cxsecret_source = next((str(frame.get("cxsecret_style") or "") for frame in frame_infos if frame.get("cxsecret_style")), "")
        cxsecret_decode_map = self._build_cxsecret_decode_map(cxsecret_source)
        if cxsecret_decode_map:
            info = self._decode_snapshot_value(info, cxsecret_decode_map)
            info["cxsecret_decoded"] = True
            info["cxsecret_map_size"] = len(cxsecret_decode_map)
            info["cxsecret_map"] = dict(list(cxsecret_decode_map.items())[:120])
        else:
            info["cxsecret_decoded"] = False
            info["cxsecret_map_size"] = 0

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

    def answer_and_submit_current_page(
        self,
        snapshot: Optional[Dict[str, Any]] = None,
        *,
        label: str = "",
        submit: bool = True,
    ) -> Dict[str, Any]:
        """按题库匹配当前单元测试，自动勾选答案；全部题目命中后才提交。"""

        try:
            snapshot = snapshot or {}
            questions = self._questions_from_snapshot_or_raw(snapshot)
            if not questions:
                inspected = self.inspect_current_page(label=label)
                questions = inspected.get("questions") or []

            plan_result = self._build_answer_plan(questions)
            answer_plan = plan_result.get("plan") or []
            missing = plan_result.get("missing") or []
            if missing:
                logger.warning("单元测试自动答题未提交：有 %s 道题未匹配到答案: %s", len(missing), missing[:5])
                return {
                    "answered": False,
                    "submitted": False,
                    "question_count": len(questions),
                    "planned_count": len(answer_plan),
                    "missing": missing,
                    "message": f"有 {len(missing)} 道题未匹配到答案，已跳过提交",
                }

            if not answer_plan:
                return {
                    "answered": False,
                    "submitted": False,
                    "question_count": len(questions),
                    "planned_count": 0,
                    "missing": ["没有可执行的答题计划"],
                    "message": "没有可执行的答题计划",
                }

            apply_result = self._apply_answer_plan(answer_plan)
            if not apply_result.get("ok"):
                return {
                    "answered": False,
                    "submitted": False,
                    "question_count": len(questions),
                    "planned_count": len(answer_plan),
                    "missing": apply_result.get("missing") or [],
                    "message": "答案勾选失败，已跳过提交",
                }

            submit_result: Dict[str, Any] = {"submitted": False, "message": "已勾选答案，未提交"}
            if submit:
                submit_result = self._submit_answered_work()

            return {
                "answered": True,
                "submitted": bool(submit_result.get("submitted")),
                "question_count": len(questions),
                "planned_count": len(answer_plan),
                "plan": answer_plan,
                "submit_result": submit_result,
                "message": submit_result.get("message") or "已自动答题",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("单元测试自动答题/提交失败: %s", exc)
            return {
                "answered": False,
                "submitted": False,
                "question_count": 0,
                "planned_count": 0,
                "missing": [str(exc)],
                "message": f"自动答题/提交失败: {exc}",
            }

    def _questions_from_snapshot_or_raw(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        questions = list(snapshot.get("questions") or [])
        if snapshot.get("cxsecret_decoded") or not snapshot.get("raw_path"):
            return questions

        try:
            raw_path = os.path.abspath(str(snapshot.get("raw_path") or ""))
            if raw_path and os.path.exists(raw_path):
                body_text = open(raw_path, "r", encoding="utf-8", errors="replace").read()
                decode_map = self._build_cxsecret_decode_map(body_text)
                if decode_map:
                    parsed_body_text = self._decode_cxsecret_html_text(body_text, decode_map)
                    return self._extract_questions_from_html_text(parsed_body_text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("从 raw_path 重新解码题目失败: %s", exc)
        return questions

    @classmethod
    def _load_question_bank(cls) -> List[Dict[str, Any]]:
        bank = list(cls.DEFAULT_QUESTION_BANK)
        bank_path = LOG_DIR.parent / "config" / "question_bank.json"
        if bank_path.exists():
            try:
                data = json.loads(bank_path.read_text(encoding="utf-8"))
                extra = data.get("questions") if isinstance(data, dict) else data
                if isinstance(extra, list):
                    bank.extend(item for item in extra if isinstance(item, dict))
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取题库失败 %s: %s", bank_path, exc)
        return bank

    @classmethod
    def _normalize_question_text(cls, text: str) -> str:
        text = cls._apply_cxsecret_text_fixes(str(text or ""))
        text = re.sub(r"【[^】]*题】", "", text)
        text = re.sub(r"\(\s*\d+(?:\.\d+)?\s*\)", "", text)
        text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)
        return text.lower()

    @classmethod
    def _find_bank_entry(cls, stem: str, question_id: str = "") -> Optional[Dict[str, Any]]:
        normalized_stem = cls._normalize_question_text(stem)
        for entry in cls._load_question_bank():
            entry_ids = [str(item) for item in entry.get("ids", []) or []]
            if question_id and question_id in entry_ids:
                return entry
            keywords = entry.get("keywords") or entry.get("question") or []
            if isinstance(keywords, str):
                keywords = [keywords]
            normalized_keywords = [cls._normalize_question_text(str(keyword)) for keyword in keywords if keyword]
            if normalized_keywords and all(keyword in normalized_stem for keyword in normalized_keywords):
                return entry
        return None

    @classmethod
    def _match_answer_option(cls, answer_text: str, options: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        answer_text = str(answer_text or "").strip()
        if not answer_text:
            return None
        if re.fullmatch(r"[A-H]", answer_text, re.I):
            for option in options:
                if str(option.get("letter") or "").upper() == answer_text.upper():
                    return option

        normalized_answer = cls._normalize_question_text(answer_text)
        best_option: Optional[Dict[str, Any]] = None
        best_score = -1
        for option in options:
            normalized_option = cls._normalize_question_text(str(option.get("text") or ""))
            if not normalized_option:
                continue
            score = -1
            if normalized_option == normalized_answer:
                score = 100
            elif normalized_option.startswith(normalized_answer):
                score = 80
            elif normalized_answer in normalized_option:
                score = 60
            elif normalized_option in normalized_answer:
                score = 50
            if score > best_score:
                best_score = score
                best_option = option
        return best_option if best_score >= 50 else None

    @classmethod
    def _build_answer_plan(cls, questions: List[Dict[str, Any]]) -> Dict[str, Any]:
        plan: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        for question in questions:
            options = list(question.get("options") or [])
            question_id = str(question.get("id") or "")
            stem = str(question.get("stem") or question.get("text") or "")
            if not options or not question_id:
                missing.append({"id": question_id, "stem": stem[:120], "reason": "缺少题目 id 或选项"})
                continue

            bank_entry = cls._find_bank_entry(stem, question_id=question_id)
            if not bank_entry:
                missing.append({"id": question_id, "stem": stem[:120], "reason": "题库未命中"})
                continue

            answers = bank_entry.get("answers") or bank_entry.get("answer") or []
            if isinstance(answers, str):
                answers = [answers]

            selected_options: List[Dict[str, Any]] = []
            failed_answers: List[str] = []
            for answer in answers:
                option = cls._match_answer_option(str(answer), options)
                if not option:
                    failed_answers.append(str(answer))
                    continue
                if option not in selected_options:
                    selected_options.append(option)

            if failed_answers or not selected_options:
                missing.append(
                    {
                        "id": question_id,
                        "stem": stem[:120],
                        "reason": f"答案未匹配到选项: {failed_answers}",
                    }
                )
                continue

            plan.append(
                {
                    "id": question_id,
                    "type": question.get("type") or "unknown",
                    "stem": stem[:160],
                    "answers": [
                        {
                            "letter": option.get("letter"),
                            "text": option.get("text"),
                            "value": option.get("value"),
                        }
                        for option in selected_options
                    ],
                }
            )
        return {"plan": plan, "missing": missing}

    def _apply_answer_plan(self, answer_plan: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            self.page.wait_for_selector(".TiMu", timeout=10000)
        except Exception:
            pass

        script = """
            (answerPlan) => {
                const result = { ok: true, applied: [], missing: [] };
                const dispatch = (node) => {
                    node.dispatchEvent(new Event('input', { bubbles: true }));
                    node.dispatchEvent(new Event('change', { bubbles: true }));
                };

                for (const item of answerPlan) {
                    const qid = String(item.id || '');
                    const values = (item.answers || []).map((answer) => String(answer.value || '')).filter(Boolean);
                    const root = document.querySelector(`.TiMu[data="${qid}"]`) || document;
                    if (!qid || !values.length) {
                        result.ok = false;
                        result.missing.push({ id: qid, reason: '缺少 qid 或 value' });
                        continue;
                    }

                    const checkboxInputs = Array.from(root.querySelectorAll(`input[name="answercheck${qid}"]`));
                    if (checkboxInputs.length) {
                        checkboxInputs.forEach((input) => {
                            const shouldCheck = values.includes(String(input.value || ''));
                            if (Boolean(input.checked) !== shouldCheck) {
                                input.click();
                            }
                            input.checked = shouldCheck;
                            dispatch(input);
                        });
                        const hidden = document.getElementById(`answer${qid}`);
                        if (hidden) {
                            hidden.value = values.join('');
                            dispatch(hidden);
                        }
                        if (typeof addcheck === 'function') {
                            try { addcheck(qid); } catch (error) {}
                        }
                        result.applied.push({ id: qid, values });
                        continue;
                    }

                    const radioInputs = Array.from(root.querySelectorAll(`input[type="radio"][name="answer${qid}"]`));
                    const target = radioInputs.find((input) => values.includes(String(input.value || '')));
                    if (!target) {
                        result.ok = false;
                        result.missing.push({ id: qid, values, reason: '没有找到对应 radio' });
                        continue;
                    }
                    if (!target.checked) target.click();
                    target.checked = true;
                    dispatch(target);
                    result.applied.push({ id: qid, values });
                }

                try {
                    if (typeof setMultiChoiceAnswer === 'function') setMultiChoiceAnswer();
                    if (typeof setConnLineAnswer === 'function') setConnLineAnswer();
                    if (typeof setSortQuesAnswer === 'function') setSortQuesAnswer();
                    if (typeof setCompoundQuesAnswer === 'function') setCompoundQuesAnswer();
                    if (typeof setProceduralQuesAnswer === 'function') setProceduralQuesAnswer();
                    if (typeof setBType === 'function') setBType();
                } catch (error) {}
                return result;
            }
        """
        result = self.page.evaluate(script, answer_plan)
        logger.info("单元测试答案已勾选: %s", result)
        return result

    def _submit_answered_work(self) -> Dict[str, Any]:
        """执行学习通页面自己的提交流程。"""

        try:
            self.page.evaluate(
                """
                () => {
                    if (typeof btnBlueSubmit === 'function') {
                        btnBlueSubmit();
                    } else if (typeof toadd === 'function') {
                        toadd(['']);
                    }
                }
                """
            )
            try:
                self.page.wait_for_selector("#confirmSubWin", state="visible", timeout=12000)
                self.page.evaluate(
                    """
                    () => {
                        if (typeof submitCheckTimes === 'function') {
                            submitCheckTimes();
                        } else if (typeof form1submit === 'function') {
                            form1submit();
                        }
                    }
                    """
                )
            except Exception:
                self.page.evaluate(
                    """
                    () => {
                        if (typeof form1submit === 'function') {
                            form1submit();
                        } else if (typeof confirmSubmitWork === 'function') {
                            confirmSubmitWork();
                        }
                    }
                    """
                )
            self.page.wait_for_timeout(3000)
            logger.info("单元测试已触发提交")
            return {"submitted": True, "message": "已自动答题并触发提交"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("单元测试提交失败: %s", exc)
            return {"submitted": False, "message": f"提交失败: {exc}"}

    @classmethod
    def _build_cxsecret_decode_map(cls, html_text: str) -> Dict[str, str]:
        """解析学习通 font-cxsecret 内嵌字体，生成“混淆字符 -> 真实字符”的映射。"""

        if not html_text or "font-cxsecret" not in html_text:
            return {}

        font_match = cls.CXSECRET_FONT_PATTERN.search(html_text)
        if not font_match:
            return {}

        try:
            font_bytes = base64.b64decode(re.sub(r"\s+", "", font_match.group(1)), validate=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("font-cxsecret base64 解析失败: %s", exc)
            return {}

        font_hash = hashlib.sha1(font_bytes).hexdigest()
        if font_hash in cls.CXSECRET_DECODE_CACHE:
            return dict(cls.CXSECRET_DECODE_CACHE[font_hash])

        if font_hash in cls.CXSECRET_KNOWN_MAPS:
            decode_map = dict(cls.CXSECRET_KNOWN_MAPS[font_hash])
            decode_map.update(cls._infer_cxsecret_map_from_html_context(html_text, decode_map))
            cls.CXSECRET_DECODE_CACHE[font_hash] = dict(decode_map)
            logger.info("font-cxsecret 已使用内置映射解析: chars=%s hash=%s", len(decode_map), font_hash[:8])
            return decode_map

        try:
            codepoints = cls._parse_ttf_unicode_codepoints(font_bytes)
            decode_map = cls._match_cxsecret_font_glyphs(font_bytes, codepoints)
            decode_map.update(cls._infer_cxsecret_map_from_html_context(html_text, decode_map))
            cls.CXSECRET_DECODE_CACHE[font_hash] = dict(decode_map)
            if decode_map:
                logger.info("font-cxsecret 已解析: chars=%s", len(decode_map))
            elif "font-cxsecret" in html_text:
                logger.warning("检测到 font-cxsecret，但未能生成解码映射，请确认当前 Python 环境已安装 Pillow")
            return decode_map
        except Exception as exc:  # noqa: BLE001
            logger.debug("font-cxsecret 解析失败: %s", exc)
            cls.CXSECRET_DECODE_CACHE[font_hash] = {}
            return {}

    @staticmethod
    def _parse_ttf_unicode_codepoints(font_bytes: bytes) -> List[int]:
        """从 TTF cmap 表里取出当前字体覆盖的 Unicode 编码。"""

        if len(font_bytes) < 16:
            return []

        num_tables = struct.unpack_from(">H", font_bytes, 4)[0]
        tables: Dict[str, tuple[int, int]] = {}
        offset = 12
        for _ in range(num_tables):
            if offset + 16 > len(font_bytes):
                break
            tag, _checksum, table_offset, table_length = struct.unpack_from(">4sIII", font_bytes, offset)
            tables[tag.decode("latin1", errors="ignore")] = (table_offset, table_length)
            offset += 16

        cmap_info = tables.get("cmap")
        if not cmap_info:
            return []

        cmap_offset, _cmap_length = cmap_info
        if cmap_offset + 4 > len(font_bytes):
            return []

        subtable_count = struct.unpack_from(">H", font_bytes, cmap_offset + 2)[0]
        codepoints: set[int] = set()
        for index in range(subtable_count):
            record_offset = cmap_offset + 4 + index * 8
            if record_offset + 8 > len(font_bytes):
                continue
            _platform_id, _encoding_id, subtable_rel_offset = struct.unpack_from(">HHI", font_bytes, record_offset)
            subtable_offset = cmap_offset + subtable_rel_offset
            if subtable_offset + 2 > len(font_bytes):
                continue
            fmt = struct.unpack_from(">H", font_bytes, subtable_offset)[0]
            if fmt == 4:
                if subtable_offset + 14 > len(font_bytes):
                    continue
                seg_count = struct.unpack_from(">H", font_bytes, subtable_offset + 6)[0] // 2
                pos = subtable_offset + 14
                if pos + seg_count * 2 + 2 + seg_count * 2 > len(font_bytes):
                    continue
                end_codes = list(struct.unpack_from(">" + "H" * seg_count, font_bytes, pos))
                pos += seg_count * 2 + 2
                start_codes = list(struct.unpack_from(">" + "H" * seg_count, font_bytes, pos))
                for start, end in zip(start_codes, end_codes):
                    if start == 0xFFFF and end == 0xFFFF:
                        continue
                    if 0 <= start <= end <= 0xFFFF:
                        codepoints.update(range(start, end + 1))
            elif fmt == 12:
                if subtable_offset + 16 > len(font_bytes):
                    continue
                group_count = struct.unpack_from(">I", font_bytes, subtable_offset + 12)[0]
                group_offset = subtable_offset + 16
                for group_index in range(group_count):
                    current_offset = group_offset + group_index * 12
                    if current_offset + 12 > len(font_bytes):
                        break
                    start, end, _start_glyph = struct.unpack_from(">III", font_bytes, current_offset)
                    if 0 <= start <= end <= 0x10FFFF and end - start <= 5000:
                        codepoints.update(range(start, end + 1))

        return sorted(codepoints)

    @classmethod
    def _match_cxsecret_font_glyphs(cls, font_bytes: bytes, codepoints: List[int]) -> Dict[str, str]:
        """把内嵌字体里的字形与系统中文字体做视觉匹配。"""

        if not codepoints:
            return {}

        try:
            from PIL import Image, ImageChops, ImageDraw, ImageFont  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.debug("PIL 不可用，跳过 font-cxsecret 自动还原: %s", exc)
            return {}

        reference_font_path = next((path for path in cls.CXSECRET_REFERENCE_FONTS if os.path.exists(path)), "")
        if not reference_font_path:
            logger.debug("未找到可用于 font-cxsecret 视觉匹配的系统中文字体")
            return {}

        font_size = 42
        try:
            secret_font = ImageFont.truetype(io.BytesIO(font_bytes), font_size)
            reference_font = ImageFont.truetype(reference_font_path, font_size)
        except Exception as exc:  # noqa: BLE001
            logger.debug("font-cxsecret 字体加载失败: %s", exc)
            return {}

        def render_char(char: str, font: Any) -> Any:
            image = Image.new("L", (84, 84), 255)
            draw = ImageDraw.Draw(image)
            try:
                if hasattr(draw, "textbbox"):
                    bbox = draw.textbbox((0, 0), char, font=font)
                    width = max(1, bbox[2] - bbox[0])
                    height = max(1, bbox[3] - bbox[1])
                    draw_x = (84 - width) // 2 - bbox[0]
                    draw_y = (84 - height) // 2 - bbox[1]
                else:
                    width, height = draw.textsize(char, font=font)
                    draw_x = (84 - width) // 2
                    draw_y = (84 - height) // 2
                draw.text((draw_x, draw_y), char, font=font, fill=0)
                bbox = ImageChops.invert(image).getbbox()
                if not bbox:
                    return None
                crop = image.crop(bbox)
                normalized = Image.new("L", (60, 60), 255)
                normalized.paste(crop, ((60 - crop.size[0]) // 2, (60 - crop.size[1]) // 2))
                return normalized
            except Exception:
                return None

        def glyph_score(left: Any, right: Any) -> int:
            diff = ImageChops.difference(left, right)
            return sum(value * index for index, value in enumerate(diff.histogram()))

        reference_cache_key = f"{reference_font_path}|{font_size}"
        reference_glyphs = cls.CXSECRET_REFERENCE_CACHE.get(reference_cache_key)
        if reference_glyphs is None:
            reference_glyphs = []
            # 基本 CJK 区覆盖学习通题目常见汉字；不扫扩展区可避免大量形近生僻字误判。
            for codepoint in range(0x4E00, 0xA000):
                char = chr(codepoint)
                rendered = render_char(char, reference_font)
                if rendered is not None:
                    reference_glyphs.append((char, rendered))
            cls.CXSECRET_REFERENCE_CACHE[reference_cache_key] = reference_glyphs

        decode_map: Dict[str, str] = {}
        for codepoint in codepoints:
            source_char = chr(codepoint)
            source_image = render_char(source_char, secret_font)
            if source_image is None:
                continue

            best_matches: List[tuple[int, str]] = []
            for candidate_char, candidate_image in reference_glyphs:
                best_matches.append((glyph_score(source_image, candidate_image), candidate_char))
            if not best_matches:
                continue

            best_matches.sort(key=lambda item: item[0])
            best_score, best_char = best_matches[0]
            chosen_char = best_char
            for score, candidate_char in best_matches[:20]:
                if candidate_char in cls.CXSECRET_PREFERRED_CHARS and score <= best_score * 1.35:
                    chosen_char = candidate_char
                    break
            decode_map[source_char] = chosen_char

        return decode_map

    @classmethod
    def _infer_cxsecret_map_from_html_context(cls, html_text: str, decode_map: Dict[str, str]) -> Dict[str, str]:
        """根据 HTML 里的常见上下文补充少量高置信映射。"""

        inferred: Dict[str, str] = {}
        if not html_text or not decode_map:
            return inferred

        # Word 粘贴进来的题目常见 font-family: 宋体；被加密后可能出现“宋?”。
        for family_match in re.finditer(r"font-family\s*:\s*([^;\"']{1,8})", html_text):
            family = unescape(family_match.group(1)).strip()
            if len(family) == 2 and decode_map.get(family[0]) == "宋" and family[1] in decode_map:
                inferred[family[1]] = "体"

        return inferred

    @classmethod
    def _decode_cxsecret_html_text(cls, html_text: str, decode_map: Dict[str, str]) -> str:
        """只还原 class 含 font-cxsecret 的 HTML 片段，避免误改普通正文。"""

        if not html_text or not decode_map:
            return html_text or ""

        def decode_match(match: re.Match[str]) -> str:
            return cls._decode_cxsecret_text(match.group(0), decode_map)

        decoded = re.sub(
            r"(?is)<(?P<tag>[a-z0-9]+)\b[^>]*class\s*=\s*(['\"])[^'\"]*\bfont-cxsecret\b[^'\"]*\2[^>]*>.*?</(?P=tag)>",
            decode_match,
            html_text,
        )
        # aria-label/title 等属性也可能带 font-cxsecret 文字，通常位于同一标签里；上面的片段替换已覆盖。
        return decoded

    @classmethod
    def _decode_cxsecret_text(cls, text: str, decode_map: Dict[str, str]) -> str:
        if not text or not decode_map:
            return text or ""
        decoded = "".join(decode_map.get(char, char) for char in text)
        return cls._apply_cxsecret_text_fixes(decoded)

    @staticmethod
    def _apply_cxsecret_text_fixes(text: str) -> str:
        """修正少数字形匹配常见形近误差。"""

        if not text:
            return text or ""
        text = text.replace("昰", "是").replace("対", "对")
        replacements = {
            "反営性": "反常性",
            "営性": "常性",
            "恉": "指",
            "析制": "机制",
            "彰响": "影响",
            "进餮": "进餐",
            "组炽": "组织",
            "蛋古": "蛋白",
            "苊害": "危害",
            "憎高": "增高",
            "分圧": "分压",
            "紧紺": "紫绀",
            "亚硝酸血中毒": "亚硝酸盐中毒",
            "黑色细胞制徼素": "黑色细胞刺激素",
            "脂皮质蛋古": "脂皮质蛋白",
            "热代谢秲点": "热代谢特点",
            "方热": "产热",
            "対流": "对流",
            "肺仌": "肺炎",
            "先于性汙腺": "先天性汗腺",
            "甲犾腺": "甲状腺",
            "内生致热原者": "内生致热原有",
            "䣫中毒": "酸中毒",
            "醈中毒": "酸中毒",
            "䣫性": "酸性",
            "醈性": "酸性",
            "钏性": "钾性",
            "鉀性": "钾性",
            "增犬": "增大",
            "增太": "增大",
            "增人": "增大",
            "分机": "分析",
            "宋㤓": "宋体",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    @classmethod
    def _decode_snapshot_value(cls, value: Any, decode_map: Dict[str, str]) -> Any:
        """递归还原快照中的字符串字段。"""

        if not decode_map:
            return value
        if isinstance(value, str):
            return cls._decode_cxsecret_text(value, decode_map)
        if isinstance(value, list):
            return [cls._decode_snapshot_value(item, decode_map) for item in value]
        if isinstance(value, dict):
            return {key: cls._decode_snapshot_value(item, decode_map) for key, item in value.items()}
        return value

    @staticmethod
    def _plain_text_from_html(html_text: str) -> str:
        """把 HTML 粗略转成可读文本。"""

        text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html_text or "")
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = unescape(text)
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _attrs_from_tag(tag_text: str) -> Dict[str, str]:
        """解析单个 HTML 标签里的属性。"""

        attrs: Dict[str, str] = {}
        pattern = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
        for match in pattern.finditer(tag_text or ""):
            raw_value = match.group(2).strip()
            if (raw_value.startswith('"') and raw_value.endswith('"')) or (
                raw_value.startswith("'") and raw_value.endswith("'")
            ):
                raw_value = raw_value[1:-1]
            attrs[match.group(1)] = unescape(raw_value)
        return attrs

    @classmethod
    def _extract_questions_from_html_text(cls, html_text: str) -> List[Dict[str, Any]]:
        """从题目 HTML 响应里粗略提取题干、选项和表单控件。"""

        def clean_text(value: str) -> str:
            return cls._apply_cxsecret_text_fixes(re.sub(r"\s+", " ", value or "").strip())

        timu_matches = list(re.finditer(r'(?is)<div\b[^>]*class\s*=\s*["\'][^"\']*\bTiMu\b[^"\']*["\'][^>]*>', html_text or ""))
        if timu_matches:
            questions: List[Dict[str, Any]] = []
            for index, match in enumerate(timu_matches[:80], start=1):
                block_start = match.start()
                block_end = timu_matches[index].start() if index < len(timu_matches) else len(html_text)
                block_html = html_text[block_start:block_end]
                attrs = cls._attrs_from_tag(match.group(0))
                question_id = attrs.get("data", "") or attrs.get("data-id", "") or attrs.get("questionid", "")

                title_match = re.search(
                    r'(?is)<div\b[^>]*class\s*=\s*["\'][^"\']*(?:Zy_TItle|Cy_TItle)[^"\']*["\'][^>]*>(.*?)'
                    r'(?:<ul\b|<div\b[^>]*class\s*=\s*["\'][^"\']*clearfix[^"\']*["\']|</div>\s*</div>)',
                    block_html,
                )
                font_label_match = re.search(
                    r'(?is)<div\b[^>]*class\s*=\s*["\'][^"\']*\bfontLabel\b[^"\']*["\'][^>]*>(.*?)</div>',
                    block_html,
                )
                title_html = font_label_match.group(1) if font_label_match else (title_match.group(1) if title_match else "")
                stem = clean_text(cls._plain_text_from_html(title_html))
                stem = re.sub(r"^\d+\s*", "", stem).strip()

                block_text = clean_text(cls._plain_text_from_html(block_html))
                if not stem:
                    stem = block_text[:500]

                options: List[Dict[str, Any]] = []
                li_matches = list(re.finditer(r'(?is)<li\b[^>]*class\s*=\s*["\'][^"\']*\bbefore-after\b[^"\']*["\'][^>]*>.*?</li>', block_html))
                for option_index, li_match in enumerate(li_matches[:20], start=1):
                    li_html = li_match.group(0)
                    input_match = re.search(r"(?is)<input\b[^>]*(?:type\s*=\s*['\"]?(?:radio|checkbox)['\"]?)[^>]*>", li_html)
                    input_attrs = cls._attrs_from_tag(input_match.group(0)) if input_match else {}
                    label_text = clean_text(cls._plain_text_from_html(re.search(r"(?is)<label\b[^>]*>(.*?)</label>", li_html).group(1))) if re.search(r"(?is)<label\b[^>]*>(.*?)</label>", li_html) else ""
                    letter_match = re.search(r"\b([A-H])\b", label_text)
                    option_letter = (letter_match.group(1) if letter_match else chr(64 + option_index)).upper()

                    option_html_match = re.search(r'(?is)<a\b[^>]*class\s*=\s*["\'][^"\']*\bafter\b[^"\']*["\'][^>]*>(.*?)</a>', li_html)
                    option_html = option_html_match.group(1) if option_html_match else li_html
                    option_text = clean_text(cls._plain_text_from_html(option_html))
                    if not option_text:
                        option_text = clean_text(cls._plain_text_from_html(li_html))

                    options.append(
                        {
                            "index": option_index,
                            "letter": option_letter,
                            "text": option_text[:300],
                            "value": input_attrs.get("value", ""),
                            "name": input_attrs.get("name", ""),
                        }
                    )

                if "多选" in stem or "multipleQuesId" in match.group(0):
                    question_type = "multiple"
                elif "判断" in stem:
                    question_type = "judge"
                elif "填空" in stem:
                    question_type = "fill"
                elif "简答" in stem:
                    question_type = "text"
                elif options:
                    question_type = "single"
                else:
                    question_type = "unknown"

                questions.append(
                    {
                        "index": index,
                        "id": question_id,
                        "type": question_type,
                        "stem": stem[:500],
                        "text": block_text[:1200],
                        "options": options,
                        "answer_candidates": [],
                        "source": "network_html_timu",
                    }
                )

            answer_candidates = cls._extract_html_answer_candidates(html_text)
            if answer_candidates and questions:
                questions[0]["answer_candidates"] = answer_candidates
            return questions

        plain = cls._plain_text_from_html(html_text)
        questions: List[Dict[str, Any]] = []

        marker_pattern = re.compile(r"(?:第\s*\d+\s*题|单选题|多选题|判断题|填空题|简答题)", re.I)
        markers = list(marker_pattern.finditer(plain))
        blocks: List[str] = []
        if markers:
            for index, marker in enumerate(markers[:80]):
                start = marker.start()
                end = markers[index + 1].start() if index + 1 < len(markers) else min(len(plain), start + 2500)
                blocks.append(plain[start:end].strip())
        else:
            # 没有明显“第 N 题”时，按包含 A/B/C/D 选项的段落兜底切分。
            for part in re.split(r"\n{2,}", plain):
                if re.search(r"(?:^|\s)[A-H][\.、．]\s*.{1,100}(?:\s+[B-H][\.、．]\s*)", part):
                    blocks.append(part.strip())

        option_pattern = re.compile(
            r"(?:^|\s)([A-H])[\.\、．]\s*(.{1,260}?)(?=(?:\s+[A-H][\.\、．]\s*)|$)",
            re.S,
        )
        for index, block in enumerate(blocks[:80], start=1):
            block = re.sub(r"\s+", " ", block).strip()
            if len(block) < 4:
                continue
            options = []
            for option_index, match in enumerate(option_pattern.finditer(block), start=1):
                option_text = re.sub(r"\s+", " ", match.group(2)).strip()
                if not option_text:
                    continue
                options.append(
                    {
                        "index": option_index,
                        "letter": match.group(1).upper(),
                        "text": option_text[:300],
                    }
                )

            stem = block
            if options:
                first_option = re.search(r"(?:^|\s)[A-H][\.\、．]\s*", block)
                if first_option:
                    stem = block[: first_option.start()].strip()

            if "多选" in block:
                question_type = "multiple"
            elif "判断" in block:
                question_type = "judge"
            elif "填空" in block:
                question_type = "fill"
            elif "简答" in block:
                question_type = "text"
            elif options:
                question_type = "single"
            else:
                question_type = "unknown"

            questions.append(
                {
                    "index": index,
                    "type": question_type,
                    "stem": stem[:500],
                    "text": block[:1200],
                    "options": options,
                    "answer_candidates": [],
                    "source": "network_html",
                }
            )

        # 额外记录疑似隐藏答案/标准答案字段，供后续适配。
        answer_candidates = cls._extract_html_answer_candidates(html_text)

        if answer_candidates:
            if not questions:
                questions.append(
                    {
                        "index": 1,
                        "type": "unknown",
                        "stem": "",
                        "text": plain[:1200],
                        "options": [],
                        "answer_candidates": answer_candidates,
                        "source": "network_html",
                    }
                )
            else:
                questions[0]["answer_candidates"] = answer_candidates

        return questions

    @classmethod
    def _extract_html_answer_candidates(cls, html_text: str) -> List[Dict[str, Any]]:
        """记录 HTML 中疑似隐藏答案/标准答案相关属性，供后续适配。"""

        answer_candidates: List[Dict[str, Any]] = []
        for tag_match in re.finditer(
            r"(?is)<(?:input|textarea|select|div|span|li|p)[^>]*(?:answer|correct|right|standard|答案|正确)[^>]*>",
            (html_text or "")[:500_000],
        ):
            tag_text = tag_match.group(0)
            attrs = cls._attrs_from_tag(tag_text)
            if not attrs:
                continue
            interesting = {
                key: value
                for key, value in attrs.items()
                if cls.ANSWER_KEY_PATTERN.search(key) or cls.ANSWER_KEY_PATTERN.search(value)
            }
            if interesting:
                answer_candidates.append(
                    {
                        "source": "html_attr",
                        "tag": tag_text[:80],
                        "attrs": interesting,
                    }
                )
            if len(answer_candidates) >= 80:
                break
        return answer_candidates

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

        def is_noise_value(value: str) -> bool:
            value = str(value or "").strip()
            if not value:
                return True
            lower = value.lower()
            noise_tokens = (
                "$(",
                "$.",
                ".val(",
                "getcontent",
                "ue.geteditor",
                "opteditor",
                "input:",
                "function",
                "return ",
                "var ",
                "this.",
                "window.",
                "document.",
                "answereditor",
            )
            if any(token in lower for token in noise_tokens):
                return True
            if len(value) > 300:
                return True
            return False

        seen_candidates: set[tuple[str, str, str]] = set()

        def add_candidate(candidate: Dict[str, Any]) -> None:
            value = shorten(candidate.get("value", ""))
            if is_noise_value(value):
                return
            candidate = dict(candidate)
            candidate["value"] = value
            key = (str(candidate.get("source") or ""), str(candidate.get("key") or ""), value)
            if key in seen_candidates:
                return
            seen_candidates.add(key)
            candidates.append(candidate)

        def walk(value: Any, path: str = "$") -> None:
            if len(candidates) >= 200:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    key_text = str(key)
                    current_path = f"{path}.{key_text}"
                    if cls.ANSWER_KEY_PATTERN.search(key_text):
                        add_candidate(
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
                add_candidate(
                    {
                        "source": "regex",
                        "key": match.group(1),
                        "value": value,
                    }
                )
                if len(candidates) >= 200:
                    break
        return candidates
