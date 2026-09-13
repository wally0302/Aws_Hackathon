import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { FileText, Loader2, Paperclip, Upload, X } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { AppShell } from "@/components/AppShell";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, apiEnabled, ApiError } from "@/api/client";
import { useWorkflow } from "@/state/workflow";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/new-case")({
  head: () => ({
    meta: [
      { title: "建立訴願案件｜訴願快速拆解平台" },
      {
        name: "description",
        content:
          "輸入案號、上傳訴願書與答辯書，可另附證明文件；建立後案件列於「待分析」，待承辦人啟動 AI 分析。",
      },
    ],
  }),
  component: NewCasePage,
});

/** 後端 `create_case` 回的其中一個上傳位置。 */
type UploadSlot = {
  role: "petition" | "defense" | "attachment";
  label: string;
  filename?: string;
  s3_key: string;
  upload_url: string;
  content_type?: string;
  parseable?: boolean;
};

/**
 * ⚠️ **證明文件接受 PDF 與圖片，但圖片目前不做 OCR。**
 * 後端會保存圖片並列在清單裡，但 `text` 是 null，不會進入階段 3 的檢索。
 * 這裡先講清楚，免得承辦人以為傳了就會被讀。
 */
const ATTACHMENT_ACCEPT = ".pdf,.jpg,.jpeg,.png";

/** 案號格式跟後端 `create_case` 的正規表達式一致。 */
const CASE_ID_RE = /^[0-9A-Za-z-]{4,40}$/;

function NewCasePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setActiveCase } = useWorkflow();

  const [caseId, setCaseId] = useState("");
  const [note, setNote] = useState("");
  const [petition, setPetition] = useState<File | null>(null);
  const [defense, setDefense] = useState<File | null>(null);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");

  const attachInput = useRef<HTMLInputElement>(null);

  const idError = caseId && !CASE_ID_RE.test(caseId) ? "只能是數字、英文、連字號，4–40 字" : "";
  const ready = Boolean(caseId && !idError && petition && defense) && !busy;

  const addAttachments = (files: FileList | null) => {
    if (!files?.length) return;
    const picked = Array.from(files);
    setAttachments((prev) => {
      // ⚠️ 檔名重複會在 S3 上互相覆蓋，後端也會擋（400）。這裡先擋掉比較好懂。
      const taken = new Set([...prev.map((f) => f.name), petition?.name, defense?.name]);
      const kept: File[] = [];
      for (const f of picked) {
        if (taken.has(f.name)) {
          toast.error(`檔名重複，已略過：${f.name}`);
          continue;
        }
        taken.add(f.name);
        kept.push(f);
      }
      return [...prev, ...kept];
    });
    if (attachInput.current) attachInput.current.value = "";
  };

  /**
   * 建案 → 逐份 PUT 到 presigned URL → 回「我的案件」的「待分析」。
   *
   * ⚠️ **這裡不跑階段 1。** 上傳完的案件停在「待分析」，要承辦人在列表按
   * 「執行 AI 分析」才會動——不要讓 AI 在人還沒看過卷之前就先跑。
   *
   * ⚠️ **PUT 一定要帶後端給的 `content_type`。** presigned URL 的簽章把
   * content-type 算進去了，自己猜一個會吃 403 SignatureDoesNotMatch。
   */
  const submit = async () => {
    if (!ready || !petition || !defense) return;
    setBusy(true);
    try {
      setStep("建立案件…");
      const created = (await api.createCase({
        case_id: caseId,
        petition_filename: petition.name,
        defense_filename: defense.name,
        ...(attachments.length
          ? { attachments: attachments.map((f) => ({ filename: f.name })) }
          : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      })) as { uploads?: UploadSlot[] };

      const slots = created.uploads ?? [];
      const fileFor = (slot: UploadSlot): File | undefined => {
        if (slot.role === "petition") return petition;
        if (slot.role === "defense") return defense;
        return attachments.find((f) => f.name === slot.filename);
      };

      let done = 0;
      for (const slot of slots) {
        const file = fileFor(slot);
        if (!file) continue;
        setStep(`上傳 ${slot.label}（${++done}/${slots.length}）…`);
        const res = await fetch(slot.upload_url, {
          method: "PUT",
          headers: { "Content-Type": slot.content_type ?? "application/pdf" },
          body: file,
        });
        if (!res.ok) {
          throw new Error(`${slot.label} 上傳失敗（HTTP ${res.status}）`);
        }
      }

      await queryClient.invalidateQueries({ queryKey: ["cases"] });

      setActiveCase(caseId);
      toast.success(`案件 ${caseId} 已建立，請在「待分析」按「執行 AI 分析」`);
      navigate({ to: "/", search: { tab: "awaitingAnalysis" } });
    } catch (e) {
      const msg =
        e instanceof ApiError ? `${e.message}` : e instanceof Error ? e.message : "未知錯誤";
      toast.error(msg);
      setStep("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppShell sectionLabel="建立訴願案件">
      <ApiErrorBanner
        error={apiEnabled ? null : new Error("尚未設定 VITE_API_BASE，無法建立案件。")}
      />

      <div>
        <h1 className="text-xl font-semibold">建立訴願案件</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          訴願書與答辯書缺一不可；證明文件可傳 0 至多份。建立後案件會列在「待分析」，按「執行 AI
          分析」才會開始程序檢核。
        </p>
      </div>

      {!apiEnabled ? (
        <p className="mt-4 rounded-lg border border-warning/50 bg-warning/10 p-3 text-sm">
          未設定 VITE_API_BASE，目前無法建立真實案件。
        </p>
      ) : null}

      <section className="mt-5 max-w-3xl space-y-5 rounded-lg border bg-card p-5 shadow-sm">
        <div>
          <Label htmlFor="case-id">案號</Label>
          <Input
            id="case-id"
            value={caseId}
            onChange={(e) => setCaseId(e.target.value.trim())}
            placeholder="例如 1146030004"
            className={cn("mt-1.5", idError && "border-destructive")}
            disabled={busy}
          />
          <p className={cn("mt-1 text-xs", idError ? "text-destructive" : "text-muted-foreground")}>
            {idError ||
              "案號來自收受分案通知。系統不自動編號——實測 101 份決定書，案號年份 ≤ 發文年份，有 11 件跨年。"}
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <FilePicker
            label="訴願書"
            required
            accept=".pdf"
            file={petition}
            onPick={setPetition}
            disabled={busy}
            hint="只接受 PDF"
          />
          <FilePicker
            label="答辯書"
            required
            accept=".pdf"
            file={defense}
            onPick={setDefense}
            disabled={busy}
            hint="判案件法規類型、抽機關方主張都要用它"
          />
        </div>

        <div>
          <div className="flex items-center justify-between">
            <Label>證明文件（選用）</Label>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => attachInput.current?.click()}
            >
              <Paperclip className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              加入檔案
            </Button>
          </div>
          <input
            ref={attachInput}
            type="file"
            multiple
            accept={ATTACHMENT_ACCEPT}
            className="hidden"
            onChange={(e) => addAttachments(e.target.files)}
          />

          {attachments.length === 0 ? (
            <p className="mt-2 rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground">
              可加入送達證書、原處分書等佐證資料。PDF 的內容會用於檢索相關法條。
            </p>
          ) : (
            <ul className="mt-2 space-y-1.5">
              {attachments.map((f) => {
                const isPdf = f.name.toLowerCase().endsWith(".pdf");
                return (
                  <li
                    key={f.name}
                    className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2 text-sm"
                  >
                    <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{f.name}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {(f.size / 1024).toFixed(0)} KB
                    </span>
                    {!isPdf ? (
                      <span
                        className="shrink-0 rounded bg-warning/20 px-1.5 py-0.5 text-xs text-amber-900"
                        title="圖片目前不做 OCR，內容不會進入檢索"
                      >
                        不解析
                      </span>
                    ) : null}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        setAttachments((prev) => prev.filter((x) => x.name !== f.name))
                      }
                      className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-destructive"
                      aria-label={`移除 ${f.name}`}
                    >
                      <X className="h-3.5 w-3.5" aria-hidden />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          {attachments.some((f) => !f.name.toLowerCase().endsWith(".pdf")) ? (
            <p className="mt-1.5 text-xs text-amber-900">
              ⚠️ 圖片會保存並列在案件卷證裡，但目前不做 OCR，內容不會進入檢索。
            </p>
          ) : null}
        </div>

        <div>
          <Label htmlFor="note">備註（選用）</Label>
          <Textarea
            id="note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="例如：本件與 1146030001 為同一處分之分別提起"
            className="mt-1.5 min-h-16"
            disabled={busy}
          />
        </div>

        <div className="flex items-center justify-end gap-3 border-t pt-4">
          {busy ? (
            <span className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {step}
            </span>
          ) : null}
          <Button disabled={!ready} onClick={submit}>
            <Upload className="mr-1.5 h-4 w-4" aria-hidden />
            建立案件
          </Button>
        </div>
      </section>
    </AppShell>
  );
}

function FilePicker({
  label,
  required,
  accept,
  file,
  onPick,
  disabled,
  hint,
}: {
  label: string;
  required?: boolean;
  accept: string;
  file: File | null;
  onPick: (f: File | null) => void;
  disabled?: boolean;
  hint?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <div>
      <Label>
        {label}
        {required ? <span className="ml-1 text-destructive">*</span> : null}
      </Label>
      <input
        ref={ref}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
      />
      <button
        type="button"
        disabled={disabled}
        onClick={() => ref.current?.click()}
        className={cn(
          "mt-1.5 flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors",
          file ? "border-primary/40 bg-secondary/60" : "border-dashed hover:border-primary/50",
        )}
      >
        <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className={cn("min-w-0 flex-1 truncate", !file && "text-muted-foreground")}>
          {file ? file.name : "選擇 PDF 檔案"}
        </span>
        {file ? (
          <span className="shrink-0 text-xs text-muted-foreground">
            {(file.size / 1024).toFixed(0)} KB
          </span>
        ) : null}
      </button>
      {hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
