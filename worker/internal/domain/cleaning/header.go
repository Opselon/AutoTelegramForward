package cleaning

import (
	"strings"
	"time"
)

// header.go — the HeaderFormatter port (core/domain/services.py:320-389).
//
// HeaderFormatter interpolates a task's header/footer template against the
// message it was configured for: the source channel's title and link, the
// post's date, the sender, the rule's own name. It is the only transform
// stage that reads message METADATA rather than the message text, which is
// why it is a separate pass from the text-only pipeline.
//
// THE DEFECT THIS FILE FIXES (finding F-15, "real customer impact"): the
// Python durable path applies the header RAW
// (pipeline.py:1547-1552 — "From [[chat_title]]" stays literal), while
// only the gRPC TestRule preview and the edit-sync path run the formatter
// (services.py:307-309). An owner writing {source_link} sees it posted
// literally while the in-product preview shows it expanded. Stage 4.4
// moves the interpolation onto the path the Worker owns, so the durable
// delivery renders what the preview promised — and the preview, which ran
// the same code on the Python side, keeps agreeing because the renderer
// is this one port, not a second implementation.
//
// All placeholders render to a string; an unavailable value renders empty
// rather than leaving the placeholder visible. An unknown placeholder is
// left in place (Python's loop only replaces the keys it knows), so a
// typo stays visible to the owner instead of silently evaporating.

// HeaderVars is the message-level input set the renderer reads. It is a
// snapshot of what Python's MessagePayload carried at ingest time; the
// delivery engine builds it from the durable job payload so a restart
// renders the same header the enqueue path would have rendered.
type HeaderVars struct {
	SourceChatID       string
	SourceChatUsername string
	SourceChatTitle    string
	SourceMessageID    int64
	SourceDate         int64
	SenderName         string
	SenderID           string
	EffectiveText      string
	// ForwardOrigin carries the "forwarded from" chat id when the source
	// message is itself a forward; "" otherwise.
	ForwardFromChat string
}

// HeaderTask is the task-level input set: the rule's own identity.
type HeaderTask struct {
	Name     string
	Category string
}

// FormatHeader renders one header/footer template against the message and
// task variables, returning the expanded text ("" for an empty template).
//
// Every placeholder accepts both the {name} and the [[name]] spelling,
// exactly as Python's loop does both replacements per variable.
func FormatHeader(template string, task HeaderTask, vars HeaderVars) string {
	if strings.TrimSpace(template) == "" {
		return ""
	}

	now := time.Now().UTC()
	dateStr := now.Format("2006-01-02 15:04 UTC")
	postDate := dateStr
	if vars.SourceDate > 0 {
		postDate = time.Unix(vars.SourceDate, 0).UTC().Format("2006-01-02 15:04 UTC")
	}

	sourceLink := ""
	username := strings.TrimLeft(vars.SourceChatUsername, "@")
	if username != "" && vars.SourceMessageID > 0 {
		sourceLink = "https://t.me/" + username + "/" + formatInt64(vars.SourceMessageID)
	} else if strings.HasPrefix(vars.SourceChatID, "-100") && vars.SourceMessageID > 0 {
		// A private channel has no @username, so Telegram exposes its posts
		// through the /c/<internal id> form.
		sourceLink = "https://t.me/c/" + strings.TrimPrefix(vars.SourceChatID, "-100") + "/" + formatInt64(vars.SourceMessageID)
	}

	senderName := vars.SenderName
	chatTitle := vars.SourceChatTitle
	if chatTitle == "" {
		chatTitle = vars.SourceChatUsername
	}
	if chatTitle == "" {
		chatTitle = vars.SourceChatID
	}
	originUsername := ""
	if vars.SourceChatUsername != "" {
		originUsername = "@" + strings.TrimLeft(vars.SourceChatUsername, "@")
	}
	forwardFromChat := vars.ForwardFromChat
	if forwardFromChat == "" {
		forwardFromChat = vars.SourceChatID
	}

	ruleName := task.Name
	if strings.TrimSpace(ruleName) == "" {
		// Python's fallback is f"Rule-{rule.id[:8]}". The id is not on the
		// HeaderTask because the rule's display name is the property the
		// owner configured; the caller sets the fallback when it prefers
		// the id-derived label.
		ruleName = ""
	}
	category := task.Category
	if strings.TrimSpace(category) == "" {
		// Python's ForwardRule entity defaults message_category to "ALL",
		// and the placeholder renders that default — not empty. Keeping
		// the same value keeps the Go rendering byte-identical.
		category = "ALL"
	}

	nameURL := chatTitle
	if sourceLink != "" {
		nameURL = "[" + chatTitle + "](" + sourceLink + ")"
	}
	fromUser := senderName
	if fromUser == "" {
		fromUser = vars.SenderID
	}

	// Key order is not load-bearing: each replacement is a plain string
	// substitution over the running result, and no key's expansion can
	// contain another key's placeholder braces (every value is a fully
	// rendered string built above).
	table := [][2]string{
		{"rule_name", ruleName},
		{"category", category},
		{"date", postDate},
		{"source_link", sourceLink},
		{"sender_name", senderName},
		{"chat_title", chatTitle},
		{"source_chat", vars.SourceChatID},
		{"ORIGIN_TEXT", vars.EffectiveText},
		{"ORIGIN_NAME", chatTitle},
		{"ORIGIN_USERNAME", originUsername},
		{"ORIGIN_USERID", vars.SenderID},
		{"ORIGIN_POST_ID", formatInt64(vars.SourceMessageID)},
		{"ORIGIN_CHAT_ID", vars.SourceChatID},
		{"ORIGIN_POST_DATE", postDate},
		{"ORIGIN_NAME_URL", nameURL},
		{"SOURCE_NAME", chatTitle},
		{"SENDER_CHAT", vars.SourceChatID},
		{"FORWARD_FROM_CHAT", forwardFromChat},
		{"FROM_USER", fromUser},
	}

	out := template
	for _, entry := range table {
		if entry[0] == "" {
			continue
		}
		out = strings.ReplaceAll(out, "{"+entry[0]+"}", entry[1])
		out = strings.ReplaceAll(out, "[["+entry[0]+"]]", entry[1])
	}
	return strings.TrimSpace(out)
}

// formatInt64 renders an int64 without the leading zeros or exponent the
// generic %v might produce for large message ids.
func formatInt64(n int64) string {
	if n == 0 {
		return ""
	}
	return strings.TrimLeft(formatInt(n), "0")
}

// formatInt is the integer-to-string primitive, kept tiny so a reader can
// see there is no formatting policy hiding in it.
func formatInt(n int64) string {
	if n == 0 {
		return "0"
	}
	negative := n < 0
	if negative {
		n = -n
	}
	digits := make([]byte, 0, 20)
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	if negative {
		digits = append([]byte{'-'}, digits...)
	}
	return string(digits)
}
