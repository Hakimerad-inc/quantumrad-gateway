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
import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchConfig, saveConfig, apiUrl, fetchConfigWarnings, previewRouting } from "../api";
import type { ConfigWarning, GatewayConfig, RulePreview } from "../api";
import { IconCheck, IconX, IconPlus } from "../ui/icons";

/** Fields each destination type exposes in the form. Secret fields are masked.
 *
 * These mirror the required/optional fields of each pydantic destination model
 * in ``src/mercure_gateway/config/__init__.py``. A required field missing here
 * means the operator cannot create a valid destination of that type from the
 * page — the save fails with a 400 naming a field the form never showed. */
const TYPE_FIELDS: Record<
  string,
  Array<{
    key: string;
    label: string;
    type?: "number" | "boolean";
    secret?: boolean;
    placeholder?: string;
    hint?: string;
  }>
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
    { key: "verify_peer", label: "Verify server cert", type: "boolean" },
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
    { key: "passphrase", label: "Key passphrase", secret: true, placeholder: "(optional)" },
    { key: "remote_path", label: "Remote path" },
    // An unset known_hosts means no host keys are trusted, so every connection
    // is rejected — a panel-created SFTP destination would be guaranteed
    // non-functional, the exact footgun this page exists to remove.
    {
      key: "known_hosts",
      label: "known_hosts path",
      hint: "Pre-seed with ssh-keyscan; empty rejects every connection",
    },
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
    { key: "remote_prefix", label: "Remote prefix" },
    { key: "use_https", label: "Use HTTPS", type: "boolean" },
  ],
  folder: [{ key: "path", label: "Path" }],
  xnat: [
    { key: "url", label: "URL" },
    // username/password/project are all min_length=1 in the model; the form
    // previously showed only the URL, so every XNAT add ended in a 400.
    { key: "username", label: "Username" },
    { key: "password", label: "Password", secret: true },
    { key: "project", label: "Project" },
    { key: "subject", label: "Subject", placeholder: "(optional)" },
  ],
};

const DEST_TYPES = Object.keys(TYPE_FIELDS);

// The model's enum values are developer-facing ("dicom_tls"); an operator
// picking from a dropdown should not have to decode them. The value sent to
// the server stays the enum — only the label changes.
const TYPE_LABELS: Record<string, string> = {
  dicom: "DICOM (C-STORE)",
  dicom_tls: "DICOM over TLS",
  dicomweb: "DICOMweb (STOW-RS)",
  sftp: "SFTP",
  rsync: "rsync over SSH",
  s3: "S3 / object storage",
  folder: "Local folder",
  xnat: "XNAT",
};

interface Destination {
  name: string;
  type: string;
  enabled: boolean;
  // Client-only: a stable identity for React's key, never sent to the backend
  // (stripped in handleSave). See STABLE_ID below.
  _uid?: number;
  [key: string]: unknown;
}

// React keys must be stable across reorders and deletes. Keying by array index
// is subtly wrong here: delete a card above a focused one and React reuses the
// DOM node for a *different* destination, so an in-flight keystroke or an
// open dropdown lands on the wrong row. A monotonic id assigned when a
// destination is loaded or created keeps each card bound to one object for
// the life of the list (review M-series).
let nextStableId = 1;
const STABLE_ID = "_uid";

function withStableIds(ds: Destination[]): Destination[] {
  return ds.map((d) => (d[STABLE_ID] === undefined ? { ...d, [STABLE_ID]: nextStableId++ } : d));
}

/** Drop client-only fields so the payload matches the backend's model. */
function forPayload(ds: Destination[]): Destination[] {
  return ds.map((d) => {
    const { [STABLE_ID]: _omit, ...rest } = d;
    return rest;
  });
}

async function echoProbe(host: string, port: number, aet: string, aetSource: string): Promise<string> {
  // Send the destination's own calling AE title: if the PACS whitelists
  // callers, probing as the default "GATEWAY" fails while real forwarding
  // would succeed — a false negative that sends an operator "fixing" a
  // working config.
  const res = await fetch(apiUrl("/api/echo"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "probe", host, port, aet, aet_source: aetSource }),
    credentials: "include",
  });
  if (!res.ok) {
    // A 401 is not a property of the destination: the operator's session
    // expired (auth is on, the cookie lapsed). Reporting "error" here puts
    // an "unreachable" badge on a PACS that may be perfectly healthy and
    // sends someone chasing a network problem that is really a login
    // prompt. "expired" makes the page say so and offer a re-login.
    return res.status === 401 ? "expired" : "error";
  }
  const json = (await res.json()) as { status: string };
  return json.status;
}

