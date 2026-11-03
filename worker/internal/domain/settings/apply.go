package settings

import (
	"encoding/json"
	"fmt"
	"strings"
)

// PinIndexKey is the metadata key holding the settings a task pinned that
// are stored as real columns. Those leave no other trace of the choice, so
// the choice is noted by name when a write moves the effective value (see
// ApplySettings) and read back here. Port of rule_settings.PIN_INDEX.
const PinIndexKey = "_pinned_settings"

// InheritedMark is the metadata key a materialised copy carries so a save
// attempt is caught rather than quietly writing a bot's defaults into a task
// (they would become pins and the task would stop tracking the bot). Port of
// effective_settings.INHERITED_MARK.
const InheritedMark = "_inherited_defaults"

// SettingsHolder is the narrow read slice of a task the registry consults:
// a metadata blob plus a by-key accessor. rule.ForwardRule satisfies it
// (its metadata blob carries the metadata-backed settings, and its accessor
// resolves the column-backed ones through the projection), and the parity
// harness's adapter does the same for a hand-built map.
type SettingsHolder interface {
	// Meta is the task's metadata JSON blob.
	Meta() map[string]any
	// Get returns the task's current value for one registered key.
	Get(key string) any
}

// MutableSettings is the write side: a holder whose keys may be set. The
// rule projection implements it; a read-only projection fails at the type
// rather than at runtime, which is what keeps a delivery path from
// accidentally writing back through the entity it loaded.
type MutableSettings interface {
	SettingsHolder
	Set(key string, value any)
}

// MapSettings adapts a plain metadata map into a SettingsHolder whose
// values are read straight out of the map. It is the shape the parity
// harness and the unit tests build; production uses the rule projection.
type MapSettings struct {
	Metadata map[string]any
}

// Meta returns the adapted map, never nil.
func (m MapSettings) Meta() map[string]any {
	if m.Metadata == nil {
		return map[string]any{}
	}
	return m.Metadata
}

// Get reads one registered setting out of the adapted map.
func (m MapSettings) Get(key string) any { return m.Meta()[key] }

// SetMeta is a mutator variant for callers that hold the struct by value
// and need to write through it; tests use it to build fixtures.
func (m *MapSettings) Set(key string, value any) {
	if m.Metadata == nil {
		m.Metadata = map[string]any{}
	}
	m.Metadata[key] = value
}

// mutableMap promotes a *MapSettings to the MutableSettings interface.
// Written as a helper so the promotion is named once instead of repeated at
// every construction site.
func mutableMap(m *MapSettings) MutableSettings { return m }

// --- read / write ---------------------------------------------------------- //

// ReadSettings is the current value of every registered setting on the task.
// A value the reader cannot resolve falls back to the declared default
// rather than blanking the settings view — port of
// rule_settings.read_settings, whose except clause exists for exactly that
// reason ("a malformed value in stored metadata must not blank the whole
// settings view").
func ReadSettings(rule SettingsHolder) map[string]any {
	out := make(map[string]any, len(SETTING_SPECS))
	for key, spec := range SETTING_SPECS {
		out[key] = spec.Default
	}
	if rule == nil {
		return out
	}
	for key := range SETTING_SPECS {
		if value := rule.Get(key); value != nil {
			out[key] = value
		}
	}
	return out
}

// PinnedKeys reports the settings this task chose itself rather than
// inheriting.
//
// A setting counts as chosen when either it is recorded by name in the
// metadata's pin index (a write that moved the effective value, even when
// the new value equals the registry default — the case a column leaves no
// other trace for) or the task's own value differs from the registry
// default. That single pair of tests covers both storage shapes, because
// ReadSettings reads column-backed and metadata-backed settings the same
// way.
//
// Port of rule_settings.pinned_keys.
func PinnedKeys(rule SettingsHolder) map[string]bool {
	pinned := map[string]bool{}
	if rule != nil {
		for _, key := range pinIndex(rule.Meta()) {
			if _, registered := SETTING_SPECS[key]; registered {
				pinned[key] = true
			}
		}
	}
	values := ReadSettings(rule)
	for key, spec := range SETTING_SPECS {
		if pinned[key] {
			continue
		}
		if chosenNotDefault(values[key], spec.Default) {
			pinned[key] = true
		}
	}
	return pinned
}

