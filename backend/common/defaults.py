# -*- coding: utf-8 -*-
"""三張設定表的預設內容（佔位用）。

**這個檔案刻意不 import boto3**，因為：
  1. 規則引擎要能在本機跑測試，不該為了讀幾條規則就得裝 AWS SDK
  2. 這些是「資料」不是「程式」，正式的來源是 S3 上的 JSON

正式環境會用 S3 的版本蓋掉這裡（見 config_store.py）。
S3 上還沒有檔案時就用這裡的值，讓流程不會因為設定沒上傳就整條掛掉。
"""

# ────────────────────────────────────────────────────────────
# 規則表（spec_rules.json）
# ────────────────────────────────────────────────────────────
# 這張表照 `階段2_訴願書規格檢查.png` 寫的，欄位名用圖上
# 「建議系統欄位」那一欄的名字，**不要自己改名**——改了就跟圖對不起來。
#
# ⚠️ 規則是資料，引擎是程式。要調檢查方式**只改這張表**，
#    或者直接改 S3 上的 `config/spec_rules.json`（不用重新部署）。
#
# 🚧 **圖上有三項寫「一般訴願必填」，現在一律當必填。**
#    要分流得看 `appeal_type`，而 appeal_type 目前只抽不用
#    （見 `待優化清單.md` 的 A2）。所以進來的如果是**不作為訴願**，
#    這三項會被誤標成缺漏：
#      original_agency / disposition_received_or_known_date /
#      attachments.disposition_copy_present
#    選這個方向是因為它偏「多標記讓人看」，比「誤放行」安全。

