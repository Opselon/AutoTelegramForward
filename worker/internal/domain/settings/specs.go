// Package settings is the Go port of core/domain/rule_settings.py: the
// declarative registry of per-task (per-rule) feature settings, together
// with the read/coerce/apply machinery that keeps the Go Worker's view of a
// task's settings byte-for-byte equal to the Python Core's.
//
// Two things make this more than a table of defaults:
//
//   - Every value crosses a validation boundary. The Python registry's
//     coerce_value is what the bot's widgets and the REST/gRPC control plane
//     both rely on; a Go side that read a stored value without the same
//     coercion would silently disagree with the panel about what a task is
//     configured to do.
//   - Inheritance. A task inherits the defaults of the bot token it has
//     attached (core/application/effective_settings.py, mirrored in
//     Effective/Materialize below). The registry's inheritable flag is what
//     keeps that inheritance from pointing one task's traffic at another
//     task's chats: a setting that names a chat, a route or a schedule is
//     never inheritable, so a bot default can never carry it.
//
// The registry is deliberately a literal port, key for key: adding an entry
// in Python is what makes a setting readable through the API, and the Go side
// must answer with the same default for the same key or the two engines
// diverge on the same task.
package settings

import (
	"fmt"
	"strconv"
	"strings"
)

// SCHEMA_VERSION mirrors core/domain/rule_settings.SCHEMA_VERSION.
const SCHEMA_VERSION = 1

// Kind is the value type of one setting: bool | int | float | str | list |
// dict, the same vocabulary the Python registry declares.
type Kind string

const (
	KindBool  Kind = "bool"
	KindInt   Kind = "int"
	KindFloat Kind = "float"
	KindStr   Kind = "str"
	KindList  Kind = "list"
	KindDict  Kind = "dict"
)

// SettingSpec is one controllable per-task setting, the port of the Python
// SettingSpec dataclass. Only the fields the Worker's resolution and parity
// surfaces consume are carried; the label/help/group metadata that exists to
// render a UI stays on the Python side (the Worker renders nothing).
type SettingSpec struct {
	// Kind is the value type; coercion accepts exactly what Python's
	// coerce_value accepts for the same kind (a 0/1 int for bool, an int
	// for a float slot, a numeric string never).
	Kind Kind
	// Default is the registry's canonical default for the key. Defaults
	// are spelled in the same JSON-native Go types a decode produces
	// (bool, float64, string, []any, map[string]any) so an equivalence
	// check between a stored value and the default is a plain compare.
	Default any
	// Inheritable reports whether a bot default may carry this setting
	// for every task that uses the bot. False for anything that names a
	// chat, a route or a schedule (delivery_transport, bot_token_id,
	// source_policies, target_policies, link_buttons, affiliate_kit,
	// auto_post_scheduler, power_schedule, auto_posting, power_control):
	// inheriting those would point one task's forwards at someone else's
	// chats, so a bot default that carries one is refused, and resolution
	// never applies one.
	Inheritable bool
	// Choices is the closed set of legal values for a str setting
	// ("" always passes: the registry treats an empty string as "unset").
	Choices []string
	// Minimum/Maximum bound int and float settings.
	Minimum *float64
	Maximum *float64
	// ItemKind is the element type of a list setting ("str" when the
	// registry declares item_kind="str", "any" otherwise).
	ItemKind string
}

// SettingError reports that a setting name is unknown or its value failed
// validation. It mirrors core/domain/rule_settings.SettingError: a code, a
// message and the key, so the parity harness can compare the two engines'
// refusals word for word.
type SettingError struct {
	Code    string
	Message string
	Key     string
}

func (e *SettingError) Error() string { return e.Message }

// specOpts build one registry entry, named for the Python helper (_s) whose
// place they take. Inheritable defaults to TRUE in the Python registry — a
// Go zero value is false — so every entry spells the flag out and only the
// chat/route/schedule keys opt out.
func newSpec(kind Kind, def any, inheritable bool, opts ...specOpt) SettingSpec {
	spec := SettingSpec{Kind: kind, Default: def, Inheritable: inheritable, ItemKind: "any"}
	for _, opt := range opts {
		opt(&spec)
	}
	return spec
}

