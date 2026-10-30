# 工具詳細設計（13 個啟用工具，另 1 個暫停）

> 文件版本：2026-05-02（更新：get_program_info 整合）  
> 來源：`backend/app/services/tools.py`  
> 向量資料庫：Qdrant（本地模式，`data/processed/qdrant_data/`）  
> 圖計算：igraph（PPR / BFS）+ JSON adjacency walk（其餘）

---

## 工具總覽

| # | 工具名稱 | 資料源 | 分數欄位 | 主要用途 |
|---|---------|--------|---------|---------|
| 1 | `search_courses` | Qdrant 向量（3 層 Hybrid：query expansion + RRF）+ 圖（tech-first） | `distance` | 語意搜尋課程；tech 參數走圖精確查詢 |
| 2 | `get_course_detail` | Qdrant payload + course_eligibility.json | 無 | 課綱（目標/內容/教科書）+ 修課資格分發條件（整合版） |
| 3 | `get_dept_courses` | 知識圖譜（→ Qdrant fallback） | 無 | 查詢系所必/選修 |
| — | `get_dept_info`（暫停） | Qdrant 向量 | — | `ncu_departments` 審查完成前不暴露給 agent |
| 5 | `get_graduation_requirements` | schedule_draft JSON + requirements_notes.json | 無 | 畢業規定（整合版） |
| 6 | `get_program_info` | program_descriptions.json + 知識圖譜 | 無 | 學程說明 + 必/選修課程（整合版） |
| 7 | `search_programs` | Qdrant 向量 | `distance` | 語意搜尋學分學程 |
| 8 | `get_teacher_info` | Qdrant + 知識圖譜 | 無 | 查詢教師詳情 |
| 9 | `search_teachers` | Qdrant 向量 | `distance` | 語意搜尋教師 |
| 10 | `get_course_knowledge_map` | 知識圖譜 + RRF（相似課程段落） | `shared_concepts` | 知識地圖探索 |
| 11 | `find_similar_courses` | 知識圖譜 + Qdrant 向量（RRF 融合） | RRF score | 找相似課程 |
| 12 | `get_depts_by_tech` | 知識圖譜 | 無 | 查詢技術分布系所 |
| 13 | `ppr_explore` | 知識圖譜 igraph PPR | `score` (×1000) | 廣泛圖探索 |
| 14 | `explore_concept_neighborhood` | Qdrant `ncu_graph_nodes` + igraph BFS | 無 | 概念鄰域精確探索 |

> **向下相容保留（`_TOOL_MAP` 有，`TOOLS` schema 無）**：  
> `get_graduation_rules`, `get_requirements_notes`, `get_program_courses`, `get_program_description`  
> **已整合移除**：`get_course_syllabus`、`get_course_eligibility`、`get_prereq_info` → 合併為 `get_course_detail`。  
> **2026-05-02 整合**：`get_program_description` + `get_program_courses` → 合併為 `get_program_info`。

---

## Qdrant Collections 對應

| Collection | 工具使用 | 維度 |
|-----------|---------|------|
| `ncu_courses_ug` | search_courses, get_course_detail, get_dept_courses fallback | 3072 |
| `ncu_courses_grad` | search_courses (is_grad=True), get_course_detail | 3072 |
| `ncu_credit_programs` | search_programs | 3072 |
| `ncu_departments` | get_dept_info | 3072 |
| `ncu_teachers` | search_teachers, get_teacher_info | 3072 |
| `ncu_graph_nodes` | explore_concept_neighborhood, ppr_explore（種子查找）, search_courses（Layer 1 query expansion） | 3072 |

---

## 詳細說明

### 1. `search_courses` — 語意搜尋課程

**功能**：以自然語言語意搜尋課程，支援多種過濾條件。若傳入 `tech` 參數，優先走圖的精確技術查詢（graph-first），再補向量結果。

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `query` | string | ✓ | — | 搜尋關鍵詞或自然語言描述 |
| `dept` | string | | — | 限縮系所，如「大氣科學學系」 |
| `college` | string | | — | 限縮學院，如「理學院」 |
| `course_type` | string | | — | `"必修"` 或 `"選修"` |
| `tech` | string | | — | 技術/工具名稱，如 `"Python"`；觸發 graph-first 路徑 |
| `is_grad` | boolean | | `false` | `true`=搜尋研究所課程 |
| `exclude_grad_only` | boolean | | `true` | `true`=排除僅限研究所課程 |
| `n` | integer | | `8` | 回傳筆數 |

