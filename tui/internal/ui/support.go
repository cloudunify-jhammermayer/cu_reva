package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

// Support shows REVA's support-answer threads (one per Odoo record + field
// being answered) and lets an operator inspect and requeue an individual turn.
// Selecting a thread fetches GET /support-threads/{id}, which returns the
// thread plus its turns (oldest-first by seq, including failed ones — this is
// the operator view, so failures must stay visible). The list endpoint
// deliberately omits turns (a page of 50 threads would drag every answer body
// with it), so the turns only load on drill-down.
type Support struct {
	client    api.ClientIface
	threads   []api.SupportThreadSummary
	total     int
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string
	filtering bool   // capturing the `/` filter text
	filter    string // case-insensitive substring on repo/ticket/model

	// Thread detail: the turns of the selected thread. detailID guards a
	// supportThreadDetailLoadedMsg against a response for a thread the user
	// has since navigated away from; detailSummary is captured at drill-down
	// time so a background refresh of the thread list can't shift it out from
	// under an open detail pane.
	detail        bool
	detailID      int
	detailSummary api.SupportThreadSummary
	detailLoading bool
	detailErr     error
	turns         []api.SupportTurnDetail
	turnCursor    int
	turnOffset    int
}

func newSupport(client api.ClientIface) Support {
	return Support{client: client, loading: true}
}

func (s Support) load() tea.Cmd {
	client := s.client
	return func() tea.Msg {
		data, err := client.SupportThreads(100, 0)
		return supportThreadsLoadedMsg{data: data, err: err}
	}
}

// filtered returns the threads matching the active `/` filter (case-
// insensitive substring on "repo #ticket model"), or all threads when unset.
func (s Support) filtered() []api.SupportThreadSummary {
	if s.filter == "" {
		return s.threads
	}
	q := strings.ToLower(s.filter)
	out := make([]api.SupportThreadSummary, 0, len(s.threads))
	for _, t := range s.threads {
		hay := strings.ToLower(fmt.Sprintf("%s #%d %s", repoOf(t.GithubURL), t.TicketID, t.ModelName))
		if strings.Contains(hay, q) {
			out = append(out, t)
		}
	}
	return out
}

// repoOf renders a thread's github_url as "owner/name", or "—" when absent
// or unparseable (a project-less ticket).
func repoOf(githubURL string) string {
	owner, name, ok := parseOwnerName(githubURL)
	if !ok {
		return "—"
	}
	return owner + "/" + name
}

func (s Support) loadThreadCmd(threadID int) tea.Cmd {
	client := s.client
	return func() tea.Msg {
		data, err := client.SupportThread(threadID)
		return supportThreadDetailLoadedMsg{threadID: threadID, data: data, err: err}
	}
}

func (s Support) requeueTurnCmd(id int) tea.Cmd {
	client := s.client
	return func() tea.Msg {
		err := client.RequeueSupportTurn(id)
		return supportTurnRequeuedMsg{turnID: id, err: err}
	}
}

func (s Support) update(msg tea.Msg) (Support, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		if !s.detail {
			return s, s.load()
		}

	case supportThreadsLoadedMsg:
		s.loading = false
		s.err = m.err
		if m.data != nil {
			s.threads = m.data.Items
			s.total = m.data.Total
		}
		if s.cursor >= len(s.filtered()) {
			s.cursor, s.offset = 0, 0
		}

	case supportThreadDetailLoadedMsg:
		if !s.detail || m.threadID != s.detailID {
			return s, nil // navigated away before the fetch returned
		}
		s.detailLoading = false
		s.detailErr = m.err
		if m.err == nil && m.data != nil {
			s.turns = m.data.Turns
			if s.turnCursor >= len(s.turns) {
				s.turnCursor, s.turnOffset = 0, 0
			}
		}

	case supportTurnRequeuedMsg:
		if m.err != nil {
			s.statusMsg = fmt.Sprintf("requeue of turn #%d failed: %s", m.turnID, m.err)
		} else {
			s.statusMsg = fmt.Sprintf("turn #%d requeued", m.turnID)
		}

	case tea.KeyMsg:
		if s.filtering {
			var changed bool
			s.filtering, s.filter, changed = applyFilterKey(m, s.filter)
			if changed {
				s.cursor, s.offset = 0, 0
			}
			return s, nil
		}

		if s.detail {
			turnRows := s.height - 8
			if turnRows < 1 {
				turnRows = 1
			}
			if c, o, ok := listNav(m.String(), s.turnCursor, s.turnOffset, len(s.turns), turnRows); ok {
				s.turnCursor, s.turnOffset = c, o
				return s, nil
			}
			s.statusMsg = ""
			switch m.String() {
			case "esc", "left", "h":
				s.detail = false
			case "e":
				if s.turnCursor < len(s.turns) {
					return s, s.requeueTurnCmd(s.turns[s.turnCursor].ID)
				}
			case "r":
				s.detailLoading = true
				return s, s.loadThreadCmd(s.detailID)
			}
			return s, nil
		}

		items := s.filtered()
		visibleRows := s.height - 5
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), s.cursor, s.offset, len(items), visibleRows); ok {
			s.cursor, s.offset = c, o
			return s, nil
		}
		s.statusMsg = ""
		switch m.String() {
		case "/":
			s.filtering = true
		case "enter":
			if s.cursor < len(items) {
				t := items[s.cursor]
				s.detail = true
				s.detailID = t.ID
				s.detailSummary = t
				s.detailLoading = true
				s.detailErr = nil
				s.turns, s.turnCursor, s.turnOffset = nil, 0, 0
				return s, s.loadThreadCmd(t.ID)
			}
		case "r":
			s.loading = true
			return s, s.load()
		}
	}
	return s, nil
}

