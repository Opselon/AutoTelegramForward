package dedup

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"
)

// sqliteTestSchema is the Core's processed_messages + content_dedup DDL
// (core/infrastructure/persistence/database.py), kept here so the store
// tests run against the real column set rather than a hand-rolled look-alike.
const sqliteTestSchema = `
CREATE TABLE IF NOT EXISTS processed_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    media_group_id TEXT,
    ts INTEGER NOT NULL,
    UNIQUE(rule_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS content_dedup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(rule_id, content_hash)
);
`

// newSQLiteTestStore opens a fresh temp SQLite carrying the Core's dedup
// tables. It mirrors queue.newTestStore's shape (temp dir, schema, then the
// production open path) so the store under test is the production store.
func newSQLiteTestStore(t *testing.T) Store {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	if _, err := db.Exec(sqliteTestSchema); err != nil {
		db.Close()
		t.Fatalf("apply schema: %v", err)
	}
	db.Close()
	store, err := NewSQLite(context.Background(), path+"?_busy_timeout=10000&_journal_mode=WAL")
	if err != nil {
		t.Fatalf("new dedup store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// sqliteTestDB returns the store's underlying *sql.DB for direct row checks.
func sqliteTestDB(t *testing.T, s Store) *sql.DB {
	t.Helper()
	if st, ok := s.(*sqliteStore); ok {
		return st.db
	}
	t.Fatalf("store is not a sqliteStore")
	return nil
}

// --- fingerprint ---------------------------------------------------------- //

// TestFingerprintMatchesPython runs the byte-for-byte ports of the Python
// _content_fingerprint properties as golden cases. Every expectation here
// was produced by executing the real Python pipeline, not by reasoning about
// it: the reference is core/application/pipeline.py::_content_fingerprint.
func TestFingerprintMatchesPython(t *testing.T) {
	cases := []struct {
		name string
		in   FingerprintInput
		want string
	}{
		{
			name: "plain text",
			in:   FingerprintInput{Text: "Hello World", MediaType: "text"},
			want: "809aedf48ebe04a0506d919f1fa88b1ca69662937dfc3a2952274275ae47f06c",
		},
		{
			name: "casefold lowercases",
			in:   FingerprintInput{Text: "hello world", MediaType: "text"},
			want: "809aedf48ebe04a0506d919f1fa88b1ca69662937dfc3a2952274275ae47f06c",
		},
		{
			name: "internal whitespace run collapses",
			in:   FingerprintInput{Text: "Hello   World", MediaType: "text"},
			want: "809aedf48ebe04a0506d919f1fa88b1ca69662937dfc3a2952274275ae47f06c",
		},
		{
			name: "a newline rewrap is the same content",
			in:   FingerprintInput{Text: "Hello \n World", MediaType: "text"},
			want: "809aedf48ebe04a0506d919f1fa88b1ca69662937dfc3a2952274275ae47f06c",
		},
		{
			name: "caption joins text at the || seam",
			in:   FingerprintInput{Text: "ab", Caption: "cd", MediaType: "text"},
			want: "22b2a3af25104e818e4e937f87ff9c17ae343e7996da5103250537eb9614f5da",
		},
		{
			name: "empty caption is the identity",
			in:   FingerprintInput{Text: "ab", MediaType: "text"},
			want: "029c8b65ae100837b56868fb2508c540af9eabee04bb1532d9e881af3e8bf67d",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := PayloadFingerprint(tc.in)
			if got != tc.want {
				t.Fatalf("fingerprint = %s, want %s", got, tc.want)
			}
		})
	}
}

// TestFingerprintKeyShape pins the parts of the Python function that a
// golden hash would hide: which fields feed the key and how they combine.
// A divergence here is a divergence in what the two engines consider "the
// same post", which is the whole point of the Duplicate Filters feature.
func TestFingerprintKeyShape(t *testing.T) {
	base := PayloadFingerprint(FingerprintInput{Text: "Hello World", MediaType: "text"})

	// The album id is part of the key: the same text under a different
	// album id is NOT the same content.
	albumA := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "photo", MediaGroupID: "g1"})
	albumB := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "photo", MediaGroupID: "g2"})
	if albumA == albumB {
		t.Error("two album ids produced one fingerprint: the album scope was lost")
	}

	// A grouped message is not the same content as an ungrouped one.
	grouped := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "photo", MediaGroupID: "g1"})
	ungrouped := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "photo"})
	if grouped == ungrouped {
		t.Error("album id changed the fingerprint but the ungrouped form matched")
	}

	// The media kind is part of the key.
	photo := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "photo"})
	video := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "video"})
	if photo == video {
		t.Error("photo and video media kinds produced one fingerprint")
	}
	// The media kind normalizes to the lowercase wire form the Python
	// enqueue path snapshots ("photo", not "PHOTO") — a mixed-case input
	// must not split one media kind into two keys.
	if got := PayloadFingerprint(FingerprintInput{Text: "x", MediaType: "PHOTO"}); got != photo {
		t.Error("a mixed-case media kind produced a different key; " +
			"the fingerprint must read the lowercase form Python writes")
	}

	// Rule-scoping is the CALLER's contract (the claim key carries the rule
	// id); the fingerprint itself must not bake a rule in, or two tasks
	// sharing text would collapse when the Python side keeps them apart.
	if PayloadFingerprint(FingerprintInput{Text: "Hello World", MediaType: "text"}) != base {
		t.Error("a pure function changed its output between calls")
	}
}

