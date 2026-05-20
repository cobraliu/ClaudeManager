import { useEffect, useState, useCallback, useRef } from "react";
import {
  listUsers,
  createUser,
  changePassword,
  deleteUser,
  setUserIsAdmin,
  listAllSessions,
  getConfig,
  setWorkspace,
  setClaudeBin,
  setProxy,
  restartServer,
  getSshConfig,
  updateSshConfig,
  getSystemFonts,
  setTerminalFont,
  type UserInfo,
  type SessionMeta,
  type SshConfig,
  type FontInfo,
} from "../api/sessionApi";
import { SessionCard } from "../components/SessionCard";

const PAGE_SIZE = 30;

interface Props {
  onLogout: () => void;
  onBack?: () => void;
  theme?: "dark" | "light";
  onToggleTheme?: () => void;
}

export function AdminPage({ onLogout, onBack, theme, onToggleTheme }: Props) {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<"admin" | "user">("user");
  const [pwUser, setPwUser] = useState("");
  const [pwValue, setPwValue] = useState("");
  const [tab, setTab] = useState<"sessions" | "users" | "config">("config");
  const [msg, setMsg] = useState("");
  const [workspace, setWorkspaceVal] = useState("");
  const [workspaceInput, setWorkspaceInput] = useState("");
  const [claudeBin, setClaudeBinVal] = useState("");
  const [claudeBinInput, setClaudeBinInput] = useState("");
  const [proxy, setProxyVal] = useState("");
  const [proxyInput, setProxyInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [restarting, setRestarting] = useState(false);
  const [sshConfig, setSshConfig] = useState<SshConfig>({ host: "", port: 22, user: "" });
  const [sshInput, setSshInput] = useState<SshConfig>({ host: "", port: 22, user: "" });
  const [terminalFont, setTerminalFontVal] = useState("");
  const [fontList, setFontList] = useState<FontInfo[]>([]);
  const [fontSearch, setFontSearch] = useState("");
  const [fontPreview, setFontPreview] = useState<string | null>(null);
  const [fontLoading, setFontLoading] = useState(false);

  const handleRestart = async () => {
    if (!window.confirm("Restart server? All current connections will be disconnected.")) return;
    setRestarting(true);
    try { await restartServer(); } catch { /* server may disconnect before responding */ }
    const poll = setInterval(async () => {
      try { const r = await fetch("/health"); if (r.ok) { clearInterval(poll); setRestarting(false); } } catch {}
    }, 1500);
    setTimeout(() => { clearInterval(poll); setRestarting(false); }, 30000);
  };

  const refreshUsers = useCallback(async () => {
    try {
      setUsers(await listUsers());
    } catch {}
  }, []);

  const refreshConfig = useCallback(async () => {
    try {
      const c = await getConfig();
      setWorkspaceVal(c.workspace);
      setWorkspaceInput(c.workspace);
      setClaudeBinVal(c.claude_bin);
      setClaudeBinInput(c.claude_bin);
      setProxyVal(c.proxy);
      setProxyInput(c.proxy);
      setTerminalFontVal(c.terminal_font || "");
    } catch {}
    try {
      const ssh = await getSshConfig();
      setSshConfig(ssh);
      setSshInput(ssh);
    } catch {}
  }, []);

  const searchRef2 = useRef(search);
  searchRef2.current = search;

  const refreshSessions = useCallback(async (q?: string) => {
    try {
      const res = await listAllSessions(q || undefined);
      setSessions(res.items);
    } catch {}
  }, []);

  useEffect(() => {
    if (tab !== "config" || fontList.length > 0 || fontLoading) return;
    setFontLoading(true);
    getSystemFonts().then((f) => { setFontList(f); setFontLoading(false); }).catch(() => setFontLoading(false));
  }, [tab, fontList.length, fontLoading]);

  useEffect(() => {
    refreshUsers();
    refreshConfig();
    let id: ReturnType<typeof setInterval>;
    const start = () => {
      refreshSessions(searchRef2.current);
      id = setInterval(() => refreshSessions(searchRef2.current), 5000);
    };
    const stop = () => clearInterval(id);
    const onVis = () => (document.hidden ? stop() : start());
    start();
    document.addEventListener("visibilitychange", onVis);
    return () => { stop(); document.removeEventListener("visibilitychange", onVis); };
  }, [refreshUsers, refreshSessions]);

  // Debounced search
  useEffect(() => {
    setPage(0);
    const timer = setTimeout(() => refreshSessions(search), 300);
    return () => clearTimeout(timer);
  }, [search, refreshSessions]);

  const totalPages = Math.max(1, Math.ceil(sessions.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageItems = sessions.slice(
    safePage * PAGE_SIZE,
    (safePage + 1) * PAGE_SIZE
  );

  const handleCreateUser = async () => {
    if (!newUsername || !newPassword) return;
    try {
      await createUser(newUsername, newPassword, newRole);
      setNewUsername("");
      setNewPassword("");
      setMsg(`User "${newUsername}" created`);
      await refreshUsers();
    } catch (e) {
      setMsg(String(e));
    }
  };

  const handleChangePassword = async () => {
    if (!pwUser || !pwValue) return;
    try {
      await changePassword(pwUser, pwValue);
      setPwUser("");
      setPwValue("");
      setMsg(`Password updated for "${pwUser}"`);
    } catch (e) {
      setMsg(String(e));
    }
  };

  const handleDeleteUser = async (username: string) => {
    if (!confirm(`Delete user "${username}"?`)) return;
    try {
      await deleteUser(username);
      setMsg(`User "${username}" deleted`);
      await refreshUsers();
    } catch (e) {
      setMsg(String(e));
    }
  };

  const handleToggleIsAdmin = async (u: UserInfo) => {
    try {
      const updated = await setUserIsAdmin(u.username, !u.is_admin);
      setUsers((prev) => prev.map((x) => x.username === u.username ? updated : x));
      setMsg(`"${u.username}" is_admin → ${updated.is_admin}`);
    } catch (e) {
      setMsg(String(e));
    }
  };

  return (
    <div
      style={{
        height: "100dvh",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        background: "var(--bg-sidebar)",
      }}
    >
      {/* header */}
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid var(--bg-hover)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexShrink: 0,
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <h2 style={{ margin: 0, fontSize: 15, whiteSpace: "nowrap" }}>Admin Panel</h2>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            <TabBtn active={tab === "config"} label="Config" onClick={() => setTab("config")} />
            <TabBtn active={tab === "users"} label={`Users (${users.length})`} onClick={() => setTab("users")} />
            <TabBtn active={tab === "sessions"} label={`Sessions (${sessions.length})`} onClick={() => setTab("sessions")} />
          </div>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          {onBack && (
            <button
              onClick={onBack}
              style={{ background: "var(--bg-hover)", color: "var(--accent-blue)", fontSize: 12, padding: "5px 12px", border: "1px solid var(--text-faintest)", borderRadius: 4 }}
            >
              ← Sessions
            </button>
          )}
          <button
            onClick={handleRestart}
            disabled={restarting}
            style={{ background: restarting ? "var(--bg-hover)" : "#4c1d95", color: restarting ? "var(--text-muted)" : "#c4b5fd", fontSize: 12, padding: "5px 12px", border: "1px solid #6d28d9", borderRadius: 4, cursor: restarting ? "not-allowed" : "pointer" }}
          >
            {restarting ? "Restarting…" : "⟳ Restart"}
          </button>
          <button
            onClick={onToggleTheme}
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            style={{
              background: "none",
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: "4px 8px",
              cursor: "pointer",
              fontSize: 14,
              color: "var(--text-muted)",
              marginRight: 8,
            }}
          >
            {theme === "dark" ? "☀️" : "🌙"}
          </button>
          <button
            onClick={onLogout}
            style={{ background: "var(--bg-hover)", color: "var(--text-body)", fontSize: 12, padding: "5px 12px" }}
          >
            Logout
          </button>
        </div>
      </div>

      {msg && (
        <div
          style={{
            padding: "8px 16px",
            background: "#1e3a5f",
            fontSize: 13,
            display: "flex",
            justifyContent: "space-between",
            flexShrink: 0,
          }}
        >
          {msg}
          <button onClick={() => setMsg("")} style={{ background: "transparent", color: "#cce5ff", padding: "2px 8px", fontSize: 11 }}>
            x
          </button>
        </div>
      )}

      {/* body */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {tab === "sessions" && (
          <div style={{ padding: "12px 16px" }}>
            <input
              placeholder="Search by owner, project, cwd, session ID, prompt..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ ...inputStyle, width: "100%", marginBottom: 12, boxSizing: "border-box" }}
            />
            <div style={{ columns: "minmax(260px, 1fr)", columnGap: 10 }}>
              {pageItems.map((s) => (
                <SessionCard key={s.id} session={s} showOwner />
              ))}
            </div>
            {sessions.length === 0 && (
              <p style={{ color: "var(--text-faint)", fontSize: 13, textAlign: "center", marginTop: 40 }}>
                {search ? "No matching sessions." : "No sessions."}
              </p>
            )}
            {totalPages > 1 && (
              <div style={{ marginTop: 12, display: "flex", justifyContent: "center", alignItems: "center", gap: 10, fontSize: 12, color: "var(--text-secondary)" }}>
                <PgBtn disabled={safePage === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} label="Prev" />
                <span>{safePage + 1}/{totalPages} ({sessions.length} total)</span>
                <PgBtn disabled={safePage >= totalPages - 1} onClick={() => setPage((p) => p + 1)} label="Next" />
              </div>
            )}
          </div>
        )}

        {tab === "config" && (
          <div style={{ padding: "12px 16px", maxWidth: 600 }}>
            {/* Workspace */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>Workspace Base Directory</h3>
              <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
                Users can only create sessions under <code style={{ color: "var(--text-secondary)" }}>{workspace}/{"<uid>"}/</code>.
                This value is saved to config.yaml.
              </p>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  value={workspaceInput}
                  onChange={(e) => setWorkspaceInput(e.target.value)}
                  placeholder="~/Projs"
                  style={{ ...inputStyle, flex: 1 }}
                />
                <button
                  disabled={!workspaceInput.trim() || workspaceInput.trim() === workspace}
                  onClick={async () => {
                    try {
                      const c = await setWorkspace(workspaceInput.trim());
                      setWorkspaceVal(c.workspace);
                      setMsg("Workspace updated.");
                    } catch (e) { setMsg(String(e)); }
                  }}
                  style={{ background: "#58a6ff", color: "#fff" }}
                >
                  Save
                </button>
              </div>
            </div>

            {/* Claude Binary */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>Claude Binary Path</h3>
              <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
                Path to the <code style={{ color: "var(--text-secondary)" }}>claude</code> CLI binary.
                If an absolute path is given, its directory is prepended to PATH in each session.
              </p>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  value={claudeBinInput}
                  onChange={(e) => setClaudeBinInput(e.target.value)}
                  placeholder="~/.local/bin/claude"
                  style={{ ...inputStyle, flex: 1 }}
                />
                <button
                  disabled={!claudeBinInput.trim() || claudeBinInput.trim() === claudeBin}
                  onClick={async () => {
                    try {
                      const c = await setClaudeBin(claudeBinInput.trim());
                      setClaudeBinVal(c.claude_bin);
                      setMsg("Claude binary path updated. Restart the server to apply.");
                    } catch (e) { setMsg(String(e)); }
                  }}
                  style={{ background: "#58a6ff", color: "#fff" }}
                >
                  Save
                </button>
              </div>
            </div>

            {/* Proxy */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>Proxy Settings</h3>
              <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
                Proxy for all sessions (applied as http_proxy &amp; https_proxy). Leave blank to disable.
              </p>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  value={proxyInput}
                  onChange={(e) => setProxyInput(e.target.value)}
                  placeholder="http://proxy:port"
                  style={{ ...inputStyle, flex: 1 }}
                />
                <button
                  disabled={proxyInput === proxy}
                  onClick={async () => {
                    try {
                      const c = await setProxy(proxyInput.trim());
                      setProxyVal(c.proxy);
                      setMsg("Proxy settings updated.");
                    } catch (e) { setMsg(String(e)); }
                  }}
                  style={{ background: "#58a6ff", color: "#fff" }}
                >
                  Save
                </button>
              </div>
            </div>

            {/* Terminal Font */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 8, fontSize: 15 }}>Terminal Font</h3>
              <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 10 }}>
                Choose a terminal font. Recommend a monospace font with CJK character support for proper alignment. Current:
                <code style={{ color: "var(--accent-blue)", marginLeft: 4 }}>{terminalFont || "default"}</code>
              </p>
              <div style={{ marginBottom: 8, display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  placeholder="Search font name…"
                  value={fontSearch}
                  onChange={(e) => setFontSearch(e.target.value)}
                  style={{ ...inputStyle, flex: 1, padding: "6px 10px", fontSize: 12 }}
                />
                {fontLoading && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Loading…</span>}
              </div>
              {/* Font list */}
              <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid var(--text-faintest)", borderRadius: 6, background: "var(--bg-base)" }}>
                {(() => {
                  const q = fontSearch.toLowerCase();
                  const filtered = fontList.filter((f) => !q || f.family.toLowerCase().includes(q));
                  if (!filtered.length) return <div style={{ padding: "10px 12px", fontSize: 12, color: "var(--text-faint)" }}>No matching fonts</div>;
                  return filtered.map((f) => {
                    const isActive = terminalFont === f.family || (!terminalFont && f.recommended && f.family.startsWith("Ubuntu"));
                    const isPreviewing = fontPreview === f.family;
                    return (
                      <div
                        key={f.family}
                        onClick={() => setFontPreview(isPreviewing ? null : f.family)}
                        style={{
                          padding: "6px 12px", cursor: "pointer", fontSize: 12,
                          background: isActive ? "rgba(88,166,255,0.12)" : isPreviewing ? "var(--bg-hover)" : "transparent",
                          borderBottom: "1px solid var(--text-faintest)",
                          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
                        }}
                        onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = "var(--bg-hover)"; }}
                        onMouseLeave={(e) => { e.currentTarget.style.background = isActive ? "rgba(88,166,255,0.12)" : isPreviewing ? "var(--bg-hover)" : "transparent"; }}
                      >
                        <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                          {f.recommended && <span style={{ fontSize: 9, padding: "1px 4px", background: "rgba(88,166,255,0.15)", color: "var(--accent-blue)", borderRadius: 3, flexShrink: 0 }}>Recommended</span>}
                          <span style={{ color: isActive ? "var(--accent-blue)" : "var(--text-body)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.family}</span>
                        </div>
                        <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
                          <span style={{ fontFamily: f.family, fontSize: 12, color: "var(--text-muted)", letterSpacing: 0.5 }}>AaBb 你好 こんにちは 안녕 مرحبا</span>
                          {isActive && <span style={{ color: "var(--accent-blue)", fontSize: 11 }}>✓</span>}
                        </div>
                      </div>
                    );
                  });
                })()}
              </div>
              {/* Preview panel */}
              {fontPreview && (
                <div style={{ marginTop: 10, padding: "10px 12px", background: "#0d1117", borderRadius: 6, border: "1px solid var(--text-faintest)" }}>
                  <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>Preview — {fontPreview}</div>
                  <div style={{ fontFamily: fontPreview, fontSize: 14, color: "#c9d1d9", lineHeight: 1.6 }}>
                    <div>AaBbCcDdEeFfGg 0123456789 !@#$%^&*()</div>
                    <div>中文你好 日本語こんにちは 한국어안녕 العربيةمرحبا</div>
                    <div>Héllo Wörld • Ñoño • Ångström • Ça va • Ελληνικά</div>
                    <div>if (x &gt;= 0) {"{"} return x * 2; {"}"}</div>
                    <div>┌─────────┐ │ table   │ └─────────┘</div>
                  </div>
                  <div style={{ marginTop: 8, display: "flex", justifyContent: "flex-end" }}>
                    <button
                      onClick={async () => {
                        try {
                          const c = await setTerminalFont(fontPreview);
                          setTerminalFontVal(c.terminal_font);
                          setFontPreview(null);
                          setMsg(`Terminal font updated to "${fontPreview}". Return to Sessions for it to take effect.`);
                        } catch (e) { setMsg(String(e)); }
                      }}
                      style={{ background: "#58a6ff", color: "#fff", fontSize: 12, padding: "5px 14px", borderRadius: 4 }}
                    >
                      Apply Font
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* SSH connection config */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>SSH Connection Config</h3>
              <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
                Used to directly open session directories in VS Code / Cursor (Remote SSH deep link).
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: "var(--text-secondary)", width: 90, flexShrink: 0 }}>Host</span>
                  <input value={sshInput.host} onChange={(e) => setSshInput((s) => ({ ...s, host: e.target.value }))} placeholder="192.168.1.1" style={{ ...inputStyle, flex: 1 }} />
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: "var(--text-secondary)", width: 90, flexShrink: 0 }}>Port</span>
                  <input type="number" value={sshInput.port} onChange={(e) => setSshInput((s) => ({ ...s, port: parseInt(e.target.value) || 22 }))} placeholder="22" style={{ ...inputStyle, flex: 1 }} />
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: "var(--text-secondary)", width: 90, flexShrink: 0 }}>User</span>
                  <input value={sshInput.user} onChange={(e) => setSshInput((s) => ({ ...s, user: e.target.value }))} placeholder="username" style={{ ...inputStyle, flex: 1 }} />
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  <button
                    disabled={sshInput.host === sshConfig.host && sshInput.port === sshConfig.port && sshInput.user === sshConfig.user}
                    onClick={async () => {
                      try {
                        const saved = await updateSshConfig(sshInput);
                        setSshConfig(saved);
                        setMsg("SSH config updated.");
                      } catch (e) { setMsg(String(e)); }
                    }}
                    style={{ background: "#58a6ff", color: "#fff" }}
                  >
                    Save
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {tab === "users" && (
          <div style={{ padding: "12px 16px", maxWidth: 700 }}>
            {/* Create User */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>Create User</h3>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <input placeholder="Username" value={newUsername} onChange={(e) => setNewUsername(e.target.value)} style={inputStyle} />
                <input type="password" placeholder="Password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} style={inputStyle} />
                <select value={newRole} onChange={(e) => setNewRole(e.target.value as "admin" | "user")} style={{ ...inputStyle, cursor: "pointer", width: "auto" }}>
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
                <button onClick={handleCreateUser} disabled={!newUsername || !newPassword} style={{ background: "#5cb85c", color: "#fff" }}>
                  Create
                </button>
              </div>
            </div>

            {/* Change Password */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>Change Password</h3>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <select value={pwUser} onChange={(e) => setPwUser(e.target.value)} style={{ ...inputStyle, cursor: "pointer", width: "auto" }}>
                  <option value="">-- select user --</option>
                  {users.map((u) => <option key={u.username} value={u.username}>{u.username}</option>)}
                </select>
                <input type="password" placeholder="New password" value={pwValue} onChange={(e) => setPwValue(e.target.value)} style={inputStyle} />
                <button onClick={handleChangePassword} disabled={!pwUser || !pwValue} style={{ background: "#f0ad4e", color: "#fff" }}>
                  Update
                </button>
              </div>
            </div>

            {/* User List */}
            <div style={cardStyle}>
              <h3 style={{ marginBottom: 12, fontSize: 15 }}>Users</h3>
              <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                    <th style={{ padding: "6px 10px" }}>Username</th>
                    <th style={{ padding: "6px 10px" }}>Role</th>
                    <th style={{ padding: "6px 10px" }}>is_admin</th>
                    <th style={{ padding: "6px 10px" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => (
                    <tr key={u.username} style={{ borderTop: "1px solid var(--bg-hover)" }}>
                      <td style={{ padding: "8px 10px" }}>{u.username}</td>
                      <td style={{ padding: "8px 10px" }}>
                        <span style={{ color: u.role === "admin" ? "#f0ad4e" : "#5bc0de", fontWeight: 600 }}>
                          {u.role}
                        </span>
                      </td>
                      <td style={{ padding: "8px 10px" }}>
                        <button
                          onClick={() => handleToggleIsAdmin(u)}
                          title="Toggle admin panel access"
                          style={{
                            background: u.is_admin ? "#4c1d95" : "var(--bg-hover)",
                            color: u.is_admin ? "#c4b5fd" : "var(--text-muted)",
                            fontSize: 11,
                            padding: "3px 10px",
                            border: `1px solid ${u.is_admin ? "#6d28d9" : "var(--text-faintest)"}`,
                            borderRadius: 4,
                            cursor: "pointer",
                          }}
                        >
                          {u.is_admin ? "✓ on" : "off"}
                        </button>
                      </td>
                      <td style={{ padding: "8px 10px" }}>
                        <button
                          onClick={() => handleDeleteUser(u.username)}
                          style={{ background: "#d9534f", color: "#fff", fontSize: 11, padding: "3px 10px" }}
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

    </div>
  );
}

function TabBtn({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button onClick={onClick} style={{ background: active ? "var(--accent-blue)" : "var(--text-faintest)", color: "#fff", fontSize: 12, padding: "4px 10px" }}>
      {label}
    </button>
  );
}

function PgBtn({ disabled, onClick, label }: { disabled: boolean; onClick: () => void; label: string }) {
  return (
    <button disabled={disabled} onClick={onClick} style={{ background: "var(--text-faintest)", color: "var(--text-body)", fontSize: 11, padding: "4px 10px" }}>
      {label}
    </button>
  );
}

const inputStyle: React.CSSProperties = {
  background: "var(--bg-base)",
  border: "1px solid var(--text-faintest)",
  borderRadius: 6,
  padding: "8px 12px",
  color: "var(--text-body)",
  fontSize: 13,
  outline: "none",
};

const cardStyle: React.CSSProperties = {
  padding: 16,
  background: "var(--bg-modal)",
  borderRadius: 8,
  marginBottom: 16,
};