type specOpt func(*SettingSpec)

func withChoices(choices ...string) specOpt {
	return func(s *SettingSpec) { s.Choices = choices }
}

func withMinMax(min, max float64) specOpt {
	return func(s *SettingSpec) {
		mn, mx := min, max
		s.Minimum, s.Maximum = &mn, &mx
	}
}

func withItemStr() specOpt {
	return func(s *SettingSpec) { s.ItemKind = "str" }
}

// SETTING_SPECS is the registry: every per-task setting keyed by its entity
// property name. It is a port of core/domain/rule_settings.SETTING_SPECS,
// and the key set must stay equal to it — a key present on one engine and
// absent on the other is the two engines disagreeing about the same task.
//
// Settings that already have a first-class ForwardRule field (forward_mode,
// content_mode, delay_seconds, sync_edits, prompt_id, ...) are deliberately
// NOT listed: they belong on the rule itself, and a second write path could
// disagree with the first.
var SETTING_SPECS = map[string]SettingSpec{
	// ---- content cleanup -------------------------------------------------
	"header":               newSpec(KindStr, "", true),
	"footer":               newSpec(KindStr, "", true),
	"replacements":         newSpec(KindDict, map[string]any{}, true),
	"link_replacement":     newSpec(KindStr, "", true),
	"link_replace_enabled": newSpec(KindBool, false, true),
	"link_rewrite_map":     newSpec(KindDict, map[string]any{}, true),
	"mention_rewrite_map":  newSpec(KindDict, map[string]any{}, true),
	"clean_usernames":      newSpec(KindBool, false, true),
	"clean_hashtags":       newSpec(KindBool, false, true),
	"clean_phones":         newSpec(KindBool, false, true),
	"clean_buttons":        newSpec(KindBool, false, true),
	"clean_empty_lines":    newSpec(KindBool, false, true),
	"clean_custom_words":   newSpec(KindList, []any{}, true, withItemStr()),
	"remove_emojis":        newSpec(KindBool, false, true),
	"cleaner_enabled":      newSpec(KindBool, false, true),
	"cleaner_config":       newSpec(KindDict, map[string]any{}, true),

	// ---- media filtering -------------------------------------------------
	"block_text":          newSpec(KindBool, false, true),
	"block_voice":         newSpec(KindBool, false, true),
	"block_audio":         newSpec(KindBool, false, true),
	"block_video":         newSpec(KindBool, false, true),
	"block_stickers":      newSpec(KindBool, false, true),
	"block_links":         newSpec(KindBool, false, true),
	"allowed_media_types": newSpec(KindList, []any{}, true, withItemStr()),
	"album_mode":          newSpec(KindStr, "album", true, withChoices("album", "first", "split")),
	"watermark":           newSpec(KindStr, "", true),
	"watermark_text":      newSpec(KindStr, "", true),

	// ---- sending behaviour ----------------------------------------------
	"duplicate_filters":       newSpec(KindBool, false, true),
	"max_time_edit":           newSpec(KindInt, float64(86400), true, withMinMax(0, 2592000)),
	"edit_time_limit_seconds": newSpec(KindInt, float64(0), true, withMinMax(0, 2592000)),
	"forward_delay_seconds":   newSpec(KindFloat, float64(0), true, withMinMax(0, 86400)),
	"auto_delete_seconds":     newSpec(KindInt, float64(0), true, withMinMax(0, 604800)),
	"forward_every_n":         newSpec(KindInt, float64(1), true, withMinMax(1, 1000)),
	"debug_mode":              newSpec(KindBool, false, true),
	"sync_replies":            newSpec(KindBool, false, true),
	"process_raw_text":        newSpec(KindBool, false, true),
	"feature_group":           newSpec(KindStr, "all", true),
	"sender_type":             newSpec(KindStr, "Personal Telegram Account", true),

	// ---- keywords / replies ---------------------------------------------
	"keyword_confirmation": newSpec(KindBool, false, true),
	"keyword_reply":        newSpec(KindStr, "", true),
	"translate_lang":       newSpec(KindStr, "", true),

	// ---- audience filters ------------------------------------------------
	"blacklist":       newSpec(KindList, []any{}, true, withItemStr()),
	"whitelist":       newSpec(KindList, []any{}, true, withItemStr()),
	"blacklist_users": newSpec(KindList, []any{}, true, withItemStr()),
	"whitelist_users": newSpec(KindList, []any{}, true, withItemStr()),

	// ---- AI --------------------------------------------------------------
	"ai_creativity":    newSpec(KindFloat, float64(0.7), true, withMinMax(0, 2)),
	"ai_max_tokens":    newSpec(KindInt, float64(600), true, withMinMax(1, 100000)),
	"ocr_image_ai":     newSpec(KindBool, false, true),
	"news_digest":      newSpec(KindBool, false, true),
	"smart_ai_rewrite": newSpec(KindBool, false, true),
	"crypto_mode":      newSpec(KindBool, false, true),
	"paid_addons":      newSpec(KindDict, map[string]any{}, true),

	// ---- bot route -------------------------------------------------------
	// Two keys, one decision: who the copy is sent as. Both are
	// non-inheritable — a bot default may never name a route.
	"delivery_transport": newSpec(KindStr, "USER", false, withChoices("USER", "BOT")),
	"bot_token_id":       newSpec(KindStr, "", false),

	// ---- automation ------------------------------------------------------
	"auto_post_scheduler": newSpec(KindDict, map[string]any{"enabled": false}, false),
	"auto_posting":        newSpec(KindBool, false, false),
	"power_control":       newSpec(KindBool, false, false),
	"power_schedule":      newSpec(KindDict, map[string]any{"enabled": false}, false),
	"button_to_text":      newSpec(KindBool, false, true),
	"link_buttons":        newSpec(KindList, []any{}, false, withItemStr()),
	"affiliate_kit":       newSpec(KindDict, map[string]any{}, false),

	// ---- per-task content rules ------------------------------------------
	"content_policy_enabled": newSpec(KindBool, false, true),
	"blacklist_off":          newSpec(KindBool, false, true),
	"whitelist_off":          newSpec(KindBool, false, true),
	"blacklist_words":        newSpec(KindList, []any{}, true, withItemStr()),
	"whitelist_words":        newSpec(KindList, []any{}, true, withItemStr()),
	"whitelist_mode":         newSpec(KindStr, "any", true, withChoices("any", "all", "phrase")),
	"remove_links":           newSpec(KindBool, false, true),
	"strip_words":            newSpec(KindList, []any{}, true, withItemStr()),
	"regex_block":            newSpec(KindList, []any{}, true, withItemStr()),
	"regex_require":          newSpec(KindList, []any{}, true, withItemStr()),
	"source_policies":        newSpec(KindDict, map[string]any{}, false),
	"target_policies":        newSpec(KindDict, map[string]any{}, false),

	// ---- Branding hop (A -> C -> B): the *trigger* policy ----------------
	// The enable switch and the channel itself are first-class rule fields;
	// what is registered here is which messages earn the branded hop.
	"branding_triggers_enabled": newSpec(KindBool, false, true),
	"branding_origin_titles":    newSpec(KindList, []any{}, true, withItemStr()),
	"branding_origin_ids":       newSpec(KindList, []any{}, true, withItemStr()),
	"branding_keywords":         newSpec(KindList, []any{}, true, withItemStr()),
	"branding_match_mode":       newSpec(KindStr, "any", true, withChoices("any", "all")),
	"branding_require_forward":  newSpec(KindBool, false, true),
	"branding_require_channel":  newSpec(KindBool, false, true),
	"branding_force_forward":    newSpec(KindBool, true, true),
}

