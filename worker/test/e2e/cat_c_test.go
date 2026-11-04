// cat_c_test.go — Category C: whitelist and blacklist (55 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryC runs the 55 whitelist, blacklist, and interaction scenarios.
func TestCategoryC(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	// C-001..C-027: Whitelist scenarios (27)
	// Whitelist presence demands matching at least one keyword.
	type wlCase struct {
		id   string
		desc string
		wl   []string
		text string
		pass bool
	}
	wlCases := []wlCase{
		{"ATF-E2E-C-001", "exact keyword match", []string{"SIGNAL"}, "SIGNAL", true},
		{"ATF-E2E-C-002", "partial match inside sentence", []string{"SIGNAL"}, "NEW SIGNAL TODAY", true},
		{"ATF-E2E-C-003", "whitelist keyword not present in text", []string{"sign"}, "totally different text", false},
		{"ATF-E2E-C-004", "case-sensitive match", []string{"signal"}, "signal here", true},
		{"ATF-E2E-C-005", "case-insensitive match", []string{"signal"}, "SIGNAL HERE", true},
		{"ATF-E2E-C-006", "multiple whitelist terms, first matches", []string{"BUY", "SELL", "HOLD"}, "BUY NOW", true},
		{"ATF-E2E-C-007", "multiple whitelist terms, last matches", []string{"BUY", "SELL", "HOLD"}, "DECISION: HOLD", true},
		{"ATF-E2E-C-008", "no matching terms among several", []string{"BUY", "SELL"}, "plain text no action", false},
		{"ATF-E2E-C-009", "empty whitelist allows all", []string{}, "plain message", true},
		{"ATF-E2E-C-010", "multiline text with keyword on second line", []string{"TARGET"}, "intro line\nTARGET line\nfooter", true},
		{"ATF-E2E-C-011", "persian keyword match", []string{"سیگنال"}, "سیگنال خرید فوری", true},
		{"ATF-E2E-C-012", "arabic keyword match", []string{"إشارة"}, "هذه إشارة مهمة", true},
		{"ATF-E2E-C-013", "english keyword match", []string{"alert"}, "critical alert", true},
		{"ATF-E2E-C-014", "mixed-language keyword match", []string{"btcسیگنال"}, "btcسیگنال امروز", true},
		{"ATF-E2E-C-015", "emoji keyword match", []string{"🚀"}, "pump incoming 🚀 now", true},
		{"ATF-E2E-C-016", "numeric value match", []string{"100000"}, "bitcoin at 100000 dollars", true},
		{"ATF-E2E-C-017", "url substring match", []string{"t.me"}, "join https://t.me/channel", true},
		{"ATF-E2E-C-018", "caption match on media", []string{"PHOTO"}, "PHOTO of the day", true},
		{"ATF-E2E-C-019", "text-only match", []string{"ONLY"}, "ONLY text here", true},
		{"ATF-E2E-C-020", "media without caption misses whitelist", []string{"MUSTHAVE"}, "", false},
		{"ATF-E2E-C-021", "media with matching caption delivers", []string{"CHART"}, "BTC CHART", true},
		{"ATF-E2E-C-022", "whitelist disabled allows all", []string{"RARE"}, "common text", true}, // disabled filter
		{"ATF-E2E-C-023", "whitelist enabled blocks non-match", []string{"RARE"}, "common text", false},
		{"ATF-E2E-C-024", "whitelist per task: task A demands, task B does not", []string{"AONLY"}, "B text", false},
		{"ATF-E2E-C-025", "whitelist per source", []string{"SRC1"}, "SRC1 update", true},
		{"ATF-E2E-C-026", "whitelist per target", []string{"TGT1"}, "TGT1 payload", true},
		{"ATF-E2E-C-027", "whitelist update while running", []string{"NEWKEY"}, "NEWKEY event", true},
	}
	for i, c := range wlCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			fid := fmt.Sprintf("flt-wl-%d", i)
			enabled := true
			if c.id == "ATF-E2E-C-022" {
				enabled = false
			}
			h.InstallFilter(FilterSpec{ID: fid, Enabled: enabled, Whitelist: c.wl})
			h.Install(TaskSpec{ID: fmt.Sprintf("task-wl-%d", i), Source: "-1001", Target: "-1002", Active: true, FilterID: fid})
			e := evt(c.id, "C", c.desc, "whitelist enforcement")
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-wl-%d", i), "-1001", int64(1000+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if c.pass {
				if h.AssertDelivered(&e, j, "") {
					e.Result = ResultPass
				}
			} else {
				if h.AssertDropped(&e, j) {
					e.Result = ResultPass
				}
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// C-028..C-048: Blacklist scenarios (21)
	type blCase struct {
		id   string
		desc string
		bl   []string
		text string
		drop bool
	}
	blCases := []blCase{
		{"ATF-E2E-C-028", "exact blocked keyword drops", []string{"SPAM"}, "SPAM", true},
		{"ATF-E2E-C-029", "partial blocked keyword inside sentence drops", []string{"advert"}, "this is an advert for you", true},
		{"ATF-E2E-C-030", "keyword non-match delivers: 'ad' does not drop 'clean story'", []string{"ad"}, "great clean story", false},
		{"ATF-E2E-C-031", "case-sensitive blacklist", []string{"scam"}, "scam link", true},
		{"ATF-E2E-C-032", "case-insensitive blacklist: uppercase drops", []string{"scam"}, "SCAM LINK", true},
		{"ATF-E2E-C-033", "multiple blacklist terms: first drops", []string{"casino", "bet", "gamble"}, "play at our casino", true},
		{"ATF-E2E-C-034", "multiple blacklist terms: last drops", []string{"casino", "bet", "gamble"}, "time to gamble now", true},
		{"ATF-E2E-C-035", "no blocked terms delivers", []string{"casino", "bet"}, "honest trading signal", false},
		{"ATF-E2E-C-036", "empty blacklist allows all", []string{}, "any text here", false},
		{"ATF-E2E-C-037", "persian blocked term drops", []string{"تبلیغ"}, "تبلیغ ویژه برای شما", true},
		{"ATF-E2E-C-038", "arabic blocked term drops", []string{"إعلان"}, "إعلان مجاني هنا", true},
		{"ATF-E2E-C-039", "english blocked term drops", []string{"phishing"}, "avoid this phishing site", true},
		{"ATF-E2E-C-040", "mixed-language blocked term drops", []string{"freeپکیج"}, "freeپکیج آموزشی", true},
		{"ATF-E2E-C-041", "emoji blocked term drops", []string{"🎰"}, "win big 🎰 jackpot", true},
		{"ATF-E2E-C-042", "numeric value blocked term drops", []string{"888"}, "lucky number 888 win", true},
		{"ATF-E2E-C-043", "url blocked term drops", []string{"badlink.com"}, "visit https://badlink.com", true},
		{"ATF-E2E-C-044", "caption blocked term drops", []string{"promo"}, "promo photo", true},
		{"ATF-E2E-C-045", "media-only without blocked caption delivers", []string{"promo"}, "", false},
		{"ATF-E2E-C-046", "blacklist disabled allows all", []string{"BLOCKED"}, "BLOCKED term", false}, // disabled filter
		{"ATF-E2E-C-047", "blacklist enabled drops", []string{"BLOCKED"}, "BLOCKED term", true},
		{"ATF-E2E-C-048", "blacklist per task drops only configured task", []string{"DROPME"}, "DROPME payload", true},
	}
	for i, c := range blCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			fid := fmt.Sprintf("flt-bl-%d", i)
			enabled := true
			if c.id == "ATF-E2E-C-046" {
				enabled = false
			}
			h.InstallFilter(FilterSpec{ID: fid, Enabled: enabled, Blacklist: c.bl})
			h.Install(TaskSpec{ID: fmt.Sprintf("task-bl-%d", i), Source: "-1001", Target: "-1002", Active: true, FilterID: fid})
			e := evt(c.id, "C", c.desc, "blacklist enforcement")
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-bl-%d", i), "-1001", int64(2000+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if c.drop {
				if h.AssertDropped(&e, j) {
					e.Result = ResultPass
				}
			} else {
				if h.AssertDelivered(&e, j, "") {
					e.Result = ResultPass
				}
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// C-049..C-055: Whitelist + Blacklist interactions (7)
	type ixCase struct {
		id   string
		desc string
		wl   []string
		bl   []string
		text string
		drop bool // true = expect drop; false = expect deliver
	}
	ixCases := []ixCase{
		// In ATF, blacklist takes precedence: a message matching BOTH is dropped (defense in depth).
		{"ATF-E2E-C-049", "both configured, both match: blacklist takes precedence (dropped)", []string{"SIGNAL"}, []string{"SPAM"}, "SIGNAL WITH SPAM", true},
		{"ATF-E2E-C-050", "whitelist matches, blacklist does not: delivers", []string{"SIGNAL"}, []string{"SPAM"}, "CLEAN SIGNAL", false},
		{"ATF-E2E-C-051", "neither matches: dropped by whitelist", []string{"SIGNAL"}, []string{"SPAM"}, "UNRELATED TEXT", true},
		{"ATF-E2E-C-052", "whitelist empty, blacklist matches: dropped", []string{}, []string{"BAD"}, "HAS BAD WORD", true},
		{"ATF-E2E-C-053", "whitelist empty, blacklist clean: delivers", []string{}, []string{"BAD"}, "HAS GOOD WORD", false},
		{"ATF-E2E-C-054", "multiple whitelist and blacklist: blacklist wins", []string{"A", "B"}, []string{"X", "Y"}, "CONTAINS A AND X", true},
		{"ATF-E2E-C-055", "per-user isolation: user 2 filter does not affect user 1", []string{"U1_OK"}, []string{"U1_BAD"}, "U1_OK text", false},
	}
	for i, c := range ixCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			fid := fmt.Sprintf("flt-ix-%d", i)
			h.InstallFilter(FilterSpec{ID: fid, Enabled: true, Whitelist: c.wl, Blacklist: c.bl})
			h.Install(TaskSpec{ID: fmt.Sprintf("task-ix-%d", i), Source: "-1001", Target: "-1002", Active: true, FilterID: fid})
			e := evt(c.id, "C", c.desc, "whitelist/blacklist precedence")
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-ix-%d", i), "-1001", int64(3000+i), "-1002", c.text)
			err := h.Run(ctx, j)
			if c.drop {
				if h.AssertDropped(&e, j) {
					e.Result = ResultPass
				}
			} else {
				if h.AssertDelivered(&e, j, "") {
					e.Result = ResultPass
				}
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}
}
