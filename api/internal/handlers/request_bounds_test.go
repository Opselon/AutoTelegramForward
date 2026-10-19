package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pb "autoforward/proto"
)

// Every JSON endpoint decodes its body through bind/bindPB. Neither used to
// bound the body, so the size of the allocation was chosen by the caller: a
// single POST could make the API read an arbitrarily large body into memory
// before any handler-level validation (maxMetadataBytes, the 64 KiB task
// payload) ever ran. These tests pin the shared cap and the refusal shape.
//
// The refusal is a 413 rather than a 400 because "your body is too big" is a
// different client bug from "your JSON is malformed", and a client that retries
// on 400 would otherwise loop on a request that can never succeed.

// requireSaneBodyCap pins the value of the cap to a usable band: small enough
// that a legitimate dashboard/mobile payload is never refused, large enough
// that one request still cannot decide this process's allocation.
//
// It also keeps the tests below honest. They size their fixtures FROM the
// constant, so a cap mutated to a terabyte would turn "the refusal is missing"
// into a terabyte allocation (and an OOM) instead of a clean assertion failure.
func requireSaneBodyCap(t *testing.T) {
	t.Helper()
	const floor, ceiling = 64 << 10, 8 << 20
	if maxJSONBodyBytes < floor || maxJSONBodyBytes > ceiling {
		t.Fatalf("maxJSONBodyBytes = %d is outside the sane band [%d, %d]",
			maxJSONBodyBytes, floor, ceiling)
	}
}

func TestJSONBodyCapIsWithinASaneBand(t *testing.T) {
	requireSaneBodyCap(t)
}

func oversizedJSONBody(t *testing.T) string {
	t.Helper()
	requireSaneBodyCap(t)
	return `{"blob":"` + strings.Repeat("A", int(maxJSONBodyBytes)+4096) + `"}`
}

func TestBind_RefusesBodyOverTheLimit(t *testing.T) {
	req := httptest.NewRequest("POST", "/api/auth/login", strings.NewReader(oversizedJSONBody(t)))
	rec := httptest.NewRecorder()

	var target map[string]any
	if bind(rec, req, &target) {
		t.Fatal("bind accepted a body larger than the cap")
	}
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413 for an oversized body, got %d: %s", rec.Code, rec.Body.String())
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("refusal must be JSON: %v (%s)", err, rec.Body.String())
	}
	if body["error_code"] != "body_too_large" {
		t.Fatalf("refusal must name the reason, got %q", body["error_code"])
	}
}

// The cap must not be so tight that it breaks a legitimate payload: a body
// exactly at the limit still parses.
func TestBind_AcceptsBodyAtTheLimit(t *testing.T) {
	requireSaneBodyCap(t)
	payload := `{"blob":"` + strings.Repeat("A", int(maxJSONBodyBytes)-64) + `"}`
	if int64(len(payload)) > maxJSONBodyBytes {
		t.Fatalf("test fixture is wrong: %d bytes > cap %d", len(payload), maxJSONBodyBytes)
	}
	req := httptest.NewRequest("POST", "/api/auth/login", strings.NewReader(payload))
	rec := httptest.NewRecorder()

	var target map[string]string
	if !bind(rec, req, &target) {
		t.Fatalf("bind rejected a body inside the cap: %d %s", rec.Code, rec.Body.String())
	}
	if len(target["blob"]) != int(maxJSONBodyBytes)-64 {
		t.Fatalf("body was truncated: got %d chars", len(target["blob"]))
	}
}

