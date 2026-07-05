package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Timesheets struct {
	client  api.ClientIface
	items   []api.TimesheetReviewSummary
	total   int
	err     error
	loading bool
	cursor  int
	offset  int
	width   int
	height  int
}

func newTimesheets(client api.ClientIface) Timesheets {
	return Timesheets{client: client, loading: true}
}

func (t Timesheets) load() tea.Cmd {
	return func() tea.Msg {
		data, err := t.client.TimesheetReviews(100)
		return timesheetsLoadedMsg{data: data, err: err}
	}
}

func (t Timesheets) update(msg tea.Msg) (Timesheets, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return t, t.load()
	case timesheetsLoadedMsg:
		t.loading = false
		t.err = m.err
		if m.data != nil {
			t.items = m.data.Items
			t.total = m.data.Total
		}
		if t.cursor >= len(t.items) {
			t.cursor, t.offset = 0, 0
		}
	case tea.KeyMsg:
		visibleRows := t.height - 10
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), t.cursor, t.offset, len(t.items), visibleRows); ok {
			t.cursor, t.offset = c, o
			return t, nil
		}
		switch m.String() {
		case "r", "R":
			t.loading = true
			return t, t.load()
		}
	}
	return t, nil
}

func (t Timesheets) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Timesheet Reviews  (%d)", t.total))
	if t.loading && len(t.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if t.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+t.err.Error()))
	}
	if len(t.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No timesheet reviews yet")))
	}

	colID, colStatus, colLines, colChanges, colCost := 6, 12, 9, 16, 10
	colReq := w - colID - colStatus - colLines - colChanges - colCost - 18
	if colReq < 16 {
		colReq = 16
	}
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colID, "Run",
			colStatus, "Status",
			colReq, "Request",
			colLines, "Lines",
			colChanges, "Changes",
			colCost, "Cost"),
	)

	visibleRows := h - 10
	if visibleRows < 1 {
		visibleRows = 1
	}
	off := ensureVisible(t.offset, t.cursor, visibleRows, len(t.items))
	end := off + visibleRows
	if end > len(t.items) {
		end = len(t.items)
	}

	rows := []string{hdr}
	for i := off; i < end; i++ {
		item := t.items[i]
		statusPlain := item.Status
		statusColored := timesheetStatusStyle(item.Status).Render(item.Status)
		changes := fmt.Sprintf("%d rw / %d human", item.RewrittenCount, item.NeedsHumanCount)
		cost := ""
		if item.EstimatedCostUSD != nil {
			cost = fmt.Sprintf("$%.4f", *item.EstimatedCostUSD)
		}
		line := fmt.Sprintf("  %-*s  %-*s  %-*s  %-*d  %-*s  %-*s",
			colID, fmt.Sprintf("#%d", item.ID),
			colStatus, statusPlain,
			colReq, truncate(item.RequestID, colReq),
			colLines, item.TotalLines,
			colChanges, changes,
			colCost, cost,
		)
		if i == t.cursor {
			rows = append(rows, styleSelected.Width(w-2).Render(line))
			continue
		}
		rows = append(rows, fmt.Sprintf("  %-*s  %s  %-*s  %-*d  %-*s  %-*s",
			colID, fmt.Sprintf("#%d", item.ID),
			padCell(statusColored, colStatus),
			colReq, truncate(item.RequestID, colReq),
			colLines, item.TotalLines,
			colChanges, changes,
			colCost, cost,
		))
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", t.cursor+1, len(t.items)))
	detail := ""
	if t.cursor < len(t.items) {
		detail = t.detail(t.items[t.cursor], w)
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", strings.Join(rows, "\n"), "", detail, "", pos)
}

func (t Timesheets) detail(item api.TimesheetReviewSummary, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s", item.ID, item.RequestID)) + "\n")
	b.WriteString(fmt.Sprintf("  Status    %s\n", timesheetStatusStyle(item.Status).Render(item.Status)))
	b.WriteString(fmt.Sprintf("  Counts    %d ok / %d rewritten / %d needs human\n",
		item.OkCount, item.RewrittenCount, item.NeedsHumanCount))
	b.WriteString(fmt.Sprintf("  Created   %s\n", relativeTime(item.CreatedAt)))
	if item.CompletedAt != nil {
		b.WriteString(fmt.Sprintf("  Done      %s\n", relativeTime(*item.CompletedAt)))
	}
	if item.CallbackSentAt != nil {
		b.WriteString(fmt.Sprintf("  Callback  %s\n", relativeTime(*item.CallbackSentAt)))
	}
	if item.ErrorMessage != nil && *item.ErrorMessage != "" {
		b.WriteString(styleStatusFailed.Render(truncate("  Error     "+*item.ErrorMessage, w-4)) + "\n")
	}
	return styleBorder.Width(w - 2).Height(7).Render(b.String())
}

func timesheetStatusStyle(status string) lipgloss.Style {
	switch status {
	case "completed":
		return styleStatusCompleted
	case "failed":
		return styleStatusFailed
	case "pending":
		return styleStatusStale
	default:
		return styleStatusOther
	}
}
