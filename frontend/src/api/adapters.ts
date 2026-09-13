/** 後端 API 形狀轉為前端顯示模型。 */

import type {
  AiAssessment,
  CaseFile,
  CheckItem,
  FactItem,
  CaseRisk,
  CaseStage,
  Issue as UiIssue,
  Quadrant,
  RefItem,
  WorkStatus,
} from "@/domain/case";
import type {
  CaseRow,
  CheckStatus,
  Claim,
  DecisionDoc,
  Draft,
  DraftSection,
  DispositionOption,
  Issue as ApiIssue,
  PerClaimRetrieval,
  RetrievalHit,
  Stage1Detail,
  Stage2Detail,
  Stage3Detail,
  Stage4Detail,
  Verdict,
} from "./types";
import { DEFAULT_ASSIGNEE } from "@/state/auth";

/* ═══════════════════════ 案件列表 ═══════════════════════ */

/**
 * 後端 `case_status` → 前端 `WorkStatus`。
 *
 * ⚠️ **兩邊的值域不一樣**，只能對映。後端的值是
 * 「檢查規格_待確認」這種（`envelope.py` 的 `ALL_STATES` 是唯一真實來源）。
 */
export function toWorkStatus(caseStatus: string | null | undefined): WorkStatus {
  const s = caseStatus ?? "";
  if (s === "已入庫") return "已完成";
  if (s === "待補正") return "待補件";
  if (s === "不受理" || s === "已撤銷") return "草稿中";
  if (s.startsWith("決定書草稿")) return "草稿中";
  if (s.endsWith("_待確認")) return "待確認";
  if (s.endsWith("_已確認")) return "審理中";
  return "待確認"; // 待分析
}

/**
 * 到期日換算成優先度。
 * 逾期最急，其次是剩不到 7 天。
 */
export function toPriority(s: CaseRow["summary"]): CaseFile["priority"] {
  if (s?.is_overdue) return "critical";
  const left = s?.remaining_days;
  if (left === null || left === undefined) return "normal";
  if (left <= 3) return "critical";
  if (left <= 7) return "high";
  if (left <= 14) return "medium";
  return "normal";
}

/**
 * AI 的評估分類。
 * ⚠️ 這是**前端的視覺化概念**，後端沒有這個欄位——由 verdict 與
 * 系統建議的處置推出來。
 */
export function toAiAssessment(row: CaseRow): AiAssessment {
  if (row.summary?.suggest_disposition === "inadmissible") {
    return "admissibility-risk";
  }
  const v = row.stages["4"]?.verdict ?? row.stages["1"]?.verdict;
  if (v === "need_human") return "insufficient";
  if (v === "warning") return "decision-risk";
  return "clear";
}

/**
 * AI 與人的同意度矩陣。
 * ⚠️ 同樣是**前端概念**。承辦人還沒選處置時，只能顯示 AI 那一側。
 */
export function toQuadrant(row: CaseRow): Quadrant {
  const risky =
    row.summary?.is_overdue === true || row.summary?.suggest_disposition === "inadmissible";
  const decided = row.disposition;

  if (decided === null) return risky ? "ai-fail" : "ai-pass";
  const humanSaysFail = decided === "inadmissible" || decided === "withdraw";
  return humanSaysFail ? "human-fail" : "human-pass";
}

/** 剩餘天數的顯示字串。逾期用負數表達會看不懂，直接寫「逾期 N 日」。 */
export function deadlineLabel(s: CaseRow["summary"]): string {
  if (!s) return "尚未解析";
  if (s.is_overdue && s.overdue_days !== null) {
    return `已逾期 ${s.overdue_days} 日`;
  }
  if (s.remaining_days !== null) return `剩 ${s.remaining_days} 日`;
  return "—";
}

/**
 * 案件列表的一列。
 *
 * ⚠️ **`summary` 是 null 時要顯示「尚未解析」**，不要顯示空白
 * ——那代表階段 1 還沒跑，不是資料壞了。
 */
