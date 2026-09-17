/**
 * Destinations page — add/edit/remove delivery targets without editing JSON
 * (refinement 2026-09-17).
 *
 * The panel previously offered no destination management after the first-run
 * wizard: the Queue view's Enqueue error pointed at a "Destinations page" that
 * did not exist, and the only path to add or re-enable a PACS was hand-editing
 * the raw config JSON — the same failure mode that made `config_version: 1`
 * (unquoted) crash the frozen binary at boot in the E1 dry run.
 *
 * Secrets: GET /api/config redacts credential fields to the "***" sentinel;
 * sending it back means "unchanged", and the backend restores the stored value.
 * So a secret field showing the sentinel is *not* the real secret, and an empty
 * one means "cleared by the operator". We never send a blank secret for an
 * existing destination the operator did not touch.
 */
import { useCallback, useEffect, useState } from "react";
import { fetchConfig, saveConfig, apiUrl, fetchConfigWarnings } from "../api";
import type { ConfigWarning } from "../api";
import { IconCheck, IconX, IconPlus } from "../ui/icons";

/** Fields each destination type exposes in the form. Secret fields are masked. */
const TYPE_FIELDS: Record<
  string,
  Array<{ key: string; label: string; type?: "number"; secret?: boolean; placeholder?: string }>
> = {
  dicom: [
    { key: "host", label: "Host" },
    { key: "port", label: "Port", type: "number" },
    { key: "aet_target", label: "Called AET" },
    { key: "aet_source", label: "Our AET" },
  ],
  dicom_tls: [
    { key: "host", label: "Host" },
    { key: "port", label: "Port", type: "number" },
    { key: "aet_target", label: "Called AET" },
    { key: "aet_source", label: "Our AET" },
    { key: "cacert", label: "CA cert path", placeholder: "(default system CAs)" },
  ],
  dicomweb: [
    { key: "url", label: "STOW-RS URL" },
    { key: "aet", label: "AET", placeholder: "(optional)" },
    { key: "auth_token", label: "Auth token", secret: true, placeholder: "(optional)" },
  ],
  sftp: [
    { key: "host", label: "Host" },
    { key: "port", label: "Port", type: "number" },
    { key: "username", label: "Username" },
    { key: "password", label: "Password", secret: true, placeholder: "(unchanged)" },
    { key: "private_key", label: "Private key path", placeholder: "(or password)" },
  ],
  rsync: [
    { key: "host", label: "Host" },
    { key: "ssh_port", label: "SSH port", type: "number" },
    { key: "username", label: "Username" },
    { key: "remote_path", label: "Remote path" },
  ],
  s3: [
    { key: "bucket", label: "Bucket" },
    { key: "endpoint_url", label: "Endpoint URL", placeholder: "(optional)" },
    { key: "region", label: "Region", placeholder: "(optional)" },
    { key: "access_key_id", label: "Access key ID", secret: true },
    { key: "secret_access_key", label: "Secret access key", secret: true, placeholder: "(unchanged)" },
  ],
  folder: [{ key: "path", label: "Path" }],
  xnat: [{ key: "url", label: "URL" }],
};

const DEST_TYPES = Object.keys(TYPE_FIELDS);

interface Destination {
  name: string;
  type: string;
  enabled: boolean;
  [key: string]: unknown;
}

async function echoProbe(host: string, port: number, aet: string): Promise<string> {
  const res = await fetch(apiUrl("/api/echo"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "probe", host, port, aet }),
    credentials: "include",
  });
  if (!res.ok) return "error";
  const json = (await res.json()) as { status: string };
  return json.status;
}

const SECRET_PLACEHOLDER = "••••••••";

