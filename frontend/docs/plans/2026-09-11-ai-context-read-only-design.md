# AI Context 管理 read-only 設計

日期：2026-09-11  
範圍：`frontend` 純前端 PC 版

## 目標

將「AI Context 管理」調整為承辦人員的唯讀檢視頁，避免承辦人修改系統使用的 prompt。AI Context 內容仍可依步驟切換查看，但不提供編輯或保存操作。

## 核准方案

- 已設定的 prompt 由可編輯 `Textarea` 改為唯讀、可選取複製的純文字區塊。
- 移除「儲存 prompt」按鈕、`steps` 編輯狀態及相關 toast 行為。
- 保留左側步驟切換，讓承辦人可查看不同步驟的 prompt 狀態。
- 步驟一、四繼續顯示既有 prompt；步驟二、三、五維持「prompt 開發中」提示。
- 不更動 prompt 內容、步驟流程及其他管理頁面。

## 驗收條件

- 頁面上不再存在可輸入或修改 prompt 的控制項。
- 頁面上不再出現「儲存 prompt」按鈕。
- 已設定 prompt 可完整閱讀與選取複製。
- 切換步驟仍可正常顯示對應內容。
- `ai-context.tsx` ESLint 與專案 build 通過。
