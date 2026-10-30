# 系統架構總覽

> 文件版本：2026-05-02

---

## 系統目的

**中央大學選課助理**（NCU Course Advisor）是一個針對中央大學課程資訊的 GraphRAG 對話系統，協助高中生探索科系、大學生規劃選課、了解學程與畢業規定。

---

## 技術棧

| 層次 | 技術 |
|------|------|
| 後端框架 | FastAPI（Python 3.11+） |
| LLM | Azure OpenAI GPT-4o（`AZURE_OPENAI_CHAT_DEPLOYMENT`） |
| 向量資料庫 | Qdrant（local mode，`data/processed/qdrant_data/`） |
| 知識圖譜 | igraph（載入自 `knowledge_graph.json`） |
| 前端框架 | React 19 + TypeScript + Tailwind CSS |
| 監控 | LangSmith（選填） |
| Session 儲存 | JSON 檔案（`data/sessions/`） |

---

## 資料源

### 1. Qdrant 向量資料庫

| Collection | 內容 | 用途 |
|-----------|------|------|
| `ncu_courses_ug` | 大學部課程嵌入（含 metadata） | 課程語意搜尋 |
| `ncu_courses_grad` | 研究所課程嵌入 | 研究所課程搜尋 |
| `ncu_teachers` | 教師專長嵌入 | 教師搜尋 |
| `ncu_departments` | 舊系所介紹 collection（暫停使用） | 待資料內容與向量策略 review |
| `ncu_credit_programs` | 學分學程嵌入 | 學程語意搜尋 |
| `ncu_graph_nodes` | 知識圖譜節點嵌入（概念/技術/領域） | Query expansion（3-layer hybrid search） |

**課程 metadata 欄位（含課綱）**：每筆課程記錄除基本欄位外，另含 `objective`（課程目標）、`content`（授課內容）、`textbook`（教科書/參考書）三個欄位，由 `update_syllabus_metadata.py` 獨立更新（不需重新嵌入向量）。

### 2. 知識圖譜（igraph）

載入自 `data/processed/graph/knowledge_graph.json`，節點類型包含：

- `Course`：課程節點
- `Instructor`：教師節點
- `Department / DeptGroup / CollegeBachelorProgram`：系所節點
- `CreditProgram`：學分學程節點
- `Concept / Technology / Field`：知識概念節點
- `CurriculumPlan / ElectiveGroup / Slot`：課程規劃節點

圖演算法：Weighted PPR（igraph，~0.23s/次）、BFS（概念鄰域探索）。

### 3. 靜態 JSON 資料

| 檔案 | 內容 |
|------|------|
| `data/processed/program_descriptions.json` | 學分學程說明文字 |
| `data/processed/requirements_notes.json` | 系所畢業規定原文 |
| `data/processed/schedule_draft/` | 課程規劃草稿（畢業學分） |

---

## 資料流

```
使用者問題
    │
    ▼
Stage 1 — ReAct Agent（最多 4 輪）
    │  ┌─────────────────────────────────────────┐
    │  │  每輪：LLM 決定呼叫哪些工具              │
    │  │  ThreadPoolExecutor 並行執行             │
    │  │  工具回傳結果 → course_pool 累積         │
    │  └─────────────────────────────────────────┘
    │
    ▼
LLM 生成回答（含所有工具結果為 context）
    │  以 <course>課名（系所）</course> 標籤標記提到的課程
    │
    ▼
_extract_courses_from_tags()
    │  regex 擷取 <course> 標籤 → exact/fuzzy match pool
    │  只保留 pool 內存在的課程 → 零幻覺
    │
    ▼
回傳 {answer, course_cards, session_id, ...}
```

---

## 前端三欄 Layout

```
┌────────────┬──────────────────────────┬──────────────┐
│  左側欄    │      聊天區域            │  右側面板    │
│  (320px)   │    (flex-1)              │   (280px)    │
│            │                          │              │
│  對話紀錄  │  訊息泡泡（Markdown）    │  推薦課程    │
│  列表      │  + DebugTracePanel       │  卡片列表    │
│            │                          │              │
│ 預設收合   │  輸入框                  │  lg:flex     │
└────────────┴──────────────────────────┴──────────────┘
```

- **左側欄**：預設收合，顯示歷史對話列表
- **聊天區域**：SSE 串流輸出，每則訊息下方有 DebugTracePanel 按鈕
- **右側面板**：永久顯示最新一輪回答的推薦課程，手機版隱藏（`lg:flex`）

---

## 工具清單（14 個）

| 類別 | 工具 | 說明 |
|------|------|------|
| 課程搜尋 | `search_courses` | 3-layer hybrid search（query expansion → RRF） |
| 課程搜尋 | `get_dept_courses` | 系所必/選修課程列表 |
| 課程搜尋 | `get_course_detail` | 課程詳情（課綱 + 修課資格 + 先修條件，整合版） |
| 系所 | `get_dept_info`（暫停） | `ncu_departments` 內容與向量策略確認前不暴露給 agent |
| 系所 | `get_graduation_requirements` | 畢業規定（結構化+原文） |
| 學程 | `search_programs` | 語意搜尋學分學程 |
| 學程 | `get_program_info` | 學程說明 + 必/選修課清單（一次取得） |
| 教師 | `get_teacher_info` | 教師詳情 + 開課清單 |
| 教師 | `search_teachers` | 語意搜尋教師專長 |
| 圖探索 | `get_course_knowledge_map` | 課程知識地圖（概念+技術+相似課） |
| 圖探索 | `find_similar_courses` | 共享概念最多的相似課程 |
| 圖探索 | `get_depts_by_tech` | 技術分布到哪些系所 |
| 圖探索 | `ppr_explore` | Weighted PPR 廣泛探索 |
| 圖探索 | `explore_concept_neighborhood` | BFS 概念鄰域課程 |

> **向下相容保留**（`_TOOL_MAP` 有，`TOOLS` schema 無，LLM 不會主動呼叫）：
> `get_graduation_rules`, `get_requirements_notes`, `get_program_courses`, `get_program_description`

---

## SSE 串流事件序列

```
tool_start(tool, args) → tool_done(tool, count, courses_found) →
[重複多輪] → verify_start(pool_size) → verify_done(method="tag", selected, filtered_out) →
done(session_id, course_cards, course_pool, tools_used, debug_trace, ...)
```

> `verify_start` / `verify_done` 仍保留在事件序列中，但現在只表示 tag 提取階段完成（`method="tag"`），不再代表額外 LLM 呼叫。

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/chat` | 同步聊天 |
| POST | `/api/chat/stream` | SSE 串流聊天（含 debug_trace） |
| POST | `/api/chat/course_detail` | 依課名查詢完整課程資訊 |
| GET  | `/api/chat/sessions` | 列出所有對話紀錄 |
| GET  | `/api/chat/session/{id}` | 取得特定對話的完整 turns |
| DELETE | `/api/chat/session/{id}` | 刪除對話 |