/**
 * 後端案件狀態 → 夥伴在 dashboard 用的 `CaseStage`。
 *
 * ⚠️ 這是**兩套詞彙的對照**，不是一對一：後端的狀態machine有五個階段
 * 各自的「執行中／待確認／已確認」，前端 dashboard 只分五種視覺分組。
 * 對照原則是「承辦人現在該做什麼」，不是「跑到第幾階段」。
 */
export function toCaseStage(row: CaseRow): CaseStage {
  const st = row.stages ?? {};
  const s1 = st["1"];
  const status = row.case_status ?? "";

  // ⚠️⚠️ **`pending` 的意思是「已結案」不是「待處理」。**
  //    見 index.tsx：stageLabel.pending →「已結案」，summaryOf() 歸到 closed。
  //    判斷順序有意義，不要隨意調換。

  // ① 已結案最優先。**後端的 case_status 才是唯一可靠的依據**——
  //    階段 5 confirmed 不代表入庫成功（存檔可能失敗）。
  if (status === "已入庫") return "pending";

  // ② 還沒跑階段 1 → 待分析。**不自動跑**，等承辦人在列表按「執行 AI 分析」
  if (!s1 || s1.status === "pending") return "not-started";

  // ③ 階段 1 被擋下來（缺件、程序不合）→ 補正／補卷
  if (s1.verdict === "blocked") return "supplement";

  // ④ 階段 1 跑著／跑完／失敗，但承辦人還沒確認 → 待人工初檢
  if (s1.status !== "confirmed") return "intake";

  // ⑤ 五階段都確認完，但 case_status 還沒翻成「已入庫」——
  //    仍然算結案（避免卡在中間狀態顯示成待審理）
  if (st["5"]?.status === "confirmed") return "pending";

  // ⑥ 離開主線、等外部動作的兩種狀態。
  //
  // ⚠️⚠️ **原本漏了這一段**（2026-09-13 補）。承辦人在階段 1 按「進入待補正」
  //    之後，後端確實把 `case_status` 改成「待補正」了，但這裡沒有認它，
  //    一路掉到最後的 `return "ready"`，案件就出現在「待審理／待處理」
  //    而不是「等待外部資料」——看起來像按鈕沒有作用。
  //    （index.tsx 的「等待外部資料」篩的就是 `stage === "supplement"`。）
  if (status === "待補正") return "supplement";
  // 訴願人撤回，案件終止——沒有下一步了，歸已結案
  if (status === "已撤銷") return "pending";

  // ⑦ 有階段在跑，或正在等承辦人確認 → 審理／撰稿中
  if (Object.values(st).some((x) => x.status === "running")) return "reviewing";
  if (status.endsWith("待確認")) return "reviewing";

  // ⑧ 階段 1 確認了、還沒進入實體審查 → 待實體審理
  //    ⚠️ **fallback 不能是 pending**，那會讓分類不到的案子顯示成「已結案」。
  return "ready";
}

/**
 * 案件上的風險標籤。**一件案子可以同時有好幾個。**
 *
 * ⚠️ 只放**看得出來的**風險，不要猜。`summary` 是 null（階段 1 還沒跑）
 * 時回空陣列，而不是編一個「資料不足」的風險——那會讓剛建好的案子
 * 每一件都亮紅燈。
 */
export function toRisks(row: CaseRow): CaseRisk[] {
  const out: CaseRisk[] = [];
  const s = row.summary;

  // 逾期或快到期。remaining_days 是純程式算的（roc_date）
  if (s?.is_overdue) out.push("due-soon");
  else if (typeof s?.remaining_days === "number" && s.remaining_days <= 3) {
    out.push("due-soon");
  }

  // 階段 1 擋下來 = 有缺件或程序不合
  if (row.stages?.["1"]?.verdict === "blocked") out.push("blocking-missing");

  // 階段 4 的草稿有警示 = 決定書本身有疑慮
  if (row.stages?.["4"]?.verdict === "warning") out.push("decision-risk");

  // 任何階段失敗 = AI 跑出異常，要人看
  if (Object.values(row.stages ?? {}).some((x) => x.status === "failed")) {
    out.push("ai-anomaly");
  }
  return out;
}