> **為何移除 `year`/`sem`**：建議修習時間是系所相對資訊（`when` 欄位已含科系脈絡），若不同時指定 `dept`，按年級過濾會撈出所有科系大一的課，語意混亂。年級相關查詢應改用 `get_dept_courses` 或 `get_graduation_requirements`。

**資料流**

```
[有 tech 參數]
  → graph_service.search_courses_by_tech(tech)     ← 知識圖譜精確比對（graph-first）

[無 tech 參數] — 三層 Hybrid 流程
  Layer 1：Query Expansion
    → _search_concept_nodes(query, top_k=3)         ← Qdrant ncu_graph_nodes 向量
    → 提取概念節點 name，拼接 expanded_query

  Layer 2：Multi-signal Retrieval
    Signal A：retriever.search_courses(expanded_query, filters, n_results=n×2)
    Signal B：retriever.search_courses(original_query, filters, n_results=n×2)
              （expanded ≠ original 時才執行；保留原始語意）

  Layer 3：RRF Fusion（k=60）
    → 按 course_code 合併 Signal A + B 的排名分數
    → 取前 n 筆回傳
```

**工具選用指引**

| 情境 | 做法 |
|------|------|
| 主題式查詢（「通識有法律相關嗎」） | `search_courses(query="法律", dept="通識教育中心")` |
| 廣泛列舉（「通識有哪些選修」） | `get_dept_courses("通識教育中心", course_type="elective")` |
| 技術課程（「有哪些教 Python 的課」） | `search_courses(query="程式設計", tech="Python")` |

**回傳格式**（`list[dict]`）

```json
[
  {
    "course_code": "CS1001",
    "name_zh": "資料結構",
    "name_en": "Data Structures",
    "dept": "資訊工程學系",
    "college": "資訊電機學院",
    "credits": 3,
    "type": "必修",
    "teacher": "王教授",
    "when": "大二上（資訊工程學系）",
    "concepts": "樹,堆疊,圖,排序",
    "technologies": "C++, Python",
    "domain_tags": "演算法,資料結構",
    "summary": "課程摘要前 200 字..."
  }
]
```

> `when` 欄位含科系脈絡：單科系必修顯示「大一上（地球科學學系）」；多科系共必修顯示「大一上（化學學系、光電科學與工程學系、物理學系...）」；無修習建議時為空字串。

---

### 2. `get_course_detail` — 查詢課程完整資訊（整合版）

**功能**：一次回傳官方課綱（課程目標、授課內容、教科書）與修課資格分發條件（`raw_conditions`，含年級/系所限制與先修要求）。整合原 `get_course_syllabus` + `get_course_eligibility` + `get_prereq_info` 三個工具。

**同名消歧義邏輯**：
- 有 `course_code` → 精確查詢（無歧義）
- 只有 `name_zh` → 若多科系都有此課，回傳 `ambiguous=True` + candidates
- `name_zh` + `dept` → 定位到指定科系

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `name_zh` | string | | 課程中文名稱，如「演算法」 |
| `dept` | string | | 指定系所以消歧義 |
| `course_code` | string | | 課號，精確查詢優先使用 |

**回傳格式（找到且無歧義）**（`dict`）

```json
{
  "found": true,
  "ambiguous": false,
  "course_code": "CE3005",
  "name_zh": "演算法",
  "dept": "資訊工程學系",
  "teacher": "江振瑞",
  "credits": 3,
  "objective": "熟悉一般軟體所會用到的演算法...",
  "content": "1. 演算法基本介紹\n2. 演算法分析...",
  "textbook": "Introduction to Algorithms...",
  "raw_conditions": "不限修課條件（全校皆可修）"
}
```

**回傳格式（同名歧義）**（`dict`）

```json
{
  "found": true,
  "ambiguous": true,
  "message": "找到 4 個科系都有「普通化學」，請指定 dept 或由使用者選擇：",
  "candidates": [
    {"dept": "化學學系", "course_code": "CHEM1001", "teacher": "林教授", "credits": 3}
  ]
}
```

