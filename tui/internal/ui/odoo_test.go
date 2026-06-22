package ui

import (
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
	o, _ = o.update(keyMsg("enter")) // advance to callback_url
	o, _ = o.update(keyMsg("enter")) // advance to outbound key (leave url blank)
	o, cmd := o.update(keyMsg("enter")) // submit
	if cmd == nil {
		t.Fatal("submit produced no command")
	}
	o, _ = o.update(cmd().(odooCreatedMsg))
	if o.newKey == "" {
		t.Fatal("minted key not surfaced after create")
	}
}
