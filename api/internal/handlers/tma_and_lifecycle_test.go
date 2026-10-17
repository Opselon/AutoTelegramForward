package handlers

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"sort"
	"strings"
	"testing"
	"time"

	pb "autoforward/proto"
)

func TestEnrichCustomMetadata_And_FormatRuleJSON(t *testing.T) {
	rule := &pb.ForwardRule{
		Id:                 "test_rule_1",
		Name:               "Original Task",
		CustomMetadataJson: `{"custom_flag":true}`,
	}

	rawBody := []byte(`{
		"topic_id": 42,
		"debug_mode": true,
		"sender_type": "Personal Telegram Account",
		"duplicate_filters": true,
		"crypto_mode": true,
		"auto_posting": true,
		"paid_addons": {"channel_comments": true}
	}`)

	enrichCustomMetadata(rawBody, rule)

	var meta map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
		t.Fatalf("failed to unmarshal enriched metadata: %v", err)
	}

	if meta["custom_flag"] != true {
		t.Errorf("expected custom_flag to be preserved")
	}
	if fmt.Sprintf("%v", meta["topic_id"]) != "42" {
		t.Errorf("expected topic_id 42, got %v", meta["topic_id"])
	}
	if meta["debug_mode"] != true {
		t.Errorf("expected debug_mode true, got %v", meta["debug_mode"])
	}
	if meta["duplicate_filters"] != true {
		t.Errorf("expected duplicate_filters true, got %v", meta["duplicate_filters"])
	}

	formatted := formatRuleJSON(rule)
	if formatted["id"] != "test_rule_1" {
		t.Errorf("expected id test_rule_1, got %v", formatted["id"])
	}
	if formatted["topic_id"] != float64(42) && fmt.Sprintf("%v", formatted["topic_id"]) != "42" {
		t.Errorf("expected topic_id expanded onto JSON, got %v", formatted["topic_id"])
	}
	if formatted["debug_mode"] != true {
		t.Errorf("expected debug_mode expanded onto JSON, got %v", formatted["debug_mode"])
	}
}

func TestAuthTMA_SignatureVerification(t *testing.T) {
	testBotToken := "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
	os.Setenv("ATF_BOT_TOKEN", testBotToken)
	defer os.Unsetenv("ATF_BOT_TOKEN")

	h := &Handlers{}

	// 1. Missing init_data returns 400
	req := httptest.NewRequest(http.MethodPost, "/api/auth/tma", bytes.NewBufferString(`{"init_data": ""}`))
	rec := httptest.NewRecorder()
	h.AuthTMA(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for empty init_data, got %d", rec.Code)
	}

	// 2. Build valid signed Telegram init_data
	now := time.Now().Unix()
	vals := url.Values{}
	vals.Set("auth_date", fmt.Sprintf("%d", now))
	vals.Set("query_id", "AAHdF6IQAAAAAN0XohDhrOrc")
	vals.Set("user", `{"id":5094837833,"first_name":"User","username":"testuser"}`)

	var keys []string
	for k := range vals {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var pairs []string
	for _, k := range keys {
		pairs = append(pairs, k+"="+vals.Get(k))
	}
	dataCheckString := strings.Join(pairs, "\n")

	hSecret := hmac.New(sha256.New, []byte("WebAppData"))
	hSecret.Write([]byte(testBotToken))
	secretKey := hSecret.Sum(nil)

	hData := hmac.New(sha256.New, secretKey)
	hData.Write([]byte(dataCheckString))
	validHash := hex.EncodeToString(hData.Sum(nil))

	vals.Set("hash", validHash)
	validInitData := vals.Encode()

	// 3. Test tampered hash returns 401
	tamperedInitData := strings.Replace(validInitData, validHash, "badhash123456", 1)
	reqTampered := httptest.NewRequest(http.MethodPost, "/api/auth/tma", bytes.NewBufferString(fmt.Sprintf(`{"init_data": %q}`, tamperedInitData)))
	recTampered := httptest.NewRecorder()
	h.AuthTMA(recTampered, reqTampered)
	if recTampered.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 for tampered hash, got %d", recTampered.Code)
	}

	// 4. Test expired auth_date returns 401
	expiredVals := url.Values{}
	expiredVals.Set("auth_date", fmt.Sprintf("%d", now-100000))
	expiredVals.Set("user", `{"id":5094837833,"first_name":"User"}`)

	var expKeys []string
	for k := range expiredVals {
		expKeys = append(expKeys, k)
	}
	sort.Strings(expKeys)
	var expPairs []string
	for _, k := range expKeys {
		expPairs = append(expPairs, k+"="+expiredVals.Get(k))
	}
	expCheckString := strings.Join(expPairs, "\n")

	hExp := hmac.New(sha256.New, secretKey)
	hExp.Write([]byte(expCheckString))
	expHash := hex.EncodeToString(hExp.Sum(nil))
	expiredVals.Set("hash", expHash)

	reqExpired := httptest.NewRequest(http.MethodPost, "/api/auth/tma", bytes.NewBufferString(fmt.Sprintf(`{"init_data": %q}`, expiredVals.Encode())))
	recExpired := httptest.NewRecorder()
	h.AuthTMA(recExpired, reqExpired)
	if recExpired.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 for expired init_data, got %d", recExpired.Code)
	}
}

