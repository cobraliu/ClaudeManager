import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import hljs from "highlight.js/lib/common";
import {
  getMergeStatus,
  gitMergeStart,
  gitMergeAbort,
  gitMergeContinue,
  getMergeConflictFile,
  gitResolveFile,
  type MergeStatus,
  type ConflictFileVersions,
  type GitBranchInfo,
} from "../api/sessionApi";

interface Props {
  sessionId: string;
  branches: GitBranchInfo;
  setMsg: (msg: string | null) => void;
  /** Called when the merge fully completes (commit succeeded) or is aborted. */
  onCompleted: () => void;
}

export function MergeTab({ sessionId, branches, setMsg, onCompleted }: Props) {
  const [status, setStatus] = useState<MergeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState<string>("");
  const [target, setTarget] = useState<string>("");
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(() => {
    return getMergeStatus(sessionId)
      .then(s => { setStatus(s); return s; })
      .catch(e => { setErr(String(e)); return null; });
  }, [sessionId]);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  // Default target to main/master; leave source empty (user must pick).
  useEffect(() => {
    if (target || !branches.local.length) return;
    const def = branches.local.includes("main")
      ? "main"
      : branches.local.includes("master")
        ? "master"
        : branches.current || branches.local[0];
    setTarget(def);
  }, [target, branches]);

  const handleStart = async () => {
    if (!source || !target) return;
    if (source === target) {
      setErr("Source and target must be different branches.");
      return;
    }
    setStarting(true);
    setErr(null);
    try {
      const r = await gitMergeStart(sessionId, source, target);
      if (r.up_to_date) {
        setMsg(`${target} is already up to date with ${source}.`);
        onCompleted();
        return;
      }
      if (r.clean) {
        setMsg(`Merged ${source} into ${target} cleanly.`);
        onCompleted();
        return;
      }
      // Conflict — refresh status to enter resolver mode.
      await refresh();
    } catch (e) {
      setErr(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setStarting(false);
    }
  };

  if (loading) {
    return <div style={{ padding: 16, color: "var(--text-muted)", fontSize: 13 }}>Loading merge status…</div>;
  }

  if (status?.in_progress) {
    return (
      <MergeResolver
        sessionId={sessionId}
        status={status}
        onStatusChange={setStatus}
        onCompleted={() => { setStatus(null); onCompleted(); }}
        setMsg={setMsg}
      />
    );
  }

  const allBranches = branches.local;
  const canStart = !!source && !!target && source !== target && !starting;
  const selectStyle: React.CSSProperties = {
    background: "var(--bg-surface)", color: "var(--text-body)",
    border: "1px solid var(--text-faintest)", borderRadius: 4,
    padding: "4px 8px", fontSize: 12, fontFamily: "monospace",
  };

  return (
    <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12, fontSize: 13, color: "var(--text-body)" }}>
      <div style={{ color: "var(--text-secondary)" }}>
        Merge a source branch into a target branch.
      </div>
      {allBranches.length < 2 ? (
        <div style={{ color: "var(--text-muted)" }}>Need at least 2 local branches to merge.</div>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Source:</label>
            <select value={source} onChange={(e) => setSource(e.target.value)} style={selectStyle}>
              <option value="">— select source —</option>
              {allBranches.map(b => <option key={b} value={b}>{b}</option>)}
            </select>
            <span style={{ color: "var(--text-faint)" }}>→</span>
            <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Target:</label>
            <select value={target} onChange={(e) => setTarget(e.target.value)} style={selectStyle}>
              <option value="">— select target —</option>
              {allBranches.map(b => <option key={b} value={b}>{b}</option>)}
            </select>
            <button
              disabled={!canStart}
              onClick={handleStart}
              style={{ background: canStart ? "var(--accent-blue)" : "var(--bg-hover)", color: canStart ? "#fff" : "var(--text-faint)", fontSize: 12, padding: "4px 14px" }}
            >
              {starting ? "Merging…" : "Start Merge"}
            </button>
          </div>
          {source && target && source === target && (
            <div style={{ fontSize: 11, color: "var(--accent-amber)" }}>Source and target must differ.</div>
          )}
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            Checks out <span style={{ fontFamily: "monospace" }}>{target || "<target>"}</span> (if not already), then runs
            {" "}<span style={{ fontFamily: "monospace" }}>git merge --no-ff --no-edit {source || "<source>"}</span>.
            On conflict, you'll get a 3-pane resolver for each conflicted file.
          </div>
        </>
      )}
      {err && (
        <div style={{ background: "rgba(248,81,73,0.12)", border: "1px solid var(--accent-red)", borderRadius: 4, padding: "8px 10px", color: "var(--text-body)", fontSize: 12, whiteSpace: "pre-wrap" }}>
          {err}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Resolver: per-file 3-pane editor with hunk-level Accept buttons

interface ConflictHunk {
  startLine: number;    // 0-based line index of <<<<<<<
  endLine: number;      // 0-based line index of >>>>>>>
  ours: string[];
  theirs: string[];
}

function parseConflictHunks(content: string): ConflictHunk[] {
  const lines = content.split("\n");
  const hunks: ConflictHunk[] = [];
  let i = 0;
  while (i < lines.length) {
    if (lines[i].startsWith("<<<<<<<")) {
      const startLine = i;
      const ours: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("=======") && !lines[i].startsWith(">>>>>>>")) {
        ours.push(lines[i]);
        i++;
      }
      const theirs: string[] = [];
      if (i < lines.length && lines[i].startsWith("=======")) {
        i++;
        while (i < lines.length && !lines[i].startsWith(">>>>>>>")) {
          theirs.push(lines[i]);
          i++;
        }
      }
      if (i < lines.length && lines[i].startsWith(">>>>>>>")) {
        hunks.push({ startLine, endLine: i, ours, theirs });
        i++;
      }
    } else {
      i++;
    }
  }
  return hunks;
}

/** Replace the lines [startLine..endLine] inclusive with the given replacement lines. */
function replaceLines(content: string, startLine: number, endLine: number, replacement: string[]): string {
  const lines = content.split("\n");
  lines.splice(startLine, endLine - startLine + 1, ...replacement);
  return lines.join("\n");
}

function langForPath(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const map: Record<string, string> = {
    ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
    py: "python", go: "go", rs: "rust", java: "java", c: "c", cpp: "cpp", h: "c", hpp: "cpp",
    rb: "ruby", php: "php", sh: "bash", md: "markdown", json: "json", yaml: "yaml", yml: "yaml",
    html: "html", css: "css", scss: "scss", sql: "sql", xml: "xml", toml: "ini",
  };
  return map[ext] || "plaintext";
}

function MergeResolver({
  sessionId, status, onStatusChange, onCompleted, setMsg,
}: {
  sessionId: string;
  status: MergeStatus;
  onStatusChange: (s: MergeStatus) => void;
  onCompleted: () => void;
  setMsg: (m: string | null) => void;
}) {
  const files = status.conflicted_files;
  const [activeFile, setActiveFile] = useState<string | null>(files[0] ?? null);
  const [versions, setVersions] = useState<ConflictFileVersions | null>(null);
  const [result, setResult] = useState<string>("");
  const [loadingFile, setLoadingFile] = useState(false);
  const [busy, setBusy] = useState<"abort" | "continue" | "resolve" | null>(null);
  const [confirmAbort, setConfirmAbort] = useState(false);
  // When activeFile changes, reload its versions
  useEffect(() => {
    if (!activeFile) { setVersions(null); setResult(""); return; }
    setLoadingFile(true);
    getMergeConflictFile(sessionId, activeFile)
      .then(v => { setVersions(v); setResult(v.working); })
      .catch(e => setMsg(String(e)))
      .finally(() => setLoadingFile(false));
  }, [sessionId, activeFile, setMsg]);

  // Re-pick default file when the conflicted set shrinks
  useEffect(() => {
    if (activeFile && !files.includes(activeFile)) {
      setActiveFile(files[0] ?? null);
    }
  }, [files, activeFile]);

  const hunks = useMemo(() => parseConflictHunks(result), [result]);

  const acceptHunk = (hunk: ConflictHunk, choice: "ours" | "theirs" | "both") => {
    let replacement: string[];
    if (choice === "ours") replacement = hunk.ours;
    else if (choice === "theirs") replacement = hunk.theirs;
    else replacement = [...hunk.ours, ...hunk.theirs];
    setResult(prev => replaceLines(prev, hunk.startLine, hunk.endLine, replacement));
  };

  const handleSaveResolved = async () => {
    if (!activeFile) return;
    setBusy("resolve");
    try {
      const r = await gitResolveFile(sessionId, activeFile, result);
      onStatusChange(r.status);
      setMsg(`Resolved ${activeFile}`);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(null);
    }
  };

  const handleContinue = async () => {
    setBusy("continue");
    try {
      const r = await gitMergeContinue(sessionId);
      setMsg(r.output || "Merge completed.");
      onCompleted();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(null);
    }
  };

  const doAbort = async () => {
    setBusy("abort");
    try {
      const r = await gitMergeAbort(sessionId);
      setMsg(r.output || "Merge aborted.");
      onCompleted();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(null);
      setConfirmAbort(false);
    }
  };

  const allResolved = files.length === 0;
  const hasMarkers = hunks.length > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, padding: 0 }}>
      {/* Toolbar */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 16px", background: "var(--bg-surface)", borderBottom: "1px solid var(--bg-hover)", flexShrink: 0 }}>
        <div style={{ fontSize: 12, color: "var(--text-body)" }}>
          Merging <span style={{ fontFamily: "monospace", color: "var(--accent-amber)" }}>{status.merge_head}</span> into <span style={{ fontFamily: "monospace", color: "var(--accent-blue)" }}>{status.current_branch}</span>
        </div>
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {allResolved ? "All conflicts resolved." : `${files.length} file${files.length === 1 ? "" : "s"} remaining`}
        </div>
        <div style={{ flex: 1 }} />
        <button
          disabled={busy !== null}
          onClick={() => setConfirmAbort(true)}
          style={{ background: "var(--bg-hover)", color: "var(--accent-red)", fontSize: 12, padding: "4px 12px", border: "1px solid var(--accent-red)" }}
        >
          {busy === "abort" ? "Aborting…" : "Abort Merge"}
        </button>
        <button
          disabled={!allResolved || busy !== null}
          onClick={handleContinue}
          style={{ background: allResolved && busy === null ? "#238636" : "var(--bg-hover)", color: allResolved && busy === null ? "#fff" : "var(--text-faint)", fontSize: 12, padding: "4px 14px" }}
        >
          {busy === "continue" ? "Committing…" : "Continue Merge"}
        </button>
      </div>

      {/* Body: file list + 3-pane resolver */}
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {/* File list */}
        <div style={{ width: 220, flexShrink: 0, borderRight: "1px solid var(--bg-hover)", overflowY: "auto", background: "var(--bg-surface)" }}>
          <div style={{ padding: "6px 10px", fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
            Conflicted files
          </div>
          {files.length === 0 && (
            <div style={{ padding: "6px 10px", fontSize: 12, color: "var(--accent-green)" }}>✓ All resolved</div>
          )}
          {files.map(f => (
            <div
              key={f}
              onClick={() => setActiveFile(f)}
              style={{
                padding: "5px 10px", fontSize: 12, fontFamily: "monospace", cursor: "pointer",
                background: f === activeFile ? "rgba(88,166,255,0.15)" : "transparent",
                color: f === activeFile ? "var(--accent-blue)" : "var(--text-body)",
                borderLeft: f === activeFile ? "2px solid var(--accent-blue)" : "2px solid transparent",
                wordBreak: "break-all",
              }}
              title={f}
            >
              {f}
            </div>
          ))}
        </div>

        {/* 3-pane resolver */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          {!activeFile ? (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: 13 }}>
              {allResolved ? "All files resolved — click Continue Merge." : "Select a file to resolve."}
            </div>
          ) : loadingFile || !versions ? (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: 13 }}>
              Loading {activeFile}…
            </div>
          ) : (
            <FileResolver
              path={activeFile}
              versions={versions}
              result={result}
              setResult={setResult}
              hunks={hunks}
              onAcceptHunk={acceptHunk}
              onSaveResolved={handleSaveResolved}
              saving={busy === "resolve"}
              hasMarkers={hasMarkers}
            />
          )}
        </div>
      </div>

      {/* Abort confirm */}
      {confirmAbort && (
        <div
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", zIndex: 6000, display: "flex", alignItems: "center", justifyContent: "center" }}
          onClick={busy ? undefined : () => setConfirmAbort(false)}
        >
          <div
            style={{ width: 420, maxWidth: "92vw", background: "var(--bg-base)", border: "1px solid var(--border-strong)", borderRadius: 8, display: "flex", flexDirection: "column" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--bg-hover)", fontSize: 13, fontWeight: 600 }}>Abort merge?</div>
            <div style={{ padding: "12px 14px", fontSize: 12, color: "var(--text-body)" }}>
              This restores the working tree to its state before the merge started. Any resolutions you've saved will be lost.
            </div>
            <div style={{ padding: "10px 14px", borderTop: "1px solid var(--bg-hover)", display: "flex", justifyContent: "flex-end", gap: 6 }}>
              <button onClick={() => setConfirmAbort(false)} disabled={busy !== null} style={{ background: "var(--text-faintest)", color: "var(--text-secondary)", fontSize: 12, padding: "5px 12px" }}>Cancel</button>
              <button onClick={doAbort} disabled={busy !== null} style={{ background: "var(--accent-red)", color: "#fff", fontSize: 12, padding: "5px 14px" }}>
                {busy === "abort" ? "Aborting…" : "Abort Merge"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-file 3-pane viewer

function FileResolver({
  path, versions, result, setResult, hunks, onAcceptHunk,
  onSaveResolved, saving, hasMarkers,
}: {
  path: string;
  versions: ConflictFileVersions;
  result: string;
  setResult: (s: string) => void;
  hunks: ConflictHunk[];
  onAcceptHunk: (h: ConflictHunk, choice: "ours" | "theirs" | "both") => void;
  onSaveResolved: () => void;
  saving: boolean;
  hasMarkers: boolean;
}) {
  const lang = langForPath(path);
  const [showRaw, setShowRaw] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  return (
    <>
      {/* Pane labels */}
      <div style={{ display: "flex", borderBottom: "1px solid var(--bg-hover)", background: "var(--bg-surface)", flexShrink: 0 }}>
        <div style={{ flex: 1, padding: "4px 10px", fontSize: 11, color: "var(--accent-blue)", borderRight: "1px solid var(--bg-hover)" }}>
          Current (ours)
        </div>
        <div style={{ flex: 1, padding: "4px 10px", fontSize: 11, color: "var(--accent-amber)", borderRight: "1px solid var(--bg-hover)" }}>
          Incoming (theirs)
        </div>
        <div style={{ flex: 1, padding: "4px 10px", fontSize: 11, color: "var(--text-body)", display: "flex", alignItems: "center", gap: 8 }}>
          Result
          <button
            onClick={() => setShowRaw(s => !s)}
            style={{ background: "var(--bg-hover)", color: "var(--text-secondary)", fontSize: 10, padding: "1px 6px" }}
            title={showRaw ? "Switch to hunk-by-hunk view" : "Edit raw text"}
          >
            {showRaw ? "Hunks" : "Raw"}
          </button>
        </div>
      </div>

      {/* Three panes */}
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <ReadOnlyPane content={versions.ours} lang={lang} />
        <ReadOnlyPane content={versions.theirs} lang={lang} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, borderLeft: "1px solid var(--bg-hover)" }}>
          {showRaw ? (
            <textarea
              ref={textareaRef}
              value={result}
              onChange={(e) => setResult(e.target.value)}
              spellCheck={false}
              style={{
                flex: 1, background: "var(--bg-base)", color: "var(--text-body)",
                border: "none", outline: "none", padding: 8, fontSize: 11,
                fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace',
                resize: "none", whiteSpace: "pre", overflow: "auto",
              }}
            />
          ) : (
            <ResultHunkView
              content={result}
              hunks={hunks}
              lang={lang}
              onAccept={onAcceptHunk}
            />
          )}
        </div>
      </div>

      {/* Footer: save resolved */}
      <div style={{ padding: "6px 12px", borderTop: "1px solid var(--bg-hover)", background: "var(--bg-surface)", display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {hasMarkers ? `${hunks.length} unresolved hunk${hunks.length === 1 ? "" : "s"}` : "No conflict markers — ready to mark resolved."}
        </span>
        <div style={{ flex: 1 }} />
        <button
          disabled={hasMarkers || saving}
          onClick={onSaveResolved}
          title={hasMarkers ? "Resolve all hunks first" : "git add this file"}
          style={{ background: !hasMarkers && !saving ? "var(--accent-blue)" : "var(--bg-hover)", color: !hasMarkers && !saving ? "#fff" : "var(--text-faint)", fontSize: 12, padding: "4px 14px" }}
        >
          {saving ? "Saving…" : "Mark Resolved"}
        </button>
      </div>
    </>
  );
}

function ReadOnlyPane({ content, lang }: { content: string; lang: string }) {
  const html = useMemo(() => {
    try { return hljs.highlight(content || " ", { language: lang, ignoreIllegals: true }).value; }
    catch { return escapeHtml(content); }
  }, [content, lang]);
  return (
    <div style={{ flex: 1, overflow: "auto", background: "var(--bg-base)", borderRight: "1px solid var(--bg-hover)", minWidth: 0 }}>
      <pre style={{ margin: 0, padding: 8, fontSize: 11, fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace', whiteSpace: "pre", color: "var(--text-body)" }}>
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Render the Result with each conflict hunk inline-replaced by an Accept-buttons widget. */
function ResultHunkView({
  content, hunks, lang, onAccept,
}: {
  content: string;
  hunks: ConflictHunk[];
  lang: string;
  onAccept: (h: ConflictHunk, choice: "ours" | "theirs" | "both") => void;
}) {
  const lines = content.split("\n");

  // Build a sequence of segments: { kind: "text", text } | { kind: "hunk", hunk }
  type Segment = { kind: "text"; text: string } | { kind: "hunk"; hunk: ConflictHunk };
  const segments: Segment[] = [];
  let cursor = 0;
  for (const h of hunks) {
    if (h.startLine > cursor) {
      const text = lines.slice(cursor, h.startLine).join("\n");
      segments.push({ kind: "text", text });
    }
    segments.push({ kind: "hunk", hunk: h });
    cursor = h.endLine + 1;
  }
  if (cursor < lines.length) {
    segments.push({ kind: "text", text: lines.slice(cursor).join("\n") });
  }

  return (
    <div style={{ flex: 1, overflow: "auto", background: "var(--bg-base)", minWidth: 0, padding: 0 }}>
      {segments.length === 0 && (
        <div style={{ padding: 12, fontSize: 12, color: "var(--text-muted)" }}>(empty)</div>
      )}
      {segments.map((seg, i) => {
        if (seg.kind === "text") {
          if (!seg.text) return null;
          let html = "";
          try { html = hljs.highlight(seg.text, { language: lang, ignoreIllegals: true }).value; }
          catch { html = escapeHtml(seg.text); }
          return (
            <pre key={i} style={{ margin: 0, padding: "0 8px", fontSize: 11, fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace', whiteSpace: "pre", color: "var(--text-body)" }}>
              <code dangerouslySetInnerHTML={{ __html: html }} />
            </pre>
          );
        }
        const h = seg.hunk;
        const oursHtml = (() => { try { return hljs.highlight(h.ours.join("\n"), { language: lang, ignoreIllegals: true }).value; } catch { return escapeHtml(h.ours.join("\n")); } })();
        const theirsHtml = (() => { try { return hljs.highlight(h.theirs.join("\n"), { language: lang, ignoreIllegals: true }).value; } catch { return escapeHtml(h.theirs.join("\n")); } })();
        return (
          <div key={i} style={{ margin: "4px 8px", border: "1px solid var(--accent-amber)", borderRadius: 4, overflow: "hidden", background: "var(--bg-surface)" }}>
            <div style={{ display: "flex", gap: 6, padding: "4px 8px", background: "rgba(187,128,9,0.15)", fontSize: 11, color: "var(--text-secondary)", alignItems: "center" }}>
              <span>Conflict</span>
              <div style={{ flex: 1 }} />
              <button onClick={() => onAccept(h, "ours")} title="Use the Current (ours) version" style={{ background: "var(--accent-blue)", color: "#fff", fontSize: 10, padding: "2px 8px" }}>Accept Current</button>
              <button onClick={() => onAccept(h, "theirs")} title="Use the Incoming (theirs) version" style={{ background: "var(--accent-amber)", color: "#000", fontSize: 10, padding: "2px 8px" }}>Accept Incoming</button>
              <button onClick={() => onAccept(h, "both")} title="Keep both, ours then theirs" style={{ background: "var(--bg-hover)", color: "var(--text-body)", fontSize: 10, padding: "2px 8px", border: "1px solid var(--text-faintest)" }}>Accept Both</button>
            </div>
            <div style={{ display: "flex" }}>
              <div style={{ flex: 1, borderRight: "1px solid var(--bg-hover)", background: "rgba(88,166,255,0.08)", minWidth: 0, overflow: "auto" }}>
                <div style={{ padding: "2px 6px", fontSize: 10, color: "var(--accent-blue)", background: "rgba(88,166,255,0.12)" }}>Current</div>
                <pre style={{ margin: 0, padding: "2px 8px", fontSize: 11, fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace', whiteSpace: "pre", color: "var(--text-body)" }}>
                  <code dangerouslySetInnerHTML={{ __html: oursHtml }} />
                </pre>
              </div>
              <div style={{ flex: 1, background: "rgba(187,128,9,0.08)", minWidth: 0, overflow: "auto" }}>
                <div style={{ padding: "2px 6px", fontSize: 10, color: "var(--accent-amber)", background: "rgba(187,128,9,0.12)" }}>Incoming</div>
                <pre style={{ margin: 0, padding: "2px 8px", fontSize: 11, fontFamily: '"Cascadia Code","Fira Code",Menlo,Monaco,"Courier New",monospace', whiteSpace: "pre", color: "var(--text-body)" }}>
                  <code dangerouslySetInnerHTML={{ __html: theirsHtml }} />
                </pre>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
