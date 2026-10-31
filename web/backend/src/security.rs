//! Security headers for the SPA surface, including a Content-Security-Policy
//! that is *derived from the embedded index.html* instead of guessed at.
//!
//! Why derived: the one legitimate inline script (the pre-paint lang/theme
//! snippet) would otherwise force `script-src 'unsafe-inline'`, which is CSP
//! theater — an injected inline script looks exactly like that snippet to the
//! browser. Hash-pinning each actual inline script keeps the policy strict AND
//! survives frontend edits: the hash is recomputed from the shipped asset at
//! boot, so a changed snippet updates its own hash instead of breaking the
//! page silently. Operators who need something different set `ATF_WEB_CSP`.
//!
//! `frame-ancestors` deliberately allows Telegram's web clients: the dashboard
//! doubles as a Telegram Mini App, which runs inside an iframe on
//! web.telegram.org. Locking it to `'self'` (or adding `X-Frame-Options:
//! DENY` here) would brick TMA logins.

use sha2::{Digest, Sha256};

/// Headers every static/SPA response carries. Values without a dynamic part.
pub const STATIC_SECURITY_HEADERS: [(&str, &str); 4] = [
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
];

/// Find the next inline `<script>` open tag (no `src` attribute), returning
/// the byte span `start..(index just past the closing '>')`. Scripts with a
/// `src` are external — they hash nothing — so they are skipped and scanning
/// continues past them.
fn find_inline_open(html: &str) -> Option<(usize, usize)> {
    let mut search_from = 0usize;
    while let Some(rel) = html[search_from..].find("<script") {
        let start = search_from + rel;
        let close_gt = html[start..].find('>')? + start;
        let tag = &html[start..=close_gt];
        search_from = close_gt + 1;
        if tag.contains("src=") {
            continue;
        }
        return Some((start, close_gt + 1));
    }
    None
}

/// Extract each inline `<script>…</script>` body from an HTML document.
pub fn inline_scripts(html: &str) -> Vec<String> {
    let mut found = Vec::new();
    let mut rest = html;
    while let Some((_, after_open)) = find_inline_open(rest) {
        let body = match rest[after_open..].find("</script>") {
            Some(close) => &rest[after_open..after_open + close],
            None => break,
        };
        found.push(body.to_string());
        rest = &rest[after_open + body.len() + "</script>".len()..];
    }
    found
}

/// SHA-256 of each inline script, base64'd the way CSP wants (`'sha256-…'`).
pub fn script_hashes(html: &str) -> Vec<String> {
    inline_scripts(html)
        .into_iter()
        .map(|body| format!("'sha256-{}'", b64(Sha256::digest(body.as_bytes()))))
        .collect()
}

/// Build the default policy for the embedded app. `frame-ancestors` keeps TMA
/// working; everything else is locked to same-origin.
pub fn default_csp(index_html: &str) -> String {
    let hashes = script_hashes(index_html);
    let script_sources: String = if hashes.is_empty() {
        "'self'".to_string()
    } else {
        format!("'self' {}", hashes.join(" "))
    };
    format!(
        "default-src 'self'; \
         script-src {script_sources}; \
         style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; \
         font-src 'self' https://fonts.gstatic.com data:; \
         img-src 'self' data: blob:; \
         connect-src 'self'; \
         object-src 'none'; \
         base-uri 'self'; \
         form-action 'self'; \
         frame-ancestors 'self' https://web.telegram.org https://telegram.org"
    )
}

/// Standard-alphabet base64 with padding (RFC 4648), which is the form RFC
/// 7469 CSP digests use. Local to avoid pulling a codec crate for 20 lines.
fn b64(bytes: impl AsRef<[u8]>) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let bytes = bytes.as_ref();
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(triple >> 18) as usize & 63] as char);
        out.push(ALPHABET[(triple >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { ALPHABET[(triple >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[triple as usize & 63] as char } else { '=' });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inline_script_detection_skips_src_scripts() {
        let html = "<html><script src=\"/assets/app.js\"></script>\
                    <script>var a = 1;</script></html>";
        let found = inline_scripts(html);
        assert_eq!(found, vec!["var a = 1;"]);
    }

    #[test]
    fn b64_matches_rfc4648_vectors() {
        assert_eq!(b64(""), "");
        assert_eq!(b64("f"), "Zg==");
        assert_eq!(b64("fo"), "Zm8=");
        assert_eq!(b64("foo"), "Zm9v");
        assert_eq!(b64("foob"), "Zm9vYg==");
        assert_eq!(b64("fooba"), "Zm9vYmE=");
        assert_eq!(b64("foobar"), "Zm9vYmFy");
    }

    #[test]
    fn hash_is_deterministic_sha256() {
        let a = script_hashes("<script>var a = 1;</script>");
        let b = script_hashes("<script>var a = 1;</script>");
        assert_eq!(a, b);
        // sha256 → 32 bytes → 44 base64 chars (RFC 4648 padding included),
        // wrapped in 'sha256-…' quotes per RFC 7469.
        let inner = a[0].strip_prefix("'sha256-").and_then(|s| s.strip_suffix('\''));
        assert_eq!(inner.map(str::len), Some(44));
    }

    #[test]
    fn csp_pins_the_real_script_hash() {
        let html = "<html><head><script>\n try { var x = 1 } catch (e) {}\n</script></head></html>";
        let csp = default_csp(html);
        assert!(csp.contains("'sha256-"), "policy must pin a hash");
        assert!(!csp.contains("script-src 'self' 'unsafe-inline'"), "no blanket inline allowance");
        assert!(csp.contains("frame-ancestors 'self' https://web.telegram.org https://telegram.org"));
    }

    #[test]
    fn csp_without_inline_scripts_is_self_only() {
        let csp = default_csp("<html><script src=\"app.js\"></script></html>");
        assert!(csp.contains("script-src 'self';"));
    }
}
