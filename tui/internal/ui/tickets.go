package ui

import (
	"fmt"
	"os/exec"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

// ticketRow is one Odoo record in the Tickets tab — it may have a REVA
// analysis, a create-issues run, or both. The tab is the union of the two
// feeds so a ticket that only had issues created (never analyzed) still shows.
type ticketRow struct {
	modelName string
	ticketID  int
	analysis  *api.TicketAnalysisSummary
	issueRun  *api.TicketIssueRunSummary
	activity  time.Time // most recent of the two, for ordering
}

type Tickets struct {
	client   api.ClientIface
	odooURL  string
	analyses []api.TicketAnalysisSummary
	// Latest create-issues run per record, keyed by "<model_name>#<ticket_id>".
	issueRuns map[string]api.TicketIssueRunSummary
	rows      []ticketRow // union of analyses + issueRuns, newest first
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string
	// Issue drill-down: the full issue list for the selected ticket's run.
	detail       bool
	detailIssues []api.TicketIssueRef
	detailCursor int
}

func newTickets(client api.ClientIface, odooURL string) Tickets {
	return Tickets{client: client, odooURL: odooURL, loading: true}
}

func issueRunKey(modelName string, ticketID int) string {
	return fmt.Sprintf("%s#%d", modelName, ticketID)
}

func (t Tickets) load() tea.Cmd {
	return tea.Batch(
		func() tea.Msg {
			data, err := t.client.TicketAnalyses(100)
			return ticketAnalysesLoadedMsg{data: data, err: err}
		},
		func() tea.Msg {
			data, err := t.client.TicketIssueRuns(100)
			return ticketIssueRunsLoadedMsg{data: data, err: err}
		},
	)
}

// rebuildRows recomputes the union from the two feeds, newest activity first.
func (t *Tickets) rebuildRows() {
	byKey := map[string]*ticketRow{}
	var order []string
	get := func(model string, id int) *ticketRow {
		k := issueRunKey(model, id)
		r, ok := byKey[k]
		if !ok {
			r = &ticketRow{modelName: model, ticketID: id}
			byKey[k] = r
			order = append(order, k)
		}
		return r
	}
	for i := range t.analyses {
		a := &t.analyses[i]
		r := get(a.ModelName, a.TicketID)
		r.analysis = a
		if a.CreatedAt.After(r.activity) {
			r.activity = a.CreatedAt
		}
	}
	for k := range t.issueRuns {
		run := t.issueRuns[k]
		r := get(run.ModelName, run.TicketID)
		rc := run // map value copy; take a stable address
		r.issueRun = &rc
		if run.CreatedAt.After(r.activity) {
			r.activity = run.CreatedAt
		}
	}
	rows := make([]ticketRow, 0, len(order))
	for _, k := range order {
		rows = append(rows, *byKey[k])
	}
	sort.Slice(rows, func(i, j int) bool {
		if !rows[i].activity.Equal(rows[j].activity) {
			return rows[i].activity.After(rows[j].activity)
		}
		return rows[i].ticketID > rows[j].ticketID
	})
	t.rows = rows
	if t.cursor >= len(t.rows) {
		t.cursor = 0
		t.offset = 0
	}
}

func (t Tickets) requeueCmd(id int) tea.Cmd {
	return func() tea.Msg {
		err := t.client.RequeueTicket(id)
		return ticketRequeuedMsg{id: id, err: err}
	}
}

func (t Tickets) update(msg tea.Msg) (Tickets, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return t, t.load()

	case ticketAnalysesLoadedMsg:
		t.loading = false
		t.err = m.err
		if m.data != nil {
			t.analyses = m.data.Items
		}
		t.rebuildRows()

	case ticketIssueRunsLoadedMsg:
		// Issue runs are auxiliary — an error here must not blank the tab.
		if m.err == nil && m.data != nil {
			runs := make(map[string]api.TicketIssueRunSummary, len(m.data.Items))
			for _, run := range m.data.Items {
				key := issueRunKey(run.ModelName, run.TicketID)
				if _, seen := runs[key]; !seen { // feed is newest-first
					runs[key] = run
				}
			}
			t.issueRuns = runs
			t.rebuildRows()
		}

	case ticketRequeuedMsg:
		if m.err != nil {
			t.statusMsg = fmt.Sprintf("requeue failed: %s", m.err)
		} else {
			t.statusMsg = fmt.Sprintf("analysis #%d requeued", m.id)
		}

	case tea.KeyMsg:
		t.statusMsg = ""
		visibleRows := t.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}

		// Issue drill-down: navigate/open the selected ticket's issues.
		if t.detail {
			switch m.String() {
			case "esc", "left", "h":
				t.detail = false
			case "j", "down":
				if t.detailCursor < len(t.detailIssues)-1 {
					t.detailCursor++
				}
			case "k", "up":
				if t.detailCursor > 0 {
					t.detailCursor--
				}
			case "o", "enter":
				if t.detailCursor < len(t.detailIssues) {
					ref := t.detailIssues[t.detailCursor]
					if ref.URL != nil && *ref.URL != "" {
						_ = exec.Command("xdg-open", *ref.URL).Start()
					}
				}
			}
			return t, nil
		}

		switch m.String() {
		case "j", "down":
			t.cursor, t.offset = moveCursor(t.cursor, t.offset, len(t.rows), visibleRows, true)
		case "k", "up":
			t.cursor, t.offset = moveCursor(t.cursor, t.offset, len(t.rows), visibleRows, false)
		case "enter":
			if t.cursor < len(t.rows) {
				row := t.rows[t.cursor]
				if row.issueRun != nil && len(row.issueRun.Issues) > 0 {
					t.detail = true
					t.detailIssues = row.issueRun.Issues
					t.detailCursor = 0
				} else {
					t.statusMsg = "no GitHub issues for this ticket"
				}
			}
		case "r":
			t.loading = true
			return t, t.load()
		case "e":
			if t.cursor < len(t.rows) {
				row := t.rows[t.cursor]
				if row.analysis == nil {
					t.statusMsg = "no analysis to requeue for this ticket"
				} else if row.analysis.Status == "failed" || row.analysis.Status == "completed" {
					return t, t.requeueCmd(row.analysis.ID)
				} else {
					t.statusMsg = "only failed or completed analyses can be requeued"
				}
			}
		case "o":
			if t.cursor < len(t.rows) {
				row := t.rows[t.cursor]
				url := fmt.Sprintf("%s/web#model=%s&id=%d&view_type=form",
					t.odooURL, row.modelName, row.ticketID)
				_ = exec.Command("xdg-open", url).Start()
			}
		}
	}
	return t, nil
}

