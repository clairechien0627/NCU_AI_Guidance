# Tools 設計說明（最新版）

> 撰寫日期：2026-06-02  
> 對應版本：`backend/app/services/tools.py`、`backend/app/services/llm_service.py`

---

## 概述

本系統的工具層（Tool Layer）是 LLM ReAct 迴圈的執行核心：LLM 根據 system prompt 的指引自行決定呼叫哪些工具、以何種順序，工具層負責實際查詢資料並回傳結構化結果。工具層主要由兩個檔案構成：

- **`tools.py`**：工具函式定義、結果格式化、OpenAI Function Schema
- **`llm_service.py`**：LLM 呼叫、ReAct 迴圈、課程卡片萃取流水線

---

## 一、資料來源架構

工具層整合了三個資料來源，每個來源有不同的查詢強項：

| 資料來源 | 類型 | 強項 |
|---------|------|------|
| **Qdrant** | 向量資料庫 | 語意搜尋、payload 過濾（系所/學院/修課資格/type） |
| **知識圖譜**（igraph） | 圖資料庫 | 結構化關係（必修/選修、先修、教師-課程、技術-課程）、多跳遍歷、PPR 探索 |
| **JSON 靜態資料** | 檔案 | 學程說明、畢業規定原文、系所別名、排課草稿 |

---

## 二、暴露給 LLM 的工具清單

目前共 **13 個工具**透過 OpenAI Function Schema 暴露給 LLM；`get_dept_info` 已從 schema 與 `_TOOL_MAP` 暫時移除，另有 4 個向後相容工具保留於 `_TOOL_MAP` 但不暴露：

### 主要工具

| 工具名稱 | 主要資料來源 | 用途 |
|---------|------------|------|
| `search_courses` | Qdrant + Graph | 語意搜尋課程（多 Signal RRF 融合，見第三節） |
| `get_dept_courses` | Graph（Qdrant fallback） | 查詢系所必修/選修課程清單 |
| `get_course_detail` | Qdrant | 課綱 + 修課資格 + 先修（三合一整合工具） |
| `get_dept_info`（暫停） | Qdrant + Graph | `ncu_departments` 內容與向量策略確認前不暴露給 LLM |
| `get_graduation_requirements` | JSON + Graph | 畢業規定（結構化學分 + 原文，二合一整合工具） |
| `get_program_info` | JSON + Graph | 學程說明 + 必選修課程清單（二合一整合工具） |
| `search_programs` | Qdrant | 語意搜尋學分學程 |
| `find_similar_courses` | Graph + Qdrant | 找概念相近的課程（RRF 融合，見第四節） |
| `get_course_knowledge_map` | Graph + Qdrant | 課程知識地圖（概念、技術、相似課） |
| `get_depts_by_tech` | Graph | 哪些系所的課有教某技術（必修/選修分離） |
| `ppr_explore` | Graph | Personalized PageRank 廣泛探索（課程/教師/系所/學程） |
| `explore_concept_neighborhood` | Graph + Qdrant | N 跳 BFS 概念鄰域（精確直接連結） |
| `get_teacher_info` | Qdrant + Graph | 教師專長 + 開課清單 |
| `search_teachers` | Qdrant | 依研究領域語意搜尋教師 |

### 向後相容工具（不暴露給 LLM）

- `get_graduation_rules`、`get_requirements_notes`：已整合為 `get_graduation_requirements`
- `get_program_courses`、`get_program_description`：已整合為 `get_program_info`
- `get_course_community`、`list_course_communities`：社群已注入圖，暫不單獨暴露

---

## 三、`search_courses` 多 Signal RRF 搜尋架構

`search_courses` 是系統中最複雜的工具，採用多路 Signal 融合的 RRF（Reciprocal Rank Fusion）架構：

### 3.1 Graph-first 快捷路徑（僅 tech 查詢）

若有 `tech` 參數，先走圖精確查詢（`graph_service.search_courses_by_tech`），命中則直接回傳，略過向量搜尋。這條路徑對技術/工具名稱（Python、PyTorch 等）最精確。

### 3.2 Layer 1：Query Expansion（雙軌）

呼叫 `_search_concept_nodes_with_scores(query, top_k=25)` 對知識圖譜節點做語意搜尋：

- **Track A（Concept/Technology/Field/Competency 節點，score 無閾值）**：取得相關概念名稱，附加到 expanded_query
- **Track B（Course 節點，score ≥ 0.65）**：記錄為 Signal C 等候注入

### 3.3 Layer 2：Qdrant Filter 構建

依照呼叫參數組裝 Qdrant 過濾條件（$and 組合）：

