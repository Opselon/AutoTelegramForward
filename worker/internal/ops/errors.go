package ops

import (
	"strings"
	"sync"
	"time"
)

// ErrorItem is one entry of the deduped error/warn ring, in the contract's
// §1.5 shape. Every field is sanitized by construction: the ring is fed
// from the engine's classified failures and log lines, which carry ids and
// reason codes — never message content, never credentials.
type ErrorItem struct {
	// Ts is when this entry was LAST seen (RFC3339). first_seen/last_seen
	// fold the dedup chain's span into the item itself.
	Ts            string `json:"ts"`
	Severity      string `json:"severity"`
	Component     string `json:"component"`
	WorkerID      string `json:"worker_id"`
	RuleID        string `json:"rule_id,omitempty"`
	JobID         string `json:"job_id,omitempty"`
	Category      string `json:"category"`
	Code          string `json:"code"`
	Retryable     bool   `json:"retryable"`
	RetryCount    int    `json:"retry_count"`
	CorrelationID string `json:"correlation_id,omitempty"`
	Message       string `json:"message"`
	// Count/FirstSeen are the dedup bookkeeping the contract folds into the
	// returned rows (count, first_seen, last_seen).
	Count     int64  `json:"-"`
	FirstSeen string `json:"-"`
	dedupKey  string `json:"-"`
}

// errorRing is the bounded, deduped recent error/warn ring.
//
// DEDUP: entries collapse on (component, code, rule_id). A repeat of an
// entry already in the ring bumps that entry's count and refreshes its
// last_seen, so a tight retry loop produces ONE line ("4 times in the last
// minute") instead of 400 identical lines evicting everything else.
//
// BOUND: 512 entries. When the ring is full, a NEW dedup key evicts the
// LEAST RECENTLY SEEN entry — by last_seen, not by insertion — so a fresh
// problem can always be seen even when the ring is full of a long-running
// old one.
//
// In-memory by design: the contract forbids per-message DB writes, and a
// diagnostic ring that cost a durable write per entry would be paid on the
// exact path (failure handling) that must stay cheap.
type errorRing struct {
	mu       sync.Mutex
	capacity int
	entries  []*ErrorItem
	index    map[string]*ErrorItem
}

// newErrorRing builds a ring with the contract's 512-entry bound.
func newErrorRing(capacity int) *errorRing {
	if capacity < 1 {
		capacity = 1
	}
	return &errorRing{
		capacity: capacity,
		entries:  make([]*ErrorItem, 0, capacity),
		index:    map[string]*ErrorItem{},
	}
}

// Record appends one error/warn line, deduped.
//
// The message arrives from the engine's log paths (logger.Warn/Error args)
// or its classified failures (DeliveryFailure.Code/ErrorType/Message). Both
// are ids-and-codes shaped; sanitize() is still applied so a future caller
// passing a longer string cannot leak content or a secret into the surface.
func (r *errorRing) Record(it ErrorItem) {
	if it.Component == "" && it.Category == "" {
		return
	}
	it.Message = sanitize(it.Message)
	it.Category = sanitize(it.Category)
	it.Code = sanitize(it.Code)
	it.Severity = uppercaseSeverity(it.Severity)
	if it.Ts == "" {
		it.Ts = time.Now().UTC().Format(time.RFC3339)
	}
	key := dedupKey(it.Component, it.Code, it.RuleID)
	it.dedupKey = key

	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, ok := r.index[key]; ok {
		existing.Count++
		existing.Ts = it.Ts
		if it.Message != "" {
			existing.Message = it.Message
		}
		if it.RetryCount > existing.RetryCount {
			existing.RetryCount = it.RetryCount
		}
		existing.Retryable = it.Retryable
		// last_seen moves; first_seen stays — the span is what an operator
		// reads as "how long has this been happening".
		return
	}
	it.Count = 1
	it.FirstSeen = it.Ts
	if len(r.entries) >= r.capacity {
		// Evict the least-recently-seen, not the oldest insertion: a
		// recurring old problem that fired 30 seconds ago must not survive
		// over a brand-new failure just because it was inserted first.
		oldestIdx := 0
		for i, e := range r.entries[1:] {
			if e.Ts < r.entries[oldestIdx].Ts {
				oldestIdx = i + 1
			}
		}
		delete(r.index, r.entries[oldestIdx].dedupKey)
		r.entries = append(r.entries[:oldestIdx], r.entries[oldestIdx+1:]...)
	}
	item := it // copy before publishing
	r.entries = append(r.entries, &item)
	r.index[key] = &item
}

// Items returns the ring's entries newest-first, with the dedup counts
// folded into the returned rows (count, first_seen).
func (r *errorRing) Items() []map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]map[string]any, 0, len(r.entries))
	// Newest first: the operator's eye goes to the top of the list.
	for i := len(r.entries) - 1; i >= 0; i-- {
		e := r.entries[i]
		out = append(out, map[string]any{
			"ts":             e.Ts,
			"severity":       e.Severity,
			"component":      e.Component,
			"worker_id":      e.WorkerID,
			"rule_id":        e.RuleID,
			"job_id":         e.JobID,
			"category":       e.Category,
			"code":           e.Code,
			"retryable":      e.Retryable,
			"retry_count":    e.RetryCount,
			"correlation_id": e.CorrelationID,
			"message":        e.Message,
			"count":          e.Count,
			"first_seen":     e.FirstSeen,
		})
	}
	return out
}

// Total is the number of distinct deduped entries currently held.
func (r *errorRing) Total() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.entries)
}

// Filter applies the /ops/errors query parameters (severity, component,
// rule_id, since, limit) to a copy of the ring. limit is clamped to 256 —
// the contract's bound — and every field except since is an exact,
// case-insensitive compare (since is a timestamp floor).
func (r *errorRing) Filter(severity, component, ruleID string, since time.Time, limit int) []map[string]any {
	if limit <= 0 || limit > 256 {
		limit = 256
	}
	severity = strings.ToUpper(strings.TrimSpace(severity))
	component = strings.TrimSpace(component)
	ruleID = strings.TrimSpace(ruleID)
	out := make([]map[string]any, 0, limit)
	for _, row := range r.Items() {
		if severity != "" {
			if s, ok := row["severity"].(string); !ok || strings.ToUpper(s) != severity {
				continue
			}
		}
		if component != "" {
			if s, ok := row["component"].(string); !ok || !strings.EqualFold(s, component) {
				continue
			}
		}
		if ruleID != "" {
			if s, _ := row["rule_id"].(string); s != ruleID {
				continue
			}
		}
		if !since.IsZero() {
			if ts, ok := row["ts"].(string); ok {
				if parsed, err := time.Parse(time.RFC3339, ts); err == nil && parsed.Before(since) {
					continue
				}
			}
		}
		out = append(out, row)
		if len(out) >= limit {
			break
		}
	}
	return out
}

// dedupKey is the contract's (component, code, rule_id) triple. The
// separator is NUL: a field can never contain one, so the triple is
// unambiguous without escaping.
func dedupKey(component, code, ruleID string) string {
	return component + "\x00" + code + "\x00" + ruleID
}

// uppercaseSeverity normalizes to the contract's WARN/ERROR vocabulary;
// anything else is kept as-given (CRITICAL arrives from the logger mapping).
func uppercaseSeverity(s string) string {
	s = strings.ToUpper(strings.TrimSpace(s))
	switch s {
	case "WARNING":
		return "WARN"
	case "ERR":
		return "ERROR"
	}
	return s
}
