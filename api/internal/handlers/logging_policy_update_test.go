package handlers

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"autoforward/internal/grpcclient"
	pb "autoforward/proto"
	"google.golang.org/grpc"
)

// Changing Core's collection policy is the one control that decides what the
// evidence trail keeps, so these tests are mostly about what the write path must
// refuse and what it must not claim: a change merged onto an assumed baseline,
// a write attempted when the current policy cannot be read, a refusal reported as
// success, or an accepted-but-volatile change reported as saved.

// fakeCorePolicy is a core whose policy can be read, changed and made to fail on
// demand, and that records every call so "did the API write at all?" is assertable.
type fakeCorePolicy struct {
	pb.SystemStatusControlServiceClient
	current  *pb.LogPolicyResponse
	getErr   error
	setErr   error
	getFails int // fail this many reads, then succeed (0 = never fail)
	setReply *pb.LogPolicyResponse
	onSet    func(in *pb.SetLogPolicyRequest)

	gets int
	sets []*pb.SetLogPolicyRequest
}

func (f *fakeCorePolicy) GetLogPolicy(
	ctx context.Context, in *pb.LogPolicyRequest, opts ...grpc.CallOption,
) (*pb.LogPolicyResponse, error) {
	f.gets++
	if f.getErr != nil {
		return nil, f.getErr
	}
	if f.getFails > 0 {
		f.getFails--
		return nil, errors.New("core: connection refused")
	}
	return f.current, nil
}

func (f *fakeCorePolicy) SetLogPolicy(
	ctx context.Context, in *pb.SetLogPolicyRequest, opts ...grpc.CallOption,
) (*pb.LogPolicyResponse, error) {
	f.sets = append(f.sets, in)
	if f.setErr != nil {
		return nil, f.setErr
	}
	if f.onSet != nil {
		f.onSet(in)
	}
	if f.setReply != nil {
		return f.setReply, nil
	}
	return &pb.LogPolicyResponse{Available: true, Applied: true, Persisted: true}, nil
}

// policyHandlers builds handlers whose core starts with `current`.
func policyHandlers(t *testing.T, current *pb.LogPolicyResponse) (*Handlers, *fakeCorePolicy, *[]map[string]any) {
	t.Helper()
	h, recorded := newPolicyHandlers(nil)
	core := &fakeCorePolicy{current: current}
	h.c = &grpcclient.Clients{System: core}
	return h, core, recorded
}

func corePolicy(levels []string, rate float64) *pb.LogPolicyResponse {
	return &pb.LogPolicyResponse{
		Available:            true,
		ConfiguredLevels:     levels,
		EffectiveLevels:      levels,
		VerboseRatePerSecond: rate,
	}
}

