package ui

import (
	"strings"
	"testing"

	"reva-tui/internal/api"
)

func supportWithThreads(t *testing.T) Support {
	t.Helper()
	s := newSupport(&api.MockClient{})
	s.width, s.height = 120, 30
	data, _ := (&api.MockClient{}).SupportThreads(100, 0)
	s, _ = s.update(supportThreadsLoadedMsg{data: data})
	return s
}

// supportDetailLoaded selects threadID's row, opens its detail (enter), and
// feeds back the GET /support-threads/{id} response so the turns list is
// populated — the standard fixture for drill-down tests below.
func supportDetailLoaded(t *testing.T, s Support, threadID int) Support {
	t.Helper()
	idx := -1
	for i, th := range s.filtered() {
		if th.ID == threadID {
			idx = i
			break
		}
	}
	if idx < 0 {
		t.Fatalf("thread %d not found in the list", threadID)
	}
	s.cursor = idx
	s, cmd := s.update(keyMsg("enter"))
	if cmd == nil {
		t.Fatalf("enter on thread %d produced no load command", threadID)
	}
	msg, ok := cmd().(supportThreadDetailLoadedMsg)
	if !ok {
		t.Fatalf("expected supportThreadDetailLoadedMsg, got %T", cmd())
	}
	s, _ = s.update(msg)
	return s
}

func TestSupportEmptyView(t *testing.T) {
	s := newSupport(&api.MockClient{})
	s.width, s.height = 120, 30
	s, _ = s.update(supportThreadsLoadedMsg{data: &api.SupportThreadPage{Total: 0}})

	out := s.view(120, 30)
	if !strings.Contains(out, "No support threads yet") {
		t.Fatalf("empty view missing placeholder:\n%s", out)
	}
}

func TestSupportErrorView(t *testing.T) {
	s := newSupport(&api.MockClient{})
	s, _ = s.update(supportThreadsLoadedMsg{err: errFake})

	out := s.view(120, 30)
	if !strings.Contains(out, "Error: boom") {
		t.Fatalf("error view missing message:\n%s", out)
	}
}

func TestSupportListShowsColumnsAndRows(t *testing.T) {
	s := supportWithThreads(t)
	out := s.view(120, 30)

	for _, want := range []string{
		"Repository", "Ticket", "Model", "Status", "Last turn",
		"acme/widgets", "#456", "helpdesk.ticket", "open",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("list view missing %q:\n%s", want, out)
		}
	}
	// The project-less thread (github_url "") groups under the "—" placeholder,
	// not a fabricated repo name.
	if !strings.Contains(out, "—") {
		t.Fatal("list view missing the no-repo placeholder for a project-less ticket")
	}
}

func TestSupportFilterNarrowsToMatchingThreads(t *testing.T) {
	s := supportWithThreads(t)
	s.filtering, s.filter = true, "widgets"
	out := s.view(120, 30)
	if !strings.Contains(out, "acme/widgets") {
		t.Fatalf("filtered view missing matching repo:\n%s", out)
	}
	if strings.Contains(out, "odoo-modules") {
		t.Fatalf("filtered view leaked a non-matching repo:\n%s", out)
	}
}

func TestSupportEnterOpensThreadDetailAndLoadsTurns(t *testing.T) {
	s := supportWithThreads(t)
	idx := -1
	for i, th := range s.filtered() {
		if th.ID == 4 {
			idx = i
		}
	}
	s.cursor = idx
	s, cmd := s.update(keyMsg("enter"))
	if !s.detail {
		t.Fatal("enter did not open the thread detail")
	}
	if cmd == nil {
		t.Fatal("enter produced no thread-load command")
	}
	// Before the response arrives, the pane shows a loading placeholder.
	if !strings.Contains(s.view(120, 30), "Loading turns") {
		t.Fatalf("detail view missing loading placeholder:\n%s", s.view(120, 30))
	}

	msg, ok := cmd().(supportThreadDetailLoadedMsg)
	if !ok || msg.threadID != 4 {
		t.Fatalf("expected a thread-4 load, got %+v", msg)
	}
	s, _ = s.update(msg)

	out := s.view(120, 30)
	for _, want := range []string{"Seq", "Kind", "Answer", "Grounding", "completed", "failed", "pending"} {
		if !strings.Contains(out, want) {
			t.Errorf("turns view missing %q:\n%s", want, out)
		}
	}
}

