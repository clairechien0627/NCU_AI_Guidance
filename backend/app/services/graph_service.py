"""
graph_service.py

從 knowledge_graph.json 提供結構化圖查詢。
Graph schema（實際邊 relation）：
  HAS_CURRICULUM    — Dept/CBP/Track → CurriculumPlan
  REQUIRES          — CurriculumPlan → Course（系所必修）
  HAS_ELECTIVE_GROUP — CurriculumPlan → ElectiveGroup
  OFFERS_ELECTIVE   — ElectiveGroup/Slot → Course（選修選項）
  HAS_SLOT          — ElectiveGroup → Slot
  PROGRAM_REQUIRES  — CreditProgram → Course（學程必修）
  PROGRAM_OFFERS    — CreditProgram → ElectiveGroup（學程選修群）
  REQUIRES_SLOT     — CreditProgram → Slot
  TAUGHT_BY         — Course → Instructor
  HAS_CREDIT_PROGRAM — College → CreditProgram
  REQUIRES_PROGRAM  — Dept/CBP → CreditProgram（畢業必須完成學程）
"""

import json
import statistics
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent
GRAPH_JSON        = ROOT / "data" / "processed" / "graph" / "knowledge_graph.json"
GRAPH_IGRAPH      = ROOT / "data" / "processed" / "graph" / "knowledge_graph.pkl"
SCHEDULE_DIR      = ROOT / "data" / "processed" / "schedule_draft"
_DEPT_ALIASES_PATH = ROOT / "data" / "processed" / "dept_aliases.json"
QDRANT_DIR   = ROOT / "data" / "processed" / "qdrant_data"

try:
    import igraph as ig
    _IGRAPH_AVAILABLE = True
except ImportError:
    _IGRAPH_AVAILABLE = False

# 邊權重表（用於 igraph Weighted PPR）
_COVERS_FIELD_W: dict[str, float] = {"high": 1.5, "medium": 1.0, "low": 0.5}
_REL_WEIGHT: dict[str, float] = {
    "COVERS": 1.0,
    "TEACHES": 1.2,
    "PREREQUISITE_OF": 0.8,
    "EXPERT_IN": 1.0,
    "RELEVANT_EXPERT": 1.0,
    "COURSE_EXPERT": 0.8,
    "TAGGED_AS": 0.5,
    "IN_DOMAIN": 0.5,
    "DEVELOPS": 0.3,
}
_DEFAULT_EDGE_WEIGHT = 0.3


@lru_cache(maxsize=1)
def _load_graph() -> dict:
    with open(GRAPH_JSON, encoding="utf-8") as f:
        raw = json.load(f)

    nodes: dict[str, dict] = {n["id"]: n for n in raw["nodes"]}
    edges: list[dict] = raw.get("links", raw.get("edges", []))

    # 建立出向鄰接表 {src: [(tgt, relation)]}
    out_adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    # 建立反向索引 {tgt: [(src, relation)]}
    in_adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    # 邊屬性查詢 {(src, tgt): edge_dict}（用於取 level_num 等屬性）
    edge_attrs: dict[tuple[str, str], dict] = {}

    for e in edges:
        src, tgt, rel = e["source"], e["target"], e["relation"]
        if src in out_adj:
            out_adj[src].append((tgt, rel))
        if tgt in in_adj:
            in_adj[tgt].append((src, rel))
        edge_attrs[(src, tgt)] = e

    return {"nodes": nodes, "edges": edges, "edge_attrs": edge_attrs, "out": out_adj, "in": in_adj}


def _g():
    return _load_graph()



@lru_cache(maxsize=1)
def _load_igraph():
    """建立 igraph（含預計算邊權重）。

    優先從 knowledge_graph.pkl（build_graph.py 輸出的快取）載入，
    快取不存在時從 knowledge_graph.json 動態建圖。
    回傳 (G_ig, id_to_idx, idx_to_id)；若 igraph 未安裝則回傳 (None, {}, {})。
    SIMILAR_TO 雙向邊去重（只保留一條），PPR 設 directed=False 自動雙向遍歷。
    """
    if not _IGRAPH_AVAILABLE:
        return None, {}, {}

    # 快速路徑：從預計算的 igraph pickle 載入
    if GRAPH_IGRAPH.exists():
        G_ig = ig.Graph.Read_Pickle(str(GRAPH_IGRAPH))
        id_to_idx: dict[str, int] = {v["name"]: v.index for v in G_ig.vs}
        idx_to_id: dict[int, str] = {v.index: v["name"] for v in G_ig.vs}
    else:
        raw = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
        node_list = raw["nodes"]
        edge_list  = raw.get("links", raw.get("edges", []))

        id_to_idx = {n["id"]: i for i, n in enumerate(node_list)}
        idx_to_id = {i: n["id"] for i, n in enumerate(node_list)}

        G_ig = ig.Graph(directed=True)
        G_ig.add_vertices(len(node_list))
        for i, n in enumerate(node_list):
            G_ig.vs[i]["name"]      = n["id"]
            G_ig.vs[i]["node_type"] = n.get("node_type", "")
            G_ig.vs[i]["node_name"] = n.get("name", "")
            G_ig.vs[i]["dept"]      = n.get("dept", "")
            G_ig.vs[i]["credits"]   = n.get("credits")

        edge_tuples: list[tuple] = []
        weights: list[float]     = []
        relations: list[str]     = []
        seen_similar: set[tuple] = set()

        for e in edge_list:
            si = id_to_idx.get(e["source"])
            ti = id_to_idx.get(e["target"])
            if si is None or ti is None:
                continue
            rel = e.get("relation", "")
            if rel == "SIMILAR_TO":
                pair = (min(si, ti), max(si, ti))
                if pair in seen_similar:
                    continue
                seen_similar.add(pair)
                w = float(e.get("weight", 0.8))
            elif rel == "COVERS_FIELD":
                w = _COVERS_FIELD_W.get(e.get("relevance", "medium"), 1.0)
            else:
                w = _REL_WEIGHT.get(rel, _DEFAULT_EDGE_WEIGHT)
            edge_tuples.append((si, ti))
            weights.append(w)
            relations.append(rel)

        G_ig.add_edges(edge_tuples)
        G_ig.es["weight"]   = weights
        G_ig.es["relation"] = relations

    return G_ig, id_to_idx, idx_to_id


