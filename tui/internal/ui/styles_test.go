package ui

import (
	"testing"
	"unicode/utf8"

	"github.com/charmbracelet/lipgloss"
)

func TestTruncateASCII(t *testing.T) {
	if got := truncate("hello world", 8); got != "hello..." {
		t.Fatalf("got %q", got)
	}
	if got := truncate("short", 10); got != "short" {
		t.Fatalf("got %q", got)
	}
}

func TestTruncateDoesNotCorruptMultibyte(t *testing.T) {
	// CORR-16: byte-slicing would split a multibyte rune and yield invalid UTF-8.
	s := "café société ñoño 日本語テキスト"
	for n := 1; n <= len([]rune(s))+2; n++ {
		got := truncate(s, n)
		if !utf8.ValidString(got) {
			t.Fatalf("truncate(%q, %d) = %q is not valid UTF-8", s, n, got)
		}
		if utf8.RuneCountInString(got) > n {
			t.Fatalf("truncate(%q, %d) = %q exceeds %d runes", s, n, got, n)
		}
	}
}

func TestPadCellPadsByVisibleWidthIgnoringANSI(t *testing.T) {
	// Plain text pads like %-*s would.
	if got := padCell("ab", 5); got != "ab   " {
		t.Fatalf("padCell(\"ab\",5) = %q, want \"ab   \"", got)
	}
	// Already at/over width → unchanged (no truncation).
	if got := padCell("abcdef", 4); got != "abcdef" {
		t.Fatalf("padCell over width changed the string: %q", got)
	}
	// The bug: a colored cell must still occupy exactly `width` visible columns,
	// even though its byte length is inflated by ANSI escapes. (lipgloss emits no
	// ANSI under a non-TTY test profile, so use a literal escape to be
	// deterministic — this is what the real terminal produces.)
	colored := "\x1b[32m+ completed\x1b[0m" // 11 visible cols, more bytes
	if len(colored) <= lipgloss.Width(colored) {
		t.Fatal("precondition: ANSI string should have more bytes than visible width")
	}
	if w := lipgloss.Width(padCell(colored, 20)); w != 20 {
		t.Fatalf("padded colored cell visible width = %d, want 20", w)
	}
}

func TestListNav(t *testing.T) {
	const total, vis = 50, 10
	// g jumps to top, G to bottom (offset follows the cursor into view).
	if c, o, ok := listNav("G", 0, 0, total, vis); !ok || c != 49 || o != 40 {
		t.Fatalf("G = (%d,%d,%v), want (49,40,true)", c, o, ok)
	}
	if c, o, ok := listNav("g", 49, 40, total, vis); !ok || c != 0 || o != 0 {
		t.Fatalf("g = (%d,%d,%v), want (0,0,true)", c, o, ok)
	}
	// pgdown moves a full page; ctrl+d a half page.
	if c, _, _ := listNav("pgdown", 0, 0, total, vis); c != 10 {
		t.Fatalf("pgdown cursor = %d, want 10", c)
	}
	if c, _, _ := listNav("ctrl+d", 0, 0, total, vis); c != 5 {
		t.Fatalf("ctrl+d cursor = %d, want 5", c)
	}
	// Non-nav keys are not handled.
	if _, _, ok := listNav("x", 3, 0, total, vis); ok {
		t.Fatal("listNav claimed a non-nav key")
	}
	// Clamps at the ends — can't page past the last row.
	if c, _, _ := listNav("pgdown", 49, 40, total, vis); c != 49 {
		t.Fatalf("pgdown at end cursor = %d, want 49", c)
	}
}

func TestShortSHA(t *testing.T) {
	cases := map[string]string{
		"deadbeefcafebabe": "deadbeef", // long → first 8
		"deadbeef":         "deadbeef", // exactly 8
		"abc":              "abc",      // short → unchanged (no panic)
		"":                 "",         // empty → unchanged (CORR-15)
	}
	for in, want := range cases {
		if got := shortSHA(in); got != want {
			t.Fatalf("shortSHA(%q) = %q, want %q", in, got, want)
		}
	}
}
