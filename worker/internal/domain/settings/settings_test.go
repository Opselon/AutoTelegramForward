package settings

import (
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// The settings package is a port of core/domain/rule_settings.py, and these
// tests pin the behaviours the port must not drift from:
//
//   - the registry's key set and defaults (a key missing on one engine is
//     the two engines disagreeing about the same task);
//   - coercion accepting and refusing exactly what the Python registry
//     accepts and refuses, for the same reasons;
//   - pinned_keys/read_settings deciding "the task chose this" the same way
//     both engines do, including the explicit-pin-index and the empty-dict
//     cases;
//   - effective/materialize resolution: task over bot over registry, never
//     mutating the loaded entity, and a validation failure skipping a key
//     instead of the delivery.

func TestRegistryCoversTheKeysPythonRegisters(t *testing.T) {
	// The key set is the contract: every Python registry entry must have
	// a Go twin with the same default and the same inheritable flag, or
	// the two engines answer different questions about the same task.
	for key, want := range pythonRegistryExpectations {
		got, ok := SETTING_SPECS[key]
		if !ok {
			t.Errorf("SETTING_SPECS is missing %q (Python registers it)", key)
			continue
		}
		if got.Inheritable != want.inheritable {
			t.Errorf("%q: inheritable = %v, want %v", key, got.Inheritable, want.inheritable)
		}
		if got.Kind != want.kind {
			t.Errorf("%q: kind = %q, want %q", key, got.Kind, want.kind)
		}
		if !equalValues(got.Default, want.def) {
			t.Errorf("%q: default = %v (%T), want %v (%T)", key, got.Default, got.Default, want.def, want.def)
		}
	}
	// And no extra keys: a Go-only key would be a setting the Python
	// control plane cannot write or read.
	for key := range SETTING_SPECS {
		if _, ok := pythonRegistryExpectations[key]; !ok {
			t.Errorf("SETTING_SPECS carries Go-only key %q", key)
		}
	}
}

type pySpec struct {
	kind        Kind
	def         any
	inheritable bool
}

// pythonRegistryExpectations is core/domain/rule_settings.SETTING_SPECS,
// transcribed. Keeping it literal (rather than decoding the Python module)
// is deliberate: a drift in EITHER direction is a test failure, and a test
// that imported the Python registry could only ever prove the Go side
// matched it, never that the two stay in lockstep.
var pythonRegistryExpectations = map[string]pySpec{
	// ---- content cleanup ------------------------------------------------
	"header":             {KindStr, "", true},
	"footer":             {KindStr, "", true},
	"replacements":       {KindDict, map[string]any{}, true},
	"link_replacement":   {KindStr, "", true},
	"clean_usernames":    {KindBool, false, true},
	"clean_hashtags":     {KindBool, false, true},
	"clean_phones":       {KindBool, false, true},
	"clean_buttons":      {KindBool, false, true},
	"clean_empty_lines":  {KindBool, false, true},
	"clean_custom_words": {KindList, []any{}, true},
	"remove_emojis":      {KindBool, false, true},
	"cleaner_enabled":    {KindBool, false, true},
	"cleaner_config":     {KindDict, map[string]any{}, true},

	// ---- media filtering ------------------------------------------------
	"block_text":          {KindBool, false, true},
	"block_voice":         {KindBool, false, true},
	"block_audio":         {KindBool, false, true},
	"block_video":         {KindBool, false, true},
	"block_stickers":      {KindBool, false, true},
	"block_links":         {KindBool, false, true},
	"allowed_media_types": {KindList, []any{}, true},
	"album_mode":          {KindStr, "album", true},
	"watermark":           {KindStr, "", true},
	"watermark_text":      {KindStr, "", true},

	// ---- sending behaviour ----------------------------------------------
	"duplicate_filters":       {KindBool, false, true},
	"max_time_edit":           {KindInt, float64(86400), true},
	"edit_time_limit_seconds": {KindInt, float64(0), true},
	"forward_delay_seconds":   {KindFloat, float64(0), true},
	"auto_delete_seconds":     {KindInt, float64(0), true},
	"forward_every_n":         {KindInt, float64(1), true},
	"debug_mode":              {KindBool, false, true},
	"sync_replies":            {KindBool, false, true},
	"process_raw_text":        {KindBool, false, true},
	"feature_group":           {KindStr, "all", true},
	"sender_type":             {KindStr, "Personal Telegram Account", true},

	// ---- keywords / replies ---------------------------------------------
	"keyword_confirmation": {KindBool, false, true},
	"keyword_reply":        {KindStr, "", true},
	"translate_lang":       {KindStr, "", true},

	// ---- audience filters -----------------------------------------------
	"blacklist":       {KindList, []any{}, true},
	"whitelist":       {KindList, []any{}, true},
	"blacklist_users": {KindList, []any{}, true},
	"whitelist_users": {KindList, []any{}, true},

	// ---- AI --------------------------------------------------------------
	"ai_creativity":    {KindFloat, float64(0.7), true},
	"ai_max_tokens":    {KindInt, float64(600), true},
	"ocr_image_ai":     {KindBool, false, true},
	"news_digest":      {KindBool, false, true},
	"smart_ai_rewrite": {KindBool, false, true},
	"crypto_mode":      {KindBool, false, true},
	"paid_addons":      {KindDict, map[string]any{}, true},

	// ---- bot route (non-inheritable: a bot default may never name one) --
	"delivery_transport": {KindStr, "USER", false},
	"bot_token_id":       {KindStr, "", false},

	// ---- automation ------------------------------------------------------
	"auto_post_scheduler": {KindDict, map[string]any{"enabled": false}, false},
	"auto_posting":        {KindBool, false, false},
	"power_control":       {KindBool, false, false},
	"power_schedule":      {KindDict, map[string]any{"enabled": false}, false},
	"button_to_text":      {KindBool, false, true},
	"link_buttons":        {KindList, []any{}, false},
	"affiliate_kit":       {KindDict, map[string]any{}, false},

	// ---- per-task content rules ------------------------------------------
	"content_policy_enabled": {KindBool, false, true},
	"blacklist_off":          {KindBool, false, true},
	"whitelist_off":          {KindBool, false, true},
	"blacklist_words":        {KindList, []any{}, true},
	"whitelist_words":        {KindList, []any{}, true},
	"whitelist_mode":         {KindStr, "any", true},
	"remove_links":           {KindBool, false, true},
	"strip_words":            {KindList, []any{}, true},
	"regex_block":            {KindList, []any{}, true},
	"regex_require":          {KindList, []any{}, true},
	"source_policies":        {KindDict, map[string]any{}, false},
	"target_policies":        {KindDict, map[string]any{}, false},

	// ---- branding triggers ------------------------------------------------
	"branding_triggers_enabled": {KindBool, false, true},
	"branding_origin_titles":    {KindList, []any{}, true},
	"branding_origin_ids":       {KindList, []any{}, true},
	"branding_keywords":         {KindList, []any{}, true},
	"branding_match_mode":       {KindStr, "any", true},
	"branding_require_forward":  {KindBool, false, true},
	"branding_force_forward":    {KindBool, true, true},
}

func TestRegistryChoicesAndBoundsMatchThePythonRegistry(t *testing.T) {
	for key, want := range map[string][]string{
		"delivery_transport":  {"USER", "BOT"},
		"whitelist_mode":      {"any", "all", "phrase"},
		"branding_match_mode": {"any", "all"},
	} {
		got := SETTING_SPECS[key].Choices
		sort.Strings(got)
		sort.Strings(want)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%q choices = %v, want %v", key, got, want)
		}
	}
	for key, want := range map[string][2]float64{
		"max_time_edit":           {0, 2592000},
		"edit_time_limit_seconds": {0, 2592000},
		"forward_delay_seconds":   {0, 86400},
		"auto_delete_seconds":     {0, 604800},
		"forward_every_n":         {1, 1000},
		"ai_creativity":           {0, 2},
		"ai_max_tokens":           {1, 100000},
	} {
		spec := SETTING_SPECS[key]
		if spec.Minimum == nil || spec.Maximum == nil {
			t.Errorf("%q has no bounds, want %v", key, want)
			continue
		}
		if *spec.Minimum != want[0] || *spec.Maximum != want[1] {
			t.Errorf("%q bounds = [%v, %v], want %v", key, *spec.Minimum, *spec.Maximum, want)
		}
	}
}

