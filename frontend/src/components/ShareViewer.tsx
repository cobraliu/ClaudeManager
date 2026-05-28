import { useCallback, useEffect, useRef, useState } from "react";
import {
  getPublicShareMeta,
  getPublicShareMessages,
  type RawMessage,
  type ShareType,
} from "../api/sessionApi";
import { renderConversationBody, LIGHT_STYLE, DARK_STYLE } from "../lib/exportChat";
import { ShareFilesTab } from "./ShareFilesTab";

const PERMANENT_EXPIRES = 2147483647;
const POLL_MS = 1500;
const PAGE = 100;
const NEAR_BOTTOM_PX = 600;
type Theme = "light" | "dark";

/* Page-frame background for the area outside the centered 920px column
 * (index.css paints html/#root with the app's dark var; override per theme). */
const PAGE_BG: Record<Theme, string> = { light: "#fafafa", dark: "#1a1a1a" };

/* Per-share key: a reader's manual toggle overrides the creator-set
 * default_theme, but only for that share. */
const themeKey = (hash: string) => `cm_share_theme:${hash}`;

function savedTheme(hash: string): Theme | null {
  const s = localStorage.getItem(themeKey(hash));
  return s === "light" || s === "dark" ? s : null;
}

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

  const bodyRef = useRef<HTMLDivElement | null>(null);
  const loadedRef = useRef<RawMessage[]>([]);
  const totalRef = useRef(0);
  const busyRef = useRef(false);

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
        if (!themeOverriddenRef.current && (m.default_theme === "light" || m.default_theme === "dark")) {
          setTheme(m.default_theme);
        }
      })
      .catch(() => { /* messages fetch surfaces the real error */ });
    return () => { cancelled = true; };
  }, [hash]);

  // Fetch the next forward page (oldest→newest) and append. `force` re-checks
  // the server even when everything is already loaded (used by full-sync poll).
  const fetchForward = useCallback(async (force: boolean): Promise<void> => {
    if (busyRef.current) return;
    const loadedLen = loadedRef.current.length;
    if (!force && totalRef.current > 0 && loadedLen >= totalRef.current) return;
    busyRef.current = true;
    try {
      const data = await getPublicShareMessages(hash, loadedLen, PAGE);
      totalRef.current = data.total;
      setTotal(data.total);
      setTitle((prev) => data.title || prev);
      setExpiresAt(data.expires_at);
      if (data.messages.length > 0) {
        const wasAtBottom = nearBottom();
        loadedRef.current = loadedRef.current.concat(data.messages);
        setLoadedCount(loadedRef.current.length);
        const html = await renderConversationBody(loadedRef.current);
        setBodyHtml(html);
        if (shareType === "full" && wasAtBottom) {
          requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
        }
      }
    } finally {
      busyRef.current = false;
    }
  }, [hash, shareType]);

  // Initial load.
  useEffect(() => {
    let cancelled = false;
    loadedRef.current = [];
    totalRef.current = 0;
    setLoading(true);
    setError(null);
    fetchForward(true)
      .catch((e) => { if (!cancelled) setError(String(e?.message || e || "加载失败")); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash]);

  // Infinite scroll: load the next page as the reader nears the bottom.
  useEffect(() => {
    if (error) return;
    const onScroll = () => { if (nearBottom()) void fetchForward(false); };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [error, fetchForward]);

  // Full-sync polling — pick up newly appended messages (limited shares are frozen).
  useEffect(() => {
    if (shareType !== "full" || error) return;
    const id = window.setInterval(() => { void fetchForward(true); }, POLL_MS);
    return () => window.clearInterval(id);
  }, [shareType, error, fetchForward]);

  // Re-wire copy/expand interactions whenever the rendered body changes.
  useEffect(() => {
    if (!bodyRef.current) return;
    return attachInteractions(bodyRef.current);
  }, [bodyHtml]);

  // Auto-fill: if a freshly rendered page doesn't fill the viewport (so the
  // reader can't scroll to trigger the next one), keep loading until it does.
  useEffect(() => {
    if (error || loading) return;
    const id = requestAnimationFrame(() => {
      if (loadedRef.current.length < totalRef.current && nearBottom()) void fetchForward(false);
    });
    return () => cancelAnimationFrame(id);
  }, [bodyHtml, error, loading, fetchForward]);

  const toggleTheme = () => setTheme((t) => {
    const next = t === "dark" ? "light" : "dark";
    themeOverriddenRef.current = true;
    try { localStorage.setItem(themeKey(hash), next); } catch { /* ignore */ }
    return next;
  });

  const expandAll = () => bodyRef.current?.querySelectorAll("details").forEach((d) => { (d as HTMLDetailsElement).open = true; });
  const collapseAll = () => bodyRef.current?.querySelectorAll("details").forEach((d) => { (d as HTMLDetailsElement).open = false; });

  if (error) {
    return (
      <div style={{ maxWidth: 920, margin: "0 auto", padding: "80px 20px", textAlign: "center", color: "#888", fontFamily: "sans-serif" }}>
        <h1 style={{ fontSize: 18, color: "#c0392b" }}>分享已失效或不存在</h1>
        <p style={{ fontSize: 13 }}>This share link is invalid or has expired.</p>
      </div>
    );
  }

  const remaining = total - loadedCount;

  return (
    <div>
      <header>
        <h1>{title}</h1>
        <div className="meta">
          {shareType === "full" ? (
            <span>🟢 实时同步</span>
          ) : (
            <span>⏸ 截止于 {cutoffTs ? fmtTime(cutoffTs) : "—"}</span>
          )}
          {expiresAt != null && <span> · {fmtExpiry(expiresAt)}</span>}
          {total > 0 && <span> · 共 {total} 条</span>}
        </div>
        <div className="toolbar">
          {(!hasFiles || activeTab === "chat") && (
            <>
              <button type="button" onClick={expandAll}>Expand all</button>
              <button type="button" onClick={collapseAll}>Collapse all</button>
            </>
          )}
          <button type="button" onClick={toggleTheme} title="切换深色 / 浅色">
            {theme === "dark" ? "☀️ 浅色" : "🌙 深色"}
          </button>
        </div>
        {hasFiles && (
          <div className="share-tabs" style={{ display: "flex", gap: 6, marginTop: 12 }}>
            {(["chat", "files"] as const).map((id) => {
              const on = activeTab === id;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setActiveTab(id)}
                  style={{
                    fontSize: 13, padding: "6px 16px", borderRadius: 6, cursor: "pointer",
                    border: `1px solid ${on ? (theme === "dark" ? "#58a6ff" : "#2563eb") : (theme === "dark" ? "#444" : "#ddd")}`,
                    background: on ? (theme === "dark" ? "rgba(88,166,255,0.15)" : "rgba(37,99,235,0.08)") : "transparent",
                    color: on ? (theme === "dark" ? "#58a6ff" : "#2563eb") : (theme === "dark" ? "#aaa" : "#666"),
                  }}
                >
                  {id === "chat" ? "💬 Chat" : "📁 Files"}
                </button>
              );
            })}
          </div>
        )}
      </header>

      <div style={{ display: hasFiles && activeTab === "files" ? "block" : "none" }}>
        {hasFiles && activeTab === "files" && <ShareFilesTab hash={hash} theme={theme} />}
      </div>

      <div style={{ display: hasFiles && activeTab === "files" ? "none" : "block" }}>
        {loading && !bodyHtml ? (
          <div style={{ color: "#888", fontSize: 13, fontFamily: "sans-serif" }}>加载中…</div>
        ) : (
          <>
            <div ref={bodyRef} dangerouslySetInnerHTML={{ __html: bodyHtml }} />
            {remaining > 0 && (
              <div style={{ textAlign: "center", color: "#888", fontSize: 12, padding: "16px 0", fontFamily: "sans-serif" }}>
                下滑加载更多（剩余 {remaining} 条）…
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
