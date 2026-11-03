// Package routing is the Go Worker's port of the Python Core's routing and
// anti-loop decisions: _chat_key normalisation, ForwardRule.matches_source,
// ForwardRule.all_targets and SmartRoutingEngine.check_loop
// (core/domain/entities.py, core/domain/services.py).
//
// These decisions used to run only on the Python ingest side, before any
// delivery job existed. The Worker re-runs them at delivery time for the
// same reason it re-checks the task itself: a job can sit in the queue
// while its owner re-points the task's sources or targets, and the target
// that was fine at enqueue time can be the source chat by the time the
// worker dispatches — which is exactly the loop the Python engine exists
// to prevent.
//
// The port is deliberately line-for-line with the Python source: the
// reason strings (target_matches_source, target_matches_forward_origin,
// intermediate_target_is_origin) are the user-facing vocabulary the Python
// engine emits, and the dashboard/localization layer translates them, so a
// Go side that invented its own words would report a different reason for
// the same decision.
package routing

import (
	"strconv"
	"strings"

	"atf/worker/internal/domain/rule"
)

// Input is the message snapshot CheckLoop decides against. It carries only
// the three fields the Python check_loop reads off the MessagePayload: the
// source chat id, the source chat's @username, and the forward origin
// ("Forwarded from <channel>" header). Everything else a payload carries is
// irrelevant to loop detection, and a wider struct would invite the caller
// to fill fields the decision never reads.
//
// Username is carried separately from ChatID because Telegram spells a
// public chat two ways (id and @username) and the Python engine compares
// both against the target — matching only the numeric id lets a loop run
// through the username spelling undetected.
type Input struct {
	// ChatID is the source chat id of the message being routed.
	ChatID string
	// ChatUsername is the source chat's @username, "" for private chats.
	ChatUsername string
	// ForwardOrigin is the "Forwarded from <chat>" header: a map with a
	// "from_chat_id" key, or nil when the message is not a forward. Kept as
	// a map because the durable payload snapshot stores it as one, and a
	// struct here would force a second representation of the same JSON.
	ForwardOrigin map[string]any
}

// reason strings mirror SmartRoutingEngine.check_loop verbatim
// (services.py:508). They are user-facing (the pipeline logs them and the
// i10n layer translates the loop_detected:* prefix), so a Go port that
// spelled them differently would report different reasons for the same
// decisions.
const (
	// ReasonTargetMatchesSource: cannot forward back to the exact same chat.
	ReasonTargetMatchesSource = "target_matches_source"
	// ReasonTargetMatchesForwardOrigin: cannot forward back to the chat a
	// forwarded message originally came from.
	ReasonTargetMatchesForwardOrigin = "target_matches_forward_origin"
	// ReasonIntermediateTargetIsOrigin: the branding channel C is the target
	// and the message originated in C.
	ReasonIntermediateTargetIsOrigin = "intermediate_target_is_origin"
)

// ChatKey normalises a chat id so every spelling of it compares equal.
//
// "-100123" as an int, as a float and as a string is one chat; an
// "@username" is passed through unchanged. Mirrors core/domain/entities.py
// _chat_key verbatim: it is the equality basis for per-source and
// per-target matching, so two engines that normalise differently simply
// disagree about whether two chats are the same chat.
func ChatKey(chatID string) string {
	raw := strings.TrimSpace(chatID)
	if raw == "" {
		return ""
	}
	// int(float(raw)) is the Python spelling: a float string like
	// "-100123.0" normalises to "-100123", matching the Python side's
	// behaviour for the JSON-decoded ids it receives.
	if f, err := strconv.ParseFloat(raw, 64); err == nil {
		// A value outside int64 range is not a chat id the pipeline can
		// produce; fall through to the raw string rather than silently
		// collapsing it onto an unrelated id.
		if f >= float64(-9223372036854775808) && f <= float64(9223372036854775807) {
			return strconv.FormatInt(int64(f), 10)
		}
	}
	return raw
}

// same is _same from ForwardRule.matches_source (entities.py:1976): two
// chat references are the same chat when their normalised keys agree, or —
// for usernames, which arrive with and without the @ and whose case never
// matters — when their username spellings compare equal case-insensitively.
//
// The username arm is intentionally NOT symmetric in what it strips: the
// Python _same strips whitespace only from the first argument
// (str(a).strip().lstrip("@").lower()) and lowercases both, which is the
// exact arm a @username-configured task relies on. Reproducing the
// asymmetry matters: a value the Python side would not have matched must
// not match here either.
func same(a, b string) bool {
	keyA, keyB := ChatKey(a), ChatKey(b)
	if keyA != "" && keyA == keyB {
		return true
	}
	left := strings.ToLower(strings.TrimPrefix(strings.TrimSpace(a), "@"))
	right := strings.ToLower(strings.TrimPrefix(b, "@"))
	return left == right
}

