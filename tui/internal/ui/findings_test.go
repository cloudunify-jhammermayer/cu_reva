package ui

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"reva-tui/internal/api"
)

// typeInto feeds each rune of s as a separate key message (filter capture).
func typeInto(f Findings, s string) Findings {
	for _, r := range s {
		f, _ = f.update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
	}
	return f
}

func TestFindingsRepoColumnAndFilter(t *testing.T) {
	f := newFindings(&api.MockClient{})
	f.width, f.height = 140, 30
	data, _ := (&api.MockClient{}).Findings("", "", 200)
	f, _ = f.update(findingsLoadedMsg{data: data})
	if len(f.items) == 0 {
		t.Fatal("expected mock findings")
	}

	// The repo column is rendered.
	out := f.view(140, 30)
	if !strings.Contains(out, "Repository") {
		t.Fatal("view missing Repository column header")
	}
	if !strings.Contains(out, "acme/") {
		t.Fatalf("view missing repo in rows:\n%s", out)
	}

	// `/` enters filter mode; typing a repo substring narrows the list.
	f, _ = f.update(keyMsg("/"))
	if !f.filtering {
		t.Fatal("/ did not enter filter mode")
	}
	f = typeInto(f, "widgets")
	got := f.filtered()
	if len(got) == 0 || len(got) == len(f.items) {
		t.Fatalf("filter did not narrow: %d of %d", len(got), len(f.items))
	}
	for _, it := range got {
		if !strings.Contains(it.RepoFullName, "widgets") {
			t.Fatalf("filtered finding outside repo: %q", it.RepoFullName)
		}
	}

	// esc clears the filter entirely.
	f, _ = f.update(tea.KeyMsg{Type: tea.KeyEsc})
	if f.filtering || f.filter != "" {
		t.Fatal("esc did not clear the filter")
	}
	if len(f.filtered()) != len(f.items) {
		t.Fatal("filter still applied after clear")
	}
}

func TestFindingsGNavJumpsToBottom(t *testing.T) {
	f := newFindings(&api.MockClient{})
	f.width, f.height = 140, 12
	data, _ := (&api.MockClient{}).Findings("", "", 200)
	f, _ = f.update(findingsLoadedMsg{data: data})

	f, _ = f.update(keyMsg("G"))
	if f.cursor != len(f.items)-1 {
		t.Fatalf("G cursor = %d, want %d", f.cursor, len(f.items)-1)
	}
	f, _ = f.update(keyMsg("g"))
	if f.cursor != 0 || f.offset != 0 {
		t.Fatalf("g cursor/offset = %d/%d, want 0/0", f.cursor, f.offset)
	}
}
