import { useState, useEffect, useMemo } from "react";
import { fetchConfig, saveConfig, fetchConfigWarnings } from "../api";
import type { ConfigWarning } from "../api";
import { lintConfigDocument } from "../config/lint";
import ServiceCard from "../ui/ServiceCard";

export default function ConfigView() {
  const [, setConfig] = useState<Record<string, unknown> | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [loadError, setLoadError] = useState("");
  const [serverWarnings, setServerWarnings] = useState<ConfigWarning[]>([]);

  useEffect(function loadConfigOnMount() {
    fetchConfig()
      .then((c) => {
        setConfig(c);
        // A slow load must not clobber edits the operator already made into
        // the textarea — an in-flight keystroke would otherwise be silently
        // replaced once the fetch resolves.
        setText((prev) => (prev ? prev : JSON.stringify(c, null, 2)));
      })
      .catch((e: unknown) => {
        setLoadError(e instanceof Error ? e.message : String(e));
      });
    fetchConfigWarnings()
      .then((r) => setServerWarnings(r.warnings ?? []))
      .catch(() => {
        /* older backend without the endpoint — not fatal */
      });
  }, []);

  // Immediate feedback: lint on every keystroke so a footgun appears under the
  // cursor instead of after a save or a restart. Server stays authoritative.
  const findings = useMemo(() => (text ? lintConfigDocument(text) : []), [text]);
  const parseErrors = findings.filter((f) => f.message.startsWith("Invalid JSON"));
  const canSave = dirty && !saving && parseErrors.length === 0;

  const handleSave = async () => {
    if (parseErrors.length > 0) return; // never send a document that won't parse
    setSaving(true);
    setMsg("");
    try {
      const parsed = JSON.parse(text);
      const result = await saveConfig(parsed);
      if (result) {
        // The running components hold their own config refs, so a saved change
        // only applies after a restart (review H5).
        const found = result.warnings?.length ?? 0;
        setMsg(
          found > 0
            ? `Config saved — ${found} lint warning${found > 1 ? "s" : ""} below; restart the gateway to apply.`
            : "Config saved — restart the gateway to apply changes.",
        );
        if (result.warnings) setServerWarnings(result.warnings);
        setDirty(false);
        setConfig(parsed);
      }
    } catch (e) {
      if (e instanceof SyntaxError) {
        setMsg(`Invalid JSON: ${e.message}`);
      } else if (e instanceof Error) {
        // saveConfig throws with the server's 400 detail (review M8).
        setMsg(e.message);
      } else {
        setMsg(String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h2>Configuration</h2>
      {loadError && (
        <div className="error-banner" role="alert">Failed to load config: {loadError}</div>
      )}

      {serverWarnings.length > 0 ? (
        <div className="banner warn" role="alert" style={{ marginBottom: 12 }}>
          {serverWarnings.map((w) => (
            <div key={w.path + w.message}>{w.severity === "info" ? "Note: " : ""}{w.message}</div>
          ))}
        </div>
      ) : null}

      <div className="toolbar">
        <button className="btn primary" onClick={handleSave} disabled={!canSave}>
          {saving ? "Saving..." : "Save"}
        </button>
        {msg ? (
          // Success messages begin "Config saved…"; anything else is a parse
          // or server rejection, which renders as an error.
          <span
            className={msg.startsWith("Config") ? "ok-note" : "error-banner"}
            style={{ margin: 0 }}
          >
            {msg}
          </span>
        ) : null}
        {dirty && parseErrors.length === 0 && findings.length > 0 ? (
          <span className="warn-note" style={{ margin: 0, display: "inline-block" }}>
            {findings.length} potential issue{findings.length > 1 ? "s" : ""}
          </span>
        ) : null}
      </div>

      {findings.length > 0 ? (
        <div className="lint-findings">
          {findings.map((f, i) => (
            <div key={i} className={f.message.startsWith("Invalid JSON") ? "lint-error" : "lint-warn"}>
              {f.line ? `Line ${f.line}: ` : ""}
              {f.message}
            </div>
          ))}
        </div>
      ) : null}

      <div className="card">
        <textarea
          style={{
            width: "100%", minHeight: 500, resize: "vertical",
          }}
          value={text}
          onChange={(e) => { setText(e.target.value); setDirty(true); }}
          spellCheck={false}
          aria-label="Configuration JSON"
        />
      </div>
      <ServiceCard />
    </div>
  );
}
