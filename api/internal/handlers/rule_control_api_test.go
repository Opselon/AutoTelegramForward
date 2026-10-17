package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"

	pb "autoforward/proto"

	"autoforward/internal/grpcclient"
)

// --------------------------------------------------------------------------- //
// update_mask plumbing
// --------------------------------------------------------------------------- //

func TestExtractUpdateMask_FromBodyQueryAndAliases(t *testing.T) {
	cases := []struct {
		name   string
		url    string
		body   string
		expect []string
	}{
		{"body array", "/rules/x", `{"update_mask":["a","b"]}`, []string{"a", "b"}},
		{"body camelCase", "/rules/x", `{"updateMask":["c"]}`, []string{"c"}},
		{"body single string", "/rules/x", `{"update_mask":"d"}`, []string{"d"}},
		{"query comma list", "/rules/x?update_mask=a,%20b", `{}`, []string{"a", "b"}},
		{"query fields alias", "/rules/x?fields=c", `{}`, []string{"c"}},
		{"trimmed and deduped", "/rules/x", `{"update_mask":[" a ","a","","b"]}`, []string{"a", "b"}},
		{"absent", "/rules/x", `{"name":"n"}`, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("PATCH", tc.url, strings.NewReader(tc.body))
			got := extractUpdateMask(req, []byte(tc.body))
			if len(got) != len(tc.expect) {
				t.Fatalf("got %v, want %v", got, tc.expect)
			}
			for i := range got {
				if got[i] != tc.expect[i] {
					t.Fatalf("got %v, want %v", got, tc.expect)
				}
			}
		})
	}
}

// maskCapturingClient records what the handler forwarded to the core, including
// the mask — without which a client can never set a value to zero.
type maskCapturingClient struct {
	pb.ForwardRuleControlServiceClient
	gotMask    []string
	gotRule    *pb.ForwardRule
	gotOwner   int64
	settingsRc *pb.RuleSettingsResponse
}

func (f *maskCapturingClient) ListRules(ctx context.Context, in *pb.ListRulesRequest, opts ...grpc.CallOption) (*pb.ListRulesResponse, error) {
	return &pb.ListRulesResponse{Rules: []*pb.ForwardRule{{Id: "rule-a", OwnerUserId: in.OwnerUserId}}}, nil
}

func (f *maskCapturingClient) UpdateRule(ctx context.Context, in *pb.UpdateRuleRequest, opts ...grpc.CallOption) (*pb.ForwardRule, error) {
	f.gotMask = in.UpdateMask
	f.gotRule = in.Rule
	f.gotOwner = in.Rule.OwnerUserId
	return in.Rule, nil
}

func (f *maskCapturingClient) GetRuleSettings(ctx context.Context, in *pb.RuleSettingsRequest, opts ...grpc.CallOption) (*pb.RuleSettingsResponse, error) {
	if f.settingsRc != nil {
		return f.settingsRc, nil
	}
	return &pb.RuleSettingsResponse{Success: true, SettingsJson: `{"remove_emojis":true}`, SchemaJson: `{"version":1}`}, nil
}

func (f *maskCapturingClient) UpdateRuleSettings(ctx context.Context, in *pb.UpdateRuleSettingsRequest, opts ...grpc.CallOption) (*pb.RuleSettingsResponse, error) {
	if f.settingsRc != nil {
		return f.settingsRc, nil
	}
	return &pb.RuleSettingsResponse{Success: true, Message: "1 setting(s) updated", SettingsJson: in.SettingsJson}, nil
}

func (f *maskCapturingClient) GetRuleSettingsSchema(ctx context.Context, in *pb.RuleSettingsSchemaRequest, opts ...grpc.CallOption) (*pb.RuleSettingsResponse, error) {
	return &pb.RuleSettingsResponse{Success: true, SchemaJson: `{"version":1,"count":2}`}, nil
}