export function toCaseFile(row: CaseRow): CaseFile {
  const s = row.summary;
  const stage1Verdict = row.stages["1"]?.verdict;

  return {
    id: row.case_id,
    caseNo: row.case_id,
    title: s?.original_agency
      ? `${s.original_agency}　${s.law_type ?? ""}`.trim()
      : `案件 ${row.case_id}`,
    appellant: s?.appellant ?? "尚未解析",
    agency: s?.original_agency ?? "尚未解析",
    law: s?.law_type ?? "",
    received: s?.petition_date ?? "—",
    aiVerdict: !stage1Verdict ? "pending" : stage1Verdict === "ok" ? "pass" : "fail",
    aiSummary: s ? deadlineLabel(s) : "尚未執行階段 1，還沒有解析結果",
    quadrant: toQuadrant(row),
    aiAssessment: toAiAssessment(row),
    workStatus: toWorkStatus(row.case_status),
    // 夥伴的 dashboard 用的兩個欄位
    stage: toCaseStage(row),
    nextStage: row.next_stage,
    risks: toRisks(row),
    deadline: s?.deadline ?? "—",
    deadlineDays: s?.remaining_days ?? null,
    priority: toPriority(s),
    keyAlert: s?.is_overdue ? `逾法定期間 ${s.overdue_days ?? "?"} 日` : "",
    updatedAt: row.updated_at ?? "",
    // ⚠️ 前端寫死。後端沒有承辦人欄位（demo 登入純示意，不送 actor）
    assignee: DEFAULT_ASSIGNEE,
  };
}

/* ═══════════════════════ 階段 1 ═══════════════════════ */

/**
 * 後端 8 種 status → 前端 3 種 verdict。
 *
 * ⚠️ **`partial` 會被併進 `fail`，這會丟資訊。**
 * 「記載不完全」跟「完全沒寫」的法律效果不同（前者是**可補正**的，
 * 依訴願法第 62 條應先通知補正）。所以另外導出 `isCurable`，
 * 讓 UI 至少能把可補正的標成不同顏色。
 * 前端 `CheckItem.verdict` 加一個 `warn` 之後就可以拿掉這個折衷。
 */
export function toVerdict(status: CheckStatus): CheckItem["verdict"] {
  switch (status) {
    case "present":
    case "no_match":
      return "pass";
    case "missing":
    case "partial":
    case "triggered":
      return "fail";
    default:
      return "info";
  }
}

export type CheckItemPlus = CheckItem & {
  /** 原始 status，`partial` 這種資訊不要丟掉 */
  status: CheckStatus;
  /** 可補正 → 依訴願法第 62 條應先通知補正，不是直接不受理 */
  curable: boolean;
  basis: string | null;
  /** 期間計算（77-2 才有） */
  computed: NonNullable<Stage1Detail["procedure_check"]["items"][number]["computed"]> | null;
};

export function toCheckItems(d: Stage1Detail): CheckItemPlus[] {
  const all = [
    ...(d.spec_check?.items ?? []),
    ...(d.procedure_check?.items ?? []),
    ...(d.other_check?.items ?? []),
  ];
  return all.map((it) => ({
    id: it.code,
    label: it.label,
    value: it.value ?? "",
    verdict: toVerdict(it.status),
    reason: [it.note, it.basis].filter(Boolean).join("　"),
    // 反白定位：後端沒有座標，用 value 當關鍵字去原文找
    anchor: it.code,
    status: it.status,
    curable: it.curable === true,
    basis: it.basis ?? null,
    computed: it.computed ?? null,
  }));
}

/** 訴願書／答辯書全文切成段落，給文件檢視器用。 */
/**
 * 階段 1 的 detail → 「文件與 AI 判讀依據」那個對照畫面要的三樣東西。
 *
 * **為什麼需要這一支**：`toCaseFile()` 只吃得到 `GET /cases` 的列表資料，
 * 那裡沒有檢核項也沒有原文，所以它把 `checks` 和 `document` 留成空陣列——
 * 結果對照畫面整個空白（實際踩過）。這些資料只有階段 1 的 detail 才有。
 *
 * **反白怎麼定位**：後端沒有回原文的座標（offset），但**檢核項的 `value`
 * 就是從原文抽出來的字串**，所以拿它回去原文做子字串比對就能定位，
 * 不用改 `CaseAnalysis` 的畫線邏輯。
 *
 * ⚠️ 抽出來的值跟原文**不一定逐字相同**（模型可能正規化過，例如把
 * 「１１２年」寫成「112年」）。對不上的就不反白——寧可少畫一條線，
 * 也不要把線指到錯的地方。
 */
