import { useState, useCallback } from "react";
import { fetchConfig, saveConfig, apiUrl } from "../api";
import { IconCheck, IconX, IconChevronLeft, IconChevronRight } from "../ui/icons";

const STEPS = ["receiver", "destinations", "reports", "admin", "summary"];

const STEP_LABELS: Record<string, string> = {
  receiver: "Receiver Settings",
  destinations: "Destinations",
  reports: "Reports (Optional)",
  admin: "Admin Password (Optional)",
  summary: "Summary",
};

interface WizardData {
  receiver: { ae_title: string; port: number };
  destinations: Array<{ name: string; host: string; port: number; aet: string }>;
  reports: { enabled: boolean; query_source?: string };
  // Plaintext only — never a hash. Posted to /api/web-ui/password on save.
  admin: { password: string; confirm: string };
}

async function validateStep(step: string, data: unknown): Promise<string[]> {
  const res = await fetch(apiUrl("/api/wizard/validate/" + step), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
    credentials: "include",
  });
  if (!res.ok) return ["validation failed"];
  const json = await res.json();
  return json.errors as string[];
}

async function echoProbe(host: string, port: number, aet: string): Promise<string> {
  const res = await fetch(apiUrl("/api/echo"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "probe", host, port, aet }),
    credentials: "include",
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
    admin: { password: "", confirm: "" },
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
    setErrors([]);
    try {
      const current = (await fetchConfig()) as Record<string, unknown>;
      // The receiver step's ae_title/port belong to the *receiver* section —
      // writing them into `general` silently discards them (GeneralConfig has
      // neither field), the operator gets a success toast, and the receiver
      // keeps its defaults forever (review P0-4).
      current.receiver = { ...(current.receiver as object || {}), ...data.receiver };
      current.destinations = data.destinations.map((d) => ({
        name: d.name,
        type: "dicom",
        enabled: true,
        host: d.host,
        port: d.port,
        aet_target: d.aet,
        aet_source: "GATEWAY",
      }));
      // Only `enabled` is taken from the wizard: `query_source` is a
      // structured object (ReportQuerySource), not the free-text
      // "host:port" string this step collects.
      current.reports = {
        ...(current.reports as object || {}),
        enabled: data.reports.enabled,
      };
      await saveConfig(current);
      // The admin password is never round-tripped through the config body —
      // it is posted as plaintext to the dedicated endpoint, which hashes it
      // server-side (review P0-8). Blank means "leave the panel open".
      if (data.admin.password) {
        const pwRes = await fetch(apiUrl("/api/web-ui/password"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ new_password: data.admin.password }),
          credentials: "include",
        });
        if (!pwRes.ok) {
          const detail = await pwRes.text();
          throw new Error(`could not set the admin password: ${detail}`);
        }
      }
      setDone(true);
    } catch (err) {
      // Surface the failure in the step's error banner instead of failing
      // silently — the operator cannot fix what they cannot see (review M8).
      setErrors([err instanceof Error ? err.message : String(err)]);
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
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
            <span style={{ color: "var(--green)", display: "inline-flex" }}><IconCheck size={18} /></span>
            Configuration saved
          </div>
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
      <div className="card">
        <div className="steps">
          {STEPS.map((s, i) => (
            <span
              key={s}
              className={`step ${i === step ? "current" : i < step ? "done" : ""}`}
              // The current step is otherwise conveyed by colour alone.
              aria-current={i === step ? "step" : undefined}
            >
              {i < step ? <IconCheck size={13} /> : null}{STEP_LABELS[s]}
            </span>
          ))}
        </div>

        <div style={{ minHeight: 200 }}>
          {errors.length > 0 ? (
            <div className="error-banner" role="alert" style={{ marginBottom: 12 }}>
              {errors.map((e) => <div key={e}>{e}</div>)}
            </div>
          ) : null}

          {currentStep === "receiver" && (
            <div>
              <div className="card-header">Receiver Settings</div>
              <div className="field-row">
                <label>
                  <span className="label">AE Title</span>
                  <input
                    className="input"
                    value={data.receiver.ae_title}
                    onChange={(e) => setData((d) => ({ ...d, receiver: { ...d.receiver, ae_title: e.target.value } }))}
                  />
                </label>
                <label>
                  <span className="label">Port</span>
                  <input
                    className="input"
                    type="number"
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
                <div key={i} className="dest-row">
                  <input className="input" name="name" placeholder="Name" value={dest.name}
                    onChange={(e) => updateDest(i, "name", e.target.value)} />
                  <input className="input" name="host" placeholder="Host" value={dest.host}
                    onChange={(e) => updateDest(i, "host", e.target.value)} />
                  <input className="input" name="port" type="number" placeholder="Port" value={dest.port}
                    onChange={(e) => updateDest(i, "port", Number(e.target.value))} />
                  <input className="input" name="aet" placeholder="AET" value={dest.aet}
                    onChange={(e) => updateDest(i, "aet", e.target.value)} />
                  <button className="btn" onClick={() => handleEcho(dest)}>Echo</button>
                  {echoStatus[dest.host + ":" + dest.port] && (
                    <span className={`badge ${echoStatus[dest.host + ":" + dest.port] === "ok" ? "green" : "red"}`}>
                      {echoStatus[dest.host + ":" + dest.port]}
                    </span>
                  )}
                  <button className="btn danger icon-only" aria-label={`Remove destination ${dest.name || i + 1}`} onClick={() => removeDest(i)}>
                    <IconX size={14} />
                  </button>
                </div>
              ))}
              <button className="btn" onClick={addDestination}>+ Add Destination</button>
            </div>
          )}

          {currentStep === "reports" && (
            <div>
              <div className="card-header">Reports (Optional)</div>
              <label className="check" style={{ marginBottom: 12 }}>
                <input type="checkbox" checked={data.reports.enabled}
                  onChange={(e) => setData((d) => ({ ...d, reports: { ...d.reports, enabled: e.target.checked } }))} />
                Enable report retrieval
              </label>
              {data.reports.enabled && (
                <label>
                  <span className="label">Query Source (host:port)</span>
                  <input className="input" value={data.reports.query_source || ""}
                    onChange={(e) => setData((d) => ({ ...d, reports: { ...d.reports, query_source: e.target.value } }))} />
                </label>
              )}
            </div>
          )}

          {currentStep === "admin" && (
            <div>
              <div className="card-header">Admin Password</div>
              <p style={{ color: "var(--muted)", marginTop: 0, fontSize: 13 }}>
                Protect the web panel with a password. Leave blank to keep the panel
                open — the gateway then binds to localhost only. The password is
                hashed before it is stored and is never sent back.
              </p>
              <div className="field-row">
                <label>
                  <span className="label">Password</span>
                  <input
                    className="input"
                    type="password"
                    autoComplete="new-password"
                    value={data.admin.password}
                    onChange={(e) => setData((d) => ({ ...d, admin: { ...d.admin, password: e.target.value } }))}
                  />
                </label>
                <label>
                  <span className="label">Confirm</span>
                  <input
                    className="input"
                    type="password"
                    autoComplete="new-password"
                    value={data.admin.confirm}
                    onChange={(e) => setData((d) => ({ ...d, admin: { ...d.admin, confirm: e.target.value } }))}
                  />
                </label>
              </div>
            </div>
          )}

          {currentStep === "summary" && (
            <div>
              <div className="card-header">Configuration Summary</div>
              <pre style={{ fontSize: 12 }}>
                {/* The plaintext never reaches the DOM — a screenshot or a
                    copy of this summary must not carry it (review P0-8). */}
                {JSON.stringify(
                  {
                    ...data,
                    admin: { password: data.admin.password ? "***" : "", confirm: "" },
                  },
                  null,
                  2,
                )}
              </pre>
            </div>
          )}
        </div>
      </div>

      <div className="toolbar">
        <button className="btn" onClick={handleBack} disabled={step === 0}>
          <IconChevronLeft size={14} /> Back
        </button>
        {step < STEPS.length - 1 ? (
          <button className="btn primary" onClick={handleNext}>
            Next <IconChevronRight size={14} />
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
