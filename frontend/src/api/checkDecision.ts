/**
 * 檢核結果 → 法律效果 → 處置方向。
 *
 * **這裡放純邏輯，不放畫面。** 顏色與圖示在 `CheckDecisionList.tsx`，
 * 但「這一條採用之後會怎樣」是領域規則，要能單獨測。
 */

import type { CheckResultItem, Disposition } from "./types";

/**
 * 一條檢核結果採用之後**會導致什麼法律效果**。
 *
 * ⚠️⚠️ **是照「法律效果」分，不是照「嚴重度」分。**
 * 訴願法第 77 條第 3 款（訴願人不適格）就算系統只判到「要人審酌」，
 * 它的效果仍然是**不受理**，所以歸 inadmissible 不是 amend。
 * 照嚴重度分的話，承辦人會以為「黃的比較輕，補正一下就好」
 * ——但 77 條那幾款根本不能補正。
 */
export type Effect = "pass" | "amend" | "inadmissible" | "unknown";

export const EFFECT_LABEL: Record<Effect, string> = {
  pass: "通過",
  amend: "待補正",
  inadmissible: "不受理",
  unknown: "系統無法判斷",
};

/**
 * ⚠️ **56 條與 77 條的同一個 status 意思不同**，所以要看 code 前綴：
 *   56 條的 missing   = 格式缺漏 → 可補正 → 黃
 *   77 條的 triggered = 踩到不受理事由 → 紅
 *
 * ⚠️ `unclear` 不是「通過」。系統算不出來（例如訴願書沒寫日期，逾期
 * 無從計算）時硬歸成綠色，會讓承辦人以為查過沒問題。
 */
export function effectOf(item: CheckResultItem): Effect {
  const is77 = String(item.code).startsWith("77");

  switch (item.status) {
    case "present":
    case "no_match":
    case "not_applicable":
      return "pass";
    case "unclear":
      return "unknown";
    case "triggered":
      return "inadmissible";
    case "need_human":
      // 77 條要人審酌 → 採用就是不受理；56 條的話是格式問題
      return is77 ? "inadmissible" : "amend";
    case "missing":
    case "partial":
      // ⚠️ 目前只有 56 條會出現，但規則表是可改的設定檔，
      //    之後有人加了 77 條的 missing 規則時不要分錯邊。
      return is77 ? "inadmissible" : "amend";
    default:
      return "unknown";
  }
}

/**
 * 這一條能不能給承辦人選。
 *
 * ⚠️ **只有「不受理」和「待補正」可以選。**
 *
 *   通過        沒什麼好採用的——它不會改變處置方向
 *   系統無法判斷 選了也只會得到「進入下一步」，跟沒選一樣，
 *               讓人以為表達了什麼其實沒有
 *
 * 兩者都沒選就是正常流程（`decide(null)` → substantive）。
 */
export function isSelectable(item: CheckResultItem): boolean {
  const e = effectOf(item);
  return e === "amend" || e === "inadmissible";
}

/**
 * 預設要選哪一條。
 *
 * ⚠️ **一件案子只有一個處置方向**，所以只挑一條，順序是
 * 紅 → 黃 → 都沒有就不選。
 *
 * ⚠️ **紅色優先於黃色，而且理由不是「紅比較嚴重」。**
 * 逾期、當事人不適格這些是**不可補正**的——踩到了就沒有補正的意義。
 * 後端產生不受理草稿時也是這樣處理（「本件已逾法定期間（不可補正），
 * 訴願逾期即不再審查訴願書格式」）。
 *
 * ⚠️ 同一色之內取**程式算得出來的那條**（`triggered` 有數字、
 * `missing`/`partial` 是欄位確實空白）優先於 `need_human`
 * ——後者是 AI 判的或要法律判斷的，讓系統替承辦人選它不妥當。
 */
const HARD_STATUS = ["triggered", "missing", "partial"];

export function defaultPick(items: CheckResultItem[]): string | null {
  const rank = (i: CheckResultItem) => {
    const e = effectOf(i);
    const color = e === "inadmissible" ? 0 : e === "amend" ? 1 : 9;
    return color * 10 + (HARD_STATUS.includes(i.status) ? 0 : 1);
  };
  // 只從可選的裡面挑——不然會預選一條承辦人根本點不掉的項目
  const candidates = items.filter((i) => isSelectable(i) && rank(i) < 90);
  if (!candidates.length) return null;
  // ⚠️ `sort` 會改原陣列——items 是 useMemo 出來的，改它會讓畫面順序跟著變
  const best = [...candidates].sort((a, b) => rank(a) - rank(b))[0];
  return best?.code ?? null;
}

export type Plan = {
  disposition: Disposition;
  label: string;
  hint: string;
  /** 被選中的那一條，沒選是 null */
  picked: CheckResultItem | null;
};

/**
 * 選中的那一條 → 按鈕要做什麼。
 *
 * ⚠️ **承辦人只能選一條**（2026-09-13 改，原本是複選）。
 * 一件案子只會有一個處置方向，複選的話「勾了 3 紅 2 黃」要靠程式
 * 幫他收斂成一個結論——那個收斂規則是承辦人自己該決定的事。
 *
 * ⚠️ **沒選 = 程序上沒問題 = 進實體審查。** 不要當成「還沒決定」而
 * 擋住按鈕：全部 20 條都綠的案子本來就不必選任何一條。
 */
export function decide(picked: CheckResultItem | null): Plan {
  const effect = picked ? effectOf(picked) : "pass";
  const name = picked ? `${picked.code} ${picked.label}` : "";

  if (effect === "inadmissible") {
    return {
      disposition: "inadmissible",
      label: "進入不受理",
      // ⚠️ 這句話 2026-09-13 改過：不受理**不再跳過**階段 2、3。
      //    實際案例的不受理決定書仍然會就訴願人的主張作說明。
      hint: `將依「${name}」作成不受理決定。仍會整理爭點並檢索依據——決定書的理由第一點寫不受理事由，後面照樣要對訴願人的主張與機關的答辯作說明。`,
      picked,
    };
  }
  if (effect === "amend") {
    return {
      disposition: "amend",
      label: "進入待補正",
      hint: `將依訴願法第 62 條通知訴願人於 20 日內補正「${name}」。`,
      picked,
    };
  }
  return {
    disposition: "substantive",
    label: "確認並進入下一步",
    hint: picked
      ? `已採用「${name}」，該項不影響受理，進入實體審查。`
      : "未採用任何不受理或補正事由，進入實體審查。",
    picked,
  };
}