func patchRule(t *testing.T, h *Handlers, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest("PATCH", "/api/rules/rule-a", strings.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.UpdateRule(rec, req)
	return rec
}

func TestUpdateRule_ForwardsTheMaskAndKeepsUnnamedFieldsAlone(t *testing.T) {
	rc := &maskCapturingClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"delaySeconds":0,"name":"changed","update_mask":["delay_seconds"]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(rc.gotMask) != 1 || rc.gotMask[0] != "delay_seconds" {
		t.Fatalf("mask not forwarded: %v", rc.gotMask)
	}
	// The handler must not ship the other body fields into an unguarded write:
	// with a mask, an unnamed field has to reach the core empty so the stored
	// value survives.
	if rc.gotRule.Name != "" {
		t.Fatalf("masked update leaked an unnamed field: name=%q", rc.gotRule.Name)
	}
	if rc.gotRule.SourceChatId != "" || len(rc.gotRule.SourceChatIds) != 0 {
		t.Fatalf("masked update leaked sources: %q %v", rc.gotRule.SourceChatId, rc.gotRule.SourceChatIds)
	}
	if rc.gotOwner != 111 {
		t.Fatalf("owner not forwarded, got %d", rc.gotOwner)
	}
}

func TestUpdateRule_NoMaskKeepsLegacyFullBodySemantics(t *testing.T) {
	rc := &maskCapturingClient{}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}

	rec := patchRule(t, h, `{"name":"changed","delaySeconds":5}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if len(rc.gotMask) != 0 {
		t.Fatalf("expected no mask, got %v", rc.gotMask)
	}
	if rc.gotRule.Name != "changed" {
		t.Fatalf("legacy path must still carry present fields, got %q", rc.gotRule.Name)
	}
}

func TestUpdateRule_RejectsForeignRule(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{Rules: &fakeRulesClient{
		owned: []*pb.ForwardRule{{Id: "rule-a", OwnerUserId: 222}},
	}}}
	rec := patchRule(t, h, `{"name":"x"}`)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign rule update must 404, got %d", rec.Code)
	}
}

// --------------------------------------------------------------------------- //
// settings endpoints
// --------------------------------------------------------------------------- //

func TestGetRuleSettings_RejectsForeignRule(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{Rules: &fakeRulesClient{
		owned: []*pb.ForwardRule{{Id: "rule-a", OwnerUserId: 222}},
	}}}
	req := httptest.NewRequest("GET", "/api/rules/rule-a/settings", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.GetRuleSettings(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("foreign settings read must 404, got %d", rec.Code)
	}
}

func TestRuleSettings_FailClosedWithoutAnOwner(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{Rules: &maskCapturingClient{}}}
	for _, tc := range []struct {
		name string
		call http.HandlerFunc
		verb string
	}{
		{"get", h.GetRuleSettings, "GET"},
		{"update", h.UpdateRuleSettings, "PATCH"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.verb, "/api/rules/rule-a/settings", strings.NewReader(`{"remove_emojis":true}`))
			// no identity in the context at all
			req.SetPathValue("id", "rule-a")
			rec := httptest.NewRecorder()
			tc.call(rec, req)
			if rec.Code != http.StatusNotFound {
				t.Fatalf("unlinked caller must be refused, got %d", rec.Code)
			}
		})
	}
}

func TestGetRuleSettings_ReturnsSettingsAndSchema(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{Rules: &maskCapturingClient{}}}
	req := httptest.NewRequest("GET", "/api/rules/rule-a/settings", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.GetRuleSettings(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad body: %v", err)
	}
	if body["settings"] == nil || body["schema"] == nil {
		t.Fatalf("settings/schema missing: %s", rec.Body.String())
	}
}

func TestUpdateRuleSettings_ValidationErrorBecomesA4xxNotA200(t *testing.T) {
	rc := &maskCapturingClient{settingsRc: &pb.RuleSettingsResponse{
		Success: false, ErrorCode: "unknown_setting", Message: "unknown setting 'nope'",
	}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
	req := httptest.NewRequest("PATCH", "/api/rules/rule-a/settings", strings.NewReader(`{"nope":1}`))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.UpdateRuleSettings(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("rejected settings write must be a 4xx, got %d", rec.Code)
	}
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["error_code"] != "unknown_setting" {
		t.Fatalf("error_code not surfaced: %s", rec.Body.String())
	}
}

func TestUpdateRuleSettings_NotFoundMapsTo404(t *testing.T) {
	rc := &maskCapturingClient{settingsRc: &pb.RuleSettingsResponse{
		Success: false, ErrorCode: "not_found",
	}}
	h := &Handlers{c: &grpcclient.Clients{Rules: rc}}
	req := httptest.NewRequest("PATCH", "/api/rules/rule-a/settings", strings.NewReader(`{"remove_emojis":true}`))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	req.SetPathValue("id", "rule-a")
	rec := httptest.NewRecorder()
	h.UpdateRuleSettings(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestGetRuleSettingsSchema_NeedsNoRule(t *testing.T) {
	h := &Handlers{c: &grpcclient.Clients{Rules: &maskCapturingClient{}}}
	req := httptest.NewRequest("GET", "/api/rules/settings/schema", nil)
	rec := httptest.NewRecorder()
	h.GetRuleSettingsSchema(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["schema"] == nil {
		t.Fatalf("schema missing: %s", rec.Body.String())
	}
}

func TestParseSettingsBody_WrappedAndBare(t *testing.T) {
	wrapped, replace, edited, _, err := parseSettingsBody([]byte(`{"settings":{"a":1},"replace_all":true}`))
	if err != nil || !replace || wrapped["a"] != float64(1) {
		t.Fatalf("wrapped body mis-parsed: %v %v %v", wrapped, replace, err)
	}
	bare, replace2, edited2, _, err := parseSettingsBody([]byte(`{"a":1,"b":"x"}`))
	if err != nil || replace2 || bare["a"] != float64(1) || bare["b"] != "x" {
		t.Fatalf("bare body mis-parsed: %v %v %v", bare, replace2, err)
	}
	if edited != nil || edited2 != nil {
		t.Fatalf("no changed list was sent, so none should be reported: %v %v", edited, edited2)
	}
	// A form posts every field; `changed` is how it says which one the user
	// actually edited, so the rest are not pinned onto the task.
	_, _, named, _, err := parseSettingsBody(
		[]byte(`{"settings":{"a":1,"b":"x"},"changed":["b"]}`))
	if err != nil || len(named) != 1 || named[0] != "b" {
		t.Fatalf("changed list mis-parsed: %v %v", named, err)
	}
	if _, _, _, _, err := parseSettingsBody([]byte(`{"settings":{},"changed":"b"}`)); err == nil {
		t.Fatal("a non-list changed value must be rejected")
	}
	// The bare shape honours it too, and `changed` is never itself a setting.
	bareEdited, _, named2, _, err := parseSettingsBody([]byte(`{"a":1,"changed":["a"]}`))
	if err != nil || bareEdited["a"] != float64(1) || len(named2) != 1 || named2[0] != "a" {
		t.Fatalf("bare body with changed mis-parsed: %v %v %v", bareEdited, named2, err)
	}
	if _, present := bareEdited["changed"]; present {
		t.Fatal("changed must not appear as a setting")
	}
	if _, _, _, _, err := parseSettingsBody([]byte(`[1,2]`)); err == nil {
		t.Fatal("a non-object body must be rejected")
	}
	if _, _, _, _, err := parseSettingsBody([]byte(`{"settings":"nope"}`)); err == nil {
		t.Fatal("a non-object settings value must be rejected")
	}
}

// TestParseSettingsBody_Reset covers the verb a client needs to hand a setting
// back to the bot's default. It travels next to the values rather than inside
// them: handing a key back is the opposite of writing one, and a client must be
// able to say it without inventing a value.
func TestParseSettingsBody_Reset(t *testing.T) {
	values, _, _, resets, err := parseSettingsBody(
		[]byte(`{"settings":{},"reset":["remove_emojis","link_policy"]}`))
	if err != nil {
		t.Fatalf("reset body rejected: %v", err)
	}
	if len(values) != 0 || len(resets) != 2 || resets[1] != "link_policy" {
		t.Fatalf("reset list mis-parsed: values=%v resets=%v", values, resets)
	}
	// A bare body honours it and never leaks it in as a setting name.
	bare, _, _, bareResets, err := parseSettingsBody([]byte(`{"a":1,"reset":["a"]}`))
	if err != nil || bare["a"] != float64(1) || len(bareResets) != 1 {
		t.Fatalf("bare body with reset mis-parsed: %v %v %v", bare, bareResets, err)
	}
	if _, present := bare["reset"]; present {
		t.Fatal("reset must not appear as a setting")
	}
	// Both verbs in one request: the values are written and the named keys are
	// handed back, which is how a form saves alongside a "use the default" click.
	both, _, changed, bothResets, err := parseSettingsBody(
		[]byte(`{"settings":{"remove_emojis":true},"changed":["remove_emojis"],"reset":["link_policy"]}`))
	if err != nil || both["remove_emojis"] != true || len(changed) != 1 || len(bothResets) != 1 {
		t.Fatalf("combined body mis-parsed: %v %v %v %v", both, changed, bothResets, err)
	}
	if _, _, _, _, err := parseSettingsBody([]byte(`{"settings":{},"reset":"link_policy"}`)); err == nil {
		t.Fatal("a non-list reset value must be rejected")
	}
}

// --------------------------------------------------------------------------- //
// response shape
// --------------------------------------------------------------------------- //

func TestFormatRuleJSON_ExposesTheV17SurfaceAndSettings(t *testing.T) {
	rl := &pb.ForwardRule{
		Id: "r1", SourceChatId: "-100", SourceChatIds: []string{"-100", "-200"},
		SourceChatNames: []string{"A", "B"}, ContentMode: "TEXT_ONLY",
		DelaySeconds: 2.5, SkipHistory: true, SinceTs: 1700000000,
		IgnoreEdits: true, SyncEdits: true, SyncDeletes: true,
		TriggerEvents: []string{"NEW_MESSAGE"}, TopicId: "42", LinkPolicy: "STRIP_ALL",
		DomainAllowlist: []string{"a.com"}, AllowedMediaTypes: []string{"photo"},
		SplitLongCaption: true, AlbumAggregationWindowSeconds: 1.25,
		IntermediateMode: "GATE", RetryPolicyJson: `{"max_attempts":3}`,
		ProxyJson: `{"host":"p"}`, SettingsJson: `{"remove_emojis":true}`,
	}
	out := formatRuleJSON(rl)
	if got := out["source_chat_ids"]; got == nil {
		t.Fatal("source_chat_ids missing")
	}
	for _, key := range []string{"content_mode", "delay_seconds", "skip_history",
		"since_ts", "ignore_edits", "sync_edits", "sync_deletes", "trigger_events",
		"topic_id", "link_policy", "domain_allowlist", "allowed_media_types",
		"split_long_caption", "album_aggregation_window_seconds",
		"intermediate_mode", "retry_policy", "proxy", "settings"} {
		if _, ok := out[key]; !ok {
			t.Fatalf("%s missing from the rule payload", key)
		}
	}
	settings, ok := out["settings"].(map[string]any)
	if !ok || settings["remove_emojis"] != true {
		t.Fatalf("settings snapshot not decoded: %#v", out["settings"])
	}
	retry, ok := out["retry_policy"].(map[string]any)
	if !ok || retry["max_attempts"] != float64(3) {
		t.Fatalf("retry_policy not decoded: %#v", out["retry_policy"])
	}
	if out["topic_id"] != "42" {
		t.Fatalf("topic_id wrong: %#v", out["topic_id"])
	}
}

func TestFormatRuleJSON_SurvivesMalformedJSONColumns(t *testing.T) {
	rl := &pb.ForwardRule{
		Id: "r1", RetryPolicyJson: "{not json", ProxyJson: "<xml/>",
		SettingsJson: "broken", CustomMetadataJson: "]}",
	}
	out := formatRuleJSON(rl) // must not panic
	if _, ok := out["retry_policy"].(map[string]any); !ok {
		t.Fatalf("malformed retry_policy should degrade to an empty object, got %#v", out["retry_policy"])
	}
	if out["proxy"] != nil {
		t.Fatalf("malformed proxy should degrade to nil, got %#v", out["proxy"])
	}
	if _, ok := out["settings"]; ok {
		t.Fatalf("malformed settings_json must be omitted, not guessed: %#v", out["settings"])
	}
}

func TestFormatRuleJSON_PrefersMetadataWhenTheProtoFieldIsUnset(t *testing.T) {
	rl := &pb.ForwardRule{Id: "r1", CustomMetadataJson: `{"topic_id":42,"prompt_id":"p-1"}`}
	out := formatRuleJSON(rl)
	if out["topic_id"] != float64(42) {
		t.Fatalf("topic_id should fall back to metadata, got %#v", out["topic_id"])
	}
	if out["prompt_id"] != "p-1" {
		t.Fatalf("prompt_id should fall back to metadata, got %#v", out["prompt_id"])
	}
	rl2 := &pb.ForwardRule{Id: "r2", TopicId: "7", CustomMetadataJson: `{"topic_id":42}`}
	if formatRuleJSON(rl2)["topic_id"] != "7" {
		t.Fatal("the proto field must win when it is set")
	}
}
