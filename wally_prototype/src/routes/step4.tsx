import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { LoaderCircle, Play, Save, UserCheck } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { decisionToText, toDecisionDoc } from "@/api/adapters";
import { api } from "@/api/client";
import { useStageRunner } from "@/api/useStageRunner";
import type { DecisionDoc, Stage4Detail } from "@/api/types";
import { RationalePanel } from "@/components/RationalePanel";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useWorkflow } from "@/state/workflow";

export const Route = createFileRoute("/step4")({
  head: () => ({ meta: [{ title: "決定文草擬｜訴願快速拆解平台" }] }),
  component: Step4,
});

function Step4() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { activeCaseId } = useWorkflow();
  // 進頁面若還沒跑就自動跑；「產生草稿」按鈕留著給承辦人重跑
  const {
    stage,
    busy: running,
    runStage,
    refresh,
  } = useStageRunner<Stage4Detail>(activeCaseId, 4, {
    autoRun: true,
    successMessage: "決定文草稿已產生",
    failMessage: "階段四執行失敗",
  });
  const [confirming, setConfirming] = useState(false);
  const busy = running || confirming;
  const detail = stage.data?.detail;

  const confirm = async () => {
    if (!activeCaseId) return;
    setConfirming(true);
    try {
      await api.confirmStage(activeCaseId, 4);
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("階段四已確認");
      void navigate({ to: "/step5" });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "確認失敗");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <AppShell caseStep={4}>
      <ApiErrorBanner error={stage.error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">步驟四：決定文草擬</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            左邊是決定書欄位，右邊是系統寫這份草稿時的判斷過程。
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" disabled={busy || !activeCaseId} onClick={runStage}>
            {running ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Play className="h-4 w-4" aria-hidden />
            )}
            {detail ? "重新產生草稿" : "產生草稿"}
          </Button>
          {/* ⚠️ 原本有 `alerts.some(level === "blocking")` 會擋住這顆按鈕。
              警示清單拿掉之後這個條件**一起拿掉**——按鈕變灰卻沒有任何
              說明的話，承辦人只會以為系統壞了。要擋的話警示要先看得到。 */}
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
          正在讀取階段四結果…
        </div>
      ) : running && !detail ? (
        <div className="mt-5 flex items-center justify-center gap-2 rounded-lg border border-dashed py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          AI 正在草擬決定文，約需一至兩分鐘…
        </div>
      ) : detail ? (
        <>
          {/* ⚠️ **這裡原本有「處置：實體審理／來源 9 筆」兩個標籤，
              以及後端檢核警示的清單（「主文與各段結論對不上」
              「3 條主張機關沒有答辯」那些）。2026-09-13 拿掉。**

              資料沒有消失，只是不在這一頁講：警示仍然在階段四的
              `summary` 與 `draft.main_text_vs_sections_warning` 裡，
              被標記的段落也還是有 `guardrail` 欄位。
              要放回來的話用 `draftAlerts(detail)`（adapters.ts）。 */}
          {/* 左決定書、右心路歷程。⚠️ 右欄獨立捲動——推理文字很長，
              跟著整頁捲的話看下面幾段時左邊對應的欄位已經捲掉了。 */}
          <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
            {/*
              key 綁版本號 + 狀態：重新產生草稿後表單整個重置成新的預填值，
              不會留著承辦人對舊版本的修改。

              ⚠️⚠️ **`status` 一定要放進 key，不能只有 version。**
              後端 `get_stage()` 回的 `version` 取自 progress，而 `detail`
              取自已存檔的結果——重跑時這兩個**不同步**：

                  按下重新產生 → {version: 7, status: running, detail: 第6版}
                                  ← key 變成 :7，表單用**舊** detail 重掛
                  跑完        → {version: 7, status: done,    detail: 第7版}
                                  ← key 沒變，`useState` 不會重取 → 畫面停在舊的

              症狀是「產生草稿跑完，結構化欄位還是舊的，重新整理才對」
              （2026-09-13 回報）。加上 status 之後 running→done 這一下
              就會讓它重掛。
            */}
            <DecisionPanel
              key={`${activeCaseId}:${stage.data?.version ?? 0}:${stage.data?.status}`}
              caseId={activeCaseId ?? ""}
              initial={toDecisionDoc(detail, activeCaseId ?? "")}
              onSaved={refresh}
            />
            <RationalePanel detail={detail} />
          </div>
        </>
      ) : !stage.error ? (
        <div className="mt-5 rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
          後端尚未產生階段四結果。
        </div>
      ) : null}
    </AppShell>
  );
}

/* ───────────────── 決定書固定欄位表單 ───────────────── */

type FieldSpec = {
  key: keyof DecisionDoc;
  label: string;
  /** 沒給就是單行 Input */
  rows?: number;
  hint?: string;
};

