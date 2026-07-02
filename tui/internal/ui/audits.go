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
	detail         bool
	detailRun      int
	detailRepo     string
	findings       []api.AuditFindingSummary
	findingsOffset int // scroll position within the findings drill-down
	fLoading       bool
	fErr           error
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
		a.findingsOffset = 0

	case tea.KeyMsg:
		if a.detail {
			vis := a.height - 5
			if vis < 1 {
				vis = 1
			}
			switch m.String() {
			case "esc", "left", "h":
				a.detail = false
			case "j", "down":
				a.findingsOffset = clampOffset(a.findingsOffset+1, len(a.findings), vis)
			case "k", "up":
				a.findingsOffset = clampOffset(a.findingsOffset-1, len(a.findings), vis)
			case "pgdown", "f":
				a.findingsOffset = clampOffset(a.findingsOffset+vis, len(a.findings), vis)
			case "pgup", "b":
				a.findingsOffset = clampOffset(a.findingsOffset-vis, len(a.findings), vis)
			case "ctrl+d":
				a.findingsOffset = clampOffset(a.findingsOffset+vis/2, len(a.findings), vis)
			case "ctrl+u":
				a.findingsOffset = clampOffset(a.findingsOffset-vis/2, len(a.findings), vis)
			case "g", "home":
				a.findingsOffset = 0
			case "G", "end":
				a.findingsOffset = clampOffset(len(a.findings), len(a.findings), vis)
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
		if c, o, ok := listNav(m.String(), a.cursor, a.offset, len(a.runs), visibleRows); ok {
			a.cursor, a.offset = c, o
			return a, nil
		}
		switch m.String() {
		case "enter":
			if a.cursor < len(a.runs) {
				run := a.runs[a.cursor]
				a.detail, a.detailRun, a.detailRepo = true, run.ID, run.RepoFullName
				a.findings, a.fErr, a.fLoading = nil, nil, true
				a.findingsOffset = 0
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
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   [enter] view findings", a.cursor+1, len(a.runs))) +
		cappedNote(len(a.runs), a.total)
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
		fmt.Sprintf("     %-*s  %-*s  %-*s", colTitle, "Title", colFile, "File:Line", colIssue, "Issue"))

	// Window the finding rows; the column header (hdr) stays pinned.
	vis := h - 5
	if vis < 1 {
		vis = 1
	}
	off := clampOffset(a.findingsOffset, len(a.findings), vis)
	end := off + vis
	if end > len(a.findings) {
		end = len(a.findings)
	}
	rows := []string{hdr}
	for _, f := range a.findings[off:end] {
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
	footer := styleSubtitle.Render("  [esc] back   [r] refresh")
	if sh := scrollHint(off, vis, len(a.findings)); sh != "" {
		footer += sh
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", footer)
}
