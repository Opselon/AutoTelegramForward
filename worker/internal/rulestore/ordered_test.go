package rulestore

import (
	"testing"
)

// ordered_test.go — the JSON key-order contract.
//
// Python's dict preserves insertion order and text_replace applies
// same-priority replacement entries in it. Go's map[string]any discards
// that order at decode time, so the rule store re-decodes the
// replacements map from the raw metadata blob with the ordered decoder.
// These tests pin the behavior the parity harness caught as a real
// divergence (PR #294): a task storing "#": "##" then "##": "x" must get
// "x sale", and the same entries stored in reverse must get "## sale".

func TestOrderedReplacementsKeepsKeyOrder(t *testing.T) {
	blob := `{"replacements":{"#":"##","##":"x"},"unrelated":1}`
	entries, ok := orderedReplacements(blob, "replacements")
	if !ok {
		t.Fatal("expected the replacements field to be found")
	}
	if len(entries) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(entries))
	}
	if entries[0].Find != "#" || entries[0].Replace != "##" {
		t.Errorf("first entry: %+v", entries[0])
	}
	if entries[1].Find != "##" || entries[1].Replace != "x" {
		t.Errorf("second entry: %+v", entries[1])
	}
}

func TestOrderedReplacementsReversedOrderIsObservable(t *testing.T) {
	// The order is the observable contract: reversed storage composes the
	// other way (RPL-PRIORITY-01's notes), and an order-losing decode
	// would make both spellings agree.
	blob := `{"replacements":{"##":"x","#":"##"}}`
	entries, ok := orderedReplacements(blob, "replacements")
	if !ok {
		t.Fatal("expected the replacements field to be found")
	}
	if len(entries) != 2 || entries[0].Find != "##" || entries[1].Find != "#" {
		t.Fatalf("reversed order lost: %+v", entries)
	}
}

func TestOrderedReplacementsOptionsDict(t *testing.T) {
	blob := `{"replacements":{"(\\w+): (\\w+)":{"to":"\\1 is \\2","mode":"regex"},"plain":"text"}}`
	entries, ok := orderedReplacements(blob, "replacements")
	if !ok {
		t.Fatal("expected the replacements field to be found")
	}
	if len(entries) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(entries))
	}
	if !entries[0].IsRegex || entries[0].Replace != "\\1 is \\2" {
		t.Errorf("regex entry: %+v", entries[0])
	}
	if entries[1].Replace != "text" {
		t.Errorf("literal entry: %+v", entries[1])
	}
}

func TestOrderedReplacementsManagerShape(t *testing.T) {
	// The replace-rule manager keys entries by rule id and carries the
	// find text inside the options dict; the order must survive that too.
	blob := `{"replacements":{"rule-a":{"source":"#","to":"##","whole_word":true},"rule-b":{"source":"##","to":"x"}}}`
	entries, ok := orderedReplacements(blob, "replacements")
	if !ok {
		t.Fatal("expected the replacements field to be found")
	}
	if len(entries) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(entries))
	}
	if entries[0].Find != "#" || entries[0].WholeWord != true {
		t.Errorf("first entry: %+v", entries[0])
	}
	if entries[1].Find != "##" {
		t.Errorf("second entry: %+v", entries[1])
	}
}

func TestOrderedReplacementsMalformed(t *testing.T) {
	cases := []string{"", "  ", "{", "[1,2]", `{"other":1}`, "not json", `{"replacements":[1,2]}`}
	for _, blob := range cases {
		if entries, ok := orderedReplacements(blob, "replacements"); ok && len(entries) > 0 {
			t.Errorf("malformed blob %q unexpectedly yielded %v", blob, entries)
		}
	}
	// A non-object replacements field reports present-but-not-an-object,
	// so the caller falls back rather than dropping the entries.
	if _, ok := orderedReplacements(`{"replacements":[1,2]}`, "replacements"); !ok {
		t.Error("a list replacements field should report present")
	}
}

func TestOrderedReplacementsIsStableAcrossCalls(t *testing.T) {
	// The decoder is used per rule read; it must not carry state between
	// blobs and must not reorder under its own map use.
	for i := 0; i < 50; i++ {
		blob := `{"replacements":{"a":"1","b":"2","c":"3","d":"4"}}`
		entries, ok := orderedReplacements(blob, "replacements")
		if !ok || len(entries) != 4 {
			t.Fatalf("iteration %d: ok=%v entries=%d", i, ok, len(entries))
		}
		want := []string{"a", "b", "c", "d"}
		for j, w := range want {
			if entries[j].Find != w {
				t.Fatalf("iteration %d: entry %d = %q, want %q", i, j, entries[j].Find, w)
			}
		}
	}
}
