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
	Audits(limit int) (*AuditRunPage, error)
	AuditFindings(auditRunID, limit int) (*AuditFindingPage, error)
	Repos() (*RepoPage, error)
	TriggerAudit(repoID int) error
	AddRepo(owner, name string) error
	TicketAnalyses(limit int) (*TicketAnalysisPage, error)
	TicketIssueRuns(limit int) (*TicketIssueRunPage, error)
	RequeueTicket(id int) error
	Learning() ([]LearningStat, error)
	Mutes() ([]MuteEntry, error)
	LearnedMemory() ([]LearnedMemoryEntry, error)
	OdooInstances() (*OdooInstancePage, error)
	CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error)
	RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error)
	SetOdooInstanceActive(id int, active bool) error
}
