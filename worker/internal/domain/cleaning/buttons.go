package cleaning

import (
	"strings"
)

// buttons.go — the link policy applied to the message's INLINE KEYBOARD
// (finding F-16).
//
// `LinkManagementEngine.filter_inline_keyboard` (services.py:1065) is the
// Python half of this: a link policy that removes URLs from the BODY must
// also remove the URL BUTTONS the source message carries, or the task
// "removes links" in the text while every button under the message still
// opens one. The audit filed it as zero production callers — nothing on the
// Python side ever invoked it, so the buttons leaked. Stage 4.4 makes the
// Worker — the business-logic owner — apply the same policy to the markup.
//
// The decision travels as a `ButtonDecision` on the transform Result, not as
// a channelled side-effect: the delivery engine decides what to send, this
// package only decides what the policy implies. Buttons with no URL
// (callback/data buttons the SOURCE chat configured) are never touched —
// the policy is about links, not about keyboards in general.
//
// Semantics mirror filter_inline_keyboard exactly:
//   - PRESERVE_ALL (and an empty policy) keeps everything.
//   - REMOVE_ALL_URLS / REMOVE_BUTTON_URLS remove every URL button.
//   - REMOVE_EXTERNAL_URLS removes buttons whose domain is not a Telegram
//     one (t.me, telegram.me, telegram.dog).
//   - REMOVE_SELECTED_DOMAINS removes buttons matching the blocklist.
//   - ALLOWLIST_ONLY removes buttons NOT matching the allowlist.
//   - CUSTOM_LINK_POLICY: blocklist first, then allowlist.
//   - REWRITE_LINKS keeps the buttons (the rewrite happens in the body; the
//     buttons' destination is a property of the SOURCE message the task
//     cannot rebuild).
//
// A row left empty is dropped entirely, matching Python; a keyboard left
// with no rows is reported as RemoveAll so the caller can drop the markup.

// ButtonRow is one row of the source message's inline keyboard.
type ButtonRow struct {
	// Buttons in row order.
	Buttons []Button
}

// Button is one inline-keyboard button. Only URL buttons are governed by
// the link policy; a button with an empty URL is a callback/data button the
// source chat configured and is never removed.
type Button struct {
	Text string
	URL  string
}

// ButtonDecision is what the link policy implies for the markup. The caller
// applies it: RemoveRows lists the row indices to keep (others dropped),
// and RemoveAll reports that no URL buttons survive, so the markup itself
// should not be sent.
type ButtonDecision struct {
	// KeepRows are the indices (into the original keyboard) of the rows that
	// survive the policy, in order. Empty means nothing survives.
	KeepRows []int
	// RemoveAll reports that every URL button was removed. The caller drops
	// the whole reply markup in that case (an empty keyboard is not a valid
	// Telegram payload), exactly as Python returns None.
	RemoveAll bool
	// Applied reports whether the policy changed anything at all. False for
	// PRESERVE_ALL, or a keyboard that already satisfied the policy.
	Applied bool
}

// FilterInlineKeyboard applies the task's link policy to the source
// message's inline keyboard, mirroring
// LinkManagementEngine.filter_inline_keyboard (services.py:1065).
//
// policy is the raw policy string (LinkPolicy*), allowlist and blocklist the
// rule's domain lists. An empty policy or PRESERVE_ALL keeps everything;
// that is the documented default every task starts from.
func FilterInlineKeyboard(rows []ButtonRow, policy string, allowlist, blocklist []string) ButtonDecision {
	policy = strings.ToUpper(strings.TrimSpace(policy))
	if policy == "" || policy == LinkPolicyPreserveAll || len(rows) == 0 {
		return ButtonDecision{}
	}

	allow := normalizeDomains(allowlist)
	block := normalizeDomains(blocklist)

	keep := make([]int, 0, len(rows))
	removedAny := false
	for i, row := range rows {
		kept := make([]Button, 0, len(row.Buttons))
		for _, btn := range row.Buttons {
			if btn.URL == "" {
				// A callback button the source chat configured: not a link,
				// and the policy does not reach it (Python keeps these
				// untouched through the same branch).
				kept = append(kept, btn)
				continue
			}
			if buttonPolicyRemoves(btn.URL, policy, allow, block) {
				removedAny = true
				continue
			}
			kept = append(kept, btn)
		}
		if len(kept) > 0 {
			keep = append(keep, i)
		}
	}

	decision := ButtonDecision{KeepRows: keep, Applied: removedAny}
	// Python returns None when no row survives: the markup as a whole goes.
	if removedAny && len(keep) == 0 {
		decision.RemoveAll = true
	}
	return decision
}

// buttonPolicyRemoves reports whether the policy drops one URL button. It
// is the `remove_btn` decision in filter_inline_keyboard, one branch per
// policy value, and it deliberately has no default: a policy this package
// does not recognize removes nothing (fail-open is wrong for a removal, so
// the unknown policy is treated as PRESERVE_ALL at the caller instead).
func buttonPolicyRemoves(btnURL, policy string, allowlist, blocklist []string) bool {
	domain := ExtractDomain(btnURL)
	switch policy {
	case LinkPolicyRemoveAllURLs, LinkPolicyRemoveButtonURLs:
		return true
	case LinkPolicyRemoveExternal:
		return !matchesDomain(domain, tgDomainList())
	case LinkPolicyRemoveSelected:
		return matchesDomain(domain, blocklist)
	case LinkPolicyAllowlistOnly:
		return !matchesDomain(domain, allowlist)
	case LinkPolicyCustom:
		if len(blocklist) > 0 && matchesDomain(domain, blocklist) {
			return true
		}
		if len(allowlist) > 0 && !matchesDomain(domain, allowlist) {
			return true
		}
		return false
	default:
		// REWRITE_LINKS and any unrecognized value keep the buttons: the
		// rewrite is a body-text transformation, and an unknown policy has
		// no documented removal semantics.
		return false
	}
}