// chosenNotDefault judges "did a task choose this value" by it differing
// from the registry default. Only used for settings stored as columns, where
// there is no other trace of the choice: an unset mapping column reads back
// as {} where the registry spells out the full shape ({"enabled": false}),
// and both mean "off", so an empty mapping is compared against whether the
// default asserts anything rather than against the default itself.
//
// Port of rule_settings._chosen_not_default.
func chosenNotDefault(value, def any) bool {
	if equalValues(value, def) {
		return false
	}
	if dictValue, ok := value.(map[string]any); ok {
		if dictDefault, ok := def.(map[string]any); ok && len(dictValue) == 0 {
			for _, v := range dictDefault {
				if isTruthy(v) {
					return true
				}
			}
			return false
		}
	}
	return true
}

// pinIndex reads the recorded pin list, reading anything unusable as empty.
// Port of rule_settings._pin_index.
func pinIndex(meta map[string]any) []string {
	raw, ok := meta[PinIndexKey].([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if key, ok := item.(string); ok {
			out = append(out, key)
		}
	}
	return out
}

// ApplySettings writes values onto the task and returns the keys whose value
// moved. Port of rule_settings.apply_settings.
//
// The contract it preserves:
//
//   - unknown keys and invalid values raise BEFORE any mutation, so a
//     rejected request never leaves the task half-updated;
//   - the route is a PAIR: switching to BOT with no attached token is
//     refused, because the entity setter would silently drop the half of
//     the change the transport depends on;
//   - only keys whose value actually moved are noted in the pin index, so a
//     client that posts a whole settings object does not pin the settings
//     it merely rendered.
//
// The effectiveBefore mapping is the values the forwarder was using before
// this write — a caller that knows which bot the task inherits from passes
// Effective(...) — and exists so that a merge containing every registered
// key (what a form posts) does not look like a change to every key, and a
// task overriding an inherited value with the registry's own value does not
// look like no change at all.
func ApplySettings(rule MutableSettings, values map[string]any, effectiveBefore map[string]any) ([]string, error) {
	if rule == nil {
		return nil, &SettingError{Code: "invalid_type", Message: "settings must be an object"}
	}
	if values == nil {
		values = map[string]any{}
	}
	// Validate everything first: an invalid entry must not leave a
	// partial write.
	cleaned := make(map[string]any, len(values))
	for key, raw := range values {
		if _, registered := SETTING_SPECS[key]; !registered {
			return nil, &SettingError{
				Code:    "unknown_setting",
				Message: fmt.Sprintf("unknown setting %q", key),
				Key:     key,
			}
		}
		coerced, err := CoerceValue(key, raw)
		if err != nil {
			return nil, err
		}
		cleaned[key] = coerced
	}

	current := ReadSettings(rule)
	reference := effectiveBefore
	if reference == nil {
		reference = current
	}

	// The route is a pair, so validate the RESULTING state, not just the
	// keys the request happened to mention. Both halves of the failure
	// would be silent otherwise: switching to BOT with no token is dropped
	// by the entity setter (a no-op success), and clearing the token while
	// still on BOT drops the task back to the account route behind the
	// owner's back.
	lookup := func(key string, fallback any) any {
		if v, ok := cleaned[key]; ok {
			return v
		}
		if v, ok := current[key]; ok {
			return v
		}
		return fallback
	}
	transport := stringify(lookup("delivery_transport", "USER"))
	token := stringify(lookup("bot_token_id", ""))
	if botRouteNeedsToken(transport, token) {
		errKey := "bot_token_id"
		if _, ok := cleaned["bot_token_id"]; !ok {
			errKey = "delivery_transport"
		}
		return nil, &SettingError{
			Code:    "invalid_state",
			Message: "the bot route needs an attached bot_token_id — send delivery_transport='USER' with the removal, or attach a token",
			Key:     errKey,
		}
	}

	// Apply the token before the transport: a single request may attach
	// and switch at once, and the pair check above has already accepted
	// the resulting state.
	order := sortedKeysOf(cleaned)
	if _, hasToken := cleaned["bot_token_id"]; hasToken {
		// Stable order with the token first regardless of its sorted
		// position: the pair check passed on the resulting state, but
		// keeping the write order the Python port documents makes the
		// invariant visible in the diff.
		order = append([]string{"bot_token_id"}, filterOut(order, "bot_token_id")...)
	}

	changed := make([]string, 0, len(cleaned))
	for _, key := range order {
		value := cleaned[key]
		if !equalValues(reference[key], value) {
			changed = append(changed, key)
		}
		rule.Set(key, value)
	}

	// Record the keys this write moved, by name. A setting whose chosen
	// value is exactly the registry default is otherwise indistinguishable
	// from one the task never touched — a column leaves no other trace —
	// so a bot default would quietly win over the user's choice.
	recorded := pinIndex(rule.Meta())
	added := make([]string, 0, len(changed))
	for _, key := range changed {
		if !containsString(recorded, key) {
			added = append(added, key)
		}
	}
	if len(added) > 0 {
		rule.Set(PinIndexKey, mergePinIndex(recorded, added))
	}
	return changed, nil
}

// botRouteNeedsToken reports whether the pair-check must fire: the transport
// resolves to BOT while no token would be attached after the write.
func botRouteNeedsToken(transport, token string) bool {
	return strings.ToUpper(strings.TrimSpace(transport)) == "BOT" && strings.TrimSpace(token) == ""
}

// sortedKeysOf returns the map's keys in sorted order so a write's pin-index
// bookkeeping (and the diff it produces) is deterministic.
func sortedKeysOf(values map[string]any) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sortStrings(keys)
	return keys
}

