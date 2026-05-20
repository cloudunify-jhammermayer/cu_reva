package api

import "time"

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
	Summary       *string        `json:"summary"`
	DeclineReason *string        `json:"decline_reason"`
	ErrorMessage  *string        `json:"error_message"`
	ErrorClass    *string        `json:"error_class"`
	InputTokens   *int           `json:"input_tokens"`
	OutputTokens  *int           `json:"output_tokens"`
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

type DashboardMetrics struct {
	Last24h            PeriodStats   `json:"last_24h"`
	Last7d             PeriodStats   `json:"last_7d"`
	Findings24h        FindingCounts `json:"findings_24h"`
	TotalCost7d        float64       `json:"total_cost_7d"`
	AvgCostPerReview7d *float64      `json:"avg_cost_per_review_7d"`
}
