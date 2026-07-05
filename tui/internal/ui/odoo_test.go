package ui

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"

	"reva-tui/internal/api"
)

func TestOdooLoadsAndShowsCost(t *testing.T) {
	o := newOdoo(&api.MockClient{})
	o.width, o.height = 140, 30
	data, _ := (&api.MockClient{}).OdooInstances()
	o, _ = o.update(odooLoadedMsg{data: data})
	if len(o.items) != 2 {
		t.Fatalf("expected 2 instances, got %d", len(o.items))
	}
	out := o.view(140, 30)
	if out == "" {
		t.Fatal("empty view")
	}
}

func TestOdooViewShowsBudgetColumn(t *testing.T) {
	o := newOdoo(&api.MockClient{})
	b := 10.0
	page := &api.OdooInstancePage{
		Items: []api.OdooInstanceSummary{
			{
				ID: 1, Name: "capped", KeyPrefix: "reva_odoo_x", Active: true,
				DailyBudgetUSD: &b,
				Cost: api.OdooInstanceCost{Last24h: api.WindowCost{
					Analysis: api.TaskCost{CostUSD: 3.2}}},
			},
			{ID: 2, Name: "unlimited", KeyPrefix: "reva_odoo_y", Active: true},
		},
		Total: 2,
	}
	o, _ = o.update(odooLoadedMsg{data: page})
	o.width, o.height = 140, 30
	out := o.view(140, 30)
	if !strings.Contains(out, "3.20/10") {
		t.Fatalf("expected budget cell '3.20/10' in view:\n%s", out)
	}
	if !strings.Contains(out, "Budget") {
		t.Fatalf("expected Budget column header:\n%s", out)
	}
}

func TestOdooCreateFlowPostsAndShowsKey(t *testing.T) {
	o := newOdoo(&api.MockClient{})
	o.width, o.height = 140, 30
	o, _ = o.update(keyMsg("n")) // enter create mode
	if !o.creating {
		t.Fatal("n did not enter create mode")
	}
	for _, ch := range "ACME" { // field 0: name
		o, _ = o.update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{ch}})
	}
	o, _ = o.update(keyMsg("enter"))    // advance to callback_url
	o, _ = o.update(keyMsg("enter"))    // advance to outbound key (leave url blank)
	o, cmd := o.update(keyMsg("enter")) // submit
	if cmd == nil {
		t.Fatal("submit produced no command")
	}
	o, _ = o.update(cmd().(odooCreatedMsg))
	if o.newKey == "" {
		t.Fatal("minted key not surfaced after create")
	}
}
