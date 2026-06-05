package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Audits struct {
	client  api.ClientIface
	runs    []api.AuditRunSummary
	total   int
	err     error
	loading bool
	cursor  int
	offset  int
	width   int
	height  int

	// detail: findings for the selected run
	detail     bool
	detailRun  int
	detailRepo string
	findings   []api.AuditFindingSummary
	fLoading   bool
	fErr       error
}

func newAudits(client api.ClientIface) Audits {
	return Audits{client: client, loading: true}
}

func (a Audits) load() tea.Cmd {
	client := a.client
	return func() tea.Msg {
		data, err := client.Audits(100)
		return auditRunsLoadedMsg{data: data, err: err}
	}
}

func (a Audits) loadFindings(runID int) tea.Cmd {
	client := a.client
	return func() tea.Msg {
		data, err := client.AuditFindings(runID, 200)
		return auditFindingsLoadedMsg{data: data, err: err}
	}
}

func (a Audits) update(msg tea.Msg) (Audits, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		if !a.detail {
			return a, a.load() // poll runs so a running audit flips to completed
		}

	case auditRunsLoadedMsg:
		a.loading = false
		a.err = m.err
		if m.data != nil {
			a.runs = m.data.Items
			a.total = m.data.Total
		}
		if a.cursor >= len(a.runs) {
			a.cursor, a.offset = 0, 0
		}

	case auditFindingsLoadedMsg:
		a.fLoading = false
		a.fErr = m.err
		if m.data != nil {
			a.findings = m.data.Items
		}

	case tea.KeyMsg:
		if a.detail {
			switch m.String() {
			case "esc", "left", "h":
				a.detail = false
			case "r":
				a.fLoading = true
				return a, a.loadFindings(a.detailRun)
			}
			return a, nil
		}
		visibleRows := a.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "j", "down":
			a.cursor, a.offset = moveCursor(a.cursor, a.offset, len(a.runs), visibleRows, true)
		case "k", "up":
			a.cursor, a.offset = moveCursor(a.cursor, a.offset, len(a.runs), visibleRows, false)
		case "enter":
			if a.cursor < len(a.runs) {
				run := a.runs[a.cursor]
				a.detail, a.detailRun, a.detailRepo = true, run.ID, run.RepoFullName
				a.findings, a.fErr, a.fLoading = nil, nil, true
				return a, a.loadFindings(run.ID)
			}
		case "r":
			a.loading = true
			return a, a.load()
		}
	}
	return a, nil
}

func (a Audits) view(w, h int) string {
	if a.detail {
		return a.findingsView(w, h)
	}

	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Audits (%d)", a.total))
	if a.loading && len(a.runs) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if a.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", styleStatusFailed.Render("  Error: "+a.err.Error()))
	}
	if len(a.runs) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No audits yet — trigger one from the Repos tab (a) or POST /repos/{id}/audit")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	colRepo, colStatus, colFind, colModel, colWhen := 26, 10, 12, 14, 10
	remaining := w - colStatus - colFind - colModel - colWhen - 12
	if remaining > colRepo {
		colRepo = remaining
	}

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colRepo, "Repository", colStatus, "Status", colFind, "Findings",
			colModel, "Model", colWhen, "When"))
	rows := []string{hdr}

	end := a.offset + visibleRows
	if end > len(a.runs) {
		end = len(a.runs)
	}
	for i := a.offset; i < end; i++ {
		r := a.runs[i]
		findings := "—"
		if r.Status == "completed" {
			findings = fmt.Sprintf("%d", r.FindingCount)
			if r.IssuedCount > 0 {
				findings = fmt.Sprintf("%d (%d issue)", r.FindingCount, r.IssuedCount)
			}
		}
		model := "—"
		if r.Model != nil {
			model = strings.TrimPrefix(*r.Model, "claude-")
		}
		cursor := "  "
		if i == a.cursor {
			cursor = styleStatusCompleted.Render("▸ ")
		}
		// Status is pre-styled (variable width); pad the plain text, render the rest plain.
		line := fmt.Sprintf("%s%-*s  %-*s  %-*s  %-*s  %-*s",
			cursor,
			colRepo, truncate(r.RepoFullName, colRepo),
			colStatus, r.Status,
			colFind, truncate(findings, colFind),
			colModel, truncate(model, colModel),
			colWhen, relativeTime(r.CreatedAt))
		rows = append(rows, line)
	}
	table := strings.Join(rows, "\n")
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   [enter] view findings", a.cursor+1, len(a.runs)))
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}

func (a Audits) findingsView(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(
		fmt.Sprintf("Audit #%d — %s  (%d findings)", a.detailRun, a.detailRepo, len(a.findings)))
	if a.fLoading && len(a.findings) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if a.fErr != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", styleStatusFailed.Render("  Error: "+a.fErr.Error()))
	}
	if len(a.findings) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("No findings recorded for this audit")),
			styleSubtitle.Render("  [esc] back"))
	}

	colTitle, colFile, colIssue := 44, 26, 7
	remaining := w - 1 - colFile - colIssue - 8
	if remaining > colTitle {
		colTitle = remaining
	}
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s", colTitle, "Title", colFile, "File:Line", colIssue, "Issue"))
	rows := []string{hdr}
	for _, f := range a.findings {
		fileLine := ""
		if f.FilePath != nil {
			fileLine = *f.FilePath
			if f.LineStart != nil {
				fileLine = fmt.Sprintf("%s:%d", fileLine, *f.LineStart)
			}
		}
		issue := "—"
		if f.GithubIssueNumber != nil {
			issue = fmt.Sprintf("#%d", *f.GithubIssueNumber)
		}
		rows = append(rows, fmt.Sprintf("  %s  %-*s  %-*s  %-*s",
			severityDot(f.Severity),
			colTitle, truncate(f.Title, colTitle),
			colFile, truncate(fileLine, colFile),
			colIssue, issue))
	}
	table := strings.Join(rows, "\n")
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", styleSubtitle.Render("  [esc] back   [r] refresh"))
}
