package api

import (
	"encoding/json"
	"time"
)

type JourneyEvent struct {
	TS      *time.Time `json:"ts"`
	Kind    string     `json:"kind"`
	Summary string     `json:"summary"`
}

// UnmarshalJSON handles both naive (2026-07-10T12:00:00) and offset-aware
// (2026-07-10T12:00:00+00:00) RFC3339 timestamps. Naive timestamps are
// treated as UTC.
func (je *JourneyEvent) UnmarshalJSON(b []byte) error {
	type alias JourneyEvent
	raw := struct {
		TS *string `json:"ts"`
		*alias
	}{
		alias: (*alias)(je),
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	if raw.TS == nil {
		je.TS = nil
		return nil
	}
	s := *raw.TS
	// Try naive format first (2026-07-10T12:00:00), treating as UTC
	t, err := time.Parse("2006-01-02T15:04:05", s)
	if err == nil {
		je.TS = &t
		return nil
	}
	// Fall back to RFC3339 (handles offset-aware like 2026-07-10T12:00:00+00:00)
	t, err = time.Parse(time.RFC3339, s)
	if err != nil {
		return err
	}
	je.TS = &t
	return nil
}

type JourneyTicket struct {
	OdooInstanceID *int   `json:"odoo_instance_id"`
	ModelName      string `json:"model_name"`
	TicketID       int    `json:"ticket_id"`
	Ready          bool   `json:"ready"`
}

type TicketJourney struct {
	Ticket JourneyTicket  `json:"ticket"`
	Events []JourneyEvent `json:"events"`
}

type FindingSummary struct {
	ID           int      `json:"id"`
	Severity     string   `json:"severity"`
	Category     string   `json:"category"`
	Title        string   `json:"title"`
	Confidence   *float64 `json:"confidence"`
	FilePath     *string  `json:"file_path"`
	LineStart    *int     `json:"line_start"`
	RepoFullName string   `json:"repo_full_name"`
	PRNumber     int      `json:"pr_number"`
}

type FindingPage struct {
	Items []FindingSummary `json:"items"`
	Total int              `json:"total"`
}

type AuditFindingSummary struct {
	ID                int       `json:"id"`
	AuditRunID        int       `json:"audit_run_id"`
	RepoFullName      string    `json:"repo_full_name"`
	Severity          string    `json:"severity"`
	Category          string    `json:"category"`
	Title             string    `json:"title"`
	Confidence        *float64  `json:"confidence"`
	FilePath          *string   `json:"file_path"`
	LineStart         *int      `json:"line_start"`
	GithubIssueNumber *int      `json:"github_issue_number"`
	CreatedAt         time.Time `json:"created_at"`
}

type AuditFindingPage struct {
	Items []AuditFindingSummary `json:"items"`
	Total int                   `json:"total"`
}

type AuditRunSummary struct {
	ID           int        `json:"id"`
	RepoFullName string     `json:"repo_full_name"`
	Status       string     `json:"status"`
	Model        *string    `json:"model"`
	FindingCount int        `json:"finding_count"`
	IssuedCount  int        `json:"issued_count"`
	DurationMS   *int       `json:"duration_ms"`
	RequestedBy  *string    `json:"requested_by"`
	CreatedAt    time.Time  `json:"created_at"`
	CompletedAt  *time.Time `json:"completed_at"`
}

type AuditRunPage struct {
	Items []AuditRunSummary `json:"items"`
	Total int               `json:"total"`
}

type RepoSummary struct {
	ID            int        `json:"id"`
	FullName      string     `json:"full_name"`
	DefaultBranch *string    `json:"default_branch"`
	Enabled       bool       `json:"enabled"`
	ReviewCount   int        `json:"review_count"`
	LastReviewAt  *time.Time `json:"last_review_at"`
	CreatedAt     time.Time  `json:"created_at"`
}

type RepoPage struct {
	Items []RepoSummary `json:"items"`
	Total int           `json:"total"`
}

type CarriedFrom struct {
	RunID int `json:"run_id"`
	PR    int `json:"pr"`
}

type ReviewSummary struct {
	ID               int          `json:"id"`
	RepoFullName     string       `json:"repo_full_name"`
	PRNumber         int          `json:"pr_number"`
	PRTitle          string       `json:"pr_title"`
	AuthorLogin      *string      `json:"author_login"`
	HeadSHA          string       `json:"head_sha"`
	Status           string       `json:"status"`
	ReviewMode       string       `json:"review_mode"`
	Model            *string      `json:"model"`
	RiskLevel        *string      `json:"risk_level"`
	FindingCount     int          `json:"finding_count"`
	DurationMS       *int         `json:"duration_ms"`
	EstimatedCostUSD *float64     `json:"estimated_cost_usd"`
	CreatedAt        time.Time    `json:"created_at"`
	CarriedFrom      *CarriedFrom `json:"carried_from,omitempty"`
}

type FindingDetail struct {
	ID             int      `json:"id"`
	Severity       string   `json:"severity"`
	Category       string   `json:"category"`
	Title          string   `json:"title"`
	Confidence     *float64 `json:"confidence"`
	FilePath       *string  `json:"file_path"`
	LineStart      *int     `json:"line_start"`
	Body           string   `json:"body"`
	Suggestion     *string  `json:"suggestion"`
	IsOdooSpecific bool     `json:"is_odoo_specific"`
	ThumbsUp       int      `json:"thumbs_up"`
	ThumbsDown     int      `json:"thumbs_down"`
}

type IntentCheckItem struct {
	IssueNumber int    `json:"issue_number"`
	Verdict     string `json:"verdict"`
	Note        string `json:"note"`
}

type ReviewDetail struct {
	ReviewSummary
	Summary       *string           `json:"summary"`
	DeclineReason *string           `json:"decline_reason"`
	ErrorMessage  *string           `json:"error_message"`
	ErrorClass    *string           `json:"error_class"`
	InputTokens   *int              `json:"input_tokens"`
	OutputTokens  *int              `json:"output_tokens"`
	Findings      []FindingDetail   `json:"findings"`
	IntentCheck   []IntentCheckItem `json:"intent_check"`
}

type ReviewPage struct {
	Items []ReviewSummary `json:"items"`
	Total int             `json:"total"`
}

type FailurePage struct {
	Items []ReviewDetail `json:"items"`
	Total int            `json:"total"`
}

type PeriodStats struct {
	ReviewsCompleted int      `json:"reviews_completed"`
	ReviewsFailed    int      `json:"reviews_failed"`
	SuccessRate      float64  `json:"success_rate"`
	AvgDurationMS    *float64 `json:"avg_duration_ms"`
}

type FindingCounts struct {
	Critical int `json:"critical"`
	Major    int `json:"major"`
	Minor    int `json:"minor"`
	Info     int `json:"info"`
}

type PendingReview struct {
	ID           int       `json:"id"`
	RepoFullName string    `json:"repo_full_name"`
	PRNumber     int       `json:"pr_number"`
	PRTitle      string    `json:"pr_title"`
	HeadSHA      string    `json:"head_sha"`
	ScheduledAt  time.Time `json:"scheduled_at"`
	TriggerEvent string    `json:"trigger_event"`
	ReviewMode   string    `json:"review_mode"`
	Status       string    `json:"status"`
}

type PendingPage struct {
	Items []PendingReview `json:"items"`
	Total int             `json:"total"`
}

type TicketAnalysisSummary struct {
	ID             int    `json:"id"`
	OdooInstanceID *int   `json:"odoo_instance_id"`
	TicketID       int    `json:"ticket_id"`
	ModelName      string `json:"model_name"`
	FieldName      string `json:"field_name"`
	// GithubURL is the record's project repo, stamped at analysis time; ""
	// (JSON null) for legacy/analysis-only rows. Feeds Tickets-tab grouping.
	GithubURL        string     `json:"github_url"`
	Status           string     `json:"status"`
	Model            *string    `json:"model"`
	InputTokens      *int       `json:"input_tokens"`
	OutputTokens     *int       `json:"output_tokens"`
	EstimatedCostUSD *float64   `json:"estimated_cost_usd"`
	CreatedAt        time.Time  `json:"created_at"`
	CompletedAt      *time.Time `json:"completed_at"`
	ErrorMessage     *string    `json:"error_message"`
	// CallbackSentAt is nil until the Odoo write_field callback lands; a
	// completed analysis with a nil value never reached Odoo.
	CallbackSentAt   *time.Time `json:"callback_sent_at"`
	CallbackError    *string    `json:"callback_error"`
	EstimateHoursMin *float64   `json:"estimate_hours_min"`
	EstimateHoursMax *float64   `json:"estimate_hours_max"`
	// EstimateAnchorRef/EstimateAnchorConfidence identify the golden anchor the
	// first story estimate was based on; nil when nothing comparable was found
	// or the row predates anchoring. Internal only — an anchor names another
	// customer's ticket, so this must never reach anything customer-facing.
	EstimateAnchorRef        *string `json:"estimate_anchor_ref"`
	EstimateAnchorConfidence *string `json:"estimate_anchor_confidence"`
	// RepoDocsSectionsUsed is how many customer-repo doc sections grounded the
	// analysis; nil for legacy/not-attempted rows, 0 when nothing was injected.
	RepoDocsSectionsUsed *int `json:"repo_docs_sections_used"`
}

type TicketAnalysisPage struct {
	Items []TicketAnalysisSummary `json:"items"`
	Total int                     `json:"total"`
}

// TicketIssueRef is one planned/created GitHub issue of a create-issues run.
// Number/URL are nil until the issue exists on GitHub; State ("open"/"closed")
// is synced from GitHub issue webhooks. EstimateHours is nil until/unless the
// issue carries an estimate.
type TicketIssueRef struct {
	Number        *int     `json:"number"`
	Title         string   `json:"title"`
	URL           *string  `json:"url"`
	State         *string  `json:"state"`
	EstimateHours *float64 `json:"estimate_hours"`
}

type TicketIssueRunSummary struct {
	ID               int              `json:"id"`
	OdooInstanceID   *int             `json:"odoo_instance_id"`
	TicketID         int              `json:"ticket_id"`
	ModelName        string           `json:"model_name"`
	GithubURL        string           `json:"github_url"`
	Status           string           `json:"status"`
	IssueType        *string          `json:"issue_type"`
	GithubUsername   *string          `json:"github_username"`
	GithubProjectURL *string          `json:"github_project_url"`
	PlanDate         *string          `json:"plan_date"`
	Issues           []TicketIssueRef `json:"issues"`
	ParentIssue      *TicketIssueRef  `json:"parent_issue"`
	ErrorMessage     *string          `json:"error_message"`
	Model            *string          `json:"model"`
	EstimatedCostUSD *float64         `json:"estimated_cost_usd"`
	CreatedAt        time.Time        `json:"created_at"`
	CompletedAt      *time.Time       `json:"completed_at"`
}

type TicketIssueRunPage struct {
	Items []TicketIssueRunSummary `json:"items"`
	Total int                     `json:"total"`
}

type TimesheetReviewSummary struct {
	ID               int        `json:"id"`
	RequestID        string     `json:"request_id"`
	Status           string     `json:"status"`
	TotalLines       int        `json:"total_lines"`
	OkCount          int        `json:"ok_count"`
	RewrittenCount   int        `json:"rewritten_count"`
	NeedsHumanCount  int        `json:"needs_human_count"`
	EstimatedCostUSD *float64   `json:"estimated_cost_usd"`
	CallbackSentAt   *time.Time `json:"callback_sent_at"`
	ErrorMessage     *string    `json:"error_message"`
	CreatedAt        time.Time  `json:"created_at"`
	CompletedAt      *time.Time `json:"completed_at"`
}

type TimesheetReviewPage struct {
	Items []TimesheetReviewSummary `json:"items"`
	Total int                      `json:"total"`
}

type DashboardMetrics struct {
	Last24h            PeriodStats         `json:"last_24h"`
	Last7d             PeriodStats         `json:"last_7d"`
	Findings24h        FindingCounts       `json:"findings_24h"`
	TotalCost7d        float64             `json:"total_cost_7d"`
	AvgCostPerReview7d *float64            `json:"avg_cost_per_review_7d"`
	ActiveWorkers      int                 `json:"active_workers"`
	Degradations24h    int                 `json:"degradations_24h"`
	TicketsReady       int                 `json:"tickets_ready"`
	CoreKnowledge      []CoreVersionStatus `json:"core_knowledge"`
}

type CoreVersionStatus struct {
	OdooVersion string    `json:"odoo_version"`
	LoadedAt    time.Time `json:"loaded_at"`
	Modules     int       `json:"modules"`
	Sections    int       `json:"sections"`
}

type OpsEventEntry struct {
	ID        int            `json:"id"`
	Component string         `json:"component"`
	Severity  string         `json:"severity"`
	Event     string         `json:"event"`
	Detail    map[string]any `json:"detail"`
	CreatedAt time.Time      `json:"created_at"`
}

type OpsEventPage struct {
	Items []OpsEventEntry `json:"items"`
	Total int             `json:"total"`
}

// LearningStat is one (repo, category) row of the Tier-3 feedback statistic:
// how many findings were posted, dismissed, and fixed — the input for per-repo
// learned memory. Served by GET /api/v1/metrics/learning.
type LearningStat struct {
	Repo             string `json:"repo"`
	Category         string `json:"category"`
	Findings         int    `json:"findings"`
	Dismissed        int    `json:"dismissed"`
	ResolvedByFix    int    `json:"resolved_by_fix"`
	StillOpenAtMerge int    `json:"still_open_at_merge"`
}

// MuteEntry is one active (repo, category) mute. Served by GET /api/v1/metrics/mutes.
type MuteEntry struct {
	Repo      string    `json:"repo"`
	Category  string    `json:"category"`
	MutedBy   string    `json:"muted_by"`
	CreatedAt time.Time `json:"created_at"`
}

// LearnedMemoryEntry is one repo's active learned-review-memory block (Tier-3
// feature B). Served by GET /api/v1/metrics/learned-memory.
type LearnedMemoryEntry struct {
	Repo             string    `json:"repo"`
	Version          int       `json:"version"`
	Content          string    `json:"content"`
	ItemCount        int       `json:"item_count"`
	EstimatedCostUSD *float64  `json:"estimated_cost_usd"`
	CreatedAt        time.Time `json:"created_at"`
}

type TaskCost struct {
	CostUSD      float64 `json:"cost_usd"`
	InputTokens  int     `json:"input_tokens"`
	OutputTokens int     `json:"output_tokens"`
	Count        int     `json:"count"`
}

type WindowCost struct {
	Analysis   TaskCost `json:"analysis"`
	Issues     TaskCost `json:"issues"`
	Timesheets TaskCost `json:"timesheets"`
}

// Total is the window's full spend — every kind the per-instance quota gate
// sums. Display code must use this (not Analysis+Issues) or the shown spend
// disagrees with the API's 429 behavior.
func (w WindowCost) Total() float64 {
	return w.Analysis.CostUSD + w.Issues.CostUSD + w.Timesheets.CostUSD
}

type OdooInstanceCost struct {
	Lifetime WindowCost `json:"lifetime"`
	Last24h  WindowCost `json:"last_24h"`
	Last30d  WindowCost `json:"last_30d"`
}

type OdooInstanceSummary struct {
	ID          int       `json:"id"`
	Name        string    `json:"name"`
	KeyPrefix   string    `json:"key_prefix"`
	CallbackURL string    `json:"callback_url"`
	Active      bool      `json:"active"`
	OdooVersion *string   `json:"odoo_version"`
	CreatedAt   time.Time `json:"created_at"`
	// Per-instance quotas; nil = unlimited.
	DailyBudgetUSD     *float64         `json:"daily_budget_usd"`
	RateLimitPerMinute *int             `json:"rate_limit_per_minute"`
	Cost               OdooInstanceCost `json:"cost"`
}

type OdooInstancePage struct {
	Items []OdooInstanceSummary `json:"items"`
	Total int                   `json:"total"`
}

type OdooInstanceCreated struct {
	ID        int    `json:"id"`
	Name      string `json:"name"`
	KeyPrefix string `json:"key_prefix"`
	APIKey    string `json:"api_key"`
}

// SupportThreadSummary is one REVA<->consultant Q&A thread about an Odoo
// record. GithubURL is "" (JSON null) for a project-less ticket, matching the
// TicketAnalysisSummary convention. Served by GET /api/v1/support-threads.
type SupportThreadSummary struct {
	ID             int        `json:"id"`
	OdooInstanceID *int       `json:"odoo_instance_id"`
	TicketID       int        `json:"ticket_id"`
	ModelName      string     `json:"model_name"`
	FieldName      string     `json:"field_name"`
	GithubURL      string     `json:"github_url"`
	Status         string     `json:"status"`
	CreatedAt      time.Time  `json:"created_at"`
	LastTurnAt     *time.Time `json:"last_turn_at"`
}

type SupportThreadPage struct {
	Items []SupportThreadSummary `json:"items"`
	Total int                    `json:"total"`
}

// SupportThreadDetail is the drill-down payload for one thread: the summary
// fields plus its turns, oldest-first by seq and including failed ones (this
// is the operator view, so failures must stay visible). The list endpoint
// deliberately omits turns — a page of 50 threads would drag every answer
// body with it. Served by GET /api/v1/support-threads/{thread_id}.
type SupportThreadDetail struct {
	SupportThreadSummary
	Turns []SupportTurnDetail `json:"turns"`
}

// SupportTurnDetail is one question/answer round inside a support thread.
// GroundingLevel is "code" / "docs" / "none" (nil until the turn completes) —
// operationally the most important field: how well-grounded the drafted
// answer was. Served by GET /api/v1/support-turn/{turn_id}.
type SupportTurnDetail struct {
	ID             int     `json:"id"`
	ThreadID       int     `json:"thread_id"`
	Seq            int     `json:"seq"`
	JobID          *string `json:"job_id"`
	Question       string  `json:"question"`
	AnswerHTML     *string `json:"answer_html"`
	RequestKind    *string `json:"request_kind"`
	AnswerStatus   *string `json:"answer_status"`
	GroundingLevel *string `json:"grounding_level"`
	// Confidence is the model's own high/medium/low read on the draft, derived
	// server-side from the turn's structured result. Nil until it completes.
	Confidence *string `json:"confidence"`
	// ImageCount is how many screenshots the turn was submitted with. The bytes
	// are not stored, so a requeued turn re-runs image-blind — this column is
	// how an operator tells that apart from a genuinely well-grounded answer.
	ImageCount       int        `json:"image_count"`
	Status           string     `json:"status"`
	ErrorMessage     *string    `json:"error_message"`
	EstimatedCostUSD *float64   `json:"estimated_cost_usd"`
	CreatedAt        time.Time  `json:"created_at"`
	CompletedAt      *time.Time `json:"completed_at"`
	CallbackSentAt   *time.Time `json:"callback_sent_at"`
	CallbackError    *string    `json:"callback_error"`
}

// Persona is one configured tone row — either the single 'default' scope row
// or one 'repo' scope row. A nil knob inherits from the default row at
// resolution time (see ResolvedPersona / GET /personas/resolved).
type Persona struct {
	ID             int     `json:"id"`
	Scope          string  `json:"scope"`
	RepoFullName   *string `json:"repo_full_name"`
	Language       *string `json:"language"`
	Formality      *string `json:"formality"`
	TechnicalDepth *string `json:"technical_depth"`
	Length         *string `json:"length"`
	Salutation     *string `json:"salutation"`
	SignOff        *string `json:"sign_off"`
	StyleNotes     *string `json:"style_notes"`
	ContentPolicy  *string `json:"content_policy"`
	Active         bool    `json:"active"`
}

type PersonaPage struct {
	Items []Persona `json:"items"`
	Total int       `json:"total"`
}

// PersonaBody is the create/update payload. Replace semantics: PATCH replaces
// every knob with what's set here (nil clears it back to "inherit"), so an
// active-only toggle must resend the row's other current values.
type PersonaBody struct {
	Scope          string  `json:"scope"`
	RepoFullName   *string `json:"repo_full_name,omitempty"`
	Language       *string `json:"language,omitempty"`
	Formality      *string `json:"formality,omitempty"`
	TechnicalDepth *string `json:"technical_depth,omitempty"`
	Length         *string `json:"length,omitempty"`
	Salutation     *string `json:"salutation,omitempty"`
	SignOff        *string `json:"sign_off,omitempty"`
	StyleNotes     *string `json:"style_notes,omitempty"`
	ContentPolicy  *string `json:"content_policy,omitempty"`
	Active         bool    `json:"active"`
}

// ResolvedPersona is what a support answer for RepoFullName would actually be
// written with — the merged knobs (default < repo < hardcoded fallback), plus
// RenderedBlock, the exact text injected into the prompt. This is the view
// that answers "why did REVA write it in that tone". Served by
// GET /api/v1/personas/resolved?repo_full_name=.
type ResolvedPersona struct {
	RepoFullName   *string   `json:"repo_full_name"`
	Language       *string   `json:"language"`
	Formality      *string   `json:"formality"`
	TechnicalDepth *string   `json:"technical_depth"`
	Length         *string   `json:"length"`
	Salutation     *string   `json:"salutation"`
	SignOff        *string   `json:"sign_off"`
	StyleNotes     *string   `json:"style_notes"`
	ContentPolicy  *string   `json:"content_policy"`
	RenderedBlock  string    `json:"rendered_block"`
	ResolvedAt     time.Time `json:"resolved_at"`
}
