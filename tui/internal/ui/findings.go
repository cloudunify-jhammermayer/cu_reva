package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Findings struct {
	client          api.ClientIface
	items           []api.FindingSummary
	total           int
	err             error
	loading         bool
	cursor          int
	offset          int
	width           int
	height          int
	severityFilter  string
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
		if f.cursor >= len(f.items) {
			f.cursor = 0
			f.offset = 0
		}

	case tea.KeyMsg:
		visibleRows := f.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "j", "down":
			if f.cursor < len(f.items)-1 {
				f.cursor++
				if f.cursor >= f.offset+visibleRows {
					f.offset++
				}
			}
		case "k", "up":
			if f.cursor > 0 {
				f.cursor--
				if f.cursor < f.offset {
					f.offset--
				}
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
	filterLabel := f.severityFilter
	if filterLabel == "" {
		filterLabel = "all"
	}
	header := styleTitle.Padding(0, 1).Render(
		fmt.Sprintf("Findings (%d)  filter: %s", f.total, filterLabel),
	)

	if f.loading && len(f.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading…")))
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

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	// dot column is just 1 visible char (colored), no width padding needed
	colTitle    := 40
	colCategory := 16
	colFile     := 28
	colConf     := 5
	// dot=1 + spacing=2+2+2+2 = 9 extra chars
	remaining := w - 1 - colCategory - colFile - colConf - 10
	if remaining > colTitle {
		colTitle = remaining
	}

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %-*s",
			colTitle, "Title",
			colCategory, "Category",
			colFile, "File:Line",
			colConf, "Conf%"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := f.offset + visibleRows
	if end > len(f.items) {
		end = len(f.items)
	}
	for i := f.offset; i < end; i++ {
		item := f.items[i]
		title := truncate(item.Title, colTitle)
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
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				"●",
				colTitle, title,
				colCategory, category,
				colFile, fileLine,
				colConf, conf,
			))
		} else {
			line = fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s",
				severityDot(item.Severity),
				colTitle, title,
				colCategory, category,
				colFile, fileLine,
				colConf, conf,
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", f.cursor+1, len(f.items)))

	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}
