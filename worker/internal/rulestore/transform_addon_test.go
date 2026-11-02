package rulestore

import (
	"encoding/json"
	"testing"

	"atf/worker/internal/domain/cleaning"
)

// transform_addon_test.go — the paid-addon entitlement gate
// (entities.py:1843 ForwardRule.is_addon_enabled).
//
// The projection is the single enforcement point the Go pipeline consults
// before a premium transformation runs. A client-supplied flag is never
// trusted on its own: the addon must be marked enabled in the persisted
// rule metadata, and anything else fails closed. These tests exist
// because the parity driver builds its Config by hand and so cannot see
// this gate at all — a regression here is invisible to
// scripts/test_stage42_parity.py.

// configFrom is the projection under test, fed by a metadata map.
func configFrom(t *testing.T, meta map[string]any) cleaning.Config {
	t.Helper()
	raw, err := json.Marshal(meta)
	if err != nil {
		t.Fatalf("marshal metadata: %v", err)
	}
	return transformConfigFromMetadata(meta, string(raw))
}

func TestAddonGateLinkButtonsRequiresEntitlement(t *testing.T) {
	// Paid add-on off: the buttons never render, however the task is
	// configured. This is the fail-closed contract — an owner who did not
	// purchase the add-on cannot get the transform by configuring it.
	meta := map[string]any{
		"link_buttons": []any{map[string]any{"text": "Join", "url": "https://t.me/x"}},
		"paid_addons":  map[string]any{},
	}
	if cfg := configFrom(t, meta); cfg.LinkButtons != nil {
		t.Errorf("link_buttons without the entitlement must not project, got %v", cfg.LinkButtons)
	}

	// The entitlement granted: the buttons project and the pipeline renders
	// them (the rendering itself is covered by the parity harness).
	meta["paid_addons"] = map[string]any{"link_buttons": true}
	cfg := configFrom(t, meta)
	if len(cfg.LinkButtons) != 1 || cfg.LinkButtons[0].URL != "https://t.me/x" {
		t.Errorf("entitled link_buttons must project, got %v", cfg.LinkButtons)
	}
}

func TestAddonGateAffiliateKitRequiresEntitlement(t *testing.T) {
	// Paid add-on off: no affiliate rewrite, even with the kit enabled in
	// the task's own settings.
	meta := map[string]any{
		"affiliate_kit": map[string]any{"enabled": true, "prefix": "https://aff.io/"},
		"paid_addons":   map[string]any{},
	}
	if cfg := configFrom(t, meta); cfg.AffiliateKit != nil {
		t.Errorf("affiliate_kit without the entitlement must not project, got %+v", cfg.AffiliateKit)
	}

	meta["paid_addons"] = map[string]any{"affiliate_kit": true}
	cfg := configFrom(t, meta)
	if cfg.AffiliateKit == nil || cfg.AffiliateKit.Prefix != "https://aff.io/" {
		t.Errorf("entitled affiliate_kit must project, got %+v", cfg.AffiliateKit)
	}
}

func TestAddonGateAbsentPaidAddonsFailsClosed(t *testing.T) {
	// No paid_addons block at all (a pre-add-on task): both premium
	// transforms stay off. An absent entitlement is not a granted one.
	meta := map[string]any{
		"link_buttons":  []any{map[string]any{"text": "Join", "url": "https://t.me/x"}},
		"affiliate_kit": map[string]any{"enabled": true, "prefix": "https://aff.io/"},
	}
	cfg := configFrom(t, meta)
	if cfg.LinkButtons != nil {
		t.Errorf("absent paid_addons must not enable link_buttons, got %v", cfg.LinkButtons)
	}
	if cfg.AffiliateKit != nil {
		t.Errorf("absent paid_addons must not enable affiliate_kit, got %+v", cfg.AffiliateKit)
	}
}

func TestAddonGateNonDictPaidAddonsFailsClosed(t *testing.T) {
	// A malformed (non-dict) paid_addons value is the same as none: Python's
	// isinstance check returns False, and so must the Go side.
	meta := map[string]any{
		"link_buttons": []any{map[string]any{"text": "Join", "url": "https://t.me/x"}},
		"paid_addons":  "true",
	}
	if cfg := configFrom(t, meta); cfg.LinkButtons != nil {
		t.Errorf("non-dict paid_addons must not enable link_buttons, got %v", cfg.LinkButtons)
	}
}

func TestAddonGateWatermarkIsNotGated(t *testing.T) {
	// The watermark is NOT a paid add-on: Python's apply_content_transforms
	// applies it unconditionally, so the Go projection must not gate it on
	// paid_addons. A regression that added the gate would silently drop
	// every owner's existing watermark.
	meta := map[string]any{
		"watermark_text": "© me",
		"paid_addons":    map[string]any{},
	}
	cfg := configFrom(t, meta)
	if cfg.Watermark != "© me" {
		t.Errorf("watermark must project without any entitlement, got %q", cfg.Watermark)
	}
}