DEFAULT_RULES = [
    # ── 訴願類型（系統必填，不是法定要件）──
    {
        "code": "sys-1", "label": "訴願類型",
        "basis": "系統必填（非法定要件）",
        "check": "present", "fields": ["appeal_type"],
        "curable": False, "enabled": True,
    },

    # ── 訴願法第56條第1項各款 ──
    {
        # 圖上「訴願人」「自然人資料」「法人或團體資料」三列，
        # 法律上同屬第 1 款，所以合成一條規則、用逐筆條件必填處理。
        "code": "56-1-1", "label": "訴願人資料",
        "basis": "訴願法第56條第1項第1款",
        "check": "array_items_present", "fields": ["appellants"],
        "params": {
            "by": "kind",
            "require": {
                "自然人": ["name", "birth", "address", "id_no"],
                "法人或團體": ["org_name", "org_address",
                               "rep_name", "rep_birth", "rep_address"],
            },
        },
        "partial_ok": True, "curable": True, "enabled": True,
    },
    {
        "code": "56-1-2", "label": "訴願代理人資料",
        "basis": "訴願法第56條第1項第2款",
        "check": "array_items_present", "fields": ["agents"],
        "params": {
            # 沒有委任代理人是常態，陣列空的要回 not_applicable 不是 missing
            "optional_if_empty": True,
            "require": ["name", "birth", "address", "id_no"],
        },
        "partial_ok": True, "curable": True, "enabled": True,
    },
    {
        "code": "56-1-3", "label": "原行政處分機關",
        "basis": "訴願法第56條第1項第3款",
        "check": "present", "fields": ["original_agency"],
        "curable": True, "enabled": True,
        "note": "🚧 圖上為「一般訴願必填」，目前未依 appeal_type 分流",
    },
    {
        "code": "56-1-4", "label": "訴願請求事項",
        "basis": "訴願法第56條第1項第4款",
        "check": "array_min", "fields": ["requested_relief"],
        "params": {"min": 1},
        "curable": True, "enabled": True,
    },
    {
        # 圖上「事實」「理由」兩列，法律上同屬第 5 款。
        # 圖上特別註明：即使文件寫在同一段，也應分別抽取或保留合併內容。
        "code": "56-1-5", "label": "訴願之事實及理由",
        "basis": "訴願法第56條第1項第5款",
        "check": "all_present", "fields": ["facts_text", "reasons_text"],
        "partial_ok": True, "curable": True, "enabled": True,
    },
    {
        "code": "56-1-6", "label": "收受或知悉行政處分之年月日",
        "basis": "訴願法第56條第1項第6款",
        "check": "present",
        "fields": ["disposition_received_or_known_date"],
        "curable": True, "enabled": True,
        "note": "🚧 圖上為「一般訴願必填」，目前未依 appeal_type 分流",
    },
    {
        "code": "56-1-7", "label": "受理訴願之機關",
        "basis": "訴願法第56條第1項第7款",
        "check": "present", "fields": ["appellate_authority"],
        "curable": True, "enabled": True,
    },
    {
        "code": "56-1-8", "label": "證據",
        "basis": "訴願法第56條第1項第8款",
        "check": "array_min", "fields": ["evidence"],
        "params": {"min": 1},
        "curable": True, "enabled": True,
        "hint": "文書證據應添具繕本或影本",
    },
    {
        "code": "56-1-9", "label": "訴願書日期",
        "basis": "訴願法第56條第1項第9款",
        "check": "present", "fields": ["petition_date"],
        "curable": True, "enabled": True,
    },
    {
        "code": "56-1-sign", "label": "簽名或蓋章",
        "basis": "訴願法第56條第1項（訴願人或代理人應簽名或蓋章）",
        "check": "flag_true", "fields": ["signature.present"],
        "params": {"role_field": "signature.role"},
        "hint": "未見訴願人或代理人簽名或蓋章",
        "curable": True, "enabled": True,
    },
    {
        "code": "56-2", "label": "原行政處分書影本",
        "basis": "訴願法第56條第2項",
        "check": "flag_true",
        "fields": ["attachments.disposition_copy_present"],
        "hint": "未附原行政處分書影本",
        "curable": True, "enabled": True,
        "note": "🚧 圖上為「一般訴願應附影本」，目前未依 appeal_type 分流",
    },

    # ── 訴願法第77條（程序要件）──
    # ⚠️ 這裡只有第 2 款是程式算得出來的。其餘 7 款要實質法律判斷，
    #    見 `待優化清單.md` 的 A（訴願書違規檢查，尚未實作）。
    {
        # ⚠️ **`from` 用 `service_date_effective`，不是 56-1-6 那個欄位。**
        #
        #    56-1-6 問的是「訴願書有沒有記載收受日」（法定應記載事項），
        #    這一條問的是「事實上哪一天送達」——兩件不同的事，
        #    共用一個欄位會出錯：
        #
        #    實測樣本 C（真實案例）：訴願書從頭到尾沒寫收受日期，
        #    56-1-6 正確判成 missing，但期間也跟著算不出來，
        #    結果一個**逾期 678 日**的案子被建議「待補正」。
        #    而答辯書明明寫著「原處分於 112 年 11 月 12 日送達訴願人本人
        #    並經其簽收」。
        #
        #    `service_date_effective` 由階段 1 組出來：訴願書優先，
        #    訴願書沒寫就採答辯書所載，並標明來源。
        "code": "77-2", "label": "提起訴願逾法定期間",
        "basis": "訴願法第14條第1項、第77條第2款",
        "check": "date_within",
        "fields": {"from": "service_date_effective",
                   "to": "petition_date"},
        "params": {"days": 30, "start_from": "next_day"},
        "on_fail": "blocked", "enabled": True,
    },
    # ══════════════════════════════════════════════════════════
    # 訴願法第 77 條 · 不受理八款
    #
    # ⚠️ **只有款 2 是系統能單獨判成立的**（純日期計算）。
    #    其餘各款要嘛是法律判斷（款 3 二層、款 8）、要嘛依賴訴願會發函後的
    #    程序事實（款 1/4/5 的「逾期不補正」，文件裡看不到），
    #    所以一律回 need_human，由承辦人審酌。
    #
    # ⚠️ **款 1/4/5 是「補正型」**：要件鏈是「有缺項 → 通知補正 → 逾期不補」。
    #    我們只看得到第一段。沒有補正紀錄時**不能標成立也不能標不成立**。
    # ══════════════════════════════════════════════════════════
    {
        "code": "77-1",
        "label": "訴願書不合法定程式，經通知補正逾期不補正",
        "basis": "訴願法第77條第1款（併第56條、第62條）",
        "check": "manual",
        "fields": None,
        "params": {
            "note": "本款依賴「已通知補正且逾期未補」——那是訴願會發函後的"
                    "程序事實，卷內文件看不到。系統只能指出 56 條有哪些缺項"
                    "（見上方規格檢查），是否已逾補正期間請承辦人確認",
        },
    },
    {
        "code": "77-3",
        "label": "訴願人非行政處分相對人亦非利害關係人",
        "basis": "訴願法第77條第3款、第18條",
        "check": "name_match",
        "fields": {"respondent": "respondent", "appellants": "appellants"},
    },
    {
        "code": "77-4",
        "label": "無訴願能力而未由法定代理人代為",
        "basis": "訴願法第77條第4款、民法第12條",
        "check": "adult_age",
        "fields": {"appellants": "appellants", "as_of": "petition_date"},
    },
    {
        "code": "77-5",
        "label": "法人或團體未由代表人或管理人為訴願行為",
        "basis": "訴願法第77條第5款",
        "check": "entity_kind",
        "fields": {"appellants": "appellants"},
    },
    {
        "code": "77-6",
        "label": "行政處分已不存在",
        "basis": "訴願法第77條第6款、第58條第2項",
        # ⚠️ 用 flag_triggers 不是 flag_true——極性相反（為真才是踩到），
        #    而且回的 status 要是 77 條那一組（triggered/no_match）
        "check": "flag_triggers",
        "fields": ["self_revoked"],
        "params": {
            "note": "⚠️ 訴願會最後把處分撤銷是訴願的**結果**（第81條），"
                    "不是本款。本款是提訴願**當時**處分就已經不存在",
        },
    },
    {
        "code": "77-7",
        "label": "對已決定或已撤回之訴願事件重行提起",
        "basis": "訴願法第77條第7款",
        "check": "lookup",
        "fields": ["original_doc_no", "appellants"],
        "params": {
            "note": "比對鍵為（訴願人、原處分文號）。文號相符只到「可疑」，"
                    "是否為「同一事件」要承辦人確認",
        },
    },
    {
        "code": "77-8", "label": "對非行政處分提起訴願",
        "basis": "訴願法第77條第8款",
        "check": "manual",
        "hint": "行政處分之認定屬法律判斷，請承辦人審酌",
        "enabled": True,
    },
]