func (s Support) view(w, h int) string {
	if s.detail {
		return s.detailView(w, h)
	}

	items := s.filtered()
	title := fmt.Sprintf("Support (%d)", s.total)
	if s.filter != "" {
		title = fmt.Sprintf("Support (%d/%d)", len(items), s.total)
	}
	header := styleTitle.Padding(0, 1).Render(title)

	if s.loading && len(s.threads) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if s.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", styleStatusFailed.Render("  Error: "+s.err.Error()))
	}
	if len(s.threads) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("No support threads yet")))
	}
	if len(items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No threads match \""+s.filter+"\"  ( / edit · esc clear )")))
	}

	colRepo, colTicket, colModel, colStatus, colWhen := 24, 9, 20, 10, 12
	remaining := w - colTicket - colModel - colStatus - colWhen - 14
	if remaining > colRepo {
		colRepo = remaining
	}
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colRepo, "Repository", colTicket, "Ticket", colModel, "Model",
			colStatus, "Status", colWhen, "Last turn"))

	visibleRows := h - 5
	if visibleRows < 1 {
		visibleRows = 1
	}
	off := ensureVisible(s.offset, s.cursor, visibleRows, len(items))
	end := off + visibleRows
	if end > len(items) {
		end = len(items)
	}
	rows := []string{hdr}
	for i := off; i < end; i++ {
		t := items[i]
		lastTurn := "—"
		if t.LastTurnAt != nil {
			lastTurn = relativeTime(*t.LastTurnAt)
		}
		line := fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s",
			colRepo, truncate(repoOf(t.GithubURL), colRepo),
			colTicket, fmt.Sprintf("#%d", t.TicketID),
			colModel, truncate(t.ModelName, colModel),
			colStatus, t.Status,
			colWhen, lastTurn)
		if i == s.cursor {
			line = styleSelected.Width(w - 2).Render(line)
		}
		rows = append(rows, line)
	}
	table := strings.Join(rows, "\n")

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   [enter] inspect", s.cursor+1, len(items))) +
		cappedNote(len(items), s.total)
	if s.filter != "" && !s.filtering {
		pos = styleSubtitle.Render(fmt.Sprintf("  filter %q  ", s.filter)) + pos
	}
	if s.filtering {
		pos = "  " + styleTitle.Render(" filter ") + "  " + s.filter + "█" +
			styleSubtitle.Render("    [enter] keep   [esc] clear")
	}
	if s.statusMsg != "" {
		pos = "  " + s.statusMsg
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", table, "", pos)
}