// --- coercion -------------------------------------------------------------- //

func TestCoerceValueAcceptsWhatThePythonRegistryAccepts(t *testing.T) {
	cases := []struct {
		name  string
		key   string
		value any
		want  any
	}{
		// bool before int: a JSON true is not "an int that is 1".
		{"bool true", "remove_emojis", true, true},
		{"bool 0/1 int", "remove_emojis", float64(1), true},
		{"bool zero", "remove_emojis", float64(0), false},
		// int slots take an integer, refuse a fraction.
		{"int from json float", "auto_delete_seconds", float64(42), int64(42)},
		{"int from go int", "auto_delete_seconds", 42, int64(42)},
		// float slots take an int (Python's float(3) == 3.0).
		{"float from int", "ai_creativity", 1, float64(1)},
		{"float from float", "ai_creativity", 0.5, float64(0.5)},
		// strings are trimmed; the empty string passes a choices check.
		{"str trimmed", "header", "  hi  ", "hi"},
		{"str empty passes choices", "album_mode", "", ""},
		{"str choice", "album_mode", "first", "first"},
		// lists: items trimmed, blanks dropped, non-strings refused.
		{"list of strings", "blacklist", []any{"  a  ", "b", "  "}, []any{"a", "b"}},
		{"list from go strings", "blacklist", []string{"a", "b"}, []any{"a", "b"}},
		// dicts: keys stringified, values carried.
		{"dict", "replacements", map[string]any{"a": "b"}, map[string]any{"a": "b"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := CoerceValue(tc.key, tc.value)
			if err != nil {
				t.Fatalf("CoerceValue(%q, %v): %v", tc.key, tc.value, err)
			}
			if !equalValues(got, tc.want) {
				t.Errorf("CoerceValue(%q, %v) = %v (%T), want %v (%T)", tc.key, tc.value, got, got, tc.want, tc.want)
			}
		})
	}
}

