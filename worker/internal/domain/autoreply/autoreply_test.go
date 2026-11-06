package autoreply

import (
	"strings"
	"testing"
	"time"
)

func TestNormalizationAndDefaults(t *testing.T) {
	s := DefaultSettings(42)
	if s.OwnerUserID != 42 || s.CooldownSeconds != 15 || s.MaxConcurrency != 3 {
		t.Fatalf("unexpected defaults: %+v", s)
	}

	s.Temperature = 5.0
	s.MaxTokens = 99999
	s.QuietHoursStart = -5
	s.QuietHoursEnd = 5
	s.ActiveDays = []int{1, 3, 1, 9, -1, 5}
	s.Normalize()

	if s.Temperature != 2.0 {
		t.Errorf("expected clamped temp 2.0, got %f", s.Temperature)
	}
	if s.MaxTokens != 4000 {
		t.Errorf("expected clamped max tokens 4000, got %d", s.MaxTokens)
	}
	if s.QuietHoursStart != -1 || s.QuietHoursEnd != -1 {
		t.Errorf("expected invalid quiet window reset to -1, got %d..%d", s.QuietHoursStart, s.QuietHoursEnd)
	}
	if len(s.ActiveDays) != 3 || s.ActiveDays[0] != 1 || s.ActiveDays[1] != 3 || s.ActiveDays[2] != 5 {
		t.Errorf("expected sorted unique active days [1 3 5], got %v", s.ActiveDays)
	}
}

func TestQuietHoursAndActiveDays(t *testing.T) {
	s := DefaultSettings(1)
	s.TimezoneOffsetMinutes = 0 // UTC
	s.QuietHoursStart = 22
	s.QuietHoursEnd = 6 // crosses midnight

	t1 := time.Date(2026, 11, 3, 23, 15, 0, 0, time.UTC)
	if !s.IsQuietNow(t1) {
		t.Errorf("expected 23:15 to be quiet in window 22..6")
	}

	t2 := time.Date(2026, 11, 3, 4, 30, 0, 0, time.UTC)
	if !s.IsQuietNow(t2) {
		t.Errorf("expected 04:30 to be quiet in window 22..6")
	}

	t3 := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	if s.IsQuietNow(t3) {
		t.Errorf("expected 12:00 to NOT be quiet in window 22..6")
	}

	// Active days: Monday=0, Tuesday=1 ... Sunday=6
	// 2026-11-03 is Tuesday (tm_wday = 1)
	s.ActiveDays = []int{0, 2} // Mon, Wed only
	if s.IsActiveDay(t3) {
		t.Errorf("expected Tuesday to be inactive when active_days=[0, 2]")
	}
	s.ActiveDays = []int{1, 4} // Tue, Fri
	if !s.IsActiveDay(t3) {
		t.Errorf("expected Tuesday to be active when active_days=[1, 4]")
	}
}

func TestRuleMatchingAndRanking(t *testing.T) {
	rule1 := &AutoReplyRule{
		ID:        "rule-1",
		Priority:  10,
		Action:    RuleActionReply,
		IsEnabled: true,
		Conditions: map[string]any{
			"keywords_any": []string{"pricing", "cost"},
		},
		CreatedAt: 100,
	}
	rule2 := &AutoReplyRule{
		ID:        "rule-2",
		Priority:  20,
		Action:    RuleActionSkip,
		IsEnabled: true,
		Conditions: map[string]any{
			"keywords_any": []string{"urgent", "pricing"},
		},
		CreatedAt: 200,
	}
	rule3 := &AutoReplyRule{
		ID:        "rule-3",
		Priority:  10,
		Action:    RuleActionReply,
		IsEnabled: true,
		Conditions: map[string]any{
			"keywords_any": []string{"pricing"},
			"min_length":   5,
		},
		CreatedAt: 50,
	}

	msg := &IncomingMessage{
		ChatID:    "123",
		Text:      "what is the pricing urgent?",
		ChatType:  "private",
		MediaType: "text",
	}

	ranked := RankRules([]*AutoReplyRule{rule1, rule2, rule3}, msg, 0)
	if len(ranked) != 3 {
		t.Fatalf("expected 3 matched rules, got %d", len(ranked))
	}
	// rule2 has priority 20 -> must be first
	if ranked[0].ID != "rule-2" {
		t.Errorf("expected top rule to be rule-2 (priority 20), got %s", ranked[0].ID)
	}
	// between rule1 and rule3: both priority 10, but rule3 has 2 conditions (specificity 2 > 1)
	if ranked[1].ID != "rule-3" {
		t.Errorf("expected 2nd rule to be rule-3 (higher specificity), got %s", ranked[1].ID)
	}
	if ranked[2].ID != "rule-1" {
		t.Errorf("expected 3rd rule to be rule-1, got %s", ranked[2].ID)
	}
}

