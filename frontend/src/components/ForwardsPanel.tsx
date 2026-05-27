import { useEffect, useMemo, useRef, useState } from "react";
import { apiPath } from "../lib/baseUrl";

/** Forwards panel — browse a localStorage-backed list of reverse-proxy
 *  shortcuts and preview the selected one in an iframe inside this pane.
 *
 *  Each shortcut maps to GET /serverforward/{port}/{path}. The path is
 *  user-editable so a shortcut can target a specific sub-page of the
 *  upstream dev server (e.g. {port: 8000, path: "/docs"}).
 *
 *  Shortcuts are stored client-side only — they are per-browser, never
 *  synced to the backend, since ports are a machine-local concept.
 */
interface Shortcut {
  id: string;
  label: string;
  port: number;
  path: string;
}

interface Props {
  onClose?: () => void;
}

const STORAGE_KEY = "forwardsPanel.shortcuts.v1";
const SELECTED_KEY = "forwardsPanel.selected.v1";

function loadShortcuts(): Shortcut[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((s): s is Shortcut =>
        s && typeof s.id === "string" && typeof s.label === "string"
        && Number.isInteger(s.port) && typeof s.path === "string"
      );
  } catch {
    return [];
  }
}

function saveShortcuts(list: Shortcut[]): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch { /* ignore */ }
}

function normalizePath(p: string): string {
  const trimmed = (p || "").trim();
  if (!trimmed) return "/";
  return trimmed.startsWith("/") ? trimmed : "/" + trimmed;
}

function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

function buildSrc(s: Shortcut): string {
  const cleanPath = s.path.startsWith("/") ? s.path.slice(1) : s.path;
  return apiPath(`/serverforward/${s.port}/${cleanPath}`);
}

