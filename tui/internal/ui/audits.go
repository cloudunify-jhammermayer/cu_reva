package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Audits struct {
	client         api.ClientIface
	items          []api.AuditFindingSummary
	total          int
	err            error
	loading        bool
	cursor         int
	offset         int
	width          int
	height         int
	severityFilter string
}

func newAudits(client api.ClientIface) Audits {
	return Audits{client: client, loading: true}
}

func (a Audits) load() tea.Cmd {
	sev := a.severityFilter
	client := a.client
	return func() tea.Msg {
		data, err := client.AuditFindings(sev, 200)
		return auditFindingsLoadedMsg{data: data, err: err}
	}
}

func (a Audits) update(msg tea.Msg) (Audits, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return a, a.load()

	case auditFindingsLoadedMsg:
		a.loading = false
		a.err = m.err
		if m.data != nil {
			a.items = m.data.Items
			a.total = m.data.Total
		}
		if a.cursor >= len(a.items) {
			a.cursor = 0
			a.offset = 0
		}

	case tea.KeyMsg:
		visibleRows := a.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "j", "down":
			a.cursor, a.offset = moveCursor(a.cursor, a.offset, len(a.items), visibleRows, true)
		case "k", "up":
			a.cursor, a.offset = moveCursor(a.cursor, a.offset, len(a.items), visibleRows, false)
		case "r":
			a.loading = true
			return a, a.load()
		case "a":
			a.severityFilter = ""
			a.loading = true
			return a, a.load()
		case "c":
			a.severityFilter = "critical"
			a.loading = true
			return a, a.load()
		case "m":
			a.severityFilter = "major"
			a.loading = true
			return a, a.load()
		case "n":
			a.severityFilter = "minor"
			a.loading = true
			return a, a.load()
		case "i":
			a.severityFilter = "info"
			a.loading = true
			return a, a.load()
		}
	}
	return a, nil
}

func (a Audits) view(w, h int) string {
	filterLabel := a.severityFilter
	if filterLabel == "" {
		filterLabel = "all"
	}
	header := styleTitle.Padding(0, 1).Render(
		fmt.Sprintf("Audit findings (%d)  filter: %s", a.total, filterLabel),
	)

	if a.loading && len(a.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if a.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+a.err.Error()))
	}
	if len(a.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No audit findings — trigger one with POST /repos/{id}/audit")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	colTitle := 36
	colRepo := 22
	colFile := 24
	colIssue := 6
	// dot=1 + spacing(2+2+2+2) = 9 extra chars
	remaining := w - 1 - colRepo - colFile - colIssue - 10
	if remaining > colTitle {
		colTitle = remaining
	}

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %-*s",
			colTitle, "Title",
			colRepo, "Repo",
			colFile, "File:Line",
			colIssue, "Issue"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := a.offset + visibleRows
	if end > len(a.items) {
		end = len(a.items)
	}
	for i := a.offset; i < end; i++ {
		item := a.items[i]
		title := truncate(item.Title, colTitle)
		repo := truncate(item.RepoFullName, colRepo)
		fileLine := ""
		if item.FilePath != nil {
			fileLine = *item.FilePath
			if item.LineStart != nil {
				fileLine = fmt.Sprintf("%s:%d", fileLine, *item.LineStart)
			}
		}
		fileLine = truncate(fileLine, colFile)
		issue := "—"
		if item.GithubIssueNumber != nil {
			issue = fmt.Sprintf("#%d", *item.GithubIssueNumber)
		}

		var line string
		if i == a.cursor {
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				"●",
				colTitle, title,
				colRepo, repo,
				colFile, fileLine,
				colIssue, issue,
			))
		} else {
			line = fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				severityDot(item.Severity),
				colTitle, title,
				colRepo, repo,
				colFile, fileLine,
				colIssue, issue,
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", a.cursor+1, len(a.items)))

	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}
