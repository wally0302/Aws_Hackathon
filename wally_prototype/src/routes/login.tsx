import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { LogIn, Scale } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DEMO_USERS, useAuth, type DemoUser } from "@/state/auth";

export const Route = createFileRoute("/login")({
  head: () => ({ meta: [{ title: "登入｜訴願快速拆解平台" }] }),
  component: LoginPage,
});

/** demo 帳號 chip 填進去的假密碼；只是為了讓表單看起來像真的 */
const DEMO_PASSWORD = "demo1234";

function resolveUser(account: string): DemoUser {
  const key = account.trim();
  const found = DEMO_USERS.find((u) => u.id === key || u.name === key);
  // 找不到就用輸入的字當名字——demo 時打錯字不要被擋在門外
  return found ?? { id: key, name: key, unit: "法制局" };
}

function LoginPage() {
  const navigate = useNavigate();
  const { user, hydrated, login } = useAuth();
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  // 已登入的人直接進工作台
  useEffect(() => {
    if (hydrated && user) void navigate({ to: "/" });
  }, [hydrated, user, navigate]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!account.trim()) {
      setError("請輸入帳號");
      return;
    }
    if (!password) {
      setError("請輸入密碼");
      return;
    }
    setError(null);
    login(resolveUser(account));
    void navigate({ to: "/" });
  };

  const fill = (u: DemoUser) => {
    setAccount(u.id);
    setPassword(DEMO_PASSWORD);
    setError(null);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-secondary/40 px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <span className="rounded-xl bg-primary p-3 text-primary-foreground shadow-sm">
            <Scale className="h-6 w-6" aria-hidden />
          </span>
          <h1 className="text-xl font-semibold tracking-wide">訴願快速拆解平台</h1>
          <p className="text-sm text-muted-foreground">新北市政府法制局</p>
        </div>

        <Card>
          <CardHeader className="pb-4">
            <CardTitle className="text-base">承辦人登入</CardTitle>
            <CardDescription>請使用機關帳號登入承辦工作台</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={submit} className="space-y-4" noValidate>
              <div className="space-y-1.5">
                <Label htmlFor="account">帳號</Label>
                <Input
                  id="account"
                  autoComplete="username"
                  value={account}
                  onChange={(e) => setAccount(e.target.value)}
                  placeholder="員工帳號"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="password">密碼</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                />
              </div>
              {error ? <p className="text-sm text-destructive">{error}</p> : null}
              {/* SSR 送到瀏覽器、React 還沒接手之前按下去會變成原生表單送出（整頁重載到
                  /login?），所以 hydrate 完才開放按鈕 */}
              <Button type="submit" className="w-full" disabled={!hydrated}>
                <LogIn className="h-4 w-4" aria-hidden />
                登入
              </Button>
            </form>

            <div className="mt-5 border-t pt-4">
              <p className="mb-2 text-xs text-muted-foreground">示範帳號（點一下自動填入）</p>
              <div className="flex flex-wrap gap-2">
                {DEMO_USERS.map((u) => (
                  <button
                    key={u.id}
                    type="button"
                    disabled={!hydrated}
                    onClick={() => fill(u)}
                    className="inline-flex items-center gap-1.5 rounded-full border bg-background px-3 py-1 text-xs font-medium transition-colors hover:border-primary/50 hover:bg-secondary"
                  >
                    <span className="flex h-4 w-4 items-center justify-center rounded-full bg-primary text-[10px] text-primary-foreground">
                      {u.name.charAt(0)}
                    </span>
                    {u.name}
                  </button>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>

        <p className="mt-4 text-center text-[11px] text-muted-foreground">
          示範環境：登入僅供操作者識別，不驗證密碼
        </p>
      </div>
    </div>
  );
}
