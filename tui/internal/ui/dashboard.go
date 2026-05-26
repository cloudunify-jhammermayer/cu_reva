package ui

import (
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Dashboard struct {
	client  api.ClientIface
	data    *api.DashboardMetrics
	err     error
	loading bool
	width   int
	height  int
}

func newDashboard(client api.ClientIface) Dashboard {
	return Dashboard{client: client, loading: true}
}

func (d Dashboard) load() tea.Cmd {
	return func() tea.Msg {
		data, err := d.client.Dashboard()
		return dashboardLoadedMsg{data: data, err: err}
	}
}

func (d Dashboard) update(msg tea.Msg) (Dashboard, tea.Cmd) {
	switch m := msg.(type) {
	case dashboardLoadedMsg:
		d.loading = false
		d.data = m.data
		d.err = m.err
	case tickMsg:
		d.loading = true
		return d, d.load()
	}
	return d, nil
}

func (d Dashboard) view(w, h, pendingCount int) string {
	d.width = w
	d.height = h

	if d.loading && d.data == nil {
		return lipgloss.Place(w, h, lipgloss.Center, lipgloss.Center,
			styleSubtitle.Render("Loading..."))
	}
	if d.err != nil {
		return lipgloss.Place(w, h, lipgloss.Center, lipgloss.Center,
			styleStatusFailed.Render("Error: "+d.err.Error()))
	}
	if d.data == nil {
		return ""
	}

	m := d.data
	col := (w - 6) / 2

	left := d.renderPeriodCard("Last 24 h", m.Last24h, col)
	right := d.renderPeriodCard("Last 7 d", m.Last7d, col)
	topRow := lipgloss.JoinHorizontal(lipgloss.Top, left, right)

	findingsCard := d.renderFindingsCard(m.Findings24h, col)
	costCard := d.renderCostCard(m, pendingCount, col)
	bottomRow := lipgloss.JoinHorizontal(lipgloss.Top, findingsCard, costCard)

	refreshNote := styleSubtitle.Render(fmt.Sprintf("  refreshed %s |auto-refresh 30s",
		time.Now().Format("15:04:05")))

	return lipgloss.JoinVertical(lipgloss.Left,
		styleTitle.Padding(0, 1).Render("REVA Dashboard"),
		"",
		topRow,
		"",
		bottomRow,
		"",
		refreshNote,
	)
}

func (d Dashboard) renderPeriodCard(title string, p api.PeriodStats, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(title) + "\n")
	b.WriteString(fmt.Sprintf("  Completed  %s\n",
		styleStatusCompleted.Render(fmt.Sprintf("%d", p.ReviewsCompleted))))
	b.WriteString(fmt.Sprintf("  Failed     %s\n",
		styleStatusFailed.Render(fmt.Sprintf("%d", p.ReviewsFailed))))
	b.WriteString(fmt.Sprintf("  Success    %s\n",
		lipgloss.NewStyle().Foreground(colorAccent).Render(fmt.Sprintf("%.0f%%", p.SuccessRate*100))))
	if p.AvgDurationMS != nil {
		b.WriteString(fmt.Sprintf("  Avg time   %s\n",
			styleSubtitle.Render(fmt.Sprintf("%.0f ms", *p.AvgDurationMS))))
	} else {
		b.WriteString("  Avg time   " + styleSubtitle.Render("—") + "\n")
	}
	return styleBorder.Width(w).Render(b.String())
}

func (d Dashboard) renderFindingsCard(fc api.FindingCounts, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("Findings (24 h)") + "\n")
	b.WriteString(fmt.Sprintf("  %s Critical  %d\n", styleSeverityCritical.Render("●"), fc.Critical))
	b.WriteString(fmt.Sprintf("  %s Major     %d\n", styleSeverityMajor.Render("●"), fc.Major))
	b.WriteString(fmt.Sprintf("  %s Minor     %d\n", styleSeverityMinor.Render("●"), fc.Minor))
	b.WriteString(fmt.Sprintf("  %s Info      %d\n", styleSeverityInfo.Render("●"), fc.Info))
	return styleBorder.Width(w).Render(b.String())
}

func (d Dashboard) renderCostCard(m *api.DashboardMetrics, pendingCount, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("Cost (7 d)") + "\n")
	b.WriteString(fmt.Sprintf("  Total   $%.4f\n", m.TotalCost7d))
	if m.AvgCostPerReview7d != nil {
		b.WriteString(fmt.Sprintf("  Per PR  $%.4f\n", *m.AvgCostPerReview7d))
	} else {
		b.WriteString("  Per PR  " + styleSubtitle.Render("—") + "\n")
	}
	if pendingCount > 0 {
		b.WriteString(fmt.Sprintf("  Queue   %s pending\n",
			lipgloss.NewStyle().Foreground(colorYellow).Render(fmt.Sprintf("%d", pendingCount))))
	} else {
		b.WriteString(fmt.Sprintf("  Queue   %s\n",
			styleStatusCompleted.Render("0 pending")))
	}
	if m.ActiveWorkers > 0 {
		b.WriteString(fmt.Sprintf("  Workers %s\n",
			styleStatusCompleted.Render(fmt.Sprintf("%d active", m.ActiveWorkers))))
	} else {
		b.WriteString(fmt.Sprintf("  Workers %s\n",
			styleSubtitle.Render("0 active")))
	}
	return styleBorder.Width(w).Render(b.String())
}
