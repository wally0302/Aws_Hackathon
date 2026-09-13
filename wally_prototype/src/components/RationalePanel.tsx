import { Bot, Lightbulb, Settings2 } from "lucide-react";
import type { Stage4Detail } from "@/api/types";

/**
 * AI 撰寫決定書時的推理過程。**給承辦人看的，不進決定書。**
 *
 * 分兩層顯示，跟後端的結構一致：
 *   全案  `overall_rationale`        為什麼整體是這個結論
 *   每段  `sections[].rationale`     這一條主張為什麼這樣回應
 *
 * ⚠️ **不受理路線的前幾段沒有推理過程**（2026-09-13 起）。那幾段是
 * 不受理事由，由程式依規則算出來的（`sections[].source === "rule"`）；
 * 後面 AI 寫的說明一樣有 rationale。所以這裡不能整頁換成「沒有推理過程」，
 * 要講清楚是「前幾段沒有」。
 */
export function RationalePanel({ detail }: { detail: Stage4Detail }) {
  const sections = detail.draft?.sections ?? [];
  const withRationale = sections.filter((s) => s.rationale?.trim());
  const overall = detail.overall_rationale?.trim();
  const conclusion = detail.overall_conclusion?.trim();
  const ruleSections = sections.filter((s) => s.source === "rule");
  // 不受理而且**一段 AI 說明都沒有**時才是舊的純模板草稿
  const isTemplate = detail.route === "inadmissible" && !withRationale.length;

  return (
    <section className="rounded-lg border bg-card shadow-sm">
      <header className="flex items-center gap-2 border-b bg-muted/50 px-4 py-2.5">
        <Bot className="h-4 w-4 text-primary" aria-hidden />
        <h2 className="text-sm font-semibold">AI 判斷脈絡</h2>
        <span className="ml-auto text-xs text-muted-foreground">不進決定書</span>
      </header>

      <div className="max-h-[70vh] space-y-4 overflow-y-auto p-4">
        {isTemplate ? (
          <div className="flex gap-2 rounded-md border border-border bg-muted/40 p-3 text-sm">
            <Settings2 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            <div>
              <p className="font-medium">本件是不受理，草稿由程式產生</p>
              <p className="mt-1 leading-6 text-muted-foreground">
                不受理決定書不談實體理由，內容就是「哪一條法定要件不合、逾期幾日」，
                而這些數字全是程式依規則算出來的。讓 AI 寫只會多出編造的風險，
                所以這條路線沒有推理過程可以呈現。
              </p>
              <p className="mt-1.5 text-xs text-muted-foreground">
                判斷依據請看左側的「理由」欄位與階段一的檢核結果。
              </p>
            </div>
          </div>
        ) : null}

        {/* ★ **結論放最前面，而且要一眼看得到。**
            承辦人看完這一整頁推理，第一個問題是「所以結論是什麼」。
            埋在 400 字的判斷脈絡中間的話，他得先讀完才知道。 */}
        {conclusion ? (
          <div className="rounded-md border-l-4 border-primary bg-primary/5 p-3">
            <h3 className="text-xs font-semibold text-muted-foreground">結論</h3>
            <p className="mt-1 whitespace-pre-wrap text-sm font-medium leading-7">{conclusion}</p>
          </div>
        ) : null}

        {/* 不受理時前幾段是程式產的，這件事要講，不然承辦人會找不到
            那幾段的推理過程 */}
        {detail.route === "inadmissible" && ruleSections.length ? (
          <div className="flex gap-2 rounded-md border border-dashed p-3 text-xs text-muted-foreground">
            <Settings2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <p className="leading-6">
              理由第 1–{ruleSections.length} 點是<strong>不受理事由</strong>
              ，由程式依規則算出來的（逾期天數、欠缺哪一款），沒有 AI
              的推理過程可看——請直接核對左側的期間計算。 下面列的是 AI
              對訴願人主張與機關答辯那幾段的推理。
            </p>
          </div>
        ) : null}

        {overall ? (
          <div className="rounded-md border border-primary/25 bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5">
              <Lightbulb className="h-4 w-4 text-primary" aria-hidden />
              <h3 className="text-sm font-semibold">判斷脈絡</h3>
            </div>
            <p className="mt-1.5 whitespace-pre-wrap text-sm leading-7">{overall}</p>
          </div>
        ) : null}

        {withRationale.length ? (
          <div className="space-y-3">
            <h3 className="text-xs font-semibold text-muted-foreground">
              各段理由的推理（{withRationale.length} 段）
            </h3>
            {withRationale.map((s) => (
              <div key={`${s.no}-${s.claim_id ?? ""}`} className="rounded-md border p-3">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-sm font-semibold">第 {s.no} 段</span>
                  {s.conclusion ? (
                    <span className="rounded bg-secondary px-1.5 py-0.5 text-xs">
                      {s.conclusion}
                    </span>
                  ) : null}
                  {/* 這一段引用了哪幾筆依據——推理要能對到依據才可查核 */}
                  {s.citations?.length ? (
                    <span className="text-xs text-muted-foreground">
                      引用 {s.citations.length} 筆
                    </span>
                  ) : null}
                </div>
                <p className="mt-1.5 whitespace-pre-wrap text-sm leading-7 text-muted-foreground">
                  {s.rationale}
                </p>
              </div>
            ))}
          </div>
        ) : null}

        {!isTemplate && !overall && !conclusion && !withRationale.length ? (
          <p className="py-12 text-center text-sm text-muted-foreground">
            這份草稿沒有推理過程。
            <br />
            <span className="text-xs">草稿是在後端加入此功能之前產生的，重新草擬即可產生。</span>
          </p>
        ) : null}
      </div>
    </section>
  );
}
