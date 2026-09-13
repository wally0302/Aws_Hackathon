import { useQuery } from "@tanstack/react-query";
import { Download, ExternalLink, LoaderCircle, RotateCw } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export type SourcePreviewDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** hits[].doc_type → /sources?kind= */
  kind: string;
  /** hits[].file → /sources?file= */
  file: string;
  /** 彈窗標題（Radix 無障礙要求一定要有 DialogTitle） */
  title: string;
};

/**
 * 在同頁彈窗內用瀏覽器內建 PDF viewer 顯示原文。
 * 打開時才向 /sources 換 presigned URL（15 分鐘有效），關閉即丟棄快取，
 * 重開一定重抓，避免拿到過期連結。
 */
export function SourcePreviewDialog({
  open,
  onOpenChange,
  kind,
  file,
  title,
}: SourcePreviewDialogProps) {
  const query = useQuery({
    queryKey: ["source", kind, file],
    queryFn: () => api.sourceUrl(kind, file),
    enabled: open,
    staleTime: 0,
    gcTime: 0,
    // 舊索引沒有 file 時後端回 404，是確定性錯誤，不要退避重試拖延錯誤顯示
    retry: false,
    // 切分頁回來不要換新 URL，否則 iframe 會重載、捲動位置遺失
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const url = query.data?.url;
  const error = query.error;
  const hint = error instanceof ApiError ? error.hint : undefined;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[92dvh] w-[96vw] max-w-none flex-col gap-0 overflow-hidden p-0">
        {/* pr-12 讓標題避開 DialogContent 內建的右上角關閉鈕 */}
        <DialogHeader className="border-b px-4 py-3 pr-12 text-left">
          <DialogTitle className="text-base">{title}</DialogTitle>
          <DialogDescription className="truncate text-xs">
            {kind}・{file}
          </DialogDescription>
        </DialogHeader>

        <div className="relative min-h-0 flex-1 bg-muted">
          {query.isPending ? (
            <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
              正在取得原文連結…
            </div>
          ) : error ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
              <p className="text-sm font-medium text-destructive">
                {error instanceof Error ? error.message : "來源連結取得失敗"}
              </p>
              {hint ? <p className="max-w-md text-xs text-muted-foreground">{hint}</p> : null}
              <Button size="sm" variant="outline" onClick={() => void query.refetch()}>
                <RotateCw className="h-3.5 w-3.5" aria-hidden />
                重試
              </Button>
            </div>
          ) : url ? (
            <>
              {/* 常駐在 iframe 後方的備援提示：PDF viewer 一渲染就會蓋住它；
                  若 presigned URL 是 attachment 或瀏覽器不支援內嵌，這行會一直露出，
                  引導使用者改用下方按鈕。跨域 iframe 的 onLoad/onError 不可靠，所以不用事件偵測。 */}
              <p className="absolute inset-0 flex items-center justify-center px-6 text-center text-sm text-muted-foreground">
                PDF 載入中；若長時間空白，請改用下方「在新分頁開啟」。
              </p>
              {/* 不要加 sandbox：Chrome 內建 PDF viewer 在 sandboxed iframe 內不會渲染 */}
              <iframe src={url} title={title} className="absolute inset-0 h-full w-full border-0" />
            </>
          ) : null}
        </div>

        {url ? (
          <DialogFooter className="items-center gap-2 border-t px-4 py-2 sm:justify-between">
            <span className="text-xs text-muted-foreground">無法顯示時：</span>
            <div className="flex gap-2">
              <Button asChild size="sm" variant="outline">
                <a href={url} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                  在新分頁開啟
                </a>
              </Button>
              <Button asChild size="sm" variant="outline">
                <a href={url} download={file}>
                  <Download className="h-3.5 w-3.5" aria-hidden />
                  下載
                </a>
              </Button>
            </div>
          </DialogFooter>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