def _node(nid: str) -> dict:
    return _g()["nodes"].get(nid, {})


def _find_nodes_by(attr: str, value: str, node_type: str = None) -> list[str]:
    """找所有 attr==value 的節點 id（可加 node_type 過濾）"""
    results = []
    for nid, n in _g()["nodes"].items():
        if n.get(attr) == value:
            if node_type is None or n.get("node_type") == node_type:
                results.append(nid)
    return results


# ── 公開查詢 API ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_dept_aliases() -> dict[str, str]:
    """載入系所別名對照表（縮寫 → 正式全名）。"""
    if _DEPT_ALIASES_PATH.exists():
        return json.loads(_DEPT_ALIASES_PATH.read_text(encoding="utf-8"))
    return {}


def _find_dept_like(dept_name: str) -> list[str]:
    """找系所節點，依序嘗試：精確比對 → 別名查找 → 子字串比對。"""
    dept_types = {"Department", "DeptGroup", "CollegeBachelorProgram"}

    def _match_name(name: str) -> list[str]:
        return [
            nid for nid, n in _g()["nodes"].items()
            if n.get("node_type") in dept_types and n.get("name") == name
        ]

    # 1. 精確比對
    exact = _match_name(dept_name)
    if exact:
        return exact

    # 2. 別名查找
    canonical = _load_dept_aliases().get(dept_name)
    if canonical:
        via_alias = _match_name(canonical)
        if via_alias:
            return via_alias

    # 3. 子字串包含比對
    substring = [
        nid for nid, n in _g()["nodes"].items()
        if n.get("node_type") in dept_types and dept_name in n.get("name", "")
    ]
    if substring:
        return substring

    # ncu_departments 的資料內容與向量策略尚在審查中，不允許 agent
    # 透過系所名稱解析間接查詢該 collection。名稱解析僅使用圖與別名表。
    return []


def _collect_courses_from_plan(plan_id: str, relation_label: str) -> list[dict]:
    """
    從 CurriculumPlan 出發，收集直接 REQUIRES 課程。
    relation_label：供輸出用，如「必修」「選修」。
    """
    g = _g()
    results = []
    for tgt, rel in g["out"].get(plan_id, []):
        if rel == "REQUIRES":
            n = _node(tgt)
            if n.get("node_type") == "Course":
                edge = g["edge_attrs"].get((plan_id, tgt), {})
                results.append({
                    "id":       tgt,
                    "name":     n.get("name", tgt),
                    "credits":  n.get("credits"),
                    "relation": relation_label,
                    "when_raw": edge.get("when_raw", ""),
                    "_sort_key": edge.get("when_year_start", 9),
                })
    results.sort(key=lambda x: x.pop("_sort_key"))
    return results


def _collect_electives_from_plan(plan_id: str) -> list[dict]:
    """
    從 CurriculumPlan 出發，透過 HAS_ELECTIVE_GROUP → OFFERS_ELECTIVE (+ HAS_SLOT) 收集選修課。
    """
    results = []
    for eg_id, rel1 in _g()["out"].get(plan_id, []):
        if rel1 != "HAS_ELECTIVE_GROUP":
            continue
        # 直接 OFFERS_ELECTIVE
        for c_id, rel2 in _g()["out"].get(eg_id, []):
            if rel2 == "OFFERS_ELECTIVE":
                n = _node(c_id)
                if n.get("node_type") == "Course":
                    results.append({
                        "id": c_id,
                        "name": n.get("name", c_id),
                        "credits": n.get("credits"),
                        "relation": "選修",
                    })
            elif rel2 == "HAS_SLOT":
                for c_id2, rel3 in _g()["out"].get(c_id, []):
                    if rel3 == "OFFERS_ELECTIVE":
                        n = _node(c_id2)
                        if n.get("node_type") == "Course":
                            results.append({
                                "id": c_id2,
                                "name": n.get("name", c_id2),
                                "credits": n.get("credits"),
                                "relation": "選修",
                            })
    return results


def _iter_dept_plans(did: str) -> list[str]:
    """回傳一個 Dept/DeptGroup 節點下所有 CurriculumPlan 的 ID，含 HAS_GROUP 子組。"""
    plan_ids: list[str] = []
    for tgt, rel in _g()["out"].get(did, []):
        if rel == "HAS_CURRICULUM":
            plan_ids.append(tgt)
        elif rel == "HAS_GROUP":
            for tgt2, rel2 in _g()["out"].get(tgt, []):
                if rel2 == "HAS_CURRICULUM":
                    plan_ids.append(tgt2)
    return plan_ids


def get_dept_domain_profile(dept_name: str) -> dict:
    """統計某系所必修課與選修課的領域分布，回傳 top-5 domain 頻率。"""
    from collections import Counter
    dept_ids = _find_dept_like(dept_name)
    domain_counter: Counter = Counter()
    course_count = 0
    g = _g()
    seen: set[str] = set()
    for did in dept_ids:
        for plan_id in _iter_dept_plans(did):
            for tgt, rel in g["out"].get(plan_id, []):
                # 圖中必修邊可能為 "REQUIRES" 或 "REQUIRED"，兩者都處理
                if rel in ("REQUIRED", "REQUIRES", "OFFERS_ELECTIVE"):
                    if tgt in seen:
                        continue
                    node = g["nodes"].get(tgt, {})
                    if node.get("node_type") == "Course":
                        seen.add(tgt)
                        course_count += 1
                        for d in node.get("domains", []):
                            if d:
                                domain_counter[d] += 1
                elif rel == "HAS_ELECTIVE_GROUP":
                    for tgt2, rel2 in g["out"].get(tgt, []):
                        if rel2 in ("OFFERS_ELECTIVE", "REQUIRES", "REQUIRED"):
                            if tgt2 in seen:
                                continue
                            node2 = g["nodes"].get(tgt2, {})
                            if node2.get("node_type") == "Course":
                                seen.add(tgt2)
                                course_count += 1
                                for d in node2.get("domains", []):
                                    if d:
                                        domain_counter[d] += 1
    top_domains = [{"domain": d, "count": c} for d, c in domain_counter.most_common()]
    return {"course_count": course_count, "top_domains": top_domains}


