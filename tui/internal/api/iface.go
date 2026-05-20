package api

// ClientIface is the set of API calls the TUI needs.
type ClientIface interface {
	Dashboard() (*DashboardMetrics, error)
	Reviews(limit int) (*ReviewPage, error)
	ReviewDetail(id int) (*ReviewDetail, error)
	Failures(limit int) (*FailurePage, error)
	Requeue(id int) error
}