func TestLogPolicyUpdate_AppliesTheChangeAndReportsWhatTheCoreEnforces(t *testing.T) {
	h, core, recorded := policyHandlers(t, corePolicy([]string{"WARN", "ERROR", "CRITICAL"}, 0))
	// The core applies it and reports the new policy; the read-back afterwards
	// must be the source of the answer, not the request that was sent.
	core.onSet = func(in *pb.SetLogPolicyRequest) {
		core.current = corePolicy(in.Levels, in.GetVerboseRatePerSecond())
	}

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest(
		"PUT", "/logs/policy", `{"levels":["WARN","INFO"],"verbose_rate_per_second":5}`,
	))

	if rec.Code != http.StatusOK {
		t.Fatalf("an admin change must succeed, got %d: %s", rec.Code, rec.Body)
	}
	body := decode(t, rec)
	if body["ok"] != true || body["applied"] != true {
		t.Fatalf("a change that was applied must say so: %v", body)
	}
	if body["persisted"] != true {
		t.Fatalf("the core persisted it, so the API must report it as durable: %v", body)
	}

	if len(core.sets) != 1 {
		t.Fatalf("exactly one change must reach the core, got %d", len(core.sets))
	}
	sent := core.sets[0]
	if strings.Join(sent.Levels, ",") != "WARN,INFO" {
		t.Fatalf("the requested levels must reach the core, got %v", sent.Levels)
	}
	if sent.VerboseRatePerSecond == nil || *sent.VerboseRatePerSecond != 5 {
		t.Fatalf("the requested rate must reach the core as an explicit value, got %v", sent.VerboseRatePerSecond)
	}
	if sent.Actor != "5094837833" {
		t.Fatalf("the change must name the actor for the core's audit record, got %q", sent.Actor)
	}

	scope, ok := body["scope"].(map[string]any)
	if !ok {
		t.Fatalf("the answer must carry the producer's state: %v", body)
	}
	if scope["available"] != true {
		t.Fatalf("the read-back must be available, got %v (%v)", scope["available"], scope["reason"])
	}
	levels, _ := scope["effective_levels"].([]any)
	if len(levels) != 2 {
		t.Fatalf("the answer must describe the policy in force, got %v", scope["effective_levels"])
	}
	if got := core.gets; got != 2 {
		t.Fatalf("the policy must be read before and after the change, got %d reads", got)
	}

	if len(*recorded) != 1 {
		t.Fatalf("an applied change must be audited exactly once, got %v", *recorded)
	}
	entry := (*recorded)[0]
	if entry["action"] != "logging.policy.update" || entry["actor"] != int64(5094837833) {
		t.Fatalf("the audit record must name the action and the actor: %v", entry)
	}
	detail, _ := entry["detail"].(map[string]any)
	if detail["result"] != "applied" || detail["persisted"] != true {
		t.Fatalf("the audit record must say what happened: %v", detail)
	}
	if _, ok := detail["before"]; !ok {
		t.Fatalf("the audit record must keep what the policy was: %v", detail)
	}
}

func TestLogPolicyUpdate_MergesOntoThePolicyTheCoreReports(t *testing.T) {
	// Changing only the rate must not reset the levels: the merge baseline is
	// what the core says it is enforcing, not this API's idea of a default.
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN", "ERROR"}, 12))

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"verbose_rate_per_second":30}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("a rate-only change must succeed, got %d: %s", rec.Code, rec.Body)
	}
	sent := core.sets[0]
	if strings.Join(sent.Levels, ",") != "WARN,ERROR" {
		t.Fatalf("a rate-only change must keep the collected levels, got %v", sent.Levels)
	}
	if sent.VerboseRatePerSecond == nil || *sent.VerboseRatePerSecond != 30 {
		t.Fatalf("the new rate must be sent as an explicit value, got %v", sent.VerboseRatePerSecond)
	}
}

func TestLogPolicyUpdate_AnExplicitZeroRateAsksForNoCeiling(t *testing.T) {
	// 0 is the value that means "no ceiling" — the dashboard says so on the
	// field itself. It must reach the core as a *stated* value: a truthiness
	// check anywhere on this path turns "remove the ceiling" into "leave it
	// alone", which is a silently ignored request.
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN", "ERROR"}, 5000))

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"verbose_rate_per_second":0}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("removing the ceiling must succeed, got %d: %s", rec.Code, rec.Body)
	}
	if len(core.sets) != 1 {
		t.Fatalf("exactly one change must reach the core, got %d", len(core.sets))
	}
	sent := core.sets[0]
	if sent.VerboseRatePerSecond == nil {
		t.Fatalf("the rate must be stated, not omitted: a nil field reads as \"unchanged\"")
	}
	if *sent.VerboseRatePerSecond != 0 {
		t.Fatalf("an explicit 0 must reach the core as 0, got %v", *sent.VerboseRatePerSecond)
	}
}

func TestLogPolicyUpdate_AnOmittedRateIsNotZeroedByAccident(t *testing.T) {
	// The mirror image: a levels-only change must carry the enforced ceiling
	// forward rather than clearing it, so the merge is not the place where a
	// ceiling disappears without anyone asking.
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN", "ERROR"}, 77))

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["ERROR"]}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("a levels-only change must succeed, got %d: %s", rec.Code, rec.Body)
	}
	sent := core.sets[0]
	if sent.VerboseRatePerSecond == nil || *sent.VerboseRatePerSecond != 77 {
		t.Fatalf("the enforced ceiling must be carried forward, got %v", sent.VerboseRatePerSecond)
	}
}

