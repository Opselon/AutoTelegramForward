// Package logpolicy is the Go producer-side log collection policy.
//
// It is the Go half of a contract that is enforced in two places: at the Python
// producer (core/application/log_policy.py) and here, at the Go API's producer
// boundary. Both are governed by one shared fixture file
// (contracts/log_policy_decisions.json) that both test suites must satisfy, so
// the two implementations cannot drift apart unnoticed.
//
// Why a producer policy exists at all: the Logger service persists what it
// receives. The documented defaults (WARN/ERROR/CRITICAL collected;
// INFO/SUCCESS/DEBUG/TRACE not) are therefore only real if the producer applies
// them. Without this, every successful API request writes an INFO row, which is
// exactly the high-volume happy path the logging contract says must not be
// collected by default.
//
// Rules the rest of the system depends on, and that the tests pin:
//
//   - security-relevant events (authentication failures, authorization denials,
//     cross-tenant attempts, administrative changes) pass regardless of the
//     threshold, the diagnostic state and the rate limit. A severity threshold is
//     a noise control, not a way to disarm auditing.
//   - a policy refusal is NOT a transport failure, a storage failure or a queue
//     eviction. It is counted here and never increments a delivery counter.
//   - the gate is applied to a structured level plus structured event metadata,
//     never by matching rendered log text.
package logpolicy

import (
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Levels, in ascending severity order.
const (
	LevelTrace    = "TRACE"
	LevelDebug    = "DEBUG"
	LevelInfo     = "INFO"
	LevelSuccess  = "SUCCESS"
	LevelWarn     = "WARN"
	LevelError    = "ERROR"
	LevelCritical = "CRITICAL"
)

// AllLevels is the canonical ordering, used for reporting effective settings.
var AllLevels = []string{LevelTrace, LevelDebug, LevelInfo, LevelSuccess, LevelWarn, LevelError, LevelCritical}

// VerboseLevels are the levels the contract does not collect by default.
var VerboseLevels = []string{LevelTrace, LevelDebug, LevelInfo, LevelSuccess}

// DefaultLevels are collected out of the box.
var DefaultLevels = []string{LevelWarn, LevelError, LevelCritical}

var levelAliases = map[string]string{
	"WARNING": LevelWarn,
	"ERR":     LevelError,
	"FATAL":   LevelCritical,
	"NOTICE":  LevelInfo,
}

// Security event prefixes and reason codes that are never suppressed. Kept as
// prefixes/codes rather than free text so a typo cannot silently widen or narrow
// the exemption (it must match the Python implementation exactly).
var securityEventPrefixes = []string{"auth.", "security.", "tenant.", "admin.", "permission."}

var securityReasonCodes = map[string]bool{
	"UNAUTHORIZED": true, "FORBIDDEN": true, "PERMISSION_DENIED": true,
	"CROSS_TENANT": true, "TENANT_MISMATCH": true, "RULE_OWNER_MISMATCH": true,
	"ADMIN_REQUIRED": true, "TOKEN_INVALID": true,
}

// Clock lets tests (and the shared fixtures) freeze time. Production uses Now.
type Clock func() time.Time

// Now is the default clock.
func Now() time.Time { return time.Now() }

// NormalizeLevel maps a level name onto the canonical set. A legacy alias is
// honoured; anything unrecognised becomes INFO, so an unknown or malformed level
// can never be silently promoted to a security level.
func NormalizeLevel(level string) string {
	name := strings.ToUpper(strings.TrimSpace(level))
	if name == "" {
		return LevelInfo
	}
	if alias, ok := levelAliases[name]; ok {
		name = alias
	}
	for _, known := range AllLevels {
		if known == name {
			return name
		}
	}
	return LevelInfo
}

// normaliseKnownLevel maps an alias onto the canonical set and returns "" for a
// name that is not a level at all, so list parsing can drop it.
func normaliseKnownLevel(level string) string {
	name := strings.ToUpper(strings.TrimSpace(level))
	if alias, ok := levelAliases[name]; ok {
		name = alias
	}
	for _, known := range AllLevels {
		if known == name {
			return name
		}
	}
	return ""
}

// IsVerbose reports whether a level is one the contract keeps out by default.
func IsVerbose(level string) bool {
	name := NormalizeLevel(level)
	for _, v := range VerboseLevels {
		if v == name {
			return true
		}
	}
	return false
}

// IsSecurityRelevant reports whether an event carries security/authorization
// meaning worth keeping regardless of its level.
func IsSecurityRelevant(eventType, reasonCode string) bool {
	lowered := strings.ToLower(strings.TrimSpace(eventType))
	for _, prefix := range securityEventPrefixes {
		if strings.HasPrefix(lowered, prefix) {
			return true
		}
	}
	return securityReasonCodes[strings.ToUpper(strings.TrimSpace(reasonCode))]
}

// Config is the validated policy configuration.
type Config struct {
	Levels               []string
	VerboseRatePerSecond float64
	DiagnosticSeconds    int
}

// ConfigFromEnv reads ATF_LOG_LEVELS, ATF_LOG_VERBOSE_RATE and
// ATF_LOG_DIAGNOSTIC_SECONDS. It never fails: an unparseable value falls back to
// the documented default, because a typo in an env file must not silently change
// the policy into something nobody intended (and must not disable logging).
func ConfigFromEnv(get func(string) string) Config {
	if get == nil {
		get = os.Getenv
	}
	cfg := Config{Levels: DefaultLevels}

	if raw := strings.TrimSpace(get("ATF_LOG_LEVELS")); raw != "" {
		if levels := ParseLevels(raw); len(levels) > 0 {
			cfg.Levels = levels
		}
	}
	if rate, err := parseFloat(get("ATF_LOG_VERBOSE_RATE")); err == nil && rate >= 0 {
		cfg.VerboseRatePerSecond = rate
	}
	if seconds, err := parseInt(get("ATF_LOG_DIAGNOSTIC_SECONDS")); err == nil && seconds > 0 {
		cfg.DiagnosticSeconds = seconds
	}
	return cfg
}

// ParseLevels parses a comma/semicolon separated level list, normalising aliases
// and dropping unknown names.
func ParseLevels(raw string) []string {
	var levels []string
	seen := map[string]bool{}
	for _, piece := range strings.FieldsFunc(raw, func(r rune) bool { return r == ',' || r == ';' }) {
		piece = strings.TrimSpace(piece)
		if piece == "" {
			continue
		}
		// An unrecognised name in a *list* is dropped rather than normalised to
		// INFO: a typo like "bogus" must not quietly add a verbose level to a
		// deliberately narrowed policy. (A single unknown level name on an event
		// is a different thing and does normalise to INFO.)
		name := normaliseKnownLevel(piece)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		levels = append(levels, name)
	}
	return levels
}

func parseFloat(raw string) (float64, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0, nil
	}
	return strconv.ParseFloat(raw, 64)
}

