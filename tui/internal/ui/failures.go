package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Failures struct {
	client    api.ClientIface
	items     []api.ReviewDetail
	total     int
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string
}

func newFailures(client api.ClientIface) Failures {
	return Failures{client: client, loading: true}
}

func (f Failures) load() tea.Cmd {
	return func() tea.Msg {
		data, err := f.client.Failures(50)
		return failuresLoadedMsg{data: data, err: err}
	}
}

func (f Failures) update(msg tea.Msg) (Failures, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return f, f.load()

	case failuresLoadedMsg:
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

	case requeuedMsg:
		if m.err != nil {
			f.statusMsg = styleStatusFailed.Render("requeue failed: " + m.err.Error())
		} else {
			f.statusMsg = styleStatusCompleted.Render("queued - review will run shortly")
		}

	case tea.KeyMsg:
		visibleRows := f.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), f.cursor, f.offset, len(f.items), visibleRows); ok {
			f.cursor, f.offset = c, o
			return f, nil
		}
		switch m.String() {
		case "r":
			f.loading = true
			f.statusMsg = ""
			return f, f.load()
		case "e":
			if f.cursor < len(f.items) {
				id := f.items[f.cursor].ID
				client := f.client
				return f, func() tea.Msg {
					err := client.Requeue(id)
					return requeuedMsg{id: id, err: err}
				}
			}
		}
	}
	return f, nil
}

func (f Failures) view(w, h int) string {

	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Failures  (%d)", f.total))

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
				styleSubtitle.Render("No failures — all good")))
	}

	// Reserve the fixed chrome around the table: header + blank + table
	// column-header + blank + 8-line detail panel + blank + position line = 14
	// non-data lines. Was h-5, which overran by the detail panel + position line
	// when the list filled the table, so the MaxHeight clamp cut them off (M23).
	// If the terminal is too short for the detail panel plus a few rows, drop the
	// detail so the list and position line still fit.
	showDetail := true
	visibleRows := h - 14
	if visibleRows < 3 {
		showDetail = false
		visibleRows = h - 5 // compact: header + blank + table hdr + blank + pos
	}
	if visibleRows < 1 {
		visibleRows = 1
	}

	colStatus := 3
	colRepo := 22
	colPR := 6
	colErr := w - colStatus - colRepo - colPR - 8

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s",
			colStatus, " ",
			colRepo, "Repository",
			colPR, "PR#",
			colErr, "Error"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := f.offset + visibleRows
	if end > len(f.items) {
		end = len(f.items)
	}
	for i := f.offset; i < end; i++ {
		item := f.items[i]
		repo := truncate(item.RepoFullName, colRepo)
		prNum := fmt.Sprintf("#%d", item.PRNumber)
		errMsg := ""
		if item.ErrorMessage != nil {
			errMsg = *item.ErrorMessage
		} else if item.Status == "stale" {
			errMsg = "timed out"
		}
		errMsg = truncate(errMsg, colErr)

		var line string
		if i == f.cursor {
			line = styleSelected.Width(w - 2).Render(fmt.Sprintf("  %s  %-*s  %-*s  %-*s",
				statusChar(item.Status),
				colRepo, repo,
				colPR, prNum,
				colErr, errMsg,
			))
		} else {
			line = fmt.Sprintf("  %s  %-*s  %-*s  %-*s",
				statusSymbol(item.Status),
				colRepo, repo,
				colPR, prNum,
				colErr, errMsg,
			)
		}
		rows = append(rows, line)
	}

	table := strings.Join(rows, "\n")

	var posLine string
	if f.statusMsg != "" {
		posLine = f.statusMsg
	} else {
		posLine = styleSubtitle.Render(fmt.Sprintf("  %d/%d", f.cursor+1, len(f.items))) +
			cappedNote(len(f.items), f.total)
	}

	if showDetail && f.cursor < len(f.items) {
		detail := f.renderDetail(f.items[f.cursor], w)
		return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", detail, "", posLine)
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", posLine)
}

func (f Failures) renderDetail(item api.ReviewDetail, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s", item.PRNumber, truncate(item.PRTitle, w-20))) + "\n")
	b.WriteString(fmt.Sprintf("  Repo    %s\n", item.RepoFullName))
	b.WriteString(fmt.Sprintf("  Status  %s  %s\n", statusSymbol(item.Status), item.Status))
	if item.ErrorClass != nil {
		b.WriteString(fmt.Sprintf("  Class   %s\n", styleSubtitle.Render(*item.ErrorClass)))
	}
	if item.ErrorMessage != nil {
		b.WriteString(fmt.Sprintf("  Error   %s\n", styleStatusFailed.Render(truncate(*item.ErrorMessage, w-12))))
	}
	return styleBorder.Width(w - 2).Height(6).Render(b.String())
}