| 參數 | 對應 Qdrant Filter |
|------|------------------|
| `dept` | `dept == value` |
| `college` | `college == value` |
| `course_type` | `type == "必修"/"選修"` |
| `exclude_grad_only` | `is_grad_only == false` |
| `tech` | `tools/languages/concepts $contains tech` |
| `topic_tag` | `topic_tags $contains tag` |
| `student_college` | OR 條件：全校開放 OR college_include 含 OR dept_include 含 |

`student_college` 另有 `is_open_with_exclusions` 路徑，獨立搜尋並以 must_not 排除不可修的學院，結果記錄為 Signal A_excl。

### 3.4 Layer 3：RRF 融合

依優先度：Signal A（擴展查詢）→ Signal B（原始查詢）→ Signal A_excl → Signal N → Signal N2 → Signal C

| Signal | 說明 | Boost 值 |
|--------|------|---------|
| **A** | 擴展查詢 Qdrant 結果 | 標準 RRF（K=60） |
| **B** | 原始查詢 Qdrant 結果（query ≠ expanded_query 時才執行） | 標準 RRF |
| **A_excl** | 排除型開放課程補回（距離門檻 ≤ 0.65） | 標準 RRF |
| **N** | 課名完全匹配 boost（Signal N） | +0.15（遠超標準 RRF 上限 ≈0.016） |
| **N2** | 課名包含 query（部分匹配） | +0.05 |
| **C** | 圖 Course 節點直達 boost（score/（K+1）） | 按圖節點 score 動態計算 |

最終排名：exact_match 置頂（不占語意結果 n 名額），語意結果取前 n 筆，排除 SC 前綴服務學習課程。

---

## 四、`find_similar_courses` RRF 融合

`_rrf_similar_courses()` 融合兩個來源找相似課程：

1. **圖共概念**：`graph_service.search_courses_by_concept_cluster(course_name, top_n=40)`，根據共享 Concept 節點計數排名
2. **向量語意**：`retriever.search_courses(course_name, n_results=20)`，根據向量距離排名

以 RRF（K=60）融合兩個排名清單，排除課名與查詢相同的課程，回傳前 15 筆。`get_course_knowledge_map` 也使用此函式取得相似課程清單。

---

## 五、`ppr_explore` Personalized PageRank

PPR 以種子概念為起點，在整個知識圖譜做能量擴散，找出最相關的各類節點。

### 5.1 種子確認（E2-a 改進）

先透過 `graph_service._find_ppr_seeds(seed_list)` 確認種子是否在圖中：
- 全部找不到 → 直接回傳建議訊息，不進行 PPR
- 部分找不到 → 執行 PPR，結果末尾加 ⚠️ 警告指出哪些種子無效

### 5.2 Focus 模式

| focus | 回傳節點類型 | 適用場景 |
|-------|------------|---------|
| `course`（預設） | Course | 找相關課程（多概念交集時比 search_courses 更佳） |
| `instructor` | Instructor | 研究某領域的教師（圖有 WORKS_FOR/TEACHES 邊） |
| `dept` | Department/DeptGroup/CollegeBachelorProgram | 哪些系所涉及某概念 |
| `overview` | 全部類型分組顯示 | 使用者明確要看課程+教師+系所全貌 |

`concept` 傳入時自動重導向為 `course`（概念是種子，不是輸出目標）。

### 5.3 設計限制

PPR 在**單種子**時效果不如 `search_courses`（能量集中於 1-hop 直接鄰居，等同簡單圖查詢）。PPR 真正的獨特價值在**多種子**：兩個 seed 的共同鄰居節點積累分數，這些正是橋接兩個概念的課程/教師。

---

## 六、LLM Service 核心流水線（`llm_service.py`）

### 6.1 ReAct 迴圈

`generate_with_tools()` 和 `stream_with_tools()` 實作 ReAct 模式：

```
for _ in range(max_rounds=4):
    response = LLM(messages, tools)
    if no tool_calls:
        → 最終回答，進入課程卡片萃取流水線
    execute all tool_calls (並行，ThreadPoolExecutor)
    collect_course_pool()
    append results to messages
↓
強制生成最終回答（超過 max_rounds）
```

### 6.2 課程池（Course Pool）

每次工具呼叫後，`_collect_course_pool()` 從結果中收集課程資料到共用 `course_pool: dict`，key 為 `course_code`（search_courses）或課名（圖工具）。不同工具回傳格式各異，`_collect_course_pool` 分別處理：

| 工具 | 解析方式 |
|------|---------|
| `search_courses` | 直接讀 `course_code`、`name_zh` |
| `get_dept_courses`、`get_program_info` | 讀 `courses` list |
| `ppr_explore`、`find_similar_courses`、`get_course_knowledge_map`、`get_depts_by_tech` | regex 解析字串格式（`_parse_courses_from_str`） |
| `get_course_detail` | 讀單筆 or candidates（ambiguous=True 時） |
| `get_teacher_info` | 讀 `courses` list，以 `name|id` 作 key 保留多版本 |

