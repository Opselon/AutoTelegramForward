package cleaning

import (
	"net/url"
	"regexp"
	"strings"
)

// LinkManagementEngine is the Go port of core.domain.services.
// LinkManagementEngine — the entity-aware, SSRF-protected, domain-policy
// driven link processor. It is the single authority for what "remove the
// links" means in a message body: the pipeline only ever calls through here,
// so there is one answer, not three (finding F-14).
//
// What it is NOT: this is not the FilterEngine's URL_REGEX gate and not the
// content policy's has_link predicate. Those answer "does this text carry a
// link" for block/drop decisions; this answers "which spans do I remove or
// rewrite, and what happens to the entities that pointed at them".
type LinkManagementEngine struct{}

// LinkPolicy mirrors core.domain.value_objects.LinkPolicy. The string values
// are the persistence contract — the rule row stores exactly these, so a Go
// constant set that changed them would break every stored task.
const (
	LinkPolicyPreserveAll      = "PRESERVE_ALL"
	LinkPolicyRemoveAllURLs    = "REMOVE_ALL_URLS"
	LinkPolicyRemoveExternal   = "REMOVE_EXTERNAL_URLS"
	LinkPolicyRemoveSelected   = "REMOVE_SELECTED_DOMAINS"
	LinkPolicyAllowlistOnly    = "ALLOWLIST_ONLY"
	LinkPolicyRewriteLinks     = "REWRITE_LINKS"
	LinkPolicyRemoveButtonURLs = "REMOVE_BUTTON_URLS"
	LinkPolicyCustom           = "CUSTOM_LINK_POLICY"
)

// tgDomains is LinkManagementEngine.TG_DOMAINS (services.py:763): the
// Telegram-owned domains an "external" policy still permits.
var tgDomains = map[string]bool{
	"t.me": true, "telegram.me": true, "telegram.dog": true,
}

// linkSpanRegex is the URL alternation the link engine scans visible text
// with — the SAME alternation Python's remove_urls/URL_REGEX applies
// (services.py:161, :26-29), url-only. It deliberately does NOT include
// URL_REGEX's @mention arm here: this is the span set the engine removes
// or rewrites, and a mention is never one of those (services.py:911:
// "A mention is not a link" — stripping them would remove usernames the
// user asked to keep, the F-13 divergence).
var linkSpanRegex = regexp.MustCompile(`(?i)(https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+)`)

// linkGateRegex is the full URL_REGEX (services.py:26-29), mention arm
// included. It is the "does this text carry anything URL-shaped" gate the
// FilterEngine's block_links path asks — a different question from "which
// spans do I remove", asked by a different caller, so the two patterns are
// two constants rather than one that is sometimes trimmed.
var linkGateRegex = regexp.MustCompile(`(?i)(https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+|@[A-Za-z0-9_]{3,32})`)

// markdownLinkRegex is step 1 of Python remove_urls / remove_links: a
// markdown hyperlink keeps its anchor text and loses the URL.
var markdownLinkRegex = regexp.MustCompile(`\[([^]]+)\]\((?:https?://|www\.|t\.me/|[^)]+)\)`)

// Entity is the UTF-16-offset form Telegram itself uses: offset and length
// count UTF-16 code units, not runes and not bytes. The pipeline's entity
// list is carried in this shape so the Go port never has to guess which unit
// a number is in.
type Entity struct {
	Type   string
	Offset int
	Length int
	URL    string
}

// LinkOutcome is the result of applying a link policy to one body: the new
// text, the surviving entities with shifted offsets, and whether anything
// changed. The entity list is always returned (possibly empty), matching the
// Python engine's contract, so a caller never has a stale view.
type LinkOutcome struct {
	Text     string
	Entities []Entity
	Altered  bool
}

