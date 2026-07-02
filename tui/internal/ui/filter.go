package ui

import tea "github.com/charmbracelet/bubbletea"

// applyFilterKey is the shared `/`-filter text state machine, extracted from the
// three tabs (Findings, Repos, Tickets) that had it copy-pasted nearly verbatim
// (M27). Given the current filter value and one key, it returns the new
// (active, value) plus whether the value changed — the caller resets its
// cursor/offset on a change. Only call while the filter is active.
//
//   - Esc:       cancel and clear the filter (changed → caller resets position).
//   - Enter:     keep the value, leave edit mode (no position reset needed).
//   - Backspace: drop the last rune (rune-safe; no-op on empty).
//   - runes:     append typed text.
func applyFilterKey(m tea.KeyMsg, value string) (active bool, newValue string, changed bool) {
	switch m.Type {
	case tea.KeyEsc:
		return false, "", true
	case tea.KeyEnter:
		return false, value, false
	case tea.KeyBackspace:
		if value == "" {
			return true, value, false
		}
		return true, dropLastRune(value), true
	case tea.KeyRunes, tea.KeySpace:
		return true, value + string(m.Runes), true
	}
	return true, value, false
}