func TestCoerceValueRefusesWhatThePythonRegistryRefuses(t *testing.T) {
	cases := []struct {
		name  string
		key   string
		value any
		code  string
	}{
		{"unknown key", "not_a_setting", true, "unknown_setting"},
		{"null refused", "remove_emojis", nil, "invalid_type"},
		{"bool refuses a 2", "remove_emojis", float64(2), "invalid_type"},
		{"bool refuses a string", "remove_emojis", "true", "invalid_type"},
		{"int refuses a fraction", "auto_delete_seconds", 1.5, "invalid_type"},
		{"int refuses a string", "auto_delete_seconds", "12", "invalid_type"},
		{"int refuses a bool", "auto_delete_seconds", true, "invalid_type"},
		{"float refuses a string", "ai_creativity", "0.7", "invalid_type"},
		{"str refuses a number", "header", 42, "invalid_type"},
		{"list refuses a string", "blacklist", "a", "invalid_type"},
		{"str list refuses a number item", "blacklist", []any{1}, "invalid_type"},
		{"dict refuses a list", "replacements", []any{"a"}, "invalid_type"},
		{"out of range low", "auto_delete_seconds", float64(-1), "out_of_range"},
		{"out of range high", "auto_delete_seconds", float64(604801), "out_of_range"},
		{"fen below 1", "forward_every_n", float64(0), "out_of_range"},
		{"choices refused", "album_mode", "gallery", "invalid_choice"},
		{"whitelist_mode choice refused", "whitelist_mode", "PHRASE-ish", "invalid_choice"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := CoerceValue(tc.key, tc.value)
			if err == nil {
				t.Fatalf("CoerceValue(%q, %v) succeeded; want %s", tc.key, tc.value, tc.code)
			}
			se, ok := err.(*SettingError)
			if !ok {
				t.Fatalf("error is %T, want *SettingError", err)
			}
			if se.Code != tc.code {
				t.Errorf("code = %q, want %q (message: %s)", se.Code, tc.code, se.Message)
			}
			if se.Key != tc.key {
				t.Errorf("key = %q, want %q", se.Key, tc.key)
			}
		})
	}
}

func TestCoerceValueMessageMatchesThePythonWording(t *testing.T) {
	// The parity harness compares refusal messages across the two engines;
	// pin the exact shapes the Python f-strings produce.
	cases := []struct {
		key     string
		value   any
		message string
	}{
		{"not_a_setting", true, `unknown setting "not_a_setting"`},
		{"remove_emojis", nil, "remove_emojis may not be null"},
		{"remove_emojis", "x", "remove_emojis expects a boolean"},
		{"auto_delete_seconds", "12", "auto_delete_seconds expects an integer"},
		{"ai_creativity", "x", "ai_creativity expects a number"},
		{"header", 42, "header expects a string"},
		{"blacklist", "a", "blacklist expects a list"},
		{"blacklist", []any{1}, "blacklist expects a list of strings"},
		{"replacements", []any{}, "replacements expects an object"},
		{"auto_delete_seconds", float64(-1), "auto_delete_seconds must be >= 0"},
		{"auto_delete_seconds", float64(604801), "auto_delete_seconds must be <= 604800"},
		{"album_mode", "gallery", "album_mode must be one of album, first, split"},
	}
	for _, tc := range cases {
		t.Run(tc.key, func(t *testing.T) {
			_, err := CoerceValue(tc.key, tc.value)
			if err == nil {
				t.Fatalf("CoerceValue(%q, %v) succeeded; want refusal %q", tc.key, tc.value, tc.message)
			}
			if err.Error() != tc.message {
				t.Errorf("message = %q, want %q", err.Error(), tc.message)
			}
		})
	}
}

