# -*- coding: utf-8 -*-
"""民國年日期處理與訴願法定期間計算。

這個模組是**純程式、零 AI**，而且結論會直接決定案件受不受理，
所以每個函式都要能被測試資料驗證（見 tests/test_samples.py）。

法源：
  訴願法第 14 條第 1 項
    「訴願之提起，應自行政處分達到或公告期滿之**次日**起三十日內為之。」
  行政程序法第 48 條第 2 項（期間末日為假日的順延，本模組未實作，見下方 TODO）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta

# 訴願提起期間（日）。放成常數而不是寫死在算式裡，
# 因為法規若修正只要改這裡。
APPEAL_PERIOD_DAYS = 30

# 補正期間（日）。審議規則第 7 條：自文到之次日起 20 日內補正。
AMEND_PERIOD_DAYS = 20


# ────────────────────────────────────────────────────────────
# 解析：把各種寫法的民國日期變成 date
# ────────────────────────────────────────────────────────────

# 民眾自由書寫會出現的格式，實測樣本 B 就混用了「2 月 3 號」和「114 年 4 月 8 日」
_PATTERNS = [
    # 114-03-10 / 114/3/10 / 114.03.10
    re.compile(r"^\s*(\d{2,3})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*$"),
    # 民國114年3月10日 / 114年3月10日 / 114 年 3 月 10 號
    re.compile(r"^\s*(?:民國)?\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號]?\s*$"),
]


def parse_roc(value: str | None) -> date | None:
    """把民國日期字串轉成 date。看不懂就回 None，不要猜。

    >>> parse_roc("114-03-10")
    datetime.date(2025, 3, 10)
    >>> parse_roc("民國114年3月10日")
    datetime.date(2025, 3, 10)
    >>> parse_roc("不知道") is None
    True
    """
    if not value or not isinstance(value, str):
        return None
    for pat in _PATTERNS:
        m = pat.match(value)
        if not m:
            continue
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y + 1911, mo, d)
        except ValueError:
            # 例如 114-02-30。日期本身不合法，視為抽錯，回 None 讓人工確認。
            return None
    return None


def to_roc(d: date | None) -> str | None:
    """date 轉回 `114-03-10` 這種格式，給前端顯示與存檔用。"""
    if d is None:
        return None
    return f"{d.year - 1911:03d}-{d.month:02d}-{d.day:02d}"


# ────────────────────────────────────────────────────────────
# 計算：訴願法定期間
# ────────────────────────────────────────────────────────────

@dataclass
class PeriodResult:
    """期間計算的結果。欄位名稱直接對應階段 1 輸出的 `computed` 區塊。"""
    ok: bool                      # 能不能算（兩個日期都有效才是 True）
    served_on: str | None         # 收受處分日
    filed_on: str | None          # 提起訴願日
    start_from: str | None        # 起算日（收受日的次日）
    deadline: str | None          # 期間末日
    day_n: int | None             # 提起日是第幾日
    overdue_days: int | None      # 逾期幾日（未逾期為 0）
    remaining_days: int | None    # 還剩幾日（已逾期為 0）
    is_overdue: bool | None       # 是否逾期
    reason: str | None            # 算不出來的原因

    def to_dict(self) -> dict:
        return asdict(self)


def appeal_period(
    served_on: str | date | None,
    filed_on: str | date | None,
    days: int = APPEAL_PERIOD_DAYS,
) -> PeriodResult:
    """算訴願是否逾期。

    規則（訴願法第 14 條第 1 項）：
      起算日 = 收受處分之**次日**
      期間末日 = 起算日 + (days - 1)
      提起日 > 期間末日  →  逾期

    注意「次日起算」：收受日當天**不算**在 30 日之內。

    >>> r = appeal_period("114-03-10", "114-04-02")
    >>> r.deadline, r.day_n, r.is_overdue
    ('114-04-09', 23, False)
    >>> r = appeal_period("114-02-05", "114-04-08")
    >>> r.deadline, r.overdue_days, r.is_overdue
    ('114-03-07', 32, True)
    """
    s = served_on if isinstance(served_on, date) else parse_roc(served_on)
    f = filed_on if isinstance(filed_on, date) else parse_roc(filed_on)

    if s is None or f is None:
        missing = []
        if s is None:
            missing.append("收受處分日")
        if f is None:
            missing.append("提起訴願日")
        return PeriodResult(
            ok=False, served_on=to_roc(s), filed_on=to_roc(f),
            start_from=None, deadline=None, day_n=None,
            overdue_days=None, remaining_days=None, is_overdue=None,
            reason=f"缺少或無法解析：{'、'.join(missing)}",
        )

    if f < s:
        # 提起日早於收受日，一定是有一個抽錯了。不要硬算。
        return PeriodResult(
            ok=False, served_on=to_roc(s), filed_on=to_roc(f),
            start_from=None, deadline=None, day_n=None,
            overdue_days=None, remaining_days=None, is_overdue=None,
            reason="提起訴願日早於收受處分日，日期可能抽取錯誤",
        )

    start = s + timedelta(days=1)          # ← 次日起算
    deadline = start + timedelta(days=days - 1)
    day_n = (f - start).days + 1
    diff = (f - deadline).days

    return PeriodResult(
        ok=True,
        served_on=to_roc(s), filed_on=to_roc(f),
        start_from=to_roc(start), deadline=to_roc(deadline),
        day_n=day_n,
        overdue_days=max(diff, 0),
        remaining_days=max(-diff, 0),
        is_overdue=diff > 0,
        reason=None,
    )


def amend_deadline(notice_sent_on: str | date | None,
                   days: int = AMEND_PERIOD_DAYS) -> str | None:
    """算補正期限（審議規則第 7 條：自文到之次日起 20 日）。

    🚧 補正流程的完整需求尚未取得，這裡只提供期限計算。
    """
    d = notice_sent_on if isinstance(notice_sent_on, date) else parse_roc(notice_sent_on)
    if d is None:
        return None
    return to_roc(d + timedelta(days=1) + timedelta(days=days - 1))


# TODO（等法制局確認）：
#   行政程序法第 48 條第 2 項規定期間末日為星期日、國定假日或其他休息日時，
#   以該日之次日為末日。要正確實作需要「政府行政機關辦公日曆表」。
#   目前**不做順延**，算出來的 deadline 是未順延的日期。
#   影響：期限剛好落在假日的案件，系統可能誤判為逾期 1~3 日。
#   界面上必須提示承辦人自行確認，不可直接據以作成不受理決定。
