package job

// Payload is the job payload the Python pipeline persists in
// delivery_jobs.payload_json. Every key here is a key the existing Python
// enqueue path already writes, so a job created by Python is executed by Go
// with no translation layer and no second business representation.
//
// Accessors are deliberately tolerant of a missing key: older jobs (created
// before a field was added) must still execute with the pre-feature default,
// the same way the Python worker treats them.
type Payload struct {
	raw map[string]any
}

// NewPayload wraps a decoded payload_json blob.
func NewPayload(raw map[string]any) *Payload {
	if raw == nil {
		raw = map[string]any{}
	}
	return &Payload{raw: raw}
}

// Raw returns the underlying map (never nil), for fields without a typed
// accessor yet.
func (p *Payload) Raw() map[string]any { return p.raw }

// str returns a string field, or the default when absent/not a string.
func (p *Payload) str(key, def string) string {
	if v, ok := p.raw[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return def
}

// num returns a float64 field (JSON numbers decode as float64), or def.
func (p *Payload) num(key string, def float64) float64 {
	if v, ok := p.raw[key]; ok {
		switch n := v.(type) {
		case float64:
			return n
		case int:
			return float64(n)
		case int64:
			return float64(n)
		}
	}
	return def
}

// boolean returns a bool field, or def when absent.
func (p *Payload) boolean(key string, def bool) bool {
	if v, ok := p.raw[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return def
}

// intList returns a list-of-numbers field as []int64.
func (p *Payload) intList(key string, def []int64) []int64 {
	v, ok := p.raw[key]
	if !ok {
		return def
	}
	switch list := v.(type) {
	case []any:
		out := make([]int64, 0, len(list))
		for _, item := range list {
			switch n := item.(type) {
			case float64:
				out = append(out, int64(n))
			case int:
				out = append(out, int64(n))
			case int64:
				out = append(out, n)
			}
		}
		return out
	case []float64:
		out := make([]int64, 0, len(list))
		for _, n := range list {
			out = append(out, int64(n))
		}
		return out
	case []int:
		out := make([]int64, 0, len(list))
		for _, n := range list {
			out = append(out, int64(n))
		}
		return out
	}
	return def
}

// stringList returns a list-of-strings field.
func (p *Payload) stringList(key string, def []string) []string {
	v, ok := p.raw[key]
	if !ok {
		return def
	}
	switch list := v.(type) {
	case []any:
		out := make([]string, 0, len(list))
		for _, item := range list {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	case []string:
		return list
	}
	return def
}

// --- typed accessors: the delivery-shape decision inputs ----------------- //

// SessionID is the Telegram session the job must be sent through.
func (p *Payload) SessionID() string { return p.str("session_id", "") }

// Text is the fully transformed text/caption for the target.
func (p *Payload) Text() string { return p.str("text", "") }

// ForwardMode is DIRECT_FORWARD | COPY_MESSAGE | CUSTOM_HEADER_COPY.
func (p *Payload) ForwardMode() string { return p.str("forward_mode", "COPY_MESSAGE") }

// ContentMode is AUTO | TEXT_ONLY | MEDIA_ONLY | TEXT_AND_MEDIA.
func (p *Payload) ContentMode() string { return p.str("content_mode", "AUTO") }

// HasMedia reports whether the source message carried media.
func (p *Payload) HasMedia() bool { return p.boolean("has_media", false) }

// MediaType is the ingest-side classification (photo, video, document, ...).
func (p *Payload) MediaType() string { return p.str("media_type", "") }

// MediaGroupID is the album grouping id, "" for a single message.
func (p *Payload) MediaGroupID() string { return p.str("media_group_id", "") }

// AlbumMessageIDs are the ids of the album's members (falls back to the one
// source message, mirroring the Python worker).
func (p *Payload) AlbumMessageIDs() []int64 {
	return p.intList("album_message_ids", nil)
}

// AlbumMode is album | first | split (multi-photo option).
func (p *Payload) AlbumMode() string { return p.str("album_mode", "album") }

// AlbumSent is the map of already-confirmed split-album items
// `{"<source id>": <target id>}` used to resume rather than duplicate.
func (p *Payload) AlbumSent() map[string]int64 {
	v, ok := p.raw["album_sent"]
	if !ok {
		return nil
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	out := make(map[string]int64, len(m))
	for k, item := range m {
		switch n := item.(type) {
		case float64:
			out[k] = int64(n)
		case int:
			out[k] = int64(n)
		case int64:
			out[k] = n
		}
	}
	return out
}

// FallbackEnabled is the rule's copy/re-upload opt-out, snapshotted at
// enqueue. Missing key => older job => default on.
func (p *Payload) FallbackEnabled() bool { return p.boolean("fallback_enabled", true) }

// DeliveryTransport is USER (owner account) | BOT (attached bot token).
func (p *Payload) DeliveryTransport() string { return p.str("delivery_transport", "USER") }

// TopicID is the forum thread to send into (0 = General / none).
func (p *Payload) TopicID() int64 { return int64(p.num("topic_id", 0)) }

// IsReply marks a keyword-reply deliverable: text-only, no media ever.
func (p *Payload) IsReply() bool { return p.boolean("is_reply", false) }

// CorrelationID threads the whole delivery through the log chain.
func (p *Payload) CorrelationID() string { return p.str("correlation_id", "") }

// DelaySeconds is the (already-elapsed) dispatch delay, logged only.
func (p *Payload) DelaySeconds() float64 { return p.num("delay_seconds", 0) }

// AdvancedDelaySeconds is the (already-elapsed) advanced settings delay.
func (p *Payload) AdvancedDelaySeconds() float64 { return p.num("advanced_delay_seconds", 0) }

// AutoDeleteSeconds schedules a delete of the forwarded message (0 = never).
func (p *Payload) AutoDeleteSeconds() int64 { return int64(p.num("auto_delete_seconds", 0)) }

// Branding is the already-performed A->C hop the worker must reproduce
// ({"chat_id": C, "message_id"/"message_ids": [...], "force_forward": bool}).
func (p *Payload) Branding() map[string]any {
	if v, ok := p.raw["branding"]; ok {
		if m, ok := v.(map[string]any); ok {
			return m
		}
	}
	return nil
}

// InlineButtons is the raw inline-button spec for the BOT transport.
func (p *Payload) InlineButtons() []any { return p.stringListAny("inline_buttons") }

// BotTokenID is the stored bot token a BOT job names (never the token).
func (p *Payload) BotTokenID() string {
	// The pipeline snapshots the task's metadata-resolved value at enqueue;
	// older jobs carry nothing and the live rule's read fills it in.
	return p.str("bot_token_id", "")
}

// ForwardOrigin is the "Forwarded from <channel>" header, when the source
// message carried one. The hop decision matches on it.
func (p *Payload) ForwardOrigin() map[string]any {
	if v, ok := p.raw["forward_origin"]; ok {
		if m, ok := v.(map[string]any); ok {
			return m
		}
	}
	return nil
}

func (p *Payload) stringListAny(key string) []any {
	if v, ok := p.raw[key]; ok {
		if list, ok := v.([]any); ok {
			return list
		}
	}
	return nil
}

// OwnerUserID is the tenant the delivery belongs to, for log attribution.
func (p *Payload) OwnerUserID() int64 { return int64(p.num("owner_user_id", 0)) }
