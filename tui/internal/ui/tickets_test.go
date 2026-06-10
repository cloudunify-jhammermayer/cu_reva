package ui

import (
	"errors"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"reva-tui/internal/api"
)

var errFake = errors.New("boom")

// keyMsg builds a tea.KeyMsg whose String() matches what update() switches on:
// named keys ("enter", "esc") use their KeyType; single chars are runes.
func keyMsg(s string) tea.KeyMsg {
	switch s {
	case "enter":
		return tea.KeyMsg{Type: tea.KeyEnter}
	case "esc":
		return tea.KeyMsg{Type: tea.KeyEscape}
	default:
		return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(s)}
	}
}

func intPtr(i int) *int    { return &i }
func strPtr(s string) *string { return &s }

// rowOf returns the union-row index for a ticket id (-1 if absent).
func rowOf(tab Tickets, ticketID int) int {
	for i, r := range tab.rows {
		if r.ticketID == ticketID {
			return i
		}
	}
	return -1
}

func ticketsWithData() Tickets {
	t := newTickets(&api.MockClient{}, "https://odoo.example.com")
	t.width, t.height = 120, 30

	analyses, _ := (&api.MockClient{}).TicketAnalyses(100)
	t, _ = t.update(ticketAnalysesLoadedMsg{data: analyses})
	runs, _ := (&api.MockClient{}).TicketIssueRuns(100)
	t, _ = t.update(ticketIssueRunsLoadedMsg{data: runs})
	return t
}

func TestIssueRunsMappedLatestPerRecord(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	newer := api.TicketIssueRunSummary{
		ID: 9, TicketID: 1, ModelName: "helpdesk.ticket", Status: "completed",
		CreatedAt: time.Now(),
	}
	older := api.TicketIssueRunSummary{
		ID: 4, TicketID: 1, ModelName: "helpdesk.ticket", Status: "failed",
		CreatedAt: time.Now().Add(-time.Hour),
	}
	// feed is newest-first; the first run per record must win
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{
		data: &api.TicketIssueRunPage{Items: []api.TicketIssueRunSummary{newer, older}, Total: 2},
	})

	got, ok := tab.issueRuns[issueRunKey("helpdesk.ticket", 1)]
	if !ok || got.ID != 9 {
		t.Fatalf("expected latest run 9 mapped, got %+v (ok=%v)", got, ok)
	}
}

func TestIssueRunsErrorDoesNotBlankMap(t *testing.T) {
	tab := ticketsWithData()
	before := len(tab.issueRuns)
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{err: errFake})
	if len(tab.issueRuns) != before {
		t.Fatalf("issueRuns map changed on error: %d -> %d", before, len(tab.issueRuns))
	}
}

func TestIssueRunCounts(t *testing.T) {
	full := api.TicketIssueRunSummary{Status: "completed", Issues: []api.TicketIssueRef{
		{Number: intPtr(42), Title: "a"}, {Number: intPtr(43), Title: "b"},
	}}
	if got := issueRunCounts(full); got != "2" {
		t.Errorf("full run counts = %q, want 2", got)
	}
	partial := api.TicketIssueRunSummary{Status: "failed", Issues: []api.TicketIssueRef{
		{Number: intPtr(40), Title: "a"}, {Number: nil, Title: "b"},
	}}
	if got := issueRunCounts(partial); got != "1/2" {
		t.Errorf("partial run counts = %q, want 1/2", got)
	}
	if got := issueRunCounts(api.TicketIssueRunSummary{Status: "pending"}); got != "" {
		t.Errorf("pending run counts = %q, want empty", got)
	}
}

func TestTicketsViewShowsIssueColumnAndDetails(t *testing.T) {
	tab := ticketsWithData()
	// ticket 456 has a completed run with issues #42/#43; select its row
	tab.cursor = rowOf(tab, 456)
	out := tab.view(120, 30)

	if !strings.Contains(out, "Issues") {
		t.Fatal("view missing Issues column header")
	}
	if !strings.Contains(out, "issues: completed") {
		t.Fatalf("view missing selected-row issue status, got:\n%s", out)
	}
	// closed (done) issues carry a check mark, open ones don't
	if !strings.Contains(out, "#42 Implement login form ✓") {
		t.Fatal("view missing done mark on the closed issue")
	}
	if strings.Contains(out, "#43 Add session handling ✓") {
		t.Fatal("open issue must not carry a done mark")
	}
}

