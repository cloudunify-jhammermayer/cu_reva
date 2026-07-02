package ui

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func TestApplyFilterKey(t *testing.T) {
	// Type "ab" then backspace → "a", still active, each step marks changed.
	active, v, changed := applyFilterKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("ab")}, "")
	if !active || v != "ab" || !changed {
		t.Fatalf("runes: got active=%v v=%q changed=%v", active, v, changed)
	}
	active, v, changed = applyFilterKey(tea.KeyMsg{Type: tea.KeyBackspace}, "ab")
	if !active || v != "a" || !changed {
		t.Fatalf("backspace: got active=%v v=%q changed=%v", active, v, changed)
	}

	// Backspace on empty is a no-op (stays active, unchanged).
	active, v, changed = applyFilterKey(tea.KeyMsg{Type: tea.KeyBackspace}, "")
	if !active || v != "" || changed {
		t.Fatalf("backspace-empty: got active=%v v=%q changed=%v", active, v, changed)
	}

	// Enter keeps the value and leaves edit mode without a position reset.
	active, v, changed = applyFilterKey(tea.KeyMsg{Type: tea.KeyEnter}, "keep")
	if active || v != "keep" || changed {
		t.Fatalf("enter: got active=%v v=%q changed=%v", active, v, changed)
	}

	// Esc cancels and clears (changed → caller resets cursor).
	active, v, changed = applyFilterKey(tea.KeyMsg{Type: tea.KeyEsc}, "x")
	if active || v != "" || !changed {
		t.Fatalf("esc: got active=%v v=%q changed=%v", active, v, changed)
	}
}
