# backend 目錄說明

## 一句話：哪個資料夾是哪個子系統

```
backend/
├── common/        🔧 兩邊共用      → 打包進 Layer，四個 Lambda 都用
│
├── prep/          🅰️ 前置作業      → appeal-ingest        （階段1流程圖）
├── review/        🅱️ 訴願作業      → appeal-api + appeal-worker（階段2流程圖）
├── admin/         ⚙️ 管理端點      → appeal-admin
│
├── layer/         Layer 打包腳本
├── statemachine/  Step Functions 定義
├── tests/         本機測試（不上傳）
└── dist/          打包產出的 zip（不上傳原始碼，上傳這裡的 zip）
```

---

## 對應到你畫的兩張流程圖

### 🅰️ `prep/` = 前置作業（階段1流程圖）

管理員上傳歷史資料 → 建庫。**跑一次可以給訴願作業用很久。**

| 檔案 | 流程圖節點 | 程式 or AI | 狀態 |
|---|---|---|---|
| `handler.py` | 整條流程的入口 + **`selftest` 環境診斷** | 程式 | ✅ |
| `parse_laws.py` | A3 切條文 | 程式 | ✅ 764 條 |
| `parse_decisions.py` | A4 抽六欄位 + 理由分點 | 程式 | ✅ 101/101、358 分點 |
| `normalize.py` | A7 統一名稱 | 程式 | ✅ 16 件 |
| `index_writer.py` | A8 產生向量 + A9 寫索引 | 程式 + 嵌入模型 | ✅ |
| `parse_interpretations.py` | A5 函釋濾雜訊 | 程式 | ⬜ 未做 |
| `parse_precedents.py` | A6 判解補標頭 | 程式 + **AI** | ⬜ 未做 |
| `extract_claims.py` | A10 抽主張與答辯 | **AI** | ⬜ 未做 |

### 🅱️ `review/` = 訴願作業（階段2流程圖）

承辦人上傳民眾訴願書 → 五個階段，**每階段停下來等人確認**。

| 檔案 | 流程圖節點 | 狀態 |
|---|---|---|
| `api.py` | 「後端確認案件狀態」那個**菱形** + 三個端點 | ⬜ 未做 |
| `worker.py` | 依 stage 分流，實際做事的地方 | ⬜ 未做 |
| `stage1_check.py` | 抽文字 → LLM 結構化 → 跑規則 | ⬜ 未做 |
| `stage2_claims.py` | 抽取主張 | ⬜ 未做 |
| `stage3_retrieve.py` | 改寫 → 混合檢索 → 前例統計 | ⬜ 未做 |
| `stage4_draft.py` | 生成草稿（含不受理模板分支）→ Guardrails | ⬜ 未做 |
| `stage5_archive.py` | 草稿入庫 | ⬜ 未做 |

### 🔧 `common/` = 兩邊共用

**這個資料夾不會單獨變成 Lambda，它被打包進 Layer。**

| 檔案 | 做什麼 | 本機測過？ |
|---|---|---|
| `envelope.py` | 統一回應外殼、**14 個狀態值常數**、路由菱形 | ✅ 9 條路由 |
| `state.py` | DynamoDB 進度／版本／過期／稽核 | 需 AWS |
| `config_store.py` | 讀 S3 三張設定表（60 秒快取） | 需 AWS |
| `defaults.py` | 三張表的佔位內容（**刻意不 import boto3**） | ✅ |
| `osclient.py` | OpenSearch client + 混合檢索 | 需 AWS |
| `bedrock.py` | Converse / 強制 JSON / 向量 / Guardrails | 需 AWS |
| `roc_date.py` | 民國年 + 訴願法 14 條期間計算 | ✅ 兩份樣本全對 |
| `rules_engine.py` | 六種 handler 的規則引擎 | ✅ 標準答案 100% |

---

## 打包與上傳

```powershell
cd D:\ai_hackthon\2026_Aws_Hackathon\backend

.\layer\build.ps1     # common/ + 三個套件 → layer.zip
.\package.ps1         # prep/ review/ admin/ → dist\*.zip
```

| zip | 上傳到哪個 Lambda | **Handler 要填** |
|---|---|---|
| `layer\layer.zip` | Layers → `appeal-deps` | — |
| `dist\appeal-prep.zip` | `appeal-ingest` | `handler.lambda_handler` |
| `dist\appeal-review.zip` | `appeal-api` | **`api.lambda_handler`** |
| `dist\appeal-review.zip` | `appeal-worker` | **`worker.lambda_handler`** |
| `dist\appeal-admin.zip` | `appeal-admin` | `handler.lambda_handler` |

> **`appeal-review.zip` 同一個包上傳給兩個 Lambda**，只是 Handler 不同。
> 因為 `api` 和 `worker` 共用大量程式碼，分兩包還要同步維護。

### ⚠️ 兩個最容易踩的坑

**1. Handler 沒改** → `ImportModuleError`
上傳 zip 後預設是 `lambda_function.lambda_handler`，一定要改成上表的值。
**如果 Test 回傳 `"Hello from Lambda!"`，就是 zip 沒上去或 Handler 沒改。**

**2. 改了 `common/` 卻沒更新 Layer** → 行為不變，你會以為程式沒生效
`common/` 在 Layer 裡，改完要：重跑 `build.ps1` → 上傳成**新版本** → 在每個函式上把 Layer 換成新版本。

---

## 本機測試（不需要 AWS）

```powershell
cd D:\ai_hackthon\2026_Aws_Hackathon\backend

python -m tests.test_rules        # 規則引擎 vs 訴願書樣本標準答案
python prep\parse_laws.py         # 11 個法規 → 應為 764 條
python prep\parse_decisions.py    # 101 份決定書 → 六欄位 101/101、分點 3/4/10
python prep\normalize.py          # 正名 16 件
python prep\index_writer.py       # 整條建庫線乾跑 + 寫入前驗證
```

**這些都不碰 AWS**，改完程式先在本機跑過再打包，省很多來回。
