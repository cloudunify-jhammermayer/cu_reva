package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"reva-tui/internal/api"
)

type Odoo struct {
	client    api.ClientIface
	items     []api.OdooInstanceSummary
	total     int
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string

	creating   bool // capturing the create form
	createStep int  // 0=name, 1=callback_url, 2=outbound key
	createName string
	createURL  string
	createKey  string

	confirmingRotate bool // awaiting y/n confirmation for an irreversible key rotation
	confirmingDelete bool // awaiting y/n confirmation for an instance delete

	newKey string // plaintext key to display once after create/rotate
}

func newOdoo(client api.ClientIface) Odoo {
	return Odoo{client: client, loading: true}
}

func (o Odoo) load() tea.Cmd {
	client := o.client
	return func() tea.Msg {
		data, err := client.OdooInstances()
		return odooLoadedMsg{data: data, err: err}
	}
}

func (o Odoo) update(msg tea.Msg) (Odoo, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return o, o.load()

	case odooLoadedMsg:
		o.loading = false
		o.err = m.err
		if m.data != nil {
			o.items = m.data.Items
			o.total = m.data.Total
		}
		if o.cursor >= len(o.items) {
			o.cursor, o.offset = 0, 0
		}

	case odooCreatedMsg:
		if m.err != nil {
			o.statusMsg = styleStatusFailed.Render("create failed: " + m.err.Error())
			return o, nil
		}
		o.newKey = m.created.APIKey
		o.statusMsg = styleStatusCompleted.Render("created " + m.created.Name)
		return o, o.load()

	case odooActionMsg:
		if m.err != nil {
			o.statusMsg = styleStatusFailed.Render("action failed: " + m.err.Error())
			return o, nil
		}
		return o, o.load()

	case tea.KeyMsg:
		if o.newKey != "" {
			// Dismiss the key banner only on an explicit ack. A stray key —
			// e.g. a buffered Enter typed right after the rotate confirm —
			// must not eat the one chance to copy the key.
			if m.String() == "esc" || m.String() == "enter" {
				o.newKey = ""
			}
			return o, nil
		}
		if o.confirmingRotate {
			o.confirmingRotate = false
			if m.String() == "y" && o.cursor < len(o.items) {
				id := o.items[o.cursor].ID
				client := o.client
				o.statusMsg = styleSubtitle.Render("rotating key...")
				return o, func() tea.Msg {
					created, err := client.RotateOdooInstanceKey(id)
					return odooCreatedMsg{created: created, err: err}
				}
			}
			o.statusMsg = styleSubtitle.Render("rotation cancelled")
			return o, nil
		}
		if o.confirmingDelete {
			o.confirmingDelete = false
			if m.String() == "y" && o.cursor < len(o.items) {
				it := o.items[o.cursor]
				client := o.client
				o.statusMsg = styleSubtitle.Render("deleting " + it.Name + " ...")
				return o, func() tea.Msg {
					return odooActionMsg{err: client.DeleteOdooInstance(it.ID)}
				}
			}
			o.statusMsg = styleSubtitle.Render("delete cancelled")
			return o, nil
		}
		if o.creating {
			return o.updateCreate(m)
		}
		visibleRows := o.height - 6
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, off, ok := listNav(m.String(), o.cursor, o.offset, len(o.items), visibleRows); ok {
			o.cursor, o.offset = c, off
			return o, nil
		}
		switch m.String() {
		case "n":
			o.creating, o.createStep = true, 0
			o.createName, o.createURL, o.createKey, o.statusMsg = "", "", "", ""
		case "ctrl+r":
			// Key rotation is irreversible (the old key stops working immediately),
			// so it's gated behind a confirmation and kept off "r" — which means
			// refresh on every other tab.
			if o.cursor < len(o.items) {
				o.confirmingRotate = true
				o.statusMsg = styleStatusFailed.Render(
					"rotate API key for " + o.items[o.cursor].Name + "? invalidates the current key — press y to confirm, any other key to cancel")
			}
		case "D":
			// Deleting is destructive (removes the instance and detaches its
			// run history), so it's confirm-gated like key rotation. ctrl+d
			// is taken — listNav uses it for half-page-down.
			if o.cursor < len(o.items) {
				o.confirmingDelete = true
				o.statusMsg = styleStatusFailed.Render(
					"delete " + o.items[o.cursor].Name + "? removes the instance and detaches its history — press y to confirm, any other key to cancel")
			}
		case "t":
			if o.cursor < len(o.items) {
				it := o.items[o.cursor]
				client := o.client
				o.statusMsg = styleSubtitle.Render("toggling active...")
				return o, func() tea.Msg {
					return odooActionMsg{err: client.SetOdooInstanceActive(it.ID, !it.Active)}
				}
			}
		case "r", "R":
			o.loading, o.statusMsg = true, ""
			return o, o.load()
		}
	}
	return o, nil
}

