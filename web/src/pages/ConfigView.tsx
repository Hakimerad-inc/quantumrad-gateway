import { useState, useEffect, useMemo, useRef } from "react";
import {
  fetchConfig,
  saveConfig,
  fetchConfigWarnings,
  exportConfig,
  importConfig,
  exportDiagnostics,
} from "../api";
import type { ConfigWarning } from "../api";
import { lintConfigDocument } from "../config/lint";
import ServiceCard from "../ui/ServiceCard";

export default function ConfigView() {
  const [, setConfig] = useState<Record<string, unknown> | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  // Explicit kind alongside msg: the prefix test ("starts with 'Config'") was
  // fine for one success string, but export/import add their own and an error
  // message can legitimately begin with the same word.
  const [msgKind, setMsgKind] = useState<"ok" | "error">("ok");
  const [loadError, setLoadError] = useState("");
  const [serverWarnings, setServerWarnings] = useState<ConfigWarning[]>([]);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [bundling, setBundling] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

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
        setMsgKind("ok");
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
        setMsgKind("error");
        setMsg(`Invalid JSON: ${e.message}`);
      } else if (e instanceof Error) {
        // saveConfig throws with the server's 400 detail (review M8).
        setMsgKind("error");
        setMsg(e.message);
      } else {
        setMsgKind("error");
        setMsg(String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  // Download the redacted config. The file carries '***' sentinels where
  // secrets were, so it is safe to attach to a ticket but is not itself a
  // restorable backup (see import below).
  const handleExport = async () => {
    setExporting(true);
    setMsg("");
    try {
      await exportConfig();
      setMsgKind("ok");
      setMsg("Config exported — secrets are redacted in the downloaded file.");
    } catch (e) {
      setMsgKind("error");
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  };

  const handleImport = async (file: File) => {
    setImporting(true);
    setMsg("");
    try {
      const result = await importConfig(file);
      // Unknown keys the appliance's schema does not carry are dropped, not
      // stored — name them so a settings gap does not pass silently.
      const dropped = result.ignored_keys ?? [];
      setMsgKind("ok");
      setMsg(
        dropped.length > 0
          ? `Config imported — restart to apply. ${dropped.length} unknown key(s) not applied: ${dropped.join(", ")}`
          : "Config imported — restart the gateway to apply.",
      );
      // The import replaced the running config. Reload it into the editor, or
      // the textarea still holds the pre-import document and a later Save
      // would silently write it back over the file just applied.
      const reloaded = await fetchConfig();
      setConfig(reloaded);
      setText(JSON.stringify(reloaded, null, 2));
      setDirty(false);
    } catch (e) {
      setMsgKind("error");
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setImporting(false);
    }
  };

  const handleFileChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // Reset first so a rejected import can be re-selected immediately — a file
    // input that still holds the same path will not fire onChange twice.
    e.target.value = "";
    if (!file) return;
    if (
      !window.confirm(
        "Importing replaces the current configuration and only takes effect after a restart. Continue?",
      )
    ) {
      return;
    }
    await handleImport(file);
  };

  const handleDiagnostics = async () => {
    setBundling(true);
    setMsg("");
    try {
      await exportDiagnostics();
      setMsgKind("ok");
      setMsg("Support bundle downloaded.");
    } catch (e) {
      setMsgKind("error");
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBundling(false);
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
        <button className="btn" onClick={handleExport} disabled={exporting}>
          {exporting ? "Exporting..." : "Export config"}
        </button>
        <button
          className="btn"
          onClick={() => fileInput.current?.click()}
          disabled={importing}
        >
          {importing ? "Importing..." : "Import config…"}
        </button>
        {/* Visually hidden but focusable and labelled: the Import button drives
          it, but a keyboard or assistive-tech user reaching the input directly
          still gets a name for it. */}
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          onChange={handleFileChosen}
          style={{ display: "none" }}
          aria-label="Import configuration file"
        />
        {msg ? (
          // msgKind says whether the message is a status update or an error,
          // so an operator's screen reader announces each appropriately.
          <span
            className={msgKind === "ok" ? "ok-note" : "error-banner"}
            role={msgKind === "ok" ? "status" : "alert"}
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
      <div className="card">
        <div className="card-header">Diagnostics</div>
        <div style={{ padding: 16 }}>
          <p className="hint" style={{ marginTop: 0 }}>
            Download a support bundle — redacted configuration, audit events, spool summary, and
            version — to send with any support request. Credentials are stripped; patient
            identifiers follow <code>audit.phi_scope</code> and are reduced, not removed, by
            default.
          </p>
          <button className="btn" onClick={handleDiagnostics} disabled={bundling}>
            {bundling ? "Preparing..." : "Download support bundle"}
          </button>
        </div>
      </div>
      <ServiceCard />
    </div>
  );
}
