package handlers

import (
	"encoding/json"
	"testing"

	pb "autoforward/proto"
)

// The route pair is stored in the rule's metadata, and the forwarder reads it
// twice: to inherit the attached bot's defaults, and to deliver through that
// bot's credential. `enrichCustomMetadata` is the only place a REST client's
// scalar fields become metadata, and it used to whitelist neither key — so a
// dashboard or Android client could attach a bot, see it saved, and get a task
// that inherited nothing and delivered on the account lane. The attachment has
// to survive the round trip, and unknown keys must keep surviving too.
func TestEnrichCustomMetadataKeepsTheRoutePair(t *testing.T) {
	body := []byte(`{
		"source_chat_id": "-1001",
		"target_chat_id": "-1002",
		"bot_token_id": "2a1f6c00-0000-4000-8000-000000000001",
		"delivery_transport": "bot",
		"forward_delay_seconds": 12
	}`)
	rule := &pb.ForwardRule{}
	enrichCustomMetadata(body, rule)

	var meta map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
		t.Fatalf("metadata is not JSON: %v (%q)", err, rule.CustomMetadataJson)
	}
	if got := meta["bot_token_id"]; got != "2a1f6c00-0000-4000-8000-000000000001" {
		t.Fatalf("bot_token_id was dropped: %#v", got)
	}
	if got := meta["delivery_transport"]; got != "bot" {
		t.Fatalf("delivery_transport was dropped: %#v", got)
	}
	if got, _ := meta["forward_delay_seconds"].(float64); got != 12 {
		t.Fatalf("forward_delay_seconds = %#v, want 12", meta["forward_delay_seconds"])
	}
}

// A rule with no attached bot is the normal account lane: the enrichment must
// not invent a key (an empty string would be read as "attached, to nothing").
func TestEnrichCustomMetadataDoesNotInventABot(t *testing.T) {
	rule := &pb.ForwardRule{}
	enrichCustomMetadata([]byte(`{"source_chat_id": "-1001", "target_chat_id": "-1002"}`), rule)

	if rule.CustomMetadataJson == "" {
		return // nothing to inspect, nothing invented
	}
	var meta map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
		t.Fatalf("metadata is not JSON: %v", err)
	}
	if _, present := meta["bot_token_id"]; present {
		t.Fatalf("bot_token_id invented with no value: %#v", meta["bot_token_id"])
	}
}

// `metadata` is the escape hatch for keys the parity list does not name; the
// route pair must work through it as well, because the Android editor sends a
// nested metadata object.
func TestEnrichCustomMetadataTakesTheRoutePairFromNestedMetadata(t *testing.T) {
	body := []byte(`{"metadata": {"bot_token_id": "tok-1", "delivery_transport": "account"}}`)
	rule := &pb.ForwardRule{}
	enrichCustomMetadata(body, rule)

	var meta map[string]any
	if err := json.Unmarshal([]byte(rule.CustomMetadataJson), &meta); err != nil {
		t.Fatalf("metadata is not JSON: %v", err)
	}
	if meta["bot_token_id"] != "tok-1" || meta["delivery_transport"] != "account" {
		t.Fatalf("nested route pair lost: %#v", meta)
	}
}