func (t Tickets) view(w, h int) string {
	if t.detail {
		return t.detailView(w, h)
	}

	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Tickets  (%d)", len(t.rows)))

	if t.loading && len(t.rows) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if t.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+t.err.Error()))
	}
	if len(t.rows) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No tickets")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	colTicket := 9
	colModule := 10
	colAnalysis := 12
	colIssues := 12
	colCost := 10
	colWhen := w - colTicket - colModule - colAnalysis - colIssues - colCost - 14

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colTicket, "Ticket",
			colModule, "Module",
			colAnalysis, "Analysis",
			colIssues, "Issues",
			colCost, "Cost",
			colWhen, "When"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := t.offset + visibleRows
	if end > len(t.rows) {
		end = len(t.rows)
	}
	for i := t.offset; i < end; i++ {
		row := t.rows[i]
		ticket := fmt.Sprintf("#%d", row.ticketID)
		module := truncate(strings.SplitN(row.modelName, ".", 2)[0], colModule)

		analysisPlain, analysisColored := "—", styleStatusOther.Render("—")
		cost := ""
		if a := row.analysis; a != nil {
			analysisPlain = strings.TrimSpace(plainStatusSymbol(a.Status, a.CreatedAt) + " " + a.Status)
			analysisColored = strings.TrimSpace(ticketStatusSymbol(a.Status, a.CreatedAt) + " " + a.Status)
			if a.EstimatedCostUSD != nil {
				cost = fmt.Sprintf("$%.4f", *a.EstimatedCostUSD)
			}
		}

		issuesPlain, issuesColored := "—", styleStatusOther.Render("—")
		if run := row.issueRun; run != nil {
			counts := issueRunCounts(*run)
			issuesPlain = strings.TrimSpace(plainStatusSymbol(run.Status, run.CreatedAt) + " " + counts)
			issuesColored = strings.TrimSpace(ticketStatusSymbol(run.Status, run.CreatedAt) + " " + counts)
			if cost == "" && run.EstimatedCostUSD != nil {
				cost = fmt.Sprintf("$%.4f", *run.EstimatedCostUSD)
			}
		}

		when := truncate(relativeTime(row.activity), colWhen)

		if i == t.cursor {
			rows = append(rows, styleSelected.Width(w-2).Render(
				fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
					colTicket, ticket,
					colModule, module,
					colAnalysis, analysisPlain,
					colIssues, issuesPlain,
					colCost, cost,
					colWhen, when),
			))
		} else {
			rows = append(rows, fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
				colTicket, ticket,
				colModule, module,
				colAnalysis, analysisColored,
				colIssues, issuesColored,
				colCost, cost,
				colWhen, when,
			))
		}
	}

	table := strings.Join(rows, "\n")
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", t.cursor+1, len(t.rows)))

	var extras []string
	if t.cursor < len(t.rows) {
		sel := t.rows[t.cursor]
		if a := sel.analysis; a != nil {
			var meta []string
			meta = append(meta, fmt.Sprintf("analysis #%d", a.ID), "field:"+a.FieldName)
			if a.InputTokens != nil && a.OutputTokens != nil {
				meta = append(meta, fmt.Sprintf("tokens:%d in / %d out", *a.InputTokens, *a.OutputTokens))
			}
			extras = append(extras, styleSubtitle.Render("  "+strings.Join(meta, "  ")))
			if a.ErrorMessage != nil && *a.ErrorMessage != "" {
				extras = append(extras, styleStatusFailed.Render(truncate("  analysis error: "+*a.ErrorMessage, w-2)))
			}
		}
		if run := sel.issueRun; run != nil {
			line := "issues: " + run.Status
			if refs := issueRefList(run.Issues); refs != "" {
				line += " — " + refs
			}
			extras = append(extras, styleSubtitle.Render(truncate("  "+line, w-2)))
			if run.Status == "failed" && run.ErrorMessage != nil && *run.ErrorMessage != "" {
				extras = append(extras, styleStatusFailed.Render(
					truncate("  issues error: "+*run.ErrorMessage, w-2)))
			}
		}
	}
	if t.statusMsg != "" {
		extras = append(extras, styleSubtitle.Render("  "+t.statusMsg))
	}

	parts := []string{header, "", table, "", pos}
	parts = append(parts, extras...)
	return lipgloss.JoinVertical(lipgloss.Left, parts...)
}

