package ui

import "reva-tui/internal/api"

type dashboardLoadedMsg struct {
	data *api.DashboardMetrics
	err  error
}
type reviewsLoadedMsg struct {
	data *api.ReviewPage
	err  error
}
type reviewDetailLoadedMsg struct {
	id   int // which review this detail is for — guards against stale responses
	data *api.ReviewDetail
	err  error
}
type failuresLoadedMsg struct {
	data *api.FailurePage
	err  error
}
type opsEventsLoadedMsg struct {
	data *api.OpsEventPage
	err  error
}
type requeuedMsg struct {
	id  int
	err error
}
type pendingLoadedMsg struct {
	data *api.PendingPage
	err  error
}
type findingsLoadedMsg struct {
	data *api.FindingPage
	err  error
}
type auditRunsLoadedMsg struct {
	data *api.AuditRunPage
	err  error
}
type auditFindingsLoadedMsg struct {
	data *api.AuditFindingPage
	err  error
}
type auditTriggeredMsg struct {
	id  int
	err error
}
type repoAddedMsg struct {
	owner string
	name  string
	err   error
}
type reposLoadedMsg struct {
	data *api.RepoPage
	err  error
}
type ticketAnalysesLoadedMsg struct {
	data *api.TicketAnalysisPage
	err  error
}
type ticketIssueRunsLoadedMsg struct {
	data *api.TicketIssueRunPage
	err  error
}
type timesheetsLoadedMsg struct {
	data *api.TimesheetReviewPage
	err  error
}
type ticketRequeuedMsg struct {
	id  int
	err error
}
type feedbackLoadedMsg struct {
	stats  []api.LearningStat
	mutes  []api.MuteEntry
	memory []api.LearnedMemoryEntry
	err    error
}
type odooLoadedMsg struct {
	data *api.OdooInstancePage
	err  error
}
type odooCreatedMsg struct {
	created *api.OdooInstanceCreated
	err     error
}
type odooActionMsg struct {
	err error
}
type tickMsg struct{}