// TestNormalizeAdvancedMetadataValidation covers the Phase 2 typed-validation
// contract: every known advanced field is coerced to its canonical type and an
// out-of-range or malformed value is normalized instead of persisted as
// garbage that could reach the worker.
func TestNormalizeAdvancedMetadataValidation(t *testing.T) {
	t.Run("negative and oversized delay is clamped", func(t *testing.T) {
		meta := map[string]any{"forward_delay_seconds": -5.0}
		normalizeAdvancedMetadata(meta)
		if v, _ := toFloat(meta["forward_delay_seconds"]); v != 0 {
			t.Fatalf("expected 0, got %v", v)
		}

		meta = map[string]any{"forward_delay_seconds": 999999.0}
		normalizeAdvancedMetadata(meta)
		if v, _ := toFloat(meta["forward_delay_seconds"]); v != maxDelaySeconds {
			t.Fatalf("expected %v, got %v", maxDelaySeconds, v)
		}
	})

	t.Run("non-numeric delay is dropped", func(t *testing.T) {
		meta := map[string]any{"forward_delay_seconds": "soon"}
		normalizeAdvancedMetadata(meta)
		if _, ok := meta["forward_delay_seconds"]; ok {
			t.Fatalf("malformed numeric field must be dropped")
		}
	})

	t.Run("string truthiness coerced to boolean", func(t *testing.T) {
		meta := map[string]any{"debug_mode": "true", "crypto_mode": "on", "auto_posting": "yes"}
		normalizeAdvancedMetadata(meta)
		for _, k := range []string{"debug_mode", "crypto_mode", "auto_posting"} {
			if b, ok := meta[k].(bool); !ok || !b {
				t.Fatalf("%s must be a true boolean, got %T(%v)", k, meta[k], meta[k])
			}
		}
	})

	t.Run("oversized watermark is truncated", func(t *testing.T) {
		meta := map[string]any{"watermark_text": strings.Repeat("W", maxWatermarkLen+50)}
		normalizeAdvancedMetadata(meta)
		if s, ok := meta["watermark_text"].(string); !ok || len(s) != maxWatermarkLen {
			t.Fatalf("watermark must be exactly %d chars, got %d", maxWatermarkLen, len(s))
		}
	})

	t.Run("negative topic id normalized to zero", func(t *testing.T) {
		meta := map[string]any{"topic_id": -3.0}
		normalizeAdvancedMetadata(meta)
		if v, ok := meta["topic_id"].(int); !ok || v != 0 {
			t.Fatalf("expected 0, got %T(%v)", meta["topic_id"], meta["topic_id"])
		}
	})

	t.Run("ai creativity out of range reset to default", func(t *testing.T) {
		meta := map[string]any{"ai_creativity": 5.0}
		normalizeAdvancedMetadata(meta)
		if v, _ := toFloat(meta["ai_creativity"]); v != 0.7 {
			t.Fatalf("expected 0.7 default, got %v", v)
		}
	})

	t.Run("oversized metadata blob is trimmed to known fields", func(t *testing.T) {
		meta := map[string]any{}
		meta["topic_id"] = 1
		meta["forward_delay_seconds"] = 5.0
		meta["junk_padding"] = strings.Repeat("x", maxMetadataBytes)
		normalizeAdvancedMetadata(meta)
		if _, ok := meta["junk_padding"]; ok {
			t.Fatalf("oversized blob must drop unknown fields")
		}
		if _, ok := meta["topic_id"]; !ok {
			t.Fatalf("known fields must survive trimming")
		}
	})
}