// SortedKeys returns every registered key in sorted order. The sorted order
// is the registry's own comparison order — Python's effective() iterates
// SETTING_SPECS and describe() reports a sorted list, so any Go output that
// must line up with either is built from this order.
func SortedKeys() []string {
	keys := make([]string, 0, len(SETTING_SPECS))
	for key := range SETTING_SPECS {
		keys = append(keys, key)
	}
	sortStrings(keys)
	return keys
}

// sortStrings orders a short string list in place. The key set is ~90
// entries; the sort is insertion so the package stays dependency-free.
func sortStrings(list []string) {
	for i := 1; i < len(list); i++ {
		for j := i; j > 0 && list[j-1] > list[j]; j-- {
			list[j-1], list[j] = list[j], list[j-1]
		}
	}
}

// --- validation ------------------------------------------------------------ //

// CoerceValue validates and normalises one setting value, or returns a
// SettingError. It is a port of core/domain/rule_settings.coerce_value:
// the same types are accepted, the same bounds enforced, the same refusals
// raised for the same reasons — a value the registry refuses on one engine
// must be refused on the other, or the two would store different things for
// the same request.
//
// The one deliberate divergence from a transliteration is the input type:
// JSON decoding hands this function float64 for every number, so an integer
// slot accepts a float64 that IS an integer and refuses one that is not —
// Python's isinstance(int) test drawn against Go's type system.
func CoerceValue(key string, value any) (any, error) {
	spec, ok := SETTING_SPECS[key]
	if !ok {
		return nil, &SettingError{
			Code:    "unknown_setting",
			Message: fmt.Sprintf("unknown setting %q", key),
			Key:     key,
		}
	}
	if value == nil {
		return nil, &SettingError{
			Code:    "invalid_type",
			Message: fmt.Sprintf("%s may not be null", key),
			Key:     key,
		}
	}
	switch spec.Kind {
	case KindBool:
		// bool must be checked before int: a JSON true is not "an int
		// that happens to be 1", and Python's coerce makes the same
		// distinction (bool is checked first there for exactly this
		// reason).
		switch b := value.(type) {
		case bool:
			return b, nil
		case float64:
			if b == 0 || b == 1 {
				return b != 0, nil
			}
		case int:
			if b == 0 || b == 1 {
				return b != 0, nil
			}
		case int64:
			if b == 0 || b == 1 {
				return b != 0, nil
			}
		}
		return nil, &SettingError{
			Code:    "invalid_type",
			Message: fmt.Sprintf("%s expects a boolean", key),
			Key:     key,
		}
	case KindInt:
		switch n := value.(type) {
		case float64:
			if n != float64(int64(n)) {
				return nil, &SettingError{
					Code:    "invalid_type",
					Message: fmt.Sprintf("%s expects an integer", key),
					Key:     key,
				}
			}
			return bounded(key, int64(n), &spec)
		case int:
			return bounded(key, int64(n), &spec)
		case int64:
			return bounded(key, n, &spec)
		}
		return nil, &SettingError{
			Code:    "invalid_type",
			Message: fmt.Sprintf("%s expects an integer", key),
			Key:     key,
		}
	case KindFloat:
		switch n := value.(type) {
		case float64:
			return boundedFloat(key, n, &spec)
		case int:
			return boundedFloat(key, float64(n), &spec)
		case int64:
			return boundedFloat(key, float64(n), &spec)
		}
		return nil, &SettingError{
			Code:    "invalid_type",
			Message: fmt.Sprintf("%s expects a number", key),
			Key:     key,
		}
	case KindStr:
		s, ok := value.(string)
		if !ok {
			return nil, &SettingError{
				Code:    "invalid_type",
				Message: fmt.Sprintf("%s expects a string", key),
				Key:     key,
			}
		}
		s = strings.TrimSpace(s)
		if len(spec.Choices) > 0 && s != "" && !containsString(spec.Choices, s) {
			return nil, &SettingError{
				Code:    "invalid_choice",
				Message: fmt.Sprintf("%s must be one of %s", key, strings.Join(spec.Choices, ", ")),
				Key:     key,
			}
		}
		return s, nil
	case KindList:
		switch typed := value.(type) {
		case []any:
			return coerceList(key, typed, &spec)
		case []string:
			list := make([]any, len(typed))
			for i, item := range typed {
				list[i] = item
			}
			return coerceList(key, list, &spec)
		}
		return nil, &SettingError{
			Code:    "invalid_type",
			Message: fmt.Sprintf("%s expects a list", key),
			Key:     key,
		}
	case KindDict:
		dict, ok := value.(map[string]any)
		if !ok {
			return nil, &SettingError{
				Code:    "invalid_type",
				Message: fmt.Sprintf("%s expects an object", key),
				Key:     key,
			}
		}
		// JSON keys are always strings; normalising here keeps an
		// equality check between a read and a re-write from failing on
		// int keys, exactly as the Python registry does.
		out := make(map[string]any, len(dict))
		for k, v := range dict {
			out[str(k)] = v
		}
		return out, nil
	}
	return nil, &SettingError{
		Code:    "invalid_spec",
		Message: fmt.Sprintf("%s has an unsupported kind", key),
		Key:     key,
	}
}