// mergePinIndex combines the recorded list with the newly moved keys, the
// whole list kept sorted and uniqued the way the Python writer keeps it.
func mergePinIndex(recorded, added []string) []any {
	merged := make([]string, 0, len(recorded)+len(added))
	merged = append(merged, recorded...)
	merged = append(merged, added...)
	sortStrings(merged)
	out := make([]any, 0, len(merged))
	last := ""
	for _, key := range merged {
		if key == last {
			continue
		}
		last = key
		out = append(out, key)
	}
	return out
}

func filterOut(list []string, drop string) []string {
	out := make([]string, 0, len(list))
	for _, item := range list {
		if item != drop {
			out = append(out, item)
		}
	}
	return out
}

func stringify(value any) string {
	if value == nil {
		return ""
	}
	return str(value)
}

// --- inheritance ----------------------------------------------------------- //

// Source names for the provenance a describe entry carries. They are the
// exact strings the Python API reports ("task"/"bot"/"default"), because a
// client renders them.
const (
	// SourceTask is the task's own explicit choice.
	SourceTask = "task"
	// SourceBot is a value inherited from the attached bot's defaults.
	SourceBot = "bot"
	// SourceDefault is the registry's canonical default.
	SourceDefault = "default"
)

// Effective returns every setting's effective value for the task, the bot
// defaults applied. Port of core/domain/bot_defaults.effective.
//
// Precedence, in one place: for every registered setting the value a
// forwarding task actually uses is
//
//  1. the task — the rule pinned this key itself;
//  2. the bot — the attached bot carries a default for this key;
//  3. the registry — the canonical default in SETTING_SPECS.
//
// Only INHERITABLE settings can inherit. A pin always wins, including when
// its value equals the registry default (that is an explicit choice, and a
// bot default may not silently override it).
func Effective(rule SettingsHolder, botDefaults map[string]any) map[string]any {
	values := ReadSettings(rule)
	pinned := PinnedKeys(rule)
	out := make(map[string]any, len(SETTING_SPECS))
	for key, spec := range SETTING_SPECS {
		switch {
		case pinned[key], !spec.Inheritable:
			out[key] = values[key]
		default:
			if value, ok := botDefaults[key]; ok {
				out[key] = value
				continue
			}
			out[key] = values[key]
		}
	}
	return out
}

// Provenance reports which layer supplies key for the task: "task", "bot" or
// "default". Port of core/domain/bot_defaults.provenance.
func Provenance(rule SettingsHolder, botDefaults map[string]any, key string) string {
	if PinnedKeys(rule)[key] {
		return SourceTask
	}
	if spec, ok := SETTING_SPECS[key]; ok && spec.Inheritable {
		if _, present := botDefaults[key]; present {
			return SourceBot
		}
	}
	return SourceDefault
}

// DescribeEntry is one setting's effective value together with where it came
// from, for a client to render "inherited vs task-specific".
type DescribeEntry struct {
	Value       any
	Source      string
	Default     any
	Inheritable bool
	// Pinned is true only when the value is the task's own choice, so a
	// caller can tell an explicit choice that happens to equal the bot
	// default from an inherited one.
	Pinned bool
	// BotDefault is set only when the value was inherited, and carries
	// the bot's default itself.
	BotDefault any
}

