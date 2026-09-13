import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Archive, FileDown, LoaderCircle, Play, UserCheck } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { toFullText } from "@/api/adapters";
import { api } from "@/api/client";
import { useStage } from "@/api/hooks";
import { useStageRunner } from "@/api/useStageRunner";
import type { Stage4Detail, Stage5Detail } from "@/api/types";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/button";
import { useWorkflow } from "@/state/workflow";

export const Route = createFileRoute("/step5")({
  head: () => ({ meta: [{ title: "完成與匯出｜訴願快速拆解平台" }] }),
  component: Step5,
});

function Step5() {
  const queryClient = useQueryClient();
  const { activeCaseId } = useWorkflow();
  // 進頁面若還沒封存就自動封存；「執行封存」按鈕留著給承辦人重跑
  const {
    stage,
    busy: running,
    runStage,
  } = useStageRunner<Stage5Detail>(activeCaseId, 5, {
    autoRun: true,
    successMessage: "案件封存已完成",
    failMessage: "階段五執行失敗",
    invalidateCases: true,
  });
  const [confirming, setConfirming] = useState(false);
  const busy = running || confirming;
  const detail = stage.data?.detail;

  // 決定書草稿從階段 4 的結果來。`alwaysFresh`：每次進來、切回視窗都重抓，
  // 每 10 秒再抓一次——草稿可能在另一個分頁被重新產生，這頁要跟著更新。
  const stage4 = useStage<Stage4Detail>(activeCaseId, 4, { alwaysFresh: true });
  const draft4 = stage4.data?.detail?.draft ?? null;
  const draftReady = Boolean(draft4) && stage4.data?.status !== "running";
  const [exporting, setExporting] = useState(false);

  // PDF 由後端排版（GET /cases/{id}/decision.pdf 回 presigned URL），前端只負責開連結
  const exportPdf = async () => {
    if (!activeCaseId) return;
    setExporting(true);
    try {
      const res = await api.decisionPdf(activeCaseId);
      const a = document.createElement("a");
      a.href = res.url;
      a.download = res.filename || `訴願決定書_${activeCaseId}.pdf`;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast.success("決定書 PDF 已開始下載");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "無法取得決定書 PDF");
    } finally {
      setExporting(false);
    }
  };

  const confirm = async () => {
    if (!activeCaseId) return;
    setConfirming(true);
    try {
      await api.confirmStage(activeCaseId, 5);
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      await queryClient.invalidateQueries({ queryKey: ["stage", activeCaseId, 5] });
      toast.success("案件已完成");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "確認失敗");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <AppShell caseStep={5}>
      <ApiErrorBanner error={stage.error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">步驟五：完成與匯出</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            封存狀態與檔案位置皆由後端階段五結果提供。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={exporting || !activeCaseId || !draftReady}
            onClick={exportPdf}
            title={
              draftReady
                ? "由後端產生決定書 PDF 並下載"
                : stage4.isLoading
                  ? "正在讀取階段四結果…"
                  : "階段四尚未產生決定書"
            }
          >
            {exporting ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <FileDown className="h-4 w-4" aria-hidden />
            )}
            下載決定書 PDF
          </Button>
          <Button variant="outline" disabled={busy || !activeCaseId} onClick={runStage}>
            {running ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Play className="h-4 w-4" aria-hidden />
            )}
            {detail ? "重新封存" : "執行封存"}
          </Button>
          <Button
            disabled={busy || !stage.data || stage.data.status === "running"}
            onClick={confirm}
          >
            <UserCheck className="h-4 w-4" aria-hidden />
            確認完成
          </Button>
        </div>
      </div>

      {stage.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          正在讀取階段五結果…
        </div>
      ) : running && !detail ? (
        <div className="mt-5 flex items-center justify-center gap-2 rounded-lg border border-dashed py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          正在封存案件…
        </div>
      ) : detail ? (
        // 承辦人只需要知道「封存好了」；S3 位置、桶名那些是給工程師看的，不放這裡
        <div className="mt-5 flex items-center gap-3 rounded-lg border bg-card px-5 py-4 shadow-sm">
          <span className="rounded-lg bg-success/10 p-2 text-success">
            <Archive className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <h2 className="font-semibold">已封存</h2>
            <p className="text-xs text-muted-foreground">{detail.archived.case_status_after}</p>
          </div>
        </div>
      ) : !stage.error && !draft4 ? (
        // 草稿已經顯示在下面時不再放這塊空狀態——兩個「尚未產生」疊在一起會誤導承辦人
        <div className="mt-5 rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
          後端尚未產生階段五結果。
        </div>
      ) : null}

      <section className="mt-4 rounded-lg border bg-card">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/50 px-4 py-2">
          <h2 className="text-sm font-semibold">決定書草稿</h2>
          <span className="text-xs text-muted-foreground">
            {stage4.data
              ? `階段四 v${stage4.data.version}・${stage4.data.status}・每 10 秒自動更新`
              : "尚未讀到階段四結果"}
          </span>
        </header>
        {draft4 && activeCaseId ? (
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap p-5 font-mono text-sm leading-7">
            {toFullText(draft4, activeCaseId)}
          </pre>
        ) : (
          <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
            {stage4.isLoading || stage4.data?.status === "running" ? (
              <>
                <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                {stage4.data?.status === "running"
                  ? "階段四正在重新產生草稿…"
                  : "正在讀取階段四結果…"}
              </>
            ) : (
              "後端尚未產生階段四草稿。"
            )}
          </div>
        )}
      </section>
    </AppShell>
  );
}
