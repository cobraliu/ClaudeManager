import { useState, useEffect, useRef, useCallback } from "react";
import {
  getGitBranches,
  gitCheckoutBranch,
  getActiveCwdSessions,
  GitCheckoutConflictError,
  type ActiveCwdSession,
  type GitBranchInfo,
} from "../api/sessionApi";

interface Props {
  sessionId: string;
  /** Bumped whenever an external action (commit/checkout) should re-fetch state. */
  refreshKey?: number;
  /** Called after a successful branch switch so consumers can refresh their data. */
  onBranchChanged?: (branch: string) => void;
  /** Compact = small button suitable for the FILES header. */
  compact?: boolean;
}

export function GitBranchPicker({ sessionId, refreshKey, onBranchChanged, compact = true }: Props) {
  const [info, setInfo] = useState<GitBranchInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [pendingBranch, setPendingBranch] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(() => {
    getGitBranches(sessionId).then(setInfo).catch(() => setInfo({ current: "", local: [] }));
  }, [sessionId]);

  useEffect(() => { reload(); }, [reload, refreshKey]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  const current = info?.current ?? "";
  const branches = (info?.local ?? []).filter(b =>
    !filter.trim() || b.toLowerCase().includes(filter.trim().toLowerCase())
  );

  const handlePick = (branch: string) => {
    if (branch === current) { setOpen(false); return; }
    setOpen(false);
    setPendingBranch(branch);
  };

  const onSuccess = (branch: string) => {
    setPendingBranch(null);
    reload();
    onBranchChanged?.(branch);
  };

  const btnStyle: React.CSSProperties = compact
    ? { display: "inline-flex", alignItems: "center", gap: 4, background: "var(--bg-hover)", border: "1px solid var(--text-faintest)", borderRadius: 4, padding: "1px 6px", fontSize: 11, color: "var(--text-secondary)", cursor: "pointer", maxWidth: 200, overflow: "hidden" }
    : { display: "inline-flex", alignItems: "center", gap: 4, background: "var(--bg-hover)", border: "1px solid var(--text-faintest)", borderRadius: 4, padding: "3px 10px", fontSize: 12, color: "var(--text-secondary)", cursor: "pointer", maxWidth: 260 };

  return (
    <div ref={containerRef} style={{ position: "relative", display: "inline-block" }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={btnStyle}
        title={current ? `Branch: ${current}` : "Not on a branch"}
        disabled={!info || info.local.length === 0}
      >
        <svg width={10} height={10} viewBox="0 0 16 16" fill="currentColor" style={{ flexShrink: 0 }}>
          <path d="M11.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122V6A2.5 2.5 0 0110 8.5H6a1 1 0 00-1 1v1.128a2.251 2.251 0 11-1.5 0V5.372a2.25 2.25 0 111.5 0v1.836A2.492 2.492 0 016 7h4a1 1 0 001-1v-.628A2.25 2.25 0 019.5 3.25zM4.25 12a.75.75 0 100 1.5.75.75 0 000-1.5zM3.5 3.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0z" />
        </svg>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
          {current || "(detached)"}
        </span>
        {info?.dirty && <span title="Working tree has uncommitted changes" style={{ color: "var(--accent-amber)", fontSize: 10 }}>●</span>}
        <span style={{ color: "var(--text-faint)", fontSize: 9 }}>▾</span>
      </button>

      {open && info && info.local.length > 0 && (
        <div style={{ position: "absolute", top: "100%", left: 0, marginTop: 2, zIndex: 100, background: "var(--bg-surface)", border: "1px solid var(--border-strong)", borderRadius: 6, boxShadow: "0 4px 12px rgba(0,0,0,0.4)", minWidth: 220, maxWidth: 320 }}>
          <div style={{ padding: 6, borderBottom: "1px solid var(--bg-hover)" }}>
            <input
              autoFocus
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter branches..."
              style={{ width: "100%", background: "var(--bg-base)", border: "1px solid var(--text-faintest)", borderRadius: 4, padding: "3px 6px", color: "var(--text-body)", fontSize: 11, outline: "none" }}
            />
          </div>
          <div style={{ maxHeight: 280, overflowY: "auto", padding: "4px 0" }}>
            {branches.length === 0 ? (
              <div style={{ padding: "6px 10px", fontSize: 11, color: "var(--text-faint)" }}>No matches</div>
            ) : branches.map(b => (
              <div
                key={b}
                onClick={() => handlePick(b)}
                style={{ padding: "4px 10px", fontSize: 12, fontFamily: "monospace", cursor: "pointer", display: "flex", alignItems: "center", gap: 6, background: b === current ? "rgba(88,166,255,0.12)" : "transparent", color: b === current ? "var(--accent-blue)" : "var(--text-body)" }}
                onMouseEnter={(e) => { if (b !== current) (e.currentTarget as HTMLDivElement).style.background = "var(--bg-hover)"; }}
                onMouseLeave={(e) => { if (b !== current) (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
              >
                <span style={{ width: 10, textAlign: "center" }}>{b === current ? "✓" : ""}</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{b}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {pendingBranch && (
        <BranchCheckoutConfirm
          sessionId={sessionId}
          branch={pendingBranch}
          onCancel={() => setPendingBranch(null)}
          onDone={() => onSuccess(pendingBranch)}
        />
      )}
    </div>
  );
}

/* ─── Branch checkout confirm — also reused by Revert (see ConfirmAffectingChangeModal) ─── */
function BranchCheckoutConfirm({
  sessionId, branch, onCancel, onDone,
}: {
  sessionId: string;
  branch: string;
  onCancel: () => void;
  onDone: () => void;
}) {
  const [active, setActive] = useState<ActiveCwdSession[] | null>(null);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Set after the first attempt comes back with a conflict; switches the UI
  // to a discard-prompt mode and the action button to "Discard & Checkout".
  const [conflict, setConflict] = useState<{ files: string[] } | null>(null);

  useEffect(() => {
    getActiveCwdSessions(sessionId).then(r => setActive(r.sessions)).catch(() => setActive([]));
  }, [sessionId]);

  const hasActive = (active?.length ?? 0) > 0;
  const requireAck = hasActive;
  const canProceed = !requireAck || ack;

  const handleConfirm = async () => {
    setBusy(true);
    setErr(null);
    try {
      await gitCheckoutBranch(sessionId, branch, /* force_discard */ conflict !== null);
      onDone();
    } catch (e) {
      if (e instanceof GitCheckoutConflictError) {
        setConflict({ files: e.conflict.conflicting_files });
        setErr(null);
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", zIndex: 6000, display: "flex", alignItems: "center", justifyContent: "center" }}
      onClick={busy ? undefined : onCancel}
    >
      <div
        style={{ width: 480, maxWidth: "92vw", background: "var(--bg-base)", border: "1px solid var(--border-strong)", borderRadius: 8, display: "flex", flexDirection: "column", overflow: "hidden" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--bg-hover)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-body)" }}>Checkout branch</span>
          <button onClick={onCancel} disabled={busy} style={{ background: "var(--text-faintest)", color: "var(--text-secondary)", fontSize: 12, padding: "3px 8px" }}>✕</button>
        </div>
        <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10, fontSize: 12, color: "var(--text-body)" }}>
          <div>
            Switching to branch <span style={{ fontFamily: "monospace", color: "var(--accent-blue)" }}>{branch}</span>
            {conflict === null && (
              <span style={{ color: "var(--text-muted)" }}> · uncommitted edits will be carried over if they don't conflict.</span>
            )}
          </div>

          {conflict && (
            <div style={{ background: "rgba(248, 81, 73, 0.12)", border: "1px solid var(--accent-red)", borderRadius: 4, padding: "8px 10px", color: "var(--text-body)", fontSize: 12, display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ color: "var(--accent-red)", fontWeight: 600 }}>⚠ Local changes would be overwritten by checkout</div>
              {conflict.files.length > 0 && (
                <div style={{ maxHeight: 140, overflowY: "auto", fontFamily: "monospace", fontSize: 11, color: "var(--text-secondary)" }}>
                  {conflict.files.map(f => <div key={f}>{f}</div>)}
                </div>
              )}
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                Commit or stash these first to keep them. Continuing will <b>discard them</b> (git reset --hard + clean -fd).
              </div>
            </div>
          )}

          <div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>
              Other sessions currently editing this working directory:
            </div>
            {active === null ? (
              <div style={{ fontSize: 12, color: "var(--text-faint)" }}>Checking…</div>
            ) : active.length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--accent-green)" }}>✓ No active sessions on this cwd.</div>
            ) : (
              <div style={{ background: "var(--bg-surface)", border: "1px solid var(--accent-red)", borderRadius: 4, padding: 8, display: "flex", flexDirection: "column", gap: 4 }}>
                {active.map(s => (
                  <div key={s.id} style={{ display: "flex", gap: 8, fontSize: 12, fontFamily: "monospace" }}>
                    <span style={{ color: "var(--accent-red)" }}>● {s.status}</span>
                    <span style={{ color: "var(--text-body)" }}>{s.name}</span>
                    <span style={{ color: "var(--text-faint)" }}>({s.tool})</span>
                  </div>
                ))}
                <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
                  Stop these sessions first, or acknowledge below that you understand the working tree may change under them.
                </div>
              </div>
            )}
          </div>

          {requireAck && (
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12, color: "var(--text-body)" }}>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              I understand the active session(s) may break.
            </label>
          )}

          {err && <div style={{ fontSize: 12, color: "var(--accent-red)" }}>{err}</div>}
        </div>
        <div style={{ padding: "10px 14px", borderTop: "1px solid var(--bg-hover)", display: "flex", justifyContent: "flex-end", gap: 6 }}>
          <button onClick={onCancel} disabled={busy} style={{ background: "var(--text-faintest)", color: "var(--text-secondary)", fontSize: 12, padding: "5px 12px" }}>Cancel</button>
          <button
            disabled={!canProceed || busy}
            onClick={handleConfirm}
            style={{
              background: !canProceed ? "var(--bg-hover)" : conflict ? "var(--accent-red)" : "var(--accent-blue)",
              color: !canProceed ? "var(--text-faint)" : "#fff",
              fontSize: 12, padding: "5px 14px",
            }}
          >
            {busy ? "Checking out…" : conflict ? "Discard & Checkout" : "Checkout"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─── Generic confirm modal: warn about active sessions before any destructive change.
 *    Reused for Revert (action=revert). Pure UI; caller supplies the action handler. */
export function ConfirmAffectingChangeModal({
  sessionId, title, description, actionLabel, busyLabel, onCancel, onConfirm,
}: {
  sessionId: string;
  title: string;
  description: React.ReactNode;
  actionLabel: string;
  busyLabel?: string;
  onCancel: () => void;
  onConfirm: () => Promise<void> | void;
}) {
  const [active, setActive] = useState<ActiveCwdSession[] | null>(null);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getActiveCwdSessions(sessionId).then(r => setActive(r.sessions)).catch(() => setActive([]));
  }, [sessionId]);

  const hasActive = (active?.length ?? 0) > 0;
  const requireAck = hasActive;
  const canProceed = !requireAck || ack;

  const handleConfirm = async () => {
    setBusy(true);
    setErr(null);
    try {
      await onConfirm();
    } catch (e) {
      setErr(String(e));
      setBusy(false);
      return;
    }
    setBusy(false);
  };

  return (
    <div
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", zIndex: 6000, display: "flex", alignItems: "center", justifyContent: "center" }}
      onClick={busy ? undefined : onCancel}
    >
      <div
        style={{ width: 480, maxWidth: "92vw", background: "var(--bg-base)", border: "1px solid var(--border-strong)", borderRadius: 8, display: "flex", flexDirection: "column", overflow: "hidden" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--bg-hover)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-body)" }}>{title}</span>
          <button onClick={onCancel} disabled={busy} style={{ background: "var(--text-faintest)", color: "var(--text-secondary)", fontSize: 12, padding: "3px 8px" }}>✕</button>
        </div>
        <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10, fontSize: 12, color: "var(--text-body)" }}>
          <div>{description}</div>

          <div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>
              Other sessions currently editing this working directory:
            </div>
            {active === null ? (
              <div style={{ fontSize: 12, color: "var(--text-faint)" }}>Checking…</div>
            ) : active.length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--accent-green)" }}>✓ No active sessions on this cwd.</div>
            ) : (
              <div style={{ background: "var(--bg-surface)", border: "1px solid var(--accent-red)", borderRadius: 4, padding: 8, display: "flex", flexDirection: "column", gap: 4 }}>
                {active.map(s => (
                  <div key={s.id} style={{ display: "flex", gap: 8, fontSize: 12, fontFamily: "monospace" }}>
                    <span style={{ color: "var(--accent-red)" }}>● {s.status}</span>
                    <span style={{ color: "var(--text-body)" }}>{s.name}</span>
                    <span style={{ color: "var(--text-faint)" }}>({s.tool})</span>
                  </div>
                ))}
                <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
                  Stop these sessions first, or acknowledge below.
                </div>
              </div>
            )}
          </div>

          {requireAck && (
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12, color: "var(--text-body)" }}>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              I understand this may interfere with the active session(s).
            </label>
          )}

          {err && <div style={{ fontSize: 12, color: "var(--accent-red)" }}>{err}</div>}
        </div>
        <div style={{ padding: "10px 14px", borderTop: "1px solid var(--bg-hover)", display: "flex", justifyContent: "flex-end", gap: 6 }}>
          <button onClick={onCancel} disabled={busy} style={{ background: "var(--text-faintest)", color: "var(--text-secondary)", fontSize: 12, padding: "5px 12px" }}>Cancel</button>
          <button
            disabled={!canProceed || busy}
            onClick={handleConfirm}
            style={{ background: canProceed ? "var(--accent-red)" : "var(--bg-hover)", color: canProceed ? "#fff" : "var(--text-faint)", fontSize: 12, padding: "5px 14px" }}
          >
            {busy ? (busyLabel ?? "Working…") : actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
