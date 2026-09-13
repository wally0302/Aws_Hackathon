/**
 * 後端回傳的形狀。**這是照後端實際輸出寫的，不是想像的。**
 *
 * ⚠️ 改這裡要同時改後端的 `tests/test_api_shapes.py`——
 * 那份測試就是為了擋「後端改了 key 名字、前端安靜地顯示空白」。
 *
 * 每個階段的回傳都是同一個外殼（envelope），只有 `detail` 不同。
 */

/* ─────────────────────────── 外殼 ─────────────────────────── */

/** 階段的執行狀態。**系統決定的**，跟承辦人的處置是兩件事。 */
export type StageStatus = "pending" | "running" | "done" | "confirmed" | "failed" | "stale";

export type Verdict = "ok" | "warning" | "need_human" | "blocked";

/** 承辦人的處置方向。**只有這四個。** */
export type Disposition = "substantive" | "inadmissible" | "amend" | "withdraw";

export type AiCall = {
  purpose: string;
  model: string;
  tokens_in: number;
  tokens_out: number;
  elapsed_ms: number;
};

export type StageEnvelope<D = unknown> = {
  case_id: string;
  stage: number;
  stage_label: string;
  version: number;
  status: StageStatus;
  verdict: Verdict;
  summary: string;
  /** ★ 這一階段有沒有用 AI。階段 5 和不受理路線的階段 4 是 false */
  used_ai: boolean;
  ai_calls: AiCall[];
  elapsed_ms: number;
  detail: D;
  editable_fields: string[];
  /** 改了上游欄位後，哪些下游階段被標成過期 */
  stale_stages: number[];
  next_action: string;
  /** 後端在 `pending` 外殼上附的：現在能不能跑這一階段（上一階段確認了沒） */
  can_run?: boolean;
  needs_confirmation?: {
    key: string;
    label: string;
    why: string;
    input_type: string;
  }[];
  available_versions?: number[];
};

/* ────────────────────────── 案件列表 ────────────────────────── */

/**
 * 階段 1 跑完後反正規化到 progress 筆的欄位。
 * **列表頁靠這個一次請求畫完**，不用逐案再打 stages/1。
 * 還沒跑階段 1 的案件是 `null`。
 */
export type CaseSummary = {
  appellant: string | null;
  appellant_count: number | null;
  original_agency: string | null;
  law_type: string | null;
  petition_date: string | null;
  service_date: string | null;
  deadline: string | null;
  remaining_days: number | null;
  overdue_days: number | null;
  is_overdue: boolean | null;
  suggest_disposition: Disposition | null;
};

export type CaseRow = {
  case_id: string;
  case_status: string;
  disposition: Disposition | null;
  next_stage: number | null;
  updated_at: string | null;
  uploaded_by: string | null;
  summary: CaseSummary | null;
  stages: Record<string, { status: StageStatus; version: number; verdict: Verdict | null }>;
};

export type CaseListResponse = { count: number; cases: CaseRow[] };

/* ───────────────────────── 階段 1 ───────────────────────── */

export type CheckStatus =
  | "present"
  | "partial"
  | "missing"
  | "not_applicable"
  | "triggered"
  | "no_match"
  | "need_human"
  | "unclear";

export type CheckResultItem = {
  code: string;
  label: string;
  status: CheckStatus;
  value?: string | null;
  note?: string | null;
  basis?: string | null;
  curable?: boolean;
  /** 期間計算（77-2 才有），純程式算的 */
  computed?: {
    ok: boolean;
    served_on: string | null;
    filed_on: string | null;
    start_from: string | null;
    deadline: string | null;
    day_n: number | null;
    overdue_days: number | null;
    remaining_days: number | null;
    is_overdue: boolean;
    reason: string | null;
  } | null;
  /* ── AI 判斷層（77 條的款 3 二層、6、8）。⚠️ 最高只到「可疑」 ── */
  /** 可疑 / 不成立 / 資料不足。**沒有「成立」**——那只有程式能給 */
  ai_verdict?: "可疑" | "不成立" | "資料不足" | null;
  ai_reason?: string | null;
  /** 文件原句，AI 說「可疑」時一定要有，沒有就會被降級成「資料不足」 */
  ai_quote?: string | null;
  ai_quote_from?: string | null;
  /** 程式已有明確結論時，AI 的不同意見只會放在這裡，不改變 status */
  ai_note?: string | null;
  ai_downgraded?: string | null;

  /* ── 白話說明（每一條都有，**不是判斷**）────────────────────
     ⚠️ 跟上面那組 `ai_*` 不一樣：上面是「AI 判了什麼」（只有 77-3/6/8
     有），這一組是「為什麼是這個結果」的說明，20 條每一條都有。
     說明不會改變 `status`——結論仍然是程式判的。 */
  /** 三句話：文件裡怎麼寫 → 依據哪一條 → 所以結果是什麼 */
  ai_narrative?: string | null;
  /** 說明裡引的原句，逐字照抄。文件根本沒寫這件事時是 null */
  ai_narrative_quote?: string | null;
  ai_narrative_quote_from?: string | null;
  /** 模型認為程式判錯了才有。⚠️ **只是提醒，不會改變結論** */
  ai_narrative_disagree?: string | null;
};

