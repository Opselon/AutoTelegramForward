package cleaning

import "testing"

// TestFilterInlineKeyboardMirrorsPython runs the same cases Python's
// LinkManagementEngine.filter_inline_keyboard (services.py:1065) was probed
// with during the Stage 4.4 port. Every expectation came from executing the
// Python engine on the same input; the two are pinned to agree.
func TestFilterInlineKeyboardMirrorsPython(t *testing.T) {
	cases := []struct {
		name   string
		policy string
		rows   [][]Button
		// the row indices Python kept, in order
		wantKeep []int
		// wantDrop is RemoveAll, which is true only when NO row survives —
		// Python returns None for the markup in exactly that case. The
		// mixed cases keep rows, so the filtered markup travels on.
		wantDrop bool
	}{
		{"REMOVE_ALL_URLS keeps a callback-only row", LinkPolicyRemoveAllURLs,
			[][]Button{{{"Join", "https://t.me/x"}}, {{"Site", "https://a.io"}}, {{"plain", ""}}},
			[]int{2}, false},
		{"REMOVE_BUTTON_URLS", LinkPolicyRemoveButtonURLs,
			[][]Button{{{"Join", "https://t.me/x"}}, {{"plain", ""}}},
			[]int{1}, false},
		{"REMOVE_EXTERNAL_URLS keeps Telegram domains", LinkPolicyRemoveExternal,
			[][]Button{{{"TG", "https://t.me/x"}}, {{"Ext", "https://a.io"}}},
			[]int{0}, false},
		{"REMOVE_SELECTED_DOMAINS", LinkPolicyRemoveSelected,
			[][]Button{{{"Blocked", "https://bad.io"}}, {{"OK", "https://good.io"}}},
			[]int{1}, false},
		{"ALLOWLIST_ONLY", LinkPolicyAllowlistOnly,
			[][]Button{{{"OK", "https://good.io"}}, {{"Bad", "https://bad.io"}}},
			[]int{0}, false},
		{"CUSTOM_LINK_POLICY blocklist then allowlist", LinkPolicyCustom,
			[][]Button{{{"B", "https://bad.io"}}, {{"G", "https://good.io"}}},
			[]int{1}, false},
		{"PRESERVE_ALL keeps everything", LinkPolicyPreserveAll,
			[][]Button{{{"Join", "https://t.me/x"}}},
			nil, false},
		{"empty policy keeps everything", "",
			[][]Button{{{"Join", "https://t.me/x"}}},
			nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rows := make([]ButtonRow, len(c.rows))
			for i, r := range c.rows {
				rows[i] = ButtonRow{Buttons: r}
			}
			got := FilterInlineKeyboard(rows, c.policy, []string{"good.io"}, []string{"bad.io"})
			if !equalInts(got.KeepRows, c.wantKeep) {
				t.Errorf("keep rows: got %v, want %v", got.KeepRows, c.wantKeep)
			}
			if got.RemoveAll != c.wantDrop {
				t.Errorf("RemoveAll: got %v, want %v", got.RemoveAll, c.wantDrop)
			}
		})
	}
}

// TestFilterInlineKeyboardDropsEveryButton is the F-16 case itself: a policy
// that removes every URL button must leave nothing — Python returns None for
// the markup, the Go side reports RemoveAll so the caller drops it entirely.
func TestFilterInlineKeyboardDropsEveryButton(t *testing.T) {
	rows := []ButtonRow{
		{Buttons: []Button{{"a", "https://a.io"}, {"b", "https://b.io"}}},
		{Buttons: []Button{{"c", "https://c.io"}}},
	}
	got := FilterInlineKeyboard(rows, LinkPolicyRemoveAllURLs, nil, nil)
	if !got.RemoveAll {
		t.Errorf("a policy that removes every URL button must report RemoveAll: %+v", got)
	}
	if len(got.KeepRows) != 0 {
		t.Errorf("no row may survive: got %v", got.KeepRows)
	}
	if !got.Applied {
		t.Error("Applied must be true when the policy removed something")
	}
}

// TestFilterInlineKeyboardNeverTouchesCallbackButtons pins the
// non-removal half: a button with no URL is the source chat's own callback
// button, and the link policy does not reach it (Python keeps it through the
// same branch).
func TestFilterInlineKeyboardNeverTouchesCallbackButtons(t *testing.T) {
	rows := []ButtonRow{
		{Buttons: []Button{{"like", ""}, {"link", "https://a.io"}}},
	}
	got := FilterInlineKeyboard(rows, LinkPolicyRemoveAllURLs, nil, nil)
	if !equalInts(got.KeepRows, []int{0}) {
		t.Errorf("the callback button's row must survive: got %v", got.KeepRows)
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
