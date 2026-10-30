"""
build_vector_index.py

建立 Qdrant 向量索引，涵蓋五個 collection：
  ncu_courses_ug    — 大學部課程（含 NLP 萃取結果、教師專長、應修學期）
  ncu_courses_grad  — 研究所課程
  ncu_credit_programs — 學分學程
  ncu_departments   — 暫停建立，等待資料內容與向量策略 review
  ncu_teachers      — 教師官方專長（114_ulistteacher.csv）

執行方式：
  cd <project_root>
  python scripts/rag/build_vector_index.py
  python scripts/rag/build_vector_index.py --reset   # 強制重建（清空舊資料）

環境變數（.env）：
  AZURE_OPENAI_API_KEY=...
  AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
  AZURE_OPENAI_API_VERSION=2024-02-01
  AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large
"""

import argparse
import csv
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import AzureOpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, PointStruct, VectorParams,
    SparseVectorParams, SparseIndexParams, SparseVector,
    PayloadSchemaType,
)
from tqdm import tqdm

# ── 路徑設定 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROC = ROOT / "data" / "processed"
QDRANT_DIR = DATA_PROC / "qdrant_data"

COURSE_ELIGIBILITY = DATA_PROC / "course_eligibility.json"
DEPT_ALIASES       = DATA_PROC / "dept_aliases.json"
COURSES_DEDUPED_UG   = DATA_PROC / "courses_deduped" / "undergrad.json"
COURSES_DEDUPED_GRAD = DATA_PROC / "courses_deduped" / "grad.json"
TEACHER_CSV = DATA_RAW / "114_ulistteacher.csv"

NLP_DIR = DATA_PROC / "nlp"
SCHEDULE_DIR = DATA_PROC / "schedule_draft"
CREDIT_PROGRAMS_DIR = DATA_PROC / "credit_programs"
PROGRAM_DESC = DATA_PROC / "program_descriptions.json"

VECTOR_DIM = 3072   # text-embedding-3-large
EMBED_BATCH = 50    # 每批送給 Azure OpenAI 的文件數


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _create_qdrant_client(url: str, api_key: Optional[str]) -> QdrantClient:
    prefer_grpc = _env_bool("QDRANT_PREFER_GRPC", False)
    grpc_port = _env_int("QDRANT_GRPC_PORT", 6334)
    timeout = _env_float("QDRANT_TIMEOUT_SEC", 60.0)
    return QdrantClient(
        url=url,
        api_key=api_key,
        prefer_grpc=prefer_grpc,
        grpc_port=grpc_port,
        timeout=timeout,
    )

# ── 工具函式 ────────────────────────────────────────────────────────────────

def clean_code(raw: str) -> str:
    return raw.split("-")[0].strip() if raw else ""


def _doc_id_to_int(doc_id: str) -> int:
    return int(hashlib.md5(doc_id.encode()).hexdigest()[:15], 16)


_YEAR_MAP = {"一": 1, "二": 2, "三": 3, "四": 4}
_SEM_MAP  = {"上": 1, "下": 2}

_ALL_SEMS: list[tuple[int, int]] = [
    (1, 1), (1, 2), (2, 1), (2, 2),
    (3, 1), (3, 2), (4, 1), (4, 2),
]


def _parse_single(token: str) -> tuple[Optional[int], Optional[int]]:
    m = re.search(r"大([一二三四])([上下])?", token.strip())
    if not m:
        return None, None
    return _YEAR_MAP.get(m.group(1)), _SEM_MAP.get(m.group(2)) if m.group(2) else None


def parse_when(when_str: str, dept_id: str = "") -> dict:
    result: dict = {
        "when_raw": when_str or "",
        "when_is_dept_scoped": True,   # when 是系所相對時間，跨系比較無意義
        "when_context": f"{dept_id}@{when_str}" if (dept_id and when_str) else (when_str or ""),
        "when_year_start": 0,
        "when_year_end": 0,
        "when_sem_start": 0,
        "when_sem_end": 0,
        "when_semesters": [],   # list[str]，供 Qdrant MatchAny 過濾（需同時指定 dept filter）
    }
    if not when_str:
        return result

    parts = [p.strip() for p in when_str.split("~")]
    y_s, s_s = _parse_single(parts[0])
    if y_s is None:
        return result

    y_e, s_e = (_parse_single(parts[1]) if len(parts) > 1 else (y_s, s_s))
    if y_e is None:
        y_e, s_e = y_s, s_s

    result["when_year_start"] = y_s
    result["when_sem_start"]  = s_s or 1
    result["when_year_end"]   = y_e
    result["when_sem_end"]    = s_e or 2

    try:
        idx_s = _ALL_SEMS.index((y_s, result["when_sem_start"]))
        idx_e = _ALL_SEMS.index((y_e, result["when_sem_end"]))
        result["when_semesters"] = [f"{y}_{s}" for y, s in _ALL_SEMS[idx_s:idx_e + 1]]
    except ValueError:
        pass

    return result


def load_json(path: Path) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── 載入 NLP 萃取資料 ────────────────────────────────────────────────────────

