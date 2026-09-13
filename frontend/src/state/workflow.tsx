import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

type WorkflowContextValue = {
  activeCaseId: string | null;
  hydrated: boolean;
  setActiveCase: (caseId: string | null) => void;
};

const STORAGE_KEY = "suyuan-active-case";
const WorkflowContext = createContext<WorkflowContextValue | null>(null);

export function WorkflowProvider({ children }: { children: ReactNode }) {
  const [activeCaseId, setActiveCaseId] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setActiveCaseId(window.sessionStorage.getItem(STORAGE_KEY));
    setHydrated(true);
  }, []);

  const setActiveCase = useCallback((caseId: string | null) => {
    setActiveCaseId(caseId);
    if (caseId) window.sessionStorage.setItem(STORAGE_KEY, caseId);
    else window.sessionStorage.removeItem(STORAGE_KEY);
  }, []);

  const value = useMemo(
    () => ({ activeCaseId, hydrated, setActiveCase }),
    [activeCaseId, hydrated, setActiveCase],
  );

  return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>;
}

export function useWorkflow() {
  const context = useContext(WorkflowContext);
  if (!context) throw new Error("useWorkflow must be used inside WorkflowProvider");
  return context;
}