### 6.3 課程卡片萃取流水線

LLM 回答完成後：

1. **`_enrich_course_cards()`**：以課名或課號向量 DB 查詢，補齊 `domain_tags`、`teacher`、`credits`、`type` 等欄位
2. **`_extract_courses_from_tags(answer, course_pool)`**：從 LLM 回答中的 `<course>` 標籤提取課程
   - 支援 `<course>課名（系所）</course>` 消歧義格式
   - 支援 `<course>課名（N學分）</course>` 學分消歧義格式
   - Exact match → 優先；Fuzzy match（ratio ≥ 0.85）→ 自動修正 answer 中的錯誤課名
   - LLM 未輸出任何標籤時回傳空清單（不依賴 LLM 驗證）

### 6.4 Streaming 模式（`stream_with_tools`）

SSE 事件序列：

```
tool_start  → 工具開始執行（含 args）
tool_done   → 工具執行完成（含 count、courses_found、scores）
tool_result → 字串型工具回傳原文預覽（800 字截斷）
token       → LLM 回答逐字輸出
verify_start → 課程卡片驗證開始（含 pool_size）
verify_done  → 驗證完成（method=tag，selected/filtered_out 清單）
done        → 完整回答 + 元資料（tools_used, course_cards, course_pool, debug_trace）
error       → 例外訊息
```

`debug_trace.toolCalls` 記錄每次工具呼叫的 args、coursesFound、count、scores、scoreType，供開發監控分析。

---

## 七、輔助工具函式

### 系所別名解析

`_resolve_name()` 查 `dept_aliases.json`，LLM 傳縮寫（「資工系」）自動轉正式名稱（「資訊工程學系」），適用於 `dept`、`college`、`student_college` 三個參數。

### 課程語意欄位補齊

`_enrich_courses_metadata()` 對圖查詢回傳的課程補齊以下欄位（透過 `graph_service.get_course_tags`）：

- `concepts`、`technologies`、`field_tags`、`course_domain`：文字標籤
- `competencies`：結構化核心能力 list（含 name/level_num/level_label）
- `topic_tags`、`core_questions`：通識課特有欄位

### 同名課程去重

`_deduplicate_courses_by_name()` 合併「同名 + 同學分」的課程（視為分班），保留 `sections` 數量與 `course_ids` 清單。不同學分的同名課程視為不同課，各自保留。

### 上課時間格式化

`_fmt_when()` 將 `when_contexts`（`"dept_id@大一上"` 格式）轉換為可讀字串：
- 1 系：`"大一上（地球科學學系）"`
- 多系同 when：`"大一上（資工系、電機系）"`
- 多系不同 when：`"大一上（地球科學學系）、大二上（資訊工程學系）"`

---

## 八、整合工具設計模式

系統在多次優化後採用「整合工具」模式，將原本分散的多個小工具合併為單一工具：

| 整合工具 | 取代的原有工具 | 效益 |
|---------|-------------|------|
| `get_course_detail` | get_course_syllabus + get_course_eligibility + get_prereq_info | 一次查詢取得課綱 + 修課資格 + 先修 |
| `get_graduation_requirements` | get_graduation_rules + get_requirements_notes | 結構化學分 + 原文 |
| `get_program_info` | get_program_description + get_program_courses | 說明 + 課程清單 |

整合工具減少 LLM 的工具呼叫輪次，降低 token 消耗，也避免分步查詢時 LLM 漏掉第二步的情況。

---

## 九、防幻覺機制

### 標籤限制

System prompt 要求 LLM 在回答中用 `<course>` 標籤包住課程名稱，且：
- 課名必須與工具回傳的原始名稱逐字相同
- 只能標記工具實際回傳過的課程
- PPR 回傳 0 筆時不得使用標籤

### Course Pool 驗證

`_extract_courses_from_tags` 只從 `course_pool`（本輪工具實際查詢到的課程）中比對，標籤內課名若不在 pool 中（exact 或 fuzzy ratio < 0.85）則不產生課程卡片。

### found=False 規則

System prompt 明確規定：工具回傳 `found=False` 時，只能依 `fallback_candidates` 提供候選，絕不能自行推斷或補全系所/課程名稱。

---

## 十、效能考量

| 機制 | 實作方式 | 效果 |
|------|---------|------|
| 並行工具執行 | `ThreadPoolExecutor` | 同輪次的多個工具並行，不互相等待 |
| LRU cache | `@lru_cache(maxsize=1)` 於靜態資料載入 | `dept_aliases.json` 等只讀一次 |
| 圖資料 in-memory | `graph_service._g()` 快取 | igraph 物件常駐記憶體 |
| LangSmith tracing | `wrap_openai(client)` | 選填，設定 LANGSMITH_API_KEY 才啟用 |
| Streaming | SSE 逐字輸出 | 降低首字延遲，前端即時顯示 |
