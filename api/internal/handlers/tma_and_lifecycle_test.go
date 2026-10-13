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
