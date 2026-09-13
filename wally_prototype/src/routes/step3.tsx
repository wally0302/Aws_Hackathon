import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { FileText, LoaderCircle, Play, UserCheck } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { useStageRunner } from "@/api/useStageRunner";
import type { RetrievalHit, Stage3Detail } from "@/api/types";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { SourcePreviewDialog } from "@/components/SourcePreviewDialog";
import { Button } from "@/components/ui/button";
import { useWorkflow } from "@/state/workflow";

export const Route = createFileRoute("/step3")({
  head: () => ({ meta: [{ title: "法規與案例援引｜訴願快速拆解平台" }] }),
  component: Step3,
});

function Step3() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { activeCaseId } = useWorkflow();
  // 進頁面若還沒跑就自動跑；「執行檢索」按鈕留著給承辦人重跑
  const {
    stage,
    busy: running,
    runStage,
  } = useStageRunner<Stage3Detail>(activeCaseId, 3, {
    autoRun: true,
    successMessage: "法規與案例檢索已完成",
    failMessage: "階段三執行失敗",
  });
  const [confirming, setConfirming] = useState(false);
  const busy = running || confirming;
  const detail = stage.data?.detail;

  const confirm = async () => {
    if (!activeCaseId) return;
    setConfirming(true);
    try {
      await api.confirmStage(activeCaseId, 3);
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("階段三已確認");
      void navigate({ to: "/step4" });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "確認失敗");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <AppShell caseStep={3}>
      <ApiErrorBanner error={stage.error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">步驟三：法規與案例援引</h1>
          <p className="mt-1 text-sm text-muted-foreground">檢索結果與來源全文連結皆由後端提供。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" disabled={busy || !activeCaseId} onClick={runStage}>
            {running ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Play className="h-4 w-4" aria-hidden />
            )}
            {detail ? "重新檢索" : "執行檢索"}
          </Button>
          <Button
            disabled={busy || !stage.data || stage.data.status === "running"}
            onClick={confirm}
          >
            <UserCheck className="h-4 w-4" aria-hidden />
            確認並進入下一步
          </Button>
        </div>
      </div>

      {stage.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          正在讀取階段三結果…
        </div>
      ) : running && !detail ? (
        <div className="mt-5 flex items-center justify-center gap-2 rounded-lg border border-dashed py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          AI 正在檢索法規與案例，約需兩至三分鐘…
        </div>
      ) : detail ? (
        <div className="mt-5 space-y-4">
          {detail.per_claim.length ? (
            detail.per_claim.map((claim) => (
              <section key={claim.claim_id} className="rounded-lg border bg-card shadow-sm">
                <header className="border-b bg-muted/50 px-4 py-3">
                  <p className="text-xs text-muted-foreground">{claim.side_label}</p>
                  <h2 className="mt-1 text-sm font-semibold">{claim.claim}</h2>
                  {/* ⚠️ claim.issue 是物件 {issue_id, issue}，要顯示的是 .issue。
                      直接渲染物件會讓整頁掛掉（React 不接受物件當 child）。 */}
                  {claim.issue?.issue ? (
                    <p className="mt-1 text-xs text-muted-foreground">爭點：{claim.issue.issue}</p>
                  ) : null}
                </header>
                <div className="space-y-4 p-4">
                  {claim.groups.flatMap((group) => group.hits).length ? (
                    claim.groups.map((group) => (
                      <div key={`${claim.claim_id}-${group.authority_rank}`}>
                        <h3 className="text-xs font-semibold text-muted-foreground">
                          {group.label}
                        </h3>
                        <div className="mt-2 grid gap-3 lg:grid-cols-2">
                          {group.hits.map((hit) => (
                            <SourceCard key={hit.ref_key} hit={hit} />
                          ))}
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">後端未回傳檢索命中。</p>
                  )}
                </div>
              </section>
            ))
          ) : (
            <div className="rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
              後端未回傳主張檢索結果。
            </div>
          )}
        </div>
      ) : !stage.error ? (
        <div className="mt-5 rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
          後端尚未產生階段三結果。
        </div>
      ) : null}
    </AppShell>
  );
}

function SourceCard({ hit }: { hit: RetrievalHit }) {
  // 「原文」改為同頁彈窗預覽；presigned URL 由 SourcePreviewDialog 在打開時才去換
  const [previewOpen, setPreviewOpen] = useState(false);
  const title = [hit.law, hit.article].filter(Boolean).join(" ") || hit.doc_type;

  return (
    <article className="rounded-md border p-3 text-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-semibold">{title}</p>
          <p className="mt-1 text-xs text-muted-foreground">{hit.doc_type}</p>
        </div>
        {hit.file ? (
          <>
            <Button size="sm" variant="outline" onClick={() => setPreviewOpen(true)}>
              <FileText className="h-3.5 w-3.5" aria-hidden />
              原文
            </Button>
            <SourcePreviewDialog
              open={previewOpen}
              onOpenChange={setPreviewOpen}
              kind={hit.doc_type}
              file={hit.file}
              title={title}
            />
          </>
        ) : null}
      </div>
      <p className="mt-3 leading-6 text-muted-foreground">{hit.text}</p>
      {hit.blocked_reason ? (
        <p className="mt-2 text-xs text-destructive">{hit.blocked_reason}</p>
      ) : null}
    </article>
  );
}
