//! Tauri build script.
//!
//! Beyond the standard `tauri_build::build()`, this is where the frozen
//! backend sidecar is provenance-checked (review P0-2).
//!
//! **Why here, and not only in `scripts/package_backend.py`:** the packaging
//! doc runs the pipeline as two separate steps — freeze with
//! `scripts/package_backend.py`, then `cd src-tauri && cargo tauri build`.
//! The script's `assert_frozen_version_matches_source` only fires while *it*
//! is the thing doing the freezing. A developer who still carries a sidecar
//! frozen from an older tree (the committed one was 1.1.0-rc1 and predated
//! `_enforce_bind_security`, so an installer from it would have booted an
//! unauthenticated admin panel on the LAN) and goes straight to
//! `cargo tauri build` bundles that stale backend with no check at all.
//! The build script runs on every one of those paths, so the check lives here.

use std::path::{Path, PathBuf};

#[path = "src/sidecar.rs"]
mod sidecar;

/// The frozen bundle's own package init — where a stale snapshot's version
/// betrays it. Mirrors `FROZEN_INIT` in scripts/package_backend.py.
const FROZEN_INIT_REL: &str = "binaries/mercure-gateway/_internal/mercure_gateway/__init__.py";

/// The canonical source version, the same file `_source_version()` regexes.
const SOURCE_INIT_REL: &str = "../src/mercure_gateway/__init__.py";

/// Written beside the bundle by `write_provenance()`.
const PROVENANCE_REL: &str = "binaries/mercure-gateway/PROVENANCE.json";

fn main() {
    let manifest = manifest_dir();
    // Re-run when either side of the comparison changes; without this Cargo
    // caches build.rs's output and a newly frozen sidecar would be bundled
    // unchecked until something else invalidates the script.
    rerun_if_changed(&manifest, FROZEN_INIT_REL);
    rerun_if_changed(&manifest, PROVENANCE_REL);
    rerun_if_changed(&manifest, SOURCE_INIT_REL);

    if let Err(message) = check_sidecar(&manifest) {
        // panic=abort in [profile.release] still fails the build and prints
        // this — the message is the actionable part, not the exit code.
        panic!("{message}");
    }

    tauri_build::build()
}

fn manifest_dir() -> PathBuf {
    PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by Cargo"))
}

fn rerun_if_changed(manifest: &Path, rel: &str) {
    let path = manifest.join(rel);
    println!("cargo:rerun-if-changed={}", path.display());
}

/// Compare the frozen sidecar against the source tree it is being bundled
/// from. Returns `Err` with an operator-actionable message on any mismatch.
///
/// Absence is *not* an error: `scripts/tauri_placeholders.py` leaves a
/// zero-byte marker in `binaries/mercure-gateway/` so `cargo check`,
/// `clippy` and `cargo test` have well-defined bundle inputs, and those run
/// with nothing frozen. The guard fires only when a real frozen backend is
/// present — which is the only state it can be wrong about.
fn check_sidecar(manifest: &Path) -> Result<(), String> {
    let frozen_init = manifest.join(FROZEN_INIT_REL);
    if !frozen_init.exists() {
        return Ok(());
    }

    let source = read_version(&manifest.join(SOURCE_INIT_REL))
        .ok_or_else(|| format!("cannot parse __version__ from {}", SOURCE_INIT_REL))?;
    let frozen = read_version(&frozen_init).ok_or_else(|| {
        format!(
            "the frozen sidecar exists but {} reports no version — delete \
             src-tauri/binaries/mercure-gateway or rebuild it with \
             `just refresh-sidecar`",
            FROZEN_INIT_REL
        )
    })?;

    if frozen != source {
        return Err(format!(
            "the frozen sidecar reports version {frozen:?} but the source is \
             {source:?}. The bundled backend is stale: rebuild it with \
             `just refresh-sidecar` (or scripts/package_backend.py) from this \
             tree. Shipping it would silently run an older backend."
        ));
    }

    check_provenance(manifest, &frozen)
}

/// `PROVENANCE.json` must exist beside a bundled backend and must agree with
/// the version the bundle actually carries (review P0-2).
///
/// The record is what an operator reads on a support call to see what is
/// running; a bundle shipped without one, or with one that disagrees, defeats
/// the only external view into a frozen snapshot — which is otherwise
/// indistinguishable from a current build: it boots and answers health checks.
fn check_provenance(manifest: &Path, frozen: &str) -> Result<(), String> {
    let path = manifest.join(PROVENANCE_REL);
    let text = std::fs::read_to_string(&path).map_err(|_| {
        format!(
            "a frozen sidecar is bundled but {} is missing — rebuild with \
             `just refresh-sidecar`, which writes it beside the bundle",
            PROVENANCE_REL
        )
    })?;
    match sidecar::parse_provenance_frozen_version(&text) {
        Some(recorded) if recorded == frozen => Ok(()),
        Some(recorded) => Err(format!(
            "{} records frozen_version {recorded:?} but the bundled backend \
             reports {frozen:?} — rebuild with `just refresh-sidecar`",
            PROVENANCE_REL
        )),
        None => Err(format!(
            "{} is present but records no frozen_version — rebuild with \
             `just refresh-sidecar`",
            PROVENANCE_REL
        )),
    }
}

/// Extract `__version__ = "..."` from a package init.
fn read_version(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    sidecar::extract_version(&text)
}

#[cfg(test)]
mod tests {
    // The parsers are unit-tested in src/sidecar.rs. They used to live here,
    // but `cargo test` compiles a build script as a build script and never
    // runs its #[cfg(test)] module — so eight tests sat green and executed
    // nowhere, which is not a safety net. Moving them into the crate makes
    // `cargo test` cover them, and build.rs includes the identical file via
    // the `#[path]` above.
}