def load_nlp_data() -> dict:
    tech = load_json(NLP_DIR / "nlp_tech_nodes.json")
    simplified = load_json(NLP_DIR / "nlp_simplified_concepts.json")
    domain = load_json(NLP_DIR / "nlp_domain_tags.json")
    topic = load_json(NLP_DIR / "nlp_topic_tags.json")
    prof = load_json(NLP_DIR / "nlp_professor_links.json")

    combined: dict = {}
    all_codes = set(tech) | set(simplified) | set(domain) | set(topic) | set(prof)
    for code in all_codes:
        raw_domain = domain.get(code, {}).get("domain_tags", [])
        domain_fields = [
            tag["field"] for tag in raw_domain
            if isinstance(tag, dict) and tag.get("field")
        ] if raw_domain else []
        domain_rich = "||".join(
            f"{t['field']}::{t.get('relevance', 'medium')}"
            for t in raw_domain
            if isinstance(t, dict) and t.get("field")
        ) if raw_domain else ""

        combined[code] = {
            "tech": tech.get(code, {}),
            "simplified": simplified.get(code, {}).get("simplified_concepts", []),
            "domain_tags": domain_fields,
            "domain_tags_rich": domain_rich,
            "topic_tags": topic.get(code, {}).get("topic_tags", []),
            "core_questions": topic.get(code, {}).get("core_questions", []),
            "prof_links": prof.get(code, []),
        }
    print(f"[NLP] 載入 {len(combined)} 筆課程 NLP 資料")
    return combined


