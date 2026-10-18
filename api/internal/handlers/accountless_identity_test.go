package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
)

// --------------------------------------------------------------------------- //
// An account-less identity, on the dashboard surface
// --------------------------------------------------------------------------- //
//
// The core reports `user_id <= 0` for an account that has no Telegram id yet
// (RegisterAccount allocates synthetic negative ids for a web-only signup). Two
// things follow from that, and both are the reason this rule lives in ONE place:
//
//   - Owner-scoped reads treat owner 0 as the shared, unattributed bucket
//     rather than as "nobody's data".
//   - The backup surface documents owner 0 as "everything" (see the proto
//     comment on BackupExport), so an unscoped export is every tenant's rules,
//     sessions and API keys in one payload.
//
// A handler-by-handler guard was the previous state of the world: ListChats,
// CreateBotToken and RequireAdmin each refuse `id.UserID <= 0`, and the other
// forty-odd dashboard handlers do not. The sweep showed how easy that is to get
// wrong, so the refusal is applied once, at the point where the verified
// identity is created.

type fakeBackupClient struct {
	pb.BackupControlServiceClient

	exportCalls int
	importCalls int
	lastExport  *pb.BackupExportRequest
	lastImport  *pb.BackupImportRequest
}

func (f *fakeBackupClient) BackupExport(_ context.Context, in *pb.BackupExportRequest, _ ...grpc.CallOption) (*pb.BackupExportResponse, error) {
	f.exportCalls++
	f.lastExport = in
	return &pb.BackupExportResponse{Success: true, Bundle: "enc:v2:snapshot", Counts: map[string]int32{"rules": 1}}, nil
}

func (f *fakeBackupClient) BackupImport(_ context.Context, in *pb.BackupImportRequest, _ ...grpc.CallOption) (*pb.BackupImportResponse, error) {
	f.importCalls++
	f.lastImport = in
	return &pb.BackupImportResponse{Success: true}, nil
}

// dashboardRequest builds a request carrying a Bearer credential, as the
// middleware sees it: the credential itself is opaque here because the account
// client (not the header) decides who the caller is.
func dashboardRequest(method, target, body string) *http.Request {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer opaque-session-token")
	return req
}

// reqAsIdentity injects the verified identity the way the middleware does, for
// the tests that call a handler directly to pin the handler's own guard.
func reqAsIdentity(uid int64, isAdmin bool, method, target, body string) *http.Request {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{
		UserID: uid, IsAdmin: isAdmin,
	}))
}

// served runs the middleware in front of the handler, so the identity the
// handler acts on is the one the core reported for the token — the production
// path, not a hand-injected context.
func served(h *Handlers, handler http.HandlerFunc, method, target, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.authMiddleware(handler).ServeHTTP(rec, dashboardRequest(method, target, body))
	return rec
}

func backupHandlers(backup *fakeBackupClient, account *fakeAccountClient) *Handlers {
	return &Handlers{c: &grpcclient.Clients{Backup: backup, Account: account}}
}

func TestAuthMiddleware_RefusesAnAccountlessIdentity(t *testing.T) {
	for _, uid := range []int64{0, -1, -7} {
		inner := false
		account := &fakeAccountClient{validateResp: &pb.ValidateTokenResponse{Valid: true, UserId: uid}}
		h := &Handlers{c: &grpcclient.Clients{Account: account}}

		reached := http.HandlerFunc(func(http.ResponseWriter, *http.Request) { inner = true })
		rec := httptest.NewRecorder()
		h.authMiddleware(reached).ServeHTTP(rec, dashboardRequest("GET", "/sessions", ""))

		if inner {
			t.Fatalf("user id %d: an account-less identity reached a dashboard handler", uid)
		}
		if rec.Code != http.StatusForbidden {
			t.Fatalf("user id %d: want 403, got %d: %s", uid, rec.Code, rec.Body.String())
		}
		var body map[string]string
		_ = json.Unmarshal(rec.Body.Bytes(), &body)
		if body["code"] != "account_not_linked" {
			t.Fatalf("user id %d: the refusal must say why, got %v", uid, body)
		}
	}
}

