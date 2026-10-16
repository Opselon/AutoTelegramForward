//! Ingest-time secret redaction — a field-aware port of `logger/redaction.py`.
//!
//! Defense-in-depth (mission §6.5): producers already redact when they build an
//! audit event; the Logger applies its own pass at ingest so a record from *any*
//! producer (including a raw `logging` mirror or a buggy client) can never
//! persist a bot token, API hash, session string, password, phone code or bearer
//! header.
//!
//! Layers, applied in this order:
//!
//! 1. [`redact_json_fields`] — parses a JSON `detail` blob and replaces the whole
//!    value of any secret-looking key, recursively and depth-bounded. A container
//!    value (`{"password": {"nested": 1}}`) is redacted too, which the regex-only
//!    Python pass cannot see. This layer is a strict superset of the text pass:
//!    it never turns a redacted value back into a readable one.
//! 2. [`redact`] — the byte-for-byte port of the Python function: bearer headers,
//!    quoted and unquoted `key=value` pairs, bot-token shapes, Telegram session
//!    blobs, long hex (sha256) blobs and E.164-ish phone numbers.
//!
//! Layer 2 is what `tests/redaction_parity.json` pins: that fixture was generated
//! by running `logger/redaction.py` itself (see `tests/redaction_fixture.rs`), so
//! "parity" is measured against the Python implementation rather than asserted.
//!
//! Everything here is bounded: the caller truncates oversized input on a char
//! boundary before calling in (see [`crate::ingest`]), and the JSON walk carries a
//! depth limit, so a hostile 10 MiB blob cannot make this module allocate without
//! bound.

use regex::{Captures, NoExpand, Regex};
use serde_json::{Map, Value};
use std::sync::OnceLock;

/// What a secret is replaced with. Identical to the Python module's value.
pub const REPLACEMENT: &str = "[REDACTED]";
/// Suffix used when only the first four characters of a phone number survive.
pub const PHONE_SUFFIX: &str = "[PHONE]";
/// Maximum JSON nesting depth the field-aware pass will descend into.
pub const MAX_DETAIL_DEPTH: usize = 12;

/// Key-name alternation, copied verbatim from `logger/redaction.py`.
const SECRET_KEYS: &str = r"bot_?token|api_?hash|api_?id|session_?string|auth_?key|\
passwo?rd|passwd|phone_?code|login_?code|2fa|two_?factor|secret|\
access_?token|refresh_?token|bearer|authorization|api_?key|\
cookie|x-?api-?key|x-?auth-?token";

fn bearer() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        // "Bearer eyJ...", "token eyJ..." — any casing, 8+ credential chars.
        Regex::new(r"(?i)\b(bearer|token)\s+([A-Za-z0-9_\-\.=]{8,})").expect("static bearer regex")
    })
}

fn kv_quoted() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        // "\"password\": \"hunter2\"", "'api_key': \"sk-abc...\""
        Regex::new(&format!(
            r#"(?i)(["']?\b(?:{SECRET_KEYS})\b["']?\s*[=:]\s*["'])[^"']*?(["'])"#
        ))
        .expect("static quoted-kv regex")
    })
}

fn kv_unquoted() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        // token=abc123, api_key: sk-xyz
        Regex::new(&format!(r#"(?i)\b((?:{SECRET_KEYS})\s*[=:]\s*)([^\s;,&"']+)"#))
            .expect("static unquoted-kv regex")
    })
}

fn bot_token() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"\b(\d{8,12}:[A-Za-z0-9_\-]{30,})\b").expect("static bot-token regex")
    })
}

fn session_blob() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"\b([A-Za-z0-9+/]{80,}={0,2})\b").expect("static session-blob regex")
    })
}

fn long_hex() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)\b[0-9a-f]{64,}\b").expect("static long-hex regex"))
}

/// Candidate phone numbers. The Python pattern uses lookbehind/lookahead, which
/// the `regex` crate does not support; the boundaries are therefore re-implemented
/// in [`redact_phones`] as explicit ASCII-digit checks, which is what `[0-9]`
/// inside the Python lookarounds means.
fn phone_candidate() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"\+?\d{10,15}").expect("static phone regex"))
}

/// Key-name matcher for the field-aware layer: a *whole* key must be secret.
fn secret_key() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(&format!(r"(?i)^(?:{SECRET_KEYS})$")).expect("static secret-key regex"))
}

