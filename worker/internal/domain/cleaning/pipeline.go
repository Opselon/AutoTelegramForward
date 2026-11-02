package cleaning

import (
	"regexp"
	"strconv"
	"strings"
)

// Pipeline is the Go port of DurableMessagePipeline._apply_transforms
// (core/application/pipeline.py:1401-1580): the single ordered owner of every
// text transform a task applies between the raw source message and the
// deliverable that gets enqueued.
//
// THE ORDER IS THE CONTRACT (processing-pipeline.md). Do not reorder:
//
//  1. cleaner                     — removal-only, before every other transform
//                                   so nothing downstream can resurrect
//                                   removed content
//  2. link management             — the task link policy (entity-aware), or
//                                   the legacy link_replacement rewrite
//                                   (durable path: no remove_links+replacement
//                                   combination — F-04)
//  3. remove_emojis flag          — the task-level flag, distinct from the
//                                   cleaner's own emoji switch
//  4. empty_after_clean gate      — the ONLY transform-internal drop
//  5. content policy rewrite      — remove_links / strip_words / replacements
//  6. replacements                — the task's replace entries
//  7. header / footer             — raw, placeholders NOT interpolated (F-15)
//  8. advanced transforms         — watermark / link buttons / affiliate kit
//  9. length clamp                — Telegram's 1024/4096 ceilings
//
// Every stage is failure-tolerant by design: a transform that crashes or
// refuses delivers the text it was handed, never a drop (the Python
// pipeline's own rule, pipeline.py:1437 and :1493). The drop only comes from
// the empty_after_clean gate, and only when the text had content before the
// stages ran.
type Pipeline struct {
	links LinkManagementEngine
}

// NewPipeline returns a Pipeline. It is stateless and safe for concurrent
// use: every call works from the caller's own config and input.
func NewPipeline() *Pipeline { return &Pipeline{} }

// Result is what _apply_transforms returns to its caller: the transformed
// text, whether the transform pipeline decided the message has nothing left
// to send, and a short record of which stages changed the text.
type Result struct {
	Text string
	// EmptyAfterClean is the pipeline's only drop: the stages removed the
	// whole body of a message that had content before them.
	EmptyAfterClean bool
	// StageNotes names the stages that changed the text, in order — the
	// observable answer to "which transformation changed the content?".
	StageNotes []string
	// EntitiesDropped reports whether the entity list needed clipping because
	// a transform shortened the body under the entities.
	EntitiesDropped bool
}

