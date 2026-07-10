package ui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

var (
	colorAccent     = lipgloss.Color("#4A9EFF")
	colorMuted      = lipgloss.Color("#6C7086")
	colorGreen      = lipgloss.Color("#A6E3A1")
	colorYellow     = lipgloss.Color("#F9E2AF")
	colorRed        = lipgloss.Color("#F38BA8")
	colorOrange     = lipgloss.Color("#FAB387")
	colorBlue       = lipgloss.Color("#89B4FA")
	colorDim        = lipgloss.Color("#45475A")
	colorBorder     = lipgloss.Color("#313244")
	colorSelected   = lipgloss.Color("#2A5298")
	colorSelectedFg = lipgloss.Color("#CDD6F4")

	styleTitle = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorAccent)

	styleSubtitle = lipgloss.NewStyle().
			Foreground(colorMuted)

	styleBorder = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorBorder)

	styleBorderFocused = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(colorAccent)

	styleSelected = lipgloss.NewStyle().
			Background(colorSelected).
			Foreground(colorSelectedFg).
			Bold(true)

	styleStatusBar = lipgloss.NewStyle().
			Foreground(colorMuted).
			Padding(0, 1)

	styleSeverityCritical = lipgloss.NewStyle().Foreground(colorRed)
	styleSeverityMajor    = lipgloss.NewStyle().Foreground(colorOrange)
	styleSeverityMinor    = lipgloss.NewStyle().Foreground(colorBlue)
	styleSeverityInfo     = lipgloss.NewStyle().Foreground(colorDim)

	styleStatusCompleted = lipgloss.NewStyle().Foreground(colorGreen)
	styleStatusFailed    = lipgloss.NewStyle().Foreground(colorRed)
	styleStatusStale     = lipgloss.NewStyle().Foreground(colorYellow)
	styleStatusOther     = lipgloss.NewStyle().Foreground(colorMuted)

	styleRiskLow      = lipgloss.NewStyle().Foreground(colorGreen)
	styleRiskMedium   = lipgloss.NewStyle().Foreground(colorYellow)
	styleRiskHigh     = lipgloss.NewStyle().Foreground(colorOrange)
	styleRiskCritical = lipgloss.NewStyle().Foreground(colorRed)
)

func severityDot(severity string) string {
	switch severity {
	case "critical":
		return styleSeverityCritical.Render("●")
	case "major":
		return styleSeverityMajor.Render("●")
	case "minor":
		return styleSeverityMinor.Render("●")
	default:
		return styleSeverityInfo.Render("●")
	}
}

func statusSymbol(status string) string {
	switch status {
	case "completed":
		return styleStatusCompleted.Render("+")
	case "failed":
		return styleStatusFailed.Render("x")
	case "stale":
		return styleStatusStale.Render("!")
	default:
		return styleStatusOther.Render("-")
	}
}

func intentSymbol(verdict string) string {
	switch verdict {
	case "matches":
		return styleStatusCompleted.Render("+")
	case "partial":
		return styleStatusStale.Render("~")
	case "does_not_match":
		return styleStatusFailed.Render("x")
	default: // unclear
		return styleStatusOther.Render("?")
	}
}

func riskStyle(risk string) lipgloss.Style {
	switch risk {
	case "low":
		return styleRiskLow
	case "medium":
		return styleRiskMedium
	case "high":
		return styleRiskHigh
	case "critical":
		return styleRiskCritical
	default:
		return styleStatusOther
	}
}

// statusChar returns the status symbol without ANSI wrapping, for use inside
// styleSelected.Render() where embedded ANSI resets would break the background.
func statusChar(status string) string {
	switch status {
	case "completed":
		return "+"
	case "failed":
		return "x"
	case "stale":
		return "!"
	default:
		return "-"
	}
}

func fmtDurationMS(ms int) string {
	switch {
	case ms < 1000:
		return fmt.Sprintf("%dms", ms)
	case ms < 60000:
		return fmt.Sprintf("%.1fs", float64(ms)/1000)
	default:
		m := ms / 60000
		s := (ms % 60000) / 1000
		return fmt.Sprintf("%dm%ds", m, s)
	}
}

// moveCursor advances or retreats the cursor and keeps offset in sync so the
// selected row stays within the visible window. Returns updated (cursor, offset).
func moveCursor(cursor, offset, total, visibleRows int, down bool) (int, int) {
	if down {
		if cursor < total-1 {
			cursor++
			if cursor >= offset+visibleRows {
				offset++
			}
		}
	} else {
		if cursor > 0 {
			cursor--
			if cursor < offset {
				offset--
			}
		}
	}
	return cursor, offset
}

// clampOffset bounds a scroll offset to [0, max(0, total-visible)] so a
// free-flowing panel can't scroll past its content. It's the companion to
// moveCursor: that windows a *selectable* list around a cursor, this windows
// rendered text lines that have no cursor (detail / findings / feedback panes).
func clampOffset(offset, total, visible int) int {
	if visible < 1 {
		visible = 1
	}
	maxOff := total - visible
	if maxOff < 0 {
		maxOff = 0
	}
	if offset < 0 {
		return 0
	}
	if offset > maxOff {
		return maxOff
	}
	return offset
}

