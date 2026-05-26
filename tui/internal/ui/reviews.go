package ui

import (
	"fmt"
	"os/exec"
	"strings"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

type Reviews struct {
	client        api.ClientIface
	items         []api.ReviewSummary
	total         int
	detail        *api.ReviewDetail
	loadingList   bool
	loadingDetail bool
	errList       error
	errDetail     error
	cursor        int
	offset        int
	width         int
	height        int
	focusLeft     bool
	statusMsg     string
	// filter state
	filterMode   bool
	repoInput    textinput.Model
	statusFilter string // "", "completed", "failed", "stale"
	authorFilter string
}

func newReviews(client api.ClientIface) Reviews {
	ti := textinput.New()
	ti.Placeholder = "repo or author..."
	ti.CharLimit = 100
	return Reviews{client: client, loadingList: true, focusLeft: true, repoInput: ti}
}

func (r Reviews) loadList() tea.Cmd {
	repo := r.repoInput.Value()
	status := r.statusFilter
	author := r.authorFilter
	return func() tea.Msg {
		data, err := r.client.Reviews(100, repo, status, author)
		return reviewsLoadedMsg{data: data, err: err}
	}
}

func (r Reviews) loadDetail(id int) tea.Cmd {
	return func() tea.Msg {
		data, err := r.client.ReviewDetail(id)
		return reviewDetailLoadedMsg{data: data, err: err}
	}
}

func (r Reviews) update(msg tea.Msg) (Reviews, tea.Cmd) {
	switch m := msg.(type) {
	case reviewsLoadedMsg:
		r.loadingList = false
		r.errList = m.err
		if m.data != nil {
			r.items = m.data.Items
			r.total = m.data.Total
		}
		r.cursor = 0
		r.offset = 0
		if len(r.items) > 0 {
			r.loadingDetail = true
			return r, r.loadDetail(r.items[0].ID)
		}

	case reviewDetailLoadedMsg:
		r.loadingDetail = false
		r.errDetail = m.err
		r.detail = m.data

	case requeuedMsg:
		if m.err != nil {
			r.statusMsg = styleStatusFailed.Render("requeue failed: " + m.err.Error())
		} else {
			r.statusMsg = styleStatusCompleted.Render("queued - review will run shortly")
		}

	case tea.KeyMsg:
		// Filter mode: route most keys to the textinput
		if r.filterMode {
			switch m.String() {
			case "esc":
				r.filterMode = false
				r.repoInput.Blur()
				return r, nil
			case "enter":
				r.filterMode = false
				r.repoInput.Blur()
				r.loadingList = true
				r.items = nil
				r.statusMsg = ""
				return r, r.loadList()
			case "ctrl+c":
				return r, tea.Quit
			default:
				var cmd tea.Cmd
				r.repoInput, cmd = r.repoInput.Update(msg)
				return r, cmd
			}
		}

		visibleRows := r.listHeight() // rows, not display lines
		switch m.String() {
		case "/":
			r.filterMode = true
			r.repoInput.Focus()
			return r, textinput.Blink
		case "s":
			switch r.statusFilter {
			case "":
				r.statusFilter = "completed"
			case "completed":
				r.statusFilter = "failed"
			case "failed":
				r.statusFilter = "stale"
			default:
				r.statusFilter = ""
			}
			r.loadingList = true
			r.items = nil
			r.statusMsg = ""
			return r, r.loadList()
		case "c":
			// Clear all filters
			r.repoInput.SetValue("")
			r.statusFilter = ""
			r.authorFilter = ""
			r.loadingList = true
			r.items = nil
			r.statusMsg = ""
			return r, r.loadList()
		case "o":
			if r.detail != nil {
				url := fmt.Sprintf("https://github.com/%s/pull/%d",
					r.detail.RepoFullName, r.detail.PRNumber)
				_ = exec.Command("xdg-open", url).Start()
			}
		case "j", "down":
			if r.cursor < len(r.items)-1 {
				r.cursor++
				if r.cursor >= r.offset+visibleRows {
					r.offset++
				}
				if r.cursor < len(r.items) {
					r.loadingDetail = true
					r.detail = nil
					return r, r.loadDetail(r.items[r.cursor].ID)
				}
			}
		case "k", "up":
			if r.cursor > 0 {
				r.cursor--
				if r.cursor < r.offset {
					r.offset--
				}
				if r.cursor < len(r.items) {
					r.loadingDetail = true
					r.detail = nil
					return r, r.loadDetail(r.items[r.cursor].ID)
				}
			}
		case "r":
			r.loadingList = true
			r.items = nil
			r.detail = nil
			r.statusMsg = ""
			return r, r.loadList()
		case "e":
			if r.cursor < len(r.items) {
				id := r.items[r.cursor].ID
				client := r.client
				return r, func() tea.Msg {
					err := client.Requeue(id)
					return requeuedMsg{id: id, err: err}
				}
			}
		}
	}
	return r, nil
}

func (r Reviews) listHeight() int {
	// Each row renders as 2 display lines. Outer chrome: header(1) + filterLine(1)
	// + blank(1) + border-top(1) + status(1) + border-bottom(1) = 6.
	h := (r.height - 6) / 2
	if h < 1 {
		h = 1
	}
	return h
}

func (r Reviews) view(w, h int) string {
	leftW := w * 2 / 5
	rightW := w - leftW - 1

	left := r.renderList(leftW, h)
	right := r.renderDetail(rightW, h)

	return lipgloss.JoinHorizontal(lipgloss.Top, left, right)
}

func (r Reviews) renderList(w, h int) string {
	// Build filter line (always 1 line to keep height stable)
	var filterLine string
	if r.filterMode {
		label := styleSubtitle.Render(" / ")
		filterLine = label + r.repoInput.View()
		if r.statusFilter != "" {
			filterLine += styleSubtitle.Render("  [s=" + r.statusFilter + "]")
		}
	} else {
		var parts []string
		if r.repoInput.Value() != "" {
			parts = append(parts, "repo="+r.repoInput.Value())
		}
		if r.statusFilter != "" {
			parts = append(parts, "s="+r.statusFilter)
		}
		if len(parts) > 0 {
			filterLine = styleSubtitle.Render("  >"+strings.Join(parts, " |")) +
				styleSubtitle.Render("  c=clear")
		} else {
			filterLine = styleSubtitle.Render("  / filter  s=status")
		}
	}

	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Reviews  (%d)", r.total))

	if r.loadingList && len(r.items) == 0 {
		content := lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
			styleSubtitle.Render("Loading..."))
		return lipgloss.JoinVertical(lipgloss.Left, header, filterLine, content)
	}
	if r.errList != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, filterLine, "",
			styleStatusFailed.Render("  "+r.errList.Error()))
	}
	if len(r.items) == 0 {
		content := lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
			styleSubtitle.Render("No reviews"))
		return lipgloss.JoinVertical(lipgloss.Left, header, filterLine, content)
	}

	visibleRows := r.listHeight()
	end := r.offset + visibleRows
	if end > len(r.items) {
		end = len(r.items)
	}

	var rows []string
	for i := r.offset; i < end; i++ {
		item := r.items[i]
		repo := truncate(item.RepoFullName, w-8)

		timeInfo := relativeTime(item.CreatedAt)
		if item.DurationMS != nil {
			timeInfo += "  " + fmtDurationMS(*item.DurationMS)
		}
		titleBase := truncate(fmt.Sprintf("  #%d %s", item.PRNumber, item.PRTitle), w-2-len(timeInfo)-2)

		var combined string
		if i == r.cursor {
			line1 := fmt.Sprintf(" %s %s", statusChar(item.Status), repo)
			line2 := titleBase + "  " + timeInfo
			combined = styleSelected.Width(w - 2).Render(line1 + "\n" + line2)
		} else {
			line1 := fmt.Sprintf(" %s %s", statusSymbol(item.Status), repo)
			line2 := styleSubtitle.Render(titleBase + "  " + timeInfo)
			combined = line1 + "\n" + line2
		}
		rows = append(rows, combined)
	}

	var statusContent string
	if r.statusMsg != "" {
		statusContent = r.statusMsg
	} else {
		statusContent = styleSubtitle.Render(fmt.Sprintf("  %d/%d", r.cursor+1, len(r.items)))
	}

	innerH := h - 6 // header + filterLine + blank + border-top + pos + border-bottom
	if innerH < 1 {
		innerH = 1
	}
	list := styleBorderFocused.Width(w - 2).Height(innerH).Render(
		lipgloss.JoinVertical(lipgloss.Left,
			strings.Join(rows, "\n"),
			"",
			statusContent,
		),
	)
	return lipgloss.JoinVertical(lipgloss.Left, header, filterLine, "", list)
}

