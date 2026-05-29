import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  getPublicShareMeta,
  getPublicShareMessages,
  postPublicSharePrompt,
  type RawMessage,
  type ShareType,
} from "../api/sessionApi";
import { renderConversationBody, LIGHT_STYLE, DARK_STYLE } from "../lib/exportChat";
import { ShareFilesTab } from "./ShareFilesTab";

const PERMANENT_EXPIRES = 2147483647;
const POLL_MS = 1500;
const PAGE = 100;
const NEAR_BOTTOM_PX = 600;
const NEAR_TOP_PX = 600;
type Theme = "light" | "dark";
/* asc = top-anchored, oldest first, scroll DOWN for newer (default full/limited).
 * desc = bottom-anchored chat-style: still chronological, but opens at the latest
 * message and scrolls UP for older (default for chat shares). */
type Order = "asc" | "desc";

/* Page-frame background for the area outside the centered content column
 * (index.css paints html/#root with the app's dark var; override per theme). */
const PAGE_BG: Record<Theme, string> = { light: "#fafafa", dark: "#1a1a1a" };

/* Per-share key: a reader's manual toggle overrides the creator-set
 * default_theme, but only for that share. */
const themeKey = (hash: string) => `cm_share_theme:${hash}`;

function savedTheme(hash: string): Theme | null {
  const s = localStorage.getItem(themeKey(hash));
  return s === "light" || s === "dark" ? s : null;
}

/* Per-share reading order, same override semantics as theme. */
const orderKey = (hash: string) => `cm_share_order:${hash}`;

function savedOrder(hash: string): Order | null {
  const s = localStorage.getItem(orderKey(hash));
  return s === "asc" || s === "desc" ? s : null;
}

/* Widen the page cap past exportChat's 1080px reading column — mainly so the
 * Files tab gets room on PC. body is block-level, so max-width:1920 resolves to
 * min(1920px, viewport width). */
const PAGE_MAX = 1920;

interface Props {
  hash: string;
  shareType: ShareType;
}

function fmtTime(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleString();
}

function fmtExpiry(epochSec: number): string {
  if (epochSec >= PERMANENT_EXPIRES) return "永久有效";
  return `失效于 ${fmtTime(epochSec)}`;
}

function nearBottom(): boolean {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - NEAR_BOTTOM_PX;
}

/** Minimal copy/expand interactions for the injected static HTML body. */
function attachInteractions(root: HTMLElement): () => void {
  const fallbackCopy = (text: string) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    document.body.removeChild(ta);
  };
  const copyText = (text: string) => {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
    } else {
      fallbackCopy(text);
    }
  };
  const flash = (btn: HTMLElement) => {
    const orig = btn.textContent;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1100);
  };
  const preText = (pre: Element | null | undefined): string => {
    if (!pre) return "";
    // Numbered output: copy the code column only, dropping the line-number gutter.
    if (pre.classList.contains("numbered")) {
      return Array.from(pre.querySelectorAll(".num-code"))
        .map((c) => (c as HTMLElement).innerText)
        .join("\n");
    }
    return (pre as HTMLElement).innerText;
  };

  const onClick = (e: Event) => {
    const t = e.target as HTMLElement | null;
    if (!t || !t.classList || !t.classList.contains("copy-btn")) return;
    e.preventDefault();
    e.stopPropagation();
    const src = t.getAttribute("data-copy-source");
    let text = "";
    if (src === "next-pre") {
      text = preText(t.parentElement?.parentElement?.querySelector("pre"));
    } else if (src === "next-md") {
      const md = t.parentElement?.parentElement?.querySelector(".md, .plan-body");
      if (md) text = (md as HTMLElement).innerText;
    } else if (src === "diff") {
      const block = t.closest(".diff-block");
      if (block) {
        const lines: string[] = [];
        block.querySelectorAll(".diff-table tr").forEach((tr) => {
          if (tr.classList.contains("diff-skip")) {
            lines.push("@@ " + (tr.textContent || "").trim() + " @@");
            return;
          }
          const td = tr.querySelector(".diff-text");
          if (!td) return;
          const sign = tr.classList.contains("diff-add") ? "+" : tr.classList.contains("diff-del") ? "-" : " ";
          let inner = td.textContent || "";
          if (inner.length > 0) inner = inner.slice(1);
          lines.push(sign + inner);
        });
        text = lines.join("\n");
      }
    } else {
      text = preText(t.closest("details, div")?.querySelector("pre"));
    }
    if (text) { copyText(text); flash(t); }
  };
  root.addEventListener("click", onClick);

  root.querySelectorAll("pre.conv-code-block").forEach((pre) => {
    if (pre.querySelector(".copy-btn")) return;
    const btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.style.position = "absolute";
    btn.style.top = "4px";
    btn.style.right = "4px";
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const code = pre.querySelector("code");
      const text = code ? (code as HTMLElement).innerText : (pre as HTMLElement).innerText;
      copyText(text);
      flash(btn);
    });
    pre.appendChild(btn);
  });

  return () => root.removeEventListener("click", onClick);
}

