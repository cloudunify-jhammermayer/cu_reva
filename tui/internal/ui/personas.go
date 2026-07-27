package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

// Personas lists the configured tone rows (one 'default' + one per repo
// override) and shows the RESOLVED persona for a repo — the merged knobs
// plus rendered_block, the exact text injected into the support-answer
// prompt. That resolved view answers "why did REVA write it in that tone",
// which a raw row can't show on its own (a repo row inherits NULL knobs from
// the default row, per-field).
//
// Editing here is a single active/inactive toggle (`t`), not a full field
// editor: a persona carries nine free-form/enum knobs and PATCH replaces the
// whole set, so a faithful multi-field editor is a form framework this tab
// doesn't otherwise need or have a pattern for. The toggle is still a
// complete, real operation — resolve_persona treats an inactive row as
// absent, so deactivating genuinely takes effect (falls back to the default,
// or to REVA's hardcoded persona if the default itself is deactivated).
type Personas struct {
	client    api.ClientIface
	items     []api.Persona
	total     int
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string

	// Resolved-persona drill-down for the selected row's repo ("" for the
	// default row, which resolves to what any repo without an override gets).
	detail      bool
	resolved    *api.ResolvedPersona
	resolvedErr error
	resolving   bool
}

func newPersonas(client api.ClientIface) Personas {
	return Personas{client: client, loading: true}
}

func (p Personas) load() tea.Cmd {
	client := p.client
	return func() tea.Msg {
		data, err := client.Personas()
		return personasLoadedMsg{data: data, err: err}
	}
}

// repoFor returns the repo_full_name a persona row resolves for: its own for
// a 'repo' row, "" (no override) for the 'default' row.
func repoFor(persona api.Persona) string {
	if persona.RepoFullName != nil {
		return *persona.RepoFullName
	}
	return ""
}

func (p Personas) resolveCmd(repoFullName string) tea.Cmd {
	client := p.client
	return func() tea.Msg {
		data, err := client.ResolvedPersona(repoFullName)
		return personaResolvedMsg{repoFullName: repoFullName, data: data, err: err}
	}
}

// toggleActiveCmd flips `active` on the persona, resending every other
// current knob unchanged. PATCH replaces the whole knob set (it's not a merge
// patch), so omitting them would silently null out the rest of the row.
func (p Personas) toggleActiveCmd(persona api.Persona) tea.Cmd {
	client := p.client
	body := api.PersonaBody{
		Scope: persona.Scope, RepoFullName: persona.RepoFullName,
		Language: persona.Language, Formality: persona.Formality,
		TechnicalDepth: persona.TechnicalDepth, Length: persona.Length,
		Salutation: persona.Salutation, SignOff: persona.SignOff,
		StyleNotes: persona.StyleNotes, ContentPolicy: persona.ContentPolicy,
		Active: !persona.Active,
	}
	return func() tea.Msg {
		_, err := client.UpdatePersona(persona.ID, body)
		return personaUpdatedMsg{id: persona.ID, err: err}
	}
}

func (p Personas) update(msg tea.Msg) (Personas, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		if !p.detail {
			return p, p.load()
		}

	case personasLoadedMsg:
		p.loading = false
		p.err = m.err
		if m.data != nil {
			p.items = m.data.Items
			p.total = m.data.Total
		}
		if p.cursor >= len(p.items) {
			p.cursor, p.offset = 0, 0
		}

	case personaResolvedMsg:
		if !p.detail {
			return p, nil // navigated away before the fetch returned
		}
		p.resolving = false
		p.resolvedErr = m.err
		if m.err == nil {
			p.resolved = m.data
		}

	case personaUpdatedMsg:
		if m.err != nil {
			p.statusMsg = fmt.Sprintf("update failed: %s", m.err)
		} else {
			p.statusMsg = "updated"
			return p, p.load()
		}

	case tea.KeyMsg:
		if p.detail {
			switch m.String() {
			case "esc", "left", "h":
				p.detail = false
			case "r":
				if p.cursor < len(p.items) {
					p.resolving = true
					return p, p.resolveCmd(repoFor(p.items[p.cursor]))
				}
			}
			return p, nil
		}

		visibleRows := p.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), p.cursor, p.offset, len(p.items), visibleRows); ok {
			p.cursor, p.offset = c, o
			return p, nil
		}
		p.statusMsg = ""
		switch m.String() {
		case "enter":
			if p.cursor < len(p.items) {
				p.detail = true
				p.resolved, p.resolvedErr, p.resolving = nil, nil, true
				return p, p.resolveCmd(repoFor(p.items[p.cursor]))
			}
		case "t":
			if p.cursor < len(p.items) {
				return p, p.toggleActiveCmd(p.items[p.cursor])
			}
		case "r":
			p.loading = true
			return p, p.load()
		}
	}
	return p, nil
}