// scrollHint renders a "↑↓ 3–20 of 57" position indicator for a scrollable
// panel, or "" when all `total` lines already fit in `visible` rows. The arrows
// show which directions still have hidden content.
func scrollHint(offset, visible, total int) string {
	if total <= visible {
		return ""
	}
	end := offset + visible
	if end > total {
		end = total
	}
	up, down := " ", " "
	if offset > 0 {
		up = "↑"
	}
	if end < total {
		down = "↓"
	}
	return styleSubtitle.Render(fmt.Sprintf("  %s%s %d–%d of %d", up, down, offset+1, end, total))
}

// padCell right-pads s to `width` *visible* columns. Unlike fmt's %-*s, it
// measures with lipgloss.Width, so embedded ANSI color codes (and wide runes)
// don't count toward the width — the cause of columns shifting when a styled
// cell sits in a fixed-width table row (only the unselected, colored rows;
// the selected row pads plain text and stayed aligned).
func padCell(s string, width int) string {
	if gap := width - lipgloss.Width(s); gap > 0 {
		return s + strings.Repeat(" ", gap)
	}
	return s
}

// pageCursor moves the cursor by delta rows (negative = up), clamps it to the
// list, and recomputes offset so the cursor stays within the visible window.
// delta may exceed the list size (g/G jumps). Companion to moveCursor's
// single-step move.
func pageCursor(cursor, offset, total, visibleRows, delta int) (int, int) {
	if total <= 0 {
		return 0, 0
	}
	if visibleRows < 1 {
		visibleRows = 1
	}
	cursor += delta
	if cursor < 0 {
		cursor = 0
	} else if cursor > total-1 {
		cursor = total - 1
	}
	if cursor < offset {
		offset = cursor
	} else if cursor >= offset+visibleRows {
		offset = cursor - visibleRows + 1
	}
	maxOff := total - visibleRows
	if maxOff < 0 {
		maxOff = 0
	}
	if offset > maxOff {
		offset = maxOff
	}
	if offset < 0 {
		offset = 0
	}
	return cursor, offset
}

// listNav applies the standard list-navigation keys to a (cursor, offset) over
// `total` rows showing `visibleRows` at once: j/k + arrows (single step),
// ctrl+d/ctrl+u (half page), pgdown/pgup (full page), g/home and G/end
// (top/bottom). Returns the new cursor/offset and whether the key was handled,
// so a tab can `if c, o, ok := listNav(...); ok { ...; return }`.
func listNav(key string, cursor, offset, total, visibleRows int) (int, int, bool) {
	half := visibleRows / 2
	if half < 1 {
		half = 1
	}
	var delta int
	switch key {
	case "j", "down":
		delta = 1
	case "k", "up":
		delta = -1
	case "ctrl+d":
		delta = half
	case "ctrl+u":
		delta = -half
	case "pgdown":
		delta = visibleRows
	case "pgup":
		delta = -visibleRows
	case "g", "home":
		delta = -total
	case "G", "end":
		delta = total
	default:
		return cursor, offset, false
	}
	c, o := pageCursor(cursor, offset, total, visibleRows, delta)
	return c, o, true
}

// ensureVisible nudges a scroll offset so display line `line` stays within a
// `budget`-tall window over `total` lines (top-anchored). Used by grouped lists
// where group-header lines make the visible count differ from the row count.
func ensureVisible(offset, line, budget, total int) int {
	if budget < 1 {
		budget = 1
	}
	if line < offset {
		offset = line
	}
	if line >= offset+budget {
		offset = line - budget + 1
	}
	maxOff := total - budget
	if maxOff < 0 {
		maxOff = 0
	}
	if offset > maxOff {
		offset = maxOff
	}
	if offset < 0 {
		offset = 0
	}
	return offset
}

// cappedNote returns " · showing N of M" when a list was truncated to fewer
// rows than exist server-side (the fetch hit its limit), or "" when complete.
func cappedNote(shown, total int) string {
	if total > shown {
		return styleSubtitle.Render(fmt.Sprintf("   ·  showing %d of %d", shown, total))
	}
	return ""
}

// truncate shortens s to at most n characters, counting by runes so multibyte
// UTF-8 (e.g. accented or CJK text) isn't sliced mid-codepoint (CORR-16).
// dropLastRune removes the last rune (not byte) from s, so backspacing a
// multibyte character (e.g. an umlaut) in a filter/form input doesn't leave a
// dangling continuation byte behind (CORR-16, matching truncate's rune slicing).
func dropLastRune(s string) string {
	r := []rune(s)
	if len(r) == 0 {
		return s
	}
	return string(r[:len(r)-1])
}

func truncate(s string, n int) string {
	if n <= 0 {
		// A caller passed a column width that underflowed (e.g. a fixed-column
		// table on a very narrow terminal). Returning "" beats panicking on r[:n].
		return ""
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	if n <= 3 {
		return string(r[:n])
	}
	return string(r[:n-3]) + "..."
}

// shortSHA returns the first 8 chars of a git SHA, guarding against a short or
// empty server-supplied value (CORR-15: a bare s[:8] panics).
func shortSHA(s string) string {
	if len(s) > 8 {
		return s[:8]
	}
	return s
}
