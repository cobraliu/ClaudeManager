import { useState, useEffect, useRef } from "react";
import type { SessionMeta, ScheduledTask, SshConfig } from "../api/sessionApi";
import { createTask, cancelTask, renameSession, makeRemoteUri } from "../api/sessionApi";
import claudeIcon from "../assets/claude.svg";
import cursorIcon from "../assets/cursor.svg";
import gitIcon from "../assets/git.svg";
import terminalIcon from "../assets/terminal.svg";
import downloadIcon from "../assets/download.svg";
import downloadChatIcon from "../assets/download-chat.svg";
import scheduleIcon from "../assets/schedule.svg";

function _relTime(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

const STATUS_COLORS: Record<string, string> = {
  creating: "#f0ad4e",
  running: "#5cb85c",
  detached: "#5bc0de",
  archived: "#777",
  terminated: "#d9534f",
};

interface Props {
  session: SessionMeta;
  isActive?: boolean;
  showOwner?: boolean;
  sshConfig?: SshConfig;
  onAttach?: () => void;
  onViewChat?: () => void;
  onTerminate?: () => void;
  onResume?: () => void;
  onDelete?: () => void;
  onTaskChange?: () => void;
  onFiles?: () => void;
  onShell?: () => void;
  onGit?: () => void;
  onJsonl?: () => void;
  onRename?: () => void;
  onDownloadCwd?: () => void;
  onDownloadChat?: () => void;
  loading?: boolean;
}

function formatRemaining(runAt: string): string {
  const ms = new Date(runAt).getTime() - Date.now();
  if (ms <= 0) return "due";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function ScheduleForm({
  sessionId,
  onCreated,
  onClose,
}: {
  sessionId: string;
  onCreated: () => void;
  onClose: () => void;
}) {
  const [command, setCommand] = useState("");
  const [delayValue, setDelayValue] = useState("5");
  const [delayUnit, setDelayUnit] = useState<"seconds" | "minutes" | "hours">("minutes");
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (!command.trim()) return;
    const multiplier = delayUnit === "seconds" ? 1 : delayUnit === "minutes" ? 60 : 3600;
    const delay_seconds = Math.max(1, parseInt(delayValue, 10) || 1) * multiplier;
    setLoading(true);
    try {
      await createTask(sessionId, command.trim(), delay_seconds);
      onCreated();
      onClose();
    } catch (e) {
      alert(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        marginTop: 8,
        padding: "10px 12px",
        background: "var(--bg-base)",
        borderRadius: 6,
        border: "1px solid var(--text-faintest)",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ fontSize: 11, color: "var(--text-secondary)", fontWeight: 600 }}>Schedule Command</div>
      <input
        autoFocus
        placeholder="Command to send..."
        value={command}
        onChange={(e) => setCommand(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") onClose(); }}
        style={inputStyle}
      />
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)", flexShrink: 0 }}>In</span>
        <input
          type="number"
          min={1}
          value={delayValue}
          onChange={(e) => setDelayValue(e.target.value)}
          style={{ ...inputStyle, width: 60, padding: "5px 8px" }}
        />
        <select
          value={delayUnit}
          onChange={(e) => setDelayUnit(e.target.value as typeof delayUnit)}
          style={{
            background: "var(--bg-hover)",
            border: "1px solid var(--text-faintest)",
            borderRadius: 4,
            color: "var(--text-body)",
            fontSize: 11,
            padding: "5px 6px",
            cursor: "pointer",
          }}
        >
          <option value="seconds">seconds</option>
          <option value="minutes">minutes</option>
          <option value="hours">hours</option>
        </select>
        <button
          disabled={loading || !command.trim()}
          onClick={submit}
          style={{ background: "var(--accent-blue)", color: "#fff", fontSize: 11, padding: "4px 10px", marginLeft: "auto" }}
        >
          {loading ? "..." : "Set"}
        </button>
        <button onClick={onClose} style={{ background: "var(--text-faintest)", color: "var(--text-secondary)", fontSize: 11, padding: "4px 8px" }}>
          ✕
        </button>
      </div>
    </div>
  );
}

function TaskChip({ task, sessionId, onCancel }: { task: ScheduledTask; sessionId: string; onCancel: () => void }) {
  const [cancelling, setCancelling] = useState(false);
  const [, setTick] = useState(0);

  // Update countdown every second locally without waiting for server poll
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const handleCancel = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setCancelling(true);
    try {
      await cancelTask(sessionId, task.id);
      onCancel();
    } catch {
      setCancelling(false);
    }
  };

  const cmd = task.command.length > 35 ? task.command.slice(0, 35) + "…" : task.command;
  const remaining = formatRemaining(task.run_at);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        background: "var(--bg-hover)",
        borderRadius: 4,
        padding: "3px 8px",
        fontSize: 11,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <img src={scheduleIcon} style={{ width: 12, height: 12, flexShrink: 0, filter: "invert(0.7) sepia(1) saturate(3) hue-rotate(10deg)" }} />
      <span style={{ color: "var(--text-secondary)", fontFamily: "monospace", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {cmd}
      </span>
      <span style={{ color: remaining === "due" ? "#f59e0b" : "var(--text-muted)", flexShrink: 0 }}>{remaining}</span>
      <button
        disabled={cancelling}
        onClick={handleCancel}
        style={{ background: "transparent", color: "var(--text-faint)", fontSize: 12, padding: "0 2px", lineHeight: 1 }}
        title="Cancel task"
      >
        ✕
      </button>
    </div>
  );
}

export function SessionCard({
  session: s,
  isActive,
  showOwner,
  sshConfig,
  onAttach,
  onViewChat,
  onTerminate,
  onResume,
  onDelete,
  onTaskChange,
  onFiles,
  onShell,
  onGit,
  onJsonl,
  onRename,
  onDownloadCwd,
  onDownloadChat,
  loading,
}: Props) {
  const canAttach = s.status === "running" || s.status === "detached";
  const canResume = s.status === "terminated";
  const canDelete = s.status === "terminated";
  const canSchedule = s.status === "running" || s.status === "detached";
  const [showSchedule, setShowSchedule] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(s.project);
  const editRef = useRef<HTMLInputElement>(null);

  const pendingTasks = s.scheduled_tasks?.filter((t) => t.status === "pending") ?? [];

  const startEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditName(s.project);
    setEditing(true);
    setTimeout(() => { editRef.current?.select(); }, 0);
  };

  const submitRename = async () => {
    const trimmed = editName.trim();
    setEditing(false);
    if (!trimmed || trimmed === s.project) return;
    try {
      await renameSession(s.id, trimmed);
      onRename?.();
    } catch {
      /* ignore */
    }
  };

  return (
    <div
      style={{
        padding: "10px 12px",
        background: isActive ? "rgba(88,166,255,0.12)" : "var(--bg-modal)",
        borderRadius: 8,
        border: isActive ? "1px solid var(--accent-blue)" : "1px solid var(--bg-hover)",
        borderLeft: isActive
          ? "3px solid var(--accent-blue)"
          : s.is_streaming
          ? "3px solid #22c55e"
          : s.has_new_output
          ? "3px solid var(--accent-red)"
          : "1px solid var(--bg-hover)",
        cursor: (canAttach && onAttach) || (canResume && onViewChat) ? "pointer" : "default",
        breakInside: "avoid",
      }}
      onClick={() => {
        if (canAttach && onAttach) { onAttach(); }
        else if (canResume && onViewChat) { onViewChat(); }
      }}
    >
      {/* row 1: owner? + project + status */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {showOwner && (
            <span style={{ fontSize: 10, background: "var(--text-faintest)", padding: "1px 5px", borderRadius: 3, color: "var(--text-secondary)" }}>
              {s.owner_id}
            </span>
          )}
          {editing ? (
            <input
              ref={editRef}
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onBlur={submitRename}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submitRename(); } if (e.key === "Escape") setEditing(false); }}
              onClick={(e) => e.stopPropagation()}
              style={{ fontSize: 13, fontWeight: 600, background: "var(--bg-base)", border: "1px solid var(--accent-blue)", borderRadius: 3, color: "var(--text-body)", padding: "1px 4px", outline: "none", width: 160 }}
              autoFocus
            />
          ) : (
            <strong style={{ fontSize: 13, cursor: "text" }} title="Click to rename" onClick={startEdit}>{s.project}</strong>
          )}
          {s.is_streaming && <span className="streaming-dot" title="Terminal is outputting" />}
          {!s.is_streaming && s.has_new_output && <span className="new-output-dot" title="New output since last view" />}
          {s.tool === "cursor" ? (
            <span style={{ fontSize: 9, background: "#4c1d95", color: "#c4b5fd", padding: "1px 5px", borderRadius: 3, fontWeight: 600 }}>
              CURSOR
            </span>
          ) : (
            <span style={{ fontSize: 9, background: "#78350f", color: "#fcd34d", padding: "1px 5px", borderRadius: 3, fontWeight: 600 }}>
              CLAUDE
            </span>
          )}
        </div>
        <span style={{ fontSize: 10, color: STATUS_COLORS[s.status] || "var(--text-muted)", fontWeight: 600, textTransform: "uppercase" }}>
          {s.status}
        </span>
      </div>

      {/* row 2: cwd */}
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3 }}>{s.cwd}</div>

      {/* row 3: claude session id + title */}
      {s.claude_session_id && (
        <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
          <span style={{ color: "var(--text-secondary)", fontFamily: "monospace" }}>{s.claude_session_id.slice(0, 8)}</span>
          {s.claude_title && <span style={{ marginLeft: 6, color: "var(--text-muted)" }}>{s.claude_title}</span>}
        </div>
      )}

      {/* row 4: prompts + last input time */}
      {s.prompts && s.prompts.length > 0 && (
        <div style={{ marginTop: 4, fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>
          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={s.prompts[0]}>
            <span style={{ color: "var(--text-faintest)", fontFamily: "monospace" }}>#0</span>{" "}<PromptText text={s.prompts[0]} />
          </div>
          {s.prompts.length >= 2 && (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }} title={s.prompts[s.prompts.length - 1]}>
                <span style={{ color: "var(--text-faintest)", fontFamily: "monospace" }}>#-1</span>{" "}<PromptText text={s.prompts[s.prompts.length - 1]} />
              </div>
              {s.last_user_input_at && <span style={{ fontSize: 10, color: "var(--text-faintest)", flexShrink: 0 }}>{_relTime(s.last_user_input_at)}</span>}
            </div>
          )}
          {s.prompts.length < 2 && s.last_user_input_at && (
            <div style={{ fontSize: 10, color: "var(--text-faintest)" }}>{_relTime(s.last_user_input_at)}</div>
          )}
        </div>
      )}

      {/* row 5: time + model + actions */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginTop: 6, flexWrap: "wrap", gap: 4 }}>
        <span style={{ fontSize: 10, color: "var(--text-faint)", flexShrink: 0 }}>
          {new Date(s.created_at).toLocaleString()}
          {s.model && ` · ${s.model.replace("claude-", "").replace(/-\d{8}$/, "")}`}
        </span>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {sshConfig?.host && (
            <>
              <Btn
                color="#1e3a2a"
                label="VS Code"
                title={`Open in VS Code (Remote SSH: ${sshConfig.user}@${sshConfig.host})`}
                onClick={(e) => { e.stopPropagation(); window.open(makeRemoteUri("vscode", sshConfig, s.cwd)); }}
              />
              <Btn
                color="#1a2a3a"
                label="Cursor"
                title={`Open in Cursor (Remote SSH: ${sshConfig.user}@${sshConfig.host})`}
                onClick={(e) => { e.stopPropagation(); window.open(makeRemoteUri("cursor", sshConfig, s.cwd)); }}
              />
            </>
          )}
          {onFiles && (
            <Btn
              color="var(--btn-icon-bg)"
              label="📁"
              title="Browse & edit files"
              onClick={(e) => { e.stopPropagation(); onFiles(); }}
            />
          )}
          {onDownloadCwd && (
            <Btn
              color="var(--btn-icon-bg)"
              label={<img src={downloadIcon} style={{ width: 13, height: 13, display: "block", filter: "invert(1)" }} />}
              title="Download working directory as zip"
              onClick={(e) => { e.stopPropagation(); onDownloadCwd(); }}
            />
          )}
          {onDownloadChat && s.claude_session_id && (
            <Btn
              color="var(--btn-icon-bg)"
              label={<img src={downloadChatIcon} style={{ width: 13, height: 13, display: "block", filter: "invert(1)" }} />}
              title="Export chat as HTML"
              onClick={(e) => { e.stopPropagation(); onDownloadChat(); }}
            />
          )}
          {onShell && (
            <Btn
              color="var(--btn-icon-bg)"
              label={<img src={terminalIcon} style={{ width: 13, height: 13, display: "block", filter: "invert(1)" }} />}
              title="Open bash terminal"
              onClick={(e) => { e.stopPropagation(); onShell(); }}
            />
          )}
          {onJsonl && s.claude_session_id && (
            <Btn
              color="var(--btn-icon-bg)"
              label={<img src={s.tool === "cursor" ? cursorIcon : claudeIcon} style={{ width: 12, height: 12, display: "block", filter: "invert(1)" }} />}
              title="Preview conversation JSONL"
              onClick={(e) => { e.stopPropagation(); onJsonl(); }}
            />
          )}
          {onGit && (
            <Btn
              color="var(--btn-icon-bg)"
              label={<img src={gitIcon} style={{ width: 12, height: 12, display: "block", filter: "invert(1)" }} />}
              title="Git"
              onClick={(e) => { e.stopPropagation(); onGit(); }}
            />
          )}
          {canSchedule && (
            <Btn
              color={showSchedule ? "var(--accent-blue)" : "var(--btn-icon-bg)"}
              label={<img src={scheduleIcon} style={{ width: 12, height: 12, display: "block", filter: "invert(1)" }} />}
              title="Schedule a command"
              onClick={(e) => { e.stopPropagation(); setShowSchedule((v) => !v); }}
            />
          )}
          {s.status === "running" && onTerminate && (
            <Btn color="#d9534f" label="⏹" title="Terminate session" onClick={onTerminate} />
          )}
          {canResume && onResume && (
            <Btn color="#5cb85c" label="▶" title="Resume session" disabled={loading} onClick={onResume} />
          )}
          {canDelete && onDelete && (
            <Btn color="var(--btn-icon-bg)" label="🗑" title="Delete session" onClick={onDelete} />
          )}
        </div>
      </div>

      {/* scheduled tasks */}
      {pendingTasks.length > 0 && (
        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 3 }}>
          {pendingTasks.map((t) => (
            <TaskChip key={t.id} task={t} sessionId={s.id} onCancel={() => onTaskChange?.()} />
          ))}
        </div>
      )}

      {/* schedule form */}
      {showSchedule && (
        <ScheduleForm
          sessionId={s.id}
          onCreated={() => { onTaskChange?.(); }}
          onClose={() => setShowSchedule(false)}
        />
      )}
    </div>
  );
}

