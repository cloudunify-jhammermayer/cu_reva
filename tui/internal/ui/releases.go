package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

// Releases lists Odoo release-log lookups (GET /release-notes): which repo
// page REVA handed to Odoo for a release, or why it could not. Read-only.
type Releases struct {
	client  api.ClientIface
	items   []api.ReleaseNoteSummary
	total   int
	err     error
	loading bool
	cursor  int
	offset  int
	width   int
	height  int
}

func newReleases(client api.ClientIface) Releases {
	return Releases{client: client, loading: true}
}

func (r Releases) load() tea.Cmd {
	return func() tea.Msg {
		data, err := r.client.ReleaseNotes(100)
		return releasesLoadedMsg{data: data, err: err}
	}
}

func (r Releases) update(msg tea.Msg) (Releases, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return r, r.load()
	case releasesLoadedMsg:
		r.loading = false
		r.err = m.err
		if m.data != nil {
			r.items = m.data.Items
			r.total = m.data.Total
		}
		if r.cursor >= len(r.items) {
			r.cursor, r.offset = 0, 0
		}
	case tea.KeyMsg:
		visibleRows := r.height - 12
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), r.cursor, r.offset, len(r.items), visibleRows); ok {
			r.cursor, r.offset = c, o
			return r, nil
		}
		switch m.String() {
		case "r", "R":
			r.loading = true
			return r, r.load()
		}
	}
	return r, nil
}

func (r Releases) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Release Logs  (%d)", r.total))
	if r.loading && len(r.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if r.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+r.err.Error()))
	}
	if len(r.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No release-log lookups yet")))
	}

	colID, colInst, colStatus, colSource, colAge := 6, 5, 10, 30, 9
	colRelease := w - colID - colInst - colStatus - colSource - colAge - 18
	if colRelease < 12 {
		colRelease = 12
	}
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colID, "Note",
			colInst, "Inst",
			colStatus, "Status",
			colRelease, "Release",
			colSource, "Source",
			colAge, "Age"),
	)

	visibleRows := h - 12
	if visibleRows < 1 {
		visibleRows = 1
	}
	off := ensureVisible(r.offset, r.cursor, visibleRows, len(r.items))
	end := off + visibleRows
	if end > len(r.items) {
		end = len(r.items)
	}

	rows := []string{hdr}
	for i := off; i < end; i++ {
		item := r.items[i]
		source := ""
		if item.SourcePath != nil {
			source = *item.SourcePath
		}
		line := fmt.Sprintf("  %-*s  %-*d  %-*s  %-*s  %-*s  %-*s",
			colID, fmt.Sprintf("#%d", item.ID),
			colInst, item.OdooInstanceID,
			colStatus, item.Status,
			colRelease, truncate(item.ReleaseName, colRelease),
			colSource, truncate(source, colSource),
			colAge, relativeTime(item.CreatedAt),
		)
		if i == r.cursor {
			rows = append(rows, styleSelected.Width(w-2).Render(line))
			continue
		}
		rows = append(rows, fmt.Sprintf("  %-*s  %-*d  %s  %-*s  %-*s  %-*s",
			colID, fmt.Sprintf("#%d", item.ID),
			colInst, item.OdooInstanceID,
			padCell(releaseStatusStyle(item.Status).Render(item.Status), colStatus),
			colRelease, truncate(item.ReleaseName, colRelease),
			colSource, truncate(source, colSource),
			colAge, relativeTime(item.CreatedAt),
		))
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", r.cursor+1, len(r.items)))
	detail := ""
	if r.cursor < len(r.items) {
		detail = r.detail(r.items[r.cursor], w)
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", strings.Join(rows, "\n"), "", detail, "", pos)
}

func (r Releases) detail(item api.ReleaseNoteSummary, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s  (%s)", item.ID, item.ReleaseName, item.Slug)) + "\n")
	b.WriteString(fmt.Sprintf("  Status    %s\n", releaseStatusStyle(item.Status).Render(item.Status)))
	b.WriteString(fmt.Sprintf("  Odoo      instance %d · release %d\n", item.OdooInstanceID, item.ReleaseID))
	if item.SourcePath != nil {
		repo := ""
		if item.SourceRepoID != nil {
			repo = fmt.Sprintf(" (repo %d)", *item.SourceRepoID)
		}
		b.WriteString(fmt.Sprintf("  Source    %s%s\n", *item.SourcePath, repo))
	}
	if item.URL != nil {
		b.WriteString(truncate(fmt.Sprintf("  URL       %s", *item.URL), w-4) + "\n")
	}
	b.WriteString(fmt.Sprintf("  Created   %s\n", relativeTime(item.CreatedAt)))
	if item.CompletedAt != nil {
		b.WriteString(fmt.Sprintf("  Done      %s\n", relativeTime(*item.CompletedAt)))
	}
	if item.CallbackSentAt != nil {
		b.WriteString(fmt.Sprintf("  Callback  %s\n", relativeTime(*item.CallbackSentAt)))
	}
	if item.Error != nil && *item.Error != "" {
		b.WriteString(styleStatusFailed.Render(truncate("  Error     "+*item.Error, w-4)) + "\n")
	}
	return styleBorder.Width(w - 2).Height(9).Render(b.String())
}

func releaseStatusStyle(status string) lipgloss.Style {
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
