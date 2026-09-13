import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Activity, Database, Flame, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { useAdminSelftest, useCorpus } from "@/api/hooks";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/preprocessing")({
  head: () => ({ meta: [{ title: "知識庫前置作業｜訴願快速拆解平台" }] }),
  component: PreprocessingPage,
});

function PreprocessingPage() {
  const queryClient = useQueryClient();
  const corpus = useCorpus();
  const selftest = useAdminSelftest();
  const [warming, setWarming] = useState(false);
  const [warmupResult, setWarmupResult] = useState<Record<string, unknown> | null>(null);

  const warmup = async () => {
    setWarming(true);
    try {
      const result = await api.adminWarmup();
      setWarmupResult(result);
      await queryClient.invalidateQueries({ queryKey: ["admin", "selftest"] });
      await queryClient.invalidateQueries({ queryKey: ["admin", "corpus"] });
      toast.success("OpenSearch 預熱請求已完成");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "預熱失敗");
    } finally {
      setWarming(false);
    }
  };

  const report = corpus.data;
  const unitEntries = Object.entries(report?.case_reasons.by_unit_type ?? {});
  const rankEntries = Object.entries(report?.law_articles.by_authority_rank ?? {});
  const typeEntries = Object.entries(report?.law_articles.by_doc_type ?? {});

  return (
    <AppShell sectionLabel="知識庫前置作業">
      <ApiErrorBanner error={corpus.error ?? selftest.error} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">知識庫前置作業</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            檢視後端索引現況、服務自我檢查結果並預熱 OpenSearch。
          </p>
        </div>
        <Button disabled={warming} onClick={warmup}>
          {warming ? (
            <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <Flame className="h-4 w-4" aria-hidden />
          )}
          預熱 OpenSearch
        </Button>
      </div>

      {corpus.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          正在讀取知識庫現況…
        </div>
      ) : report ? (
        <>
          <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="法規與權威資料切片" value={report.law_articles.total} />
            {typeEntries.map(([label, value]) => (
              <Metric key={label} label={label} value={value} />
            ))}
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <DataTable
              title="權威位階分布"
              entries={rankEntries}
              labelFor={(key) =>
                ({
                  "1": "位階 1 · 法規",
                  "2": "位階 2 · 司法院釋字",
                  "3": "位階 3 · 判解",
                  "4": "位階 4 · 函釋",
                })[key] ?? `位階 ${key}`
              }
            />
            <DataTable title="歷史案件索引單元" entries={unitEntries} labelFor={(key) => key} />
          </div>

          {report.note ? (
            <p className="mt-4 rounded-lg border bg-muted/30 p-4 text-sm leading-6 text-muted-foreground">
              {report.note}
            </p>
          ) : null}
        </>
      ) : !corpus.error ? (
        <div className="mt-5 rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
          後端尚未回傳知識庫報告。
        </div>
      ) : null}

      <section className="mt-5 rounded-lg border bg-card shadow-sm">
        <header className="flex items-center gap-2 border-b bg-muted/50 px-4 py-3 text-sm font-semibold">
          <Activity className="h-4 w-4 text-primary" aria-hidden />
          管理服務自我檢查
        </header>
        <div className="p-4">
          {selftest.isLoading ? (
            <p className="text-sm text-muted-foreground">正在執行後端自我檢查…</p>
          ) : selftest.data ? (
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/20 p-4 font-mono text-xs leading-6">
              {JSON.stringify(selftest.data, null, 2)}
            </pre>
          ) : !selftest.error ? (
            <p className="text-sm text-muted-foreground">後端未回傳自我檢查結果。</p>
          ) : null}
          {warmupResult ? (
            <div className="mt-4">
              <p className="mb-2 text-xs font-semibold">最近一次預熱回應</p>
              <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/20 p-4 font-mono text-xs leading-6">
                {JSON.stringify(warmupResult, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      </section>

      <section className="mt-5 flex items-start gap-3 rounded-lg border border-dashed p-4 text-sm">
        <Database className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
        <div>
          <h2 className="font-semibold">建庫作業由後端執行</h2>
          <p className="mt-1 leading-6 text-muted-foreground">
            完整建庫、PDF 解析與向量化會超過 HTTP API 的執行時間，目前由 ingest Lambda
            直接執行；本頁不模擬進度或結果。
          </p>
        </div>
      </section>
    </AppShell>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-2 text-2xl font-semibold">{value.toLocaleString()}</p>
    </div>
  );
}

function DataTable({
  title,
  entries,
  labelFor,
}: {
  title: string;
  entries: [string, number][];
  labelFor: (key: string) => string;
}) {
  return (
    <section className="rounded-lg border bg-card shadow-sm">
      <header className="border-b bg-muted/50 px-4 py-3 text-sm font-semibold">{title}</header>
      {entries.length ? (
        <dl className="divide-y">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-center justify-between px-4 py-3 text-sm">
              <dt>{labelFor(key)}</dt>
              <dd className="font-semibold">{value.toLocaleString()}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="px-4 py-10 text-center text-sm text-muted-foreground">後端未回傳分布資料。</p>
      )}
    </section>
  );
}