func (o Odoo) updateCreate(m tea.KeyMsg) (Odoo, tea.Cmd) {
	switch m.Type {
	case tea.KeyEsc:
		o.creating = false
		return o, nil
	case tea.KeyEnter:
		if o.createStep < 2 {
			o.createStep++
			return o, nil
		}
		name, url, key := o.createName, o.createURL, o.createKey
		o.creating = false
		if strings.TrimSpace(name) == "" {
			o.statusMsg = styleStatusFailed.Render("name is required")
			return o, nil
		}
		client := o.client
		o.statusMsg = styleSubtitle.Render("creating " + name + " ...")
		return o, func() tea.Msg {
			created, err := client.CreateOdooInstance(name, url, key)
			return odooCreatedMsg{created: created, err: err}
		}
	case tea.KeyBackspace:
		switch o.createStep {
		case 0:
			o.createName = dropLastRune(o.createName)
		case 1:
			o.createURL = dropLastRune(o.createURL)
		case 2:
			o.createKey = dropLastRune(o.createKey)
		}
	case tea.KeyRunes, tea.KeySpace:
		appended := string(m.Runes)
		switch o.createStep {
		case 0:
			o.createName += appended
		case 1:
			o.createURL += appended
		case 2:
			o.createKey += appended
		}
	}
	return o, nil
}

func (o Odoo) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Odoo Instances (%d)", o.total))

	if o.newKey != "" {
		banner := lipgloss.JoinVertical(lipgloss.Left,
			styleStatusCompleted.Render("  New API key — copy it now, it will not be shown again:"),
			"",
			"    "+styleTitle.Render(o.newKey),
			"",
			styleSubtitle.Render("  press esc or enter to dismiss"))
		return lipgloss.JoinVertical(lipgloss.Left, header, "", banner)
	}
	if o.creating {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", o.createForm())
	}
	if o.loading && len(o.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if o.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+o.err.Error()))
	}
	if len(o.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No Odoo instances — press n to add one")))
	}

	colName, colPrefix, colHost, colVer, colA, colI, colW, colB := 24, 16, 24, 6, 10, 10, 9, 12
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %-*s  %*s  %*s  %*s  %*s  %*s",
			colName, "Name", colPrefix, "Key", colHost, "Callback",
			colVer, "Ver",
			colA, "Life A$", colI, "Life I$", colW, "24h$", colW, "30d$",
			colB, "Budget"))

	visibleRows := h - 6
	if visibleRows < 1 {
		visibleRows = 1
	}
	end := o.offset + visibleRows
	if end > len(o.items) {
		end = len(o.items)
	}
	rows := []string{hdr}
	for i := o.offset; i < end; i++ {
		it := o.items[i]
		host := it.CallbackURL
		if host == "" {
			host = "—"
		}
		version := "—"
		if it.OdooVersion != nil && *it.OdooVersion != "" {
			version = *it.OdooVersion
		}
		life := it.Cost.Lifetime
		// Total() covers every kind the quota gate sums (incl. timesheets) —
		// the budget cell must agree with the API's 429 behavior.
		d24 := it.Cost.Last24h.Total()
		d30 := it.Cost.Last30d.Total()
		budget := "—"
		overBudget := false
		if it.DailyBudgetUSD != nil {
			budget = fmt.Sprintf("%.2f/%.0f", d24, *it.DailyBudgetUSD)
			overBudget = *it.DailyBudgetUSD > 0 && d24 >= 0.9*(*it.DailyBudgetUSD)
		}
		active := "+"
		if !it.Active {
			active = "x"
		}
		line := fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s  %*.2f  %*.2f  %*.2f  %*.2f  %*s",
			active,
			colName, truncate(it.Name, colName),
			colPrefix, truncate(it.KeyPrefix, colPrefix),
			colHost, truncate(host, colHost),
			colVer, truncate(version, colVer),
			colA, life.Analysis.CostUSD, colI, life.Issues.CostUSD,
			colW, d24, colW, d30, colB, budget)
		if i == o.cursor {
			line = styleSelected.Width(w - 2).Render(line)
		} else if overBudget {
			line = styleStatusFailed.Render(line)
		}
		rows = append(rows, line)
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   n add · ^R rotate key · D delete · t toggle · r refresh", o.cursor+1, len(o.items)))
	if o.statusMsg != "" {
		pos = "  " + o.statusMsg
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", strings.Join(rows, "\n"), "", pos)
}

func (o Odoo) createForm() string {
	field := func(idx int, label, val string) string {
		cursor := ""
		if o.createStep == idx {
			cursor = "█"
		}
		return fmt.Sprintf("  %-14s %s%s", label+":", val, cursor)
	}
	return lipgloss.JoinVertical(lipgloss.Left,
		styleTitle.Render("  Add Odoo instance"),
		"",
		field(0, "Name", o.createName),
		field(1, "Callback URL", o.createURL),
		field(2, "Outbound key", o.createKey),
		"",
		styleSubtitle.Render("  [enter] next/submit   [esc] cancel"))
}