export function PromptText({ text }: { text: string }) {
  const trimmed = text.trimStart();
  if (trimmed.startsWith("/")) {
    // Slash-command prompt: split "/cmd <rest>" and render with a "cmd" badge.
    const sp = trimmed.indexOf(" ");
    const cmd = sp === -1 ? trimmed : trimmed.slice(0, sp);
    const rest = sp === -1 ? "" : trimmed.slice(sp + 1);
    return (
      <>
        <span
          style={{
            display: "inline-block",
            fontSize: 9,
            background: "#4c1d95",
            color: "#c4b5fd",
            padding: "0 4px",
            borderRadius: 3,
            fontWeight: 600,
            marginRight: 4,
            verticalAlign: "1px",
            letterSpacing: 0.5,
          }}
        >
          cmd
        </span>
        <span style={{ color: "#c4b5fd", fontFamily: "monospace" }}>{cmd}</span>
        {rest && <span style={{ color: "var(--text-faint)" }}>{" "}{rest}</span>}
      </>
    );
  }
  return <>{text}</>;
}

function Btn({
  color,
  label,
  title,
  disabled,
  onClick,
}: {
  color: string;
  label: React.ReactNode;
  title?: string;
  disabled?: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  return (
    <button
      disabled={disabled}
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick(e);
      }}
      style={{ fontSize: 10, padding: "2px 8px", background: color, color: "#fff" }}
    >
      {label}
    </button>
  );
}

const inputStyle: React.CSSProperties = {
  background: "var(--bg-hover)",
  border: "1px solid var(--text-faintest)",
  borderRadius: 4,
  padding: "6px 8px",
  color: "var(--text-body)",
  fontSize: 12,
  outline: "none",
  width: "100%",
};
