/**
 * Demo 登入狀態。**純前端示意，不接後端。**
 *
 * ⚠️ 登入頁不驗證密碼，登入的名字也不會送給後端；這裡只是讓畫面有
 * 「誰在用」的感覺。案件列表的承辦人欄一律顯示 `DEFAULT_ASSIGNEE`。
 *
 * 作法照 `workflow.tsx`：SSR 期間不能碰 `window`，storage 要在 `useEffect`
 * 讀，讀完才把 `hydrated` 翻成 true。差別是用 localStorage，重新整理與
 * 新分頁都還在。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type DemoUser = { id: string; name: string; unit: string };

export const DEMO_USERS: DemoUser[] = [
  { id: "wang", name: "王小明", unit: "法制局 訴願審議科" },
  { id: "li", name: "李美華", unit: "法制局 訴願審議科" },
  { id: "chen", name: "陳建宏", unit: "法制局 訴願審議科" },
];

/** 案件列表的承辦人欄寫死顯示這個人（後端沒有承辦人欄位，前端不接）。 */
export const DEFAULT_ASSIGNEE = "王小明";

type AuthContextValue = {
  user: DemoUser | null;
  hydrated: boolean;
  login: (user: DemoUser) => void;
  logout: () => void;
};

const STORAGE_KEY = "suyuan-demo-user";
const AuthContext = createContext<AuthContextValue | null>(null);

function readStored(): DemoUser | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<DemoUser>;
    if (typeof parsed.name !== "string" || !parsed.name) return null;
    return {
      id: typeof parsed.id === "string" ? parsed.id : parsed.name,
      name: parsed.name,
      unit: typeof parsed.unit === "string" ? parsed.unit : "",
    };
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<DemoUser | null>(null);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setUser(readStored());
    setHydrated(true);
  }, []);

  const login = useCallback((next: DemoUser) => {
    setUser(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      // 私密視窗等情況存不進去也沒關係，這一次 session 還是登入的
    }
  }, []);

  const logout = useCallback(() => {
    setUser(null);
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // 同上
    }
  }, []);

  const value = useMemo(() => ({ user, hydrated, login, logout }), [user, hydrated, login, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
