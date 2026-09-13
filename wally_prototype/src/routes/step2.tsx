import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { FileText, LoaderCircle, Play, UserCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { useStage } from "@/api/hooks";
import { useStageRunner } from "@/api/useStageRunner";
import type { Claim, Issue, Stage1Detail, Stage2Detail } from "@/api/types";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { useWorkflow } from "@/state/workflow";

export const Route = createFileRoute("/step2")({
  head: () => ({ meta: [{ title: "事實與爭點整理｜訴願快速拆解平台" }] }),
  component: Step2,
});

/** 爭點的顯示名稱。後端沒給短名稱就整串顯示——**不要截斷**。 */
function labelOf(issue: Issue): string {
  return issue.issue_label?.trim() || issue.issue;
}

/**
 * 建 claim_id → 爭點 的索引。
 *
 * ⚠️ 後端保證 1:1（模型違反時會被 `pairing_dropped` 擋下來），所以一個
 * claim 最多對到一個爭點。這裡還是用 Map 而不是塞回 claim 物件，
 * 因為 claim 陣列是後端原樣，改它會讓「畫面上的東西 vs 後端回的東西」
 * 對不起來。
 */
function buildIndex(detail: Stage2Detail | undefined) {
  const byClaim = new Map<string, Issue>();
  for (const issue of detail?.issues ?? []) {
    if (issue.petition_claim_id) byClaim.set(issue.petition_claim_id, issue);
    if (issue.defense_claim_id) byClaim.set(issue.defense_claim_id, issue);
  }
  return byClaim;
}

function Step2() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { activeCaseId } = useWorkflow();
  // 進頁面若還沒跑就自動跑；「執行整理」按鈕留著給承辦人重跑
  const {
    stage,
    busy: running,
    runStage,
  } = useStageRunner<Stage2Detail>(activeCaseId, 2, {
    autoRun: true,
    successMessage: "事實與爭點整理已完成",
    failMessage: "階段二執行失敗",
  });
  // 原文從**階段一**來。階段一解析 PDF 時就把兩份全文存進 detail 了
  // （`petition_text` / `defense_text`），不需要另外存一份到 S3。
  const stage1 = useStage<Stage1Detail>(activeCaseId, 1);

  const [confirming, setConfirming] = useState(false);
  const [activeIssue, setActiveIssue] = useState<string | null>(null);
  const [preview, setPreview] = useState<"petition" | "defense" | null>(null);

  const busy = running || confirming;
  const detail = stage.data?.detail;
  const byClaim = useMemo(() => buildIndex(detail), [detail]);
  const current = detail?.issues.find((i) => i.issue_id === activeIssue) ?? null;

  const confirm = async () => {
    if (!activeCaseId) return;
    setConfirming(true);
    try {
      await api.confirmStage(activeCaseId, 2);
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("階段二已確認");
      void navigate({ to: "/step3" });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "確認失敗");
    } finally {
      setConfirming(false);
    }
  };

  const s1 = stage1.data?.detail;
  const previewText =
    preview === "petition"
      ? (s1?.petition_text ?? "")
      : preview === "defense"
        ? (s1?.defense_text ?? "")
        : "";

  return (
    <AppShell caseStep={2}>
      <ApiErrorBanner error={stage.error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">步驟二：事實與爭點整理</h1>
          <p className="mt-1 text-sm text-muted-foreground">所有主張與爭點皆來自後端階段二結果。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" disabled={busy || !activeCaseId} onClick={runStage}>
            {running ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Play className="h-4 w-4" aria-hidden />
            )}
            {detail ? "重新整理" : "執行整理"}
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
          正在讀取階段二結果…
        </div>
      ) : running && !detail ? (
        <div className="mt-5 flex items-center justify-center gap-2 rounded-lg border border-dashed py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          AI 正在整理事實與爭點，約需一至兩分鐘…
        </div>
      ) : detail ? (
        <>
          {/* ───────── 爭點：可點的標籤，點了兩邊一起反白 ───────── */}
          <section className="mt-5 rounded-lg border bg-card p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold">爭點</h2>
              <p className="text-xs text-muted-foreground">
                點一下標籤，下方對應的主張與答辯會一起反白；再點一次取消。
              </p>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {detail.issues.length ? (
                detail.issues.map((issue) => {
                  const on = issue.issue_id === activeIssue;
                  return (
                    <button
                      key={issue.issue_id}
                      type="button"
                      // title 放完整爭點——標籤是短名稱，滑過去要看得到全文
                      title={issue.issue}
                      aria-pressed={on}
                      onClick={() => setActiveIssue(on ? null : issue.issue_id)}
                      className={cn(
                        "rounded-full border px-3 py-1.5 text-xs transition",
                        on
                          ? "border-primary bg-primary text-primary-foreground shadow-sm"
                          : "bg-secondary hover:border-primary/50 hover:bg-secondary/70",
                      )}
                    >
                      {labelOf(issue)}
                    </button>
                  );
                })
              ) : (
                <p className="text-sm text-muted-foreground">後端未回傳爭點。</p>
              )}
            </div>
            {current ? (
              <div className="mt-3 rounded-md border-l-2 border-primary bg-primary/5 p-3 text-sm leading-7">
                {current.issue}
                {current.note ? (
                  <p className="mt-1 text-xs text-muted-foreground">{current.note}</p>
                ) : null}
              </div>
            ) : null}
          </section>

          {/* ───────── 主張 / 答辯：全部列出，不分配對與否 ───────── */}
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <ClaimPanel
              title="訴願人主張"
              items={detail.petition_claims}
              byClaim={byClaim}
              activeIssue={activeIssue}
              onOpenSource={() => setPreview("petition")}
              sourceReady={Boolean(s1?.petition_text)}
              sourceLoading={stage1.isLoading}
            />
            <ClaimPanel
              title="原處分機關答辯"
              items={detail.defense_claims}
              byClaim={byClaim}
              activeIssue={activeIssue}
              onOpenSource={() => setPreview("defense")}
              sourceReady={Boolean(s1?.defense_text)}
              sourceLoading={stage1.isLoading}
            />
          </div>
        </>
      ) : !stage.error ? (
        <div className="mt-5 rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
          後端尚未產生階段二結果。
        </div>
      ) : null}

      {/* 原文預覽。open 綁 preview 是否為 null，關掉就清狀態 */}
      <Dialog open={preview !== null} onOpenChange={(o) => !o && setPreview(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{preview === "defense" ? "答辯書原文" : "訴願書原文"}</DialogTitle>
            <DialogDescription>
              由階段一解析 PDF 後留存的全文，共 {previewText.length} 字。
            </DialogDescription>
          </DialogHeader>
          {/* whitespace-pre-wrap：PDF 抽出來的換行是有意義的（分項、段落） */}
          <div className="max-h-[65vh] overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-4 text-sm leading-7">
            {previewText || "階段一沒有留下這份文件的原文。"}
          </div>
        </DialogContent>
      </Dialog>
    </AppShell>
  );
}