export type DispositionOption = {
  value: Disposition;
  label: string;
  description: string;
  /** 系統建議的那一個 */
  suggested: boolean;
};

export type Stage1Detail = {
  documents: {
    petition?: { page_count?: number; chars?: number; compat_chars_normalized?: number };
    defense?: { page_count?: number; chars?: number; compat_chars_normalized?: number };
  };
  fields: { key: string; value: unknown; confidence?: string; note?: string | null }[];
  /** ⚠️ `display_only: true`——規格檢查結果**只呈現，不進任何 prompt** */
  spec_check: { items: CheckResultItem[]; display_only?: boolean };
  procedure_check: { items: CheckResultItem[] };
  other_check?: { items: CheckResultItem[] };
  /** 白話說明的產生狀況。`missing` 是模型漏掉沒寫說明的那幾條 */
  check_narrative?: { count: number; missing: string[]; error: string | null };
  case_law_types: string[];
  service_date: {
    value: string | null;
    /** 「訴願書」或「答辯書」——**中文字串，不是 petition/defense** */
    source: string | null;
    from_petition: string | null;
    from_defense: string | null;
    defense_quote: string | null;
    why_two_fields: string;
  };
  facts_text: string | null;
  reasons_text: string | null;
  petition_text: string;
  defense_text: string;
  dispositions: DispositionOption[];
  case_summary: CaseSummary;
};

/* ───────────────────────── 階段 2 ───────────────────────── */

export type Claim = {
  claim_id: string;
  no: number;
  claim: string;
  /** 原文逐字，前端靠它反白 */
  quote: string | null;
  claim_kind?: string | null;
  /** 訴願書裡明文寫的法條 */
  cited_laws?: string[];
  /** AI 推斷的（**跟明文引用分開，不要混**） */
  inferred_laws?: string[];
};

export type Issue = {
  issue_id: string;
  no: number;
  /** 完整爭點，通常是 30-50 字的問句 */
  issue: string;
  /**
   * 爭點的短名稱，4-10 字名詞短語（例：「割裂處理義務」）。
   *
   * ⚠️ **不要自己截斷 `issue` 來代替**——會切在奇怪的地方
   * （「系爭政府資訊是否應因部分內…」）。後端要不到時就整串顯示。
   */
  issue_label?: string | null;
  issue_kind: string | null;
  confidence: string | null;
  note: string | null;
  petition_claim_id: string | null;
  defense_claim_id: string | null;
  petition_claim: string | null;
  defense_claim: string | null;
};

export type Stage2Detail = {
  petition_claims: Claim[];
  defense_claims: Claim[];
  issues: Issue[];
  /** ★ 機關漏答——決定書要處理，前端要顯示 */
  unpaired_petition: string[];
  unpaired_defense: string[];
  likely_missed_petition_claims?: string[];
  /** 模型違反 1:1 被程式擋下來的 */
  pairing_dropped?: unknown[];
};

/* ───────────────────────── 階段 3 ───────────────────────── */

export type RetrievalHit = {
  ref_key: string;
  doc_type: "法規" | "判解" | "函釋" | string;
  authority_rank: number;
  law: string | null;
  article: string | null;
  text: string;
  /** 來源 PDF 檔名，開原始檔用（2026-09-10 才加，舊索引沒有） */
  file?: string | null;
  selected: boolean;
  score_vector_raw: number | null;
  score_bm25_raw: number | null;
  /** 這一筆是被哪幾種檢索撈到的，例如 ["向量"]、["BM25","向量"]。**是陣列不是字串。** */
  passed_by?: string[] | null;
  /** 沒被自動勾選的原因 */
  blocked_reason?: string | null;
  relevance_rank?: number;
  scope?: string;
};

export type RetrievalGroup = {
  authority_rank: number;
  label: string;
  hits: RetrievalHit[];
};

export type PerClaimRetrieval = {
  claim_id: string;
  claim: string;
  /** petition / defense */
  side: string;
  side_label: string;
  /**
   * 這條主張屬於哪個爭點。
   *
   * ⚠️ **這是物件不是字串。** 後端 `stage3_retrieve.run()` 組的是
   * `{"issue_id": ..., "issue": ...}`——要顯示的是 `.issue`。
   * 我一開始把它宣告成 `string | null`，tsc 因此抓不到，結果前端
   * 直接把物件丟進 JSX，整頁掛掉：
   *   「Objects are not valid as a React child (found: object with
   *     keys {issue_id, issue})」（2026-09-12 實際踩過）
   */
  issue: { issue_id: string | null; issue: string | null } | null;
  groups: RetrievalGroup[];
  query_rewritten?: string;
  law_hints?: string[];
};