def load_teacher_csv() -> dict[str, str]:
    result: dict[str, str] = {}
    with open(TEACHER_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["教師名稱"].strip()
            spec = row["教師專長"].strip()
            if name:
                result[name] = spec
    print(f"[Teacher CSV] 載入 {len(result)} 位教師")
    return result


def load_eligibility_lookup() -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    if not COURSE_ELIGIBILITY.exists():
        return lookup
    data = load_json(COURSE_ELIGIBILITY)
    if not isinstance(data, list):
        return lookup
    for entry in data:
        code = entry.get("course_code", "").strip()
        if not code:
            continue

        access_rules = entry.get("access_rules")
        if access_rules is not None:
            rels = entry.get("course_relations") or {}
            dept_union    = sorted({d for r in access_rules for d in r.get("dept_include", [])})
            college_union = sorted({c for r in access_rules for c in r.get("college_include", [])})
            open_to_minor        = any(r.get("open_to_minor")        for r in access_rules)
            open_to_double_major = any(r.get("open_to_double_major") for r in access_rules)
            open_to_credit_prog  = any(r.get("open_to_credit_prog")  for r in access_rules)
            open_to_cross_school = any(r.get("open_to_cross_school") for r in access_rules)
            _GRAD = {"master", "phd", "master_inservice", "master_industry"}
            undergrad_years: set[int] = set()
            for r in access_rules:
                if not r.get("program_types") or any(pt not in _GRAD for pt in r["program_types"]):
                    undergrad_years.update(r.get("years", []))
            lookup[code] = {
                "access_rules":        access_rules,
                "eligible_years":      sorted(undergrad_years),
                "dept_include":        dept_union,
                "college_include":     college_union,
                "open_to_minor":       open_to_minor,
                "open_to_double_major":open_to_double_major,
                "open_to_credit_prog": open_to_credit_prog,
                "open_to_cross_school":open_to_cross_school,
                "is_unrestricted":     entry.get("is_unrestricted", False),
                "is_grad_only":        entry.get("is_grad_only", False),
                "is_undergrad_open":   entry.get("is_undergrad_open", True),
                "has_special_condition":entry.get("has_special_condition", False),
                "prereq_codes":        rels.get("prereq_codes", []),
                "coreq_codes":         rels.get("coreq_codes", []),
                "conflict_codes":      rels.get("conflict_codes", []),
            }
        else:
            elig = entry.get("eligibility") or entry
            rels = entry.get("course_relations") or entry
            lookup[code] = {
                "eligible_years":       elig.get("eligible_years", []),
                "dept_include":         elig.get("dept_include", []),
                "college_include":      elig.get("college_include", []),
                "open_to_minor":        elig.get("open_to_minor", False),
                "open_to_double_major": elig.get("open_to_double_major", False),
                "open_to_credit_prog":  elig.get("open_to_credit_prog", False),
                "open_to_cross_school": elig.get("open_to_cross_school", False),
                "is_unrestricted":      elig.get("is_unrestricted", False),
                "is_grad_only":         False,
                "is_undergrad_open":    True,
                "has_special_condition":elig.get("has_special_condition", False),
                "prereq_codes":         rels.get("prereq_codes", []),
                "coreq_codes":          rels.get("coreq_codes", []),
                "conflict_codes":       rels.get("conflict_codes", []),
            }

    print(f"[Eligibility] 載入 {len(lookup)} 筆修課條件")
    return lookup


def load_schedule_lookup() -> dict[str, dict[str, dict]]:
    """回傳 {dept_id: {course_code: {when_raw, when_year_start, when_sem_start, when_year_end, when_sem_end}}}。
    同一課程可出現在多個系所，各自保有獨立 when 記錄。
    """
    lookup: dict[str, dict[str, dict]] = {}
    if not SCHEDULE_DIR.exists():
        return lookup

    def _parse_range(when_str: str) -> tuple[int, int, int, int]:
        parts = [p.strip() for p in when_str.split("~")]
        y_s, s_s = _parse_single(parts[0])
        if y_s is None:
            return 0, 0, 0, 0
        y_e, s_e = (_parse_single(parts[1]) if len(parts) > 1 else (y_s, s_s))
        if y_e is None:
            y_e, s_e = y_s, s_s
        return y_s, (s_s or 1), y_e, (s_e or 2)

    def _index(courses: list, did: str, display_name: str):
        for rc in courses:
            code = rc.get("code", "").strip()
            when = rc.get("when", "")
            if code and when:
                y_start, s_start, y_end, s_end = _parse_range(when)
                lookup.setdefault(did, {})[code] = {
                    "when_raw":        when,
                    "when_year_start": y_start,
                    "when_sem_start":  s_start,
                    "when_year_end":   y_end,
                    "when_sem_end":    s_end,
                    "dept_name":       display_name,
                }

    for college_dir in SCHEDULE_DIR.iterdir():
        if not college_dir.is_dir():
            continue
        for dept_file in college_dir.glob("*.json"):
            try:
                data = load_json(dept_file)
                dept_id   = data.get("id",   dept_file.stem)
                dept_name = data.get("name", dept_id)
                _index(data.get("required_courses", []), dept_id, dept_name)
                for track in data.get("specialization_tracks", []):
                    tid = track.get("id", dept_id)
                    tname = track.get("name", tid)
                    _index(track.get("required_courses", []), tid, tname)
                    for grp in track.get("groups", []):
                        gid = grp.get("id", tid)
                        gname = grp.get("name", gid)
                        _index(grp.get("required_courses", []), gid, gname)
                for grp in data.get("groups", []):
                    gid = grp.get("id", dept_id)
                    gname = grp.get("name", gid)
                    _index(grp.get("required_courses", []), gid, gname)
            except Exception:
                pass

    total = sum(len(v) for v in lookup.values())
    print(f"[Schedule] 載入 {len(lookup)} 個系所，共 {total} 筆必修學期資訊")
    return lookup


# ── 嵌入 ────────────────────────────────────────────────────────────────────

class BM25Embedder:
    """用 fastembed Qdrant/bm25 批量產生 sparse vectors。"""

    def __init__(self):
        try:
            from fastembed import SparseTextEmbedding
            self.model = SparseTextEmbedding(model_name="Qdrant/bm25")
            self.available = True
            print("[BM25] fastembed Qdrant/bm25 model 載入完成")
        except ImportError:
            self.model = None
            self.available = False
            print("[BM25] fastembed 未安裝，跳過 sparse vector（建議：pip install fastembed）")

    def embed_batch(self, texts: list[str]) -> list | None:
        if not self.available:
            return None
        return list(self.model.embed(texts))


class Embedder:
    def __init__(self, client: AzureOpenAI, deployment: str):
        self.client = client
        self.deployment = deployment

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # text-embedding-3-large 最大 8192 tokens；中文約 2 token/字，保守截斷於 3500 字元
        MAX_CHARS = 4000
        texts = [t[:MAX_CHARS] if len(t) > MAX_CHARS else t for t in texts]
        results = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            for attempt in range(3):
                try:
                    resp = self.client.embeddings.create(model=self.deployment, input=batch)
                    results.extend([item.embedding for item in resp.data])
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print(f"  Embedding 重試 {attempt+1}/3：{e}")
                    time.sleep(2 ** attempt)
        return results


# ── 組裝課程文件 ─────────────────────────────────────────────────────────────

def build_course_doc(
    course: dict,
    nlp: dict,
    teacher_lookup: dict[str, str],
    schedule_lookup: dict,
    eligibility_lookup: dict,
    is_grad: bool = False,
) -> tuple[str, str, dict]:
    """回傳 (doc_id, document_text, payload)"""
    syllabus = course.get("課程綱要") or {}
    if isinstance(syllabus, str):
        syllabus = {}

    code_raw = course.get("課號-班別", "")
    code = clean_code(code_raw)
    semester = int(course.get("學期") or 0)
    year = course.get("學年度", "114")
    serial = course.get("流水號", "")

    name_zh = syllabus.get("課程名稱(中文)") or course.get("課程名稱(中文)", "")
    name_en = syllabus.get("課程名稱(英文)") or course.get("課程名稱(英文)", "")
    objective = syllabus.get("課程目標", "") or ""
    content = syllabus.get("授課內容", "") or ""
    dept = course.get("系所", "") or syllabus.get("開課單位", "")
    college = course.get("學院", "")
    credits_raw = syllabus.get("學分") or course.get("學分", "0")
    credits = int(credits_raw) if str(credits_raw).isdigit() else 0
    type_ = course.get("選修別", "")
    teacher = course.get("授課教師", "")

    nlp_data = nlp.get(code, {})
    tech = nlp_data.get("tech", {})
    languages: list[str] = tech.get("languages", [])
    tools: list[str] = tech.get("tools", [])
    concepts: list[str] = tech.get("concepts", [])
    domain_tags: list[str] = nlp_data.get("domain_tags", [])
    topic_tags: list[str] = nlp_data.get("topic_tags", [])

    simplified = nlp_data.get("simplified", [])
    simplified_text = " ".join(
        sc.get("display", "") for sc in simplified if sc.get("display")
    )
    teacher_spec = teacher_lookup.get(teacher, "")
    dept_schedule: list[str] = []
    for _dept_courses in schedule_lookup.values():
        if code in _dept_courses:
            info = _dept_courses[code]
            dept_schedule.append(f"{info['dept_name']}: {info['when_raw']}")
    elig = eligibility_lookup.get(code, {})
    eligible_years: list[int] = elig.get("eligible_years", [])
    prereq_codes: list[str] = elig.get("prereq_codes", [])
    coreq_codes: list[str] = elig.get("coreq_codes", [])
    conflict_codes: list[str] = elig.get("conflict_codes", [])

    core_qs: list[str] = nlp_data.get("core_questions", [])
    prof_links = nlp_data.get("prof_links", [])
    prof_fields = list({
        link["field"] for link in prof_links
        if isinstance(link, dict) and link.get("field")
    })[:5]

    textbook = syllabus.get("教科書/參考書", "") or ""
    core_abilities = syllabus.get("核心能力", []) or []
    ability_names = [a.get("能力名稱", "") for a in core_abilities if isinstance(a, dict) and a.get("能力名稱")]

    # ── document text ──
    parts = []
    if name_zh:
        header = name_zh
        if name_en:
            header += f"（{name_en}）"
        parts.append(header)
    if dept:
        parts.append(f"開課系所：{dept}")
    if teacher:
        parts.append(f"授課教師：{teacher}")
    if teacher_spec:
        parts.append(f"教師專長：{teacher_spec}")
    if type_:
        parts.append(f"修課性質：{type_}")
    if dept_schedule:
        parts.append(f"建議修習：{'、'.join(dept_schedule)}")
    if objective:
        parts.append(f"課程目標：{objective}")
    if content:
        parts.append(f"授課內容：{content}")
    if simplified_text:
        parts.append(f"相關概念：{simplified_text}")
    if languages or tools:
        parts.append(f"使用技術：{', '.join(languages + tools)}")
    if domain_tags:
        parts.append(f"課程領域：{', '.join(domain_tags)}")
    if topic_tags:
        parts.append(f"主題：{', '.join(topic_tags)}")
    if core_qs:
        parts.append(f"核心議題：{' '.join(core_qs[:3])}")
    if prof_fields:
        parts.append(f"相關研究領域：{', '.join(prof_fields)}")
    if ability_names:
        parts.append(f"核心能力：{', '.join(ability_names)}")
    if textbook:
        parts.append(f"參考書目：{textbook}")
    document = "\n".join(parts)

    # ── payload（Qdrant）──
    # 過濾用的 list 欄位直接存陣列，供 MatchAny 做成員查詢
    payload: dict = {
        "_text": document,   # 原始文件文字
        "course_code": code,
        "name_zh": name_zh,
        "name_en": name_en,
        "dept": dept,
        "college": college,
        "credits": credits,
        "type": type_,
        "semester": semester,
        "academic_year": year,
        "teacher": teacher,
        "is_grad": is_grad,
        # ── list 欄位（支援 MatchAny / $contains 過濾）──
        "languages": languages,
        "tools": tools,
        "concepts": concepts,
        "domain_tags": domain_tags,
        "topic_tags": topic_tags,
        "eligible_years": [int(y) for y in eligible_years],
        "dept_include": elig.get("dept_include", []),
        "college_include": elig.get("college_include", []),
        "prereq_codes": prereq_codes,
        "coreq_codes": coreq_codes,
        "conflict_codes": conflict_codes,
        "dept_schedule": dept_schedule,
        # ── 字串/數值欄位 ──
        "domain_tags_rich": nlp_data.get("domain_tags_rich", ""),
        "core_questions": " | ".join(core_qs) if core_qs else "",
        "simplified_concepts": "||".join(
            f"{c.get('original','')}::{c.get('display','')}"
            for c in nlp_data.get("simplified", [])
            if c.get("original") and c.get("display")
        ),
        "teacher_specialties": teacher_spec,
        "open_to_minor":         elig.get("open_to_minor", False),
        "open_to_double_major":  elig.get("open_to_double_major", False),
        "open_to_credit_prog":   elig.get("open_to_credit_prog", False),
        "open_to_cross_school":  elig.get("open_to_cross_school", False),
        "is_unrestricted":          elig.get("is_unrestricted", False),
        "is_grad_only":             elig.get("is_grad_only", False),
        "is_undergrad_open":        elig.get("is_undergrad_open", True),
        "is_open_to_all_undergrad": elig.get("is_open_to_all_undergrad", False),
        "has_special_condition": elig.get("has_special_condition", False),
        "has_prereq":            len(prereq_codes) > 0,
        "objective": objective,
        "content":   content,
        "textbook":  textbook,
        "course_domain":     syllabus.get("課程領域", "") or "",
        "class_time":        course.get("上課時間", "") or "",
        "capacity":          int(course.get("人數限制", 0) or 0) if str(course.get("人數限制", "0") or "0").isdigit() else 0,
    }

    doc_id = f"{year}{semester}_{serial}_{code_raw}"
    return doc_id, document, payload


def load_all_courses(dirs: list[Path]) -> list[dict]:
    courses = []
    for d in dirs:
        if not d.exists():
            continue
        for fp in d.glob("*.json"):
            try:
                data = load_json(fp)
                if isinstance(data, list):
                    courses.extend(data)
            except Exception as e:
                print(f"  [WARN] {fp.name}: {e}")
    return courses


# ── 建立各 Collection ─────────────────────────────────────────────────────────

def _create_payload_indexes(client: QdrantClient, name: str):
    """為 collection 建立 payload 欄位 index，讓 scroll/count 的 filter 在 cloud Qdrant 上正常運作。
    已存在的 index 會被 Qdrant 忽略（冪等操作）。
    """
    # 課程 collection 的 list[str] 欄位（$contains / MatchAny 過濾用）
    keyword_list_fields = [
        "concepts", "tools", "languages",        # NLP 萃取
        "domain_tags", "topic_tags",             # 領域 / 主題標籤
        "dept_schedule",                         # 建議修習學期（格式：「系所: 修習時間」）
        "eligible_years",                        # 修課年級（int list，仍用 KEYWORD）
        "dept_include", "college_include",       # 修課條件
        "prereq_codes", "coreq_codes",           # 先修/同修課號
    ]
    # 課程 collection 的單值字串欄位（$eq / MatchValue 過濾用）
    keyword_scalar_fields = [
        "course_code", "name_zh", "dept", "college", "type", "teacher",
    ]
    # 教師 collection 用
    teacher_fields = ["name", "dept"]

    fields: list[tuple[str, PayloadSchemaType]] = []

    if name in ("ncu_courses_ug", "ncu_courses_grad"):
        fields += [(f, PayloadSchemaType.KEYWORD) for f in keyword_list_fields + keyword_scalar_fields]
        fields += [
            ("is_grad_only",    PayloadSchemaType.KEYWORD),   # bool，用 MatchValue(False)
            ("is_unrestricted", PayloadSchemaType.KEYWORD),
        ]
    elif name == "ncu_teachers":
        fields += [(f, PayloadSchemaType.KEYWORD) for f in teacher_fields]

    created = 0
    for field, schema in fields:
        try:
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=schema,
            )
            created += 1
        except Exception:
            pass  # 已存在或不支援時略過

    print(f"  Payload index 建立完成（{name}）：處理 {len(fields)} 個欄位，新建 {created} 個")


