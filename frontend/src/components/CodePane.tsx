import { useState, useEffect, useRef, useCallback } from "react";
import hljs from "highlight.js/lib/common";
import { marked } from "../lib/markdown";
import {
  getCodeChangedFiles, getCodeFile,
  listFiles, fetchRawFileBlob,
  type ChangedFile, type FileData, type FileEntry,
} from "../api/sessionApi";
import { SqliteViewer, CsvViewer, ArchiveViewer, copyText } from "./FileEditorModal";

const POLL_MS = 8000;

// ── File icons (copied from FileEditorModal) ──────────────────────────────

const EXT_ICONS: Record<string, string> = {
  py: "🐍", js: "📜", ts: "📘", tsx: "⚛️", jsx: "⚛️",
  json: "{ }", yaml: "📋", yml: "📋", toml: "📋",
  md: "📝", txt: "📄", csv: "📊", sql: "🗄️",
  css: "🎨", scss: "🎨", html: "🌐", htm: "🌐",
  sh: "⚙️", bash: "⚙️", zsh: "⚙️",
  go: "🔵", rs: "🦀", java: "☕", kt: "🟣",
  c: "🔷", cpp: "🔷", h: "🔷",
  rb: "💎", php: "🐘", swift: "🍎",
  tf: "🏗️", dockerfile: "🐳", env: "🔑",
  log: "📋", lock: "🔒",
  db: "🗄️", sqlite: "🗄️", sqlite3: "🗄️",
  pdf: "📕",
  zip: "🗜️", tar: "🗜️", gz: "🗜️", bz2: "🗜️", xz: "🗜️",
  tgz: "🗜️", tbz2: "🗜️", txz: "🗜️",
  png: "🖼️", jpg: "🖼️", jpeg: "🖼️", gif: "🖼️", webp: "🖼️",
  bmp: "🖼️", ico: "🖼️", avif: "🖼️", tiff: "🖼️", tif: "🖼️", svg: "🖼️",
};
const SPECIAL_ICONS: Record<string, string> = {
  Makefile: "⚙️", Dockerfile: "🐳", ".gitignore": "🔍",
  ".env": "🔑", "package.json": "📦", "go.mod": "🔵",
  "Cargo.toml": "🦀", "pyproject.toml": "🐍",
};
function fileIcon(name: string): string {
  if (SPECIAL_ICONS[name]) return SPECIAL_ICONS[name];
  const ext = name.split(".").pop()?.toLowerCase() || "";
  return EXT_ICONS[ext] || "📄";
}

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
  sessionId, entry, depth, selected, changed, onSelect, revealPath, refreshKey,
}: {
  sessionId: string;
  entry: FileEntry;
  depth: number;
  selected: string | null;
  changed: Set<string>;
  onSelect: (entry: FileEntry) => void;
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
        <span style={{ fontSize: 13, flexShrink: 0 }}>{state.open ? "📂" : "📁"}</span>
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
                depth={depth + 1} selected={selected} changed={changed} onSelect={onSelect} revealPath={revealPath} refreshKey={refreshKey}
              />
            ) : (
              <FileTreeFile
                key={child.path} entry={child}
                depth={depth + 1} selected={selected} changed={changed} onSelect={onSelect}
              />
            )
          )}
        </div>
      )}
    </div>
  );
}

function FileTreeFile({
  entry, depth, selected, changed, onSelect,
}: {
  entry: FileEntry;
  depth: number;
  selected: string | null;
  changed: Set<string>;
  onSelect: (entry: FileEntry) => void;
}) {
  const isSelected = selected === entry.path;
  const isChanged = changed.has(entry.path);
  const indent = depth * 14 + 6;

  return (
    <div
      onClick={() => onSelect(entry)}
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
      <span style={{ fontSize: 13, flexShrink: 0 }}>{fileIcon(entry.name)}</span>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
        {entry.name}
      </span>
      {isChanged && <span style={{ color: "var(--accent-amber)", fontSize: 9, flexShrink: 0, paddingRight: 4 }}>●</span>}
    </div>
  );
}

// ── Root tree (loads top-level entries once) ──────────────────────────────