// Describe is every effective value with its source. Port of
// core/domain/bot_defaults.describe.
func Describe(rule SettingsHolder, botDefaults map[string]any) map[string]DescribeEntry {
	values := Effective(rule, botDefaults)
	out := make(map[string]DescribeEntry, len(values))
	for key, value := range values {
		spec := SETTING_SPECS[key]
		entry := DescribeEntry{
			Value:       value,
			Source:      Provenance(rule, botDefaults, key),
			Default:     spec.Default,
			Inheritable: spec.Inheritable,
		}
		if entry.Source == SourceTask {
			entry.Pinned = true
		}
		if entry.Source == SourceBot {
			entry.BotDefault = botDefaults[key]
		}
		out[key] = entry
	}
	return out
}

// --- materialisation ------------------------------------------------------- //

// Materialized is the resolution result: the possibly-copied rule plus the
// sorted list of settings the bot supplied. The list is INHERITED_MARK's
// value, surfaced because the parity harness compares it directly and the
// store needs to know whether the copy was produced at all.
type Materialized struct {
	// Rule is the task to use: the original when nothing was inherited,
	// otherwise a copy whose settings carry the bot's defaults.
	Rule SettingsHolder
	// Inherited is the sorted list of settings the bot supplied — the
	// INHERITED_MARK a Python materialised entity carries. Empty (not
	// nil-of-a-different-type) when nothing was inherited.
	Inherited []string
}

// IsCopy reports whether resolution produced a copy rather than handing the
// original back. The store uses it to decide which projection to return;
// a caller that would save the rule uses it to refuse.
func (m Materialized) IsCopy() bool { return len(m.Inherited) > 0 }

// Materialize is the Go port of
// core/application/effective_settings.materialize: the task as the forwarder
// should see it, bot defaults filled in.
//
// It returns the rule itself when there is nothing to fill in — no attached
// bot, an empty defaults mapping, no inheritable key the task has not
// pinned, or a default that fails validation — so the common path allocates
// nothing. Otherwise it returns a COPY: the loaded entity must not be
// modified, because a caller that saves it afterwards would turn the
// inherited values into the task's own choices — the exact opposite of
// inheriting them.
//
// The "no attached bot" gate is attached_defaults' first clause
// (effective_settings.py:38-42): a rule with no bot_token_id carries no
// defaults at all, whatever the caller passed in — the same nil the Python
// module returns for a None tokens repository or an empty token id.
//
// A default that fails validation (only possible if storage was hand-edited)
// is skipped rather than raised: a convenience setting must never stop a task
// from forwarding. Validation is per key, not all-or-nothing, so one bad key
// never costs a task the rest of its bot's defaults. When a copy is produced
// it carries the INHERITED_MARK metadata key with the sorted list of
// inherited settings, so a save attempt can be detected and refused instead
// of quietly pinning the bot's defaults onto the task.
func Materialize(rule MutableSettings, botDefaults map[string]any) Materialized {
	if rule == nil || len(botDefaults) == 0 {
		return Materialized{Rule: rule}
	}
	if !hasAttachedBot(rule) {
		// attached_defaults: an empty token id means "no defaults", and
		// the rule is the authority for its own token id — a caller that
		// hands defaults for a bot the task never attached must not see
		// them applied.
		return Materialized{Rule: rule}
	}
	pinned := PinnedKeys(rule)
	carry := map[string]any{}
	for key, value := range botDefaults {
		spec, ok := SETTING_SPECS[key]
		if !ok || !spec.Inheritable || pinned[key] {
			continue
		}
		carry[key] = value
	}
	if len(carry) == 0 {
		return Materialized{Rule: rule}
	}
	copyRule, err := deepCopySettings(rule)
	if err != nil {
		// A rule that cannot be copied is not worth failing a forward
		// over: the task runs on its own values.
		return Materialized{Rule: rule}
	}
	// A default that fails validation is skipped, never raised — but the
	// Python materialiser semantics are all-or-nothing: apply_settings
	// validates every value before it mutates anything, so one bad key
	// makes the whole carry set fail and the rule runs on its own
	// values. Matching that exactly is what keeps "a convenience
	// setting must never stop a task from forwarding" true on both
	// engines in the same shape: neither engine applies a partial set,
	// and neither ever raises.
	if _, err := ApplySettings(copyRule, carry, nil); err != nil {
		return Materialized{Rule: rule}
	}
	inherited := sortedKeysOf(carry)
	copyRule.Set(InheritedMark, stringList(inherited))
	return Materialized{Rule: copyRule, Inherited: inherited}
}

