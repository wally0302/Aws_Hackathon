# backend

訴願案件助審系統的後端：Python 3.12，四支 Lambda 共用一個 Layer，全部 Serverless。整體設計見[根目錄 README](../README.md)。

## 哪個資料夾是哪個子系統

```
backend/
├── common/          共用模組 → 打包進 Layer appeal-deps，四支 Lambda 都用
├── prep/            流程 1 前置建庫 → appeal-ingest
├── review/          流程 2 訴願作業 → appeal-api + appeal-worker（同一包 zip，Handler 不同）
├── admin/           管理端點       → appeal-admin
├── tests/           本機測試，不需要 AWS
├── layer/           Layer 打包腳本與 requirements.txt
├── provision_*.py   建 AWS 資源（OpenSearch、API Gateway、S3 CORS、CloudFront）
├── deploy_lambdas.py / deploy_web.py   部署程式碼與前端
└── package.ps1      把 prep/ review/ admin/ 打包成 dist/*.zip
```

| Lambda | Handler | Timeout | Memory | 做什麼 |
|---|---|---|---|---|
| `appeal-api` | `review.api.lambda_handler` | 15 s | 1024 MB | 只做路由與觸發，要快。唯一例外是 `/cases/{id}/decision.pdf` 排 PDF |
| `appeal-worker` | `review.worker.lambda_handler` | 15 min | 2048 MB | 真正跑五個階段，由 api 以 `InvocationType="Event"` 非同步觸發 |
| `appeal-ingest` | `prep.handler.lambda_handler` | 15 min | 2048 MB | 建庫，用測試事件的 `action` 分流 |
| `appeal-admin` | `admin.handler.lambda_handler` | 30 s | 512 MB | `/admin/*`：設定表、前例回饋、暖機、語料統計 |

記憶體給大是因為 Lambda 的 CPU 跟記憶體綁定，PyMuPDF 解析與向量化都吃 CPU。

## `review/` 流程 2：五個階段

| 檔案 | 做什麼 | 用 AI？ |
|---|---|---|
| `api.py` | 路由、建案（presigned 上傳）、輪詢、修改、確認、稽核、presigned 下載 | ❌ |
| `worker.py` | 依 `stage` 分流到各階段模組；任何例外都寫回 `failed`，畫面不會永遠轉圈 | ❌ |
| `stage1_check.py` | 讀兩份 PDF → AI 抽 15 個結構化欄位 → 規則引擎跑訴願法 §56 / §77 | 抽欄位 ✅、規則 ❌ |
| `inadmissibility.py` | §77 第 3、6、8 款的 AI 判斷層，最高只能給「可疑」，schema 裡沒有「成立」 | ✅ |
| `check_narrative.py` | 每一條檢查的白話說明與原文引句，一次呼叫涵蓋約 20 條；只解釋、不改結果 | ✅ |
| `stage2_claims.py` | 訴願書、答辯書各抽一次主張，配成一對一爭點；一對一由程式強制 | ✅ |
| `stage3_retrieve.py` | AI 改寫查詢 → BM25 + kNN 混合檢索 → 依位階自動勾選；docstring 記錄了七次校準的實測數字 | 改寫 ✅、檢索 ❌ |
| `stage4_draft.py` | 實體審查：AI 撰寫 + 8 道程式檢查 + Guardrails；不受理：純模板 | 實體 ✅、不受理 ❌ |
| `stage5_archive.py` | Markdown + JSON 稽核包 + PDF 存 S3，刻意不寫進索引 | ❌ |
| `decision_pdf.py` | 官方格式 A4 決定書，內嵌子集化 CJK 字型，中文禁則處理 | ❌ |

## `prep/` 流程 1：前置建庫

| 檔案 | 做什麼 | 產出 |
|---|---|---|
| `handler.py` | 入口，`action` 分流；`selftest` 九項檢查全部跑完才回報 | — |
| `parse_laws.py` | 相關法規，正則切條文；雙欄排版必須 `sort=True`；排除民法與已刪除條文 | 11 份 → 764 條 |
| `parse_precedents.py` | 釋字與判解，切主文與理由分點；要旨取自檔名 | 19 份 → 112 塊 |
| `parse_interpretations.py` | 行政函釋，整份一筆，清掉公文影本雜訊 | 10 份 → 10 筆 |
| `parse_decisions.py` | 歷史決定書，抽六個標頭欄位（全形空格標籤）+ 理由分點；只收 110–113 年 | 101 份 → 358 分點 |
| `extract_claims.py` | 從實體審查決定書抽「訴願人主張 ↔ 機關答辯 ↔ 結果」，**全流程唯一必須用 LLM 的一步** | 69 條 |
| `normalize.py` | 三層正名：字元 → 別名表 → 權威清單；對不到的原樣保留並警示 | — |
| `index_writer.py` | Titan 向量化 + bulk 寫 OpenSearch；不帶 `_id`，批次 20 | — |

`appeal-ingest` 的 actions：`selftest`、`build_all`、`rebuild_indexes`、`parse`、`load`（可續傳，回 `partial: true` 帶 `next_offset`）、`extract_claims`、`delete`、`report`、`search`、`verify_search`（黃金答案回歸測試，重建後必跑）。

## `common/` 共用模組

