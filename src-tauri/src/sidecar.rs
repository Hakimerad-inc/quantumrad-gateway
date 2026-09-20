//! Pure parsers used to provenance-check the frozen Python sidecar.
//!
//! These live outside ``build.rs`` for one reason: a build script is compiled
//! as a build script, and ``cargo test`` never runs its ``#[cfg(test)]``
//! module. Keeping the pure functions here means the parsers have executed
//! coverage under the ordinary ``cargo test`` that a developer runs, while
//! ``build.rs`` pulls in the identical code via ``#[path]`` — no second
//! implementation to drift against the one the guard actually uses.
//!
//! The version extractor mirrors the Python regex
//! ``^__version__\s*=\s*"([^"]+)"`` (multiline) in scripts/package_backend.py;
//! see tests/test_package_backend.py for the cross-implementation guard.
//!
//! ``#[allow(dead_code)]`` because the sole production consumer is
//! ``build.rs``, which inlines this file with ``#[path]``; from the library
//! crate's point of view the functions are unused, but they are not — and
//! the tests below are the reason the module lives here at all.

#![allow(dead_code)]

/// Extract ``__version__ = "..."`` from a package init.
pub fn extract_version(text: &str) -> Option<String> {
    // NB: a `?` on the prefix here would return from *this function*, not
    // advance the loop — a file whose first line is a comment would report
    // "no version" even with a valid one further down. The canonical
    // src/mercure_gateway/__init__.py opens with two comment lines, so that
    // bug failed every build of a *correctly* frozen bundle. The guard must
    // keep scanning until it finds the line or runs out.
    for line in text.lines() {
        let Some(rest) = line.trim_start().strip_prefix("__version__") else {
            continue;
        };
        // \s*= between the name and the value.
        let Some(value) = rest.trim_start().strip_prefix('=') else {
            continue;
        };
        // \s* before the opening quote.
        let Some(value) = value.trim_start().strip_prefix('"') else {
            continue;
        };
        let Some(end) = value.find('"') else {
            continue;
        };
        return Some(value[..end].to_string());
    }
    None
}

/// Pull ``frozen_version`` out of a PROVENANCE.json record.
///
/// Written by this repo's own ``write_provenance``, so the key is stable; a
/// targeted scan avoids making ``serde_json`` a build-dependency (a second
/// compile of it, on every ``cargo check``, for one field in a small file).
pub fn parse_provenance_frozen_version(text: &str) -> Option<String> {
    let key = "\"frozen_version\"";
    let start = text.find(key)?;
    let after_key = &text[start + key.len()..];
    let colon = after_key.find(':')?;
    let value = after_key[colon + 1..].trim_start();
    let value = value.strip_prefix('"')?;
    let end = value.find('"')?;
    Some(value[..end].to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_canonical_version_line() {
        assert_eq!(
            extract_version("__version__ = \"1.1.0-rc3\"\n"),
            Some("1.1.0-rc3".into())
        );
    }

    #[test]
    fn tolerates_indentation_and_odd_spacing() {
        assert_eq!(
            extract_version("  __version__  =  \"9.9.9\""),
            Some("9.9.9".into())
        );
    }

    #[test]
    fn a_missing_version_is_none_not_a_panic() {
        assert!(extract_version("# no version here\n").is_none());
        assert!(extract_version("").is_none());
    }

    #[test]
    fn scans_past_leading_comments_to_find_the_version() {
        // The canonical __init__.py opens with doc comment lines; a `?` in
        // the scan loop would surrender at the first one and report no
        // version, failing every build of a correctly-frozen bundle.
        let text = "# Single source of truth for the gateway version.\n# Keep in sync with pyproject.toml.\n__version__ = \"1.1.0-rc3\"\n";
        assert_eq!(extract_version(text), Some("1.1.0-rc3".into()));
    }

    #[test]
    fn ignores_similarly_named_fields() {
        // `__version_info__` must not satisfy a prefix match.
        assert!(extract_version("__version_info__ = (1, 1, 0)\n").is_none());
    }

    #[test]
    fn reads_frozen_version_from_a_provenance_record() {
        let record =
            "{\n  \"frozen_version\": \"1.1.0-rc3\",\n  \"source_version\": \"1.1.0-rc3\"\n}\n";
        assert_eq!(
            parse_provenance_frozen_version(record),
            Some("1.1.0-rc3".into())
        );
    }

    #[test]
    fn provenance_without_a_frozen_version_is_none() {
        assert!(parse_provenance_frozen_version("{\"source_version\": \"1.1.0\"}").is_none());
        assert!(parse_provenance_frozen_version("").is_none());
    }

    #[test]
    fn a_mismatched_sidecar_is_reported_not_shipped() {
        // The rc1 scenario: bundle frozen from an older tree.
        assert_ne!(
            extract_version("__version__ = \"1.1.0-rc1\"\n"),
            extract_version("__version__ = \"1.1.0-rc3\"\n")
        );
    }
}
