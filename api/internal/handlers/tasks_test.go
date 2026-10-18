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

// The task handlers must carry the verified identity end-to-end: never trust
// a body field, never return another tenant's row — even if the downstream
// service ignores OwnerUserId (the fakes below deliberately do).

type fakeTasksClient struct {
	pb.TaskControlServiceClient
	createdCreate []*pb.CreateTaskRequest
	createdGet    []*pb.GetTaskRequest
	createdIndex  int64
	actionAnswer  *pb.TaskResponse
	listAnswer    *pb.ListTasksResponse
	listSeen      int64
}

func (f *fakeTasksClient) CreateTask(ctx context.Context, in *pb.CreateTaskRequest, opts ...grpc.CallOption) (*pb.CreateTaskResponse, error) {
	f.createdCreate = append(f.createdCreate, in)
	return &pb.CreateTaskResponse{
		Success: true,
		Task: &pb.UserTask{
			Id: "t1", OwnerUserId: in.OwnerUserId, Kind: in.Kind,
			Status: "PENDING", MaxAttempts: in.MaxAttempts,
		},
	}, nil
}

func (f *fakeTasksClient) GetTask(ctx context.Context, in *pb.GetTaskRequest, opts ...grpc.CallOption) (*pb.TaskResponse, error) {
	f.createdGet = append(f.createdGet, in)
	if f.actionAnswer != nil {
		return f.actionAnswer, nil
	}
	return &pb.TaskResponse{Success: true, Task: &pb.UserTask{
		Id: in.TaskId, OwnerUserId: 111}}, nil
}

func (f *fakeTasksClient) ListTasks(ctx context.Context, in *pb.ListTasksRequest, opts ...grpc.CallOption) (*pb.ListTasksResponse, error) {
	f.listSeen = in.OwnerUserId
	if f.listAnswer != nil {
		return f.listAnswer, nil
	}
	return &pb.ListTasksResponse{Success: true, Total: 0}, nil
}

func (f *fakeTasksClient) CancelTask(ctx context.Context, in *pb.CancelTaskRequest, opts ...grpc.CallOption) (*pb.TaskResponse, error) {
	if f.actionAnswer != nil {
		return f.actionAnswer, nil
	}
	return &pb.TaskResponse{Success: true, Task: &pb.UserTask{
		Id: in.TaskId, OwnerUserId: in.OwnerUserId, Status: "CANCELLED"}}, nil
}

func (f *fakeTasksClient) RetryTask(ctx context.Context, in *pb.RetryTaskRequest, opts ...grpc.CallOption) (*pb.TaskResponse, error) {
	if f.actionAnswer != nil {
		return f.actionAnswer, nil
	}
	return &pb.TaskResponse{Success: true, Task: &pb.UserTask{
		Id: in.TaskId, OwnerUserId: in.OwnerUserId, Status: "PENDING"}}, nil
}

func taskHandler(tc *fakeTasksClient) *Handlers {
	return &Handlers{c: &grpcclient.Clients{Tasks: tc}}
}

func doTask(t *testing.T, h *Handlers, uid int64, method, target, body string) *httptest.ResponseRecorder {
	t.Helper()
	var req *http.Request
	if body == "" {
		req = httptest.NewRequest(method, target, nil)
	} else {
		req = httptest.NewRequest(method, target, strings.NewReader(body))
	}
	if strings.HasPrefix(target, "/tasks/") {
		req.SetPathValue("id", "t-other-tenant")
	}
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{UserID: uid}))
	rec := httptest.NewRecorder()
	switch target {
	case "/tasks":
		if method == "POST" {
			h.CreateTask(rec, req)
		} else {
			h.ListTasks(rec, req)
		}
	case "/tasks/t-other-tenant":
		h.GetTask(rec, req)
	case "/tasks/t-other-tenant/cancel":
		h.CancelTask(rec, req)
	case "/tasks/t-other-tenant/retry":
		h.RetryTask(rec, req)

	default:
		h.ListTasks(rec, req)
	}
	return rec
}

