import { useState, useRef, useEffect } from "react";
import { sendCodexMessage } from "../api/sessionApi";

type Props = {
  sessionId: string;
  onSent?: () => void;
};

export default function CodexChatInput({ sessionId, onSent }: Props) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  }, [text]);

  const submit = async () => {
    const t = text.trim();
    if (!t || sending) return;
    setSending(true);
    setError(null);
    try {
      await sendCodexMessage(sessionId, t);
      setText("");
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
          onChange={(e) => setText(e.target.value)}
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
