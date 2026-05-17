import { useState, useEffect, useCallback, useRef } from "react";
import { getConversation, attachSession, type ConversationTurn } from "../api/sessionApi";
import { WsClient } from "../lib/wsClient";

const POLL_MS = 1500;
const INITIAL_TAIL = 20;

function formatTs(ts: number): string {
  if (!ts || ts < 1_000_000) return "";  // skip line-index-based ts (cursor)
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* ── Bubble ── */
function Bubble({ turn }: { turn: ConversationTurn }) {
  const isUser = turn.role === "user";
  const ts = formatTs(turn.ts);
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: isUser ? "flex-end" : "flex-start", marginBottom: 10, padding: "0 16px" }}>
      <div style={{
        maxWidth: "78%",
        padding: "10px 14px",
        borderRadius: isUser ? "16px 16px 4px 16px" : "16px 16px 16px 4px",
        background: isUser ? "#1a4a7a" : "var(--bg-hover)",
        color: isUser ? "#cce5ff" : "var(--text-body)",
        fontSize: 13,
        lineHeight: 1.6,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}>
        {turn.text}
      </div>
      {ts && <span style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 2, paddingLeft: 2, paddingRight: 2 }}>{ts}</span>}
    </div>
  );
}

/* ── Streaming bubble ── */
function StreamingBubble({ text, compacting }: { text: string; compacting?: boolean }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", marginBottom: 10, padding: "0 16px" }}>
      {compacting && (
        <div style={{
          fontSize: 11,
          color: "var(--accent-orange, #d59f00)",
          padding: "2px 8px",
          marginBottom: 4,
          background: "color-mix(in srgb, var(--accent-orange, #d59f00) 14%, transparent)",
          border: "1px solid color-mix(in srgb, var(--accent-orange, #d59f00) 35%, transparent)",
          borderRadius: 10,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
        }}>
          <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: "50%", background: "var(--accent-orange, #d59f00)", animation: "cursor-blink 1s step-end infinite" }} />
          Compacting conversation…
        </div>
      )}
      <div style={{
        maxWidth: "85%",
        padding: "10px 14px",
        borderRadius: "16px 16px 16px 4px",
        background: "var(--bg-sidebar)",
        border: "1px solid var(--bg-hover)",
        color: "var(--text-secondary)",
        fontSize: 12,
        lineHeight: 1.5,
        fontFamily: '"Cascadia Code", Menlo, Monaco, "Courier New", monospace',
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
      }}>
        {text}
        <span style={{ display: "inline-block", marginLeft: 3, color: "var(--accent-blue)", animation: "cursor-blink 0.8s step-end infinite" }}>▍</span>
      </div>
    </div>
  );
}

/* ── BubblePane ── */
interface Props {
  sessionId: string;
  tool?: "claude" | "cursor";
}

type WsStatus = "connecting" | "connected" | "disconnected" | "error";

