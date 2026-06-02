package ui

import (
	"fmt"
	"os/exec"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Repos struct {
	client  api.ClientIface
	items   []api.RepoSummary
	total   int
	err     error
	loading bool
	cursor  int
	offset  int
	width   int
	height  int
}

func newRepos(client api.ClientIface) Repos {
	return Repos{client: client, loading: true}
}

func (r Repos) load() tea.Cmd {
	client := r.client
	return func() tea.Msg {
		data, err := client.Repos()
		return reposLoadedMsg{data: data, err: err}
	}
}

func (r Repos) update(msg tea.Msg) (Repos, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return r, r.load()

	case reposLoadedMsg:
		r.loading = false
		r.err = m.err
		if m.data != nil {
			r.items = m.data.Items
			r.total = m.data.Total
		}
		if r.cursor >= len(r.items) {
			r.cursor = 0
			r.offset = 0
		}

	case tea.KeyMsg:
		visibleRows := r.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "j", "down":
			r.cursor, r.offset = moveCursor(r.cursor, r.offset, len(r.items), visibleRows, true)
		case "k", "up":
			r.cursor, r.offset = moveCursor(r.cursor, r.offset, len(r.items), visibleRows, false)
		case "o":
			if r.cursor < len(r.items) {
				url := "https://github.com/" + r.items[r.cursor].FullName
				_ = exec.Command("xdg-open", url).Start()
			}
		case "r":
			r.loading = true
			return r, r.load()
		}
	}
	return r, nil
}

func (r Repos) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Repos (%d)", r.total))

	if r.loading && len(r.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if r.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+r.err.Error()))
	}
	if len(r.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No repos configured")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	// enabled column is a single symbol rendered as %s (no width)
	colName := 32
	colBranch := 14
	colCount := 8
	colWhen := 12
	// enabled=1 + spacings = 2+2+2+2+2 = 11 extra
	remaining := w - 1 - colBranch - colCount - colWhen - 12
	if remaining > colName {
		colName = remaining
	}

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %-*s",
			colName, "Repository",
			colBranch, "Branch",
			colCount, "Reviews",
			colWhen, "Last review"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := r.offset + visibleRows
	if end > len(r.items) {
		end = len(r.items)
	}
	for i := r.offset; i < end; i++ {
		item := r.items[i]
		name := truncate(item.FullName, colName)
		branch := "—"
		if item.DefaultBranch != nil {
			branch = *item.DefaultBranch
		}
		branch = truncate(branch, colBranch)
		count := fmt.Sprintf("%d", item.ReviewCount)
		var lastReview string
		if item.LastReviewAt != nil {
			lastReview = relativeTime(*item.LastReviewAt)
		} else {
			lastReview = "never"
		}

		var line string
		if i == r.cursor {
			enabledChar := "x"
			if item.Enabled {
				enabledChar = "+"
			}
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				enabledChar,
				colName, name,
				colBranch, branch,
				colCount, count,
				colWhen, lastReview,
			))
		} else {
			var enabledSym string
			if item.Enabled {
				enabledSym = styleStatusCompleted.Render("+")
			} else {
				enabledSym = styleStatusFailed.Render("x")
			}
			line = fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				enabledSym,
				colName, name,
				colBranch, branch,
				colCount, count,
				colWhen, lastReview,
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", r.cursor+1, len(r.items)))

	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}

func relativeTime(t time.Time) string {
	d := time.Since(t)
	if d < 0 {
		d = 0
	}
	minutes := int(d.Minutes())
	hours := int(d.Hours())
	days := int(d.Hours() / 24)

	switch {
	case days >= 1:
		return fmt.Sprintf("%dd ago", days)
	case hours >= 1:
		return fmt.Sprintf("%dh ago", hours)
	default:
		if minutes < 1 {
			return "just now"
		}
		return fmt.Sprintf("%dm ago", minutes)
	}
}