export function toAnalysis(d: Stage1Detail): {
  checks: CheckItemPlus[];
  document: { id: string; text: string; marks?: string[] }[];
  highlights: Record<string, string>;
} {
  const checks = toCheckItems(d);

  // anchor（= 檢核項代碼）→ 要在原文裡反白的字串
  const highlights: Record<string, string> = {};
  for (const c of checks) {
    const v = (c.value ?? "").trim();
    // 太短的值（"是"、"1"）到處都比對得到，反白會亂跳
    if (v.length >= 4) highlights[c.anchor] = v;
  }

  const paras = toParagraphs(d.petition_text ?? "");
  const document = paras.map((p) => {
    const marks = Object.entries(highlights)
      .filter(([, needle]) => p.text.includes(needle))
      .map(([key]) => key);
    return marks.length ? { ...p, marks } : p;
  });

  return { checks, document, highlights };
}

export function toParagraphs(text: string): { id: string; text: string }[] {
  return text
    .split(/\n+/)
    .map((t) => t.trim())
    .filter((t) => t.length > 0)
    .map((t, i) => ({ id: `p${i + 1}`, text: t }));
}

/** 處置按鈕。後端已經算好哪一個是建議值。 */
export function toDispositionOptions(d: Stage1Detail): DispositionOption[] {
  return d.dispositions ?? [];
}

/* ═══════════════════════ 階段 2 ═══════════════════════ */

/**
 * 主張 → `FactItem`。
 *
 * ⚠️ 前端的 `issues` 是「一條主張 → 多個爭點」，後端是**一對一**。
 * 這裡反查後端的 `issues[]` 填成長度 0 或 1 的陣列，前端不用改。
 */
export function toFactItems(
  claims: Claim[],
  issues: ApiIssue[],
  side: "petition" | "defense",
): FactItem[] {
  const key = side === "petition" ? "petition_claim_id" : "defense_claim_id";
  return claims.map((c) => ({
    id: c.claim_id,
    text: c.claim,
    issues: issues.filter((i) => i[key] === c.claim_id).map((i) => i.issue_id),
  }));
}

export function toUiIssues(d: Stage2Detail): UiIssue[] {
  return (d.issues ?? []).map((i) => ({
    id: i.issue_id,
    label: i.issue,
    note: [i.issue_kind, i.confidence ? `信心 ${i.confidence}` : null].filter(Boolean).join("　"),
    // 爭點配對是 AI 做的
    source: "AI",
  }));
}

/** 機關漏答的主張。**決定書要處理，前端要顯示。** */
export function unansweredClaims(d: Stage2Detail): Claim[] {
  const ids = new Set(d.unpaired_petition ?? []);
  return (d.petition_claims ?? []).filter((c) => ids.has(c.claim_id));
}

/**
 * 兩邊各自沒配對到的主張。**要分開，不要合在一起顯示。**
 *
 * ⚠️ `unpaired_petition` / `unpaired_defense` 後端回的是 **claim_id 陣列**，
 * 不是主張物件。直接渲染會在畫面上印出「c-f074ea」這種亂碼
 * （2026-09-12 實際踩過）。要拿 id 回 claims 裡查。
 *
 * ⚠️ **兩邊的法律意義完全不同**：
 *   - 訴願人主張沒有對應答辯 = **機關漏答**，決定書依法要逐項回應
 *   - 機關答辯沒有對應主張   = 機關講了訴願人沒提的事，通常不必處理
 * 合在一起顯示會讓承辦人分不出哪個要動作。
 *
 * ⚠️ **2026-09-13 起步驟二已經不用這個**——改成所有主張／答辯都列出來，
 * 未配對的只是標籤上寫「未配對」。這裡留著是因為「機關漏答」在階段四
 * 仍然是實體問題（`claims_unanswered_by_agency`），別因為沒人 import
 * 就刪掉。
 */
