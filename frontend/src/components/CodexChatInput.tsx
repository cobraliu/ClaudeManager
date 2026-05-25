import { useState, useRef, useEffect } from "react";
import { sendCodexMessage } from "../api/sessionApi";

type Props = {
  sessionId: string;
  onSent?: () => void;
};

// Per-session input draft cache. Mirrors ConversationPane.tsx's pattern so
// Codex app-server sessions get the same draft-survives-refresh behavior as
// Claude/Cursor sessions. Different prefix so the two caches don't collide.
const _codexInputDrafts = new Map<string, string>();

const DRAFT_PREFIX = "codexInputDraft:v1:";
const DRAFT_TTL_MS = 10 * 60 * 1000;
const DRAFT_MAX_BYTES = 4096;
const DRAFT_HEARTBEAT_MS = 60 * 1000;

function _draftKey(sessionId: string): string { return DRAFT_PREFIX + sessionId; }

function _loadDraft(sessionId: string): string {
  try {
    const raw = localStorage.getItem(_draftKey(sessionId));
    if (!raw) return "";
    const obj = JSON.parse(raw);
    if (typeof obj?.text !== "string" || typeof obj?.updatedAt !== "number") return "";
    if (Date.now() - obj.updatedAt > DRAFT_TTL_MS) {
      try { localStorage.removeItem(_draftKey(sessionId)); } catch { /* ignore */ }
      return "";
    }
    return obj.text;
  } catch { return ""; }
}

function _saveDraft(sessionId: string, text: string): void {
  if (!text) { _clearDraft(sessionId); return; }
  let bytes: number;
  try { bytes = new TextEncoder().encode(text).length; } catch { bytes = text.length * 4; }
  if (bytes > DRAFT_MAX_BYTES) {
    try { localStorage.removeItem(_draftKey(sessionId)); } catch { /* ignore */ }
    return;
  }
  try {
    localStorage.setItem(_draftKey(sessionId), JSON.stringify({ text, updatedAt: Date.now() }));
  } catch { /* quota or disabled — ignore */ }
}

function _clearDraft(sessionId: string): void {
  try { localStorage.removeItem(_draftKey(sessionId)); } catch { /* ignore */ }
}

export default function CodexChatInput({ sessionId, onSent }: Props) {
  const [text, setText] = useState(() => {
    const inMem = _codexInputDrafts.get(sessionId);
    if (inMem !== undefined) return inMem;
    const persisted = _loadDraft(sessionId);
    if (persisted) _codexInputDrafts.set(sessionId, persisted);
    return persisted;
  });
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  }, [text]);

  // Heartbeat: refresh updatedAt while the user stares at a draft so a
  // long-running compose doesn't get reaped at 10min boundary.
  useEffect(() => {
    if (!text) return;
    const id = setInterval(() => { _saveDraft(sessionId, text); }, DRAFT_HEARTBEAT_MS);
    return () => clearInterval(id);
  }, [sessionId, text]);

  const submit = async () => {
    const t = text.trim();
    if (!t || sending) return;
    setSending(true);
    setError(null);
    try {
      await sendCodexMessage(sessionId, t);
      setText("");
      _codexInputDrafts.delete(sessionId);
      _clearDraft(sessionId);
      onSent?.();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSending(false);
    }
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submit();
    }
  };

  const onChange = (val: string) => {
    setText(val);
    if (val) {
      _codexInputDrafts.set(sessionId, val);
      _saveDraft(sessionId, val);
    } else {
      _codexInputDrafts.delete(sessionId);
      _clearDraft(sessionId);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: 8,
        background: "var(--bg-panel)",
        borderTop: "1px solid var(--border)",
      }}
    >
      {error && (
        <div style={{ fontSize: 11, color: "#ef4444" }}>{error}</div>
      )}
      <div style={{ display: "flex", gap: 6, alignItems: "flex-end" }}>
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKey}
          placeholder="Message Codex (Ctrl/⌘+Enter to send)…"
          rows={2}
          style={{
            flex: 1,
            resize: "none",
            background: "var(--bg-main)",
            color: "var(--text-body)",
            border: "1px solid var(--border)",
            borderRadius: 5,
            padding: "6px 8px",
            fontSize: 13,
            fontFamily: "inherit",
            outline: "none",
            minHeight: 40,
            maxHeight: 240,
          }}
        />
        <button
          onClick={submit}
          disabled={sending || !text.trim()}
          style={{
            background: "var(--accent-blue)",
            color: "#fff",
            border: "none",
            padding: "8px 14px",
            borderRadius: 5,
            cursor: sending || !text.trim() ? "not-allowed" : "pointer",
            opacity: sending || !text.trim() ? 0.5 : 1,
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