// detailView lists every turn of the selected thread, one per line: seq,
// status, request_kind, answer_status and grounding_level — the columns the
// operator needs to distinguish outcomes at a glance, without a further
// drill. Selecting a row and pressing 'e' requeues that turn.
func (s Support) detailView(w, h int) string {
	t := s.detailSummary
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Thread #%d — %s #%d", t.ID, t.ModelName, t.TicketID))

	meta := fmt.Sprintf("  Repo %s   Field %s   Status %s", repoOf(t.GithubURL), t.FieldName, t.Status)

	if s.detailLoading && len(s.turns) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, styleSubtitle.Render(meta), "",
			lipgloss.Place(w, h-5, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading turns...")))
	}
	if s.detailErr != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, styleSubtitle.Render(meta), "",
			styleStatusFailed.Render("  Error: "+s.detailErr.Error()))
	}
	if len(s.turns) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, styleSubtitle.Render(meta), "",
			lipgloss.Place(w, h-5, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("No turns recorded yet")))
	}

	// Img sits BEFORE Grounding: groundingLabel returns lipgloss-styled text,
	// whose ANSI escapes count toward a %-*s width, so the styled column has to
	// stay last and unpadded (as it was before Img existed).
	colSeq, colStatus, colKind, colAnswer, colImg, colGround := 5, 10, 10, 14, 4, 10
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colSeq, "Seq", colStatus, "Status", colKind, "Kind",
			colAnswer, "Answer", colImg, "Img", colGround, "Grounding"))

	visibleRows := h - 8
	if visibleRows < 1 {
		visibleRows = 1
	}
	off := ensureVisible(s.turnOffset, s.turnCursor, visibleRows, len(s.turns))
	end := off + visibleRows
	if end > len(s.turns) {
		end = len(s.turns)
	}
	rows := []string{hdr}
	for i := off; i < end; i++ {
		turn := s.turns[i]
		kind, answer := "—", "—"
		if turn.RequestKind != nil {
			kind = *turn.RequestKind
		}
		if turn.AnswerStatus != nil {
			answer = *turn.AnswerStatus
		}
		plain := fmt.Sprintf("  %-*d  %-*s  %-*s  %-*s  %-*s  %-*s",
			colSeq, turn.Seq, colStatus, turn.Status, colKind, truncate(kind, colKind),
			colAnswer, truncate(answer, colAnswer), colImg, imageCountLabel(turn.ImageCount),
			colGround, groundingPlain(turn.GroundingLevel))
		if i == s.turnCursor {
			rows = append(rows, styleSelected.Width(w-2).Render(plain))
			continue
		}
		colored := fmt.Sprintf("  %-*d  %-*s  %-*s  %-*s  %-*s  %s",
			colSeq, turn.Seq, colStatus, turn.Status, colKind, truncate(kind, colKind),
			colAnswer, truncate(answer, colAnswer), colImg, imageCountLabel(turn.ImageCount),
			groundingLabel(turn.GroundingLevel))
		rows = append(rows, colored)
	}
	table := strings.Join(rows, "\n")

	var extras []string
	if s.turnCursor < len(s.turns) {
		sel := s.turns[s.turnCursor]
		var meta2 []string
		if sel.Confidence != nil && *sel.Confidence != "" {
			meta2 = append(meta2, "confidence:"+*sel.Confidence)
		}
		if sel.EstimatedCostUSD != nil {
			meta2 = append(meta2, fmt.Sprintf("cost:$%.4f", *sel.EstimatedCostUSD))
		}
		if sel.Status == "completed" && sel.CallbackSentAt == nil {
			meta2 = append(meta2, "⚠ not delivered to Odoo")
		}
		if len(meta2) > 0 {
			extras = append(extras, styleSubtitle.Render("  turn #"+fmt.Sprint(sel.ID)+"  "+strings.Join(meta2, "  ")))
		}
		if sel.ErrorMessage != nil && *sel.ErrorMessage != "" {
			extras = append(extras, styleStatusFailed.Render(truncate("  error: "+*sel.ErrorMessage, w-2)))
		}
		if sel.CallbackError != nil && *sel.CallbackError != "" {
			extras = append(extras, styleStatusFailed.Render(truncate("  callback error: "+*sel.CallbackError, w-2)))
		}
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d turns   [e] requeue selected", s.turnCursor+1, len(s.turns)))
	if s.statusMsg != "" {
		pos = "  " + s.statusMsg
	}

	parts := []string{header, styleSubtitle.Render(meta), "", table, "", pos}
	parts = append(parts, extras...)
	return lipgloss.JoinVertical(lipgloss.Left, parts...)
}

// groundingLabel is "code" (best) / "docs" / "none" (worst) — colour-coded so
// the operator can scan for weakly-grounded answers at a glance.
func groundingLabel(level *string) string {
	if level == nil {
		return styleStatusOther.Render("—")
	}
	switch *level {
	case "code":
		return styleStatusCompleted.Render("code")
	case "docs":
		return styleStatusStale.Render("docs")
	case "none":
		return styleStatusFailed.Render("none")
	default:
		return styleStatusOther.Render(*level)
	}
}

// groundingPlain is groundingLabel without ANSI wrapping, for the selected row
// where styleSelected's background would otherwise be broken by embedded
// color codes (matches Tickets' plainStatusSymbol/statusSymbol split).
// imageCountLabel renders the screenshot count for a turn. Plain (unstyled) so
// it can be width-padded like the other text columns.
func imageCountLabel(n int) string {
	if n == 0 {
		return "—"
	}
	return fmt.Sprintf("%d", n)
}

func groundingPlain(level *string) string {
	if level == nil {
		return "—"
	}
	return *level
}