export function unpairedClaims(d: Stage2Detail): {
  petition: Claim[];
  defense: Claim[];
} {
  const byId = new Map<string, Claim>();
  for (const c of [...(d.petition_claims ?? []), ...(d.defense_claims ?? [])]) {
    byId.set(c.claim_id, c);
  }
  // ⚠️ 查不到 id 就略過，不要回一個假的物件——那會讓畫面顯示空白卡片，
  //    比直接不顯示更難查。
  const pick = (ids: string[] | undefined) =>
    (ids ?? []).map((id) => byId.get(id)).filter((c): c is Claim => Boolean(c));

  return {
    petition: pick(d.unpaired_petition),
    defense: pick(d.unpaired_defense),
  };
}

/* ═══════════════════════ 階段 3 ═══════════════════════ */

function hitTitle(h: RetrievalHit): string {
  if (h.doc_type === "法規" && h.law && h.article) {
    return `${h.law}第 ${h.article} 條`;
  }
  return h.ref_key;
}

export type RefItemPlus = RefItem & {
  refKey: string;
  docType: string;
  authorityRank: number;
  selected: boolean;
  /** 開原始 PDF 用（舊索引沒有這個欄位，會是 null） */
  file: string | null;
  vectorScore: number | null;
  /** 沒被自動勾選的原因 */
  blockedReason: string | null;
};

function toRefItem(h: RetrievalHit): RefItemPlus {
  return {
    id: h.ref_key,
    title: hitTitle(h),
    // ⚠️ 後端沒有外部連結。原始 PDF 要另外呼叫 /sources 換 presigned URL，
    //    所以這裡留空，由 UI 用 `file` 去換。
    url: "",
    summary: h.text,
    source: h.selected ? "AI" : "人工",
    refKey: h.ref_key,
    docType: h.doc_type,
    authorityRank: h.authority_rank,
    selected: h.selected,
    file: h.file ?? null,
    vectorScore: h.score_vector_raw,
    blockedReason: h.blocked_reason ?? null,
  };
}

/** 攤平所有主張的檢索結果，法規與其他分開（前端是兩個清單）。 */
export function toRetrievalRefs(d: Stage3Detail): {
  laws: RefItemPlus[];
  cases: RefItemPlus[];
  byClaim: Record<string, RefItemPlus[]>;
} {
  const laws = new Map<string, RefItemPlus>();
  const cases = new Map<string, RefItemPlus>();
  const byClaim: Record<string, RefItemPlus[]> = {};

  for (const pc of d.per_claim ?? []) {
    const bucket: RefItemPlus[] = [];
    for (const g of pc.groups ?? []) {
      for (const h of g.hits ?? []) {
        const item = toRefItem(h);
        bucket.push(item);
        // ⚠️ 同一筆可能被多條主張撈到，只留一份（**保留已勾選的那一版**）
        const target = h.doc_type === "法規" ? laws : cases;
        const prev = target.get(item.refKey);
        if (!prev || (item.selected && !prev.selected)) {
          target.set(item.refKey, item);
        }
      }
    }
    byClaim[pc.claim_id] = bucket;
  }

  const rank = (a: RefItemPlus, b: RefItemPlus) => a.authorityRank - b.authorityRank;

  return {
    laws: [...laws.values()].sort(rank),
    cases: [...cases.values()].sort(rank),
    byClaim,
  };
}

/** 每條主張撈到什麼（含它是訴願方還是機關方）。 */
export function retrievalByClaim(d: Stage3Detail): PerClaimRetrieval[] {
  return d.per_claim ?? [];
}

/* ═══════════════════════ 階段 4 ═══════════════════════ */

/** 把三段組成完整決定書文字（後端階段 5 的 `_render()` 做同一件事）。 */
export function toFullText(draft: Draft, caseId: string): string {
  const L: string[] = [`新北市政府訴願決定書　案號：${caseId}`, ""];
  L.push("主文", "", draft.main_text, "");
  if (draft.facts_summary) L.push("事實", "", draft.facts_summary, "");
  L.push("理由", "");
  for (const s of draft.sections ?? []) {
    L.push(`${s.no}、${s.text}`, "");
  }
  return L.join("\n");
}

