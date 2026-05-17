import { marked } from "marked";
import markedKatex from "marked-katex-extension";
// Use the "common" subset (~35 most-used languages) instead of the full ~200.
// Cuts highlight.js bundle from ~950KB to ~430KB.
import hljs from "highlight.js/lib/common";

// ── KaTeX (LaTeX math) ────────────────────────────────────────────────────────
marked.use(markedKatex({ throwOnError: false, output: "html" }));

// Base64-encode a UTF-8 string for safe round-trip through HTML attributes.
// btoa() only handles latin-1, so we percent-encode first.
function encodeMermaidSrc(s: string): string {
  return btoa(unescape(encodeURIComponent(s)));
}

// ── Code highlighting + inline code styling ───────────────────────────────────
marked.use({
  breaks: true,
  gfm: true,
  renderer: {
    code(token) {
      const lang = token.lang || "";
      if (lang === "mermaid") {
        // Emit a placeholder. lib/mermaid.ts has a MutationObserver that
        // finds these, decodes data-src, and replaces innerHTML with the
        // rendered SVG. Source is base64-encoded so Mermaid syntax
        // (-->, &, <, etc.) survives going through HTML.
        return `<div class="mermaid-block" data-src="${encodeMermaidSrc(token.text)}"></div>`;
      }
      let highlighted: string;
      try {
        highlighted = lang && hljs.getLanguage(lang)
          ? hljs.highlight(token.text, { language: lang }).value
          : hljs.highlightAuto(token.text).value;
      } catch {
        highlighted = token.text.replace(/&/g, "&amp;").replace(/</g, "&lt;");
      }
      return `<pre class="conv-code-block"><code class="hljs language-${lang}">${highlighted}</code></pre>`;
    },
    codespan(token) {
      const escaped = token.text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      return `<code class="conv-code-inline">${escaped}</code>`;
    },
  },
} as Parameters<typeof marked.use>[0]);

export { marked };

export function renderMarkdown(text: string): string {
  try {
    return marked.parse(text) as string;
  } catch {
    return `<pre>${text.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</pre>`;
  }
}
