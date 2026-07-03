package ui

import (
	"fmt"
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
	filtering bool            // capturing the `/` filter text
	filter    string          // case-insensitive substring on ticket id / model
	expanded  map[string]bool // repo keys whose group is unfolded (default: collapsed)
	// Issue drill-down: the full issue list for the selected ticket's run,
	// plus its parent ("epic") issue if the run synthesized one (else nil).
	detail          bool
	detailIssues    []api.TicketIssueRef
	detailParent    *api.TicketIssueRef
	detailIssueType string
	detailCursor    int
	detailOffset    int
}

func newTickets(client api.ClientIface, odooURL string) Tickets {
	return Tickets{client: client, odooURL: odooURL, loading: true, expanded: map[string]bool{}}
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

// filteredRows returns the union rows matching the active `/` filter
// (case-insensitive substring on "#<id> <model>"), or all rows when unset.
func (t Tickets) filteredRows() []ticketRow {
	if t.filter == "" {
		return t.rows
	}
	q := strings.ToLower(t.filter)
	out := make([]ticketRow, 0, len(t.rows))
	for _, row := range t.rows {
		hay := strings.ToLower(fmt.Sprintf("#%d %s", row.ticketID, row.modelName))
		if strings.Contains(hay, q) {
			out = append(out, row)
		}
	}
	return out
}

// repoKey is the grouping key for a row: "owner/repo" parsed from its
// create-issues run's github_url, or "" when the ticket has no run yet
// (analysis-only — it carries no repo).
func (t Tickets) repoKey(r ticketRow) string {
	if r.issueRun == nil || r.issueRun.GithubURL == "" {
		return ""
	}
	owner, name, ok := parseOwnerName(r.issueRun.GithubURL)
	if !ok {
		return ""
	}
	return owner + "/" + name
}

// groupedRows clusters the filtered rows by repo. Groups appear most-recently-
// active first (rows arrive newest-first, so first-seen == most-recent); the
// "no repo yet" bucket (analysis-only tickets) always sorts last.
func (t Tickets) groupedRows() []ticketRow {
	rows := t.filteredRows()
	var order []string
	buckets := map[string][]ticketRow{}
	for _, r := range rows {
		k := t.repoKey(r)
		if _, ok := buckets[k]; !ok {
			order = append(order, k)
		}
		buckets[k] = append(buckets[k], r)
	}
	out := make([]ticketRow, 0, len(rows))
	for _, k := range order {
		if k != "" {
			out = append(out, buckets[k]...)
		}
	}
	return append(out, buckets[""]...)
}

// ticketItem is one selectable line in the grouped list: a repo group header or
// a ticket row under it. The cursor moves over these — headers are selectable so
// they can be folded. Each item renders as exactly one display line, so the
// cursor index doubles as the display line (simple windowing).
type ticketItem struct {
	header bool
	key    string    // repo key; the row's group for rows, the group for headers
	count  int       // tickets in the group (headers only)
	row    ticketRow // valid when !header
}

// visibleItems flattens the grouped rows into selectable lines: every group
// header, plus the rows of expanded groups. Groups are collapsed by default
// (absent from `expanded`), so a fresh tab shows just one line per repo.
func (t Tickets) visibleItems() []ticketItem {
	grouped := t.groupedRows()
	sizes := map[string]int{}
	for _, r := range grouped {
		sizes[t.repoKey(r)]++
	}
	var items []ticketItem
	prev := "\x00"
	for _, r := range grouped {
		k := t.repoKey(r)
		if k != prev {
			items = append(items, ticketItem{header: true, key: k, count: sizes[k]})
			prev = k
		}
		if t.expanded[k] {
			items = append(items, ticketItem{key: k, row: r})
		}
	}
	return items
}

// headerIndexOf finds the header line for key (-1 if absent) — used to park the
// cursor on a group's header after folding it.
func headerIndexOf(items []ticketItem, key string) int {
	for i, it := range items {
		if it.header && it.key == key {
			return i
		}
	}
	return -1
}

// groupKeys lists the distinct repo keys in display order (for collapse/expand
// all).
func (t Tickets) groupKeys() []string {
	var keys []string
	for _, it := range t.visibleItems() {
		if it.header {
			keys = append(keys, it.key)
		}
	}
	return keys
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
		// Filter-input mode (main list only): capture keys for the `/` filter.
		if t.filtering {
			var changed bool
			t.filtering, t.filter, changed = applyFilterKey(m, t.filter)
			if changed {
				t.cursor, t.offset = 0, 0
			}
			return t, nil
		}

		t.statusMsg = ""
		visibleRows := t.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}

		// Issue drill-down: navigate/open the selected ticket's issues.
		if t.detail {
			detailRows := t.height - 4 // header + blank + blank + pos
			if detailRows < 1 {
				detailRows = 1
			}
			if c, o, ok := listNav(m.String(), t.detailCursor, t.detailOffset, len(t.detailIssues), detailRows); ok {
				t.detailCursor, t.detailOffset = c, o
				return t, nil
			}
			switch m.String() {
			case "esc", "left", "h":
				t.detail = false
			case "o", "enter":
				if t.detailCursor < len(t.detailIssues) {
					ref := t.detailIssues[t.detailCursor]
					if ref.URL != nil {
						openInBrowser(*ref.URL)
					}
				}
			}
			return t, nil
		}

		// Items (group headers + rows of expanded groups) are one line each, so
		// the cursor index is the display line — plain cursor/offset windowing.
		items := t.visibleItems()
		if c, o, ok := listNav(m.String(), t.cursor, t.offset, len(items), visibleRows); ok {
			t.cursor, t.offset = c, o
			return t, nil
		}
		var cur ticketItem
		if t.cursor >= 0 && t.cursor < len(items) {
			cur = items[t.cursor]
		}
		// park keeps the cursor on a group's header (and the window) after a fold.
		park := func(key string) {
			vi := t.visibleItems()
			if idx := headerIndexOf(vi, key); idx >= 0 {
				t.cursor = idx
			}
			t.offset = ensureVisible(t.offset, t.cursor, visibleRows, len(vi))
		}
		switch m.String() {
		case "/":
			t.filtering = true
		case "enter":
			if cur.header {
				t.expanded[cur.key] = !t.expanded[cur.key]
				park(cur.key)
			} else if cur.row.issueRun != nil && len(cur.row.issueRun.Issues) > 0 {
				t.detail = true
				t.detailIssues = cur.row.issueRun.Issues
				t.detailParent = cur.row.issueRun.ParentIssue
				t.detailIssueType = ""
				if cur.row.issueRun.IssueType != nil {
					t.detailIssueType = *cur.row.issueRun.IssueType
				}
				t.detailCursor, t.detailOffset = 0, 0
			} else {
				t.statusMsg = "no GitHub issues for this ticket"
			}
		case " ":
			t.expanded[cur.key] = !t.expanded[cur.key]
			park(cur.key)
		case "h", "left":
			t.expanded[cur.key] = false
			park(cur.key)
		case "l", "right":
			t.expanded[cur.key] = true
			park(cur.key)
		case "z":
			// Toggle all: expand every group if any is collapsed, else collapse all.
			expandAll := false
			for _, k := range t.groupKeys() {
				if !t.expanded[k] {
					expandAll = true
					break
				}
			}
			for _, k := range t.groupKeys() {
				t.expanded[k] = expandAll
			}
			park(cur.key)
		case "r":
			t.loading = true
			return t, t.load()
		case "e":
			if !cur.header {
				if cur.row.analysis == nil {
					t.statusMsg = "no analysis to requeue for this ticket"
				} else if cur.row.analysis.Status == "failed" || cur.row.analysis.Status == "completed" {
					return t, t.requeueCmd(cur.row.analysis.ID)
				} else {
					t.statusMsg = "only failed or completed analyses can be requeued"
				}
			}
		case "o":
			if cur.header {
				if cur.key != "" {
					openInBrowser("https://github.com/" + cur.key)
				}
			} else if cur.row.ticketID != 0 {
				// Guard against a zero-value cur (empty/collapsed list), which
				// would otherwise open "<odoo>/web#model=&id=0&view_type=form".
				url := fmt.Sprintf("%s/web#model=%s&id=%d&view_type=form",
					t.odooURL, cur.row.modelName, cur.row.ticketID)
				openInBrowser(url)
			}
		}
	}
	return t, nil
}

