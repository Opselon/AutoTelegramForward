package cleaning

import (
	"context"
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

// CaptionHint tells the pipeline whether the text it is handed is a media
// message's caption rather than a plain body.
//
// The Python pipeline derives this implicitly — it always feeds
// payload.effective_text, which IS the caption for media
// (entities.py:2498) — and so it cannot honor the cleaner's
// apply_to_caption=False switch: the switch has no reader (finding F-09,
// "an owner who turns the caption switch off still gets captions
// cleaned"). The Go side takes the flag seriously instead: when the text
// is a caption and the task opted out of caption cleaning, the TEXT
// cleaner is skipped (markup-level options such as strip_buttons still
// apply, exactly as Python's pipeline applies them outside clean()).
//
// Zero value (IsCaption false) is the pre-4.4 behavior: clean everything
// handed in, which remains correct for a plain-body message.
type CaptionHint struct {
	IsCaption bool
}

// Apply is the compatibility entry point: it runs the pipeline with no
// caption information, so the text is treated as a plain body and every
// configured stage runs. Callers that know whether the text is a caption
// should prefer ApplyText.
func (p *Pipeline) Apply(raw string, cfg Config, entities []Entity) Result {
	return p.ApplyText(raw, cfg, entities, CaptionHint{})
}

// ApplyCtx is Apply with a context, for the one stage that needs one:
// the Stage 5 (W4) AI rewrite is a network call, and its deadline is the
// caller's. Every other stage is deterministic and ignores the context.
func (p *Pipeline) ApplyCtx(ctx context.Context, raw string, cfg Config, entities []Entity, ch CaptionHint) Result {
	return p.applyText(ctx, raw, cfg, entities, ch)
}

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
	// AIDrop is the Stage 5 (W4) AI-refusal drop: the rewrite failed and the
	// rule's fallback policy is DROP, RETRY or QUARANTINE, so nothing is
	// sendable. It is a distinct flag rather than a reuse of
	// EmptyAfterClean because the two drops have different operators: the
	// cleaner's drop is a content decision, the AI's is a delivery-routing
	// decision the delivery layer answers for (re-queue, hold, discard).
	AIDrop bool
	// AIClassification carries the AI error code on a refused rewrite, so
	// the delivery layer can log and route on the same vocabulary the
	// Python pipeline uses ([PIPELINE:AI_REWRITE:FAILED] code=…,
	// pipeline.py:1795) without re-parsing an error string.
	AIClassification string
}

// ApplyText runs the full ordered transform on the raw source text. The
// caller hands it the payload's effective text (the caption for media, the
// body otherwise), the task's resolved configuration, and a caption hint
// carrying whether that text is a caption. It returns the text the target
// should receive.
//
// It never returns an error. Every Python failure mode here is "deliver what
// you have and log", and the callers that need to drop use Result.
//
// The Stage 5 (W4) AI rewrite needs a context; ApplyText delegates to the
// context-carrying implementation with a background context, which is
// exactly the pre-W4 behaviour for every AI-less config (a nil AIRewrite
// hook never observes it).
func (p *Pipeline) ApplyText(raw string, cfg Config, entities []Entity, ch CaptionHint) Result {
	return p.applyText(context.Background(), raw, cfg, entities, ch)
}