def get_dept_required_courses(dept_name: str) -> list[dict]:
    """回傳某系所的必修課程 list（支援 Dept → HAS_GROUP → DeptGroup → CurriculumPlan）"""
    dept_ids = _find_dept_like(dept_name)
    results = []
    seen: set[str] = set()
    for did in dept_ids:
        for plan_id in _iter_dept_plans(did):
            for course in _collect_courses_from_plan(plan_id, "必修"):
                if course["id"] not in seen:
                    seen.add(course["id"])
                    results.append(course)
    return results


def get_dept_elective_courses(dept_name: str) -> list[dict]:
    """回傳某系所的選修課程 list（支援 HAS_GROUP 子組）"""
    dept_ids = _find_dept_like(dept_name)
    results = []
    seen: set[str] = set()
    for did in dept_ids:
        for plan_id in _iter_dept_plans(did):
            for course in _collect_electives_from_plan(plan_id):
                if course["id"] not in seen:
                    seen.add(course["id"])
                    results.append(course)
    return results


def get_program_courses(program_name: str) -> list[dict]:
    """回傳學分學程的必/選修課程"""
    prog_ids = _find_nodes_by("name", program_name, "CreditProgram")
    if not prog_ids:
        prog_ids = [
            nid for nid, n in _g()["nodes"].items()
            if n.get("node_type") == "CreditProgram" and program_name in n.get("name", "")
        ]
    results = []
    seen: set[str] = set()
    for pid in prog_ids:
        for tgt, rel in _g()["out"].get(pid, []):
            if rel == "PROGRAM_REQUIRES":
                n = _node(tgt)
                if n.get("node_type") == "Course" and tgt not in seen:
                    seen.add(tgt)
                    results.append({
                        "id": tgt,
                        "name": n.get("name", tgt),
                        "credits": n.get("credits"),
                        "relation": "學程必修",
                    })
            elif rel == "PROGRAM_OFFERS":
                # ElectiveGroup → OFFERS_ELECTIVE
                for c_id, rel2 in _g()["out"].get(tgt, []):
                    if rel2 == "OFFERS_ELECTIVE":
                        n = _node(c_id)
                        if n.get("node_type") == "Course" and c_id not in seen:
                            seen.add(c_id)
                            results.append({
                                "id": c_id,
                                "name": n.get("name", c_id),
                                "credits": n.get("credits"),
                                "relation": "學程選修",
                            })
                    elif rel2 == "HAS_SLOT":
                        for c_id2, rel3 in _g()["out"].get(c_id, []):
                            if rel3 == "OFFERS_ELECTIVE":
                                n = _node(c_id2)
                                if n.get("node_type") == "Course" and c_id2 not in seen:
                                    seen.add(c_id2)
                                    results.append({
                                        "id": c_id2,
                                        "name": n.get("name", c_id2),
                                        "credits": n.get("credits"),
                                        "relation": "學程選修",
                                    })
            elif rel == "REQUIRES_SLOT":
                # Slot → OFFERS_ELECTIVE
                for c_id, rel2 in _g()["out"].get(tgt, []):
                    if rel2 == "OFFERS_ELECTIVE":
                        n = _node(c_id)
                        if n.get("node_type") == "Course" and c_id not in seen:
                            seen.add(c_id)
                            results.append({
                                "id": c_id,
                                "name": n.get("name", c_id),
                                "credits": n.get("credits"),
                                "relation": "學程必修（擇一）",
                            })
    return results


def get_teacher_courses(teacher_name: str) -> list[dict]:
    """回傳某教師所授的課程。

    查詢路徑（優先順序）：
    1. Course --[TAUGHT_BY]--> Instructor（走反向邊）
    2. Instructor --[COURSE_EXPERT]--> Course（NLP 分析的專長關聯課程，作為補充）
    """
    teacher_ids = _find_nodes_by("name", teacher_name, "Instructor")
    if not teacher_ids:
        teacher_ids = [
            nid for nid, n in _g()["nodes"].items()
            if n.get("node_type") == "Instructor" and teacher_name in n.get("name", "")
        ]
    results = []
    seen: set[str] = set()

    for tid in teacher_ids:
        # 路徑 1：TAUGHT_BY 反向邊（實際授課紀錄）
        for src, rel in _g()["in"].get(tid, []):
            if rel == "TAUGHT_BY" and src not in seen:
                seen.add(src)
                course_node = _node(src)
                results.append({
                    "id": src,
                    "name": course_node.get("name", src),
                    "credits": course_node.get("credits"),
                    "relation": "授課",
                })

    # 路徑 2：COURSE_EXPERT 出向邊（當 TAUGHT_BY 結果為空時，補充專長關聯）
    if not results:
        for tid in teacher_ids:
            for tgt, rel in _g()["out"].get(tid, []):
                if rel == "COURSE_EXPERT" and tgt not in seen:
                    seen.add(tgt)
                    course_node = _node(tgt)
                    if course_node.get("node_type") == "Course":
                        results.append({
                            "id": tgt,
                            "name": course_node.get("name", tgt),
                            "credits": course_node.get("credits"),
                            "relation": "專長相關",
                        })

    return results


def get_course_info(course_name_or_code: str) -> list[dict]:
    """用名稱（雙向模糊比對）或代碼查課程節點基本資訊"""
    results = []
    for nid, n in _g()["nodes"].items():
        if n.get("node_type") != "Course":
            continue
        name = n.get("name", "")
        code = n.get("code", "")
        if course_name_or_code == code:
            results.append({"id": nid, **n})
            continue
        # 雙向包含比對（排除空名稱）：query 包含課名 或 課名包含 query
        if name and (course_name_or_code in name or name in course_name_or_code):
            results.append({"id": nid, **n})
    return results