func TestRegexSettingsRefuseUnsafeUserPatterns(t *testing.T) {
	// The two keys whose stored items are compiled as patterns run every
	// item through the same compile gate the Python registry applies
	// (compile_user_regex): an unusable pattern is a write failure, not a
	// silently-never-evaluated entry.
	unsafe := []string{
		"(a+)+",     // nested quantifier: the _REDOS shape
		"(a|a)+",    // ambiguous alternation: _REDOS_ALT
		"(.+)*b",    // the classic blow-up
		"(unclosed", // invalid syntax
		"",          // empty
	}
	for _, pattern := range unsafe {
		if err := ValidateUserRegex(pattern); err == nil {
			t.Errorf("ValidateUserRegex(%q) succeeded; want refused", pattern)
		}
	}
	for _, valid := range []string{"sale", "\\bsale\\b", "(sale|discount)", "^#\\w+$"} {
		if err := ValidateUserRegex(valid); err != nil {
			t.Errorf("ValidateUserRegex(%q) = %v; want accepted", valid, err)
		}
	}
	// The registry gate itself: coercion refuses the item.
	if _, err := CoerceValue("regex_block", []any{"(a+)+"}); err == nil {
		t.Error("CoerceValue accepted a nested-quantifier regex_block item")
	}
	if _, err := CoerceValue("regex_require", []any{"sale"}); err != nil {
		t.Errorf("CoerceValue refused a valid regex_require item: %v", err)
	}
	// A pattern over the length cap is refused before it is compiled.
	long := strings.Repeat("a", maxPatternLen+1)
	if err := ValidateUserRegex(long); err == nil {
		t.Error("ValidateUserRegex accepted an over-length pattern")
	}
}

// --- read / pin ----------------------------------------------------------- //

func TestReadSettingsFallsBackToTheRegistryDefault(t *testing.T) {
	rule := MapSettings{Metadata: map[string]any{"remove_emojis": true}}
	values := ReadSettings(rule)
	if !values["remove_emojis"].(bool) {
		t.Error("an explicitly set value was replaced by the default")
	}
	if values["header"] != "" {
		t.Errorf("an unset key read back as %v, want the registry default", values["header"])
	}
	if values["auto_delete_seconds"] != float64(0) {
		t.Errorf("auto_delete_seconds default = %v, want 0", values["auto_delete_seconds"])
	}
	if len(values) != len(SETTING_SPECS) {
		t.Errorf("ReadSettings returned %d keys, want every one of the %d registered", len(values), len(SETTING_SPECS))
	}
}

func TestPinnedKeysSeesAValueThatDiffersFromTheDefault(t *testing.T) {
	// The single test that covers both storage shapes: a column-backed
	// setting leaves no other trace of the choice, so "differs from the
	// registry default" is what makes it the task's own.
	rule := MapSettings{Metadata: map[string]any{"remove_emojis": true}}
	if !PinnedKeys(rule)["remove_emojis"] {
		t.Error("remove_emojis=true on a task whose default is false must be pinned")
	}
	if PinnedKeys(MapSettings{})["remove_emojis"] {
		t.Error("a task with no settings pinned everything")
	}
}

func TestPinnedKeysSeesThePinIndexEntries(t *testing.T) {
	// The gap the value-difference test leaves: a task that CHOSE the
	// registry's own value (turning a bot's link-cleaning back off) has a
	// column that reads back exactly the default, so the choice is
	// recorded by name in the pin index — and read back here.
	meta := map[string]any{
		"remove_emojis": false, // exactly the registry default
		PinIndexKey:     []any{"remove_emojis"},
	}
	if !PinnedKeys(MapSettings{Metadata: meta})["remove_emojis"] {
		t.Error("a pin-index entry must pin the key even when its value equals the default")
	}
}

func TestPinnedKeysEmptyDictMeansOff(t *testing.T) {
	// An unset mapping column reads back as {} where the registry spells
	// out the full shape ({"enabled": false}); both mean "off", so the
	// empty dict is NOT a choice when the default asserts nothing true.
	if pinned := PinnedKeys(MapSettings{Metadata: map[string]any{"power_schedule": map[string]any{}}})["power_schedule"]; pinned {
		t.Error("an empty power_schedule dict was read as a choice; it means off")
	}
	// The registry default {"enabled": false} also asserts nothing true,
	// so a task whose auto_post_scheduler equals the default shape is
	// not pinned either — it inherits.
	if pinned := PinnedKeys(MapSettings{})["auto_post_scheduler"]; pinned {
		t.Error("the default auto_post_scheduler shape was read as a choice")
	}
}