def _prepare_collection(
    client: QdrantClient, name: str, reset: bool, with_bm25: bool = True
) -> bool:
    """若 collection 存在且不 reset，回傳 False（跳過）；否則建立並回傳 True。"""
    existing = {c.name for c in client.get_collections().collections}
    if name in existing:
        if reset:
            client.delete_collection(name)
            print(f"  刪除舊 collection：{name}")
        else:
            print(f"  Collection '{name}' 已存在，跳過（使用 --reset 強制重建）")
            return False
    create_kwargs: dict = dict(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )
    if with_bm25:
        create_kwargs["sparse_vectors_config"] = {
            "bm25": SparseVectorParams(index=SparseIndexParams(on_disk=False))
        }
    client.create_collection(**create_kwargs)
    bm25_tag = "含 BM25" if with_bm25 else "無 BM25"
    print(f"  建立 collection：{name}（{bm25_tag}）")
    return True


def _upsert_batch(client: QdrantClient, name: str, doc_ids: list[str],
                  documents: list[str], payloads: list[dict], embeddings: list,
                  sparse_embeddings=None):
    points = []
    for i, (doc_id, payload, emb) in enumerate(zip(doc_ids, payloads, embeddings)):
        if sparse_embeddings is not None and i < len(sparse_embeddings):
            sp = sparse_embeddings[i]
            # "" 代表 unnamed/default dense vector（named vector context 下的規格）
            vector = {
                "": emb,
                "bm25": SparseVector(indices=sp.indices.tolist(), values=sp.values.tolist()),
            }
        else:
            vector = emb
        points.append(PointStruct(id=_doc_id_to_int(doc_id), vector=vector, payload=payload))
    batch_size = max(1, _env_int("QDRANT_UPSERT_BATCH", 200))
    for i in range(0, len(points), batch_size):
        _upsert_with_retry(client, name, points[i:i + batch_size])


