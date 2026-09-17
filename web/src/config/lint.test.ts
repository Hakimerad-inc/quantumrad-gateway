/**
 * Config lint tests — immediate feedback for JSON footguns (refinement 2026-09-17).
 *
 * The motivating case (E1 dry run): `config_version: 1` (unquoted) parses as a
 * number with no complaint, and is only rejected by the pydantic model — which
 * on the frozen binary happens at *boot*. The linter must catch it at the
 * keystroke, and must not pretend to validate (the server is authoritative).
 */
import { describe, it, expect } from 'vitest';
import { lintConfigDocument } from './lint';

function versioned(extra: string): string {
  return `{\n  "config_version": "1.0",\n${extra}\n}`;
}

describe('lintConfigDocument', () => {
  it('reports no findings for a clean document', () => {
    const findings = lintConfigDocument(versioned('  "general": { "ae_title": "GATEWAY" }\n'));
    expect(findings).toEqual([]);
  });

  it('catches an unquoted config_version — the E1 boot-crash footgun', () => {
    const text = `{\n  "config_version": 1,\n  "general": {}\n}`;
    const findings = lintConfigDocument(text);
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/must be a string/);
    expect(findings[0].message).toMatch(/crashes the gateway at boot/);
    // Points at the offending line, not at null.
    expect(findings[0].line).toBe(2);
  });

  it('flags a config_version other than 1.0', () => {
    // A complete, parseable document — the version rule only runs once JSON
    // parses, so the body needs a real following key (versioned('') leaves a
    // trailing comma and would be rejected as invalid JSON first).
    const text = `{\n  "config_version": "2.0",\n  "general": {}\n}`;
    const findings = lintConfigDocument(text);
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/expects "1.0"/);
  });

  it('flags a missing config_version', () => {
    const findings = lintConfigDocument('{\n  "general": {}\n}');
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/config_version is missing/);
  });

  it('reports the line a JSON parse error occurred on', () => {
    // Trailing comma — invalid JSON; the parser reports a position we convert.
    const text = '{\n  "config_version": "1.0",\n  "general": {},\n}\n';
    const findings = lintConfigDocument(text);
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/Invalid JSON/);
    expect(findings[0].line).not.toBeNull();
  });

  it('flags a duplicate destination name', () => {
    const findings = lintConfigDocument(
      versioned(
        '  "destinations": [\n' +
          '    { "name": "pacs", "type": "dicom", "host": "a", "port": 104, "aet_target": "A" },\n' +
          '    { "name": "pacs", "type": "dicom", "host": "b", "port": 104, "aet_target": "B" }\n' +
          '  ]\n',
      ),
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/duplicates an earlier destination/);
  });

  it('flags an unknown destination type', () => {
    const findings = lintConfigDocument(
      versioned(
        '  "destinations": [\n' +
          '    { "name": "pacs", "type": "magic", "host": "a", "port": 104 }\n' +
          '  ]\n',
      ),
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/not a known destination type/);
  });

  it('does not flag a document the client cannot judge — server stays authoritative', () => {
    // A syntactically clean doc with a bogus *value* (port out of range) is the
    // server's call; the linter must stay quiet rather than duplicate validation.
    const findings = lintConfigDocument(
      versioned(
        '  "destinations": [\n' +
          '    { "name": "pacs", "type": "dicom", "host": "a", "port": 99999, "aet_target": "A" }\n' +
          '  ]\n',
      ),
    );
    expect(findings).toEqual([]);
  });

  it('rejects a non-object top level', () => {
    const findings = lintConfigDocument('[1, 2, 3]');
    expect(findings).toHaveLength(1);
    expect(findings[0].message).toMatch(/JSON object at the top level/);
  });
});
