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
	data *api.ReviewDetail
	err  error
}
type failuresLoadedMsg struct {
	data *api.FailurePage
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
type ticketRequeuedMsg struct {
	id  int
	err error
}
type tickMsg struct{}