def _upsert_with_retry(client: QdrantClient, name: str, points: list[PointStruct]) -> None:
    retries = max(1, _env_int("QDRANT_UPSERT_RETRIES", 5))
    wait = _env_bool("QDRANT_UPSERT_WAIT", False)
    for attempt in range(retries):
        try:
            client.upsert(collection_name=name, points=points, wait=wait)
            return
        except Exception as ex:
            if attempt == retries - 1:
                raise
            delay = min(30, 2 ** attempt)
            print(f"  [WARN] upsert 失敗，{delay}s 後重試：{ex}")
            time.sleep(delay)


def _build_exclude_lookup() -> dict[str, dict]:
    """從 course_eligibility.json 取出有 dept_exclude / college_exclude 的課程。
    所有名稱透過 dept_aliases.json 正規化。
    """
    aliases: dict[str, str] = {}
    if DEPT_ALIASES.exists():
        aliases = json.loads(DEPT_ALIASES.read_text(encoding="utf-8"))

    if not COURSE_ELIGIBILITY.exists():
        return {}
    data = load_json(COURSE_ELIGIBILITY)
    if not isinstance(data, list):
        return {}

    def resolve(name: str) -> str:
        return aliases.get(name, name)

    lookup: dict[str, dict] = {}
    for entry in data:
        code = entry.get("course_code", "").strip()
        rules = entry.get("access_rules") or []
        if not code or not rules:
            continue

        dept_excl = sorted({resolve(d) for r in rules for d in r.get("dept_exclude", [])})
        coll_excl = sorted({resolve(c) for r in rules for c in r.get("college_exclude", [])})
        if not dept_excl and not coll_excl:
            continue

        dept_incl = sorted({d for r in rules for d in r.get("dept_include", [])})
        coll_incl = sorted({c for r in rules for c in r.get("college_include", [])})
        is_open_all = entry.get("is_open_to_all_undergrad", False)

        lookup[code] = {
            "dept_exclude":            dept_excl,
            "college_exclude":         coll_excl,
            "is_open_with_exclusions": not dept_incl and not coll_incl and not is_open_all,
        }
    return lookup