func TestTaskCreate_CarriesVerifiedIdentityNotBodyIntent(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	// The body tries the oldest trick in the book: name another owner.
	body := `{"kind":"session.backup","user_id":999888,"owner_user_id":999888,"payload":{"session_id":"s1"},"idempotency_key":"k1"}`
	rec := doTask(t, h, 111, "POST", "/tasks", body)
	if rec.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d: %s", rec.Code, rec.Body.String())
	}
	if len(tc.createdCreate) != 1 || tc.createdCreate[0].OwnerUserId != 111 {
		t.Fatalf("CreateTask must scope to the JWT identity, saw %+v", tc.createdCreate)
	}
	// Idempotent resubmission of the same key is reported, not a duplicate.
	if strings.Contains(rec.Body.String(), "already_existed") == false {
		t.Fatalf("response must carry already_existed for client dedupe: %s", rec.Body.String())
	}
}

func TestTaskCreate_RefusesInsecureSetup(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	// No identity at all: the API returns 401 before a gRPC round trip.
	rec := doTask(t, h, 0, "POST", "/tasks", `{"kind":"session.backup"}`)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated create must be 401, got %d", rec.Code)
	}
	if len(tc.createdCreate) != 0 {
		t.Fatalf("no request may reach core without identity")
	}
	// Unknown kind: refused with the domain's own code.
	rec = doTask(t, h, 111, "POST", "/tasks", `{"kind":"rm -rf"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("unknown kind must be 400, got %d", rec.Code)
	}
}

func TestTaskRead_ForeignRowIs404_EvenIfCoreAnswers(t *testing.T) {
	// The fake answers 111's question with tenant 222's row: the handler must
	// refuse it as it would refuse a nonexistent one.
	tc := &fakeTasksClient{actionAnswer: &pb.TaskResponse{
		Success: true, Task: &pb.UserTask{Id: "x1", OwnerUserId: 222}}}
	h := taskHandler(tc)
	for _, path := range []string{"/tasks/t-other-tenant", "/tasks/t-other-tenant/cancel", "/tasks/t-other-tenant/retry"} {
		rec := doTask(t, h, 111, "GET", path, "")
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s must be 404 for a foreign task, got %d", path, rec.Code)
		}
	}
}

func TestTaskList_FiltersForeignRows_WhateverDownstreamSays(t *testing.T) {
	tc := &fakeTasksClient{listAnswer: &pb.ListTasksResponse{
		Success: true,
		Tasks: []*pb.UserTask{
			{Id: "mine", OwnerUserId: 111},
			{Id: "theirs", OwnerUserId: 222},
		},
		Total: 2,
	}}
	h := taskHandler(tc)
	rec := doTask(t, h, 111, "GET", "/tasks?limit=500", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var out struct {
		Tasks []struct {
			Id string `json:"id"`
		} `json:"tasks"`
		Limit int `json:"limit"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v (%s)", err, rec.Body.String())
	}
	if tc.listSeen != 111 {
		t.Fatalf("ListTasks must pass the JWT owner, saw %d", tc.listSeen)
	}
	if len(out.Tasks) != 1 || out.Tasks[0].Id != "mine" {
		t.Fatalf("cross-tenant leak in list: %+v", out.Tasks)
	}
	// Handler-side page bound: 500 clamps to 200.
	if out.Limit != 200 {
		t.Fatalf("page limit must clamp to 200, got %d", out.Limit)
	}
}

func TestTaskAction_OwnerScopesTheCall(t *testing.T) {
	tc := &fakeTasksClient{}
	h := taskHandler(tc)
	rec := doTask(t, h, 222, "POST", "/tasks/t-other-tenant/cancel", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("owner cancel must succeed, got %d (%s)", rec.Code, rec.Body.String())
	}
	rec = doTask(t, h, 222, "POST", "/tasks/t-other-tenant/retry", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("owner retry must succeed, got %d (%s)", rec.Code, rec.Body.String())
	}
}