export type Stage3Detail = {
  per_claim: PerClaimRetrieval[];
  claims_without_basis?: string[];
  authority_note?: Record<string, { corpus_docs?: number; note?: string }>;
};

/* ───────────────────────── 階段 4 ───────────────────────── */

export type DraftSection = {
  claim_id: string | null;
  no: number;
  text: string;
  citations: string[];
  conclusion: string;
  note?: string | null;
  /** "ai" | "rule"——**這一段是 AI 寫的還是程式產的** */
  source: string;
  /**
   * **寫這一段時的推理過程**，給承辦人看的，不進決定書。
   *
   * ⚠️ 一段一則，不是整篇一則——前端左右對照要對得出是哪一段的推理。
   * ⚠️ 不受理路線是模板產的（`source: "rule"`），沒有這個欄位。
   */
  rationale?: string | null;
  /** 不受理模板才有 */
  ground_type?: string;

  /* ── 八道程式檢核的結果。有值就是有問題，要顯示給承辦人 ── */
  /** 正文引用了勾選清單以外的法條（可能是法規名寫錯） */
  text_citations_outside_list?: string[] | null;
  /** 引用陣列越界 */
  citation_violation?: string[] | null;
  /** 實體審查路線卻導向不受理 */
  route_conflict_words?: string[] | null;
  route_conflict_warning?: string | null;
  /** 引文歸屬寫錯／標點不符／核不到 */
  quote_misattributed?: unknown[] | null;
  quote_punctuation_differs?: unknown[] | null;
  quote_unverified?: unknown[] | null;
  quote_warning?: string | null;
  /** 「應由原處分機關認定」又下結論的自我矛盾 */
  deferral_phrase?: string | null;
  deferral_warning?: string | null;
  /** 正文出現系統內部用語（例如「階段1」、`birth`） */
  text_internal_terms?: string[] | null;
  text_internal_warning?: string | null;
  /** conclusion 超出選項 */
  conclusion_outside_enum?: string | null;
  conclusion_warning?: string | null;
  /** 未通過來源檢核時，模型原本寫的內容 */
  text_original?: string | null;
  flag_reason?: string | null;
  guardrail?: string | null;

  issue_id?: string | null;
  issue?: string | null;
  agency_answered?: boolean;
};

export type Draft = {
  main_text: string;
  facts_summary?: string | null;
  main_text_basis?: string | null;
  sections: DraftSection[];
  main_text_warning?: string | null;
  main_text_vs_sections_warning?: string | null;
  /** 不受理路線才有 */
  grounds?: unknown[];
  /**
   * 不受理事由對應的法條原文。
   *
   * ⚠️ **後端目前只有殼**，`implemented` 一律是 false、`hits` 一律是空的。
   * 不受理案跳過階段 3，所以沒有 `hits[].text` 可以看條文——這是要補的落差。
   * 前端先照這個形狀接，之後後端填上去就會自己有東西。
   */
  ground_laws?: {
    implemented: boolean;
    note: string;
    hits: RetrievalHit[];
    /** 事由用到的法規名稱，例如 ["訴願法"] */
    wanted: string[];
  };
  curable_warning?: { item: string; law: string }[] | null;
  grounds_omitted_from_reasons?: { item: string; law: string; why: string }[] | null;
};

export type GuardrailSummary = {
  sections_checked?: number;
  passed?: number;
  flagged?: number;
  citation_violations?: number;
  guardrail_configured?: boolean;
  note?: string;
  skipped?: boolean;
  reason?: string;
  sections_with_outside_text_citations?: number;
  sections_turning_inadmissible?: number;
  sections_with_misattributed_quotes?: number;
  sections_with_unverified_quotes?: number;
  sections_with_quote_punctuation_diff?: number;
  sections_with_unknown_conclusion?: number;
  sections_deferring_but_deciding?: number;
  sections_with_internal_terms?: number;
};

/**
 * 決定書的固定欄位，照新北市政府訴願決定書的版面分三塊。
 *
 * ⚠️ **後端目前還沒有這個 key。** 前端先照這個形狀接；後端沒給時
 * `toDecisionDoc` 會從 `draft` / `case_meta` 拼出預設值。
 * 後端補上後直接覆蓋，不用改前端。
 */