function ClaimPanel({
  title,
  items,
  byClaim,
  activeIssue,
  onOpenSource,
  sourceReady,
  sourceLoading,
}: {
  title: string;
  items: Claim[];
  byClaim: Map<string, Issue>;
  activeIssue: string | null;
  onOpenSource: () => void;
  sourceReady: boolean;
  sourceLoading: boolean;
}) {
  return (
    <section className="rounded-lg border bg-card">
      <header className="flex items-center justify-between gap-2 border-b bg-muted/50 px-4 py-1.5">
        <h2 className="text-sm font-semibold">
          {title}
          <span className="ml-2 font-normal text-xs text-muted-foreground">{items.length} 條</span>
        </h2>
        <Button
          variant="ghost"
          size="sm"
          onClick={onOpenSource}
          disabled={sourceLoading || !sourceReady}
          title={sourceReady ? undefined : "階段一尚未產生原文"}
        >
          {sourceLoading ? (
            <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <FileText className="h-4 w-4" aria-hidden />
          )}
          原文
        </Button>
      </header>
      <div className="space-y-3 p-4">
        {items.length ? (
          items.map((item) => {
            const issue = byClaim.get(item.claim_id);
            const on = Boolean(activeIssue) && issue?.issue_id === activeIssue;
            // 有選爭點時，沒中的淡掉。⚠️ 是**淡掉不是隱藏**——承辦人要能
            // 隨時看到全部幾條，隱藏會讓人以為主張變少了。
            const dimmed = Boolean(activeIssue) && !on;
            return (
              <article
                key={item.claim_id}
                className={cn(
                  "rounded-md border p-3 text-sm leading-7 transition",
                  on && "border-primary bg-primary/5 ring-2 ring-primary/40",
                  dimmed && "opacity-45",
                )}
              >
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-muted-foreground">{item.no}</span>
                  {issue ? (
                    <span
                      className={cn(
                        "rounded px-1.5 py-0.5 text-xs",
                        on ? "bg-primary text-primary-foreground" : "bg-secondary",
                      )}
                      title={issue.issue}
                    >
                      {labelOf(issue)}
                    </span>
                  ) : (
                    // 未配對不是錯誤，只是沒有對應到爭點
                    <span className="rounded px-1.5 py-0.5 text-xs text-muted-foreground">
                      未配對
                    </span>
                  )}
                </div>
                <p>{item.claim}</p>
                {item.quote ? (
                  <blockquote className="mt-2 border-l-2 pl-3 text-xs text-muted-foreground">
                    {item.quote}
                  </blockquote>
                ) : null}
              </article>
            );
          })
        ) : (
          <p className="py-10 text-center text-sm text-muted-foreground">後端未回傳內容。</p>
        )}
      </div>
    </section>
  );
}
