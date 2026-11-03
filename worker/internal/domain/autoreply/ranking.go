package autoreply

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// Specificity computes the tie-breaker score: how many independent conditions the rule states.
func (r *AutoReplyRule) Specificity() int {
	if r == nil || r.Conditions == nil {
		return 0
	}
	score := 0
	for _, val := range r.Conditions {
		if val == nil {
			continue
		}
		switch v := val.(type) {
		case []string:
			if len(v) > 0 {
				score++
			}
		case []any:
			if len(v) > 0 {
				score++
			}
		case bool:
			if v {
				score++
			}
		case int, int64, float64:
			if fmt.Sprintf("%v", v) != "0" {
				score++
			}
		case string:
			if strings.TrimSpace(v) != "" {
				score++
			}
		}
	}
	return score
}

// Matches returns true when this rule's conditions hold and no exclusion fires.
func (r *AutoReplyRule) Matches(msg *IncomingMessage, historyCount int) bool {
	if r == nil || msg == nil {
		return false
	}
	text := msg.Text
	lowered := strings.ToLower(text)
	cond := r.Conditions
	if cond == nil {
		cond = make(map[string]any)
	}

	if raw, ok := cond["chat_ids"]; ok {
		wanted := asStringSlice(raw)
		if len(wanted) > 0 {
			targetChat := NormalizeChatID(msg.ChatID)
			found := false
			for _, c := range wanted {
				if NormalizeChatID(c) == targetChat {
					found = true
					break
				}
			}
			if !found {
				return false
			}
		}
	}

	if raw, ok := cond["keywords_any"]; ok {
		keywords := asStringSlice(raw)
		if len(keywords) > 0 {
			found := false
			for _, kw := range keywords {
				if strings.Contains(lowered, strings.ToLower(kw)) {
					found = true
					break
				}
			}
			if !found {
				return false
			}
		}
	}

	if raw, ok := cond["keywords_all"]; ok {
		keywords := asStringSlice(raw)
		if len(keywords) > 0 {
			for _, kw := range keywords {
				if !strings.Contains(lowered, strings.ToLower(kw)) {
					return false
				}
			}
		}
	}

	if raw, ok := cond["regex_any"]; ok {
		patterns := asStringSlice(raw)
		for _, pat := range patterns {
			re, err := regexp.Compile("(?i)" + pat)
			if err != nil || !re.MatchString(text) {
				return false
			}
		}
	}

	if raw, ok := cond["media_types"]; ok {
		mediaTypes := asStringSlice(raw)
		if len(mediaTypes) > 0 {
			actual := strings.ToLower(strings.TrimSpace(msg.MediaType))
			found := false
			for _, m := range mediaTypes {
				if strings.ToLower(strings.TrimSpace(m)) == actual {
					found = true
					break
				}
			}
			if !found {
				return false
			}
		}
	}

	if raw, ok := cond["sender_usernames"]; ok {
		usernames := asStringSlice(raw)
		if len(usernames) > 0 {
			actual := strings.ToLower(strings.TrimPrefix(strings.TrimSpace(msg.SenderUsername), "@"))
			found := false
			for _, u := range usernames {
				if strings.ToLower(strings.TrimPrefix(strings.TrimSpace(u), "@")) == actual {
					found = true
					break
				}
			}
			if !found {
				return false
			}
		}
	}

	if raw, ok := cond["min_length"]; ok {
		minLen := toInt(raw)
		if minLen > 0 && len(text) < minLen {
			return false
		}
	}
	if raw, ok := cond["max_length"]; ok {
		maxLen := toInt(raw)
		if maxLen > 0 && len(text) > maxLen {
			return false
		}
	}

	if raw, ok := cond["is_first_message"]; ok && toBool(raw) {
		if historyCount > 0 {
			return false
		}
	}

	// Exclusions
	exc := r.Exclusions
	if exc != nil {
		if raw, ok := exc["chat_ids"]; ok {
			excludeChats := asStringSlice(raw)
			if len(excludeChats) > 0 {
				targetChat := NormalizeChatID(msg.ChatID)
				for _, c := range excludeChats {
					if NormalizeChatID(c) == targetChat {
						return false
					}
				}
			}
		}

		if raw, ok := exc["sender_ids"]; ok {
			excludeSenders := asStringSlice(raw)
			if len(excludeSenders) > 0 && msg.SenderID != "" {
				for _, s := range excludeSenders {
					if s == msg.SenderID {
						return false
					}
				}
			}
		}

		if raw, ok := exc["keywords_any"]; ok {
			excludeKeywords := asStringSlice(raw)
			for _, kw := range excludeKeywords {
				if strings.Contains(lowered, strings.ToLower(kw)) {
					return false
				}
			}
		}

		if raw, ok := exc["regex_any"]; ok {
			excludePatterns := asStringSlice(raw)
			for _, pat := range excludePatterns {
				re, err := regexp.Compile("(?i)" + pat)
				if err == nil && re.MatchString(text) {
					return false
				}
			}
		}

		if raw, ok := exc["commands"]; ok {
			commands := asStringSlice(raw)
			if len(commands) > 0 && strings.HasPrefix(lowered, "/") {
				parts := strings.Fields(lowered)
				if len(parts) > 0 {
					cmd := strings.TrimPrefix(parts[0], "/")
					cmd = strings.Split(cmd, "@")[0]
					for _, c := range commands {
						cand := strings.TrimPrefix(strings.ToLower(c), "/")
						cand = strings.Split(cand, "@")[0]
						if cmd == cand {
							return false
						}
					}
				}
			}
		}

		if raw, ok := exc["forwarded"]; ok && toBool(raw) {
			if msg.IsForwarded {
				return false
			}
		}
	}

	return true
}

// RankRules deterministically sorts rules: priority DESC, specificity DESC, created ASC, id ASC.
func RankRules(rules []*AutoReplyRule, msg *IncomingMessage, historyCount int) []*AutoReplyRule {
	var matched []*AutoReplyRule
	for _, r := range rules {
		if r != nil && r.IsEnabled && r.Matches(msg, historyCount) {
			matched = append(matched, r)
		}
	}
	sort.SliceStable(matched, func(i, j int) bool {
		a, b := matched[i], matched[j]
		if a.Priority != b.Priority {
			return a.Priority > b.Priority // DESC
		}
		aSpec, bSpec := a.Specificity(), b.Specificity()
		if aSpec != bSpec {
			return aSpec > bSpec // DESC
		}
		if a.CreatedAt != b.CreatedAt {
			return a.CreatedAt < b.CreatedAt // ASC
		}
		return a.ID < b.ID // ASC
	})
	return matched
}

func toInt(v any) int {
	if v == nil {
		return 0
	}
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	case string:
		var out int
		fmt.Sscanf(n, "%d", &out)
		return out
	default:
		return 0
	}
}

func toBool(v any) bool {
	if v == nil {
		return false
	}
	switch b := v.(type) {
	case bool:
		return b
	case int, int64, float64:
		return b != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(b))
		return s == "1" || s == "true" || s == "yes" || s == "on"
	default:
		return false
	}
}