func parseInt(raw string) (int, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0, nil
	}
	return strconv.Atoi(raw)
}

// Counters are deliberately distinguishable from each other: an operator judging
// volume must be able to tell "we chose not to collect this" apart from "we lost
// it", so suppression is counted here and never in a delivery counter.
type Counters struct {
	Emitted              int64
	EmittedSecurity      int64
	SuppressedLevel      int64
	SuppressedRate       int64
	DiagnosticActivation int64
}

// Snapshot is the read-only view an operator (or the control plane) needs.
type Snapshot struct {
	Counters
	Levels               []string
	ConfiguredLevels     []string
	DiagnosticActive     bool
	DiagnosticUntil      int64
	VerboseRatePerSecond float64
}

// Policy decides whether one event may be emitted. It is safe for concurrent use:
// the API logs from many request goroutines at once.
type Policy struct {
	mu sync.Mutex

	levels      map[string]bool
	verboseRate float64
	clock       Clock

	diagUntil     time.Time
	diagUntilWall time.Time
	diagLevels    map[string]bool

	windowStart time.Time
	windowCount int

	counters Counters
}

// New builds a policy with the given collected levels and verbose rate. A nil or
// empty level list falls back to the documented defaults.
func New(levels []string, verboseRatePerSecond float64) *Policy {
	if len(levels) == 0 {
		levels = DefaultLevels
	}
	p := &Policy{
		levels:      map[string]bool{},
		verboseRate: verboseRatePerSecond,
		clock:       Now,
		diagLevels:  map[string]bool{},
	}
	for _, l := range levels {
		p.levels[NormalizeLevel(l)] = true
	}
	if p.verboseRate < 0 {
		p.verboseRate = 0
	}
	p.windowStart = p.clock()
	return p
}

// FromEnv builds the policy the deployment asks for.
func FromEnv() *Policy {
	cfg := ConfigFromEnv(os.Getenv)
	p := New(cfg.Levels, cfg.VerboseRatePerSecond)
	if cfg.DiagnosticSeconds > 0 {
		p.EnableDiagnostics(cfg.DiagnosticSeconds)
	}
	return p
}

