import { Link, useNavigate, useRouterState } from "@tanstack/react-router";
import {
  Archive,
  ArrowLeft,
  Bot,
  Database,
  FileCheck2,
  FilePenLine,
  FilePlus2,
  FileSearch,
  FolderOpen,
  ListTree,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Scale,
  User,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { useCase } from "@/api/hooks";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { useAuth } from "@/state/auth";
import { useWorkflow } from "@/state/workflow";

const globalItems = [
  { to: "/", label: "我的案件", icon: FileSearch },
  { to: "/new-case", label: "建立訴願案件", icon: FilePlus2 },
] as const;

const adminItems = [
  { to: "/ai-context", label: "AI Context 管理", icon: Bot },
  { to: "/preprocessing", label: "知識庫前置作業", icon: Database },
] as const;

const caseWorkflowItems = [
  { to: "/case-review", label: "文件與程序檢核", icon: FileCheck2, step: 1 },
  { to: "/step2", label: "事實與爭點", icon: ListTree, step: 2 },
  { to: "/step3", label: "決定與援引", icon: FolderOpen, step: 3 },
  { to: "/step4", label: "決定文草擬", icon: FilePenLine, step: 4 },
  { to: "/step5", label: "完成與匯出", icon: Archive, step: 5 },
] as const;

type NavItem = { to: string; label: string; icon: LucideIcon };

function NavigationItem({
  item,
  active,
  collapsed,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  collapsed: boolean;
  onNavigate?: (() => void) | undefined;
}) {
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      onClick={onNavigate}
      title={collapsed ? item.label : undefined}
      aria-label={collapsed ? item.label : undefined}
      className={cn(
        "group relative flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
        collapsed && "justify-center px-2",
        active
          ? "bg-sidebar-accent text-sidebar-accent-foreground before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-r-full before:bg-sidebar-primary"
          : "text-sidebar-foreground/70 hover:bg-sidebar-accent/70 hover:text-sidebar-foreground",
      )}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden />
      {!collapsed ? <span className="min-w-0 flex-1 truncate">{item.label}</span> : null}
    </Link>
  );
}

function SidebarNavigation({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean;
  onNavigate?: (() => void) | undefined;
}) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const insideCase = caseWorkflowItems.some((item) => item.to === pathname);
  return (
    <nav className="flex min-h-0 flex-1 flex-col px-3 py-4" aria-label="主要導覽">
      <p
        className={cn(
          "mb-2 px-3 text-[11px] font-semibold tracking-[0.12em] text-muted-foreground",
          collapsed && "sr-only",
        )}
      >
        承辦工作台
      </p>
      <div className="space-y-1">
        {globalItems.map((item) => (
          <NavigationItem
            key={item.to}
            item={item}
            active={pathname === item.to || (item.to === "/" && insideCase)}
            collapsed={collapsed}
            onNavigate={onNavigate}
          />
        ))}
      </div>
      <p
        className={cn(
          "mb-2 mt-6 px-3 text-[11px] font-semibold tracking-[0.12em] text-muted-foreground",
          collapsed && "sr-only",
        )}
      >
        系統管理
      </p>
      <div className="space-y-1">
        {adminItems.map((item) => (
          <NavigationItem
            key={item.to}
            item={item}
            active={pathname === item.to}
            collapsed={collapsed}
            onNavigate={onNavigate}
          />
        ))}
      </div>
    </nav>
  );
}

