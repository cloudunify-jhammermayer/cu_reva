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
	viewFindings // tab 3
	viewFailures // tab 4
	viewRepos    // tab 5
	viewPending  // tab 6
	viewTickets  // tab 7
)

type App struct {
	client    api.ClientIface
	active    view
	dashboard Dashboard
	reviews   Reviews
	findings  Findings
	failures  Failures
	repos     Repos
	pending   Pending
	tickets   Tickets
	width     int
	height    int
}

func NewApp(client api.ClientIface) *App {
	return &App{
		client:    client,
		active:    viewDashboard,
		dashboard: newDashboard(client),
		reviews:   newReviews(client),
		findings:  newFindings(client),
		failures:  newFailures(client),
		repos:     newRepos(client),
		pending:   newPending(client),
		tickets:   newTickets(client),
	}
}

func (a *App) Init() tea.Cmd {
	return tea.Batch(
		a.dashboard.load(),
		a.reviews.loadList(),
		a.findings.load(),
		a.failures.load(),
		a.repos.load(),
		a.pending.load(),
		a.tickets.load(),
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
		a.findings.width = m.Width
		a.findings.height = contentH
		a.failures.width = m.Width
		a.failures.height = contentH
		a.repos.width = m.Width
		a.repos.height = contentH
		a.pending.width = m.Width
		a.pending.height = contentH
		a.tickets.width = m.Width
		a.tickets.height = contentH
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
			a.active = viewFindings
			return a, nil
		case "4":
			a.active = viewFailures
			return a, nil
		case "5":
			a.active = viewRepos
			return a, nil
		case "6":
			a.active = viewPending
			return a, nil
		case "7":
			a.active = viewTickets
			return a, nil
		}
		if a.active == viewReviews {
			var cmd tea.Cmd
			a.reviews, cmd = a.reviews.update(msg)
			return a, cmd
		}
		if a.active == viewFindings {
			var cmd tea.Cmd
			a.findings, cmd = a.findings.update(msg)
			return a, cmd
		}
		if a.active == viewFailures {
			var cmd tea.Cmd
			a.failures, cmd = a.failures.update(msg)
			return a, cmd
		}
		if a.active == viewRepos {
			var cmd tea.Cmd
			a.repos, cmd = a.repos.update(msg)
			return a, cmd
		}
		if a.active == viewPending {
			var cmd tea.Cmd
			a.pending, cmd = a.pending.update(msg)
			return a, cmd
		}
		if a.active == viewTickets {
			var cmd tea.Cmd
			a.tickets, cmd = a.tickets.update(msg)
			return a, cmd
		}

	case tickMsg:
		var cmd tea.Cmd
		a.dashboard, cmd = a.dashboard.update(msg)
		var findCmd tea.Cmd
		a.findings, findCmd = a.findings.update(msg)
		var failCmd tea.Cmd
		a.failures, failCmd = a.failures.update(msg)
		var repoCmd tea.Cmd
		a.repos, repoCmd = a.repos.update(msg)
		var pendCmd tea.Cmd
		a.pending, pendCmd = a.pending.update(msg)
		var ticketCmd tea.Cmd
		a.tickets, ticketCmd = a.tickets.update(msg)
		return a, tea.Batch(cmd, findCmd, failCmd, repoCmd, pendCmd, ticketCmd, tick())

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

	case findingsLoadedMsg:
		a.findings, _ = a.findings.update(msg)

	case reposLoadedMsg:
		a.repos, _ = a.repos.update(msg)

	case pendingLoadedMsg:
		a.pending, _ = a.pending.update(msg)

	case ticketAnalysesLoadedMsg:
		a.tickets, _ = a.tickets.update(msg)

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
		content = a.dashboard.view(a.width, contentH, a.pending.total)
	case viewReviews:
		content = a.reviews.view(a.width, contentH)
	case viewFindings:
		content = a.findings.view(a.width, contentH)
	case viewFailures:
		content = a.failures.view(a.width, contentH)
	case viewRepos:
		content = a.repos.view(a.width, contentH)
	case viewPending:
		content = a.pending.view(a.width, contentH)
	case viewTickets:
		content = a.tickets.view(a.width, contentH)
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
		badge int
		v     view
	}{
		{"1", "Dashboard", 0, viewDashboard},
		{"2", "Reviews", 0, viewReviews},
		{"3", "Findings", 0, viewFindings},
		{"4", "Failures", a.failures.total, viewFailures},
		{"5", "Repos", 0, viewRepos},
		{"6", "Pending", a.pending.total, viewPending},
		{"7", "Tickets", 0, viewTickets},
	}

	var parts []string
	for _, t := range tabs {
		label := t.label
		if t.badge > 0 {
			label = fmt.Sprintf("%s (%d)", label, t.badge)
		}
		text := fmt.Sprintf("  %s %s  ", t.key, label)
		if a.active == t.v {
			parts = append(parts, lipgloss.NewStyle().
				Bold(true).
				Foreground(colorAccent).
				Underline(true).
				Render(text))
		} else {
			parts = append(parts, styleSubtitle.Render(text))
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
	var hint string
	switch a.active {
	case viewDashboard:
		hint = "1–6 switch tabs · r=refresh · q quit"
	case viewReviews:
		hint = "j/k navigate · / filter · s=status · c=clear · e=requeue · o=browser · r=refresh · q quit"
	case viewFindings:
		hint = "j/k navigate · a=all · c=critical · m=major · n=minor · i=info · r=refresh · q quit"
	case viewFailures:
		hint = "j/k navigate · e=requeue · r=refresh · q quit"
	case viewRepos:
		hint = "j/k navigate · o=open in browser · r=refresh · q quit"
	case viewPending:
		hint = "j/k navigate · r=refresh · q quit"
	case viewTickets:
		hint = "j/k navigate · r=refresh · q quit"
	default:
		hint = "1 Dash · 2 Reviews · 3 Findings · 4 Failures · 5 Repos · 6 Pending · 7 Tickets · q quit"
	}
	return styleStatusBar.Width(a.width).Render(hint)
}
