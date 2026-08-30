import { useState, useCallback } from "react";
import type React from "react";
import { fetchConfig, saveConfig } from "../api";

const STEPS = ["receiver", "destinations", "reports", "summary"];

const STEP_LABELS: Record<string, string> = {
  receiver: "Receiver Settings",
  destinations: "Destinations",
  reports: "Reports (Optional)",
  summary: "Summary",
};

interface WizardData {
  receiver: { ae_title: string; port: number };
  destinations: Array<{ name: string; host: string; port: number; aet: string }>;
  reports: { enabled: boolean; query_source?: string };
}

async function validateStep(step: string, data: unknown): Promise<string[]> {
  const res = await fetch("/api/wizard/validate/" + step, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) return ["validation failed"];
  const json = await res.json();
  return json.errors as string[];
}

async function echoProbe(host: string, port: number, aet: string): Promise<string> {
  const res = await fetch("/api/echo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "probe", host, port, aet }),
  });
  if (!res.ok) return "error";
  const json = await res.json();
  return json.status as string;
}

export default function SetupWizardPage() {
  const [step, setStep] = useState(0);
  const [data, setData] = useState<WizardData>({
    receiver: { ae_title: "GATEWAY", port: 11112 },
    destinations: [],
    reports: { enabled: false },
  });
  const [errors, setErrors] = useState<string[]>([]);
  const [echoStatus, setEchoStatus] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);

  const currentStep = STEPS[step];

  const handleNext = useCallback(async () => {
    setErrors([]);
    const stepData = data[currentStep as keyof WizardData];
    const errs = await validateStep(currentStep, stepData);
    if (errs.length > 0) {
      setErrors(errs);
      return;
    }
    if (step < STEPS.length - 1) setStep(step + 1);
  }, [step, currentStep, data]);

  const handleBack = useCallback(() => {
    setErrors([]);
    if (step > 0) setStep(step - 1);
  }, [step]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const current = (await fetchConfig()) as Record<string, unknown>;
      current.general = { ...(current.general as object || {}), ...data.receiver };
      current.destinations = data.destinations.map((d) => ({
        name: d.name,
        type: "dicom",
        enabled: true,
        host: d.host,
        port: d.port,
        aet_target: d.aet,
        aet_source: "GATEWAY",
      }));
      current.reports = { ...(current.reports as object || {}), ...data.reports };
      await saveConfig(current);
      setDone(true);
    } finally {
      setSaving(false);
    }
  };

  const handleEcho = async (dest: { host: string; port: number; aet: string }) => {
    const status = await echoProbe(dest.host, dest.port, dest.aet);
    setEchoStatus((prev) => ({ ...prev, [dest.host + ":" + dest.port]: status }));
  };

  const addDestination = () => {
    setData((d) => ({
      ...d,
      destinations: [...d.destinations, { name: "", host: "", port: 104, aet: "MERCURE" }],
    }));
  };

  const updateDest = (i: number, field: string, value: string | number) => {
    setData((d) => {
      const dests = [...d.destinations];
      dests[i] = { ...dests[i], [field]: value };
      return { ...d, destinations: dests };
    });
  };

  const removeDest = (i: number) => {
    setData((d) => ({ ...d, destinations: d.destinations.filter((_, idx) => idx !== i) }));
  };

  if (done) {
    return (
      <div>
        <h2>Setup Complete</h2>
        <div className="card">
          <div className="card-header" style={{ fontSize: 16 }}>✓ Configuration saved</div>
          <p style={{ color: "var(--muted)", marginTop: 8 }}>
            The gateway is configured and running. You can now receive and forward DICOM studies.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2>Setup Wizard</h2>
      <div className="card" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
          {STEPS.map((s, i) => (
            <span
              key={s}
              style={{
                flex: 1, padding: "8px 12px", borderRadius: 6, textAlign: "center",
                background: i === step ? "var(--accent-dim)" : i < step ? "rgba(34,197,94,0.12)" : "var(--surface2)",
                color: i === step ? "var(--accent)" : i < step ? "var(--green)" : "var(--muted)",
                fontSize: 12, fontWeight: 600,
              }}
            >
              {i < step ? "✓ " : ""}{STEP_LABELS[s]}
            </span>
          ))}
        </div>

        <div style={{ minHeight: 200 }}>
          {errors.length > 0 && (
            <div className="error-banner" style={{ marginBottom: 12 }}>
              {errors.map((e) => <div key={e}>{e}</div>)}
            </div>
          )}

          {currentStep === "receiver" && (
            <div>
              <div className="card-header">Receiver Settings</div>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                <label>
                  <div className="label">AE Title</div>
                  <input
                    style={inputStyle}
                    value={data.receiver.ae_title}
                    onChange={(e) => setData((d) => ({ ...d, receiver: { ...d.receiver, ae_title: e.target.value } }))}
                  />
                </label>
                <label>
                  <div className="label">Port</div>
                  <input
                    type="number"
                    style={inputStyle}
                    value={data.receiver.port}
                    onChange={(e) => setData((d) => ({ ...d, receiver: { ...d.receiver, port: Number(e.target.value) } }))}
                  />
                </label>
              </div>
            </div>
          )}

          {currentStep === "destinations" && (
            <div>
              <div className="card-header">Destinations</div>
              {data.destinations.map((dest, i) => (
                <div key={i} style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "center" }}>
                  <input placeholder="Name" style={inputStyle} value={dest.name}
                    onChange={(e) => updateDest(i, "name", e.target.value)} />
                  <input placeholder="Host" style={inputStyle} value={dest.host}
                    onChange={(e) => updateDest(i, "host", e.target.value)} />
                  <input type="number" placeholder="Port" style={{ ...inputStyle, width: 80 }} value={dest.port}
                    onChange={(e) => updateDest(i, "port", Number(e.target.value))} />
                  <input placeholder="AET" style={{ ...inputStyle, width: 80 }} value={dest.aet}
                    onChange={(e) => updateDest(i, "aet", e.target.value)} />
                  <button className="btn" onClick={() => handleEcho(dest)}>Echo</button>
                  {echoStatus[dest.host + ":" + dest.port] && (
                    <span className={`badge ${echoStatus[dest.host + ":" + dest.port] === "ok" ? "green" : "red"}`}>
                      {echoStatus[dest.host + ":" + dest.port]}
                    </span>
                  )}
                  <button className="btn danger" onClick={() => removeDest(i)}>✕</button>
                </div>
              ))}
              <button className="btn" onClick={addDestination}>+ Add Destination</button>
            </div>
          )}

          {currentStep === "reports" && (
            <div>
              <div className="card-header">Reports (Optional)</div>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                <input type="checkbox" checked={data.reports.enabled}
                  onChange={(e) => setData((d) => ({ ...d, reports: { ...d.reports, enabled: e.target.checked } }))} />
                Enable report retrieval
              </label>
              {data.reports.enabled && (
                <label>
                  <div className="label">Query Source (host:port)</div>
                  <input style={inputStyle} value={data.reports.query_source || ""}
                    onChange={(e) => setData((d) => ({ ...d, reports: { ...d.reports, query_source: e.target.value } }))} />
                </label>
              )}
            </div>
          )}

          {currentStep === "summary" && (
            <div>
              <div className="card-header">Configuration Summary</div>
              <pre style={{ fontSize: 12 }}>
                {JSON.stringify(data, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <button className="btn" onClick={handleBack} disabled={step === 0}>
          ← Back
        </button>
        {step < STEPS.length - 1 ? (
          <button className="btn primary" onClick={handleNext}>
            Next →
          </button>
        ) : (
          <button className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save Configuration"}
          </button>
        )}
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  background: "var(--surface2)", color: "var(--text)", border: "1px solid var(--border)",
  borderRadius: 6, padding: "8px 12px", fontSize: 13, fontFamily: "var(--mono)",
};