from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from functools import lru_cache
from pydantic import BaseModel, Field, TypeAdapter
from typing import List, Optional
import json
from pathlib import Path
from app.models.schemas import Course
from app.services.access_control import require_live_ai
from app.services.course_info_service import answer_course_question, enrich_course_dict, semantic_search_courses

router = APIRouter()

_COURSE_LIST_ADAPTER = TypeAdapter(List[Course])


class CourseSemanticSearchRequest(BaseModel):
    query: str
    selected_types: List[str] = Field(default_factory=list)
    selected_credits: List[str] = Field(default_factory=list)
    selected_semesters: List[str] = Field(default_factory=list)
    limit: int = 50


class CourseSemanticSearchResponse(BaseModel):
    mode: str
    message: str = ""
    results: List[Course] = Field(default_factory=list)


class CourseAskRequest(BaseModel):
    question: str


class CourseAskResponse(BaseModel):
    answer: str
    model: str
    warnings: List[str] = Field(default_factory=list)

def _normalize_level(level: str) -> str:
    """只接受 undergrad／grad；其他值一律視為 undergrad（與原本行為相同）。"""
    return 'grad' if level == 'grad' else 'undergrad'


# 載入課程資料
def load_courses(level: str = 'undergrad'):
    """回傳快取的課程清單（每個 level 只解析一次）。

    回傳的 list 與 dict 是共享物件，呼叫端只能讀取、篩選，不能修改。
    """
    return list(_load_courses_cached(_normalize_level(level)))


@lru_cache(maxsize=2)
def _load_courses_cached(level: str) -> tuple:
    return tuple(_load_courses_uncached(level))


@lru_cache(maxsize=2)
def _all_courses_json(level: str) -> bytes:
    """未篩選完整清單預先驗證、序列化好的 JSON bytes，讓完整清單請求不必每次重做 3000+ 筆驗證。"""
    # 先驗證成 Course 再序列化，輸出與 response_model=List[Course] 相同（多餘欄位會被丟掉）
    validated = _COURSE_LIST_ADAPTER.validate_python(list(_load_courses_cached(level)))
    return _COURSE_LIST_ADAPTER.dump_json(validated)


@lru_cache(maxsize=1)
def _course_lookup() -> dict:
    """serial_no／course_id → 課程（大學部）。依原順序 setdefault，保留「第一筆符合者」的語意。"""
    index: dict = {}
    for course in _load_courses_cached('undergrad'):
        for key in (course.get('serial_no'), course.get('course_id')):
            if key:
                index.setdefault(key, course)
    return index


def _load_courses_uncached(level: str):
    """從 data/processed/courses_deduped/{level}.json 載入並轉換課程資料"""
    filename = 'grad.json' if level == 'grad' else 'undergrad.json'
    canonical = Path(__file__).parent.parent.parent.parent / 'data' / 'processed' / 'courses_deduped' / filename

    if not canonical.exists():
        print(f"[WARN] 找不到 {canonical}")
        return []

    try:
        with open(canonical, 'r', encoding='utf-8') as f:
            raw_courses = json.load(f)
    except Exception as e:
        print(f"Error loading courses ({level}): {e}")
        return []

    all_courses = []
    for raw_course in raw_courses:
        converted_course = convert_course_format(raw_course)
        if converted_course:
            all_courses.append(enrich_course_dict(converted_course))

    return all_courses

def convert_course_format(raw_course: dict) -> Optional[dict]:
    """將原始課程資料轉換為 API 格式"""
    try:
        # 基本資訊
        semester = raw_course.get('學期', '')
        semester_year = raw_course.get('學年度', '')
        full_half = raw_course.get('全半年', '半')

        # 計算 semester_display
        if full_half == '全':
            semester_display = '全年'
        elif semester == '1':
            semester_display = '上學期'
        elif semester == '2':
            semester_display = '下學期'
        else:
            semester_display = '未指定'

        # 處理課程綱要
        course_outline = raw_course.get('課程綱要', {})

        # 處理核心能力
        core_abilities = []
        for ability in course_outline.get('核心能力', []):
            core_abilities.append({
                'ability_name': ability.get('能力名稱', ''),
                'intensity': ability.get('強度指數', ''),
                'evaluation': ability.get('評量方式', '')
            })

        # 處理分發條件
        distribution_conditions = []
        distribution_data = raw_course.get('分發條件', {})
        for priority_item in distribution_data.get('優先順序列表', []):
            distribution_conditions.append({
                'priority': priority_item.get('優先順序', ''),
                'condition': priority_item.get('相關條件限制說明', '')
            })

        # 轉換為 API 格式
        converted = {
            'serial_no': raw_course.get('流水號', ''),
            'course_id': raw_course.get('課號-班別', ''),
            'course_name_zh': raw_course.get('課程名稱(中文)', ''),
            'course_name_en': raw_course.get('課程名稱(英文)', ''),
            'college': raw_course.get('學院', ''),
            'department': raw_course.get('系所', ''),
            'instructor': raw_course.get('授課教師', ''),
            'credits': int(raw_course.get('學分', 0)),
            'required_elective': raw_course.get('選修別', ''),
            'semester_display': semester_display,
            'semester': f"{semester_year}_{semester}",
            'full_half_year': full_half,
            'course_objective': course_outline.get('課程目標', None),
            'course_content': course_outline.get('授課內容', None),
            'textbooks': course_outline.get('教科書/參考書', None),
            'grading': course_outline.get('評量配分比重', None),
            'course_system': course_outline.get('課程學制', None),
            'course_field': course_outline.get('課程領域', None),
            'core_abilities': core_abilities if core_abilities else None,
            'distribution_conditions': distribution_conditions if distribution_conditions else None,
            'distribution_link': raw_course.get('分發條件_連結', None),
            'outline_link': raw_course.get('課程綱要_連結', None),
            'class_time': raw_course.get('上課時間', None),
            'classroom': raw_course.get('教室', None),
            'note': raw_course.get('備註', None),
            'teaching_method': course_outline.get('授課方式', None),
            'office_hours': course_outline.get('辦公時間', None),
            'weeks': course_outline.get('授課週數', None)
        }

        return converted

    except Exception as e:
        print(f"Error converting course: {e}")
        return None