// coerceList validates one list setting the registry declared with an
// item_kind, dropping empty string entries the way the Python registry
// does (an item that is only whitespace carries nothing).
func coerceList(key string, list []any, spec *SettingSpec) (any, error) {
	if spec.ItemKind != "str" {
		return list, nil
	}
	out := make([]any, 0, len(list))
	for _, item := range list {
		entry, ok := item.(string)
		if !ok {
			return nil, &SettingError{
				Code:    "invalid_type",
				Message: fmt.Sprintf("%s expects a list of strings", key),
				Key:     key,
			}
		}
		trimmed := strings.TrimSpace(entry)
		if trimmed == "" {
			continue
		}
		// Python refuses an unusable user regex for the two keys whose
		// contents are compiled as patterns; a refused item is a
		// validation failure here too, so an unsafe pattern can never
		// be stored on either engine.
		if key == "regex_block" || key == "regex_require" {
			if err := ValidateUserRegex(trimmed); err != nil {
				return nil, &SettingError{
					Code:    "invalid_choice",
					Message: fmt.Sprintf("%s contains invalid or unsafe regular expression: %s", key, firstN(trimmed, 32)),
					Key:     key,
				}
			}
		}
		out = append(out, trimmed)
	}
	return out, nil
}

func bounded(key string, value int64, spec *SettingSpec) (any, error) {
	if spec.Minimum != nil && float64(value) < *spec.Minimum {
		return nil, &SettingError{
			Code:    "out_of_range",
			Message: fmt.Sprintf("%s must be >= %s", key, formatBound(*spec.Minimum)),
			Key:     key,
		}
	}
	if spec.Maximum != nil && float64(value) > *spec.Maximum {
		return nil, &SettingError{
			Code:    "out_of_range",
			Message: fmt.Sprintf("%s must be <= %s", key, formatBound(*spec.Maximum)),
			Key:     key,
		}
	}
	return value, nil
}

func boundedFloat(key string, value float64, spec *SettingSpec) (any, error) {
	if spec.Minimum != nil && value < *spec.Minimum {
		return nil, &SettingError{
			Code:    "out_of_range",
			Message: fmt.Sprintf("%s must be >= %s", key, formatBound(*spec.Minimum)),
			Key:     key,
		}
	}
	if spec.Maximum != nil && value > *spec.Maximum {
		return nil, &SettingError{
			Code:    "out_of_range",
			Message: fmt.Sprintf("%s must be <= %s", key, formatBound(*spec.Maximum)),
			Key:     key,
		}
	}
	return value, nil
}

// formatBound renders a bound the way the Python registry's f-string does:
// int-valued bounds print without a decimal point ("must be >= 0"), so the
// parity harness compares the two engines' refusal messages verbatim.
func formatBound(v float64) string {
	if v == float64(int64(v)) {
		return strconv.FormatInt(int64(v), 10)
	}
	return strconv.FormatFloat(v, 'g', -1, 64)
}

func containsString(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

func firstN(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func str(value any) string {
	if s, ok := value.(string); ok {
		return s
	}
	return fmt.Sprint(value)
}
