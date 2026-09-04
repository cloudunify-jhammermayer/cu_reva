package api

// ClientIface is the set of API calls the TUI needs.
type ClientIface interface {
	Dashboard() (*DashboardMetrics, error)
	Reviews(limit int, repo, status, author string) (*ReviewPage, error)
	ReviewDetail(id int) (*ReviewDetail, error)
	Failures(limit int) (*FailurePage, error)
	OpsEvents(limit int) (*OpsEventPage, error)
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
	TimesheetReviews(limit int) (*TimesheetReviewPage, error)
	ReleaseNotes(limit int) (*ReleaseNotePage, error)
	RequeueTicket(id int) error
	RequeueIssueRun(id int) error
	Learning() ([]LearningStat, error)
	Mutes() ([]MuteEntry, error)
	LearnedMemory() ([]LearnedMemoryEntry, error)
	OdooInstances() (*OdooInstancePage, error)
	CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error)
	RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error)
	SetOdooInstanceActive(id int, active bool) error
	DeleteOdooInstance(id int) error
	TicketJourney(odooInstanceID *int, modelName string, ticketID int) (*TicketJourney, error)
	SupportThreads(limit, offset int) (*SupportThreadPage, error)
	SupportThread(threadID int) (*SupportThreadDetail, error)
	RequeueSupportTurn(turnID int) error
	Personas() (*PersonaPage, error)
	ResolvedPersona(repoFullName string) (*ResolvedPersona, error)
	CreatePersona(body PersonaBody) (*Persona, error)
	UpdatePersona(id int, body PersonaBody) (*Persona, error)
}
