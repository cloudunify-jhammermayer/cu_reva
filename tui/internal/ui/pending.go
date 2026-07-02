package ui

import (
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Pending struct {
	client  api.ClientIface
	items   []api.PendingReview
	total   int
	err     error
	loading bool
	cursor  int
	offset  int
	width   int
	height  int
}

func newPending(client api.ClientIface) Pending {
	return Pending{client: client, loading: true}
}

func (p Pending) load() tea.Cmd {
	return func() tea.Msg {
		data, err := p.client.Pending()
		return pendingLoadedMsg{data: data, err: err}
	}
}

func (p Pending) update(msg tea.Msg) (Pending, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return p, p.load()

	case pendingLoadedMsg:
		p.loading = false
		p.err = m.err
		if m.data != nil {
			p.items = m.data.Items
			p.total = m.data.Total
		}
		if p.cursor >= len(p.items) {
			p.cursor = 0
			p.offset = 0
		}

	case tea.KeyMsg:
		visibleRows := p.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), p.cursor, p.offset, len(p.items), visibleRows); ok {
			p.cursor, p.offset = c, o
			return p, nil
		}
		switch m.String() {
		case "r":
			p.loading = true
			return p, p.load()
		}
	}
	return p, nil
}

func (p Pending) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Pending / Running  (%d)", p.total))

	if p.loading && len(p.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if p.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+p.err.Error()))
	}
	if len(p.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No pending or running reviews")))
	}

	// Reserve the fixed chrome around the table: header + blank + table
	// column-header + blank + 8-line detail panel + blank + position line = 14
	// non-data lines. Was h-5, which overran by the detail panel + position line
	// when the list filled the table, so the MaxHeight clamp cut them off (M23).
	// If the terminal is too short for the detail panel plus a few rows, drop the
	// detail so the list and position line still fit.
	showDetail := true
	visibleRows := h - 14
	if visibleRows < 3 {
		showDetail = false
		visibleRows = h - 5 // compact: header + blank + table hdr + blank + pos
	}
	if visibleRows < 1 {
		visibleRows = 1
	}

	colStatus := 9
	colRepo := 26
	colPR := 6
	colEvent := 12
	colWhen := w - colStatus - colRepo - colPR - colEvent - 10

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colStatus, "Status",
			colRepo, "Repository",
			colPR, "PR#",
			colEvent, "Trigger",
			colWhen, "When"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := p.offset + visibleRows
	if end > len(p.items) {
		end = len(p.items)
	}
	for i := p.offset; i < end; i++ {
		item := p.items[i]
		repo := truncate(item.RepoFullName, colRepo)
		prNum := fmt.Sprintf("#%d", item.PRNumber)
		event := truncate(item.TriggerEvent, colEvent)

		var line string
		if i == p.cursor {
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
				colStatus, item.Status,
				colRepo, repo,
				colPR, prNum,
				colEvent, event,
				colWhen, formatPendingTimePlain(item),
			))
		} else {
			// The status cell is colored (ANSI), so pad it by visible width
			// (padCell) instead of %-*s, or the escape bytes shift every column
			// after it. formatPendingTime is also colored but is the last column.
			line = fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				padCell(pendingStatusStyle(item.Status).Render(item.Status), colStatus),
				colRepo, repo,
				colPR, prNum,
				colEvent, event,
				colWhen, formatPendingTime(item),
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", p.cursor+1, len(p.items)))
	if showDetail && p.cursor < len(p.items) {
		detail := p.renderDetail(p.items[p.cursor], w)
		return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", detail, "", pos)
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}

func (p Pending) renderDetail(item api.PendingReview, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s", item.PRNumber, truncate(item.PRTitle, w-20))) + "\n")
	b.WriteString(fmt.Sprintf("  Repo     %s\n", item.RepoFullName))
	b.WriteString(fmt.Sprintf("  SHA      %s\n", styleSubtitle.Render(shortSHA(item.HeadSHA))))
	b.WriteString(fmt.Sprintf("  Trigger  %s\n", styleSubtitle.Render(item.TriggerEvent)))
	b.WriteString(fmt.Sprintf("  Mode     %s\n", styleSubtitle.Render(item.ReviewMode)))
	timeLabel := "Fires"
	if item.Status == "running" {
		timeLabel = "Started"
	}
	b.WriteString(fmt.Sprintf("  %-7s  %s\n", timeLabel, formatPendingTime(item)))
	return styleBorder.Width(w - 2).Height(6).Render(b.String())
}

func formatScheduled(t time.Time) string {
	now := time.Now()
	if t.After(now) {
		d := t.Sub(now).Round(time.Second)
		return styleStatusStale.Render("in " + fmtDuration(d))
	}
	return styleStatusCompleted.Render("due now")
}

func formatScheduledPlain(t time.Time) string {
	now := time.Now()
	if t.After(now) {
		d := t.Sub(now).Round(time.Second)
		return "in " + fmtDuration(d)
	}
	return "due now"
}

func formatPendingTime(item api.PendingReview) string {
	if item.Status == "running" {
		d := time.Since(item.ScheduledAt).Round(time.Second)
		return styleStatusStale.Render(fmtDuration(d) + " ago")
	}
	return formatScheduled(item.ScheduledAt)
}

func formatPendingTimePlain(item api.PendingReview) string {
	if item.Status == "running" {
		d := time.Since(item.ScheduledAt).Round(time.Second)
		return fmtDuration(d) + " ago"
	}
	return formatScheduledPlain(item.ScheduledAt)
}

func pendingStatusStyle(status string) lipgloss.Style {
	switch status {
	case "running":
		return styleStatusStale
	default:
		return styleSubtitle
	}
}

func fmtDuration(d time.Duration) string {
	d = d.Round(time.Second)
	m := int(d.Minutes())
	s := int(d.Seconds()) % 60
	if m > 0 {
		return fmt.Sprintf("%dm %ds", m, s)
	}
	return fmt.Sprintf("%ds", s)
}
