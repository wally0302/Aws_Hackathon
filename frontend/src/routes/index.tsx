import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import {
  CheckCircle2,
  CircleDashed,
  FileSearch,
  Inbox,
  LoaderCircle,
  Pencil,
  Plus,
  Search,
  Sparkles,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { useCases } from "@/api/hooks";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { CaseFile, CaseStage } from "@/domain/case";
import { cn } from "@/lib/utils";
import { useWorkflow } from "@/state/workflow";

type SummaryFilter = "awaitingAnalysis" | "awaitingReview" | "inReview" | "closed";
type AwaitingReviewFilter = "all" | "actionable" | "waiting";

const SUMMARY_FILTERS: readonly SummaryFilter[] = [
  "awaitingAnalysis",
  "awaitingReview",
  "inReview",
  "closed",
];

function isSummaryFilter(v: unknown): v is SummaryFilter {
  return typeof v === "string" && (SUMMARY_FILTERS as readonly string[]).includes(v);
}

const AWAITING_FILTERS: readonly AwaitingReviewFilter[] = ["all", "actionable", "waiting"];

function isAwaitingFilter(v: unknown): v is AwaitingReviewFilter {
  return typeof v === "string" && (AWAITING_FILTERS as readonly string[]).includes(v);
}

export const Route = createFileRoute("/")({
  head: () => ({ meta: [{ title: "我的案件｜訴願快速拆解平台" }] }),
  // 建案完成後帶 `?tab=awaitingAnalysis` 回來，讓承辦人直接看到剛建好的案件。
  // ★ `sub` 指定「待審理」裡面的第二層篩選——階段 1 按「進入待補正」之後
  //   要直接落在「等待外部資料」，不然承辦人得自己再點一次才找得到案件。
  validateSearch: (
    search: Record<string, unknown>,
  ): { tab?: SummaryFilter; sub?: AwaitingReviewFilter } => {
    const tab = search["tab"];
    const sub = search["sub"];
    return {
      ...(isSummaryFilter(tab) ? { tab } : {}),
      ...(isAwaitingFilter(sub) ? { sub } : {}),
    };
  },
  component: CaseQueue,
});

const summaryMeta = {
  awaitingAnalysis: { label: "待分析", helper: "卷證已上傳，尚未執行 AI 分析", icon: CircleDashed },
  awaitingReview: {
    label: "待審理",
    helper: "AI 分析完成，等待初檢、補正或排審",
    icon: FileSearch,
  },
  inReview: { label: "審理中", helper: "承辦人審理爭點、撰擬決定書", icon: Pencil },
  closed: { label: "已結案", helper: "決定書完成的歷史案件", icon: CheckCircle2 },
} satisfies Record<SummaryFilter, { label: string; helper: string; icon: typeof FileSearch }>;

const stageLabel: Record<CaseStage, string> = {
  "not-started": "待分析",
  intake: "待人工初檢",
  supplement: "補正／補卷處理",
  ready: "待實體審理",
  reviewing: "審理／撰稿中",
  pending: "已結案",
};

function summaryOf(stage: CaseStage): SummaryFilter {
  if (stage === "not-started") return "awaitingAnalysis";
  if (stage === "reviewing") return "inReview";
  if (stage === "pending") return "closed";
  return "awaitingReview";
}

function routeFor(item: CaseFile): "/case-review" | "/step2" | "/step3" | "/step4" | "/step5" {
  if (item.stage === "pending") return "/step5";
  if (item.nextStage === 2) return "/step2";
  if (item.nextStage === 3) return "/step3";
  if (item.nextStage === 4) return "/step4";
  if (item.nextStage === 5) return "/step5";
  return "/case-review";
}

function CaseQueue() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { tab, sub } = Route.useSearch();
  const { setActiveCase } = useWorkflow();
  const { data: cases, isLoading, error } = useCases();
  const [summaryFilter, setSummaryFilter] = useState<SummaryFilter>(tab ?? "awaitingReview");
  const [awaitingReviewFilter, setAwaitingReviewFilter] = useState<AwaitingReviewFilter>(
    sub ?? "all",
  );
  const [query, setQuery] = useState("");
  /** 正在送出「執行 AI 分析」的案件。202 回來就結束，不等階段跑完。 */
  const [starting, setStarting] = useState<string | null>(null);

  const counts = useMemo(
    () => ({
      awaitingAnalysis: cases.filter((item) => summaryOf(item.stage) === "awaitingAnalysis").length,
      awaitingReview: cases.filter((item) => summaryOf(item.stage) === "awaitingReview").length,
      inReview: cases.filter((item) => summaryOf(item.stage) === "inReview").length,
      closed: cases.filter((item) => summaryOf(item.stage) === "closed").length,
    }),
    [cases],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return cases.filter((item) => {
      if (summaryOf(item.stage) !== summaryFilter) return false;
      if (summaryFilter === "awaitingReview") {
        if (awaitingReviewFilter === "actionable" && !["intake", "ready"].includes(item.stage))
          return false;
        if (awaitingReviewFilter === "waiting" && item.stage !== "supplement") return false;
      }
      if (!needle) return true;
      return [item.caseNo, item.appellant, item.agency, item.law, item.title, item.assignee]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [cases, query, summaryFilter, awaitingReviewFilter]);

  const openCase = (item: CaseFile) => {
    setActiveCase(item.id);
    void navigate({ to: routeFor(item) });
  };

  /**
   * 「待分析」→「待審理」的那一下。
   *
   * ⚠️ 階段 1 是**非同步**的（後端回 202），這裡只負責觸發並重抓列表；
   * 列表重抓後階段 1 會是 `running`，案件就會落到「待人工初檢」。
   */
  const startAnalysis = async (item: CaseFile) => {
    setStarting(item.id);
    try {
      await api.runStage(item.id, 1);
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success(`案件 ${item.caseNo} 已開始 AI 分析，已移至「待審理」`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "無法啟動 AI 分析");
    } finally {
      setStarting(null);
    }
  };

  return (
    <AppShell sectionLabel="我的案件">
      <ApiErrorBanner error={error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">我的案件</h1>
          <p className="mt-1 text-sm text-muted-foreground">案件狀態與內容皆由後端即時提供。</p>
        </div>
        <Button onClick={() => navigate({ to: "/new-case" })}>
          <Plus className="h-4 w-4" aria-hidden />
          建立案件
        </Button>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {SUMMARY_FILTERS.map((key) => {
          const meta = summaryMeta[key];
          const Icon = meta.icon;
          const selected = summaryFilter === key;
          return (
            <button
              key={key}
              type="button"
              onClick={() => {
                setSummaryFilter(key);
                setAwaitingReviewFilter("all");
              }}
              className={cn(
                "rounded-xl border bg-card p-4 text-left shadow-sm transition-colors",
                selected ? "border-primary ring-1 ring-primary" : "hover:border-primary/40",
              )}
            >
              <div className="flex items-center gap-3">
                <span className="rounded-lg bg-secondary p-2 text-primary">
                  <Icon className="h-5 w-5" aria-hidden />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="font-semibold">{meta.label}</p>
                  <p className="text-xs text-muted-foreground">{meta.helper}</p>
                </div>
                <span className="text-2xl font-semibold">{counts[key]}</span>
              </div>
            </button>
          );
        })}
      </div>

      {summaryFilter === "awaitingReview" ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {/* 沒有「全部」按鈕：選中的再點一次就取消，回到全部待審理 */}
          {(
            [
              ["actionable", "待處理"],
              ["waiting", "等待外部資料"],
            ] as const
          ).map(([value, label]) => (
            <Button
              key={value}
              size="sm"
              variant={awaitingReviewFilter === value ? "default" : "outline"}
              onClick={() =>
                setAwaitingReviewFilter((current) => (current === value ? "all" : value))
              }
            >
              {value === "waiting" ? <Inbox className="h-4 w-4" aria-hidden /> : null}
              {label}
            </Button>
          ))}
        </div>
      ) : null}

      <div className="mt-4 max-w-md">
        <div className="relative">
          <Search
            className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜尋案號、訴願人、機關、法規或承辦人"
            className="pl-9"
          />
        </div>
      </div>

      <section className="mt-4 overflow-hidden rounded-xl border bg-card shadow-sm">
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 px-4 py-16 text-sm text-muted-foreground">
            <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            正在讀取案件…
          </div>
        ) : filtered.length === 0 ? (
          <div className="px-4 py-16 text-center text-sm text-muted-foreground">
            {error
              ? "後端資料目前無法載入。"
              : cases.length === 0
                ? "後端目前沒有案件。"
                : "沒有符合篩選條件的案件。"}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-left text-xs text-muted-foreground [&_th]:whitespace-nowrap">
                <tr>
                  <th className="px-4 py-3">案件</th>
                  <th className="px-4 py-3">原處分機關</th>
                  <th className="px-4 py-3">承辦人</th>
                  <th className="px-4 py-3">狀態</th>
                  <th className="px-4 py-3">期限</th>
                  <th className="px-4 py-3">更新時間</th>
                  {summaryFilter === "awaitingAnalysis" ? (
                    <th className="px-4 py-3 text-right">動作</th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => (
                  <tr
                    key={item.id}
                    tabIndex={0}
                    role="button"
                    onClick={() => openCase(item)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") openCase(item);
                    }}
                    className="cursor-pointer border-t hover:bg-muted/30"
                  >
                    <td className="px-4 py-3">
                      <p className="font-medium">{item.caseNo}</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {item.appellant || "尚未解析"}
                        {item.law ? ` · ${item.law}` : ""}
                      </p>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">{item.agency || "尚未解析"}</td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-primary/10 text-[10px] font-semibold text-primary">
                          {item.assignee.charAt(0)}
                        </span>
                        {item.assignee}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className="rounded-full border bg-secondary px-2 py-1 text-xs font-medium text-primary">
                        {stageLabel[item.stage]}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">{item.deadline || "—"}</td>
                    <td className="px-4 py-3 text-muted-foreground">{item.updatedAt || "—"}</td>
                    {summaryFilter === "awaitingAnalysis" ? (
                      <td className="px-4 py-3 text-right">
                        <Button
                          size="sm"
                          disabled={starting !== null}
                          onClick={(event) => {
                            // 不要順便觸發整列的「開啟案件」
                            event.stopPropagation();
                            void startAnalysis(item);
                          }}
                        >
                          {starting === item.id ? (
                            <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                          ) : (
                            <Sparkles className="h-4 w-4" aria-hidden />
                          )}
                          執行 AI 分析
                        </Button>
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </AppShell>
  );
}
