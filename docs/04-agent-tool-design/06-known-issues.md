# Known Issues & Technical Debt

> 更新時間：2026-05-02

---

## 一、工具設計現況

### 1.1 目前對外工具（15 個，已在 TOOLS schema 中）

| 組別 | 工具 | 備註 |
|------|------|------|
| **課程搜尋** | `search_courses`, `get_dept_courses`, `get_course_detail` | 覆蓋完整 |
| **探索/推薦** | `find_similar_courses`, `get_course_knowledge_map`, `ppr_explore`, `explore_concept_neighborhood` | ⚠️ 有重疊風險（見 1.2） |
| **學程** | `search_programs`, `get_program_description`, `get_program_courses` | ⚠️ 後兩者幾乎總是並行（見 1.3） |
| **系所/教師** | `get_teacher_info`, `search_teachers` | `get_dept_info` 暫停，待系所向量資料策略確認 |
| **畢業規定** | `get_graduation_requirements` | 無問題 |
| **技術分布** | `get_depts_by_tech` | 無問題 |

> 向下相容保留（`_TOOL_MAP` 有，`TOOLS` schema 無，LLM 不會主動呼叫）：
> `get_graduation_rules`, `get_requirements_notes`

---

### 1.2 工具選擇歧義風險（探索群 4 個）

**問題**：當使用者問「有哪些課和 X 有關？」或「有哪些課涵蓋 X 概念？」時，以下工具都可能被呼叫，但應用場景不同：

| 工具 | 正確使用場景 | 錯誤使用場景 |
|------|------------|------------|
| `find_similar_courses` | 「有沒有類似演算法的課？」（輸入：課程名） | 輸入概念詞而非課名 |
| `get_course_knowledge_map` | 「演算法在教什麼？」（同上，但要概念地圖） | 只想要相似課清單時（用 find_similar 即可）|
| `explore_concept_neighborhood` | 「有哪些課涵蓋神經網路概念？」（輸入：概念詞） | 用課程名輸入 |
| `ppr_explore(focus="course")` | 「和深度學習有關的一切課程」（廣泛探索） | 需要精確概念鄰域時 |

**`find_similar_courses` vs `get_course_knowledge_map` 重疊**：兩者都用 `_rrf_similar_courses()` 回傳相似課程。差別只在 `get_course_knowledge_map` 多了概念/技術清單。LLM 可能永遠選 knowledge_map，讓 `find_similar_courses` 變冗餘。

**建議**（中期）：考慮移除 `find_similar_courses`，把它的功能直接整入 `get_course_knowledge_map`，或在 system prompt 中更明確區分「只要相似課清單時用 find_similar_courses，要完整知識地圖時用 get_course_knowledge_map」。

---

### 1.3 `get_program_description` + `get_program_courses` 應合併

**問題**：這兩個工具輸入相同（學程名稱），且幾乎永遠被並行呼叫，浪費一次 LLM 工具決策 + 兩次工具執行。

**建議**（待實作，★★★）：合併為 `get_program_info`，一次回傳：
```json
{
  "program_name": "...",
  "description": "完整說明",
  "required_courses": [...],
  "elective_courses": [...]
}
```
`search_programs` 保持獨立（輸入為 query，場景不同）。

---

## 二、llm_service.py Dead Code

### 2.1 整條廢棄路徑

以下函式已被 Tool-Use flow 取代，但仍保留在檔案中：

| 函式 | 狀態 |
|------|------|
| `_build_context(vector_results, graph_results)` | ⛔ 只被 `generate_answer()` 呼叫 |
| `generate_answer(question, ...)` | ⛔ 只被 `generate_simple_answer()` 呼叫 |
| `generate_simple_answer(question, context)` | ⛔ `chat.py` 完全沒有 import 使用 |

**建議**：三者可一併刪除（或保留作離線測試用，加 `# test-only` 標記）。

---

### 2.2 `_verify_course_list()` 未被呼叫

`llm_service.py:395` 定義的 `_verify_course_list()` 是舊版課程驗證邏輯（呼叫第二次 LLM 做 anti-hallucination），目前已被 `_extract_courses_from_tags()` 的標籤比對機制取代，但函式本體仍在。

**建議**：刪除或加 `# deprecated` 標記。

---

### 2.3 `stream_with_tools` done 事件缺 `sources`

`generate_with_tools` 回傳 dict 包含 `"sources"` 欄位（由 `search_courses` 結果填充），`ChatResponse` model 也有 `sources: list[dict]`。但 `stream_with_tools` 的 `done` 事件沒有 `sources`，chat.py 的串流路徑儲存 session 時也沒傳 sources。

**影響**：串流模式的 sources 欄位永遠空白（前端 `ChatResponse.sources` 不適用於串流）。
**建議**：在 `stream_with_tools` 也收集 sources（對齊 `generate_with_tools` 邏輯），或確認前端串流路徑不需要 sources 後直接從 `ChatResponse` 移除。

---

## 三、tools.py Dead Code

以下函式定義在 `tools.py` 但沒有掛進 `_TOOL_MAP`，是整合 `get_course_detail` 後遺留的舊程式碼：

| 函式 | 行號 | 說明 |
|------|------|------|
| `tool_get_prereq_info` | ~386 | 已整合進 `get_course_detail` |
| `_matches_access_rule` | ~437 | 僅被 `can_student_take` 呼叫 |
| `can_student_take` | ~476 | 路由層沒有呼叫（可能是未來功能預留） |
| `tool_get_course_eligibility` | ~497 | 已整合進 `get_course_detail` |
| `tool_get_course_community` | ~741 | 社群功能未完成，未暴露 |
| `tool_list_course_communities` | ~763 | 社群功能未完成，未暴露 |

**建議**：
- `tool_get_prereq_info` + `tool_get_course_eligibility` → 直接刪除（功能已在 `get_course_detail` 中）
- `_matches_access_rule` + `can_student_take` → 保留若有「學生可修課判斷」功能規劃；否則刪除
- `tool_get_course_community` + `tool_list_course_communities` → 社群功能就緒時再掛入 `_TOOL_MAP`，現在可加 `# pending: community feature` 標記

---

## 四、待實作改進項目

| 優先 | 項目 | 說明 |
|------|------|------|
| ✅ | `get_program_info` 合併（2026-05-02 完成） | `get_program_description + get_program_courses` → `get_program_info` |
| ★★☆ | 清理 dead code（見二、三節） | 降低維護成本 |
| ★★☆ | `find_similar_courses` 與 `get_course_knowledge_map` 邊界釐清（見 1.2） | 避免 LLM 選錯工具 |
| ★★☆ | `stream_with_tools` 補 `sources` 欄位（見 2.3） | 對齊 API 一致性 |
| ★☆☆ | `find_similar_courses` 加入 BM25 概念列表（三路 RRF） | 相似度更精準（需 rank_bm25） |
| ★☆☆ | MMR 多樣化（`search_courses` 結果去重） | 需修改 retriever 加 `with_vectors=True` |