func TestAuthMiddleware_AdmitsALinkedAccount(t *testing.T) {
	account := &fakeAccountClient{validateResp: &pb.ValidateTokenResponse{
		Valid: true, UserId: 5989354154, Username: "tg_5989354154",
	}}
	h := &Handlers{c: &grpcclient.Clients{Account: account}}

	var seen *Identity
	reached := http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		seen = identity(r)
	})
	rec := httptest.NewRecorder()
	h.authMiddleware(reached).ServeHTTP(rec, dashboardRequest("GET", "/sessions", ""))

	if seen == nil || seen.UserID != 5989354154 {
		t.Fatalf("a linked account must pass with its own identity, got %+v (status %d)", seen, rec.Code)
	}
}

func TestAuthMiddleware_StillRejectsAnInvalidToken(t *testing.T) {
	account := &fakeAccountClient{validateResp: &pb.ValidateTokenResponse{Valid: false, Error: "INVALID_OR_EXPIRED"}}
	h := &Handlers{c: &grpcclient.Clients{Account: account}}

	inner := false
	reached := http.HandlerFunc(func(http.ResponseWriter, *http.Request) { inner = true })
	rec := httptest.NewRecorder()
	h.authMiddleware(reached).ServeHTTP(rec, dashboardRequest("GET", "/sessions", ""))

	if inner || rec.Code != http.StatusUnauthorized {
		t.Fatalf("an invalid token must stay 401 and never reach a handler, got %d", rec.Code)
	}
}

// The backup routes carry the escalation, so they refuse the same caller on
// their own even if they are ever mounted outside the middleware.
func TestBackupExport_AccountlessIdentityNeverReachesTheCore(t *testing.T) {
	backup := &fakeBackupClient{}
	h := backupHandlers(backup, &fakeAccountClient{})

	// Called directly, with the identity the middleware would have refused: the
	// handler keeps its own guard, so the route does not depend on being mounted
	// under the middleware.
	rec := httptest.NewRecorder()
	h.BackupExport(rec, reqAsIdentity(0, false, "POST", "/api/backup/export", `{"passphrase":"pw"}`))

	if rec.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d: %s", rec.Code, rec.Body.String())
	}
	if backup.exportCalls != 0 {
		t.Fatal("owner 0 means the whole installation: the request must never reach the core")
	}
}

func TestBackupImport_AccountlessIdentityNeverReachesTheCore(t *testing.T) {
	backup := &fakeBackupClient{}
	h := backupHandlers(backup, &fakeAccountClient{})

	rec := httptest.NewRecorder()
	h.BackupImport(rec, reqAsIdentity(-3, false, "POST", "/api/backup/import", `{"bundle":"enc:v2:x"}`))

	if rec.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d: %s", rec.Code, rec.Body.String())
	}
	if backup.importCalls != 0 {
		t.Fatal("owner 0 keeps the owner ids inside the bundle: the request must not reach the core")
	}
}

func TestBackupExport_ScopesANonAdminToItsOwnTenant(t *testing.T) {
	backup := &fakeBackupClient{}
	account := &fakeAccountClient{validateResp: &pb.ValidateTokenResponse{Valid: true, UserId: 5989354154}}
	h := backupHandlers(backup, account)

	// The body names somebody else's id; a non-admin must not be able to export
	// it by typing it into the payload. Served through the middleware, so the
	// owner the core sees is the token's, not the body's.
	rec := served(h, h.BackupExport, "POST", "/api/backup/export", `{"owner_user_id":5094837833}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if backup.lastExport == nil || backup.lastExport.GetOwnerUserId() != 5989354154 {
		t.Fatalf("a non-admin must export its own tenant, asked for %v", backup.lastExport)
	}
}

func TestBackupExport_AdminKeepsTheWholeInstallationContract(t *testing.T) {
	backup := &fakeBackupClient{}
	account := &fakeAccountClient{validateResp: &pb.ValidateTokenResponse{Valid: true, UserId: 5094837833, IsAdmin: true}}
	h := backupHandlers(backup, account)

	rec := served(h, h.BackupExport, "POST", "/api/backup/export", `{}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if backup.lastExport == nil || backup.lastExport.GetOwnerUserId() != 0 {
		t.Fatalf("an admin that names no owner keeps the documented whole-installation scope, got %v", backup.lastExport)
	}
}