// --- apply ----------------------------------------------------------------- //

func TestApplySettingsOnlyNotesTheKeysThatMoved(t *testing.T) {
	rule := &MapSettings{Metadata: map[string]any{}}
	changed, err := ApplySettings(rule, map[string]any{
		"remove_emojis": true,
		"header":        "", // equals the default AND the current value: no move
	}, nil)
	if err != nil {
		t.Fatalf("ApplySettings: %v", err)
	}
	if len(changed) != 1 || changed[0] != "remove_emojis" {
		t.Errorf("changed = %v, want [remove_emojis]", changed)
	}
	recorded, ok := rule.Metadata[PinIndexKey].([]any)
	if !ok {
		t.Fatalf("pin index not written: %v", rule.Metadata[PinIndexKey])
	}
	if len(recorded) != 1 || recorded[0] != "remove_emojis" {
		t.Errorf("pin index = %v, want [remove_emojis]", recorded)
	}
	// A second apply that posts the same settings moves nothing and adds
	// nothing to the index — a client that re-posts a rendered form does
	// not pin everything it rendered.
	changed, err = ApplySettings(rule, map[string]any{"remove_emojis": true, "header": ""}, nil)
	if err != nil {
		t.Fatalf("ApplySettings (second): %v", err)
	}
	if len(changed) != 0 {
		t.Errorf("second apply moved %v; want nothing", changed)
	}
}

func TestApplySettingsValidatesBeforeAnyMutation(t *testing.T) {
	rule := &MapSettings{Metadata: map[string]any{}}
	_, err := ApplySettings(rule, map[string]any{
		"remove_emojis": true,
		"not_a_setting": true,
	}, nil)
	if err == nil {
		t.Fatal("an unknown key was accepted")
	}
	if _, present := rule.Metadata["remove_emojis"]; present {
		t.Error("a rejected request left a partial write behind")
	}
	// An invalid value refuses the same way.
	_, err = ApplySettings(rule, map[string]any{
		"remove_emojis":       true,
		"auto_delete_seconds": float64(-1),
	}, nil)
	if err == nil {
		t.Fatal("an out-of-range value was accepted")
	}
	if _, present := rule.Metadata["remove_emojis"]; present {
		t.Error("an out-of-range request left a partial write behind")
	}
}

func TestApplySettingsRefusesTheBotRouteWithoutAToken(t *testing.T) {
	// The route is a pair: both halves of the failure would be silent
	// otherwise. The error key follows which half the request named.
	rule := &MapSettings{Metadata: map[string]any{}}
	_, err := ApplySettings(rule, map[string]any{"delivery_transport": "BOT"}, nil)
	if err == nil {
		t.Fatal("delivery_transport=BOT with no token was accepted")
	}
	if e := err.(*SettingError); e.Code != "invalid_state" {
		t.Errorf("code = %q, want invalid_state", e.Code)
	}

	// Attaching and switching in ONE request is fine.
	rule = &MapSettings{Metadata: map[string]any{}}
	if _, err := ApplySettings(rule, map[string]any{
		"delivery_transport": "BOT",
		"bot_token_id":       "tok-1",
	}, nil); err != nil {
		t.Fatalf("attach+switch in one request failed: %v", err)
	}

	// Clearing the token while still on BOT is the other silent failure.
	rule = &MapSettings{Metadata: map[string]any{
		"delivery_transport": "BOT",
		"bot_token_id":       "tok-1",
	}}
	_, err = ApplySettings(rule, map[string]any{"bot_token_id": ""}, nil)
	if err == nil {
		t.Fatal("clearing the token while on BOT was accepted")
	}
	if e := err.(*SettingError); e.Key != "bot_token_id" {
		t.Errorf("error key = %q, want bot_token_id", e.Key)
	}
}

func TestApplySettingsEffectiveBeforeKeepsAnOverrideVisible(t *testing.T) {
	// A task overriding an inherited value with the registry's own value
	// (turning a bot's "clean links on" back off) must look like a
	// CHANGE, not a no-op: without effective_before the override would be
	// dropped and the bot default would keep winning.
	rule := &MapSettings{Metadata: map[string]any{}}
	changed, err := ApplySettings(rule, map[string]any{"remove_emojis": false},
		map[string]any{"remove_emojis": true})
	if err != nil {
		t.Fatalf("ApplySettings: %v", err)
	}
	if len(changed) != 1 || changed[0] != "remove_emojis" {
		t.Errorf("changed = %v, want the override recorded as a move", changed)
	}
}

// --- inheritance ----------------------------------------------------------- //