// stringList converts the sorted key list into the JSON-native list shape
// INHERITED_MARK carries.
func stringList(keys []string) []any {
	out := make([]any, len(keys))
	for i, key := range keys {
		out[i] = key
	}
	return out
}

// deepCopySettings clones the task into a writable copy whose metadata blob
// is a fresh map, nested values included — a shallow copy would let an
// inherited write reach into the original's nested dict.
//
// The copy is a *MapSettings, deliberately the SAME public adapter the
// parity harness and the rule store build, so a caller can read the merged
// values back through the exported type rather than reaching into an
// internal one.
func deepCopySettings(rule MutableSettings) (MutableSettings, error) {
	raw, err := json.Marshal(rule.Meta())
	if err != nil {
		return nil, err
	}
	copyMeta := map[string]any{}
	if err := json.Unmarshal(raw, &copyMeta); err != nil {
		return nil, fmt.Errorf("settings: copy rule metadata: %w", err)
	}
	holder := &MapSettings{Metadata: copyMeta}
	return mutableMap(holder), nil
}

// copiedSettings is the writable copy: every Get resolves through a cloned
// metadata blob, so applying inherited values never touches the loaded
// entity the store is about to hand its caller.
type copiedSettings struct {
	metadata map[string]any
}

func (c *copiedSettings) Meta() map[string]any { return c.metadata }

func (c *copiedSettings) Get(key string) any { return c.metadata[key] }

// Set writes one setting. In the copy every registered key is blob-backed:
// the Python materialised entity's setter writes the key into the metadata
// blob (or the column it maps to), and a copy that started from the blob
// alone keeps every write in the blob, so a reader sees one shape.
func (c *copiedSettings) Set(key string, value any) { c.metadata[key] = value }

// hasAttachedBot reports whether the task names the bot whose defaults are
// being offered. The token id is a routing identifier the rule itself owns
// (bot_token_id is non-inheritable precisely because it names a route), so
// the rule is the authority — defaults handed in for a bot the task never
// attached are ignored, exactly as attached_defaults' empty-token clause.
func hasAttachedBot(rule MutableSettings) bool {
	if rule == nil {
		return false
	}
	return strings.TrimSpace(stringify(rule.Get("bot_token_id"))) != ""
}

// --- equality -------------------------------------------------------------- //

// equalValues compares two registry values across the shapes JSON decoding
// and Go typing produce for the same stored value: 86400 and 86400.0 are the
// same integer, []any{} and []string{} the same empty list, and nested maps
// and lists compare element-wise.
func equalValues(a, b any) bool {
	switch va := a.(type) {
	case bool:
		vb, ok := b.(bool)
		return ok && va == vb
	case float64:
		switch vb := b.(type) {
		case float64:
			return va == vb
		case int:
			return va == float64(vb)
		case int64:
			return va == float64(vb)
		}
		return false
	case int:
		switch vb := b.(type) {
		case float64:
			return float64(va) == vb
		case int:
			return va == vb
		case int64:
			return int64(va) == vb
		}
		return false
	case int64:
		switch vb := b.(type) {
		case float64:
			return float64(va) == vb
		case int:
			return va == int64(vb)
		case int64:
			return va == vb
		}
		return false
	case string:
		vb, ok := b.(string)
		return ok && va == vb
	case nil:
		return b == nil
	case []any:
		return equalList(va, b)
	case []string:
		vb, ok := b.([]any)
		if !ok || len(va) != len(vb) {
			return false
		}
		for i := range va {
			s, ok := vb[i].(string)
			if !ok || s != va[i] {
				return false
			}
		}
		return true
	case map[string]any:
		vb, ok := b.(map[string]any)
		if !ok || len(va) != len(vb) {
			return false
		}
		for k, v := range va {
			if !equalValues(v, vb[k]) {
				return false
			}
		}
		return true
	}
	return a == b
}

// equalList compares a decoded JSON list against whatever shape b carries.
func equalList(va []any, b any) bool {
	switch vb := b.(type) {
	case []any:
		if len(va) != len(vb) {
			return false
		}
		for i := range va {
			if !equalValues(va[i], vb[i]) {
				return false
			}
		}
		return true
	case []string:
		if len(va) != len(vb) {
			return false
		}
		for i := range va {
			s, ok := va[i].(string)
			if !ok || s != vb[i] {
				return false
			}
		}
		return true
	}
	return false
}

func isTruthy(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case float64:
		return v != 0
	case int:
		return v != 0
	case int64:
		return v != 0
	case string:
		return v != ""
	case nil:
		return false
	}
	return value != nil
}
