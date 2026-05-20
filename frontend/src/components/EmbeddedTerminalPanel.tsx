import { useCallback, useEffect, useRef, useState } from "react";
import {
  createTerminal,
  deleteTerminal,
  issueTerminalToken,
  listTerminals,
  renameTerminal,
  type TerminalInfo,
} from "../api/sessionApi";
import { TerminalPane } from "./TerminalPane";

interface Props {
  sessionId: string | null;
  cwd?: string;
  theme: "dark" | "light";
  fontFamily?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  height: number;
  onHeightChange: (h: number) => void;
  resizeFrom?: "top" | "bottom";
  minHeight?: number;
  maxHeightVh?: number;
}

type Attached = { termId: string; wsUrl: string; name: string | null; isNamed: boolean };

const POLL_MS = 4000;

export function EmbeddedTerminalPanel({
  sessionId, cwd, theme, fontFamily,
  open, onOpenChange,
  height, onHeightChange,
  resizeFrom = "top",
  minHeight = 100,
  maxHeightVh = 70,
}: Props) {
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [attached, setAttached] = useState<Attached | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const dragging = useRef(false);
  const dragStartY = useRef(0);
  const dragStartH = useRef(0);
  const pickerRef = useRef<HTMLDivElement | null>(null);

  const refreshList = useCallback(async () => {
    if (!sessionId) return;
    try {
      const r = await listTerminals(sessionId);
      setTerminals(r.items);
    } catch {
      // swallow; periodic poll will retry
    }
  }, [sessionId]);

  const openEphemeral = useCallback(async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await createTerminal(sessionId, {});
      setAttached({ termId: r.term_id, wsUrl: r.ws_url, name: r.name, isNamed: r.is_named });
      await refreshList();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [sessionId, busy, refreshList]);

  const attachExisting = useCallback(async (term: TerminalInfo) => {
    if (!sessionId || busy) return;
    // No-op when picking the terminal that's already attached — otherwise we'd
    // tear down the working WS and re-attach with a new token for no reason.
    if (attached && attached.termId === term.term_id) {
      setPicking(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const t = await issueTerminalToken(sessionId, term.term_id);
      setAttached({ termId: term.term_id, wsUrl: t.ws_url, name: term.name, isNamed: term.is_named });
      setPicking(false);
      await refreshList();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [sessionId, busy, attached, refreshList]);

  const saveAsNamed = useCallback(async (name: string) => {
    if (!sessionId || !attached || busy) return;
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    setRenameError(null);
    try {
      const r = await renameTerminal(sessionId, attached.termId, trimmed);
      setAttached((a) => (a ? { ...a, name: r.name, isNamed: r.is_named } : a));
      setRenaming(false);
      setRenameValue("");
      await refreshList();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setRenameError(msg);
    } finally {
      setBusy(false);
    }
  }, [sessionId, attached, busy, refreshList]);

  const deleteCurrent = useCallback(async () => {
    if (!sessionId || !attached || busy) return;
    const label = attached.name ? `"${attached.name}"` : "this ephemeral terminal";
    if (!confirm(`Delete ${label}? Any running processes inside will be killed.`)) return;
    setBusy(true);
    try {
      await deleteTerminal(sessionId, attached.termId);
      setAttached(null);
      await refreshList();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [sessionId, attached, busy, refreshList]);

  // Reset all panel state when the session changes or the panel closes.
  // Without this, switching from session A to B would leave A's terminal
  // attached (and its WS streaming) because `attached` is otherwise
  // session-agnostic. The auto-open effect below then creates a fresh
  // ephemeral for the new session.
  useEffect(() => {
    setAttached(null);
    setTerminals([]);
    setError(null);
    setPicking(false);
    setRenaming(false);
  }, [sessionId, open]);

  // Auto-open an ephemeral when the panel is visible and nothing is attached.
  useEffect(() => {
    if (!open || !sessionId || attached) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await listTerminals(sessionId);
        if (cancelled) return;
        setTerminals(r.items);
        const c = await createTerminal(sessionId, {});
        if (cancelled) return;
        setAttached({ termId: c.term_id, wsUrl: c.ws_url, name: c.name, isNamed: c.is_named });
        listTerminals(sessionId).then((rr) => { if (!cancelled) setTerminals(rr.items); }).catch(() => {});
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [open, sessionId, attached]);

  // Periodic list refresh (mostly to update attach_count badges)
  useEffect(() => {
    if (!open || !sessionId) return;
    const id = setInterval(refreshList, POLL_MS);
    return () => clearInterval(id);
  }, [open, sessionId, refreshList]);

  // Close picker on outside-click / Escape.
  useEffect(() => {
    if (!picking) return;
    const onDown = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPicking(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPicking(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [picking]);

  // Drag-to-resize
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const dy = e.clientY - dragStartY.current;
      const direction = resizeFrom === "top" ? -1 : 1;
      const next = dragStartH.current + direction * dy;
      const max = Math.floor(window.innerHeight * maxHeightVh / 100);
      onHeightChange(Math.max(minHeight, Math.min(next, max)));
    };
    const onUp = () => { dragging.current = false; document.body.style.cursor = ""; };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [resizeFrom, minHeight, maxHeightVh, onHeightChange]);

  const startDrag = (e: React.MouseEvent) => {
    dragging.current = true;
    dragStartY.current = e.clientY;
    dragStartH.current = height;
    document.body.style.cursor = "row-resize";
    e.preventDefault();
  };

  if (!open) return null;

  const resizeHandle = (
    <div
      onMouseDown={startDrag}
      style={{ height: 4, cursor: "row-resize", background: "var(--bg-hover)", flexShrink: 0 }}
      onMouseEnter={e => { e.currentTarget.style.background = "var(--border-strong)"; }}
      onMouseLeave={e => { if (!dragging.current) e.currentTarget.style.background = "var(--bg-hover)"; }}
    />
  );

  const named = terminals.filter(t => t.is_named);
  const ephemeral = terminals.filter(t => !t.is_named);

  const currentLabel = attached
    ? (attached.name ? `📌 ${attached.name}` : `▶ ephemeral (${attached.termId.slice(0, 6)})`)
    : "(no terminal)";

  return (
    <div style={{
      height, flexShrink: 0,
      display: "flex", flexDirection: "column",
      background: "var(--bg-base)",
      borderTop: resizeFrom === "top" ? "1px solid var(--border)" : undefined,
      borderBottom: resizeFrom === "bottom" ? "1px solid var(--border)" : undefined,
      overflow: "hidden",
    }}>
      {resizeFrom === "top" && resizeHandle}

      {/* Header */}
      <div style={{
        padding: "4px 10px", background: "var(--bg-surface)",
        borderBottom: "1px solid var(--bg-hover)",
        display: "flex", alignItems: "center", gap: 8, flexShrink: 0, fontSize: 11,
        position: "relative",
      }}>
        {/* Picker */}
        <div ref={pickerRef} style={{ position: "relative", display: "flex" }}>
        <button
          onClick={() => setPicking(p => !p)}
          title="Switch terminal"
          disabled={!sessionId || busy}
          style={{
            background: "var(--bg-hover)", color: "var(--text-body)",
            fontSize: 11, padding: "2px 8px", lineHeight: 1,
            display: "flex", alignItems: "center", gap: 4,
          }}
        >
          <span style={{ fontFamily: "monospace" }}>{currentLabel}</span>
          <span style={{ fontSize: 9, color: "var(--text-muted)" }}>▾</span>
        </button>

        {picking && (
          <div style={{
            position: "absolute", top: "100%", left: 0, marginTop: 2,
            background: "var(--bg-surface)", border: "1px solid var(--border-strong)",
            borderRadius: 6, padding: 4, minWidth: 240, maxHeight: 280, overflowY: "auto",
            zIndex: 50, boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
          }}>
            <div style={{ fontSize: 10, color: "var(--text-faint)", padding: "4px 8px", textTransform: "uppercase", letterSpacing: 0.6 }}>Named</div>
            {named.length === 0 && (
              <div style={{ padding: "4px 10px", fontSize: 11, color: "var(--text-muted)" }}>(none yet — save current to name it)</div>
            )}
            {named.map(t => (
              <button
                key={t.term_id}
                onClick={() => attachExisting(t)}
                style={{
                  display: "flex", width: "100%", textAlign: "left", padding: "4px 8px",
                  background: attached?.termId === t.term_id ? "rgba(88,166,255,0.12)" : "transparent",
                  color: attached?.termId === t.term_id ? "var(--accent-blue)" : "var(--text-body)",
                  border: "none", fontSize: 11, gap: 6, alignItems: "center",
                  cursor: "pointer",
                }}
              >
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "monospace" }}>📌 {t.name}</span>
                {t.attach_count > 0 && (
                  <span style={{ fontSize: 9, padding: "1px 5px", background: "rgba(34,197,94,0.18)", color: "#22c55e", borderRadius: 3 }}>
                    👥{t.attach_count}
                  </span>
                )}
              </button>
            ))}

            <div style={{ fontSize: 10, color: "var(--text-faint)", padding: "4px 8px", marginTop: 4, textTransform: "uppercase", letterSpacing: 0.6 }}>Ephemeral</div>
            {ephemeral.length === 0 && (
              <div style={{ padding: "4px 10px", fontSize: 11, color: "var(--text-muted)" }}>(none)</div>
            )}
            {ephemeral.map(t => (
              <button
                key={t.term_id}
                onClick={() => attachExisting(t)}
                style={{
                  display: "flex", width: "100%", textAlign: "left", padding: "4px 8px",
                  background: attached?.termId === t.term_id ? "rgba(88,166,255,0.12)" : "transparent",
                  color: attached?.termId === t.term_id ? "var(--accent-blue)" : "var(--text-body)",
                  border: "none", fontSize: 11, gap: 6, alignItems: "center",
                  cursor: "pointer",
                }}
              >
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "monospace" }}>▶ {t.term_id.slice(0, 8)}</span>
                {t.attach_count > 0 && (
                  <span style={{ fontSize: 9, padding: "1px 5px", background: "rgba(34,197,94,0.18)", color: "#22c55e", borderRadius: 3 }}>
                    👥{t.attach_count}
                  </span>
                )}
              </button>
            ))}

            <div style={{ borderTop: "1px solid var(--bg-hover)", marginTop: 6, paddingTop: 4 }}>
              <button
                onClick={() => { setPicking(false); openEphemeral(); }}
                disabled={busy}
                style={{ display: "block", width: "100%", textAlign: "left", padding: "4px 8px", background: "transparent", border: "none", color: "var(--accent-blue)", fontSize: 11, cursor: "pointer" }}
              >+ New ephemeral terminal</button>
            </div>
          </div>
        )}
        </div>

        {/* Save (rename) — only when attached and ephemeral */}
        {attached && !attached.isNamed && !renaming && (
          <button
            onClick={() => { setRenameValue(""); setRenameError(null); setRenaming(true); }}
            disabled={busy}
            title="Save this terminal with a name (won't be auto-killed)"
            style={{ background: "var(--bg-hover)", color: "var(--text-body)", fontSize: 11, padding: "2px 8px", lineHeight: 1 }}
          >💾 Save</button>
        )}

        {attached && renaming && (
          <form
            onSubmit={(e) => { e.preventDefault(); saveAsNamed(renameValue); }}
            style={{ display: "flex", gap: 4, alignItems: "center" }}
          >
            <input
              autoFocus
              value={renameValue}
              onChange={(e) => { setRenameValue(e.target.value); if (renameError) setRenameError(null); }}
              onKeyDown={(e) => { if (e.key === "Escape") { setRenaming(false); setRenameError(null); } }}
              placeholder="terminal name"
              title={renameError ?? undefined}
              style={{
                fontSize: 11, padding: "2px 6px", width: 120,
                background: "var(--bg-base)",
                border: `1px solid ${renameError ? "var(--accent-red, #f85149)" : "var(--border)"}`,
                color: "var(--text-body)",
                borderRadius: 3,
              }}
            />
            <button type="submit" disabled={!renameValue.trim() || busy}
              style={{
                background: renameValue.trim() ? "var(--accent-blue)" : "var(--text-faintest)",
                color: "#fff", fontSize: 11, padding: "2px 8px", lineHeight: 1,
              }}
            >OK</button>
            <button type="button" onClick={() => { setRenaming(false); setRenameError(null); }}
              style={{ background: "var(--bg-hover)", color: "var(--text-muted)", fontSize: 11, padding: "2px 6px", lineHeight: 1 }}
            >✕</button>
            {renameError && (
              <span style={{ fontSize: 10, color: "var(--accent-red, #f85149)", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={renameError}>
                {renameError}
              </span>
            )}
          </form>
        )}

        {/* Delete */}
        {attached && (
          <button
            onClick={deleteCurrent}
            disabled={busy}
            title="Delete this terminal (kills tmux session)"
            style={{ background: "var(--bg-hover)", color: "var(--text-muted)", fontSize: 11, padding: "2px 8px", lineHeight: 1 }}
          >🗑</button>
        )}

        {/* Attach-count badge */}
        {attached && (() => {
          const live = terminals.find(t => t.term_id === attached.termId);
          if (!live || live.attach_count <= 1) return null;
          return (
            <span style={{ fontSize: 9, padding: "1px 5px", background: "rgba(34,197,94,0.18)", color: "#22c55e", borderRadius: 3 }}>
              👥 {live.attach_count} attached
            </span>
          );
        })()}

        <span style={{ flex: 1 }} />
        <span style={{ color: "var(--text-faint)", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 280 }}>
          {cwd || ""}
        </span>
        <button
          onClick={() => onOpenChange(false)}
          title="Hide terminal"
          style={{
            background: "var(--bg-hover)", color: "var(--text-secondary)",
            fontSize: 11, padding: "2px 8px", lineHeight: 1,
          }}
        >✕</button>
      </div>

      {/* Body */}
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {error && (
          <div style={{ padding: 12, fontSize: 12, color: "var(--text-danger, #f85149)" }}>
            {error}
          </div>
        )}
        {!sessionId && !error && (
          <div style={{ padding: 12, fontSize: 12, color: "var(--text-muted)" }}>
            Select a session to open a terminal.
          </div>
        )}
        {attached && !error && (
          <TerminalPane
            key={attached.termId + attached.wsUrl + (fontFamily || "")}
            sessionId={attached.termId}
            wsUrl={attached.wsUrl}
            scrollMode="tmux"
            theme={theme}
            onDisconnect={() => setAttached(null)}
            defaultFit
            fontFamily={fontFamily}
          />
        )}
      </div>
      {resizeFrom === "bottom" && resizeHandle}
    </div>
  );
}