func TestRuleExclusions(t *testing.T) {
	rule := &AutoReplyRule{
		ID:        "r-exc",
		Priority:  10,
		Action:    RuleActionReply,
		IsEnabled: true,
		Conditions: map[string]any{
			"keywords_any": []string{"hello"},
		},
		Exclusions: map[string]any{
			"commands": []string{"start", "help"},
			"regex_any": []string{`^stop`},
		},
	}

	msg1 := &IncomingMessage{Text: "hello there", ChatType: "private"}
	if !rule.Matches(msg1, 0) {
		t.Errorf("msg1 should match")
	}

	msg2 := &IncomingMessage{Text: "/start@bot hello", ChatType: "private"}
	if rule.Matches(msg2, 0) {
		t.Errorf("msg2 should be excluded by command /start")
	}

	msg3 := &IncomingMessage{Text: "stop hello", ChatType: "private"}
	if rule.Matches(msg3, 0) {
		t.Errorf("msg3 should be excluded by regex ^stop")
	}
}

func TestTargeting(t *testing.T) {
	targetMuted := &ContactTarget{ChatID: "100", IsEnabled: false}
	targetEnabled := &ContactTarget{ChatID: "200", IsEnabled: true}
	targets := []*ContactTarget{targetMuted, targetEnabled}

	// EVERYONE
	allowed, reason := IsTargeted(TargetModeEveryone, targets, &IncomingMessage{ChatID: "100", ChatType: "private"})
	if allowed || reason != "chat_muted" {
		t.Errorf("expected chat_muted, got allowed=%v reason=%s", allowed, reason)
	}
	allowed, reason = IsTargeted(TargetModeEveryone, targets, &IncomingMessage{ChatID: "300", ChatType: "private"})
	if !allowed || reason != "target_everyone" {
		t.Errorf("expected target_everyone, got allowed=%v reason=%s", allowed, reason)
	}

	// ALL_EXCEPT
	allowed, reason = IsTargeted(TargetModeAllExcept, targets, &IncomingMessage{ChatID: "200", ChatType: "private"})
	if allowed || reason != "chat_excepted" {
		t.Errorf("expected chat_excepted, got allowed=%v reason=%s", allowed, reason)
	}
	allowed, reason = IsTargeted(TargetModeAllExcept, targets, &IncomingMessage{ChatID: "300", ChatType: "private"})
	if !allowed || reason != "target_all_except" {
		t.Errorf("expected target_all_except, got allowed=%v reason=%s", allowed, reason)
	}

	// SELECTED_ONLY
	allowed, reason = IsTargeted(TargetModeSelectedOnly, targets, &IncomingMessage{ChatID: "200", ChatType: "private"})
	if !allowed || reason != "chat_enabled" {
		t.Errorf("expected chat_enabled, got allowed=%v reason=%s", allowed, reason)
	}
	allowed, reason = IsTargeted(TargetModeSelectedOnly, targets, &IncomingMessage{ChatID: "300", ChatType: "private"})
	if allowed || reason != "chat_not_selected" {
		t.Errorf("expected chat_not_selected, got allowed=%v reason=%s", allowed, reason)
	}
}