/// Header-shaped credentials, scheme included.
///
/// This is the one rule the engine's ingest path adds on top of the Python port,
/// because the port has a real hole here: `_SECRET_KV` eats the *scheme* word of
/// `Authorization: basic <b64>` and leaves the base64 credential behind (see the
/// `basic_auth` case in `tests/fixtures/redaction_parity.json`, where
/// `python_redacts` is false). The replacement drops the scheme entirely, so the
/// result is idempotent and the scheme pass cannot leave a fragment for the next
/// pass to expose.
fn auth_header() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(concat!(
            r"(?i)\b((?:proxy-)?authorization|x-auth-token|x-api-key|token|bearer)\b",
            r"\s*([=:]\s*)(?:(?:basic|digest|bearer|token)\s+)?([A-Za-z0-9+/=_\-\.]{4,})",
        ))
        .expect("static auth-header regex")
    })
}

/// Touch every lazily-compiled pattern so a broken pattern fails at startup
/// (loudly) rather than on the first hostage-ish log line.
pub fn warm() {
    let _ = (
        bearer(),
        kv_quoted(),
        kv_unquoted(),
        bot_token(),
        session_blob(),
        long_hex(),
        phone_candidate(),
        secret_key(),
        auth_header(),
    );
}

/// True when `key` names a credential. `apiKey`/`api_key` and `botToken`/
/// `bot_token` spellings agree.
pub fn is_secret_key(key: &str) -> bool {
    let trimmed = key.trim();
    if secret_key().is_match(trimmed) {
        return true;
    }
    let mut snake = String::with_capacity(trimmed.len() + 4);
    for (i, ch) in trimmed.chars().enumerate() {
        if ch.is_ascii_uppercase() {
            if i != 0 {
                snake.push('_');
            }
            snake.push(ch.to_ascii_lowercase());
        } else if ch == '-' || ch == ' ' {
            snake.push('_');
        } else {
            snake.push(ch);
        }
    }
    secret_key().is_match(&snake)
}

/// Return `text` with secret-bearing substrings replaced.
///
/// Pass order matches `logger/redaction.py` exactly (bearer first, otherwise the
/// key/value pass would eat the `Bearer` keyword and leave the credential).
pub fn redact(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let mut out = bearer()
        .replace_all(text, |c: &Captures<'_>| format!("{} {REPLACEMENT}", &c[1]))
        .into_owned();
    out = kv_quoted()
        .replace_all(
            &out,
            |c: &Captures<'_>| format!("{}{REPLACEMENT}{}", &c[1], &c[2]),
        )
        .into_owned();
    out = kv_unquoted()
        .replace_all(&out, |c: &Captures<'_>| format!("{}{REPLACEMENT}", &c[1]))
        .into_owned();
    out = bot_token()
        .replace_all(&out, NoExpand(REPLACEMENT))
        .into_owned();
    out = session_blob()
        .replace_all(&out, NoExpand(REPLACEMENT))
        .into_owned();
    out = long_hex()
        .replace_all(&out, NoExpand(REPLACEMENT))
        .into_owned();
    redact_phones(&out)
}

/// Phone pass: keep the first four characters of a 10–15 digit number and label
/// the rest. Numbers of 16+ digits are deliberately left alone, matching the
/// Python engine's backtracking behaviour (its lookahead fails for every
/// substring of a longer run).
fn redact_phones(text: &str) -> String {
    if !text.contains(|c: char| c.is_ascii_digit()) {
        return text.to_string();
    }
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0usize;
    let mut changed = false;
    for m in phone_candidate().find_iter(text) {
        let (start, end) = (m.start(), m.end());
        let prev_is_digit = start > 0 && bytes[start - 1].is_ascii_digit();
        let next_is_digit = end < bytes.len() && bytes[end].is_ascii_digit();
        if prev_is_digit || next_is_digit {
            continue;
        }
        out.push_str(&text[cursor..start]);
        out.extend(m.as_str().chars().take(4));
        out.push_str(PHONE_SUFFIX);
        cursor = end;
        changed = true;
    }
    if !changed {
        return text.to_string();
    }
    out.push_str(&text[cursor..]);
    out
}

/// The ingest path's redaction: the ported Python pass, hardened by the
/// auth-header rule first.
///
/// `redact` stays byte-for-byte compatible with `logger/redaction.py` (and is the
/// function the parity fixture pins); `redact_strong` is a strict superset and is
/// what the engine persists, so closing a hole there can never make a stored
/// record weaker than a Python one.
pub fn redact_strong(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let pre = auth_header()
        .replace_all(text, |c: &Captures<'_>| {
            format!("{}{}{REPLACEMENT}", &c[1], &c[2])
        })
        .into_owned();
    redact(&pre)
}

/// Outcome of a detail-redaction pass.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RedactedDetail {
    /// The redacted text.
    pub text: String,
    /// True when the input was longer than the byte budget and was cut.
    pub truncated: bool,
    /// True when the input parsed as JSON and the field-aware pass ran.
    pub structured: bool,
}

