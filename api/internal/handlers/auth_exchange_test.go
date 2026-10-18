package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	grpcclient "autoforward/internal/grpcclient"
	pb "autoforward/proto"

	"google.golang.org/grpc"
)

// fakeAccountClient records what the exchange path asks the core to do.
type fakeAccountClient struct {
	pb.AccountControlServiceClient

	issuedFor      []int64
	exchangedCodes []string
	exchangeResp   *pb.LoginAccountResponse
	validateResp   *pb.ValidateTokenResponse
}

func (f *fakeAccountClient) IssueWebToken(_ context.Context, req *pb.IssueWebTokenRequest, _ ...grpc.CallOption) (*pb.LoginAccountResponse, error) {
	f.issuedFor = append(f.issuedFor, req.GetUserId())
	return &pb.LoginAccountResponse{Success: true, Token: "minted-by-user-id"}, nil
}

func (f *fakeAccountClient) ExchangeWebLoginCode(_ context.Context, req *pb.ExchangeWebLoginCodeRequest, _ ...grpc.CallOption) (*pb.LoginAccountResponse, error) {
	f.exchangedCodes = append(f.exchangedCodes, req.GetLoginCode())
	if f.exchangeResp != nil {
		return f.exchangeResp, nil
	}
	return &pb.LoginAccountResponse{
		Success: true, Token: "session-token", Message: "Login successful",
		Account: &pb.WebAccount{UserId: 111, Username: "tg_111"},
	}, nil
}

func (f *fakeAccountClient) ValidateToken(_ context.Context, _ *pb.ValidateTokenRequest, _ ...grpc.CallOption) (*pb.ValidateTokenResponse, error) {
	if f.validateResp != nil {
		return f.validateResp, nil
	}
	return &pb.ValidateTokenResponse{Valid: false, Error: "INVALID_OR_EXPIRED"}, nil
}

func exchangeRequest(t *testing.T, body string) *http.Request {
	t.Helper()
	return httptest.NewRequest("POST", "/api/auth/exchange-bot-token", strings.NewReader(body))
}

// A Telegram user id is public. Posting one must never mint a session: that is
// exactly the cross-tenant hole this endpoint used to have.
func TestAuthBotExchange_RefusesUserIdWithoutCode(t *testing.T) {
	fake := &fakeAccountClient{}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	rec := httptest.NewRecorder()
	h.AuthBotExchange(rec, exchangeRequest(t, `{"user_id": 5094837833}`))

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("posting a user id must be refused with 401, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(fake.issuedFor) != 0 {
		t.Fatalf("no token may be minted from a user id, mints=%v", fake.issuedFor)
	}
	if len(fake.exchangedCodes) != 0 {
		t.Fatalf("no code was supplied, exchange must not be called: %v", fake.exchangedCodes)
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %s", rec.Body.String())
	}
	if body["error_code"] != "LOGIN_CODE_REQUIRED" {
		t.Fatalf("error_code: %q", body["error_code"])
	}
	if _, leaked := body["token"]; leaked {
		t.Fatalf("a refusal must not carry a token: %s", rec.Body.String())
	}
}

func TestAuthBotExchange_RefusesBlankCode(t *testing.T) {
	fake := &fakeAccountClient{}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	for _, body := range []string{`{"login_code": "   "}`, `{"login_code": ""}`, `{}`} {
		rec := httptest.NewRecorder()
		h.AuthBotExchange(rec, exchangeRequest(t, body))
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("blank code %s must 401, got %d", body, rec.Code)
		}
		if len(fake.exchangedCodes) != 0 {
			t.Fatalf("a blank code must not reach the core: %v", fake.exchangedCodes)
		}
	}
}

// A refused exchange (used, expired, unknown) is a 401 that carries no token.
func TestAuthBotExchange_RefusedCodeIsUnauthorized(t *testing.T) {
	fake := &fakeAccountClient{exchangeResp: &pb.LoginAccountResponse{
		Success:   false,
		Message:   "This login link is invalid, expired, or was already used.",
		ErrorCode: "INVALID_LOGIN_CODE",
	}}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	rec := httptest.NewRecorder()
	h.AuthBotExchange(rec, exchangeRequest(t, `{"login_code": "already-used"}`))

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "INVALID_LOGIN_CODE") {
		t.Fatalf("refusal must name the result code: %s", rec.Body.String())
	}
	var parsed map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &parsed); err != nil {
		t.Fatalf("body is not JSON: %s", rec.Body.String())
	}
}

func TestAuthBotExchange_AcceptsCode(t *testing.T) {
	fake := &fakeAccountClient{}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	rec := httptest.NewRecorder()
	h.AuthBotExchange(rec, exchangeRequest(t, `{"login_code": "fresh-code"}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(fake.exchangedCodes) != 1 || fake.exchangedCodes[0] != "fresh-code" {
		t.Fatalf("the code must be the only thing sent to the core: %v", fake.exchangedCodes)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %s", rec.Body.String())
	}
	if body["token"] != "session-token" {
		t.Fatalf("token missing from response: %s", rec.Body.String())
	}
}

// The code names the account; a posted user id must not be able to redirect it.
func TestAuthBotExchange_CodeDecidesTheAccount(t *testing.T) {
	fake := &fakeAccountClient{exchangeResp: &pb.LoginAccountResponse{
		Success: true, Token: "session-token-for-111",
		Account: &pb.WebAccount{UserId: 111, Username: "tg_111"},
	}}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	rec := httptest.NewRecorder()
	h.AuthBotExchange(rec, exchangeRequest(t, `{"login_code": "code-of-111", "user_id": 5094837833}`))

	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %s", rec.Body.String())
	}
	acct, _ := body["account"].(map[string]any)
	if acct == nil || acct["user_id"].(float64) != 111 {
		t.Fatalf("the code's account must win, body=%s", rec.Body.String())
	}
	if len(fake.issuedFor) != 0 {
		t.Fatalf("still no user-id minting: %v", fake.issuedFor)
	}
}

// ------------------------------------------------------------------ password

func TestChangePassword_RequiresToken(t *testing.T) {
	fake := &fakeAccountClient{}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	req := httptest.NewRequest("PATCH", "/api/auth/change-password",
		strings.NewReader(`{"user_id": 222, "old_password": "x", "new_password": "longenough"}`))
	rec := httptest.NewRecorder()
	h.AuthChangePassword(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("an unauthenticated password change must 401, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestChangePassword_RefusesForeignUserId(t *testing.T) {
	fake := &fakeAccountClient{validateResp: &pb.ValidateTokenResponse{Valid: true, UserId: 111}}
	h := &Handlers{c: &grpcclient.Clients{Account: fake}}

	req := httptest.NewRequest("PATCH", "/api/auth/change-password",
		strings.NewReader(`{"user_id": 222, "old_password": "x", "new_password": "longenough"}`))
	req.Header.Set("Authorization", "Bearer token-of-111")
	rec := httptest.NewRecorder()
	h.AuthChangePassword(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("changing another account's password must 403, got %d: %s", rec.Code, rec.Body.String())
	}
}