> `raw_conditions` 為原始分發條件原文，含先修課程與年級/系所限制，直接供 LLM 理解。  
> `is_unrestricted=True` 時，`raw_conditions` 為 `"不限修課條件（全校皆可修）"`。

---

### 3. `get_dept_courses` — 查詢系所必/選修課程

**功能**：從知識圖譜查詢特定系所的必修或選修課程清單（結構化資料，比向量搜尋更精確）。**適合廣泛列舉**（「資工系有哪些必修」）；主題式查詢應改用 `search_courses(query=..., dept=...)`。

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `dept_name` | string | ✓ | — | 系所名稱，如「客家語文暨社會科學學系」 |
| `course_type` | string | | `"required"` | `"required"`, `"elective"`, `"all"` |

**回傳格式**（`dict`）

```json
{
  "dept_name": "大氣科學學系",
  "course_type": "required",
  "courses": [
    {"id": "ATM1001", "name": "大氣熱力學", "credits": 3, "relation": "必修", "teacher": "張教授"}
  ]
}
```

> `dept_name` 支援系所、學院學士班（如「理學院學士班」）、系內組別（如「機械工程學系甲組」）。選修課若圖資料缺失，自動 fallback 至 Qdrant metadata 精確查詢。

---

### `get_dept_info` — 暫停使用

此工具的函式暫時保留，但已從 OpenAI Function Schema `TOOLS` 與 `_TOOL_MAP` 移除。Agent 無法選用或
執行此工具；系所名稱解析也不再以 `ncu_departments` 作為向量 fallback。待資料內容、來源與向量策略
完成 review 後再決定是否重新啟用。

**原設計功能**：語意搜尋由課程事實資料統計產生的系所介紹，包括課程領域、核心能力與代表性課程。

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `query` | string | ✓ | — | 系所名稱或描述 |

**回傳格式**（`list[dict]`，n_results=5）

```json
[
  {
    "dept_name": "客家語文暨社會科學學系",
    "summary": "課程統計摘要前 500 字...",
    "domain_profile": [],
    "data_basis": "derived_from_ncu_course_statistics",
    "is_official_department_statement": false,
    "disclaimer": "課程資料統計摘要，不代表系所官方自述、入學建議或就業保證。"
  }
]
```

---

### 5. `get_graduation_requirements` — 查詢畢業規定（整合版）

**功能**：同時回傳結構化學分要求（最低學分、必修學分、認證要求清單）與完整原文說明。

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `dept_name` | string | ✓ | 系所名稱 |

**回傳格式**（`dict`）

```json
{
  "found": true,
  "dept_name": "中國文學學系",
  "min_credits": 128,
  "required_credits": 60,
  "certifications": ["英語能力認證"],
  "raw_notes": "一、畢業應修最低學分數為 128 學分...（完整原文）"
}
```

> `min_credits` / `required_credits` / `certifications` 來自 schedule_draft 結構化資料；`raw_notes` 來自 requirements_notes.json 原文（更完整）。

---

### 6. `get_program_info` — 查詢學程完整資訊（整合版）

**功能**：一次回傳學分學程的完整說明文字（來自 `program_descriptions.json`）與必/選修課程清單（來自知識圖譜）。整合原 `get_program_description` + `get_program_courses`。

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `program_name` | string | ✓ | 學程名稱，如「客語教學學分學程」 |

**比對策略**：精確比對 → 包含比對 → 模糊字元比對 → 回傳 `available_programs` 清單

**回傳格式（找到）**（`dict`）

```json
{
  "found": true,
  "program_name": "客語教學學分學程",
  "description": "本學程旨在培養學生具備客語教學能力...（完整說明）",
  "required_courses": [
    {"id": "HAKKA1001", "name": "客語口語表達", "credits": 2, "relation": "必修"}
  ],
  "elective_courses": [
    {"id": "HAKKA2003", "name": "客家文化與社會", "credits": 2, "relation": "選修"}
  ],
  "courses": [...]
}
```

**回傳格式（找不到）**（`dict`）

