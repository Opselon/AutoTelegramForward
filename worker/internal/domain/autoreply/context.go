package autoreply

import (
	"regexp"
	"strings"

	"atf/worker/internal/infra/aiprovider"
)

var (
	reToken  = regexp.MustCompile(`\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b`)
	reCard   = regexp.MustCompile(`\b\d{13,19}\b`)
	reBearer = regexp.MustCompile(`(?i)\b(bearer\s+)[A-Za-z0-9._-]{12,}`)

	rePersianArabic = regexp.MustCompile(`[\x{0600}-\x{06FF}]`)
	reCyrillic      = regexp.MustCompile(`[\x{0400}-\x{04FF}]`)
	reChinese       = regexp.MustCompile(`[\x{4E00}-\x{9FFF}]`)
	reJapanese      = regexp.MustCompile(`[\x{3040}-\x{30FF}]`)
	reKorean        = regexp.MustCompile(`[\x{AC00}-\x{D7AF}]`)
	reHebrew        = regexp.MustCompile(`[\x{0590}-\x{05FF}]`)
)

// DetectLanguage detects script family from the first 200 characters.
func DetectLanguage(text string) string {
	sample := text
	if len(sample) > 200 {
		sample = sample[:200]
	}
	if rePersianArabic.MatchString(sample) {
		return "fa"
	}
	if reCyrillic.MatchString(sample) {
		return "ru"
	}
	if reChinese.MatchString(sample) {
		return "zh"
	}
	if reJapanese.MatchString(sample) {
		return "ja"
	}
	if reKorean.MatchString(sample) {
		return "ko"
	}
	if reHebrew.MatchString(sample) {
		return "he"
	}
	return "en"
}

// ResolveLanguage resolves the effective response language: rule -> prompt -> settings -> detected.
func ResolveLanguage(settings *AutoReplySettings, prompt *AutoReplyPrompt, rule *AutoReplyRule, text string) string {
	if rule != nil && strings.TrimSpace(rule.Language) != "" {
		return strings.ToLower(strings.TrimSpace(rule.Language))
	}
	if prompt != nil && strings.TrimSpace(prompt.Language) != "" {
		return strings.ToLower(strings.TrimSpace(prompt.Language))
	}
	if settings != nil && strings.TrimSpace(settings.Language) != "" {
		return strings.ToLower(strings.TrimSpace(settings.Language))
	}
	return DetectLanguage(text)
}

// RedactForPrompt strips obvious credentials from text before sending to AI providers.
func RedactForPrompt(text string) string {
	if text == "" {
		return ""
	}
	out := reToken.ReplaceAllString(text, "[redacted]")
	out = reCard.ReplaceAllString(out, "[redacted-number]")
	out = reBearer.ReplaceAllString(out, "${1}[redacted]")
	return out
}

// RenderUserTemplate renders the message through the template with length protection.
func RenderUserTemplate(template, text, sender string) string {
	raw := template
	if raw == "" {
		raw = DefaultUserTemplate
	}
	if !strings.Contains(raw, "{text}") {
		raw = strings.TrimRight(raw, " \t\r\n") + "\n\n{text}"
	}
	safeText := text
	if len(safeText) > MaxInboundChars {
		safeText = safeText[:MaxInboundChars]
	}
	out := strings.ReplaceAll(raw, "{text}", safeText)
	out = strings.ReplaceAll(out, "{sender}", sender)
	return out
}

// BuildSystemPrompt constructs the bounded persona prompt with directives and memory.
func BuildSystemPrompt(
	settings *AutoReplySettings,
	prompt *AutoReplyPrompt,
	rule *AutoReplyRule,
	memory []string,
	language, text string,
) string {
	effLang := language
	if effLang == "" {
		effLang = ResolveLanguage(settings, prompt, rule, text)
	}

	tone := ToneFriendly
	if rule != nil && rule.Tone != "" {
		tone = CoerceTone(rule.Tone)
	} else if prompt != nil && prompt.Tone != "" {
		tone = CoerceTone(prompt.Tone)
	} else if settings != nil {
		tone = settings.Tone
	}

	respLength := ResponseLengthShort
	if rule != nil && rule.ResponseLength != "" {
		respLength = CoerceResponseLength(rule.ResponseLength)
	} else if prompt != nil && prompt.ResponseLength != "" {
		respLength = CoerceResponseLength(prompt.ResponseLength)
	} else if settings != nil {
		respLength = settings.ResponseLength
	}

	base := DefaultSystemPrompt
	if prompt != nil && strings.TrimSpace(prompt.SystemPrompt) != "" {
		base = strings.TrimSpace(prompt.SystemPrompt)
	}

	var blocks []string
	blocks = append(blocks, base)

	if toneLine, ok := ToneDirectives[tone]; ok && toneLine != "" {
		blocks = append(blocks, "Tone: "+toneLine)
	}
	if lenLine, ok := LengthDirectives[respLength]; ok && lenLine != "" {
		blocks = append(blocks, "Length: "+lenLine)
	}

	langName, ok := LanguageNames[effLang]
	if !ok || langName == "" {
		langName = effLang
		if langName == "" {
			langName = "English"
		}
	}
	blocks = append(blocks, "Write the reply in "+langName+".")

	if len(memory) > 0 {
		var facts []string
		limit := len(memory)
		if limit > 20 {
			limit = 20
		}
		for i := 0; i < limit; i++ {
			fact := strings.TrimSpace(memory[i])
			if fact != "" {
				facts = append(facts, "- "+fact)
			}
		}
		if len(facts) > 0 {
			blocks = append(blocks, "Things you remember about this conversation:\n"+strings.Join(facts, "\n"))
		}
	}

	if settings != nil {
		extra := strings.TrimSpace(settings.ExtraInstructions)
		if extra != "" {
			blocks = append(blocks, "Owner instructions (highest priority after safety):\n"+extra)
		}
	}

	out := strings.Join(blocks, "\n\n")
	if len(out) > MaxSystemPromptChars {
		head := out[:MaxSystemPromptChars/2]
		tail := out[len(out)-MaxSystemPromptChars/2:]
		out = head + "\n[…]\n" + tail
	}
	return out
}

// BuildMessages converts prompt context and history into provider ChatComplete messages.
func BuildMessages(
	systemPrompt string,
	history []MessageHistory,
	inboundText, template, sender string,
) []aiprovider.Message {
	messages := []aiprovider.Message{
		{Role: "system", Content: systemPrompt},
	}
	for _, item := range history {
		role := strings.ToLower(strings.TrimSpace(item.Role))
		if role != "user" && role != "assistant" {
			role = "user"
		}
		content := strings.TrimSpace(item.Content)
		if len(content) > MaxInboundChars {
			content = content[:MaxInboundChars]
		}
		if content != "" {
			messages = append(messages, aiprovider.Message{Role: role, Content: content})
		}
	}
	messages = append(messages, aiprovider.Message{
		Role:    "user",
		Content: RenderUserTemplate(template, inboundText, sender),
	})
	return messages
}
