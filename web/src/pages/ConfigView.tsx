import { useState, useEffect } from "react";
import { fetchConfig, saveConfig } from "../api";

export default function ConfigView() {
  const [, setConfig] = useState<Record<string, unknown> | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetchConfig().then((c) => {
      setConfig(c);
      setText(JSON.stringify(c, null, 2));
    });
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const parsed = JSON.parse(text);
      const result = await saveConfig(parsed);
      if (result) {
        // The running components hold their own config refs, so a saved change
        // only applies after a restart (review H5).
        setMsg("Config saved — restart the gateway to apply changes.");
        setDirty(false);
        setConfig(parsed);
      } else {
        setMsg("Save failed");
      }
    } catch (e) {
      setMsg(`Invalid JSON: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h2>Configuration</h2>
      <div className="toolbar">
        <button className="btn primary" onClick={handleSave} disabled={!dirty || saving}>
          {saving ? "Saving..." : "Save"}
        </button>
        {msg && <span className={msg.startsWith("Invalid") ? "error-banner" : "ok-note"} style={msg.startsWith("Invalid") ? { margin: 0 } : undefined}>{msg}</span>}
      </div>
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
    </div>
  );
}