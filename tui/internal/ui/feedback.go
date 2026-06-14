package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

// Feedback shows the Tier-3 learning signals: per (repo, category) how many
// findings were posted, dismissed (/dismiss), and fixed, plus the active
// /mute list. It's the visible form of the statistic that drives per-repo
// learned memory.
type Feedback struct {
	client  api.ClientIface
	stats   []api.LearningStat
	mutes   []api.MuteEntry
	err     error
	loading bool
}

func newFeedback(client api.ClientIface) Feedback {
	return Feedback{client: client, loading: true}
}

func (f Feedback) load() tea.Cmd {
	client := f.client
	return func() tea.Msg {
		stats, err := client.Learning()
		if err != nil {
			return feedbackLoadedMsg{err: err}
		}
		mutes, err := client.Mutes()
		return feedbackLoadedMsg{stats: stats, mutes: mutes, err: err}
	}
}

func (f Feedback) update(msg tea.Msg) (Feedback, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return f, f.load()
	case feedbackLoadedMsg:
		f.loading = false
		f.err = m.err
		if m.err == nil {
			f.stats = m.stats
			f.mutes = m.mutes
		}
	case tea.KeyMsg:
		if m.String() == "r" {
			f.loading = true
			return f, f.load()
		}
	}
	return f, nil
}

func (f Feedback) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render("Feedback & learning signals (last 90d)")
	if f.loading && len(f.stats) == 0 && len(f.mutes) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if f.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", styleStatusFailed.Render("  Error: "+f.err.Error()))
	}

	sections := []string{header, ""}

	// Per (repo, category) learning signals — the input for per-repo learned memory.
	if len(f.stats) == 0 {
		sections = append(sections, styleSubtitle.Render("  No findings with feedback yet."))
	} else {
		colRepo, colCat := 30, 16
		hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
			fmt.Sprintf("  %-*s  %-*s  %8s  %9s  %8s",
				colRepo, "Repository", colCat, "Category", "Findings", "Dismissed", "Fixed"))
		rows := []string{hdr}
		for _, s := range f.stats {
			rows = append(rows, fmt.Sprintf("  %-*s  %-*s  %8d  %9d  %8d",
				colRepo, truncate(s.Repo, colRepo), colCat, truncate(s.Category, colCat),
				s.Findings, s.Dismissed, s.ResolvedByFix))
		}
		sections = append(sections, strings.Join(rows, "\n"))
	}

	// Active mutes.
	sections = append(sections, "", styleSubtitle.Render("  Muted categories"))
	if len(f.mutes) == 0 {
		sections = append(sections, styleSubtitle.Render("  (none — reply /mute <category> on an inline finding)"))
	} else {
		for _, mt := range f.mutes {
			sections = append(sections, fmt.Sprintf("  • %s · %s  (by %s, %s)",
				mt.Repo, mt.Category, mt.MutedBy, relativeTime(mt.CreatedAt)))
		}
	}
	return lipgloss.JoinVertical(lipgloss.Left, sections...)
}
