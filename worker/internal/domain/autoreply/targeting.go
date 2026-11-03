package autoreply

// IsTargeted returns (allowed, reason) for a given message and target list.
//
// Machine-readable reasons:
// - not_private
// - chat_muted (TargetModeEveryone, entry present and disabled)
// - target_everyone (TargetModeEveryone, allowed)
// - chat_excepted (TargetModeAllExcept, entry present and enabled)
// - target_all_except (TargetModeAllExcept, allowed)
// - chat_enabled (TargetModeSelectedOnly / Manual, entry present and enabled)
// - chat_not_selected (TargetModeSelectedOnly / Manual, entry absent or disabled)
func IsTargeted(mode TargetMode, targets []*ContactTarget, msg *IncomingMessage) (bool, string) {
	if msg == nil || msg.ChatType != "private" {
		return false, "not_private"
	}
	mode = CoerceTargetMode(string(mode))
	wanted := NormalizeChatID(msg.ChatID)

	var entry *ContactTarget
	for _, t := range targets {
		if t != nil && NormalizeChatID(t.ChatID) == wanted {
			entry = t
			break
		}
	}

	switch mode {
	case TargetModeEveryone:
		if entry != nil && !entry.IsEnabled {
			return false, "chat_muted"
		}
		return true, "target_everyone"

	case TargetModeAllExcept:
		// The list is the exception list: an enabled row means "do not answer this one".
		if entry != nil && entry.IsEnabled {
			return false, "chat_excepted"
		}
		return true, "target_all_except"

	case TargetModeSelectedOnly, TargetModeManual:
		// Only an explicitly enabled chat is answered.
		if entry != nil && entry.IsEnabled {
			return true, "chat_enabled"
		}
		return false, "chat_not_selected"

	default:
		return true, "target_everyone"
	}
}
