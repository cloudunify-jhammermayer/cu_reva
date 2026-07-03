package ui

import (
	"strings"
	"testing"

	"reva-tui/internal/api"
)

func TestFeedbackLoadAndView(t *testing.T) {
	f := newFeedback(&api.MockClient{})
	msg := f.load()() // run the load command -> feedbackLoadedMsg
	f, _ = f.update(msg)

	if f.err != nil {
		t.Fatalf("unexpected err: %v", f.err)
	}
	if len(f.stats) == 0 || len(f.mutes) == 0 {
		t.Fatalf("expected mock stats and mutes, got %d/%d", len(f.stats), len(f.mutes))
	}

	out := f.view(120, 30)
	for _, want := range []string{"Feedback & learning", "Dismissed", "Muted categories", "style"} {
		if !strings.Contains(out, want) {
			t.Errorf("view missing %q", want)
		}
	}
}

func TestFeedbackShowsLearnedMemory(t *testing.T) {
	f := newFeedback(&api.MockClient{})
	f, _ = f.update(f.load()())
	if len(f.memory) == 0 {
		t.Fatal("expected mock learned memory")
	}
	// tall viewport so the memory section (rendered after stats + mutes) is visible
	out := f.view(120, 60)
	for _, want := range []string{"Learned memory", "v3", "Learned team preferences"} {
		if !strings.Contains(out, want) {
			t.Errorf("view missing %q:\n%s", want, out)
		}
	}
}

func TestFeedbackRefreshKey(t *testing.T) {
	f := newFeedback(&api.MockClient{})
	if _, cmd := f.update(keyMsg("r")); cmd == nil {
		t.Fatal("expected a refresh command on 'r'")
	}
}
