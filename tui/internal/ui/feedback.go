package ui

import (
	"fmt"

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
	offset  int // scroll position over the body lines
	width   int
	height  int
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
		switch m.String() {
		case "r":
			f.loading = true
			return f, f.load()
		case "j", "down":
			f.offset = clampOffset(f.offset+1, len(f.bodyLines()), f.visibleRows())
		case "k", "up":
			f.offset = clampOffset(f.offset-1, len(f.bodyLines()), f.visibleRows())
		case "pgdown", "f":
			v := f.visibleRows()
			f.offset = clampOffset(f.offset+v, len(f.bodyLines()), v)
		case "pgup", "b":
			v := f.visibleRows()
			f.offset = clampOffset(f.offset-v, len(f.bodyLines()), v)
		}
	}
	return f, nil
}

// visibleRows is the body area in the Feedback tab: total height minus the
// pinned header and a reserved scroll-indicator line.
func (f Feedback) visibleRows() int {
	v := f.height - 2
	if v < 1 {
		v = 1
	}
	return v
}

// bodyLines is everything below the pinned header — the per-(repo,category)
// stats table and the mute list — one display line per slice element. Shared by
// view() (to render) and update() (to bound the scroll offset), so the two
// can't drift.
func (f Feedback) bodyLines() []string {
	var body []string
	body = append(body, "")

	if len(f.stats) == 0 {
		body = append(body, styleSubtitle.Render("  No findings with feedback yet."))
	} else {
		colRepo, colCat := 30, 16
		body = append(body, lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
			fmt.Sprintf("  %-*s  %-*s  %8s  %9s  %8s",
				colRepo, "Repository", colCat, "Category", "Findings", "Dismissed", "Fixed")))
		for _, s := range f.stats {
			body = append(body, fmt.Sprintf("  %-*s  %-*s  %8d  %9d  %8d",
				colRepo, truncate(s.Repo, colRepo), colCat, truncate(s.Category, colCat),
				s.Findings, s.Dismissed, s.ResolvedByFix))
		}
	}

	body = append(body, "", styleSubtitle.Render("  Muted categories"))
	if len(f.mutes) == 0 {
		body = append(body, styleSubtitle.Render("  (none — reply /mute <category> on an inline finding)"))
	} else {
		for _, mt := range f.mutes {
			body = append(body, fmt.Sprintf("  • %s · %s  (by %s, %s)",
				mt.Repo, mt.Category, mt.MutedBy, relativeTime(mt.CreatedAt)))
		}
	}
	return body
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

	body := f.bodyLines()

	// Window the body so a long stats/mute list scrolls instead of overflowing.
	avail := h - 1 // header is pinned
	if avail < 1 {
		avail = 1
	}
	visible := avail
	overflow := len(body) > avail
	if overflow {
		visible = avail - 1 // reserve a line for the scroll indicator
		if visible < 1 {
			visible = 1
		}
	}
	off := clampOffset(f.offset, len(body), visible)
	end := off + visible
	if end > len(body) {
		end = len(body)
	}

	out := append([]string{header}, body[off:end]...)
	if overflow {
		out = append(out, scrollHint(off, visible, len(body)))
	}
	return lipgloss.JoinVertical(lipgloss.Left, out...)
}