func (p Personas) view(w, h int) string {
	if p.detail {
		return p.detailView(w, h)
	}

	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Personas (%d)", p.total))
	if p.loading && len(p.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if p.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", styleStatusFailed.Render("  Error: "+p.err.Error()))
	}
	if len(p.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No personas configured — REVA falls back to its hardcoded default")))
	}

	colScope, colRepo, colLang, colForm, colDepth, colLength := 8, 22, 6, 10, 8, 10
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colScope, "Scope", colRepo, "Repo", colLang, "Lang",
			colForm, "Formality", colDepth, "Depth", colLength, "Length"))

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	off := ensureVisible(p.offset, p.cursor, visibleRows, len(p.items))
	end := off + visibleRows
	if end > len(p.items) {
		end = len(p.items)
	}
	rows := []string{hdr}
	for i := off; i < end; i++ {
		it := p.items[i]
		repo := "—"
		if it.RepoFullName != nil {
			repo = *it.RepoFullName
		}
		active := "+"
		if !it.Active {
			active = "x"
		}
		line := fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			active,
			colScope, it.Scope,
			colRepo, truncate(repo, colRepo),
			colLang, strOr(it.Language, "—"),
			colForm, strOr(it.Formality, "—"),
			colDepth, strOr(it.TechnicalDepth, "—"),
			colLength, strOr(it.Length, "—"))
		if i == p.cursor {
			line = styleSelected.Width(w - 2).Render(line)
		} else if !it.Active {
			line = styleStatusOther.Render(line)
		}
		rows = append(rows, line)
	}
	table := strings.Join(rows, "\n")

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   [enter] resolved view · [t] toggle active", p.cursor+1, len(p.items)))
	if p.statusMsg != "" {
		pos = "  " + p.statusMsg
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}

func (p Personas) detailView(w, h int) string {
	title := "default (no repo override)"
	if p.cursor < len(p.items) {
		if repo := repoFor(p.items[p.cursor]); repo != "" {
			title = repo
		}
	}
	header := styleTitle.Padding(0, 1).Render("Resolved persona — " + title)

	var b strings.Builder
	switch {
	case p.resolving:
		b.WriteString(styleSubtitle.Render("  Resolving...") + "\n")
	case p.resolvedErr != nil:
		b.WriteString(styleStatusFailed.Render("  "+p.resolvedErr.Error()) + "\n")
	case p.resolved != nil:
		r := p.resolved
		b.WriteString(fmt.Sprintf("  Language         %s\n", strOr(r.Language, "—")))
		b.WriteString(fmt.Sprintf("  Formality        %s\n", strOr(r.Formality, "—")))
		b.WriteString(fmt.Sprintf("  Technical depth  %s\n", strOr(r.TechnicalDepth, "—")))
		b.WriteString(fmt.Sprintf("  Length           %s\n", strOr(r.Length, "—")))
		if r.Salutation != nil && *r.Salutation != "" {
			b.WriteString(fmt.Sprintf("  Salutation       %s\n", *r.Salutation))
		}
		if r.SignOff != nil && *r.SignOff != "" {
			b.WriteString(fmt.Sprintf("  Sign-off         %s\n", *r.SignOff))
		}
		if r.StyleNotes != nil && *r.StyleNotes != "" {
			b.WriteString(fmt.Sprintf("  Style notes      %s\n", truncate(*r.StyleNotes, w-21)))
		}
		if r.ContentPolicy != nil && *r.ContentPolicy != "" {
			b.WriteString(fmt.Sprintf("  Content policy   %s\n", truncate(*r.ContentPolicy, w-21)))
		}
		b.WriteString("\n")
		b.WriteString(styleTitle.Render("  Rendered block (exact prompt text)") + "\n")
		for _, line := range strings.Split(r.RenderedBlock, "\n") {
			// Not truncated (mirrors the Feedback tab's learned-memory block):
			// this is the exact text injected into the prompt, so cutting it
			// off would defeat the point of showing it.
			b.WriteString("    " + line + "\n")
		}
	}

	footer := styleSubtitle.Render("  [esc] back   [r] re-resolve")
	return lipgloss.JoinVertical(lipgloss.Left, header, "", strings.TrimRight(b.String(), "\n"), "", footer)
}

// strOr returns *s, or fallback when s is nil or empty — the common case for
// rendering an optional/inherited persona knob.
func strOr(s *string, fallback string) string {
	if s == nil || *s == "" {
		return fallback
	}
	return *s
}
