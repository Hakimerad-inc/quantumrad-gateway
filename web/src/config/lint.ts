/**
 * Client-side config lint — catch footguns while the operator types
 * (refinement 2026-09-17).
 *
 * The server is authoritative: ``PUT /api/config`` validates into the pydantic
 * model and rejects a bad document with a 400. This module is not a second
 * validator — it is *immediate feedback*, so an operator hand-editing JSON sees
 * a likely mistake under their cursor instead of after a save, a restart, or
 * (worst case) a boot crash on the frozen binary.
 *
 * The case that motivated it (E1 dry run): an unquoted ``config_version: 1``
 * parses as a number, sails through ``JSON.parse`` with no complaint, and is
 * rejected by the model only at load time — where the frozen binary exits at
 * boot with a ValidationError. The panel's textarea would happily let the
 * operator type it and save it.
 */

export interface LintFinding {
  /** 1-based line number in the edited document, or null when structural. */
  line: number | null;
  message: string;
}

/** Known config sections, used to locate a bad key without a full schema. */
const KNOWN_DEST_TYPES = new Set([
  "dicom",
  "dicom_tls",
  "dicomweb",
  "sftp",
  "rsync",
  "s3",
  "folder",
  "xnat",
]);

/**
 * Lint an edited config document. JSON parse errors are reported with the line
 * the parser choked on; otherwise structural checks run against the object.
 *
 * Returns findings only — the caller decides whether to warn, block, or ignore.
 * An empty array means "nothing to flag", not "config is valid": the server
 * remains the authority.
 */
export function lintConfigDocument(text: string): LintFinding[] {
  const findings: LintFinding[] = [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    // JSON.parse errors are unhelpful ("Unexpected token"); pull the position
    // out where we can and point at the line.
    const message = e instanceof Error ? e.message : String(e);
    const line = lineNumberFromMessage(message, text);
    findings.push({
      line,
      message: `Invalid JSON: ${message}`,
    });
    return findings;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return [{ line: null, message: "Config must be a JSON object at the top level." }];
  }

  const cfg = parsed as Record<string, unknown>;
  findings.push(...lintConfigVersion(cfg, text));
  findings.push(...lintDestinations(cfg, text));
  return findings;
}

function lineNumberFromMessage(message: string, text: string): number | null {
  // V8 reports "Unexpected token ... in JSON at position N" (older) or
  // "...at line L column C" (newer). Accept either; fall back to null.
  const posMatch = /position (\d+)/.exec(message);
  if (posMatch) {
    return offsetToLine(text, Number(posMatch[1]));
  }
  const lineMatch = /line (\d+)/.exec(message);
  return lineMatch ? Number(lineMatch[1]) : null;
}

function offsetToLine(text: string, offset: number): number {
  let line = 1;
  for (let i = 0; i < offset && i < text.length; i++) {
    if (text[i] === "\n") line++;
  }
  return line;
}

/** Locate a top-level key's line so the finding points near the edit. */
function findKeyLine(text: string, key: string, fallback: number | null = null): number | null {
  const re = new RegExp(`^\\s*"${key}"\\s*:`, "m");
  const m = re.exec(text);
  if (!m) return fallback;
  return offsetToLine(text, m.index) || fallback;
}

/**
 * The E1 footgun: ``config_version`` must be the *string* "1.0". A bare number
 * or a wrong version loads through JSON.parse silently and is only rejected by
 * the model — which, on the frozen binary, happens at boot.
 */
function lintConfigVersion(cfg: Record<string, unknown>, text: string): LintFinding[] {
  const value = cfg.config_version;
  if (value === undefined) {
    return [{ line: null, message: "config_version is missing — expected the string \"1.0\"." }];
  }
  if (typeof value !== "string") {
    return [
      {
        line: findKeyLine(text, "config_version", 1),
        message: `config_version must be a string ("1.0"), not ${typeof value} — an unquoted value here crashes the gateway at boot.`,
      },
    ];
  }
  if (value !== "1.0") {
    return [
      {
        line: findKeyLine(text, "config_version", 1),
        message: `config_version is ${JSON.stringify(value)}; this build expects "1.0".`,
      },
    ];
  }
  return [];
}

function lintDestinations(cfg: Record<string, unknown>, text: string): LintFinding[] {
  const findings: LintFinding[] = [];
  const dests = cfg.destinations;
  if (dests === undefined) return findings;
  if (!Array.isArray(dests)) {
    return [
      { line: findKeyLine(text, "destinations"), message: "destinations must be a list of destination objects." },
    ];
  }

  const seen = new Set<string>();
  dests.forEach((d, i) => {
    if (typeof d !== "object" || d === null) {
      findings.push({ line: null, message: `destinations[${i}] must be an object.` });
      return;
    }
    const dest = d as Record<string, unknown>;
    const name = typeof dest.name === "string" ? dest.name : null;

    if (!name || name.trim() === "") {
      findings.push({ line: null, message: `destinations[${i}].name is missing — names must be unique and non-empty.` });
    } else if (seen.has(name)) {
      findings.push({
        line: null,
        message: `destinations[${i}].name "${name}" duplicates an earlier destination; names key routing and must be unique.`,
      });
    } else {
      seen.add(name);
    }

    const dtype = dest.type;
    if (typeof dtype === "string" && !KNOWN_DEST_TYPES.has(dtype)) {
      findings.push({
        line: null,
        message: `destinations[${i}].type "${dtype}" is not a known destination type (${[...KNOWN_DEST_TYPES].join(", ")}).`,
      });
    }
  });
  return findings;
}
