import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { LoaderCircle, Play, UserCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { useCase } from "@/api/hooks";
import { useStageRunner } from "@/api/useStageRunner";
import type { Stage1Detail } from "@/api/types";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { decide, defaultPick, isSelectable } from "@/api/checkDecision";
import { CheckDecisionList, PassOption } from "@/components/CheckDecisionList";
import { Button } from "@/components/ui/button";
import { useWorkflow } from "@/state/workflow";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/case-review")({
  head: () => ({ meta: [{ title: "文件與程序檢核｜訴願快速拆解平台" }] }),
  component: CaseReview,
});

function CaseReview() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { activeCaseId } = useWorkflow();
  const currentCase = useCase(activeCaseId);
  const {
    stage,
    busy: running,
    runStage,
    refresh,
  } = useStageRunner<Stage1Detail>(activeCaseId, 1, {
    autoRun: false,
    successMessage: "程序檢核已完成",
    failMessage: "程序檢核失敗",
    invalidateCases: true,
  });
  const [confirming, setConfirming] = useState(false);
  const busy = running || confirming;

  const detail = stage.data?.detail;
  const specItems = useMemo(() => detail?.spec_check?.items ?? [], [detail]);
  const procItems = useMemo(() => detail?.procedure_check?.items ?? [], [detail]);
  const allItems = useMemo(() => [...procItems, ...specItems], [procItems, specItems]);

  // ⚠️ **只能選一條**（2026-09-13 改，原本是複選的 Set）。
  //    一件案子只會有一個處置方向；複選的話「勾了 3 紅 2 黃」要靠程式
  //    幫承辦人收斂出一個結論，那是他該決定的事，不是系統該猜的。
  const [picked, setPicked] = useState<string | null>(null);

  // ⚠️ **檢核結果換了才重設選擇**，不要每次 render 都重算——
  //    承辦人手動改的選擇會被蓋回去。用 version 當 key，
  //    重跑檢核（version 會變）時才套用新的預設值。
  const version = stage.data?.version;
  useEffect(() => {
    if (!allItems.length) return;
    setPicked(defaultPick(allItems));
  }, [version, allItems]);

  const pickedItem = useMemo(
    () => allItems.find((i) => i.code === picked) ?? null,
    [allItems, picked],
  );
  const plan = decide(pickedItem);

  // 說明整批沒產生時要講原因，不然那 20 條全部顯示「沒有產生說明」
  // 看起來像系統壞了
  const narrativeNote = detail?.check_narrative?.error
    ? "（這一批說明沒有產生成功，結論仍然有效）"
    : null;

  const confirm = async () => {
    if (!activeCaseId) return;
    setConfirming(true);
    try {
      await api.confirmStage(activeCaseId, 1, { disposition: plan.disposition });
      await refresh();
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      // ⚠️ **不受理也走階段 2、3**（2026-09-13 改，原本直接跳 /step4）。
      //    實際案例的不受理決定書：主文寫不受理、理由第一點寫不受理事由，
      //    但後面仍然要對訴願人的主張與機關的答辯作說明。
      if (plan.disposition === "inadmissible" || plan.disposition === "substantive") {
        toast.success(`階段一已確認：${plan.label}`);
        void navigate({ to: "/step2" });
        return;
      }
      // 待補正離開主線——這件案子接下來要等訴願人補件，承辦人在這一頁
      // 已經沒有事情可做。**直接帶回案件清單的「等待外部資料」**，
      // 留在原地的話看起來像按鈕沒有作用（實測 2026-09-13 的回報）。
      if (plan.disposition === "amend") {
        toast.success("已轉為待補正，案件移至「等待外部資料」");
        void navigate({ to: "/", search: { tab: "awaitingReview", sub: "waiting" } });
        return;
      }
      toast.success(`階段一已確認：${plan.label}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "確認失敗");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <AppShell caseStep={1}>
      <ApiErrorBanner error={currentCase.error ?? stage.error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">步驟一：文件與程序檢核</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            右邊每一條都有系統的白話說明。<strong>只能選一條</strong>
            ，右上按鈕會跟著改變處置方向；程序沒問題就選最上面那一項。
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" disabled={busy || !activeCaseId} onClick={runStage}>
            {running ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Play className="h-4 w-4" aria-hidden />
            )}
            {detail ? "重新檢核" : "執行檢核"}
          </Button>
          <Button
            className={cn(
              plan.disposition === "inadmissible" &&
                "bg-destructive text-destructive-foreground hover:bg-destructive/90",
              plan.disposition === "amend" && "bg-warning text-amber-950 hover:bg-warning/90",
            )}
            disabled={busy || !stage.data || stage.data.status === "running"}
            onClick={confirm}
          >
            {confirming ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <UserCheck className="h-4 w-4" aria-hidden />
            )}
            {plan.label}
          </Button>
        </div>
      </div>

      {detail ? (
        <p
          className={cn(
            "mt-3 rounded-md border p-2.5 text-sm",
            plan.disposition === "inadmissible" && "border-destructive/40 bg-destructive/5",
            plan.disposition === "amend" && "border-warning/50 bg-warning/10",
            plan.disposition === "substantive" && "border-border bg-muted/40",
          )}
        >
          {plan.hint}
        </p>
      ) : null}

      {/* 左原文、右勾選。⚠️ 兩欄各自捲動——原文很長（實測 1,809 字），
          跟著整頁捲的話勾到下面幾條時就看不到對應的段落了。 */}
      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <section className="rounded-lg border bg-card shadow-sm">
          <header className="flex items-baseline justify-between border-b bg-muted/50 px-4 py-2.5">
            <h2 className="text-sm font-semibold">訴願書原文</h2>
            {detail?.petition_text ? (
              <span className="text-xs text-muted-foreground">
                {detail.petition_text.length.toLocaleString()} 字
              </span>
            ) : null}
          </header>
          <div className="max-h-[70vh] overflow-y-auto p-4">
            {detail?.petition_text ? (
              <div className="space-y-3 text-sm leading-7">
                {detail.petition_text
                  .split("\n")
                  .filter((line) => line.trim())
                  .map((line, i) => (
                    <p key={i}>{line}</p>
                  ))}
              </div>
            ) : (
              <p className="py-16 text-center text-sm text-muted-foreground">
                {running ? "檢核執行中…" : "尚未執行檢核，或訴願書沒有可讀取的文字。"}
              </p>
            )}
          </div>
        </section>

        <div className="space-y-4">
          {detail ? (
            <>
              {/* ⚠️⚠️ 「通過」必須是**看得見的選項**，不能只靠「都不要選」。
                  radio 點下去之後取消不掉，而預設又會幫他選一條最嚴重的
                  ——少了這一項，承辦人就再也回不到通過
                  （2026-09-13 實測回報）。 */}
              <PassOption
                picked={picked}
                onPick={setPicked}
                candidateCount={allItems.filter(isSelectable).length}
              />
              {/* ⚠️ 三者**共用同一個 radio group**（`PICK_GROUP`），
                  所以在 56 條選一條會取消 77 條的選擇——這是刻意的，
                  處置方向只有一個。 */}
              <CheckDecisionList
                title="訴願法第 77 條 · 不受理事由（八款）"
                items={procItems}
                picked={picked}
                onPick={setPicked}
                narrativeNote={narrativeNote}
              />
              <CheckDecisionList
                title="訴願法第 56 條 · 訴願書應記載事項"
                items={specItems}
                picked={picked}
                onPick={setPicked}
                narrativeNote={narrativeNote}
              />
            </>
          ) : (
            <div className="rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
              {running ? "檢核執行中…" : "尚未執行檢核。"}
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}
