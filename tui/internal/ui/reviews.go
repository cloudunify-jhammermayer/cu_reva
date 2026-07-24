package ui

import (
	"fmt"
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
	detailOffset  int // scroll position within the right-hand detail pane
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
	ti.Placeholder = "filter by repo (substring)..."
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
		return reviewDetailLoadedMsg{id: id, data: data, err: err}
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
		// Holding j/k fires a loadDetail per row, each in its own goroutine;
		// ignore a response for a row we've since scrolled off so the last one
		// to arrive can't overwrite the pane for the wrong review (M24).
		if r.cursor < len(r.items) && m.id != r.items[r.cursor].ID {
			return r, nil
		}
		r.loadingDetail = false
		r.errDetail = m.err
		r.detail = m.data
		r.detailOffset = 0

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
				openInBrowser(url)
			}
		case "j", "down", "k", "up", "g", "G", "home", "end", "ctrl+d", "ctrl+u":
			// List nav moves the cursor and (re)loads that review's detail.
			// pgup/pgdn and J/K are reserved below for scrolling the detail pane.
			half := visibleRows / 2
			if half < 1 {
				half = 1
			}
			delta := 0
			switch m.String() {
			case "j", "down":
				delta = 1
			case "k", "up":
				delta = -1
			case "ctrl+d":
				delta = half
			case "ctrl+u":
				delta = -half
			case "g", "home":
				delta = -len(r.items)
			case "G", "end":
				delta = len(r.items)
			}
			prev := r.cursor
			r.cursor, r.offset = pageCursor(r.cursor, r.offset, len(r.items), visibleRows, delta)
			if r.cursor != prev && r.cursor < len(r.items) {
				r.loadingDetail = true
				r.detail = nil
				r.detailOffset = 0
				return r, r.loadDetail(r.items[r.cursor].ID)
			}
		case "J", "pgdown":
			r.detailOffset = r.scrollDetail(r.detailOffset, true)
		case "K", "pgup":
			r.detailOffset = r.scrollDetail(r.detailOffset, false)
		case "r":
			r.loadingList = true
			r.items = nil
			r.detail = nil
			r.statusMsg = ""
			return r, r.loadList()
		case "e":
			if r.cursor < len(r.items) {
				item := r.items[r.cursor]
				if item.Status != "failed" && item.Status != "stale" && item.Status != "completed" && item.Status != "declined" {
					r.statusMsg = styleStatusFailed.Render("only failed, stale, declined, or completed reviews can be requeued")
					return r, nil
				}
				id := item.ID
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
		titleRaw := fmt.Sprintf("  #%d %s", item.PRNumber, item.PRTitle)
		if item.CarriedFrom != nil {
			titleRaw += fmt.Sprintf(" (carried from #%d)", item.CarriedFrom.PR)
		}
		titleBase := truncate(titleRaw, w-2-len(timeInfo)-2)

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
		statusContent = styleSubtitle.Render(fmt.Sprintf("  %d/%d", r.cursor+1, len(r.items))) +
			cappedNote(len(r.items), r.total)
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

// detailWidth / detailBodyArea mirror the geometry renderDetail uses, so the
// scroll clamp in update() bounds detailOffset against exactly what's rendered.
func (r Reviews) detailWidth() int {
	leftW := r.width * 2 / 5
	return r.width - leftW - 1
}

func (r Reviews) detailBodyArea() int {
	innerH := r.height - 2 - 1 // border (2) + reserved scroll-indicator line
	if innerH < 1 {
		innerH = 1
	}
	return innerH
}

// scrollDetail moves the detail pane by half a page and returns the clamped
// offset. A no-op when there's no detail loaded.
func (r Reviews) scrollDetail(off int, down bool) int {
	if r.detail == nil {
		return off
	}
	total := len(strings.Split(r.detailBody(r.detailWidth()), "\n"))
	area := r.detailBodyArea()
	step := area / 2
	if step < 1 {
		step = 1
	}
	if down {
		off += step
	} else {
		off -= step
	}
	return clampOffset(off, total, area)
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

	lines := strings.Split(r.detailBody(w), "\n")
	innerH := h - 2 // inside the rounded border
	if innerH < 1 {
		innerH = 1
	}
	bodyArea := innerH
	overflow := len(lines) > innerH
	if overflow {
		bodyArea = innerH - 1 // reserve a line for the scroll indicator
		if bodyArea < 1 {
			bodyArea = 1
		}
	}
	off := clampOffset(r.detailOffset, len(lines), bodyArea)
	end := off + bodyArea
	if end > len(lines) {
		end = len(lines)
	}
	inner := strings.Join(lines[off:end], "\n")
	if overflow {
		inner = lipgloss.JoinVertical(lipgloss.Left, inner, scrollHint(off, bodyArea, len(lines)))
	}
	return styleBorder.Width(w - 2).Height(innerH).Render(inner)
}

// detailBody renders the review detail as plain (border-less) text, wrapped to
// width w. renderDetail windows the returned lines into the scroll pane.
func (r Reviews) detailBody(w int) string {
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

	// Requirements check (issue-conformance verdicts, advisory)
	if len(d.IntentCheck) > 0 {
		b.WriteString("\n")
		b.WriteString(styleTitle.Render("Requirements check") + "\n")
		for _, ic := range d.IntentCheck {
			line := fmt.Sprintf("#%d %s", ic.IssueNumber, strings.ReplaceAll(ic.Verdict, "_", " "))
			if ic.Note != "" {
				line += " — " + ic.Note
			}
			b.WriteString(fmt.Sprintf("  %s %s\n", intentSymbol(ic.Verdict), truncate(line, w-6)))
		}
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

	return strings.TrimRight(b.String(), "\n")
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
