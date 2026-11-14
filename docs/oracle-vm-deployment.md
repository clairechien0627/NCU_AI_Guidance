# Oracle Cloud VM + Cloudflare Tunnel 部署

本文件說明如何把後端以 Docker Compose 跑在一台 Oracle Cloud VM（例如 Always Free 的 Ampere A1），
並透過 Cloudflare Tunnel 對外提供 HTTPS。VM 不需要開放任何對外 port，也不需要自己管 TLS 憑證。
前端（Firebase Hosting）、PostgreSQL（Neon）、Qdrant Cloud、Cloudinary、Azure OpenAI 維持原樣。
一般的環境變數與本地開發請見 [deployment.md](deployment.md)；AWS 做法見 [aws-deployment.md](aws-deployment.md)。

範例檔都在 `deploy/oracle/`，複製到 VM 上的 `/opt/ncu-backend/` 使用：

| 檔案 | 用途 |
|---|---|
| `compose.yaml` | 後端 + cloudflared 兩個服務、Docker log 輪替、模型快取 volume |
| `deploy.sh` | 拉程式 → 用 repo 根目錄的 `Dockerfile` 建置 → 重建容器 → 等健康檢查 |
| `backend.env.example` | 後端環境變數範本（複製成 `backend.env`） |
| `tunnel.env.example` | Cloudflare Tunnel token 範本（複製成 `tunnel.env`） |

> 文件中的 `api.example.com`、`frontend.example.com`、`<VM 公有 IP>` 等都是佔位符，請換成自己的值。

---

## 架構

```
使用者 ─► Firebase Hosting（前端）
              │  VITE_API_URL=https://api.example.com/api
              ▼
      Cloudflare edge（HTTPS，憑證由 Cloudflare 管理）
              ▲  tunnel 由 VM 主動往外連線，VM 不開任何 inbound port
              │
  ┌─ Oracle VM ─────────────────────────────────────────┐
  │  docker compose                                      │
  │   ├─ tunnel  (cloudflare/cloudflared) ──► backend:8000 │
  │   └─ backend (本 repo 的 Dockerfile，單 worker)        │
  │        └ 127.0.0.1:8000 只給 VM 內部健康檢查用          │
  └─────────────────────────────────────────────────────┘
        backend ──► Azure OpenAI / Neon / Qdrant Cloud / Cloudinary / Firebase
```

---

## 1. 建立 VM

1. Oracle Cloud Console → Compute → Instances → Create instance。
   - Shape：`VM.Standard.A1.Flex`（ARM64）。Always Free 額度內可給到 4 OCPU / 24 GB；後端啟動後約用 400 MB，
     研究助理 rerank 時會再多用將近 1 GB，建議至少 2 OCPU / 8 GB。
   - Image：Ubuntu 22.04 / 24.04（下文以 Ubuntu 為例）。
   - 熱門區域的 A1 常出現 *Out of capacity*，換 availability domain 或稍後再試。
2. 網路：Security List／NSG **只需要 SSH（22）**，建議限制來源為自己的 IP。80／443／8000 都**不要**開，
   對外流量全部走 Cloudflare Tunnel。
