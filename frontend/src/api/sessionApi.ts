export interface ScheduledTask {
  id: string;
  command: string;
  run_at: string;
  status: string;
  created_at: string;
  loop_seconds?: number | null;
}

export interface SessionMeta {
  id: string;
  owner_id: string;
  name: string;
  project: string;
  cwd: string;
  status: string;
  created_at: string;
  attached_clients: number;
  model: string | null;
  resume_session_id: string | null;
  claude_session_id: string | null;
  claude_title: string | null;
  prompts: string[];
  last_user_input_at?: string | null;
  has_new_output: boolean;
  is_streaming: boolean;
  scheduled_tasks: ScheduledTask[];
  git_auto_commit: boolean;
  git_repo_url: string | null;
  tool: "claude" | "cursor";
}

export interface SessionListResponse {
  items: SessionMeta[];
  total: number;
}

export interface AttachResponse {
  session_id: string;
  ws_token: string;
  ws_url: string;
  status: string;
}

export interface LoginResponse {
  token: string;
  username: string;
  role: "admin" | "user";
  is_admin: boolean;
}

export interface UserInfo {
  username: string;
  role: "admin" | "user";
  is_admin: boolean;
}

function getToken(): string {
  return localStorage.getItem("token") || "";
}

async function request<T>(
  path: string,
  init?: RequestInit,
  skipAuth?: boolean
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string>) || {}),
  };
  if (!skipAuth) {
    const token = getToken();
    if (!token) {
      throw new Error("not logged in");
    }
    headers["Authorization"] = `Bearer ${token}`;
  }
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401 && !skipAuth) {
    localStorage.removeItem("token");
    localStorage.removeItem("username");
    localStorage.removeItem("role");
    window.location.reload();
    throw new Error("unauthorized");
  }
  if (!resp.ok) {
    const text = await resp.text();
    let msg = text;
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === "string") msg = j.detail;
      else if (Array.isArray(j?.detail)) msg = j.detail.map((d: { msg?: string }) => d?.msg).filter(Boolean).join("; ") || text;
    } catch { /* not JSON, use raw text */ }
    throw new Error(msg || `HTTP ${resp.status}`);
  }
  if (resp.status === 204) return undefined as unknown as T;
  return resp.json();
}

// AskUserQuestion structured answer
export interface AuqAnswerItem {
  option_idx: number | null;
  option_indices: number[] | null;
  n_options: number;
  custom_text: string | null;
  is_multi: boolean;
}

export function answerAuq(
  sessionId: string,
  answers: AuqAnswerItem[],
  submitConfirmIdx?: number,
): Promise<{ ok: boolean }> {
  return request(`/api/sessions/${sessionId}/answer-auq`, {
    method: "POST",
    body: JSON.stringify({
      answers,
      submit_confirm_idx: submitConfirmIdx ?? null,
    }),
  });
}

export function submitAuqAnswers(
  sessionId: string,
  answers: unknown[],
  questions: object[],
  singleShot = false,
): Promise<{ ok: boolean; via?: string }> {
  return request(`/api/sessions/${sessionId}/auq/submit`, {
    method: "POST",
    body: JSON.stringify({ answers, questions, single_shot: singleShot }),
  });
}

export function rewindSession(
  sessionId: string,
  messageUuid: string,
): Promise<{ ok: boolean; restored_files: string[]; kept_lines: number }> {
  return request(`/api/sessions/${sessionId}/rewind`, {
    method: "POST",
    body: JSON.stringify({ message_uuid: messageUuid }),
  });
}

// Auth
export function login(
  username: string,
  password: string
): Promise<LoginResponse> {
  return request(
    "/api/auth/login",
    { method: "POST", body: JSON.stringify({ username, password }) },
    true
  );
}

export function getGoogleClientId(): Promise<{ client_id: string }> {
  return request("/api/auth/google-client-id", {}, true);
}

export function loginWithGoogle(credential: string): Promise<LoginResponse> {
  return request(
    "/api/auth/google",
    { method: "POST", body: JSON.stringify({ credential }) },
    true
  );
}

export function listUsers(): Promise<UserInfo[]> {
  return request("/api/auth/users");
}

export function createUser(
  username: string,
  password: string,
  role: "admin" | "user"
): Promise<UserInfo> {
  return request("/api/auth/users", {
    method: "POST",
    body: JSON.stringify({ username, password, role }),
  });
}

export function changePassword(
  username: string,
  password: string
): Promise<void> {
  return request(`/api/auth/users/${username}/password`, {
    method: "PUT",
    body: JSON.stringify({ password }),
  });
}

