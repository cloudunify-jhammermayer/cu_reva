package api

import "time"

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

type ReviewSummary struct {
	ID               int       `json:"id"`
	RepoFullName     string    `json:"repo_full_name"`
	PRNumber         int       `json:"pr_number"`
	PRTitle          string    `json:"pr_title"`
	AuthorLogin      *string   `json:"author_login"`
	HeadSHA          string    `json:"head_sha"`
	Status           string    `json:"status"`
	ReviewMode       string    `json:"review_mode"`
	Model            *string   `json:"model"`
	RiskLevel        *string   `json:"risk_level"`
	FindingCount     int       `json:"finding_count"`
	DurationMS       *int      `json:"duration_ms"`
	EstimatedCostUSD *float64  `json:"estimated_cost_usd"`
	CreatedAt        time.Time `json:"created_at"`
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

type ReviewDetail struct {
	ReviewSummary
	Summary       *string         `json:"summary"`
	DeclineReason *string         `json:"decline_reason"`
	ErrorMessage  *string         `json:"error_message"`
	ErrorClass    *string         `json:"error_class"`
	InputTokens   *int            `json:"input_tokens"`
	OutputTokens  *int            `json:"output_tokens"`
	Findings      []FindingDetail `json:"findings"`
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
	ID               int        `json:"id"`
	TicketID         int        `json:"ticket_id"`
	ModelName        string     `json:"model_name"`
	FieldName        string     `json:"field_name"`
	Status           string     `json:"status"`
	Model            *string    `json:"model"`
	InputTokens      *int       `json:"input_tokens"`
	OutputTokens     *int       `json:"output_tokens"`
	EstimatedCostUSD *float64   `json:"estimated_cost_usd"`
	CreatedAt        time.Time  `json:"created_at"`
	CompletedAt      *time.Time `json:"completed_at"`
	ErrorMessage     *string    `json:"error_message"`
}

type TicketAnalysisPage struct {
	Items []TicketAnalysisSummary `json:"items"`
	Total int                     `json:"total"`
}

// TicketIssueRef is one planned/created GitHub issue of a create-issues run.
// Number/URL are nil until the issue exists on GitHub; State ("open"/"closed")
// is synced from GitHub issue webhooks.
type TicketIssueRef struct {
	Number *int    `json:"number"`
	Title  string  `json:"title"`
	URL    *string `json:"url"`
	State  *string `json:"state"`
}

type TicketIssueRunSummary struct {
	ID               int              `json:"id"`
	TicketID         int              `json:"ticket_id"`
	ModelName        string           `json:"model_name"`
	GithubURL        string           `json:"github_url"`
	Status           string           `json:"status"`
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

type DashboardMetrics struct {
	Last24h            PeriodStats   `json:"last_24h"`
	Last7d             PeriodStats   `json:"last_7d"`
	Findings24h        FindingCounts `json:"findings_24h"`
	TotalCost7d        float64       `json:"total_cost_7d"`
	AvgCostPerReview7d *float64      `json:"avg_cost_per_review_7d"`
	ActiveWorkers      int           `json:"active_workers"`
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

type TaskCost struct {
	CostUSD      float64 `json:"cost_usd"`
	InputTokens  int     `json:"input_tokens"`
	OutputTokens int     `json:"output_tokens"`
	Count        int     `json:"count"`
}

type WindowCost struct {
	Analysis TaskCost `json:"analysis"`
	Issues   TaskCost `json:"issues"`
}

type OdooInstanceCost struct {
	Lifetime WindowCost `json:"lifetime"`
	Last24h  WindowCost `json:"last_24h"`
	Last30d  WindowCost `json:"last_30d"`
}

type OdooInstanceSummary struct {
	ID          int              `json:"id"`
	Name        string           `json:"name"`
	KeyPrefix   string           `json:"key_prefix"`
	CallbackURL string           `json:"callback_url"`
	Active      bool             `json:"active"`
	CreatedAt   time.Time        `json:"created_at"`
	Cost        OdooInstanceCost `json:"cost"`
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
