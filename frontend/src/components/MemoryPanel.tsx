import { useCallback, useEffect, useRef, useState } from "react";
import { listMemory, readMemory, type MemoryFile } from "../api/sessionApi";
import { renderMarkdown } from "../lib/markdown";

/** Project-memory file browser.
 *
 *  Lists `*.md` files at `~/.claude/projects/<encoded-cwd>/memory/` and
 *  renders the selected one as Markdown. Two-column layout with a vertical
 *  splitter the user can drag to resize the file list. The splitter width
 *  persists in localStorage so it's stable across sessions on the same
 *  device.
 *
 *  Inline mode (default for the desktop bottom-toolbar tab): fills the
 *  parent container. Mobile uses the same component without `onClose`.
 */
interface MemoryPanelProps {
  sessionId: string;
  /** Mobile passes inline=true; both modes use the same render path now. */
  inline?: boolean;
  onClose?: () => void;
}

const SIDEBAR_WIDTH_KEY = "memoryPanel.sidebarWidth";
const SIDEBAR_MIN = 140;
const SIDEBAR_MAX = 480;
const SIDEBAR_DEFAULT = 220;

function loadSidebarWidth(): number {
  const v = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY) || "", 10);
  if (Number.isFinite(v) && v >= SIDEBAR_MIN && v <= SIDEBAR_MAX) return v;
  return SIDEBAR_DEFAULT;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

export function MemoryPanel({ sessionId, onClose }: MemoryPanelProps) {
  const [files, setFiles] = useState<MemoryFile[]>([]);
  const [dirPath, setDirPath] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<string>("");
  const [listLoading, setListLoading] = useState(true);
  const [contentLoading, setContentLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [contentError, setContentError] = useState<string | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState<number>(loadSidebarWidth);

  // Splitter drag state — kept in refs so we don't re-run mousemove
  // listeners on every state tick.
  const dragging = useRef(false);
  const dragStartX = useRef(0);
  const dragStartW = useRef(0);

  const reloadList = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const res = await listMemory(sessionId);
      setFiles(res.files);
      setDirPath(res.dir);
      if (res.files.length > 0) {
        // Auto-select MEMORY.md if present (the standard index), else first
        // entry alphabetically — list is already sorted by the backend.
        const memoryIdx = res.files.findIndex((f) => f.name === "MEMORY.md");
        const pick = memoryIdx >= 0 ? res.files[memoryIdx].name : res.files[0].name;
        setSelected((prev) => prev && res.files.some((f) => f.name === prev) ? prev : pick);
      } else {
        setSelected(null);
        setContent("");
      }
    } catch (e) {
      setListError(String((e as Error).message || e));
    } finally {
      setListLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    reloadList();
  }, [reloadList]);

  useEffect(() => {
    if (!selected) {
      setContent("");
      return;
    }
    let cancelled = false;
    setContentLoading(true);
    setContentError(null);
    readMemory(sessionId, selected)
      .then((r) => {
        if (!cancelled) setContent(r.content);
      })
      .catch((e) => {
        if (!cancelled) setContentError(String((e as Error).message || e));
      })
      .finally(() => {
        if (!cancelled) setContentLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, selected]);

  // Splitter drag — uses window listeners while dragging so the cursor
  // stays consistent even if it leaves the handle element.
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const dx = e.clientX - dragStartX.current;
      const next = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, dragStartW.current + dx));
      setSidebarWidth(next);
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      try {
        localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
      } catch {
        // Quota / privacy mode — splitter still works, just not persisted.
      }
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [sidebarWidth]);

  const startDrag = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    dragStartX.current = e.clientX;
    dragStartW.current = sidebarWidth;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, [sidebarWidth]);

  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", background: "var(--bg-base)", overflow: "hidden" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderBottom: "1px solid var(--border)", background: "var(--bg-surface)", flexShrink: 0 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-body)" }}>🧠 Memory</span>
        {dirPath && (
          <span title={dirPath} style={{ fontSize: 11, color: "var(--text-faint)", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>
            {dirPath}
          </span>
        )}
        <button
          onClick={reloadList}
          title="Refresh"
          style={{ background: "var(--bg-hover)", color: "var(--text-body)", fontSize: 11, padding: "2px 8px", border: "1px solid transparent", borderRadius: 4, flexShrink: 0 }}
        >
          ↻
        </button>
        {onClose && (
          <button
            onClick={onClose}
            title="Close"
            style={{ background: "var(--bg-hover)", color: "var(--text-body)", fontSize: 11, padding: "2px 8px", border: "1px solid transparent", borderRadius: 4, flexShrink: 0 }}
          >
            ✕
          </button>
        )}
      </div>

      {/* Body: list | splitter | content */}
      <div style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
        {/* File list */}
        <div style={{ width: sidebarWidth, flexShrink: 0, display: "flex", flexDirection: "column", borderRight: "1px solid var(--border)", minHeight: 0, overflow: "hidden", background: "var(--bg-base)" }}>
          {listLoading ? (
            <div style={{ padding: 10, color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
          ) : listError ? (
            <div style={{ padding: 10, color: "var(--accent-red)", fontSize: 12 }}>{listError}</div>
          ) : files.length === 0 ? (
            <div style={{ padding: 10, color: "var(--text-faint)", fontSize: 12 }}>
              No memory files.
              <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
                Memory lives at <code style={{ fontFamily: "monospace" }}>~/.claude/projects/&lt;cwd&gt;/memory/</code>.
              </div>
            </div>
          ) : (
            <div style={{ overflow: "auto", flex: 1, minHeight: 0 }}>
              {files.map((f) => {
                const isActive = f.name === selected;
                return (
                  <button
                    key={f.name}
                    onClick={() => setSelected(f.name)}
                    style={{
                      width: "100%",
                      textAlign: "left",
                      background: isActive ? "var(--bg-hover)" : "transparent",
                      color: isActive ? "var(--text-body)" : "var(--text-secondary)",
                      border: "none",
                      borderLeft: isActive ? "2px solid var(--accent-blue)" : "2px solid transparent",
                      padding: "5px 10px",
                      fontSize: 12,
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      overflow: "hidden",
                    }}
                    onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = "var(--bg-hover-subtle, rgba(255,255,255,0.03))"; }}
                    onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
                  >
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.name}</span>
                    <span style={{ fontSize: 10, color: "var(--text-faint)", flexShrink: 0 }}>{formatBytes(f.size)}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Vertical splitter — 4px hit area with a 1px visual line */}
        <div
          onMouseDown={startDrag}
          title="Drag to resize"
          style={{ width: 5, cursor: "col-resize", background: "var(--bg-hover)", flexShrink: 0 }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--border-strong)"; }}
          onMouseLeave={(e) => { if (!dragging.current) e.currentTarget.style.background = "var(--bg-hover)"; }}
        />

        {/* Markdown content */}
        <div style={{ flex: 1, minHeight: 0, minWidth: 0, overflow: "auto", padding: "12px 16px" }}>
          {contentLoading ? (
            <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
          ) : contentError ? (
            <div style={{ color: "var(--accent-red)", fontSize: 12 }}>{contentError}</div>
          ) : selected ? (
            <div
              className="conv-markdown"
              style={{ fontSize: 13, lineHeight: 1.6, color: "var(--text-body)" }}
              dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
            />
          ) : (
            <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Select a file to preview.</div>
          )}
        </div>
      </div>
    </div>
  );
}