// detailView lists every issue of the selected ticket's run, one per line:
// number, open/done state, and title — far more readable than the cramped
// summary line, and 'o' opens the highlighted issue on GitHub.
func (t Tickets) detailView(w, h int) string {
	created := 0
	for _, ref := range t.detailIssues {
		if ref.Number != nil {
			created++
		}
	}
	header := styleTitle.Padding(0, 1).Render(
		fmt.Sprintf("GitHub Issues  (%d created / %d planned)", created, len(t.detailIssues)))

	var rows []string
	for i, ref := range t.detailIssues {
		num := "  —"
		if ref.Number != nil {
			num = fmt.Sprintf("#%d", *ref.Number)
		}
		state := "planned"
		switch {
		case ref.Number == nil:
			state = "not created"
		case ref.State != nil && *ref.State == "closed":
			state = "done ✓"
		case ref.State != nil:
			state = *ref.State
		}
		line := fmt.Sprintf("  %-6s  %-12s  %s", num, state, ref.Title)
		if i == t.detailCursor {
			rows = append(rows, styleSelected.Width(w-2).Render(truncate(line, w-4)))
		} else {
			rows = append(rows, truncate(line, w-2))
		}
	}

	body := strings.Join(rows, "\n")
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", t.detailCursor+1, len(t.detailIssues)))
	return lipgloss.JoinVertical(lipgloss.Left, header, "", body, "", pos)
}

func ticketStatusSymbol(status string, createdAt time.Time) string {
	switch status {
	case "completed":
		return styleStatusCompleted.Render("+")
	case "failed":
		return styleStatusFailed.Render("x")
	case "pending":
		if time.Since(createdAt) > 10*time.Minute {
			return styleStatusFailed.Render("?")
		}
		return styleStatusStale.Render("~")
	default:
		return styleStatusOther.Render("-")
	}
}

// plainStatusSymbol is ticketStatusSymbol without color — for cells rendered
// inside styleSelected, where ANSI codes would break the column alignment.
func plainStatusSymbol(status string, createdAt time.Time) string {
	switch status {
	case "completed":
		return "+"
	case "failed":
		return "x"
	case "pending":
		if time.Since(createdAt) > 10*time.Minute {
			return "?"
		}
		return "~"
	default:
		return "-"
	}
}

// issueRunCounts renders the created-issue count of a run: "3" when all
// created, "1/4" when a failed run created only part of its plan.
func issueRunCounts(run api.TicketIssueRunSummary) string {
	created := 0
	for _, ref := range run.Issues {
		if ref.Number != nil {
			created++
		}
	}
	if len(run.Issues) == 0 {
		return ""
	}
	if created == len(run.Issues) {
		return fmt.Sprintf("%d", created)
	}
	return fmt.Sprintf("%d/%d", created, len(run.Issues))
}

// issueRefList renders the run's issues as "#42 Title ✓ · #43 Title"; closed
// (done) issues get a check mark, planned but not-yet-created items show
// without a number.
func issueRefList(refs []api.TicketIssueRef) string {
	var bits []string
	for _, ref := range refs {
		if ref.Number != nil {
			bit := fmt.Sprintf("#%d %s", *ref.Number, ref.Title)
			if ref.State != nil && *ref.State == "closed" {
				bit += " ✓"
			}
			bits = append(bits, bit)
		} else {
			bits = append(bits, "(not created) "+ref.Title)
		}
	}
	return strings.Join(bits, " · ")
}
