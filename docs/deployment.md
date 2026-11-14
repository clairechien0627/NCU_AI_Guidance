# 部署與環境設定

本文件涵蓋環境變數設定、本地開發、雲端部署流程與資料部署原則。專案介紹請見根目錄
[README.md](../README.md)。

### 後端部署方式總覽

| 方式 | 適用情境 | 說明文件 |
|---|---|---|
| Render（Python runtime） | 目前的正式站；免費方案記憶體較小 | 本文件「部署流程」 |
| Docker 映像（根目錄 `Dockerfile`） | 任何能跑容器的環境，下面兩種做法都用它 | 本文件「後端容器映像」 |
| Oracle Cloud VM + Cloudflare Tunnel | 單台 VM、Docker Compose，不開對外 port | [oracle-vm-deployment.md](oracle-vm-deployment.md)（範例檔在 `deploy/oracle/`） |
| AWS ECS Fargate + ALB | 受管容器服務，CloudFormation 建置，可選 GitHub Actions 自動部署 | [aws-deployment.md](aws-deployment.md)（範本在 `deploy/aws/`） |

不論哪一種，後端都必須維持**單一 process、單一 worker**（`WEB_CONCURRENCY=1`），原因見 README「系統架構」。

---

## 環境需求

- Python 3.10+
- Node.js 18+
- npm 9+

---

## 環境變數

### 根目錄 `.env`

給本地後端與工具腳本使用：

```env
APP_ENV=development
APP_PORT=8000
DATA_DIR=data
COURSE_JSON_PATH=data/processed/courses.json
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:5174,http://localhost:3000

CLOUDINARY_CLOUD_NAME=
CLOUDINARY_API_KEY=
CLOUDINARY_API_SECRET=
CLOUDINARY_PROJECT_FOLDER=ncu-ai-guidance/data/raw/projects
```

說明：
- 本地 FastAPI 會讀根目錄 `.env`
- `ALLOWED_ORIGINS` 在本地通常只需要 localhost
- 正式站的 CORS 請在 Render 後台設定

### AI 供應商開關（`AI_PROVIDER`）

對話／推理 LLM 可以在 Azure OpenAI 與 Amazon Bedrock（Claude）之間切換：

| `AI_PROVIDER` | 對話／推理 LLM | embedding |
| --- | --- | --- |
| 未設定或 `azure`（預設） | Azure OpenAI（`AZURE_OPENAI_CHAT_DEPLOYMENT`、`AZURE_CHAT_DEPLOYMENT`／`AZURE_MINI_DEPLOYMENT`） | Azure `text-embedding-3-large`（3072 維） |
| `bedrock` | Bedrock 上的 Claude（`BEDROCK_CHAT_MODEL_ID`／`BEDROCK_MINI_MODEL_ID`） | 同上，不變 |

- 未設定時行為與加入開關前完全相同，團隊現有部署不需要改任何設定。
- embedding 不受開關影響，所以 Qdrant collection、LangGraph store 的 3072 維都不用重建；`AI_PROVIDER=bedrock` 時 Azure 的金鑰與 embedding 部署仍然必填。
- `bedrock` 時涵蓋：課程助理（`llm_service`）、單門課程問答（`course_info_service`）、PDF 問答的 router／chat／retrieval agent／research agent、HyDE 查詢擴展、使用者側寫抽取。
- Bedrock 設定（皆有預設值，見 `.env.example`）：`BEDROCK_REGION`（預設 `ap-northeast-1`）、`BEDROCK_CHAT_MODEL_ID`（預設 `global.anthropic.claude-sonnet-4-6`）、`BEDROCK_MINI_MODEL_ID`（預設 `global.anthropic.claude-haiku-4-5-20251001-v1:0`）、`BEDROCK_MAX_TOKENS`（預設 8192）。
- AWS 憑證走 boto3 預設鏈（環境變數、`AWS_PROFILE`／SSO、IAM role 或 `AWS_BEARER_TOKEN_BEDROCK`），程式不讀取也不記錄金鑰；所選模型須在該帳號／區域的 Bedrock 開通，並支援 structured outputs 與 tool use。
- `boto3`、`langchain-aws` 只在 `bedrock` 時才會被 import，`azure` 模式不需要 AWS 憑證也能啟動。
- 不支援的值（例如打錯字）會在建立／呼叫對話模型時拋出 `AI_PROVIDER_INVALID` 錯誤，不會靜默退回 azure。
- `scripts/` 底下的離線資料管線（例如 `scripts/nlp/build_glossary.py`）不受開關影響，仍直接使用 Azure。

### 即時 AI 開放對象（`LIVE_AI_ACCESS`）

控制誰能使用會呼叫對話／推理型 LLM（會燒 token）的功能：

| `LIVE_AI_ACCESS` | 誰能用即時 AI |
| --- | --- |
| 未設定或 `all`（預設） | 所有人，行為與加入開關前完全相同 |
| `developers` | 只有 `DEVELOPER_EMAILS` 名單內、已用 Google 登入且 Firebase 已驗證 email（`email_verified`）的帳號 |

