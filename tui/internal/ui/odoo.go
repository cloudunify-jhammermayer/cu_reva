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
		if o.newKey != "" { // dismiss the key banner on any key
			o.newKey = ""
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
		case "r":
			if o.cursor < len(o.items) {
				id := o.items[o.cursor].ID
				client := o.client
				o.statusMsg = styleSubtitle.Render("rotating key...")
				return o, func() tea.Msg {
					created, err := client.RotateOdooInstanceKey(id)
					return odooCreatedMsg{created: created, err: err}
				}
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
		case "R":
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
		f := func(s string) string {
			if len(s) > 0 {
				return s[:len(s)-1]
			}
			return s
		}
		switch o.createStep {
		case 0:
			o.createName = f(o.createName)
		case 1:
			o.createURL = f(o.createURL)
		case 2:
			o.createKey = f(o.createKey)
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
			styleSubtitle.Render("  press any key to dismiss"))
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

	colName, colPrefix, colHost, colA, colI, colW := 24, 16, 26, 10, 10, 9
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %*s  %*s  %*s  %*s",
			colName, "Name", colPrefix, "Key", colHost, "Callback",
			colA, "Life A$", colI, "Life I$", colW, "24h$", colW, "30d$"))

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
		life := it.Cost.Lifetime
		d24 := it.Cost.Last24h.Analysis.CostUSD + it.Cost.Last24h.Issues.CostUSD
		d30 := it.Cost.Last30d.Analysis.CostUSD + it.Cost.Last30d.Issues.CostUSD
		active := "+"
		if !it.Active {
			active = "x"
		}
		line := fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %*.2f  %*.2f  %*.2f  %*.2f",
			active,
			colName, truncate(it.Name, colName),
			colPrefix, truncate(it.KeyPrefix, colPrefix),
			colHost, truncate(host, colHost),
			colA, life.Analysis.CostUSD, colI, life.Issues.CostUSD,
			colW, d24, colW, d30)
		if i == o.cursor {
			line = styleSelected.Width(w - 2).Render(line)
		}
		rows = append(rows, line)
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   n add · r rotate · t toggle · R refresh", o.cursor+1, len(o.items)))
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
