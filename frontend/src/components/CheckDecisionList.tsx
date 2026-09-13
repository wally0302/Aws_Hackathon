import { AlertTriangle, CheckCircle2, ChevronRight, CircleHelp, XCircle } from "lucide-react";
import type { CheckResultItem } from "@/api/types";
import { cn } from "@/lib/utils";

import { effectOf, isSelectable, EFFECT_LABEL, type Effect } from "@/api/checkDecision";

/** 顏色與圖示。**判定邏輯在 `@/api/checkDecision`**，這裡只管呈現。 */
const EFFECT_STYLE: Record<
  Effect,
  { cls: string; on: string; badge: string; icon: typeof CheckCircle2 }
> = {
  pass: {
    cls: "border-success/40 bg-success/5",
    on: "border-green-600 ring-2 ring-green-600/40",
    badge: "bg-success/15 text-green-800",
    icon: CheckCircle2,
  },
  amend: {
    cls: "border-warning/50 bg-warning/10",
    on: "border-amber-600 ring-2 ring-amber-500/50",
    badge: "bg-warning/25 text-amber-900",
    icon: AlertTriangle,
  },
  inadmissible: {
    cls: "border-destructive/50 bg-destructive/10",
    on: "border-destructive ring-2 ring-destructive/45",
    badge: "bg-destructive/15 text-destructive",
    icon: XCircle,
  },
  unknown: {
    cls: "border-border bg-muted/40",
    on: "border-foreground/50 ring-2 ring-foreground/20",
    badge: "bg-muted text-muted-foreground",
    icon: CircleHelp,
  },
};

/**
 * 兩個清單 + 「通過」那一項共用同一個 radio group。
 *
 * ⚠️ **名字一定要一樣**，不然在 56 條選一條不會取消 77 條的選擇，
 * 畫面上會同時有兩個黑點，但 `picked` 只有一個值。
 */
export const PICK_GROUP = "check-pick";

/**
 * 「程序無不受理事由」——**正常流程要有一個看得見的選項**。
 *
 * ⚠️⚠️ **不能只靠「都不要選」。** radio 點下去之後**取消不掉**
 * （這是 radio 的天性，不是 bug），而系統預設會幫他選一條最嚴重的。
 * 所以少了這一項，承辦人一旦看到預選的紅色項目就再也回不到通過
 * ——實測 2026-09-13 回報「沒辦法取消勾選，這樣就沒辦法走通過了」。
 */
export function PassOption({
  picked,
  onPick,
  candidateCount,
}: {
  picked: string | null;
  onPick: (code: string | null) => void;
  /** 系統找到幾項可採用的事由，用來提示他下面有東西要看 */
  candidateCount: number;
}) {
  const on = picked === null;
  return (
    <label
      className={cn(
        "flex cursor-pointer gap-3 rounded-lg border bg-card p-3 shadow-sm",
        on && "border-green-600 bg-success/5 ring-2 ring-green-600/40",
      )}
    >
      <input
        type="radio"
        name={PICK_GROUP}
        className="mt-1 h-4 w-4 shrink-0 accent-current"
        checked={on}
        onChange={() => onPick(null)}
      />
      <div className="min-w-0 flex-1 text-sm">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-medium">程序無不受理或補正事由</span>
          <span className="rounded bg-success/15 px-1.5 py-0.5 text-xs text-green-800">通過</span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {candidateCount
            ? `進入實體審查。⚠️ 系統在下面標了 ${candidateCount} 項可採用的事由，請先看過再選這一項。`
            : "進入實體審查。系統沒有找到可採用的不受理或補正事由。"}
        </p>
      </div>
    </label>
  );
}

