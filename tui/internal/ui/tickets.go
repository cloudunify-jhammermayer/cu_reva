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

type Tickets struct {
	client    api.ClientIface
	odooURL   string
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

func newTickets(client api.ClientIface, odooURL string) Tickets {
	return Tickets{client: client, odooURL: odooURL, loading: true}
}

func (t Tickets) load() tea.Cmd {
	return func() tea.Msg {
		data, err := t.client.TicketAnalyses(100)
		return ticketAnalysesLoadedMsg{data: data, err: err}
	}
}

func (t Tickets) requeueCmd(id int) tea.Cmd {
	return func() tea.Msg {
		err := t.client.RequeueTicket(id)
		return ticketRequeuedMsg{id: id, err: err}
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
			t.statusMsg = fmt.Sprintf("requeue failed: %s", m.err)
		} else {
			t.statusMsg = fmt.Sprintf("analysis #%d requeued", m.id)
		}

	case tea.KeyMsg:
		t.statusMsg = ""
		visibleRows := t.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		switch m.String() {
		case "j", "down":
			t.cursor, t.offset = moveCursor(t.cursor, t.offset, len(t.items), visibleRows, true)
		case "k", "up":
			t.cursor, t.offset = moveCursor(t.cursor, t.offset, len(t.items), visibleRows, false)
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
		case "o":
			if len(t.items) > 0 && t.cursor < len(t.items) {
				item := t.items[t.cursor]
				url := fmt.Sprintf("%s/web#model=%s&id=%d&view_type=form",
					t.odooURL, item.ModelName, item.TicketID)
				_ = exec.Command("xdg-open", url).Start()
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
	colModule := 12
	colStatus := 10
	colCost := 10
	colWhen := w - colID - colTicket - colModule - colStatus - colCost - 12

	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colID, "ID",
			colTicket, "Ticket",
			colModule, "Module",
			colStatus, "Status",
			colCost, "Cost",
			colWhen, "When"),
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
		module := truncate(strings.SplitN(item.ModelName, ".", 2)[0], colModule)
		cost := ""
		if item.EstimatedCostUSD != nil {
			cost = fmt.Sprintf("$%.4f", *item.EstimatedCostUSD)
		}
		when := relativeTime(item.CreatedAt)
		if item.CompletedAt != nil {
			dur := int(item.CompletedAt.Sub(item.CreatedAt).Milliseconds())
			when += "  " + fmtDurationMS(dur)
		}
		when = truncate(when, colWhen)
		statusStr := ticketStatusSymbol(item.Status, item.CreatedAt) + " " + item.Status

		if i == t.cursor {
			rows = append(rows, styleSelected.Width(w-2).Render(
				fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
					colID, id,
					colTicket, ticket,
					colModule, module,
					colStatus, item.Status,
					colCost, cost,
					colWhen, when),
			))
		} else {
			rows = append(rows, fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
				colID, id,
				colTicket, ticket,
				colModule, module,
				colStatus, statusStr,
				colCost, cost,
				colWhen, when,
			))
		}
	}

	table := strings.Join(rows, "\n")
	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", t.cursor+1, len(t.items)))

	var extras []string
	if t.cursor < len(t.items) {
		sel := t.items[t.cursor]
		var meta []string
		meta = append(meta, "field:"+sel.FieldName)
		if sel.Model != nil {
			meta = append(meta, "model:"+truncate(*sel.Model, 24))
		}
		if sel.InputTokens != nil && sel.OutputTokens != nil {
			meta = append(meta, fmt.Sprintf("tokens:%d in / %d out", *sel.InputTokens, *sel.OutputTokens))
		}
		extras = append(extras, styleSubtitle.Render("  "+strings.Join(meta, "  ")))
		if sel.ErrorMessage != nil && *sel.ErrorMessage != "" {
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

func ticketStatusSymbol(status string, createdAt time.Time) string {
	switch status {
	case "completed":
		return styleStatusCompleted.Render("+")
	case "failed":
		return styleStatusFailed.Render("x")
	case "pending":
		if time.Since(createdAt) > 10*time.Minute {
			return styleStatusFailed.Render("?")
		}
		return styleStatusStale.Render("~")
	default:
		return styleStatusOther.Render("-")
	}
}
