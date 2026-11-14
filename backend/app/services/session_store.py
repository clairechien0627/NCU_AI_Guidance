"""
session_store.py

以 PostgreSQL 持久化對話歷史。
需要在環境變數設定 DATABASE_URL。
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

import psycopg
from psycopg.rows import dict_row


MAX_HISTORY = 20  # 保留最近幾則 role/content 訊息給 LLM 使用
_SCHEMA_READY = False
_TW = timezone(timedelta(hours=8))
_MONITOR_CACHE: dict[str, dict] = {}
_MONITOR_CACHE_AT: dict[str, datetime] = {}
_MONITOR_CACHE_TTL_BY_RANGE: dict[str, int] = {"1d": 60, "7d": 300, "30d": 600}
_DB_STATS_CACHE: dict | None = None
_DB_STATS_CACHE_AT: datetime | None = None
_DB_STATS_TTL = 600
_ROOT = Path(__file__).parent.parent.parent.parent


@lru_cache(maxsize=1)
def _load_topic_tags() -> dict:
    p = _ROOT / "data" / "processed" / "nlp" / "nlp_topic_tags.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@lru_cache(maxsize=1)
def _load_course_domains() -> dict[str, list[str]]:
    """course_code → list of 課程領域 strings（已切開）。"""
    p = _ROOT / "data" / "processed" / "course_index.json"
    if not p.exists():
        return {}
    idx = json.loads(p.read_text(encoding="utf-8"))
    return {code: entry.get("course_domains", []) for code, entry in idx.items()}


# 中心、處室 系所 → 通識大分類
GE_CATEGORY_MAP: dict[str, str] = {
    "語言中心":                             "外語",
    "體育室":                               "體育",
    "軍訓室":                               "體育",
    "學務處-服務學習發展中心":              "服務學習",
    "學務處-職涯發展中心":                  "服務學習",
    "通識教育中心":                         "通識",
    "核心通識課程":                         "通識",
    "總教學中心":                           "通識",
    "臺灣大專院校人工智慧學程聯盟":         "通識",
    "環境科技博士學位學程(台灣聯合大學系統)": "通識",
    "遙測科技碩士學位學程":                 "通識",
}

TOOL_LABELS: dict[str, str] = {
    "search_courses":              "課程語意搜尋",
    "get_dept_courses":            "系所課程查詢",
    "get_dept_overview":           "系所概覽",
    "get_program_info":            "學分學程資訊",
    "get_program_courses":         "學分學程課程",
    "get_teacher_info":            "教師資訊查詢",
    "search_teachers":             "教師語意搜尋",
    "get_graduation_requirements": "畢業規定查詢",
    "get_graduation_rules":        "畢業規定查詢",
    "get_requirements_notes":      "修課規定查詢",
    "get_dept_info":               "系所介紹查詢",
    "get_course_detail":           "課程詳情查詢",
    "search_programs":             "學分學程搜尋",
    "get_program_description":     "學程說明查詢",
    "find_similar_courses":        "相似課程推薦",
    "get_course_knowledge_map":    "課程知識地圖",
    "get_depts_by_tech":           "技術科系分佈",
    "ppr_explore":                 "知識圖譜探索",
    "explore_concept_neighborhood": "概念鄰域探索",
}


def new_session_id() -> str:
    return uuid.uuid4().hex


def load(session_id: str, user_id: str | None = None) -> list[dict]:
    """載入對話歷史，只回傳 role/content 清單（供 LLM 使用）。"""
    if user_id is None:
        return []

    _ensure_schema()
    turn_limit = max(1, MAX_HISTORY // 2)

    with _connect() as conn:
        session = _get_accessible_session(conn, session_id, user_id)
        if session is None:
            return []

        rows = conn.execute(
            """
            select user_msg, assistant_msg
            from chat_turns
            where session_id = %s
            order by id desc
            limit %s
            """,
            (session_id, turn_limit),
        ).fetchall()

    messages: list[dict] = []
    for row in reversed(rows):
        messages.append({"role": "user", "content": row["user_msg"]})
        messages.append({"role": "assistant", "content": row["assistant_msg"]})
    return messages[-MAX_HISTORY:]


def save(
    session_id: str,
    user_msg: str,
    assistant_msg: str,
    course_cards: list | None = None,
    tools_used: list | None = None,
    course_pool: list | None = None,
    debug_trace: dict | None = None,
    user_id: str | None = None,
    user_profile: dict | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_latency_ms: int = 0,
    model_name: str = "",
) -> None:
    """追加一輪對話到 PostgreSQL。"""
    if user_id is None:
        return

    _ensure_schema()
    now = _now_datetime()
    title = user_msg[:40] if user_msg else ""

    with _connect() as conn:
        if user_id:
            _upsert_user(conn, user_id, user_profile or {}, now)

        existing = conn.execute(
            "select user_id from chat_sessions where session_id = %s",
            (session_id,),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                insert into chat_sessions (session_id, user_id, title, created_at, updated_at)
                values (%s, %s, %s, %s, %s)
                """,
                (session_id, user_id, title, now, now),
            )
        elif not _can_access(existing["user_id"], user_id):
            return
        else:
            conn.execute(
                """
                update chat_sessions
                set
                    user_id = coalesce(user_id, %s),
                    title = case when title = '' then %s else title end,
                    updated_at = %s
                where session_id = %s
                """,
                (user_id, title, now, session_id),
            )

        conn.execute(
            """
            insert into chat_turns (
                session_id,
                user_msg,
                assistant_msg,
                course_cards,
                course_pool,
                tools_used,
                debug_trace,
                input_tokens,
                output_tokens,
                llm_latency_ms,
                model_name,
                created_at
            )
            values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                user_msg,
                assistant_msg,
                _json(course_cards or []),
                _json(course_pool or []),
                _json(tools_used or []),
                _json(debug_trace or {}),
                input_tokens,
                output_tokens,
                llm_latency_ms,
                model_name or None,
                now,
            ),
        )
        conn.commit()


def delete(session_id: str, user_id: str | None = None) -> bool:
    """刪除 session，回傳是否成功。"""
    if user_id is None:
        return False

    _ensure_schema()
    with _connect() as conn:
        session = _get_accessible_session(conn, session_id, user_id)
        if session is None:
            return False

        result = conn.execute(
            "delete from chat_sessions where session_id = %s",
            (session_id,),
        )
        conn.commit()
        return (result.rowcount or 0) > 0


def list_sessions(limit: int = 50, user_id: str | None = None) -> list[dict]:
    """列出 session 摘要，按更新時間降序排列。"""
    if user_id is None:
        return []

    _ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            """
            select
                s.session_id,
                coalesce(nullif(s.title, ''), '未命名對話') as title,
                s.updated_at,
                count(t.id)::int as turn_count
            from chat_sessions s
            left join chat_turns t on t.session_id = s.session_id
            where s.user_id = %s
            group by s.session_id, s.title, s.updated_at
            order by s.updated_at desc
            limit %s
            """,
            (user_id, limit),
        ).fetchall()

    return [
        {
            "session_id": row["session_id"],
            "title": row["title"],
            "updated_at": _format_dt(row["updated_at"]),
            "turn_count": row["turn_count"],
        }
        for row in rows
    ]


def get_display(session_id: str, user_id: str | None = None) -> dict | None:
    """取得完整對話資料（含課程卡片與工具紀錄）供前端顯示。"""
    if user_id is None:
        return None

    _ensure_schema()
    with _connect() as conn:
        session = _get_accessible_session(conn, session_id, user_id)
        if session is None:
            return None

        rows = conn.execute(
            """
            select
                user_msg,
                assistant_msg,
                course_cards,
                course_pool,
                tools_used,
                debug_trace,
                created_at
            from chat_turns
            where session_id = %s
            order by id asc
            """,
            (session_id,),
        ).fetchall()

    return {
        "session_id": session_id,
        "title": session["title"] or "未命名對話",
        "updated_at": _format_dt(session["updated_at"]),
        "turns": [
            {
                "user": row["user_msg"],
                "assistant": row["assistant_msg"],
                "course_cards": _from_jsonb(row["course_cards"], []),
                "course_pool": _from_jsonb(row["course_pool"], []),
                "tools_used": _from_jsonb(row["tools_used"], []),
                "debug_trace": _from_jsonb(row["debug_trace"], {}),
                "created_at": _format_dt(row["created_at"]),
            }
            for row in rows
        ],
    }


def _connect() -> psycopg.Connection:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for PostgreSQL session storage")
    return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10)


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    with _connect() as conn:
        conn.execute(
            """
            create table if not exists app_users (
                user_id text primary key,
                email text not null default '',
                name text not null default '',
                picture text not null default '',
                provider text not null default 'firebase',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now(),
                last_seen_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists chat_sessions (
                session_id text primary key,
                user_id text,
                title text not null default '',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists chat_turns (
                id bigserial primary key,
                session_id text not null references chat_sessions(session_id) on delete cascade,
                user_msg text not null,
                assistant_msg text not null,
                course_cards jsonb not null default '[]'::jsonb,
                course_pool jsonb not null default '[]'::jsonb,
                tools_used jsonb not null default '[]'::jsonb,
                debug_trace jsonb not null default '{}'::jsonb,
                input_tokens int not null default 0,
                output_tokens int not null default 0,
                llm_latency_ms int not null default 0,
                created_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            "alter table chat_turns add column if not exists input_tokens int not null default 0"
        )
        conn.execute(
            "alter table chat_turns add column if not exists llm_latency_ms int not null default 0"
        )
        conn.execute(
            "alter table chat_turns add column if not exists output_tokens int not null default 0"
        )
        conn.execute(
            "alter table chat_turns add column if not exists model_name text"
        )
        conn.execute(
            "create index if not exists idx_chat_sessions_user_updated on chat_sessions(user_id, updated_at desc)"
        )
        conn.execute(
            "create index if not exists idx_app_users_email on app_users(email)"
        )
        conn.execute(
            "create index if not exists idx_chat_turns_session_id on chat_turns(session_id, id)"
        )
        conn.execute(
            """
            create table if not exists user_analytics (
                user_id     text primary key,
                data        jsonb not null default '{}'::jsonb,
                computed_at timestamptz not null default to_timestamp(0)
            )
            """
        )
        conn.execute(
            """
            create table if not exists user_pdf_analytics (
                user_id     text primary key,
                data        jsonb not null default '{}'::jsonb,
                computed_at timestamptz not null default to_timestamp(0)
            )
            """
        )
        conn.commit()

    _SCHEMA_READY = True


_ANALYTICS_TTL_SECONDS = 900  # 15 分鐘


def _analytics_cache_get(conn, user_id: str) -> dict | None:
    """若快取存在且未過期則回傳，否則回傳 None。"""
    row = conn.execute(
        """
        SELECT data, computed_at
        FROM user_analytics
        WHERE user_id = %s
          AND computed_at > now() - make_interval(secs => %s)
        """,
        (user_id, _ANALYTICS_TTL_SECONDS),
    ).fetchone()
    return _from_jsonb(row["data"], None) if row else None


def _analytics_cache_set(conn, user_id: str, data: dict) -> None:
    """寫入（upsert）快取。"""
    conn.execute(
        """
        INSERT INTO user_analytics (user_id, data, computed_at)
        VALUES (%s, %s::jsonb, now())
        ON CONFLICT (user_id) DO UPDATE
          SET data = EXCLUDED.data,
              computed_at = EXCLUDED.computed_at
        """,
        (user_id, _json(data)),
    )
    conn.commit()


def _pdf_analytics_cache_get(conn, user_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT data, computed_at
        FROM user_pdf_analytics
        WHERE user_id = %s
          AND computed_at > now() - make_interval(secs => %s)
        """,
        (user_id, _ANALYTICS_TTL_SECONDS),
    ).fetchone()
    return _from_jsonb(row["data"], None) if row else None


def _pdf_analytics_cache_set(conn, user_id: str, data: dict) -> None:
    conn.execute(
        """
        INSERT INTO user_pdf_analytics (user_id, data, computed_at)
        VALUES (%s, %s::jsonb, now())
        ON CONFLICT (user_id) DO UPDATE
          SET data = EXCLUDED.data,
              computed_at = EXCLUDED.computed_at
        """,
        (user_id, _json(data)),
    )
    conn.commit()


def _get_accessible_session(
    conn: psycopg.Connection,
    session_id: str,
    user_id: str | None,
) -> dict | None:
    session = conn.execute(
        "select session_id, user_id, title, updated_at from chat_sessions where session_id = %s",
        (session_id,),
    ).fetchone()
    if session is None:
        return None
    if not _can_access(session["user_id"], user_id):
        return None
    return session


def _upsert_user(
    conn: psycopg.Connection,
    user_id: str,
    profile: dict,
    now: datetime,
) -> None:
    conn.execute(
        """
        insert into app_users (
            user_id,
            email,
            name,
            picture,
            provider,
            created_at,
            updated_at,
            last_seen_at
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (user_id) do update
        set
            email = excluded.email,
            name = excluded.name,
            picture = excluded.picture,
            updated_at = excluded.updated_at,
            last_seen_at = excluded.last_seen_at
        """,
        (
            user_id,
            profile.get("email", ""),
            profile.get("name", ""),
            profile.get("picture", ""),
            profile.get("provider", "firebase"),
            now,
            now,
            now,
        ),
    )


def _can_access(owner: str | None, user_id: str | None) -> bool:
    return user_id is not None and owner == user_id


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _from_jsonb(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _now_datetime() -> datetime:
    return datetime.now(_TW)


def _format_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(_TW).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    if value:
        return str(value)
    return ""


_WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def get_analytics(
    user_id: str,
    college_map: dict[str, str] | None = None,
    enrich: Callable[[dict], None] | None = None,
) -> dict:
    """從用戶歷史對話萃取學習傾向統計（15 分鐘快取）。

    enrich：寫入快取前對結果做的補充（例如用 Qdrant 補 domain_tags），
    讓快取涵蓋整份回應，快取命中時不必再重做。
    """
    if not user_id:
        return _empty_analytics()

    _ensure_schema()
    college_map = college_map or {}

    with _connect() as conn:
        cached = _analytics_cache_get(conn, user_id)
        if cached is not None:
            return cached

        session_row = conn.execute(
            "SELECT count(*)::int AS n FROM chat_sessions WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        total_sessions: int = session_row["n"] if session_row else 0

        rows = conn.execute(
            """
            SELECT t.tools_used, t.course_pool, t.course_cards, t.created_at
            FROM chat_turns t
            JOIN chat_sessions s ON s.session_id = t.session_id
            WHERE s.user_id = %s
            ORDER BY t.id ASC
            """,
            (user_id,),
        ).fetchall()

    total_turns = len(rows)
    seen_courses: set[str]            = set()
    dept_counter: dict[str, int]      = {}
    tool_counter: dict[str, int]      = {}
    card_name_counter: dict[str, int] = {}   # top_courses：來自 course_cards
    domain_tag_counter: dict[str, int] = {}  # 興趣標籤：course_cards.domain_tags
    ge_category_counter: dict[str, int] = {}  # 通識分類計數
    ge_codes: set[str] = set()               # 通識課程代碼，用於查 topic_tags
    course_domain_counter: dict[str, int] = {}  # 課程領域計數

    for row in rows:
        # 工具計數
        for tool in _from_jsonb(row["tools_used"], []):
            if isinstance(tool, str):
                tool_counter[tool] = tool_counter.get(tool, 0) + 1

        # course_pool：系所分佈 + 唯一課程數 + 通識分類
        for course in _from_jsonb(row["course_pool"], []):
            if not isinstance(course, dict):
                continue
            uid = course.get("code") or course.get("name", "")
            if uid:
                seen_courses.add(uid)
            dept = course.get("dept", "").strip()
            if dept:
                dept_counter[dept] = dept_counter.get(dept, 0) + 1
                ge_cat = GE_CATEGORY_MAP.get(dept)
                if ge_cat:
                    ge_category_counter[ge_cat] = ge_category_counter.get(ge_cat, 0) + 1
                    code = course.get("code", "").strip()
                    if code:
                        ge_codes.add(code)

            # 課程領域（從 course_index 查）
            code = course.get("code", "").strip()
            if code:
                for domain in _load_course_domains().get(code, []):
                    course_domain_counter[domain] = course_domain_counter.get(domain, 0) + 1

        # course_cards：AI 推薦課程名稱 + domain_tags 興趣標籤
        for card in _from_jsonb(row["course_cards"], []):
            if not isinstance(card, dict):
                continue
            name = card.get("name", "").strip()
            if name:
                card_name_counter[name] = card_name_counter.get(name, 0) + 1
            # domain_tags 格式："人工智慧::0.85||機器學習::0.72" 或純 "人工智慧||機器學習"
            for part in (card.get("domain_tags") or "").split("||"):
                part = part.strip()
                if not part:
                    continue
                if "::" in part:
                    tag, score_str = part.rsplit("::", 1)
                    tag = tag.strip()
                    try:
                        if tag and float(score_str) >= 0.5:
                            domain_tag_counter[tag] = domain_tag_counter.get(tag, 0) + 1
                    except ValueError:
                        if tag:  # 分數解析失敗，仍計入
                            domain_tag_counter[tag] = domain_tag_counter.get(tag, 0) + 1
                else:
                    domain_tag_counter[part] = domain_tag_counter.get(part, 0) + 1

    # ── 系所 Top 8（只保留 college_map 中有對應的真實系所）────────
    # course_pool 的 dept 欄位有時存學院名（如「工學院」院級課程），需過濾
    valid_depts = {d: c for d, c in dept_counter.items() if d in college_map}
    total_dept = sum(valid_depts.values()) or 1
    dept_distribution = [
        {"name": d, "count": c, "pct": round(c / total_dept * 100, 1)}
        for d, c in sorted(valid_depts.items(), key=lambda x: -x[1])[:8]
    ]

    # ── 學院 Top 5（從 dept 換算）────────────────────────────────
    college_counter: dict[str, int] = {}
    for dept, cnt in dept_counter.items():
        college = college_map.get(dept, "其他")
        college_counter[college] = college_counter.get(college, 0) + cnt
    total_col = sum(college_counter.values()) or 1
    college_distribution = [
        {"name": c, "count": n, "pct": round(n / total_col * 100, 1)}
        for c, n in sorted(college_counter.items(), key=lambda x: -x[1])[:5]
    ]
    fav_college = college_distribution[0]["name"] if college_distribution else ""

    # ── 工具使用頻率 ─────────────────────────────────────────────
    tool_usage = [
        {"tool": t, "label": TOOL_LABELS.get(t, t), "count": c}
        for t, c in sorted(tool_counter.items(), key=lambda x: -x[1])
    ]

    # ── AI 推薦課程 Top 10（來自 course_cards）────────────────────
    top_courses = [
        {"name": n, "count": c}
        for n, c in sorted(card_name_counter.items(), key=lambda x: -x[1])[:10]
    ]

    # ── 興趣標籤 Top 15（解析 course_cards.domain_tags）──────────
    top_domain_tags = [
        {"tag": t, "count": c}
        for t, c in sorted(domain_tag_counter.items(), key=lambda x: -x[1])[:15]
    ]

    # ── 通識 / 語言 / 體育 / 服務學習 分析 ───────────────────────
    ge_topic_counter: dict[str, int] = {}
    if ge_codes:
        topic_tags_db = _load_topic_tags()
        for code in ge_codes:
            for tag in topic_tags_db.get(code, {}).get("topic_tags", []):
                tag = str(tag).strip()
                if tag:
                    ge_topic_counter[tag] = ge_topic_counter.get(tag, 0) + 1

    GE_ORDER = ["通識", "外語", "體育", "服務學習"]
    ge_total = sum(ge_category_counter.values())
    general_edu = {
        "total": ge_total,
        "categories": [
            {"name": cat, "count": ge_category_counter.get(cat, 0)}
            for cat in GE_ORDER
            if ge_category_counter.get(cat, 0) > 0
        ],
        "top_topic_tags": [
            {"tag": t, "count": c}
            for t, c in sorted(ge_topic_counter.items(), key=lambda x: -x[1])[:20]
        ],
    }

    # ── 課程領域 Top 20 ──────────────────────────────────────────
    top_course_domains = [
        {"domain": d, "count": c}
        for d, c in sorted(course_domain_counter.items(), key=lambda x: -x[1])[:20]
    ]

    result = {
        "overview": {
            "total_sessions":         total_sessions,
            "total_turns":            total_turns,
            "total_courses_explored": len(seen_courses),
            "fav_college":            fav_college,
        },
        "dept_distribution":    dept_distribution,
        "college_distribution": college_distribution,
        "tool_usage":           tool_usage,
        "top_courses":          top_courses,
        "top_domain_tags":      top_domain_tags,
        "general_edu":          general_edu,
        "top_course_domains":   top_course_domains,
    }
    if enrich is not None:
        enrich(result)
    with _connect() as conn:
        _analytics_cache_set(conn, user_id, result)
    return result


def _empty_pdf_analytics() -> dict:
    return {
        "overview": {
            "total_conversations": 0,
            "total_questions": 0,
            "total_documents_explored": 0,
            "avg_depth": 0.0,
        },
        "dept_distribution": [],
        "college_distribution": [],
        "depth_distribution": [
            {"range": "1–2 輪", "count": 0},
            {"range": "3–5 輪", "count": 0},
            {"range": "6+ 輪",  "count": 0},
        ],
        "exploration_type": {"type": "", "desc": ""},
        "document_list": [],
        "recent_questions": [],
    }


def get_pdf_analytics(user_id: str, doc_meta: dict | None = None) -> dict:
    """從使用者的研究計畫 PDF chat 歷史萃取探索傾向統計（15 分鐘快取）。

    doc_meta: {document_id: {title, department, year}}，由呼叫端從 projects.json 建立並傳入。
    """
    if not user_id:
        return _empty_pdf_analytics()

    _ensure_schema()
    doc_meta = doc_meta or {}

    with _connect() as conn:
        cached = _pdf_analytics_cache_get(conn, user_id)
        if cached is not None:
            return cached

    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation, PdfAgentMessage
    from app.services.retriever import _load_college_map

    college_map = _load_college_map()

    from sqlalchemy import text as _sa_text

    with PdfSessionLocal() as db:
        convs = (
            db.query(PdfConversation)
            .filter(
                PdfConversation.user_id == user_id,
                PdfConversation.document_id.isnot(None),
            )
            .order_by(PdfConversation.created_at.desc())
            .all()
        )

        if not convs:
            result = _empty_pdf_analytics()
            with _connect() as conn:
                _pdf_analytics_cache_set(conn, user_id, result)
            return result

        # 每個 thread 的訊息數（SQL 聚合，不撈大欄位）
        _tc_rows = db.execute(
            _sa_text(
                "SELECT thread_id, COUNT(*) AS q_count"
                " FROM pdf_agent_messages WHERE user_id = :uid"
                " GROUP BY thread_id"
            ),
            {"uid": user_id},
        ).all()
        thread_msg_counts: dict[str, int] = {r.thread_id: r.q_count for r in _tc_rows}
        total_questions = sum(thread_msg_counts.values())

        # 最近 10 筆提問（只取三個輕量欄位）
        recent_msg_rows = db.execute(
            _sa_text(
                "SELECT thread_id, user_question, created_at"
                " FROM pdf_agent_messages WHERE user_id = :uid"
                " ORDER BY created_at DESC LIMIT 10"
            ),
            {"uid": user_id},
        ).all()

    total_conversations = len(convs)

    # 按 document_id 分組對話
    doc_conv_map: dict[int, list] = {}
    for c in convs:
        doc_conv_map.setdefault(c.document_id, []).append(c)

    # thread_id → document_id 對照（用於 recent_questions）
    thread_doc_map: dict[str, int] = {c.thread_id: c.document_id for c in convs if c.document_id}

    # 系所計數（以對話次數為權重）
    dept_counter: dict[str, int] = {}
    document_list = []

    for doc_id, doc_convs in sorted(doc_conv_map.items(), key=lambda x: -len(x[1])):
        meta = doc_meta.get(doc_id, {})
        conv_count = len(doc_convs)
        q_count = sum(thread_msg_counts.get(c.thread_id, 0) for c in doc_convs)
        avg_depth = round(q_count / conv_count, 1) if conv_count > 0 else 0.0

        last_viewed = max(
            (c.created_at for c in doc_convs if c.created_at),
            default=None,
        )

        dept = meta.get("department", "")
        if dept:
            dept_counter[dept] = dept_counter.get(dept, 0) + conv_count
        college = college_map.get(dept, "其他") if dept else "其他"

        document_list.append({
            "document_id": doc_id,
            "title": meta.get("title", ""),
            "department": dept,
            "college": college,
            "year": meta.get("year", ""),
            "conversation_count": conv_count,
            "question_count": q_count,
            "avg_depth": avg_depth,
            "last_viewed_at": last_viewed.isoformat() if last_viewed else None,
        })

    # 系所分佈 Top 8
    total_dept = sum(dept_counter.values()) or 1
    dept_distribution = [
        {"name": d, "count": c, "pct": round(c / total_dept * 100, 1)}
        for d, c in sorted(dept_counter.items(), key=lambda x: -x[1])[:8]
    ]

    # 學院分佈 Top 5
    college_counter: dict[str, int] = {}
    for dept, cnt in dept_counter.items():
        col = college_map.get(dept, "其他")
        college_counter[col] = college_counter.get(col, 0) + cnt
    total_col = sum(college_counter.values()) or 1
    college_distribution = [
        {"name": c, "count": n, "pct": round(n / total_col * 100, 1)}
        for c, n in sorted(college_counter.items(), key=lambda x: -x[1])[:5]
    ]

    # 整體平均對話深度
    avg_depth_overall = round(total_questions / total_conversations, 1) if total_conversations > 0 else 0.0

    # 對話深度分佈
    depth_buckets: dict[str, int] = {"1–2 輪": 0, "3–5 輪": 0, "6+ 輪": 0}
    for c in convs:
        n = thread_msg_counts.get(c.thread_id, 0)
        if n <= 2:
            depth_buckets["1–2 輪"] += 1
        elif n <= 5:
            depth_buckets["3–5 輪"] += 1
        else:
            depth_buckets["6+ 輪"] += 1
    depth_distribution = [{"range": k, "count": v} for k, v in depth_buckets.items()]

    # 探索型態診斷
    doc_count = len(doc_conv_map)
    real_colleges = [c for c in college_counter if c != "其他"]
    college_count = len(real_colleges)
    top_dept_pct = dept_distribution[0]["pct"] if dept_distribution else 0

    if avg_depth_overall >= 4 or (avg_depth_overall >= 3 and doc_count <= 2):
        exp_type = "深度鑽研型"
        exp_desc = f"你深度鑽研研究計畫，平均每次對話提問 {avg_depth_overall} 輪，展現出對學術內容的高度專注與深究精神。"
    elif college_count >= 3 and total_conversations >= 3:
        exp_type = "跨域探索型"
        exp_desc = f"你涉獵了來自 {college_count} 個學院的研究計畫，具備廣泛的跨領域研究視野。"
    elif doc_count >= 5 and avg_depth_overall < 2.5:
        exp_type = "廣泛涉獵型"
        exp_desc = f"你瀏覽了 {doc_count} 篇不同的研究計畫，善於快速掌握多個研究方向的概況。"
    elif top_dept_pct > 70:
        top_dept_name = dept_distribution[0]["name"] if dept_distribution else "特定系所"
        exp_type = "專注研究型"
        exp_desc = f"你高度聚焦於 {top_dept_name} 的研究計畫，方向清晰、目標明確。"
    else:
        exp_type = "均衡探索型"
        exp_desc = "你在深度與廣度之間保持均衡，兼顧了不同系所的研究成果。"

    # 最近 10 筆提問
    recent_questions = []
    for r in recent_msg_rows:
        doc_id = thread_doc_map.get(r.thread_id)
        doc_title = doc_meta.get(doc_id, {}).get("title", "") if doc_id else ""
        recent_questions.append({
            "question": r.user_question or "",
            "document_title": doc_title,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    result = {
        "overview": {
            "total_conversations":   total_conversations,
            "total_questions":       total_questions,
            "total_documents_explored": len(doc_conv_map),
            "avg_depth":             avg_depth_overall,
        },
        "dept_distribution":    dept_distribution,
        "college_distribution": college_distribution,
        "depth_distribution":   depth_distribution,
        "exploration_type":     {"type": exp_type, "desc": exp_desc},
        "document_list":        document_list[:8],
        "recent_questions":     recent_questions,
    }
    with _connect() as conn:
        _pdf_analytics_cache_set(conn, user_id, result)
    return result


# ── 定價常數 ──────────────────────────────────────────────────────────────────
_COURSE_PRICE  = {"input": 0.75 / 1_000_000, "output": 4.50 / 1_000_000}
_PDF_AGENT_PX  = {"input": 2.50 / 1_000_000, "output": 10.00 / 1_000_000}
_PDF_ROUTER_PX = {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000}


def _cost_course(inp: int, out: int) -> float:
    return round(inp * _COURSE_PRICE["input"] + out * _COURSE_PRICE["output"], 6)


def _cost_pdf(inp: int, out: int, r_inp: int, r_out: int) -> float:
    agent_inp = max(inp - r_inp, 0)
    agent_out = max(out - r_out, 0)
    return round(
        agent_inp * _PDF_AGENT_PX["input"]  + agent_out * _PDF_AGENT_PX["output"] +
        r_inp     * _PDF_ROUTER_PX["input"] + r_out     * _PDF_ROUTER_PX["output"],
        6,
    )


def _get_pdf_stats(conn, start_dt, end_dt, is_hourly: bool, prev_start_dt, prev_end_dt) -> dict:
    """查詢 pdf_agent_messages 的監控統計。"""
    def _pct(cur, prev):
        return round((cur - prev) / prev * 100, 1) if prev else None

    try:
        alltime = conn.execute("""
            select
                (select count(distinct user_id)::int from pdf_conversations
                 where user_id is not null)                                      as total_users,
                (select count(distinct thread_id)::int from pdf_conversations)  as total_sessions,
                (select count(*)::int from pdf_agent_messages)                  as total_turns,
                (select coalesce(sum(input_tokens),0)::int from pdf_agent_messages) as total_input,
                (select coalesce(sum(output_tokens),0)::int from pdf_agent_messages) as total_output,
                (select coalesce(sum(router_input_tokens),0)::int from pdf_agent_messages)  as total_router_input,
                (select coalesce(sum(router_output_tokens),0)::int from pdf_agent_messages) as total_router_output,
                (select count(distinct thread_id)::int from pdf_agent_messages
                 where created_at > now() - interval '15 minutes')              as online_now
        """).fetchone()

        period = conn.execute("""
            select
                count(distinct m.thread_id)::int              as sessions,
                count(m.id)::int                              as turns,
                coalesce(sum(m.input_tokens),0)::int          as input_tokens,
                coalesce(sum(m.output_tokens),0)::int         as output_tokens,
                coalesce(sum(m.router_input_tokens),0)::int   as router_input,
                coalesce(sum(m.router_output_tokens),0)::int  as router_output,
                count(*) filter (where m.latency_ms > 0)::int as latency_count,
                coalesce(round(avg(m.latency_ms) filter (where m.latency_ms > 0)),0)::int as lat_avg,
                coalesce(percentile_cont(0.95) within group (order by m.latency_ms)
                         filter (where m.latency_ms > 0),0)::int as lat_p95,
                coalesce(percentile_cont(0.99) within group (order by m.latency_ms)
                         filter (where m.latency_ms > 0),0)::int as lat_p99,
                coalesce(min(m.latency_ms) filter (where m.latency_ms > 0),0)::int as lat_min,
                coalesce(max(m.latency_ms) filter (where m.latency_ms > 0),0)::int as lat_max
            from pdf_agent_messages m
            where m.created_at >= %s and m.created_at <= %s
        """, (start_dt, end_dt)).fetchone()

        prev_period = conn.execute("""
            select count(*)::int as turns,
                   coalesce(sum(input_tokens+output_tokens),0)::int as tokens
            from pdf_agent_messages
            where created_at >= %s and created_at <= %s
        """, (prev_start_dt, prev_end_dt)).fetchone()

        active_users = conn.execute("""
            select count(distinct c.user_id)::int as active_users
            from pdf_conversations c
            join pdf_agent_messages m on m.thread_id = c.thread_id
            where m.created_at >= %s and m.created_at <= %s
              and c.user_id is not null
        """, (start_dt, end_dt)).fetchone()

        prev_active = conn.execute("""
            select count(distinct c.user_id)::int as active_users
            from pdf_conversations c
            join pdf_agent_messages m on m.thread_id = c.thread_id
            where m.created_at >= %s and m.created_at <= %s
              and c.user_id is not null
        """, (prev_start_dt, prev_end_dt)).fetchone()

        new_users = conn.execute("""
            select count(distinct user_id)::int as new_users
            from pdf_conversations
            where created_at >= %s and created_at <= %s
              and user_id is not null
              and user_id not in (
                  select distinct user_id from pdf_conversations
                  where created_at < %s and user_id is not null
              )
        """, (start_dt, end_dt, start_dt)).fetchone()

        avg_turns = conn.execute("""
            select round(count(*)::numeric / nullif(count(distinct thread_id),0), 1) as avg_turns
            from pdf_agent_messages
            where created_at >= %s and created_at <= %s
        """, (start_dt, end_dt)).fetchone()

        agent_dist = conn.execute("""
            select agent_name, count(*)::int as cnt
            from pdf_agent_messages
            where created_at >= %s and created_at <= %s
            group by 1 order by 2 desc
        """, (start_dt, end_dt)).fetchall()

        latency_by_agent = conn.execute("""
            select agent_name,
                   count(*)::int as cnt,
                   coalesce(round(avg(latency_ms) filter (where latency_ms>0)),0)::int as avg_ms,
                   coalesce(percentile_cont(0.95) within group (order by latency_ms)
                            filter (where latency_ms>0),0)::int as p95_ms
            from pdf_agent_messages
            where created_at >= %s and created_at <= %s
            group by 1 order by avg_ms desc
        """, (start_dt, end_dt)).fetchall()

        if is_hourly:
            trend_rows = conn.execute("""
                select extract(hour from created_at at time zone 'Asia/Taipei')::int as hour,
                       coalesce(sum(input_tokens),0)::int          as input,
                       coalesce(sum(output_tokens),0)::int         as output,
                       coalesce(sum(router_input_tokens),0)::int   as router_input,
                       coalesce(sum(router_output_tokens),0)::int  as router_output,
                       count(*)::int                               as turns,
                       count(distinct thread_id)::int              as sessions
                from pdf_agent_messages
                where created_at >= %s and created_at <= %s
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()
        else:
            trend_rows = conn.execute("""
                select (created_at at time zone 'Asia/Taipei')::date as date,
                       coalesce(sum(input_tokens),0)::int          as input,
                       coalesce(sum(output_tokens),0)::int         as output,
                       coalesce(sum(router_input_tokens),0)::int   as router_input,
                       coalesce(sum(router_output_tokens),0)::int  as router_output,
                       count(*)::int                               as turns,
                       count(distinct thread_id)::int              as sessions
                from pdf_agent_messages
                where created_at >= %s and created_at <= %s
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()

        if is_hourly:
            activity_rows = conn.execute("""
                select extract(hour from created_at at time zone 'Asia/Taipei')::int as label,
                       count(distinct thread_id)::int as sessions,
                       count(*)::int as turns
                from pdf_agent_messages
                where created_at >= %s and created_at <= %s
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()
        else:
            activity_rows = conn.execute("""
                select (created_at at time zone 'Asia/Taipei')::date as label,
                       count(distinct thread_id)::int as sessions,
                       count(*)::int as turns
                from pdf_agent_messages
                where created_at >= %s and created_at <= %s
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()

        depth_stats_pdf = conn.execute("""
            select count(*)::int as cnt,
                   coalesce(min(tc), 0)::int as min_t,
                   coalesce(round(percentile_cont(0.25) within group (order by tc)), 0)::int as q1,
                   coalesce(round(percentile_cont(0.50) within group (order by tc)), 0)::int as median,
                   coalesce(round(percentile_cont(0.75) within group (order by tc)), 0)::int as q3,
                   coalesce(max(tc), 0)::int as max_t,
                   coalesce(round(avg(tc)::numeric, 1), 0.0)::float as avg_t
            from (select thread_id, count(*)::int as tc from pdf_agent_messages
                  where created_at >= %s and created_at <= %s group by thread_id) s
        """, (start_dt, end_dt)).fetchone()

        pdf_peak_rows = conn.execute("""
            select extract(hour from created_at at time zone 'Asia/Taipei')::int as hour,
                   count(*)::int as turns
            from pdf_agent_messages
            where created_at >= %s and created_at <= %s
            group by 1 order by 1
        """, (start_dt, end_dt)).fetchall()

        if is_hourly:
            latency_trend = conn.execute("""
                select extract(hour from created_at at time zone 'Asia/Taipei')::int as hour,
                       coalesce(round(avg(latency_ms) filter (where latency_ms>0)),0)::int as avg_ms
                from pdf_agent_messages
                where created_at >= %s and created_at <= %s and latency_ms > 0
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()
        else:
            latency_trend = conn.execute("""
                select (created_at at time zone 'Asia/Taipei')::date as date,
                       coalesce(round(avg(latency_ms) filter (where latency_ms>0)),0)::int as avg_ms
                from pdf_agent_messages
                where created_at >= %s and created_at <= %s and latency_ms > 0
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()

    except Exception as exc:
        logger.warning("_get_pdf_stats failed: %s", exc)
        return {}

    at = alltime or {}
    p = period or {}
    total_input  = at.get("total_input", 0)  or 0
    total_output = at.get("total_output", 0) or 0
    total_r_inp  = at.get("total_router_input", 0)  or 0
    total_r_out  = at.get("total_router_output", 0) or 0
    p_inp    = p.get("input_tokens", 0)   or 0
    p_out    = p.get("output_tokens", 0)  or 0
    p_r_inp  = p.get("router_input", 0)   or 0
    p_r_out  = p.get("router_output", 0)  or 0
    cur_turns   = p.get("turns", 0) or 0
    prev_turns  = (prev_period or {}).get("turns", 0) or 0
    cur_tokens  = p_inp + p_out
    prev_tokens = (prev_period or {}).get("tokens", 0) or 0
    cur_active  = (active_users or {}).get("active_users", 0) or 0
    prev_active_v = (prev_active or {}).get("active_users", 0) or 0

    agent_inp  = max(p_inp - p_r_inp, 0)
    agent_out  = max(p_out - p_r_out, 0)
    agent_cost = round(agent_inp * _PDF_AGENT_PX["input"] + agent_out * _PDF_AGENT_PX["output"], 6)
    router_cost = round(p_r_inp * _PDF_ROUTER_PX["input"] + p_r_out * _PDF_ROUTER_PX["output"], 6)

    if is_hourly:
        hmap = {row["hour"]: row for row in trend_rows}
        daily_trend = [
            {"hour": h,
             "input": hmap[h]["input"] if h in hmap else 0,
             "output": hmap[h]["output"] if h in hmap else 0,
             "router_input": hmap[h]["router_input"] if h in hmap else 0,
             "router_output": hmap[h]["router_output"] if h in hmap else 0,
             "turns": hmap[h]["turns"] if h in hmap else 0,
             "sessions": hmap[h]["sessions"] if h in hmap else 0}
            for h in range(24)
        ]
        amap = {row["label"]: row for row in activity_rows}
        activity_trend = [
            {"date": f"{h:02d}:00",
             "sessions": amap[h]["sessions"] if h in amap else 0,
             "turns":    amap[h]["turns"]    if h in amap else 0}
            for h in range(24)
        ]
    else:
        daily_trend = [
            {"date": str(row["date"]),
             "input": row["input"], "output": row["output"],
             "router_input": row["router_input"], "router_output": row["router_output"],
             "turns": row["turns"], "sessions": row["sessions"]}
            for row in trend_rows
        ]
        activity_trend = [
            {"date": str(row["label"]), "sessions": row["sessions"], "turns": row["turns"]}
            for row in activity_rows
        ]

    llm_latency_trend = [
        {"hour": row["hour"], "avg_ms": row["avg_ms"]} if is_hourly
        else {"date": str(row["date"]), "avg_ms": row["avg_ms"]}
        for row in latency_trend
    ]

    return {
        "all_time": {
            "total_users":         at.get("total_users", 0) or 0,
            "total_sessions":      at.get("total_sessions", 0) or 0,
            "total_turns":         at.get("total_turns", 0) or 0,
            "total_input":         total_input,
            "total_output":        total_output,
            "total_router_input":  total_r_inp,
            "total_router_output": total_r_out,
            "online_now":          at.get("online_now", 0) or 0,
            "estimated_cost_usd":  _cost_pdf(total_input, total_output, total_r_inp, total_r_out),
        },
        "period": {
            "active_users":          cur_active,
            "new_users":             (new_users or {}).get("new_users", 0) or 0,
            "sessions":              p.get("sessions", 0) or 0,
            "turns":                 cur_turns,
            "avg_turns_per_session": float((avg_turns or {}).get("avg_turns", 0) or 0),
            "depth_stats": {
                "count":  depth_stats_pdf["cnt"]    if depth_stats_pdf else 0,
                "min":    depth_stats_pdf["min_t"]  if depth_stats_pdf else 0,
                "q1":     depth_stats_pdf["q1"]     if depth_stats_pdf else 0,
                "median": depth_stats_pdf["median"] if depth_stats_pdf else 0,
                "q3":     depth_stats_pdf["q3"]     if depth_stats_pdf else 0,
                "max":    depth_stats_pdf["max_t"]  if depth_stats_pdf else 0,
                "avg":    float(depth_stats_pdf["avg_t"] or 0) if depth_stats_pdf else 0.0,
            } if depth_stats_pdf and depth_stats_pdf["cnt"] > 0 else None,
            "input_tokens":          p_inp,
            "output_tokens":         p_out,
            "router_input_tokens":   p_r_inp,
            "router_output_tokens":  p_r_out,
            "estimated_cost_usd":    agent_cost + router_cost,
            "estimated_cost_breakdown": {
                "agent_usd":  agent_cost,
                "router_usd": router_cost,
            },
            "llm_latency": {
                "count":  p.get("latency_count", 0),
                "avg_ms": p.get("lat_avg", 0),
                "p95_ms": p.get("lat_p95", 0),
                "p99_ms": p.get("lat_p99", 0),
                "min_ms": p.get("lat_min", 0),
                "max_ms": p.get("lat_max", 0),
            } if p.get("latency_count", 0) else None,
            "latency_by_agent": [
                {"agent_name": row["agent_name"], "cnt": row["cnt"],
                 "avg_ms": row["avg_ms"], "p95_ms": row["p95_ms"]}
                for row in latency_by_agent
            ],
            "agent_distribution": [
                {"agent_name": row["agent_name"], "cnt": row["cnt"]}
                for row in agent_dist
            ],
            "llm_latency_trend": llm_latency_trend,
        },
        "trends": {
            "turns":        {"current": cur_turns,  "previous": prev_turns,  "change_pct": _pct(cur_turns,  prev_turns)},
            "tokens":       {"current": cur_tokens, "previous": prev_tokens, "change_pct": _pct(cur_tokens, prev_tokens)},
            "active_users": {"current": cur_active, "previous": prev_active_v, "change_pct": _pct(cur_active, prev_active_v)},
        },
        "daily_trend":    daily_trend,
        "activity_trend": activity_trend,
        "peak_hours": (
            [{"hour": h, "turns": {r["hour"]: r["turns"] for r in pdf_peak_rows}.get(h, 0)} for h in range(24)]
            if pdf_peak_rows is not None else []
        ),
    }


def get_monitor_stats(
    start_dt,
    end_dt,
    cache_key=None,
    feature: str = "all",
):
    """取得系統監控統計（開發人員用）。preset 範圍有快取；自訂日期不快取。"""
    global _MONITOR_CACHE, _MONITOR_CACHE_AT
    now = _now_datetime()

    full_cache_key = f"{cache_key}:{feature}" if cache_key else None
    if full_cache_key is not None:
        ttl = _MONITOR_CACHE_TTL_BY_RANGE.get(cache_key, 300)
        if full_cache_key in _MONITOR_CACHE and full_cache_key in _MONITOR_CACHE_AT:
            if (now - _MONITOR_CACHE_AT[full_cache_key]).total_seconds() < ttl:
                return _MONITOR_CACHE[full_cache_key]

    _ensure_schema()

    is_hourly = (end_dt.date() == start_dt.date())
    duration = end_dt - start_dt
    prev_end_dt   = start_dt - timedelta(seconds=1)
    prev_start_dt = prev_end_dt - duration

    def _pct(cur, prev):
        return round((cur - prev) / prev * 100, 1) if prev else None

    def _cost(inp: int, out: int) -> float:
        return _cost_course(inp, out)

    postgres_status = "ok"
    alltime_row = period_row = prev_period_row = None
    active_users_row = prev_active_row = new_users_row = avg_turns_row = None
    peak_rows = trend_rows = activity_trend_rows = tools_rows = latency_trend_rows = []

    try:
        with _connect() as conn:
            alltime_row = conn.execute("""
                select
                    (select count(*)::int from app_users)                                        as total_users,
                    (select count(distinct session_id)::int from chat_sessions)                  as total_sessions,
                    (select count(id)::int from chat_turns)                                      as total_turns,
                    (select coalesce(sum(input_tokens),0)::int from chat_turns)                  as total_input,
                    (select coalesce(sum(output_tokens),0)::int from chat_turns)                 as total_output,
                    (select count(distinct session_id)::int from chat_turns
                     where created_at > now() - interval '15 minutes')                           as online_now
            """).fetchone()

            period_row = conn.execute("""
                select count(distinct t.session_id)::int     as sessions,
                       count(t.id)::int                      as turns,
                       coalesce(sum(t.input_tokens),0)::int  as input_tokens,
                       coalesce(sum(t.output_tokens),0)::int as output_tokens,
                       count(*) filter (where t.llm_latency_ms > 0)::int as latency_count,
                       coalesce(round(avg(t.llm_latency_ms) filter (where t.llm_latency_ms > 0)), 0)::int as lat_avg,
                       coalesce(percentile_cont(0.95) within group (order by t.llm_latency_ms)
                                filter (where t.llm_latency_ms > 0), 0)::int as lat_p95,
                       coalesce(percentile_cont(0.99) within group (order by t.llm_latency_ms)
                                filter (where t.llm_latency_ms > 0), 0)::int as lat_p99,
                       coalesce(min(t.llm_latency_ms) filter (where t.llm_latency_ms > 0), 0)::int as lat_min,
                       coalesce(max(t.llm_latency_ms) filter (where t.llm_latency_ms > 0), 0)::int as lat_max
                from chat_turns t
                where t.created_at >= %s and t.created_at <= %s
            """, (start_dt, end_dt)).fetchone()

            prev_period_row = conn.execute("""
                select count(id)::int                                   as turns,
                       coalesce(sum(input_tokens+output_tokens),0)::int as tokens
                from chat_turns
                where created_at >= %s and created_at <= %s
            """, (prev_start_dt, prev_end_dt)).fetchone()

            active_users_row = conn.execute("""
                select count(distinct s.user_id)::int as active_users
                from chat_sessions s
                join chat_turns t on t.session_id = s.session_id
                where t.created_at >= %s and t.created_at <= %s
            """, (start_dt, end_dt)).fetchone()

            prev_active_row = conn.execute("""
                select count(distinct s.user_id)::int as active_users
                from chat_sessions s
                join chat_turns t on t.session_id = s.session_id
                where t.created_at >= %s and t.created_at <= %s
            """, (prev_start_dt, prev_end_dt)).fetchone()

            new_users_row = conn.execute("""
                select count(*)::int as new_users
                from app_users
                where created_at >= %s and created_at <= %s
            """, (start_dt, end_dt)).fetchone()

            avg_turns_row = conn.execute("""
                select round(count(id)::numeric / nullif(count(distinct session_id),0), 1) as avg_turns
                from chat_turns
                where created_at >= %s and created_at <= %s
            """, (start_dt, end_dt)).fetchone()

            peak_rows = conn.execute("""
                select extract(hour from created_at at time zone 'Asia/Taipei')::int as hour,
                       count(id)::int as turns
                from chat_turns
                where created_at >= %s and created_at <= %s
                group by 1 order by 1
            """, (start_dt, end_dt)).fetchall()

            if is_hourly:
                trend_rows = conn.execute("""
                    select extract(hour from created_at at time zone 'Asia/Taipei')::int as hour,
                           coalesce(sum(input_tokens),0)::int  as input,
                           coalesce(sum(output_tokens),0)::int as output,
                           count(id)::int                       as turns,
                           count(distinct session_id)::int     as sessions
                    from chat_turns
                    where created_at >= %s and created_at <= %s
                    group by 1 order by 1
                """, (start_dt, end_dt)).fetchall()
            else:
                trend_rows = conn.execute("""
                    select (created_at at time zone 'Asia/Taipei')::date as date,
                           coalesce(sum(input_tokens),0)::int  as input,
                           coalesce(sum(output_tokens),0)::int as output,
                           count(id)::int                       as turns,
                           count(distinct session_id)::int     as sessions
                    from chat_turns
                    where created_at >= %s and created_at <= %s
                    group by 1 order by 1
                """, (start_dt, end_dt)).fetchall()

            if is_hourly:
                activity_trend_rows = conn.execute("""
                    select extract(hour from created_at at time zone 'Asia/Taipei')::int as label,
                           count(distinct session_id)::int as sessions,
                           count(id)::int                   as turns
                    from chat_turns
                    where created_at >= %s and created_at <= %s
                    group by 1 order by 1
                """, (start_dt, end_dt)).fetchall()
            else:
                activity_trend_rows = conn.execute("""
                    select (created_at at time zone 'Asia/Taipei')::date as label,
                           count(distinct session_id)::int as sessions,
                           count(id)::int                   as turns
                    from chat_turns
                    where created_at >= %s and created_at <= %s
                    group by 1 order by 1
                """, (start_dt, end_dt)).fetchall()

            tools_rows = conn.execute("""
                select tool_name, count(*)::int as cnt
                from chat_turns,
                     jsonb_array_elements_text(tools_used) as tool_name
                where jsonb_array_length(tools_used) > 0
                  and created_at >= %s and created_at <= %s
                group by 1 order by 2 desc
                limit 15
            """, (start_dt, end_dt)).fetchall()

            if is_hourly:
                latency_trend_rows = conn.execute("""
                    select extract(hour from created_at at time zone 'Asia/Taipei')::int as hour,
                           coalesce(round(avg(llm_latency_ms) filter (where llm_latency_ms > 0)), 0)::int as avg_ms
                    from chat_turns
                    where created_at >= %s and created_at <= %s and llm_latency_ms > 0
                    group by 1 order by 1
                """, (start_dt, end_dt)).fetchall()
            else:
                latency_trend_rows = conn.execute("""
                    select (created_at at time zone 'Asia/Taipei')::date as date,
                           coalesce(round(avg(llm_latency_ms) filter (where llm_latency_ms > 0)), 0)::int as avg_ms
                    from chat_turns
                    where created_at >= %s and created_at <= %s and llm_latency_ms > 0
                    group by 1 order by 1
                """, (start_dt, end_dt)).fetchall()

            depth_stats_row = conn.execute("""
                select count(*)::int as cnt,
                       coalesce(min(tc), 0)::int as min_t,
                       coalesce(round(percentile_cont(0.25) within group (order by tc)), 0)::int as q1,
                       coalesce(round(percentile_cont(0.50) within group (order by tc)), 0)::int as median,
                       coalesce(round(percentile_cont(0.75) within group (order by tc)), 0)::int as q3,
                       coalesce(max(tc), 0)::int as max_t,
                       coalesce(round(avg(tc)::numeric, 1), 0.0)::float as avg_t
                from (select session_id, count(*)::int as tc from chat_turns
                      where created_at >= %s and created_at <= %s group by session_id) s
            """, (start_dt, end_dt)).fetchone()

    except Exception:
        postgres_status = "error"
        latency_trend_rows = []
        depth_stats_row = None

    qdrant_debug = _qdrant_debug()
    qdrant_status = "ok" if qdrant_debug["status"] == "ok" else "error"

    hour_map = {row["hour"]: row["turns"] for row in peak_rows}
    peak_hours = [{"hour": h, "turns": hour_map.get(h, 0)} for h in range(24)]

    if is_hourly:
        hmap = {row["hour"]: row for row in trend_rows}
        daily_trend = [
            {"hour": h,
             "input":    hmap[h]["input"]    if h in hmap else 0,
             "output":   hmap[h]["output"]   if h in hmap else 0,
             "turns":    hmap[h]["turns"]    if h in hmap else 0,
             "sessions": hmap[h]["sessions"] if h in hmap else 0}
            for h in range(24)
        ]
    else:
        daily_trend = [
            {"date": str(row["date"]), "input": row["input"],
             "output": row["output"], "turns": row["turns"], "sessions": row["sessions"]}
            for row in trend_rows
        ]

    if is_hourly:
        amap = {row["label"]: row for row in activity_trend_rows}
        activity_trend = [
            {"date": f"{h:02d}:00",
             "sessions": amap[h]["sessions"] if h in amap else 0,
             "turns":    amap[h]["turns"]    if h in amap else 0}
            for h in range(24)
        ]
    else:
        activity_trend = [
            {"date": str(row["label"]), "sessions": row["sessions"], "turns": row["turns"]}
            for row in activity_trend_rows
        ]

    cur_turns   = period_row["turns"]       if period_row else 0
    prev_turns  = prev_period_row["turns"]  if prev_period_row else 0
    cur_tokens  = (period_row["input_tokens"] + period_row["output_tokens"]) if period_row else 0
    prev_tokens = prev_period_row["tokens"] if prev_period_row else 0
    cur_active  = active_users_row["active_users"] if active_users_row else 0
    prev_active = prev_active_row["active_users"]  if prev_active_row else 0

    result = {
        "system_health": {"postgres": postgres_status, "qdrant": qdrant_status},
        "qdrant_debug": qdrant_debug,
        "all_time": {
            "total_users":         alltime_row["total_users"]    if alltime_row else 0,
            "total_sessions":      alltime_row["total_sessions"] if alltime_row else 0,
            "total_turns":         alltime_row["total_turns"]    if alltime_row else 0,
            "total_input":         alltime_row["total_input"]    if alltime_row else 0,
            "total_output":        alltime_row["total_output"]   if alltime_row else 0,
            "online_now":          alltime_row["online_now"]     if alltime_row else 0,
            "estimated_cost_usd":  _cost(
                alltime_row["total_input"]  if alltime_row else 0,
                alltime_row["total_output"] if alltime_row else 0,
            ),
        },
        "period": {
            "active_users":          cur_active,
            "new_users":             new_users_row["new_users"]        if new_users_row else 0,
            "sessions":              period_row["sessions"]            if period_row else 0,
            "turns":                 cur_turns,
            "avg_turns_per_session": float(avg_turns_row["avg_turns"]) if avg_turns_row and avg_turns_row["avg_turns"] else 0.0,
            "depth_stats": {
                "count":  depth_stats_row["cnt"]    if depth_stats_row else 0,
                "min":    depth_stats_row["min_t"]  if depth_stats_row else 0,
                "q1":     depth_stats_row["q1"]     if depth_stats_row else 0,
                "median": depth_stats_row["median"] if depth_stats_row else 0,
                "q3":     depth_stats_row["q3"]     if depth_stats_row else 0,
                "max":    depth_stats_row["max_t"]  if depth_stats_row else 0,
                "avg":    float(depth_stats_row["avg_t"] or 0) if depth_stats_row else 0.0,
            } if depth_stats_row and depth_stats_row["cnt"] > 0 else None,
            "input_tokens":          period_row["input_tokens"]        if period_row else 0,
            "output_tokens":         period_row["output_tokens"]       if period_row else 0,
            "estimated_cost_usd":    _cost(
                period_row["input_tokens"]  if period_row else 0,
                period_row["output_tokens"] if period_row else 0,
            ),
            "llm_latency": {
                "count":  period_row["latency_count"] if period_row else 0,
                "avg_ms": period_row["lat_avg"]       if period_row else 0,
                "p95_ms": period_row["lat_p95"]       if period_row else 0,
                "p99_ms": period_row["lat_p99"]       if period_row else 0,
                "min_ms": period_row["lat_min"]       if period_row else 0,
                "max_ms": period_row["lat_max"]       if period_row else 0,
            } if period_row and period_row["latency_count"] > 0 else None,
            "llm_latency_trend": [
                {"hour": row["hour"],         "avg_ms": row["avg_ms"]} if is_hourly
                else {"date": str(row["date"]), "avg_ms": row["avg_ms"]}
                for row in latency_trend_rows
            ],
        },
        "trends": {
            "turns":        {"current": cur_turns,  "previous": prev_turns,  "change_pct": _pct(cur_turns,  prev_turns)},
            "tokens":       {"current": cur_tokens, "previous": prev_tokens, "change_pct": _pct(cur_tokens, prev_tokens)},
            "active_users": {"current": cur_active, "previous": prev_active, "change_pct": _pct(cur_active, prev_active)},
        },
        "peak_hours": peak_hours,
        "daily_trend": daily_trend,
        "tools_usage": [
            {"tool": row["tool_name"], "label": TOOL_LABELS.get(row["tool_name"], row["tool_name"]), "count": row["cnt"]}
            for row in tools_rows
        ],
        "activity_trend": activity_trend,
    }

    # ── PDF stats ────────────────────────────────────────────────────────────
    pdf_stats: dict = {}
    if feature in ("all", "pdf"):
        try:
            with _connect() as conn2:
                pdf_stats = _get_pdf_stats(conn2, start_dt, end_dt, is_hourly, prev_start_dt, prev_end_dt)
        except Exception as exc:
            logger.warning("PDF stats failed: %s", exc)

    # ── combined 去重用戶 ─────────────────────────────────────────────────────
    combined_users = 0
    combined_online = 0
    try:
        if feature in ("all", "course", "pdf"):
            with _connect() as conn3:
                row = conn3.execute("""
                    select count(distinct uid)::int as cnt from (
                        select user_id as uid from chat_sessions where user_id is not null
                        union
                        select user_id as uid from pdf_conversations where user_id is not null
                    ) sub
                """).fetchone()
                combined_users = (row["cnt"] if row else 0) or 0
                combined_online = (
                    (result.get("all_time") or {}).get("online_now", 0) or 0
                ) + (
                    (pdf_stats.get("all_time") or {}).get("online_now", 0) or 0
                )
    except Exception:
        combined_users = result.get("all_time", {}).get("total_users", 0)

    course_at = result.get("all_time", {})
    pdf_at = pdf_stats.get("all_time", {})
    combined = {
        "total_users":    combined_users,
        "total_turns":    (course_at.get("total_turns", 0) or 0) + (pdf_at.get("total_turns", 0) or 0),
        "total_cost_usd": round(
            (course_at.get("estimated_cost_usd", 0) or 0) +
            (pdf_at.get("estimated_cost_usd", 0) or 0),
            6,
        ),
        "online_now": combined_online,
    }

    # course 子物件：把課程相關的 key 組成獨立 dict
    course_stats = {
        "all_time":       result.get("all_time", {}),
        "period":         result.get("period", {}),
        "trends":         result.get("trends", {}),
        "peak_hours":     result.get("peak_hours", []),
        "daily_trend":    result.get("daily_trend", []),
        "tools_usage":    result.get("tools_usage", []),
        "activity_trend": result.get("activity_trend", []),
    } if feature in ("all", "course") else {}

    result["course"]   = course_stats
    result["pdf"]      = pdf_stats
    result["combined"] = combined

    if full_cache_key is not None:
        _MONITOR_CACHE[full_cache_key] = result
        _MONITOR_CACHE_AT[full_cache_key] = now
    return result

def _fmt_bytes(b: int) -> str:
    if b >= 1_073_741_824:
        return f"{b/1_073_741_824:.2f} GB"
    if b >= 1_048_576:
        return f"{b/1_048_576:.1f} MB"
    return f"{b/1_024:.0f} KB"


def _parse_region(url: str, pattern: str) -> str:
    import re
    m = re.search(pattern, url or "")
    return m.group(1) if m else "unknown"


def _qdrant_debug() -> dict:
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    masked_url = qdrant_url
    if qdrant_url:
        from urllib.parse import urlparse
        parsed = urlparse(qdrant_url)
        masked_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    debug = {
        "configured": bool(qdrant_url),
        "url": masked_url or "local:data/processed/qdrant_data",
        "api_key_set": bool(os.getenv("QDRANT_API_KEY", "").strip()),
        "status": "unknown",
        "error": None,
        "collections": [],
    }
    try:
        from app.services.retriever import _get_qdrant
        client = _get_qdrant()
        collections = client.get_collections().collections
        debug["collections"] = [collection.name for collection in collections]
        debug["status"] = "ok"
    except Exception as exc:
        debug["status"] = "error"
        debug["error"] = f"{type(exc).__name__}: {exc}"
    return debug


def get_db_stats() -> dict:
    """回傳 PostgreSQL / Qdrant / Cloudinary 統計 + 基礎設施資訊（60 秒快取）。"""
    global _DB_STATS_CACHE, _DB_STATS_CACHE_AT
    now = _now_datetime()
    if _DB_STATS_CACHE and _DB_STATS_CACHE_AT:
        if (now - _DB_STATS_CACHE_AT).total_seconds() < _DB_STATS_TTL:
            return _DB_STATS_CACHE

    _ensure_schema()

    # ── PostgreSQL ────────────────────────────────────────────────
    tables = []
    db_row = None
    try:
        with _connect() as conn:
            tables = conn.execute("""
                select relname as name,
                       n_live_tup::int as rows,
                       pg_total_relation_size(schemaname||'.'||relname)::bigint as size_bytes,
                       pg_size_pretty(pg_total_relation_size(schemaname||'.'||relname)) as size_pretty
                from pg_stat_user_tables
                order by size_bytes desc
            """).fetchall()
            db_row = conn.execute("""
                select numbackends::int as connections,
                       blks_hit::bigint, blks_read::bigint,
                       case when blks_hit + blks_read > 0
                            then round(blks_hit::numeric/(blks_hit+blks_read)*100, 1)
                            else 100 end as cache_hit_pct,
                       pg_size_pretty(pg_database_size(current_database())) as db_size
                from pg_stat_database
                where datname = current_database()
            """).fetchone()
    except Exception:
        pass

    # ── Qdrant ───────────────────────────────────────────────────
    qdrant_debug = _qdrant_debug()
    qdrant_collections: list[dict] = []
    qdrant_total_points = 0
    if qdrant_debug["status"] == "ok":
        from app.services.retriever import _get_qdrant
        client = _get_qdrant()
        for col_name in qdrant_debug["collections"]:
            try:
                info = client.get_collection(col_name)
                points   = getattr(info, "points_count", None) or 0
                segments = getattr(info, "segments_count", None) or 0
                opt_raw  = getattr(info, "optimizer_status", None)
                opt_ok   = (getattr(opt_raw, "ok", None) is not None) if opt_raw else True
                st_raw   = getattr(info, "status", None)
                status   = str(st_raw.value) if hasattr(st_raw, "value") else str(st_raw or "unknown")
                qdrant_collections.append({
                    "name":          col_name,
                    "points_count":  points,
                    "segments_count": segments,
                    "optimizer_ok":  opt_ok,
                    "status":        status,
                })
                qdrant_total_points += points
            except Exception as exc:
                qdrant_collections.append({
                    "name": col_name, "points_count": 0,
                    "segments_count": 0, "optimizer_ok": True, "status": "unknown",
                    "error": f"{type(exc).__name__}: {exc}",
                })

    # ── Cloudinary ────────────────────────────────────────────────
    cloudinary_stats: dict | None = None
    cloudinary_error: str | None = None
    _cl_name   = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    _cl_key    = os.getenv("CLOUDINARY_API_KEY", "")
    _cl_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not (_cl_name and _cl_key and _cl_secret):
        cloudinary_error = "環境變數未設定：請確認 CLOUDINARY_CLOUD_NAME / API_KEY / API_SECRET 已加入 .env 並重啟後端"
    else:
        try:
            import cloudinary
            import cloudinary.api
            cloudinary.config(
                cloud_name=_cl_name, api_key=_cl_key, api_secret=_cl_secret, secure=True
            )
            data = cloudinary.api.usage()
        except ImportError:
            data = None
            cloudinary_error = "後端未安裝 cloudinary 套件，無法取得 Cloudinary 用量統計。"
        except Exception as e:
            data = None
            err = str(e)
            if "420" in err or "Rate Limit" in err or "Enhance Your Calm" in err:
                cloudinary_error = "Cloudinary API 查詢額度暫時用完，請稍後再試；PDF 連結不受影響。"
            else:
                cloudinary_error = f"cloudinary SDK 失敗：{e}"

        if data is not None:
            # storage / bandwidth 是 dict（有 usage 子欄位）
            # resources / requests 是直接的整數
            st  = data.get("storage", {})
            bw  = data.get("bandwidth", {})
            storage_bytes   = st.get("usage", 0)  if isinstance(st, dict) else int(st or 0)
            bandwidth_bytes = bw.get("usage", 0)  if isinstance(bw, dict) else int(bw or 0)
            resource_count  = data.get("resources", 0)
            if isinstance(resource_count, dict):
                resource_count = resource_count.get("usage", 0)
            cloudinary_stats = {
                "total_resources":  int(resource_count or 0),
                "storage_bytes":    storage_bytes,
                "storage_pretty":   _fmt_bytes(storage_bytes),
                "bandwidth_bytes":  bandwidth_bytes,
                "bandwidth_pretty": _fmt_bytes(bandwidth_bytes),
                "plan":             str(data.get("plan", "unknown")),
            }

    # ── 基礎設施 metadata ─────────────────────────────────────────
    db_url     = os.getenv("DATABASE_URL", "")
    qdrant_url = os.getenv("QDRANT_URL", "")
    neon_region   = _parse_region(db_url,     r'\.([a-z]+-[a-z]+-\d+)\.aws\.neon\.tech')
    qdrant_region = _parse_region(qdrant_url, r'\.([a-z]+-[a-z]+-\d+)-\d+\.aws\.cloud\.qdrant')

    result = {
        "infra": {
            "neon_region":         neon_region,
            "qdrant_region":       qdrant_region,
            "qdrant_total_points": qdrant_total_points,
        },
        "postgres": {
            "connections":   db_row["connections"]   if db_row else 0,
            "cache_hit_pct": float(db_row["cache_hit_pct"]) if db_row else 0.0,
            "db_size":       db_row["db_size"]       if db_row else "unknown",
            "tables": [
                {"name": r["name"], "rows": r["rows"],
                 "size_bytes": r["size_bytes"], "size_pretty": r["size_pretty"]}
                for r in tables
            ],
        },
        "qdrant":           {"collections": qdrant_collections},
        "qdrant_debug":     qdrant_debug,
        "cloudinary":       cloudinary_stats,
        "cloudinary_error": cloudinary_error,
    }
    _DB_STATS_CACHE = result
    _DB_STATS_CACHE_AT = now
    return result


def _empty_analytics() -> dict:
    return {
        "overview": {
            "total_sessions": 0, "total_turns": 0,
            "total_courses_explored": 0, "fav_college": "",
        },
        "dept_distribution":    [],
        "college_distribution": [],
        "tool_usage":           [],
        "top_courses":          [],
        "top_domain_tags":      [],
        "general_edu":          {"total": 0, "categories": [], "top_topic_tags": []},
        "top_course_domains":   [],
    }
