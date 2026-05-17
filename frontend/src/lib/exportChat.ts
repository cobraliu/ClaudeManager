import { renderMarkdown } from "./markdown";
import { renderMermaidToHtml } from "./mermaid";
import { getConversation, type ConversationTurn, type SessionMeta } from "../api/sessionApi";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatTs(ts: number): string {
  if (!ts || ts < 1_000_000) return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function renderTurn(t: ConversationTurn): Promise<string> {
  const isUser = t.role === "user";
  const ts = formatTs(t.ts);
  // Assistant text may contain ```mermaid blocks — inline them as SVG so
  // the exported HTML works offline (no runtime mermaid.js needed).
  const body = isUser
    ? `<div class="text">${escapeHtml(t.text)}</div>`
    : `<div class="md">${await renderMermaidToHtml(renderMarkdown(t.text))}</div>`;
  return `<div class="row ${isUser ? "user" : "assistant"}">
  <div class="bubble">${body}</div>
  ${ts ? `<div class="ts">${ts}</div>` : ""}
</div>`;
}

async function buildHtml(title: string, turns: ConversationTurn[]): Promise<string> {
  const body = (await Promise.all(turns.map(renderTurn))).join("\n");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${escapeHtml(title)}</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
         max-width: 920px; margin: 0 auto; padding: 24px 20px 60px; line-height: 1.6;
         background: #fafafa; color: #222; }
  header { border-bottom: 1px solid #e5e5e5; margin-bottom: 20px; padding-bottom: 12px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .meta { color: #888; font-size: 12px; }
  .row { display: flex; flex-direction: column; margin-bottom: 14px; }
  .row.user { align-items: flex-end; }
  .row.assistant { align-items: flex-start; }
  .bubble { max-width: 85%; padding: 10px 14px; font-size: 14px; }
  .row.user .bubble { background: #1a4a7a; color: #cce5ff; border-radius: 16px 16px 4px 16px; }
  .row.assistant .bubble { background: #ececec; color: #222; border-radius: 16px 16px 16px 4px; }
  .text { white-space: pre-wrap; word-break: break-word; }
  .md > :first-child { margin-top: 0; }
  .md > :last-child { margin-bottom: 0; }
  .md p { margin: 0 0 8px; }
  .md h1, .md h2, .md h3, .md h4 { margin: 12px 0 6px; line-height: 1.3; }
  .md h1 { font-size: 18px; } .md h2 { font-size: 16px; } .md h3 { font-size: 15px; } .md h4 { font-size: 14px; }
  .md ul, .md ol { margin: 4px 0 8px; padding-left: 22px; }
  .md li { margin: 2px 0; }
  .md blockquote { border-left: 3px solid #bbb; margin: 6px 0; padding: 2px 12px; color: #555; }
  .md table { border-collapse: collapse; margin: 6px 0; font-size: 13px; }
  .md th, .md td { border: 1px solid #ccc; padding: 4px 8px; }
  .md a { color: #1a4a7a; }
  .md pre.conv-code-block { background: #1e1e1e; color: #f8f8f2; padding: 10px 12px;
                             border-radius: 8px; overflow-x: auto; font-size: 12.5px; margin: 6px 0; }
  .md pre.conv-code-block code { font-family: "Cascadia Code", "Fira Code", Menlo, Monaco, "Courier New", monospace; }
  .md code.conv-code-inline { background: rgba(0,0,0,0.08); padding: 1px 5px; border-radius: 4px;
                               font-size: 12.5px; font-family: "Cascadia Code", Menlo, Monaco, monospace; }
  .md .mermaid-rendered { display: flex; justify-content: center; margin: 8px 0;
                          padding: 8px; background: #fff; border: 1px solid #e5e5e5; border-radius: 6px; overflow-x: auto; }
  .md .mermaid-rendered svg { max-width: 100%; height: auto; }
  .md .mermaid-error { background: #fee; color: #c00; border: 1px solid #fcc; border-radius: 4px;
                       padding: 8px; font-size: 12px; white-space: pre-wrap; overflow-x: auto; }
  .ts { font-size: 10px; color: #999; margin-top: 2px; padding: 0 4px; }
  @media (prefers-color-scheme: dark) {
    body { background: #1a1a1a; color: #eaeaea; }
    header { border-bottom-color: #333; }
    .row.assistant .bubble { background: #2a2a2a; color: #eaeaea; }
    .md code.conv-code-inline { background: rgba(255,255,255,0.1); }
    .md blockquote { border-left-color: #555; color: #aaa; }
    .md th, .md td { border-color: #444; }
    .md a { color: #6ab0f3; }
    .md .mermaid-rendered { background: #f5f5f5; border-color: #444; }
  }
</style>
</head>
<body>
<header>
  <h1>${escapeHtml(title)}</h1>
  <div class="meta">Exported ${new Date().toLocaleString()} · ${turns.length} turn${turns.length === 1 ? "" : "s"}</div>
</header>
${body}
</body>
</html>`;
}

function sanitizeFilename(s: string): string {
  return s.replace(/[/\\:*?"<>|\x00-\x1f]/g, "_").slice(0, 120) || "session";
}

export async function downloadConversationHtml(s: SessionMeta): Promise<void> {
  const turns = await getConversation(s.id, 0);
  if (turns.length === 0) {
    throw new Error("No conversation history to export.");
  }
  const title = `${s.name || s.id} — Chat`;
  const html = await buildHtml(title, turns);
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${sanitizeFilename(s.name || s.id)}_chat.html`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
