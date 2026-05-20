package api

import (
	"fmt"
	"time"
)

// MockClient returns static data for UI development without a live server.
type MockClient struct{}

func (m *MockClient) Dashboard() (*DashboardMetrics, error) {
	avgDur := 4820.5
	avgCost := 0.0032
	return &DashboardMetrics{
		Last24h: PeriodStats{
			ReviewsCompleted: 14,
			ReviewsFailed:    2,
			SuccessRate:      0.875,
			AvgDurationMS:    &avgDur,
		},
		Last7d: PeriodStats{
			ReviewsCompleted: 87,
			ReviewsFailed:    5,
			SuccessRate:      0.945,
			AvgDurationMS:    &avgDur,
		},
		Findings24h: FindingCounts{
			Critical: 1,
			Major:    7,
			Minor:    23,
			Info:     41,
		},
		TotalCost7d:        0.2814,
		AvgCostPerReview7d: &avgCost,
	}, nil
}

func (m *MockClient) Reviews(limit int) (*ReviewPage, error) {
	now := time.Now()
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }

	items := []ReviewSummary{
		{
			ID: 101, RepoFullName: "acme/odoo-modules", PRNumber: 312,
			PRTitle:          "feat: add stock valuation override",
			AuthorLogin:      strPtr("alice"),
			HeadSHA:          "a1b2c3d4",
			Status:           "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("high"),
			FindingCount:     5,
			DurationMS:       intPtr(5120),
			EstimatedCostUSD: f64Ptr(0.0041),
			CreatedAt:        now.Add(-10 * time.Minute),
		},
		{
			ID: 100, RepoFullName: "acme/odoo-modules", PRNumber: 311,
			PRTitle:          "fix: correct account move line rounding",
			AuthorLogin:      strPtr("bob"),
			HeadSHA:          "d4e5f6a7",
			Status:           "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("medium"),
			FindingCount:     2,
			DurationMS:       intPtr(3890),
			EstimatedCostUSD: f64Ptr(0.0028),
			CreatedAt:        now.Add(-45 * time.Minute),
		},
		{
			ID: 99, RepoFullName: "acme/integrations", PRNumber: 88,
			PRTitle:          "chore: bump dependencies",
			AuthorLogin:      strPtr("carol"),
			HeadSHA:          "b2c3d4e5",
			Status:           "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("low"),
			FindingCount:     0,
			DurationMS:       intPtr(2100),
			EstimatedCostUSD: f64Ptr(0.0015),
			CreatedAt:        now.Add(-2 * time.Hour),
		},
		{
			ID: 98, RepoFullName: "acme/odoo-modules", PRNumber: 310,
			PRTitle:          "refactor: extract invoice validation logic",
			AuthorLogin:      strPtr("alice"),
			HeadSHA:          "c3d4e5f6",
			Status:           "failed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        nil,
			FindingCount:     0,
			DurationMS:       nil,
			EstimatedCostUSD: nil,
			CreatedAt:        now.Add(-3 * time.Hour),
		},
		{
			ID: 97, RepoFullName: "acme/backend", PRNumber: 201,
			PRTitle:          "feat: add webhook retry mechanism",
			AuthorLogin:      strPtr("dave"),
			HeadSHA:          "e5f6a7b8",
			Status:           "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("medium"),
			FindingCount:     3,
			DurationMS:       intPtr(6200),
			EstimatedCostUSD: f64Ptr(0.0055),
			CreatedAt:        now.Add(-5 * time.Hour),
		},
		{
			ID: 96, RepoFullName: "acme/backend", PRNumber: 200,
			PRTitle:          "fix: race condition in job queue",
			AuthorLogin:      strPtr("bob"),
			HeadSHA:          "f6a7b8c9",
			Status:           "stale", ReviewMode: "diff",
			Model:            nil,
			RiskLevel:        nil,
			FindingCount:     0,
			DurationMS:       nil,
			EstimatedCostUSD: nil,
			CreatedAt:        now.Add(-8 * time.Hour),
		},
	}

	return &ReviewPage{Items: items[:min(limit, len(items))], Total: len(items)}, nil
}

