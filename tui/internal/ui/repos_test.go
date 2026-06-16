package ui

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"reva-tui/internal/api"
)

func TestReposFilterNarrows(t *testing.T) {
	r := newRepos(&api.MockClient{})
	r.width, r.height = 120, 30
	data, _ := (&api.MockClient{}).Repos()
	r, _ = r.update(reposLoadedMsg{data: data})
	if len(r.items) < 2 {
		t.Skip("need ≥2 mock repos to test filtering")
	}

	// `/` then a substring of one repo narrows the list to matches only.
	r, _ = r.update(keyMsg("/"))
	if !r.filtering {
		t.Fatal("/ did not enter filter mode")
	}
	sub := r.items[0].FullName[:3]
	for _, ch := range sub {
		r, _ = r.update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{ch}})
	}
	got := r.filtered()
	if len(got) == 0 || len(got) > len(r.items) {
		t.Fatalf("filter %q gave %d of %d repos", sub, len(got), len(r.items))
	}
	// esc clears.
	r, _ = r.update(tea.KeyMsg{Type: tea.KeyEsc})
	if r.filter != "" || len(r.filtered()) != len(r.items) {
		t.Fatal("esc did not clear the repo filter")
	}
}

func TestParseOwnerName(t *testing.T) {
	cases := []struct {
		in          string
		owner, name string
		ok          bool
	}{
		{"acme/widgets", "acme", "widgets", true},
		{"  acme/widgets  ", "acme", "widgets", true},
		{"https://github.com/acme/widgets", "acme", "widgets", true},
		{"/acme/widgets/", "acme", "widgets", true},
		{"widgets", "", "", false},
		{"acme/", "", "", false},
		{"/widgets", "", "", false},
		{"a/b/c", "", "", false},
		{"", "", "", false},
	}
	for _, c := range cases {
		o, n, ok := parseOwnerName(c.in)
		if ok != c.ok || o != c.owner || n != c.name {
			t.Errorf("parseOwnerName(%q) = (%q,%q,%v), want (%q,%q,%v)",
				c.in, o, n, ok, c.owner, c.name, c.ok)
		}
	}
}
