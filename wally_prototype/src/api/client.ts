/**
 * 打後端 API。**只有這個檔案知道 URL 長什麼樣子。**
 *
 * 設定：`.env.local` 放
 *     VITE_API_BASE=https://xxxx.execute-api.us-east-1.amazonaws.com
 *
 * 沒設 `VITE_API_BASE` 時，畫面會顯示設定錯誤，不會使用替代資料。
 */

import type {
  DecisionPdfResponse,
  CaseListResponse,
  AdminConfigDetail,
  AdminConfigList,
  AdminConfigSaveResult,
  CorpusReport,
  Disposition,
  SourceUrlResponse,
  StageEnvelope,
} from "./types";

/**
 * 清掉貼上時常見的雜訊，再驗證是不是合法網址。
 *
 * ⚠️ **最常見的是結尾多一個 `%`。** 在 macOS 的 zsh 裡，輸出沒有換行結尾時
 * 會顯示一個反白的 `%`——那是提示符號不是字元，但從終端機複製時會被帶進去：
 *
 *     Failed to parse URL from https://xxx.execute-api...amazonaws.com%/cases
 *
 * 沒有這層清理的話，錯誤訊息只說「Failed to parse URL」，看不出是多了一個字元
 * （2026-09-12 實際踩過）。
 *
 * 也一併處理：前後空白、被引號包住、結尾斜線。
 */
function normalizeBase(raw: string): string {
  const cleaned = raw
    .trim()
    .replace(/^["']|["']$/g, "") // .env 裡有人會加引號
    .replace(/[%\s]+$/, "") // ★ zsh 的 % 提示符、尾端空白
    .replace(/\/+$/, ""); // 結尾斜線

  if (!cleaned) return "";

  try {
    const u = new URL(cleaned);
    if (u.protocol !== "https:" && u.protocol !== "http:") {
      throw new Error(`VITE_API_BASE 的協定不對（${u.protocol}），應該是 https://`);
    }
  } catch {
    // ⚠️ 這裡直接丟出去。設錯的話整個前端都不能用，**早點大聲失敗**
    //    比讓每一次 fetch 各自吐難懂的錯誤好。
    throw new Error(
      `VITE_API_BASE 不是合法網址：「${raw}」。` +
        "常見原因：從終端機複製時把 zsh 的 % 提示符一起帶進來了。" +
        "應該長得像 https://xxxx.execute-api.us-east-1.amazonaws.com（結尾不要有任何符號）",
    );
  }
  return cleaned;
}

const BASE = normalizeBase(import.meta.env["VITE_API_BASE"] ?? "");

/** 是否已設定後端。 */
export const apiEnabled = BASE.length > 0;

export const apiBase = BASE;

/** 後端回 4xx/5xx 時丟這個，帶著後端寫的 hint。 */
export class ApiError extends Error {
  readonly status: number;
  readonly hint?: string;

  constructor(status: number, message: string, hint?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    // exactOptionalPropertyTypes：只有真的有值才設，不要塞 undefined
    if (hint !== undefined) this.hint = hint;
  }
}

type Query = Record<string, string | number | boolean | undefined>;

function url(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined) qs.set(k, String(v));
  }
  const suffix = qs.toString();
  return `${BASE}${path}${suffix ? `?${suffix}` : ""}`;
}

async function request<T>(path: string, init?: RequestInit & { query?: Query }): Promise<T> {
  if (!apiEnabled) {
    throw new ApiError(0, "沒有設定 VITE_API_BASE", "請在 .env.local 設定後端網址");
  }

  const { query, ...rest } = init ?? {};
  const res = await fetch(url(path, query), {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(rest.headers ?? {}),
    },
  });

  // 後端一律回 JSON（連錯誤也是），但 502/504 那種是 API Gateway 自己回的，
  // 可能不是 JSON——所以要防。
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(res.status, `後端回的不是 JSON（${res.status}）`, text.slice(0, 200));
  }

  if (!res.ok) {
    const b = (body ?? {}) as { error?: string; hint?: string };
    throw new ApiError(res.status, b.error ?? `HTTP ${res.status}`, b.hint);
  }
  return body as T;
}

/* ───────────────────────────── 端點 ───────────────────────────── */