// SetClock replaces the clock (tests and fixtures only).
func (p *Policy) SetClock(clock Clock) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.clock = clock
	p.windowStart = clock()
}

// Allow answers the single question, counting the answer either way.
func (p *Policy) Allow(level, eventType, reasonCode string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()

	name := NormalizeLevel(level)
	security := IsSecurityRelevant(eventType, reasonCode)

	if !security && !p.effectiveLevelsLocked()[name] {
		p.counters.SuppressedLevel++
		return false
	}

	if security {
		// Exempt from the rate limit as well: security evidence is low volume by
		// nature and is exactly what must not be lost to a burst.
		p.counters.Emitted++
		p.counters.EmittedSecurity++
		return true
	}

	if p.verboseRate > 0 && IsVerbose(name) {
		now := p.clock()
		if now.Sub(p.windowStart) >= time.Second {
			p.windowStart = now
			p.windowCount = 0
		}
		if float64(p.windowCount) >= p.verboseRate {
			p.counters.SuppressedRate++
			return false
		}
		p.windowCount++
	}

	p.counters.Emitted++
	return true
}

// EnableDiagnostics opens a bounded window that expires on its own; nothing has
// to remember to close it. Returns the UTC wall-clock expiry, which is the value
// an administrator is shown.
func (p *Policy) EnableDiagnostics(seconds int) int64 {
	if seconds < 1 {
		seconds = 1
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.diagUntil = p.clock().Add(time.Duration(seconds) * time.Second)
	p.diagUntilWall = time.Now().Add(time.Duration(seconds) * time.Second)
	for _, l := range VerboseLevels {
		p.diagLevels[l] = true
	}
	p.counters.DiagnosticActivation++
	return p.diagUntilWall.Unix()
}

// DisableDiagnostics closes an active window early.
func (p *Policy) DisableDiagnostics() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.diagUntil = time.Time{}
	p.diagUntilWall = time.Time{}
	p.diagLevels = map[string]bool{}
}

// DiagnosticActive reports whether verbose collection is currently enabled,
// clearing an expired window so the decision path stays a plain set lookup.
func (p *Policy) DiagnosticActive() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.diagnosticActiveLocked()
}

func (p *Policy) diagnosticActiveLocked() bool {
	if !p.diagUntil.IsZero() && !p.clock().Before(p.diagUntil) {
		p.diagUntil = time.Time{}
		p.diagUntilWall = time.Time{}
		p.diagLevels = map[string]bool{}
	}
	return !p.diagUntil.IsZero()
}

// EffectiveLevels is the level set actually in force right now (configured plus
// any open diagnostic window), in ascending severity order.
func (p *Policy) EffectiveLevels() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	effective := p.effectiveLevelsLocked()
	var out []string
	for _, l := range AllLevels {
		if effective[l] {
			out = append(out, l)
		}
	}
	return out
}

func (p *Policy) effectiveLevelsLocked() map[string]bool {
	effective := map[string]bool{}
	for l := range p.levels {
		effective[l] = true
	}
	if p.diagnosticActiveLocked() {
		for l := range p.diagLevels {
			effective[l] = true
		}
	}
	return effective
}

// Snapshot reports the effective configuration and the counters.
func (p *Policy) Snapshot() Snapshot {
	p.mu.Lock()
	defer p.mu.Unlock()
	active := p.diagnosticActiveLocked()
	configured := make([]string, 0, len(p.levels))
	for _, l := range AllLevels {
		if p.levels[l] {
			configured = append(configured, l)
		}
	}
	snapshot := Snapshot{
		Counters:             p.counters,
		ConfiguredLevels:     configured,
		DiagnosticActive:     active,
		VerboseRatePerSecond: p.verboseRate,
	}
	for _, l := range AllLevels {
		if configuredContains(configured, l) {
			snapshot.Levels = append(snapshot.Levels, l)
			continue
		}
		if active && p.diagLevels[l] {
			snapshot.Levels = append(snapshot.Levels, l)
		}
	}
	sort.Strings(snapshot.Levels)
	if active {
		snapshot.DiagnosticUntil = p.diagUntilWall.Unix()
	}
	return snapshot
}

func configuredContains(levels []string, level string) bool {
	for _, l := range levels {
		if l == level {
			return true
		}
	}
	return false
}
