"""SQLite + FTS5 本地题库和 DeepSeek 查询模块。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config.config import (
    BASE_DIR,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_LOCAL_MATCH_THRESHOLD,
    DEEPSEEK_MAX_WORKERS,
    DEEPSEEK_MIN_CONFIDENCE,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT,
    QUESTION_BANK_DB,
)
from utils.logger import get_logger

logger = get_logger()


class AnswerBank:
    """本地 SQLite+FTS5 题库，未命中时并行调用 DeepSeek 并写回。"""

    def __init__(
        self,
        db_path: Path = QUESTION_BANK_DB,
        *,
        local_match_threshold: float = DEEPSEEK_LOCAL_MATCH_THRESHOLD,
        min_confidence: float = DEEPSEEK_MIN_CONFIDENCE,
    ) -> None:
        self.db_path = Path(db_path)
        self.local_match_threshold = float(local_match_threshold)
        self.min_confidence = float(min_confidence)
        self._api_key_cache: Optional[str] = None
        self._ensure_db()

    @staticmethod
    def normalize_text(text: str) -> str:
        """用于题干相似度匹配的归一化文本。"""

        text = str(text or "")
        text = re.sub(r"【[^】]*题】", "", text)
        text = re.sub(r"\(\s*\d+(?:\.\d+)?\s*\)", "", text)
        text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)
        return text.lower()

    @staticmethod
    def _clean_fts_query(text: str) -> str:
        tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]+", str(text or ""))
        # FTS5 空格是 AND；题干里常有“单选题/第N题”等噪音，使用 OR 更适合召回。
        stop_words = {"单选题", "多选题", "判断题", "填空题", "简答题", "题目", "第"}
        query_terms = []
        for token in tokens:
            token = token.strip()
            if not token or token in stop_words:
                continue
            if len(token) > 80:
                # unicode61 对中文长句可能整段成一个 token，保留长句本身意义不大。
                token = token[:80]
            query_terms.append(f'"{token.replace(chr(34), chr(34) + chr(34))}"')
        cleaned = " OR ".join(query_terms)
        return cleaned[:500]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_hash TEXT NOT NULL UNIQUE,
                    question_id TEXT,
                    question_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    question_type TEXT,
                    options_json TEXT,
                    answers_json TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    model TEXT,
                    raw_response TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_questions_qid ON questions(question_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_questions_norm ON questions(normalized_text)")
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(
                    question_text,
                    answer_text,
                    content='questions',
                    content_rowid='id',
                    tokenize='unicode61'
                )
                """
            )
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS questions_ai AFTER INSERT ON questions BEGIN
                    INSERT INTO questions_fts(rowid, question_text, answer_text)
                    VALUES (new.id, new.question_text, new.answer_text);
                END;
                CREATE TRIGGER IF NOT EXISTS questions_ad AFTER DELETE ON questions BEGIN
                    INSERT INTO questions_fts(questions_fts, rowid, question_text, answer_text)
                    VALUES('delete', old.id, old.question_text, old.answer_text);
                END;
                CREATE TRIGGER IF NOT EXISTS questions_au AFTER UPDATE ON questions BEGIN
                    INSERT INTO questions_fts(questions_fts, rowid, question_text, answer_text)
                    VALUES('delete', old.id, old.question_text, old.answer_text);
                    INSERT INTO questions_fts(rowid, question_text, answer_text)
                    VALUES (new.id, new.question_text, new.answer_text);
                END;
                """
            )
            conn.commit()

    def _question_hash(self, question: Dict[str, Any]) -> str:
        stem = str(question.get("stem") or question.get("text") or "")
        normalized = self.normalize_text(stem)
        options = [
            self.normalize_text(str(option.get("text") or ""))
            for option in (question.get("options") or [])
            if option.get("text")
        ]
        return "|".join([normalized, *options])

    def lookup(self, question: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """本地题库优先按题目 ID/哈希精确匹配，再用 FTS5 + 相似度模糊匹配。"""

        stem = str(question.get("stem") or question.get("text") or "")
        question_id = str(question.get("id") or "")
        normalized = self.normalize_text(stem)
        question_hash = self._question_hash(question)
        if not normalized:
            return None

        with self._connect() as conn:
            rows: List[sqlite3.Row] = []
            if question_id:
                rows.extend(
                    conn.execute(
                        """
                        SELECT *, 1.0 AS match_score
                        FROM questions
                        WHERE question_id = ?
                        ORDER BY updated_at DESC
                        LIMIT 5
                        """,
                        (question_id,),
                    ).fetchall()
                )
            rows.extend(
                conn.execute(
                    """
                    SELECT *, 1.0 AS match_score
                    FROM questions
                    WHERE question_hash = ? OR normalized_text = ?
                    ORDER BY updated_at DESC
                    LIMIT 5
                    """,
                    (question_hash, normalized),
                ).fetchall()
            )

            fts_query = self._clean_fts_query(stem)
            if fts_query:
                try:
                    rows.extend(
                        conn.execute(
                            """
                            SELECT q.*, bm25(questions_fts) AS rank
                            FROM questions_fts
                            JOIN questions q ON q.id = questions_fts.rowid
                            WHERE questions_fts MATCH ?
                            ORDER BY rank
                            LIMIT 10
                            """,
                            (fts_query,),
                        ).fetchall()
                    )
                except sqlite3.Error as exc:
                    logger.debug("FTS5 查询失败，降级使用 LIKE: %s", exc)

            # 中文题库常以“关键词组合”保存，未必能被 unicode61 分词召回。
            # 这里做一次基于归一化文本的包含关系扫描，作为 FTS5 的补充。
            rows.extend(
                conn.execute(
                    """
                    SELECT *, 0.0 AS match_score
                    FROM questions
                    WHERE ? LIKE '%' || normalized_text || '%'
                       OR normalized_text LIKE '%' || ? || '%'
                    ORDER BY length(normalized_text) DESC, updated_at DESC
                    LIMIT 30
                    """,
                    (normalized, normalized),
                ).fetchall()
            )

            if not rows:
                like_key = f"%{stem[:30]}%" if stem else "%"
                rows.extend(
                    conn.execute(
                        """
                        SELECT *, 0.0 AS match_score
                        FROM questions
                        WHERE question_text LIKE ?
                        ORDER BY updated_at DESC
                        LIMIT 10
                        """,
                        (like_key,),
                    ).fetchall()
                )

        best: Optional[Dict[str, Any]] = None
        best_score = -1.0
        seen_ids: set[int] = set()
        for row in rows:
            row_id = int(row["id"])
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            candidate_norm = str(row["normalized_text"] or "")
            score = SequenceMatcher(None, normalized, candidate_norm).ratio()
            if candidate_norm and (candidate_norm in normalized or normalized in candidate_norm):
                # 过短关键词容易误命中，短词只作为候选不直接通过阈值。
                if min(len(candidate_norm), len(normalized)) >= 5:
                    score = max(score, 0.92)
                else:
                    score = max(score, 0.75)
            if question_id and question_id == str(row["question_id"] or ""):
                score = max(score, 1.0)
            if question_hash == str(row["question_hash"] or ""):
                score = max(score, 1.0)
            if score > best_score:
                best_score = score
                best = dict(row)

        if not best or best_score < self.local_match_threshold:
            return None

        try:
            answers = json.loads(str(best.get("answers_json") or "[]"))
        except json.JSONDecodeError:
            answers = []
        if isinstance(answers, str):
            answers = [answers]
        if not isinstance(answers, list) or not answers:
            return None

        result = {
            "answers": [str(item) for item in answers if str(item).strip()],
            "source": best.get("source") or "sqlite",
            "confidence": float(best.get("confidence") or 0),
            "match_score": best_score,
            "question_id": best.get("question_id") or "",
            "db_id": best.get("id"),
        }
        logger.info(
            "题库命中: qid=%s db_id=%s source=%s score=%.3f answers=%s",
            question_id or "未显示",
            result["db_id"],
            result["source"],
            best_score,
            result["answers"],
        )
        return result

    def count_by_source(self, source: str) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM questions WHERE source = ?", (source,)).fetchone()
            return int(row["count"] if row else 0)

    def save_answer(
        self,
        question: Dict[str, Any],
        answers: Iterable[str],
        *,
        source: str,
        confidence: float = 1.0,
        model: str = "",
        raw_response: str = "",
    ) -> None:
        """保存或更新题目答案到 SQLite，并同步 FTS5。"""

        answers_list = [str(item).strip() for item in answers if str(item).strip()]
        if not answers_list:
            return

        stem = str(question.get("stem") or question.get("text") or "").strip()
        normalized = self.normalize_text(stem)
        if not normalized:
            return

        question_hash = self._question_hash(question)
        options = question.get("options") or []
        question_id = str(question.get("id") or "")
        answer_text = "；".join(answers_list)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO questions (
                    question_hash, question_id, question_text, normalized_text,
                    question_type, options_json, answers_json, answer_text,
                    source, confidence, model, raw_response
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(question_hash) DO UPDATE SET
                    question_id = COALESCE(NULLIF(excluded.question_id, ''), questions.question_id),
                    question_text = excluded.question_text,
                    normalized_text = excluded.normalized_text,
                    question_type = excluded.question_type,
                    options_json = excluded.options_json,
                    answers_json = excluded.answers_json,
                    answer_text = excluded.answer_text,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    model = excluded.model,
                    raw_response = excluded.raw_response,
                    updated_at = datetime('now','localtime')
                """,
                (
                    question_hash,
                    question_id,
                    stem,
                    normalized,
                    str(question.get("type") or "unknown"),
                    json.dumps(options, ensure_ascii=False),
                    json.dumps(answers_list, ensure_ascii=False),
                    answer_text,
                    source,
                    float(confidence),
                    model,
                    raw_response[:8000],
                ),
            )
            conn.commit()
        logger.info("题目答案已写入 SQLite: qid=%s source=%s answers=%s", question_id or "未显示", source, answers_list)

    def import_entries(self, entries: Iterable[Dict[str, Any]], *, source: str = "builtin") -> None:
        """把旧 JSON/内置题库导入 SQLite，方便后续 FTS5 命中。"""

        for entry in entries:
            keywords = entry.get("keywords") or entry.get("question") or []
            if isinstance(keywords, str):
                keywords = [keywords]
            answers = entry.get("answers") or entry.get("answer") or []
            if isinstance(answers, str):
                answers = [answers]
            if not keywords or not answers:
                continue
            question = {
                "id": str((entry.get("ids") or [""])[0]) if isinstance(entry.get("ids"), list) else "",
                "stem": " ".join(str(item) for item in keywords if item),
                "type": entry.get("type") or "unknown",
                "options": [],
            }
            self.save_answer(question, answers, source=source, confidence=1.0)

    def answer_questions(self, questions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """批量获取答案：本地命中直接返回，未命中并行调用 DeepSeek。"""

        results: Dict[str, Dict[str, Any]] = {}
        missing: List[Dict[str, Any]] = []

        for question in questions:
            key = str(question.get("id") or question.get("index") or len(results))
            local = self.lookup(question)
            if local:
                results[key] = local
            else:
                missing.append(question)

        if missing:
            logger.info("本地题库未命中 %s 道题，开始并行调用 DeepSeek", len(missing))
            deepseek_results = self.query_deepseek_parallel(missing)
            for key, answer in deepseek_results.items():
                if answer and answer.get("answers"):
                    results[str(key)] = answer

        return results

    def _load_api_key(self) -> str:
        if self._api_key_cache is not None:
            return self._api_key_cache

        api_key = DEEPSEEK_API_KEY
        local_path = BASE_DIR / "config" / "deepseek.local.json"
        if not api_key and local_path.exists():
            try:
                data = json.loads(local_path.read_text(encoding="utf-8-sig"))
                api_key = str(data.get("api_key") or data.get("key") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 DeepSeek 本地配置失败 %s: %s", local_path, exc)

        self._api_key_cache = api_key
        return api_key

    def query_deepseek_parallel(self, questions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """并行查询 DeepSeek；返回以 question 对象为 key 的结果。"""

        api_key = self._load_api_key()
        if not api_key:
            logger.warning("DeepSeek API Key 未配置，无法联网查询未命中题目")
            return {}

        max_workers = max(1, min(int(DEEPSEEK_MAX_WORKERS or 4), len(questions), 8))
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._query_deepseek_one, question, api_key): (index, question)
                for index, question in enumerate(questions)
            }
            for future in as_completed(future_map):
                index, question = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DeepSeek 查询失败 qid=%s: %s", question.get("id") or "", exc)
                    continue
                if not result or not result.get("answers"):
                    continue
                key = str(question.get("id") or question.get("index") or index)
                results[key] = result
                self.save_answer(
                    question,
                    result.get("answers") or [],
                    source="deepseek",
                    confidence=float(result.get("confidence") or 0),
                    model=DEEPSEEK_MODEL,
                    raw_response=result.get("raw_response") or "",
                )

        return results

    def _query_deepseek_one(self, question: Dict[str, Any], api_key: str) -> Optional[Dict[str, Any]]:
        options = question.get("options") or []
        option_lines = []
        for option in options:
            letter = str(option.get("letter") or "").strip()
            value = str(option.get("value") or "").strip()
            text = str(option.get("text") or "").strip()
            option_lines.append(f"{letter}. {text} [value={value}]")

        prompt = (
            "你是课程选择题答题助手。请只根据题干和选项判断答案。"
            "必须返回 JSON，不要输出 Markdown。\n"
            "JSON 格式：{\"answers\":[\"A\"],\"answer_texts\":[\"选项原文\"],\"confidence\":0.0到1.0,\"reason\":\"一句话依据\"}\n"
            "多选题 answers 返回多个字母；单选题只返回一个字母。无法判断时 confidence 低于 0.35。\n\n"
            f"题型：{question.get('type') or 'unknown'}\n"
            f"题干：{question.get('stem') or question.get('text') or ''}\n"
            "选项：\n"
            + "\n".join(option_lines)
        )

        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": "你只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            DEEPSEEK_BASE_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        last_error = ""
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=DEEPSEEK_TIMEOUT) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                content = (
                    parsed.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                answer = self._parse_deepseek_content(content, options)
                if not answer:
                    return None
                answer["raw_response"] = raw
                if float(answer.get("confidence") or 0) < self.min_confidence:
                    logger.warning(
                        "DeepSeek 置信度过低，跳过写入: qid=%s confidence=%s",
                        question.get("id") or "",
                        answer.get("confidence"),
                    )
                    return None
                logger.info(
                    "DeepSeek 命中: qid=%s answers=%s confidence=%s",
                    question.get("id") or "",
                    answer.get("answers"),
                    answer.get("confidence"),
                )
                return answer
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:500]}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if attempt == 0:
                time.sleep(1)

        logger.warning("DeepSeek 请求最终失败 qid=%s: %s", question.get("id") or "", last_error)
        return None

    def _parse_deepseek_content(self, content: str, options: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        text = str(content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
            text = re.sub(r"```$", "", text).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return None
            data = json.loads(match.group(0))

        raw_answers = data.get("answers") or data.get("answer") or []
        if isinstance(raw_answers, str):
            raw_answers = re.split(r"[,，、\s]+", raw_answers.strip())

        answer_texts = data.get("answer_texts") or data.get("answerTexts") or []
        if isinstance(answer_texts, str):
            answer_texts = [answer_texts]

        valid_letters = {str(option.get("letter") or "").upper(): option for option in options}
        resolved: List[str] = []
        for item in raw_answers:
            letter = str(item or "").strip().upper()
            if letter in valid_letters and letter not in resolved:
                resolved.append(letter)

        if not resolved:
            for answer_text in answer_texts:
                matched = self._match_text_to_letter(str(answer_text), options)
                if matched and matched not in resolved:
                    resolved.append(matched)

        if not resolved:
            return None

        confidence = data.get("confidence", 0.7)
        try:
            confidence_value = max(0.0, min(1.0, float(confidence)))
        except Exception:
            confidence_value = 0.7

        answers_as_text = []
        for letter in resolved:
            option = valid_letters.get(letter)
            answers_as_text.append(str(option.get("text") or letter) if option else letter)

        return {
            "answers": answers_as_text,
            "answer_letters": resolved,
            "confidence": confidence_value,
            "reason": str(data.get("reason") or "")[:500],
            "source": "deepseek",
        }

    def _match_text_to_letter(self, answer_text: str, options: List[Dict[str, Any]]) -> str:
        normalized_answer = self.normalize_text(answer_text)
        if not normalized_answer:
            return ""
        best_letter = ""
        best_score = -1.0
        for option in options:
            normalized_option = self.normalize_text(str(option.get("text") or ""))
            if not normalized_option:
                continue
            score = SequenceMatcher(None, normalized_answer, normalized_option).ratio()
            if normalized_answer in normalized_option or normalized_option in normalized_answer:
                score = max(score, 0.9)
            if score > best_score:
                best_score = score
                best_letter = str(option.get("letter") or "").upper()
        return best_letter if best_score >= 0.55 else ""