_2HOP_DISCOUNT = 0.5  # SIMILAR_TO 鄰居概念的分數折扣

def search_courses_by_concept_cluster(seed_course_name: str, top_n: int = 20) -> list[dict]:
    """
    給定課程名稱，透過共享 Concept/Technology 節點找最相似的跨系課程。

    步驟：
    1. 找 seed 課程的 COVERS / TEACHES 出向邊 → 直接概念集合（weight=1.0）
    2. 沿 SIMILAR_TO 邊擴展到鄰居概念（weight=0.5，2-hop）
    3. 走反向邊找到有共同概念的其他課程，加總 weight 排序
    4. 回傳 top_n（由 Qdrant RRF 過濾 2-hop 的低品質候選）
    """
    seed_courses = get_course_info(seed_course_name)
    if not seed_courses:
        return []

    seed_concepts: set[str] = set()
    for sc in seed_courses:
        for tgt, rel in _g()["out"].get(sc["id"], []):
            if rel in ("COVERS", "TEACHES"):
                seed_concepts.add(tgt)

    if not seed_concepts:
        return []

    # 2-hop 擴展：1-hop 概念 weight=1.0，SIMILAR_TO 鄰居 weight=0.5
    concept_weights: dict[str, float] = {cid: 1.0 for cid in seed_concepts}
    for cid in seed_concepts:
        for neighbor, rel in _g()["out"].get(cid, []):
            if rel == "SIMILAR_TO" and neighbor not in concept_weights:
                concept_weights[neighbor] = _2HOP_DISCOUNT
        for neighbor, rel in _g()["in"].get(cid, []):
            if rel == "SIMILAR_TO" and neighbor not in concept_weights:
                concept_weights[neighbor] = _2HOP_DISCOUNT

    seed_ids = {sc["id"] for sc in seed_courses}
    score: dict[str, float] = {}
    for concept_id, weight in concept_weights.items():
        for src, rel in _g()["in"].get(concept_id, []):
            if rel in ("COVERS", "TEACHES") and src not in seed_ids:
                if _node(src).get("node_type") == "Course":
                    score[src] = score.get(src, 0.0) + weight

    ranked = sorted(score.items(), key=lambda x: x[1], reverse=True)[:top_n]
    results = []
    for cid, cnt in ranked:
        n = _node(cid)
        results.append({
            "id":             cid,
            "name":           n.get("name", cid),
            "dept":           n.get("dept", ""),
            "credits":        n.get("credits"),
            "level":          n.get("level", ""),
            "shared_concepts": cnt,
        })
    return results


def search_courses_by_tech(tech_name: str) -> list[dict]:
    """
    Graph-first 技術查課：
    走 tech::{name} / concept::{name} 節點的反向邊（TEACHES / COVERS）找到所有課程。
    比向量搜尋更精確，不漏課。
    """
    results = []
    seen: set[str] = set()

    # 精確 id 查找
    candidate_ids = [
        f"tech::{tech_name}",
        f"concept::{tech_name}",
        f"field::{tech_name}",
    ]
    # 大小寫不同、簡稱等精確名稱比對
    lower = tech_name.lower()
    for nid, n in _g()["nodes"].items():
        if n.get("node_type") in ("Technology", "Concept", "Field"):
            if n.get("name", "").lower() == lower:
                candidate_ids.append(nid)

    # 向量搜尋補充（處理別名、縮寫，如 GIS → 地理資訊系統）
    candidate_ids.extend(_search_concept_nodes(tech_name, top_k=3))

    # 去重
    candidate_ids = list(dict.fromkeys(candidate_ids))

    COURSE_RELS = {"TEACHES", "COVERS", "COVERS_FIELD", "DEVELOPS"}
    for tech_nid in candidate_ids:
        if not _g()["nodes"].get(tech_nid):
            continue
        for src, rel in _g()["in"].get(tech_nid, []):
            if rel in COURSE_RELS and src not in seen:
                course_node = _node(src)
                if course_node.get("node_type") == "Course":
                    seen.add(src)
                    results.append({
                        "id": src,
                        "name": course_node.get("name", src),
                        "credits": course_node.get("credits"),
                        "dept": course_node.get("dept", ""),
                        "level": course_node.get("level", ""),
                        "tech_node": tech_nid,
                    })

    return results


def list_all_programs() -> list[dict]:
    """列出所有學分學程"""
    return [
        {"id": nid, "name": n.get("name", ""), "college": n.get("college", "")}
        for nid, n in _g()["nodes"].items()
        if n.get("node_type") == "CreditProgram"
    ]


def get_graduation_rules(dept_name: str) -> dict:
    """從 schedule_draft 取得系所畢業規定（模糊名稱比對）"""
    if not SCHEDULE_DIR.exists():
        return {}
    for college_dir in SCHEDULE_DIR.iterdir():
        if not college_dir.is_dir():
            continue
        for fp in college_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if dept_name in data.get("name", ""):
                    return {
                        "dept_name": data.get("name", ""),
                        "min_credits": data.get("min_credits", 0),
                        "required_credits": data.get("required_credits", 0),
                        "graduation_rules": data.get("graduation_rules", []),
                        "certifications": data.get("certifications", []),
                    }
            except Exception:
                pass
    return {}