export function deleteUser(username: string): Promise<void> {
  return request(`/api/auth/users/${username}`, { method: "DELETE" });
}

export function setUserIsAdmin(username: string, is_admin: boolean): Promise<UserInfo> {
  return request(`/api/auth/users/${username}/is_admin`, {
    method: "PUT",
    body: JSON.stringify({ is_admin }),
  });
}

// Config
export interface ConfigView {
  workspace: string;
  claude_bin: string;
  cursor_bin: string;
  proxy: string;
  terminal_font: string;
  term_idle_grace_seconds: number;
  term_standby_grace_seconds: number;
}

export interface FontInfo {
  family: string;
  recommended: boolean;
}

export function getSystemFonts(): Promise<FontInfo[]> {
  return request("/api/config/fonts");
}

export function setTerminalFont(font: string): Promise<ConfigView> {
  return request("/api/config/terminal-font", {
    method: "PUT",
    body: JSON.stringify({ font }),
  });
}

export function getConfig(): Promise<ConfigView> {
  return request("/api/config");
}

export function setWorkspace(workspace: string): Promise<ConfigView> {
  return request("/api/config/workspace", {
    method: "PUT",
    body: JSON.stringify({ workspace }),
  });
}

export function setClaudeBin(claude_bin: string): Promise<ConfigView> {
  return request("/api/config/claude-bin", {
    method: "PUT",
    body: JSON.stringify({ claude_bin }),
  });
}

export function setCursorBin(cursor_bin: string): Promise<ConfigView> {
  return request("/api/config/cursor-bin", {
    method: "PUT",
    body: JSON.stringify({ cursor_bin }),
  });
}

export function setProxy(proxy: string): Promise<ConfigView> {
  return request("/api/config/proxy", {
    method: "PUT",
    body: JSON.stringify({ proxy }),
  });
}

export function setTermLifecycle(
  idle_grace_seconds: number,
  standby_grace_seconds: number,
): Promise<ConfigView> {
  return request("/api/config/term-lifecycle", {
    method: "PUT",
    body: JSON.stringify({ idle_grace_seconds, standby_grace_seconds }),
  });
}

export function restartServer(): Promise<void> {
  return request("/api/config/restart", { method: "POST" });
}

export function getAvailableTools(): Promise<{ claude: boolean; cursor: boolean }> {
  return request("/api/config/available-tools");
}

// Filesystem
export function listDirs(path: string): Promise<string[]> {
  return request(`/api/fs/dirs?path=${encodeURIComponent(path)}`);
}

// Sessions
export function listSessions(q?: string): Promise<SessionListResponse> {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return request(`/api/sessions${qs}`);
}

export function getSession(sessionId: string): Promise<SessionMeta> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}`);
}

export function listAllSessions(q?: string): Promise<SessionListResponse> {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return request(`/api/sessions/all${qs}`);
}

export interface TuiAuqData {
  // Screen-parsed format
  question?: string;
  header?: string;
  multiSelect?: boolean;
  allowFreeform?: boolean;
  options?: { label: string; description?: string }[];
  // Hook format (raw tool_input)
  questions?: Array<{
    question: string;
    options?: Array<string | { label?: string; value?: string }>;
    multiSelect?: boolean;
  }>;
}

export interface TuiApproveData {
  tool_name: string;
  tool_input: Record<string, unknown>;
}

export interface SessionStatusItem {
  id: string;
  status: string;
  attached_clients: number;
  has_new_output: boolean;
  is_streaming: boolean;
  is_compacting?: boolean;
  compacting_progress?: string | null;
  scheduled_tasks: ScheduledTask[];
  tui_hint?: string | null;
  tui_auq_data?: TuiAuqData | null;
  tui_approve_data?: TuiApproveData | null;
}

export function approveToolRequest(sessionId: string, decision: "allow" | "deny"): Promise<{ ok: boolean }> {
  return request(`/api/sessions/${sessionId}/tool-approve`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
}

export interface SessionStatusListResponse {
  items: SessionStatusItem[];
  total: number;
}

export function listSessionsStatus(): Promise<SessionStatusListResponse> {
  return request("/api/sessions/status");
}


export function createSession(body: {
  project: string;
  cwd?: string;
  model?: string;
  resume_session_id?: string;
  git_repo_url?: string;
  tool?: "claude" | "cursor";
}): Promise<SessionMeta> {
  return request("/api/sessions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function attachSession(sessionId: string): Promise<AttachResponse> {
  return request(`/api/sessions/${sessionId}/attach`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function detachSession(sessionId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}/detach`, { method: "POST" });
}

