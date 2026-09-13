export type Quadrant = "ai-fail" | "ai-pass" | "human-pass" | "human-fail";

export type AiAssessment = "admissibility-risk" | "clear" | "insufficient" | "decision-risk";

export type WorkStatus = "待確認" | "審理中" | "待補件" | "草稿中" | "已完成";

/**
 * `not-started`：卷證已上傳、階段 1 還沒跑。**要承辦人按「執行 AI 分析」才會動**，
 * 之後才進到 `intake`（待人工初檢）。
 */
export type CaseStage = "not-started" | "intake" | "supplement" | "ready" | "reviewing" | "pending";

export type CaseRisk = "ai-anomaly" | "due-soon" | "blocking-missing" | "decision-risk";

export type CheckItem = {
  id: string;
  label: string;
  value: string;
  verdict: "pass" | "fail" | "info";
  reason: string;
  anchor: string;
};

export type DocParagraph = {
  id: string;
  text: string;
  marks?: string[];
};

export type FactItem = { id: string; text: string; issues: string[] };

export type Issue = { id: string; label: string; note: string; source: "AI" | "人工" };

export type RefItem = {
  id: string;
  title: string;
  url: string;
  summary: string;
  source: "AI" | "人工";
};

export type CaseFile = {
  id: string;
  caseNo: string;
  title: string;
  appellant: string;
  agency: string;
  law: string;
  received: string;
  aiVerdict: "fail" | "pass" | "pending";
  aiSummary: string;
  quadrant: Quadrant;
  aiAssessment: AiAssessment;
  workStatus: WorkStatus;
  stage: CaseStage;
  nextStage: number | null;
  risks: CaseRisk[];
  deadline: string;
  deadlineDays: number | null;
  priority: "critical" | "high" | "medium" | "normal";
  keyAlert: string;
  updatedAt: string;
  /** 承辦人。前端寫死的示意值，後端沒有這個欄位。 */
  assignee: string;
};