def get_course_knowledge_map(course_name: str) -> dict:
    """
    回傳一門課的知識地圖：它在學什麼、使用什麼技術，以及概念重疊最多的跨系相似課程。

    回傳格式：
    {
      "course": {id, name, dept, credits},
      "concepts": [{name, node_type}],
      "technologies": [{name}],
      "similar_courses": [{name, dept, credits, shared_concepts}],
    }
    """
    courses = get_course_info(course_name)
    if not courses:
        return {}

    # 聚合多個同名課程的概念（如演算法有多個系開課）
    all_concept_ids: set[str] = set()
    all_tech_ids: set[str] = set()
    for sc in courses:
        for tgt, rel in _g()["out"].get(sc["id"], []):
            if rel == "COVERS":
                all_concept_ids.add(tgt)
            elif rel == "TEACHES":
                all_tech_ids.add(tgt)

    concepts = []
    for cid in all_concept_ids:
        n = _node(cid)
        concepts.append({"name": n.get("name", cid), "node_type": n.get("node_type", "")})

    technologies = []
    for tid in all_tech_ids:
        n = _node(tid)
        technologies.append({"name": n.get("name", tid)})

    similar = search_courses_by_concept_cluster(course_name, top_n=10)

    primary = courses[0]
    return {
        "course": {
            "id":      primary["id"],
            "name":    primary.get("name", ""),
            "dept":    primary.get("dept", ""),
            "credits": primary.get("credits"),
        },
        "concepts":        sorted(concepts, key=lambda x: x["name"]),
        "technologies":    technologies,
        "similar_courses": similar,
    }


# 中文技術名稱別名對照（lowercase 去空格 → 圖中正式節點名稱）
# 解決「R 語言」「Python語言」等自然語言輸入無法精確命中的問題
_TECH_ALIAS_MAP: dict[str, str] = {
    "r語言":             "R",
    "rlanguage":         "R",
    "r language":        "R",
    "python語言":        "Python",
    "c語言":             "C",
    "c++語言":           "C++",
    "c#語言":            "C#",
    "java語言":          "Java",
    "go語言":            "Go",
    "rust語言":          "Rust",
    "julia語言":         "Julia",
    "swift語言":         "Swift",
    "kotlin語言":        "Kotlin",
    "javascript語言":    "JavaScript",
    "typescript語言":    "TypeScript",
    "sql語言":           "SQL",
    "matlab語言":        "MATLAB",
    "fortran語言":       "Fortran",
    "haskell語言":       "Haskell",
    "scala語言":         "Scala",
    "perl語言":          "Perl",
    "ruby語言":          "Ruby",
    "php語言":           "PHP",
    "lua語言":           "Lua",
}

# 常見中文後綴，嘗試去除後重試（e.g., "R語言" → "R"）
_STRIP_SUFFIXES = ("程式語言", "語言", "程式", "軟體", "工具", "框架")


def _normalize_tech_name(name: str) -> str:
    """將使用者輸入的中文技術名稱正規化為圖中節點名稱。"""
    key = name.lower().replace(" ", "")
    if key in _TECH_ALIAS_MAP:
        return _TECH_ALIAS_MAP[key]
    return name


def get_depts_by_tech(tech_name: str) -> dict:
    """
    Multi-hop 查詢：哪些系所的課程有教某技術/概念？
    回傳以「匹配節點」為軸心的階層結構，LLM 可依 score 自行判斷哪些節點正確。

    路徑：
      tech/concept 節點 → 反向 TEACHES/COVERS → Course
      Course → 反向 REQUIRES ← CurriculumPlan → 反向 HAS_CURRICULUM ← Department
    """
    # 別名正規化（「R 語言」→「R」等中文慣用名稱）
    tech_name = _normalize_tech_name(tech_name)

    g = _g()
    COURSE_RELS = {"TEACHES", "COVERS", "COVERS_FIELD", "DEVELOPS"}
    dept_types = {"Department", "DeptGroup", "CollegeBachelorProgram"}
    SCORE_THRESHOLD = 0.65

    # 1. 精確字串比對（score=1.0），不受閾值限制
    candidate_with_scores: dict[str, float] = {}
    lower = tech_name.lower()
    for nid, n in g["nodes"].items():
        if n.get("node_type") in ("Technology", "Concept", "Field", "Competency"):
            if n.get("name", "").lower() == lower:
                candidate_with_scores[nid] = 1.0
    for prefix in ("tech", "concept", "field"):
        cid = f"{prefix}::{tech_name}"
        if g["nodes"].get(cid):
            candidate_with_scores[cid] = 1.0

    # 2. 語意向量搜尋補充（score ≥ 0.65，處理別名/縮寫）
    for nid, score in _search_concept_nodes_with_scores(tech_name, top_k=20):
        if score >= SCORE_THRESHOLD and nid not in candidate_with_scores:
            candidate_with_scores[nid] = score

    # 3. fallback：若仍無命中，嘗試去除常見中文後綴後重試
    if not candidate_with_scores:
        for suffix in _STRIP_SUFFIXES:
            if tech_name.endswith(suffix) and len(tech_name) > len(suffix):
                stripped = tech_name[: -len(suffix)].strip()
                if stripped:
                    for nid, n in g["nodes"].items():
                        if n.get("node_type") in ("Technology", "Concept", "Field", "Competency"):
                            if n.get("name", "").lower() == stripped.lower():
                                candidate_with_scores[nid] = 0.95
                    for nid, score in _search_concept_nodes_with_scores(stripped, top_k=10):
                        if score >= SCORE_THRESHOLD and nid not in candidate_with_scores:
                            candidate_with_scores[nid] = score * 0.95
                    if candidate_with_scores:
                        break

    if not candidate_with_scores:
        return {"tech": tech_name, "nodes": []}

    # 3. 對每個候選節點，個別追蹤其連結的課程與系所
    nodes_output = []
    for tech_nid, node_score in sorted(candidate_with_scores.items(), key=lambda x: -x[1]):
        nd = g["nodes"].get(tech_nid)
        if not nd:
            continue

        node_required_depts: dict[str, str] = {}
        node_elective_depts: dict[str, str] = {}
        courses_for_node: list[dict] = []

        for src, rel in g["in"].get(tech_nid, []):
            if rel not in COURSE_RELS:
                continue
            src_node = g["nodes"].get(src)
            if not src_node or src_node.get("node_type") != "Course":
                continue
            if "[已停開]" in src_node.get("name", ""):
                continue

            course_req_depts: list[str] = []
            course_elec_depts: list[str] = []

            for plan_id, rel2 in g["in"].get(src, []):
                plan_node = g["nodes"].get(plan_id)
                if not plan_node or plan_node.get("node_type") != "CurriculumPlan":
                    continue
                is_required = (rel2 == "REQUIRES")
                for dept_id, rel3 in g["in"].get(plan_id, []):
                    if rel3 != "HAS_CURRICULUM":
                        continue
                    dept_node = g["nodes"].get(dept_id)
                    if dept_node and dept_node.get("node_type") in dept_types:
                        dname = dept_node.get("name", dept_id)
                        if is_required:
                            node_required_depts[dname] = dept_id
                            course_req_depts.append(dname)
                        else:
                            node_elective_depts[dname] = dept_id
                            course_elec_depts.append(dname)

            # fallback：課程 dept 欄位（無法追溯 CurriculumPlan 時）
            if not course_req_depts and not course_elec_depts:
                cdept = src_node.get("dept", "")
                if cdept:
                    node_elective_depts[cdept] = cdept
                    course_elec_depts.append(cdept)

            courses_for_node.append({
                "name":        src_node.get("name", src),
                "dept":        src_node.get("dept", ""),
                "college":     src_node.get("college", ""),
                "required_in": sorted(set(course_req_depts)),
                "elective_in": sorted(set(course_elec_depts)),
            })

        if not courses_for_node:
            continue

        nodes_output.append({
            "node_name":     nd.get("name", tech_nid),
            "node_type":     nd.get("node_type", ""),
            "score":         node_score,
            "required_depts": sorted(node_required_depts.keys()),
            "elective_depts": sorted(node_elective_depts.keys()),
            "courses":        courses_for_node[:20],
        })

    return {"tech": tech_name, "nodes": nodes_output}


