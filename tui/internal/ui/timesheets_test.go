package ui

import (
	"errors"
	"strings"
	"testing"

	"reva-tui/internal/api"
)

func TestTimesheetsLoadAndView(t *testing.T) {
	ts := newTimesheets(&api.MockClient{})
	ts.width, ts.height = 120, 30
	data, _ := (&api.MockClient{}).TimesheetReviews(100)
	ts, _ = ts.update(timesheetsLoadedMsg{data: data})

	if len(ts.items) != 3 {
		t.Fatalf("expected 3 timesheet rows, got %d", len(ts.items))
	}
	out := ts.view(120, 30)
	for _, want := range []string{"Timesheet Reviews", "TS-2026-07-05-001", "23 rw / 4 human"} {
		if !strings.Contains(out, want) {
			t.Fatalf("view missing %q:\n%s", want, out)
		}
	}
}

func TestTimesheetsErrorView(t *testing.T) {
	ts := newTimesheets(&api.MockClient{})
	ts, _ = ts.update(timesheetsLoadedMsg{err: errors.New("boom")})

	out := ts.view(100, 20)
	if !strings.Contains(out, "Error: boom") {
		t.Fatalf("error view missing message:\n%s", out)
	}
}
