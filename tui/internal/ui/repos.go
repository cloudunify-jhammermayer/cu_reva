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
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string
	adding    bool   // capturing owner/name text input
	input     string // the add-repo buffer
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

	case auditTriggeredMsg:
		if m.err != nil {
			r.statusMsg = styleStatusFailed.Render("audit failed: " + m.err.Error())
		} else {
			r.statusMsg = styleStatusCompleted.Render("audit queued - findings/issues will appear shortly")
		}

	case repoAddedMsg:
		if m.err != nil {
			r.statusMsg = styleStatusFailed.Render("add failed: " + m.err.Error())
		} else {
			r.statusMsg = styleStatusCompleted.Render("added " + m.owner + "/" + m.name)
			return r, r.load() // refresh so the new repo shows up
		}

	case tea.KeyMsg:
		// Text-input mode: capture keys for the owner/name buffer.
		if r.adding {
			switch m.Type {
			case tea.KeyEsc:
				r.adding, r.input = false, ""
			case tea.KeyEnter:
				owner, name, ok := parseOwnerName(r.input)
				typed := r.input
				r.adding, r.input = false, ""
				if !ok {
					r.statusMsg = styleStatusFailed.Render("expected owner/name, got: " + typed)
					return r, nil
				}
				client := r.client
				r.statusMsg = styleSubtitle.Render("adding " + owner + "/" + name + " ...")
				return r, func() tea.Msg {
					return repoAddedMsg{owner: owner, name: name, err: client.AddRepo(owner, name)}
				}
			case tea.KeyBackspace:
				if len(r.input) > 0 {
					r.input = r.input[:len(r.input)-1]
				}
			case tea.KeyRunes:
				r.input += string(m.Runes)
			}
			return r, nil
		}

		visibleRows := r.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "n":
			r.adding, r.input, r.statusMsg = true, "", ""
		case "j", "down":
			r.cursor, r.offset = moveCursor(r.cursor, r.offset, len(r.items), visibleRows, true)
		case "k", "up":
			r.cursor, r.offset = moveCursor(r.cursor, r.offset, len(r.items), visibleRows, false)
		case "o":
			if r.cursor < len(r.items) {
				url := "https://github.com/" + r.items[r.cursor].FullName
				_ = exec.Command("xdg-open", url).Start()
			}
		case "a":
			if r.cursor < len(r.items) {
				id := r.items[r.cursor].ID
				client := r.client
				r.statusMsg = styleSubtitle.Render("triggering audit...")
				return r, func() tea.Msg {
					err := client.TriggerAudit(id)
					return auditTriggeredMsg{id: id, err: err}
				}
			}
		case "r":
			r.loading = true
			r.statusMsg = ""
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

	posLine := styleSubtitle.Render(fmt.Sprintf("  %d/%d", r.cursor+1, len(r.items)))
	if r.statusMsg != "" {
		posLine = "  " + r.statusMsg
	}
	if r.adding {
		posLine = "  " + styleTitle.Render(" add repo ") +
			"  owner/name: " + r.input + "█" +
			styleSubtitle.Render("    [enter] add   [esc] cancel")
	}

	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", posLine)
}

// parseOwnerName splits "owner/name" (trimming spaces, tolerating a leading
// scheme/host if pasted). Returns ok=false unless both parts are non-empty.
func parseOwnerName(s string) (owner, name string, ok bool) {
	s = strings.TrimSpace(s)
	s = strings.TrimPrefix(s, "https://github.com/")
	s = strings.Trim(s, "/")
	parts := strings.Split(s, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
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