export const api = {
  selftest: () => request<Record<string, unknown>>("/selftest"),

  listCases: () => request<CaseListResponse>("/cases"),

  getCase: (caseId: string) =>
    request<Record<string, unknown>>(`/cases/${encodeURIComponent(caseId)}`),

  getStage: <D>(caseId: string, stage: number, version?: number) =>
    request<StageEnvelope<D>>(
      `/cases/${encodeURIComponent(caseId)}/stages/${stage}`,
      version === undefined ? undefined : { query: { version } },
    ),

  /**
   * 跑某階段。**回 202 就結束，不等結果。**
   *
   * ⚠️ API Gateway 的整合逾時是 30 秒硬上限，而階段 3 要 60–90 秒
   * ——所以後端是非同步的：這裡回 202，之後要用 `pollStage` 輪詢。
   */
  runStage: (caseId: string, stage: number) =>
    request<StageEnvelope<unknown>>(`/cases/${encodeURIComponent(caseId)}/stages/${stage}/run`, {
      method: "POST",
      body: "{}",
    }),

  /**
   * 確認某階段。
   *
   * ⚠️ **`disposition` 只有階段 1 能帶**，其他階段帶了後端會回 409。
   * （以前是靜靜忽略，害人以為設了什麼。）
   */
  confirmStage: (
    caseId: string,
    stage: number,
    opts?: { disposition?: Disposition; actor?: string },
  ) => {
    const body: Record<string, string> = {};
    if (opts?.actor !== undefined) body["actor"] = opts.actor;
    if (opts?.disposition !== undefined) {
      body["disposition"] = opts.disposition;
    }
    return request<Record<string, unknown>>(
      `/cases/${encodeURIComponent(caseId)}/stages/${stage}/confirm`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  patchFields: (caseId: string, stage: number, fields: Record<string, unknown>, actor?: string) =>
    request<Record<string, unknown>>(
      `/cases/${encodeURIComponent(caseId)}/stages/${stage}/fields`,
      {
        method: "PATCH",
        body: JSON.stringify(actor === undefined ? { fields } : { fields, actor }),
      },
    ),

  createCase: (input: {
    case_id: string;
    petition_filename?: string;
    defense_filename?: string;
    attachments?: { filename: string }[];
    note?: string;
    actor?: string;
  }) =>
    request<Record<string, unknown>>("/cases", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  /**
   * 建庫現況（各索引的筆數與分佈）。
   *
   * ⚠️ 走的是 `/admin/{proxy+}` 那條路由（接 appeal-admin），
   * 跟其他端點不是同一個 Lambda。
   */
  adminCorpus: () => request<CorpusReport>("/admin/corpus"),

  adminSelftest: () => request<Record<string, unknown>>("/admin/selftest"),

  adminWarmup: () =>
    request<Record<string, unknown>>("/admin/warmup", { method: "POST", body: "{}" }),

  adminConfig: () => request<AdminConfigList>("/admin/config"),

  adminGetConfig: (name: string) =>
    request<AdminConfigDetail>(`/admin/config/${encodeURIComponent(name)}`),

  adminPutConfig: (name: string, data: unknown, actor?: string) =>
    request<AdminConfigSaveResult>(`/admin/config/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(actor ? { data, actor } : { data }),
    }),

  audit: (caseId: string) =>
    request<Record<string, unknown>>(`/cases/${encodeURIComponent(caseId)}/audit`),

  /**
   * 原始 PDF 的 presigned URL（15 分鐘有效）。
   *
   * `kind` 用階段 3 `hits[].doc_type`（法規／判解／函釋），
   * `file` 用 `hits[].file`。
   *
   * ⚠️ `file` 欄位是後端 2026-09-10 才加的，**在那之前建的索引沒有**
   * ——那種情況後端會回 404 並在 hint 裡說明。
   */
  sourceUrl: (kind: string, file: string) =>
    request<SourceUrlResponse>("/sources", { query: { kind, file } }),

  /**
   * 拿決定書 PDF 的下載連結（presigned URL）。
   *
   * 後端用階段 4 目前版本的 `decision` 排成公文版 PDF 放到 S3，回連結；
   * 前端拿到 `url` 直接用 `<a download>` 開。
   *
   * ⚠️ **後端尚未實作這支**——實作前會回 404/403。
   * 階段 4 還沒 done 時後端應回 409（hint：「先完成階段四」）。
   */
  decisionPdf: (caseId: string) =>
    request<DecisionPdfResponse>(`/cases/${encodeURIComponent(caseId)}/decision.pdf`),
};

/* ─────────────────────────── 輪詢 ─────────────────────────── */

/** 各階段的預估耗時（毫秒），用來決定輪詢要等多久才放棄。實測值。 */
export const STAGE_TIMEOUT_MS: Record<number, number> = {
  1: 90_000, // 兩份 PDF + 抽欄位 + 判法規類型
  2: 120_000, // 三次 AI 呼叫
  3: 180_000, // 每條主張 3 次呼叫，還有 embed 配額限制
  4: 120_000, // 生成草稿（不受理路線只要 1 秒）
  5: 30_000, // 純程式
};

export type PollOptions = {
  intervalMs?: number;
  timeoutMs?: number;
  /** 每次輪到就呼叫一次，前端可以顯示「已等 N 秒」 */
  onTick?: (elapsedMs: number, status: string) => void;
  signal?: AbortSignal;
};

/**
 * 輪詢到階段跑完。
 *
 * ⚠️ **`failed` 也要回傳，不要丟例外。** 失敗的階段有 `summary` 寫著
 * 原因，那是承辦人最需要看到的東西——丟例外會讓那段訊息消失。
 */
export async function pollStage<D>(
  caseId: string,
  stage: number,
  opts: PollOptions = {},
): Promise<StageEnvelope<D>> {
  const interval = opts.intervalMs ?? 3_000;
  const timeout = opts.timeoutMs ?? STAGE_TIMEOUT_MS[stage] ?? 120_000;
  const started = Date.now();

  for (;;) {
    if (opts.signal?.aborted) throw new ApiError(0, "已取消");

    const env = await api.getStage<D>(caseId, stage);
    const elapsed = Date.now() - started;
    opts.onTick?.(elapsed, env.status);

    // running 以外都算「有結果了」——包含 failed
    if (env.status !== "running") return env;

    if (elapsed > timeout) {
      throw new ApiError(
        408,
        `階段 ${stage} 等了 ${Math.round(elapsed / 1000)} 秒還在跑`,
        "去 CloudWatch Logs 看 /aws/lambda/appeal-worker",
      );
    }
    await new Promise((r) => setTimeout(r, interval));
  }
}

/** 跑一個階段並等它完成。前端大部分情況用這個就好。 */
export async function runAndWait<D>(
  caseId: string,
  stage: number,
  opts?: PollOptions,
): Promise<StageEnvelope<D>> {
  await api.runStage(caseId, stage);
  return pollStage<D>(caseId, stage, opts);
}