```json
{
  "found": false,
  "message": "找不到「OO學程」的學程資料",
  "available_programs": ["客語教學學分學程", "人工智慧技術應用學分學程", "..."]
}
```

> `courses` 為 `required_courses + elective_courses` 的合併清單，供系統收集課程卡片用。

---

### 8. `search_programs` — 語意搜尋學分學程

**功能**：語意搜尋 `ncu_credit_programs` collection，依主題或描述找最相關的學程清單。

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `query` | string | ✓ | — | 學程主題或描述，如「語言文化」「永續環境」「AI 應用」 |
| `n` | integer | | `5` | 回傳筆數 |

**回傳格式**（`list[dict]`）

```json
[
  {
    "program_name": "客語教學學分學程",
    "college": "客家學院",
    "description_excerpt": "本學程旨在...（前 300 字）",
    "distance": 0.412
  }
]
```

---

### 9. `get_teacher_info` — 查詢教師詳情

**功能**：查詢特定教師的官方專長（教育部申報資料，Qdrant ncu_teachers）與開課清單（來自知識圖譜）。

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `teacher_name` | string | ✓ | 教師姓名 |

**回傳格式**（`dict`）

```json
{
  "teacher_name": "林教授",
  "profile": [
    {"name": "林教授", "dept": "大氣科學學系", "rank": "教授", "specialties": "數值天氣預報,大氣動力學"}
  ],
  "courses": [
    {"id": "ATM2001", "name": "大氣動力學"}
  ]
}
```

---

### 10. `search_teachers` — 語意搜尋教師

**功能**：依研究領域或專長關鍵詞在 Qdrant `ncu_teachers` collection 做語意搜尋。

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `query` | string | ✓ | — | 研究領域或專長描述 |
| `n` | integer | | `5` | 回傳筆數 |

**回傳格式**（`list[dict]`）

```json
[
  {"name": "林教授", "dept": "大氣科學學系", "rank": "教授", "specialties": "數值天氣預報,大氣動力學"}
]
```

---

### 11. `get_course_knowledge_map` — 知識地圖探索

**功能**：回傳一門課的完整知識地圖：涵蓋的學術概念、使用的技術工具，以及概念重疊最高的相似課程（top 8，RRF 融合）。

**算法**：
1. 查詢課程節點
2. 取出 `USES_TECH`/`COVERS_CONCEPT` 出向邊 → 技術清單（前 10）、概念清單（全部）
3. 呼叫 `_rrf_similar_courses(course_name, top_n=8)` 取相似課程（圖 + 向量 RRF）

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `course_name` | string | ✓ | 課程名稱 |

**回傳格式**（`str`，格式化字串）

```
【氣候動力學】（大氣科學學系，3學分）

使用技術：Python, MATLAB, NCO
涵蓋概念（共 12 個）：大氣環流, 海洋耦合, 輻射平衡, ...

概念重疊最高的相關課程（可延伸學習）：
  - 大氣動力學（大氣科學學系）[共享 8 個概念]
  - 物理海洋學（地科系）[共享 5 個概念]
```

---

### 12. `find_similar_courses` — 找相似課程

**功能**：透過知識圖譜 Concept/Technology 節點 + Qdrant 向量語意，以 RRF 融合排序，找與指定課程最相近的跨系課程。

**算法（`_rrf_similar_courses()`，與 `get_course_knowledge_map` 共用）**：
1. **圖路徑**：種子課程 `COVERS`/`TEACHES` 出向邊 → 共同 Concept 節點 → 反向邊找候選課程，按 `shared_concepts` 排序（top-25）
2. **向量路徑**：`retriever.search_courses(course_name, n=20)` → Qdrant 語意相似排名
3. **RRF 融合**（k=60）：`score = Σ 1/(60 + rank)`，兩路各自貢獻，排除自身後取前 15

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `course_name` | string | ✓ | 課程名稱，如「有機化學」「生物統計」 |

**回傳格式**（`str`，格式化字串）

```
與「有機化學」概念相近的課程（共 9 門）：
- 生物化學（生命科學學系，3學分） [共享概念：6 個]
- 藥物合成（化學學系，3學分） [共享概念：4 個]
```

---

### 13. `get_depts_by_tech` — 查詢技術分布系所