// TestEnrichCustomMetadataRejectsNothing covers the Phase 2 parity list: a
// client-supplied advanced setting must survive enrichment and re-appear in the
// formatted JSON exactly once.
func TestEnrichCustomMetadataRejectsNothing(t *testing.T) {
	rawBody := map[string]any{
		"topic_id":              7.0,
		"debug_mode":            true,
		"forward_delay_seconds": 4.5,
		"auto_delete_seconds":   120.0,
		"watermark_text":        "ATF",
		"paid_addons":           map[string]any{"crypto_mode": true},
		"keyword_reply":         "hello",
	}
	rule := &pb.ForwardRule{Id: "r1", CustomMetadataJson: ""}
	bodyJSON, err := json.Marshal(rawBody)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	enrichCustomMetadata(bodyJSON, rule)

	var parsed map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &parsed); err != nil {
		t.Fatalf("metadata json invalid: %v", err)
	}
	expected := []string{
		"topic_id", "debug_mode", "forward_delay_seconds",
		"auto_delete_seconds", "watermark_text", "paid_addons", "keyword_reply",
	}
	for _, k := range expected {
		if _, ok := parsed[k]; !ok {
			t.Fatalf("field %s missing from persisted metadata: %v", k, parsed)
		}
	}
}

// TestCleanerConfigNormalization covers the v2.0 cleaner feature's typed
// contract: the nested config is validated, bounded, and non-string patterns
// are dropped instead of stringified.
func TestCleanerConfigNormalization(t *testing.T) {
	t.Run("non-object cleaner config is dropped", func(t *testing.T) {
		meta := map[string]any{"cleaner_config": "not-an-object"}
		normalizeAdvancedMetadata(meta)
		if _, ok := meta["cleaner_config"]; ok {
			t.Fatalf("non-object cleaner_config must be dropped")
		}
	})

	t.Run("booleans coerced and patterns filtered", func(t *testing.T) {
		meta := map[string]any{
			"cleaner_enabled": "true",
			"cleaner_config": map[string]any{
				"patterns":       []any{"spam", 42, nil, "  ", "ok"},
				"remove_urls":    "yes",
				"use_regex":      1,
				"remove_emojis":  "on",
				"case_sensitive": false,
			},
		}
		normalizeAdvancedMetadata(meta)

		if b, ok := meta["cleaner_enabled"].(bool); !ok || !b {
			t.Fatalf("cleaner_enabled must be bool true, got %T(%v)", meta["cleaner_enabled"], meta["cleaner_enabled"])
		}

		cfg, ok := meta["cleaner_config"].(map[string]any)
		if !ok {
			t.Fatalf("cleaner_config must remain an object, got %T", meta["cleaner_config"])
		}
		pats, ok := cfg["patterns"].([]any)
		if !ok || len(pats) != 2 {
			t.Fatalf("patterns must be filtered to 2 strings, got %v", cfg["patterns"])
		}
		if pats[0] != "spam" || pats[1] != "ok" {
			t.Fatalf("unexpected patterns: %v", pats)
		}
		for _, k := range []string{"remove_urls", "use_regex", "remove_emojis"} {
			if b, ok := cfg[k].(bool); !ok || !b {
				t.Fatalf("%s must be coerced to true, got %T(%v)", k, cfg[k], cfg[k])
			}
		}
	})

	t.Run("pattern array is bounded", func(t *testing.T) {
		pats := make([]any, 0, maxCleanerPatterns+50)
		for i := 0; i < maxCleanerPatterns+50; i++ {
			pats = append(pats, "x")
		}
		meta := map[string]any{"cleaner_config": map[string]any{"patterns": pats}}
		normalizeAdvancedMetadata(meta)
		cfg, _ := meta["cleaner_config"].(map[string]any)
		got, _ := cfg["patterns"].([]any)
		if len(got) != maxCleanerPatterns {
			t.Fatalf("patterns must be capped at %d, got %d", maxCleanerPatterns, len(got))
		}
	})

	t.Run("v2 metadata round trips through enrichCustomMetadata", func(t *testing.T) {
		rawBody := map[string]any{
			"cleaner_enabled": true,
			"cleaner_config": map[string]any{
				"patterns":    []any{"spam"},
				"remove_urls": true,
				"language":    "pt",
			},
			"sync_replies":        true,
			"sync_edits":          true,
			"sync_deletes":        true,
			"show_forward_header": true,
			"duplicate_filters":   true,
		}
		rule := &pb.ForwardRule{Id: "r-v2", CustomMetadataJson: ""}
		bodyJSON, err := json.Marshal(rawBody)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		enrichCustomMetadata(bodyJSON, rule)

		var parsed map[string]any
		if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &parsed); err != nil {
			t.Fatalf("metadata invalid: %v", err)
		}
		for _, k := range []string{
			"cleaner_enabled", "cleaner_config", "sync_replies",
			"sync_edits", "sync_deletes", "show_forward_header",
			"duplicate_filters",
		} {
			if _, ok := parsed[k]; !ok {
				t.Fatalf("v2 field %s missing from persisted metadata", k)
			}
		}
	})
}
