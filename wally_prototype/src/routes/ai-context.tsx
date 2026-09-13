import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { AlertCircle, CheckCircle2, LoaderCircle, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { useAdminConfig, useAdminConfigList } from "@/api/hooks";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/ai-context")({
  head: () => ({ meta: [{ title: "AI Context 管理｜訴願快速拆解平台" }] }),
  component: AiContextPage,
});

function AiContextPage() {
  const queryClient = useQueryClient();
  const list = useAdminConfigList();
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const selected = useAdminConfig(selectedName);
  const [editor, setEditor] = useState("");
  const [saving, setSaving] = useState(false);
  const [parseError, setParseError] = useState("");

  useEffect(() => {
    if (!selectedName && list.data?.configs[0]) setSelectedName(list.data.configs[0].name);
  }, [list.data, selectedName]);

  useEffect(() => {
    if (!selected.data) return;
    setEditor(JSON.stringify(selected.data.data, null, 2));
    setParseError("");
  }, [selected.data]);

  const save = async () => {
    if (!selectedName) return;
    let data: unknown;
    try {
      data = JSON.parse(editor);
      setParseError("");
    } catch (error) {
      const message = error instanceof Error ? error.message : "JSON 格式錯誤";
      setParseError(message);
      return;
    }

    setSaving(true);
    try {
      const result = await api.adminPutConfig(selectedName, data);
      await queryClient.invalidateQueries({ queryKey: ["admin", "config"] });
      toast.success(result.note || "設定已儲存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "儲存失敗");
    } finally {
      setSaving(false);
    }
  };

  return (
    <AppShell sectionLabel="AI Context 管理">
      <ApiErrorBanner error={list.error ?? selected.error} />
      <div>
        <h1 className="text-xl font-semibold">AI Context 管理</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          檢視與維護後端實際使用的規則、別名及欄位影響設定。
        </p>
      </div>

      <div className="mt-4 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        <p>各階段 Prompt 目前寫在後端程式中，尚未提供管理 API；本頁不會以任何前端內容替代。</p>
      </div>

      {list.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          正在讀取後端設定…
        </div>
      ) : list.data?.configs.length ? (
        <div className="mt-5 grid gap-4 lg:grid-cols-[280px_1fr]">
          <nav className="space-y-2" aria-label="後端設定清單">
            {list.data.configs.map((config) => (
              <button
                key={config.name}
                type="button"
                onClick={() => setSelectedName(config.name)}
                className={cn(
                  "w-full rounded-lg border bg-card p-3 text-left transition-colors",
                  selectedName === config.name
                    ? "border-primary ring-1 ring-primary"
                    : "hover:border-primary/40",
                )}
              >
                <span className="flex items-center gap-2 text-sm font-semibold">
                  {config.name}
                  {config.is_default ? (
                    <span className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      後端預設
                    </span>
                  ) : (
                    <CheckCircle2 className="ml-auto h-4 w-4 text-success" aria-label="已自訂" />
                  )}
                </span>
                {config.description ? (
                  <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                    {config.description}
                  </span>
                ) : null}
                <span className="mt-2 block text-[11px] text-muted-foreground">
                  {config.items ?? 0} 項 · {config.s3_key}
                </span>
              </button>
            ))}
          </nav>

          <section className="rounded-lg border bg-card shadow-sm">
            <header className="flex flex-col gap-2 border-b bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="text-sm font-semibold">{selected.data?.name ?? selectedName}</h2>
                {selected.data?.description ? (
                  <p className="mt-1 text-xs text-muted-foreground">{selected.data.description}</p>
                ) : null}
              </div>
              <Button
                size="sm"
                disabled={saving || selected.isLoading || !selected.data}
                onClick={save}
              >
                {saving ? (
                  <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                ) : (
                  <Save className="h-4 w-4" aria-hidden />
                )}
                儲存至後端
              </Button>
            </header>
            {selected.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
                <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                正在讀取設定內容…
              </div>
            ) : selected.data ? (
              <div className="p-4">
                <Textarea
                  value={editor}
                  onChange={(event) => setEditor(event.target.value)}
                  spellCheck={false}
                  className="min-h-[520px] font-mono text-xs leading-6"
                  aria-label="後端設定 JSON"
                />
                {parseError ? <p className="mt-2 text-xs text-destructive">{parseError}</p> : null}
                <p className="mt-2 text-xs text-muted-foreground">
                  儲存時會由後端驗證格式；驗證失敗不會覆寫原設定。
                </p>
              </div>
            ) : null}
          </section>
        </div>
      ) : !list.error ? (
        <div className="mt-5 rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
          後端目前沒有可管理的設定。
        </div>
      ) : null}
    </AppShell>
  );
}
