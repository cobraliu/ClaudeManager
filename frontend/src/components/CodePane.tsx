import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import hljs from "highlight.js/lib/common";
import { marked } from "../lib/markdown";
import {
  getCodeChangedFiles, getCodeFile,
  listFiles, fetchRawFileBlob,
  getGitInfo,
  searchFiles, createDir, uploadFile, renameEntry, moveEntry, deleteEntry, writeFile, FileWriteConflictError,
  downloadFile,
  getFileGitLog, getFileGitShow, getFileGitDiff,
  type ChangedFile, type FileData, type FileEntry,
  type GitLogEntry,
} from "../api/sessionApi";
import { SqliteViewer, CsvViewer, ArchiveViewer, copyText, DirPicker } from "./FileEditorModal";
import { CodeMirrorEditor, type CodeMirrorEditorHandle } from "./CodeMirrorEditor";
import { GitPanel, CommitDetailModal } from "./GitPanel";
import { GitBranchPicker, GitPullButton } from "./GitBranchPicker";
import { ConfigFormatToggle } from "./ConfigFormatToggle";
import { ConfigCheckButton } from "./ConfigCheckButton";
import { ConfigValidationBanner } from "./ConfigValidationBanner";
import { detectFormat, convert, languageFor, type ConfigFormat } from "../lib/configConvert";
import downloadIcon from "../assets/download.svg";

const MAX_TRANSFER_MB = 16;
const MAX_TRANSFER_BYTES = MAX_TRANSFER_MB * 1024 * 1024;

const POLL_MS = 8000;

function humanBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

// ── useResizeWidth: tracks the clientWidth of an element via ResizeObserver ──
function useResizeWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setWidth(entry.contentRect.width);
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

// ── File icons (shared with FileEditorModal & MobilePage) ────────────────
import { FileIcon } from "./FileIcon";

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "tiff", "tif", "ico", "svg"]);
function isImage(name: string) { return IMAGE_EXTS.has(name.split(".").pop()?.toLowerCase() ?? ""); }
function isMdFile(name: string) { return name.split(".").pop()?.toLowerCase() === "md"; }

const SQLITE_EXTS = new Set(["db", "sqlite", "sqlite3"]);
function isSqliteFile(name: string) { return SQLITE_EXTS.has(name.split(".").pop()?.toLowerCase() ?? ""); }
function isPdfFile(name: string) { return name.split(".").pop()?.toLowerCase() === "pdf"; }
function isCsvFile(name: string) { const e = name.split(".").pop()?.toLowerCase() ?? ""; return e === "csv" || e === "tsv"; }
function csvDelimiter(name: string) { return name.split(".").pop()?.toLowerCase() === "tsv" ? "\t" : ","; }

const ARCHIVE_EXTS = new Set(["zip", "tar", "gz", "bz2", "xz", "tgz", "tbz2", "txz", "7z", "rar"]);
function isArchiveFile(name: string) {
  const lower = name.toLowerCase();
  if (lower.endsWith(".tar.gz") || lower.endsWith(".tar.bz2") || lower.endsWith(".tar.xz")) return true;
  return ARCHIVE_EXTS.has(lower.split(".").pop() ?? "");
}

// ── Image Viewer ──────────────────────────────────────────────────────────

function ImageViewer({ sessionId, path }: { sessionId: string; path: string }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let url: string | null = null;
    fetchRawFileBlob(sessionId, path)
      .then((u) => { url = u; setBlobUrl(u); })
      .catch((e) => setError(String(e)));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [sessionId, path]);

  if (error) return <div style={{ padding: 24, color: "var(--accent-red)", fontSize: 13 }}>{error}</div>;
  if (!blobUrl) return <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>;
  return (
    <div style={{ flex: 1, overflow: "auto", display: "flex", alignItems: "center", justifyContent: "center", padding: 16, background: "var(--bg-deep)" }}>
      <img src={blobUrl} alt={path} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", borderRadius: 4 }} />
    </div>
  );
}

// ── PDF Viewer ────────────────────────────────────────────────────────────

function PdfViewer({ sessionId, path }: { sessionId: string; path: string }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let url: string | null = null;
    fetchRawFileBlob(sessionId, path)
      .then((u) => { url = u; setBlobUrl(u); })
      .catch((e) => setError(String(e)));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [sessionId, path]);

  if (error) return <div style={{ padding: 24, color: "var(--accent-red)", fontSize: 13 }}>{error}</div>;
  if (!blobUrl) return <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>;
  return <iframe src={blobUrl} style={{ flex: 1, border: "none", background: "white", minHeight: 0 }} title={path} />;
}

// ── Markdown Viewer ───────────────────────────────────────────────────────