const SECTIONS: { title: string; fields: FieldSpec[] }[] = [
  {
    title: "一、基本資料",
    fields: [
      { key: "case_no", label: "案號" },
      { key: "gist", label: "要旨" },
      { key: "issue_date", label: "發文日期", hint: "預設為今天，發文時再改" },
      { key: "doc_no", label: "發文字號", hint: "由文書系統取號後填入" },
      { key: "related_laws", label: "相關法條", rows: 3, hint: "依主文推定，請核對" },
    ],
  },
  {
    title: "二、全文",
    fields: [
      { key: "appellant", label: "訴願人" },
      { key: "original_agency", label: "原處分機關" },
      { key: "main_text", label: "主文（最終裁定結果）", rows: 3 },
      { key: "facts", label: "事實", rows: 10 },
      { key: "reasons", label: "理由", rows: 18 },
    ],
  },
  {
    title: "三、委員會署名",
    fields: [
      { key: "committee", label: "訴願審議委員會", rows: 4, hint: "主任委員一行、每位委員一行" },
    ],
  },
];

function DecisionPanel({
  caseId,
  initial,
  onSaved,
}: {
  caseId: string;
  initial: DecisionDoc;
  onSaved: () => Promise<void>;
}) {
  const [doc, setDoc] = useState<DecisionDoc>(initial);
  // ⚠️ **預設看結構化欄位。** 原文是拿來核對用的，承辦人的主要工作
  //    是在欄位上改字。
  const [view, setView] = useState<"fields" | "raw">("fields");
  const [saving, setSaving] = useState(false);
  const set = (key: keyof DecisionDoc) => (value: string) =>
    setDoc((prev) => ({ ...prev, [key]: value }));

  // 哪幾個欄位被改過。⚠️ 只送改過的——整包送會把承辦人沒碰過的欄位
  //    也蓋成前端 fallback 拼出來的值。
  const dirty = (Object.keys(initial) as (keyof DecisionDoc)[]).filter(
    (k) => doc[k] !== initial[k],
  );

  const save = async () => {
    if (!caseId || !dirty.length) return;
    setSaving(true);
    try {
      const patch: Partial<DecisionDoc> = {};
      for (const k of dirty) patch[k] = doc[k];
      await api.patchFields(caseId, 4, { decision: patch });
      // 重抓：版本號會變，決定書 PDF 也會跟著重排成新版本
      await onSaved();
      toast.success(`已儲存 ${dirty.length} 個欄位`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "儲存失敗");
    } finally {
      setSaving(false);
    }
  };

  return (
    // ⚠️ 不要加 mt-*：現在放在 grid 裡，外層已經有間距，
    //    自己再加會讓左右兩欄上緣對不齊。
    <section className="rounded-lg border bg-card shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/50 px-4 py-2">
        {/* 兩個檢視共用同一個區塊，用按鈕切換。⚠️ 原本原文是下面獨立的
            `<details>`，兩邊要對照時得上下捲——同一個位置切換才對得起來。 */}
        <div className="flex gap-1" role="group" aria-label="檢視方式">
          {(
            [
              ["fields", "結構化欄位"],
              ["raw", "決定書原文"],
            ] as const
          ).map(([value, label]) => (
            <Button
              key={value}
              size="sm"
              variant={view === value ? "default" : "ghost"}
              onClick={() => setView(value)}
              aria-pressed={view === value}
            >
              {label}
            </Button>
          ))}
        </div>

        {view === "raw" ? (
          <span className="text-xs text-muted-foreground">
            由左邊的欄位即時組出來的，唯讀——要改請切回「結構化欄位」
          </span>
        ) : (
          <div className="flex items-center gap-2">
            {dirty.length ? (
              <span className="text-xs text-amber-700">{dirty.length} 個欄位未儲存</span>
            ) : null}
            <Button size="sm" disabled={saving || !dirty.length} onClick={save}>
              {saving ? (
                <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
              ) : (
                <Save className="h-4 w-4" aria-hidden />
              )}
              儲存
            </Button>
          </div>
        )}
      </header>

      {view === "raw" ? (
        // ⚠️ **唯讀，而且是從左邊的欄位即時算的**（`decisionToText(doc)`），
        //    不是從 `draft.sections` 算的。改了欄位這裡馬上跟著變，
        //    兩邊永遠同步。原本用 `toFullText(detail.draft)` 的話，
        //    承辦人改完欄位這裡還是舊的，而且畫面上看不出來。
        <pre className="max-h-[70vh] overflow-auto whitespace-pre-wrap p-5 font-mono text-sm leading-7">
          {decisionToText(doc)}
        </pre>
      ) : (
        <div className="space-y-6 p-4">
          {SECTIONS.map((section) => (
            <fieldset key={section.title} className="space-y-3">
              <legend className="text-sm font-semibold">{section.title}</legend>
              {section.fields.map((f) => {
                const id = `decision-${f.key}`;
                return (
                  <div key={f.key} className="grid gap-1.5">
                    <Label htmlFor={id}>{f.label}</Label>
                    {f.rows ? (
                      <Textarea
                        id={id}
                        rows={f.rows}
                        value={doc[f.key]}
                        onChange={(e) => set(f.key)(e.target.value)}
                        className="leading-7"
                      />
                    ) : (
                      <Input
                        id={id}
                        value={doc[f.key]}
                        onChange={(e) => set(f.key)(e.target.value)}
                      />
                    )}
                    {f.hint ? <p className="text-xs text-muted-foreground">{f.hint}</p> : null}
                  </div>
                );
              })}
            </fieldset>
          ))}
        </div>
      )}
    </section>
  );
}
