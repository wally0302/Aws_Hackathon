import { useQuery } from "@tanstack/react-query";
import { toCaseFile } from "./adapters";
import { api, apiEnabled } from "./client";
import type { AdminConfigDetail, AdminConfigList, CorpusReport, StageEnvelope } from "./types";
import type { CaseFile } from "@/domain/case";

export type Loaded<T> = {
  data: T;
  isLoading: boolean;
  error: Error | null;
};

function configError(): Error | null {
  return apiEnabled ? null : new Error("尚未設定 VITE_API_BASE，無法連線至後端 API。");
}

export function useCases(): Loaded<CaseFile[]> {
  const query = useQuery({
    queryKey: ["cases"],
    queryFn: () => api.listCases(),
    enabled: apiEnabled,
    staleTime: 30_000,
  });

  return {
    data: query.data?.cases.map(toCaseFile) ?? [],
    isLoading: apiEnabled && query.isLoading,
    error: (query.error as Error | null) ?? configError(),
  };
}

export function useCase(caseId: string | null | undefined): Loaded<CaseFile | null> {
  const list = useCases();
  return {
    ...list,
    data: caseId ? (list.data.find((item) => item.id === caseId) ?? null) : null,
  };
}

export function useStage<D>(
  caseId: string | null | undefined,
  stage: number,
  opts?: {
    /**
     * 不吃 cache：進頁面、切回視窗都重抓，而且每 10 秒再抓一次。
     * 給「顯示別的階段的結果」用（例如步驟五顯示步驟四的草稿）——
     * 那個階段可能在別的分頁被重跑，這頁要跟著更新。
     */
    alwaysFresh?: boolean;
  },
): Loaded<StageEnvelope<D> | null> {
  const fresh = opts?.alwaysFresh ?? false;
  const query = useQuery({
    queryKey: ["stage", caseId, stage],
    queryFn: () => api.getStage<D>(caseId!, stage),
    enabled: apiEnabled && Boolean(caseId),
    staleTime: 0,
    refetchOnMount: fresh ? "always" : true,
    refetchOnWindowFocus: fresh ? "always" : true,
    // ★ 階段在跑就自己每 3 秒抓一次。輪詢綁在 query 上而不是頁面元件上，
    //   承辦人切去別頁再回來，cache 裡就是 running、輪詢也不會斷。
    refetchInterval: (q) =>
      (q.state.data as StageEnvelope<D> | undefined)?.status === "running"
        ? 3_000
        : fresh
          ? 10_000
          : false,
  });

  return {
    data: (query.data as StageEnvelope<D> | undefined) ?? null,
    isLoading: apiEnabled && Boolean(caseId) && query.isLoading,
    error: (query.error as Error | null) ?? configError(),
  };
}

export function useCorpus(): Loaded<CorpusReport | null> {
  const query = useQuery({
    queryKey: ["admin", "corpus"],
    queryFn: () => api.adminCorpus(),
    enabled: apiEnabled,
    staleTime: 5 * 60_000,
  });

  return {
    data: query.data ?? null,
    isLoading: apiEnabled && query.isLoading,
    error: (query.error as Error | null) ?? configError(),
  };
}

export function useAdminConfigList(): Loaded<AdminConfigList | null> {
  const query = useQuery({
    queryKey: ["admin", "config"],
    queryFn: () => api.adminConfig(),
    enabled: apiEnabled,
  });

  return {
    data: query.data ?? null,
    isLoading: apiEnabled && query.isLoading,
    error: (query.error as Error | null) ?? configError(),
  };
}

export function useAdminConfig(name: string | null): Loaded<AdminConfigDetail | null> {
  const query = useQuery({
    queryKey: ["admin", "config", name],
    queryFn: () => api.adminGetConfig(name!),
    enabled: apiEnabled && Boolean(name),
  });

  return {
    data: query.data ?? null,
    isLoading: apiEnabled && Boolean(name) && query.isLoading,
    error: (query.error as Error | null) ?? configError(),
  };
}

export function useAdminSelftest(): Loaded<Record<string, unknown> | null> {
  const query = useQuery({
    queryKey: ["admin", "selftest"],
    queryFn: () => api.adminSelftest(),
    enabled: apiEnabled,
    staleTime: 30_000,
  });

  return {
    data: query.data ?? null,
    isLoading: apiEnabled && query.isLoading,
    error: (query.error as Error | null) ?? configError(),
  };
}