func (r Reviews) renderDetail(w, h int) string {
	if r.loadingDetail && r.detail == nil {
		return styleBorder.Width(w - 2).Height(h - 2).Render(
			lipgloss.Place(w-4, h-4, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("Loading...")))
	}
	if r.errDetail != nil {
		return styleBorder.Width(w - 2).Height(h - 2).Render(
			styleStatusFailed.Render("Error: " + r.errDetail.Error()))
	}
	if r.detail == nil {
		return styleBorder.Width(w - 2).Height(h - 2).Render("")
	}

	d := r.detail
	var b strings.Builder

	// Header
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s", d.PRNumber, truncate(d.PRTitle, w-12))) + "\n")
	b.WriteString(styleSubtitle.Render(fmt.Sprintf("  %s", d.RepoFullName)) + "\n\n")

	// Meta row
	author := "—"
	if d.AuthorLogin != nil {
		author = "@" + *d.AuthorLogin
	}
	risk := "—"
	if d.RiskLevel != nil {
		risk = riskStyle(*d.RiskLevel).Render(*d.RiskLevel)
	}
	model := "—"
	if d.Model != nil {
		model = truncate(*d.Model, 20)
	}
	b.WriteString(fmt.Sprintf("  Status  %s %s\n", statusSymbol(d.Status), d.Status))
	b.WriteString(fmt.Sprintf("  Queued  %s\n", styleSubtitle.Render(
		d.CreatedAt.Local().Format("2006-01-02 15:04")+" ("+relativeTime(d.CreatedAt)+")")))
	b.WriteString(fmt.Sprintf("  Author  %s\n", styleSubtitle.Render(author)))
	b.WriteString(fmt.Sprintf("  Risk    %s\n", risk))
	b.WriteString(fmt.Sprintf("  Model   %s\n", styleSubtitle.Render(model)))
	if d.DurationMS != nil {
		b.WriteString(fmt.Sprintf("  Took    %s\n", styleSubtitle.Render(fmtDurationMS(*d.DurationMS))))
	}
	if d.EstimatedCostUSD != nil {
		b.WriteString(fmt.Sprintf("  Cost    %s\n", styleSubtitle.Render(fmt.Sprintf("$%.4f", *d.EstimatedCostUSD))))
	}

	// PR URL hint
	prURL := fmt.Sprintf("https://github.com/%s/pull/%d", d.RepoFullName, d.PRNumber)
	b.WriteString(fmt.Sprintf("  URL     %s\n", styleSubtitle.Render(truncate(prURL, w-12))))

	// Summary
	if d.Summary != nil && *d.Summary != "" {
		b.WriteString("\n")
		b.WriteString(styleTitle.Render("Summary") + "\n")
		b.WriteString(wordWrap(*d.Summary, w-4) + "\n")
	}

	// Error info
	if d.ErrorMessage != nil {
		b.WriteString("\n")
		b.WriteString(styleStatusFailed.Render("Error") + "\n")
		if d.ErrorClass != nil {
			b.WriteString(styleSubtitle.Render("  "+*d.ErrorClass) + "\n")
		}
		b.WriteString("  " + wordWrap(*d.ErrorMessage, w-4) + "\n")
	}

	// Findings
	if len(d.Findings) > 0 {
		b.WriteString("\n")
		b.WriteString(styleTitle.Render(fmt.Sprintf("Findings (%d)", len(d.Findings))) + "\n")
		for _, f := range d.Findings {
			dot := severityDot(f.Severity)
			loc := ""
			if f.FilePath != nil {
				loc = *f.FilePath
				if f.LineStart != nil {
					loc = fmt.Sprintf("%s:%d", loc, *f.LineStart)
				}
				loc = "  " + styleSubtitle.Render(truncate(loc, w-6))
			}
			b.WriteString(fmt.Sprintf("  %s %s\n", dot, truncate(f.Title, w-6)))
			if loc != "" {
				b.WriteString(loc + "\n")
			}
		}
	}

	return styleBorder.Width(w - 2).Height(h - 2).Render(b.String())
}

func wordWrap(s string, width int) string {
	if width <= 0 {
		return s
	}
	words := strings.Fields(s)
	var lines []string
	var line strings.Builder
	for _, w := range words {
		if line.Len() > 0 && line.Len()+1+len(w) > width {
			lines = append(lines, line.String())
			line.Reset()
		}
		if line.Len() > 0 {
			line.WriteByte(' ')
		}
		line.WriteString(w)
	}
	if line.Len() > 0 {
		lines = append(lines, line.String())
	}
	return strings.Join(lines, "\n")
}
