// Package probes holds the single list of unauthenticated liveness/readiness
// endpoints.
//
// Three layers have to agree about these paths, and each of them used to keep
// its own copy:
//
//   - the API-key gate must not refuse them. A deploy gate, a container probe
//     or a load balancer has no session and no API key, so a readiness endpoint
//     that only answers to credentials is not a readiness endpoint — it is a
//     401 that a deploy reads as "broken" from a stack that is actually healthy;
//   - the access logger must not archive their polling as operator actions;
//   - the per-address shield must not spend a caller's budget on them.
//
// A new probe remembered in one copy but not the others fails in exactly the way
// this package exists to prevent: the deploy gate probes /readyz, the API-key
// gate answers 401 because nobody added it there, and a healthy deployment is
// rolled back. One list, checked against the routes main.go actually registers
// (TestEveryHealthRouteInMainIsListed), makes that a caught mistake instead.
package probes

// paths are the liveness/readiness/version endpoints served without credentials
// and treated as machine polling rather than operator actions.
var paths = []string{
	"/healthz",
	"/health",
	"/readyz",
	"/ready",
	"/api/version",
	"/api/health/services",
	"/api/v1/health/services",
}

var set = func() map[string]bool {
	m := make(map[string]bool, len(paths))
	for _, p := range paths {
		m[p] = true
	}
	return m
}()

// IsProbePath reports whether path is served without credentials and treated as
// machine polling rather than an operator action.
func IsProbePath(path string) bool { return set[path] }

// Paths returns a copy of the list, for tests, diagnostics and documentation.
func Paths() []string {
	out := make([]string, len(paths))
	copy(out, paths)
	return out
}
