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
// Bot-route credentials: the three properties that must not drift
// --------------------------------------------------------------------------- //

// fakeBotTokens behaves like the core servicer: reads are owner-scoped, a create
// can be refused, and a delete can be refused because a task still routes
// through the bot.
type fakeBotTokens struct {
	pb.BotTokenControlServiceClient
	tokens       []*pb.BotTokenInfo
	owners       map[string]int64
	createRef    *pb.CreateBotTokenResponse
	delRefusal   string
	lastCreate   *pb.CreateBotTokenRequest
	lastDefaults *pb.SetBotDefaultsRequest
}

func (f *fakeBotTokens) ListBotTokens(ctx context.Context, in *pb.ListBotTokensRequest, opts ...grpc.CallOption) (*pb.ListBotTokensResponse, error) {
	out := &pb.ListBotTokensResponse{}
	for _, t := range f.tokens {
		if in.OwnerUserId == 0 || f.owners[t.Id] == in.OwnerUserId {
			out.Tokens = append(out.Tokens, t)
		}
	}
	return out, nil
}

func (f *fakeBotTokens) CreateBotToken(ctx context.Context, in *pb.CreateBotTokenRequest, opts ...grpc.CallOption) (*pb.CreateBotTokenResponse, error) {
	f.lastCreate = in
	if f.createRef != nil {
		return f.createRef, nil
	}
	return &pb.CreateBotTokenResponse{
		Success: true,
		Token:   &pb.BotTokenInfo{Id: "tok-new", Label: "@fresh_bot", IsActive: true},
	}, nil
}

func (f *fakeBotTokens) DeleteBotToken(ctx context.Context, in *pb.DeleteBotTokenRequest, opts ...grpc.CallOption) (*pb.StatusResponse, error) {
	if f.delRefusal != "" {
		return &pb.StatusResponse{Success: false, Message: f.delRefusal}, nil
	}
	return &pb.StatusResponse{Success: true, Message: "deleted"}, nil
}

func (f *fakeBotTokens) SetBotDefaults(ctx context.Context, in *pb.SetBotDefaultsRequest, opts ...grpc.CallOption) (*pb.BotDefaultsResponse, error) {
	f.lastDefaults = in
	return &pb.BotDefaultsResponse{
		Success:      true,
		Message:      "1 default(s) updated",
		DefaultsJson: in.GetDefaultsJson(),
		Changed:      []string{"remove_emojis"},
	}, nil
}

func botTokenHandlers(client *fakeBotTokens) *Handlers {
	return &Handlers{c: &grpcclient.Clients{BotTokens: client}}
}

func botReq(uid int64, method, target, body string) *http.Request {
	var reader *strings.Reader
	if body == "" {
		reader = strings.NewReader("")
	} else {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, reader)
	return req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
}