export function ForwardsPanel({ onClose }: Props) {
  const [shortcuts, setShortcuts] = useState<Shortcut[]>(loadShortcuts);
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    try { return localStorage.getItem(SELECTED_KEY); } catch { return null; }
  });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [addingNew, setAddingNew] = useState(false);
  const [draft, setDraft] = useState<{ label: string; port: string; path: string }>({ label: "", port: "", path: "/" });
  const [iframeKey, setIframeKey] = useState(0);
  const portInputRef = useRef<HTMLInputElement | null>(null);

  // Persist shortcuts on any change.
  useEffect(() => { saveShortcuts(shortcuts); }, [shortcuts]);

  // Persist selection so reopening the panel restores the last preview.
  useEffect(() => {
    try {
      if (selectedId) localStorage.setItem(SELECTED_KEY, selectedId);
      else localStorage.removeItem(SELECTED_KEY);
    } catch { /* ignore */ }
  }, [selectedId]);

  // Focus the port input whenever the inline form opens.
  useEffect(() => {
    if (addingNew || editingId) {
      requestAnimationFrame(() => portInputRef.current?.focus());
    }
  }, [addingNew, editingId]);

  const selected = useMemo(
    () => shortcuts.find((s) => s.id === selectedId) ?? null,
    [shortcuts, selectedId],
  );

  function startAdd() {
    setEditingId(null);
    setAddingNew(true);
    setDraft({ label: "", port: "", path: "/" });
  }

  function startEdit(s: Shortcut) {
    setAddingNew(false);
    setEditingId(s.id);
    setDraft({ label: s.label, port: String(s.port), path: s.path });
  }

  function cancelDraft() {
    setAddingNew(false);
    setEditingId(null);
  }

  function saveDraft() {
    const port = parseInt(draft.port, 10);
    if (!Number.isFinite(port) || port < 1 || port > 65535) return;
    const label = draft.label.trim() || `:${port}`;
    const path = normalizePath(draft.path);
    if (addingNew) {
      const s: Shortcut = { id: newId(), label, port, path };
      setShortcuts((prev) => [...prev, s]);
      setSelectedId(s.id);
      setAddingNew(false);
    } else if (editingId) {
      setShortcuts((prev) => prev.map((x) => x.id === editingId ? { ...x, label, port, path } : x));
      setEditingId(null);
    }
  }

  function removeShortcut(id: string) {
    setShortcuts((prev) => prev.filter((s) => s.id !== id));
    if (selectedId === id) setSelectedId(null);
    if (editingId === id) setEditingId(null);
  }

  function openInNewTab(s: Shortcut) {
    window.open(buildSrc(s), "_blank", "noopener,noreferrer");
  }

  const formValid = (() => {
    const p = parseInt(draft.port, 10);
    return Number.isFinite(p) && p >= 1 && p <= 65535;
  })();

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-base)", overflow: "hidden" }}>
      {/* Header bar */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, padding: "6px 10px", borderBottom: "1px solid var(--bg-hover)", flexShrink: 0, fontSize: 12 }}>
        <span style={{ color: "var(--text-faint)", marginRight: 4 }}>Forwards</span>
        {shortcuts.length === 0 && (
          <span style={{ color: "var(--text-faint)", fontStyle: "italic" }}>no shortcuts yet</span>
        )}
        {shortcuts.map((s) => {
          const active = s.id === selectedId;
          return (
            <button
              key={s.id}
              onClick={() => { setSelectedId(s.id); setIframeKey((k) => k + 1); }}
              title={`http://127.0.0.1:${s.port}${s.path}`}
              style={{
                fontSize: 11,
                padding: "2px 8px",
                background: active ? "var(--bg-hover)" : "transparent",
                color: active ? "var(--text-body)" : "var(--text-faint)",
                border: "1px solid " + (active ? "var(--text-faint)" : "var(--bg-hover)"),
                borderRadius: 4,
                cursor: "pointer",
                whiteSpace: "nowrap",
              }}
            >
              {s.label} <span style={{ opacity: 0.6 }}>:{s.port}</span>
            </button>
          );
        })}
        <button
          onClick={startAdd}
          title="Add forward shortcut"
          style={{ fontSize: 11, padding: "2px 8px", background: "transparent", color: "var(--text-faint)", border: "1px dashed var(--bg-hover)", borderRadius: 4, cursor: "pointer" }}
        >
          + Add
        </button>
        <div style={{ flex: 1 }} />
        {selected && (
          <>
            <span title={buildSrc(selected)} style={{ fontSize: 11, color: "var(--text-faint)", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {buildSrc(selected)}
            </span>
            <button
              onClick={() => setIframeKey((k) => k + 1)}
              title="Reload iframe"
              style={{ fontSize: 11, padding: "2px 8px", background: "transparent", color: "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: "pointer" }}
            >
              ↻
            </button>
            <button
              onClick={() => openInNewTab(selected)}
              title="Open in new tab"
              style={{ fontSize: 11, padding: "2px 8px", background: "transparent", color: "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: "pointer" }}
            >
              ↗
            </button>
            <button
              onClick={() => startEdit(selected)}
              title="Edit shortcut"
              style={{ fontSize: 11, padding: "2px 8px", background: "transparent", color: "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: "pointer" }}
            >
              ✎
            </button>
            <button
              onClick={() => { if (confirm(`Delete "${selected.label}"?`)) removeShortcut(selected.id); }}
              title="Delete shortcut"
              style={{ fontSize: 11, padding: "2px 8px", background: "transparent", color: "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: "pointer" }}
            >
              ✕
            </button>
          </>
        )}
        {onClose && (
          <button
            onClick={onClose}
            title="Close panel"
            style={{ fontSize: 11, padding: "2px 8px", background: "transparent", color: "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: "pointer", marginLeft: 4 }}
          >
            Close
          </button>
        )}
      </div>

      {/* Inline add/edit form */}
      {(addingNew || editingId) && (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, padding: "6px 10px", borderBottom: "1px solid var(--bg-hover)", flexShrink: 0, fontSize: 12, background: "var(--bg-card, var(--bg-base))" }}>
          <span style={{ color: "var(--text-faint)" }}>{addingNew ? "New" : "Edit"}:</span>
          <input
            ref={portInputRef}
            type="number"
            min={1}
            max={65535}
            placeholder="port"
            value={draft.port}
            onChange={(e) => setDraft((d) => ({ ...d, port: e.target.value }))}
            onKeyDown={(e) => { if (e.key === "Enter" && formValid) saveDraft(); if (e.key === "Escape") cancelDraft(); }}
            style={{ width: 80, fontSize: 12, padding: "2px 6px", background: "var(--bg-base)", color: "var(--text-body)", border: "1px solid var(--bg-hover)", borderRadius: 4 }}
          />
          <input
            type="text"
            placeholder="label"
            value={draft.label}
            onChange={(e) => setDraft((d) => ({ ...d, label: e.target.value }))}
            onKeyDown={(e) => { if (e.key === "Enter" && formValid) saveDraft(); if (e.key === "Escape") cancelDraft(); }}
            style={{ width: 160, fontSize: 12, padding: "2px 6px", background: "var(--bg-base)", color: "var(--text-body)", border: "1px solid var(--bg-hover)", borderRadius: 4 }}
          />
          <input
            type="text"
            placeholder="/path"
            value={draft.path}
            onChange={(e) => setDraft((d) => ({ ...d, path: e.target.value }))}
            onKeyDown={(e) => { if (e.key === "Enter" && formValid) saveDraft(); if (e.key === "Escape") cancelDraft(); }}
            style={{ flex: 1, minWidth: 100, fontSize: 12, padding: "2px 6px", background: "var(--bg-base)", color: "var(--text-body)", border: "1px solid var(--bg-hover)", borderRadius: 4 }}
          />
          <button
            disabled={!formValid}
            onClick={saveDraft}
            style={{ fontSize: 11, padding: "2px 10px", background: formValid ? "var(--bg-hover)" : "transparent", color: formValid ? "var(--text-body)" : "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: formValid ? "pointer" : "not-allowed" }}
          >
            Save
          </button>
          <button
            onClick={cancelDraft}
            style={{ fontSize: 11, padding: "2px 10px", background: "transparent", color: "var(--text-faint)", border: "1px solid var(--bg-hover)", borderRadius: 4, cursor: "pointer" }}
          >
            Cancel
          </button>
        </div>
      )}

      {/* Iframe area */}
      <div style={{ flex: 1, minHeight: 0, position: "relative", background: "#fff" }}>
        {selected ? (
          <iframe
            key={`${selected.id}-${iframeKey}`}
            src={buildSrc(selected)}
            title={selected.label}
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", border: "none", background: "#fff" }}
          />
        ) : (
          <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faint)", fontSize: 13, padding: 24, textAlign: "center", background: "var(--bg-base)" }}>
            {shortcuts.length === 0
              ? "Add a forward shortcut to preview a local dev server here."
              : "Select a shortcut above to preview."}
          </div>
        )}
      </div>
    </div>
  );
}