def _update_exclude_for_collection(client: QdrantClient, collection_name: str, lookup: dict[str, dict]):
    """將 dept_exclude / college_exclude / is_open_with_exclusions 寫入指定 collection。"""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    print(f"  [{collection_name}] 寫入 exclude payload...")
    updated = skipped = 0
    for code, info in lookup.items():
        records, _ = client.scroll(
            collection_name=collection_name,
            scroll_filter=Filter(must=[FieldCondition(key="course_code", match=MatchValue(value=code))]),
            limit=50, with_payload=False, with_vectors=False,
        )
        if not records:
            skipped += 1
            continue
        client.set_payload(
            collection_name=collection_name,
            payload=info,
            points=[r.id for r in records],
        )
        updated += len(records)
    print(f"    更新 {updated} 個 point，跳過 {skipped} 個課號（不在此 collection）")


def update_exclude_payload_all(client: QdrantClient):
    """補齊兩個 course collection 的 dept_exclude / college_exclude / is_open_with_exclusions。
    自動在 --payload-only 或完整重建後執行。
    """
    from qdrant_client.models import PayloadSchemaType as PST
    lookup = _build_exclude_lookup()
    open_excl = [c for c, v in lookup.items() if v["is_open_with_exclusions"]]
    print(f"\n=== [update-exclude] 共 {len(lookup)} 門課程有 exclude 欄位（{len(open_excl)} 門純負向限制）===")

    for col in ("ncu_courses_ug", "ncu_courses_grad"):
        try:
            client.get_collection(col)
        except Exception:
            print(f"  Collection {col} 不存在，跳過")
            continue
        for field, schema in [
            ("dept_exclude",            PST.KEYWORD),
            ("college_exclude",         PST.KEYWORD),
            ("is_open_with_exclusions", PST.BOOL),
        ]:
            try:
                client.create_payload_index(collection_name=col, field_name=field, field_schema=schema)
            except Exception:
                pass
        _update_exclude_for_collection(client, col, lookup)


def update_courses_payload_only(
    qdrant: QdrantClient,
    nlp: dict,
    teacher_lookup: dict,
    schedule_lookup: dict,
    eligibility_lookup: dict,
    name: str,
    canonical_json: Path,
    is_grad: bool,
):
    """只更新 payload，不重新嵌入向量。以 course_code filter 定位 point，避免 ID 猜測錯誤。"""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    print(f"\n=== [payload-only] 更新 {name} ===")
    if not canonical_json.exists():
        print(f"  [ERROR] 找不到 {canonical_json}")
        return

    deduped = load_json(canonical_json)
    if not isinstance(deduped, list):
        print(f"  [ERROR] {canonical_json} 格式異常")
        return

    items: list[tuple[str, dict]] = []   # (course_code, payload)
    skipped = 0
    for c in deduped:
        _, doc_text, payload = build_course_doc(
            c, nlp, teacher_lookup, schedule_lookup, eligibility_lookup, is_grad
        )
        if not doc_text.strip():
            skipped += 1
            continue
        code = payload.get("course_code", "")
        if code:
            items.append((code, payload))

    print(f"  共 {len(items)} 筆需更新（略過空白 {skipped} 筆）")
    updated = errors = 0
    batch_size = max(1, _env_int("QDRANT_UPSERT_BATCH", 200))

    for i in tqdm(range(0, len(items), batch_size), desc="  overwrite_payload"):
        batch = items[i:i + batch_size]
        for code, payload in batch:
            try:
                qdrant.overwrite_payload(
                    collection_name=name,
                    payload=payload,
                    points=Filter(must=[FieldCondition(key="course_code", match=MatchValue(value=code))]),
                )
                updated += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"\n  [WARN] 失敗 code={code}: {e}")

    print(f"  ✓ 完成 {updated} 個課號，失敗 {errors} 個")


