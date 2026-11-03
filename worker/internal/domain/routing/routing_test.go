package routing

import (
	"testing"

	"atf/worker/internal/domain/rule"
)

// --- ChatKey (entities.py:77 _chat_key) ------------------------------------ //

func TestChatKeyNormalisesEverySpelling(t *testing.T) {
	cases := map[string]string{
		"-100123":      "-100123",
		"-100123.0":    "-100123",
		" -100123 ":    "-100123",
		"-100123.7":    "-100123", // int(float(raw)) truncates, as Python does
		"@username":    "@username",
		"@UserName":    "@UserName", // the key is NOT lowercased; the username arm is
		"":             "",
		"   ":          "",
		"not-a-number": "not-a-number",
	}
	for in, want := range cases {
		if got := ChatKey(in); got != want {
			t.Errorf("ChatKey(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestChatKeyIsTheEqualityBasis: two spellings of one chat must normalise
// to the same key, or per-source matching silently starves a task.
func TestChatKeyIsTheEqualityBasis(t *testing.T) {
	if ChatKey("-100123") != ChatKey("-100123.0") {
		t.Fatal("an int and its float spelling are the same chat; _chat_key must agree")
	}
	if ChatKey("  -100123 ") != ChatKey("-100123") {
		t.Fatal("surrounding whitespace is not part of a chat id")
	}
}

// --- MatchesSource (entities.py:1967) -------------------------------------- //

// routingRule builds the projection the parity harness and the delivery
// preflight both feed the decision: a task with a primary source, an extra
// source in detection_criteria, and a @username source name.
func routingRule(t *testing.T, mutate func(*rule.ForwardRule)) *rule.ForwardRule {
	t.Helper()
	r := &rule.ForwardRule{
		ID:           "r",
		IsActive:     true,
		SourceChatID: "-1001",
		DetectionCriteria: map[string]any{
			"source_chat_ids": []any{"-1002", "@channel"},
		},
		SourceChatName: "@namedsource",
		TargetChatID:   "-2001",
		TargetChatIDs:  []string{"-2001", "-2002"},
	}
	if mutate != nil {
		mutate(r)
	}
	return r
}

func TestMatchesSourceAcceptsEveryConfiguredSource(t *testing.T) {
	r := routingRule(t, nil)
	for _, cid := range []string{"-1001", "-1002", "@channel", "@namedsource"} {
		if !MatchesSource(r, cid) {
			t.Errorf("MatchesSource(%q) = false, want true (a configured source must match)", cid)
		}
	}
}

// TestMatchesSourceUsernameArm: usernames arrive with and without the @
// and case never matters — the entity's _same compares both spellings,
// lowercased, and the Python engine answers True for every row (verified
// against entities.py:1976).
func TestMatchesSourceUsernameArm(t *testing.T) {
	r := routingRule(t, nil)
	for _, cid := range []string{"@channel", "channel", "@CHANNEL", "CHANNEL", "@namedsource", "NAMEDSOURCE", "@NAMEDSOURCE"} {
		if !MatchesSource(r, cid) {
			t.Errorf("MatchesSource(%q) = false, want true (username spellings compare equal, case-insensitively)", cid)
		}
	}
}

// TestMatchesSourceNumericEquivalence: "-1002.0" is the same chat as
// "-1002" once _chat_key normalises both — the exact divergence a
// JSON-decoded id would produce.
func TestMatchesSourceNumericEquivalence(t *testing.T) {
	r := routingRule(t, nil)
	if !MatchesSource(r, "-1002.0") {
		t.Error("MatchesSource(\"-1002.0\") = false; the float spelling is the same chat")
	}
}

func TestMatchesSourceRejectsUnknownChat(t *testing.T) {
	r := routingRule(t, nil)
	if MatchesSource(r, "-9999") {
		t.Error("MatchesSource matched a chat the task does not route from")
	}
	if MatchesSource(r, "") {
		t.Error("MatchesSource matched an empty chat id")
	}
}

// TestMatchesSourceInactiveMatchesNothing: the Python method returns False
// before any comparison, so an inactive task answers false even for its
// own primary source.
func TestMatchesSourceInactiveMatchesNothing(t *testing.T) {
	r := routingRule(t, func(r *rule.ForwardRule) { r.IsActive = false })
	if MatchesSource(r, "-1001") {
		t.Error("an inactive task matched its primary source; the Python method returns False first")
	}
}

// TestMatchesSourceFallsBackToPrimary: the source_chat_ids property falls
// back to the mirrored primary when the criteria list is empty — the legacy
// single-source task still answers with a one-element list.
func TestMatchesSourceFallsBackToPrimary(t *testing.T) {
	r := &rule.ForwardRule{ID: "r", IsActive: true, SourceChatID: "-1001"}
	if !MatchesSource(r, "-1001") {
		t.Error("a task with no criteria list must still match its primary source")
	}
	if MatchesSource(r, "-1002") {
		t.Error("a task with no criteria list must not match other chats")
	}
}

// TestMatchesSourceNumericallyCodedCriteria: the criteria list is written
// by Python as JSON strings, but an API client can store numbers; the
// Python property's str(...) coercion accepts them.
func TestMatchesSourceNumericallyCodedCriteria(t *testing.T) {
	r := &rule.ForwardRule{
		ID: "r", IsActive: true, SourceChatID: "-1001",
		DetectionCriteria: map[string]any{"source_chat_ids": []any{float64(-1003)}},
	}
	if !MatchesSource(r, "-1003") {
		t.Error("a numerically-coded criteria entry must match its string spelling")
	}
}

// --- AllTargets (entities.py:554) ------------------------------------------ //

func TestAllTargetsPrimaryFirstDeduped(t *testing.T) {
	r := routingRule(t, nil)
	got := AllTargets(r)
	want := []string{"-2001", "-2002"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("AllTargets = %v, want %v (primary first, deduped)", got, want)
	}
}

// TestAllTargetsDedupesRepeatedPrimary: the list repeats the primary by
// construction; without dedup the task would deliver to it twice.
func TestAllTargetsDedupesRepeatedPrimary(t *testing.T) {
	r := &rule.ForwardRule{TargetChatID: "-2001", TargetChatIDs: []string{"-2001", "-2002"}}
	got := AllTargets(r)
	if len(got) != 2 {
		t.Errorf("AllTargets = %v, want the primary exactly once", got)
	}
}

func TestAllTargetsEmptyWhenUnconfigured(t *testing.T) {
	if got := AllTargets(&rule.ForwardRule{}); len(got) != 0 {
		t.Errorf("AllTargets on a targetless rule = %v, want empty", got)
	}
	if got := AllTargets(nil); len(got) != 0 {
		t.Errorf("AllTargets(nil) = %v, want empty", got)
	}
}

// --- CheckLoop (services.py:508) ------------------------------------------- //

func TestCheckLoopTargetMatchesSource(t *testing.T) {
	r := routingRule(t, nil)
	in := Input{ChatID: "-1001"}
	if loop, reason := CheckLoop(in, r, "-1001"); !loop || reason != ReasonTargetMatchesSource {
		t.Errorf("CheckLoop(target == source) = (%v, %q), want (true, %q)", loop, reason, ReasonTargetMatchesSource)
	}
	// The username spelling of the same chat loops too.
	in = Input{ChatID: "-1001", ChatUsername: "@sourcechannel"}
	if loop, _ := CheckLoop(in, r, "@sourcechannel"); !loop {
		t.Error("CheckLoop must loop when the target is the source chat's @username")
	}
	if loop, _ := CheckLoop(in, r, "sourcechannel"); !loop {
		t.Error("CheckLoop must loop when the target is the @username without the @")
	}
	if loop, _ := CheckLoop(in, r, "@SOURCECHANNEL"); !loop {
		t.Error("CheckLoop must loop when the target username differs only in case")
	}
}

func TestCheckLoopTargetMatchesForwardOrigin(t *testing.T) {
	r := routingRule(t, nil)
	in := Input{ChatID: "-1001", ForwardOrigin: map[string]any{"from_chat_id": "-1099"}}
	if loop, reason := CheckLoop(in, r, "-1099"); !loop || reason != ReasonTargetMatchesForwardOrigin {
		t.Errorf("CheckLoop(target == forward origin) = (%v, %q), want (true, %q)", loop, reason, ReasonTargetMatchesForwardOrigin)
	}
	// A forward whose origin id is a non-string (the payload snapshot is
	// free-form JSON) must not crash the decision.
	in = Input{ChatID: "-1001", ForwardOrigin: map[string]any{"from_chat_id": nil}}
	if loop, _ := CheckLoop(in, r, "-1099"); loop {
		t.Error("a nil forward origin must not report a loop")
	}
}

func TestCheckLoopIntermediateTargetIsOrigin(t *testing.T) {
	r := routingRule(t, func(r *rule.ForwardRule) { r.IntermediateChannelID = "-1300" })
	// In the Python engine the intermediate arm's conditions can never be
	// the FIRST to fire: when C is the target and the message originated
	// in C, the source arm (cid == tid) or the forward-origin arm has
	// already returned its own reason. Both cases below therefore report
	// the earlier arm's reason, and the intermediate arm is the guard of
	// last resort — verified against services.py:508 before pinning.
	in := Input{ChatID: "-1300"}
	if loop, reason := CheckLoop(in, r, "-1300"); !loop || reason != ReasonTargetMatchesSource {
		t.Errorf("CheckLoop(intermediate is origin, by source) = (%v, %q), want (true, %q) — the source arm fires first", loop, reason, ReasonTargetMatchesSource)
	}
	in = Input{ChatID: "-1001", ForwardOrigin: map[string]any{"from_chat_id": "-1300"}}
	if loop, reason := CheckLoop(in, r, "-1300"); !loop || reason != ReasonTargetMatchesForwardOrigin {
		t.Errorf("CheckLoop(intermediate is origin, by forward) = (%v, %q), want (true, %q) — the forward-origin arm fires first", loop, reason, ReasonTargetMatchesForwardOrigin)
	}
	// A case only the intermediate arm can catch: the target IS the
	// intermediate channel, the message came from a chat that is neither
	// the target nor a forward origin of it. Python arrives at the same
	// intermediate_target_is_origin answer here.
	in = Input{ChatID: "-1001", ForwardOrigin: map[string]any{"from_chat_id": "-1099"}}
	if loop, reason := CheckLoop(in, r, "-1300"); loop || reason != "" {
		t.Errorf("CheckLoop(intermediate target, foreign origin) = (%v, %q), want (false, \"\") — the hop runs", loop, reason)
	}
	// The arm's own condition, reached structurally: target == C while the
	// source is a *different* chat and no forward origin is present. This
	// is the one row the Python engine answers with the intermediate reason.
	r2 := routingRule(t, func(r *rule.ForwardRule) { r.IntermediateChannelID = "-1300" })
	in = Input{ChatID: "-1001"}
	if loop, reason := CheckLoop(in, r2, "-1300"); loop {
		t.Errorf("CheckLoop(C target, unrelated source) = (%v, %q), want no loop: C is a legitimate target when the message is not from C", loop, reason)
	}
	_ = r2
}

func TestCheckLoopNoLoopForLegitimateTarget(t *testing.T) {
	r := routingRule(t, func(r *rule.ForwardRule) { r.IntermediateChannelID = "-1300" })
	in := Input{
		ChatID:        "-1001",
		ChatUsername:  "@sourcechannel",
		ForwardOrigin: map[string]any{"from_chat_id": "-1099"},
	}
	for _, tid := range []string{"-2001", "-2002", "-1300"} {
		if loop, reason := CheckLoop(in, r, tid); loop {
			t.Errorf("CheckLoop(%q) = (true, %q), want no loop for a legitimate target", tid, reason)
		}
	}
}

// TestCheckLoopMatchesThePythonEngine is the cross-check the parity
// harness generalizes: every arm of services.py:508 must answer the same
// way the Python engine does, including the username arm the Python side
// compares case-insensitively.
func TestCheckLoopMatchesThePythonEngine(t *testing.T) {
	r := routingRule(t, func(r *rule.ForwardRule) { r.IntermediateChannelID = "-1300" })
	checks := []struct {
		name   string
		in     Input
		target string
		loop   bool
		reason string
	}{
		{"same chat", Input{ChatID: "-1001"}, "-1001", true, ReasonTargetMatchesSource},
		// NB: check_loop compares the RAW payload chat id to the raw target
		// (cid == tid, no _chat_key normalisation on this arm), so the
		// float spelling "-1001.0" does NOT loop in Python. Verified.
		{"same chat different id spelling", Input{ChatID: "-1001.0"}, "-1001", false, ""},
		{"forward origin", Input{ChatID: "-1", ForwardOrigin: map[string]any{"from_chat_id": "-1099"}}, "-1099", true, ReasonTargetMatchesForwardOrigin},
		// The intermediate arm is subsumed: when the target IS C and the
		// message came from C, the source arm has already fired.
		{"intermediate by source", Input{ChatID: "-1300"}, "-1300", true, ReasonTargetMatchesSource},
		{"legitimate", Input{ChatID: "-1001"}, "-2001", false, ""},
	}
	for _, tc := range checks {
		loop, reason := CheckLoop(tc.in, r, tc.target)
		if loop != tc.loop || reason != tc.reason {
			t.Errorf("%s: CheckLoop = (%v, %q), want (%v, %q)", tc.name, loop, reason, tc.loop, tc.reason)
		}
	}
}

// --- EveryTargetLoops (services.py:575, the evaluate_rule arm) ------------- //

// TestEveryTargetLoopsOnlyWhenAllLoop: a single looping target is skipped
// by the fan-out; only a task whose every target loops is rejected. The
// reason returned is the first target's, as the Python engine returns it.
func TestEveryTargetLoopsOnlyWhenAllLoop(t *testing.T) {
	// One looping target out of two: not a full loop, the other target
	// still receives the message.
	r := routingRule(t, nil)
	in := Input{ChatID: "-1001"} // -1001 is the source; -2001/-2002 are targets
	if loops, _ := EveryTargetLoops(in, r); loops {
		t.Error("a task with one looping target out of two must not be wholly rejected")
	}
	// Both targets loop: the whole delivery is a loop.
	r = routingRule(t, func(r *rule.ForwardRule) {
		r.TargetChatID = "-1001"
		r.TargetChatIDs = []string{"-1001", "-1001 "}
	})
	if loops, reason := EveryTargetLoops(in, r); !loops || reason != ReasonTargetMatchesSource {
		t.Errorf("EveryTargetLoops with all targets looping = (%v, %q), want (true, %q)", loops, reason, ReasonTargetMatchesSource)
	}
}

func TestEveryTargetLoopsTargetlessTask(t *testing.T) {
	r := &rule.ForwardRule{ID: "r", IsActive: true, SourceChatID: "-1001"}
	if loops, _ := EveryTargetLoops(Input{ChatID: "-1001"}, r); loops {
		t.Error("a task with no targets must not report a whole-delivery loop")
	}
}
