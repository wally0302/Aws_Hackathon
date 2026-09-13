import { AlertTriangle } from "lucide-react";
import { apiEnabled } from "@/api/client";

export function ApiErrorBanner({ error }: { error?: Error | null }) {
  if (!error && apiEnabled) return null;

  return (
    <div className="mb-4 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden />
      <div>
        <p className="font-medium text-destructive">無法讀取後端資料</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {error?.message ?? "尚未設定 VITE_API_BASE，請先設定後端 API 網址。"}
        </p>
      </div>
    </div>
  );
}