func TestLogPolicyUpdate_RefusesToWriteWhenTheCurrentPolicyCannotBeRead(t *testing.T) {
	// A merge onto an unknown baseline is how one change silently reverts another,
	// so an unreadable current policy must stop the write rather than guess.
	h, core, recorded := policyHandlers(t, nil)
	core.getErr = errors.New("core: dial tcp: connection refused")

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["ERROR"]}`))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("an unreadable baseline must not be written to, got %d: %s", rec.Code, rec.Body)
	}
	if len(core.sets) != 0 {
		t.Fatalf("no change may be attempted, got %v", core.sets)
	}
	if len(*recorded) != 0 {
		t.Fatalf("nothing happened, so nothing must be audited as if it did: %v", *recorded)
	}
}

func TestLogPolicyUpdate_ReportsACoreThatEnforcesNoPolicyAsUnavailable(t *testing.T) {
	h, core, _ := policyHandlers(t, &pb.LogPolicyResponse{
		Available: false, Reason: "no collection policy attached to this core process",
	})

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["ERROR"]}`))

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("a core with no policy cannot be changed, got %d", rec.Code)
	}
	if len(core.sets) != 0 {
		t.Fatalf("no change may be attempted, got %v", core.sets)
	}
	if body := decode(t, rec); body["reason"] == "" {
		t.Fatalf("the core's own reason must be passed through: %v", body)
	}
}

func TestLogPolicyUpdate_PassesTheCoresRefusalBackAndClaimsNothing(t *testing.T) {
	// The producer is the validation authority: a rejected change must reach the
	// caller with the core's own words, and must not be reported as applied.
	h, core, recorded := policyHandlers(t, corePolicy([]string{"WARN"}, 0))
	core.setReply = &pb.LogPolicyResponse{
		Available: true,
		Applied:   false,
		Reason:    "unknown level 'WARNN'; expected one of TRACE, DEBUG, INFO, SUCCESS, WARN, ERROR, CRITICAL",
	}

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["WARNN"]}`))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a refused change is a bad request, got %d: %s", rec.Code, rec.Body)
	}
	body := decode(t, rec)
	if !strings.Contains(body["error"].(string), "WARNN") {
		t.Fatalf("the core's validation message must reach the caller: %v", body)
	}
	if body["applied"] == true {
		t.Fatalf("a refused change must never be reported as applied: %v", body)
	}
	if len(*recorded) != 1 {
		t.Fatalf("a refusal must be recorded: %v", *recorded)
	}
	if detail, _ := (*recorded)[0]["detail"].(map[string]any); detail["result"] != "rejected" {
		t.Fatalf("the audit record must distinguish a refusal from a change: %v", detail)
	}
}

func TestLogPolicyUpdate_RejectsARequestBodyThatChangesNothing(t *testing.T) {
	// Answering "ok" to a request that did nothing is worse than refusing it.
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN"}, 0))

	for name, body := range map[string]string{
		"empty object":      `{}`,
		"unknown field":     `{"level":["ERROR"]}`,
		"not json":          `levels=ERROR`,
		"level as a string": `{"levels":"ERROR"}`,
	} {
		rec := httptest.NewRecorder()
		h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", body))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s must be refused, got %d: %s", name, rec.Code, rec.Body)
		}
	}
	if len(core.sets) != 0 {
		t.Fatalf("no malformed request may reach the core, got %v", core.sets)
	}
}

func TestLogPolicyUpdate_RejectsAContradictoryDiagnosticRequest(t *testing.T) {
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN"}, 0))

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest(
		"PUT", "/logs/policy", `{"diagnostic_seconds":600,"disable_diagnostics":true}`,
	))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("opening and closing a window at once must be refused, got %d", rec.Code)
	}
	if len(core.sets) != 0 {
		t.Fatalf("a contradictory request must not reach the core, got %v", core.sets)
	}
}

func TestLogPolicyUpdate_IsRefusedForANonAdminBeforeAnyCoreCall(t *testing.T) {
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN"}, 0))

	req := httptest.NewRequest("PUT", "/logs/policy", strings.NewReader(`{"levels":["INFO"]}`))
	req = req.WithContext(context.WithValue(req.Context(), identityKey{}, &Identity{
		UserID: 42, Username: "someone", IsAdmin: false,
	}))
	rec := httptest.NewRecorder()

	h.RequireAdmin(http.HandlerFunc(h.LogPolicyUpdate)).ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("a non-admin must be refused, got %d: %s", rec.Code, rec.Body)
	}
	if core.gets != 0 || len(core.sets) != 0 {
		t.Fatalf("the gate must answer before the core is touched: %d reads, %d writes",
			core.gets, len(core.sets))
	}
}

func TestLogPolicyUpdate_WithoutACoreClientIsUnavailableRatherThanSilent(t *testing.T) {
	h, recorded := newPolicyHandlers(nil)

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["ERROR"]}`))

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("a change with nowhere to go must say so, got %d", rec.Code)
	}
	if len(*recorded) != 0 {
		t.Fatalf("no change happened, so none may be recorded: %v", *recorded)
	}
}

