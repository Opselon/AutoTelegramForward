package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestWriteJSON_Encodes(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusOK, map[string]string{"a": "b"})
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content type: %s", ct)
	}
	var got map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || got["a"] != "b" {
		t.Fatalf("body: %s", rec.Body.String())
	}
}

func TestBind_InvalidJSON(t *testing.T) {
	req := httptest.NewRequest("POST", "/", strings.NewReader("{invalid"))
	rec := httptest.NewRecorder()
	if bind(rec, req, &struct{}{}) {
		t.Fatal("bind should fail on invalid json")
	}
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

func TestBind_ValidJSON(t *testing.T) {
	req := httptest.NewRequest("POST", "/", strings.NewReader(`{"session_id":"abc"}`))
	rec := httptest.NewRecorder()
	var target backupReq
	if !bind(rec, req, &target) {
		t.Fatal("bind should succeed on valid json")
	}
	if target.SessionID != "abc" {
		t.Fatalf("parsed: %+v", target)
	}
}

func TestHealth(t *testing.T) {
	// Health handler doesn't touch gRPC, safe to test via nil clients.
	h := &Handlers{}
	req := httptest.NewRequest("GET", "/healthz", nil)
	rec := httptest.NewRecorder()
	h.Health(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body map[string]string
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["status"] != "ok" {
		t.Fatalf("body: %s", rec.Body.String())
	}
}