def _find_ppr_seeds(seed_names: list[str]) -> set[str]:
    """Exact-first 策略找 PPR 種子節點 ID。

    精確匹配優先；某個 seed 無精確匹配時才 fallback 到 substring。
    避免「演算法」substring 抓到 50+ 節點稀釋分數。

    Substring (fuzzy) 比對只允許 Concept/Technology/Field 節點，
    排除 Course 節點——防止「文學」substring 命中「水文學」Course 等語意無關的課程名稱。
    Course 節點只透過精確比對加入種子集合。
    """
    g = _g()
    _SEED_TYPES = {"Concept", "Technology", "Field", "Course"}
    _FUZZY_TYPES = {"Concept", "Technology", "Field"}   # Course 節點只做精確比對
    seed_ids: set[str] = set()
    for name in seed_names:
        nl = name.lower()
        exact: set[str] = set()
        fuzzy: set[str] = set()
        for nid, nd in g["nodes"].items():
            ntype = nd.get("node_type", "")
            if ntype not in _SEED_TYPES:
                continue
            nname = nd.get("name", "").lower()
            if not nname:
                continue
            if nl == nname:
                exact.add(nid)
            elif nl in nname and ntype in _FUZZY_TYPES:
                fuzzy.add(nid)
        seed_ids |= exact if exact else fuzzy
    return seed_ids


def _ppr_gap_filter(results: list[dict]) -> list[dict]:
    """移除 PPR 尾部噪音：score < mean − 0.5σ 的結果。
    結果少於 4 筆時不截斷（避免過度過濾）。
    """
    if len(results) < 4:
        return results
    scores = [r["score"] for r in results]
    mean = statistics.mean(scores)
    stdev = statistics.stdev(scores)
    threshold = mean - 0.5 * stdev
    filtered = [r for r in results if r["score"] >= threshold]
    return filtered if filtered else results