func TestEffectivePrecedenceIsTaskOverBotOverRegistry(t *testing.T) {
	bot := map[string]any{
		"remove_emojis":       true,  // not pinned: inherits
		"header":              "BOT", // not pinned: inherits
		"auto_delete_seconds": float64(42),
	}
	rule := MapSettings{Metadata: map[string]any{
		"remove_emojis": false, // equals the registry default AND is NOT
		// recorded in the pin index: NOT a choice, so the bot's true
		// wins — the exact gap the pin index exists to close. (Python
		// agrees; see effective() with the same inputs.)
		"block_links": true, // differs from the default: chosen → pinned
	}}
	values := Effective(rule, bot)
	if !values["remove_emojis"].(bool) {
		t.Error("an unpinned remove_emojis=false must inherit the bot's true; only the pin index makes the choice stick")
	}
	if got := values["header"].(string); got != "BOT" {
		t.Errorf("header = %q, want the inherited BOT", got)
	}
	if got := values["auto_delete_seconds"]; got != float64(42) && got != int64(42) {
		t.Errorf("auto_delete_seconds = %v, want the inherited 42", got)
	}
	// A setting the task never touched and the bot does not carry keeps
	// the registry default.
	if values["footer"] != "" {
		t.Errorf("footer = %v, want the registry default", values["footer"])
	}
	if provenance := Provenance(rule, bot, "remove_emojis"); provenance != SourceBot {
		t.Errorf("remove_emojis provenance = %q, want bot", provenance)
	}
	if provenance := Provenance(rule, bot, "block_links"); provenance != SourceTask {
		t.Errorf("block_links provenance = %q, want task (pinned)", provenance)
	}
	if provenance := Provenance(rule, bot, "header"); provenance != SourceBot {
		t.Errorf("header provenance = %q, want bot", provenance)
	}
	if provenance := Provenance(rule, bot, "footer"); provenance != SourceDefault {
		t.Errorf("footer provenance = %q, want default", provenance)
	}
}

func TestEffectivePinsAnExplicitChoiceRecordedByIndex(t *testing.T) {
	// The pin index is what keeps a task's explicit choice standing
	// against a bot default when the chosen value happens to equal the
	// registry default — the "turn the bot's cleaning back off" case.
	// Without it the value-difference test sees no choice at all and the
	// bot default silently wins.
	rule := MapSettings{Metadata: map[string]any{
		"remove_emojis": false,
		PinIndexKey:     []any{"remove_emojis"},
	}}
	values := Effective(rule, map[string]any{"remove_emojis": true})
	if values["remove_emojis"].(bool) {
		t.Error("a pin-indexed remove_emojis=false lost to the bot's true")
	}
	if Provenance(rule, map[string]any{"remove_emojis": true}, "remove_emojis") != SourceTask {
		t.Error("a pin-indexed choice must report provenance task")
	}
}

func TestEffectiveNeverAppliesNonInheritableSettings(t *testing.T) {
	// A bot default may never name a chat, a route or a schedule:
	// delivery_transport/bot_token_id/source_policies & friends are marked
	// inheritable=False in both registries, so effective() must never
	// hand one to a task even when the bot carries it.
	bot := map[string]any{
		"delivery_transport":  "BOT",
		"bot_token_id":        "tok-theirs",
		"source_policies":     map[string]any{"-1001": map[string]any{}},
		"target_policies":     map[string]any{"-2001": map[string]any{}},
		"link_buttons":        []any{"x"},
		"affiliate_kit":       map[string]any{"enabled": true},
		"auto_post_scheduler": map[string]any{"enabled": true},
		"power_schedule":      map[string]any{"enabled": true},
		"auto_posting":        true,
		"power_control":       true,
	}
	rule := MapSettings{}
	values := Effective(rule, bot)
	for key, want := range map[string]any{
		"delivery_transport":  "USER",
		"bot_token_id":        "",
		"source_policies":     map[string]any{},
		"target_policies":     map[string]any{},
		"link_buttons":        []any{},
		"affiliate_kit":       map[string]any{},
		"auto_post_scheduler": map[string]any{"enabled": false},
		"power_schedule":      map[string]any{"enabled": false},
		"auto_posting":        false,
		"power_control":       false,
	} {
		if !equalValues(values[key], want) {
			t.Errorf("%s = %v, want the registry default %v (never inherited)", key, values[key], want)
		}
	}
}

