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
      const ok = await saveConfig(parsed);
      setMsg(ok ? "Config saved" : "Save failed");
      if (ok) { setDirty(false); setConfig(parsed); }
    } catch (e) {
      setMsg(`Invalid JSON: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h2>Configuration</h2>
      <div className="card" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
          <button className="btn primary" onClick={handleSave} disabled={!dirty || saving}>
            {saving ? "Saving..." : "Save"}
          </button>
          {msg && <span style={{ color: "var(--accent)", fontSize: 13 }}>{msg}</span>}
        </div>
        <pre style={{ maxHeight: 600, overflowY: "auto" }}>
          <textarea
            style={{
              width: "100%", minHeight: 500, background: "var(--surface2)",
              color: "var(--text)", border: "1px solid var(--border)", borderRadius: 6,
              padding: 12, fontFamily: "var(--mono)", fontSize: 12, resize: "vertical",
            }}
            value={text}
            onChange={(e) => { setText(e.target.value); setDirty(true); }}
          />
        </pre>
      </div>
    </div>
  );
}