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
		switch m.String() {
		case "j", "down":
			if p.cursor < len(p.items)-1 {
				p.cursor++
				if p.cursor >= p.offset+visibleRows {
					p.offset++
				}
			}
		case "k", "up":
			if p.cursor > 0 {
				p.cursor--
				if p.cursor < p.offset {
					p.offset--
				}
			}
		case "r":
			p.loading = true
			return p, p.load()
		}
	}
	return p, nil
}

func (p Pending) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Pending  (%d)", p.total))

	if p.loading && len(p.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading…")))
	}
	if p.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+p.err.Error()))
	}
	if len(p.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No pending reviews")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	colRepo := 28
	colPR := 6
	colEvent := 12
	colWhen := w - colRepo - colPR - colEvent - 8

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s",
			colRepo, "Repository",
			colPR, "PR#",
			colEvent, "Trigger",
			colWhen, "Fires"),
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
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s",
				colRepo, repo,
				colPR, prNum,
				colEvent, event,
				colWhen, formatScheduledPlain(item.ScheduledAt),
			))
		} else {
			line = fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s",
				colRepo, repo,
				colPR, prNum,
				colEvent, event,
				colWhen, formatScheduled(item.ScheduledAt),
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")

	var detail string
	if p.cursor < len(p.items) {
		detail = p.renderDetail(p.items[p.cursor], w)
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", p.cursor+1, len(p.items)))
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", detail, "", pos)
}

func (p Pending) renderDetail(item api.PendingReview, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s", item.PRNumber, truncate(item.PRTitle, w-20))) + "\n")
	b.WriteString(fmt.Sprintf("  Repo     %s\n", item.RepoFullName))
	b.WriteString(fmt.Sprintf("  SHA      %s\n", styleSubtitle.Render(item.HeadSHA[:8])))
	b.WriteString(fmt.Sprintf("  Trigger  %s\n", styleSubtitle.Render(item.TriggerEvent)))
	b.WriteString(fmt.Sprintf("  Mode     %s\n", styleSubtitle.Render(item.ReviewMode)))
	b.WriteString(fmt.Sprintf("  Fires    %s\n", formatScheduled(item.ScheduledAt)))
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

func fmtDuration(d time.Duration) string {
	d = d.Round(time.Second)
	m := int(d.Minutes())
	s := int(d.Seconds()) % 60
	if m > 0 {
		return fmt.Sprintf("%dm %ds", m, s)
	}
	return fmt.Sprintf("%ds", s)
}