func (m *MockClient) ReviewDetail(id int) (*ReviewDetail, error) {
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }
	fp := func(s string) *string { return &s }

	switch id {
	case 101:
		return &ReviewDetail{
			ReviewSummary: ReviewSummary{
				ID: 101, RepoFullName: "acme/odoo-modules", PRNumber: 312,
				PRTitle: "feat: add stock valuation override",
				AuthorLogin: strPtr("alice"), HeadSHA: "a1b2c3d4",
				Status: "completed", ReviewMode: "diff",
				Model: strPtr("claude-opus-4-5"), RiskLevel: strPtr("high"),
				FindingCount: 5, DurationMS: intPtr(5120),
				EstimatedCostUSD: f64Ptr(0.0041),
				CreatedAt:        time.Now().Add(-10 * time.Minute),
			},
			Summary:      strPtr("This PR introduces a stock valuation override mechanism. Several high-risk patterns were detected including direct SQL manipulation and missing access control checks. Review carefully before merging."),
			InputTokens:  intPtr(18400),
			OutputTokens: intPtr(2100),
			Findings: []FindingDetail{
				{
					ID: 1, Severity: "critical", Category: "security",
					Title: "Direct SQL execution bypasses ORM access rules",
					Confidence: f64Ptr(0.95), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(142),
					Body:           "The method uses `env.cr.execute()` with unsanitized input, bypassing Odoo's ORM security layer.",
					Suggestion:     strPtr("Use `env['stock.valuation'].search()` or `env['stock.valuation'].browse()` instead."),
					IsOdooSpecific: true, ThumbsUp: 0, ThumbsDown: 0,
				},
				{
					ID: 2, Severity: "major", Category: "access-control",
					Title: "Missing `@api.model` decorator on public method",
					Confidence: f64Ptr(0.88), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(87),
					Body:           "The method `_compute_value_override` is accessible from RPC but lacks proper decorator.",
					Suggestion:     strPtr("Add `@api.model` and a `check_access_rights('read')` call."),
					IsOdooSpecific: true, ThumbsUp: 1, ThumbsDown: 0,
				},
				{
					ID: 3, Severity: "major", Category: "data-integrity",
					Title: "Valuation update not wrapped in savepoint",
					Confidence: f64Ptr(0.82), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(201),
					Body:           "Partial failures during batch valuation update can leave data in an inconsistent state.",
					IsOdooSpecific: false, ThumbsUp: 0, ThumbsDown: 0,
				},
				{
					ID: 4, Severity: "minor", Category: "style",
					Title: "Magic number 365 should be a constant",
					Confidence: f64Ptr(0.72), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(55),
					Body:           "The value 365 is used for annual depreciation but is not named.",
					IsOdooSpecific: false, ThumbsUp: 0, ThumbsDown: 1,
				},
				{
					ID: 5, Severity: "info", Category: "performance",
					Title: "N+1 query in valuation report",
					Confidence: f64Ptr(0.65), FilePath: fp("reports/stock_report.py"), LineStart: intPtr(33),
					Body:           "The report iterates over lines and queries the DB per line. Prefetch or a JOIN would help.",
					IsOdooSpecific: false, ThumbsUp: 0, ThumbsDown: 0,
				},
			},
		}, nil

	case 98:
		return &ReviewDetail{
			ReviewSummary: ReviewSummary{
				ID: 98, RepoFullName: "acme/odoo-modules", PRNumber: 310,
				PRTitle: "refactor: extract invoice validation logic",
				AuthorLogin: strPtr("alice"), HeadSHA: "c3d4e5f6",
				Status: "failed", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-3 * time.Hour),
			},
			ErrorClass:   strPtr("APIError"),
			ErrorMessage: strPtr("Claude API returned 529 (overloaded). Retry limit exceeded after 3 attempts."),
		}, nil

	case 96:
		return &ReviewDetail{
			ReviewSummary: ReviewSummary{
				ID: 96, RepoFullName: "acme/backend", PRNumber: 200,
				PRTitle: "fix: race condition in job queue",
				AuthorLogin: strPtr("bob"), HeadSHA: "f6a7b8c9",
				Status: "stale", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-8 * time.Hour),
			},
			ErrorMessage: strPtr("Review did not complete within the 10-minute timeout window."),
		}, nil

	default:
		// Generate a simple completed review for any other ID
		page, _ := m.Reviews(100)
		for _, s := range page.Items {
			if s.ID == id {
				summary := fmt.Sprintf("Review of PR #%d completed. No critical issues found.", s.PRNumber)
				return &ReviewDetail{
					ReviewSummary: s,
					Summary:       &summary,
					InputTokens:   intPtr(8200),
					OutputTokens:  intPtr(940),
					Findings:      []FindingDetail{},
				}, nil
			}
		}
		return nil, fmt.Errorf("review %d not found", id)
	}
}

func (m *MockClient) Failures(limit int) (*FailurePage, error) {
	strPtr := func(s string) *string { return &s }

	items := []ReviewDetail{
		{
			ReviewSummary: ReviewSummary{
				ID: 98, RepoFullName: "acme/odoo-modules", PRNumber: 310,
				PRTitle: "refactor: extract invoice validation logic",
				AuthorLogin: strPtr("alice"), HeadSHA: "c3d4e5f6",
				Status: "failed", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-3 * time.Hour),
			},
			ErrorClass:   strPtr("APIError"),
			ErrorMessage: strPtr("Claude API returned 529 (overloaded). Retry limit exceeded after 3 attempts."),
		},
		{
			ReviewSummary: ReviewSummary{
				ID: 96, RepoFullName: "acme/backend", PRNumber: 200,
				PRTitle: "fix: race condition in job queue",
				AuthorLogin: strPtr("bob"), HeadSHA: "f6a7b8c9",
				Status: "stale", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-8 * time.Hour),
			},
			ErrorMessage: strPtr("Review did not complete within the 10-minute timeout window."),
		},
		{
			ReviewSummary: ReviewSummary{
				ID: 91, RepoFullName: "acme/integrations", PRNumber: 77,
				PRTitle: "feat: Salesforce sync",
				AuthorLogin: strPtr("carol"), HeadSHA: "1a2b3c4d",
				Status: "failed", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-26 * time.Hour),
			},
			ErrorClass:   strPtr("TokenLimitError"),
			ErrorMessage: strPtr("PR diff exceeds maximum context length (214k tokens > 200k limit)."),
		},
	}

	n := limit
	if n > len(items) {
		n = len(items)
	}
	return &FailurePage{Items: items[:n], Total: len(items)}, nil
}

func (m *MockClient) Requeue(id int) error {
	return nil
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