**功能**：多跳圖查詢，找哪些系所的課程教授某技術或概念，並區分必修與選修。

**參數表**

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `tech_name` | string | ✓ | 技術或概念名稱，如「統計」「GIS」 |

**回傳格式**（`str`，格式化字串）

```
教「GIS」的系所分布（共 8 門相關課程）：

必修課含此技術的系所（2 個）：
  - 地球科學學系

選修課含此技術的系所（5 個）：
  - 大氣科學學系

相關課程（前 10 門）：
  - 地理資訊系統（地科系）
```

---

### 14. `ppr_explore` — Personalized PageRank 廣泛探索

**功能**：從概念/課程節點出發，在整個知識圖譜做 igraph Weighted Personalized PageRank 隨機遊走，找出最相關的節點（可跨課程、教師、系所、概念類型）。

**算法**：
- igraph `personalized_pagerank(directed=False, damping=0.85, reset=v, weights="weight")`（C 底層，約 0.23s/次）
- 種子節點（雙路取聯集）：
  - **Qdrant `ncu_graph_nodes` 向量搜尋**（優先，top_k=3/seed）
  - **字串精確/子字串比對**（保底 fallback）
- 邊加權：TEACHES=1.2, COVERS=1.0, EXPERT_IN=1.0, PREREQUISITE_OF=0.8, 結構邊=0.3
- **Gap Truncation**：移除 `score < mean − 0.5σ` 的尾部噪音（< 4 筆時不截斷）
- 分數 ×1000（讓數字可讀）

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `seed` | string | ✓ | — | 起始概念/課程，可用逗號分隔多個 |
| `focus` | string | | `"all"` | `"all"`, `"course"`, `"instructor"`, `"dept"`, `"concept"` |
| `top_k` | integer | | `15` | 回傳數量 |

**`focus` 對應的節點類型過濾**

| focus | 包含節點類型 |
|-------|------------|
| `"course"` | `Course` |
| `"instructor"` | `Instructor` |
| `"dept"` | `Department`, `DeptGroup`, `CollegeBachelorProgram` |
| `"concept"` | `Concept`, `Technology`, `Field` |
| `"all"` | 全部 |

**回傳格式**（`str`，格式化字串）

```
以「客家文化」為起點的 PPR 探索結果（all 模式）：
  [課程] 客家文化導論（客家語文暨社會科學學系）  [PPR: 48.97]
  [教師] 劉教授（客家學院）
  [概念] 族群認同
  [系所] 客家語文暨社會科學學系
```

---

### 15. `explore_concept_neighborhood` — 概念鄰域精確探索

**功能**：以概念/技術關鍵詞為入口，在知識圖譜做 N 跳 BFS 鄰域探索，找出直接覆蓋此概念的課程。比 `ppr_explore` 更精確（有限跳數），比 `search_courses` 更廣（不限向量相似）。

**算法**：
1. Qdrant `ncu_graph_nodes` 向量搜尋概念/技術節點（top-5）→ node_id
2. igraph BFS 展開 N 跳（白名單邊：COVERS, TEACHES, COVERS_FIELD, SIMILAR_TO）
3. 過濾出 Course 節點，回傳課程清單
4. Fallback（ncu_graph_nodes 未建立）：字串比對 knowledge_graph.json

**參數表**

| 參數 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|-------|------|
| `query` | string | ✓ | — | 概念或技術關鍵詞，如「神經網路」「資料視覺化」 |
| `hops` | integer | | `2` | BFS 跳數（1-3） |
| `top_k` | integer | | `15` | 回傳課程數量 |

**回傳格式**（`str`，格式化字串）

```
以「神經網路」為中心的概念鄰域（2 跳，共 12 門課）：
- 深度學習（資訊工程學系，3 學分）
- 機器學習概論（資訊工程學系，3 學分）
```

**與其他工具的對比**

| 工具 | 適用場景 | 廣度 |
|------|---------|------|
| `search_courses` | 課程語意搜尋（有 query 描述） | 向量近鄰 |
| `explore_concept_neighborhood` | 精確概念鄰域（「有哪些課教 X？」） | N 跳 BFS |
| `ppr_explore` | 廣泛圖擴散（跨類型探索） | 全圖 PPR |
