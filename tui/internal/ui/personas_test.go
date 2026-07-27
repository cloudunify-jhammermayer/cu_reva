package ui

import (
	"strings"
	"testing"

	"reva-tui/internal/api"
)

// personaUpdateStubClient captures the UpdatePersona call so a test can assert
// on the full body sent — in particular that toggling `active` resends every
// other knob unchanged (PATCH replaces the whole knob set, not a merge patch).
type personaUpdateStubClient struct {
	api.MockClient
	lastID   int
	lastBody api.PersonaBody
}

func (c *personaUpdateStubClient) UpdatePersona(id int, body api.PersonaBody) (*api.Persona, error) {
	c.lastID = id
	c.lastBody = body
	return &api.Persona{ID: id}, nil
}

func personasWithData(t *testing.T) Personas {
	t.Helper()
	p := newPersonas(&api.MockClient{})
	p.width, p.height = 120, 30
	data, _ := (&api.MockClient{}).Personas()
	p, _ = p.update(personasLoadedMsg{data: data})
	return p
}

func TestPersonasEmptyView(t *testing.T) {
	p := newPersonas(&api.MockClient{})
	p.width, p.height = 120, 30
	p, _ = p.update(personasLoadedMsg{data: &api.PersonaPage{Total: 0}})

	out := p.view(120, 30)
	if !strings.Contains(out, "No personas configured") {
		t.Fatalf("empty view missing placeholder:\n%s", out)
	}
}

func TestPersonasErrorView(t *testing.T) {
	p := newPersonas(&api.MockClient{})
	p, _ = p.update(personasLoadedMsg{err: errFake})

	out := p.view(120, 30)
	if !strings.Contains(out, "Error: boom") {
		t.Fatalf("error view missing message:\n%s", out)
	}
}

func TestPersonasListShowsColumnsAndRows(t *testing.T) {
	p := personasWithData(t)
	out := p.view(120, 30)

	for _, want := range []string{
		"Scope", "Repo", "Lang", "Formality", "Depth", "Length",
		"default", "repo", "acme/widgets", "informal", "high",
		"acme/legacy-erp", "brief",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("list view missing %q:\n%s", want, out)
		}
	}
}

// TestPersonasEnterResolvesRepoIncludingRenderedBlock is the high-value view:
// selecting a repo-scoped row must fetch and show the RESOLVED knobs plus the
// exact rendered_block text injected into the prompt.
func TestPersonasEnterResolvesRepoIncludingRenderedBlock(t *testing.T) {
	p := personasWithData(t)
	// index 1 is the acme/widgets repo row (index 0 is the default row).
	p.cursor = 1
	p, cmd := p.update(keyMsg("enter"))
	if !p.detail {
		t.Fatal("enter did not open the resolved-persona detail")
	}
	if cmd == nil {
		t.Fatal("enter produced no resolve command")
	}
	msg, ok := cmd().(personaResolvedMsg)
	if !ok || msg.repoFullName != "acme/widgets" {
		t.Fatalf("expected a resolve for acme/widgets, got %+v", msg)
	}
	p, _ = p.update(msg)

	out := p.view(120, 30)
	for _, want := range []string{
		"Resolved persona — acme/widgets",
		"informal", "high",
		"Constraint: never quote a delivery date",
		"Rendered block", "terse, code-referenced answers",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("resolved view missing %q:\n%s", want, out)
		}
	}
}

// TestPersonasEnterOnDefaultRowResolvesNoRepoOverride: the default row
// resolves what any repo without its own override would get — repo_full_name
// "".
func TestPersonasEnterOnDefaultRowResolvesNoRepoOverride(t *testing.T) {
	p := personasWithData(t)
	p.cursor = 0 // the default row
	p, cmd := p.update(keyMsg("enter"))
	if cmd == nil {
		t.Fatal("enter produced no resolve command")
	}
	msg, ok := cmd().(personaResolvedMsg)
	if !ok || msg.repoFullName != "" {
		t.Fatalf("expected a resolve for the empty (default) repo, got %+v", msg)
	}
	p, _ = p.update(msg)

	out := p.view(120, 30)
	if !strings.Contains(out, "default (no repo override)") {
		t.Fatalf("resolved view missing the default-scope title:\n%s", out)
	}
	if strings.Contains(out, "Constraint:") {
		t.Fatalf("default resolve should carry no repo-specific content policy:\n%s", out)
	}
}

func TestPersonasToggleActiveResendsFullKnobSet(t *testing.T) {
	stub := &personaUpdateStubClient{}
	stub.MockClient = api.MockClient{}
	p := newPersonas(stub)
	p.width, p.height = 120, 30
	data, _ := stub.Personas()
	p, _ = p.update(personasLoadedMsg{data: data})
	p.cursor = 1 // acme/widgets: active=true, style_notes/content_policy set

	p, cmd := p.update(keyMsg("t"))
	if cmd == nil {
		t.Fatal("t produced no toggle command")
	}
	msg, ok := cmd().(personaUpdatedMsg)
	if !ok || msg.id != 2 || msg.err != nil {
		t.Fatalf("expected a successful update of persona 2, got %+v", msg)
	}

	if stub.lastID != 2 {
		t.Fatalf("UpdatePersona called with id %d, want 2", stub.lastID)
	}
	body := stub.lastBody
	if body.Active {
		t.Fatal("active was not flipped to false")
	}
	if body.Scope != "repo" || body.RepoFullName == nil || *body.RepoFullName != "acme/widgets" {
		t.Fatalf("scope/repo not resent unchanged: %+v", body)
	}
	if body.StyleNotes == nil || *body.StyleNotes == "" {
		t.Fatal("style_notes was dropped by the toggle — PATCH would have nulled it out")
	}
	if body.ContentPolicy == nil || *body.ContentPolicy == "" {
		t.Fatal("content_policy was dropped by the toggle — PATCH would have nulled it out")
	}
}

func TestPersonasEscFromDetailReturnsToList(t *testing.T) {
	p := personasWithData(t)
	p, _ = p.update(keyMsg("enter"))
	if !p.detail {
		t.Fatal("enter did not open detail")
	}
	p, _ = p.update(keyMsg("esc"))
	if p.detail {
		t.Fatal("esc did not leave the resolved view")
	}
}

// TestPersonasListFitsShortTerminal guards the list's row windowing (visibleRows
// derived from h), mirroring the other tabs' list views. The resolved-persona
// pane's overflow case is covered by TestNoTabOverflowsTerminal (via the App's
// MaxHeight safety net), since it isn't a scrolling list.
func TestPersonasListFitsShortTerminal(t *testing.T) {
	p := personasWithData(t)
	if lines := strings.Count(p.view(80, 10), "\n") + 1; lines > 10 {
		t.Fatalf("list view is %d lines, want <= 10", lines)
	}
}
