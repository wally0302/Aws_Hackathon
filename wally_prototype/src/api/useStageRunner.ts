import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api } from "./client";
import { type Loaded, useStage } from "./hooks";
import type { StageEnvelope, StageStatus } from "./types";

export type StageRunnerOptions = {
  /** 進頁面時若階段還是 `pending` 就自動跑。步驟一由列表觸發，設 false。 */
  autoRun: boolean;
  successMessage: string;
  failMessage: string;
  /** 跑完後是否連 `["cases"]` 一起重抓（階段 1、5 會改列表上的狀態） */
  invalidateCases?: boolean;
};

export type StageRunner<D> = {
  stage: Loaded<StageEnvelope<D> | null>;
  /** 正在 POST run，或後端說這階段 `running` */
  busy: boolean;
  /** 手動（重新）執行 */
  runStage: () => Promise<void>;
  refresh: () => Promise<void>;
};

/**
 * 步驟頁共用的「執行 + 自動觸發 + 完成通知」。
 *
 * 流程是「先確認進下一步，再由新頁面自己跑」——所以自動觸發放在這裡，
 * 不在上一頁的 confirm 裡偷跑。
 *
 * **「正在跑」以後端 `status` 為準，不是這個 hook 自己的 state。**
 * 輪詢由 `useStage` 的 `refetchInterval` 負責（status 是 `running` 就每 3 秒抓），
 * 所以承辦人切去別頁再回來，動畫會接著顯示、也不會再 POST 一次 run。
 * （以前把輪詢綁在元件上：離開就 abort、回來看到過期的 `pending` 就再 run 一次，
 *   dev 的 StrictMode 雙重 mount 更會讓它 abort 自己，畫面停在「尚未產生結果」。）
 *
 * 進頁面時依 `status` 決定：
 * - `running`：什麼都不做，query 已在輪詢
 * - `pending` 且 `autoRun` 且後端說 `can_run`：自動 POST run
 * - 其他（done / confirmed / failed / stale）：不動，交給承辦人按「執行」重跑
 */
export function useStageRunner<D>(
  caseId: string | null,
  stageNo: number,
  opts: StageRunnerOptions,
): StageRunner<D> {
  const queryClient = useQueryClient();
  const stage = useStage<D>(caseId, stageNo);
  /** 只涵蓋 POST run 到後端回 202 那一瞬間；之後靠 `status === "running"` */
  const [starting, setStarting] = useState(false);
  /** 已自動觸發過的 `${caseId}:${stageNo}`，避免 refetch 造成重複 run */
  const autoKeyRef = useRef<string | null>(null);

  const status = stage.data?.status;
  const canRun = stage.data?.can_run;
  const busy = starting || status === "running";

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["stage", caseId, stageNo] });
    if (opts.invalidateCases) {
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
    }
  }, [queryClient, caseId, stageNo, opts.invalidateCases]);

  const runStage = useCallback(async () => {
    if (!caseId) return;
    setStarting(true);
    try {
      await api.runStage(caseId, stageNo);
      // 202 之後立刻重抓一次：GET 會回 running，`useStage` 就接手輪詢。
      // 不直接把 202 的 body 塞進 cache——它的 `detail` 是空的 `{}`，
      // 步驟頁看到 truthy 的 detail 會去讀 `detail.issues` 之類的欄位而炸掉。
      // `refresh` 等到重抓完才 resolve，所以 `starting` 放開時 status 已是 running，動畫不會閃。
      await refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : opts.failMessage);
    } finally {
      setStarting(false);
    }
  }, [caseId, stageNo, refresh, opts.failMessage]);

  // 自動觸發：只在 pending 時 POST。running 不動（query 已在輪詢）。
  useEffect(() => {
    if (!caseId || stage.isLoading || status !== "pending") return;
    if (!opts.autoRun || canRun === false) return;
    const key = `${caseId}:${stageNo}`;
    if (autoKeyRef.current === key) return;
    autoKeyRef.current = key;
    void runStage();
  }, [caseId, stageNo, stage.isLoading, status, canRun, opts.autoRun, runStage]);

  // 完成通知：status 從 running 轉成別的那一刻
  const prevStatusRef = useRef<StageStatus | undefined>(status);
  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = status;
    if (prev !== "running" || !status || status === "running") return;
    if (status === "failed") {
      toast.error(stage.data?.summary || opts.failMessage);
    } else {
      toast.success(opts.successMessage);
    }
    if (opts.invalidateCases) {
      void queryClient.invalidateQueries({ queryKey: ["cases"] });
    }
  }, [
    status,
    stage.data?.summary,
    opts.failMessage,
    opts.successMessage,
    opts.invalidateCases,
    queryClient,
  ]);

  return { stage, busy, runStage, refresh };
}
