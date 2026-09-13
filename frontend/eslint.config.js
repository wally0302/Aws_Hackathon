import js from "@eslint/js";
import eslintPluginPrettier from "eslint-plugin-prettier/recommended";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", ".output", ".vinxi"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "server-only",
              message:
                "TanStack Start does not use the Next.js `server-only` package. Rename the module to `*.server.ts` or mark it with `@tanstack/react-start/server-only`.",
            },
          ],
        },
      ],
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": "off",
      // 全角空格（U+3000）在中文顯示文字裡是**刻意的排版**，不是打錯字
      // ——公文體用它分隔欄位（「案　　號：」、「機關　法規類型」）。
      // 這條規則預設 skipStrings: true 但 skipTemplates: false，
      // 所以字串沒事、模板字串會被擋。
      // ⚠️ 只放寬「會顯示給人看的文字」——模板字串與 JSX 文字節點。
      // 識別字之間、程式碼縫隙的全角空格照樣要抓，那種才是
      // 會讓程式壞掉又看不出來的隱形字元（已驗證仍抓得到）。
      "no-irregular-whitespace": ["error", { skipTemplates: true, skipJSXText: true }],
    },
  },
  eslintPluginPrettier,
);
