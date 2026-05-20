package ui

import (
	"fmt"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type view int

const (
	viewDashboard view = iota
	viewReviews
	viewFailures
)

type App struct {
	client    api.ClientIface
	active    view
	dashboard Dashboard
	reviews   Reviews
	failures  Failures
	width     int
	height    int
}

func NewApp(client api.ClientIface) *App {
	return &App{
		client:    client,
		active:    viewDashboard,
		dashboard: newDashboard(client),
		reviews:   newReviews(client),
		failures:  newFailures(client),
	}
}

func (a *App) Init() tea.Cmd {
	return tea.Batch(
		a.dashboard.load(),
		a.reviews.loadList(),
		a.failures.load(),
		tick(),
	)
}

func tick() tea.Cmd {
	return tea.Tick(30*time.Second, func(t time.Time) tea.Msg {
		return tickMsg{}
	})
}

func (a *App) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch m := msg.(type) {
	case tea.WindowSizeMsg:
		a.width = m.Width
		a.height = m.Height
		contentH := m.Height - 3
		if contentH < 1 {
			contentH = 1
		}
		a.reviews.width = m.Width
		a.reviews.height = contentH
		a.failures.width = m.Width
		a.failures.height = contentH
		return a, nil

	case tea.KeyMsg:
		switch m.String() {
		case "q", "ctrl+c":
			return a, tea.Quit
		case "1":
			a.active = viewDashboard
			return a, nil
		case "2":
			a.active = viewReviews
			return a, nil
		case "3":
			a.active = viewFailures
			return a, nil
		}
		if a.active == viewReviews {
			var cmd tea.Cmd
			a.reviews, cmd = a.reviews.update(msg)
			return a, cmd
		}
		if a.active == viewFailures {
			var cmd tea.Cmd
			a.failures, cmd = a.failures.update(msg)
			return a, cmd
		}

	case tickMsg:
		var cmd tea.Cmd
		a.dashboard, cmd = a.dashboard.update(msg)
		return a, tea.Batch(cmd, tick())

	case dashboardLoadedMsg:
		a.dashboard, _ = a.dashboard.update(msg)

	case reviewsLoadedMsg:
		var cmd tea.Cmd
		a.reviews, cmd = a.reviews.update(msg)
		return a, cmd

	case reviewDetailLoadedMsg:
		var cmd tea.Cmd
		a.reviews, cmd = a.reviews.update(msg)
		return a, cmd

	case failuresLoadedMsg:
		a.failures, _ = a.failures.update(msg)

	case requeuedMsg:
		// Deliver to whichever view is active so the status message lands there.
		if a.active == viewReviews {
			a.reviews, _ = a.reviews.update(msg)
		} else {
			a.failures, _ = a.failures.update(msg)
		}
	}

	return a, nil
}

func (a *App) View() string {
	if a.width == 0 {
		return ""
	}

	// tabBar = 1 line text + 1 line bottom border = 2 lines
	// statusBar = 1 line
	contentH := a.height - 3
	if contentH < 1 {
		contentH = 1
	}

	var content string
	switch a.active {
	case viewDashboard:
		content = a.dashboard.view(a.width, contentH)
	case viewReviews:
		content = a.reviews.view(a.width, contentH)
	case viewFailures:
		content = a.failures.view(a.width, contentH)
	}

	return lipgloss.JoinVertical(lipgloss.Left,
		a.tabBar(),
		content,
		a.statusBar(),
	)
}

// tabBar renders a single-line tab bar with a bottom border (2 lines total).
func (a *App) tabBar() string {
	tabs := []struct {
		key   string
		label string
		v     view
	}{
		{"1", "Dashboard", viewDashboard},
		{"2", "Reviews", viewReviews},
		{"3", "Failures", viewFailures},
	}

	var parts []string
	for _, t := range tabs {
		label := fmt.Sprintf("  %s %s  ", t.key, t.label)
		if a.active == t.v {
			parts = append(parts, lipgloss.NewStyle().
				Bold(true).
				Foreground(colorAccent).
				Render(label))
		} else {
			parts = append(parts, styleSubtitle.Render(label))
		}
	}

	bar := lipgloss.JoinHorizontal(lipgloss.Left, parts...)
	return lipgloss.NewStyle().
		Width(a.width).
		BorderBottom(true).
		BorderStyle(lipgloss.NormalBorder()).
		BorderBottomForeground(colorBorder).
		Render(bar)
}

func (a *App) statusBar() string {
	hint := "1 Dashboard · 2 Reviews · 3 Failures · q Quit"
	return styleStatusBar.Width(a.width).Render(hint)
}
