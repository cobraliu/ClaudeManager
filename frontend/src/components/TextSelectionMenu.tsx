// Global right-click menu for text selections.
//
// Mounted once at the SessionsPage root. On contextmenu:
//   - if there's no text selected → fall through to native menu
//   - if target is inside an excluded zone (terminal, CodeMirror,
//     [data-no-context-menu]) → fall through to native menu
//   - otherwise: preventDefault and show our menu of string tools.
//
// After picking a tool, a result dialog opens showing the transformed
// output with a Copy button. The selection text is captured at click
// time so the result keeps working even after the selection is cleared
// (which happens the moment you click on the menu).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CATEGORY_ORDER, STRING_TOOLS, StringTool, ToolCategory } from "../lib/stringTools";
import { copyText } from "./FileEditorModal";

interface MenuState {
  x: number;
  y: number;
  text: string;
}

interface ResultState {
  toolLabel: string;
  output: string;
  error?: string;
}

function isInExcludedZone(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target.closest(".xterm")) return true;
  if (target.closest('[class*="cm-"]')) return true;
  if (target.closest("[data-no-context-menu]")) return true;
  return false;
}

export function TextSelectionMenu() {
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [result, setResult] = useState<ResultState | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // ── contextmenu trap ────────────────────────────────────────────────────────
  useEffect(() => {
    const onCtx = (e: MouseEvent) => {
      if (e.defaultPrevented) return;
      if (isInExcludedZone(e.target)) return;
      const sel = window.getSelection();
      const text = sel ? sel.toString() : "";
      if (text.length === 0) return;
      e.preventDefault();
      e.stopPropagation();
      // Clamp to viewport so the menu doesn't render offscreen.
      const W = 220;
      const H = 460;
      const x = Math.min(e.clientX, window.innerWidth - W - 8);
      const y = Math.min(e.clientY, window.innerHeight - H - 8);
      setMenu({ x, y, text });
    };
    window.addEventListener("contextmenu", onCtx, true);
    return () => window.removeEventListener("contextmenu", onCtx, true);
  }, []);

  // ── dismiss on outside click / Esc / scroll / resize ────────────────────────
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && menuRef.current.contains(e.target as Node)) return;
      setMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenu(null);
    };
    const onScroll = () => setMenu(null);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [menu]);

  const runTool = useCallback(async (tool: StringTool, input: string) => {
    setMenu(null);
    try {
      const out = await tool.run(input);
      setResult({ toolLabel: tool.label, output: out });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setResult({ toolLabel: tool.label, output: "", error: msg });
    }
  }, []);

  const grouped = useMemo<Record<ToolCategory, StringTool[]>>(() => {
    const m: Record<string, StringTool[]> = {};
    for (const t of STRING_TOOLS) {
      (m[t.category] ||= []).push(t);
    }
    return m as Record<ToolCategory, StringTool[]>;
  }, []);

  return (
    <>
      {menu && (
        <div
          ref={menuRef}
          style={{
            position: "fixed",
            top: menu.y,
            left: menu.x,
            width: 220,
            maxHeight: 460,
            overflowY: "auto",
            background: "var(--bg-modal, #1c1f24)",
            border: "1px solid var(--border, #333)",
            borderRadius: 6,
            boxShadow: "0 6px 20px rgba(0,0,0,0.4)",
            zIndex: 9000,
            fontSize: 12,
            color: "var(--text-body)",
            padding: "4px 0",
            userSelect: "none",
          }}
        >
          {CATEGORY_ORDER.map(cat => (
            <div key={cat}>
              <div
                style={{
                  padding: "4px 10px 2px",
                  fontSize: 10,
                  letterSpacing: 0.5,
                  textTransform: "uppercase",
                  color: "var(--text-muted)",
                  borderTop: "1px solid var(--bg-hover, #222)",
                  background: "var(--bg-surface, transparent)",
                }}
              >
                {cat}
              </div>
              {(grouped[cat] || []).map(t => (
                <MenuItem key={t.id} label={t.label} onClick={() => runTool(t, menu.text)} />
              ))}
            </div>
          ))}
        </div>
      )}

      {result && (
        <ResultDialog
          title={result.toolLabel}
          output={result.output}
          error={result.error}
          onClose={() => setResult(null)}
        />
      )}
    </>
  );
}

function MenuItem({ label, onClick }: { label: string; onClick: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={onClick}
      style={{
        padding: "5px 12px",
        cursor: "pointer",
        background: hover ? "var(--bg-hover)" : "transparent",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {label}
    </div>
  );
}

function ResultDialog({
  title,
  output,
  error,
  onClose,
}: {
  title: string;
  output: string;
  error?: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const handleCopy = () => {
    copyText(output);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9100,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: "var(--bg-modal, #1c1f24)",
          border: "1px solid var(--border, #333)",
          borderRadius: 8,
          width: "min(720px, 90vw)",
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
          boxShadow: "0 12px 40px rgba(0,0,0,0.5)",
        }}
      >
        <div
          style={{
            padding: "10px 14px",
            borderBottom: "1px solid var(--bg-hover)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-body)" }}>
            {title}
          </div>
          <div style={{ flex: 1 }} />
          {!error && (
            <button
              onClick={handleCopy}
              style={{
                background: copied ? "var(--accent-green, #2ea043)" : "var(--bg-hover)",
                color: copied ? "#fff" : "var(--text-body)",
                border: "1px solid var(--border)",
                borderRadius: 5,
                padding: "4px 10px",
                fontSize: 11,
                cursor: "pointer",
              }}
            >
              {copied ? "Copied ✓" : "Copy"}
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              background: "var(--bg-hover)",
              color: "var(--text-body)",
              border: "1px solid var(--border)",
              borderRadius: 5,
              padding: "4px 10px",
              fontSize: 11,
              cursor: "pointer",
            }}
          >
            Close
          </button>
        </div>
        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: 14,
            fontFamily: "var(--font-mono, ui-monospace, monospace)",
            fontSize: 12,
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
            color: error ? "var(--accent-red, #ff6464)" : "var(--text-body)",
          }}
        >
          {error ? error : output}
        </div>
      </div>
    </div>
  );
}