def build_courses_collection(
    qdrant: QdrantClient,
    embedder: Embedder,
    bm25: "BM25Embedder",
    nlp: dict,
    teacher_lookup: dict,
    schedule_lookup: dict,
    eligibility_lookup: dict,
    name: str,
    canonical_json: Path,
    is_grad: bool,
    reset: bool,
):
    print(f"\n=== 建立 {name} ===")
    if not _prepare_collection(qdrant, name, reset, with_bm25=bm25.available):
        return

    if not canonical_json.exists():
        print(f"  [ERROR] 找不到 {canonical_json}，請先執行 deduplicate_courses.py")
        return

    deduped = load_json(canonical_json)
    if not isinstance(deduped, list):
        print(f"  [ERROR] {canonical_json} 格式異常")
        return
    print(f"  載入 canonical 課程數：{len(deduped)} 筆")

    ids, docs, payloads = [], [], []
    skipped = 0
    for c in deduped:
        doc_id, doc_text, payload = build_course_doc(
            c, nlp, teacher_lookup, schedule_lookup, eligibility_lookup, is_grad
        )
        if not doc_text.strip():
            skipped += 1
            continue
        ids.append(doc_id)
        docs.append(doc_text)
        payloads.append(payload)

    print(f"  略過空白文件：{skipped}，準備嵌入：{len(ids)}")

    embeddings = []
    for i in tqdm(range(0, len(docs), EMBED_BATCH), desc=f"  Embedding {name}"):
        batch = docs[i : i + EMBED_BATCH]
        embeddings.extend(embedder.embed_batch(batch))

    sparse_embs = bm25.embed_batch(docs) if bm25.available else None
    _upsert_batch(qdrant, name, ids, docs, payloads, embeddings, sparse_embs)
    _create_payload_indexes(qdrant, name)
    info = qdrant.get_collection(name)
    print(f"  完成，collection 總數：{info.points_count}")


def build_programs_collection(qdrant: QdrantClient, embedder: Embedder, bm25: "BM25Embedder", reset: bool):
    print("\n=== 建立 ncu_credit_programs ===")
    if not _prepare_collection(qdrant, "ncu_credit_programs", reset, with_bm25=bm25.available):
        return

    desc_map: dict[str, str] = {}
    if PROGRAM_DESC.exists():
        raw = load_json(PROGRAM_DESC)
        if isinstance(raw, dict):
            desc_map = {k: v.get("description", "") for k, v in raw.items()}

    programs: list[dict] = []
    if CREDIT_PROGRAMS_DIR.exists():
        for fp in CREDIT_PROGRAMS_DIR.glob("*.json"):
            try:
                data = load_json(fp)
                if isinstance(data, list):
                    programs.extend(data)
            except Exception:
                pass

    print(f"  載入 {len(programs)} 個學分學程")

    ids, docs, payloads = [], [], []
    for p in programs:
        pid = p.get("id", "")
        pname = p.get("name", "")
        college = p.get("college", "")
        min_credits = p.get("min_credits", 0)
        description = desc_map.get(pname, p.get("description", ""))

        course_names = []
        for rc in p.get("required_courses", []):
            course_names.append(rc.get("name", ""))
        for eg in p.get("elective_groups", []):
            for c in eg.get("courses", []):
                course_names.append(c.get("name", ""))
        course_names = [n for n in course_names if n]

        parts = [f"{pname}（{college}，最低 {min_credits} 學分）"]
        if description:
            parts.append(description)
        if course_names:
            parts.append(f"包含課程：{', '.join(course_names[:30])}")
        doc_text = "\n".join(parts)

        if not doc_text.strip() or not pid:
            continue

        ids.append(pid)
        docs.append(doc_text)
        payloads.append({
            "_text": doc_text,
            "program_id": pid,
            "program_name": pname,
            "college": college,
            "min_credits": int(min_credits) if str(min_credits).isdigit() else 0,
        })

    embeddings = embedder.embed_batch(docs)
    sparse_embs = bm25.embed_batch(docs) if bm25.available else None
    _upsert_batch(qdrant, "ncu_credit_programs", ids, docs, payloads, embeddings, sparse_embs)
    info = qdrant.get_collection("ncu_credit_programs")
    print(f"  完成，collection 總數：{info.points_count}")


def build_departments_collection(qdrant: QdrantClient, embedder: Embedder, bm25: "BM25Embedder", reset: bool):
    """暫停建立；保留函式簽名，待資料與向量策略 review 完成後實作。"""
    print("\n=== 跳過 ncu_departments：資料內容與向量策略尚未核定 ===")


