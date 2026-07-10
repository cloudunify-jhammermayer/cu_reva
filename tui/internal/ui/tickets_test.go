package ui

import (
	"errors"
	"fmt"
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

func intPtr(i int) *int       { return &i }
func strPtr(s string) *string { return &s }

// rowOf returns the cursor index of a ticket's row in the visible (grouped,
// foldable) item list the cursor navigates (-1 if absent / collapsed).
func rowOf(tab Tickets, ticketID int) int {
	for i, it := range tab.visibleItems() {
		if !it.header && it.row.ticketID == ticketID {
			return i
		}
	}
	return -1
}

// onRow expands the ticket's repo group and parks the cursor on its row. Groups
// are collapsed by default, so row-level tests must open the group first.
func onRow(tab Tickets, ticketID int) Tickets {
	for _, r := range tab.groupedRows() {
		if r.ticketID == ticketID {
			tab.expanded[tab.repoKey(r)] = true
		}
	}
	tab.cursor = rowOf(tab, ticketID)
	return tab
}

// journeyStubClient overrides MockClient.TicketJourney to return a caller-set
// event list or error, for tests that need control over the journey response
// (e.g. truncation past 30 events) beyond MockClient's fixed 6-event fixture.
type journeyStubClient struct {
	api.MockClient
	events []api.JourneyEvent
	err    error
}

func (c *journeyStubClient) TicketJourney(odooInstanceID *int, modelName string, ticketID int) (*api.TicketJourney, error) {
	if c.err != nil {
		return nil, c.err
	}
	return &api.TicketJourney{Events: c.events}, nil
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

func TestRequeuePrefersFailedIssueRun(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	tab.width, tab.height = 120, 30
	tab, _ = tab.update(ticketAnalysesLoadedMsg{data: &api.TicketAnalysisPage{
		Items: []api.TicketAnalysisSummary{{
			ID: 7, TicketID: 1, ModelName: "helpdesk.ticket",
			Status: "completed", CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{data: &api.TicketIssueRunPage{
		Items: []api.TicketIssueRunSummary{{
			ID: 33, TicketID: 1, ModelName: "helpdesk.ticket", Status: "failed",
			GithubURL: "https://github.com/acme/alpha", CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab = onRow(tab, 1)

	tab, cmd := tab.update(keyMsg("e"))
	if cmd == nil {
		t.Fatal("e produced no command")
	}
	msg, ok := cmd().(ticketRequeuedMsg)
	if !ok || msg.kind != "issues run" || msg.id != 33 {
		t.Fatalf("expected issues-run requeue of #33, got %+v", msg)
	}
	tab, _ = tab.update(msg)
	if !strings.Contains(tab.statusMsg, "issues run #33 requeued") {
		t.Fatalf("statusMsg = %q", tab.statusMsg)
	}
}

func TestRequeueFallsBackToAnalysisWhenRunNotFailed(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	tab.width, tab.height = 120, 30
	tab, _ = tab.update(ticketAnalysesLoadedMsg{data: &api.TicketAnalysisPage{
		Items: []api.TicketAnalysisSummary{{
			ID: 7, TicketID: 1, ModelName: "helpdesk.ticket",
			Status: "failed", CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{data: &api.TicketIssueRunPage{
		Items: []api.TicketIssueRunSummary{{
			ID: 33, TicketID: 1, ModelName: "helpdesk.ticket", Status: "completed",
			GithubURL: "https://github.com/acme/alpha", CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab = onRow(tab, 1)

	tab, cmd := tab.update(keyMsg("e"))
	if cmd == nil {
		t.Fatal("e produced no command")
	}
	msg, ok := cmd().(ticketRequeuedMsg)
	if !ok || msg.kind != "analysis" || msg.id != 7 {
		t.Fatalf("expected analysis requeue of #7, got %+v", msg)
	}
}

func TestAnalysesLatestPerTicketWins(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	newer := api.TicketAnalysisSummary{
		ID: 19, TicketID: 5, ModelName: "helpdesk.ticket", FieldName: "x_reva_analysis",
		Status: "completed", CreatedAt: time.Now(),
	}
	older := api.TicketAnalysisSummary{
		ID: 18, TicketID: 5, ModelName: "helpdesk.ticket", FieldName: "x_reva_analysis",
		Status: "failed", CreatedAt: time.Now().Add(-time.Hour),
	}
	// feed is newest-first; a resent analysis must shadow the failed one
	tab, _ = tab.update(ticketAnalysesLoadedMsg{
		data: &api.TicketAnalysisPage{Items: []api.TicketAnalysisSummary{newer, older}, Total: 2},
	})
	if len(tab.rows) != 1 {
		t.Fatalf("expected 1 row, got %d", len(tab.rows))
	}
	if a := tab.rows[0].analysis; a == nil || a.ID != 19 || a.Status != "completed" {
		t.Fatalf("expected latest analysis 19/completed on the row, got %+v", a)
	}
}

func TestIssueRunsMergeOlderCreatedIssues(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	epic := api.TicketIssueRef{Number: intPtr(78), Title: "epic"}
	feedback := api.TicketIssueRunSummary{
		ID: 9, TicketID: 1181, ModelName: "helpdesk.ticket", Status: "completed",
		GithubURL: "https://github.com/Cloudunify/bmd-test",
		Issues:    []api.TicketIssueRef{{Number: intPtr(82), Title: "new"}},
		CreatedAt: time.Now(),
	}
	initial := api.TicketIssueRunSummary{
		ID: 4, TicketID: 1181, ModelName: "helpdesk.ticket", Status: "completed",
		GithubURL:   "https://github.com/Cloudunify/bmd-test",
		ParentIssue: &epic,
		Issues: []api.TicketIssueRef{
			{Number: intPtr(79), Title: "a"},
			{Number: intPtr(80), Title: "b"},
			{Number: intPtr(81), Title: "c", State: strPtr("closed")},
		},
		CreatedAt: time.Now().Add(-time.Hour),
	}
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{
		data: &api.TicketIssueRunPage{Items: []api.TicketIssueRunSummary{feedback, initial}, Total: 2},
	})

	got := tab.issueRuns[issueRunKey("helpdesk.ticket", 1181)]
	if got.ID != 9 {
		t.Fatalf("expected newest run 9 kept, got %d", got.ID)
	}
	if len(got.Issues) != 4 {
		t.Fatalf("expected 4 merged issues, got %d: %+v", len(got.Issues), got.Issues)
	}
	for i, want := range []int{79, 80, 81, 82} {
		if got.Issues[i].Number == nil || *got.Issues[i].Number != want {
			t.Fatalf("issue %d = %+v, want #%d", i, got.Issues[i], want)
		}
	}
	if got.ParentIssue == nil || *got.ParentIssue.Number != 78 {
		t.Fatalf("epic not adopted from the older run: %+v", got.ParentIssue)
	}
	if counts := issueRunCounts(got); counts != "4" {
		t.Fatalf("counts = %q, want 4", counts)
	}
}

func TestIssueRunsDoNotMergeAcrossRepos(t *testing.T) {
	tab := newTickets(&api.MockClient{}, "")
	newer := api.TicketIssueRunSummary{
		ID: 9, TicketID: 1, ModelName: "helpdesk.ticket", Status: "completed",
		GithubURL: "https://github.com/acme/alpha",
		Issues:    []api.TicketIssueRef{{Number: intPtr(5), Title: "new"}},
		CreatedAt: time.Now(),
	}
	older := api.TicketIssueRunSummary{
		ID: 4, TicketID: 1, ModelName: "helpdesk.ticket", Status: "completed",
		GithubURL: "https://github.com/acme/beta",
		Issues:    []api.TicketIssueRef{{Number: intPtr(3), Title: "other repo"}},
		CreatedAt: time.Now().Add(-time.Hour),
	}
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{
		data: &api.TicketIssueRunPage{Items: []api.TicketIssueRunSummary{newer, older}, Total: 2},
	})
	got := tab.issueRuns[issueRunKey("helpdesk.ticket", 1)]
	if len(got.Issues) != 1 || *got.Issues[0].Number != 5 {
		t.Fatalf("issues from another repo were merged: %+v", got.Issues)
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
	closed := api.TicketIssueRunSummary{Status: "completed", Issues: []api.TicketIssueRef{
		{Number: intPtr(42), Title: "a", State: strPtr("closed")},
		{Number: intPtr(43), Title: "b", State: strPtr("closed")},
	}}
	if got := issueRunCounts(closed); got != "✓ 2" {
		t.Errorf("closed run counts = %q, want ✓ 2", got)
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
	tab = onRow(tab, 456)
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
	tab = onRow(tab, 456)
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

func TestDetailViewShowsParentEpicWhenPresent(t *testing.T) {
	tab := ticketsWithData()
	// ticket 456's mock run carries a parent ("epic") issue #41.
	tab = onRow(tab, 456)
	tab, _ = tab.update(keyMsg("enter"))
	if tab.detailParent == nil {
		t.Fatal("entering detail did not capture the run's parent issue")
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "Epic: #41") {
		t.Fatalf("detail view missing the parent epic line, got:\n%s", out)
	}
}

func TestDetailViewOmitsEpicLineWithoutParent(t *testing.T) {
	// A run without a parent (the common single-issue / legacy case) must render
	// the issue list unchanged — no Epic line.
	tab := newTickets(&api.MockClient{}, "")
	tab.width, tab.height = 120, 30
	tab, _ = tab.update(ticketAnalysesLoadedMsg{data: &api.TicketAnalysisPage{Total: 0}})
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{data: &api.TicketIssueRunPage{
		Items: []api.TicketIssueRunSummary{{
			ID: 7, TicketID: 3030, ModelName: "project.task", Status: "completed",
			Issues: []api.TicketIssueRef{
				{Number: intPtr(50), Title: "Lone issue", State: strPtr("open")},
			},
			CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab = onRow(tab, 3030)
	tab, _ = tab.update(keyMsg("enter"))
	if !tab.detail {
		t.Fatal("did not drill into the run with one issue")
	}
	if tab.detailParent != nil {
		t.Fatal("captured a parent for a parentless run")
	}
	if strings.Contains(tab.view(120, 30), "Epic:") {
		t.Fatal("detail view rendered an Epic line for a parentless run")
	}
}

func TestDetailViewShowsIssueType(t *testing.T) {
	tt := Tickets{detail: true, detailIssueType: "CR",
		detailIssues: []api.TicketIssueRef{{Title: "x"}}}
	if out := tt.detailView(80, 20); !strings.Contains(out, "type CR") {
		t.Errorf("detail header missing type tag:\n%s", out)
	}
}

func TestDetailViewShowsAssignee(t *testing.T) {
	tt := Tickets{detail: true, detailAssignee: "alice",
		detailIssues: []api.TicketIssueRef{{Title: "x"}}}
	if out := tt.detailView(80, 20); !strings.Contains(out, "assignee @alice") {
		t.Errorf("detail header missing assignee:\n%s", out)
	}
}

func TestDetailViewShowsProjectAndPlanDate(t *testing.T) {
	tt := Tickets{detail: true,
		detailProject:  "https://github.com/orgs/acme/projects/5",
		detailPlanDate: "2026-07-15",
		detailIssues:   []api.TicketIssueRef{{Title: "x"}}}
	out := tt.detailView(120, 20)
	if !strings.Contains(out, "orgs/acme/projects/5") {
		t.Errorf("detail view missing project board line:\n%s", out)
	}
	if !strings.Contains(out, "due 2026-07-15") {
		t.Errorf("detail view missing plan date:\n%s", out)
	}
}

func TestDetailViewOmitsProjectLineWhenUnset(t *testing.T) {
	tt := Tickets{detail: true,
		detailIssues: []api.TicketIssueRef{{Title: "x"}}}
	if strings.Contains(tt.detailView(120, 20), "📋") {
		t.Fatal("detail view rendered a project line for a run without one")
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
	tab = onRow(tab, 777) // expand its group and land on the row
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
	tab = onRow(tab, 123)
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

	// Groups are collapsed by default; open the "(no repo yet)" group it lands in.
	tab = onRow(tab, 2112)
	if tab.cursor < 0 {
		t.Fatal("issue-only ticket 2112 is missing from the tab")
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "#2112") {
		t.Fatalf("view missing issue-only ticket, got:\n%s", out)
	}
	// its analysis cell is blank, issues column shows the count
	if !strings.Contains(tab.view(120, 30), "Scaffold module") {
		t.Fatal("enter-less detail line missing the issue")
	}
}

func TestCompletedButUndeliveredFlaggedNotInOdoo(t *testing.T) {
	// Mock ticket 777 is completed but its Odoo callback failed (CallbackSentAt
	// nil, CallbackError set) — the status cell must warn and the extras line
	// must surface the callback error.
	tab := ticketsWithData()
	tab = onRow(tab, 777)
	out := tab.view(120, 30)
	if !strings.Contains(out, "not in Odoo") {
		t.Fatalf("view missing the 'not in Odoo' delivery warning, got:\n%s", out)
	}
	if !strings.Contains(out, "callback error: Odoo write_field timed out") {
		t.Fatalf("view missing the callback error extras line, got:\n%s", out)
	}
}

func TestDeliveredAnalysisNotFlaggedNotInOdoo(t *testing.T) {
	// Mock ticket 456 is completed AND delivered (CallbackSentAt set) — it must
	// render a plain "completed", never the delivery warning, and show its
	// estimate range in the extras.
	tab := ticketsWithData()
	tab = onRow(tab, 456)
	out := tab.view(120, 30)
	if strings.Contains(out, "not in Odoo") {
		t.Fatalf("delivered analysis wrongly flagged 'not in Odoo':\n%s", out)
	}
	if !strings.Contains(out, "est. 12–20h") {
		t.Fatalf("view missing the estimate line, got:\n%s", out)
	}
}

func TestTicketsGroupedByRepo(t *testing.T) {
	tab := ticketsWithData()
	out := tab.view(120, 30)

	// Groups are collapsed by default (→ ▸); one header per repo plus the
	// analysis-only bucket, and no ticket rows are shown until a group is opened.
	for _, want := range []string{"▸ acme/widgets", "▸ acme/odoo-modules", "▸ acme/api", "(no repo yet)"} {
		if !strings.Contains(out, want) {
			t.Errorf("view missing group header %q, got:\n%s", want, out)
		}
	}
	for _, it := range tab.visibleItems() {
		if !it.header {
			t.Fatal("a ticket row is visible despite all groups being collapsed by default")
		}
	}

	g := tab.groupedRows()
	// Each repo's rows are contiguous (a group is never split).
	seen := map[string]bool{}
	prev := "\x00"
	for _, r := range g {
		if k := tab.repoKey(r); k != prev {
			if seen[k] {
				t.Fatalf("repo group %q is not contiguous", k)
			}
			seen[k] = true
			prev = k
		}
	}
	// Analysis-only tickets (no repo) come last.
	if k := tab.repoKey(g[len(g)-1]); k != "" {
		t.Fatalf("expected the (no repo yet) group last, got %q", k)
	}
}

func TestTicketsFoldGroup(t *testing.T) {
	tab := ticketsWithData()
	// All groups start collapsed; land on a repo header and expand it. Its rows
	// must appear, the header flips to ▾, and the cursor parks on the header.
	hdr := headerIndexOf(tab.visibleItems(), "acme/widgets")
	if hdr < 0 {
		t.Fatal("acme/widgets header not found")
	}
	before := len(tab.visibleItems())
	tab.cursor = hdr
	tab, _ = tab.update(keyMsg("enter")) // toggle fold on the header

	if !tab.expanded["acme/widgets"] {
		t.Fatal("enter on header did not expand the group")
	}
	if got := len(tab.visibleItems()); got <= before {
		t.Fatalf("expanding didn't reveal rows: %d -> %d", before, got)
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "▾ acme/widgets") {
		t.Fatalf("expanded header missing ▾ arrow:\n%s", out)
	}
	if it := tab.visibleItems()[tab.cursor]; !it.header || it.key != "acme/widgets" {
		t.Fatal("cursor did not park on the toggled group's header")
	}

	// z expands all when any group is collapsed (the others still are).
	tab, _ = tab.update(keyMsg("z"))
	for _, k := range tab.groupKeys() {
		if !tab.expanded[k] {
			t.Fatalf("z did not expand all (group %q still collapsed)", k)
		}
	}
	// z again collapses all.
	tab, _ = tab.update(keyMsg("z"))
	for _, k := range tab.groupKeys() {
		if tab.expanded[k] {
			t.Fatalf("second z did not collapse all (group %q still open)", k)
		}
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

func TestAppRoutesTicketJourneyLoadedMsgToTicketsTab(t *testing.T) {
	// Regression: app.Update switches on message types; a missing case for
	// ticketJourneyLoadedMsg would silently drop the fetch response and the
	// Tickets tab's detail pane would never populate the Journey block.
	app := NewApp(&api.MockClient{}, "https://odoo.example.com")
	key := issueRunKey("helpdesk.ticket", 456)
	app.tickets.detailKey = key

	model, _ := app.Update(ticketJourneyLoadedMsg{
		key:  key,
		data: &api.TicketJourney{Events: []api.JourneyEvent{{Kind: "ready", Summary: "routed via App.Update"}}},
	})

	got := model.(*App).tickets.journey
	if len(got) == 0 {
		t.Fatal("ticketJourneyLoadedMsg not routed to the Tickets tab")
	}
	if got[0].Summary != "routed via App.Update" {
		t.Fatalf("expected the routed event's summary, got %+v", got)
	}
}

func TestJourneyPopulatesAndRendersOnMatchingKey(t *testing.T) {
	tab := ticketsWithData()
	// ticket 456 has a completed run with issues; opening its detail must fire
	// a journey fetch keyed to that ticket.
	tab = onRow(tab, 456)
	tab, cmd := tab.update(keyMsg("enter"))
	if !tab.detail {
		t.Fatal("enter did not open the issue drill-down")
	}
	if cmd == nil {
		t.Fatal("enter did not return a journey fetch cmd")
	}
	msg, ok := cmd().(ticketJourneyLoadedMsg)
	if !ok {
		t.Fatalf("expected ticketJourneyLoadedMsg, got %T", cmd())
	}
	if msg.key != issueRunKey("helpdesk.ticket", 456) {
		t.Fatalf("journey fetch key = %q, want helpdesk.ticket#456", msg.key)
	}

	tab, _ = tab.update(msg)
	if len(tab.journey) == 0 {
		t.Fatal("journey not populated from a matching-key response")
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "Journey") {
		t.Fatalf("view missing the Journey title:\n%s", out)
	}
	if !strings.Contains(out, "Ticket marked ready") {
		t.Fatalf("view missing a journey event summary:\n%s", out)
	}
}

func TestJourneyStaleKeyIgnored(t *testing.T) {
	tab := ticketsWithData()
	tab = onRow(tab, 456)
	tab, _ = tab.update(keyMsg("enter"))
	if tab.detailKey != issueRunKey("helpdesk.ticket", 456) {
		t.Fatalf("detailKey = %q, want helpdesk.ticket#456", tab.detailKey)
	}

	// A response for a ticket the user has since navigated away from must be
	// dropped rather than overwriting the pane.
	tab, _ = tab.update(ticketJourneyLoadedMsg{
		key:  issueRunKey("project.task", 999),
		data: &api.TicketJourney{Events: []api.JourneyEvent{{Kind: "ready", Summary: "stale, must not apply"}}},
	})
	if len(tab.journey) != 0 {
		t.Fatalf("stale-key journey response was applied: %+v", tab.journey)
	}
	if strings.Contains(tab.view(120, 30), "stale, must not apply") {
		t.Fatal("view rendered a stale-key journey event")
	}
}

func TestJourneyTruncatesToLast30WithEarlierCount(t *testing.T) {
	events := make([]api.JourneyEvent, 35)
	for i := range events {
		events[i] = api.JourneyEvent{Kind: "ready", Summary: fmt.Sprintf("event-%d", i)}
	}
	stub := &journeyStubClient{events: events}
	tab := newTickets(stub, "")
	tab.width, tab.height = 120, 40
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{data: &api.TicketIssueRunPage{
		Items: []api.TicketIssueRunSummary{{
			ID: 1, TicketID: 456, ModelName: "helpdesk.ticket", Status: "completed",
			GithubURL: "https://github.com/acme/widgets",
			Issues:    []api.TicketIssueRef{{Number: intPtr(1), Title: "x"}},
			CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab = onRow(tab, 456)
	tab, cmd := tab.update(keyMsg("enter"))
	if cmd == nil {
		t.Fatal("enter did not return a journey fetch cmd")
	}
	tab, _ = tab.update(cmd().(ticketJourneyLoadedMsg))

	out := tab.view(120, 40)
	if !strings.Contains(out, "(+5 earlier)") {
		t.Fatalf("view missing the truncation head line, got:\n%s", out)
	}
	if strings.Contains(out, "event-4\n") || strings.Contains(out, "event-4 ") {
		t.Fatalf("view rendered a truncated (earlier-than-last-30) event:\n%s", out)
	}
	if !strings.Contains(out, "event-5") {
		t.Fatalf("view missing the oldest of the last 30 events:\n%s", out)
	}
	if !strings.Contains(out, "event-34") {
		t.Fatalf("view missing the most recent event:\n%s", out)
	}
}

func TestJourneyHeightBudgetFoldsOverflowAndFitsBudget(t *testing.T) {
	// Regression: detailView appended the (<=30-event) Journey block
	// unconditionally, so App.View's MaxHeight clamp (which cuts the BOTTOM)
	// silently clipped the newest events — including "ready" — instead of
	// the oldest. The block must budget against whatever height is left
	// after the issues list, folding overflow the same way the >30 cap does.
	events := make([]api.JourneyEvent, 8)
	for i := range events {
		events[i] = api.JourneyEvent{Kind: "ready", Summary: fmt.Sprintf("event-%d", i)}
	}
	stub := &journeyStubClient{events: events}
	tab := newTickets(stub, "")
	tab.width, tab.height = 60, 12
	tab, _ = tab.update(ticketIssueRunsLoadedMsg{data: &api.TicketIssueRunPage{
		Items: []api.TicketIssueRunSummary{{
			ID: 1, TicketID: 456, ModelName: "helpdesk.ticket", Status: "completed",
			GithubURL: "https://github.com/acme/widgets",
			Issues: []api.TicketIssueRef{
				{Number: intPtr(1), Title: "a"},
				{Number: intPtr(2), Title: "b"},
				{Number: intPtr(3), Title: "c"},
			},
			CreatedAt: time.Now(),
		}},
		Total: 1,
	}})
	tab = onRow(tab, 456)
	tab, cmd := tab.update(keyMsg("enter"))
	if cmd == nil {
		t.Fatal("enter did not return a journey fetch cmd")
	}
	tab, _ = tab.update(cmd().(ticketJourneyLoadedMsg))

	const h = 12
	out := tab.view(60, h)
	if lines := strings.Count(out, "\n") + 1; lines > h {
		t.Fatalf("output is %d lines, want <= %d (budget):\n%s", lines, h, out)
	}
	if !strings.Contains(out, "event-7") {
		t.Fatalf("view missing the last (newest) event, got:\n%s", out)
	}
	if strings.Contains(out, "event-0") {
		t.Fatalf("view rendered the earliest event, which should have been folded:\n%s", out)
	}
	if !strings.Contains(out, "(+6 earlier)") {
		t.Fatalf("view missing the height-folded count, got:\n%s", out)
	}
}

func TestJourneyErrorRendersUnavailableLine(t *testing.T) {
	tab := ticketsWithData()
	tab = onRow(tab, 456)
	tab, _ = tab.update(keyMsg("enter"))

	tab, _ = tab.update(ticketJourneyLoadedMsg{key: tab.detailKey, err: errFake})
	if tab.journeyErr == "" {
		t.Fatal("journeyErr not set from an error response")
	}
	out := tab.view(120, 30)
	if !strings.Contains(out, "Journey unavailable: boom") {
		t.Fatalf("view missing the journey-unavailable line, got:\n%s", out)
	}
	if strings.Contains(out, "Journey\n") {
		t.Fatal("view rendered the Journey title alongside an error")
	}
}