function FileTree({
  sessionId, selected, changed, onSelect, revealPath, refreshKey,
}: {
  sessionId: string;
  selected: string | null;
  changed: Set<string>;
  onSelect: (entry: FileEntry) => void;
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
            depth={0} selected={selected} changed={changed} onSelect={onSelect} revealPath={revealPath} refreshKey={refreshKey}
          />
        ) : (
          <FileTreeFile
            key={e.path} entry={e}
            depth={0} selected={selected} changed={changed} onSelect={onSelect}
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
    const parts = f.path.split("/");
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

function ChangesNodeRow({
  node, depth, selectedPath, onClickFile,
}: {
  node: ChangesNode;
  depth: number;
  selectedPath: string | null;
  onClickFile: (f: ChangedFile, path: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const isDir = node.children.length > 0;
  const indent = depth * 10 + 8;

  if (isDir) {
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
          <span style={{ fontSize: 12 }}>📁</span>
          <span>{node.name}</span>
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
      <span style={{ fontSize: 13, flexShrink: 0 }}>{fileIcon(node.name)}</span>
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

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "var(--bg-base)" }}>
      <div style={{ borderBottom: "1px solid var(--bg-hover)", flexShrink: 0 }}>
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
      <div style={{ overflowY: "auto", flex: 1, paddingTop: 4 }}>
        <div style={{ padding: "4px 10px 2px", fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
          Files
        </div>
        <FileTree sessionId={sessionId} selected={selectedPath} changed={changedSet} onSelect={handleSelect} revealPath={selectedPath} refreshKey={filesRefreshKey} />
      </div>
    </div>
  );
}

// ── File viewer header + content (shared by CodePane and FileViewerPane) ─────

function ViewerHeader({
  path, selectedChanged, fileData, showImage, showSqlite, isMd, isCsv, mdPreview, setMdPreview, viewMode, setViewMode, noDiff,
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
}) {
  const name = path.split("/").pop() ?? path;
  return (
    <div style={{
      padding: "4px 14px", borderBottom: "1px solid var(--bg-hover)", flexShrink: 0,
      display: "flex", alignItems: "center", gap: 8, background: "var(--bg-base)", fontSize: 12, minHeight: 28,
    }}>
      <span style={{ fontSize: 13, flexShrink: 0 }}>{fileIcon(name)}</span>
      <span style={{ color: "var(--text-secondary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {path}
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
          {!fileData.is_binary && (
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
        </span>
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
          <div style={{ fontSize: 32, marginBottom: 8 }}>📂</div>
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

export function FileViewerPane({ sessionId, path, viewMode: initViewMode = "full", noDiff }: {
  sessionId: string;
  path: string;
  viewMode?: "full" | "diff" | "split";
  noDiff?: boolean;
}) {
  const [fileData, setFileData] = useState<FileData | null>(null);
  const [fileLoading, setFileLoading] = useState(true);
  const [viewMode, setViewMode] = useState<"full" | "diff" | "split">(initViewMode);
  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);
  const name = path.split("/").pop() ?? path;
  const isMd = isMdFile(name);
  const isCsv = isCsvFile(name);
  const [mdPreview, setMdPreview] = useState(isMd || isCsv);

  useEffect(() => { setViewMode(initViewMode); }, [initViewMode]);
  useEffect(() => {
    const n = path.split("/").pop() ?? path;
    setMdPreview(isMdFile(n) || isCsvFile(n));
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
    const id = setInterval(() => { if (mounted) getCodeFile(sessionId, path).then(d => { if (mounted) setFileData(d); }).catch(() => {}); }, POLL_MS);
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
  const selectedChanged = changedFiles.find(f => f.path === path);
  const entry: FileEntry = { name, path, type: "file", size: null, is_text: !showSqlite && !isPdfFile(name) && !showArchive, is_skipped: false, is_sqlite: showSqlite, is_archive: showArchive };

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
      />
      <ViewerContent
        sessionId={sessionId}
        entry={entry}
        fileData={fileData}
        fileLoading={fileLoading}
        scrollToFirst={false}
        viewMode={viewMode}
        mdPreview={mdPreview}
        noDiff={noDiff}
      />
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
}: {
  sessionId: string;
  // Tree-only mode: when provided, file clicks call this instead of opening viewer inline
  onFileSelect?: (path: string, viewMode: "full" | "diff" | "split") => void;
  selectedPathExternal?: string | null;
  // Legacy: open a specific file from outside (only used in inline viewer mode)
  openPath?: { path: string; v: number; viewMode?: "full" | "diff" | "split" } | null;
  hideLeftPanel?: boolean;
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

  const autoFollowRef = useRef(autoFollow);
  autoFollowRef.current = autoFollow;
  const selectedRef = useRef<string | null>(null);
  const prevChangedRef = useRef<Set<string>>(new Set());

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

  return (
    <div style={{ display: "flex", flex: 1, minHeight: 0, overflow: "hidden", background: "var(--bg-base)" }}>

      {/* ── Left panel (tree) ── */}
      {!hideLeftPanel && (
        <div style={{ flex: treeOnly ? 1 : undefined, width: treeOnly ? undefined : 220, flexShrink: 0, borderRight: treeOnly ? "none" : "1px solid var(--bg-hover)", display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {/* Changes */}
          <div style={{ borderBottom: "1px solid var(--bg-hover)", flexShrink: 0 }}>
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

          {/* File tree */}
          <div style={{ overflowY: "auto", flex: 1, paddingTop: 4 }}>
            <div style={{ padding: "4px 10px 2px", fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Files
            </div>
            <FileTree
              sessionId={sessionId}
              selected={highlightedPath}
              changed={changedSet}
              onSelect={handleSelect}
              revealPath={highlightedPath}
              refreshKey={filesRefreshKey}
            />
          </div>
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
    </div>
  );
}
