package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Findings struct {
	client         api.ClientIface
	items          []api.FindingSummary
	total          int
	err            error
	loading        bool
	cursor         int
	offset         int
	width          int
	height         int
	severityFilter string
	filtering      bool   // capturing the `/` text filter
	filter         string // case-insensitive substring on repo/category/title/file
}

// filtered returns the loaded findings matching the active `/` filter
// (case-insensitive substring on repo · category · title · file), or all of
// them when unset. Operates over what's already fetched (severity-filtered,
// capped at the fetch limit).
func (f Findings) filtered() []api.FindingSummary {
	if f.filter == "" {
		return f.items
	}
	q := strings.ToLower(f.filter)
	out := make([]api.FindingSummary, 0, len(f.items))
	for _, it := range f.items {
		file := ""
		if it.FilePath != nil {
			file = *it.FilePath
		}
		hay := strings.ToLower(it.RepoFullName + " " + it.Category + " " + it.Title + " " + file)
		if strings.Contains(hay, q) {
			out = append(out, it)
		}
	}
	return out
}

func newFindings(client api.ClientIface) Findings {
	return Findings{client: client, loading: true}
}

func (f Findings) load() tea.Cmd {
	sev := f.severityFilter
	client := f.client
	return func() tea.Msg {
		data, err := client.Findings(sev, "", 200)
		return findingsLoadedMsg{data: data, err: err}
	}
}

func (f Findings) update(msg tea.Msg) (Findings, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return f, f.load()

	case findingsLoadedMsg:
		f.loading = false
		f.err = m.err
		if m.data != nil {
			f.items = m.data.Items
			f.total = m.data.Total
		}
		if f.cursor >= len(f.filtered()) {
			f.cursor = 0
			f.offset = 0
		}

	case tea.KeyMsg:
		// Filter-input mode: capture keys for the `/` text filter.
		if f.filtering {
			var changed bool
			f.filtering, f.filter, changed = applyFilterKey(m, f.filter)
			if changed {
				f.cursor, f.offset = 0, 0
			}
			return f, nil
		}

		visibleRows := f.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		items := f.filtered()
		if c, o, ok := listNav(m.String(), f.cursor, f.offset, len(items), visibleRows); ok {
			f.cursor, f.offset = c, o
			return f, nil
		}
		switch m.String() {
		case "/":
			f.filtering = true
		case "o":
			if f.cursor < len(items) && items[f.cursor].RepoFullName != "" {
				url := fmt.Sprintf("https://github.com/%s/pull/%d",
					items[f.cursor].RepoFullName, items[f.cursor].PRNumber)
				openInBrowser(url)
			}
		case "r":
			f.loading = true
			return f, f.load()
		case "a":
			f.severityFilter = ""
			f.loading = true
			return f, f.load()
		case "c":
			f.severityFilter = "critical"
			f.loading = true
			return f, f.load()
		case "m":
			f.severityFilter = "major"
			f.loading = true
			return f, f.load()
		case "n":
			f.severityFilter = "minor"
			f.loading = true
			return f, f.load()
		case "i":
			f.severityFilter = "info"
			f.loading = true
			return f, f.load()
		}
	}
	return f, nil
}

func (f Findings) view(w, h int) string {
	sev := f.severityFilter
	if sev == "" {
		sev = "all"
	}
	items := f.filtered()
	title := fmt.Sprintf("Findings (%d)  severity: %s", f.total, sev)
	if f.filter != "" {
		title = fmt.Sprintf("Findings (%d/%d)  severity: %s", len(items), f.total, sev)
	}
	header := styleTitle.Padding(0, 1).Render(title)

	if f.loading && len(f.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if f.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+f.err.Error()))
	}
	if len(f.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No findings")))
	}
	if len(items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No findings match \""+f.filter+"\"  ( / edit · esc clear )")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	// dot column is 1 visible char (colored), no width padding needed.
	colRepo := 20
	colCategory := 13
	colFile := 22
	colConf := 5
	colTitle := 26
	// dot=1 + 5 gaps of 2 = 11 + leading 2
	remaining := w - 1 - colRepo - colCategory - colFile - colConf - 13
	if remaining > colTitle {
		colTitle = remaining
	}

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("     %-*s  %-*s  %-*s  %-*s  %-*s",
			colTitle, "Title",
			colRepo, "Repository",
			colCategory, "Category",
			colFile, "File:Line",
			colConf, "Conf%"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := f.offset + visibleRows
	if end > len(items) {
		end = len(items)
	}
	for i := f.offset; i < end; i++ {
		item := items[i]
		title := truncate(item.Title, colTitle)
		repo := truncate(item.RepoFullName, colRepo)
		category := truncate(item.Category, colCategory)
		fileLine := ""
		if item.FilePath != nil {
			fileLine = *item.FilePath
			if item.LineStart != nil {
				fileLine = fmt.Sprintf("%s:%d", fileLine, *item.LineStart)
			}
		}
		fileLine = truncate(fileLine, colFile)
		conf := ""
		if item.Confidence != nil {
			conf = fmt.Sprintf("%.0f%%", *item.Confidence*100)
		}

		var line string
		if i == f.cursor {
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s  %-*s",
				"●",
				colTitle, title,
				colRepo, repo,
				colCategory, category,
				colFile, fileLine,
				colConf, conf,
			))
		} else {
			line = fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s  %-*s",
				severityDot(item.Severity),
				colTitle, title,
				colRepo, repo,
				colCategory, category,
				colFile, fileLine,
				colConf, conf,
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", f.cursor+1, len(items))) +
		cappedNote(len(f.items), f.total)
	if f.filter != "" && !f.filtering {
		pos = styleSubtitle.Render(fmt.Sprintf("  filter %q  ", f.filter)) + pos
	}
	if f.filtering {
		pos = "  " + styleTitle.Render(" filter ") +
			"  " + f.filter + "█" +
			styleSubtitle.Render("    [enter] keep   [esc] clear")
	}

	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}