// ApplyToBody replaces/removes URLs per the policy and keeps the entity
// offsets aligned with the surviving text. It is process_text_and_entities
// (services.py:836-1003) for the text-and-entity case.
func (LinkManagementEngine) ApplyToBody(text string, entities []Entity, cfg LinkConfig) LinkOutcome {
	policy := strings.ToUpper(strings.TrimSpace(cfg.Policy))
	if policy == "" || policy == LinkPolicyPreserveAll || text == "" {
		return LinkOutcome{Text: text, Entities: entities}
	}

	allowlist := normalizeDomains(cfg.Allowlist)
	blocklist := normalizeDomains(cfg.Blocklist)
	rewriteMap := cfg.RewriteMap

	// 1. text_link entities: strip the ones the policy rejects, rewrite the
	// ones it maps. Anchor text survives either way.
	filtered := make([]Entity, 0, len(entities))
	altered := false
	for _, ent := range entities {
		if strings.Contains(strings.ToLower(ent.Type), "text_link") {
			if shouldRemoveURL(ent.URL, policy, allowlist, blocklist) {
				altered = true
				continue
			}
			if rewriteMap != nil && (policy == LinkPolicyRewriteLinks || policy == LinkPolicyCustom) {
				if rewritten := rewriteURL(ent.URL, rewriteMap); rewritten != ent.URL {
					ent.URL = rewritten
					altered = true
				}
			}
		}
		filtered = append(filtered, ent)
	}

	// 2. Visible URL spans, processed from the end backwards so the Python
	// string-slicing indices stay valid.
	matches := linkSpanRegex.FindAllStringIndex(text, -1)
	if len(matches) == 0 {
		return LinkOutcome{Text: text, Entities: filtered, Altered: altered}
	}

	out := text
	for i := len(matches) - 1; i >= 0; i-- {
		start, end := matches[i][0], matches[i][1]
		raw := out[start:end]
		// The span regex is url-only, so no "@"-prefix case can reach here:
		// a mention is never a removal candidate (services.py:911), which is
		// exactly the F-13 fix the Python engine implements with an explicit
		// skip for the mention alternation URL_REGEX carries.
		if shouldRemoveURL(raw, policy, allowlist, blocklist) {
			out = out[:start] + out[end:]
			altered = true
			filtered = shiftAfterRemoval(filtered, start, end, raw)
		} else if rewriteMap != nil && (policy == LinkPolicyRewriteLinks || policy == LinkPolicyCustom) {
			rewritten := rewriteURL(raw, rewriteMap)
			if rewritten != raw {
				out = out[:start] + rewritten + out[end:]
				altered = true
				filtered = shiftAfterRewrite(filtered, start, raw, rewritten)
			}
		}
	}

	// 3. The trailing whitespace squeeze every Python removal applies, with
	// offsets remapped through the same arithmetic.
	out = squeezeSpaces(out)
	return LinkOutcome{Text: out, Entities: clipped(out, filtered), Altered: altered}
}

// LinkConfig is the link half of the task configuration.
type LinkConfig struct {
	Policy     string
	Allowlist  []string
	Blocklist  []string
	RewriteMap map[string]string
}

// ResolvePolicy is Python LinkManagementEngine.resolve_policy: a stored value
// that is not one of the named policies must never be read as "remove
// everything", so it fails closed to PRESERVE_ALL — and the cleaner's legacy
// "remove links" toggle means REMOVE_ALL_URLS.
func ResolvePolicy(raw string, removeLinks bool) string {
	policy := strings.ToUpper(strings.TrimSpace(raw))
	switch policy {
	case LinkPolicyPreserveAll, LinkPolicyRemoveAllURLs, LinkPolicyRemoveExternal,
		LinkPolicyRemoveSelected, LinkPolicyAllowlistOnly, LinkPolicyRewriteLinks,
		LinkPolicyRemoveButtonURLs, LinkPolicyCustom:
	default:
		policy = LinkPolicyPreserveAll
	}
	if policy == LinkPolicyPreserveAll && removeLinks {
		return LinkPolicyRemoveAllURLs
	}
	return policy
}

// shouldRemoveURL is the Python _should_remove closure: the per-policy
// domain test for one URL.
func shouldRemoveURL(rawURL, policy string, allowlist, blocklist []string) bool {
	dom := ExtractDomain(rawURL)
	switch policy {
	case LinkPolicyRemoveAllURLs:
		return true
	case LinkPolicyRemoveExternal:
		return !tgDomains[dom] && !matchesDomain(dom, tgDomainList())
	case LinkPolicyRemoveSelected:
		return matchesDomain(dom, blocklist)
	case LinkPolicyAllowlistOnly:
		return !matchesDomain(dom, allowlist)
	case LinkPolicyCustom:
		if len(blocklist) > 0 && matchesDomain(dom, blocklist) {
			return true
		}
		if len(allowlist) > 0 && !matchesDomain(dom, allowlist) {
			return true
		}
	}
	return false
}

func tgDomainList() []string {
	return []string{"t.me", "telegram.me", "telegram.dog"}
}

// rewriteURL is the Python _rewrite_url: the first rewrite entry whose
// domain (or parent domain) matches supplies the substitution.
//
// Two key shapes go beyond a plain domain, mirroring the Python engine:
//
//   - "*" is the catch-all ("replace any link found"): every URL that no
//     explicit key claimed is rewritten to its value;
//   - a key carrying "/" is a URL-prefix match: the URLs it starts with are
//     rewritten to the value (scheme-relative), so a task can redirect one
//     deep link instead of a whole domain.
func rewriteURL(rawURL string, rewriteMap map[string]string) string {
	star, hasStar := rewriteMap["*"]
	dom := ExtractDomain(rawURL)
	lowered := strings.ToLower(rawURL)
	for old, replacement := range rewriteMap {
		cleanOld := strings.ToLower(strings.TrimSpace(old))
		if cleanOld == "" || cleanOld == "*" {
			continue
		}
		if strings.Contains(cleanOld, "/") {
			if strings.HasPrefix(lowered, cleanOld) {
				return replacement + rawURL[len(cleanOld):]
			}
			continue
		}
		if dom == cleanOld || strings.HasSuffix(dom, "."+cleanOld) {
			return strings.Replace(rawURL, old, replacement, 1)
		}
	}
	if hasStar {
		// Python answers `star if star is not None else u`: an empty star
		// value is a deliberate rewrite to "" (an effective strip), not a
		// "no match".
		return star
	}
	return rawURL
}