def ppr_explore(
    seed_names: list[str],
    top_k: int = 20,
    alpha: float = 0.85,
    n_iter: int = 25,
    node_type_filter: list[str] = None,
) -> list[dict]:
    """Personalized PageRank 探索：從種子節點出發做帶重置的隨機遊走。

    優先使用 igraph Weighted PPR（C 實作，SIMILAR_TO 邊自然納入，加權邊）。
    igraph 未安裝時退回 Python power iteration（原有行為）。

    seed_names:        概念/技術/課程名稱列表
    alpha:             延續前進的機率（1-alpha = 重置到種子的機率）
    node_type_filter:  若指定，只回傳特定類型的節點（如 ["Course", "Instructor"]）
    """
    g = _g()

    # ── 種子收集：按概念名稱分組，保留來源用於權重計算 ──────────────────────────
    # 每個概念名稱佔 1/n_concepts 的重置權重，再平均分給旗下節點。
    # 這樣「工程」(354 節點) 與「永續發展」(3 節點) 各佔 50% 影響力，
    # 而非讓節點數多的概念直接主導 PPR 擴散方向。
    _SEED_VECTOR_THRESHOLD = 0.65
    name_to_nodes: dict[str, set[str]] = {}
    for name in seed_names:
        # 向量語意種子：score ≥ 0.65，過濾語意不相關的候選（如「量子力學」不應成為「量子計算」的高權重種子）
        nodes: set[str] = {
            nid for nid, score in _search_concept_nodes_with_scores(name, top_k=8)
            if score >= _SEED_VECTOR_THRESHOLD
        }
        nodes |= _find_ppr_seeds([name])
        # 排除孤立 Field 節點（只有 EXPERT_IN/RELEVANT_EXPERT，沒有課程連接）
        nodes = {
            s for s in nodes
            if g["nodes"].get(s, {}).get("node_type") != "Field" or _has_course_edge(s)
        }
        if nodes:
            name_to_nodes[name] = nodes

    if not name_to_nodes:
        return []

    # 計算每個節點的重置權重（一個節點可能同時屬於多個概念，取權重加總）
    n_concepts = len(name_to_nodes)
    node_weights: dict[str, float] = {}
    for name, nodes in name_to_nodes.items():
        per_node = 1.0 / (n_concepts * len(nodes))
        for nid in nodes:
            node_weights[nid] = node_weights.get(nid, 0.0) + per_node

    seed_ids = set(node_weights.keys())
    allowed = set(node_type_filter) if node_type_filter else None

    # ── igraph Weighted PPR（優先）───────────────────────────────────────────
    if _IGRAPH_AVAILABLE:
        G_ig, id_to_idx, idx_to_id = _load_igraph()
        if G_ig is not None:
            seed_indices = [id_to_idx[s] for s in seed_ids if s in id_to_idx]
            if seed_indices:
                n_v = G_ig.vcount()
                reset = [0.0] * n_v
                for idx in seed_indices:
                    nid = idx_to_id.get(idx, "")
                    reset[idx] = node_weights.get(nid, 0.0)
                # igraph 要求 reset 總和為 1
                total_r = sum(reset)
                if total_r > 0:
                    reset = [r / total_r for r in reset]

                scores_vec = G_ig.personalized_pagerank(
                    directed=False,
                    damping=alpha,
                    reset=reset,
                    weights="weight",
                )

                ranked = sorted(enumerate(scores_vec), key=lambda x: -x[1])
                results: list[dict] = []
                for idx, score in ranked:
                    nid = idx_to_id.get(idx, "")
                    nd  = g["nodes"].get(nid, {})
                    ntype = nd.get("node_type", "")
                    if allowed and ntype not in allowed:
                        continue
                    results.append({
                        "id":        nid,
                        "name":      nd.get("name", nid),
                        "node_type": ntype,
                        "dept":      nd.get("dept", ""),
                        "credits":   nd.get("credits"),
                        "score":     round(score * 1000, 4),
                    })
                    if len(results) >= top_k:
                        break
                return _ppr_gap_filter(results)

    # ── Fallback：Python power iteration ─────────────────────────────────────
    total_w = sum(node_weights.values())
    norm_weights = {nid: w / total_w for nid, w in node_weights.items()}
    scores: dict[str, float] = dict(norm_weights)

    for _ in range(n_iter):
        new_s: dict[str, float] = {}
        for src, score in scores.items():
            nbrs: list[str] = []
            for tgt, _ in g["out"].get(src, []):
                nbrs.append(tgt)
            for orig, _ in g["in"].get(src, []):
                nbrs.append(orig)
            if not nbrs:
                continue
            spread = alpha * score / len(nbrs)
            for nb in nbrs:
                new_s[nb] = new_s.get(nb, 0.0) + spread
        for nid, w in norm_weights.items():
            new_s[nid] = new_s.get(nid, 0.0) + (1.0 - alpha) * w
        scores = new_s

    total = sum(scores.values()) or 1.0
    scores = {k: v / total for k, v in scores.items()}

    results = []
    for nid, score in sorted(scores.items(), key=lambda x: -x[1]):
        nd    = g["nodes"].get(nid, {})
        ntype = nd.get("node_type", "")
        if allowed and ntype not in allowed:
            continue
        results.append({
            "id":        nid,
            "name":      nd.get("name", nid),
            "node_type": ntype,
            "dept":      nd.get("dept", ""),
            "credits":   nd.get("credits"),
            "score":     round(score * 1000, 4),
        })
        if len(results) >= top_k:
            break
    return _ppr_gap_filter(results)


def explore_by_concept_neighborhood(
    query: str,
    hops: int = 2,
    top_k: int = 15,
) -> list[dict]:
    """以概念詞彙為中心，igraph BFS 遍歷 N 跳收集相關課程。

    入口策略：
    1. Qdrant ncu_graph_nodes 向量搜尋（回傳 Concept/Technology/Field 節點）
    2. Fallback：字串比對 Concept/Technology/Field 節點

    BFS 使用 igraph，邊類型白名單控制擴散範圍。
    igraph 未安裝時退回 JSON adjacency BFS。
    """
    TRAVERSE_RELS = {"COVERS", "TEACHES", "COVERS_FIELD", "SIMILAR_TO"}

    entry_ids: list[str] = _search_concept_nodes(query, top_k=5)
    if not entry_ids:
        return []

    g = _g()

    # ── igraph BFS（主路徑）────────────────────────────────────────────────────
    if _IGRAPH_AVAILABLE:
        G_ig, id_to_idx, idx_to_id = _load_igraph()
        if G_ig is not None:
            visited: set[int] = set()
            course_hits: dict[str, int] = {}
            queue: list[tuple[int, int]] = [
                (id_to_idx[nid], 0) for nid in entry_ids if nid in id_to_idx
            ]
            while queue:
                idx, depth = queue.pop(0)
                if idx in visited or depth > hops:
                    continue
                visited.add(idx)
                v = G_ig.vs[idx]
                if v["node_type"] == "Course":
                    nid = v["name"]
                    course_hits[nid] = course_hits.get(nid, 0) + (hops - depth + 1)
                if depth < hops:
                    for eid in G_ig.incident(idx, mode="ALL"):
                        e = G_ig.es[eid]
                        if e["relation"] not in TRAVERSE_RELS:
                            continue
                        nbr = e.source if e.target == idx else e.target
                        if nbr not in visited:
                            queue.append((nbr, depth + 1))

            results: list[dict] = []
            for nid, score in sorted(course_hits.items(), key=lambda x: -x[1])[:top_k]:
                nd = g["nodes"].get(nid, {})
                results.append({
                    "id":      nid,
                    "name":    nd.get("name", nid),
                    "dept":    nd.get("dept", ""),
                    "credits": nd.get("credits"),
                    "score":   score,
                })
            return results

    # ── Fallback：JSON adjacency BFS ──────────────────────────────────────────
    visited_fb: set[str] = set()
    course_hits_fb: dict[str, int] = {}
    queue_fb: list[tuple[str, int]] = [(nid, 0) for nid in entry_ids]
    while queue_fb:
        nid, depth = queue_fb.pop(0)
        if nid in visited_fb or depth > hops:
            continue
        visited_fb.add(nid)
        nd = g["nodes"].get(nid, {})
        if nd.get("node_type") == "Course":
            course_hits_fb[nid] = course_hits_fb.get(nid, 0) + (hops - depth + 1)
        if depth < hops:
            for tgt, rel in g["out"].get(nid, []):
                if rel in TRAVERSE_RELS and tgt not in visited_fb:
                    queue_fb.append((tgt, depth + 1))
            for src, rel in g["in"].get(nid, []):
                if rel in TRAVERSE_RELS and src not in visited_fb:
                    queue_fb.append((src, depth + 1))

    results_fb: list[dict] = []
    for nid, score in sorted(course_hits_fb.items(), key=lambda x: -x[1])[:top_k]:
        nd = g["nodes"].get(nid, {})
        results_fb.append({
            "id":      nid,
            "name":    nd.get("name", nid),
            "dept":    nd.get("dept", ""),
            "credits": nd.get("credits"),
            "score":   score,
        })
    return results_fb


