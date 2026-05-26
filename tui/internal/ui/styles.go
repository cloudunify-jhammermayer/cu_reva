package ui

import "github.com/charmbracelet/lipgloss"

var (
	colorAccent   = lipgloss.Color("#4A9EFF")
	colorMuted    = lipgloss.Color("#6C7086")
	colorGreen    = lipgloss.Color("#A6E3A1")
	colorYellow   = lipgloss.Color("#F9E2AF")
	colorRed      = lipgloss.Color("#F38BA8")
	colorOrange   = lipgloss.Color("#FAB387")
	colorBlue     = lipgloss.Color("#89B4FA")
	colorDim      = lipgloss.Color("#45475A")
	colorBg       = lipgloss.Color("#1E1E2E")
	colorBorder   = lipgloss.Color("#313244")
	colorSelected    = lipgloss.Color("#2A5298")
	colorSelectedFg  = lipgloss.Color("#CDD6F4")

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

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	if n <= 3 {
		return s[:n]
	}
	return s[:n-3] + "..."
}
