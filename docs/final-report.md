# 人工智慧跨域應用專題 — 期末成果報告

> 國立中央大學 選課助理暨大專研究計畫 AI 平台  
> 報告日期：2026-09-08

---

## 目錄

1. [專案概述](#一專案概述)
2. [系統整體架構](#二系統整體架構)
3. [技術棧總覽](#三技術棧總覽)
4. [雲端部署架構](#四雲端部署架構)
5. [資料整合與處理 Pipeline](#五資料整合與處理-pipeline)
6. [知識圖譜與向量資料庫](#六知識圖譜與向量資料庫)
7. [核心功能一：課程智能助理（課程 RAG）](#七核心功能一課程智能助理)
8. [核心功能二：PDF 研究計畫系統](#八核心功能二pdf-研究計畫系統)
9. [核心功能三：個人學習傾向分析（期末新增）](#九核心功能三個人學習傾向分析)
10. [核心功能四：系統監控儀表板（期末新增）](#十核心功能四系統監控儀表板)
11. [期中到期末的演進](#十一期中到期末的演進)

---

## 一、專案概述

本專案打造一套服務中央大學師生與高中生的 AI 平台，整合兩條主線：

| 主線 | 說明 | 目標用戶 |
|------|------|---------|
| **課程智能助理** | 以 LLM + 知識圖譜 + 向量搜尋回答選課相關問題（課程查詢、修課條件、畢業規定、學程介紹） | 高中生、大學生 |
| **PDF 研究計畫系統** | 對國科會大專研究計畫 PDF 進行 RAG 問答，並以 AI 摘要生成興趣量表題目 | 高中生探索科系興趣 |

兩條主線共享同一套基礎設施（Azure OpenAI、Qdrant、PostgreSQL、Firebase Auth），並在期末新增兩個橫跨全站的支援功能：**個人學習傾向分析**與**開發者系統監控儀表板**。

### 整合資料規模

| 資料類型 | 來源 | 規模 |
|---------|------|------|
| 大學部課程 | 中央大學課務系統 | **3,547 門** |
| 研究所課程 | 中央大學課務系統 | **974 門** |
| 教師領域專長 | 教育部大專校院一覽表 | **1,009 位** / 2,469 個專長詞彙 |
| 應修科目表 | 中央大學教務處 | **28 系所** / 458 門必修課 |
| 學分學程 | 中央大學課務資訊網 | **42 個學程** / 998 門課 |
| 科系介紹 | **暫停使用** | `ncu_departments` 內容與向量策略 review 中 |
| 大專研究計畫 PDF | 國科會 | **459 份**（104–114 學年） |
| 知識圖譜 | NLP 萃取 + 結構化整合 | **27,447 節點 / 103,111 邊** |
| NLP 萃取語意節點 | Qwen3-14B-AWQ 本地推理 | 概念 16,985 + 領域 2,760 + 技術 895 |
| 修課資格規則 | 課務系統解析 | **7,282 條**（3,765 門課） |

---

## 二、系統整體架構

```
┌─────────────────────────────────────────────────────────────┐
│                        使用者端                              │
│  高中生 / 大學生 / 開發人員                                   │
└──────────────┬──────────────────────────────────────────────┘
               │  HTTPS
               ▼
┌─────────────────────────────────────────────────────────────┐
│              前端（React + TypeScript）                       │
│  Firebase Hosting（CDN 靜態部署）                            │
│                                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐ │
│  │課程助理  │ │PDF 計畫  │ │個人分析  │ │系統監控        │ │
│  │/         │ │/projects │ │/profile  │ │/monitor        │ │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐ │
│  │興趣量表  │ │課程瀏覽  │ │資源中心  │ │修課規定        │ │
│  │/assessment│ │/courses  │ │/resources│ │/curriculum     │ │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────┘ │
│  ┌──────────┐                                               │
│  │課程搜尋  │                                               │
│  │/course-  │                                               │
│  │search    │                                               │
│  └──────────┘                                               │
└──────────────┬──────────────────────────────────────────────┘
               │  REST API / SSE
               ▼
┌─────────────────────────────────────────────────────────────┐
│              後端（FastAPI + Uvicorn）                        │
│  Render 單容器部署（單 Worker，SSE 串流安全）                 │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────────┐  │
│  │ 課程 RAG   │  │ PDF RAG    │  │  監控 / 分析 API      │  │
│  │ ReAct 工具 │  │ 多 Agent   │  │  /api/monitor/*      │  │
│  │ 13 個工具  │  │ LangGraph  │  │  /api/chat/analytics │  │
│  └────────────┘  └────────────┘  └──────────────────────┘  │
└──┬───────────┬───────────┬───────────┬──────────────────────┘
   │           │           │           │
   ▼           ▼           ▼           ▼
┌──────┐ ┌─────────┐ ┌──────────┐ ┌───────────┐
│Azure │ │ Qdrant  │ │ Neon     │ │Cloudinary │
│OpenAI│ │ Cloud   │ │PostgreSQL│ │PDF 儲存   │
│GPT   │ │向量資料庫│ │(ap-se-1) │ │           │
└──────┘ └─────────┘ └──────────┘ └───────────┘
              ↑
         (us-west-1)
```

---

## 三、技術棧總覽

### 後端

| 類別 | 技術 |
|------|------|
| Web 框架 | FastAPI + Uvicorn（非同步） |
| LLM | Azure OpenAI：`gpt-5.4-mini`（課程助理主力模型）、`gpt-4o`（PDF 計畫 Agent）、`gpt-4o-mini`（PDF 路由/摘要） |
| Embedding | `text-embedding-3-large`（3072 維） |
| 向量資料庫 | Qdrant（Hybrid：Dense + BM25 Sparse） |
| 關聯式資料庫 | PostgreSQL（Neon Serverless） |
| 知識圖譜 | igraph（in-memory，Weighted PPR） |
| Agent 框架 | LangGraph（PDF Research Agent 狀態機） |
| LLM 追蹤 | LangSmith（選填） |
| Reranking | FlashrankRerank（本地，無額外費用） |
| PDF 處理 | PyMuPDF4LLM、Azure Document Intelligence、LlamaParse |
| 認證 | Firebase Admin SDK（JWT 驗證） |
| 檔案儲存 | Cloudinary（PDF 靜態資源） |

### 前端

| 類別 | 技術 |
|------|------|
| 框架 | React 18 + TypeScript |
| 樣式 | Tailwind CSS |
| 圖表 | ECharts（雷達圖、Pie、折線、WordCloud） |
| PDF 渲染 | react-pdf（PDF.js） |
| 串流 | SSE（Server-Sent Events） |
| 認證 | Firebase Authentication（Google / Email） |
| 部署 | Firebase Hosting（CDN） |

---

## 四、雲端部署架構

```
┌──────────────────────────────────────────────────────────┐
│                    Internet                               │
└────────┬───────────────────────────────┬─────────────────┘
         │ 靜態資源                        │ API 請求
         ▼                               ▼
┌─────────────────┐             ┌──────────────────────┐
│  Firebase       │             │   Render             │
│  Hosting        │             │   (Python FastAPI)   │
│  CDN 全球分發   │             │   單 Worker 容器     │
│  自動 HTTPS     │ ──API──►   │   WEB_CONCURRENCY=1  │
│  GitHub Actions │             │   因 SSE 串流鎖限制  │
│  CI/CD 自動部署 │             └────────┬─────────────┘
└─────────────────┘                      │
                              ┌──────────┼──────────────┐
                              ▼          ▼              ▼
                    ┌──────────────┐ ┌───────────┐ ┌───────────┐
                    │  Neon        │ │  Qdrant   │ │  Azure    │
                    │  PostgreSQL  │ │  Cloud    │ │  OpenAI   │
                    │  ap-se-1     │ │  us-w-1   │ │  API      │
                    │  對話記錄    │ │  9 個集合 │ │  GPT/Emb  │
                    │  使用者分析  │ │           │ └───────────┘
                    │  LangGraph   │ └───────────┘
                    │  checkpoint  │
                    └──────────────┘
                              │
                    ┌─────────┴─────────┐
                    │     Firebase      │
                    │  Authentication   │
                    │  身份驗證 JWT     │
                    └───────────────────┘
```

### Qdrant Collections（共 9 個，us-west-1）

| Collection | 用途 | 向量數 |
|-----------|------|------|
| `ncu_courses_ug` | 大學部課程（課綱 + NLP 語意 + 教師專長） | **3,547** |
| `ncu_courses_grad` | 研究所課程 | **974** |
| `ncu_teachers` | 教師資料（專長 + 開課） | **1,009** |
| `ncu_departments` | 舊 collection，agent 目前不可存取 | **暫停** |
| `ncu_credit_programs` | 學分學程說明 | **42** |
| `ncu_graph_nodes` | 知識圖譜節點（Query Expansion 種子；概念 / 技術 / 領域 / 核心能力 / 課程） | **25,071** |
| `reports` | PDF 研究計畫 chunks | **14,815** |
| `research_memories` | （預留，目前 0 筆） | 0 |
| `ncu_query_embedding_cache` | 常用查詢的 embedding 快取（`course_info_service.py`，避免重複呼叫 Embedding API） | **2** |

### CI/CD 流程

```
開發者 Push → GitHub
  ├─ PR：firebase-hosting-pull-request.yml → 預覽網址
  └─ Merge main：firebase-hosting-merge.yml → 正式部署
```

---

## 五、資料整合與處理 Pipeline

### 5.1 課程資料 Pipeline

```
原始資料（課程資訊 PDF/HTML + 教育部 CSV）
  │
  ▼
資料清洗 & 結構化
  ├─ 修課條件解析（parse_distribution_conditions.py）
  ├─ 畢業規定解析（parse_requirements_table.py）
  └─ 學程說明抽取（extract_program_descriptions.py）
  │
  ▼
NLP 萃取 Pipeline（Qwen3-14B-AWQ 本地推理）
  ├─ Agent 1：技術 & 概念節點萃取（FULL 類課程）
  ├─ Agent 2：研究領域關聯標記 + 教師專長比對
  ├─ Agent 3：通識課主題分類（TOPICS_ONLY 類）
  └─ Agent 4：概念高中生友善化解釋
  │
  ▼
Embedding & 向量化
  ├─ text-embedding-3-large 生成 dense vector（3072 維）
  └─ FastEmbedSparse 生成 BM25 sparse vector
  │
  ▼
Qdrant 上傳（ncu_courses_ug / ncu_courses_grad / ncu_teachers /
             ncu_departments / ncu_credit_programs / ncu_graph_nodes）
  │
  ▼
知識圖譜建構（詳見第六節）
  ├─ Phase 1：機構結構骨架（系所 / 課程 / 必修邊）
  └─ Phase 2：NLP 語意節點補入（概念 / 技術 / 領域 / 教師）
```

### 5.2 PDF 研究計畫 Pipeline

PDF 多解析器攝入（pymupdf4llm → Azure DI → LlamaParse 依品質自動升級）、向量化上傳至 `reports` collection、三步驟 AI 摘要生成（Research Agent RAG → CoreExtractionResponse + QuestionGenerationResponse 並行），完整架構詳見**第八節 8.2**。

---

## 六、知識圖譜與向量資料庫

### 6.1 知識圖譜結構

```
圖規模：27,447 節點 / 103,111 邊

節點類型：
  Concept（知識概念）  16,985  ← NLP 萃取最大類
  Course（課程）        3,982
  Field（研究領域）     2,760
  Instructor（教師）    1,187
  Technology（技術工具）  895
  Competency（核心能力）  458
  Domain（課程大領域）    359
  CreditProgram（學程）    42
  其他機構結構節點        779  （College / Department / GraduationRule 等，與期中相比未變）

邊類型：
  COVERS       課程 → 概念（23,527 條）
  DEVELOPS     課程 → 核心能力（21,608 條）
  SIMILAR_TO   同義概念（15,966 條）
  COURSE_EXPERT 教師擅長課程（10,991 條）
  COVERS_FIELD 課程 → 領域（10,694 條）
  EXPERT_IN    教師官方研究領域（2,629 條，與期中相比未變）
  其他         REQUIRES / TAUGHT_BY / IN_DOMAIN / 機構結構邊等（17,696 條）
```

### 6.2 兩階段建置

```
Phase 1（結構骨架）
  課程 JSON + 應修科目表 + 學分學程
    → University / College / Department
    → Course / GraduationRule / CreditProgram
    → 必修課程邊 + 學程邊

Phase 2（語意補入，各步驟獨立）
  2-a 教師全量補建   114_ulistteacher.csv → 1,187 個 Instructor + EXPERT_IN 邊
  2-b NLP 語意節點   Agent 1/2 結果 → Field / Technology / Concept + 相關邊
  2-c 教授課程關聯   nlp_professor_links.json → COURSE_EXPERT 邊
  2-d 修課先修條件   course_eligibility.json → PREREQUISITE_OF 邊
  2-f 同義概念邊     Jaro-Winkler ≥ 0.70 → 15,966 條 SIMILAR_TO 邊
```

### 6.3 PPR（Personalized PageRank）

```
演算法：igraph Weighted PPR（damping=0.85，directed=False）
效能：約 0.23 秒/次（in-memory）
種子策略：雙軌並行
  ├─ Qdrant ncu_graph_nodes 語意搜尋（Concept/Tech/Field）
  └─ 字串精確比對（Course 節點直達）

輸出模式（focus）：
  course     → 僅回傳課程節點
  instructor → 僅回傳教師節點
  dept       → 僅回傳系所節點
  overview   → 課程+教師+系所+學程分組顯示
```

---

## 七、核心功能一：課程智能助理

### 7.1 ReAct 對話流程

```
使用者輸入問題
       │
       ▼
┌─────────────────────────────────────┐
│         LLM（gpt-5.4-mini）         │
│  System Prompt：選課助理角色 +      │
│  工具選用指引 + 防幻覺規則          │
│  多輪對話歷史（最多 4 輪）          │
└────────────────┬────────────────────┘
                 │ 決定工具
    ┌────────────┼──────────────────┐
    │            │                  │
    ▼            ▼                  ▼
並行工具執行（ThreadPoolExecutor）
    │
    ├─ search_courses（多 Signal RRF）
    ├─ get_dept_courses（圖查詢）
    ├─ get_course_detail（課綱+資格）
    ├─ ppr_explore（PPR 廣泛探索）
    ├─ get_depts_by_tech（技術→系所）
    ├─ get_graduation_requirements
    ├─ find_similar_courses（RRF）
    └─ … 共 13 個啟用工具
         │
         ▼
  工具結果注入 messages
         │
         ▼
  LLM 生成最終回答
  （含 <course> 標籤）
         │
         ▼
  課程卡片萃取流水線
  ├─ _extract_courses_from_tags()
  ├─ _enrich_course_cards()（補齊欄位）
  └─ course_cards 回傳給前端
```

### 7.2 search_courses 多 Signal RRF 架構

```
使用者查詢（query）
       │
       ▼
[tech 查詢？] ─是─► Graph 精確查詢 → 直接回傳
       │ 否
       ▼
Layer 1：Query Expansion
  ├─ Track A：Concept/Tech/Field 節點語意搜尋 → expanded_query
  └─ Track B：Course 節點（score≥0.65） → Signal C

Layer 2：Qdrant Filter 構建
  （dept / college / course_type / tech / topic_tag / student_college）

Layer 3：RRF 融合（K=60）
  Signal A  = expanded_query + filters
  Signal B  = original query + filters（若有擴展）
  Signal excl = is_open_with_exclusions 排除型課程補回
  Signal N  = 課名完全匹配 boost（+0.15）
  Signal N2 = 課名部分匹配 boost（+0.05）
  Signal C  = 圖 Course 節點直達 boost
       │
       ▼
  排名 → exact_match 置頂 → 前 n 筆輸出
```

### 7.3 暴露給 LLM 的 13 個工具

| 工具名稱 | 資料來源 | 主要用途 |
|---------|---------|---------|
| `search_courses` | Qdrant + Graph | 多 Signal 語意搜尋 |
| `get_dept_courses` | Graph | 系所必修/選修清單 |
| `get_course_detail` | Qdrant | 課綱 + 修課資格 + 先修（三合一） |
| `get_dept_info` | 暫停 | `ncu_departments` 內容與向量策略確認前不暴露給 LLM |
| `get_graduation_requirements` | JSON + Graph | 畢業規定（二合一） |
| `get_program_info` | JSON + Graph | 學程說明 + 課程（二合一） |
| `search_programs` | Qdrant | 學程語意搜尋 |
| `find_similar_courses` | Graph + Qdrant | 概念相近課程（RRF） |
| `get_course_knowledge_map` | Graph + Qdrant | 課程知識地圖 |
| `get_depts_by_tech` | Graph | 技術 → 系所多跳查詢 |
| `ppr_explore` | Graph（PPR） | 廣泛圖探索（課程/教師/系所） |
| `explore_concept_neighborhood` | Graph | N 跳 BFS 概念鄰域 |
| `get_teacher_info` | Qdrant + Graph | 教師專長 + 開課 |
| `search_teachers` | Qdrant | 教師語意搜尋 |

### 7.4 SSE 串流事件

```
tool_start  → 工具開始（含 args）
tool_done   → 工具完成（count、scores）
tool_result → 字串型工具回傳預覽
token       → LLM 逐字輸出
verify_start → 課程卡片驗證開始
verify_done  → 驗證完成（selected / filtered_out）
done        → 完整結果（course_cards、debug_trace）
error       → 錯誤訊息
```

---

## 八、核心功能二：PDF 研究計畫系統

### 8.1 三個子功能

```
PDF 資料集（104–114 學年度大專研究計畫）
       │
 ┌─────┼───────────────────────────┐
 │     │                           │
 ▼     ▼                           ▼
大綱  興趣量表                  PDF 問答
呈現  （高中生）                  (RAG)
(靜態) (AI 生成)                 (即時)
```

### 8.2 資料準備流程

#### 計畫列表（大綱顯示用）

`data/processed/projects.json` 存放所有計畫的基本 metadata，資料結構來自 PDF 檔名的命名規則（`{學年度}{類型}{編號}_{學生姓名}_{研究題目}.pdf`），共約 459 筆，涵蓋 38 個系所。

`scripts/populate_document_ids.py` 負責將 `projects.json` 的每筆計畫與 PostgreSQL `pdf_documents` 表做比對（ilike 精確 / NFKC 正規化 / prefix 三段策略），寫入 `documentId`，供前端選取計畫時呼叫 PDF 問答 API。

#### PDF 存取（Cloudinary）

```
scripts/storage/upload_pdfs_to_cloudinary.py
  ├─ 掃描 data/raw/projects/104-114/ 下所有 PDF
  └─ 上傳至 Cloudinary（Raw 類型，保留原始資料夾結構）
       → 後端 build_pdf_url() 組出 CDN URL
       → 前端 PdfViewer 直接串流顯示原始 PDF
```

#### PDF 攝入與向量索引（report-agent branch）

```
PDF 原始檔
  │
  ▼
多解析器策略（依品質自動升級）
  ├─ pymupdf4llm（本地，速度快）→ 品質合格直接使用
  ├─ Azure Document Intelligence → 版面複雜 / 含公式
  └─ LlamaParse → 前兩者均不合格時使用
  │
  ▼
章節偵測 + 語意切 chunk + 品質過濾
（自動排除封面頁 / 目錄頁 / 參考文獻頁 / 純表格頁）
  │
  ▼
Dense（text-embedding-3-large）+ Sparse BM25 embedding
  → 上傳至 Qdrant Collection: reports（14,815 筆）
  → pdf_documents 表寫入 filename / document_id
```

#### 興趣量表題目（三步驟 AI Pipeline）

```
Step 1：Research Agent（RAG 取證，最多 10 次搜尋）
  ├─ 與 PDF 問答共用同一個 Research Agent（LangGraph）
  ├─ 固定問題：整理研究動機、方法、成果、限制
  ├─ coverage-driven，確保四個面向都有文件依據
  └─ → raw_research_answer（高資訊量研究摘要）

Step 2 + Step 3（並行執行）：
  Step 2：CoreExtractionResponse（Structured Output）
    → {motivation≤200字, method≤200字, results≤200字, tags×3~5}

  Step 3：QuestionGenerationResponse（Structured Output）
    → intro（100~220字高中生導讀）
    → questions×3（每題45~80字，三種吸引力題型）

→ 合併寫入 document_extractions 表
→ Job Queue 管理批次進度，支援 step1/2/3 各別重跑
```

### 8.3 PDF RAG 問答：多 Agent 對話流程

```
POST /projects/{id}/chat/stream
       │
       ▼
[Quota Check + Stream Lock]（防重複並行）
       │
       ▼
┌──────────────────────────────────────────┐
│         Router Agent（gpt-4o-mini）       │
│  輸入：問題 + 文件摘要 + 前輪 agent 狀態 │
│  輸出：chat / retrieval / research        │
└──────────────┬───────────────────────────┘
               │
    ┌──────────┼────────────────┐
    ▼          ▼                ▼
┌────────┐ ┌──────────┐ ┌──────────────────┐
│ Chat   │ │Retrieval │ │    Research      │
│ Agent  │ │ Agent    │ │    Agent         │
│        │ │          │ │  (LangGraph)     │
│簡單對話│ │單次 RAG  │ │多步驟研究        │
│無 RAG  │ │搜尋+回答 │ │max 14 次搜尋     │
└────┬───┘ └────┬─────┘ └──────┬───────────┘
     │          │               │
     │  [INSUFFICIENT_CONTEXT]  │
     │     → 自動升級          │
     └────────────┬────────────┘
                  │
                  ▼
           SSE 串流輸出
```

### 8.4 Research Agent 內部狀態機（LangGraph）

```
create_research_plan()
 ├─ 動態生成 coverage_items（最多 6 個）
 ├─ 每個 item：id / label / description / search_hints / use_hyde
 └─ 情境計畫：student / method / result / summary

       ▼
┌────────────────────────────────────┐
│           研究迴圈                  │
│  ┌─────────┐    ┌──────────────┐  │
│  │ planner │───►│  scheduler   │  │
│  │ 決定下一│    │ 執行搜尋     │  │
│  │ 搜尋目標│    │（含 HyDE）   │  │
│  └─────────┘    └──────┬───────┘  │
│       ▲                │          │
│       │          ┌─────▼──────┐   │
│       │          │ reflector  │   │
│  [未滿足]◄────── │ 評估覆蓋度 │   │
│                  └─────┬──────┘   │
│                  [已滿足]         │
└─────────────────────┬─────────────┘
                      ▼
               ┌──────────┐
               │  writer  │
               │ 生成最終 │
               │ 回答     │
               └──────────┘
```

### 8.5 向量搜尋配置（Collection: `reports`）

| 設定 | 值 |
|------|---|
| 密集向量 | text-embedding-3-large（3072 維） |
| 稀疏向量 | BM25（FastEmbedSparse） |
| 語言自動偵測 | zh / en / mixed |
| 混合語言策略 | Dense + Hybrid 雙路搜尋，interleave 候選 |
| Reranking | FlashrankRerank（候選 36 → 保留 4） |
| 品質過濾 | 自動排除封面頁、參考文獻頁、表格/公式頁 |

### 8.6 興趣量表架構

#### 資料來源（report-agent branch 三步驟 AI Pipeline 生成）

```
PDF 已攝入 Qdrant
        │
        ▼
Step 1：Research Agent（RAG 取證，最多 10 次搜尋）
        │  raw_research_answer
        ▼
Step 2 ──────────────────────── Step 3
CoreExtractionResponse          QuestionGenerationResponse
  {motivation, method,            {intro（100~220字導讀）,
   results, tags×3~5}             questions×3（45~80字/題）}
        │                                   │
        └──────────────┬────────────────────┘
                       ▼
           document_extractions 表
           summary_json：{motivation, method, results,
                          tags, intro, questions}
                       │
                       ▼
           GET /api/assessment/questions
                       │
                       ▼
              前端 AssessmentPage
```

#### 題目結構（每份研究計畫一筆）

```json
{
  "department": "資訊工程學系",
  "title":      "真實研究計畫標題",
  "intro":      "100~220字，高中生導讀文字（Step 3 生成）",
  "questions":  [
    "情境吸引力題目（45~80字）",
    "研究方式吸引力題目（45~80字）",
    "思考方式吸引力題目（45~80字）"
  ],
  "tags":       ["機器學習", "影像辨識", "深度學習"],
  "mode":       "grade1 | grade2 | grade3"
}
```

`mode` 依研究計畫學年度自動決定：≤ 107 → grade1；108–110 → grade2；111+ → grade3

#### 三種年級模式

| 模式 | 對象 | 篩選邏輯 |
|------|------|---------|
| `grade1`（高一） | 高一 | 無分組限制，廣泛探索各科系 |
| `grade2`（高二） | 高二 | 可依文組 / 理組過濾題目 |
| `grade3`（高三） | 高三 | 可依學測選考科目（`ncu_caac.csv`）篩選符合條件的科系 |

#### 前端評分流程

```
1. 選年級模式（grade1 / grade2 / grade3）
2. （grade2）選文組 / 理組
3. （grade3）選學測科目 → 系統過濾符合學測條件的科系
4. 逐題閱讀 intro（導讀）+ 三個問題，各評 1~5 分
5. POST /api/assessment/submit
6. 後端依科系累加平均分 → 回傳前 15 名推薦科系
7. 顯示各科系學院分類（文學院 / 理學院 / 工學院...）+ matchedTags
```

---

## 九、核心功能三：個人學習傾向分析

> **期末新增功能** | 路由：`/profile` | 需 Firebase JWT 登入

### 9.1 功能定位

- 從使用者的**對話歷史**自動萃取學習傾向，無需填寫問卷
- 整合**課程助理**（選課行為）與**研究計畫**（PDF 問答）兩個系統的資料
- 資料每 **15 分鐘**自動更新快取；統計數字載入時以滾動動畫呈現

### 9.2 三分頁架構

```
[ 全部 ]  ── 課程 + 研究計畫跨系統綜合分析
[ 課程助理 ]  ── 選課行為、興趣領域、工具使用
[ 研究計畫 ]  ── 論文閱讀、提問深度、主題分佈
```

切換時懶載入（第一次進入才呼叫 API），「全部」等兩個來源都到齊才同步顯示。

---

### 9.3 各 Tab 圖表內容

| Tab | 概覽卡片 | 圖表 |
|-----|---------|------|
| **課程助理** | 對話數、提問輪次、探索課程數、最常探索學院 | 六軸雷達圖、探索型態診斷、詞雲（興趣標籤 / 課程領域）、系所圓餅圖、通識分類環形圖 |
| **研究計畫** | 對話數、提問次數、探索論文數、平均對話深度 | 六軸雷達圖、探索型態診斷、系所圓餅圖、對話深度長條圖、論文列表、最近提問 |
| **全部** | 課程對話數、研究計畫對話數、課程提問數、探索論文數 | 綜合雷達圖（加權 / 等比切換）、使用偏好比例條、綜合探索型態診斷 |

**六軸雷達圖** 將所有學院對應至六個學術領域軸（理工 / 資訊 / 地科 / 生醫 / 人文 / 商管），課程與研究計畫使用相同座標系，方便比較。

---

### 9.4 探索型態診斷

三套診斷邏輯各自對應一個分頁，依使用行為自動歸類。

**課程助理（5 型）**

| 型態 | 特徵 |
|------|------|
| 規劃導向型 | 規劃工具使用量佔比 ≥ 25% |
| 跨域探索型 | ≥ 3 個學術領域各自佔比 ≥ 15% |
| 專注深挖型 | 最集中系所佔比 > 55% |
| 廣泛探索型 | 涉足系所 ≥ 6 個，且集中度低 |
| 均衡探索型 | 預設 |

**研究計畫（5 型）**

| 型態 | 特徵 |
|------|------|
| 深度鑽研型 | 平均對話深度高、論文數少 |
| 跨域探索型 | 涉及 ≥ 3 個學院 |
| 廣泛涉獵型 | 論文數多但對話淺 |
| 專注研究型 | 70%+ 論文來自同一系所 |
| 均衡探索型 | 預設 |

**綜合（6 型）** — 以兩系統互動比例 + 深度指標分流

| 型態 | 特徵 |
|------|------|
| 學術研究導向 | 研究計畫互動佔比 > 60% |
| 課程規劃導向 | 課程助理佔比 > 60%，偏規劃工具 |
| 課程探索導向 | 課程助理佔比 > 60%，廣泛選課 |
| 跨域整合型 | 兩系統合計橫跨 ≥ 3 學院 |
| 廣泛探索型 | 兩系統合計涉足系所 ≥ 8 個 |
| 均衡發展型 | 預設 |

---

## 十、核心功能四：系統監控儀表板

> **期末新增功能** | 路由：`/monitor` | 僅限開發人員（`DEVELOPER_EMAILS`）

### 10.1 功能定位

Grafana 風格深色主題儀表板，整合所有外部服務狀態，無需登入外部平台即可掌握系統健康與使用量。

### 10.2 存取控制

```
後端：DEVELOPER_EMAILS 環境變數（逗號分隔）
  → GET /api/monitor/* 驗證 Firebase JWT 後比對 email
  → 非開發人員 → 403 Forbidden

前端：VITE_DEVELOPER_EMAILS
  → isDeveloper(email) 比對
  → 非開發人員 → 自動導向首頁
  → Navbar 不顯示監控入口
```

### 10.3 頁面結構（5 個 Tab）

```
固定頂欄：
  系統監控  ● 正常    ● Render  · ● PostgreSQL  · ● Qdrant  · ● /api/chat 142ms
  [30s][1m][5m]  [↺手動刷新]

Tab 1 總覽
  ├─ 全時期合計（去重用戶 / 兩功能費用分列 / 合計費用 / 合計 Turns）
  ├─ 時段選擇器（1d / 7d / 30d / 自訂日期）
  ├─ 課程推薦 vs 大專生計畫 時段對比卡
  └─ 課程推薦 Token 用量趨勢折線圖

Tab 2 課程推薦
  ├─ Model 資訊（gpt-5.4-mini Global，$0.75 / $4.50 per 1M）
  ├─ Latency 統計網格（avg / P95 / P99 / min / max）
  ├─ Latency 趨勢圖
  ├─ Token 用量趨勢 + 每日費用 & 每輪 Token 圖
  └─ 工具使用統計（目前 13 個啟用工具水平條形圖）

Tab 3 大專生計畫
  ├─ 時段統計列（活躍用戶 / 輪次 / Token / 平均深度 / 趨勢 %）
  ├─ Model 費用拆分卡（gpt-4o agent vs gpt-4o-mini router）
  ├─ Agent 路由分佈圓餅圖（直接回答 / 文件搜尋 / 深度研究）
  ├─ 各 Agent Latency 橫條圖（avg / P95）
  ├─ 端到端 Latency 統計 + 趨勢圖
  ├─ Token 趨勢（4 系列：Agent Input/Output + Router Input/Output）
  └─ 全時期累計統計列

Tab 4 資料庫
  ├─ 基礎設施概覽卡片（Render uptime / PostgreSQL / Qdrant / Cloudinary）
  ├─ PostgreSQL 詳細統計（DB 大小 / 連線數 / Cache Hit %）
  ├─ Qdrant Collections 列表（各 collection 點數 / 狀態）
  └─ Cloudinary 儲存統計

Tab 5 對話活動
  ├─ Feature 切換器（合計 / 課程推薦 / 大專生計畫）
  ├─ 用戶概況（活躍 / 新用戶 / 回訪 / 平均深度）
  ├─ 對話活動趨勢（Sessions & Turns 雙柱）
  ├─ 課程推薦工具使用統計（非大專生計畫模式才顯示）
  └─ 課程推薦活躍時段分布（24 小時柱狀圖）
```

### 10.4 監控資料架構

```
資料來源 1：PostgreSQL（持久化）
  課程推薦
    chat_turns.input_tokens / output_tokens / llm_latency_ms / model_name

  大專生計畫（Alembic migration 003 新增）
    pdf_agent_messages.input_tokens / output_tokens
    pdf_agent_messages.router_input_tokens / router_output_tokens
    pdf_agent_messages.latency_ms / model_name

資料來源 2：In-Process（非持久化）
  latency_store.py
  └─ deque(maxlen=500) ← LatencyMiddleware 記錄所有 /api/* 請求
  └─ 程序重啟後重置（適合短期觀察）

資料來源 3：外部 API（即時查詢，600s 快取）
  ├─ PostgreSQL pg_stat 表（連線數、Cache Hit%、DB 大小）
  ├─ Qdrant REST API（collections 列表、點數）
  └─ Cloudinary SDK（usage API）
```

**PDF Token 收集機制**：PDF 功能一次請求涉及多個 LLM 呼叫（router + agent），使用 Python `contextvars.ContextVar` 在 `route_agent_stream()` 整個執行期間維持累積器，router token 以 `include_raw=True` 單獨擷取，agent token 透過 `@after_model` 中介層累積，結束時一併寫入資料庫。

### 10.5 成本計算

| 模型 | 用途 | Input | Output |
|------|------|-------|--------|
| gpt-5.4-mini | 課程推薦 | $0.75 / 1M | $4.50 / 1M |
| gpt-4o | PDF agent | $2.50 / 1M | $10.00 / 1M |
| gpt-4o-mini | PDF router | $0.15 / 1M | $0.60 / 1M |

PDF 費用在 Python 層以 agent 與 router token 分開計算後加總，不存入資料庫。

### 10.6 時段查詢快取策略

| 查詢模式 | 快取 TTL |
|---------|---------|
| `preset=1d` | 60 秒 |
| `preset=7d` | 300 秒（5 分鐘） |
| `preset=30d` | 600 秒（10 分鐘） |
| 自訂日期 | 不快取（即時） |

快取 key 包含 `feature` 參數（`all` / `course` / `pdf`），避免不同查詢範圍汙染快取。

---

## 十一、期中到期末的演進

### 期中已完成

- 課程智能助理基礎架構（ReAct + 13 個啟用工具）
- 知識圖譜建構（27,447 節點）
- PDF 問答系統（多 Agent + LangGraph）
- 興趣量表（資料 Pipeline + 前端評測流程）
- 雲端部署（Firebase + Render + Neon + Qdrant Cloud）

### 期末新增 / 強化

| 項目 | 說明 |
|------|------|
| **個人學習傾向分析** | `/profile` 頁面，從對話歷史自動萃取傾向，六角雷達圖、探索型態診斷、WordCloud、15 分鐘快取 |
| **系統監控儀表板** | `/monitor` 開發者儀表板，5 個 Tab 分別監控課程推薦與大專生計畫，含三種 Model 費用拆分、Agent 路由分佈、端到端 Latency 追蹤 |
| **search_courses 多 Signal RRF** | Signal A/B/excl/N/N2/C 六路融合，exact_match 置頂，student_college 可修性驗證 |
| **chat_turns 欄位擴充** | 新增 `input_tokens`、`output_tokens`、`llm_latency_ms`，支援費用與延遲分析 |
| **topic_tag 過濾** | `search_courses` 新增通識課 topic_tags 精確過濾（25 種標籤） |
| **PPR 種子確認（E2-a）** | 全部找不到時回傳建議訊息，部分找不到時末尾加警告 |
| **整合工具設計** | `get_course_detail`（三合一）、`get_graduation_requirements`（二合一）、`get_program_info`（二合一），減少工具呼叫輪次 |
| **課程卡片萃取流水線** | `<course>` 標籤 + fuzzy match + 自動修正幻覺課名，取代原有 LLM 驗證機制 |

---

## 附錄：資料庫 Schema 摘要

```sql
-- 課程助理核心表
chat_sessions  (id, user_id, created_at, ...)
chat_turns     (id, session_id, user_question, answer,
               course_pool JSONB, course_cards JSONB,
               tools_used JSONB,
               input_tokens INT,     ← 期末新增
               output_tokens INT,    ← 期末新增
               llm_latency_ms INT)   ← 期末新增
user_analytics (user_id, data JSONB, computed_at)

-- PDF 問答表
pdf_documents      (id, filename, abstract_text, status)
pdf_conversations  (id, thread_id, user_id, document_id,
                   message_count, last_agent_name)
pdf_agent_messages (id, message_id, thread_id,
                   user_question, agent_answer,
                   sources JSONB, trace_summary JSONB)

-- report-agent branch（摘要與問題生成）
documents            (id, filename, file_path, status, abstract_text, batch_status)
document_extractions (id, document_id,
                     summary_json JSONB,       -- {motivation, method, results, tags, intro, questions}
                     raw_research_answer TEXT,
                     raw_research_sources JSONB,
                     tags JSONB, category, department_hint)
job_history          (id, job_id, doc_id, filename, job_type,
                     status, stage, stage_log, error,
                     started_at, updated_at, completed_at)
```

---

*本報告由程式碼自動分析生成，對應版本：2026-09-08*