func (t Tickets) view(w, h int) string {
	if t.detail {
		return t.detailView(w, h)
	}

	grouped := t.groupedRows()
	items := t.visibleItems()
	title := fmt.Sprintf("Tickets  (%d)", len(t.rows))
	if t.filter != "" {
		title = fmt.Sprintf("Tickets  (%d/%d)", len(grouped), len(t.rows))
	}
	header := styleTitle.Padding(0, 1).Render(title)

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
	if len(grouped) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No tickets match \""+t.filter+"\"  ( / edit · esc clear )")))
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

	// rowText renders one ticket row (selected = highlighted).
	rowText := func(row ticketRow, selected bool) string {
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

		if selected {
			return styleSelected.Width(w - 2).Render(
				fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
					colTicket, ticket,
					colModule, module,
					colAnalysis, analysisPlain,
					colIssues, issuesPlain,
					colCost, cost,
					colWhen, when),
			)
		}
		// analysisColored/issuesColored carry ANSI codes, so they must be padded
		// by visible width (padCell), not %-*s — otherwise the escape bytes count
		// toward the field and the later columns shift.
		return fmt.Sprintf("  %-*s  %-*s  %s  %s  %-*s  %-*s",
			colTicket, ticket,
			colModule, module,
			padCell(analysisColored, colAnalysis),
			padCell(issuesColored, colIssues),
			colCost, cost,
			colWhen, when,
		)
	}

	var cur ticketItem
	if t.cursor >= 0 && t.cursor < len(items) {
		cur = items[t.cursor]
	}

	groups := 0
	for _, it := range items {
		if it.header {
			groups++
		}
	}
	pos := styleSubtitle.Render(fmt.Sprintf("  %d tickets · %d repos  (enter/space fold · z fold all)", len(grouped), groups))
	if t.filter != "" && !t.filtering {
		pos = styleSubtitle.Render(fmt.Sprintf("  filter %q  ", t.filter)) + pos
	}
	if t.filtering {
		pos = "  " + styleTitle.Render(" filter ") + "  " + t.filter + "█" +
			styleSubtitle.Render("    [enter] keep   [esc] clear")
	}

	var extras []string
	if !cur.header {
		sel := cur.row
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

	// Each item is one display line, so the cursor index is the line. Window to
	// what's left after header+blank+colHdr+blank+pos and the extras.
	budget := h - 5 - len(extras)
	if budget < 1 {
		budget = 1
	}
	off := ensureVisible(t.offset, t.cursor, budget, len(items))
	end := off + budget
	if end > len(items) {
		end = len(items)
	}

	body := []string{hdr}
	for i := off; i < end; i++ {
		it := items[i]
		if !it.header {
			body = append(body, rowText(it.row, i == t.cursor))
			continue
		}
		arrow := "▸ "
		if t.expanded[it.key] {
			arrow = "▾ "
		}
		label := it.key
		if label == "" {
			label = "(no repo yet)"
		}
		if i == t.cursor {
			body = append(body, styleSelected.Width(w-2).Render(
				fmt.Sprintf("%s%s  (%d)", arrow, label, it.count)))
		} else {
			body = append(body, styleStatusCompleted.Render(arrow)+
				styleTitle.Render(label)+
				styleSubtitle.Render(fmt.Sprintf("  (%d)", it.count)))
		}
	}
	table := strings.Join(body, "\n")

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
	label := fmt.Sprintf("GitHub Issues  (%d created / %d planned)", created, len(t.detailIssues))
	if t.detailIssueType != "" {
		label += "  · type " + t.detailIssueType
	}
	header := styleTitle.Padding(0, 1).Render(label)

	// Window the issue list around the cursor so a long list scrolls.
	vis := h - 4 // header + blank + blank + pos
	if vis < 1 {
		vis = 1
	}
	off := clampOffset(t.detailOffset, len(t.detailIssues), vis)
	end := off + vis
	if end > len(t.detailIssues) {
		end = len(t.detailIssues)
	}

	var rows []string
	for i := off; i < end; i++ {
		ref := t.detailIssues[i]
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
	if sh := scrollHint(off, vis, len(t.detailIssues)); sh != "" {
		pos += sh
	}

	parts := []string{header, ""}
	// When the run grouped its issues under a parent ("epic") issue, surface it
	// as one muted line above the list.
	if p := t.detailParent; p != nil && p.Number != nil {
		parts = append(parts, styleSubtitle.Render(
			truncate(fmt.Sprintf("  Epic: #%d %s", *p.Number, p.Title), w-2)))
	}
	parts = append(parts, body, "", pos)
	return lipgloss.JoinVertical(lipgloss.Left, parts...)
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