// TestFingerprintPersianArabic pins the exact fold behavior the Python
// content fingerprint has: a PLAIN casefold, NOT fold_text. Two spellings
// that fold_text unifies (Arabic kaf vs Persian keheh, a zero-width
// character) are DIFFERENT fingerprints here — this is the divergence the
// branding dedup key does not have, and confusing the two functions is the
// one mistake this file is built to prevent.
func TestFingerprintPersianArabic(t *testing.T) {
	// Arabic kaf (U+0643) and Persian keheh (U+06A9) are unified by
	// fold_text but NOT by plain casefold, so the content fingerprint keeps
	// them apart.
	arabicKaf := PayloadFingerprint(FingerprintInput{Text: "كتاب", MediaType: "text"})
	persianKeheh := PayloadFingerprint(FingerprintInput{Text: "کتاب", MediaType: "text"})
	if arabicKaf == persianKeheh {
		t.Error("plain casefold unified Arabic kaf and Persian keheh; " +
			"_content_fingerprint must NOT apply fold_text")
	}

	// Alef hamza below (U+0625) is another fold_text unification the
	// content fingerprint does not perform.
	alefHamza := PayloadFingerprint(FingerprintInput{Text: "أحمد", MediaType: "text"})
	plainAlef := PayloadFingerprint(FingerprintInput{Text: "احمد", MediaType: "text"})
	if alefHamza == plainAlef {
		t.Error("plain casefold unified alef hamza; _content_fingerprint must NOT apply fold_text")
	}

	// A zero-width character survives the plain casefold too.
	withZW := PayloadFingerprint(FingerprintInput{Text: "invis\u200bible", MediaType: "text"})
	without := PayloadFingerprint(FingerprintInput{Text: "invisible", MediaType: "text"})
	if withZW == without {
		t.Error("plain casefold stripped an invisible character; _content_fingerprint must NOT apply fold_text")
	}
}

// --- SQLite store ---------------------------------------------------------- //

func TestSQLiteWasProcessed(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()

	seen, err := s.WasProcessed(ctx, "rule1", "-100", 42)
	if err != nil {
		t.Fatalf("was_processed: %v", err)
	}
	if seen {
		t.Error("an unmarked event reported itself as processed")
	}

	if err := s.MarkProcessed(ctx, "rule1", "-100", 42, ""); err != nil {
		t.Fatalf("mark_processed: %v", err)
	}
	seen, err = s.WasProcessed(ctx, "rule1", "-100", 42)
	if err != nil {
		t.Fatalf("was_processed after mark: %v", err)
	}
	if !seen {
		t.Error("a marked event reported itself as not processed")
	}
}