/* Bottom composer for chat shares — injects a chat-mode prompt into the live
 * session. No optimistic bubble: the 1.5s poll pulls the real message back, so
 * a successful send just clears the box and waits. Uses viewer-static colors
 * (not app CSS vars) since the share viewer runs outside <App>. */
function ShareChatComposer({ hash, theme, sessionAlive }: { hash: string; theme: Theme; sessionAlive: boolean }) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const dark = theme === "dark";
  const c = {
    barBg: dark ? "rgba(26,26,26,0.96)" : "rgba(250,250,250,0.96)",
    border: dark ? "#444" : "#ddd",
    inputBg: dark ? "#0d1117" : "#fff",
    inputText: dark ? "#e6e6e6" : "#222",
    accent: dark ? "#58a6ff" : "#2563eb",
    accentText: "#fff",
    muted: dark ? "#888" : "#999",
    err: dark ? "#ff7b72" : "#c0392b",
  };

  const disabled = !sessionAlive;

  const send = useCallback(async () => {
    const value = text.trim();
    if (!value || sending) return;
    setSending(true);
    setHint(null);
    try {
      await postPublicSharePrompt(hash, value);
      setText("");
      if (taRef.current) taRef.current.style.height = "auto";
    } catch (e) {
      const msg = String((e as Error)?.message || e || "");
      if (msg.includes("auq_pending")) setHint("会话正在等待确认，请稍后再试");
      else if (msg.includes("offline")) setHint("会话已离线");
      else setHint("发送失败，请重试");
    } finally {
      setSending(false);
    }
  }, [text, sending, hash]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const autoGrow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  return (
    <div
      style={{
        position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 50,
        background: c.barBg, borderTop: `1px solid ${c.border}`,
        backdropFilter: "blur(6px)",
        padding: "10px 12px",
        boxSizing: "border-box",
      }}
    >
      <div style={{ maxWidth: 1080, margin: "0 auto" }}>
        {hint && (
          <div style={{ fontSize: 12, color: c.err, marginBottom: 6, fontFamily: "sans-serif" }}>{hint}</div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <textarea
            ref={taRef}
            value={text}
            disabled={disabled || sending}
            onChange={(e) => { setText(e.target.value); autoGrow(e.target); }}
            onKeyDown={onKeyDown}
            placeholder={disabled ? "会话已离线，无法发送" : "输入消息，Enter 发送 / Shift+Enter 换行"}
            rows={1}
            style={{
              flex: 1, resize: "none", minHeight: 38, maxHeight: 160,
              padding: "8px 10px", borderRadius: 8,
              border: `1px solid ${c.border}`, background: c.inputBg, color: c.inputText,
              fontSize: 14, lineHeight: 1.4, fontFamily: "sans-serif",
              outline: "none", boxSizing: "border-box",
              opacity: disabled ? 0.6 : 1,
            }}
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={disabled || sending || text.trim().length === 0}
            style={{
              flex: "0 0 auto", height: 38, padding: "0 18px", borderRadius: 8,
              border: "none", cursor: disabled || sending || text.trim().length === 0 ? "default" : "pointer",
              background: disabled || sending || text.trim().length === 0 ? c.muted : c.accent,
              color: c.accentText, fontSize: 14, fontFamily: "sans-serif", whiteSpace: "nowrap",
            }}
          >
            {sending ? "发送中…" : "发送"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* Fixed top-left tool drawer (collapsed by default). Holds the controls that
 * used to live in the header row — Chat/Files tabs, theme toggle, reading-order
 * toggle — so the header is just title + meta. Runs outside <App>, so colors are
 * static per theme rather than CSS vars. */
function ShareToolDrawer({
  theme, order, hasFiles, activeTab,
  onToggleTheme, onToggleOrder, onSelectTab,
}: {
  theme: Theme;
  order: Order;
  hasFiles: boolean;
  activeTab: "chat" | "files";
  onToggleTheme: () => void;
  onToggleOrder: () => void;
  onSelectTab: (t: "chat" | "files") => void;
}) {
  const [open, setOpen] = useState(false);
  const dark = theme === "dark";
  const c = {
    bg: dark ? "rgba(26,26,26,0.97)" : "rgba(255,255,255,0.97)",
    border: dark ? "#444" : "#ddd",
    text: dark ? "#aaa" : "#666",
    accent: dark ? "#58a6ff" : "#2563eb",
    accentBg: dark ? "rgba(88,166,255,0.15)" : "rgba(37,99,235,0.08)",
  };
  const btn = (on: boolean): CSSProperties => ({
    fontSize: 13, padding: "6px 14px", borderRadius: 6, cursor: "pointer",
    border: `1px solid ${on ? c.accent : c.border}`,
    background: on ? c.accentBg : "transparent",
    color: on ? c.accent : c.text,
    textAlign: "left", whiteSpace: "nowrap", fontFamily: "sans-serif",
  });

  return (
    <div style={{ position: "fixed", top: 12, left: 12, zIndex: 60, fontFamily: "sans-serif" }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="选项"
        aria-expanded={open}
        style={{
          fontSize: 16, lineHeight: 1, width: 38, height: 38, borderRadius: 8, cursor: "pointer",
          border: `1px solid ${c.border}`, background: c.bg, color: c.text,
          backdropFilter: "blur(6px)", boxShadow: "0 1px 4px rgba(0,0,0,0.12)",
        }}
      >
        {open ? "✕" : "☰"}
      </button>
      {open && (
        <div
          style={{
            display: "flex", flexDirection: "column", gap: 6, marginTop: 6, padding: 8,
            minWidth: 132, borderRadius: 8, border: `1px solid ${c.border}`, background: c.bg,
            backdropFilter: "blur(6px)", boxShadow: "0 2px 10px rgba(0,0,0,0.18)",
          }}
        >
          {hasFiles && (["chat", "files"] as const).map((id) => (
            <button key={id} type="button" style={btn(activeTab === id)} onClick={() => onSelectTab(id)}>
              {id === "chat" ? "💬 Chat" : "📁 Files"}
            </button>
          ))}
          <button type="button" style={btn(false)} onClick={onToggleOrder} title="切换阅读顺序">
            {order === "desc" ? "🔽 最新在下" : "🔼 最早在上"}
          </button>
          <button type="button" style={btn(false)} onClick={onToggleTheme} title="切换深色 / 浅色">
            {theme === "dark" ? "☀️ 浅色" : "🌙 深色"}
          </button>
        </div>
      )}
    </div>
  );
}

export function ShareViewer({ hash, shareType }: Props) {
  const [title, setTitle] = useState("Shared conversation");
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [cutoffTs, setCutoffTs] = useState<number | null>(null);
  const [total, setTotal] = useState(0);
  const [loadedCount, setLoadedCount] = useState(0);
  const [bodyHtml, setBodyHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(() => savedTheme(hash) ?? "light");
  const themeOverriddenRef = useRef(savedTheme(hash) !== null);
  const [hasFiles, setHasFiles] = useState(false);
  const [activeTab, setActiveTab] = useState<"chat" | "files">("chat");
  const [sessionAlive, setSessionAlive] = useState(true);
  // Reading order: desc (chat-style, opens at latest) defaults for chat shares,
  // asc (oldest-first, opens at top) for full/limited. Reader override persists.
  const [order, setOrder] = useState<Order>(() => savedOrder(hash) ?? (shareType === "chat" ? "desc" : "asc"));
  const [showJumpBtn, setShowJumpBtn] = useState(false);

  // chat shares are live (poll for new messages, follow bottom) like full,
  // and additionally expose a composer that injects prompts into the session.
  const isChat = shareType === "chat";
  const live = shareType === "full" || shareType === "chat";

  const bodyRef = useRef<HTMLDivElement | null>(null);
  // loadedRef is ALWAYS chronological (oldest→newest); `order` only changes which
  // end we open at and which direction we paginate. headOffsetRef is the absolute
  // index of loadedRef[0] in the full list, so we can window from either end:
  // loadNewer appends from (headOffset+len), loadOlder prepends down to 0.
  const loadedRef = useRef<RawMessage[]>([]);
  const headOffsetRef = useRef(0);
  const totalRef = useRef(0);
  const busyRef = useRef(false);
  // Scroll-position anchor: scrollHeight captured just before a prepend so we can
  // keep the reader's viewport fixed over the same content after older messages
  // are inserted above (window-scrolled, so we adjust window scroll by the delta).
  const prependAnchorRef = useRef<number | null>(null);
  // Live-follow only kicks in after the reader scrolls once, so an asc page opens
  // at the top instead of being yanked to the latest message.
  const userScrolledRef = useRef(false);

  // Free the global fixed-viewport layout (index.css pins html/body/#root to
  // height:100%;overflow:hidden) so the transcript scrolls as a normal page,
  // and inject the chosen light/dark theme. Re-runs when the reader toggles.
  useEffect(() => {
    const style = document.createElement("style");
    style.setAttribute("data-share-viewer", "");
    const themeCss = theme === "dark" ? DARK_STYLE : LIGHT_STYLE;
    style.textContent =
      `html,body,#root{height:auto!important;overflow:visible!important;}\n` +
      `${themeCss}\n` +
      `html,#root{background:${PAGE_BG[theme]}!important;}\n` +
      `body{max-width:${PAGE_MAX}px!important;}\n` +
      `:root{color-scheme:${theme};}`;
    document.head.appendChild(style);
    return () => { style.remove(); };
  }, [theme]);

  // One-time meta (cutoff for the limited badge; title/expiry also come from
  // messages). Seeds the viewer theme from the share's creator-set default,
  // unless this reader already toggled it for this share.
  useEffect(() => {
    let cancelled = false;
    getPublicShareMeta(hash)
      .then((m) => {
        if (cancelled) return;
        setTitle(m.title || "Shared conversation");
        setExpiresAt(m.expires_at);
        setCutoffTs(m.cutoff_ts ?? null);
        setHasFiles(Boolean(m.has_files));
        if (typeof m.session_alive === "boolean") setSessionAlive(m.session_alive);
        if (!themeOverriddenRef.current && (m.default_theme === "light" || m.default_theme === "dark")) {
          setTheme(m.default_theme);
        }
      })
      .catch(() => { /* messages fetch surfaces the real error */ });
    return () => { cancelled = true; };
  }, [hash]);

  // Append the next forward page (toward newer) and follow the bottom if the
  // reader is already there. `force` re-checks the server even when the newest
  // end is fully loaded (used by the live poll to pick up new arrivals).
  const loadNewer = useCallback(async (force: boolean): Promise<void> => {
    if (busyRef.current) return;
    const nextOffset = headOffsetRef.current + loadedRef.current.length;
    if (!force && totalRef.current > 0 && nextOffset >= totalRef.current) return;
    busyRef.current = true;
    try {
      const data = await getPublicShareMessages(hash, nextOffset, PAGE);
      totalRef.current = data.total;
      setTotal(data.total);
      setTitle((prev) => data.title || prev);
      setExpiresAt(data.expires_at);
      if (typeof data.session_alive === "boolean") setSessionAlive(data.session_alive);
      if (data.messages.length > 0) {
        const wasAtBottom = nearBottom();
        loadedRef.current = loadedRef.current.concat(data.messages);
        setLoadedCount(loadedRef.current.length);
        const html = await renderConversationBody(loadedRef.current);
        setBodyHtml(html);
        if (live && wasAtBottom && userScrolledRef.current) {
          requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
        }
      }
    } finally {
      busyRef.current = false;
    }
  }, [hash, live]);

  // Prepend the previous page (toward older) and pin the viewport over the same
  // content via prependAnchorRef (restored in the layout effect below).
  const loadOlder = useCallback(async (): Promise<void> => {
    if (busyRef.current) return;
    const head = headOffsetRef.current;
    if (head <= 0) return;
    busyRef.current = true;
    try {
      const start = Math.max(0, head - PAGE);
      const data = await getPublicShareMessages(hash, start, head - start);
      totalRef.current = data.total;
      setTotal(data.total);
      if (data.messages.length > 0) {
        prependAnchorRef.current = document.documentElement.scrollHeight;
        loadedRef.current = data.messages.concat(loadedRef.current);
        headOffsetRef.current = start;
        setLoadedCount(loadedRef.current.length);
        const html = await renderConversationBody(loadedRef.current);
        setBodyHtml(html);
      }
    } finally {
      busyRef.current = false;
    }
  }, [hash]);

  // Initial load — re-runs when hash or order changes. desc opens at the latest
  // message (tail fetch, scrolled to bottom); asc opens at the top (offset 0).
  useEffect(() => {
    let cancelled = false;
    loadedRef.current = [];
    headOffsetRef.current = 0;
    totalRef.current = 0;
    userScrolledRef.current = false;
    setLoadedCount(0);
    setBodyHtml("");
    setLoading(true);
    setError(null);
    const run = async () => {
      if (order === "desc") {
        const data = await getPublicShareMessages(hash, 0, PAGE, true);
        if (cancelled) return;
        totalRef.current = data.total;
        setTotal(data.total);
        setTitle((prev) => data.title || prev);
        setExpiresAt(data.expires_at);
        if (typeof data.session_alive === "boolean") setSessionAlive(data.session_alive);
        loadedRef.current = data.messages;
        headOffsetRef.current = Math.max(0, data.total - data.messages.length);
        setLoadedCount(data.messages.length);
        const html = await renderConversationBody(loadedRef.current);
        if (cancelled) return;
        setBodyHtml(html);
        requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
      } else {
        await loadNewer(true);
      }
    };
    run()
      .catch((e) => { if (!cancelled) setError(String(e?.message || e || "加载失败")); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash, order]);

  // Infinite scroll in both directions: near the bottom pulls newer, near the
  // top pulls older. Also drives the jump-to-bottom button's visibility.
  useEffect(() => {
    if (error) return;
    const onScroll = () => {
      userScrolledRef.current = true;
      if (nearBottom()) void loadNewer(false);
      if (window.scrollY < NEAR_TOP_PX) void loadOlder();
      setShowJumpBtn(!nearBottom());
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [error, loadNewer, loadOlder]);

  // Live polling — pick up newly appended messages (full + chat; limited shares
  // are frozen).
  useEffect(() => {
    if (!live || error) return;
    const id = window.setInterval(() => { void loadNewer(true); }, POLL_MS);
    return () => window.clearInterval(id);
  }, [live, error, loadNewer]);

  // Restore scroll position after a prepend so the reader doesn't jump.
  useLayoutEffect(() => {
    if (prependAnchorRef.current != null) {
      const delta = document.documentElement.scrollHeight - prependAnchorRef.current;
      if (delta !== 0) window.scrollBy(0, delta);
      prependAnchorRef.current = null;
    }
  }, [bodyHtml]);

  // Re-wire copy/expand interactions whenever the rendered body changes.
  useEffect(() => {
    if (!bodyRef.current) return;
    return attachInteractions(bodyRef.current);
  }, [bodyHtml]);

  // Auto-fill: if a freshly rendered page doesn't fill the viewport (so the
  // reader can't scroll to trigger the next one), keep loading toward the
  // open end until it does — older for desc, newer for asc.
  useEffect(() => {
    if (error || loading) return;
    const id = requestAnimationFrame(() => {
      const notScrollable = document.documentElement.scrollHeight <= window.innerHeight + 50;
      if (order === "desc") {
        if (headOffsetRef.current > 0 && notScrollable) void loadOlder();
      } else if (headOffsetRef.current + loadedRef.current.length < totalRef.current && (notScrollable || nearBottom())) {
        void loadNewer(false);
      }
    });
    return () => cancelAnimationFrame(id);
  }, [bodyHtml, error, loading, order, loadNewer, loadOlder]);

  const toggleTheme = () => setTheme((t) => {
    const next = t === "dark" ? "light" : "dark";
    themeOverriddenRef.current = true;
    try { localStorage.setItem(themeKey(hash), next); } catch { /* ignore */ }
    return next;
  });

  const toggleOrder = () => setOrder((o) => {
    const next = o === "asc" ? "desc" : "asc";
    try { localStorage.setItem(orderKey(hash), next); } catch { /* ignore */ }
    return next;
  });

  const jumpToBottom = () => {
    void loadNewer(true);
    requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
  };

  if (error) {
    return (
      <div style={{ maxWidth: 920, margin: "0 auto", padding: "80px 20px", textAlign: "center", color: "#888", fontFamily: "sans-serif" }}>
        <h1 style={{ fontSize: 18, color: "#c0392b" }}>分享已失效或不存在</h1>
        <p style={{ fontSize: 13 }}>This share link is invalid or has expired.</p>
      </div>
    );
  }

  // olderRemaining = messages above the loaded window (toward the start).
  // newerRemaining = messages below it (toward the latest).
  const olderRemaining = headOffsetRef.current;
  const newerRemaining = Math.max(0, total - headOffsetRef.current - loadedCount);

  return (
    <div>
      <ShareToolDrawer
        theme={theme}
        order={order}
        hasFiles={hasFiles}
        activeTab={activeTab}
        onToggleTheme={toggleTheme}
        onToggleOrder={toggleOrder}
        onSelectTab={setActiveTab}
      />

      <header>
        <h1>{title}</h1>
        <div className="meta">
          {isChat ? (
            <span>💬 Chat · 可对话{sessionAlive ? "" : "（会话已离线）"}</span>
          ) : shareType === "full" ? (
            <span>🟢 实时同步</span>
          ) : (
            <span>⏸ 截止于 {cutoffTs ? fmtTime(cutoffTs) : "—"}</span>
          )}
          {expiresAt != null && <span> · {fmtExpiry(expiresAt)}</span>}
          {total > 0 && <span> · 共 {total} 条</span>}
        </div>
      </header>

      <div style={{ display: hasFiles && activeTab === "files" ? "block" : "none" }}>
        {hasFiles && activeTab === "files" && <ShareFilesTab hash={hash} theme={theme} />}
      </div>

      <div style={{ display: hasFiles && activeTab === "files" ? "none" : "block", paddingBottom: isChat ? 88 : 0 }}>
        {loading && !bodyHtml ? (
          <div style={{ color: "#888", fontSize: 13, fontFamily: "sans-serif" }}>加载中…</div>
        ) : (
          <>
            {olderRemaining > 0 && (
              <div style={{ textAlign: "center", color: "#888", fontSize: 12, padding: "16px 0", fontFamily: "sans-serif" }}>
                上滑加载更早（剩余 {olderRemaining} 条）…
              </div>
            )}
            <div ref={bodyRef} dangerouslySetInnerHTML={{ __html: bodyHtml }} />
            {newerRemaining > 0 && (
              <div style={{ textAlign: "center", color: "#888", fontSize: 12, padding: "16px 0", fontFamily: "sans-serif" }}>
                下滑加载更多（剩余 {newerRemaining} 条）…
              </div>
            )}
          </>
        )}
      </div>

      {showJumpBtn && (!hasFiles || activeTab === "chat") && (
        <button
          type="button"
          onClick={jumpToBottom}
          title="回到最新"
          style={{
            position: "fixed", right: 16, bottom: isChat ? 96 : 20, zIndex: 55,
            width: 40, height: 40, borderRadius: "50%", cursor: "pointer",
            border: `1px solid ${theme === "dark" ? "#444" : "#ddd"}`,
            background: theme === "dark" ? "#1f2428" : "#fff",
            color: theme === "dark" ? "#aaa" : "#666",
            boxShadow: "0 2px 8px rgba(0,0,0,0.2)", fontSize: 16,
          }}
        >
          ↓
        </button>
      )}

      {isChat && activeTab === "chat" && (
        <ShareChatComposer hash={hash} theme={theme} sessionAlive={sessionAlive} />
      )}
    </div>
  );
}