export function CheckDecisionList({
  title,
  items,
  picked,
  onPick,
  /** 說明還沒回來時顯示的字（Bedrock 掛掉或模型漏寫） */
  narrativeNote,
}: {
  title: string;
  items: CheckResultItem[];
  /** 全畫面只有一條會被選中，所以傳 code 不傳 Set */
  picked: string | null;
  onPick: (code: string) => void;
  narrativeNote?: string | null;
}) {
  if (!items.length) return null;

  return (
    <section className="rounded-lg border bg-card shadow-sm">
      <header className="border-b bg-muted/50 px-4 py-2.5">
        <h2 className="text-sm font-semibold">{title}</h2>
      </header>
      <ul className="divide-y">
        {items.map((item) => {
          const effect = effectOf(item);
          const style = EFFECT_STYLE[effect];
          const Icon = style.icon;
          const isOn = picked === item.code;

          const inputId = `check-${item.code}`;
          // ⚠️ **只有「不受理」「待補正」可以選**（2026-09-13 改）。
          //    通過的沒什麼好採用的；「系統無法判斷」選了也只會得到
          //    「進入下一步」，跟沒選一樣，反而讓人以為表達了什麼。
          const selectable = isSelectable(item);

          return (
            <li key={item.code} className={cn("p-3", style.cls, isOn && style.on)}>
              <div className="flex gap-3">
                {/*
                  ⚠️ **radio 不是 checkbox。** 一件案子只有一個處置方向，
                  複選的話「勾了 3 紅 2 黃」要靠程式幫承辦人收斂成一個
                  結論——那是他該決定的事。

                  ⚠️ **`<label>` 只包標題那一行，不要包整列。**
                  說明是可收合的 `<details>`，包在 label 裡的話點「展開說明」
                  會連帶把這一條選起來（實測踩過）。
                */}
                {selectable ? (
                  <input
                    id={inputId}
                    type="radio"
                    name={PICK_GROUP}
                    className="mt-1 h-4 w-4 shrink-0 accent-current"
                    checked={isOn}
                    onChange={() => onPick(item.code)}
                  />
                ) : (
                  // 不能選的就放圖示占位——直接留白的話右邊整欄會對不齊
                  <Icon
                    className={cn(
                      "mt-0.5 h-4 w-4 shrink-0",
                      effect === "pass" ? "text-green-700" : "text-muted-foreground",
                    )}
                    aria-hidden
                  />
                )}

                <div className="min-w-0 flex-1 text-sm">
                  <label
                    htmlFor={selectable ? inputId : undefined}
                    className={cn(
                      "flex flex-wrap items-baseline gap-x-2",
                      selectable && "cursor-pointer",
                    )}
                  >
                    <span className="font-mono text-xs text-muted-foreground">{item.code}</span>
                    <span className="font-medium">{item.label}</span>
                    <span
                      className={cn(
                        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs",
                        style.badge,
                      )}
                    >
                      {EFFECT_LABEL[effect]}
                    </span>
                    {item.value ? (
                      <span className="text-xs text-muted-foreground">{item.value}</span>
                    ) : null}
                  </label>

                  {/* 期間計算：**把算式攤開，而且不收合**。它是 77-2 這條
                      「逾期 N 日」的全部依據，收起來就等於藏起關鍵證據。 */}
                  {item.computed?.ok ? (
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {item.computed.served_on} 送達 → {item.computed.start_from} 起算 →{" "}
                      {item.computed.deadline} 屆滿 → {item.computed.filed_on} 提起
                      {item.computed.is_overdue
                        ? `（逾期 ${item.computed.overdue_days} 日）`
                        : `（第 ${item.computed.day_n} 日，尚餘 ${item.computed.remaining_days} 日）`}
                    </p>
                  ) : null}

                  {/* ★ 說明預設收合。20 條全部攤開的話要捲三、四個畫面，
                      承辦人根本找不到該看哪一條。 */}
                  <details className="group mt-1">
                    <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                      <ChevronRight
                        className="h-3 w-3 transition-transform group-open:rotate-90"
                        aria-hidden
                      />
                      說明
                    </summary>

                    <div className="mt-1.5 border-l-2 pl-3">
                      {/* 白話說明：文件裡怎麼寫 → 依據哪一條 → 所以結果是什麼。
                          ⚠️ 這是**說明不是判斷**，結論仍然是程式判的。 */}
                      {item.ai_narrative ? (
                        <p className="leading-6">{item.ai_narrative}</p>
                      ) : (
                        <>
                          <p className="text-xs italic text-muted-foreground">
                            {narrativeNote ?? "（這一條沒有產生說明）"}
                          </p>
                          {/* 說明沒產生時退回程式的備註。⚠️ 它會出現欄位名
                              （例如「未載明：birth、id_no」），不好讀但總比空白好。 */}
                          {item.note ? <p className="mt-1 leading-6">{item.note}</p> : null}
                        </>
                      )}

                      {/* 引的原句。⚠️ 要跟說明分開顯示——承辦人要一眼看出
                          哪一句是文件裡真的有的、哪一句是系統寫的。 */}
                      {item.ai_narrative_quote ? (
                        <blockquote className="mt-1.5 rounded bg-muted/60 px-2 py-1 text-xs text-muted-foreground">
                          「{item.ai_narrative_quote}」
                          {item.ai_narrative_quote_from ? (
                            <span className="ml-1">—— {item.ai_narrative_quote_from}</span>
                          ) : null}
                        </blockquote>
                      ) : null}

                      {/* 模型不同意程式的判斷。**只提醒，不改結論。** */}
                      {item.ai_narrative_disagree ? (
                        <p className="mt-1.5 rounded border border-dashed px-2 py-1 text-xs text-muted-foreground">
                          系統的判讀另有意見：{item.ai_narrative_disagree}
                          <span className="ml-1">（結論未變，請自行斟酌）</span>
                        </p>
                      ) : null}

                      {item.basis ? (
                        <p className="mt-1.5 text-xs text-muted-foreground">依據：{item.basis}</p>
                      ) : null}
                    </div>
                  </details>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