export function terminateSession(sessionId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}/terminate`, { method: "POST" });
}

export function resumeSession(sessionId: string): Promise<SessionMeta> {
  return request(`/api/sessions/${sessionId}/resume`, { method: "POST" });
}

export interface AvailableClaudeSession {
  claude_session_id: string;
  mtime: number;
  title: string | null;
}

export function listAvailableClaudeSessions(sessionId: string): Promise<AvailableClaudeSession[]> {
  return request(`/api/sessions/${sessionId}/available-claude-sessions`);
}

export function setClaudeSessionId(sessionId: string, claudeSessionId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}/claude-session-id`, {
    method: "PUT",
    body: JSON.stringify({ claude_session_id: claudeSessionId }),
  });
}

export interface SearchResult {
  line: number;
  text: string;
  context: string;
}

export function searchSession(
  sessionId: string,
  q: string
): Promise<SearchResult[]> {
  return request(`/api/sessions/${sessionId}/search?q=${encodeURIComponent(q)}`);
}

export function deleteSession(sessionId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}`, { method: "DELETE" });
}

export function renameSession(sessionId: string, name: string): Promise<SessionMeta> {
  return request(`/api/sessions/${sessionId}/name`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

// File browser
export interface FileEntry {
  name: string;
  path: string;
  type: "file" | "dir";
  size: number | null;
  is_text: boolean;
  is_skipped: boolean;
  is_sqlite: boolean;
  is_archive: boolean;
}

export function searchFiles(
  sessionId: string,
  q: string,
  hidden?: boolean,
): Promise<{ entries: FileEntry[]; path: string }> {
  const params = new URLSearchParams({ q });
  if (hidden) params.set("hidden", "true");
  return request(`/api/sessions/${sessionId}/fs/search?${params.toString()}`);
}

export function listFiles(
  sessionId: string,
  path?: string,
  hidden?: boolean,
): Promise<{ entries: FileEntry[]; path: string }> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  if (hidden) params.set("hidden", "true");
  const qs = params.toString() ? `?${params.toString()}` : "";
  return request(`/api/sessions/${sessionId}/fs/list${qs}`);
}

export interface SqliteInfo {
  tables: string[];
  columns: string[];
  rows: unknown[][];
  total: number;
  path: string;
}

export function sqliteQuery(
  sessionId: string,
  path: string,
  table?: string,
  limit = 100,
  offset = 0,
): Promise<SqliteInfo> {
  const params = new URLSearchParams({ path });
  if (table) params.set("table", table);
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  return request(`/api/sessions/${sessionId}/fs/sqlite?${params.toString()}`);
}

export interface SqliteExecResult {
  columns: string[];
  rows: unknown[][];
  affected: number;
  message: string;
}

export function sqliteExec(
  sessionId: string,
  path: string,
  sql: string,
): Promise<SqliteExecResult> {
  return request(`/api/sessions/${sessionId}/fs/sqlite/exec`, {
    method: "POST",
    body: JSON.stringify({ path, sql }),
  });
}

export function readFile(
  sessionId: string,
  path: string
): Promise<{ path: string; content: string }> {
  return request(
    `/api/sessions/${sessionId}/fs/read?path=${encodeURIComponent(path)}`
  );
}

export async function fetchRawFileBlob(
  sessionId: string,
  path: string
): Promise<string> {
  const token = localStorage.getItem("token") || "";
  const resp = await fetch(
    `/api/sessions/${sessionId}/fs/raw?path=${encodeURIComponent(path)}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