export function BubblePane({ sessionId, tool }: Props) {
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [pendingTurns, setPendingTurns] = useState<ConversationTurn[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const [streamingCompacting, setStreamingCompacting] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);

  const latestTsRef = useRef(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const isFirstLoad = useRef(true);
  const stickToBottom = useRef(true);

  // Input / WS state
  const [input, setInput] = useState("");
  const [wsStatus, setWsStatus] = useState<WsStatus>("connecting");
  const wsRef = useRef<WsClient | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isAtBottom = () => {
    const el = scrollRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  };

  const scrollToBottom = useCallback((smooth = false) => {
    if (!stickToBottom.current) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  const loadAllHistory = useCallback(async () => {
    if (loadingHistory) return;
    setLoadingHistory(true);
    try {
      const data = await getConversation(sessionId, 0);
      const confirmed = data.filter((t) => !t.streaming && !t.pending);
      const pending = data.filter((t) => !t.streaming && t.pending);
      const streaming = data.find((t) => t.streaming) ?? null;
      setTurns(confirmed);
      setPendingTurns(pending);
      setStreamingText(streaming?.text ?? "");
      setStreamingCompacting(!!streaming?.compacting);
      const maxTs = confirmed.reduce((m, t) => Math.max(m, t.ts ?? 0), 0);
      if (maxTs > 0) latestTsRef.current = maxTs;
      setHistoryLoaded(true);
    } catch { /* ignore */ } finally {
      setLoadingHistory(false);
    }
  }, [sessionId, loadingHistory]);

  const pollConversation = useCallback(async () => {
    try {
      const fromTs = latestTsRef.current;
      const data = fromTs === 0
        ? await getConversation(sessionId, 0, INITIAL_TAIL)
        : await getConversation(sessionId, fromTs);

      const newConfirmed = data.filter((t) => !t.streaming && !t.pending);
      const newPending = data.filter((t) => !t.streaming && t.pending);
      const latestStreaming = data.find((t) => t.streaming) ?? null;

      setPendingTurns(newPending);

      if (newConfirmed.length > 0) {
        const maxTs = newConfirmed.reduce((m, t) => Math.max(m, t.ts ?? 0), latestTsRef.current);
        if (maxTs > latestTsRef.current) latestTsRef.current = maxTs;

        setTurns((prev) => fromTs === 0 ? newConfirmed : [...prev, ...newConfirmed]);

        if (isFirstLoad.current) {
          isFirstLoad.current = false;
          requestAnimationFrame(() => scrollToBottom(false));
        } else if (newConfirmed[newConfirmed.length - 1]?.role === "assistant") {
          requestAnimationFrame(() => scrollToBottom(true));
        }
      } else if (isFirstLoad.current) {
        isFirstLoad.current = false;
        requestAnimationFrame(() => scrollToBottom(false));
      }

      if (latestStreaming) {
        setStreamingText(latestStreaming.text);
        setStreamingCompacting(!!latestStreaming.compacting);
        requestAnimationFrame(() => scrollToBottom(false));
      } else {
        setStreamingText("");
        setStreamingCompacting(false);
      }
    } catch { /* ignore */ }
  }, [sessionId, scrollToBottom]);

  // Reset state when sessionId changes
  useEffect(() => {
    latestTsRef.current = 0;
    isFirstLoad.current = true;
    stickToBottom.current = true;
    setTurns([]);
    setPendingTurns([]);
    setStreamingText("");
    setStreamingCompacting(false);
    setHistoryLoaded(false);
    setLoadingHistory(false);
    setInput("");
  }, [sessionId]);

  // Start polling
  useEffect(() => {
    pollConversation();
    pollRef.current = setInterval(pollConversation, POLL_MS);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [pollConversation]);

  // WebSocket connection for sending input
  useEffect(() => {
    let ws: WsClient | null = null;
    let cancelled = false;

    const connect = async () => {
      try {
        setWsStatus("connecting");
        const res = await Promise.race([
          attachSession(sessionId),
          new Promise<never>((_, reject) => setTimeout(() => reject(new Error("attach timeout")), 12000)),
        ]);
        if (cancelled) return;
        ws = new WsClient({
          url: res.ws_url,
          onOpen: () => { if (!cancelled) setWsStatus("connected"); },
          onOutput: () => { /* display handled by polling */ },
          onState: (state) => {
            if (state.status === "terminated" && !cancelled) setWsStatus("disconnected");
          },
          onClose: () => { if (!cancelled) setWsStatus("disconnected"); },
        });
        wsRef.current = ws;
      } catch {
        if (!cancelled) setWsStatus("error");
      }
    };

    connect();
    return () => {
      cancelled = true;
      ws?.close();
      wsRef.current = null;
    };
  }, [sessionId]);

  // Track scroll position
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = isAtBottom();
    if (!historyLoaded && !loadingHistory && el.scrollTop === 0 && turns.length > 0) {
      loadAllHistory();
    }
  }, [historyLoaded, loadingHistory, turns.length, loadAllHistory]);

  const sendPrompt = useCallback(() => {
    const text = input.trim();
    if (!text || !wsRef.current || wsStatus !== "connected") return;
    setInput("");
    wsRef.current.sendPrompt(text);
    stickToBottom.current = true;
    requestAnimationFrame(() => scrollToBottom(true));
    setTimeout(() => textareaRef.current?.focus(), 10);
  }, [input, wsStatus, scrollToBottom]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendPrompt(); }
  };

  const showLoadBanner = !historyLoaded && turns.length >= INITIAL_TAIL;
  const displayTurns = [...turns, ...pendingTurns];

  const wsColor = wsStatus === "connected" ? "var(--accent-green)" : wsStatus === "connecting" ? "#f0ad4e" : "var(--accent-red)";
  const wsLabel = wsStatus === "connected" ? "Connected" : wsStatus === "connecting" ? "Connecting…" : "Disconnected";

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, overflow: "hidden", background: "var(--bg-base)" }}>
      {/* "Load full history" banner */}
      {showLoadBanner && (
        <div style={{
          padding: "5px 12px",
          background: "var(--bg-surface)",
          borderBottom: "1px solid var(--bg-hover)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
          flexShrink: 0,
          fontSize: 12,
          color: "var(--text-muted)",
        }}>
          <span>Showing latest {INITIAL_TAIL} turns</span>
          <button
            onClick={loadAllHistory}
            disabled={loadingHistory}
            style={{ background: "var(--btn-icon-bg)", color: "var(--text-secondary)", fontSize: 11, padding: "3px 12px" }}
          >
            {loadingHistory ? "Loading…" : "↑ Load full history"}
          </button>
        </div>
      )}

      {/* Scroll area */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{ flex: 1, overflowY: "auto", paddingTop: 8, paddingBottom: 12, minHeight: 0 }}
      >
        {loadingHistory && (
          <div style={{ textAlign: "center", padding: "8px 0", color: "var(--text-faint)", fontSize: 12 }}>
            Loading history…
          </div>
        )}
        {displayTurns.length === 0 && !streamingText ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", minHeight: 120, color: "var(--text-faint)", fontSize: 13 }}>
            No conversation yet
          </div>
        ) : (
          displayTurns.map((turn, i) => <Bubble key={i} turn={turn} />)
        )}
        {streamingText && <StreamingBubble text={streamingText} compacting={streamingCompacting} />}
      </div>

      {/* Input bar */}
      <div style={{ flexShrink: 0, borderTop: "1px solid var(--bg-hover)", background: "var(--bg-surface)", padding: "8px 12px 10px" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={wsStatus === "connected" ? "Type a prompt… (Ctrl+Enter to send)" : wsLabel}
            disabled={wsStatus !== "connected"}
            rows={2}
            style={{
              flex: 1,
              background: "var(--bg-base)",
              border: "1px solid var(--text-faintest)",
              borderRadius: 10,
              padding: "8px 12px",
              color: "var(--text-body)",
              fontSize: 13,
              resize: "none",
              outline: "none",
              fontFamily: "inherit",
              lineHeight: 1.5,
              opacity: wsStatus !== "connected" ? 0.5 : 1,
            }}
          />
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4, flexShrink: 0 }}>
            <button
              onClick={sendPrompt}
              disabled={!input.trim() || wsStatus !== "connected"}
              style={{
                background: !input.trim() || wsStatus !== "connected" ? "var(--bg-hover)" : "#238636",
                color: !input.trim() || wsStatus !== "connected" ? "var(--text-faint)" : "#fff",
                border: "none",
                borderRadius: 8,
                width: 36,
                height: 36,
                fontSize: 16,
                cursor: !input.trim() || wsStatus !== "connected" ? "default" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
              title="Send (Ctrl+Enter)"
            >↑</button>
            <span style={{ fontSize: 9, color: wsColor }}>{wsLabel}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