/**
 * 決定書的 11 個欄位 → 公文全文。
 *
 * ⚠️⚠️ **這個是從 `DecisionDoc` 算的，不是從 `draft.sections` 算的**
 * ——跟上面的 `toFullText()` 差別就在這裡，不要弄混。
 *
 * 步驟四的「決定書原文」檢視用這個，理由是**同步**：承辦人在結構化欄位
 * 改了「理由」之後，原文要跟著變。原文如果是從 `draft.sections` 算的，
 * 改完欄位之後兩邊就對不起來了（而且畫面上沒有任何提示）。
 *
 * 所以原文檢視是**唯讀的衍生物**，唯一的真相是那 11 個欄位。
 */
export function decisionToText(d: DecisionDoc): string {
  const L: string[] = ["新北市政府訴願決定書", ""];
  if (d.doc_no) L.push(`發文字號：${d.doc_no}`);
  if (d.issue_date) L.push(`發文日期：${d.issue_date}`);
  L.push(`案號：${d.case_no}`, "");
  if (d.gist) L.push(`要旨：${d.gist}`, "");
  if (d.related_laws) {
    L.push(`相關法條：${d.related_laws.split("\n").filter(Boolean).join("、")}`, "");
  }
  if (d.appellant) L.push(`訴願人：${d.appellant}`);
  if (d.original_agency) L.push(`原處分機關：${d.original_agency}`);
  L.push("", "主文", "", d.main_text || "（待填寫）", "");
  if (d.facts) L.push("事實", "", d.facts, "");
  L.push("理由", "", d.reasons || "（待填寫）", "");
  if (d.committee) L.push("", d.committee);
  return L.join("\n");
}