def build_teachers_collection(
    qdrant: QdrantClient,
    embedder: Embedder,
    bm25: "BM25Embedder",
    reset: bool,
):
    print("\n=== 建立 ncu_teachers ===")
    if not _prepare_collection(qdrant, "ncu_teachers", reset, with_bm25=bm25.available):
        return

    if not TEACHER_CSV.exists():
        print(f"  [WARN] 找不到 {TEACHER_CSV}")
        return

    rows = []
    with open(TEACHER_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    ids, docs, payloads = [], [], []
    for row in rows:
        name = row["教師名稱"].strip()
        dept = row["系所名稱"].strip()
        rank = row["聘書職級"].strip()
        type_ = row["專兼任"].strip()
        spec = row["教師專長"].strip()

        if not name:
            continue

        spec_list = [s.strip() for s in re.split(r"[,，、]", spec) if s.strip()]
        doc_text = (
            f"{name}（{dept}，{rank}，{type_}）\n"
            f"專長：{chr(10).join(spec_list)}"
        )
        doc_id = f"teacher_{dept}_{name}"

        ids.append(doc_id)
        docs.append(doc_text)
        payloads.append({
            "_text": doc_text,
            "name": name,
            "dept": dept,
            "rank": rank,
            "type": type_,
            "specialties": spec,
        })

    embeddings = embedder.embed_batch(docs)
    sparse_embs = bm25.embed_batch(docs) if bm25.available else None
    _upsert_batch(qdrant, "ncu_teachers", ids, docs, payloads, embeddings, sparse_embs)
    _create_payload_indexes(qdrant, "ncu_teachers")
    info = qdrant.get_collection("ncu_teachers")
    print(f"  完成，collection 總數：{info.points_count}")


# ── 主程式 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="建立 Qdrant 向量索引（五個 collection）")
    parser.add_argument(
        "--reset", action="store_true",
        help="強制重建（刪除已存在的 collection 重新嵌入）"
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="只補建 payload index，不重新嵌入（用於已存在的 cloud collection）"
    )
    parser.add_argument(
        "--payload-only", action="store_true",
        help="只更新 payload，不重新嵌入向量（補齊 college 等欄位後使用，無需 Azure 金鑰）"
    )
    parser.add_argument(
        "--update-exclude", action="store_true",
        help="只補寫 dept_exclude / college_exclude / is_open_with_exclusions（無需 Azure 金鑰）"
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")

    qdrant_url = os.environ.get("QDRANT_URL", "")
    qdrant_api_key = os.environ.get("QDRANT_API_KEY")
    if qdrant_url:
        qdrant = _create_qdrant_client(qdrant_url, qdrant_api_key)
    else:
        QDRANT_DIR.mkdir(parents=True, exist_ok=True)
        qdrant = QdrantClient(path=str(QDRANT_DIR))

    if args.index_only:
        print("=== --index-only 模式：只補建 payload index，不重新嵌入 ===")
        for col in ["ncu_courses_ug", "ncu_courses_grad", "ncu_teachers"]:
            print(f"\n[{col}]")
            _create_payload_indexes(qdrant, col)
        print("\n✓ payload index 補建完成")
        return

    if args.update_exclude:
        update_exclude_payload_all(qdrant)
        print("\n✓ exclude payload 更新完成")
        return

    if args.payload_only:
        print("=== --payload-only 模式：只更新 payload，不重新嵌入 ===")
        nlp = load_nlp_data()
        teacher_lookup = load_teacher_csv()
        schedule_lookup = load_schedule_lookup()
        eligibility_lookup = load_eligibility_lookup()
        update_courses_payload_only(
            qdrant, nlp, teacher_lookup, schedule_lookup, eligibility_lookup,
            "ncu_courses_ug", COURSES_DEDUPED_UG, is_grad=False,
        )
        update_courses_payload_only(
            qdrant, nlp, teacher_lookup, schedule_lookup, eligibility_lookup,
            "ncu_courses_grad", COURSES_DEDUPED_GRAD, is_grad=True,
        )
        update_exclude_payload_all(qdrant)
        print("\n✓ payload 更新完成（含 exclude 欄位）")
        return

    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    embed_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")

    if not api_key or not endpoint:
        raise EnvironmentError("請在 .env 設定 AZURE_OPENAI_API_KEY 和 AZURE_OPENAI_ENDPOINT")

    oai = AzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version,
    )
    embedder = Embedder(oai, embed_deployment)
    bm25 = BM25Embedder()

    nlp = load_nlp_data()
    teacher_lookup = load_teacher_csv()
    schedule_lookup = load_schedule_lookup()
    eligibility_lookup = load_eligibility_lookup()

    build_courses_collection(
        qdrant, embedder, bm25, nlp, teacher_lookup, schedule_lookup, eligibility_lookup,
        name="ncu_courses_ug",
        canonical_json=COURSES_DEDUPED_UG,
        is_grad=False,
        reset=args.reset,
    )

    build_courses_collection(
        qdrant, embedder, bm25, nlp, teacher_lookup, schedule_lookup, eligibility_lookup,
        name="ncu_courses_grad",
        canonical_json=COURSES_DEDUPED_GRAD,
        is_grad=True,
        reset=args.reset,
    )

    build_programs_collection(qdrant, embedder, bm25, args.reset)
    build_teachers_collection(qdrant, embedder, bm25, args.reset)
    update_exclude_payload_all(qdrant)

    print("\n✓ 所有 collection 建立完成")
    print(f"  儲存位置：{QDRANT_DIR}")
    for col_name in ["ncu_courses_ug", "ncu_courses_grad", "ncu_credit_programs",
                     "ncu_teachers"]:
        try:
            info = qdrant.get_collection(col_name)
            print(f"  {col_name}: {info.points_count} 筆")
        except Exception:
            pass


if __name__ == "__main__":
    main()