const SECRET_PLACEHOLDER = "••••••••";

/** Identity of the endpoint a probe actually contacted.
 *
 * A badge is only truthful while the destination still matches the target that
 * was probed. Keying the result by host/port/called AET/calling AET means an
 * edit after the probe changes the key and the badge simply stops showing,
 * instead of vouching for an endpoint it never touched (review I4). The calling
 * title is part of the signature because a PACS that whitelists callers can
 * answer differently per source (review I3). */
function probeSignature(d: Destination): string {
  return [String(d.host ?? ""), Number(d.port ?? 0), String(d.aet_target ?? ""), String(d.aet_source ?? "")].join("|");
}

export default function DestinationsView() {
  const [destinations, setDestinations] = useState<Destination[]>([]);
  // Kept so the stale-rule warning can offer the one remedy this page can
  // actually perform — adding the missing destination (review I6).
  const [forwardingRules, setForwardingRules] = useState<unknown[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [echoState, setEchoState] = useState<Record<string, string>>({});
  const [warnings, setWarnings] = useState<ConfigWarning[]>([]);
  // Routing preview (review P0-9): which destinations a study with this
  // Modality would reach under the configured forwarding rules.
  const [previewModality, setPreviewModality] = useState("");
  const [previewResult, setPreviewResult] = useState<RulePreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewBusy, setPreviewBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const cfg = await fetchConfig();
      // The schema's destination list is a discriminated union of eight read
      // types; the form edits one loose mutable shape with a client-only uid.
      // Narrowing through `unknown` keeps fetchConfig's return type honest
      // while the edit shape stays permissive.
      setDestinations(withStableIds((cfg.destinations as unknown as Destination[]) ?? []));
      setForwardingRules(cfg.forwarding_rules ?? []);
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

  // The "Saved" note describes the on-disk config; the moment the operator
  // edits it, that is no longer true, so the note must go rather than sit
  // next to unsaved changes claiming they are applied.
  const markDirty = () => {
    setDirty(true);
    setMsg("");
  };

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
      {
        name: "",
        type: "dicom",
        enabled: true,
        host: "",
        port: 104,
        aet_target: "",
        aet_source: "GATEWAY",
        [STABLE_ID]: nextStableId++,
      },
    ]);
    markDirty();
  };

  const removeDestination = (i: number) => {
    setDestinations((ds) => ds.filter((_, idx) => idx !== i));
    markDirty();
  };

  const handleEcho = async (d: Destination) => {
    const key = probeSignature(d);
    setEchoState((prev) => ({ ...prev, [key]: "probing" }));
    const status = await echoProbe(
      String(d.host),
      Number(d.port),
      String(d.aet_target ?? ""),
      String(d.aet_source ?? ""),
    );
    setEchoState((prev) => ({ ...prev, [key]: status }));
  };

  const handleSave = async () => {
    if (hasNameErrors) return; // a duplicate/empty name cannot be saved
    setSaving(true);
    setMsg("");
    setError("");
    try {
      const current = await fetchConfig();
      // Send only the destinations section; the rest of the config round-trips
      // untouched so this page cannot clobber an unrelated setting. The stable
      // ids are client-only and must not reach the backend — the model would
      // reject the extra field.
      current.destinations = forPayload(destinations) as GatewayConfig["destinations"];
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

  // Names key routing and the credential-restore lookup, so a duplicate
  // collapses two destinations and an empty one 400s on save. Lint reports
  // duplicates after the fact; blocking here means the mistake is not made.
  const nameErrors = destinations.map((d) => {
    const name = String(d.name ?? "").trim();
    if (!name) return "Name is required.";
    // Two or more entries sharing this name — the condition that collapses
    // routing and cross-attaches credentials in the by-name restore.
    return destinations.filter((x) => String(x.name ?? "").trim() === name).length > 1
      ? "Name must be unique."
      : "";
  });
  const hasNameErrors = nameErrors.some(Boolean);

  // The stale-rule lint finding says "add the destination" — and unlike the
  // "rename or remove the target" half of its advice, that is something this
  // page can do. Pointing the operator at a forwarding-rules editor that does
  // not exist sends them to the JSON; offering the fix here closes the loop
  // (review I6).
  const knownNames = useMemo(
    () => new Set(destinations.map((d) => String(d.name ?? "").trim()).filter(Boolean)),
    [destinations],
  );
  const staleTargets = useMemo(() => {
    const stale = new Set<string>();
    for (const rule of forwardingRules) {
      if (typeof rule !== "object" || rule === null) continue;
      const targets = (rule as { targets?: unknown }).targets;
      if (!Array.isArray(targets)) continue;
      for (const t of targets) if (typeof t === "string" && !knownNames.has(t)) stale.add(t);
    }
    return [...stale].sort();
  }, [forwardingRules, knownNames]);

  const addMissingDestination = (name: string) => {
    setDestinations((ds) => [
      ...ds,
      {
        name,
        type: "dicom",
        enabled: true,
        host: "",
        port: 104,
        aet_target: "",
        aet_source: "GATEWAY",
        [STABLE_ID]: nextStableId++,
      },
    ]);
    markDirty();
  };

  // Ask the backend where a study with this Modality would be routed. The
  // endpoint evaluates the configured rules through the same engine enqueue
  // uses, so the answer is what the appliance will actually do. A null return
  // means the request failed — most likely a rule that cannot be parsed, which
  // the lint banner above already reports by index.
  const runPreview = async () => {
    const modality = previewModality.trim();
    if (!modality) return;
    setPreviewBusy(true);
    setPreviewError("");
    setPreviewResult(null);
    try {
      const result = await previewRouting({ Modality: modality });
      if (result === null) {
        setPreviewError(
          "Preview failed — a forwarding rule could not be parsed. See the lint warnings above; the backend reports the offending rule index.",
        );
      } else {
        setPreviewResult(result);
      }
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : String(e));
    } finally {
      setPreviewBusy(false);
    }
  };

  if (!loaded) return <div className="loading">Loading destinations</div>;

  return (
    <div>
      <h2>Destinations</h2>

      {warnings.length > 0 ? (
        <div className="banner warn" role="alert" style={{ marginBottom: 12 }}>
          {warnings.map((w) => (
            <div key={w.path + w.message} style={{ marginBottom: 4 }}>
              {w.severity === "info" ? "Note: " : ""}
              {w.message}
              {w.path.startsWith("forwarding_rules") && staleTargets.length > 0
                ? staleTargets.map((t) => (
                    <button
                      key={t}
                      className="btn"
                      style={{ marginLeft: 8 }}
                      onClick={() => addMissingDestination(t)}
                    >
                      <IconPlus size={12} /> Add destination {t}
                    </button>
                  ))
                : null}
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
          // The badge is keyed by the endpoint that was probed, not by the
          // destination's current value — so editing host/port/AET afterwards
          // changes the key and the stale result stops showing (review I4).
          const echoKey = probeSignature(d);
          const echoStatus = echoState[echoKey];
          // Computed once for the whole list so Save can be gated too.
          const nameError = nameErrors[i];
          // The echo endpoint probes in plaintext: a TLS-only PACS would
          // report "refused" while perfectly healthy, a false negative that
          // sends an operator loosening a correct config. TLS probing is a
          // follow-up; until then the button is offered only where the
          // answer can be trusted.
          const canEcho = d.type === "dicom";
          return (
            <div className="card" key={d[STABLE_ID] ?? i} style={{ marginBottom: 12 }}>
              <div className="dest-header">
                <input
                  className="input"
                  name="name"
                  placeholder="Name (unique)"
                  value={String(d.name ?? "")}
                  onChange={(e) => update(i, "name", e.target.value)}
                  aria-label="Destination name"
                  aria-invalid={nameError ? true : undefined}
                />
                {nameError ? (
                  <span className="field-hint" style={{ color: "var(--red)" }} role="alert">
                    {nameError}
                  </span>
                ) : null}
                <select
                  className="input"
                  value={d.type}
                  onChange={(e) => {
                    // Switching type replaces the field set: rebuild the
                    // destination from the common fields only. Keeping the old
                    // object and just setting `type` left stale type-specific
                    // fields in the payload (an S3 bucket on a dicom
                    // destination) AND omitted the new type's required fields,
                    // so the save 400'd on a field the operator never filled.
                    setDestinations((ds) => {
                      const next = [...ds];
                      next[i] = {
                        name: ds[i].name,
                        type: e.target.value,
                        enabled: ds[i].enabled,
                        // The field set changes but the card's identity does
                        // not: keep the uid so this card is not re-mounted
                        // (which would drop input focus mid-edit).
                        [STABLE_ID]: ds[i][STABLE_ID] ?? nextStableId++,
                      };
                      return next;
                    });
                    markDirty();
                  }}
                  aria-label="Destination type"
                >
                  {DEST_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {TYPE_LABELS[t] ?? t}
                    </option>
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
                      <span
                        className={`badge ${echoStatus === "ok" ? "green" : echoStatus === "expired" ? "amber" : "red"}`}
                        title={
                          echoStatus === "expired"
                            ? "Your admin session expired — log in again, then re-probe. This does not mean the destination is unreachable."
                            : echoStatus
                        }
                      >
                        {echoStatus === "expired" ? "session expired" : echoStatus}
                      </span>
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
                  if (f.type === "boolean") {
                    return (
                      <label key={f.key} className="check">
                        <input
                          type="checkbox"
                          checked={Boolean(value)}
                          onChange={(e) => update(i, f.key, e.target.checked)}
                          aria-label={`${d.name || "destination"} ${f.label}`}
                        />
                        {f.label}
                      </label>
                    );
                  }
                  return (
                    <label key={f.key}>
                      <span className="label">{f.label}</span>
                      <input
                        className="input"
                        type={isSecret ? "password" : f.type ?? "text"}
                        placeholder={isSecret ? SECRET_PLACEHOLDER : f.placeholder}
                        value={display}
                        onChange={(e) => {
                          // Number("") is 0, so clearing a port field would
                          // silently submit 0 and 400 on "greater than 0" while
                          // the operator sees an empty box. An empty numeric
                          // field is omitted from the payload instead, and the
                          // server reports it as required — the accurate error.
                          const next =
                            f.type === "number"
                              ? e.target.value === ""
                                ? undefined
                                : Number(e.target.value)
                              : e.target.value;
                          update(i, f.key, next);
                        }}
                        aria-label={`${d.name || "destination"} ${f.label}`}
                      />
                      {f.hint ? (
                        <span className="field-hint">{f.hint}</span>
                      ) : null}
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
          disabled={!dirty || saving || hasNameErrors}
          title={hasNameErrors ? "Resolve the highlighted name issues first" : undefined}
        >
          {saving ? "Saving..." : dirty ? "Save Changes" : "Saved"}
          {dirty ? null : <IconCheck size={14} />}
        </button>
      </div>

      {/* Preview which destinations a study would reach, against the same rule
          engine enqueue uses (review P0-9). Before the engine was unified this
          would have answered for an engine production never ran; now the two
          cannot drift, and this is the only place an operator can ask the
          question without a real study arriving. */}
      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">Preview routing</div>
        <p style={{ color: "var(--muted)" }}>
          Only the Modality tag is known at enqueue time, so that is the whole
          tag set this preview can consider. A rule on any other tag previews
          correctly over the API but cannot affect a study the router actually
          sees.
        </p>
        <div className="field-row">
          <label>
            <span className="label">Modality</span>
            <input
              className="input"
              placeholder="e.g. CT"
              value={previewModality}
              onChange={(e) => setPreviewModality(e.target.value)}
              aria-label="Modality to preview"
            />
          </label>
          <button
            className="btn"
            onClick={() => void runPreview()}
            disabled={previewBusy || !previewModality.trim()}
          >
            {previewBusy ? "Previewing..." : 'Preview "where would this go?"'}
          </button>
        </div>
        {previewError ? (
          <div className="warn-note" role="alert">
            {previewError}
          </div>
        ) : null}
        {previewResult ? (
          <div className="ok-note">
            {previewResult.matched_any
              ? `A forwarding rule matched — this study routes to: ${previewResult.targets.join(", ") || "(no destinations)"}`
              : `No rule matched this modality — the default route applies: ${previewResult.targets.join(", ") || "(no enabled destinations)"}.`}
          </div>
        ) : null}
      </div>
    </div>
  );
}