// Apply runs the full ordered transform on the raw source text. The caller
// hands it the payload's effective text (the caption for media, the body
// otherwise) and the task's resolved configuration; it returns the text the
// target should receive.
//
// It never returns an error. Every Python failure mode here is "deliver what
// you have and log", and the callers that need to drop use Result.
func (p *Pipeline) Apply(raw string, cfg Config, entities []Entity) Result {
	text := raw
	var notes []string
	ents := entities

	// 1. Cleaner — removal-only, first, never drops.
	if cfg.Cleaner != nil && cfg.Cleaner.IsActive {
		cleaned := cfg.Cleaner.Clean(text)
		if cleaned != text {
			notes = append(notes, "cleaner")
			text = cleaned
		}
	}

	// 2. Link management. The durable path's if/elif: an explicit policy, or
	// the legacy remove_links toggle, or the link_replacement rewrite —
	// never a combination (F-04: remove_links wins and the replacement is
	// silently ignored on the durable path).
	policy := ResolvePolicy(cfg.LinkPolicy, cfg.RemoveLinks)
	if policy != LinkPolicyPreserveAll {
		outcome := p.links.ApplyToBody(text, ents, LinkConfig{
			Policy:     policy,
			Allowlist:  cfg.DomainAllowlist,
			Blocklist:  cfg.DomainBlocklist,
			RewriteMap: cfg.LinkRewriteMap,
		})
		text = outcome.Text
		ents = outcome.Entities
		if outcome.Altered {
			notes = append(notes, "link_policy:"+policy)
		}
	} else if cfg.LinkReplacement != "" {
		text = replaceLinks(text, cfg.LinkReplacement)
		notes = append(notes, "link_replacement")
	}

	// 3. The task-level emoji flag. Distinct from the cleaner's own switch;
	// both can run and both are idempotent on the same input (the audit's
	// emoji double-processing resolution).
	//
	// The Python flag path (services.remove_emojis) is
	// EMOJI_REGEX.sub("", text).strip(): a leading/trailing clean, the
	// interior whitespace left alone. The cleaner's own stage (stage 1)
	// does NOT strip — the two are deliberately different and this port
	// keeps both exactly as they are (UNI-EMOJI-04's double space is the
	// pinned evidence).
	if cfg.RemoveEmojis {
		next := cleanEmojiRegex.ReplaceAllString(text, "")
		next = strings.TrimSpace(next)
		if next != text {
			text = next
			notes = append(notes, "remove_emojis")
		}
	}

	// 4. The only transform-internal drop: the stages stripped everything
	// from a message that had content.
	if strings.TrimSpace(raw) != "" && strings.TrimSpace(text) == "" {
		return Result{Text: text, EmptyAfterClean: true, StageNotes: notes}
	}

	// 5. Content policy rewrite (remove_links / strip_words / replacements).
	// Failure never drops: deliver the text we have.
	if cfg.ContentPolicy != nil {
		next, applied := applyContentPolicyRewrite(text, cfg.ContentPolicy)
		if applied {
			text = next
			notes = append(notes, "content_policy")
		}
	}

	// 5b. The target-scoped override. The Python fan-out applies the
	// matching entry's rewrite to THIS target's copy alone — one task can
	// route A,B -> X,Y where X and Y carry different overrides — so the
	// Worker applies it after the task policy, on the same target's text.
	// An absent entry rewrites nothing; it never relaxes the task policy.
	// (pipeline.py:1276-1285, the loop over chain[1:] — chain[0] is the
	// task policy itself and was applied in step 5.)
	if override := cfg.TargetOverrides[chatKey(cfg.TargetChatID)]; override != nil {
		next, applied := applyContentPolicyRewrite(text, override)
		if applied {
			text = next
			notes = append(notes, "target_override")
		}
	}

	// 6. Replacements, AFTER the policy so an expanding entry cannot
	// resurrect what a policy strip removed (pipeline.py:1540-1543).
	if len(cfg.Replacements) > 0 {
		next := applyReplacements(text, cfg.Replacements)
		if next != text {
			text = next
			notes = append(notes, "replacements")
		}
	}

	// 7. Header / footer — raw. The durable path deliberately does NOT
	// interpolate placeholders (F-15: HeaderFormatter runs only on the
	// legacy/preview path); this port preserves production behavior instead
	// of importing the preview's expansion.
	if cfg.Header != "" || cfg.Footer != "" {
		next := applyHeaderFooter(text, cfg.Header, cfg.Footer)
		if next != text {
			text = next
			notes = append(notes, "header_footer")
		}
	}

	// 8. Advanced content transforms (watermark / link buttons / affiliate
	// kit), each gated by its paid-add-on flag having been resolved into the
	// config by the caller.
	if cfg.Watermark != "" || len(cfg.LinkButtons) > 0 || cfg.AffiliateKit != nil {
		next, applied := applyAdvanced(text, cfg)
		if applied {
			text = next
			notes = append(notes, "advanced")
		}
	}

	// 9. Telegram's length ceilings. Python clamps in the step after the
	// transform (pipeline.py:1105-1109); a media message is a caption and
	// caps at 1024, everything else at 4096. Count code points, not bytes —
	// Python len() is codepoints and a byte count would cut a Persian or
	// emoji message early.
	text = clampText(text, cfg.HasMedia)

	// The entities that survived the link engine still have to fit the final
	// body: every later stage can move the text under them.
	if len(ents) > 0 {
		clipped := clippedText(text, ents)
		if len(clipped) != len(ents) {
			ents = clipped
		}
	}

	return Result{Text: text, StageNotes: notes}
}

