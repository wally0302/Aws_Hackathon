# frontend

訴願案件助審系統的承辦人工作台。整體設計與後端見[根目錄 README](../README.md)。

## 技術棧

- React 19、[TanStack Start](https://tanstack.com/start)（file-based routing，`src/routes/`）、TanStack Query
- TypeScript strict（含 `exactOptionalPropertyTypes`、`noUncheckedIndexedAccess`）
- Vite 8、Tailwind CSS v4、shadcn/ui（new-york）+ Radix、lucide-react、sonner
- 套件管理：bun（`bun.lock`；`bunfig.toml` 設 `minimumReleaseAge = 86400` 擋剛發布的套件）。也附 `package-lock.json`，npm 可用

## 本機執行

```bash
bun install                      # 或 npm install
cp .env.local.example .env.local # 填 VITE_API_BASE=<API Gateway Invoke URL>
bun run dev
```

`.env.local` 只有一個變數 `VITE_API_BASE`。結尾不要加斜線、空白或引號；改完要重啟 dev server（Vite 只在啟動時讀環境變數）；**不要放 AWS 金鑰**，`VITE_` 開頭的變數會被編進瀏覽器 bundle。

未設定 `VITE_API_BASE` 或 API 不通時，每一頁頂端顯示未連線橫幅。前端**沒有假資料**，這是刻意的：有 fallback 的話，承辦人或評審可能把示範資料當成真卷證。

其他指令：`bun run build`、`bun run preview`、`bun run lint`、`bun run format`。目前沒有自動化測試。

## 頁面

| 路由 | 頁面 | 說明 |
|---|---|---|
| `/login` | 承辦人登入 | 示範用，只做操作者識別，不驗證密碼 |
| `/` | 我的案件 | 四個狀態群組（待分析／待審理／審理中／已結案）、子篩選、搜尋；`?tab=&sub=` 可深連結。「執行 AI 分析」由人按下才跑階段 1 |
| `/new-case` | 建立訴願案件 | 案號 + 訴願書 PDF + 答辯書 PDF + 0～N 份證明文件，用 presigned URL 直傳 S3 |
| `/case-review` | 步驟一：文件與程序檢核 | 左訴願書原文，右訴願法 §77 八款與 §56 應記載事項的單選清單；期間計算鏈（送達 → 起算 → 屆滿 → 提起）明示；確認按鈕依選項變成「進入不受理／進入待補正／確認並進入下一步」 |
| `/step2` | 步驟二：事實與爭點整理 | 爭點 chip 交叉高亮訴願人主張與機關答辯，未配對的主張另列 |
| `/step3` | 步驟三：法規與案例援引 | 每條主張的檢索結果依位階分組（法規／釋字／判解／函釋），可開原文 PDF |
| `/step4` | 步驟四：決定文草擬 | 決定書 11 個欄位可編輯，只送出改過的欄位；「決定書原文」由欄位即時組出；右側「AI 判斷脈絡」標明不進決定書 |
| `/step5` | 步驟五：完成與匯出 | 下載決定書 PDF，每 10 秒同步階段四草稿，確認後案件移到已結案 |
| `/ai-context` | AI Context 管理 | 編輯後端三張設定表（`spec_rules` / `aliases` / `field_impact`），儲存前做 JSON 驗證 |
| `/preprocessing` | 知識庫前置作業 | 索引統計、環境自檢、預熱 OpenSearch |

## 與後端的接法

- `src/api/client.ts` 是唯一的 URL 來源；所有回應先讀文字再 parse，因為 API Gateway 的 502/504 不是 JSON。
- `POST /cases/{id}/stages/{n}/run` 回 202 後，`useStage()` 以 React Query 的 `refetchInterval` 每 3 秒輪詢，`running` 結束才停；輪詢綁在 query 上，換頁再回來不會重複觸發。
- 各階段等待上限：1 → 90 s、2 → 120 s、3 → 180 s、4 → 120 s、5 → 30 s。
- `src/api/types.ts` 手寫對應後端實際輸出，與 `backend/tests/test_api_shapes.py` 同步維護；`src/api/adapters.ts` 把後端 14 個案件狀態映成 UI 的狀態群組；`src/api/checkDecision.ts` 把 §56 / §77 的檢查結果依法律效果（不受理／待補正）而非嚴重度分類，是純函式。
- 認證守衛在 `src/components/AppShell.tsx`，每一頁都包在裡面。

## 建置與部署

```bash
bun run build                    # 產出 .output/public/，必須含 index.html
cd ../backend && python deploy_web.py
```

`vite.config.ts` 開了 `tanstackStart.spa.enabled` 並把 prerender 輸出到 `/index.html`；沒開的話 build 出來是 SSR 版本，S3 + CloudFront 無法服務。CloudFront 需把 403 與 404 都轉 `/index.html`（OAC 只給 GetObject，物件不存在時 S3 回 403），否則深層連結重新整理會壞。

## 設計文件

`docs/plans/` 是各功能的設計紀錄。其中「手機收件上傳」與部分「我的案件」篩選設計尚未實作，以現行程式為準。