func TestDecideGates(t *testing.T) {
	now := time.Unix(1700000000, 0)
	settings := DefaultSettings(1)
	settings.Enabled = true

	// Gate 1: disabled
	sOff := DefaultSettings(1)
	sOff.Enabled = false
	allowed, reason, _ := Decide(sOff, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, 0, 0, now)
	if allowed || reason != "disabled" {
		t.Errorf("gate 1 disabled failed: %v %s", allowed, reason)
	}

	// Gate 2: from self
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private", IsSelf: true}, 0, 0, 0, now)
	if allowed || reason != "from_self" {
		t.Errorf("gate 2 from_self failed: %v %s", allowed, reason)
	}

	// Gate 3: from bot
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private", IsBot: true}, 0, 0, 0, now)
	if allowed || reason != "from_bot" {
		t.Errorf("gate 3 from_bot failed: %v %s", allowed, reason)
	}

	// Gate 4: not private
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "hi", ChatType: "supergroup"}, 0, 0, 0, now)
	if allowed || reason != "not_private" {
		t.Errorf("gate 4 not_private failed: %v %s", allowed, reason)
	}

	// Gate 5: empty text
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "   ", ChatType: "private"}, 0, 0, 0, now)
	if allowed || reason != "empty_text" {
		t.Errorf("gate 5 empty_text failed: %v %s", allowed, reason)
	}

	// Gate 6: command
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "/help", ChatType: "private"}, 0, 0, 0, now)
	if allowed || reason != "command" {
		t.Errorf("gate 6 command failed: %v %s", allowed, reason)
	}

	// Gate 7: inactive day. IsActiveDay reads the account-offset day (the
	// default offset is 0 → UTC): derive the "different day" from the same
	// clock the model reads, or the gate flips depending on the machine's
	// local timezone.
	utcNow := now.UTC()
	sDay := DefaultSettings(1)
	sDay.Enabled = true
	sDay.ActiveDays = []int{((int(utcNow.Weekday()) + 6) % 7 + 1) % 7} // explicitly different day
	allowed, reason, _ = Decide(sDay, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, 0, 0, now)
	if allowed || reason != "inactive_day" {
		t.Errorf("gate 7 inactive_day failed: %v %s", allowed, reason)
	}

	// Gate 8: quiet hours. IsQuietNow reads the hour through the account's
	// timezone offset (0 by default → UTC), not the machine's local zone:
	// write the window in that same clock or the test only passes on UTC
	// hosts.
	sQuiet := DefaultSettings(1)
	sQuiet.Enabled = true
	sQuiet.QuietHoursStart = utcNow.Hour()
	sQuiet.QuietHoursEnd = (utcNow.Hour() + 2) % 24
	allowed, reason, _ = Decide(sQuiet, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, 0, 0, now)
	if allowed || reason != "quiet_hours" {
		t.Errorf("gate 8 quiet_hours failed: %v %s", allowed, reason)
	}

	// Gate 9: rule skip
	skipRule := &AutoReplyRule{
		ID:        "skip-1",
		Priority:  100,
		Action:    RuleActionSkip,
		IsEnabled: true,
	}
	allowed, reason, r := Decide(settings, nil, []*AutoReplyRule{skipRule}, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, 0, 0, now)
	if allowed || reason != "rule_skip:skip-1" || r == nil {
		t.Errorf("gate 9 rule_skip failed: %v %s", allowed, reason)
	}

	// Gate 10: cooldown
	settings.CooldownSeconds = 30
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, now.Unix()-10, 0, now)
	if allowed || reason != "cooldown" {
		t.Errorf("gate 10 cooldown failed: %v %s", allowed, reason)
	}

	// Gate 11: daily cap
	settings.CooldownSeconds = 0
	settings.DailyLimitPerChat = 5
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, 0, 5, now)
	if allowed || reason != "daily_limit" {
		t.Errorf("gate 11 daily_limit failed: %v %s", allowed, reason)
	}

	// Gate 12: eligible!
	allowed, reason, _ = Decide(settings, nil, nil, &IncomingMessage{Text: "hi", ChatType: "private"}, 0, 0, 2, now)
	if !allowed || reason != "eligible" {
		t.Errorf("gate 12 eligible failed: %v %s", allowed, reason)
	}
}

func TestContextBuildingAndRedaction(t *testing.T) {
	secretText := "my api key is sk-1234567890abcdef and my card is 4111222233334444 and Bearer my-secret-token-1234"
	redacted := RedactForPrompt(secretText)
	if strings.Contains(redacted, "sk-1234567890abcdef") {
		t.Errorf("API token not redacted: %s", redacted)
	}
	if strings.Contains(redacted, "4111222233334444") {
		t.Errorf("card not redacted: %s", redacted)
	}
	if strings.Contains(redacted, "my-secret-token-1234") {
		t.Errorf("bearer token not redacted: %s", redacted)
	}

	// Detect Language
	if DetectLanguage("سلام حال شما چطوره؟") != "fa" {
		t.Errorf("expected fa for Persian text")
	}
	if DetectLanguage("Привет, как дела?") != "ru" {
		t.Errorf("expected ru for Russian text")
	}
	if DetectLanguage("Hello, how are you?") != "en" {
		t.Errorf("expected en for English text")
	}

	// Prompt template
	rendered := RenderUserTemplate("From {sender}:\n{text}", "test body", "Alice")
	if rendered != "From Alice:\ntest body" {
		t.Errorf("template render failed: %s", rendered)
	}

	// System prompt bounding
	settings := DefaultSettings(1)
	settings.ExtraInstructions = strings.Repeat("A", 10000)
	sysPrompt := BuildSystemPrompt(settings, nil, nil, []string{"fact 1", "fact 2"}, "en", "hello")
	if len(sysPrompt) > MaxSystemPromptChars+10 {
		t.Errorf("system prompt exceeded max chars: %d", len(sysPrompt))
	}
	if !strings.Contains(sysPrompt, "[…]") {
		t.Errorf("clamped system prompt should contain truncation marker")
	}
}
