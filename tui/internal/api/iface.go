package api

// ClientIface is the set of API calls the TUI needs.
type ClientIface interface {
	Dashboard() (*DashboardMetrics, error)
	Reviews(limit int, repo, status, author string) (*ReviewPage, error)
	ReviewDetail(id int) (*ReviewDetail, error)
	Failures(limit int) (*FailurePage, error)
	Requeue(id int) error
	Pending() (*PendingPage, error)
	Findings(severity, category string, limit int) (*FindingPage, error)
	Repos() (*RepoPage, error)
	TicketAnalyses(limit int) (*TicketAnalysisPage, error)
	RequeueTicket(id int) (string, error)
}