func TestEnterDrillsIntoIssueListAndEscReturns(t *testing.T) {
	tab := ticketsWithData()
	// ticket 456 has a completed run with 2 issues
	tab.cursor = rowOf(tab, 456)
	tab, _ = tab.update(keyMsg("enter"))
	if !tab.detail {
		t.Fatal("enter did not open the issue drill-down")
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "GitHub Issues") {
		t.Fatal("detail view missing header")
	}
	if !strings.Contains(out, "#42") || !strings.Contains(out, "Implement login form") {
		t.Fatalf("detail view missing issue rows, got:\n%s", out)
	}
	if !strings.Contains(out, "done ✓") {
		t.Fatal("detail view missing done state for the closed issue")
	}
	// j moves within the list, esc returns to the table
	tab, _ = tab.update(keyMsg("j"))
	if tab.detailCursor != 1 {
		t.Fatalf("detailCursor = %d, want 1", tab.detailCursor)
	}
	tab, _ = tab.update(keyMsg("esc"))
	if tab.detail {
		t.Fatal("esc did not leave the drill-down")
	}
}

func TestEnterWithoutIssuesShowsStatus(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	tab.width, tab.height = 120, 30
	// a ticket with an analysis but no create-issues run
	tab, _ = tab.update(ticketAnalysesLoadedMsg{data: &api.TicketAnalysisPage{
		Items: []api.TicketAnalysisSummary{
			{ID: 1, TicketID: 777, ModelName: "helpdesk.ticket", Status: "completed"},
		},
		Total: 1,
	}})
	tab, _ = tab.update(keyMsg("enter"))
	if tab.detail {
		t.Fatal("opened drill-down for a ticket with no issues")
	}
	if !strings.Contains(tab.statusMsg, "no GitHub issues") {
		t.Fatalf("expected a no-issues status, got %q", tab.statusMsg)
	}
}

func TestTicketsViewShowsPartialAndErrorForFailedRun(t *testing.T) {
	tab := ticketsWithData()
	// select the project.task 123 row (failed run, 1/2 created)
	tab.cursor = rowOf(tab, 123)
	out := tab.view(120, 30)
	if !strings.Contains(out, "1/2") {
		t.Fatalf("view missing partial count 1/2, got:\n%s", out)
	}
	if !strings.Contains(out, "issues error: GitHub 403") {
		t.Fatal("view missing issue-run error for failed run")
	}
	if !strings.Contains(out, "(not created) Add export cron") {
		t.Fatal("view missing not-created plan item")
	}
}

func TestIssueRunWithoutAnalysisStillAppears(t *testing.T) {
	// The reported gap: a create-issues run on a never-analyzed task must show
	// in the tab (previously the tab only listed analyses).
	tab := newTickets(&api.MockClient{}, "")
	tab.width, tab.height = 120, 30
	// no analyses at all
	tab, _ = tab.update(ticketAnalysesLoadedMsg{data: &api.TicketAnalysisPage{Total: 0}})
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{data: &api.TicketIssueRunPage{
		Items: []api.TicketIssueRunSummary{{
			ID: 6, TicketID: 2112, ModelName: "project.task", Status: "completed",
			Issues: []api.TicketIssueRef{
				{Number: intPtr(17), Title: "Scaffold module", State: strPtr("open")},
			},
			CreatedAt: time.Now(),
		}},
		Total: 1,
	}})

	if rowOf(tab, 2112) < 0 {
		t.Fatal("issue-only ticket 2112 is missing from the tab")
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "#2112") {
		t.Fatalf("view missing issue-only ticket, got:\n%s", out)
	}
	// its analysis cell is blank, issues column shows the count
	tab.cursor = rowOf(tab, 2112)
	if !strings.Contains(tab.view(120, 30), "Scaffold module") {
		t.Fatal("enter-less detail line missing the issue")
	}
}

func TestAppRoutesIssueRunsMsgToTicketsTab(t *testing.T) {
	// Regression: app.Update switches on message types; a missing case for
	// ticketIssueRunsLoadedMsg silently dropped the feed and the Tickets tab
	// never showed issue runs.
	app := NewApp(&api.MockClient{}, "https://odoo.example.com")
	runs, _ := (&api.MockClient{}).TicketIssueRuns(100)

	model, _ := app.Update(ticketIssueRunsLoadedMsg{data: runs})

	got := model.(*App).tickets.issueRuns
	if len(got) == 0 {
		t.Fatal("ticketIssueRunsLoadedMsg not routed to the Tickets tab")
	}
	if _, ok := got[issueRunKey("helpdesk.ticket", 456)]; !ok {
		t.Fatalf("expected run for helpdesk.ticket#456 in map, got %v", got)
	}
}
