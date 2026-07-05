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

	showEvents  bool
	events      []api.OpsEventEntry
	eventsTotal int
	eventsErr   error
}

func newFailures(client api.ClientIface) Failures {
	return Failures{client: client, loading: true}
}

func (f Failures) load() tea.Cmd {
	client := f.client
	return tea.Batch(
		func() tea.Msg {
			data, err := client.Failures(50)
			return failuresLoadedMsg{data: data, err: err}
		},
		func() tea.Msg {
			data, err := client.OpsEvents(100)
			return opsEventsLoadedMsg{data: data, err: err}
		},
	)
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

	case opsEventsLoadedMsg:
		f.eventsErr = m.err
		if m.data != nil {
			f.events = m.data.Items
			f.eventsTotal = m.data.Total
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
		case "v":
			f.showEvents = !f.showEvents
			f.cursor, f.offset = 0, 0
			return f, nil
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
	if f.showEvents {
		return f.eventsView(w, h)
	}

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
		// Status is a single visible char in rows (%s), so pad the header's
		// status cell to 1 too — not colStatus — or every column drifts right.
		fmt.Sprintf("  %s  %-*s  %-*s  %-*s",
			" ",
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

func (f Failures) eventsView(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(
		fmt.Sprintf("Component Events (%d)", f.eventsTotal))
	if f.eventsErr != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+f.eventsErr.Error()))
	}
	if len(f.events) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No component degradations — all good")),
			styleSubtitle.Render("  [v] back to failed runs"))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	colSev, colComp, colEvent, colWhen := 8, 16, 28, 10
	colDetail := w - colSev - colComp - colEvent - colWhen - 12
	if colDetail < 10 {
		colDetail = 10
	}

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colSev, "Severity", colComp, "Component", colEvent, "Event",
			colWhen, "When", colDetail, "Detail"))
	rows := []string{hdr}

	end := visibleRows
	if end > len(f.events) {
		end = len(f.events)
	}
	for _, e := range f.events[:end] {
		detail := ""
		for k, v := range e.Detail {
			detail += fmt.Sprintf("%s=%v ", k, v)
		}
		sev := e.Severity
		if e.Severity == "error" {
			sev = styleStatusFailed.Render("error")
		}
		rows = append(rows, fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colSev, sev,
			colComp, truncate(e.Component, colComp),
			colEvent, truncate(e.Event, colEvent),
			colWhen, relativeTime(e.CreatedAt),
			colDetail, truncate(strings.TrimSpace(detail), colDetail)))
	}
	footer := styleSubtitle.Render("  [v] back to failed runs   [r] refresh") +
		cappedNote(end, f.eventsTotal)
	return lipgloss.JoinVertical(lipgloss.Left, header, "",
		strings.Join(rows, "\n"), "", footer)
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
