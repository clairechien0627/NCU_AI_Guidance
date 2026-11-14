# AWS 部署（ECS Fargate）

本文件說明如何把後端以容器部署到 AWS ECS Fargate（前面接 Application Load Balancer）。
前端（Firebase Hosting、Firebase Auth）、PostgreSQL（Neon）、Qdrant Cloud、Cloudinary 維持原樣。
一般的環境變數與本地開發請見 [deployment.md](deployment.md)；比較便宜的單機做法見
[oracle-vm-deployment.md](oracle-vm-deployment.md)。

相關檔案：

| 檔案 | 用途 |
|---|---|
| `Dockerfile`、`.dockerignore` | 後端映像（建置 context 是 repo 根目錄） |
| `deploy/aws/ecs-fargate.yaml` | CloudFormation：ECR、ECS cluster／service／task definition、ALB、IAM、CloudWatch Logs |
| `deploy/aws/github-oidc-deploy-role.yaml` | CloudFormation：GitHub Actions 以 OIDC 取得的部署角色（選用） |
| `.github/workflows/deploy-backend-aws.yml` | 建置映像 → 推到 ECR → 更新 ECS service（選用，預設不執行） |

> ⚠️ 這些範本**尚未在真實 AWS 帳號上完整驗證**，第一次部署請依下方「驗證清單」逐項確認。

---

## 架構

```
使用者 ─► Firebase Hosting（前端，不變）
              │  VITE_API_URL=https://api.example.com/api
              ▼
        ALB（HTTPS, idle timeout 900s）
              ▼
   ECS Fargate × 1 task（FastAPI, 單 worker）──► Azure OpenAI（對話 + embedding）
              │                                  └ AiProvider=bedrock 時，對話改走 Amazon Bedrock（task role）
              ├──► Neon PostgreSQL（DATABASE_URL）
              ├──► Qdrant Cloud（QDRANT_URL）
              └──► Cloudinary（PDF）
```

為什麼只能 1 個 task：PDF 問答的 stream lock／cancel／steering 存在 process 記憶體內（見
`backend/app/main.py`），多個 task 或多個 worker 會互相看不到。所以 CloudFormation 的
`DesiredCount` 只允許 0 或 1，映像也固定 `WEB_CONCURRENCY=1`。

為什麼 ALB idle timeout 設 900 秒：研究型 agent 單一節點最長可跑 600 秒；SSE 每 15 秒會送一次心跳，
連線不會被視為閒置，但保守起見仍把上限放寬（App Runner 有約 120 秒的請求上限，因此不採用）。

---

## AI 供應商

CloudFormation 參數 `AiProvider`（傳進容器成為環境變數 `AI_PROVIDER`）：

| 值 | 對話模型 | Embedding | 需要的設定 |
|---|---|---|---|
| `azure`（預設） | Azure OpenAI | Azure OpenAI `text-embedding-3-large`（3072 維） | Secrets 內的 `AZURE_OPENAI_ENDPOINT`、`AZURE_OPENAI_API_KEY`；部署名稱參數 |
| `bedrock` | Amazon Bedrock（經 task role，不需金鑰） | 同上，**仍使用 Azure OpenAI** | 上面全部，加上 `BedrockChatModelId`（`BedrockMiniModelId` 選填） |

- **Embedding 一律使用 Azure OpenAI `text-embedding-3-large`（3072 維）**，與現有 Qdrant collection 和
  Postgres `store_vectors` 的維度一致，所以切換 `AiProvider` 不需要重建向量索引。
  `AzureEmbeddingDeployment` 必須指向這個模型。
- `bedrock` 需要後端支援 `AI_PROVIDER` 開關（另一個 PR 實作）；**使用的後端版本若還沒有這個功能，
  `AI_PROVIDER` 會被忽略，仍然走 Azure OpenAI**。
- 只有 `AiProvider=bedrock` 時，task role 才會加上 `bedrock:InvokeModel`／`InvokeModelWithResponseStream`
  權限，容器也才會帶入 `AWS_REGION`、`BEDROCK_CHAT_MODEL_ID`、`BEDROCK_MINI_MODEL_ID`。
  `azure` 模式下 task role 沒有任何權限。

Azure 部署名稱與環境變數的對應（以 upstream 程式實際讀取的名稱為準）：

| CloudFormation 參數 | 容器環境變數 | 讀取位置 |
|---|---|---|
| `AzureOpenAiApiVersion` | `AZURE_OPENAI_API_VERSION` | 課程助理、PDF 問答 |
| `AzureChatDeployment` | `AZURE_OPENAI_CHAT_DEPLOYMENT`、`AZURE_CHAT_DEPLOYMENT` | 課程助理（`llm_service.py`）、PDF 問答（`pdf_config.py`） |
| `AzureMiniDeployment` | `AZURE_MINI_DEPLOYMENT` | PDF 問答（`pdf_config.py`） |
| `AzureEmbeddingDeployment` | `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`、`AZURE_EMBEDDING_DEPLOYMENT` | 課程檢索（`retriever.py`）、PDF 問答（`pdf_config.py`） |
| （Secrets Manager） | `AZURE_OPENAI_ENDPOINT`、`AZURE_OPENAI_API_KEY` | 全部 |