// bindPB reads the whole body with io.ReadAll, so before the cap it was the
// sharper edge of the same defect: an unbounded allocation with no decoder to
// stop at the first JSON value.
func TestBindPB_RefusesBodyOverTheLimit(t *testing.T) {
	msg := &pb.FilterRule{}
	req := httptest.NewRequest("PUT", "/api/filters/x", strings.NewReader(oversizedJSONBody(t)))
	rec := httptest.NewRecorder()

	if bindPB(rec, req, msg) {
		t.Fatal("bindPB accepted a body larger than the cap")
	}
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413 for an oversized body, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestBindPB_AcceptsBodyInsideTheLimit(t *testing.T) {
	msg := &pb.FilterRule{}
	req := httptest.NewRequest("PUT", "/api/filters/x", strings.NewReader(`{"id":"f1","owner_user_id":111}`))
	rec := httptest.NewRecorder()

	if !bindPB(rec, req, msg) {
		t.Fatalf("bindPB rejected a small body: %d %s", rec.Code, rec.Body.String())
	}
	if msg.GetId() != "f1" {
		t.Fatalf("protojson did not populate the message: %+v", msg)
	}
}

// ---------------------------------------------------------------------------
// POST /tasks: the body must be read whenever there is one.
// ---------------------------------------------------------------------------

// `POST /api/tasks?kind=<kind>` is a documented convenience for a client with
// no body. The guard that allowed it also swallowed the body of a request that
// carried BOTH a query kind and a JSON body — so the payload (the session being
// backed up, the delivery being replayed) and the idempotency key were silently
// dropped and the task ran on an empty payload. A dropped payload is worse than
// a refusal: the caller gets a 201 for a job that cannot do what it asked.
func TestCreateTask_ParsesBodyEvenWhenKindIsInTheQuery(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	body := `{"payload":{"session_id":"s-1"},"idempotency_key":"k-1","max_attempts":3}`
	req := httptest.NewRequest("POST", "/api/tasks?kind=session.backup", strings.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.CreateTask(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(tc.createdCreate) != 1 {
		t.Fatalf("expected one create call, saw %d", len(tc.createdCreate))
	}
	got := tc.createdCreate[0]
	if got.GetPayloadJson() == "" {
		t.Fatal("payload in the body was silently dropped")
	}
	if got.GetIdempotencyKey() != "k-1" {
		t.Fatalf("idempotency_key was dropped: %q", got.GetIdempotencyKey())
	}
	if got.GetMaxAttempts() != 3 {
		t.Fatalf("max_attempts was dropped: %d", got.GetMaxAttempts())
	}
	// The query value and the body must not be able to disagree silently: the
	// body wins because it is the richer, explicit form.
	if got.GetKind() != "session.backup" {
		t.Fatalf("kind: %q", got.GetKind())
	}
}

// The body's kind is authoritative when both are present, even if the query
// names a different (still valid) kind.
func TestCreateTask_BodyKindWinsOverQueryKind(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	body := `{"kind":"dlq.purge","payload":{"rule_id":"rule-1"}}`
	req := httptest.NewRequest("POST", "/api/tasks?kind=session.backup", strings.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.CreateTask(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := tc.createdCreate[0]; got.GetKind() != "dlq.purge" {
		t.Fatalf("body kind must win, got %q", got.GetKind())
	}
}

// A bare query-only submission still works: that is the form the convenience
// exists for, and the fix must not remove it.
func TestCreateTask_BareQueryKindStillWorks(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	req := httptest.NewRequest("POST", "/api/tasks?kind=backup.export", nil)
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.CreateTask(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	if got := tc.createdCreate[0]; got.GetKind() != "backup.export" {
		t.Fatalf("kind: %q", got.GetKind())
	}
}

// A malformed body must not be ignored just because a query kind is present:
// silently dropping it is how the defect above hid.
func TestCreateTask_RejectsMalformedBodyEvenWithQueryKind(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	req := httptest.NewRequest("POST", "/api/tasks?kind=session.backup", strings.NewReader("{not json"))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.CreateTask(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for malformed JSON, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(tc.createdCreate) != 0 {
		t.Fatal("a malformed body must never reach the core")
	}
}

// The tasks payload is bounded by the core, but the API must not buffer an
// arbitrarily large one on the way there either.
func TestCreateTask_RefusesOversizedPayloadBody(t *testing.T) {
	requireSaneBodyCap(t)
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	body := `{"kind":"session.backup","payload":{"blob":"` +
		strings.Repeat("A", int(maxJSONBodyBytes)+1024) + `"}}`
	req := httptest.NewRequest("POST", "/api/tasks", strings.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: 111}))
	rec := httptest.NewRecorder()

	h.CreateTask(rec, req)

	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(tc.createdCreate) != 0 {
		t.Fatal("an oversized body must never reach the core")
	}
}
