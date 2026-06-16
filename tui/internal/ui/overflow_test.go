package ui

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

func TestClampOffset(t *testing.T) {
	cases := []struct {
		offset, total, visible, want int
	}{
		{0, 10, 5, 0},  // top
		{3, 10, 5, 3},  // mid, in range
		{99, 10, 5, 5}, // past end → clamped to total-visible
		{-4, 10, 5, 0}, // negative → 0
		{2, 3, 5, 0},   // everything fits → no scroll
		{7, 7, 1, 6},   // visible 1 → last line
		{99, 10, 0, 9}, // visible<1 treated as 1 → clamps to total-1
	}
	for _, c := range cases {
		if got := clampOffset(c.offset, c.total, c.visible); got != c.want {
			t.Errorf("clampOffset(%d,%d,%d)=%d, want %d",
				c.offset, c.total, c.visible, got, c.want)
		}
	}
}

func TestScrollHintShownOnlyWhenContentOverflows(t *testing.T) {
	if h := scrollHint(0, 10, 8); h != "" {
		t.Errorf("expected empty hint when all 8 lines fit in 10 rows, got %q", h)
	}
	if h := scrollHint(0, 5, 12); h == "" {
		t.Fatal("expected a hint when 12 lines exceed 5 rows")
	}
}

// apply runs one App.Update and, when it yields a command (e.g. an auto
// detail-load), runs that command once and feeds its message back — enough to
// populate the lazily-loaded detail panes the test wants to size-check.
func apply(t *testing.T, a *App, msg tea.Msg) *App {
	t.Helper()
	m, cmd := a.Update(msg)
	a = m.(*App)
	if cmd != nil {
		if next := cmd(); next != nil {
			m, _ = a.Update(next)
			a = m.(*App)
		}
	}
	return a
}

// loadedApp returns an App with every tab populated from MockClient.
func loadedApp(t *testing.T) *App {
	t.Helper()
	mc := &api.MockClient{}
	a := NewApp(mc, "https://odoo.example.com")

	dash, _ := mc.Dashboard()
	a = apply(t, a, dashboardLoadedMsg{data: dash})
	reviews, _ := mc.Reviews(100, "", "", "")
	a = apply(t, a, reviewsLoadedMsg{data: reviews}) // auto-loads detail of item 0
	fails, _ := mc.Failures(50)
	a = apply(t, a, failuresLoadedMsg{data: fails})
	finds, _ := mc.Findings("", "", 200)
	a = apply(t, a, findingsLoadedMsg{data: finds})
	repos, _ := mc.Repos()
	a = apply(t, a, reposLoadedMsg{data: repos})
	pend, _ := mc.Pending()
	a = apply(t, a, pendingLoadedMsg{data: pend})
	tan, _ := mc.TicketAnalyses(100)
	a = apply(t, a, ticketAnalysesLoadedMsg{data: tan})
	tir, _ := mc.TicketIssueRuns(100)
	a = apply(t, a, ticketIssueRunsLoadedMsg{data: tir})
	auds, _ := mc.Audits(100)
	a = apply(t, a, auditRunsLoadedMsg{data: auds})
	stats, _ := mc.Learning()
	mutes, _ := mc.Mutes()
	a = apply(t, a, feedbackLoadedMsg{stats: stats, mutes: mutes})
	return a
}

// TestNoTabOverflowsTerminal is the regression guard for the reported bug:
// with lots of content in a short terminal, every tab — including the
// detail/findings/issue drill-downs — must render within the window height so
// the tab and status bars stay on screen.
func TestNoTabOverflowsTerminal(t *testing.T) {
	// Narrow widths wrap the tab/status bars; short heights leave little room —
	// both are where overflow used to push the bars off screen.
	sizes := []struct{ w, h int }{
		{60, 8}, {80, 10}, {100, 14}, {120, 24}, {200, 50},
	}
	views := []view{viewDashboard, viewReviews, viewFindings, viewFailures,
		viewRepos, viewPending, viewTickets, viewAudits, viewFeedback}

	for _, sz := range sizes {
		a := loadedApp(t)
		a = apply(t, a, tea.WindowSizeMsg{Width: sz.w, Height: sz.h})

		check := func(label string) {
			t.Helper()
			if got := lipgloss.Height(a.View()); got > sz.h {
				t.Errorf("%dx%d %s: view height %d exceeds terminal height %d",
					sz.w, sz.h, label, got, sz.h)
			}
		}

		for _, v := range views {
			a.active = v
			check(viewName(v))
		}

		// Audits → findings drill-down (was an unbounded dump of up to 200 rows).
		a.active = viewAudits
		a = apply(t, a, keyMsg("enter"))
		check("audits/findings")

		// Tickets → issue drill-down (was unbounded). Groups are collapsed by
		// default, so open the repo group before drilling into the row.
		a.active = viewTickets
		a.tickets = onRow(a.tickets, 456)
		a = apply(t, a, keyMsg("enter"))
		check("tickets/issues")
	}
}

func viewName(v view) string {
	switch v {
	case viewDashboard:
		return "dashboard"
	case viewReviews:
		return "reviews"
	case viewFindings:
		return "findings"
	case viewFailures:
		return "failures"
	case viewRepos:
		return "repos"
	case viewPending:
		return "pending"
	case viewTickets:
		return "tickets"
	case viewAudits:
		return "audits"
	case viewFeedback:
		return "feedback"
	}
	return "?"
}