// ExtractDomain is Python LinkManagementEngine.extract_domain: lowercased,
// no leading www., no port.
func ExtractDomain(rawURL string) string {
	raw := strings.TrimSpace(rawURL)
	if raw == "" {
		return ""
	}
	if !regexp.MustCompile(`^[a-zA-Z][a-zA-Z0-9+.-]*://`).MatchString(raw) {
		raw = "https://" + raw
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	host := strings.ToLower(parsed.Hostname())
	host = strings.TrimPrefix(host, "www.")
	return host
}

// matchesDomain is Python LinkManagementEngine.matches_domain: the domain or
// any of its parents.
func matchesDomain(domain string, list []string) bool {
	clean := strings.ToLower(strings.TrimSpace(domain))
	clean = strings.TrimPrefix(clean, "www.")
	for _, item := range list {
		entry := strings.ToLower(strings.TrimSpace(item))
		entry = strings.TrimPrefix(entry, "www.")
		if clean == entry || strings.HasSuffix(clean, "."+entry) {
			return true
		}
	}
	return false
}

func normalizeDomains(list []string) []string {
	out := make([]string, 0, len(list))
	for _, item := range list {
		if trimmed := strings.ToLower(strings.TrimSpace(item)); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

// squeezeSpaces is the Python trailing cleanup: "Clean trailing double
// spaces" (services.py:967-971), collapsed runs to one and 3+ newlines to
// two.
func squeezeSpaces(text string) string {
	text = regexp.MustCompile(`[ \t]+`).ReplaceAllString(text, " ")
	text = regexp.MustCompile(`\n\s*\n\s*\n+`).ReplaceAllString(text, "\n\n")
	return strings.TrimSpace(text)
}

// shiftAfterRemoval is the entity bookkeeping for a removed span
// (services.py:916-937): entities entirely after the span shift left by the
// span's UTF-16 length; entities entirely before it are untouched; an entity
// that straddles the span is shrunk or dropped.
//
// All offsets in this function are BYTE offsets of the Go string — the
// caller's matches come from the Go regex over the Go string. The conversion
// into UTF-16 happens once, at the boundary with the Telegram wire format
// (clipped/toUTF16), so the arithmetic here stays in one unit system.
func shiftAfterRemoval(entities []Entity, start, end int, removed string) []Entity {
	removedLen := end - start
	out := make([]Entity, 0, len(entities))
	for _, ent := range entities {
		if ent.Offset >= end {
			next := ent
			next.Offset = ent.Offset - removedLen
			if next.Offset < 0 {
				next.Offset = 0
			}
			out = append(out, next)
			continue
		}
		if ent.Offset+ent.Length <= start {
			out = append(out, ent)
			continue
		}
		newLen := ent.Length - removedLen
		if newLen > 0 {
			next := ent
			next.Length = newLen
			out = append(out, next)
		}
	}
	_ = removed
	return out
}

// shiftAfterRewrite moves entities past a rewritten span by the length delta.
func shiftAfterRewrite(entities []Entity, start int, raw, rewritten string) []Entity {
	diff := len(rewritten) - len(raw)
	out := make([]Entity, 0, len(entities))
	for _, ent := range entities {
		if ent.Offset >= start+len(raw) {
			next := ent
			next.Offset += diff
			out = append(out, next)
		} else {
			out = append(out, ent)
		}
	}
	return out
}

// clipped drops entities that no longer fit the surviving body.
func clipped(text string, entities []Entity) []Entity {
	limit := len(text)
	out := make([]Entity, 0, len(entities))
	for _, ent := range entities {
		if ent.Length <= 0 || ent.Offset < 0 || ent.Offset >= limit {
			continue
		}
		if ent.Offset+ent.Length > limit {
			ent.Length = limit - ent.Offset
		}
		if ent.Length > 0 {
			out = append(out, ent)
		}
	}
	return out
}

// RemoveURLsPlain is the Python FilterEngine.remove_urls (services.py:159),
// used by the content-policy rewrite path (ContentPolicy.transform step 1)
// and by the target override. It keeps @mentions — URL_REGEX's mention
// alternation is deliberately NOT applied here, because the content policy's
// remove_links is about links, not handles.
func RemoveURLsPlain(text string) string {
	if text == "" {
		return ""
	}
	cleaned := markdownLinkRegex.ReplaceAllString(text, "$1")
	cleaned = linkSpanRegex.ReplaceAllString(cleaned, "")
	cleaned = squeezeSpacesBody(cleaned)
	return cleaned
}

func squeezeSpacesBody(text string) string {
	text = regexp.MustCompile(`[ \t]+`).ReplaceAllString(text, " ")
	text = regexp.MustCompile(`\n\s*\n\s*\n+`).ReplaceAllString(text, "\n\n")
	return strings.TrimSpace(text)
}