// TestSQLiteMarkProcessedIsIdempotent pins the INSERT OR IGNORE contract: a
// reconnect replay calls MarkProcessed on an event the task already
// accepted, and that must be a no-op rather than a UNIQUE violation. This
// is the exact replay the event-level dedup exists to absorb.
func TestSQLiteMarkProcessedIsIdempotent(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		if err := s.MarkProcessed(ctx, "rule1", "-100", 7, "album-1"); err != nil {
			t.Fatalf("mark_processed attempt %d: %v", i, err)
		}
	}
	db := sqliteTestDB(t, s)
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM processed_messages
		WHERE rule_id=? AND chat_id=? AND message_id=?`, "rule1", "-100", 7).Scan(&n); err != nil {
		t.Fatalf("count: %v", err)
	}
	if n != 1 {
		t.Errorf("replayed MarkProcessed produced %d rows, want 1", n)
	}
}

// TestSQLiteMarkProcessedScopesByRule pins the (rule_id, chat_id,
// message_id) key: a different rule, chat or message is a different event.
// The marker has no target dimension by design — the Python gate writes it
// only once the whole fan-out was accepted.
func TestSQLiteMarkProcessedScopesByRule(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()

	if err := s.MarkProcessed(ctx, "rule1", "-100", 42, ""); err != nil {
		t.Fatalf("mark: %v", err)
	}
	for _, tc := range []struct {
		ruleID, chatID string
		messageID      int64
	}{
		{"rule2", "-100", 42}, // another task sees the same event
		{"rule1", "-200", 42}, // same task, another source chat
		{"rule1", "-100", 43}, // same task, another message
	} {
		seen, err := s.WasProcessed(ctx, tc.ruleID, tc.chatID, tc.messageID)
		if err != nil {
			t.Fatalf("was_processed %v: %v", tc, err)
		}
		if seen {
			t.Errorf("event %v leaked into another scope", tc)
		}
	}
}

// TestSQLiteClaimContentHash pins the atomic-claim contract that makes
// Duplicate Filters race-safe: the FIRST caller wins, the second caller is
// told the content is already owned — no read-then-write window for two
// workers racing on the identical re-post.
func TestSQLiteClaimContentHash(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()

	first, err := s.ClaimContentHash(ctx, "rule1", "hash-A")
	if err != nil {
		t.Fatalf("claim first: %v", err)
	}
	if !first {
		t.Error("the first claim lost a race with nobody")
	}
	second, err := s.ClaimContentHash(ctx, "rule1", "hash-A")
	if err != nil {
		t.Fatalf("claim second: %v", err)
	}
	if second {
		t.Error("a second claim on the same (rule, hash) won; the claim is not atomic")
	}
	// A different rule may still claim the same content: Duplicate Filters
	// are scoped to the rule, so the same text under another task forwards.
	otherRule, err := s.ClaimContentHash(ctx, "rule2", "hash-A")
	if err != nil {
		t.Fatalf("claim other rule: %v", err)
	}
	if !otherRule {
		t.Error("the rule scope was lost: another task could not claim the same content")
	}
}

// TestSQLiteClaimThenReleasePinsTheLifecycle is the semantically load-bearing
// test. A claim is EARNED on delivery acceptance and RELEASED when the
// message never reached a target; a release that does not work is a silent
// future drop (pipeline.py:781, _release_content_claim).
func TestSQLiteClaimThenReleasePinsTheLifecycle(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()

	if _, err := s.ClaimContentHash(ctx, "rule1", "hash-B"); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if err := s.ReleaseContentHash(ctx, "rule1", "hash-B"); err != nil {
		t.Fatalf("release: %v", err)
	}
	// After the release the fingerprint is claimable again: the next
	// legitimate identical post is NOT a duplicate.
	again, err := s.ClaimContentHash(ctx, "rule1", "hash-B")
	if err != nil {
		t.Fatalf("re-claim: %v", err)
	}
	if !again {
		t.Error("a released claim could not be reclaimed; the message would be " +
			"silently dropped on the next legitimate identical post")
	}
}

// TestSQLiteReleaseIsIdempotent: releasing a claim nobody holds is a no-op,
// matching the Python DELETE-with-no-clause-guard behaviour.
func TestSQLiteReleaseIsIdempotent(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if err := s.ReleaseContentHash(ctx, "rule1", "never-claimed"); err != nil {
			t.Fatalf("release attempt %d: %v", i, err)
		}
	}
}

// TestSQLiteWasContentForwarded is the non-claiming read the Python
// pipeline falls back to for stores predating the atomic claim: it reports
// what a claim would have been told, without taking one.
func TestSQLiteWasContentForwarded(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx := context.Background()

	forwarded, err := s.WasContentForwarded(ctx, "rule1", "hash-C")
	if err != nil {
		t.Fatalf("was_content_forwarded: %v", err)
	}
	if forwarded {
		t.Error("an unclaimed hash reported itself as forwarded")
	}
	if _, err := s.ClaimContentHash(ctx, "rule1", "hash-C"); err != nil {
		t.Fatalf("claim: %v", err)
	}
	forwarded, err = s.WasContentForwarded(ctx, "rule1", "hash-C")
	if err != nil {
		t.Fatalf("was_content_forwarded after claim: %v", err)
	}
	if !forwarded {
		t.Error("a claimed hash reported itself as not forwarded")
	}
}

// --- concurrency ----------------------------------------------------------- //

// TestSQLiteConcurrentClaimRacesOneWinner is the property the Python
// implementation's UNIQUE index buys: two goroutines racing on identical
// content under the same rule produce exactly one claim. SQLite's
// single-writer serialization gives the same guarantee the production
// deployment's kernel lock gives.
func TestSQLiteConcurrentClaimRacesOneWinner(t *testing.T) {
	s := newSQLiteTestStore(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	const ruleID, hash = "rule1", "race-hash"
	type claim struct {
		won bool
		err error
	}
	results := make(chan claim, 2)
	for i := 0; i < 2; i++ {
		go func() {
			won, err := s.ClaimContentHash(ctx, ruleID, hash)
			results <- claim{won: won, err: err}
		}()
	}
	wins := 0
	for i := 0; i < 2; i++ {
		c := <-results
		if c.err != nil {
			t.Fatalf("concurrent claim errored: %v", c.err)
		}
		if c.won {
			wins++
		}
	}
	if wins != 1 {
		t.Errorf("concurrent claims produced %d winners, want exactly 1", wins)
	}
}
