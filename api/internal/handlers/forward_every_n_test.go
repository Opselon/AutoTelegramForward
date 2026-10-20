package handlers

import (
	"encoding/json"
	"testing"

	pb "autoforward/proto"
)

// "Forward every Nth message" is an advanced setting: the dashboard and the
// mobile editors post it flat, next to the other advanced fields, and
// `enrichCustomMetadata` is the only place a REST body becomes rule metadata.
// A key missing from its whitelist is the quietest possible bug — the form
// saves, the toast says saved, and the engine keeps forwarding everything.
func TestEnrichCustomMetadataKeepsForwardEveryN(t *testing.T) {
	body := []byte(`{"source_chat_id":"-1001","target_chat_id":"-1002","forward_every_n":5}`)
	rule := &pb.ForwardRule{}
	enrichCustomMetadata(body, rule)

	var meta map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
		t.Fatalf("metadata is not JSON: %v (%q)", err, rule.CustomMetadataJson)
	}
	if got, ok := meta["forward_every_n"].(float64); !ok || got != 5 {
		t.Fatalf("forward_every_n = %#v, want 5", meta["forward_every_n"])
	}
}

// The interval is a whole number of messages in [1, 1000]. Out-of-range input is
// clamped rather than rejected (a stale client must still be able to save), and
// anything that is not a number is dropped so the rule keeps its default.
func TestForwardEveryNIsNormalizedToALegalInterval(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    float64
		dropped bool
	}{
		{"keeps a legal value", `{"forward_every_n":7}`, 7, false},
		{"one stays one", `{"forward_every_n":1}`, 1, false},
		{"zero means off, never silence", `{"forward_every_n":0}`, 1, false},
		{"negative is clamped", `{"forward_every_n":-4}`, 1, false},
		{"above the ceiling is clamped", `{"forward_every_n":99999}`, 1000, false},
		{"the ceiling is legal", `{"forward_every_n":1000}`, 1000, false},
		{"a fraction is truncated", `{"forward_every_n":5.9}`, 5, false},
		{"a numeric string is accepted", `{"forward_every_n":"12"}`, 12, false},
		{"text is dropped", `{"forward_every_n":"soon"}`, 0, true},
		{"null is dropped", `{"forward_every_n":null}`, 0, true},
		{"an object is dropped", `{"forward_every_n":{"n":3}}`, 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rule := &pb.ForwardRule{}
			enrichCustomMetadata([]byte(tc.raw), rule)
			var meta map[string]any
			if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
				t.Fatalf("metadata is not JSON: %v (%q)", err, rule.CustomMetadataJson)
			}
			got, present := meta["forward_every_n"]
			if tc.dropped {
				if present {
					t.Fatalf("forward_every_n should be dropped, got %#v", got)
				}
				return
			}
			if n, ok := got.(float64); !ok || n != tc.want {
				t.Fatalf("forward_every_n = %#v, want %v", got, tc.want)
			}
		})
	}
}

// A task that never configured the feature must keep the metadata it always had:
// the enrichment may not add the key, because "absent" is what every existing
// rule and every backup of one looks like.
func TestForwardEveryNIsNotInvented(t *testing.T) {
	rule := &pb.ForwardRule{}
	enrichCustomMetadata([]byte(`{"source_chat_id":"-1001"}`), rule)
	var meta map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
		t.Fatalf("metadata is not JSON: %v", err)
	}
	if _, present := meta["forward_every_n"]; present {
		t.Fatalf("forward_every_n was invented: %#v", meta)
	}
}

// The metadata blob has a size ceiling that keeps only a whitelist of worker-read
// fields when exceeded. A key outside that list is deleted on a large payload,
// which would silently reset a task's interval to "every message".
func TestForwardEveryNSurvivesTheMetadataSizeCap(t *testing.T) {
	meta := map[string]any{"forward_every_n": float64(9)}
	for i := 0; i < 4000; i++ {
		meta["unknown_field_"+string(rune('a'+i%26))+string(rune(i))] = "x"
	}
	normalizeAdvancedMetadata(meta)
	if got, ok := meta["forward_every_n"].(float64); !ok || got != 9 {
		t.Fatalf("forward_every_n = %#v, want 9 after the cap", meta["forward_every_n"])
	}
}
