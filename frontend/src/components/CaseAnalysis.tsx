import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { toAnalysis } from "@/api/adapters";
import { useStage } from "@/api/hooks";
import type { Stage1Detail } from "@/api/types";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import type { CaseFile, CheckItem } from "@/domain/case";
import { cn } from "@/lib/utils";

const verdictStyle = {
  pass: "border-success/40 bg-success/5",
  fail: "border-destructive/40 bg-destructive/5",
  info: "border-border bg-muted/40",
} as const;

const verdictLabel = { pass: "通過", fail: "不通過", info: "註記" } as const;

export function CaseAnalysis({
  caseFile,
  running = false,
}: {
  caseFile: CaseFile;
  /** 階段 1 正在跑（父層在輪詢中） */
  running?: boolean;
}) {
  const stage = useStage<Stage1Detail>(caseFile.id, 1);
  const [hover, setHover] = useState<string | null>(null);
  const analysis = stage.data?.detail ? toAnalysis(stage.data.detail) : null;

  if (stage.isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-lg border px-4 py-16 text-sm text-muted-foreground">
        <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        正在讀取程序檢核結果…
      </div>
    );
  }

  if (!analysis && running) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed px-4 py-16 text-sm text-muted-foreground">
        <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        AI 正在檢核文件與程序，約需一分鐘…
      </div>
    );
  }

  if (!analysis) {
    return (
      <div>
        <ApiErrorBanner error={stage.error} />
        {!stage.error ? (
          <div className="rounded-lg border border-dashed px-4 py-16 text-center text-sm text-muted-foreground">
            後端尚未產生階段一結果。
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div>
      <ApiErrorBanner error={stage.error} />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="rounded-lg border">
          <header className="border-b bg-muted/50 px-4 py-2 text-sm font-semibold">
            訴願書原文
          </header>
          <div className="max-h-[65vh] space-y-4 overflow-y-auto p-4 text-sm leading-7">
            {analysis.document.length ? (
              analysis.document.map((paragraph) => (
                <p key={paragraph.id}>
                  {renderWithMarks(
                    paragraph.text,
                    paragraph.marks ?? [],
                    hover,
                    analysis.checks,
                    analysis.highlights,
                  )}
                </p>
              ))
            ) : (
              <p className="text-muted-foreground">後端未回傳文件文字。</p>
            )}
          </div>
        </section>

        <section className="rounded-lg border">
          <header className="border-b bg-muted/50 px-4 py-2 text-sm font-semibold">
            AI 判讀結果
          </header>
          <div className="space-y-3 p-4">
            {stage.data?.summary ? (
              <p className="rounded-md bg-secondary p-3 text-sm leading-6">{stage.data.summary}</p>
            ) : null}
            {analysis.checks.length ? (
              analysis.checks.map((check) => (
                <div
                  key={check.id}
                  onMouseEnter={() => setHover(check.id)}
                  onMouseLeave={() => setHover(null)}
                  className={cn("rounded-md border p-3 text-sm", verdictStyle[check.verdict])}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">{check.label}</span>
                    <span className="rounded bg-muted px-1.5 py-0.5 text-xs">
                      {verdictLabel[check.verdict]}
                    </span>
                    {check.value ? (
                      <span className="ml-auto text-xs text-muted-foreground">{check.value}</span>
                    ) : null}
                  </div>
                  {check.reason ? (
                    <p className="mt-1 leading-6 text-muted-foreground">{check.reason}</p>
                  ) : null}
                </div>
              ))
            ) : (
              <p className="py-10 text-center text-sm text-muted-foreground">
                後端未回傳檢核項目。
              </p>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function renderWithMarks(
  text: string,
  marks: string[],
  hover: string | null,
  checks: CheckItem[],
  highlights: Record<string, string>,
) {
  if (!marks.length) return text;
  const hovered = checks.find((item) => item.id === hover)?.anchor;
  const parts: Array<string | { key: string; value: string }> = [text];

  for (const mark of marks) {
    const needle = highlights[mark];
    if (!needle) continue;
    for (let index = 0; index < parts.length; index += 1) {
      const part = parts[index];
      if (typeof part !== "string") continue;
      const offset = part.indexOf(needle);
      if (offset < 0) continue;
      parts.splice(
        index,
        1,
        part.slice(0, offset),
        { key: mark, value: needle },
        part.slice(offset + needle.length),
      );
      break;
    }
  }

  return parts.map((part, index) =>
    typeof part === "string" ? (
      <span key={index}>{part}</span>
    ) : (
      <mark
        key={index}
        className={cn(
          "rounded px-0.5",
          hovered === part.key ? "bg-primary text-primary-foreground" : "bg-highlight",
        )}
      >
        {part.value}
      </mark>
    ),
  );
}