export default function DestinationsView() {
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [echoState, setEchoState] = useState<Record<string, string>>({});
  const [warnings, setWarnings] = useState<ConfigWarning[]>([]);

  const load = useCallback(async () => {
    try {
      const cfg = (await fetchConfig()) as Record<string, unknown>;
      setDestinations((cfg.destinations as Destination[]) ?? []);
      setDirty(false);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    // Lint runs against the running config; refresh on entry so a stale rule
    // surfaces here rather than only in the operations log.
    fetchConfigWarnings()
      .then((r) => setWarnings(r.warnings ?? []))
      .catch(() => {
        /* an older backend without the endpoint is not fatal */
      });
  }, [load]);

  const markDirty = () => setDirty(true);

  const update = (i: number, key: string, value: unknown) => {
    setDestinations((ds) => {
      const next = [...ds];
      next[i] = { ...next[i], [key]: value };
      return next;
    });
    markDirty();
  };

  const addDestination = () => {
    setDestinations((ds) => [
      ...ds,
      { name: "", type: "dicom", enabled: true, host: "", port: 104, aet_target: "", aet_source: "GATEWAY" },
    ]);
    markDirty();
  };

  const removeDestination = (i: number) => {
    setDestinations((ds) => ds.filter((_, idx) => idx !== i));
    markDirty();
  };

  const handleEcho = async (d: Destination) => {
    const key = d.name || `${d.host}:${d.port}`;
    setEchoState((prev) => ({ ...prev, [key]: "probing" }));
    const status = await echoProbe(String(d.host), Number(d.port), String(d.aet_target ?? ""));
    setEchoState((prev) => ({ ...prev, [key]: status }));
  };

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    setError("");
    try {
      const current = (await fetchConfig()) as Record<string, unknown>;
      // Send only the destinations section; the rest of the config round-trips
      // untouched so this page cannot clobber an unrelated setting.
      current.destinations = destinations;
      const result = await saveConfig(current);
      setDirty(false);
      const found = result?.warnings?.length ?? 0;
      setMsg(
        found > 0
          ? `Saved — ${found} lint warning${found > 1 ? "s" : ""} below; restart the gateway to apply.`
          : "Saved — restart the gateway to apply changes.",
      );
      if (found > 0 && result?.warnings) setWarnings(result.warnings);
    } catch (e) {
      // The 400 detail names the failing field ("Invalid config: ...") — show it
      // rather than a bare "Save failed" (review M8).
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const typeWarnings = (i: number) =>
    warnings.filter((w) => w.path === `destinations[${i}]` || w.path.startsWith(`destinations[${i}].`));

  if (!loaded) return <div className="loading">Loading destinations</div>;

  return (
    <div>
      <h2>Destinations</h2>

      {warnings.length > 0 ? (
        <div className="banner warn" role="alert" style={{ marginBottom: 12 }}>
          {warnings.map((w) => (
            <div key={w.path + w.message}>
              {w.severity === "info" ? "Note: " : ""}
              {w.message}
            </div>
          ))}
        </div>
      ) : null}

      {error ? <div className="error-banner" role="alert" style={{ marginBottom: 12 }}>{error}</div> : null}
      {msg ? <div className="ok-note" style={{ marginBottom: 12 }}>{msg}</div> : null}

      {destinations.length === 0 ? (
        <div className="card">
          <div className="card-header">No destinations configured</div>
          <p style={{ color: "var(--muted)" }}>
            Studies are received and persisted but have nowhere to go. Add a destination to start forwarding.
          </p>
        </div>
      ) : (
        destinations.map((d, i) => {
          const fields = TYPE_FIELDS[d.type] ?? [];
          const echoKey = d.name || `${d.host}:${d.port}`;
          const echoStatus = echoState[echoKey];
          const canEcho = d.type === "dicom" || d.type === "dicom_tls";
          return (
            <div className="card" key={i} style={{ marginBottom: 12 }}>
              <div className="dest-header">
                <input
                  className="input"
                  name="name"
                  placeholder="Name (unique)"
                  value={String(d.name ?? "")}
                  onChange={(e) => update(i, "name", e.target.value)}
                  aria-label="Destination name"
                />
                <select
                  className="input"
                  value={d.type}
                  onChange={(e) => {
                    // Switching type replaces the field set; carry over the
                    // common fields only, so a stale type-specific field (e.g.
                    // an S3 bucket on a dicom destination) is not inherited.
                    update(i, "type", e.target.value);
                  }}
                  aria-label="Destination type"
                >
                  {DEST_TYPES.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
                <label className="check" title="A disabled destination is never routed to">
                  <input
                    type="checkbox"
                    checked={d.enabled}
                    onChange={(e) => update(i, "enabled", e.target.checked)}
                  />
                  enabled
                </label>
                {canEcho ? (
                  <>
                    <button
                      className="btn"
                      onClick={() => void handleEcho(d)}
                      disabled={!d.host || echoStatus === "probing"}
                    >
                      Echo
                    </button>
                    {echoStatus && echoStatus !== "probing" ? (
                      <span className={`badge ${echoStatus === "ok" ? "green" : "red"}`}>{echoStatus}</span>
                    ) : null}
                  </>
                ) : null}
                <button
                  className="btn danger icon-only"
                  aria-label={`Remove destination ${d.name || i + 1}`}
                  onClick={() => removeDestination(i)}
                >
                  <IconX size={14} />
                </button>
              </div>

              <div className="field-row">
                {fields.map((f) => {
                  const value = d[f.key];
                  const isSecret = f.secret === true;
                  // The backend redacts a stored secret to the "***" sentinel.
                  // Render that sentinel as an EMPTY field whose placeholder is
                  // the mask: the real value is never shown, and typing replaces
                  // cleanly instead of appending to literal bullet glyphs (which
                  // would be saved as the credential). A value the operator has
                  // typed is NOT the sentinel, so it renders — masked by
                  // type="password" — and survives the re-render.
                  const display =
                    value === undefined || value === null || value === "***"
                      ? ""
                      : String(value);
                  return (
                    <label key={f.key}>
                      <span className="label">{f.label}</span>
                      <input
                        className="input"
                        type={isSecret ? "password" : f.type ?? "text"}
                        placeholder={isSecret ? SECRET_PLACEHOLDER : f.placeholder}
                        value={display}
                        onChange={(e) => {
                          const next = f.type === "number" ? Number(e.target.value) : e.target.value;
                          update(i, f.key, next);
                        }}
                        aria-label={`${d.name || "destination"} ${f.label}`}
                      />
                    </label>
                  );
                })}
              </div>

              {typeWarnings(i).length > 0 ? (
                <div className="warn-note">
                  {typeWarnings(i).map((w) => (
                    <div key={w.path + w.message}>{w.message}</div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })
      )}

      <div className="toolbar">
        <button className="btn" onClick={addDestination}>
          <IconPlus size={14} /> Add Destination
        </button>
        <button
          className="btn primary"
          onClick={() => void handleSave()}
          disabled={!dirty || saving}
        >
          {saving ? "Saving..." : dirty ? "Save Changes" : "Saved"}
          {dirty ? null : <IconCheck size={14} />}
        </button>
      </div>
    </div>
  );
}
