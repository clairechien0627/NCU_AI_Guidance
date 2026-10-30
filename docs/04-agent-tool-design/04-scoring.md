# 評分系統

> 文件版本：2026-05-01

系統共有四種分數，分別來自不同的資料源和算法。

---

## 1. Qdrant `distance`（余弦距離）

### 算法

**Cosine Distance**（1 − cosine similarity）：

```
distance = 1 − (A · B) / (‖A‖ × ‖B‖)
```

Qdrant 使用余弦距離（`Distance.COSINE`），所有 collection 均以此計算。`_fmt_search()` 回傳：

```python
"distance": round(max(0.0, 1.0 - h.score), 4)  # h.score 為 cosine similarity
```

### 特性

| 特性 | 說明 |
|------|------|
| 範圍 | 0 ~ 2（理論上限 2，實際罕見超過 1.2） |
| 0 的含義 | 完全相同的向量（方向一致） |
| **越小越好** | distance 越小 = 語意越相似 |
| 正規化向量 | text-embedding-3-large 輸出已 L2 normalize，distance ∈ [0, 1] 實際常見 |

### 典型數值參考

| distance 範圍 | 語意相似度 |
|-------------|-----------|
| 0.0 ~ 0.25 | 非常相似（幾乎相同主題） |
| 0.25 ~ 0.5 | 相關（主題相近） |
| 0.5 ~ 0.75 | 弱相關 |
| > 0.75 | 不相關 |

> text-embedding-3-large（dim=3072）在余弦空間通常比 L2 分布更集中，閾值比舊 ChromaDB L2 小很多。

### 使用工具

- `search_courses`（Signal A + B 各自取 distance）
- `search_teachers`
- `get_dept_info`（目前停用，不參與排序）
- `search_programs`

---

## 2. RRF score（Reciprocal Rank Fusion）

### 算法

**RRF（Cormack, Clarke, Buettcher, 2009）**：

```
RRF_score(d) = Σ_i  1 / (k + rank_i(d))
k = 60（常數，防止高排名主宰結果）
```

將多路信號的排名融合為單一分數，**不需要信號分數可比**。

### 適用場景

| 工具 | 融合信號 |
|------|---------|
| `search_courses` | Signal A（expanded query）+ Signal B（original query） |
| `find_similar_courses` | 圖共概念排名 + Qdrant 向量排名 |
| `get_course_knowledge_map` | 同 find_similar_courses（共用 `_rrf_similar_courses()`） |

### `_rrf_similar_courses()` — 共用 helper

`find_similar_courses` 和 `get_course_knowledge_map` 共用同一函式，確保兩工具結果一致：

```python
def _rrf_similar_courses(course_name: str, top_n: int = 15) -> list[dict]:
    graph_results  = graph_service.search_courses_by_concept_cluster(course_name, top_n=25)
    vector_results = retriever.search_courses(course_name, n_results=20)
    RRF_K = 60
    # 對兩路結果計算 1/(60+rank)，按課名合併，排除自身後取前 top_n
```

### 特性

| 特性 | 說明 |
|------|------|
| 範圍 | > 0（理論上限 N × 1/61 ≈ N/61） |
| **越大越好** | 出現在多路信號且排名高的結果分數更高 |
| 無需正規化 | 不同信號的原始分數不需可比 |
| 穩健性高 | 對異常排名不敏感（k=60 是平滑底） |

---

## 3. 知識圖譜 `shared_concepts`（共享概念數）

### 算法

**概念節點交集大小**：

```
shared_concepts(A, B) = |concepts(A) ∩ concepts(B)|
```

其中 `concepts(X)` 是課程 X 的 `COVERS`/`TEACHES`/`COVERS_FIELD` 出向邊所連到的 Concept/Technology/Field 節點集合。

### 計算流程

1. 找種子課程 A 的所有概念節點集合 `S`
2. 從 `S` 的每個概念節點走反向邊，找有連到它們的其他課程 B
3. 計算每門課 B 與 A 的共享概念數

### 特性

| 特性 | 說明 |
|------|------|
| 範圍 | 0 ~ ∞（整數） |
| **越大越好** | 共享概念越多 = 課程越相似 |
| 完全圖結構決定 | 只取決於知識圖譜中標注的概念邊 |

### 典型數值參考

| shared_concepts | 課程相似程度 |
|----------------|------------|
| 0 | 無共享概念（不相關） |
| 1 ~ 2 | 弱相關 |
| 3 ~ 5 | 相關（有共同主題） |
| 6 ~ 10 | 高度相關 |
| 10+ | 幾乎同一主題 |

### 使用工具

- `find_similar_courses`：作為圖信號輸入 RRF，最終展示含 `[共享概念：N 個]`
- `get_course_knowledge_map`：相似課程段落（top 8），同上

### 限制

- 只計算知識圖譜中**已建立概念邊**的課程，未收錄的課程不會出現
- `shared_concepts` 高不等於語意距離近（可能只是共享邊緣通識概念）
- 已藉 RRF 融合 Qdrant 向量分數修正此問題

---

## 4. PPR `score`（Personalized PageRank 分數）

### 算法

**igraph Weighted PPR**（`personalized_pagerank(directed=False, damping=0.85, weights="weight")`）：

```
PPR(v) = α × Σ [PPR(u) × w(u,v) / weighted_degree(u)] + (1-α) × personalization(v)
```

其中：
- `α = 0.85`（阻尼係數）
- `personalization[seed_id] = 1/len(seeds)`（種子節點均分）
- 邊權重：`TEACHES=1.2`，`COVERS=1.0`，`PREREQUISITE_OF=0.8`，其餘 0.3~0.5

### 輸出分數

```python
score = round(raw_ppr_score * 1000, 4)  # 乘以 1000 讓數字可讀
```

### Gap Truncation

```python
# graph_service._ppr_gap_filter()
mean_s, std_s = statistics.mean(scores), statistics.stdev(scores)
threshold = mean_s - 0.5 * std_s
filtered = [r for r in results if r['score'] >= threshold]
```

排名尾部噪音（score < mean − 0.5σ）會被自動移除。少於 4 筆時不截斷。

### 特性

| 特性 | 說明 |
|------|------|
| 範圍 | 0 ~ 1000（原始 PPR × 1000） |
| **越大越好** | 分數越高 = 與種子概念越相關 |
| 跨類型比較 | 可同時比較課程、教師、系所、概念節點 |
| 多跳擴散 | 能捕捉間接關係（A→B→C 的間接相關性） |

### 典型數值參考

| score 範圍 | 相關程度 |
|-----------|---------|
| 100 ~ 1000 | 強相關（直接連接或高度間接相關） |
| 10 ~ 100 | 相關 |
| 1 ~ 10 | 弱相關 |

### 使用工具

- `ppr_explore`：`top_k=15` + gap truncation，回傳字串中含 `[PPR: score]`

---

## 分數比較速查

| 分數 | 越大/越小越好 | 跨工具可比 | 使用工具 |
|------|------------|----------|---------|
| Qdrant `distance` | 越小越好 | 同 collection 可比 | search_courses, search_teachers（get_dept_info 目前停用） |
| RRF score | 越大越好 | 同次融合可比 | search_courses, find_similar_courses, get_course_knowledge_map |
| `shared_concepts` | 越大越好 | 課程間可比 | find_similar_courses, get_course_knowledge_map |
| PPR `score` | 越大越好 | 同次 PPR 可比 | ppr_explore |
