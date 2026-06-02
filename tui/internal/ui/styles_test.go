package ui

import (
	"testing"
	"unicode/utf8"
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
