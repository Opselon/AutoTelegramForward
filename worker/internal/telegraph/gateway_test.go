package telegraph

import "testing"

// TestNeedsReupload pins the two reasons a copy must fall through to the
// re-upload path, because production showed them treated differently:
//
//   - a source that forbids copying (FORWARDING_RESTRICTED), and
//   - a TARGET that rejects the copied media itself (the PHOTO_EXT_INVALID
//     family). The copy is not forbidden — it is incompatible — and retrying
//     the same copy can never fix it, exactly like a restriction.
//
// The second class shipped as a permanent failure for 12 real jobs because
// the fallback only ever asked about restrictions. NeedsReupload closes
// that gap by making one call cover both.
func TestNeedsReupload(t *testing.T) {
	cases := []struct {
		name       string
		code       string
		errorType  string
		want       bool
		restricted bool
	}{
		{"source restriction", "FORWARDING_RESTRICTED", "RESTRICTED", true, true},
		{"target photo rejection", "TELEGRAM_PERMANENT_ERROR", "PHOTO_EXT_INVALID", true, false},
		{"target media invalid", "TELEGRAM_PERMANENT_ERROR", "MEDIA_INVALID", true, false},
		{"target video not modified", "TELEGRAM_PERMANENT_ERROR", "VIDEO_NOT_MODIFIED", true, false},
		{"flood wait is not a reupload signal", "FLOOD_WAIT", "FLOOD_WAIT", false, false},
		{"network timeout is a retry, not a reupload", "NETWORK_TIMEOUT", "TIMEOUT", false, false},
		{"unknown outcome is ambiguous, not a reupload", "UNKNOWN_DELIVERY_OUTCOME", "UNKNOWN", false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &DeliveryFailure{Code: tc.code, ErrorType: tc.errorType}
			if got := f.NeedsReupload(); got != tc.want {
				t.Errorf("NeedsReupload() = %v, want %v", got, tc.want)
			}
			if f.IsForwardingRestricted() != tc.restricted {
				t.Errorf("IsForwardingRestricted() drift: got %v, want %v", f.IsForwardingRestricted(), tc.restricted)
			}
		})
	}
}