@router.get("", response_model=List[Course])
def get_courses(
    college: Optional[str] = Query(None, description="學院篩選"),
    department: Optional[str] = Query(None, description="系所篩選"),
    type: Optional[str] = Query(None, description="必修/選修篩選"),
    limit: Optional[int] = Query(None, description="回傳結果數量限制（預設回傳全部）"),
    level: str = Query("undergrad", description="課程層級：undergrad（大學部）或 grad（研究所）")
):
    """
    取得課程列表，支援篩選

    用一般 def（FastAPI 交給 threadpool），讓第一次載入與篩選結果的驗證不卡住 event loop；
    未篩選的完整清單直接回傳快取好的 JSON bytes。
    """
    normalized_level = _normalize_level(level)

    # 未篩選、未限制數量：回傳預先序列化好的完整清單
    if not college and not department and not type and not limit:
        return Response(content=_all_courses_json(normalized_level), media_type="application/json")

    courses = load_courses(level=normalized_level)

    # 套用篩選條件
    filtered_courses = courses

    if college:
        filtered_courses = [c for c in filtered_courses if c.get('college') == college]

    if department:
        filtered_courses = [c for c in filtered_courses if c.get('department') == department]

    if type:
        filtered_courses = [c for c in filtered_courses if c.get('required_elective') == type]

    # 如果有指定 limit，則限制回傳數量；否則回傳全部
    if limit:
        return filtered_courses[:limit]
    return filtered_courses


@router.post("/semantic-search", response_model=CourseSemanticSearchResponse)
def semantic_search_courses_endpoint(request: CourseSemanticSearchRequest):
    """
    Search courses with cached text-embedding-3-large query vectors and local Qdrant collections.
    Falls back on the frontend keyword search if any dependency is unavailable.

    embedding 與 Qdrant 呼叫都是同步的，所以用一般 def（交給 threadpool），不卡住 event loop。
    """
    try:
        results = semantic_search_courses(
            query=request.query,
            selected_types=request.selected_types,
            selected_credits=request.selected_credits,
            selected_semesters=request.selected_semesters,
            limit=max(1, min(request.limit, 120)),
        )
        return CourseSemanticSearchResponse(mode="semantic", results=results)
    except Exception as exc:
        return CourseSemanticSearchResponse(
            mode="fallback",
            message=f"關鍵字向量搜尋暫時不可用，請先改用一般文字搜尋。原因：{exc}",
            results=[],
        )


@router.post("/{course_id}/ask", response_model=CourseAskResponse, dependencies=[Depends(require_live_ai)])
def ask_course_question_endpoint(course_id: str, request: CourseAskRequest):
    """
    Answer a one-off question about a single course. This endpoint does not save chat history.

    LLM 呼叫是同步的，所以用一般 def（交給 threadpool），等待模型時不卡住 event loop。
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="問題不可為空。")

    try:
        result = answer_course_question(course_id=course_id, question=request.question)
        return CourseAskResponse(**result)
    except RuntimeError as exc:
        if "AI_API_CONFIG_MISSING" in str(exc) or "OPENAI_API_KEY" in str(exc):
            raise HTTPException(
                status_code=503,
                detail=(
                    "AI 課程助理目前無法使用，請確認後端 .env 已設定 Azure OpenAI "
                    "或 OPENAI_API_KEY。"
                ),
            ) from exc
        raise HTTPException(status_code=503, detail=f"AI 課程助理暫時無法回答：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/{course_id}", response_model=Course)
def get_course_by_id(course_id: str):
    """
    根據流水號取得課程詳情（查快取的索引，不再每次重新解析課程 JSON）
    """
    course = _course_lookup().get(course_id)
    if course is not None:
        return course

    raise HTTPException(status_code=404, detail="Course not found")