---

## 1.（僅 bedrock）在 Bedrock 開通模型

使用預設的 `azure` 可以跳過這一步。

1. AWS Console 切到部署的區域（範例使用 **ap-northeast-1（東京）**）→ Amazon Bedrock → Model access，
   申請要使用的對話模型（例如 Claude Sonnet 與 Haiku）。Anthropic 模型首次使用需填寫使用案例表單。
2. 到 Bedrock → Cross-region inference，記下要使用的 **inference profile ID**。
   實際可用的模型與 ID 以你帳號畫面為準，**請勿照抄範例**。
3. 之後填入 CloudFormation 參數：
   - `BedrockChatModelId`：主要對話模型（必填）
   - `BedrockMiniModelId`：輕量模型（router、短任務），選填；留空時的行為依後端 `AI_PROVIDER` 實作而定

> ⚠️ 模型必須支援工具呼叫（tool use）與結構化輸出；PDF 問答與研究 agent 大量使用這兩項。
> prompt 是針對 gpt-4o 調過的，改用其他模型後回答風格、工具呼叫順序可能不同，需要實際測試。

## 2. 建立 Secrets Manager 密鑰

Console → Secrets Manager → Store a new secret → **Other type of secret**，用 JSON 填入下列 key
（區域與 stack 相同）：

```json
{
  "AZURE_OPENAI_ENDPOINT": "https://<resource>.openai.azure.com/",
  "AZURE_OPENAI_API_KEY": "…",
  "DATABASE_URL": "postgresql://…",
  "QDRANT_URL": "https://…",
  "QDRANT_API_KEY": "…",
  "CLOUDINARY_CLOUD_NAME": "…",
  "CLOUDINARY_API_KEY": "…",
  "CLOUDINARY_API_SECRET": "…",
  "FIREBASE_SERVICE_ACCOUNT_JSON": "{…整份 service account JSON 當成字串…}",
  "DEVELOPER_EMAILS": "a@example.com,b@example.com"
}
```

- 每個 key 都要存在（值可以是空字串）；task definition 引用不存在的 key 時，task 會啟動失敗。
- 記下這個密鑰的**完整 ARN**（結尾含 `-xxxxxx` 六碼隨機字尾；CloudFormation 的 `ValueFrom` 需要完整 ARN）。
- LangSmith 為選填，需要時自行在 task definition 追加 `LANGSMITH_API_KEY` 等 secret。

## 3. 準備網域與憑證

瀏覽器從 HTTPS 的 Firebase 前端呼叫 API，API 必須是 HTTPS（否則會被 mixed content 擋下）。

1. 準備一個 API 網域（例如 `api.example.com`）。
2. 在 stack 同一個區域的 ACM 申請該網域的憑證並完成 DNS 驗證，記下憑證 ARN。

## 4. 部署 CloudFormation

Console → CloudFormation → Create stack → Upload `deploy/aws/ecs-fargate.yaml`。主要參數：

| 參數 | 說明 |
|---|---|
| `VpcId` / `PublicSubnetIds` | 預設 VPC 即可；至少選 2 個不同 AZ 的公有子網路 |
| `CertificateArn` | 步驟 3 的憑證 |
| `SecretArn` | 步驟 2 的密鑰（完整 ARN） |
| `AllowedOrigins` | 前端網域（CORS），逗號分隔 |
| `AiProvider` | `azure`（預設）或 `bedrock`，見「AI 供應商」 |
| `LiveAiAccess` | 傳進容器成為 `LIVE_AI_ACCESS`：`all`（預設，所有人都能用即時 AI）或 `developers`（只有密鑰 `DEVELOPER_EMAILS` 名單內、email 已驗證的帳號能用，正式站可用來限制即時 AI）；細節見 `docs/deployment.md` |
| `AzureChatDeployment` / `AzureMiniDeployment` / `AzureEmbeddingDeployment` | Azure 上的部署名稱 |
| `BedrockChatModelId` / `BedrockMiniModelId` | 只有 `bedrock` 需要；`bedrock` 時 `BedrockChatModelId` 必填 |
| `PdfQdrantCollection` | PDF 問答的 Qdrant collection（預設 `reports`） |
| `DesiredCount` | **第一次保持 0**（此時 ECR 還沒有映像） |

建好後：

1. 在 DNS 建立 `api.example.com` → Outputs 的 `LoadBalancerDnsName`（CNAME 或 Route 53 alias）。
2. 推送第一個映像（任選一種）：
   - 在本機執行（需先 `aws configure sso`；Fargate 範本使用 x86_64，在 ARM 電腦上請加 `--platform linux/amd64`）：
     ```bash
     aws ecr get-login-password --region <區域> | docker login --username AWS --password-stdin <帳號 ID>.dkr.ecr.<區域>.amazonaws.com
     docker build --platform linux/amd64 -t <RepositoryUri>:latest .
     docker push <RepositoryUri>:latest
     ```
   - 或先完成步驟 5 再用 GitHub Actions（注意 workflow 會從**現有的** task definition 複製設定，
     所以第一次仍需先把 stack 建好）。