function MarkdownViewer({ content }: { content: string }) {
  const html = marked.parse(content, { async: false }) as string;
  return (
    <div
      className="md-preview"
      style={{
        flex: 1, overflowY: "auto", padding: "20px 28px",
        background: "var(--bg-base)", color: "var(--text-primary)",
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        fontSize: 14, lineHeight: 1.7,
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

// ── File tree ─────────────────────────────────────────────────────────────

interface DirState {
  entries: FileEntry[];
  loaded: boolean;
  loading: boolean;
  open: boolean;
}

function FileTreeDir({
  sessionId, entry, depth, selected, changed, onSelect, onEntryContextMenu, revealPath, refreshKey,
}: {
  sessionId: string;
  entry: FileEntry;
  depth: number;
  selected: string | null;
  changed: Set<string>;
  onSelect: (entry: FileEntry) => void;
  onEntryContextMenu?: (e: React.MouseEvent, entry: FileEntry) => void;
  revealPath?: string | null;
  refreshKey?: number;
}) {
  const [state, setState] = useState<DirState>({ entries: [], loaded: false, loading: false, open: depth === 0 });

  const toggle = async () => {
    setState((s) => {
      if (s.open) return { ...s, open: false };
      if (s.loaded) return { ...s, open: true };
      return { ...s, open: true };
    });
    if (!state.loaded && !state.loading) {
      setState((s) => ({ ...s, loading: true }));
      try {
        const res = await listFiles(sessionId, entry.path);
        setState((s) => ({ ...s, entries: res.entries, loaded: true, loading: false }));
      } catch {
        setState((s) => ({ ...s, loaded: true, loading: false }));
      }
    }
  };

  // Auto-expand when this dir is on the path to revealPath
  useEffect(() => {
    if (!revealPath) return;
    if (!revealPath.startsWith(entry.path + "/")) return;
    setState((s) => s.open ? s : { ...s, open: true });
    listFiles(sessionId, entry.path).then((res) => {
      setState((s) => s.loaded ? s : { ...s, entries: res.entries, loaded: true, loading: false });
    }).catch(() => {
      setState((s) => s.loaded ? s : { ...s, loaded: true, loading: false });
    });
  }, [revealPath, entry.path, sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Re-fetch when refreshKey bumps (new files may have appeared)
  useEffect(() => {
    if (refreshKey === undefined || refreshKey === 0) return;
    if (!state.loaded || !state.open) return;
    listFiles(sessionId, entry.path).then((res) => {
      setState((s) => ({ ...s, entries: res.entries }));
    }).catch(() => {});
  }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const indent = depth * 14 + 6;
  const isChanged = changed.has(entry.path);

  return (
    <div>
      <div
        onClick={toggle}
        onContextMenu={onEntryContextMenu ? (e) => onEntryContextMenu(e, entry) : undefined}
        style={{
          display: "flex", alignItems: "center", gap: 5,
          padding: `2px 8px 2px ${indent}px`,
          cursor: "pointer", userSelect: "none",
          color: isChanged ? "#fbbf24" : "var(--text-secondary)",
          fontSize: 12,
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-surface)"; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
      >
        <span style={{ fontSize: 9, color: "var(--text-muted)", minWidth: 8, textAlign: "center", flexShrink: 0 }}>
          {state.loading ? "…" : state.open ? "▾" : "▸"}
        </span>
        <FileIcon isDir isOpen={state.open} />
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {entry.name}
        </span>
        {isChanged && <span style={{ color: "var(--accent-amber)", fontSize: 9, marginLeft: "auto", paddingRight: 4, flexShrink: 0 }}>●</span>}
      </div>
      {state.open && state.loaded && (
        <div>
          {state.entries.length === 0 && (
            <div style={{ paddingLeft: indent + 26, fontSize: 11, color: "var(--text-faint)", padding: `1px 0 1px ${indent + 26}px` }}>empty</div>
          )}
          {state.entries.map((child) =>
            child.type === "dir" ? (
              <FileTreeDir
                key={child.path} sessionId={sessionId} entry={child}
                depth={depth + 1} selected={selected} changed={changed} onSelect={onSelect} onEntryContextMenu={onEntryContextMenu} revealPath={revealPath} refreshKey={refreshKey}
              />
            ) : (
              <FileTreeFile
                key={child.path} entry={child}
                depth={depth + 1} selected={selected} changed={changed} onSelect={onSelect} onEntryContextMenu={onEntryContextMenu}
              />
            )
          )}
        </div>
      )}
    </div>
  );
}

function FileTreeFile({
  entry, depth, selected, changed, onSelect, onEntryContextMenu,
}: {
  entry: FileEntry;
  depth: number;
  selected: string | null;
  changed: Set<string>;
  onSelect: (entry: FileEntry) => void;
  onEntryContextMenu?: (e: React.MouseEvent, entry: FileEntry) => void;
}) {
  const isSelected = selected === entry.path;
  const isChanged = changed.has(entry.path);
  const indent = depth * 14 + 6;

  return (
    <div
      onClick={() => onSelect(entry)}
      onContextMenu={onEntryContextMenu ? (e) => onEntryContextMenu(e, entry) : undefined}
      style={{
        display: "flex", alignItems: "center", gap: 5,
        padding: `2px 8px 2px ${indent}px`,
        cursor: "pointer", fontSize: 12,
        background: isSelected ? "var(--bg-hover)" : "transparent",
        borderLeft: isSelected ? "2px solid var(--accent-blue)" : "2px solid transparent",
        color: isChanged ? "#fbbf24" : "var(--text-secondary)",
      }}
      onMouseEnter={(e) => { if (!isSelected) e.currentTarget.style.background = "var(--bg-surface)"; }}
      onMouseLeave={(e) => { if (!isSelected) e.currentTarget.style.background = "transparent"; }}
    >
      <span style={{ minWidth: 8, flexShrink: 0 }} />
      <FileIcon name={entry.name} isDir={entry.type === "dir"} />
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
        {entry.name}
      </span>
      {isChanged && <span style={{ color: "var(--accent-amber)", fontSize: 9, flexShrink: 0, paddingRight: 4 }}>●</span>}
    </div>
  );
}

// ── Root tree (loads top-level entries once) ──────────────────────────────

function FileTree({
  sessionId, selected, changed, onSelect, onEntryContextMenu, revealPath, refreshKey,
}: {
  sessionId: string;
  selected: string | null;
  changed: Set<string>;
  onSelect: (entry: FileEntry) => void;
  onEntryContextMenu?: (e: React.MouseEvent, entry: FileEntry) => void;
  revealPath?: string | null;
  refreshKey?: number;
}) {
  const [entries, setEntries] = useState<FileEntry[] | null>(null);

  useEffect(() => {
    setEntries(null);
    listFiles(sessionId).then((r) => setEntries(r.entries)).catch(() => setEntries([]));
  }, [sessionId, refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  if (entries === null) return <div style={{ padding: "8px 12px", color: "var(--text-faint)", fontSize: 11 }}>Loading…</div>;
  if (entries.length === 0) return <div style={{ padding: "8px 12px", color: "var(--text-faint)", fontSize: 11 }}>Empty</div>;

  return (
    <>
      {entries.map((e) =>
        e.type === "dir" ? (
          <FileTreeDir
            key={e.path} sessionId={sessionId} entry={e}
            depth={0} selected={selected} changed={changed} onSelect={onSelect} onEntryContextMenu={onEntryContextMenu} revealPath={revealPath} refreshKey={refreshKey}
          />
        ) : (
          <FileTreeFile
            key={e.path} entry={e}
            depth={0} selected={selected} changed={changed} onSelect={onSelect} onEntryContextMenu={onEntryContextMenu}
          />
        )
      )}
    </>
  );
}

// ── Split diff ────────────────────────────────────────────────────────────

interface SBSRow {
  type: "context" | "change" | "separator";
  oldLn: number | null;
  newLn: number | null;
  oldContent: string | null;
  newContent: string | null;
}

function parseSideBySide(diffRaw: string): SBSRow[] {
  const rows: SBSRow[] = [];
  if (!diffRaw) return rows;
  const lines = diffRaw.split("\n");
  let oldLn = 0, newLn = 0, inHunk = false;
  let removedBuf: { content: string; ln: number }[] = [];
  let addedBuf: { content: string; ln: number }[] = [];

  const flush = () => {
    const len = Math.max(removedBuf.length, addedBuf.length);
    for (let i = 0; i < len; i++) {
      rows.push({
        type: "change",
        oldLn: removedBuf[i]?.ln ?? null,
        newLn: addedBuf[i]?.ln ?? null,
        oldContent: removedBuf[i]?.content ?? null,
        newContent: addedBuf[i]?.content ?? null,
      });
    }
    removedBuf = []; addedBuf = [];
  };

  for (const raw of lines) {
    if (raw.startsWith("@@")) {
      flush();
      if (inHunk) rows.push({ type: "separator", oldLn: null, newLn: null, oldContent: null, newContent: null });
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) { oldLn = parseInt(m[1]) - 1; newLn = parseInt(m[2]) - 1; }
      inHunk = true;
    } else if (inHunk) {
      if (raw.startsWith("-") && !raw.startsWith("---")) {
        oldLn++; removedBuf.push({ content: raw.slice(1), ln: oldLn });
      } else if (raw.startsWith("+") && !raw.startsWith("+++")) {
        newLn++; addedBuf.push({ content: raw.slice(1), ln: newLn });
      } else if (raw.startsWith(" ")) {
        flush(); oldLn++; newLn++;
        rows.push({ type: "context", oldLn, newLn, oldContent: raw.slice(1), newContent: raw.slice(1) });
      }
    }
  }
  flush();
  return rows;
}

function SplitDiffViewer({ data }: { data: FileData }) {
  const rows = parseSideBySide(data.diff_raw ?? "");
  const hl = (code: string) => {
    try { return hljs.highlight(code || " ", { language: data.language, ignoreIllegals: true }).value; }
    catch { return (code || " ").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  };

  if (rows.length === 0) {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faint)", fontSize: 13 }}>
        No diff available
      </div>
    );
  }

  return (
    <div style={{
      flex: 1, overflowY: "auto", overflowX: "auto", background: "var(--bg-base)",
      fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace',
      fontSize: 12, lineHeight: "20px",
    }}>
      <table style={{ borderCollapse: "collapse", width: "100%", tableLayout: "fixed" }}>
        <colgroup>
          <col style={{ width: 44 }} />
          <col style={{ width: "50%" }} />
          <col style={{ width: 1, background: "var(--bg-hover)" }} />
          <col style={{ width: 44 }} />
          <col style={{ width: "50%" }} />
        </colgroup>
        <tbody>
          {rows.map((row, i) => {
            if (row.type === "separator") {
              return (
                <tr key={`sep-${i}`}>
                  <td colSpan={5} style={{
                    textAlign: "center", color: "var(--text-faint)", fontSize: 11,
                    background: "var(--bg-surface)", height: 20, userSelect: "none",
                  }}>···</td>
                </tr>
              );
            }
            const isChange = row.type === "change";
            const hasOld = row.oldContent !== null;
            const hasNew = row.newContent !== null;
            const oldBg = isChange && hasOld ? "var(--diff-del-bg)" : "transparent";
            const newBg = isChange && hasNew ? "var(--diff-add-bg)" : "transparent";
            return (
              <tr key={i}>
                {/* Old line number */}
                <td style={{
                  textAlign: "right", paddingRight: 8, paddingLeft: 8,
                  color: isChange && hasOld ? "var(--accent-red)" : "var(--text-faint)",
                  userSelect: "none", whiteSpace: "nowrap", verticalAlign: "top",
                  fontSize: 11, background: oldBg, minHeight: 20,
                }}>
                  {row.oldLn ?? ""}
                </td>
                {/* Old content */}
                <td style={{
                  paddingRight: 12, paddingLeft: 6, whiteSpace: "pre-wrap", wordBreak: "break-all", verticalAlign: "top",
                  background: oldBg,
                  borderLeft: isChange && hasOld ? "2px solid var(--diff-del-prefix)" : "2px solid transparent",
                }}
                  dangerouslySetInnerHTML={{ __html: hasOld ? hl(row.oldContent!) : "&nbsp;" }}
                />
                {/* Divider */}
                <td style={{ background: "var(--bg-hover)", padding: 0, width: 1 }} />
                {/* New line number */}
                <td style={{
                  textAlign: "right", paddingRight: 8, paddingLeft: 8,
                  color: isChange && hasNew ? "var(--accent-green)" : "var(--text-faint)",
                  userSelect: "none", whiteSpace: "nowrap", verticalAlign: "top",
                  fontSize: 11, background: newBg, minHeight: 20,
                }}>
                  {row.newLn ?? ""}
                </td>
                {/* New content */}
                <td style={{
                  paddingRight: 12, paddingLeft: 6, whiteSpace: "pre-wrap", wordBreak: "break-all", verticalAlign: "top",
                  background: newBg,
                  borderLeft: isChange && hasNew ? "2px solid var(--diff-add-prefix)" : "2px solid transparent",
                }}
                  dangerouslySetInnerHTML={{ __html: hasNew ? hl(row.newContent!) : "&nbsp;" }}
                />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Code viewer ───────────────────────────────────────────────────────────

const LINE_H = 20;

function CodeViewer({ data, scrollToFirst, diffOnly, noDiff }: { data: FileData; scrollToFirst: boolean; diffOnly?: boolean; noDiff?: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const addedSet = noDiff ? new Set<number>() : new Set(data.added_lines);
  const removedSet = noDiff ? new Set<number>() : new Set(data.removed_lines);
  const lines = data.content.split("\n");

  const DIFF_CONTEXT = 3;
  const visibleSet: Set<number> | null =
    diffOnly && !noDiff && (data.added_lines.length > 0 || data.removed_lines.length > 0)
      ? (() => {
          const s = new Set<number>();
          for (const ln of [...data.added_lines, ...data.removed_lines]) {
            for (let i = Math.max(1, ln - DIFF_CONTEXT); i <= Math.min(lines.length, ln + DIFF_CONTEXT); i++) s.add(i);
          }
          return s;
        })()
      : null;

  type Row = { type: "line"; ln: number } | { type: "gap" };
  const rows: Row[] = [];
  if (visibleSet) {
    const sorted = [...visibleSet].sort((a, b) => a - b);
    let prev = -1;
    for (const ln of sorted) {
      if (prev !== -1 && ln > prev + 1) rows.push({ type: "gap" });
      rows.push({ type: "line", ln });
      prev = ln;
    }
  } else {
    for (let i = 0; i < lines.length; i++) rows.push({ type: "line", ln: i + 1 });
  }

  useEffect(() => {
    if (!scrollToFirst || !containerRef.current) return;
    if (visibleSet) {
      containerRef.current.scrollTop = 0;
      return;
    }
    const first = data.added_lines[0] ?? data.removed_lines[0];
    if (!first) return;
    containerRef.current.scrollTop = Math.max(0, (first - 1) * LINE_H - 120);
  }, [data.path, scrollToFirst, diffOnly]); // eslint-disable-line

  return (
    <div
      ref={containerRef}
      style={{
        flex: 1, overflowY: "auto", overflowX: "auto", background: "var(--bg-base)",
        fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace',
        fontSize: 13, lineHeight: `${LINE_H}px`,
      }}
    >
      {data.truncated && (
        <div style={{ padding: "4px 12px", background: "var(--bg-deep)", color: "var(--text-secondary)", fontSize: 11, borderBottom: "1px solid var(--border)" }}>
          File truncated to 3000 lines
        </div>
      )}
      <table style={{ borderCollapse: "collapse", minWidth: "100%" }}>
        <tbody>
          {rows.map((row, idx) => {
            if (row.type === "gap") {
              return (
                <tr key={`gap-${idx}`} style={{ height: LINE_H }}>
                  <td colSpan={2} style={{
                    textAlign: "center", color: "var(--text-faint)", fontSize: 11,
                    userSelect: "none", background: "var(--bg-surface)",
                  }}>···</td>
                </tr>
              );
            }
            const { ln } = row;
            const line = lines[ln - 1] ?? "";
            const isAdded = addedSet.has(ln);
            const isRemoved = removedSet.has(ln);
            let hl = "";
            try {
              hl = hljs.highlight(line || " ", { language: data.language, ignoreIllegals: true }).value;
            } catch {
              hl = (line || " ").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
            }
            return (
              <tr key={ln} style={{ background: isAdded ? "var(--diff-add-bg)" : isRemoved ? "var(--diff-del-bg)" : "transparent" }}>
                <td style={{
                  textAlign: "right", paddingRight: 12, paddingLeft: 12,
                  color: isAdded ? "var(--accent-green)" : isRemoved ? "var(--accent-red)" : "var(--text-faint)",
                  userSelect: "none", whiteSpace: "nowrap", verticalAlign: "top",
                  minWidth: 40, fontSize: 11,
                }}>
                  {isAdded ? "+" : isRemoved ? "−" : ""}{ln}
                </td>
                <td style={{
                  paddingRight: 24, whiteSpace: "pre-wrap", wordBreak: "break-all", verticalAlign: "top", paddingLeft: 8,
                  borderLeft: isAdded ? "2px solid var(--diff-add-prefix)" : isRemoved ? "2px solid var(--diff-del-prefix)" : "2px solid transparent",
                }}
                  dangerouslySetInnerHTML={{ __html: hl }}
                />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Status colors ─────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  modified: "var(--accent-amber)", added: "var(--accent-green)", deleted: "var(--accent-red)",
  renamed: "var(--accent-blue)", untracked: "var(--text-secondary)", conflict: "var(--accent-red)",
};

// ── Changes tree ─────────────────────────────────────────────────────────

interface ChangesNode {
  name: string;
  path: string;
  file?: ChangedFile;
  children: ChangesNode[];
}

function buildChangesTree(files: ChangedFile[]): ChangesNode[] {
  interface InternalNode { name: string; path: string; file?: ChangedFile; childMap: Map<string, InternalNode> }
  const rootMap = new Map<string, InternalNode>();
  for (const f of files) {
    // Defensive: strip trailing slash so an untracked-directory entry like
    // "dir/" (from `git status --porcelain` without -uall) doesn't split
    // into ["dir", ""] and produce an empty-named leaf row.
    const cleanPath = f.path.replace(/\/+$/, "");
    if (!cleanPath) continue;
    const parts = cleanPath.split("/");
    let cur = rootMap;
    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const path = parts.slice(0, i + 1).join("/");
      if (!cur.has(name)) cur.set(name, { name, path, childMap: new Map() });
      const node = cur.get(name)!;
      if (i === parts.length - 1) node.file = f;
      cur = node.childMap;
    }
  }
  const flatten = (map: Map<string, InternalNode>): ChangesNode[] =>
    [...map.values()]
      .map(n => ({ name: n.name, path: n.path, file: n.file, children: flatten(n.childMap) }))
      .sort((a, b) => {
        const aDir = a.children.length > 0;
        const bDir = b.children.length > 0;
        if (aDir !== bDir) return aDir ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
  return flatten(rootMap);
}

function countChangedFiles(node: ChangesNode): number {
  if (node.children.length === 0) return node.file ? 1 : 0;
  let n = node.file ? 1 : 0;
  for (const c of node.children) n += countChangedFiles(c);
  return n;
}

function ChangesNodeRow({
  node, depth, selectedPath, onClickFile,
}: {
  node: ChangesNode;
  depth: number;
  selectedPath: string | null;
  onClickFile: (f: ChangedFile, path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const isDir = node.children.length > 0;
  const indent = depth * 10 + 8;

  if (isDir) {
    const fileCount = countChangedFiles(node);
    return (
      <>
        <div
          onClick={() => setOpen(v => !v)}
          style={{
            display: "flex", alignItems: "center", gap: 4,
            padding: `2px 8px 2px ${indent}px`, cursor: "pointer",
            color: "var(--text-secondary)", fontSize: 11, userSelect: "none",
          }}
          onMouseEnter={e => { e.currentTarget.style.background = "var(--bg-surface)"; }}
          onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}
        >
          <span style={{ fontSize: 9, color: "var(--text-faint)" }}>{open ? "▾" : "▸"}</span>
          <FileIcon isDir isOpen={open} />
          <span>{node.name}</span>
          <span style={{ fontSize: 10, color: "var(--text-faint)", marginLeft: 2 }}>({fileCount})</span>
        </div>
        {open && node.children.map(child => (
          <ChangesNodeRow key={child.path} node={child} depth={depth + 1} selectedPath={selectedPath} onClickFile={onClickFile} />
        ))}
      </>
    );
  }

  const f = node.file!;
  const isSelected = selectedPath === node.path;
  return (
    <div
      onClick={() => onClickFile(f, node.path)}
      style={{
        display: "flex", alignItems: "center", gap: 5,
        padding: `3px 8px 3px ${indent}px`, cursor: "pointer", fontSize: 11,
        background: isSelected ? "var(--bg-hover)" : "transparent",
        color: "var(--text-secondary)",
      }}
      onMouseEnter={e => { if (!isSelected) e.currentTarget.style.background = "var(--bg-surface)"; }}
      onMouseLeave={e => { if (!isSelected) e.currentTarget.style.background = "transparent"; }}
    >
      <span style={{ fontSize: 9, color: STATUS_COLORS[f.status] ?? "var(--text-secondary)", minWidth: 10, fontWeight: 700, flexShrink: 0 }}>
        {f.status[0].toUpperCase()}
      </span>
      <FileIcon name={node.name} />
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{node.name}</span>
      {(f.added != null || f.removed != null) && (
        <span style={{ display: "flex", gap: 3, flexShrink: 0, fontSize: 9, fontFamily: "monospace" }}>
          {f.added != null && f.added > 0 && <span style={{ color: "var(--accent-green)" }}>+{f.added}</span>}
          {f.removed != null && f.removed > 0 && <span style={{ color: "var(--accent-red)" }}>-{f.removed}</span>}
        </span>
      )}
    </div>
  );
}

// ── RecentCommitsPanel — bottom strip of the left panel ──────────────────

function CommitMiniRow({ entry, onClick }: { entry: GitLogEntry; onClick: () => void }) {
  const d = new Date(entry.date);
  const pad = (n: number) => String(n).padStart(2, "0");
  const dateStr = `${d.getMonth() + 1}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return (
    <div
      onClick={onClick}
      style={{ display: "flex", alignItems: "center", gap: 6, padding: "3px 10px", cursor: "pointer", fontSize: 11 }}
      onMouseEnter={e => { e.currentTarget.style.background = "var(--bg-surface)"; }}
      onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}
      title={`${entry.short_hash} · ${entry.subject}\n${entry.author} · ${dateStr}`}
    >
      <span style={{ fontFamily: "monospace", color: "var(--accent-blue)", flexShrink: 0, fontSize: 10 }}>{entry.short_hash}</span>
      <span style={{ color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{entry.subject}</span>
      <span style={{ fontSize: 9, color: "var(--text-faintest)", flexShrink: 0 }}>{dateStr}</span>
    </div>
  );
}

const COMMITS_SHOW_N = 5;

function RecentCommitsPanel({ sessionId }: { sessionId: string }) {
  const [log, setLog] = useState<GitLogEntry[]>([]);
  const [isRepo, setIsRepo] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [detailEntry, setDetailEntry] = useState<GitLogEntry | null>(null);
  const [showFullPanel, setShowFullPanel] = useState(false);

  useEffect(() => {
    let mounted = true;
    const poll = async () => {
      try {
        const info = await getGitInfo(sessionId);
        if (!mounted) return;
        setIsRepo(info.is_repo);
        setLog(info.log ?? []);
      } catch { /* ignore */ } finally {
        if (mounted) setLoaded(true);
      }
    };
    poll();
    const id = setInterval(poll, 15000);
    return () => { mounted = false; clearInterval(id); };
  }, [sessionId]);

  // Hide entirely until first fetch settles; avoids flashing "No commits" in non-repo cwd.
  if (!loaded) return null;
  if (!isRepo) return null;

  const top = log.slice(0, COMMITS_SHOW_N);
  const hasMore = log.length > COMMITS_SHOW_N;

  return (
    <>
      <div style={{ flexShrink: 0, borderTop: "1px solid var(--bg-hover)" }}>
        <div style={{ padding: "5px 10px", fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span>Commits{log.length > 0 ? ` (${log.length})` : ""}</span>
          <button
            onClick={() => setShowFullPanel(true)}
            title="Open full git panel"
            style={{ fontSize: 9, padding: "1px 6px", background: "var(--bg-hover)", color: "var(--text-muted)", border: "1px solid var(--text-faintest)", borderRadius: 3, cursor: "pointer" }}
          >
            {hasMore ? "View all →" : "Git…"}
          </button>
        </div>
        {top.length === 0 ? (
          <div style={{ padding: "4px 12px 8px", color: "var(--text-faint)", fontSize: 11 }}>No commits yet</div>
        ) : (
          <div style={{ maxHeight: 140, overflowY: "auto" }}>
            {top.map(e => (
              <CommitMiniRow key={e.hash} entry={e} onClick={() => setDetailEntry(e)} />
            ))}
          </div>
        )}
      </div>
      {detailEntry && (
        <CommitDetailModal sessionId={sessionId} entry={detailEntry} onClose={() => setDetailEntry(null)} />
      )}
      {showFullPanel && (
        <GitPanel sessionId={sessionId} onClose={() => setShowFullPanel(false)} />
      )}
    </>
  );
}

// ── FileSidePanel — reusable left-panel for Chat mode ─────────────────────

export function FileSidePanel({
  sessionId,
  selectedPath,
  onFileClick,
}: {
  sessionId: string;
  selectedPath: string | null;
  onFileClick: (path: string, fromChanges: boolean) => void;
}) {
  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);
  const [filesRefreshKey, setFilesRefreshKey] = useState(0);
  const prevChangedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let mounted = true;
    const poll = async () => {
      try {
        const files = await getCodeChangedFiles(sessionId);
        if (!mounted) return;
        setChangedFiles(files);
        // Bump refreshKey on any change to the changed-files set (additions OR
        // removals) so deletions, reverts, and new files all refresh the tree.
        const nextSet = new Set(files.map(f => f.path));
        const prev = prevChangedRef.current;
        const sameSize = nextSet.size === prev.size;
        const sameMembers = sameSize && [...nextSet].every(p => prev.has(p));
        if (!sameMembers) setFilesRefreshKey(k => k + 1);
        prevChangedRef.current = nextSet;
      } catch {/* ignore */}
    };
    poll();
    const id = setInterval(poll, 8000);
    return () => { mounted = false; clearInterval(id); };
  }, [sessionId]);

  const changedSet = new Set(changedFiles.map((f) => f.path));

  const handleSelect = (entry: FileEntry) => onFileClick(entry.path, false);

  const [sideBarRef, sideBarWidth] = useResizeWidth<HTMLDivElement>();
  const [sideBranchName, setSideBranchName] = useState<string>("");
  const sideNaturalWidth = useMemo(() => {
    const filesLabel = 32;
    const padding = 20;
    const branchOverhead = 42;
    const branchName = (sideBranchName || "main").length * 7.5;
    const pullCompact = 60;
    const gaps = 16;
    return filesLabel + padding + branchOverhead + branchName + pullCompact + gaps;
  }, [sideBranchName]);
  const sideCollapse = sideBarWidth > 0 && sideBarWidth < sideNaturalWidth;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "var(--bg-base)" }}>
      {/* Files (top, fills remaining space) */}
      <div style={{ overflowY: "auto", flex: 1, paddingTop: 4 }}>
        <div ref={sideBarRef} style={{ padding: "4px 10px 2px", fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", display: "flex", alignItems: "center", gap: sideCollapse ? 4 : 8, minWidth: 0 }}>
          <span style={{ flexShrink: 0 }}>Files</span>
          <GitBranchPicker
            sessionId={sessionId}
            refreshKey={filesRefreshKey}
            onBranchChanged={() => setFilesRefreshKey(k => k + 1)}
            onInfoLoaded={(i) => setSideBranchName(i.current)}
            iconOnly={sideCollapse}
          />
          <GitPullButton
            sessionId={sessionId}
            onPulled={() => setFilesRefreshKey(k => k + 1)}
            iconOnly={sideCollapse}
          />
        </div>
        <FileTree sessionId={sessionId} selected={selectedPath} changed={changedSet} onSelect={handleSelect} revealPath={selectedPath} refreshKey={filesRefreshKey} />
      </div>
      {/* Changes (middle) */}
      <div style={{ borderTop: "1px solid var(--bg-hover)", flexShrink: 0 }}>
        <div style={{ padding: "5px 10px", fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
          Changes ({changedFiles.length})
        </div>
        {changedFiles.length === 0 ? (
          <div style={{ padding: "4px 12px 8px", color: "var(--text-faint)", fontSize: 11 }}>No changes</div>
        ) : (
          <div style={{ maxHeight: 200, overflowY: "auto" }}>
            {buildChangesTree(changedFiles).map((node) => (
              <ChangesNodeRow
                key={node.path}
                node={node}
                depth={0}
                selectedPath={selectedPath}
                onClickFile={(_, path) => onFileClick(path, true)}
              />
            ))}
          </div>
        )}
      </div>
      {/* Recent commits (bottom) */}
      <RecentCommitsPanel sessionId={sessionId} />
    </div>
  );
}

// ── File viewer header + content (shared by CodePane and FileViewerPane) ─────

function ViewerHeader({
  path, selectedChanged, fileData, showImage, showSqlite, isMd, isCsv, mdPreview, setMdPreview, viewMode, setViewMode, noDiff,
  onDownload, canEdit, editing, saving, isModified, onEditToggle, onCancelEdit,
}: {
  path: string;
  selectedChanged?: ChangedFile;
  fileData: FileData | null;
  showImage: boolean;
  showSqlite: boolean;
  isMd: boolean;
  isCsv: boolean;
  mdPreview: boolean;
  setMdPreview: (v: boolean) => void;
  viewMode: "full" | "diff" | "split";
  setViewMode: (v: "full" | "diff" | "split") => void;
  noDiff?: boolean;
  onDownload?: () => void;
  canEdit?: boolean;
  editing?: boolean;
  saving?: boolean;
  isModified?: boolean;
  onEditToggle?: () => void;
  onCancelEdit?: () => void;
}) {
  const name = path.split("/").pop() ?? path;
  const sizeText = fileData?.size != null ? humanBytes(fileData.size) : null;
  const isTextFile = !!fileData && !fileData.is_binary && !showImage && !showSqlite;
  const linesText = isTextFile && fileData?.total_lines != null
    ? (fileData.truncated
        ? `${fileData.total_lines.toLocaleString()} lines (showing first 3000)`
        : `${fileData.total_lines.toLocaleString()} lines`)
    : null;
  return (
    <div style={{
      padding: "4px 14px", borderBottom: "1px solid var(--bg-hover)", flexShrink: 0,
      display: "flex", alignItems: "center", gap: 8, background: "var(--bg-base)", fontSize: 12, minHeight: 28,
    }}>
      <FileIcon name={name} />
      <span style={{ color: "var(--text-secondary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {path}
        {(sizeText || linesText) && (
          <span style={{ marginLeft: 8, color: "var(--text-faint)", fontSize: 11 }}>
            {sizeText}
            {sizeText && linesText ? " · " : ""}
            {linesText}
          </span>
        )}
      </span>
      {selectedChanged && (
        <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 3, background: "var(--bg-hover)", color: STATUS_COLORS[selectedChanged.status] ?? "var(--text-secondary)", flexShrink: 0 }}>
          {selectedChanged.status}
        </span>
      )}
      {fileData && !showImage && !showSqlite && (
        <span style={{ fontSize: 10, color: "var(--text-faint)", flexShrink: 0, display: "flex", alignItems: "center", gap: 4 }}>
          {!isMd && !isCsv && (fileData.is_binary ? "binary" : fileData.language)}
          {isMd && (
            <button
              onClick={() => setMdPreview(!mdPreview)}
              title="Toggle markdown preview / source"
              style={{
                fontSize: 9, padding: "1px 5px",
                background: mdPreview ? "var(--bg-modal)" : "var(--bg-hover)",
                color: mdPreview ? "#60a5fa" : "var(--text-muted)",
                border: `1px solid ${mdPreview ? "#2563eb" : "var(--text-faintest)"}`,
                borderRadius: 3, cursor: "pointer",
              }}
            >
              {mdPreview ? "PREVIEW" : "SOURCE"}
            </button>
          )}
          {isCsv && (
            <button
              onClick={() => setMdPreview(!mdPreview)}
              title="Toggle table view / source"
              style={{
                fontSize: 9, padding: "1px 5px",
                background: mdPreview ? "var(--bg-modal)" : "var(--bg-hover)",
                color: mdPreview ? "#60a5fa" : "var(--text-muted)",
                border: `1px solid ${mdPreview ? "#2563eb" : "var(--text-faintest)"}`,
                borderRadius: 3, cursor: "pointer",
              }}
            >
              {mdPreview ? "TABLE" : "SOURCE"}
            </button>
          )}
          {!isMd && !isCsv && !noDiff && fileData.added_lines.length > 0 && <span style={{ color: "var(--accent-green)", marginLeft: 6 }}>+{fileData.added_lines.length}</span>}
          {!isMd && !isCsv && !noDiff && fileData.removed_lines.length > 0 && <span style={{ color: "var(--accent-red)", marginLeft: 4 }}>−{fileData.removed_lines.length}</span>}
          {!isMd && !isCsv && !noDiff && (fileData.added_lines.length > 0 || fileData.removed_lines.length > 0) && (
            <button
              onClick={() => setViewMode(viewMode === "full" ? "diff" : viewMode === "diff" ? "split" : "full")}
              title="Cycle: Full → Diff → Split"
              style={{
                marginLeft: 6, fontSize: 9, padding: "1px 5px",
                background: viewMode !== "full" ? "var(--bg-modal)" : "var(--bg-hover)",
                color: viewMode !== "full" ? "#60a5fa" : "var(--text-muted)",
                border: `1px solid ${viewMode !== "full" ? "#2563eb" : "var(--text-faintest)"}`,
                borderRadius: 3, cursor: "pointer",
              }}
            >
              {viewMode === "full" ? "FULL" : viewMode === "diff" ? "DIFF" : "SPLIT"}
            </button>
          )}
          {!fileData.is_binary && !editing && (
            <button
              onClick={() => {
                const bytes = new Blob([fileData.content]).size;
                if (bytes > 500 * 1024) {
                  alert(`File is too large to copy (${(bytes / 1024).toFixed(0)} KB). Limit is 500 KB.`);
                  return;
                }
                copyText(fileData.content);
              }}
              title="Copy file content to clipboard"
              style={{
                marginLeft: 6, fontSize: 9, padding: "1px 5px",
                background: "#2d1a4a", color: "#a78bfa",
                border: "1px solid #4c1d95", borderRadius: 3, cursor: "pointer",
              }}
            >
              COPY
            </button>
          )}
          {canEdit && onEditToggle && !editing && (
            <button
              onClick={onEditToggle}
              title="Edit this file"
              style={{
                marginLeft: 6, fontSize: 9, padding: "1px 5px",
                background: "var(--bg-hover)", color: "var(--text-body)",
                border: "1px solid var(--text-faintest)", borderRadius: 3, cursor: "pointer",
              }}
            >
              EDIT
            </button>
          )}
          {canEdit && onEditToggle && editing && (
            <>
              <button
                onClick={onEditToggle}
                disabled={saving || !isModified}
                title={!isModified ? "No changes to save" : "Save changes"}
                style={{
                  marginLeft: 6, fontSize: 9, padding: "1px 5px",
                  background: isModified ? "var(--accent-blue)" : "var(--text-faintest)",
                  color: "#fff",
                  border: `1px solid ${isModified ? "var(--accent-blue)" : "var(--text-faintest)"}`,
                  borderRadius: 3,
                  cursor: saving || !isModified ? "default" : "pointer",
                  opacity: saving ? 0.6 : 1,
                }}
              >
                {saving ? "SAVING…" : "SAVE"}
              </button>
              {onCancelEdit && (
                <button
                  onClick={onCancelEdit}
                  disabled={saving}
                  title="Discard changes"
                  style={{
                    marginLeft: 4, fontSize: 9, padding: "1px 5px",
                    background: "var(--bg-hover)", color: "var(--text-muted)",
                    border: "1px solid var(--text-faintest)", borderRadius: 3, cursor: "pointer",
                  }}
                >
                  CANCEL
                </button>
              )}
            </>
          )}
        </span>
      )}
      {onDownload && (
        <button
          onClick={onDownload}
          title={fileData?.size != null && fileData.size > MAX_TRANSFER_BYTES ? `Too large to download (>${MAX_TRANSFER_MB}MB)` : "Download file"}
          style={{
            marginLeft: 6, background: "transparent", border: "none", padding: "0 2px",
            cursor: "pointer", flexShrink: 0, lineHeight: 1, display: "flex", alignItems: "center",
          }}
        >
          <img
            src={downloadIcon}
            style={{
              width: 14, height: 14,
              filter: fileData?.size != null && fileData.size > MAX_TRANSFER_BYTES ? "invert(0.3)" : "invert(0.6)",
            }}
          />
        </button>
      )}
    </div>
  );
}

function ViewerContent({
  sessionId, entry, fileData, fileLoading, scrollToFirst, viewMode, mdPreview, noDiff,
}: {
  sessionId: string;
  entry: FileEntry | null;
  fileData: FileData | null;
  fileLoading: boolean;
  scrollToFirst: boolean;
  viewMode: "full" | "diff" | "split";
  mdPreview: boolean;
  noDiff?: boolean;
}) {
  if (!entry) {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faintest)", fontSize: 13 }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ marginBottom: 8, lineHeight: 0 }}><FileIcon isDir isOpen size={36} /></div>
          <div>Select a file from the tree</div>
          <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>Changed files are highlighted in yellow</div>
        </div>
      </div>
    );
  }
  const showSqlite = entry.is_sqlite ?? false;
  const showPdf = isPdfFile(entry.name);
  const showImage = isImage(entry.name);
  const isMd = isMdFile(entry.name);
  const showMarkdown = isMd && mdPreview && !!fileData;

  const isCsv = isCsvFile(entry.name);

  if (entry.is_archive) return <ArchiveViewer sessionId={sessionId} path={entry.path} />;
  if (showSqlite) return <SqliteViewer key={entry.path} sessionId={sessionId} path={entry.path} />;
  if (showPdf) return <PdfViewer key={entry.path} sessionId={sessionId} path={entry.path} />;
  if (showImage) return <ImageViewer sessionId={sessionId} path={entry.path} />;
  if (fileLoading && !fileData) return <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>;
  if (fileData?.is_binary) return (
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faint)", fontSize: 13 }}>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 32, marginBottom: 8 }}>🗂</div>
        <div style={{ color: "var(--text-secondary)" }}>Binary file</div>
        {fileData.size != null && (
          <div style={{ fontSize: 11, marginTop: 4, color: "var(--text-faint)" }}>
            {fileData.size >= 1048576
              ? `${(fileData.size / 1048576).toFixed(1)} MB`
              : fileData.size >= 1024
              ? `${(fileData.size / 1024).toFixed(1)} KB`
              : `${fileData.size} B`}
          </div>
        )}
      </div>
    </div>
  );
  if (showMarkdown) return <MarkdownViewer key={fileData!.path + ":md"} content={fileData!.content} />;
  if (isCsv && fileData) return <CsvViewer key={fileData.path} content={fileData.content} delimiter={csvDelimiter(entry.name)} />;
  if (fileData) return viewMode === "split" && !noDiff
    ? <SplitDiffViewer key={fileData.path} data={fileData} />
    : <CodeViewer key={fileData.path} data={fileData} scrollToFirst={scrollToFirst} diffOnly={viewMode === "diff"} noDiff={noDiff} />;
  return null;
}

// ── FileViewerPane — standalone file viewer (used in main layout) ─────────

export function FileViewerPane({ sessionId, path, viewMode: initViewMode = "full", noDiff, onDirtyChange }: {
  sessionId: string;
  path: string;
  viewMode?: "full" | "diff" | "split";
  noDiff?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [fileData, setFileData] = useState<FileData | null>(null);
  const [fileLoading, setFileLoading] = useState(true);
  const [viewMode, setViewMode] = useState<"full" | "diff" | "split">(initViewMode);
  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);
  const [editing, setEditing] = useState(false);
  const [editBuffer, setEditBuffer] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [columnMode, setColumnMode] = useState(false);
  // mtime captured the moment the user clicked EDIT; sent on save so the
  // backend can return 409 if the file was modified externally meanwhile.
  const [editStartMtime, setEditStartMtime] = useState<number | null>(null);
  const [conflict, setConflict] = useState<{ currentMtime: number | null } | null>(null);
  const editingRef = useRef(false);
  const cmRef = useRef<CodeMirrorEditorHandle | null>(null);
  const name = path.split("/").pop() ?? path;
  const isMd = isMdFile(name);
  const isCsv = isCsvFile(name);
  const [mdPreview, setMdPreview] = useState(isMd || isCsv);

  useEffect(() => { editingRef.current = editing; }, [editing]);

  useEffect(() => { setViewMode(initViewMode); }, [initViewMode]);
  useEffect(() => {
    const n = path.split("/").pop() ?? path;
    setMdPreview(isMdFile(n) || isCsvFile(n));
    setEditing(false);
    setEditBuffer("");
    setEditStartMtime(null);
    setConflict(null);
  }, [path]);

  useEffect(() => {
    const n = path.split("/").pop() ?? path;
    if (isArchiveFile(n) || isSqliteFile(n) || isPdfFile(n) || isImage(n)) {
      setFileData(null); setFileLoading(false); return;
    }
    let mounted = true;
    setFileData(null);
    setFileLoading(true);
    const fetch = () => getCodeFile(sessionId, path)
      .then(d => { if (mounted) { setFileData(d); setFileLoading(false); } })
      .catch(() => { if (mounted) setFileLoading(false); });
    fetch();
    const id = setInterval(() => {
      if (!mounted || editingRef.current) return;
      getCodeFile(sessionId, path).then(d => { if (mounted && !editingRef.current) setFileData(d); }).catch(() => {});
    }, POLL_MS);
    return () => { mounted = false; clearInterval(id); };
  }, [sessionId, path]);

  useEffect(() => {
    let mounted = true;
    const poll = () => getCodeChangedFiles(sessionId).then(f => { if (mounted) setChangedFiles(f); }).catch(() => {});
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { mounted = false; clearInterval(id); };
  }, [sessionId]);

  const showSqlite = isSqliteFile(name);
  const showImage = isImage(name);
  const showArchive = isArchiveFile(name);
  const showPdf = isPdfFile(name);
  const selectedChanged = changedFiles.find(f => f.path === path);
  const entry: FileEntry = { name, path, type: "file", size: null, is_text: !showSqlite && !showPdf && !showArchive, is_skipped: false, is_sqlite: showSqlite, is_archive: showArchive };

  // ── Config format conversion / validation ─────────────────────────────────
  const sourceFmt = detectFormat(name);
  const [convertTarget, setConvertTarget] = useState<"raw" | ConfigFormat>("raw");
  useEffect(() => { setConvertTarget("raw"); }, [path]);

  const conversion = useMemo(() => {
    if (!fileData || !sourceFmt || convertTarget === "raw") return null;
    return convert(fileData.content, sourceFmt, convertTarget);
  }, [fileData, sourceFmt, convertTarget]);

  const displayedFileData: FileData | null = useMemo(() => {
    if (!fileData) return null;
    if (!sourceFmt || convertTarget === "raw" || !conversion?.ok) return fileData;
    return {
      ...fileData,
      content: conversion.content,
      language: languageFor(convertTarget),
      added_lines: [],
      removed_lines: [],
      diff_raw: undefined,
    };
  }, [fileData, sourceFmt, convertTarget, conversion]);

  const canEdit = !!fileData && !fileData.is_binary && !showSqlite && !showArchive && !showPdf && !showImage;
  const isModified = editing && !!fileData && editBuffer !== fileData.content;

  useEffect(() => {
    onDirtyChange?.(isModified);
  }, [isModified, onDirtyChange]);
  useEffect(() => {
    return () => { onDirtyChange?.(false); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onDownload = async () => {
    if (fileData?.size != null && fileData.size > MAX_TRANSFER_BYTES) {
      alert(`File is too large to download (${(fileData.size / 1024 / 1024).toFixed(1)} MB). Limit is ${MAX_TRANSFER_MB} MB.`);
      return;
    }
    try { await downloadFile(sessionId, path); } catch (e) { alert(String(e)); }
  };

  const beginEdit = () => {
    if (!fileData || saving) return;
    setEditBuffer(fileData.content);
    setEditStartMtime(fileData.mtime ?? null);
    setEditing(true);
    setTimeout(() => cmRef.current?.focus(), 30);
  };

  const doSave = async (force: boolean) => {
    if (!fileData) return;
    setSaving(true);
    try {
      const out = await writeFile(sessionId, path, editBuffer, {
        expectedMtime: editStartMtime,
        force,
      });
      const fresh = await getCodeFile(sessionId, path).catch(() => null);
      if (fresh) setFileData(fresh);
      else if (out.mtime != null) setEditStartMtime(out.mtime);
      setEditing(false);
      setEditBuffer("");
      setEditStartMtime(null);
      setConflict(null);
    } catch (e) {
      if (e instanceof FileWriteConflictError) {
        setConflict({ currentMtime: e.current_mtime });
      } else {
        alert(String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  const handleSave = () => {
    if (!fileData || saving || !isModified) return;
    void doSave(false);
  };

  const reloadFromDisk = async () => {
    const fresh = await getCodeFile(sessionId, path).catch(() => null);
    if (fresh) {
      setFileData(fresh);
      setEditBuffer(fresh.content);
      setEditStartMtime(fresh.mtime ?? null);
    }
    setConflict(null);
  };

  const cancelEdit = () => {
    if (saving) return;
    setEditing(false);
    setEditBuffer("");
    setEditStartMtime(null);
    setConflict(null);
  };

  return (
    <div style={{ display: "flex", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden", background: "var(--bg-base)" }}>
      <ViewerHeader
        path={path}
        selectedChanged={selectedChanged}
        fileData={fileData}
        showImage={showImage}
        showSqlite={showSqlite || showArchive}
        isMd={isMd}
        isCsv={isCsv}
        mdPreview={mdPreview}
        setMdPreview={setMdPreview}
        viewMode={viewMode}
        setViewMode={setViewMode}
        noDiff={noDiff}
        onDownload={!showSqlite ? onDownload : undefined}
        canEdit={canEdit}
        editing={editing}
        saving={saving}
        isModified={isModified}
        onEditToggle={editing ? handleSave : beginEdit}
        onCancelEdit={cancelEdit}
      />
      {sourceFmt && fileData && !editing && (
        <div style={{ padding: "4px 14px", borderBottom: "1px solid var(--bg-hover)", display: "flex", alignItems: "center", gap: 8, background: "var(--bg-base)", flexShrink: 0 }}>
          <ConfigFormatToggle
            source={sourceFmt}
            target={convertTarget}
            onChange={setConvertTarget}
            error={conversion && !conversion.ok ? conversion.error : null}
            compact
          />
          <ConfigCheckButton
            content={fileData.content}
            format={sourceFmt}
            disabled={convertTarget !== "raw"}
            compact
          />
        </div>
      )}
      {sourceFmt && fileData && convertTarget === "raw" && !editing && (
        <ConfigValidationBanner content={fileData.content} format={sourceFmt} compact />
      )}
      {editing && fileData ? (
        <>
          <div style={{ padding: "4px 14px", borderBottom: "1px solid var(--bg-hover)", display: "flex", alignItems: "center", gap: 8, background: "var(--bg-base)", flexShrink: 0 }}>
            <button
              onClick={() => setColumnMode(v => !v)}
              title={columnMode ? "Column-select mode ON: drag = rectangular selection" : "Column-select mode OFF: hold Alt to drag column"}
              style={{
                padding: "2px 8px",
                fontSize: 11,
                fontFamily: "monospace",
                background: columnMode ? "var(--accent-blue)" : "var(--bg-elevated)",
                color: columnMode ? "#fff" : "var(--text-secondary)",
                border: "1px solid var(--bg-hover)",
                borderRadius: 3,
                cursor: "pointer",
              }}
            >
              COL {columnMode ? "ON" : "OFF"}
            </button>
            <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "monospace" }}>
              Ctrl+D select next · Ctrl+F find/replace · {columnMode ? "drag = rectangle" : "Alt+drag = rectangle"}
            </span>
          </div>
          <CodeMirrorEditor
            ref={cmRef}
            content={editBuffer}
            ext={name.split(".").pop()?.toLowerCase() ?? ""}
            onChange={setEditBuffer}
            onSave={handleSave}
            columnMode={columnMode}
          />
        </>
      ) : (
        <ViewerContent
          sessionId={sessionId}
          entry={entry}
          fileData={displayedFileData}
          fileLoading={fileLoading}
          scrollToFirst={false}
          viewMode={viewMode}
          mdPreview={mdPreview}
          noDiff={noDiff}
        />
      )}
      {conflict && (
        <div
          onClick={() => setConflict(null)}
          style={{
            position: "fixed", inset: 0, zIndex: 110,
            background: "rgba(0,0,0,0.45)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              minWidth: 380, maxWidth: 560,
              background: "var(--bg-modal)", border: "1px solid var(--border)",
              borderRadius: 6, padding: "14px 16px",
              boxShadow: "0 12px 32px rgba(0,0,0,0.5)",
              display: "flex", flexDirection: "column", gap: 10,
              fontSize: 12, color: "var(--text-body)",
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--accent-orange, #d59f00)" }}>
              File modified externally
            </div>
            <div style={{ color: "var(--text-secondary)" }}>
              <code style={{ color: "var(--text-body)" }}>{path}</code> changed on disk after you started editing.
              Saving now would overwrite those changes.
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4, flexWrap: "wrap" }}>
              <button
                onClick={() => setConflict(null)}
                disabled={saving}
                style={{
                  padding: "5px 12px", fontSize: 12, cursor: saving ? "default" : "pointer",
                  background: "var(--bg-surface)", color: "var(--text-body)",
                  border: "1px solid var(--border)", borderRadius: 3,
                  opacity: saving ? 0.6 : 1,
                }}
              >
                Cancel
              </button>
              <button
                onClick={() => { void reloadFromDisk(); }}
                disabled={saving}
                title="Discard your edits and reload the on-disk version"
                style={{
                  padding: "5px 12px", fontSize: 12, cursor: saving ? "default" : "pointer",
                  background: "var(--bg-surface)", color: "var(--text-body)",
                  border: "1px solid var(--border)", borderRadius: 3,
                  opacity: saving ? 0.6 : 1,
                }}
              >
                Reload from disk
              </button>
              <button
                onClick={() => { void doSave(true); }}
                disabled={saving}
                title="Overwrite the on-disk file with your edits"
                style={{
                  padding: "5px 12px", fontSize: 12, cursor: saving ? "default" : "pointer",
                  background: "var(--accent-orange, #d59f00)", color: "#1c2128",
                  border: "1px solid var(--accent-orange, #d59f00)", borderRadius: 3,
                  fontWeight: 600,
                  opacity: saving ? 0.6 : 1,
                }}
              >
                {saving ? "Saving…" : "Force overwrite"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── CodePane ──────────────────────────────────────────────────────────────

export function CodePane({
  sessionId,
  onFileSelect,
  selectedPathExternal,
  openPath,
  hideLeftPanel,
  onGitClick,
}: {
  sessionId: string;
  // Tree-only mode: when provided, file clicks call this instead of opening viewer inline
  onFileSelect?: (path: string, viewMode: "full" | "diff" | "split") => void;
  selectedPathExternal?: string | null;
  // Legacy: open a specific file from outside (only used in inline viewer mode)
  openPath?: { path: string; v: number; viewMode?: "full" | "diff" | "split" } | null;
  hideLeftPanel?: boolean;
  onGitClick?: () => void;
}) {
  const treeOnly = !!onFileSelect;

  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);
  const [selectedEntry, setSelectedEntry] = useState<FileEntry | null>(null);
  const [fileData, setFileData] = useState<FileData | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [autoFollow, setAutoFollow] = useState(true);
  const [scrollToFirst, setScrollToFirst] = useState(false);
  const [viewMode, setViewMode] = useState<"full" | "diff" | "split">("full");
  const [mdPreview, setMdPreview] = useState(true);
  const [filesRefreshKey, setFilesRefreshKey] = useState(0);
  // false when file was opened from FILES (plain view, no diff UI)
  const [selectedFromChanges, setSelectedFromChanges] = useState(true);

  // ── Toolbar / action state (ported from FileEditorModal) ───────────────────
  type ToolForm = null | "search" | "newFile" | "newFolder" | "upload" | "historySearch";
  const [toolForm, setToolForm] = useState<ToolForm>(null);
  const [toolError, setToolError] = useState("");
  const [toolBusy, setToolBusy] = useState(false);
  // Search
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<FileEntry[] | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  // New file / folder
  const [newName, setNewName] = useState("");
  const [newParent, setNewParent] = useState("");
  // Upload
  const [uploadDir, setUploadDir] = useState("");
  const [uploadPending, setUploadPending] = useState<File | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  // History search by path
  const [historySearchPath, setHistorySearchPath] = useState("");
  // Context menu (on tree entries)
  const [ctxMenu, setCtxMenu] = useState<{ entry: FileEntry; x: number; y: number } | null>(null);
  // Rename / move / delete / git-history modals
  const [renameTarget, setRenameTarget] = useState<{ entry: FileEntry; value: string } | null>(null);
  const [moveTarget, setMoveTarget] = useState<{ entry: FileEntry; dest: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ entry: FileEntry; recursive: boolean } | null>(null);
  const [historyTarget, setHistoryTarget] = useState<{
    path: string;
    log: Array<{ hash: string; short_hash: string; subject: string; author: string; date: string }>;
    loading: boolean;
    selected: { commit: string; full: string; diff: string; viewMode: "diff" | "full" } | null;
  } | null>(null);

  const autoFollowRef = useRef(autoFollow);
  autoFollowRef.current = autoFollow;
  const selectedRef = useRef<string | null>(null);
  const prevChangedRef = useRef<Set<string>>(new Set());

  // Debounce search query
  useEffect(() => {
    if (toolForm !== "search") { setSearchResults(null); return; }
    if (!searchQuery.trim()) { setSearchResults(null); setSearchLoading(false); return; }
    setSearchLoading(true);
    const id = window.setTimeout(async () => {
      try {
        const r = await searchFiles(sessionId, searchQuery.trim(), false);
        setSearchResults(r.entries);
      } catch { setSearchResults([]); }
      finally { setSearchLoading(false); }
    }, 250);
    return () => window.clearTimeout(id);
  }, [searchQuery, sessionId, toolForm]);

  // Close context menu on outside click
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => { window.removeEventListener("click", close); window.removeEventListener("scroll", close, true); };
  }, [ctxMenu]);

  // Reset form state when toolForm changes
  useEffect(() => {
    setToolError(""); setToolBusy(false);
    if (toolForm !== "search") { setSearchQuery(""); setSearchResults(null); }
    if (toolForm !== "newFile" && toolForm !== "newFolder") { setNewName(""); setNewParent(""); }
    if (toolForm !== "upload") { setUploadDir(""); setUploadPending(null); }
    if (toolForm !== "historySearch") setHistorySearchPath("");
  }, [toolForm]);

  const loadFile = useCallback(async (entry: FileEntry, scroll = false) => {
    if (isImage(entry.name) || entry.is_sqlite || isPdfFile(entry.name) || entry.is_archive) {
      setFileData(null); setSelectedEntry(entry); setScrollToFirst(false); return;
    }
    setFileLoading(true); setScrollToFirst(scroll);
    try {
      const data = await getCodeFile(sessionId, entry.path);
      setFileData(data); setSelectedEntry(entry);
    } catch {
      setFileData(null); setSelectedEntry(entry);
    } finally { setFileLoading(false); }
  }, [sessionId]);

  const handleSelect = useCallback((entry: FileEntry) => {
    if (treeOnly && onFileSelect) {
      selectedRef.current = entry.path;
      onFileSelect(entry.path, "full");
    } else {
      setAutoFollow(false); setViewMode("full"); setMdPreview(isMdFile(entry.name) || isCsvFile(entry.name));
      setSelectedFromChanges(false);
      loadFile(entry, false);
    }
  }, [treeOnly, onFileSelect, loadFile]);

  const loadFileRef = useRef(loadFile);
  loadFileRef.current = loadFile;
  useEffect(() => {
    if (treeOnly || !openPath?.path) return;
    const name = openPath.path.split("/").pop() ?? openPath.path;
    const isSqlite = isSqliteFile(name);
    const isPdf = isPdfFile(name);
    const isArchive = isArchiveFile(name);
    const entry: FileEntry = { name, path: openPath.path, type: "file", size: null, is_text: !isSqlite && !isPdf && !isArchive, is_skipped: false, is_sqlite: isSqlite, is_archive: isArchive };
    setAutoFollow(false);
    if (openPath.viewMode) setViewMode(openPath.viewMode);
    setMdPreview(isMdFile(name));
    loadFileRef.current(entry, false);
  }, [openPath?.v, treeOnly]); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll changed files
  useEffect(() => {
    let mounted = true;
    const poll = async () => {
      try {
        const files = await getCodeChangedFiles(sessionId);
        if (!mounted) return;
        setChangedFiles(files);

        // Bump refreshKey on any change to the changed-files set (additions OR
        // removals) so deletions, reverts, and new files all refresh the tree.
        const nextSet = new Set(files.map(f => f.path));
        const prev = prevChangedRef.current;
        const sameSize = nextSet.size === prev.size;
        const sameMembers = sameSize && [...nextSet].every(p => prev.has(p));
        if (!sameMembers) setFilesRefreshKey(k => k + 1);
        prevChangedRef.current = nextSet;

        if (treeOnly) return; // FileViewerPane handles its own refresh

        if (!autoFollowRef.current) {
          if (selectedEntry && !isImage(selectedEntry.name) && !selectedEntry.is_sqlite && !selectedEntry.is_archive) {
            const data = await getCodeFile(sessionId, selectedEntry.path).catch(() => null);
            if (mounted && data) setFileData(data);
          }
          return;
        }

        if (files.length > 0) {
          const topPath = files[0].path;
          if (topPath !== selectedEntry?.path) {
            const dir = topPath.includes("/") ? topPath.split("/").slice(0, -1).join("/") : "";
            try {
              const res = await listFiles(sessionId, dir || undefined);
              const name = topPath.split("/").pop() ?? topPath;
              const entry = res.entries.find((e) => e.name === name) ?? {
                name, path: topPath, type: "file" as const, size: null, is_text: true, is_skipped: false, is_sqlite: false, is_archive: false,
              };
              if (mounted) loadFile(entry, true);
            } catch {/* ignore */}
          } else if (selectedEntry && !isImage(selectedEntry.name) && !selectedEntry.is_sqlite && !selectedEntry.is_archive) {
            const data = await getCodeFile(sessionId, selectedEntry.path).catch(() => null);
            if (mounted && data) setFileData(data);
          }
        }
      } catch {/* ignore */}
    };

    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { mounted = false; clearInterval(id); };
  }, [sessionId, loadFile, treeOnly, selectedEntry]);

  useEffect(() => {
    setChangedFiles([]); setSelectedEntry(null); setFileData(null);
    setAutoFollow(true); setViewMode("full"); setMdPreview(true);
    setSelectedFromChanges(true);
    selectedRef.current = null;
    prevChangedRef.current = new Set();
    setFilesRefreshKey(0);
  }, [sessionId]);

  const changedSet = new Set(changedFiles.map((f) => f.path));
  const highlightedPath = treeOnly ? (selectedPathExternal ?? null) : (selectedEntry?.path ?? null);
  const selectedChanged = changedFiles.find((f) => f.path === (treeOnly ? selectedPathExternal : selectedEntry?.path));

  // ── Action handlers ─────────────────────────────────────────────────────────
  const bumpFilesRefresh = useCallback(() => setFilesRefreshKey((k) => k + 1), []);

  const handleCreateFile = useCallback(async () => {
    const name = newName.trim();
    if (!name) { setToolError("filename required"); return; }
    if (/[/\\]/.test(name)) { setToolError("name cannot contain / or \\"); return; }
    setToolBusy(true); setToolError("");
    try {
      const path = newParent ? `${newParent}/${name}` : name;
      await writeFile(sessionId, path, "");
      bumpFilesRefresh();
      setToolForm(null);
    } catch (e) { setToolError(String(e)); }
    finally { setToolBusy(false); }
  }, [sessionId, newName, newParent, bumpFilesRefresh]);

  const handleCreateFolder = useCallback(async () => {
    const name = newName.trim();
    if (!name) { setToolError("folder name required"); return; }
    if (/[/\\]/.test(name)) { setToolError("name cannot contain / or \\"); return; }
    setToolBusy(true); setToolError("");
    try {
      const path = newParent ? `${newParent}/${name}` : name;
      await createDir(sessionId, path);
      bumpFilesRefresh();
      setToolForm(null);
    } catch (e) { setToolError(String(e)); }
    finally { setToolBusy(false); }
  }, [sessionId, newName, newParent, bumpFilesRefresh]);

  const handleUpload = useCallback(async () => {
    if (!uploadPending) { setToolError("pick a file first"); return; }
    setToolBusy(true); setToolError("");
    try {
      await uploadFile(sessionId, uploadDir, uploadPending);
      bumpFilesRefresh();
      setToolForm(null);
    } catch (e) { setToolError(String(e)); }
    finally { setToolBusy(false); }
  }, [sessionId, uploadDir, uploadPending, bumpFilesRefresh]);

  const handleRenameCommit = useCallback(async () => {
    if (!renameTarget) return;
    const v = renameTarget.value.trim();
    if (!v) { setToolError("new name required"); return; }
    if (/[/\\]/.test(v)) { setToolError("name cannot contain / or \\"); return; }
    if (v === renameTarget.entry.name) { setRenameTarget(null); return; }
    setToolBusy(true); setToolError("");
    try {
      await renameEntry(sessionId, renameTarget.entry.path, v);
      bumpFilesRefresh();
      setRenameTarget(null);
    } catch (e) { setToolError(String(e)); }
    finally { setToolBusy(false); }
  }, [sessionId, renameTarget, bumpFilesRefresh]);

  const handleMoveCommit = useCallback(async () => {
    if (!moveTarget) return;
    setToolBusy(true); setToolError("");
    try {
      await moveEntry(sessionId, moveTarget.entry.path, moveTarget.dest);
      bumpFilesRefresh();
      setMoveTarget(null);
    } catch (e) { setToolError(String(e)); }
    finally { setToolBusy(false); }
  }, [sessionId, moveTarget, bumpFilesRefresh]);

  const handleDeleteCommit = useCallback(async () => {
    if (!deleteTarget) return;
    setToolBusy(true); setToolError("");
    try {
      await deleteEntry(sessionId, deleteTarget.entry.path, deleteTarget.recursive);
      bumpFilesRefresh();
      setDeleteTarget(null);
    } catch (e) { setToolError(String(e)); }
    finally { setToolBusy(false); }
  }, [sessionId, deleteTarget, bumpFilesRefresh]);

  const openGitHistory = useCallback(async (path: string) => {
    setHistoryTarget({ path, log: [], loading: true, selected: null });
    try {
      const log = await getFileGitLog(sessionId, path, 50);
      setHistoryTarget((h) => h && h.path === path ? { ...h, log, loading: false } : h);
    } catch (e) {
      setToolError(String(e));
      setHistoryTarget((h) => h && h.path === path ? { ...h, loading: false } : h);
    }
  }, [sessionId]);

  const loadHistoryCommit = useCallback(async (commit: string, mode: "diff" | "full" = "diff") => {
    if (!historyTarget) return;
    const path = historyTarget.path;
    setHistoryTarget((h) => h ? { ...h, selected: { commit, full: "", diff: "", viewMode: mode } } : h);
    try {
      const [diffRes, fullRes] = await Promise.all([
        getFileGitDiff(sessionId, path, commit).catch(() => ({ diff: "" })),
        getFileGitShow(sessionId, path, commit).catch(() => ({ content: "" })),
      ]);
      setHistoryTarget((h) => h && h.selected?.commit === commit
        ? { ...h, selected: { commit, diff: diffRes.diff || "", full: fullRes.content || "", viewMode: mode } }
        : h);
    } catch {/* ignore */}
  }, [sessionId, historyTarget]);

  const handleHistorySearchSubmit = useCallback(() => {
    const v = historySearchPath.trim();
    if (!v) return;
    setToolForm(null);
    openGitHistory(v);
  }, [historySearchPath, openGitHistory]);

  // Context-menu actions
  const onEntryContextMenu = useCallback((e: React.MouseEvent, entry: FileEntry) => {
    e.preventDefault(); e.stopPropagation();
    setCtxMenu({ entry, x: e.clientX, y: e.clientY });
  }, []);

  // Two-row toolbar — Row 1: branch picker + Pull + Git panel (Pull and Git
  // adjacent, no gap). Row 2: history search + file-management actions.
  // Branch picker is granted `maxWidth = spare`, so it expands gradually as the
  // column widens (RTL ellipsis shows more of the name as the picker grows).
  const [toolbarRef, toolbarWidth] = useResizeWidth<HTMLDivElement>();

  const secondRowItems = useMemo(() => [
    { key: "historySearch", label: "Git history by path", icon: "⏱", active: toolForm === "historySearch", onClick: () => setToolForm(toolForm === "historySearch" ? null : "historySearch") },
    { key: "search",    label: "Search files", icon: "🔍", active: toolForm === "search",    onClick: () => setToolForm(toolForm === "search" ? null : "search") },
    { key: "newFile",   label: "New file",     icon: "+",  active: toolForm === "newFile",   onClick: () => setToolForm(toolForm === "newFile" ? null : "newFile") },
    { key: "newFolder", label: "New folder",   icon: "📁", active: toolForm === "newFolder", onClick: () => setToolForm(toolForm === "newFolder" ? null : "newFolder") },
    { key: "upload",    label: "Upload file",  icon: "⬆", active: toolForm === "upload",    onClick: () => setToolForm(toolForm === "upload" ? null : "upload") },
  ], [toolForm]);

  const branchMaxWidth = useMemo(() => {
    if (toolbarWidth <= 0) return undefined;
    const FILES_W = 32, PAD_W = 14, GAP_W = 3;
    const PULL_FULL = 60;
    const GIT_ICON_W = 24;
    // Layout: Files [gap] Picker [gap] [Pull][Git] (Pull/Git adjacent, no gap).
    const fixed = FILES_W + PAD_W + GAP_W * 2 + PULL_FULL + (onGitClick ? GIT_ICON_W : 0);
    return Math.max(40, toolbarWidth - fixed);
  }, [toolbarWidth, onGitClick]);

  return (
    <div style={{ display: "flex", flex: 1, minHeight: 0, overflow: "hidden", background: "var(--bg-base)" }}>

      {/* ── Left panel (tree) ── */}
      {!hideLeftPanel && (
        <div style={{ flex: treeOnly ? 1 : undefined, width: treeOnly ? undefined : 220, flexShrink: 0, borderRight: treeOnly ? "none" : "1px solid var(--bg-hover)", display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {/* Row 1: Files label + branch picker + Pull + Git panel (Pull/Git adjacent, no gap) */}
          <div ref={toolbarRef} style={{ padding: "4px 6px 2px", display: "flex", alignItems: "center", gap: 3, flexShrink: 0, borderBottom: "1px solid var(--bg-hover)", minWidth: 0 }}>
            <span style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", paddingLeft: 4, flexShrink: 0 }}>Files</span>
            <GitBranchPicker
              sessionId={sessionId}
              refreshKey={filesRefreshKey}
              onBranchChanged={bumpFilesRefresh}
              maxWidth={branchMaxWidth}
            />
            <div style={{ display: "flex", alignItems: "center", gap: 0, marginLeft: "auto", flexShrink: 0 }}>
              <GitPullButton sessionId={sessionId} onPulled={bumpFilesRefresh} />
              {onGitClick && (
                <button title="Git panel" onClick={onGitClick} style={toolbarIconBtn(false)}>⎇</button>
              )}
            </div>
          </div>

          {/* Row 2: history search + file management actions */}
          <div style={{ padding: "2px 6px 4px", display: "flex", alignItems: "center", gap: 2, flexShrink: 0, borderBottom: "1px solid var(--bg-hover)", minWidth: 0 }}>
            {secondRowItems.map(it => (
              <button key={it.key} title={it.label} onClick={it.onClick} style={toolbarIconBtn(it.active)}>{it.icon}</button>
            ))}
          </div>

          {/* Inline forms */}
          {toolForm && (
            <div style={{ padding: "6px 8px 8px", borderBottom: "1px solid var(--bg-hover)", background: "var(--bg-base)", flexShrink: 0 }}>
              {toolForm === "search" && (
                <input
                  autoFocus
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Escape") setToolForm(null); }}
                  placeholder="Search files…"
                  style={inlineInputStyle}
                />
              )}
              {(toolForm === "newFile" || toolForm === "newFolder") && (
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Parent directory:</div>
                  <DirPicker sessionId={sessionId} value={newParent} onChange={setNewParent} />
                  <div style={{ display: "flex", gap: 4 }}>
                    <input
                      autoFocus
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") { e.preventDefault(); (toolForm === "newFile" ? handleCreateFile : handleCreateFolder)(); }
                        if (e.key === "Escape") setToolForm(null);
                      }}
                      placeholder={toolForm === "newFile" ? "filename.py" : "new-dir-name"}
                      style={{ ...inlineInputStyle, flex: 1 }}
                    />
                    <button onClick={toolForm === "newFile" ? handleCreateFile : handleCreateFolder} disabled={toolBusy || !newName.trim()}
                      style={primaryBtn}>{toolBusy ? "…" : "OK"}</button>
                    <button onClick={() => setToolForm(null)} style={ghostBtn}>✕</button>
                  </div>
                  {newName.trim() && (
                    <div style={{ fontSize: 10, color: "var(--text-faint)", fontFamily: "monospace" }}>
                      → {newParent ? `${newParent}/${newName.trim()}` : newName.trim()}
                    </div>
                  )}
                </div>
              )}
              {toolForm === "upload" && (
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Upload to:</div>
                  <DirPicker sessionId={sessionId} value={uploadDir} onChange={setUploadDir} />
                  <input ref={uploadInputRef} type="file" style={{ display: "none" }}
                    onChange={(e) => setUploadPending(e.target.files?.[0] ?? null)} />
                  <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                    <button onClick={() => uploadInputRef.current?.click()} style={ghostBtn}>Choose…</button>
                    <span style={{ fontSize: 10, color: uploadPending ? "var(--text-secondary)" : "var(--text-faint)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                      {uploadPending ? uploadPending.name : "No file chosen"}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 4 }}>
                    <button onClick={handleUpload} disabled={toolBusy || !uploadPending} style={primaryBtn}>{toolBusy ? "Uploading…" : "Upload"}</button>
                    <button onClick={() => setToolForm(null)} style={ghostBtn}>✕</button>
                  </div>
                </div>
              )}
              {toolForm === "historySearch" && (
                <div style={{ display: "flex", gap: 4 }}>
                  <input
                    autoFocus
                    value={historySearchPath}
                    onChange={(e) => setHistorySearchPath(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); handleHistorySearchSubmit(); } if (e.key === "Escape") setToolForm(null); }}
                    placeholder="relative/path/to/file"
                    style={{ ...inlineInputStyle, flex: 1 }}
                  />
                  <button onClick={handleHistorySearchSubmit} disabled={!historySearchPath.trim()} style={primaryBtn}>Go</button>
                  <button onClick={() => setToolForm(null)} style={ghostBtn}>✕</button>
                </div>
              )}
              {toolError && <div style={{ fontSize: 10, color: "var(--accent-red)", marginTop: 4 }}>{toolError}</div>}
            </div>
          )}

          {/* File tree / search results (fills remaining space) */}
          <div style={{ overflowY: "auto", flex: 1, paddingTop: 4 }}>
            {toolForm === "search" && searchQuery.trim() ? (
              searchLoading ? (
                <div style={{ padding: "8px 12px", fontSize: 11, color: "var(--text-faint)" }}>Searching…</div>
              ) : searchResults && searchResults.length === 0 ? (
                <div style={{ padding: "8px 12px", fontSize: 11, color: "var(--text-faint)" }}>No matches</div>
              ) : (
                searchResults?.map((e) => (
                  <div key={e.path}
                    onClick={() => handleSelect(e)}
                    onContextMenu={(ev) => onEntryContextMenu(ev, e)}
                    style={{ padding: "3px 10px", fontSize: 12, cursor: "pointer", color: "var(--text-secondary)", display: "flex", alignItems: "center", gap: 5, borderBottom: "1px solid var(--bg-deep)" }}
                    onMouseEnter={(el) => { el.currentTarget.style.background = "var(--bg-surface)"; }}
                    onMouseLeave={(el) => { el.currentTarget.style.background = "transparent"; }}>
                    <FileIcon name={e.name} isDir={e.type === "dir"} />
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, fontFamily: "monospace" }}>{e.path}</span>
                  </div>
                ))
              )
            ) : (
              <FileTree
                sessionId={sessionId}
                selected={highlightedPath}
                changed={changedSet}
                onSelect={handleSelect}
                onEntryContextMenu={onEntryContextMenu}
                revealPath={highlightedPath}
                refreshKey={filesRefreshKey}
              />
            )}
          </div>

          {/* Changes (middle) */}
          <div style={{ borderTop: "1px solid var(--bg-hover)", flexShrink: 0 }}>
            <div style={{ padding: "5px 10px", fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span>Changes ({changedFiles.length})</span>
              {!treeOnly && (
                <button
                  onClick={() => setAutoFollow((v) => !v)}
                  title="Watch latest changed file"
                  style={{ fontSize: 9, padding: "1px 6px", background: autoFollow ? "var(--bg-modal)" : "var(--bg-hover)", color: autoFollow ? "#60a5fa" : "var(--text-muted)", border: `1px solid ${autoFollow ? "#2563eb" : "var(--text-faintest)"}`, borderRadius: 3, cursor: "pointer" }}
                >
                  {autoFollow ? "● WATCHING" : "○ WATCHING"}
                </button>
              )}
            </div>
            {changedFiles.length === 0 ? (
              <div style={{ padding: "6px 12px", color: "var(--text-faint)", fontSize: 11 }}>No changes</div>
            ) : (
              <div style={{ maxHeight: treeOnly ? 240 : 200, overflowY: "auto" }}>
                {buildChangesTree(changedFiles).map(node => (
                  <ChangesNodeRow
                    key={node.path}
                    node={node}
                    depth={0}
                    selectedPath={highlightedPath}
                    onClickFile={(_, path) => {
                      if (treeOnly && onFileSelect) {
                        selectedRef.current = path;
                        onFileSelect(path, "split");
                      } else {
                        setAutoFollow(false); setViewMode("split"); setSelectedFromChanges(true);
                        const name = path.split("/").pop() ?? path;
                        const entry: FileEntry = { name, path, type: "file", size: null, is_text: true, is_skipped: false, is_sqlite: false, is_archive: false };
                        loadFile(entry, true);
                      }
                    }}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Recent commits (bottom) */}
          <RecentCommitsPanel sessionId={sessionId} />
        </div>
      )}

      {/* ── Right: viewer (only in non-treeOnly mode) ── */}
      {!treeOnly && (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          {selectedEntry ? (
            <ViewerHeader
              path={selectedEntry.path}
              selectedChanged={selectedChanged}
              fileData={fileData}
              showImage={isImage(selectedEntry.name)}
              showSqlite={selectedEntry.is_sqlite ?? false}
              isMd={isMdFile(selectedEntry.name)}
              isCsv={isCsvFile(selectedEntry.name)}
              mdPreview={mdPreview}
              setMdPreview={setMdPreview}
              viewMode={viewMode}
              setViewMode={setViewMode}
              noDiff={!selectedFromChanges}
            />
          ) : (
            <div style={{ padding: "4px 14px", borderBottom: "1px solid var(--bg-hover)", flexShrink: 0, display: "flex", alignItems: "center", background: "var(--bg-base)", fontSize: 12, minHeight: 28 }}>
              <span style={{ color: "var(--text-faint)" }}>Select a file</span>
            </div>
          )}
          <ViewerContent
            sessionId={sessionId}
            entry={selectedEntry}
            fileData={fileData}
            fileLoading={fileLoading}
            scrollToFirst={scrollToFirst}
            viewMode={viewMode}
            mdPreview={mdPreview}
            noDiff={!selectedFromChanges}
          />
        </div>
      )}

      {/* ── Context menu ── */}
      {ctxMenu && (
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            position: "fixed", top: ctxMenu.y, left: ctxMenu.x, zIndex: 9000,
            background: "var(--bg-modal)", border: "1px solid var(--border)", borderRadius: 6,
            boxShadow: "0 6px 16px rgba(0,0,0,0.4)", minWidth: 160, padding: 4,
          }}
        >
          <CtxItem label="Rename…" onClick={() => { setRenameTarget({ entry: ctxMenu.entry, value: ctxMenu.entry.name }); setCtxMenu(null); }} />
          <CtxItem label="Move to…" onClick={() => { setMoveTarget({ entry: ctxMenu.entry, dest: "" }); setCtxMenu(null); }} />
          {ctxMenu.entry.type === "file" && (
            <CtxItem label="Git history" onClick={() => { openGitHistory(ctxMenu.entry.path); setCtxMenu(null); }} />
          )}
          <div style={{ height: 1, background: "var(--bg-hover)", margin: "3px 0" }} />
          <CtxItem
            label={ctxMenu.entry.type === "dir" ? "Delete (recursive)…" : "Delete…"}
            destructive
            onClick={() => { setDeleteTarget({ entry: ctxMenu.entry, recursive: ctxMenu.entry.type === "dir" }); setCtxMenu(null); }}
          />
        </div>
      )}

      {/* ── Rename modal ── */}
      {renameTarget && (
        <SmallModal title={`Rename ${renameTarget.entry.type === "dir" ? "folder" : "file"}`} onClose={() => setRenameTarget(null)}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontFamily: "monospace", wordBreak: "break-all" }}>
            {renameTarget.entry.path}
          </div>
          <input
            autoFocus
            value={renameTarget.value}
            onChange={(e) => setRenameTarget((t) => t ? { ...t, value: e.target.value } : t)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); handleRenameCommit(); } if (e.key === "Escape") setRenameTarget(null); }}
            style={inlineInputStyle}
          />
          {toolError && <div style={{ fontSize: 10, color: "var(--accent-red)", marginTop: 4 }}>{toolError}</div>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, marginTop: 10 }}>
            <button onClick={() => setRenameTarget(null)} style={ghostBtn}>Cancel</button>
            <button onClick={handleRenameCommit} disabled={toolBusy || !renameTarget.value.trim()} style={primaryBtn}>{toolBusy ? "…" : "Rename"}</button>
          </div>
        </SmallModal>
      )}

      {/* ── Move modal ── */}
      {moveTarget && (
        <SmallModal title="Move to" onClose={() => setMoveTarget(null)}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontFamily: "monospace", wordBreak: "break-all" }}>
            {moveTarget.entry.path}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>Destination directory:</div>
          <DirPicker sessionId={sessionId} value={moveTarget.dest} onChange={(p) => setMoveTarget((t) => t ? { ...t, dest: p } : t)} />
          <div style={{ fontSize: 10, color: "var(--text-faint)", fontFamily: "monospace", marginTop: 6 }}>
            → {moveTarget.dest ? `${moveTarget.dest}/${moveTarget.entry.name}` : moveTarget.entry.name}
          </div>
          {toolError && <div style={{ fontSize: 10, color: "var(--accent-red)", marginTop: 4 }}>{toolError}</div>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, marginTop: 10 }}>
            <button onClick={() => setMoveTarget(null)} style={ghostBtn}>Cancel</button>
            <button onClick={handleMoveCommit} disabled={toolBusy} style={primaryBtn}>{toolBusy ? "…" : "Move"}</button>
          </div>
        </SmallModal>
      )}

      {/* ── Delete confirm ── */}
      {deleteTarget && (
        <SmallModal title={`Delete ${deleteTarget.entry.type === "dir" ? "folder" : "file"}?`} onClose={() => setDeleteTarget(null)}>
          <div style={{ fontSize: 12, color: "var(--text-primary)", marginBottom: 8 }}>
            This action cannot be undone.
          </div>
          <div style={{ fontSize: 11, color: "var(--text-secondary)", fontFamily: "monospace", wordBreak: "break-all", padding: "6px 8px", background: "var(--bg-base)", borderRadius: 4, border: "1px solid var(--border)" }}>
            {deleteTarget.entry.path}
          </div>
          {deleteTarget.entry.type === "dir" && (
            <label style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 10, fontSize: 11, color: "var(--text-secondary)", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={deleteTarget.recursive}
                onChange={(e) => setDeleteTarget((t) => t ? { ...t, recursive: e.target.checked } : t)}
              />
              Recursive (delete folder and all contents)
            </label>
          )}
          {toolError && <div style={{ fontSize: 10, color: "var(--accent-red)", marginTop: 6 }}>{toolError}</div>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, marginTop: 12 }}>
            <button onClick={() => setDeleteTarget(null)} style={ghostBtn}>Cancel</button>
            <button onClick={handleDeleteCommit} disabled={toolBusy}
              style={{ ...primaryBtn, background: "var(--accent-red)" }}>{toolBusy ? "…" : "Delete"}</button>
          </div>
        </SmallModal>
      )}

      {/* ── Git history modal ── */}
      {historyTarget && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 9000, display: "flex", alignItems: "center", justifyContent: "center" }}
          onClick={() => setHistoryTarget(null)}>
          <div onClick={(e) => e.stopPropagation()}
            style={{ width: "min(1100px, 95vw)", height: "min(700px, 90vh)", background: "var(--bg-modal)", border: "1px solid var(--border)", borderRadius: 8, display: "flex", flexDirection: "column", overflow: "hidden" }}>
            <div style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>Git history</span>
              <span style={{ fontSize: 12, fontFamily: "monospace", color: "var(--text-secondary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{historyTarget.path}</span>
              {historyTarget.selected && (
                <div style={{ display: "flex", gap: 4 }}>
                  <button onClick={() => setHistoryTarget((h) => h && h.selected ? { ...h, selected: { ...h.selected, viewMode: "diff" } } : h)}
                    style={{ ...toolbarIconBtn(historyTarget.selected.viewMode === "diff"), width: "auto", padding: "2px 10px", fontSize: 11 }}>Diff</button>
                  <button onClick={() => setHistoryTarget((h) => h && h.selected ? { ...h, selected: { ...h.selected, viewMode: "full" } } : h)}
                    style={{ ...toolbarIconBtn(historyTarget.selected.viewMode === "full"), width: "auto", padding: "2px 10px", fontSize: 11 }}>Full</button>
                </div>
              )}
              <button onClick={() => setHistoryTarget(null)} style={ghostBtn}>✕</button>
            </div>
            <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
              <div style={{ width: historyTarget.selected ? 300 : "100%", overflowY: "auto", borderRight: historyTarget.selected ? "1px solid var(--border)" : "none", flexShrink: 0 }}>
                {historyTarget.loading ? (
                  <div style={{ padding: 24, color: "var(--text-muted)", fontSize: 12, textAlign: "center" }}>Loading…</div>
                ) : historyTarget.log.length === 0 ? (
                  <div style={{ padding: 24, color: "var(--text-muted)", fontSize: 12, textAlign: "center" }}>No history found</div>
                ) : historyTarget.log.map((entry) => {
                  const isSel = historyTarget.selected?.commit === entry.hash;
                  return (
                    <div key={entry.hash} onClick={() => loadHistoryCommit(entry.hash, historyTarget.selected?.viewMode ?? "diff")}
                      style={{ padding: "7px 12px", borderBottom: "1px solid var(--bg-hover)", cursor: "pointer", background: isSel ? "rgba(88,166,255,0.12)" : "transparent" }}
                      onMouseEnter={(e) => { if (!isSel) e.currentTarget.style.background = "var(--bg-hover)"; }}
                      onMouseLeave={(e) => { if (!isSel) e.currentTarget.style.background = "transparent"; }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11 }}>
                        <span style={{ fontFamily: "monospace", color: "var(--accent-blue)" }}>{entry.short_hash}</span>
                        <span style={{ color: "var(--text-muted)" }}>{entry.date.slice(0, 16).replace("T", " ")}</span>
                        <span style={{ color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{entry.author}</span>
                      </div>
                      <div style={{ fontSize: 12, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginTop: 2 }}>{entry.subject}</div>
                    </div>
                  );
                })}
              </div>
              {historyTarget.selected && (
                <div style={{ flex: 1, overflow: "auto", background: "var(--bg-base)", padding: 0 }}>
                  {historyTarget.selected.viewMode === "diff"
                    ? <pre style={{ margin: 0, padding: 12, fontSize: 11, fontFamily: "monospace", color: "var(--text-primary)", whiteSpace: "pre", overflow: "visible" }}>{historyTarget.selected.diff || "(loading)"}</pre>
                    : <pre style={{ margin: 0, padding: 12, fontSize: 11, fontFamily: "monospace", color: "var(--text-primary)", whiteSpace: "pre", overflow: "visible" }}>{historyTarget.selected.full || "(loading)"}</pre>
                  }
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Helpers for toolbar/forms/modals ────────────────────────────────────────
function toolbarIconBtn(active: boolean): React.CSSProperties {
  return {
    width: 24, height: 22, fontSize: 12, padding: 0,
    background: active ? "color-mix(in srgb, var(--accent-blue) 18%, transparent)" : "transparent",
    color: active ? "var(--accent-blue)" : "var(--text-secondary)",
    border: `1px solid ${active ? "var(--accent-blue)" : "transparent"}`,
    borderRadius: 4, cursor: "pointer",
    display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
  };
}
const inlineInputStyle: React.CSSProperties = {
  width: "100%", background: "var(--bg-base)", border: "1px solid var(--border)",
  borderRadius: 3, padding: "3px 6px", color: "var(--text-body)", fontSize: 11,
  outline: "none", boxSizing: "border-box",
};
const primaryBtn: React.CSSProperties = {
  background: "var(--accent-blue)", color: "#fff", fontSize: 11, padding: "2px 10px",
  border: "none", borderRadius: 3, cursor: "pointer", flexShrink: 0,
};
const ghostBtn: React.CSSProperties = {
  background: "var(--bg-hover)", color: "var(--text-body)", fontSize: 11, padding: "2px 8px",
  border: "none", borderRadius: 3, cursor: "pointer", flexShrink: 0,
};
function CtxItem({ label, onClick, destructive }: { label: string; onClick: () => void; destructive?: boolean }) {
  return (
    <div
      onClick={onClick}
      style={{ padding: "6px 12px", fontSize: 12, cursor: "pointer", borderRadius: 3, color: destructive ? "var(--accent-red)" : "var(--text-primary)" }}
      onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-hover)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
    >
      {label}
    </div>
  );
}
function SmallModal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 9000, display: "flex", alignItems: "center", justifyContent: "center" }}
      onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ width: "min(440px, 92vw)", background: "var(--bg-modal)", border: "1px solid var(--border)", borderRadius: 8, padding: 14, color: "var(--text-primary)" }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>{title}</div>
        {children}
      </div>
    </div>
  );
}