func TestDescribeReportsProvenancePerKey(t *testing.T) {
	bot := map[string]any{"remove_emojis": true}
	rule := MapSettings{Metadata: map[string]any{"header": "MINE"}}
	desc := Describe(rule, bot)
	if entry := desc["remove_emojis"]; !entry.Value.(bool) || entry.Source != SourceBot || entry.BotDefault != true {
		t.Errorf("remove_emojis entry = %+v, want inherited true", entry)
	}
	if entry := desc["header"]; entry.Source != SourceTask || !entry.Pinned {
		t.Errorf("header entry = %+v, want a pinned task choice", entry)
	}
	if entry := desc["footer"]; entry.Source != SourceDefault || entry.Pinned {
		t.Errorf("footer entry = %+v, want the registry default", entry)
	}
}

// --- materialize ----------------------------------------------------------- //

func TestMaterializeReturnsTheRuleWhenThereIsNothingToInherit(t *testing.T) {
	// No bot defaults at all.
	rule := &MapSettings{Metadata: map[string]any{}}
	if got := Materialize(rule, nil); got.IsCopy() {
		t.Error("a nil bot-defaults mapping produced a copy")
	}
	// A bot with no defaults.
	if got := Materialize(rule, map[string]any{}); got.IsCopy() {
		t.Error("an empty bot-defaults mapping produced a copy")
	}
	// Every candidate key pinned: nothing to fill in.
	pinned := &MapSettings{Metadata: map[string]any{"remove_emojis": true}}
	if got := Materialize(pinned, map[string]any{"remove_emojis": true}); got.IsCopy() {
		t.Error("a fully-pinned task produced a copy")
	}
	// Only non-inheritable keys on the bot: resolution must refuse them.
	if got := Materialize(rule, map[string]any{"delivery_transport": "BOT"}); got.IsCopy() {
		t.Error("a non-inheritable bot default produced a copy")
	}
}

func TestMaterializeFillsInheritableValuesAndMarksThem(t *testing.T) {
	rule := &MapSettings{Metadata: map[string]any{
		"header":       "MINE", // chosen: stays
		"bot_token_id": "tok-1",
	}}
	resolved := Materialize(rule, map[string]any{
		"remove_emojis":       true,
		"auto_delete_seconds": float64(42),
	})
	if !resolved.IsCopy() {
		t.Fatal("a rule with inheritable defaults returned the original; want a copy")
	}
	merged, ok := resolved.Rule.(*MapSettings)
	if !ok {
		t.Fatalf("resolved rule is %T, want *MapSettings", resolved.Rule)
	}
	if !merged.Metadata["remove_emojis"].(bool) {
		t.Error("the inherited remove_emojis did not land in the copy")
	}
	if merged.Metadata["auto_delete_seconds"] != float64(42) && merged.Metadata["auto_delete_seconds"] != int64(42) {
		t.Errorf("inherited auto_delete_seconds = %v, want 42", merged.Metadata["auto_delete_seconds"])
	}
	if got := merged.Metadata["header"]; got != "MINE" {
		t.Errorf("the task's own header was clobbered: %v", got)
	}
	// The mark: a save attempt can be detected rather than quietly
	// writing the bot's defaults into the task.
	mark, ok := merged.Metadata[InheritedMark].([]any)
	if !ok {
		t.Fatalf("inherited mark missing: %v", merged.Metadata[InheritedMark])
	}
	if !equalValues(mark, []any{"auto_delete_seconds", "remove_emojis"}) {
		t.Errorf("inherited mark = %v, want the sorted key list", mark)
	}
}

func TestMaterializeNeverMutatesTheLoadedEntity(t *testing.T) {
	// The copy-not-mutate rule, stated as a test: a caller that saves
	// the resolved copy would turn the bot's defaults into the task's
	// own pins — the exact opposite of inheriting them.
	stored := map[string]any{"header": "MINE", "bot_token_id": "tok-1"}
	rule := &MapSettings{Metadata: stored}
	resolved := Materialize(rule, map[string]any{"remove_emojis": true})
	if !resolved.IsCopy() {
		t.Fatal("expected a copy")
	}
	if _, present := stored["remove_emojis"]; present {
		t.Error("materialize wrote an inherited value into the ORIGINAL metadata map")
	}
	if rule.Metadata["remove_emojis"] != nil {
		t.Error("materialize wrote an inherited value through the rule's own map")
	}
	// The nested case: a bot default that lands in a nested blob must
	// not reach the original's nested map either.
	nested := map[string]any{
		"content_policy": map[string]any{"whitelist_words": []any{"a"}},
		"bot_token_id":   "tok-1",
	}
	nestedRule := &MapSettings{Metadata: nested}
	_ = Materialize(nestedRule, map[string]any{"remove_emojis": true})
	policy, ok := nested["content_policy"].(map[string]any)
	if !ok {
		t.Fatalf("the original nested blob was replaced: %v", nested["content_policy"])
	}
	if _, present := policy["blacklist_words"]; present {
		t.Error("a nested write reached the original blob's inner map")
	}
}