/** 今天的民國日期，例：「民國 114 年 12 月 17 日」 */
function rocToday(): string {
  const d = new Date();
  return `民國 ${d.getFullYear() - 1911} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

/**
 * 決定書固定欄位：後端給 `decision` 就用後端的，缺的欄位從 `draft` / `case_meta` 補。
 *
 * 只有「相關法條」是猜的——依主文用字判斷：不受理 → 77 條、駁回 → 79 條、撤銷 → 81 條，
 * 再加上案件涉及的實體法。承辦人要核對。
 */
export function toDecisionDoc(detail: Stage4Detail, caseId: string): DecisionDoc {
  const meta = detail.case_meta ?? {};
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  const lawTypes = Array.isArray(meta["case_law_types"])
    ? (meta["case_law_types"] as unknown[]).filter((x): x is string => typeof x === "string")
    : [];
  const main = detail.draft.main_text ?? "";

  const appealArticle =
    detail.disposition === "inadmissible"
      ? "訴願法 第 77 條"
      : main.includes("撤銷")
        ? "訴願法 第 81 條"
        : main.includes("駁回")
          ? "訴願法 第 79 條"
          : "訴願法";
  const relatedLaws = [appealArticle, ...lawTypes.map((l) => `${l}`)].join("\n");

  const reasons = (detail.draft.sections ?? []).map((s) => `${s.no}、${s.text}`).join("\n\n");

  const fallback: DecisionDoc = {
    case_no: caseId,
    gist: lawTypes.length ? `因違反${lawTypes[0]}事件提起訴願` : "因○○事件提起訴願",
    issue_date: rocToday(),
    doc_no: "新北府訴決字第　　　　號",
    related_laws: relatedLaws,
    appellant: str(meta["appellant"]),
    original_agency: str(meta["original_agency"]),
    main_text: main,
    facts: detail.draft.facts_summary ?? "",
    reasons,
    committee: "訴願審議委員會主任委員　\n委員　",
  };

  const given = detail.decision ?? {};
  const out = { ...fallback };
  for (const k of Object.keys(fallback) as (keyof DecisionDoc)[]) {
    const v = given[k];
    if (typeof v === "string" && v.trim()) out[k] = v;
  }
  return out;
}

/**
 * 一段草稿的所有警示，攤平成可以直接顯示的清單。
 *
 * **級別**：`blocking` 的段落不能發文，`warn` 要人判斷。
 * 前端建議 `blocking` 預設展開、`warn` 收起。
 */
export type SectionAlert = {
  level: "blocking" | "warn";
  label: string;
  detail: string;
};

export function sectionAlerts(s: DraftSection): SectionAlert[] {
  const out: SectionAlert[] = [];

  if (s.text_internal_warning) {
    out.push({
      level: "blocking",
      label: "出現系統內部用語",
      detail: s.text_internal_warning,
    });
  }
  if (s.text_citations_outside_list?.length) {
    out.push({
      level: "blocking",
      label: "正文引用清單外的法條",
      detail: `${s.text_citations_outside_list.join("、")}　← 先確認是不是法規名寫錯`,
    });
  }
  if (s.citation_violation?.length) {
    out.push({
      level: "blocking",
      label: "引用了勾選清單以外的依據",
      detail: s.citation_violation.join("、"),
    });
  }
  if (s.route_conflict_warning) {
    out.push({
      level: "blocking",
      label: "理由導向不受理，但本件走實體審查",
      detail: s.route_conflict_warning,
    });
  }
  if (s.quote_warning) {
    out.push({
      // 引文歸屬寫錯是確定的錯，標點不符與核不到要人看
      level: s.quote_misattributed?.length ? "blocking" : "warn",
      label: "引文核對",
      detail: s.quote_warning,
    });
  }
  if (s.deferral_warning) {
    out.push({
      level: "warn",
      label: "推給機關認定卻又下結論",
      detail: s.deferral_warning,
    });
  }
  if (s.conclusion_warning) {
    out.push({
      level: "warn",
      label: "結論不在系統認得的選項裡",
      detail: s.conclusion_warning,
    });
  }
  if (s.text_original) {
    out.push({
      level: "blocking",
      label: "未通過來源檢核，正文已被替換",
      detail: `原因：${s.flag_reason ?? "—"}　模型原本寫的：${s.text_original}`,
    });
  }
  return out;
}

/** 整份草稿層級的警示（不屬於某一段的）。 */
export function draftAlerts(d: Stage4Detail): SectionAlert[] {
  const out: SectionAlert[] = [];
  const draft = d.draft;

  if (draft.main_text_warning) {
    out.push({
      level: "blocking",
      label: "主文有問題",
      detail: draft.main_text_warning,
    });
  }
  if (draft.main_text_vs_sections_warning) {
    out.push({
      level: "blocking",
      label: "主文與各段結論對不上",
      detail: draft.main_text_vs_sections_warning,
    });
  }
  if (draft.curable_warning?.length) {
    out.push({
      level: "warn",
      label: `${draft.curable_warning.length} 項是可補正的缺漏`,
      detail:
        "依訴願法第 62 條應先通知補正：" +
        draft.curable_warning.map((c) => `${c.item}（${c.law}）`).join("、"),
    });
  }
  if (draft.grounds_omitted_from_reasons?.length) {
    out.push({
      level: "warn",
      label: `${draft.grounds_omitted_from_reasons.length} 項已移出理由`,
      detail: draft.grounds_omitted_from_reasons[0]?.why ?? "",
    });
  }
  if (d.claims_unanswered_by_agency?.length) {
    out.push({
      level: "warn",
      label: `${d.claims_unanswered_by_agency.length} 條主張機關沒有答辯`,
      detail: "決定書要處理機關漏答的部分，那幾段只能就法規論述",
    });
  }
  return out;
}

/** 這一階段是誰做的。承辦人對程式與 AI 的信任程度不同。 */
export function madeBy(usedAi: boolean, route?: string): string {
  if (!usedAi) {
    return route === "inadmissible"
      ? "全部由程式產生（不受理模板，未使用 AI）"
      : "全部由程式產生（未使用 AI）";
  }
  return "AI 生成，已通過程式端的引用與一致性檢核";
}

/** verdict → 顯示用的顏色語意。 */
export function verdictTone(v: Verdict): "ok" | "warn" | "danger" {
  if (v === "ok") return "ok";
  if (v === "blocked") return "danger";
  return "warn";
}