function CaseWorkflowHeader({ currentStep, onBack }: { currentStep: number; onBack: () => void }) {
  const { activeCaseId } = useWorkflow();
  const { data: activeCase } = useCase(activeCaseId);
  if (!activeCase) return null;

  return (
    <section className="sticky top-14 z-30 border-b bg-background/95 shadow-sm backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-3 px-4 py-3 xl:flex-row xl:items-center sm:px-6">
        <div className="flex min-w-0 items-start gap-3 xl:w-[36%]">
          <Link
            to="/"
            onClick={onBack}
            className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1.5 text-xs font-medium text-primary hover:bg-secondary"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
            返回案件
          </Link>
          <div className="min-w-0 border-l pl-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{activeCase.caseNo}</span>
              <span className="text-sm text-muted-foreground">{activeCase.appellant}</span>
              {activeCase.law ? (
                <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px]">
                  {activeCase.law}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <nav className="min-w-0 flex-1 overflow-x-auto" aria-label="單案審理步驟">
          <ol className="flex min-w-max items-center gap-1">
            {caseWorkflowItems.map((item) => {
              const Icon = item.icon;
              const active = item.step === currentStep;
              return (
                <li key={item.to}>
                  <Link
                    to={item.to}
                    aria-current={active ? "step" : undefined}
                    className={cn(
                      "flex h-9 items-center gap-2 rounded-md px-3 text-xs font-medium transition-colors",
                      active
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                    )}
                  >
                    <span className="flex h-5 w-5 items-center justify-center rounded-full border text-[10px]">
                      {item.step}
                    </span>
                    <Icon className="hidden h-3.5 w-3.5 2xl:block" aria-hidden />
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ol>
        </nav>
      </div>
    </section>
  );
}

function UserMenu() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  if (!user) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`目前登入：${user.name}`}
          className="inline-flex items-center gap-2 rounded-md px-2 py-1 text-xs hover:bg-primary-foreground/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-foreground/60"
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary-foreground/20 text-sm font-semibold">
            {user.name.charAt(0)}
          </span>
          <span className="hidden font-medium sm:inline">{user.name}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel className="font-normal">
          <p className="text-sm font-semibold">{user.name}</p>
          <p className="text-xs text-muted-foreground">{user.unit || "法制局"}</p>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() => {
            logout();
            void navigate({ to: "/login" });
          }}
        >
          <LogOut className="h-4 w-4" aria-hidden />
          登出
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function AppShell({
  children,
  showCaseBar,
  sectionLabel,
  caseStep,
}: {
  children: ReactNode;
  showCaseBar?: boolean;
  showWorkflowNav?: boolean;
  sectionLabel?: string;
  caseStep?: 1 | 2 | 3 | 4 | 5;
}) {
  const navigate = useNavigate();
  const { user, hydrated: authHydrated } = useAuth();
  const { activeCaseId, hydrated, setActiveCase } = useWorkflow();
  const { data: activeCase } = useCase(activeCaseId);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const redirected = useRef(false);
  const inCaseWorkspace = caseStep !== undefined;
  // 沒登入就不能進工作台。所有頁面都包 AppShell，所以守在這一處就夠。
  const needsLogin = authHydrated && !user;

  useEffect(() => {
    if (needsLogin) void navigate({ to: "/login" });
  }, [needsLogin, navigate]);

  useEffect(() => {
    setSidebarCollapsed(window.sessionStorage.getItem("app-sidebar-collapsed") === "true");
  }, []);

  useEffect(() => {
    window.sessionStorage.setItem("app-sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  useEffect(() => {
    // 沒登入的人會被上面那個 effect 送去 /login，這裡不要再疊一個「請先選案件」的 toast
    if (!authHydrated || needsLogin) return;
    if (!inCaseWorkspace || !hydrated || activeCaseId || redirected.current) return;
    redirected.current = true;
    toast.info("請先從「我的案件」選擇案件");
    void navigate({ to: "/" });
  }, [activeCaseId, hydrated, inCaseWorkspace, navigate]);

  const blockCaseContent = inCaseWorkspace && (!hydrated || !activeCaseId);
  const blockForAuth = !authHydrated || needsLogin;

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-40 flex h-14 items-center border-b border-primary/20 bg-primary text-primary-foreground shadow-sm">
        <div className="flex w-full items-center gap-3 px-4 md:px-5">
          <button
            type="button"
            onClick={() => setMobileMenuOpen(true)}
            aria-label="開啟側邊選單"
            className="rounded-md p-2 hover:bg-primary-foreground/10 md:hidden"
          >
            <Menu className="h-5 w-5" aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => setSidebarCollapsed((value) => !value)}
            aria-label={sidebarCollapsed ? "展開側邊欄" : "收合側邊欄"}
            className="hidden rounded-md p-2 hover:bg-primary-foreground/10 md:inline-flex"
          >
            {sidebarCollapsed ? (
              <PanelLeftOpen className="h-5 w-5" aria-hidden />
            ) : (
              <PanelLeftClose className="h-5 w-5" aria-hidden />
            )}
          </button>
          <Scale className="h-5 w-5 shrink-0" aria-hidden />
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold tracking-wide sm:text-base">
              訴願快速拆解平台
            </p>
            <p className="hidden text-[11px] text-primary-foreground/70 sm:block">
              新北市政府法制局
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2 text-right text-xs text-primary-foreground/85">
            {(showCaseBar || inCaseWorkspace) && activeCase ? (
              <span className="hidden items-center gap-1.5 rounded-md bg-primary-foreground/10 px-3 py-1.5 sm:inline-flex">
                <User className="h-3.5 w-3.5" aria-hidden />
                {activeCase.appellant} · {activeCase.caseNo}
              </span>
            ) : (
              <span className="hidden rounded-md bg-primary-foreground/10 px-3 py-1.5 sm:inline">
                {sectionLabel ?? "案件工作台"}
              </span>
            )}
            <UserMenu />
          </div>
        </div>
      </header>
      <div className="flex min-h-[calc(100vh-3.5rem)]">
        <aside
          className={cn(
            "sticky top-14 hidden h-[calc(100vh-3.5rem)] shrink-0 flex-col border-r bg-sidebar transition-[width] md:flex",
            sidebarCollapsed ? "w-[4.5rem]" : "w-64",
          )}
        >
          <SidebarNavigation collapsed={sidebarCollapsed} />
        </aside>
        <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
          <SheetContent side="left" className="w-72 max-w-[85vw] gap-0 p-0">
            <SheetHeader className="border-b px-5 py-5">
              <SheetTitle className="flex items-center gap-2 text-base">
                <Scale className="h-5 w-5" aria-hidden />
                訴願快速拆解平台
              </SheetTitle>
            </SheetHeader>
            <SidebarNavigation onNavigate={() => setMobileMenuOpen(false)} />
          </SheetContent>
        </Sheet>
        <main className="min-w-0 flex-1">
          {caseStep ? (
            <CaseWorkflowHeader currentStep={caseStep} onBack={() => setActiveCase(null)} />
          ) : null}
          <div className="mx-auto w-full max-w-[1600px] px-4 py-5 sm:px-6 sm:py-6">
            {blockForAuth ? (
              <div className="flex min-h-[50vh] items-center justify-center text-sm text-muted-foreground">
                {needsLogin ? "正在前往登入…" : ""}
              </div>
            ) : blockCaseContent ? (
              <div className="flex min-h-[50vh] items-center justify-center text-sm text-muted-foreground">
                正在返回案件工作台…
              </div>
            ) : (
              children
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
