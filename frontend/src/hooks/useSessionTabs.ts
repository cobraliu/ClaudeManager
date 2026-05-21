import { useCallback, useEffect, useState } from "react";

export type TabKind = "file" | "git" | "jsonl";

export interface FileTab {
  id: string;
  kind: "file";
  path: string;
  viewMode: "full" | "diff" | "split";
  noDiff?: boolean;
}

export interface GitTab {
  id: string;
  kind: "git";
}

export interface JsonlTab {
  id: string;
  kind: "jsonl";
}

export type TabEntry = FileTab | GitTab | JsonlTab;

interface TabsState {
  tabs: TabEntry[];
  activeId: string | null;
}

const EMPTY: TabsState = { tabs: [], activeId: null };

function storageKey(sid: string) {
  return `cm_session_tabs_v1_${sid}`;
}

function load(sid: string): TabsState {
  try {
    const raw = localStorage.getItem(storageKey(sid));
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.tabs)) return EMPTY;
    const tabs: TabEntry[] = parsed.tabs.filter((t: TabEntry) =>
      t && typeof t.id === "string" &&
      (t.kind === "file" || t.kind === "git" || t.kind === "jsonl")
    );
    const activeId = typeof parsed.activeId === "string" && tabs.some(t => t.id === parsed.activeId)
      ? parsed.activeId
      : (tabs[0]?.id ?? null);
    return { tabs, activeId };
  } catch {
    return EMPTY;
  }
}

function save(sid: string, s: TabsState) {
  try {
    if (s.tabs.length === 0) {
      localStorage.removeItem(storageKey(sid));
    } else {
      localStorage.setItem(storageKey(sid), JSON.stringify(s));
    }
  } catch {
    // ignore quota / privacy errors — tabs just won't persist
  }
}

function genId(): string {
  return `t_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function useSessionTabs(sessionId: string | null) {
  const [state, setState] = useState<TabsState>(EMPTY);

  useEffect(() => {
    if (!sessionId) { setState(EMPTY); return; }
    setState(load(sessionId));
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    save(sessionId, state);
  }, [sessionId, state]);

  const openFileTab = useCallback((path: string, viewMode: "full" | "diff" | "split", noDiff?: boolean) => {
    setState(prev => {
      const existing = prev.tabs.find(t => t.kind === "file" && t.path === path);
      if (existing) {
        const updated = prev.tabs.map(t =>
          t.id === existing.id && t.kind === "file" ? { ...t, viewMode, noDiff } : t
        );
        return { tabs: updated, activeId: existing.id };
      }
      const id = genId();
      const tab: FileTab = { id, kind: "file", path, viewMode, noDiff };
      return { tabs: [...prev.tabs, tab], activeId: id };
    });
  }, []);

  const openSingleton = useCallback((kind: "git" | "jsonl") => {
    setState(prev => {
      const existing = prev.tabs.find(t => t.kind === kind);
      if (existing) return { ...prev, activeId: existing.id };
      const id = genId();
      const tab: TabEntry = kind === "git" ? { id, kind } : { id, kind };
      return { tabs: [...prev.tabs, tab], activeId: id };
    });
  }, []);

  const openGitTab = useCallback(() => openSingleton("git"), [openSingleton]);
  const openJsonlTab = useCallback(() => openSingleton("jsonl"), [openSingleton]);

  const closeTab = useCallback((id: string) => {
    setState(prev => {
      const idx = prev.tabs.findIndex(t => t.id === id);
      if (idx === -1) return prev;
      const next = prev.tabs.filter(t => t.id !== id);
      let activeId = prev.activeId;
      if (activeId === id) {
        activeId = next.length === 0
          ? null
          : (next[Math.min(idx, next.length - 1)]?.id ?? null);
      }
      return { tabs: next, activeId };
    });
  }, []);

  const activate = useCallback((id: string) => {
    setState(prev => prev.activeId === id ? prev : { ...prev, activeId: id });
  }, []);

  const activeTab: TabEntry | null = state.tabs.find(t => t.id === state.activeId) ?? null;

  return {
    tabs: state.tabs,
    activeId: state.activeId,
    activeTab,
    openFileTab,
    openGitTab,
    openJsonlTab,
    closeTab,
    activate,
  };
}