3. 安裝 Docker Engine 與 Compose plugin（依 [Docker 官方文件](https://docs.docker.com/engine/install/ubuntu/)），以及 `git`、`curl`。
4. 建立部署目錄並放入範例檔：

   ```bash
   sudo mkdir -p /opt/ncu-backend && sudo chown "$USER" /opt/ncu-backend
   cd /opt/ncu-backend
   # 從本 repo 的 deploy/oracle/ 複製 compose.yaml、deploy.sh、backend.env.example、tunnel.env.example 過來
   cp backend.env.example backend.env && cp tunnel.env.example tunnel.env
   chmod 600 backend.env tunnel.env
   chmod +x deploy.sh
   ```

   從 Windows 複製檔案時，請確認 `deploy.sh` 與兩個 env 檔的換行是 LF（Git 的 `core.autocrlf` 可能轉成 CRLF，
   bash 會因此報錯、env 值尾端也會多一個 `\r`），可在 VM 上用 `sed -i 's/\r$//' deploy.sh backend.env tunnel.env` 修正。

最後的目錄結構：

```text
/opt/ncu-backend/
├── compose.yaml
├── deploy.sh
├── backend.env      # 600，含金鑰
├── tunnel.env       # 600，含 tunnel token
└── repo/            # deploy.sh 第一次執行時自動建立（原始碼）
```

> A1 是 ARM64：映像在 VM 上原生建置，不需要 cross-build。後端依賴的套件（`python-igraph`、`leidenalg`、
> `psycopg[binary]`、`fastembed` 等）都有 `linux/aarch64` wheel。

## 2. 填寫 `backend.env`

照 `backend.env.example` 的註解填值（Azure OpenAI、Neon、Qdrant Cloud、Firebase、Cloudinary 等），
`ALLOWED_ORIGINS` 要包含前端網域。
`LIVE_AI_ACCESS` 未設定時是 `all`（所有人都能用即時 AI）；正式站想把即時 AI 限制給開發團隊時設成
`developers`，只有 `DEVELOPER_EMAILS` 名單內、email 已驗證的登入帳號能用（細節見 `docs/deployment.md`）。

### `backend.env` 的格式

`env_file` 不是 shell 腳本，也不是一般的 `.env` 解析器，請遵守：

- 一律寫成 `KEY=value`，**值不要加引號**：`docker run --env-file` 會把引號當成值的一部分，
  Compose 各版本對引號的處理也不完全一致；不加引號是兩邊都能正確讀取的寫法。
- **一個值只能一行，不支援多行值**。`FIREBASE_SERVICE_ACCOUNT_JSON` 要把整份 service account JSON 壓成一行，
  `private_key` 裡的換行保持 `\n` 跳脫（不要展開成真的換行）。例如：
  `python -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))" service-account.json`
- 值裡若含 `$`，Compose 可能把它當成變數展開；密碼／token 盡量避開 `$`，或寫成 `$$`。
- 改完 `backend.env` 要執行 `./deploy.sh --env-only`。`docker restart` **不會**重新讀取 `env_file`，
  一定要重建容器（`up -d --force-recreate`）。

## 3. 建立 Cloudflare named tunnel

需要一個 DNS 由 Cloudflare 代管的網域。使用 Dashboard 建立的 **remotely-managed tunnel**，
設定都存在 Cloudflare，VM 上只放一個 token，不需要 `config.yml` 或 credentials 檔。

1. Cloudflare Zero Trust → Networks → Tunnels → **Create a tunnel** → 類型選 *Cloudflared* → 取名。
2. 安裝畫面會出現 `cloudflared ... --token <很長的字串>`，**只複製 token**，填進 `tunnel.env`：
   `TUNNEL_TOKEN=<token>`（不加引號）。不需要照畫面在 VM 上安裝 cloudflared，compose 會用容器跑。
3. 在 tunnel 的 **Public Hostname**（新版介面為 *Published application routes*）新增：

   | 欄位 | 值 |
   |---|---|
   | Subdomain / Domain | `api` / `example.com` → `api.example.com` |
   | Service Type | `HTTP` |
   | URL | `backend:8000` |

   `backend` 是 compose 的服務名稱；cloudflared 與後端在同一個 compose network，直接用服務名稱連線，
   不需要經過 VM 的 port。Cloudflare 會自動建立對應的 DNS 記錄。
4. token 等同 tunnel 的存取權：外洩時到 Dashboard 重新產生（rotate），更新 `tunnel.env` 後執行 `./deploy.sh --env-only`。

## 4. 部署與更新：`deploy.sh`

```bash
cd /opt/ncu-backend
./deploy.sh             # 拉最新原始碼 → 建置映像 → 重建容器 → 等健康檢查
./deploy.sh --env-only  # 只重建容器（改了 backend.env / tunnel.env 之後）
```

`deploy.sh` 的行為：

1. 從 `SRC_URL` 的 `SRC_BRANCH`（預設為本專案原始 repo 的 `main`）以 `--depth 1` 抓最新 commit 到 `repo/`，
   用 repo 根目錄的 `Dockerfile` 建置，同時標上 `ncu-backend:<commit sha>` 與 `ncu-backend:latest`。
   要部署 fork 或其他分支：`SRC_URL=https://github.com/<owner>/<repo>.git SRC_BRANCH=<branch> ./deploy.sh`。
2. `tunnel.env` 有 `TUNNEL_TOKEN` 才會一起啟動 cloudflared，否則只啟動後端（避免 cloudflared 不斷重啟）。
3. 等待 `http://127.0.0.1:8000/health` 回應（最多約 3 分鐘），成功後印出 `/ready` 的結果並清掉沒有標籤的舊映像；
   逾時則印出最近 50 行 log 並以非 0 結束。

`deploy.sh` 用 `sudo docker`；若部署帳號已在 `docker` 群組，可以自行拿掉 `sudo`。

## 5. 健康檢查

| 端點 | 意義 |
|---|---|
| `/health` | process 活著就回 `{"status":"healthy"}`。Dockerfile 的 `HEALTHCHECK` 與 `deploy.sh` 都用它 |
| `/ready` | 向量庫與 reranker 預熱完成才回 200；預熱中回 503（`"rag": "initializing"`）。`pdf_chat` 為 `degraded` 表示 PDF 問答的 checkpointer（Postgres）初始化失敗 |

```bash
curl -s http://127.0.0.1:8000/health              # VM 內部
curl -s http://127.0.0.1:8000/ready
curl -s https://api.example.com/health            # 經過 Cloudflare Tunnel
sudo docker compose -f compose.yaml ps            # STATUS 會顯示 (healthy)
sudo docker logs --tail 100 -f ncu-backend
sudo docker logs --tail 50 ncu-tunnel             # tunnel 連線狀態
```

剛部署完 `/ready` 回 503 是正常的（第一次啟動要下載 fastembed／flashrank 模型）；模型快取在 `model-cache`
volume，之後重建容器不會再下載。

## 6. 回退

每次建置都保留一個 `ncu-backend:<commit sha>` 標籤，回退不需要重新建置：

```bash
sudo docker image ls ncu-backend                                  # 找到要回去的 sha
sudo docker tag ncu-backend:<舊 sha> ncu-backend:latest
./deploy.sh --env-only
```

要固定在某個舊版本一段時間，請不要再執行不帶參數的 `./deploy.sh`（它會重新拉 `main`）。
舊映像會一直保留，磁碟不夠時用 `sudo docker rmi ncu-backend:<sha>` 手動清除。

## 7. Docker log 輪替

Docker 預設的 `json-file` log driver **不會輪替**，長時間執行會把磁碟塞滿。`compose.yaml` 已替兩個服務設定
`max-size: 10m`、`max-file: 3`（每個容器最多約 30 MB）。

若 VM 上還有其他容器，也可以改成全域設定（會影響整台機器的 Docker，修改前請確認）：

```json
// /etc/docker/daemon.json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
```

改完 `sudo systemctl restart docker`；全域設定只對**之後新建**的容器生效，既有容器要重建。

## 8. Oracle 免費方案的閒置回收

Oracle 會回收「閒置」的 Always Free compute instance。依 Oracle 文件，在 7 天內同時符合下列條件即視為閒置：

- CPU 使用率的第 95 百分位數低於 20%
- 網路使用率低於 20%
- 記憶體使用率低於 20%（只適用 A1 shape）

這個後端平時流量很低，很容易被判定閒置。建議：

- 把帳號升級為 **Pay As You Go**：Always Free 額度內的資源仍然免費，但不會被當成閒置回收
  （請同時設定預算警示，避免不小心用到付費資源）。
- VM 上只有 `compose.yaml`、`deploy.sh`、`backend.env`、`tunnel.env` 是無法從 repo 重建的檔案；
  把 `backend.env`／`tunnel.env` 存在安全的地方（例如密碼管理器），VM 被回收時可以照本文件重建。
  資料都在 Neon、Qdrant Cloud、Cloudinary，不在 VM 上。

回收政策可能變動，請以 Oracle 官方文件
（[Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)）為準。

## 9. 切換前端

1. 前端 build 時的 `VITE_API_URL` 設為 `https://api.example.com/api`（GitHub secret），重新部署前端。
2. `backend.env` 的 `ALLOWED_ORIGINS` 要包含前端網域，改完執行 `./deploy.sh --env-only`。

---

## 注意事項

- **單 worker**：映像固定 `WEB_CONCURRENCY=1`，PDF 問答的 stream lock／cancel／steering 是 process 內狀態，
  不要用 `--scale backend=2` 或多 worker。
- **長連線**：研究助理的 SSE 串流可能持續數分鐘，後端每 15 秒送一次心跳，經過 Cloudflare Tunnel 不會被當成閒置連線切斷。
- **重建期間會短暫停機**：`--force-recreate` 會先停舊容器再起新容器，進行中的串流會被中斷，請避開使用高峰。
- **`--proxy-headers`**：映像信任所有來源的 `X-Forwarded-*`，所以 8000 只能綁在 `127.0.0.1`（compose 範例已是如此），
  不要改成 `0.0.0.0:8000:8000` 對外開放。
