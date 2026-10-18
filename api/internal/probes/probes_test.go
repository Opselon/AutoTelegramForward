package probes

import (
	"os"
	"regexp"

	"testing"
)

// routeRe extracts the path from a route registration on the TOP-LEVEL mux in
// api/main.go, e.g. mux.HandleFunc("GET /readyz", h.Ready) -> /readyz.
//
// The leading tab + `mux.` matter: dashMux and authMux register the same helper
// under a mount prefix (/logs/health, /health/services, /api/auth/...), and those
// are authenticated surfaces by design — reading them here would demand that a
// dependency check behind a Bearer token be made public.
var routeRe = regexp.MustCompile(`(?m)^	mux\.HandleFunc\("(?:GET|POST|PUT|PATCH|DELETE) ([^"]+)"`)

// probeShape is the liveness/readiness/version surface of this process: the
// routes a container probe, a load balancer or the deploy gate can reach with no
// session and no key. Anything shaped like one of these must be served without
// credentials, or the gate reads 401 from a healthy stack.
var probeShape = regexp.MustCompile(
	`^/(health|healthz|ready|readyz)$|^/api/version$|^/api(/v1)?/health/services$`)

// TestEveryHealthRouteInMainIsListed is the drift guard for this package.
//
// A liveness/readiness route that main.go registers but this list omits is
// refused by the API-key gate with 401 — so a deploy gate probing it reads
// "broken" from a healthy stack. That is the failure this test exists to make
// impossible to merge, so it reads the routes main.go actually serves rather
// than restating the list it is checking.
func TestEveryHealthRouteInMainIsListed(t *testing.T) {
	mainGo, err := os.ReadFile("../../main.go")
	if err != nil {
		t.Fatalf("read api/main.go: %v", err)
	}
	var found []string
	for _, m := range routeRe.FindAllStringSubmatch(string(mainGo), -1) {
		if probeShape.MatchString(m[1]) {
			found = append(found, m[1])
		}
	}
	// Without this the guard would pass vacuously if the parsing ever stopped
	// finding anything (a renamed helper, a moved file).
	if len(found) < 5 {
		t.Fatalf("expected to find the liveness/readiness/version routes in api/main.go, found %v", found)
	}
	for _, p := range found {
		if !IsProbePath(p) {
			t.Errorf("api/main.go serves %s but internal/probes does not list it: "+
				"the API-key gate will answer 401 to a credential-less probe, and a "+
				"deploy gate reading that 401 will roll back a healthy deployment", p)
		}
	}
}

func TestProbePathsStayUnprivileged(t *testing.T) {
	if len(Paths()) == 0 {
		t.Fatal("the probe list must not be empty")
	}
	// The paths that must NEVER be public: anything that mutates state or reads
	// an account. /api/auth/ is exempt from the API key by prefix (it is the
	// public login surface) but it is not a probe, and it must not be listed here
	// as if it were harmless polling.
	for _, p := range []string{"/", "/api/rules", "/api/stats", "/api/auth/login", "/api/auth/exchange-bot-token"} {
		if IsProbePath(p) {
			t.Errorf("%s must not be treated as an unauthenticated probe", p)
		}
	}
}

func TestPathsIsACopy(t *testing.T) {
	got := Paths()
	if len(got) == 0 {
		t.Fatal("no paths")
	}
	got[0] = "/tampered"
	if IsProbePath("/tampered") {
		t.Fatal("Paths() must return a copy: a caller mutated the probe set")
	}
}
