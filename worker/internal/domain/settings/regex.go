package settings

import (
	"fmt"
	"regexp"
	"strings"
)

// maxPatternLen is content_policy._MAX_PATTERN_LEN: a user pattern longer
// than this is refused before it is ever compiled, because the shapes that
// turn a crafted message into minutes of backtracking are long ones.
const maxPatternLen = 1000

// redosNested is content_policy._REDOS (compiled once at package init, the
// same way the Python module compiles it once at import): a quantifier
// nested inside a group that is itself quantified — the (a+)+ shape.
var redosNested = regexp.MustCompile(`\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})`)

// redosAlternation is content_policy._REDOS_ALT: ambiguous alternation under
// a quantifier — (a|a)*b — which is exponential without carrying a + or *
// inside the parens. The Python comment is explicit that covering both is
// what makes the guard defensible instead of shape-luck; the Go port keeps
// both arms for the same reason.
var redosAlternation = regexp.MustCompile(`\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})`)

// ValidateUserRegex is the Go port of
// core/domain/content_policy.compile_user_regex: compile a user-supplied
// pattern, or report why it is unusable. A pattern that is empty, too long,
// carries a catastrophic-backtracking shape, or is not valid regexp syntax
// is refused — it never drops a message, it is simply not consulted.
//
// The regex settings in the registry (regex_block, regex_require) run every
// stored item through this gate on write, so a pattern the Python registry
// refuses is refused here too: one engine accepting what the other rejects
// would store a task configuration that only one side can evaluate.
func ValidateUserRegex(pattern string) error {
	if strings.TrimSpace(pattern) == "" {
		return fmt.Errorf("empty pattern")
	}
	if len(pattern) > maxPatternLen {
		return fmt.Errorf("pattern exceeds %d characters", maxPatternLen)
	}
	if redosNested.MatchString(pattern) || redosAlternation.MatchString(pattern) {
		return fmt.Errorf("pattern carries a catastrophic backtracking shape")
	}
	if _, err := regexp.Compile(pattern); err != nil {
		return fmt.Errorf("invalid regular expression: %s", strings.TrimSpace(regexpReason(err)))
	}
	return nil
}

// regexpReason keeps the reason the same shape Python's re.error reports:
// the diagnosis, without the Go regexp package's surrounding "regexp:
// compiling `…`: " wrapper.
func regexpReason(err error) string {
	msg := err.Error()
	if idx := strings.LastIndex(msg, ": "); idx >= 0 {
		return msg[idx+2:]
	}
	return msg
}
