package autoreply

import (
	"strings"
	"time"
)

// AutoReplyDecision is the outcome of running one inbound message through the 6 gates.
type AutoReplyDecision struct {
	Reply           bool           `json:"reply"`
	Reason          string         `json:"reason"`
	Rule            *AutoReplyRule `json:"rule,omitempty"`
	PromptID        string         `json:"prompt_id"`
	AIConfigID      string         `json:"ai_config_id"`
	CooldownSeconds int            `json:"cooldown_seconds"`
	CorrelationID   string         `json:"correlation_id"`
	JobID           string         `json:"job_id"`
	Duplicate       bool           `json:"duplicate"`
	LatencyMS       float64        `json:"latency_ms"`
}

// Decide is the pure 6-gate evaluation — no I/O, no DB reads.
//
// Gates in exact order:
// 1. message shape — private, not self, not a bot (if ignore_bots), text not empty, not a command
// 2. account switches — enabled, active day, quiet hours
// 3. targeting — mode + per-chat switch
// 4. rules — ranked deterministically; a SKIP rule stops here
// 5. pacing — cooldown, per-chat daily cap
// 6. returns (allowed, reason, matchedRule)
func Decide(
	settings *AutoReplySettings,
	targets []*ContactTarget,
	rules []*AutoReplyRule,
	message *IncomingMessage,
	historyCount int,
	lastSentAt int64,
	sentToday int,
	now time.Time,
) (bool, string, *AutoReplyRule) {
	if settings == nil || !settings.Enabled {
		return false, "disabled", nil
	}
	if message == nil {
		return false, "empty_text", nil
	}
	if message.IsSelf {
		return false, "from_self", nil
	}
	if message.IsBot && settings.IgnoreBots {
		return false, "from_bot", nil
	}
	if message.ChatType != "private" {
		return false, "not_private", nil
	}
	text := strings.TrimSpace(message.Text)
	if text == "" {
		return false, "empty_text", nil
	}
	if message.IsCommand() {
		return false, "command", nil
	}
	if !settings.IsActiveDay(now) {
		return false, "inactive_day", nil
	}
	if settings.IsQuietNow(now) {
		return false, "quiet_hours", nil
	}

	allowed, targetReason := IsTargeted(settings.TargetMode, targets, message)
	if !allowed {
		return false, targetReason, nil
	}

	var matchedRule *AutoReplyRule
	matched := RankRules(rules, message, historyCount)
	if len(matched) > 0 {
		matchedRule = matched[0]
		if matchedRule.Action == RuleActionSkip {
			return false, "rule_skip:" + matchedRule.ID, matchedRule
		}
	}

	cooldown := settings.CooldownSeconds
	if matchedRule != nil && matchedRule.CooldownSeconds >= 0 {
		cooldown = matchedRule.CooldownSeconds
	}
	ts := now.Unix()
	if cooldown > 0 && lastSentAt > 0 && (ts-lastSentAt) < int64(cooldown) {
		return false, "cooldown", matchedRule
	}
	if settings.DailyLimitPerChat > 0 && sentToday >= settings.DailyLimitPerChat {
		return false, "daily_limit", matchedRule
	}

	return true, "eligible", matchedRule
}