3. 更新 stack，把 `DesiredCount` 改為 **1**。之後每次更新 stack 都要保留這個值（Console 選 *Use existing value*），
   否則預設的 0 會把服務縮成 0 個 task。
4. GitHub Actions 部署會用 `register-task-definition` 建立新的 revision，與 CloudFormation 管理的 task definition
   會產生 drift；之後若再更新 stack 且改到 task definition（例如改 `AiProvider`），映像會回到 `ImageTag`
   參數指定的標籤，請先確認該標籤存在且是想要的版本。

## 5. 讓 GitHub Actions 部署（選用）

workflow 預設**不會執行**：沒有設定下列 variable 時 job 會被略過（skipped），不會在原始 repo 或 fork 上失敗。

1. 部署 `deploy/aws/github-oidc-deploy-role.yaml`：
   - `GitHubRepository`：要部署的 repo（`owner/repo`，大小寫要與 GitHub 顯示的一致）
   - `ExecutionRoleArn`、`TaskRoleArn`：`ecs-fargate` stack 的 Outputs（`iam:PassRole` 只會授權給這兩個角色）
   - 若帳號已有 GitHub OIDC provider，`CreateOidcProvider` 選 false 並填入其 ARN
2. 在 GitHub repo → Settings → Secrets and variables → Actions → **Variables** 設定：

   | Variable | 必填 | 說明 |
   |---|---|---|
   | `AWS_DEPLOY_ROLE_ARN` | 是 | 上一步 Outputs 的 `DeployRoleArn`（角色 ARN 不是機密，用 variable 才能在 job 的 `if` 判斷） |
   | `AWS_DEPLOY_ON_PUSH` | 否 | 設成 `true` 時，push 到 `main` 且改到 `backend/`、`data/`、`Dockerfile` 等檔案會自動部署 |
   | `AWS_REGION` | 否 | 預設 `ap-northeast-1` |
   | `ECR_REPOSITORY` / `ECS_CLUSTER` / `ECS_SERVICE` / `ECS_TASK_FAMILY` | 否 | `ProjectName` 不是預設的 `ncu-ai-guidance` 時才需要 |

3. Actions → **Deploy backend to AWS ECS Fargate** → Run workflow（`main` 分支）。流程：建置映像、推到 ECR、
   以新映像註冊 task definition、更新服務並等待穩定。OIDC 角色的信任政策只允許 `GitHubBranch`（預設 `main`）。

## 6. 切換前端

1. 更新 GitHub secret `VITE_API_URL=https://api.example.com/api`，重新部署前端。
2. Firebase Auth 不需要改程式碼（授權網域是前端網域，沒有變）。
3. CORS：`AllowedOrigins` 參數需包含前端網域。
4. 建議讓舊後端與 AWS 並行一段時間，確認穩定再切換。

---

## 驗證清單

部署後依序確認：

- [ ] `https://api.example.com/health` 回 `{"status":"healthy"}`；`/ready` 回 200（向量庫與 reranker 預熱完成前會回 503）
- [ ] ECS 任務 Running，CloudWatch Logs `/ecs/ncu-ai-guidance-backend` 沒有 Azure 認證錯誤
      （bedrock 時則是沒有 `AccessDeniedException`／`ValidationException`）
- [ ] 課程助理（`/course-search`）提一個需要呼叫工具的問題，確認：串流逐字輸出、有 `tool_start` 事件、最後有課程卡片
- [ ] 課程資訊頁的單課問答可回答
- [ ] PDF 問答：簡單問題（chat agent）與複雜問題（research agent，可能超過 2 分鐘）都能完整串流結束
- [ ] 監控頁 `/monitor` 的模型名稱與 token 數正常

## 已知限制與風險

- **部署行為**：滾動更新（最小 100%／最大 200%）時，新舊兩個 task 會短暫並存，process 內的 stream lock／cancel 在這段時間
  不一致；`StopTimeout` 已設為 Fargate 上限 120 秒，但進行中的長時間研究串流仍可能在換版時被中斷。要嚴格單 task，
  可把 `MinimumHealthyPercent` 改 0／`MaximumPercent` 改 100，代價是換版期間短暫停機。
- **冷啟動**：映像沒有預先下載 fastembed／flashrank 模型，每個新 task 啟動時會從網路下載，首次健康檢查可能較慢
  （已設 `HealthCheckGracePeriodSeconds: 120`）。
- **成本**：Fargate（2 vCPU / 8 GB）＋ ALB 是固定費用；觀察 CloudWatch 的 CPU／記憶體後可調降 `TaskCpu`／`TaskMemory`。
  公有子網路＋公有 IP 的設計省掉 NAT gateway，但 public IPv4 位址本身也會計費。
- **回滾**：ECS service 啟用了 deployment circuit breaker（新版本起不來會自動回到上一版）；
  也可在 Console 把 service 指回先前的 task definition revision。
