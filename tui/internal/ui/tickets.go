package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Tickets struct {
	client    api.ClientIface
	items     []api.TicketAnalysisSummary
	total     int
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string
}

func newTickets(client api.ClientIface) Tickets {
	return Tickets{client: client, loading: true}
}

func (t Tickets) load() tea.Cmd {
	return func() tea.Msg {
		data, err := t.client.TicketAnalyses(100)
		return ticketAnalysesLoadedMsg{data: data, err: err}
	}
}

func (t Tickets) requeueCmd(id int) tea.Cmd {
	return func() tea.Msg {
		url, err := t.client.RequeueTicket(id)
		return ticketRequeuedMsg{id: id, url: url, err: err}
	}
}

func (t Tickets) update(msg tea.Msg) (Tickets, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return t, t.load()

	case ticketAnalysesLoadedMsg:
		t.loading = false
		t.err = m.err
		if m.data != nil {
			t.items = m.data.Items
			t.total = m.data.Total
		}
		if t.cursor >= len(t.items) {
			t.cursor = 0
			t.offset = 0
		}

	case ticketRequeuedMsg:
		if m.err != nil {
			t.statusMsg = fmt.Sprintf("POST %s -> %s", m.url, m.err)
		} else {
			t.statusMsg = fmt.Sprintf("POST %s -> 202 accepted", m.url)
		}

	case tea.KeyMsg:
		t.statusMsg = ""
		visibleRows := t.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "j", "down":
			if t.cursor < len(t.items)-1 {
				t.cursor++
				if t.cursor >= t.offset+visibleRows {
					t.offset++
				}
			}
		case "k", "up":
			if t.cursor > 0 {
				t.cursor--
				if t.cursor < t.offset {
					t.offset--
				}
			}
		case "r":
			t.loading = true
			return t, t.load()
		case "e":
			if len(t.items) > 0 && t.cursor < len(t.items) {
				item := t.items[t.cursor]
				if item.Status == "failed" || item.Status == "completed" {
					return t, t.requeueCmd(item.ID)
				}
				t.statusMsg = "only failed or completed analyses can be requeued"
			}
		}
	}
	return t, nil
}

func (t Tickets) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Ticket Analyses  (%d)", t.total))

	if t.loading && len(t.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if t.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+t.err.Error()))
	}
	if len(t.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No ticket analyses")))
	}

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}

	colID := 5
	colTicket := 9
	colModel := 14
	colStatus := 10
	colCost := 10
	colCreated := w - colID - colTicket - colModel - colStatus - colCost - 12

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colID, "ID",
			colTicket, "Ticket",
			colModel, "Model",
			colStatus, "Status",
			colCost, "Cost",
			colCreated, "Created"),
	)

	var rows []string
	rows = append(rows, hdr)

	end := t.offset + visibleRows
	if end > len(t.items) {
		end = len(t.items)
	}
	for i := t.offset; i < end; i++ {
		item := t.items[i]
		id := fmt.Sprintf("%d", item.ID)
		ticket := fmt.Sprintf("#%d", item.TicketID)
		model := ""
		if item.Model != nil {
			model = truncate(*item.Model, colModel)
		}
		cost := ""
		if item.EstimatedCostUSD != nil {
			cost = fmt.Sprintf("$%.4f", *item.EstimatedCostUSD)
		}
		created := item.CreatedAt.Local().Format("01-02 15:04")
		statusStr := ticketStatusSymbol(item.Status) + " " + item.Status

		if i == t.cursor {
			rows = append(rows, styleSelected.Width(w-2).Render(
				fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
					colID, id,
					colTicket, ticket,
					colModel, model,
					colStatus, item.Status,
					colCost, cost,
					colCreated, created),
			))
		} else {
			rows = append(rows, fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
				colID, id,
				colTicket, ticket,
				colModel, model,
				colStatus, statusStr,
				colCost, cost,
				colCreated, created,
			))
		}
	}

	table := strings.Join(rows, "\n")
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", t.cursor+1, len(t.items)))

	var extras []string
	if t.cursor < len(t.items) {
		if sel := t.items[t.cursor]; sel.ErrorMessage != nil && *sel.ErrorMessage != "" {
			extras = append(extras, styleStatusFailed.Render("  error: "+*sel.ErrorMessage))
		}
	}
	if t.statusMsg != "" {
		extras = append(extras, styleSubtitle.Render("  "+t.statusMsg))
	}

	parts := []string{header, "", table, "", pos}
	parts = append(parts, extras...)
	return lipgloss.JoinVertical(lipgloss.Left, parts...)
}

func ticketStatusSymbol(status string) string {
	switch status {
	case "completed":
		return styleStatusCompleted.Render("+")
	case "failed":
		return styleStatusFailed.Render("x")
	case "pending":
		return styleStatusStale.Render("...")
	default:
		return styleStatusOther.Render("-")
	}
}