func TestListBotTokens_IsScopedToTheCaller(t *testing.T) {
	client := &fakeBotTokens{
		tokens: []*pb.BotTokenInfo{{Id: "a", Label: "@mine"}, {Id: "b", Label: "@theirs"}},
		owners: map[string]int64{"a": 111, "b": 222},
	}
	rec := httptest.NewRecorder()
	botTokenHandlers(client).ListBotTokens(rec, botReq(111, "GET", "/api/bot-tokens", ""))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body struct {
		Tokens []struct {
			ID    string `json:"id"`
			Label string `json:"label"`
		} `json:"tokens"`
		Total int `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if body.Total != 1 || len(body.Tokens) != 1 || body.Tokens[0].ID != "a" {
		t.Fatalf("a tenant must see only its own bot, got %+v", body)
	}
}

func TestCreateBotToken_UnlinkedAccountIsForbidden(t *testing.T) {
	client := &fakeBotTokens{}
	rec := httptest.NewRecorder()
	botTokenHandlers(client).CreateBotToken(
		rec, botReq(0, "POST", "/api/bot-tokens", `{"token":"123:AA"}`),
	)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("an unlinked account must not attach a bot, got %d: %s", rec.Code, rec.Body.String())
	}
	if client.lastCreate != nil {
		t.Fatal("the request must not reach the core at all")
	}
}

func TestCreateBotToken_EmptyTokenIsRejectedLocally(t *testing.T) {
	client := &fakeBotTokens{}
	rec := httptest.NewRecorder()
	botTokenHandlers(client).CreateBotToken(
		rec, botReq(111, "POST", "/api/bot-tokens", `{"token":"   "}`),
	)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
	if client.lastCreate != nil {
		t.Fatal("an empty token must not reach the core")
	}
}

func TestCreateBotToken_SetsTheCallerAsOwnerAndNeverEchoesTheSecret(t *testing.T) {
	const secret = "123456789:AAH-super-secret-value"
	client := &fakeBotTokens{}
	rec := httptest.NewRecorder()
	botTokenHandlers(client).CreateBotToken(
		rec, botReq(111, "POST", "/api/bot-tokens", `{"label":"mine","token":"`+secret+`"}`),
	)

	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	if client.lastCreate == nil || client.lastCreate.OwnerUserId != 111 {
		t.Fatalf("the owner must come from the identity, not the body: %+v", client.lastCreate)
	}
	if client.lastCreate.Token != secret {
		t.Fatal("the token must reach the core unchanged")
	}
	if strings.Contains(rec.Body.String(), secret) {
		t.Fatalf("the response echoed the token: %s", rec.Body.String())
	}
}

func TestCreateBotToken_CoreRefusalIsABadRequestNotAServerError(t *testing.T) {
	const secret = "123456789:AAH-refused-value"
	client := &fakeBotTokens{createRef: &pb.CreateBotTokenResponse{
		Success:   false,
		ErrorCode: "rejected",
		Message:   "Telegram refused this token",
	}}
	rec := httptest.NewRecorder()
	botTokenHandlers(client).CreateBotToken(
		rec, botReq(111, "POST", "/api/bot-tokens", `{"token":"`+secret+`"}`),
	)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a refused token is the caller's problem, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "Telegram refused this token") {
		t.Fatalf("the core's reason must be surfaced: %s", rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), secret) {
		t.Fatalf("the refusal echoed the token: %s", rec.Body.String())
	}
}

func TestDeleteBotToken_ForeignIdIsNotFound(t *testing.T) {
	client := &fakeBotTokens{
		tokens: []*pb.BotTokenInfo{{Id: "theirs"}},
		owners: map[string]int64{"theirs": 222},
	}
	rec := httptest.NewRecorder()
	req := botReq(111, "DELETE", "/api/bot-tokens/theirs", "")
	req.SetPathValue("id", "theirs")
	botTokenHandlers(client).DeleteBotToken(rec, req)

	// 404, not 403: a probe must not learn that the id exists.
	if rec.Code != http.StatusNotFound {
		t.Fatalf("a foreign bot must 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestDeleteBotToken_RefusedWhileATaskStillUsesIt(t *testing.T) {
	const reason = "still used by 2 task(s); switch them to the account route first"
	client := &fakeBotTokens{
		tokens:     []*pb.BotTokenInfo{{Id: "mine"}},
		owners:     map[string]int64{"mine": 111},
		delRefusal: reason,
	}
	rec := httptest.NewRecorder()
	req := botReq(111, "DELETE", "/api/bot-tokens/mine", "")
	req.SetPathValue("id", "mine")
	botTokenHandlers(client).DeleteBotToken(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("an in-use bot must 409, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "still used by 2 task(s)") {
		t.Fatalf("the refusal reason must reach the user: %s", rec.Body.String())
	}
}

func TestDeleteBotToken_OwnTokenIsDeleted(t *testing.T) {
	client := &fakeBotTokens{
		tokens: []*pb.BotTokenInfo{{Id: "mine"}},
		owners: map[string]int64{"mine": 111},
	}
	rec := httptest.NewRecorder()
	req := botReq(111, "DELETE", "/api/bot-tokens/mine", "")
	req.SetPathValue("id", "mine")
	botTokenHandlers(client).DeleteBotToken(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

// --------------------------------------------------------------------------- //
// Bot defaults: the two write verbs a client needs
// --------------------------------------------------------------------------- //

// A client that only wants to drop a default should not have to send an empty
// object it does not mean. Sending `{"clear":["k"]}` used to reach the core as
// the JSON literal `null` and come back as "defaults_json must be a JSON object".
func TestSetBotDefaults_ClearWithoutADefaultsObject(t *testing.T) {
	client := &fakeBotTokens{
		tokens: []*pb.BotTokenInfo{{Id: "mine"}},
		owners: map[string]int64{"mine": 111},
	}
	rec := httptest.NewRecorder()
	req := botReq(111, "PUT", "/api/bot-tokens/mine/defaults", `{"clear":["remove_emojis"]}`)
	req.SetPathValue("id", "mine")
	botTokenHandlers(client).SetBotDefaults(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("clearing a default must be accepted, got %d: %s", rec.Code, rec.Body.String())
	}
	if client.lastDefaults == nil {
		t.Fatal("the core was never called")
	}
	if got := client.lastDefaults.GetDefaultsJson(); got != "{}" {
		t.Fatalf("absent defaults must reach the core as an empty object, got %q", got)
	}
	if clear := client.lastDefaults.GetClear(); len(clear) != 1 || clear[0] != "remove_emojis" {
		t.Fatalf("the named keys must be passed through, got %v", clear)
	}
	if client.lastDefaults.GetReplaceAll() {
		t.Fatal("naming keys to clear is not a full replace")
	}
}

// Absent is not null: a present, non-object defaults value is still the caller's
// mistake, not a silent clear-everything.
func TestSetBotDefaults_NonObjectDefaultsIsRejected(t *testing.T) {
	client := &fakeBotTokens{
		tokens: []*pb.BotTokenInfo{{Id: "mine"}},
		owners: map[string]int64{"mine": 111},
	}
	rec := httptest.NewRecorder()
	req := botReq(111, "PUT", "/api/bot-tokens/mine/defaults", `{"defaults":"nope"}`)
	req.SetPathValue("id", "mine")
	botTokenHandlers(client).SetBotDefaults(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a non-object defaults value must 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if client.lastDefaults != nil {
		t.Fatal("a rejected body must not reach the core")
	}
}

// A full replace travels as itself: the client is stating the whole mapping, so
// the flag must survive the trip and the empty mapping must mean "none", not
// "nothing to say".
func TestSetBotDefaults_FullReplaceTravelsThrough(t *testing.T) {
	client := &fakeBotTokens{
		tokens: []*pb.BotTokenInfo{{Id: "mine"}},
		owners: map[string]int64{"mine": 111},
	}
	rec := httptest.NewRecorder()
	req := botReq(111, "PUT", "/api/bot-tokens/mine/defaults",
		`{"replace_all":true,"defaults":{"remove_emojis":true}}`)
	req.SetPathValue("id", "mine")
	botTokenHandlers(client).SetBotDefaults(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if client.lastDefaults == nil || !client.lastDefaults.GetReplaceAll() {
		t.Fatal("replace_all must reach the core")
	}
	if got := client.lastDefaults.GetDefaultsJson(); got != `{"remove_emojis":true}` {
		t.Fatalf("the mapping must travel verbatim, got %q", got)
	}
}
