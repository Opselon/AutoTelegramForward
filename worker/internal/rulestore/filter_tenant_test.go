package rulestore

import (
	"database/sql"
	"testing"

	"atf/worker/internal/domain/rule"
)

// filter_tenant_test.go — the tenant boundary on the attached filter
// (pipeline.py:930-943). A task selects a filter_rules row by id alone; the
// row it points at may belong to another tenant. The store must IGNORE a
// filter whose owner is neither the task's owner nor 0 (a pre-ownership row,
// which stays the task's own configuration). The delivery then proceeds
// unfiltered — never filtered by, or inheriting the configuration of,
// another tenant's rule.
func TestAttachedFilterOwnedByAnotherTenantIsIgnored(t *testing.T) {
	store := newTestStore(t)

	// A rule owned by tenant 7 with an attached filter owned by tenant 99.
	insertFilterOwned(t, store.rulesDB, "filt-foreign", 99, &rule.FilterRule{
		IsEnabled: true, BlacklistKeywords: []string{"secret"},
	})
	insertRuleOwnedWithFilter(t, store.rulesDB, "r1", 7, "filt-foreign")

	r, f, err := store.GetByID("r1")
	if err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	if r == nil {
		t.Fatal("rule missing")
	}
	if f != nil {
		t.Errorf("a filter owned by another tenant must be ignored, got %+v", f)
	}
}

func TestAttachedFilterOwnedBySameTenantIsApplied(t *testing.T) {
	store := newTestStore(t)

	insertFilterOwned(t, store.rulesDB, "filt-own", 7, &rule.FilterRule{
		IsEnabled: true, BlacklistKeywords: []string{"secret"},
	})
	insertRuleOwnedWithFilter(t, store.rulesDB, "r1", 7, "filt-own")

	_, f, err := store.GetByID("r1")
	if err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	if f == nil {
		t.Fatal("the task's own filter must be applied")
	}
	if f.OwnerUserID != 7 {
		t.Errorf("owner projected: got %d, want 7", f.OwnerUserID)
	}
	if len(f.BlacklistKeywords) != 1 || f.BlacklistKeywords[0] != "secret" {
		t.Errorf("filter content must survive projection: %v", f.BlacklistKeywords)
	}
}

func TestPreOwnershipFilterIsTheTasksOwn(t *testing.T) {
	store := newTestStore(t)

	// owner_user_id = 0: a row written before ownership existed. Python
	// keeps it as the task's own configuration — the worker must too.
	insertFilterOwned(t, store.rulesDB, "filt-legacy", 0, &rule.FilterRule{
		IsEnabled: true, WhitelistKeywords: []string{"ok"},
	})
	insertRuleOwnedWithFilter(t, store.rulesDB, "r1", 7, "filt-legacy")

	_, f, err := store.GetByID("r1")
	if err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	if f == nil {
		t.Fatal("a pre-ownership filter (owner 0) must be applied")
	}
}

// insertFilterOwned writes a filter row carrying its tenant.
func insertFilterOwned(t *testing.T, db *sql.DB, id string, owner int64, f *rule.FilterRule) {
	t.Helper()
	if _, err := db.Exec(`INSERT OR REPLACE INTO filter_rules
		(id, name, whitelist_keywords, blacklist_keywords, regex_patterns, regex_block, regex_require,
		 allowed_media_types, blocked_media_types, drop_service_messages,
		 min_message_length, max_message_length, is_enabled, owner_user_id)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "f",
		toJSON(t, f.WhitelistKeywords), toJSON(t, f.BlacklistKeywords),
		toJSON(t, f.RegexPatterns), toJSON(t, f.RegexBlock), toJSON(t, f.RegexRequire),
		toJSON(t, f.AllowedMediaTypes), toJSON(t, f.BlockedMediaTypes),
		f.DropService, f.MinMessageLength, f.MaxMessageLength, f.IsEnabled, owner); err != nil {
		t.Fatalf("insert filter %s: %v", id, err)
	}
}

// insertRuleOwnedWithFilter writes a forward_rules row with an owner and an
// attached filter (the rule's filter_rule_id).
func insertRuleOwnedWithFilter(t *testing.T, db *sql.DB, id string, owner int64, filterID string) {
	t.Helper()
	if _, err := db.Exec(`INSERT OR REPLACE INTO forward_rules
		(id, session_id, is_active, is_paused, paused_until, forward_mode, content_mode,
		 filter_rule_id, metadata, owner_user_id)
		VALUES (?,?,?,?,?,?,?,?,?,?)`,
		id, "s1", 1, 0, 0, "COPY_MESSAGE", "AUTO",
		sql.NullString{String: filterID, Valid: filterID != ""}, "{}", owner); err != nil {
		t.Fatalf("insert rule %s: %v", id, err)
	}
}
