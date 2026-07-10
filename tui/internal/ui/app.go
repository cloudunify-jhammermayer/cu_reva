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
	viewAudits   // tab 8
	viewFeedback // tab 9
	viewOdoo     // tab 0
	viewTimesheets
)

// tabKeys maps the number-row switch keys to their tab, so the key handler is a
// single lookup instead of ten near-identical cases (M27).
var tabKeys = map[string]view{
	"1": viewDashboard,
	"2": viewReviews,
	"3": viewFindings,
	"4": viewFailures,
	"5": viewRepos,
	"6": viewPending,
	"7": viewTickets,
	"8": viewAudits,
	"9": viewFeedback,
	"0": viewOdoo,
	"-": viewTimesheets,
}

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
	audits    Audits
	feedback  Feedback
	odoo      Odoo
	timesheet Timesheets
	width     int
	height    int
}

func NewApp(client api.ClientIface, odooURL string) *App {
	return &App{
		client:    client,
		active:    viewDashboard,
		dashboard: newDashboard(client),
		reviews:   newReviews(client),
		findings:  newFindings(client),
		failures:  newFailures(client),
		repos:     newRepos(client),
		pending:   newPending(client),
		tickets:   newTickets(client, odooURL),
		audits:    newAudits(client),
		feedback:  newFeedback(client),
		odoo:      newOdoo(client),
		timesheet: newTimesheets(client),
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
		a.audits.load(),
		a.feedback.load(),
		a.odoo.load(),
		a.timesheet.load(),
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
		// The tab bar wraps to multiple lines on narrow terminals, so derive the
		// content budget from its actual rendered height rather than assuming 2
		// lines. Reserve 1 line for the status bar (View clamps to the real value).
		contentH := m.Height - lipgloss.Height(a.tabBar()) - 1
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
		a.audits.width = m.Width
		a.audits.height = contentH
		a.feedback.width = m.Width
		a.feedback.height = contentH
		a.odoo.width = m.Width
		a.odoo.height = contentH
		a.timesheet.width = m.Width
		a.timesheet.height = contentH
		return a, nil

	case tea.KeyMsg:
		// While the active tab is capturing text (add-repo or a `/` filter),
		// route every key to it so digits/letters type instead of switching
		// tabs or quitting. ctrl+c still quits.
		if a.capturingText() && m.String() != "ctrl+c" {
			var cmd tea.Cmd
			switch a.active {
			case viewReviews:
				a.reviews, cmd = a.reviews.update(msg)
			case viewFindings:
				a.findings, cmd = a.findings.update(msg)
			case viewRepos:
				a.repos, cmd = a.repos.update(msg)
			case viewTickets:
				a.tickets, cmd = a.tickets.update(msg)
			case viewOdoo:
				a.odoo, cmd = a.odoo.update(msg)
			}
			return a, cmd
		}
		if m.String() == "q" || m.String() == "ctrl+c" {
			return a, tea.Quit
		}
		if v, ok := tabKeys[m.String()]; ok {
			a.clearStatusMsgs()
			a.active = v
			return a, nil
		}
		if a.active == viewDashboard {
			var cmd tea.Cmd
			a.dashboard, cmd = a.dashboard.update(msg)
			return a, cmd
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
		if a.active == viewAudits {
			var cmd tea.Cmd
			a.audits, cmd = a.audits.update(msg)
			return a, cmd
		}
		if a.active == viewFeedback {
			var cmd tea.Cmd
			a.feedback, cmd = a.feedback.update(msg)
			return a, cmd
		}
		if a.active == viewOdoo {
			var cmd tea.Cmd
			a.odoo, cmd = a.odoo.update(msg)
			return a, cmd
		}
		if a.active == viewTimesheets {
			var cmd tea.Cmd
			a.timesheet, cmd = a.timesheet.update(msg)
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
		var auditCmd tea.Cmd
		a.audits, auditCmd = a.audits.update(msg)
		var fbCmd tea.Cmd
		a.feedback, fbCmd = a.feedback.update(msg)
		var odooCmd tea.Cmd
		a.odoo, odooCmd = a.odoo.update(msg)
		var timesheetCmd tea.Cmd
		a.timesheet, timesheetCmd = a.timesheet.update(msg)
		return a, tea.Batch(cmd, findCmd, failCmd, repoCmd, pendCmd, ticketCmd, auditCmd, fbCmd, odooCmd, timesheetCmd, tick())

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

	case opsEventsLoadedMsg:
		a.failures, _ = a.failures.update(msg)

	case findingsLoadedMsg:
		a.findings, _ = a.findings.update(msg)

	case auditRunsLoadedMsg:
		a.audits, _ = a.audits.update(msg)

	case auditFindingsLoadedMsg:
		a.audits, _ = a.audits.update(msg)

	case feedbackLoadedMsg:
		a.feedback, _ = a.feedback.update(msg)

	case reposLoadedMsg:
		a.repos, _ = a.repos.update(msg)

	case auditTriggeredMsg:
		a.repos, _ = a.repos.update(msg)

	case repoAddedMsg:
		var cmd tea.Cmd
		a.repos, cmd = a.repos.update(msg)
		return a, cmd

	case pendingLoadedMsg:
		a.pending, _ = a.pending.update(msg)

	case ticketAnalysesLoadedMsg:
		a.tickets, _ = a.tickets.update(msg)

	case ticketIssueRunsLoadedMsg:
		a.tickets, _ = a.tickets.update(msg)

	case ticketRequeuedMsg:
		a.tickets, _ = a.tickets.update(msg)

	case ticketJourneyLoadedMsg:
		a.tickets, _ = a.tickets.update(msg)

	case odooLoadedMsg:
		a.odoo, _ = a.odoo.update(msg)

	case timesheetsLoadedMsg:
		a.timesheet, _ = a.timesheet.update(msg)

	case odooCreatedMsg:
		var cmd tea.Cmd
		a.odoo, cmd = a.odoo.update(msg)
		return a, cmd

	case odooActionMsg:
		var cmd tea.Cmd
		a.odoo, cmd = a.odoo.update(msg)
		return a, cmd

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

	// Both bars can wrap on narrow terminals (the tab bar in particular), so the
	// content budget is whatever's left after the *actual* chrome — never a fixed
	// guess. This, plus the MaxHeight clamp below, keeps the bars on screen.
	tabBar := a.tabBar()
	statusBar := a.statusBar()
	contentH := a.height - lipgloss.Height(tabBar) - lipgloss.Height(statusBar)
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
	case viewAudits:
		content = a.audits.view(a.width, contentH)
	case viewFeedback:
		content = a.feedback.view(a.width, contentH)
	case viewOdoo:
		content = a.odoo.view(a.width, contentH)
	case viewTimesheets:
		content = a.timesheet.view(a.width, contentH)
	}

	// Safety net: no tab may emit more than contentH lines. lipgloss Height() is
	// a minimum, not a clip, so without this a long detail pane / findings list
	// would push the status (and tab) bar off-screen. Tabs scroll their own
	// content; this guarantees the chrome stays put even if one miscounts.
	content = lipgloss.NewStyle().MaxHeight(contentH).MaxWidth(a.width).Render(content)

	return lipgloss.JoinVertical(lipgloss.Left,
		tabBar,
		content,
		statusBar,
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
		{"8", "Audits", 0, viewAudits},
		{"9", "Feedback", 0, viewFeedback},
		{"0", "Odoo", 0, viewOdoo},
		{"-", "Timesheets", 0, viewTimesheets},
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

// capturingText reports whether the active tab is in a text-entry mode (add-repo
// or a `/` filter), in which case the global key handler must forward keys to it
// rather than treating digits as tab switches or `q` as quit.
func (a *App) capturingText() bool {
	switch a.active {
	case viewReviews:
		return a.reviews.filterMode
	case viewFindings:
		return a.findings.filtering
	case viewRepos:
		return a.repos.adding || a.repos.filtering
	case viewTickets:
		return a.tickets.filtering
	case viewOdoo:
		return a.odoo.creating
	}
	return false
}

func (a *App) clearStatusMsgs() {
	a.reviews.statusMsg = ""
	a.failures.statusMsg = ""
	a.tickets.statusMsg = ""
	a.repos.statusMsg = ""
}

func (a *App) statusBar() string {
	var hint string
	switch a.active {
	case viewDashboard:
		hint = "0-9 switch tabs | r=refresh | q quit"
	case viewReviews:
		hint = "j/k navigate | J/K scroll detail | / filter | s=status | c=clear | e=requeue | o=browser | r=refresh | q quit"
	case viewFindings:
		hint = "j/k·g/G·pgup/dn nav | / filter | o=open PR | a/c/m/n/i severity | r=refresh | q quit"
	case viewFailures:
		hint = "j/k navigate | v=runs/events | e=requeue | r=refresh | q quit"
	case viewRepos:
		hint = "j/k·g/G nav | / filter | n=add repo | a=audit | o=open in browser | r=refresh | q quit"
	case viewPending:
		hint = "j/k navigate | r=refresh | q quit"
	case viewTickets:
		hint = "j/k·g/G nav | / filter | enter=issues/fold · space · z=fold all | e=requeue | o=open | r=refresh | q quit"
	case viewAudits:
		hint = "j/k navigate/scroll | enter=findings | esc=back | r=refresh | q quit"
	case viewFeedback:
		hint = "j/k scroll | dismissals & mutes per repo/category | r=refresh | q quit"
	case viewOdoo:
		hint = "j/k navigate | n=add · ^R=rotate key · D=delete · t=toggle active · r=refresh | q quit"
	case viewTimesheets:
		hint = "j/k navigate | r=refresh | q quit"
	default:
		hint = "1 Dash | 2 Reviews | 3 Findings | 4 Failures | 5 Repos | 6 Pending | 7 Tickets | 8 Audits | q quit"
	}
	return styleStatusBar.Width(a.width).Render(hint)
}