func (p *Pipeline) applyText(ctx context.Context, raw string, cfg Config, entities []Entity, ch CaptionHint) Result {
	text := raw
	var notes []string
	ents := entities

	// 1. Cleaner — removal-only, first, never drops. F-09: a task that
	// opted out of caption cleaning must not have its media caption
	// cleaned. Python's pipeline cannot honor the switch because it feeds
	// effective_text unconditionally; the Go side reads the payload's own
	// has_media flag, so it can. strip_buttons still applies — in Python
	// the pipeline enforces that markup-level option outside clean().
	skipTextCleaner := ch.IsCaption && cfg.Cleaner != nil && cfg.Cleaner.IsActive && !cfg.Cleaner.ApplyToCaption
	if !skipTextCleaner && cfg.Cleaner != nil && cfg.Cleaner.IsActive {
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
		// F-03: the sequence-aware stripper, not the SMP-wide regex —
		// cuneiform and math alphanumerics are not emoji and a ZWJ family
		// leaves no dangling joiners behind.
		next := RemoveEmojisGrapheme(text)
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

	// 5c. Stage 5 (W4): the AI rewrite. Python runs the provider call here
	// — after the content-policy / cleaner / link transforms so the
	// rewrite operates on the text the task already cleaned and cannot
	// resurrect what a stage removed, and before the final replacements
	// and header/footer so the task's own pair still applies to the
	// rewritten text (pipeline.py:1590-1660). A nil hook is "no AI
	// configured": the stage is absent and the pipeline is the pre-W4
	// one, byte for byte.
	if cfg.AIRewrite != nil {
		rewritten, err := cfg.AIRewrite(ctx, text)
		if err != nil {
			// The rewrite refused. The fallback policy decides what
			// survives: SEND_ORIGINAL keeps the pre-rewrite text, every
			// other policy (DROP / RETRY / QUARANTINE) leaves nothing
			// sendable — the Python pipeline returns None for all three
			// and lets the delivery layer distinguish them
			// (pipeline.py:1817-1884). A nil callback is the Python
			// default, DROP: never a silent pass-through.
			if cfg.AIFallback != nil {
				code := aiErrorCode(err)
				outcome, sendable := cfg.AIFallback(ctx, text, code, err.Error())
				if !sendable {
					return Result{Text: "", EmptyAfterClean: true, StageNotes: append(notes, "ai:"+code), AIDrop: true, AIClassification: code}
				}
				text = outcome
			} else {
				return Result{Text: "", EmptyAfterClean: true, StageNotes: append(notes, "ai:DROP"), AIDrop: true, AIClassification: "DROP"}
			}
			notes = append(notes, "ai:fallback:"+aiErrorCode(err))
		} else {
			text = rewritten
			notes = append(notes, "ai_rewrite")
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

	// 7. Header / footer. Stage 4.4 closes finding F-15: the Python durable
	// path applied these RAW ("From [[chat_title]]" stayed literal) while
	// only the preview and the edit-sync path ran the formatter, so an
	// owner's {source_link} reached the target unexpanded. The Worker now
	// renders the placeholders the same way the preview always did.
	//
	// An unknown placeholder is left in place rather than removed (Python's
	// loop replaces only the keys it knows), and an empty header/footer
	// applies nothing — including the no-duplicate-duplication guard
	// applyHeaderFooter already implements.
	if cfg.Header != "" || cfg.Footer != "" {
		header := cfg.Header
		footer := cfg.Footer
		if cfg.HeaderVars != nil {
			if header != "" {
				header = FormatHeader(header, cfg.HeaderTask, *cfg.HeaderVars)
			}
			if footer != "" {
				footer = FormatHeader(footer, cfg.HeaderTask, *cfg.HeaderVars)
			}
		}
		next := applyHeaderFooter(text, header, footer)
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

// AIError is the classification an installed AIRewriter returns. The
// delivery stage's hook builds it from the aiprovider package's typed
// error, so the fallback decision and the log line see the same code the
// Python pipeline reports ([PIPELINE:AI_REWRITE:FAILED] code=…).
type AIError struct {
	Code    string
	Message string
}

func (e *AIError) Error() string {
	if e == nil {
		return ""
	}
	if e.Message == "" {
		return "ai: " + e.Code
	}
	return "ai: " + e.Code + ": " + e.Message
}

// aiErrorCode answers the classification a rewrite refusal carries, for
// the stage note and the fallback callback. An error that carries no
// classification is PROVIDER_ERROR — the Python except-Exception default
// (transformer.py:299), which is also retryable there.
func aiErrorCode(err error) string {
	if err == nil {
		return ""
	}
	var classified *AIError
	if errorsAs(err, &classified) {
		return classified.Code
	}
	return "PROVIDER_ERROR"
}

// errorsAs is the one-call errors.As shim, kept local so the package
// does not import errors for a single use.
func errorsAs(err error, target any) bool {
	ai, ok := err.(*AIError)
	if !ok {
		return false
	}
	if setter, ok := target.(**AIError); ok {
		*setter = ai
		return true
	}
	return false
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