// TestSupportTurnsShowGroundingLevel is the operationally important case:
// grounding_level (code/docs/none) must be visible directly on each turn row,
// not behind a further drill.
func TestSupportTurnsShowGroundingLevel(t *testing.T) {
	s := supportWithThreads(t)
	s = supportDetailLoaded(t, s, 4)
	if len(s.turns) != 3 {
		t.Fatalf("expected 3 turns for thread 4, got %d", len(s.turns))
	}

	out := s.view(120, 30)
	if !strings.Contains(out, "code") {
		t.Errorf("turns view missing the code-grounded turn:\n%s", out)
	}
	// The failed turn must stay visible (this is the operator view) and must
	// not be silently dropped from the list.
	if !strings.Contains(out, "failed") {
		t.Errorf("turns view missing the failed turn:\n%s", out)
	}
	if !strings.Contains(out, "pending") {
		t.Errorf("turns view missing the still-pending turn:\n%s", out)
	}
}

func TestSupportThreadDetailErrorView(t *testing.T) {
	s := supportWithThreads(t)
	s, _ = s.update(keyMsg("enter"))
	s, _ = s.update(supportThreadDetailLoadedMsg{threadID: s.detailID, err: errFake})

	out := s.view(120, 30)
	if !strings.Contains(out, "Error: boom") {
		t.Fatalf("detail error view missing message:\n%s", out)
	}
}

// TestSupportStaleThreadResponseIgnored mirrors the Tickets tab's journey
// staleness guard: a response for a thread the user has already left (esc'd
// out of) must not be applied.
func TestSupportStaleThreadResponseIgnored(t *testing.T) {
	s := supportWithThreads(t)
	s, cmd := s.update(keyMsg("enter"))
	if cmd == nil {
		t.Fatal("enter produced no load command")
	}
	s, _ = s.update(keyMsg("esc")) // leave before the response arrives

	msg := cmd().(supportThreadDetailLoadedMsg)
	s, _ = s.update(msg)
	if len(s.turns) != 0 {
		t.Fatalf("stale thread-detail response was applied: %+v", s.turns)
	}
}

func TestSupportRequeueSelectedTurn(t *testing.T) {
	s := supportWithThreads(t)
	s = supportDetailLoaded(t, s, 4)
	// Move the cursor to seq 2 (the failed turn, id 506).
	s, _ = s.update(keyMsg("j"))
	if s.turnCursor != 1 || s.turns[s.turnCursor].ID != 506 {
		t.Fatalf("expected cursor on turn 506, got index %d (%+v)", s.turnCursor, s.turns[s.turnCursor])
	}

	s, cmd := s.update(keyMsg("e"))
	if cmd == nil {
		t.Fatal("e produced no requeue command")
	}
	msg, ok := cmd().(supportTurnRequeuedMsg)
	if !ok || msg.turnID != 506 {
		t.Fatalf("expected requeue of turn 506, got %+v", msg)
	}
	s, _ = s.update(msg)
	if !strings.Contains(s.statusMsg, "turn #506 requeued") {
		t.Fatalf("statusMsg = %q", s.statusMsg)
	}
}

func TestSupportRequeueBeforeTurnsLoadIsNoop(t *testing.T) {
	s := supportWithThreads(t)
	s, _ = s.update(keyMsg("enter")) // detail open, turns not loaded yet
	s, cmd := s.update(keyMsg("e"))
	if cmd != nil {
		t.Fatal("requeue before any turns loaded should not produce a command")
	}
}

func TestSupportEscFromDetailReturnsToList(t *testing.T) {
	s := supportWithThreads(t)
	s, _ = s.update(keyMsg("enter"))
	if !s.detail {
		t.Fatal("enter did not open detail")
	}
	s, _ = s.update(keyMsg("esc"))
	if s.detail {
		t.Fatal("esc did not leave the detail view")
	}
}

// TestSupportListFitsShortTerminal guards the list's row windowing (visibleRows
// derived from h), mirroring the other tabs' list views. The turns pane's
// overflow case is covered by TestNoTabOverflowsTerminal (via the App's
// MaxHeight safety net).
func TestSupportListFitsShortTerminal(t *testing.T) {
	s := supportWithThreads(t)
	if lines := strings.Count(s.view(80, 10), "\n") + 1; lines > 10 {
		t.Fatalf("list view is %d lines, want <= 10", lines)
	}
}