- 受影響的端點（後端以 `require_live_ai` dependency 把關）：`POST /api/chat`、`POST /api/chat/stream`、`POST /api/courses/{course_id}/ask`、`POST /api/projects/{project_id}/chat`、`POST /api/projects/{project_id}/chat/stream`。
- `developers` 模式下，未登入或不在名單內的帳號呼叫上述端點會收到 HTTP 403，`detail` 為 `{"code": "LIVE_AI_RESTRICTED", "message": "即時 AI 問答目前只開放給開發團隊"}`。
- 不呼叫對話 LLM 的功能都不受影響：興趣量表、系所修課、課程資訊（含只呼叫 embedding 的語意搜尋）、研究計畫瀏覽與 PDF、對話紀錄查詢與刪除、監控頁。
- `all` 模式下 gate 完全不解析 token，公開端點（例如單課問答）不會因為過期或無效的 token 變成 401；`developers` 模式下無效 token 回 401。
- `DEVELOPER_EMAILS` 與 `/monitor` 共用同一份名單（逗號分隔、去空白、不分大小寫）；名單是空的時，`developers` 模式等於沒有人能用即時 AI。
- 前端不另設 VITE 開關，而是呼叫公開端點 `GET /api/ai-access`（未登入也可呼叫；`developers` 模式才依 Firebase token 判斷身分，`all` 模式完全不解析 token，過期或無效的 token 也不會回 401）取得 `{"mode": "all" | "developers", "live_ai": true | false}`，`live_ai=false` 時把提問輸入框換成說明。
- 不認得的值（例如 `developer`、`true`）會讓後端在啟動時直接失敗並出現 `LIVE_AI_ACCESS_INVALID`，不會悄悄退回 `all`。
- 修改這個值只需要更新後端環境變數並重啟，前端不用重新 build。

### 前端環境變數

- `frontend/.env.example`
  本地範例
- `frontend/.env.production`
  正式 build 會使用的 API 網址

正式環境目前設定為：

```env
VITE_API_URL=https://ncu-ai-guidance.onrender.com/api
```

---

## 本地開發

### 後端

```bash
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

後端預設跑在：

- API：`http://localhost:8000`
- 文件：`http://localhost:8000/docs`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端預設跑在：

- `http://localhost:5173`

前端若未額外設定 `VITE_API_URL`，會 fallback 到：

```text
http://localhost:8000/api
```

---

## 部署流程

### 1. 前端部署到 Firebase Hosting

目前 repo 已包含 Firebase Hosting workflow：

- `.github/workflows/firebase-hosting-merge.yml`
- `.github/workflows/firebase-hosting-pull-request.yml`

規則：
- push 到 fork repo 的 `main`：自動部署正式站
- fork repo 內部 PR：自動建立 preview deploy
- workflow 已限制只在 `ChiJiun/ncu_ai_guidance` 執行

### 2. 後端部署到 Render

Render 設定建議：

- Runtime：`Python 3`
- Build Command：

```bash
pip install -r backend/requirements.txt
```

- Start Command：

```bash
cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Render 後台至少需要這些 environment variables：

```env
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_PROJECT_FOLDER=ncu-ai-guidance/data/raw/projects
ALLOWED_ORIGINS=http://localhost:5173,https://ncu-ai-guidance.web.app,https://ncu-ai-guidance.firebaseapp.com
```

部署後請確認：

- `https://ncu-ai-guidance.onrender.com/health`
- `https://ncu-ai-guidance.onrender.com/docs`
- `https://ncu-ai-guidance.onrender.com/api/projects`

### 3. 後端容器映像（Docker）

根目錄的 `Dockerfile` 會把 `backend/` 與 `data/` 打包成映像（`python:3.11-slim`、非 root 使用者、
內建 `/health` 健康檢查、固定單 worker）。建置 context 必須是 repo 根目錄：

```bash
docker build -t ncu-ai-guidance-backend .
docker run --rm -p 8000:8000 --env-file .env ncu-ai-guidance-backend
```

- `.dockerignore` 會排除 `.env`、前端、文件、腳本，以及 `.gitignore` 內的大型資料（本機 Qdrant 儲存、專題 PDF 等），
  所以映像內容與乾淨 clone 建置的結果一致；執行時需要的 `data/processed`、`data/raw` 小型資料檔會被打包進去。
- 正式環境的 Qdrant 請用 Qdrant Cloud（`QDRANT_URL`），映像內不含本機 Qdrant 資料。
- `--env-file` 不會去掉引號，`.env` 的值請不要加引號、不要寫多行值。
- 實際部署到 Oracle VM 或 AWS 的步驟見上方「後端部署方式總覽」。

### 4. PDF 部署到 Cloudinary

專題 PDF 不再跟後端一起部署，改由 Cloudinary 提供公開連結。

上傳腳本：

- `scripts/storage/upload_pdfs_to_cloudinary.py`

使用方式：

```bash
pip install cloudinary python-dotenv
python scripts/storage/upload_pdfs_to_cloudinary.py --dry-run
python scripts/storage/upload_pdfs_to_cloudinary.py
```

---

## 資料部署原則

### 應該留在 repo / 隨後端部署

- `data/processed/assessment_questions.json`
- `data/processed/projects.json`
- `data/processed/courses.json`
- `data/processed/graph/knowledge_graph.json`
- `data/raw/admission/ncu_caac.csv`
- `data/raw/courses/...`（若後端仍直接讀這些原始課程檔）

### 不建議跟後端一起部署

- `data/raw/projects/104-114/**/*.pdf`

這些 PDF 已改由 Cloudinary 提供。