/// Redact a `detail` blob: field-aware when it is valid JSON, text-only otherwise.
///
/// `max_bytes` bounds the work (input is cut on a char boundary first), and the
/// returned text may be slightly longer than the input because `[REDACTED]` can
/// be longer than the secret it replaces.
pub fn redact_detail(detail: &str, max_bytes: usize) -> RedactedDetail {
    let (bounded, truncated) = truncate_chars(detail, max_bytes);
    if bounded.is_empty() {
        return RedactedDetail {
            text: String::new(),
            truncated,
            structured: false,
        };
    }
    let mut structured = false;
    let mut body = bounded.to_string();
    // Only attempt JSON when it looks like it: this keeps a plain sentence from
    // being parsed (and re-serialised) on every ingest.
    let looks_structured = {
        let t = bounded.trim_start();
        t.starts_with('{') || t.starts_with('[')
    };
    if looks_structured {
        if let Ok(value) = serde_json::from_str::<Value>(bounded) {
            let redacted = redact_value(&value, 0);
            if let Ok(text) = serde_json::to_string(&redacted) {
                // `preserve_order` keeps the producer's key order, so a rewritten
                // blob stays diff-comparable with the original.
                body = text;
                structured = true;
            }
        }
    }
    let mut text = redact_strong(&body);
    // The text pass cannot grow a string beyond a small multiple of the input, but
    // keep the promise of a bound anyway.
    let ceiling = max_bytes.saturating_mul(2).max(64);
    if text.len() > ceiling {
        let (cut, _) = truncate_chars(&text, ceiling);
        text = cut.to_string();
    }
    RedactedDetail {
        text,
        truncated,
        structured,
    }
}

/// Recursively redact a JSON value (copying, so callers keep their input).
pub fn redact_value(value: &Value, depth: usize) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                if is_secret_key(k) {
                    out.insert(k.clone(), Value::String(REPLACEMENT.to_string()));
                } else if depth >= MAX_DETAIL_DEPTH {
                    // Beyond the depth budget: keep the shape, scrub the text with
                    // the regex pass so depth is never a hiding place.
                    out.insert(k.clone(), Value::String(redact_strong(&render(v))));
                } else {
                    out.insert(k.clone(), redact_value(v, depth + 1));
                }
            }
            Value::Object(out)
        }
        Value::Array(items) => {
            if depth >= MAX_DETAIL_DEPTH {
                Value::String(redact(&render(value)))
            } else {
                Value::Array(items.iter().map(|v| redact_value(v, depth + 1)).collect())
            }
        }
        other => other.clone(),
    }
}

fn render(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| String::from("null"))
}

/// Truncate to `max_bytes` on a char boundary. Returns the slice and whether it
/// was cut. A `max_bytes` of 0 means "no limit".
pub fn truncate_chars(text: &str, max_bytes: usize) -> (&str, bool) {
    if max_bytes == 0 || text.len() <= max_bytes {
        return (text, false);
    }
    let mut end = max_bytes;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    (&text[..end], true)
}