export type DecisionDoc = {
  /** ── 1. 基本資料 ── */
  case_no: string;
  /** 例：「因違反建築法事件提起訴願」 */
  gist: string;
  /** 例：「民國 114 年 12 月 17 日」 */
  issue_date: string;
  /** 例：「新北府訴決字第 1141934721 號」 */
  doc_no: string;
  /** 例：「訴願法 第 81 條」，多條用換行分隔 */
  related_laws: string;
  /** ── 2. 全文 ── */
  appellant: string;
  original_agency: string;
  /** 主文（最終裁定結果） */
  main_text: string;
  facts: string;
  reasons: string;
  /** ── 3. 委員會署名 ── */
  committee: string;
};

export type Stage4Detail = {
  /**
   * "substantive"（全部走 AI）或 "inadmissible"。
   *
   * ⚠️ **不受理不再是純模板**（2026-09-13 起）：理由的前幾點是程式算的
   * 不受理事由，後面幾點是 AI 寫的、對訴願人主張與機關答辯的說明。
   * 段落上用 `sections[].source`（"rule" / "ai"）分。
   */
  route: string;
  disposition: Disposition;
  case_meta: Record<string, unknown>;
  draft: Draft;
  /** 決定書固定欄位。⚠️ 2026-09-12 後端已實作，會覆蓋前端的 fallback */
  decision?: Partial<DecisionDoc> | null;
  /**
   * **一句話結論**（40-80 字），給承辦人看的，不進決定書。
   *
   * ⚠️ 跟 `overall_rationale` 分開是刻意的：承辦人要先看到「看完這一堆，
   * 結論是什麼」，再決定要不要讀下面那 400 字。顯示時要放在最前面。
   */
  overall_conclusion?: string | null;
  /**
   * **整份決定書的判斷脈絡**，給承辦人看的，不進決定書。
   *
   * 跟 `draft.sections[].rationale` 分工：這裡講「為什麼整體是這個結論」，
   * 每段講「這一條主張為什麼這樣回應」。
   *
   * ⚠️ prompt 有要求它講出**判斷不足或需要補查的地方**——那是承辦人最
   * 需要知道的，顯示時不要藏起來。
   */
  overall_rationale?: string | null;
  guardrail_summary: GuardrailSummary;
  sources_used: string[];
  source_stats?: {
    total: number;
    by_claim: Record<string, number>;
    claims_without_source: string[];
    from_both_sides: string[];
  };
  issues?: Issue[];
  claims_unanswered_by_agency?: string[];
};

/* ───────────────────────── 階段 5 ───────────────────────── */

export type Stage5Detail = {
  archived: {
    draft_s3_key: string;
    bundle_s3_key: string;
    bucket: string;
    draft_version: number;
    bytes: { markdown: number; json: number };
    audit_trail_items: number;
    case_status_after: string;
  };
  /** ⚠️ `false` 是**刻意的**，不是漏做——見 index_note */
  index_updated: boolean;
  index_note: string;
  route: string;
};

/* ───────────────────────── 其他 ───────────────────────── */

/**
 * `GET /cases/{id}/decision.pdf` 的回應。跟 `/sources` 一樣走 presigned URL，
 * 不讓 PDF 位元組穿過 API Gateway（binary media type 設定麻煩、也有 10 MB 上限）。
 *
 * ⚠️ **後端尚未實作**，見 PR 說明的 API 需求。
 */
export type DecisionPdfResponse = {
  url: string;
  expires_in: number;
  filename: string;
  bytes: number | null;
  /** 這份 PDF 是照階段 4 第幾版產的 */
  draft_version: number;
};

export type SourceUrlResponse = {
  url: string;
  expires_in: number;
  kind: string;
  file: string;
  s3_key: string;
  bytes: number | null;
  note: string;
};

export type ApiErrorBody = {
  error: string;
  hint?: string;
  available?: string[] | Record<string, unknown>;
};

/** `GET /admin/corpus` —— 建庫現況。 */
export type CorpusReport = {
  law_articles: {
    total: number;
    /** 1 法規 / 2 司法院釋字 / 3 判解 / 4 函釋 */
    by_authority_rank: Record<string, number>;
    by_doc_type: Record<string, number>;
  };
  case_reasons: {
    /** reason = 決定書理由分點、claim = 訴願人主張 */
    by_unit_type: Record<string, number>;
  };
  note?: string;
};

export type AdminConfigSummary = {
  name: string;
  description: string | null;
  s3_key: string;
  items: number | null;
  is_default: boolean;
};

export type AdminConfigList = {
  configs: AdminConfigSummary[];
  note: string;
};

export type AdminConfigDetail = {
  name: string;
  description: string | null;
  is_default: boolean;
  data: unknown;
};

export type AdminConfigSaveResult = {
  name: string;
  saved: boolean;
  actor: string;
  items: number | null;
  note: string;
};
