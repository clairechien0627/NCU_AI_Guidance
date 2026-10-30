"""
tools.py

ReAct Tool-Use 工具定義與執行器。
每個 tool_* 函式對應一個 OpenAI function schema，
execute_tool() 依工具名稱分派執行。
"""

import inspect
import json
from functools import lru_cache
from pathlib import Path

from app.services import retriever, graph_service

ROOT = Path(__file__).parent.parent.parent.parent


@lru_cache(maxsize=1)
def _load_program_descriptions() -> dict:
    path = ROOT / "data" / "processed" / "program_descriptions.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


@lru_cache(maxsize=1)
def _load_requirements_notes() -> dict:
    path = ROOT / "data" / "processed" / "requirements_notes.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


@lru_cache(maxsize=1)
def _load_dept_aliases() -> dict[str, str]:
    path = ROOT / "data" / "processed" / "dept_aliases.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _resolve_name(name: str | None) -> str | None:
    """系所／學院別名 → 正規名稱；查無則原樣回傳。"""
    if not name:
        return name
    return _load_dept_aliases().get(name, name)


@lru_cache(maxsize=1)
def _load_dept_id_name_map() -> dict[str, str]:
    """dept_id (英文 id) → 中文系所名，從 schedule_draft 目錄讀取。"""
    result: dict[str, str] = {}
    schedule_dir = ROOT / "data" / "processed" / "schedule_draft"
    if not schedule_dir.exists():
        return result
    for dept_file in schedule_dir.rglob("*.json"):
        try:
            data = json.loads(dept_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in [data, *data.get("specialization_tracks", []), *data.get("groups", [])]:
            nid = node.get("id", "")
            nname = node.get("name", "")
            if nid and nname:
                result[nid] = nname
    return result


def _fmt_when(when_contexts: list[str]) -> str:
    """when_contexts list → 顯示字串。
    1 個科系：'大一上（地球科學學系）'
    多個科系且 when 相同：'大一上（共 6 系必修）'
    多個科系且 when 不同：'大一上（地球科學學系）、大二上（資訊工程學系）'
    """
    if not when_contexts:
        return ""
    name_map = _load_dept_id_name_map()
    parsed = []
    for ctx in when_contexts:
        if "@" in ctx:
            dept_id, when = ctx.split("@", 1)
            dept_name = name_map.get(dept_id, dept_id)
        else:
            when, dept_name = ctx, ""
        parsed.append((when, dept_name))

    whens = [p[0] for p in parsed]
    if len(set(whens)) == 1:
        when = whens[0]
        if len(parsed) == 1:
            dept = parsed[0][1]
            return f"{when}（{dept}）" if dept else when
        dept_labels = "、".join(d.replace("_", "·") for _, d in parsed if d)
        return f"{when}（{dept_labels}）" if dept_labels else when
    return "、".join(
        f"{w}（{d.replace('_', '·')}）" if d else w for w, d in parsed
    )


# ── 結果格式化 ────────────────────────────────────────────────────────────────

def _s(val) -> str:
    """Qdrant payload 欄位可能是 list（$contains 過濾欄）或 str，統一轉成逗號字串。"""
    if isinstance(val, list):
        return ", ".join(str(x) for x in val if x)
    return val or ""


def _build_summary(m: dict) -> str:
    """從 metadata 結構化欄位組合 summary，避免從 document 截斷。
    通識課（GS/CC 開頭）優先用 topic_tags + core_questions，
    其他課程用 concepts + technologies。
    """
    code = m.get("course_code", "")
    parts = []

    if code.startswith(("GS", "CC")):
        topic = _s(m.get("topic_tags"))
        if topic:
            parts.append(f"主題：{topic}")
        core_qs = _s(m.get("core_questions"))
        if core_qs:
            parts.append(f"核心議題：{core_qs}")
    else:
        concepts = _s(m.get("concepts"))
        if concepts:
            parts.append(f"核心概念：{concepts}")
        tech_parts = [x.strip() for x in
                      (_s(m.get("languages")) + "," + _s(m.get("tools"))).split(",")
                      if x.strip()]
        if tech_parts:
            parts.append(f"技術工具：{', '.join(tech_parts)}")

    return "\n".join(parts)


def _fmt_courses(results: list[dict], exact_match_codes: set | None = None) -> list[dict]:
    exact_match_codes = exact_match_codes or set()
    out = []
    for r in results:
        m = r.get("metadata", {})
        code = m.get("course_code", "")
        tech_parts = [x.strip() for x in
                      (_s(m.get("languages")) + "," + _s(m.get("tools"))).split(",")
                      if x.strip()]
        entry = {
            "course_code":  code,
            "name_zh":      m.get("name_zh", ""),
            "name_en":      m.get("name_en", ""),
            "dept":         m.get("dept", ""),
            "college":      m.get("college", ""),
            "credits":      m.get("credits", 0),
            "type":         m.get("type", ""),
            "teacher":      m.get("teacher", ""),
            "when":         "、".join(m.get("dept_schedule") or []) or _fmt_when(m.get("when_contexts") or []),
            "concepts":     _s(m.get("concepts")),
            "technologies": ", ".join(tech_parts),
            "domain_tags":  _s(m.get("domain_tags_rich")) or _s(m.get("domain_tags")),
            "course_domain": m.get("course_domain", ""),
            "topic_tags":   _s(m.get("topic_tags")),
            "summary":      _build_summary(m),
            "distance":     round(r.get("distance", 0.0), 4),
        }
        if code in exact_match_codes:
            entry["exact_match"] = True
        out.append(entry)
    return out


def _enrich_courses_metadata(courses: list[dict]) -> list[dict]:
    """對 graph 路徑回傳的課程補齊語意欄位（純圖遍歷，無 I/O）。
    course dict 的 id 可能在 "id" 或 "course_code" 鍵下。
    competencies 回傳 list[dict]，含 name/level_num/level_label。
    """
    for c in courses:
        cid = c.get("id") or c.get("course_code", "")
        if not cid:
            continue
        tags = graph_service.get_course_tags(cid)
        c["concepts"]      = ", ".join(tags["concepts"])
        c["technologies"]  = ", ".join(tags["technologies"])
        c["field_tags"]    = ", ".join(tags["field_tags"])
        c["course_domain"] = tags["course_domain"]
        # competencies 保留結構化資料（含等級），方便 LLM 呈現
        c["competencies"]  = tags["competencies"]
        if tags["topic_tags"]:
            c["topic_tags"]     = ", ".join(tags["topic_tags"])
            c["core_questions"] = " | ".join(tags["core_questions"])
    return courses


def _deduplicate_courses_by_name(courses: list[dict]) -> list[dict]:
    """同名 + 同學分的課程視為同課不同班，合併為一筆並記錄 sections 數。
    同名但不同學分（真正不同課）→ 保留各自。
    """
    from collections import defaultdict
    groups: dict[tuple, list] = defaultdict(list)
    for c in courses:
        name = c.get("name") or c.get("name_zh", "")
        cred = c.get("credits") or 0
        groups[(name, cred)].append(c)
    result = []
    for group in groups.values():
        rep = group[0].copy()
        if len(group) > 1:
            rep["sections"]   = len(group)
            rep["course_ids"] = [g.get("id") or g.get("course_code", "") for g in group]
        result.append(rep)
    return result


def _rrf_similar_courses(course_name: str, top_n: int = 15) -> list[dict]:
    """圖共概念排名 + Qdrant 向量語意排名 RRF 融合，回傳相似課程 list[dict]。
    每個 dict 保證有 name、dept、credits 欄位；來自圖的結果還有 shared_concepts。
    """
    graph_results  = graph_service.search_courses_by_concept_cluster(course_name, top_n=40)
    vector_results = retriever.search_courses(course_name, n_results=20)

    RRF_K = 60
    scores: dict[str, float] = {}
    meta:   dict[str, dict]  = {}

    for rank, r in enumerate(graph_results, 1):
        n = r["name"]
        scores[n] = scores.get(n, 0.0) + 1.0 / (RRF_K + rank)
        meta[n] = r

    for rank, r in enumerate(vector_results, 1):
        n = r.get("metadata", {}).get("name_zh", "")
        if not n:
            continue
        scores[n] = scores.get(n, 0.0) + 1.0 / (RRF_K + rank)
        meta.setdefault(n, {
            "name":    n,
            "dept":    r.get("metadata", {}).get("dept", ""),
            "credits": r.get("metadata", {}).get("credits"),
        })

    ranked = [n for n in sorted(scores, key=scores.__getitem__, reverse=True)
              if n != course_name][:top_n]
    return [meta[n] for n in ranked]


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def tool_search_courses(
    query: str,
    dept: str = None,
    college: str = None,
    student_college: str = None,
    course_type: str = None,
    tech: str = None,
    topic_tag: str = None,
    is_grad: bool = False,
    exclude_grad_only: bool = True,
    n: int = 16,
) -> list[dict]:
    """語意搜尋課程，組裝 ChromaDB filters 後呼叫 retriever。
    若 tech 有值，會先走 graph 精確查詢，再補上向量搜尋結果。

    college      : 開課學院（這門課是哪個學院開設的）
    student_college : 學生所屬學院（只回傳該學院學生可修的課，含全校開放課程）
                      兩者皆可接受別名，例如「資電學院」、「生醫學院」。

    【Fallback】若回傳結果 < 3 筆，嘗試：
    1. 移除 tech 參數，改用純語意搜尋
    2. 換成更短的關鍵字（如只保留核心詞）
    3. 改用 find_similar_courses 尋找概念相關課程
    """
    # 別名解析（LLM 可能用縮寫，如「資電學院」→「資訊電機學院」）
    dept            = _resolve_name(dept)
    college         = _resolve_name(college)
    student_college = _resolve_name(student_college)
    # Graph-first：tech 查詢先走圖，結果最精確
    if tech:
        graph_hits = graph_service.search_courses_by_tech(tech)
        if graph_hits:
            # 篩選等級（研究所/大學部）
            level_filter = "grad" if is_grad else "ugrad"
            filtered = [c for c in graph_hits if c.get("level", "ugrad") == level_filter]
            if not filtered:
                filtered = graph_hits  # 若沒符合等級，仍回傳全部
            results = [
                {
                    "id":          c["id"],
                    "course_code": c["id"],
                    "name_zh":     c["name"],
                    "dept":        c["dept"],
                    "credits":     c.get("credits"),
                    "matched_via": c.get("tech_node", "").split("::", 1)[-1] if c.get("tech_node") else "",
                    "source":      "graph_tech",
                }
                for c in filtered[:n * 2]
            ]
            return _enrich_courses_metadata(results)

    # ── Layer 1：Query expansion via ncu_graph_nodes（雙軌）────────────────
    # Track A（Concept/Field/Tech）：去重名稱後附加到 expanded_query
    # Track B（Course 節點，score ≥ 0.65）：去重課名後保留 (nid, score) 作為 Signal C
    from app.services.graph_service import _search_concept_nodes_with_scores
    scored_nodes = _search_concept_nodes_with_scores(query, top_k=25)
    g = graph_service._g()

    _EXPAND_TYPES = {"Concept", "Technology", "Field", "Competency"}
    seen_term_names: set[str] = {query.lower()}       # 排除與 query 相同的詞
    extra_terms: list[str] = []
    seen_course_names: set[str] = set()
    direct_course_items: list[tuple[str, float]] = []  # (nid, score)

    for nid, score in scored_nodes:
        nd = g["nodes"].get(nid, {})
        ntype = nd.get("node_type", "")
        name = nd.get("name", "")
        if ntype in _EXPAND_TYPES:
            if name and name.lower() not in seen_term_names:
                extra_terms.append(name)
                seen_term_names.add(name.lower())
        elif ntype == "Course" and score >= 0.65:
            # 同名課程只保留第一筆（分數最高）；不同系所同名課程靠 A/B 搜到
            if name and name not in seen_course_names:
                # college/dept filter：Signal C 也要尊重學院/系所篩選
                # graph 節點的 college 欄位即為開課學院，可直接比對
                if dept and nd.get("dept", "") != dept:
                    continue
                if college and nd.get("college", "") != college:
                    continue
                seen_course_names.add(name)
                direct_course_items.append((nid, score))

    expanded_query = (query + " " + " ".join(extra_terms[:5])).strip() if extra_terms else query

    # ── Layer 2：Qdrant filters ──────────────────────────────────────────────
    conditions: list[dict] = []
    if dept:
        conditions.append({"dept": {"$eq": dept}})
    if college:
        conditions.append({"college": {"$eq": college}})
    if course_type:
        conditions.append({"type": {"$eq": course_type}})
    if exclude_grad_only and not is_grad:
        conditions.append({"is_grad_only": {"$eq": False}})
    if student_college:
        # 三種命中條件（OR）：
        #   1. 全校開放課程
        #   2. college_include 直接列有此學院
        #   3. dept_include 列有此學院旗下某系所（以 dept_college_map 展開）
        _col_map = retriever._load_college_map()
        depts_in_college = [d for d, c in _col_map.items() if c == student_college]
        student_college_or: list[dict] = [
            {"is_open_to_all_undergrad": {"$eq": True}},
            {"college_include":          {"$contains": student_college}},
        ]
        for _dept in depts_in_college:
            student_college_or.append({"dept_include": {"$contains": _dept}})
        conditions.append({"$or": student_college_or})
    if tech:
        conditions.append({"$or": [
            {"tools":     {"$contains": tech}},
            {"languages": {"$contains": tech}},
            {"concepts":  {"$contains": tech}},
        ]})
    if topic_tag:
        conditions.append({"topic_tags": {"$contains": topic_tag}})

    if len(conditions) == 0:
        filters = None
    elif len(conditions) == 1:
        filters = conditions[0]
    else:
        filters = {"$and": conditions}

    collection = "ncu_courses_grad" if is_grad else "ncu_courses_ug"

    # Signal A：擴展查詢 + filters
    results_a = retriever.search_courses(
        expanded_query, filters=filters, n_results=n * 3, collection=collection
    )

    # Signal B：原始查詢 + filters（確保原始語意不被擴展稀釋）
    if expanded_query != query:
        results_b = retriever.search_courses(
            query, filters=filters, n_results=n * 3, collection=collection
        )
    else:
        results_b = []

    # Signal A_excl：補回「開放全部學士生但排除特定系所/學院」的課程
    # 這類課程（is_open_with_exclusions=True）dept_include=[] / college_include=[]，
    # 無法被正向 OR filter 命中，需獨立搜尋並用 must_not 過濾不可修的學院
    results_excl: list[dict] = []
    if student_college:
        _col_map_excl = retriever._load_college_map()
        depts_in_college = [d for d, c in _col_map_excl.items() if c == student_college]
        # must_not 條件：dept_exclude 或 college_exclude 含此學院任何系所或學院本身
        excl_must_not: list[dict] = (
            [{"dept_exclude": {"$contains": d}} for d in depts_in_college]
            + [{"dept_exclude":    {"$contains": student_college}}]  # 學院名稱直接列在 dept_exclude 中
            + [{"college_exclude": {"$contains": student_college}}]  # college_exclude 含此學院
        )
        excl_base_filter: dict = {"is_open_with_exclusions": {"$eq": True}}
        if exclude_grad_only and not is_grad:
            excl_base_filter = {"$and": [
                {"is_open_with_exclusions": {"$eq": True}},
                {"is_grad_only": {"$eq": False}},
            ]}
        results_excl = retriever.search_courses(
            expanded_query,
            filters=excl_base_filter,
            must_not_conds=excl_must_not,
            n_results=n * 2,
            collection=collection,
        )
        # Signal B_excl：原始查詢版（確保 CC0328 這類精確命名課程不被擴展詞稀釋）
        if expanded_query != query:
            results_excl_b = retriever.search_courses(
                query,
                filters=excl_base_filter,
                must_not_conds=excl_must_not,
                n_results=n * 2,
                collection=collection,
            )
            results_excl = results_excl + results_excl_b

    # ── Layer 3：RRF 融合 + Signal C 注入 + dedup ────────────────────────────
    # 統一走 RRF 路徑（即使無擴展也能做到 course_code dedup）
    RRF_K = 60
    code_scores: dict[str, float] = {}
    code_meta:   dict[str, dict]  = {}

    for rank, r in enumerate(results_a, 1):
        code = r.get("metadata", {}).get("course_code", "") or r.get("id", "")
        if not code:
            continue
        code_scores[code] = code_scores.get(code, 0.0) + 1.0 / (RRF_K + rank)
        code_meta.setdefault(code, r)

    if results_b:
        for rank, r in enumerate(results_b, 1):
            code = r.get("metadata", {}).get("course_code", "") or r.get("id", "")
            if not code:
                continue
            code_scores[code] = code_scores.get(code, 0.0) + 1.0 / (RRF_K + rank)
            code_meta.setdefault(code, r)

    # Signal A_excl：「全體學士可修但排除特定系」課程補回（is_open_with_exclusions=True）
    # 距離門檻：避免 pool 太小時把語意不相關的課帶進結果（例如「探索太空」出現在機器學習搜尋）
    _EXCL_DIST_THRESHOLD = 0.65
    results_excl_filtered = [
        r for r in results_excl if r.get("distance", 1.0) <= _EXCL_DIST_THRESHOLD
    ]
    for rank, r in enumerate(results_excl_filtered, 1):
        code = r.get("metadata", {}).get("course_code", "") or r.get("id", "")
        if not code:
            continue
        code_scores[code] = code_scores.get(code, 0.0) + 1.0 / (RRF_K + rank)
        code_meta.setdefault(code, r)

    # Signal N：名稱完全匹配 boost（課程名稱 == query 視為最高相關度）
    # 找到同名課程後，若通過 student_college 驗證，給予固定高分 boost
    NAME_EXACT_BOOST = 0.15  # 遠高於正常 RRF 上限（單 Signal 最大 ≈ 1/61 ≈ 0.016）
    exact_name_hits = retriever.get_courses_by_name(query, collection=collection)
    exact_match_codes: set[str] = set()
    for r in exact_name_hits:
        meta = r.get("metadata", {})
        code = meta.get("course_code", "") or r.get("id", "")
        if not code:
            continue
        # student_college 驗證（同 Signal C 邏輯）
        if student_college:
            c_include  = meta.get("college_include", [])
            d_include  = meta.get("dept_include", [])
            is_open    = meta.get("is_open_to_all_undergrad", False)
            is_excl    = meta.get("is_open_with_exclusions", False)
            c_exclude  = meta.get("college_exclude", [])
            d_exclude  = meta.get("dept_exclude", [])
            _col_map_n = retriever._load_college_map()
            depts_in_scn = [d for d, c in _col_map_n.items() if c == student_college]
            depts_ok_n = any(d in d_include for d in depts_in_scn)
            if not (is_open or student_college in c_include or depts_ok_n or is_excl):
                continue
            if student_college in c_exclude or student_college in d_exclude:
                continue
            if any(d in d_exclude for d in depts_in_scn):
                continue
        code_scores[code] = code_scores.get(code, 0.0) + NAME_EXACT_BOOST
        code_meta.setdefault(code, r)
        exact_match_codes.add(code)

    # Signal N2：課名包含 query 加分（query in name_zh，非完全相同）
    # 例：搜尋「程式設計」→「程式設計-Python」應獲得 boost 上升
    PARTIAL_BOOST = 0.05  # 約為 Signal N 的 1/3，不搶 exact_match 第一位
    for code, r in list(code_meta.items()):
        if code in exact_match_codes:
            continue  # 已是 exact，跳過
        meta = r.get("metadata", {})
        name_zh = meta.get("name_zh", "")
        if not name_zh or query not in name_zh:
            continue
        if college and meta.get("college", "") != college:
            continue
        if dept and meta.get("dept", "") != dept:
            continue
        code_scores[code] = code_scores.get(code, 0.0) + PARTIAL_BOOST

    # Signal C：依 graph score 加權 boost（score ≥ 0.65；score 越高，boost 越強）
    for course_code, sig_score in direct_course_items:
        # 跳過已停開課程（graph 節點名稱含 [已停開]）
        if "[已停開]" in g["nodes"].get(course_code, {}).get("name", ""):
            continue
        boost = sig_score / (RRF_K + 1)
        if course_code in code_scores:
            code_scores[course_code] += boost
        else:
            hits = retriever.get_courses_by_code(course_code, collection=collection)
            if hits:
                if student_college:
                    # Signal C 補入前驗證：三種正向命中 + 負向排除均通過才收入
                    meta = hits[0].get("metadata", {})
                    c_include  = meta.get("college_include", [])
                    d_include  = meta.get("dept_include", [])
                    is_open    = meta.get("is_open_to_all_undergrad", False)
                    is_excl    = meta.get("is_open_with_exclusions", False)
                    c_exclude  = meta.get("college_exclude", [])
                    d_exclude  = meta.get("dept_exclude", [])
                    _col_map   = retriever._load_college_map()
                    depts_in_sc = [d for d, c in _col_map.items() if c == student_college]

                    # 正向：全開、學院命中、任一系所命中、或為 open_with_exclusions 型
                    depts_ok   = any(d in d_include for d in depts_in_sc)
                    passes_positive = (
                        is_open
                        or student_college in c_include
                        or depts_ok
                        or is_excl
                    )
                    if not passes_positive:
                        continue

                    # 負向：dept_exclude / college_exclude 命中則排除
                    if student_college in c_exclude:
                        continue
                    if any(d in d_exclude for d in depts_in_sc):
                        continue
                    if student_college in d_exclude:
                        continue
                code_scores[course_code] = boost
                code_meta[course_code] = hits[0]

    ranked_all = sorted(code_scores, key=code_scores.__getitem__, reverse=True)

    # exact_match 課程（Signal N 命中）：置頂，不佔 n 名額
    exact_ranked = [c for c in ranked_all if c in exact_match_codes and c in code_meta]

    # 語意結果：排除 exact_match 課程 + 排除服務學習課程（SC 前綴，誤標全開放）
    semantic_ranked = [
        c for c in ranked_all
        if c not in exact_match_codes
        and c in code_meta
        and not c.startswith("SC")
    ][:n]

    results = [code_meta[c] for c in exact_ranked + semantic_ranked]
    return _fmt_courses(results, exact_match_codes=exact_match_codes)


def tool_get_dept_courses(dept_name: str, course_type: str = "required") -> dict:
    """圖查詢系所必修或選修課程。圖缺資料時自動 fallback 至 ChromaDB 精確過濾。

    【Fallback】若系所名稱找不到，嘗試：
    1. 只用關鍵部分（如「資工」而非「資訊工程學系」）
    2. 呼叫 search_courses(dept="...", n=20) 做向量補充
    """
    if course_type == "required":
        courses = graph_service.get_dept_required_courses(dept_name)
    elif course_type == "elective":
        courses = graph_service.get_dept_elective_courses(dept_name)
        if not courses:
            # Fallback：用 ChromaDB 精確 get（dept+type 雙重過濾，不受向量搜尋上限影響）
            raw = retriever.get_courses_by_dept_type(dept_name, "選修")
            courses = [
                {
                    "id":       r["id"],
                    "name":     r["metadata"].get("name_zh", ""),
                    "credits":  r["metadata"].get("credits"),
                    "relation": "選修",
                    "teacher":  r["metadata"].get("teacher", ""),
                }
                for r in raw
            ]
    else:
        req  = graph_service.get_dept_required_courses(dept_name)
        elec = graph_service.get_dept_elective_courses(dept_name)
        if not elec:
            raw  = retriever.get_courses_by_dept_type(dept_name, "選修")
            elec = [
                {"id": r["id"], "name": r["metadata"].get("name_zh", ""),
                 "credits": r["metadata"].get("credits"), "relation": "選修"}
                for r in raw
            ]
        courses = req + elec
    # 補齊語意欄位（concepts/technologies/field_tags/course_domain/competencies）
    courses = _enrich_courses_metadata(courses)
    # 同名同學分去重（分班問題）
    courses = _deduplicate_courses_by_name(courses)
    return {
        "dept_name":   dept_name,
        "course_type": course_type,
        "total_found": len(courses),
        "courses":     courses,
    }


def tool_get_program_courses(program_name: str) -> dict:
    """圖查詢學分學程的必/選修課程。"""
    courses = graph_service.get_program_courses(program_name)
    return {"program_name": program_name, "courses": courses}


def tool_get_program_info(program_name: str) -> dict:
    """查詢學分學程完整說明與必/選修課程（整合版）。
    整合 get_program_description + get_program_courses，一次呼叫取得說明文字 + 課程清單。
    """
    data = _load_program_descriptions()
    description = ""
    resolved_name = program_name
    if data:
        for name, desc in data.items():
            if program_name in name or name in program_name:
                description = desc
                resolved_name = name
                break
        if not description:
            # 向量搜尋 Fallback（取代單字元模糊比對，避免誤命中不相關學程）
            try:
                hits = retriever.search_programs(program_name, n_results=3)
                for hit in hits:
                    candidate = hit.get("metadata", {}).get("program_name", "")
                    if not candidate:
                        continue
                    for k, v in data.items():
                        if candidate in k or k in candidate:
                            resolved_name = k
                            description = v
                            break
                    if description:
                        break
            except Exception:
                pass

    courses = graph_service.get_program_courses(resolved_name)

    if not description and not courses:
        # 用向量搜尋提供語意相近的學程清單，而非全部學程
        try:
            hits = retriever.search_programs(program_name, n_results=5)
            available = [
                h.get("metadata", {}).get("program_name", "")
                for h in hits
                if h.get("metadata", {}).get("program_name")
            ]
        except Exception:
            available = list(data.keys())[:10] if data else []
        return {"found": False, "message": f"找不到「{program_name}」的學程資料",
                "available_programs": available}

    required = [c for c in courses if "必" in c.get("relation", "")]
    elective  = [c for c in courses if c not in required]
    return {
        "found":            True,
        "program_name":     resolved_name,
        "description":      description,
        "required_courses": required,
        "elective_courses": elective,
        "courses":          courses,   # combined，供 course_pool 收集
    }


def tool_get_teacher_info(teacher_name: str) -> dict:
    """查詢特定教師的專長資料（CSV）與開課清單（圖）。"""
    profile_raw = retriever.get_teacher_by_name(teacher_name)
    courses = graph_service.get_teacher_courses(teacher_name)
    profile = [
        {
            "name":        r.get("metadata", {}).get("name", teacher_name),
            "dept":        r.get("metadata", {}).get("dept", ""),
            "rank":        r.get("metadata", {}).get("rank", ""),
            "specialties": r.get("metadata", {}).get("specialties", ""),
        }
        for r in profile_raw
    ]
    return {"teacher_name": teacher_name, "profile": profile, "courses": courses}


def tool_search_teachers(query: str, n: int = 5) -> list[dict]:
    """依研究領域或專長關鍵詞語意搜尋教師。"""
    results = retriever.search_teachers(query, n_results=n)
    return [
        {
            "name":        r.get("metadata", {}).get("name", ""),
            "dept":        r.get("metadata", {}).get("dept", ""),
            "rank":        r.get("metadata", {}).get("rank", ""),
            "specialties": r.get("metadata", {}).get("specialties", ""),
        }
        for r in results
    ]


def tool_get_prereq_info(course_query: str) -> dict:
    """搜尋目標課程後展開 prereq_codes，回傳先修課詳情。"""
    results = retriever.search_courses(course_query, n_results=3)
    if not results:
        return {"found": False, "message": f"找不到「{course_query}」的相關課程"}
    meta = results[0].get("metadata", {})
    prereq_str = meta.get("prereq_codes", "")
    prereq_details = []
    if prereq_str:
        for code in prereq_str.split(","):
            code = code.strip()
            if not code:
                continue
            for r in retriever.get_courses_by_code(code):
                m = r.get("metadata", {})
                prereq_details.append({
                    "course_code": m.get("course_code", code),
                    "name_zh":     m.get("name_zh", code),
                    "credits":     m.get("credits", 0),
                    "dept":        m.get("dept", ""),
                    "when_raw":    m.get("when_raw", ""),
                })
    return {
        "target_course": meta.get("name_zh", ""),
        "course_code":   meta.get("course_code", ""),
        "prereq_codes":  prereq_str,
        "prereq_details": prereq_details,
        "has_prereq":    bool(prereq_str),
    }


def tool_get_graduation_rules(dept_name: str) -> dict:
    """從 schedule_draft 取得系所畢業學分規定與認證要求。"""
    result = graph_service.get_graduation_rules(dept_name)
    if not result:
        return {"found": False, "message": f"找不到「{dept_name}」的畢業規定資料"}
    return result


def tool_get_dept_info(query: str) -> list[dict]:
    """暫停使用；不得查詢尚在 review 的 ncu_departments collection。"""
    return [{
        "error": "get_dept_info_disabled",
        "message": "系所介紹資料與向量策略尚在 review，目前不提供此查詢。",
    }]


def _matches_access_rule(rule: dict, student_dept: str, student_year: int,
                          program_type: str = "bachelor",
                          minor_depts: list | None = None,
                          double_major_depts: list | None = None,
                          student_college: str = "") -> bool:
    """回傳 True 表示此 rule 允許該學生修課。空 list = 不限制。"""
    minor_depts = minor_depts or []
    double_major_depts = double_major_depts or []

    # 學制檢查（空 = 不限）
    if rule["program_types"] and program_type not in rule["program_types"]:
        return False

    # 年級檢查（空 = 不限）
    if rule["years"] and student_year not in rule["years"]:
        return False

    # 系所/學院：兩者皆空 = 不限；否則符合任一即可
    no_restriction = not rule["dept_include"] and not rule["college_include"]
    dept_ok    = no_restriction or (student_dept in rule["dept_include"])
    college_ok = no_restriction or (bool(student_college) and student_college in rule["college_include"])
    minor_ok   = rule["open_to_minor"] and (
        not rule["minor_include"] or any(d in rule["minor_include"] for d in minor_depts)
    )
    double_ok  = rule["open_to_double_major"] and (
        not rule["double_major_include"] or any(d in rule["double_major_include"] for d in double_major_depts)
    )
    if not (dept_ok or college_ok or minor_ok or double_ok):
        return False

    # 排除檢查
    if student_dept in rule.get("dept_exclude", []):
        return False
    if student_college and student_college in rule.get("college_exclude", []):
        return False

    return True


def can_student_take(course_metadata: dict, student_dept: str, student_year: int,
                     program_type: str = "bachelor",
                     minor_depts: list | None = None,
                     double_major_depts: list | None = None) -> bool:
    """
    給定課程 metadata 與學生資料，判斷是否可修。
    course_metadata 需含 access_rules（v3 格式）或 is_unrestricted（舊格式 fallback）。
    """
    if course_metadata.get("is_unrestricted"):
        return True
    access_rules = course_metadata.get("access_rules")
    if access_rules is None:
        return True
    student_college = retriever.get_dept_college(student_dept)
    return any(
        _matches_access_rule(r, student_dept, student_year, program_type,
                             minor_depts, double_major_depts, student_college)
        for r in access_rules
    )


def tool_get_course_eligibility(course_query: str) -> dict:
    """查詢課程的修課資格限制（年級、系所、是否開放外系等）。

    回傳 raw_conditions 原文，讓 LLM 直接理解分發條件。
    有多門同名課程時全部回傳，由 LLM 判斷哪門是使用者詢問的。
    """
    def _fmt_elig(m: dict) -> dict:
        code = m.get("course_code", "")
        elig = retriever.get_course_eligibility(code)
        raw_list = elig.get("raw_conditions", [])
        if elig.get("is_unrestricted"):
            raw_conditions = "不限修課條件（全校皆可修）"
        elif raw_list:
            raw_conditions = " | ".join(raw_list)
        else:
            raw_conditions = "無詳細分發條件資料"
        return {
            "course_code":   code,
            "name_zh":       m.get("name_zh", ""),
            "dept":          m.get("dept", ""),
            "raw_conditions": raw_conditions,
        }

    exact = retriever.get_courses_by_name(course_query)
    if exact:
        return {
            "found": True,
            "match_type": "exact",
            "courses": [_fmt_elig(r["metadata"]) for r in exact],
        }

    results = retriever.search_courses(course_query, n_results=5)
    if not results:
        return {"found": False, "message": f"找不到「{course_query}」"}
    return {
        "found": True,
        "match_type": "semantic",
        "courses": [_fmt_elig(r["metadata"]) for r in results],
    }


def tool_get_course_knowledge_map(course_name: str) -> str:
    """回傳一門課的知識地圖：學什麼概念、用什麼技術、哪些課程與它概念重疊最高。

    【使用時機】使用者問「演算法在學什麼？」「機器學習這門課教哪些東西？」
    「有沒有和機器學習類似的課？」等探索式問題時使用。
    比純語意搜尋更能呈現課程的完整知識結構。
    """
    result = graph_service.get_course_knowledge_map(course_name)
    if not result:
        return f"找不到「{course_name}」的課程資料（請嘗試更短的關鍵詞）。"

    lines = []
    c = result["course"]
    lines.append(f"【{c['name']}】（{c['dept']}，{c.get('credits', '?')}學分）")

    techs = result.get("technologies", [])
    if techs:
        lines.append(f"\n使用技術：{', '.join(t['name'] for t in techs[:10])}")

    concepts = result.get("concepts", [])
    if concepts:
        lines.append(f"涵蓋概念（共 {len(concepts)} 個）：{', '.join(c['name'] for c in concepts[:15])}")
        if len(concepts) > 15:
            lines.append(f"  ...等 {len(concepts)} 個概念")

    # RRF 融合（圖 + 向量）取相似課程
    similar = _rrf_similar_courses(c["name"], top_n=8)
    if similar:
        lines.append(f"\n概念重疊最高的相關課程（可延伸學習）：")
        for s in similar:
            cnt = s.get("shared_concepts", 0)
            suffix = f" [共享 {cnt} 個概念]" if cnt > 1 else ""
            lines.append(f"  - {s['name']}（{s.get('dept', '')}）{suffix}")

    return "\n".join(lines)


def tool_get_depts_by_tech(tech_name: str) -> str:
    """Multi-hop 查詢哪些系所的課程有教某技術或概念。

    【使用時機】使用者問「什麼科系需要學 Python？」「哪些系有教機器學習？」
    「哪個系所最重視 SQL 技術？」等問題時使用。
    利用知識圖譜的多跳路徑，區分「必修」與「選修」含此技術的系所。
    比向量搜尋更能呈現結構性的「哪些系重視這個技術」。
    """
    result = graph_service.get_depts_by_tech(tech_name)
    nodes = result.get("nodes", [])
    if not nodes:
        return f"查無教「{tech_name}」的系所或課程資料。"

    exact_nodes   = [n for n in nodes if n["score"] >= 1.0]
    similar_nodes = [n for n in nodes if n["score"] < 1.0]

    lines: list[str] = []

    def _fmt_node(node: dict, label_prefix: str) -> None:
        node_name = node["node_name"]
        req  = node.get("required_depts", [])
        elec = node.get("elective_depts", [])
        courses = node["courses"]

        lines.append(label_prefix)
        if req:
            all_req = "、".join(req)
            suffix = f"（共 {len(req)} 個）" if len(req) > 8 else ""
            lines.append(f"  必修科系{suffix}：{all_req}")
        else:
            lines.append("  必修科系：（無）")
        if elec:
            shown = elec[:12]
            suffix = f"（共 {len(elec)} 個，以下列部分）" if len(elec) > 12 else f"（共 {len(elec)} 個）"
            lines.append(f"  選修科系{suffix}：{'、'.join(shown)}")
        else:
            lines.append("  選修科系：（無）")

        lines.append(f"  相關課程（{len(courses)} 門）：")
        for c in courses[:10]:
            req_in  = "、".join(c.get("required_in", []))
            elec_in = "、".join(c.get("elective_in", []))
            role = ""
            if req_in:
                role = f"必修：{req_in}"
            if elec_in:
                role += f"{'，' if role else ''}選修：{elec_in}"
            college = c.get("college", "")
            dept_str = f"（{c['dept']}）" if c.get("dept") else ""
            college_str = f"[{college}]" if college else ""
            lines.append(f"    - {c['name']}{dept_str}{college_str}" + (f" → {role}" if role else ""))

    for node in exact_nodes:
        _fmt_node(node, f"\n【{node['node_name']}】")

    if similar_nodes:
        lines.append("\n[以下為相關擴展查詢結果，可作為補充參考，回答時請自然呈現，勿使用「語意相近節點」等技術詞彙]")
        for node in similar_nodes:
            _fmt_node(node, f"\n◆ 相關：{node['node_name']}（{node['node_type']}）")

    return "\n".join(lines)


def tool_ppr_explore(
    seed: str,
    focus: str = "course",
    top_k: int = 15,
) -> str:
    """Personalized PageRank 探索：從種子概念出發，找整個知識圖譜中最相關的節點。

    【使用時機】使用者想做廣泛探索時：
    - 「和機器學習相關的一切有哪些？」
    - 「我對 AI 有興趣，中央大學有什麼相關資源？」
    - 「深度學習連結到哪些老師和系所？」
    比 find_similar_courses 更廣，能跨越課程、老師、系所等所有節點類型。

    ⚠️ 種子策略：系統自動從「語意概念節點」與「課程節點」雙路徑起跑 PPR，
       無需使用者指定種子類型。基礎學科（微積分、線代）請用 focus="course"。

    focus 參數（控制回傳節點類型）：
    - "course"（預設）：只回傳課程
    - "instructor"：只回傳教師
    - "dept"：只回傳系所
    - "overview"：分組顯示課程 + 教師 + 系所 + 學程（適合全貌探索）
    注意：概念/技術節點是 PPR 內部種子，不作為輸出——如傳入 "concept" 將自動改為 "course"
    """
    _OVERVIEW_TYPES = [
        "Course", "Instructor",
        "Department", "DeptGroup", "CollegeBachelorProgram",
        "CreditProgram",
    ]
    _FOCUS_MAP: dict[str, list[str] | None] = {
        "course":     ["Course"],
        "instructor": ["Instructor"],
        "dept":       ["Department", "DeptGroup", "CollegeBachelorProgram"],
        "overview":   _OVERVIEW_TYPES,
        "all":        _OVERVIEW_TYPES,   # 向後兼容別名
        "concept":    ["Course"],         # 重導向：概念是種子不是輸出
    }
    _TYPE_LABELS = {
        "Course": "課程", "Instructor": "教師",
        "Department": "系所", "DeptGroup": "系所",
        "CollegeBachelorProgram": "學院學士班",
        "CreditProgram": "學分學程",
    }

    is_overview = focus in ("overview", "all")
    type_filter = _FOCUS_MAP.get(focus, ["Course"])
    seed_list = [s.strip() for s in seed.replace("、", ",").replace("，", ",").split(",")]

    # E2-a：預先確認種子節點覆蓋，給 LLM 可操作的提示
    found_seed_ids = graph_service._find_ppr_seeds(seed_list)
    if not found_seed_ids:
        missing = "、".join(seed_list)
        return (
            f"圖中找不到「{missing}」的對應概念節點。\n"
            f"建議：(1) 嘗試英文關鍵詞（如 'machine learning'）；"
            f"(2) 使用更短的核心詞（如「機器學習」而非「機器學習演算法」）；"
            f"(3) 改用 search_courses 進行向量搜尋。"
        )

    # 找出哪些 seed 名稱完全沒有對應圖節點
    found_names = {
        graph_service._g()["nodes"].get(nid, {}).get("name", "").lower()
        for nid in found_seed_ids
    }
    missing_seeds = [
        s for s in seed_list
        if s and not any(s.lower() in fn or fn in s.lower() for fn in found_names if fn)
    ]

    fetch_k = top_k * 3 if is_overview else top_k
    results = graph_service.ppr_explore(
        seed_names=seed_list,
        top_k=fetch_k,
        node_type_filter=type_filter,
    )

    if not results:
        return f"找不到以「{seed}」為起點的相關節點（請確認概念名稱是否正確）。"

    def _fmt(r: dict) -> str:
        label = _TYPE_LABELS.get(r["node_type"], r["node_type"])
        extra = f"（{r['dept']}）" if r.get("dept") else ""
        return f"  [{label}] {r['name']}{extra}  [PPR: {r['score']}]"

    if not is_overview:
        lines = [f"以「{seed}」為起點的 PPR 探索（{focus} 模式，{len(results)} 筆）："]
        lines += [_fmt(r) for r in results[:top_k]]
        if missing_seeds:
            lines.append(f"\n⚠️ 注意：「{'、'.join(missing_seeds)}」在圖中無對應節點，已略去；結果僅基於其他種子。")
        return "\n".join(lines)

    # ── overview 模式：分組顯示 ──────────────────────────────────────────────
    _GROUPS = [
        ("Course",        "課程",      {"Course"}),
        ("Instructor",    "教師",      {"Instructor"}),
        ("dept",          "系所",      {"Department", "DeptGroup", "CollegeBachelorProgram"}),
        ("CreditProgram", "學分學程",  {"CreditProgram"}),
    ]
    _CAP = {"Course": 10, "Instructor": 6, "dept": 5, "CreditProgram": 4}

    buckets: dict[str, list[dict]] = {k: [] for k, _, _ in _GROUPS}
    for r in results:
        ntype = r["node_type"]
        for key, _, types in _GROUPS:
            if ntype in types:
                buckets[key].append(r)
                break

    lines = [f"以「{seed}」為起點的全景探索（overview 模式）：\n"]
    has_any = False
    for key, group_label, _ in _GROUPS:
        items = buckets[key]
        if not items:
            continue
        has_any = True
        show = items[:_CAP[key]]
        lines.append(f"{group_label}（{len(show)} 筆）：")
        lines += [_fmt(r) for r in show]
        lines.append("")

    if not has_any:
        return f"找不到以「{seed}」為起點的相關節點（請確認概念名稱是否正確）。"
    if missing_seeds:
        lines.append(f"⚠️ 注意：「{'、'.join(missing_seeds)}」在圖中無對應節點，已略去；結果僅基於其他種子。")
    return "\n".join(lines)


def tool_get_program_description(program_name: str) -> str:
    """直接從 program_descriptions.json 取得學分學程的完整說明。

    【使用時機】使用者詢問某學分學程的說明、目標、修課方式、學程內容時，
    此工具比向量搜尋更完整，應優先使用。
    若想同時取得課程清單，可搭配 get_program_courses 一起呼叫。
    """
    data = _load_program_descriptions()
    if not data:
        return "program_descriptions.json 資料尚未載入。"
    # 精確 / 包含比對
    for name, desc in data.items():
        if program_name in name or name in program_name:
            return f"【{name}】\n{desc}"
    # 部分字元模糊比對（任一字元命中）
    matches = [(k, v) for k, v in data.items()
               if any(c in k for c in program_name if len(c.encode()) > 1)]
    if matches:
        return "\n\n".join(f"【{k}】\n{v}" for k, v in matches[:3])
    return "找不到符合的學程說明。現有學程：\n" + "\n".join(f"- {k}" for k in data.keys())


def tool_get_requirements_notes(dept_name: str) -> str:
    """直接從 requirements_notes.json 取得系所畢業規定原文。

    【使用時機】使用者詢問某系所畢業學分、修課規定、必選修要求、
    修業規定細節時，此工具比 get_graduation_rules 更完整（含原始說明文字），
    應優先使用。
    """
    data = _load_requirements_notes()
    if not data:
        return "requirements_notes.json 資料尚未載入。"
    for dept, notes in data.items():
        if dept_name in dept or dept in dept_name:
            return f"【{dept} 畢業規定】\n{notes}"
    # 模糊
    matches = [(k, v) for k, v in data.items()
               if any(c in k for c in dept_name if len(c.encode()) > 1)]
    if matches:
        return "\n\n".join(f"【{k}】\n{v}" for k, v in matches[:2])
    return f"找不到「{dept_name}」的畢業規定資料。"


def tool_explore_concept_neighborhood(
    query: str,
    hops: int = 2,
    top_k: int = 15,
) -> str:
    """以概念詞彙為中心做知識圖譜鄰域探索，找出覆蓋該概念的相關課程（N跳 BFS）。

    【使用時機】使用者問「有哪些課程涵蓋 X 概念？」、「學習 Y 技術需要哪些課？」、
    「X 和哪些課有直接關聯？」時使用。

    與 ppr_explore 的差別：
    - ppr_explore：全圖 PPR 擴散，覆蓋更廣（捕捉間接關聯）
    - explore_concept_neighborhood：有限 N 跳 BFS，更精確的直接鄰域

    支援 Qdrant 向量入口（需先執行 build_qdrant_index.py）；
    未建立時自動退回字串比對入口。

    hops: BFS 跳數（預設 2，最多 3）
    """
    hops = max(1, min(hops, 3))
    results = graph_service.explore_by_concept_neighborhood(query, hops=hops, top_k=top_k)
    if not results:
        return (f"找不到與「{query}」相關的概念節點。"
                f"請嘗試更精確的概念詞，如「深度學習」、「Python」、「資料結構」。")
    lines = [f"以「{query}」為中心的概念鄰域（{hops} 跳，共 {len(results)} 門課）："]
    for r in results:
        dept    = r.get("dept") or "?"
        credits = r.get("credits") or "?"
        lines.append(f"- {r['name']}（{dept}，{credits} 學分）")
    return "\n".join(lines)


def tool_get_course_community(course_name: str) -> str:
    """找指定課程在知識圖譜 Leiden 社群中的定位，並列出同類別課程。

    【使用時機】使用者問「機器學習屬於哪個課程領域？」、「和深度學習同一類的課有哪些？」、
    「這門課的課程類別是什麼？」等分類定位問題。
    """
    result = graph_service.get_course_community(course_name)
    if not result.get("found"):
        return result.get("message", f"找不到「{course_name}」的社群資料。")
    label    = result.get("label") or f"社群 {result['community_id']}"
    match    = "（精確匹配）" if result.get("exact_match") else "（模糊匹配）"
    concepts = "、".join(result.get("top_concepts", []))
    related  = "、".join(result.get("related_courses", []))
    lines = [
        f"「{course_name}」{match}",
        f"所屬類別：{label}（共 {result['size']} 門課）",
        f"核心概念：{concepts or '（無）'}",
        f"同類別課程（部分）：{related or '（無）'}",
    ]
    return "\n".join(lines)


def tool_list_course_communities() -> str:
    """列出知識圖譜 Leiden 分群的所有課程類別（社群）及其規模與核心概念。

    【使用時機】使用者問「中央大學課程有哪些大類？」、「有哪些 AI 相關的課程類別？」、
    「學校課程大致分成哪幾個領域？」等整體性分類問題。
    """
    communities = graph_service.list_course_communities()
    if not communities:
        return "社群資料尚未建立，請執行 compute_communities.py。"
    lines = [f"中央大學課程社群（Leiden 分群，共 {len(communities)} 個類別）："]
    for c in communities:
        label    = c.get("label") or f"社群 {c['id']}"
        concepts = "、".join(c.get("top_concepts", [])[:4])
        lines.append(f"- {label}（{c['size']} 門課）｜{concepts}")
    return "\n".join(lines)


def tool_find_similar_courses(course_name: str) -> str:
    """找與指定課程有最多共同概念的相似課程（跨系所）。

    【使用時機】使用者問「有沒有類似 OO 的課？」、「哪些課和 OO 有關？」、
    「跨系有沒有教類似內容的課？」時使用。
    利用知識圖譜共概念排名 + Qdrant 向量語意排名做 RRF 融合，覆蓋比純關鍵字搜尋更廣。
    """
    similar = _rrf_similar_courses(course_name, top_n=15)

    if not similar:
        return (f"找不到與「{course_name}」概念相近的課程（可能課程名稱不符，"
                f"請嘗試更短的關鍵詞，如只輸入核心詞）。")

    lines = [f"與「{course_name}」概念相近的課程（共 {len(similar)} 門）："]
    for r in similar:
        dept    = r.get("dept", "")
        credits = r.get("credits", "?")
        line    = f"- {r['name']}（{dept}，{credits}學分）"
        cnt = r.get("shared_concepts", 0)
        if cnt > 1:
            line += f" [共享概念：{cnt} 個]"
        lines.append(line)
    return "\n".join(lines)


def tool_get_graduation_requirements(dept_name: str) -> dict:
    """查詢系所畢業規定：結構化學分要求（最低學分、必修學分、認證要求）+ 完整原文。

    整合原有 get_graduation_rules 與 get_requirements_notes，一次呼叫取得全部。
    """
    # alias 正規化（縮寫 → 正式全名），避免 fallback 取到錯誤系所
    aliases = graph_service._load_dept_aliases()
    normalized = aliases.get(dept_name, dept_name)

    rules = graph_service.get_graduation_rules(normalized)
    notes_data = _load_requirements_notes()

    raw_notes = ""
    # 依序嘗試正規化名稱、原始名稱做精確或包含比對
    for name_try in dict.fromkeys([normalized, dept_name]):
        for dept, notes in notes_data.items():
            if dept == name_try or name_try in dept or dept in name_try:
                raw_notes = notes
                break
        if raw_notes:
            break

    if not rules and not raw_notes:
        return {"found": False, "message": f"找不到「{dept_name}」的畢業規定資料"}

    result: dict = {"found": True, "dept_name": normalized, "raw_notes": raw_notes}
    if rules:
        result["min_credits"]      = rules.get("min_credits")
        result["required_credits"] = rules.get("required_credits")
        result["certifications"]   = rules.get("certifications", [])
    return result


def tool_search_programs(query: str, n: int = 20) -> list[dict]:
    """語意搜尋學分學程，依描述或主題找最相關的學程清單。

    【使用時機】使用者問「有沒有 AI 相關的學程？」「理工學院有哪些學程？」等發現型查詢。
    找到學程後，再用 get_program_description / get_program_courses 取得詳情。
    """
    results = retriever.search_programs(query, n_results=n)
    return [
        {
            "program_name":        r.get("metadata", {}).get("program_name", ""),
            "college":             r.get("metadata", {}).get("college", ""),
            "description_excerpt": r.get("document", "")[:300],
            "distance":            round(r.get("distance", 0.0), 4),
        }
        for r in results
    ]


def tool_get_course_detail(
    name_zh: str = None,
    dept: str = None,
    course_code: str = None,
) -> dict:
    """查詢課程完整資訊：官方課綱（目標/內容/教科書）+ 修課資格分發條件（含先修要求）。

    整合原 get_course_syllabus + get_course_eligibility + get_prereq_info。
    raw_conditions 已包含先修課程與年級/系所限制的原文，無需再分別查詢。

    同名課程消歧義：
    - 有 course_code → 精確查詢，無歧義
    - 只有 name_zh → 若多科系都有此課，回傳 ambiguous=True + candidates，由使用者選擇
    - name_zh + dept → 精確定位到指定科系
    """
    def _build(m: dict) -> dict:
        code = m.get("course_code", "")
        elig = retriever.get_course_eligibility(code)
        raw_list = elig.get("raw_conditions", [])
        if elig.get("is_unrestricted"):
            raw_conditions = "不限修課條件（全校皆可修）"
        elif raw_list:
            raw_conditions = " | ".join(raw_list)
        else:
            raw_conditions = "無詳細分發條件資料"
        return {
            "found": True, "ambiguous": False,
            "course_code":    code,
            "name_zh":        m.get("name_zh", ""),
            "dept":           m.get("dept", ""),
            "teacher":        m.get("teacher", ""),
            "credits":        m.get("credits", 0),
            "objective":      m.get("objective", ""),
            "content":        m.get("content", ""),
            "textbook":       m.get("textbook", ""),
            "raw_conditions": raw_conditions,
        }

    if course_code:
        results = retriever.get_courses_by_code(course_code)
        if not results:
            results = retriever.get_courses_by_code(course_code, collection="ncu_courses_grad")
        if not results:
            return {"found": False, "message": f"找不到課號「{course_code}」"}
        return _build(results[0]["metadata"])

    if not name_zh:
        return {"found": False, "message": "請提供課程名稱（name_zh）或課號（course_code）"}

    exact = retriever.get_courses_by_name(name_zh)
    if not exact:
        fallback = retriever.search_courses(name_zh, n_results=5)
        if fallback:
            return {
                "found": False,
                "fallback_candidates": [
                    {
                        "name_zh":     r.get("metadata", {}).get("name_zh", ""),
                        "dept":        r.get("metadata", {}).get("dept", ""),
                        "course_code": r.get("metadata", {}).get("course_code", ""),
                        "credits":     r.get("metadata", {}).get("credits", 0),
                        "distance":    round(r.get("distance", 0.0), 4),
                    }
                    for r in fallback
                ],
                "message": f"找不到「{name_zh}」，以下是相似課程，請確認名稱後重新查詢",
            }
        return {"found": False, "message": f"找不到課程「{name_zh}」"}

    if dept:
        filtered = [r for r in exact if r["metadata"].get("dept", "") == dept]
        if filtered:
            exact = filtered

    depts = list({r["metadata"].get("dept", "") for r in exact})
    if len(depts) > 1 and not dept:
        return {
            "found": True,
            "ambiguous": True,
            "name_zh": name_zh,
            "message": f"找到 {len(depts)} 個科系都有「{name_zh}」，請指定 dept 或由使用者選擇：",
            "candidates": [
                {
                    "dept":        r["metadata"].get("dept", ""),
                    "course_code": r["metadata"].get("course_code", ""),
                    "teacher":     r["metadata"].get("teacher", ""),
                    "credits":     r["metadata"].get("credits", 0),
                }
                for r in exact
            ],
        }

    return _build(exact[0]["metadata"])


# ── 分派表 ────────────────────────────────────────────────────────────────────

_TOOL_MAP = {
    "search_courses":              tool_search_courses,
    "get_dept_courses":            tool_get_dept_courses,
    "get_program_info":            tool_get_program_info,
    "get_program_courses":         tool_get_program_courses,    # backward compat
    "get_program_description":     tool_get_program_description, # backward compat
    "get_teacher_info":            tool_get_teacher_info,
    "search_teachers":             tool_search_teachers,
    "get_graduation_requirements": tool_get_graduation_requirements,
    "get_graduation_rules":        tool_get_graduation_rules,       # backward compat
    "get_requirements_notes":      tool_get_requirements_notes,     # backward compat
    "get_course_detail":           tool_get_course_detail,
    "search_programs":             tool_search_programs,
    "find_similar_courses":        tool_find_similar_courses,
    "get_course_knowledge_map":    tool_get_course_knowledge_map,
    "get_depts_by_tech":           tool_get_depts_by_tech,
    "ppr_explore":                    tool_ppr_explore,
    "explore_concept_neighborhood":   tool_explore_concept_neighborhood,
    # get_course_community / list_course_communities：社群已注入圖，暫不暴露為獨立工具
}


def execute_tool(name: str, args: dict):
    fn = _TOOL_MAP.get(name)
    if fn is None:
        return {"error": f"未知工具：{name}"}
    # Filter out unknown kwargs — LLM sometimes hallucinates parameter names (e.g. 'eligable_year')
    valid_params = set(inspect.signature(fn).parameters.keys())
    filtered_args = {k: v for k, v in args.items() if k in valid_params}
    try:
        return fn(**filtered_args)
    except Exception as e:
        return {"error": str(e)}


# ── OpenAI Function Schema ────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_courses",
            "description": "語意搜尋課程，支援依系所、學院、技術、選修別、修課資格等條件過濾。【重要】當使用者詢問特定程式語言或工具（如 PyTorch、Python、TensorFlow、Pandas、MATLAB 等），必須使用 tech 參數進行精確過濾，不可只靠語意搜尋。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":        {"type": "string",  "description": "搜尋關鍵詞或自然語言描述（必填）"},
                    "dept":         {"type": "string",  "description": "限縮開課系所，如「資訊工程學系」。使用者說「XX系開的課」才加"},
                    "college":      {"type": "string",  "description": "限縮開課學院，如「資訊電機學院」。使用者說「XX學院開的課」才加"},
                    "student_college": {"type": "string",  "description": "學生所屬學院，只回傳該學院學生可修的課（含全校開放課程）。使用者說「我是XX學院的學生」「XX學院學生可以修哪些」才加。支援別名，如「資電學院」→「資訊電機學院」"},
                    "course_type":  {"type": "string",  "enum": ["必修", "選修"], "description": "課程性質，使用者明確說必修/選修才加"},
                    "tech":              {"type": "string",  "description": "技術或工具名稱，如「PyTorch」「Python」"},
                    "topic_tag":         {"type": "string",  "description": "通識課主題標籤（僅適用於通識課，課號含 CC/GS 前綴）。可用值：工程、化學、心理學、文學、永續發展、生物、全球化、宗教、性別研究、法律、物理、社會學、政治、音樂、倫理學、哲學、族群文化、經濟、資訊科技、語言學、數學、歷史、環境科學、醫學、藝術"},
                    "is_grad":           {"type": "boolean", "description": "true=搜尋研究所課程"},
                    "exclude_grad_only": {"type": "boolean", "description": "true（預設）=排除限研究所才能修的課程；false=顯示全部"},
                    "n":                 {"type": "integer", "description": "回傳筆數（預設 16）"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dept_courses",
            "description": "從知識圖譜查詢某系所的必修或選修課程（結構化資料，比向量搜尋更精確）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dept_name":   {"type": "string", "description": "系所名稱，如「資訊工程學系」"},
                    "course_type": {"type": "string", "enum": ["required", "elective", "all"],
                                   "description": "required=必修，elective=選修，all=全部（預設 required）"},
                },
                "required": ["dept_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_teacher_info",
            "description": "查詢特定教師的官方專長（教育部申報）與開課清單。",
            "parameters": {
                "type": "object",
                "properties": {
                    "teacher_name": {"type": "string", "description": "教師姓名"},
                },
                "required": ["teacher_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_teachers",
            "description": "依研究領域或專長關鍵詞語意搜尋教師，適合「哪位教授專長是 NLP」此類查詢。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",  "description": "研究領域或專長描述"},
                    "n":     {"type": "integer", "description": "回傳筆數（預設 5）"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_graduation_requirements",
            "description": "查詢某系所的畢業規定：同時回傳結構化學分要求（最低學分、必修學分、認證要求清單）與完整原文說明。整合原有兩個工具，一次呼叫即可。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dept_name": {"type": "string", "description": "系所名稱，如「資訊工程學系」「大氣科學學系」"},
                },
                "required": ["dept_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_course_detail",
            "description": "查詢課程完整資訊：官方課綱（課程目標、授課內容、教科書）＋修課資格分發條件原文（含年級/系所限制與先修要求）。一次呼叫取代原有三個工具（get_course_syllabus + get_course_eligibility + get_prereq_info）。同名多科系時回傳 ambiguous=True + candidates，可搭配 dept 或 course_code 精確查詢。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_zh":     {"type": "string", "description": "課程中文名稱，如「演算法」「機器學習」"},
                    "dept":        {"type": "string", "description": "指定系所以消歧義，如「資訊工程學系」"},
                    "course_code": {"type": "string", "description": "課號，精確查詢優先使用"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_programs",
            "description": "語意搜尋學分學程，依描述、主題或學院找最相關的學程清單。適合「有沒有 AI 相關的學程？」「管理學院有哪些學程？」等發現型查詢。找到後可用 get_program_description / get_program_courses 取得詳情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "學程主題或描述，如「人工智慧」「語言文化」「永續環境」"},
                    "n":     {"type": "integer", "description": "回傳筆數（預設 20）；查詢某學院所有學程時建議使用預設值"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_program_info",
            "description": "查詢學分學程的完整說明（目標、修課方式、學程特色）與必/選修課程清單。整合原有 get_program_description + get_program_courses，一次呼叫取得全部。知道學程名稱時優先使用此工具；不知道名稱時先用 search_programs 發現。",
            "parameters": {
                "type": "object",
                "properties": {
                    "program_name": {"type": "string", "description": "學程名稱，如「人工智慧技術應用」「客語教學學分學程」"},
                },
                "required": ["program_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_similar_courses",
            "description": "透過知識圖譜 Concept 節點找與指定課程概念最相近的跨系課程。適合「有沒有類似 OO 的課」、「跨系有沒有教 OO 的課」等推薦類查詢。",
            "parameters": {
                "type": "object",
                "properties": {
                    "course_name": {"type": "string", "description": "課程名稱，如「機器學習」「演算法」"},
                },
                "required": ["course_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_course_knowledge_map",
            "description": "回傳一門課的知識地圖：它涵蓋的學術概念、使用的技術工具，以及概念重疊最高的跨系相似課程。適合「演算法在教什麼？」「機器學習和哪些課最像？」等探索式問題。",
            "parameters": {
                "type": "object",
                "properties": {
                    "course_name": {"type": "string", "description": "課程名稱，如「演算法」「機器學習」「深度學習」"},
                },
                "required": ["course_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_depts_by_tech",
            "description": "多跳圖查詢：哪些系所的課程有教某技術或概念？區分必修與選修。適合「什麼科系需要學 Python？」「哪些系重視機器學習？」等問題。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tech_name": {"type": "string", "description": "技術或概念名稱，如「Python」「機器學習」「SQL」"},
                },
                "required": ["tech_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ppr_explore",
            "description": "Personalized PageRank 廣泛探索：從概念/課程出發，找知識圖譜中最相關的節點（可跨課程、教師、系所）。種子策略自動複合（無需指定種子類型）。適合「和 AI 相關的一切有哪些？」「深度學習連結到哪些老師和系所？」等廣泛探索。⚠️ 不適合直接查詢某門具體課程：若使用者問「總體經濟學是什麼」「有沒有自然語言處理的課」，請改用 get_course_detail 或 search_courses。",
            "parameters": {
                "type": "object",
                "properties": {
                    "seed":  {"type": "string", "description": "起始概念或課程名稱，可用逗號分隔多個，如「機器學習」或「機器學習,深度學習」"},
                    "focus": {"type": "string", "enum": ["course", "instructor", "dept", "overview"],
                              "description": "回傳節點類型（預設 course）：course=課程；instructor=教師；dept=系所；overview=課程＋教師＋系所＋學程分組顯示（全貌探索時使用）"},
                    "top_k": {"type": "integer", "description": "回傳數量（預設 15）"},
                },
                "required": ["seed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_concept_neighborhood",
            "description": "以語意概念詞彙為入口，在知識圖譜做 N 跳 BFS 鄰域探索，找出直接覆蓋此概念的課程。適合「有哪些課涵蓋神經網路？」「學卷積神經網路要修哪些課？」等需要精確概念鄰域的問題（比 ppr_explore 更精準，比 search_courses 更廣）。需先執行 build_qdrant_index.py；未建立時自動退回字串比對。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",  "description": "概念或技術關鍵詞，如「神經網路」「資料視覺化」「強化學習」"},
                    "hops":  {"type": "integer", "description": "BFS 跳數（1-3，預設 2）"},
                    "top_k": {"type": "integer", "description": "回傳課程數量（預設 15）"},
                },
                "required": ["query"],
            },
        },
    },
]