// clampText enforces the Telegram caption/message ceilings the Python
// pipeline applies after a transform: 1024 for a media caption, 4096 for a
// plain text message. Code points, not bytes.
func clampText(text string, hasMedia bool) string {
	if text == "" {
		return text
	}
	max := 4096
	if hasMedia {
		max = 1024
	}
	runes := []rune(text)
	if len(runes) > max {
		return string(runes[:max])
	}
	return text
}

func clippedText(text string, entities []Entity) []Entity {
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

// replaceLinks is FilterEngine.replace_links (services.py:225): markdown
// anchors take the replacement, then raw URLs take it, then the whitespace
// squeeze.
func replaceLinks(text, replacement string) string {
	if text == "" {
		return ""
	}
	cleaned := markdownLinkRegex.ReplaceAllString(text, replacement)
	cleaned = linkSpanRegex.ReplaceAllString(cleaned, replacement)
	return squeezeSpacesBody(cleaned)
}

// applyHeaderFooter is FilterEngine.apply_header_footer (services.py:247):
// header prepended, footer appended, each suppressed when the text already
// starts/ends with it (no duplicate insertion).
func applyHeaderFooter(text, header, footer string) string {
	parts := make([]string, 0, 3)
	h := strings.TrimSpace(header)
	if h != "" {
		if !strings.HasPrefix(strings.TrimSpace(text), h) {
			parts = append(parts, h)
		}
	}
	if strings.TrimSpace(text) != "" {
		parts = append(parts, strings.TrimSpace(text))
	}
	f := strings.TrimSpace(footer)
	if f != "" {
		if !strings.HasSuffix(strings.TrimSpace(text), f) {
			parts = append(parts, f)
		}
	}
	return strings.Join(parts, "\n\n")
}

// applyContentPolicyRewrite is the transform half of ContentPolicy.transform
// (content_policy.py:907-943): remove links, strip words, apply the policy's
// own replacements — one stage, in that order, on the cleaned text.
func applyContentPolicyRewrite(text string, policy *ContentPolicy) (string, bool) {
	if policy == nil || text == "" {
		return text, false
	}
	out := text
	changed := false
	if policy.RemoveLinks {
		next := RemoveURLsPlain(out)
		if next != out {
			out = next
			changed = true
		}
	}
	if len(policy.StripWords) > 0 {
		anyRemoved := false
		for _, word := range policy.StripWords {
			var n int
			out, n = stripWord(out, word, policy.CaseSensitive)
			anyRemoved = anyRemoved || n > 0
		}
		if anyRemoved {
			out = tidy(out)
			changed = true
		}
	}
	if len(policy.Replacements) > 0 {
		next := applyPairs(out, policy.Replacements, policy.CaseSensitive)
		if next != out {
			out = next
			changed = true
		}
	}
	return out, changed
}

// stripWord removes every occurrence of word, boundary-aware, using the
// shared Persian/Arabic-aware matcher notion of a word. It mirrors
// content_policy._strip_word.
func stripWord(text, word string, caseSensitive bool) (string, int) {
	needle := strings.TrimSpace(word)
	if needle == "" || text == "" {
		return text, 0
	}
	// Word-boundary, normalized, case-folded: the policy's own match flags
	// (content_policy._match_flags defaults).
	pattern := wordBoundaryPattern(needle, caseSensitive)
	if pattern == nil {
		return text, 0
	}
	spans := pattern.FindAllStringSubmatchIndex(text, -1)
	if len(spans) == 0 {
		return text, 0
	}
	var out strings.Builder
	cursor := 0
	for _, span := range spans {
		start, end := span[0], span[1]
		if start < cursor {
			continue
		}
		out.WriteString(text[cursor:start])
		cursor = end
	}
	out.WriteString(text[cursor:])
	return out.String(), len(spans)
}

// applyPairs is content_policy._apply_replacements: one pass over the
// original text so a=>b followed by b=>c yields b, not c. A "re:" prefix on
// the find side marks a regex.
func applyPairs(text string, pairs [][2]string, caseSensitive bool) string {
	if len(pairs) == 0 || text == "" {
		return text
	}
	parts := make([]string, 0, len(pairs))
	replacements := make([]string, 0, len(pairs))
	for _, pair := range pairs {
		find, replacement := pair[0], pair[1]
		if find == "" {
			continue
		}
		body := find
		if strings.HasPrefix(body, "re:") {
			body = body[3:]
		} else {
			body = regexpQuoteMeta(body)
		}
		if body == "" {
			continue
		}
		parts = append(parts, "("+body+")")
		replacements = append(replacements, replacement)
	}
	if len(parts) == 0 {
		return text
	}
	flags := ""
	if !caseSensitive {
		flags = "(?i)"
	}
	compiled, err := compileSafe(flags + strings.Join(parts, "|"))
	if err != nil {
		return text
	}
	index := 0
	return compiled.ReplaceAllStringFunc(text, func(match string) string {
		// Which alternation matched? ReplaceAllStringFunc does not say, so
		// the pre-built list is scanned in order. The first whose body equals
		// the match text is the one that fired; an entry that matched is the
		// replacement to substitute, and the counter advances past it.
		for i, part := range parts {
			if match == part[1:len(part)-1] {
				index = i
				break
			}
		}
		if index < len(replacements) {
			rep := replacements[index]
			index++
			return rep
		}
		return match
	})
}

// applyAdvanced is AdvancedSettingsEngine.apply_content_transforms
// (services.py:1443-1490): watermark, then link buttons, then the affiliate
// kit, each appending or rewriting, each idempotent (an already-present
// watermark is not duplicated).
func applyAdvanced(text string, cfg Config) (string, bool) {
	out := text
	applied := false
	if wm := strings.TrimSpace(cfg.Watermark); wm != "" {
		if !strings.Contains(out, wm) {
			if strings.TrimSpace(out) == "" {
				out = wm
			} else {
				out = out + "\n\n" + wm
			}
			applied = true
		}
	}
	if len(cfg.LinkButtons) > 0 {
		var lines []string
		for _, button := range cfg.LinkButtons {
			if button.URL == "" {
				continue
			}
			lines = append(lines, "🔗 "+strings.TrimSpace(button.Text)+": "+strings.TrimSpace(button.URL))
		}
		if len(lines) > 0 {
			block := strings.Join(lines, "\n")
			if !strings.Contains(out, block) {
				if strings.TrimSpace(out) == "" {
					out = block
				} else {
					out = out + "\n\n" + block
				}
				applied = true
			}
		}
	}
	if cfg.AffiliateKit != nil && cfg.AffiliateKit.Prefix != "" {
		replaced := affiliateRewrite(out, cfg.AffiliateKit.Prefix)
		if replaced != out {
			out = replaced
			applied = true
		}
	}
	return out, applied
}

// affiliateRewrite prefixes naked http(s) URLs through the configured
// affiliate prefix, preserving every other character (services.py:1483).
func affiliateRewrite(text, prefix string) string {
	return linkSpanRegex.ReplaceAllStringFunc(text, func(match string) string {
		if strings.HasPrefix(match, "http") {
			return prefix + match
		}
		return match
	})
}

// tidy is content_policy._tidy: collapse the spacing a removal leaves
// behind, without touching newlines beyond trailing space removal.
func tidy(text string) string {
	text = manySpaces.ReplaceAllString(text, " ")
	text = regexp.MustCompile(`[ \t]+\n`).ReplaceAllString(text, "\n")
	return strings.TrimSpace(text)
}

// chatKey normalizes a chat id the way Python's _chat_key does
// (entities.py:81-92): "-100123" as an int, a float and a string are one
// chat; an @username passes through unchanged. The target-override lookup
// matches on the same key the Python policy_chain builds its map with, so
// a UI that sends "-100123" and a client that sends -100123 resolve to the
// same override.
func chatKey(chatID string) string {
	trimmed := strings.TrimSpace(chatID)
	if trimmed == "" {
		return ""
	}
	// A leading "-" is part of a numeric supergroup id, not whitespace.
	if n, err := strconv.ParseInt(trimmed, 10, 64); err == nil {
		return strconv.FormatInt(n, 10)
	}
	if f, err := strconv.ParseFloat(trimmed, 64); err == nil {
		return strconv.FormatInt(int64(f), 10)
	}
	return trimmed
}