| 檔案 | 做什麼 |
|---|---|
| `envelope.py` | 五階段共用的回應外殼、所有狀態字串常數、`next_stage_for()` 路由 |
| `state.py` | DynamoDB 單表狀態機：進度、各階段最新版與歷史版、稽核；`can_run()` 併發鎖；>300 KB 改存 S3 |
| `rules_engine.py` | 資料驅動的規則引擎，14 種檢查方法；規則可靠度不超過輸入可靠度 |
| `defaults.py` | 三張設定表的程式內建預設值（規則表、正名表、欄位影響表）；刻意不 import boto3 |
| `config_store.py` | 從 S3 讀三張設定表，60 秒快取，缺檔退回預設 |
| `appeal_law.py` | 訴願法 §56、§77、§62、§18 全文，寫死不走檢索 |
| `bedrock.py` | Converse / forced tool use / embed / guardrail；`_throttle()` 全域限流 ≤ 1 RPS |
| `osclient.py` | OpenSearch Serverless 客戶端、兩個索引 mapping、混合檢索與分數融合 |
| `roc_date.py` | 民國年解析與訴願期間計算（§14：送達次日起算 30 日），回傳整條計算鏈 |
| `textnorm.py` | NFC 正規化，修標楷體 CJK 相容字元問題 |
| `selftest.py` | 每支 Lambda 的 `selftest`，逐項回報缺哪個環境變數 |

## S3 上的三張設定表

| 檔案 | 內容 | 改了會怎樣 |
|---|---|---|
| `config/spec_rules.json` | §56 / §77 各款的檢查規則 | 60 秒內生效，不用重新部署 |
| `config/aliases.json` | 字元、法規名、案由的正名對照與權威清單 | 同上 |
| `config/field_impact.json` | 每個欄位被改時哪些階段要標為過期 | 同上 |

透過 `PUT /admin/config/{name}` 修改，寫入前驗證格式。

## 環境變數

每支 Lambda 的環境變數各自獨立，四支都要各設一次。

| 變數 | api | worker | ingest | admin | 說明 |
|---|:-:|:-:|:-:|:-:|---|
| `DDB_TABLE` | ✅ | ✅ | ✅ | ✅ | `appeal-cases` |
| `DATA_BUCKET` | ✅ | ✅ | ✅ | ✅ | 資料桶名稱 |
| `OS_ENDPOINT` | | ✅ | ✅ | ✅ | AOSS endpoint，**去掉 `https://`** |
| `MODEL_MAIN` | | ✅ | ✅ | ✅ | 預設 `us.anthropic.claude-sonnet-4-5-20250929-v1:0`，**必須 `us.` 開頭** |
| `MODEL_CHEAP` | | ✅ | ✅ | | 選填，預設 Haiku 4.5 |
| `EMBED_MODEL` | | ✅ | ✅ | | 預設 `amazon.titan-embed-text-v2:0` |
| `WORKER_FUNCTION` | ✅ | | ✅ | | `appeal-worker` |
| `BEDROCK_MAX_RPS` | | ✅ | ✅ | | 預設 `1`，競賽規定，不要改 |
| `GUARDRAIL_ID` / `GUARDRAIL_VERSION` | | 選填 | | | 未設時退化為只做程式檢查 |
| `OS_INDEX_LAWS` / `OS_INDEX_CASES` | | 選填 | 選填 | 選填 | 預設 `law-articles` / `case-reasons` |
| `AWS_REGION_NAME` | 選填 | 選填 | 選填 | 選填 | **不能叫 `AWS_REGION`**，那是 Lambda 保留字 |

部署腳本用的 `.env.deploy` 見 `.env.deploy.example`；該檔已被 `.gitignore` 擋住，因為 API Gateway 目前沒有認證，網址等於憑證。

## 打包與部署

| 腳本 | 做什麼 |
|---|---|
| `layer/build.ps1` | `pip install --platform manylinux2014_x86_64` 進 `layer/python/`，複製 `common/`，壓成 `layer/layer.zip` |
| `package.ps1` | 保留目錄結構，把 `prep/` `review/` `admin/` 壓成 `dist/*.zip` |
| `provision_aoss.py` | 建 collection、三個政策，min OCU 設 0（留預設會 24 小時計費），印出 `OS_ENDPOINT` |
| `provision_s3_cors.py` | 資料桶 CORS，沒有這步瀏覽器無法直傳 |
| `deploy_lambdas.py` | 把 `common/` 同步進 Layer、經 S3 發新版本、建立或更新四支 Lambda 並全部指向新 Layer |
| `provision_apigw.py` | HTTP API、兩條 catch-all 路由、CORS、Invoke 權限，印出 `API_BASE` |
| `provision_web.py` | 私有網站桶 + OAC + CloudFront + WAF；403 與 404 都轉 `/index.html` |
| `deploy_web.py` | 上傳 `../frontend/.output/public/`：`index.html` no-cache、`assets/*` immutable，然後清快取 |

完整順序見[根目錄 README 的快速開始](../README.md#快速開始)。改了 `common/` 一定要重新發 Layer 並把四支函式指向新版本，`deploy_lambdas.py` 會一次做完。

## 本機測試

```bash
cd backend
python -m tests.preflight          # zip 結構、import、AOSS 不支援的 API、已知地雷
python -m tests.test_rules         # 規則引擎 vs 標準答案
python -m tests.test_retrieval     # 分數融合與門檻
python -m tests.test_draft         # 階段 4 程式端
python -m tests.test_api_shapes    # 前端契約
```

`preflight` 的每一條規則都要登記一個已知會命中的樣本並自我驗證，避免「規則從沒觸發過卻回報全部乾淨」。