_HAS_GRAPH_NODES: bool | None = None  # None = 未檢查


def _search_concept_nodes(query: str, top_k: int = 5) -> list[str]:
    """找與 query 語意相近的 Concept/Technology/Field 節點 ID。

    優先使用 Qdrant ncu_graph_nodes 向量搜尋（如已建立）；
    否則退回字串比對。
    使用 retriever 的共用 client（lru_cache），避免重複開啟 Qdrant 檔案。
    """
    global _HAS_GRAPH_NODES
    try:
        from app.services.retriever import _get_qdrant, _embed
        client = _get_qdrant()
        if _HAS_GRAPH_NODES is None:
            cols = {c.name for c in client.get_collections().collections}
            _HAS_GRAPH_NODES = "ncu_graph_nodes" in cols
        if _HAS_GRAPH_NODES:
            q_vec = _embed(query)
            result = client.query_points("ncu_graph_nodes", query=q_vec, limit=top_k, with_payload=True)
            return [h.payload["node_id"] for h in result.points if h.payload.get("node_id")]
    except Exception:
        pass

    # Fallback：字串比對
    g = _g()
    ENTRY_TYPES = {"Concept", "Technology", "Field", "Competency"}
    ql = query.lower()
    exact: list[str] = []
    fuzzy: list[str] = []
    for nid, nd in g["nodes"].items():
        if nd.get("node_type") not in ENTRY_TYPES:
            continue
        nname = nd.get("name", "").lower()
        if ql == nname:
            exact.append(nid)
        elif ql in nname or nname in ql:
            fuzzy.append(nid)
    return (exact or fuzzy)[:top_k]


def _search_concept_nodes_with_scores(query: str, top_k: int = 20) -> list[tuple[str, float]]:
    """_search_concept_nodes 的帶分數版本，回傳 (node_id, score) list。
    score 為向量相似度（0~1，越高越相關）。
    用於動態 threshold 篩選與 RRF 加權。
    """
    global _HAS_GRAPH_NODES
    try:
        from app.services.retriever import _get_qdrant, _embed
        client = _get_qdrant()
        if _HAS_GRAPH_NODES is None:
            cols = {c.name for c in client.get_collections().collections}
            _HAS_GRAPH_NODES = "ncu_graph_nodes" in cols
        if _HAS_GRAPH_NODES:
            q_vec = _embed(query)
            result = client.query_points("ncu_graph_nodes", query=q_vec, limit=top_k, with_payload=True)
            return [
                (h.payload["node_id"], round(float(h.score), 4))
                for h in result.points
                if h.payload.get("node_id")
            ]
    except Exception:
        pass

    # Fallback：字串比對（exact=1.0, fuzzy=0.5）
    g = _g()
    ENTRY_TYPES = {"Concept", "Technology", "Field", "Competency"}
    ql = query.lower()
    exact: list[tuple[str, float]] = []
    fuzzy: list[tuple[str, float]] = []
    for nid, nd in g["nodes"].items():
        if nd.get("node_type") not in ENTRY_TYPES:
            continue
        nname = nd.get("name", "").lower()
        if ql == nname:
            exact.append((nid, 1.0))
        elif ql in nname or nname in ql:
            fuzzy.append((nid, 0.5))
    return (exact or fuzzy)[:top_k]


def _has_course_edge(nid: str) -> bool:
    """True if the node has at least one incoming edge from a Course node."""
    COURSE_RELS = {"COVERS_FIELD", "TEACHES", "COVERS", "DEVELOPS"}
    return any(
        rel in COURSE_RELS
        and _g()["nodes"].get(src, {}).get("node_type") == "Course"
        for src, rel in _g()["in"].get(nid, [])
    )


def get_course_tags(course_id: str) -> dict:
    """回傳一門課的語意標籤（純記憶體遍歷，無 I/O）。

    回傳欄位：
      concepts, technologies, field_tags, topic_tags, core_questions（list[str]）
      competencies（list[dict]，含 name/level_num/level_label）
      course_domain（str，以 ・ 連結 domains list）
    """
    g = _g()
    concepts, techs, fields, topics = [], [], [], []
    competencies: list[dict] = []

    for target_id, rel in g["out"].get(course_id, []):
        nd = g["nodes"].get(target_id, {})
        name = nd.get("name", "")
        if not name:
            continue
        ntype = nd.get("node_type", "")
        if rel == "COVERS" and ntype == "Concept":
            concepts.append(name)
        elif rel == "TEACHES" and ntype == "Technology":
            techs.append(name)
        elif rel == "COVERS_FIELD" and ntype == "Field":
            fields.append(name)
        elif rel == "TAGGED_AS" and ntype == "Domain":
            topics.append(name)
        elif rel == "DEVELOPS" and ntype == "Competency":
            edge_data = g["edge_attrs"].get((course_id, target_id), {})
            competencies.append({
                "name":        name,
                "level_num":   edge_data.get("level_num", 0),
                "level_label": edge_data.get("level_label", ""),
            })

    course_nd = g["nodes"].get(course_id, {})
    return {
        "concepts":       concepts,
        "technologies":   techs,
        "field_tags":     fields,
        "topic_tags":     topics,
        "core_questions": course_nd.get("core_questions", []),
        "course_domain":  " ・ ".join(course_nd.get("domains", [])),
        "competencies":   competencies,
    }


def list_all_departments() -> list[dict]:
    """列出所有系所"""
    return [
        {"id": nid, "name": n.get("name", ""), "college": n.get("college", "")}
        for nid, n in _g()["nodes"].items()
        if n.get("node_type") in ("Department", "DeptGroup", "CollegeBachelorProgram")
    ]


