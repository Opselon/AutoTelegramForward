package rulestore

import (
	"database/sql"
	"testing"

	"atf/worker/internal/domain/routing"
)

// --- Stage 4.5 W1: the routing projection ----------------------------------- //

// insertRoutingRule writes a forward_rules row carrying the routing
// columns, the way the Python repository's INSERT does (target_chat_ids as
// a JSON list, detection_criteria as JSON).
func insertRoutingRule(t *testing.T, db *sql.DB, id, sourceChatID, sourceChatName, targetChatID, targetChatIDs, criteria, metadata string) {
	t.Helper()
	if _, err := db.Exec(`INSERT OR REPLACE INTO forward_rules
		(id, session_id, is_active, is_paused, paused_until, forward_mode, content_mode,
		 filter_rule_id, metadata, source_chat_id, source_chat_name, target_chat_id,
		 target_chat_ids, detection_criteria)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "s1", 1, 0, 0, "COPY_MESSAGE", "AUTO", sql.NullString{}, metadata,
		sourceChatID, sourceChatName, targetChatID, targetChatIDs, criteria); err != nil {
		t.Fatalf("insert routing rule %s: %v", id, err)
	}
}

// TestRoutingColumnsProject: the routing columns the loop decision reads
// must come back on the projection, exactly as the Python ForwardRule
// loader builds them from the same columns.
func TestRoutingColumnsProject(t *testing.T) {
	store := newTestStore(t)
	insertRoutingRule(t, store.rulesDB, "route-1", "-1001", "@namedsource", "-2001",
		`["-2001","-2002"]`, `{"source_chat_ids":["-1002"]}`, `{}`)

	r, _, err := store.GetByID("route-1")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if r.SourceChatID != "-1001" {
		t.Errorf("SourceChatID = %q, want -1001", r.SourceChatID)
	}
	if r.SourceChatName != "@namedsource" {
		t.Errorf("SourceChatName = %q, want @namedsource (from the source_chat_name column)", r.SourceChatName)
	}
	if r.TargetChatID != "-2001" {
		t.Errorf("TargetChatID = %q, want -2001", r.TargetChatID)
	}
	if len(r.TargetChatIDs) != 2 || r.TargetChatIDs[0] != "-2001" || r.TargetChatIDs[1] != "-2002" {
		t.Errorf("TargetChatIDs = %v, want [-2001 -2002] (the raw column, order preserved)", r.TargetChatIDs)
	}
	// The extra sources come from detection_criteria, not a column.
	got := r.SourceChatIDs()
	if len(got) != 1 || got[0] != "-1002" {
		t.Errorf("SourceChatIDs() = %v, want [-1002] (from detection_criteria)", got)
	}
}

// TestRoutingProjectionMatchesSourceAndAllTargets: the projection feeds the
// routing decisions — a task configured with a primary source, an extra
// criteria source and a multi-target list must answer both decisions the
// way the Python ForwardRule does.
func TestRoutingProjectionMatchesSourceAndAllTargets(t *testing.T) {
	store := newTestStore(t)
	insertRoutingRule(t, store.rulesDB, "route-2", "-1001", "", "-2001",
		`["-2001","-2002","-2003"]`, `{"source_chat_ids":["-1002","-1003"]}`, `{}`)

	r, _, err := store.GetByID("route-2")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if !routing.MatchesSource(r, "-1001") {
		t.Error("the projected primary source must match")
	}
	if !routing.MatchesSource(r, "-1003") {
		t.Error("a criteria-listed extra source must match")
	}
	if routing.MatchesSource(r, "-9999") {
		t.Error("an unconfigured chat must not match")
	}
	targets := routing.AllTargets(r)
	if len(targets) != 3 || targets[0] != "-2001" || targets[1] != "-2002" || targets[2] != "-2003" {
		t.Errorf("AllTargets = %v, want primary first then the list in order", targets)
	}
}

// TestRoutingProjectionLoopDecision: the projection feeds check_loop —
// the end-to-end path from database columns to the loop decision, which is
// what stops a re-pointed task from closing a loop at delivery time.
func TestRoutingProjectionLoopDecision(t *testing.T) {
	store := newTestStore(t)
	insertRoutingRule(t, store.rulesDB, "route-3", "-1001", "", "-2001", `[]`, `{}`, `{}`)
	r, _, err := store.GetByID("route-3")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if loop, reason := routing.CheckLoop(routing.Input{ChatID: "-1001"}, r, "-2001"); loop {
		t.Errorf("a target distinct from the source must not loop (reason %q)", reason)
	}
	if loop, reason := routing.CheckLoop(routing.Input{ChatID: "-1001"}, r, "-1001"); !loop || reason != routing.ReasonTargetMatchesSource {
		t.Error("the source chat as target must loop with target_matches_source")
	}
}

// TestRoutingProjectionUsernameSource: a task configured by @username
// (source_chat_name) must match that source and the loop check must see
// the username arm — both read the same projected field.
func TestRoutingProjectionUsernameSource(t *testing.T) {
	store := newTestStore(t)
	insertRoutingRule(t, store.rulesDB, "route-user", "", "@namedsource", "-2001", `[]`, `{}`, `{}`)
	r, _, err := store.GetByID("route-user")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if !routing.MatchesSource(r, "@namedsource") {
		t.Error("a @username-configured task must match its username source")
	}
	if !routing.MatchesSource(r, "namedsource") {
		t.Error("the username without its @ must match too")
	}
	if loop, _ := routing.CheckLoop(routing.Input{ChatID: "-1001", ChatUsername: "@namedsource"}, r, "@namedsource"); !loop {
		t.Error("the target equal to the source @username must loop")
	}
}

// TestTargetChatIDsColumnDegradesGracefully: the Python loader's _safe_list
// turns a missing/invalid blob into "no extra targets" — the Go reader must
// do the same rather than failing the whole projection.
func TestTargetChatIDsColumnDegradesGracefully(t *testing.T) {
	store := newTestStore(t)
	for _, blob := range []string{"", " ", "not-json", "null", "123", `[1,2]`} {
		insertRoutingRule(t, store.rulesDB, "route-degrade", "-1001", "", "-2001", blob, `{}`, `{}`)
		r, _, err := store.GetByID("route-degrade")
		if err != nil || r == nil {
			t.Fatalf("GetByID with target_chat_ids=%q: rule=%v err=%v", blob, r, err)
		}
		if r.TargetChatID != "-2001" {
			t.Errorf("target_chat_ids=%q: primary target lost (%q)", blob, r.TargetChatID)
		}
		if len(r.TargetChatIDs) != 0 && blob != `[1,2]` {
			t.Errorf("target_chat_ids=%q: expected no parsed targets, got %v", blob, r.TargetChatIDs)
		}
		// A numeric list still parses (the Python _safe_list keeps the
		// entries; matches_source stringifies them), so AllTargets keeps
		// them — only an invalid blob means "just the primary".
		want := 1
		if blob == `[1,2]` {
			want = 3
		}
		if got := len(r.AllTargets()); got != want {
			t.Errorf("target_chat_ids=%q: AllTargets has %d targets, want %d", blob, got, want)
		}
	}
}

// TestTargetChatIDsNumericEntries: an API client can store numeric ids in
// the JSON list. They must read back as the same ids the Python loader's
// str(...) coercion produces, so all_targets keeps ordering them alongside
// string entries.
func TestTargetChatIDsNumericEntries(t *testing.T) {
	store := newTestStore(t)
	insertRoutingRule(t, store.rulesDB, "route-numeric", "-1001", "", "-2001", `[-2002]`, `{}`, `{}`)
	r, _, err := store.GetByID("route-numeric")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if len(r.TargetChatIDs) != 1 || r.TargetChatIDs[0] != "-2002" {
		t.Errorf("TargetChatIDs = %v, want [-2002] (the numeric id as a string)", r.TargetChatIDs)
	}
}