// MatchesSource reports whether chatID is one of the task's sources.
//
// Port of ForwardRule.matches_source (entities.py:1967). A task may route
// several sources to several targets (x,y,z -> a,b); the extra sources
// live in the rule's detection_criteria["source_chat_ids"] while
// source_chat_id mirrors the first one for legacy clients, and
// source_chat_name carries a @username the task was configured by. All
// three are checked here: matching only the mirrored field silently
// starved every source but the first, which is the bug the Python
// property exists to prevent.
//
// An inactive task matches nothing — the Python method returns False
// before any comparison, and the delivery preflight reaches this after its
// own IsActive gate, but the check is kept so a direct caller cannot get a
// different answer than the Python engine gives.
func MatchesSource(r *rule.ForwardRule, chatID string) bool {
	if r == nil || !r.IsActive {
		return false
	}
	cid := strings.TrimSpace(chatID)
	if cid == "" {
		return false
	}
	if r.SourceChatID != "" && same(r.SourceChatID, cid) {
		return true
	}
	for _, candidate := range r.SourceChatIDs() {
		if same(candidate, cid) {
			return true
		}
	}
	if r.SourceChatName != "" && same(r.SourceChatName, cid) {
		return true
	}
	return false
}

// CheckLoop is the anti-loop guarantee for one target.
//
// Port of SmartRoutingEngine.check_loop (services.py:508), the shared
// decision the Python pipeline, dispatcher and dry-run simulation all
// consult. A Go delivery path that skipped it could forward a chat into
// itself, which is how a forwarding loop fills a channel with its own
// echo forever.
//
// Returns (true, reason) when delivering to targetChatID would create a
// loop and (false, "") when it would not. Per-target and never
// all-or-nothing: one task fans out to many targets, so a single looping
// target is skipped by the caller (the Python pipeline's LOOP_SKIP) while
// the remaining targets still receive the message.
func CheckLoop(in Input, r *rule.ForwardRule, targetChatID string) (bool, string) {
	cid := strings.TrimSpace(in.ChatID)
	tid := strings.TrimSpace(targetChatID)

	// Cannot forward back to the exact same chat — by raw id equality, or
	// by the username spelling of the same chat (chat_username vs target
	// id, both stripped of their @ and lowercased). NB: this arm compares
	// the payload's raw chat id, with NO _chat_key normalisation —
	// "-1001.0" and "-1001" are therefore NOT the same chat here, while
	// matches_source's _same says they are. That is the Python engine's
	// own asymmetry (check_loop: cid == tid vs _chat_key in _same), kept
	// verbatim: the loop gate runs on ids the pipeline normalised long
	// before enqueue, and broadening it to normalised equality would skip
	// a delivery the Python path makes.
	if cid == tid || (in.ChatUsername != "" &&
		strings.TrimPrefix(strings.ToLower(in.ChatUsername), "@") == strings.TrimPrefix(strings.ToLower(tid), "@")) {
		return true, ReasonTargetMatchesSource
	}

	// Cannot forward back to the original forward source.
	if in.ForwardOrigin != nil {
		if raw, ok := in.ForwardOrigin["from_chat_id"]; ok {
			if fwd, ok := raw.(string); ok {
				if fcid := strings.TrimSpace(fwd); fcid != "" && fcid == tid {
					return true, ReasonTargetMatchesForwardOrigin
				}
			}
		}
	}

	// If the intermediate channel is the target, the message must not have
	// originated there — or the branding hop would post C's own message
	// back into C.
	if r != nil {
		if inter := strings.TrimSpace(r.IntermediateChannelID); inter != "" && tid == inter {
			if cid == inter {
				return true, ReasonIntermediateTargetIsOrigin
			}
			if in.ForwardOrigin != nil {
				if raw, ok := in.ForwardOrigin["from_chat_id"]; ok {
					if fwd, ok := raw.(string); ok {
						if strings.TrimSpace(fwd) == inter {
							return true, ReasonIntermediateTargetIsOrigin
						}
					}
				}
			}
		}
	}
	return false, ""
}

// AllTargets is every target chat id for a task, primary first, deduped.
//
// Thin wrapper over rule.ForwardRule.AllTargets so the routing package's
// callers stay on the projection's own accessor — the projection and the
// decision it feeds both read the pair (primary, list) the same way, and
// keeping one implementation in the rule package prevents a second,
// drifting copy.
func AllTargets(r *rule.ForwardRule) []string {
	return r.AllTargets()
}

// EveryTargetLoops reports whether a delivery would be *entirely* loops.
//
// Port of evaluate_rule's loop arm (services.py:575): the rule is rejected
// only when every target would loop, because a single looping target is
// skipped by the fan-out rather than failing the message. The reason
// returned is the first target's reason, exactly as the Python engine
// returns check_loop(payload, rule, targets[0])[1] after counting.
//
// Used by the enqueue-side parity surface; the delivery preflight works
// per target and calls CheckLoop directly.
func EveryTargetLoops(in Input, r *rule.ForwardRule) (bool, string) {
	targets := make([]string, 0, 1+len(AllTargets(r)))
	for _, tid := range AllTargets(r) {
		if strings.TrimSpace(tid) != "" {
			targets = append(targets, tid)
		}
	}
	if len(targets) == 0 {
		return false, ""
	}
	loops := 0
	for _, tid := range targets {
		isLoop, _ := CheckLoop(in, r, tid)
		if isLoop {
			loops++
		}
	}
	if loops == len(targets) {
		_, reason := CheckLoop(in, r, targets[0])
		return true, reason
	}
	return false, ""
}