export async function downloadFile(sessionId: string, path: string): Promise<void> {
  const token = localStorage.getItem("token") || "";
  const resp = await fetch(
    `/api/sessions/${sessionId}/fs/raw?path=${encodeURIComponent(path)}&download=true`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  if (resp.status === 401) { localStorage.removeItem("token"); window.location.reload(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(await resp.text().catch(() => `HTTP ${resp.status}`));
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = path.split("/").pop() || "download";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function uploadFile(
  sessionId: string,
  dirPath: string,
  file: File
): Promise<void> {
  const token = localStorage.getItem("token") || "";
  const form = new FormData();
  form.append("path", dirPath);
  form.append("file", file);
  const resp = await fetch(`/api/sessions/${sessionId}/fs/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (resp.status === 401) { localStorage.removeItem("token"); window.location.reload(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `HTTP ${resp.status}`);
  }
}

export interface DirInfoItem {
  name: string;
  path: string;
  type: "file" | "dir";
  size: number;
}

export interface DirInfoResponse {
  total_size: number;
  items: DirInfoItem[];
}

export function getDirInfo(sessionId: string, path: string): Promise<DirInfoResponse> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  const qs = params.toString() ? `?${params.toString()}` : "";
  return request(`/api/sessions/${sessionId}/fs/dir-info${qs}`);
}

async function _triggerZipDownload(resp: Response, fallbackName: string): Promise<void> {
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const cd = resp.headers.get("Content-Disposition") || "";
  const match = cd.match(/filename="([^"]+)"/);
  a.download = match ? match[1] : fallbackName + ".zip";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function downloadDirZip(sessionId: string, path: string, exclude: string[] = [], compress = true): Promise<void> {
  const token = localStorage.getItem("token") || "";
  const resp = await fetch(`/api/sessions/${sessionId}/fs/download-zip`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ path, exclude, compress }),
  });
  if (resp.status === 401) { localStorage.removeItem("token"); window.location.reload(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { const j = await resp.json(); msg = j.detail || msg; } catch { msg = await resp.text().catch(() => msg); }
    throw new Error(msg);
  }
  await _triggerZipDownload(resp, path.split("/").pop() || "workspace");
}

export function createDir(sessionId: string, path: string): Promise<void> {
  return request(`/api/sessions/${sessionId}/fs/mkdir`, {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export function renameEntry(
  sessionId: string,
  path: string,
  newName: string,
): Promise<void> {
  return request(`/api/sessions/${sessionId}/fs/rename`, {
    method: "POST",
    body: JSON.stringify({ path, new_name: newName }),
  });
}

export function moveEntry(
  sessionId: string,
  path: string,
  destDir: string,
): Promise<void> {
  return request(`/api/sessions/${sessionId}/fs/move`, {
    method: "POST",
    body: JSON.stringify({ path, dest_dir: destDir }),
  });
}

export function deleteEntry(
  sessionId: string,
  path: string,
  recursive = false,
): Promise<void> {
  return request(`/api/sessions/${sessionId}/fs/delete`, {
    method: "POST",
    body: JSON.stringify({ path, recursive }),
  });
}

export interface ArchiveListResult {
  entries: string[];
  total: number;
}

export function listArchive(
  sessionId: string,
  path: string,
): Promise<ArchiveListResult> {
  return request(`/api/sessions/${sessionId}/fs/archive-list?path=${encodeURIComponent(path)}`);
}

export interface ExtractResult {
  output_dir: string;
}

export function extractArchive(
  sessionId: string,
  path: string,
): Promise<ExtractResult> {
  return request(`/api/sessions/${sessionId}/fs/extract`, {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export async function extractToDir(targetDir: string, file: File): Promise<{ path: string }> {
  const token = localStorage.getItem("token") || "";
  const form = new FormData();
  form.append("target_dir", targetDir);
  form.append("file", file);
  const resp = await fetch("/api/fs/extract-to", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (resp.status === 401) { localStorage.removeItem("token"); window.location.reload(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export function writeFile(
  sessionId: string,
  path: string,
  content: string
): Promise<void> {
  return request(`/api/sessions/${sessionId}/fs/write`, {
    method: "PUT",
    body: JSON.stringify({ path, content }),
  });
}

// Scheduled Tasks
export function createTask(
  sessionId: string,
  command: string,
  delay_seconds: number,
  loop_seconds?: number | null,
): Promise<ScheduledTask> {
  return request(`/api/sessions/${sessionId}/tasks`, {
    method: "POST",
    body: JSON.stringify({ command, delay_seconds, loop_seconds: loop_seconds ?? null }),
  });
}

export function cancelTask(sessionId: string, taskId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}/tasks/${taskId}`, { method: "DELETE" });
}

export function listTasks(sessionId: string): Promise<ScheduledTask[]> {
  return request(`/api/sessions/${sessionId}/tasks`);
}

// /goal history
export interface Goal {
  condition: string;
  set_at: number;
  met: boolean;
  met_at: number | null;
  last_reason: string | null;
  checks: number;
  replaced: boolean;
}
export interface GoalsResponse {
  active: Goal | null;
  history: Goal[];
}
export function listGoals(sessionId: string): Promise<GoalsResponse> {
  return request(`/api/sessions/${sessionId}/goals`);
}

// AUQ history
export interface AuqQuestion {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options?: Array<{ label: string; description?: string }>;
}
export interface AuqEntry {
  tool_use_id: string;
  ts: number;
  answered_ts: number | null;
  questions: AuqQuestion[];
  answers: Record<string, string> | null;
}
export function listSessionAuqs(sessionId: string): Promise<AuqEntry[]> {
  return request(`/api/sessions/${sessionId}/auqs`);
}

export interface TodoItem {
  id?: string;
  content: string;
  description?: string;
  activeForm?: string;
  status: "pending" | "in_progress" | "completed";
  priority?: "high" | "medium" | "low";
}

export interface TodoPlan {
  todos: TodoItem[];
  created_ts: number;
  completed_ts: number;
}

export interface TodoPlansResponse {
  active: TodoItem[];
  history: TodoPlan[];
}

export function listSessionTodos(sessionId: string): Promise<TodoPlansResponse> {
  return request(`/api/sessions/${sessionId}/todos`);
}

export function openShell(sessionId: string): Promise<AttachResponse> {
  return request(`/api/sessions/${sessionId}/shell`, { method: "POST", body: JSON.stringify({}) });
}

// ── Bash terminals (tmux-backed) ────────────────────────────────────────────

export interface TerminalInfo {
  term_id: string;
  session_id: string;
  name: string | null;
  cwd: string;
  is_named: boolean;
  attach_count: number;
  created_at: number;
  kept?: boolean;
}

export interface TerminalHeartbeatResponse {
  term_id: string;
  is_named: boolean;
  kept: boolean;
  attach_count: number;
}

export interface CreateTerminalResponse {
  term_id: string;
  name: string | null;
  is_named: boolean;
  ws_token: string;
  ws_url: string;
}

export interface IssueTerminalTokenResponse {
  term_id: string;
  ws_token: string;
  ws_url: string;
  name?: string | null;
  is_named?: boolean;
  kept?: boolean;
}

export function listTerminals(sessionId: string): Promise<{ items: TerminalInfo[] }> {
  return request(`/api/sessions/${sessionId}/terminals`);
}

export function createTerminal(
  sessionId: string,
  opts: { name?: string | null; cwd?: string } = {},
): Promise<CreateTerminalResponse> {
  return request(`/api/sessions/${sessionId}/terminals`, {
    method: "POST",
    body: JSON.stringify({ name: opts.name ?? null, cwd: opts.cwd ?? null }),
  });
}

export function issueTerminalToken(
  sessionId: string,
  termId: string,
): Promise<IssueTerminalTokenResponse> {
  return request(`/api/sessions/${sessionId}/terminals/${termId}/token`, { method: "POST" });
}

export function renameTerminal(
  sessionId: string,
  termId: string,
  name: string | null,
): Promise<TerminalInfo> {
  return request(`/api/sessions/${sessionId}/terminals/${termId}/rename`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function deleteTerminal(sessionId: string, termId: string): Promise<{ ok: boolean }> {
  return request(`/api/sessions/${sessionId}/terminals/${termId}`, { method: "DELETE" });
}

/** Refresh "still alive" timestamp for a cached ephemeral terminal.
 *  Throws if the terminal has been swept (HTTP 410) — caller should treat
 *  that as "cached term_id is stale; spawn a fresh one." */
export function heartbeatTerminal(
  sessionId: string,
  termId: string,
): Promise<TerminalHeartbeatResponse> {
  return request(`/api/sessions/${sessionId}/terminals/${termId}/heartbeat`, { method: "POST" });
}

// Git
export interface GitLogEntry {
  hash: string;
  short_hash: string;
  subject: string;
  author: string;
  date: string;
  context?: string; // only present in deep-search results
}

export interface GitInfo {
  is_repo: boolean;
  auto_commit: boolean;
  log: GitLogEntry[];  // full history, pagination done client-side
  gitignore: string;
  remote: string;
}

export interface GitDiffFile {
  path: string;
  old_content: string;
  new_content: string;
}

export interface GitDiffResult {
  files: GitDiffFile[];
  old_hash: string;
  new_hash: string;
}

export function getGitInfo(sessionId: string): Promise<GitInfo> {
  return request(`/api/sessions/${sessionId}/git`);
}

export function searchGitCommits(sessionId: string, q: string): Promise<GitLogEntry[]> {
  return request(`/api/sessions/${sessionId}/git/search?q=${encodeURIComponent(q)}`);
}

export function gitInit(sessionId: string): Promise<{ output: string }> {
  return request(`/api/sessions/${sessionId}/git/init`, { method: "POST" });
}

export function setGitAutoCommit(sessionId: string, enabled: boolean): Promise<{ auto_commit: boolean }> {
  return request(`/api/sessions/${sessionId}/git/auto-commit`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

export function gitManualCommit(sessionId: string, message?: string): Promise<{ committed: boolean; output: string }> {
  return request(`/api/sessions/${sessionId}/git/commit`, {
    method: "POST",
    body: JSON.stringify({ message: message || null }),
  });
}

export function gitRollback(sessionId: string, commit_hash: string): Promise<{ output: string }> {
  return request(`/api/sessions/${sessionId}/git/rollback`, {
    method: "POST",
    body: JSON.stringify({ commit_hash }),
  });
}

export function saveGitignore(sessionId: string, content: string): Promise<{ ok: boolean }> {
  return request(`/api/sessions/${sessionId}/git/gitignore`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

export function gitSetRemote(sessionId: string, url: string): Promise<{ ok: boolean; remote: string }> {
  return request(`/api/sessions/${sessionId}/git/remote`, {
    method: "PUT",
    body: JSON.stringify({ url }),
  });
}

export function gitPush(sessionId: string): Promise<{ ok: boolean; output: string }> {
  return request(`/api/sessions/${sessionId}/git/push`, { method: "POST" });
}

export interface CommitDetail {
  message: string;
  files: GitDiffFile[];
}

export interface GitBranchInfo {
  current: string;
  local: string[];
  dirty?: boolean;
}

export interface GitGraphCommit {
  hash: string;
  short_hash: string;
  parents: string[];
  subject: string;
  author: string;
  date: string;
  refs: string[];
}

export interface ActiveCwdSession {
  id: string;
  name: string;
  status: string;
  tool: string;
  last_activity_at: string | null;
}

export function getGitBranches(sessionId: string): Promise<GitBranchInfo> {
  return request(`/api/sessions/${sessionId}/git/branches`);
}

export function getGitGraph(sessionId: string, scope = "current", n = 500): Promise<GitGraphCommit[]> {
  return request(`/api/sessions/${sessionId}/git/graph?scope=${encodeURIComponent(scope)}&n=${n}`);
}

export function getActiveCwdSessions(sessionId: string): Promise<{ sessions: ActiveCwdSession[] }> {
  return request(`/api/sessions/${sessionId}/git/active-cwd-sessions`);
}

export interface GitCheckoutConflict {
  code: "conflict";
  message: string;
  conflicting_files: string[];
}

export class GitCheckoutConflictError extends Error {
  conflict: GitCheckoutConflict;
  constructor(c: GitCheckoutConflict) {
    super(c.message);
    this.conflict = c;
  }
}

export async function gitCheckoutBranch(
  sessionId: string,
  branch: string,
  force_discard = false,
): Promise<{ ok: boolean; branch: string; output: string }> {
  const token = getToken();
  const resp = await fetch(`/api/sessions/${sessionId}/git/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ branch, force_discard }),
  });
  if (resp.status === 409) {
    const body = await resp.json().catch(() => ({}));
    const detail = body?.detail;
    if (detail && typeof detail === "object" && detail.code === "conflict") {
      throw new GitCheckoutConflictError(detail as GitCheckoutConflict);
    }
    throw new Error(typeof detail === "string" ? detail : "checkout conflict");
  }
  if (!resp.ok) {
    const text = await resp.text();
    let msg = text;
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === "string") msg = j.detail;
    } catch { /* */ }
    throw new Error(msg || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export function getCommitDetail(sessionId: string, commitHash: string): Promise<CommitDetail> {
  return request(`/api/sessions/${sessionId}/git/show/${commitHash}`);
}

export interface ConversationTurn {
  role: "user" | "assistant";
  text: string;
  streaming: boolean;
  ts: number;      // seconds since epoch from turn_duration; 0 for in-progress turns
  pending?: boolean; // true = unconfirmed (no turn_duration yet); replace not append
  compacting?: boolean; // true while the model is generating a compact summary
}

export interface JsonlPageResponse {
  lines: string[];
  total: number;
  page: number;
  page_size: number;
}

export async function getConversationJsonl(
  sessionId: string,
  page = 0,
  page_size = 200,
): Promise<JsonlPageResponse> {
  const token = localStorage.getItem("token") || "";
  const params = new URLSearchParams({ page: String(page), page_size: String(page_size) });
  const resp = await fetch(`/api/sessions/${sessionId}/conversation/jsonl?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (resp.status === 401) { localStorage.removeItem("token"); window.location.reload(); throw new Error("unauthorized"); }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export function getConversation(sessionId: string, fromTs = 0, tail?: number): Promise<ConversationTurn[]> {
  const params = new URLSearchParams();
  if (fromTs > 0) params.set("from_ts", String(fromTs));
  if (tail !== undefined) params.set("tail", String(tail));
  const qs = params.toString() ? `?${params.toString()}` : "";
  return request(`/api/sessions/${sessionId}/conversation${qs}`);
}

export function gitDiff(sessionId: string, old_hash: string, new_hash: string): Promise<GitDiffResult> {
  return request(`/api/sessions/${sessionId}/git/diff`, {
    method: "POST",
    body: JSON.stringify({ old_hash, new_hash }),
  });
}

export async function getFileGitLog(sessionId: string, path: string, n = 50): Promise<Array<{hash: string; short_hash: string; subject: string; author: string; date: string}>> {
  return request(`/api/sessions/${sessionId}/git/file-log?path=${encodeURIComponent(path)}&n=${n}`);
}

export async function getFileGitShow(sessionId: string, path: string, commit: string): Promise<{content: string; commit: string; path: string}> {
  return request(`/api/sessions/${sessionId}/git/file-show?path=${encodeURIComponent(path)}&commit=${encodeURIComponent(commit)}`);
}

export async function getFileGitDiff(sessionId: string, path: string, commit: string): Promise<{diff: string; commit: string; path: string}> {
  return request(`/api/sessions/${sessionId}/git/file-diff?path=${encodeURIComponent(path)}&commit=${encodeURIComponent(commit)}`);
}

// ── Code viewer ───────────────────────────────────────────────────────────

export interface ChangedFile {
  path: string;
  status: "modified" | "added" | "deleted" | "renamed" | "untracked" | "conflict";
  added?: number;
  removed?: number;
}

export interface FileData {
  path: string;
  content: string;
  language: string;
  added_lines: number[];
  removed_lines: number[];
  truncated: boolean;
  diff_raw?: string;
  is_binary?: boolean;
  size?: number;
}

export interface TreeNode {
  name: string;
  path: string;
  type: "file" | "dir";
  /** undefined = not a dir or empty dir; null = has content but not loaded yet */
  children?: TreeNode[] | null;
}

export function getCodeChangedFiles(sessionId: string): Promise<ChangedFile[]> {
  return request(`/api/sessions/${sessionId}/code/changed-files`);
}

export function getCodeFile(sessionId: string, path: string): Promise<FileData> {
  return request(`/api/sessions/${sessionId}/code/file?path=${encodeURIComponent(path)}`);
}

export function getCodeTree(sessionId: string, depth = 2, path = ""): Promise<TreeNode> {
  const params = new URLSearchParams({ depth: String(depth) });
  if (path) params.set("path", path);
  return request(`/api/sessions/${sessionId}/code/tree?${params}`);
}

export interface UsageWindow {
  utilization: number;  // 0..1
  resets_at: string;    // ISO timestamp
}

export interface UsageInfo {
  five_hour?: UsageWindow;
  seven_day?: UsageWindow;
  seven_day_sonnet?: UsageWindow;
}

export function getUsageInfo(): Promise<UsageInfo> {
  return request<UsageInfo>("/api/usage");
}

export function getPaneHistory(sessionId: string, lines = 20000): Promise<{ content: string }> {
  return request<{ content: string }>(`/api/sessions/${sessionId}/pane-history?lines=${lines}`);
}

export interface RawUsage {
  input_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  output_tokens?: number;
  [key: string]: unknown;
}

export interface RawMessage {
  type: string;
  uuid?: string;
  parentUuid?: string;
  timestamp?: string;
  message?: {
    role: string;
    content: RawContentBlock[] | string;
    stop_reason?: string | null;
    model?: string;
    usage?: RawUsage;
  };
  [key: string]: unknown;
}

export interface RawContentBlock {
  type: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: string | RawContentBlock[];
  is_error?: boolean;
  [key: string]: unknown;
}

export function getRawMessages(sessionId: string, tail = 500): Promise<{ messages: RawMessage[]; total: number }> {
  return request<{ messages: RawMessage[]; total: number }>(`/api/sessions/${sessionId}/raw-messages?tail=${tail}`);
}

export function getAllRawMessages(sessionId: string): Promise<{ messages: RawMessage[]; total: number }> {
  return request<{ messages: RawMessage[]; total: number }>(`/api/sessions/${sessionId}/raw-messages/all`);
}

export interface SubAgentMeta {
  agentId: string;
  description: string;
  agentType: string;
  mtime: number;
}

export function getSubAgents(sessionId: string): Promise<SubAgentMeta[]> {
  return request<SubAgentMeta[]>(`/api/sessions/${sessionId}/subagents`);
}

export function getSubAgentLines(sessionId: string, agentId: string, fromLine = 0): Promise<{ lines: string[]; total: number }> {
  return request<{ lines: string[]; total: number }>(`/api/sessions/${sessionId}/subagents/${agentId}?from_line=${fromLine}`);
}

export interface ExternalSession {
  claude_session_id: string;
  mtime: number;
  title: string | null;
  prompts: string[];
  cwd: string;
}

export interface ExternalSessionGroup {
  dir: string;
  dir_exists: boolean;
  sessions: ExternalSession[];
  latest_mtime: number;
}

export function browseExternalSessions(): Promise<ExternalSessionGroup[]> {
  return request<ExternalSessionGroup[]>("/api/sessions/external");
}

export function browseCursorSessions(): Promise<ExternalSessionGroup[]> {
  return request<ExternalSessionGroup[]>("/api/sessions/external-cursor");
}

export interface ExternalPreview {
  turns: Array<{ role: string; text: string; ts: number }>;
  total: number;
  truncated_before: number;
}

export function getExternalPreview(claude_session_id: string, cwd: string, tool = "claude"): Promise<ExternalPreview> {
  return request<ExternalPreview>(
    `/api/sessions/external-preview?claude_session_id=${encodeURIComponent(claude_session_id)}&cwd=${encodeURIComponent(cwd)}&tool=${encodeURIComponent(tool)}`
  );
}

export interface ModelInfo {
  id: string;
  name: string;
}

export function listModels(tool: string = "claude"): Promise<ModelInfo[]> {
  return request<ModelInfo[]>(`/api/models?tool=${encodeURIComponent(tool)}`);
}

export function setSessionModel(sessionId: string, model: string | null): Promise<SessionMeta> {
  return request<SessionMeta>(`/api/sessions/${encodeURIComponent(sessionId)}/model`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
}

// ── Claude Capability Management ─────────────────────────────────────────────

export interface CapItem {
  relpath: string;
  name: string;
  description: string;
  exists: boolean;
  size: number;
}

export interface CapSection {
  id: string;
  title: string;
  items: CapItem[];
  new_template: string | null;
  new_dir: string | null;
}

export interface CapListResponse {
  scope_root: string;
  sections: CapSection[];
}

export function listClaudeCaps(scope: "global" | "project", cwd?: string): Promise<CapListResponse> {
  const params = new URLSearchParams({ scope });
  if (cwd) params.set("cwd", cwd);
  return request<CapListResponse>(`/api/claude-caps/list?${params}`);
}

export function readClaudeCapFile(scope: "global" | "project", relpath: string, cwd?: string): Promise<{ content: string; exists: boolean }> {
  const params = new URLSearchParams({ scope, relpath });
  if (cwd) params.set("cwd", cwd);
  return request<{ content: string; exists: boolean }>(`/api/claude-caps/file?${params}`);
}

export function writeClaudeCapFile(scope: "global" | "project", relpath: string, content: string, cwd?: string): Promise<void> {
  return request<void>(`/api/claude-caps/file`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, relpath, content, cwd }),
  });
}

export function deleteClaudeCapFile(scope: "global" | "project", relpath: string, cwd?: string): Promise<void> {
  const params = new URLSearchParams({ scope, relpath });
  if (cwd) params.set("cwd", cwd);
  return request<void>(`/api/claude-caps/file?${params}`, { method: "DELETE" });
}

export interface CapVersion {
  version_id: string;
  saved_at: string;
  size: number;
  preview: string;
}

export interface CapVersionsResponse {
  versions: CapVersion[];
}

export function listCapVersions(scope: "global" | "project", relpath: string, cwd?: string): Promise<CapVersionsResponse> {
  const params = new URLSearchParams({ scope, relpath });
  if (cwd) params.set("cwd", cwd);
  return request<CapVersionsResponse>(`/api/claude-caps/versions?${params}`);
}

export function rollbackCapVersion(scope: "global" | "project", relpath: string, version_id: string, cwd?: string): Promise<void> {
  return request<void>(`/api/claude-caps/rollback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, relpath, version_id, cwd }),
  });
}

export function readCapVersionContent(scope: "global" | "project", relpath: string, version_id: string, cwd?: string): Promise<{ content: string }> {
  const params = new URLSearchParams({ scope, relpath, version_id });
  if (cwd) params.set("cwd", cwd);
  return request<{ content: string }>(`/api/claude-caps/version-content?${params}`);
}

