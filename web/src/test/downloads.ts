/**
 * Blob-download stub for jsdom.
 *
 * The export buttons fetch a JSON body and hand it to a synthesized anchor via
 * URL.createObjectURL. jsdom (24.x) does not implement createObjectURL/
 * revokeObjectURL, so a component test that triggers an export needs them
 * defined. Install with `stubDownloads()` in a beforeEach and clear with
 * `vi.unstubAllGlobals()` in the afterEach.
 */
import { vi } from "vitest";

export function stubDownloads(): { createObjectURL: ReturnType<typeof vi.fn> } {
  const createObjectURL = vi.fn(() => "blob:fake-url");
  const revokeObjectURL = vi.fn();
  // Attach to the real URL rather than replacing the global: the code under
  // test reaches for URL.createObjectURL only, and a wholesale swap risks a
  // test that passes against a stub whose shape the browser would reject.
  Object.defineProperty(URL, "createObjectURL", {
    value: createObjectURL,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    value: revokeObjectURL,
    configurable: true,
    writable: true,
  });
  return { createObjectURL };
}

// vi.unstubAllGlobals does not remove properties defineProperty'd onto an
// existing object, so the pair is cleared explicitly.
export function clearDownloads(): void {
  const u = URL as unknown as Record<string, unknown>;
  delete u.createObjectURL;
  delete u.revokeObjectURL;
}
