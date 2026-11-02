package policy

import (
	"fmt"
	"strings"
	"testing"
)

// Representative message shapes the filter/policy hot path actually sees.
// Persian carries the letter variants the fold must unify; the large body
// stands in for a long post.

var benchTexts = []struct {
	name string
	text string
}{
	{"SmallASCII", "sign up now for the daily digest"},
	{"SmallPersian", "ثبت‌نام برای خبرنامه روزانه ادامه دارد"},
	{"Mixed", fmt.Sprintf("%s %s", "limited offer", "تخفیف ویژه فقط امروز")},
	{"LargePersian", strings.Repeat("این یک پیام آزمایشی طولانی است. ", 120)},
	{"LargeASCII", strings.Repeat("this is a long test message body. ", 120)},
	{"EmojiHeavy", strings.Repeat("hello 🌟 world 👨‍👩‍👧 ", 60)},
}

// 50 keywords is a realistic blacklist; 500 is the far tail.

func benchKeywords(n int, script string) []string {
	base := []string{"spam", "scam", "buy now", "limited", "click", "offer", "free", "urgent", "act now", "deal"}
	if script == "fa" {
		base = []string{"اسپم", "کلاهبرداری", "خرید", "فروش", "تخفیف", "ویژه", "رایگان", "فوری", "همین حالا", "معامله"}
	}
	out := make([]string, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, fmt.Sprintf("%s%d", base[i%len(base)], i/len(base)))
	}
	return out
}

// BenchmarkEvaluatePolicy measures the full policy decision on one message
// against a realistic blacklist, the configuration most installs run.
func BenchmarkEvaluatePolicy(b *testing.B) {
	kw := benchKeywords(50, "en")
	for _, bt := range benchTexts {
		b.Run(bt.name, func(b *testing.B) {
			p := &ContentPolicy{
				BlacklistWords: kw,
				WhitelistWords: []string{"news", "update"},
				WhitelistMode:  "any",
				Normalize:      true,
				WordBoundary:   true,
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				_ = p.Evaluate(bt.text, "text")
			}
		})
	}
}

// BenchmarkEvaluatePersianKeywords isolates the cost of folding a Persian
// keyword list against a Persian body — the case where the variant classes
// and invisible runs do real work.
func BenchmarkEvaluatePersianKeywords(b *testing.B) {
	kw := benchKeywords(50, "fa")
	body := strings.Repeat("این یک پیام آزمایشی طولانی است. ", 60)
	p := &ContentPolicy{
		BlacklistWords: kw,
		Normalize:      true,
		WordBoundary:   true,
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = p.Evaluate(body, "text")
	}
}

// BenchmarkLargeKeywordList measures the 500-word tail: the pre-filter
// (needle-in-folded-body) is what keeps this flat.
func BenchmarkLargeKeywordList(b *testing.B) {
	kw := benchKeywords(500, "en")
	body := strings.Repeat("this is a long test message body. ", 60)
	p := &ContentPolicy{
		BlacklistWords: kw,
		Normalize:      true,
		WordBoundary:   true,
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = p.Evaluate(body, "text")
	}
}

// BenchmarkChain measures the additive chain: a task policy plus a source
// and a target override, one message.
func BenchmarkChain(b *testing.B) {
	task := &ContentPolicy{BlacklistWords: benchKeywords(20, "en"), Normalize: true, WordBoundary: true}
	src := &ContentPolicy{BlacklistWords: []string{"competitor"}, Normalize: true, WordBoundary: true}
	tgt := &ContentPolicy{WhitelistWords: []string{"announcement"}, WhitelistMode: "any", Normalize: true}
	c := NewChain(task, src, tgt)
	body := strings.Repeat("this is a long test message body. ", 60)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = c.Evaluate(body, "text")
	}
}

// BenchmarkFoldText isolates the fold itself (NFC + invisible strip + variant
// map + casefold), which every keyword and every pre-filter pass pays for.
func BenchmarkFoldText(b *testing.B) {
	body := strings.Repeat("این یک پیام آزمایشی طولانی است. ", 60)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = FoldText(body, true, false)
	}
}

// BenchmarkHasLink measures the canonical link predicate on bodies that
// carry links and bodies that do not.
func BenchmarkHasLink(b *testing.B) {
	bodies := []struct {
		name string
		text string
	}{
		{"NoLinks", strings.Repeat("plain body text here. ", 40)},
		{"OneLink", "see https://example.com/path for details " + strings.Repeat("plain. ", 30)},
		{"ManyLinks", strings.Repeat("visit https://example.com/a and t.me/b ", 20)},
	}
	for _, bd := range bodies {
		b.Run(bd.name, func(b *testing.B) {
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				_ = HasLink(bd.text)
			}
		})
	}
}

// BenchmarkCompiledKeywordReuse measures the cached-path: the same needle
// compiled once and reused (the production path after the first message).
func BenchmarkCompiledKeywordReuse(b *testing.B) {
	m := NewMatcher()
	needle := "تخفیف"
	body := strings.Repeat("این یک پیام آزمایشی طولانی است. ", 60)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = m.ContainsWord(body, needle, true, true, false)
	}
}