func TestMaterializeSkipsADefaultThatFailsValidation(t *testing.T) {
	// Only possible if storage was hand-edited, and a convenience
	// setting must never stop a task from forwarding: nothing is raised.
	// The Python materialiser is all-or-nothing — apply_settings
	// validates every value BEFORE it mutates anything, so one bad key
	// fails the whole carry set and the task runs on its own values.
	// (Verified against core/application/effective_settings.py: a
	// SettingError makes materialize return the rule unchanged.)
	definitions := map[string]any{
		"remove_emojis":       true,
		"auto_delete_seconds": float64(999999999), // out of range
	}
	rule := &MapSettings{Metadata: map[string]any{"bot_token_id": "tok-1"}}
	resolved := Materialize(rule, definitions)
	if resolved.IsCopy() {
		t.Fatal("a carry set containing an invalid default produced a copy; the whole set must fail")
	}
	// One valid key alone still lands, proving the refusal is the
	// invalid value's fault and not the mechanism's.
	resolved = Materialize(rule, map[string]any{"remove_emojis": true})
	if !resolved.IsCopy() {
		t.Fatal("the lone valid default did not produce a copy")
	}
	if !resolved.Rule.(*MapSettings).Metadata["remove_emojis"].(bool) {
		t.Error("the valid default did not arrive")
	}
}

func TestMaterializeIgnoresDefaultsForABotTheTaskNeverAttached(t *testing.T) {
	// attached_defaults' first clause: a rule with no bot_token_id carries
	// no defaults at all, whatever the caller passed in. Ignoring it keeps
	// the two engines from diverging when a caller hands defaults for a
	// bot the task does not reference.
	rule := &MapSettings{Metadata: map[string]any{"header": "MINE"}}
	if got := Materialize(rule, map[string]any{"remove_emojis": true}); got.IsCopy() {
		t.Error("defaults for an unattached bot produced a copy")
	}
	// The same rule WITH the bot attached inherits.
	rule = &MapSettings{Metadata: map[string]any{"header": "MINE", "bot_token_id": "tok-1"}}
	if got := Materialize(rule, map[string]any{"remove_emojis": true}); !got.IsCopy() {
		t.Error("defaults for the attached bot did not produce a copy")
	}
}

func TestMaterializePreservesTheTaskPinIndex(t *testing.T) {
	// The copy starts from the task's own recorded pins, so a key the
	// task pinned is not re-pinned (and the pin index itself is not
	// clobbered by the apply that carries the inherited values in).
	rule := &MapSettings{Metadata: map[string]any{
		PinIndexKey:    []any{"header"},
		"header":       "MINE",
		"bot_token_id": "tok-1",
	}}
	resolved := Materialize(rule, map[string]any{"remove_emojis": true})
	merged := resolved.Rule.(*MapSettings)
	recorded, ok := merged.Metadata[PinIndexKey].([]any)
	if !ok {
		t.Fatalf("pin index missing from the copy: %v", merged.Metadata[PinIndexKey])
	}
	// "header" was recorded by the task; the apply that carried
	// remove_emojis in moved THAT key, so the index gains it: the pin
	// index is the record of "which keys moved", and an inherited value
	// that landed through apply_settings is recorded by name — the same
	// bookkeeping the Python apply performs. The task's own "header"
	// entry survives alongside it.
	if !equalValues(recorded, []any{"header", "remove_emojis"}) {
		t.Errorf("pin index = %v, want [header, remove_emojis]", recorded)
	}
}

func TestRoundTripJSONKeepsValuesComparable(t *testing.T) {
	// A value coerced by this package must survive a JSON round trip
	// and still compare equal to its twin, because the parity harness
	// compares Go values against Python values decoded from the same
	// JSON the Python registry would store.
	for key, value := range map[string]any{
		"remove_emojis":       true,
		"auto_delete_seconds": float64(42),
		"forward_every_n":     float64(5),
		"ai_creativity":       float64(0.7),
		"header":              "hi",
		"blacklist":           []any{"a", "b"},
		"replacements":        map[string]any{"a": "b"},
	} {
		coerced, err := CoerceValue(key, value)
		if err != nil {
			t.Fatalf("CoerceValue(%q): %v", key, err)
		}
		raw, err := json.Marshal(coerced)
		if err != nil {
			t.Fatalf("marshal %q: %v", key, err)
		}
		var back any
		if err := json.Unmarshal(raw, &back); err != nil {
			t.Fatalf("unmarshal %q: %v", key, err)
		}
		if !equalValues(coerced, back) {
			t.Errorf("%q: %v (%T) != round-tripped %v (%T)", key, coerced, coerced, back, back)
		}
	}
}