# ────────────────────────────────────────────────────────────
# 正名對照表（aliases.json）
# ────────────────────────────────────────────────────────────
# 實測結果：決定書「檔名的案由」有錯誤寫法共 13 件；
# 而內文「相關法條」欄位是乾淨的（空氣污染防制法 4 次全對）。
# 所以正名主要在修「案由」，不是修法條引用。

DEFAULT_ALIASES = {
    # 第 1 層：字元正規化。不需要知道是哪部法就能做，零風險。
    "chars": {"汙": "污", "臺": "台"},

    # ★ 權威名稱清單 —— 「相關法規」目錄那 11 個檔名。
    #   ⚠️ **這跟正名表是兩回事，不要混。**
    #   「訴願法」本身就是正名，不需要出現在正名表裡，
    #   但它必須在這份清單裡，否則會被誤判成「庫內無權威來源」。
    #   實測：決定書引用 26 個法規名，這裡有 9 個，其餘 17 個系統無法驗證。
    "known_laws": [
        "噪音管制法", "廢棄物清理法", "建築法", "民法", "洗錢防制法",
        "空氣污染防制法", "行政執行法", "行政程序法", "行政罰法",
        "行政院及各級行政機關訴願審議委員會審議規則", "訴願法",
    ],

    # 第 2 層：正名表。只有庫裡有全文的 9 個法規能自動判斷正名，
    # 其餘 17 個（行政訴訟法、政府資訊公開法…）系統無法驗證，
    # 對不到就原樣保留並記進 warnings。
    "laws": {
        "空氣汙染管制法": "空氣污染防制法",   # 「管制」是錯字，沒有這部法
        "空氣污染管制法": "空氣污染防制法",
    },
    "case_types": {
        "違反空氣汙染管制法事件": "違反空氣污染防制法事件",   # 5 件
        "違反空氣汙染防制法事件": "違反空氣污染防制法事件",   # 8 件
        "申請政府資訊事件": "申請提供政府資訊事件",
        "公寓大廈管理條例事件": "違反公寓大廈管理條例事件",
        "建造執照事件": "請求撤銷建造執照事件",
    },
}


# ────────────────────────────────────────────────────────────
# 欄位影響表（field_impact.json）
# ────────────────────────────────────────────────────────────
# 承辦人改了欄位，只把「真的受影響」的階段標成過期。
# 不要一改就全部重跑——補個出生年月日對抽主張、檢索完全沒影響，
# 卻要重跑三個階段的 Bedrock，很浪費。
#
# 空陣列 = 只影響階段 1 自己。
# **沒列在表裡的欄位一律保守處理（全部標過期）**，見 state.mark_stale。

DEFAULT_FIELD_IMPACT = {
    # ── 訴願書的實質內容：主張是從這裡抽的，整條鏈重來 ──
    "reasons_text":      [2, 3, 4],
    "facts_text":        [2, 3, 4],
    # ── 答辯書：機關方主張、爭點配對、機關方檢索都吃它 ──
    "defense_text":      [2, 3, 4],
    # 法規類型影響階段 3 的 scoped 檢索那一輪
    "case_law_types":    [3, 4],

    # ── 只影響階段 1 自己的（期間計算、格式檢查）──
    "disposition_received_or_known_date": [],
    "appellate_authority": [],
    "appeal_type":       [],
    "signature":         [],
    "attachments":       [],
    "evidence":          [],
    "agents":            [],

    # ── 只影響草稿（抬頭、主文、引用）──
    "appellants":        [4],
    "original_agency":   [4],
    "original_doc_no":   [4],
    "original_doc_date": [4],
    "requested_relief":  [4],         # 影響主文怎麼寫
    "petition_date":     [],

    # ── 階段 4 **自己的產出**（承辦人在畫面上直接改的）──
    #
    # ⚠️⚠️ **一定要列在這裡，而且是 [5] 不是 [4]。**
    #
    #    `mark_stale()` 對**沒列在表裡**的欄位一律保守處理，套用預設的
    #    `[2, 3, 4]`。所以承辦人在步驟四改了一個發文字號，階段 4 會被
    #    標成 stale ——意思是「這階段的結果過期了，要重跑」，而重跑
    #    等於**把他剛打的字整份蓋掉**。
    #
    #    連帶還會壞掉：`GET /cases/{id}/decision.pdf` 只接受
    #    `done` / `confirmed`，stale 會回 409，存個檔就下載不了 PDF。
    #    （2026-09-13 實測踩到：PATCH 完 status 變 stale、PDF 回 409。）
    #
    #    改的是階段 4 自己的輸出，上游完全不受影響；真正過期的是
    #    **階段 5 的封存包**（它存的是舊版決定書），所以指到 [5]。
    "decision":          [5],
    "draft":             [5],
}