func TestLogPolicyUpdate_AFailedSetIsNotReportedAsAChange(t *testing.T) {
	// The core could not be asked: nothing was decided, so nothing may be claimed
	// or audited as if the policy had moved.
	h, core, recorded := policyHandlers(t, corePolicy([]string{"WARN"}, 0))
	core.setErr = errors.New("core: transport is closing")

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["INFO"]}`))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("a failed change is an upstream failure, got %d: %s", rec.Code, rec.Body)
	}
	if body := decode(t, rec); body["applied"] == true {
		t.Fatalf("nothing was applied: %v", body)
	}
	if len(*recorded) != 0 {
		t.Fatalf("no change happened, so none may be recorded: %v", *recorded)
	}
}

func TestLogPolicyUpdate_AnUnreadableReadBackIsReportedUnavailable(t *testing.T) {
	// The change was applied, but the follow-up read fails: the answer must say
	// what it cannot see instead of repeating the request as if it were state.
	h, core, _ := policyHandlers(t, corePolicy([]string{"WARN"}, 0))
	core.setReply = &pb.LogPolicyResponse{Available: true, Applied: true, Persisted: true}
	// First read succeeds, the read-back after the change fails.
	h.c = &grpcclient.Clients{System: &failNthGet{fakeCorePolicy: core, failAt: 2}}

	rec := httptest.NewRecorder()
	h.LogPolicyUpdate(rec, adminRequest("PUT", "/logs/policy", `{"levels":["INFO"]}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("the change was applied, so the request succeeded: %d %s", rec.Code, rec.Body)
	}
	body := decode(t, rec)
	if body["applied"] != true || body["persisted"] != true {
		t.Fatalf("what the core reported must be passed through: %v", body)
	}
	scope, _ := body["scope"].(map[string]any)
	if scope["available"] != false {
		t.Fatalf("an unreadable read-back must be reported as unavailable: %v", scope)
	}
	if reason, _ := scope["reason"].(string); !strings.Contains(reason, "connection refused") {
		t.Fatalf("the reason must survive to the operator: %v", scope)
	}
}

// failNthGet answers normally until request `failAt`, then fails. The read-back
// after a change is a separate observation from the read before it, and the
// control plane must not conflate the two into a stale copy of the request.
type failNthGet struct {
	*fakeCorePolicy
	failAt int
}

func (f *failNthGet) GetLogPolicy(
	ctx context.Context, in *pb.LogPolicyRequest, opts ...grpc.CallOption,
) (*pb.LogPolicyResponse, error) {
	f.fakeCorePolicy.gets++
	if f.fakeCorePolicy.gets >= f.failAt {
		return nil, errors.New("core: connection refused")
	}
	return f.fakeCorePolicy.current, nil
}