/// Redact an identifier column (`rule_id`, `correlation_id`) when the operator
/// asked for it. `user_id` is numeric and never redacted.
pub fn redact_identifier(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    redact(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_patterns_compile() {
        warm();
    }

    #[test]
    fn empty_and_plain_text_are_untouched() {
        assert_eq!(redact(""), "");
        assert_eq!(redact("forwarded 42 messages"), "forwarded 42 messages");
    }

    #[test]
    fn key_value_pairs_are_redacted() {
        assert_eq!(redact("password=hunter2"), "password=[REDACTED]");
        assert_eq!(redact("password: hunter2"), "password=[REDACTED]");
        assert_eq!(
            redact(r#"{"password": "hunter2"}"#),
            r#"{"password": "[REDACTED]"}"#
        );
        assert_eq!(redact("api_key: sk-live-abc"), "api_key: [REDACTED]");
        assert_eq!(redact("passwd: p@ss, ok=1"), "passwd: [REDACTED], ok=1");
        assert_eq!(
            redact("x-api-key=abc123; cookie=sess"),
            "x-api-key=[REDACTED]; cookie=[REDACTED]"
        );
    }

    #[test]
    fn bearer_tokens_are_redacted_and_scoped() {
        // The `Bearer` keyword survives so the operator can see what the secret was.
        assert_eq!(
            redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload"),
            "Authorization: Bearer [REDACTED]"
        );
        // Too short to be a credential: left alone (same as Python).
        assert_eq!(redact("Bearer short"), "Bearer short");
    }

    #[test]
    fn bot_tokens_sessions_and_hashes_are_redacted() {
        let token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw";
        assert_eq!(redact(&format!("bot {token}")), "bot [REDACTED]");
        let session = "A".repeat(100);
        assert_eq!(redact(&format!("session {session}")), "session [REDACTED]");
        let digest = "a".repeat(64);
        assert_eq!(redact(&format!("sha {digest}")), "sha [REDACTED]");
        // Short hex survives; a 12-digit run is phone-shaped, which is what the
        // Python engine does too (pinned by tests/fixtures/redaction_parity.json).
        assert_eq!(redact("chat -100123456789"), "chat -1001[PHONE]");
    }

    #[test]
    fn ingest_path_closes_the_auth_scheme_hole() {
        // `redact` is byte-exact with Python, which eats the scheme word of a
        // Basic header and leaves the credential; the ingest path must not.
        let credential = "dXNlcjpwYXNzd29yZA==";
        let input = format!("Authorization: basic {credential}");
        assert!(redact(&input).contains(credential), "fixture expectations rely on this");
        assert_eq!(redact_strong(&input), "Authorization: [REDACTED]");
        assert!(!redact_detail(&input, 4096).text.contains(credential));
        // …and the same rule catches a bare `token=` pair, which the Python key
        // list does not include.
        let jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload";
        let token_line = format!("token={jwt}");
        assert!(redact(&token_line).contains("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"));
        assert_eq!(redact_strong(&token_line), "token=[REDACTED]");
    }

    #[test]
    fn phones_keep_four_characters() {
        assert_eq!(redact("call +989123456789 now"), "call +989[PHONE] now");
        assert_eq!(redact("n 0912345678"), "n 0912[PHONE]");
        // 16 digits is not a phone number; the Python lookaround refuses it too.
        assert_eq!(redact("x1234567890123456"), "x1234567890123456");
    }

    #[test]
    fn unicode_input_is_not_split() {
        let input = "پیام: پاسورد=secret éèê 日本語";
        let out = redact(input);
        assert!(out.contains("éèê"));
        assert!(out.contains("日本語"));
        assert!(!out.contains("secret"));
    }

    #[test]
    fn truncation_is_char_boundary_safe() {
        let text = "é".repeat(10); // 20 bytes
        let (cut, truncated) = truncate_chars(&text, 5);
        assert!(truncated);
        assert_eq!(cut.chars().count(), 2);
        let (full, truncated) = truncate_chars(&text, 0);
        assert!(!truncated);
        assert_eq!(full.len(), 20);
    }

    #[test]
    fn detail_redaction_is_field_aware() {
        let detail =
            r#"{"rule_id":"r-1","password":{"nested":"deep"},"note":"Bearer abcdefghijklmnop"}"#;
        let out = redact_detail(detail, 4096);
        assert!(
            out.structured,
            "valid JSON detail must take the field-aware path"
        );
        assert!(
            !out.text.contains("deep"),
            "container secret leaked: {}",
            out.text
        );
        assert!(!out.text.contains("abcdefghijklmnop"));
        // Key order is preserved (serde_json/preserve_order).
        assert!(out.text.starts_with(r#"{"rule_id":"r-1","password":"#));
    }

    #[test]
    fn detail_redaction_falls_back_for_malformed_json() {
        let out = redact_detail(r#"{"password": "hunter2", broken"#, 4096);
        assert!(!out.structured);
        assert!(!out.text.contains("hunter2"));
    }

    #[test]
    fn detail_redaction_bounds_oversized_input() {
        let big = format!(r#"{{"blob":"{}"}}"#, "x".repeat(10_000));
        let out = redact_detail(&big, 256);
        assert!(out.truncated);
        assert!(out.text.len() <= 1024, "len={}", out.text.len());
    }

    #[test]
    fn secret_keys_are_recognised_in_both_spellings() {
        for key in [
            "bot_token",
            "botToken",
            "api_hash",
            "apiHash",
            "session_string",
            "password",
            "passwd",
            "phone_code",
            "two_factor",
            "access_token",
            "refresh_token",
            "authorization",
            "cookie",
            "x-api-key",
            "X-Auth-Token",
        ] {
            assert!(is_secret_key(key), "{key} should be secret");
        }
        for key in ["rule_id", "chat_id", "message", "duration_ms", "user_id"] {
            assert!(!is_secret_key(key), "{key} should not be secret");
        }
    }

    #[test]
    fn deep_nesting_is_bounded() {
        let mut value = Value::String("Bearer abcdefghijklmnop".to_string());
        for _ in 0..(MAX_DETAIL_DEPTH + 4) {
            let mut map = Map::new();
            map.insert("child".to_string(), value);
            value = Value::Object(map);
        }
        let out = redact_value(&value, 0);
        assert!(!render(&out).contains("abcdefghijklmnop"));
    }
}
