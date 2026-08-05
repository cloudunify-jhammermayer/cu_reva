package api

import (
	"fmt"
	"strings"
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
		ActiveWorkers:      2,
		TicketsReady:       1,
		CoreKnowledge: []CoreVersionStatus{{
			OdooVersion: "19.0",
			LoadedAt:    time.Now().Add(-24 * time.Hour),
			Modules:     625,
			Sections:    9000,
		}},
	}, nil
}

func (m *MockClient) Reviews(limit int, repo, status, author string) (*ReviewPage, error) {
	now := time.Now()
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }

	items := []ReviewSummary{
		{
			ID: 101, RepoFullName: "acme/odoo-modules", PRNumber: 312,
			PRTitle:     "feat: add stock valuation override",
			AuthorLogin: strPtr("alice"),
			HeadSHA:     "a1b2c3d4",
			Status:      "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("high"),
			FindingCount:     5,
			DurationMS:       intPtr(5120),
			EstimatedCostUSD: f64Ptr(0.0041),
			CreatedAt:        now.Add(-10 * time.Minute),
		},
		{
			ID: 100, RepoFullName: "acme/odoo-modules", PRNumber: 311,
			PRTitle:     "fix: correct account move line rounding",
			AuthorLogin: strPtr("bob"),
			HeadSHA:     "d4e5f6a7",
			Status:      "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("medium"),
			FindingCount:     2,
			DurationMS:       intPtr(3890),
			EstimatedCostUSD: f64Ptr(0.0028),
			CreatedAt:        now.Add(-45 * time.Minute),
		},
		{
			ID: 99, RepoFullName: "acme/integrations", PRNumber: 88,
			PRTitle:     "chore: promote to prod",
			AuthorLogin: strPtr("carol"),
			HeadSHA:     "b2c3d4e5",
			Status:      "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("low"),
			FindingCount:     0,
			DurationMS:       intPtr(2100),
			EstimatedCostUSD: f64Ptr(0.0015),
			CreatedAt:        now.Add(-2 * time.Hour),
			// Demo of a carry-forward review (spec 2026-07-24): identical diff
			// already reviewed on PR #311, so this promotion reuses the verdict.
			CarriedFrom: &CarriedFrom{RunID: 100, PR: 311},
		},
		{
			ID: 98, RepoFullName: "acme/odoo-modules", PRNumber: 310,
			PRTitle:     "refactor: extract invoice validation logic",
			AuthorLogin: strPtr("alice"),
			HeadSHA:     "c3d4e5f6",
			Status:      "failed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        nil,
			FindingCount:     0,
			DurationMS:       nil,
			EstimatedCostUSD: nil,
			CreatedAt:        now.Add(-3 * time.Hour),
		},
		{
			ID: 97, RepoFullName: "acme/backend", PRNumber: 201,
			PRTitle:     "feat: add webhook retry mechanism",
			AuthorLogin: strPtr("dave"),
			HeadSHA:     "e5f6a7b8",
			Status:      "completed", ReviewMode: "diff",
			Model:            strPtr("claude-opus-4-5"),
			RiskLevel:        strPtr("medium"),
			FindingCount:     3,
			DurationMS:       intPtr(6200),
			EstimatedCostUSD: f64Ptr(0.0055),
			CreatedAt:        now.Add(-5 * time.Hour),
		},
		{
			ID: 96, RepoFullName: "acme/backend", PRNumber: 200,
			PRTitle:     "fix: race condition in job queue",
			AuthorLogin: strPtr("bob"),
			HeadSHA:     "f6a7b8c9",
			Status:      "stale", ReviewMode: "diff",
			Model:            nil,
			RiskLevel:        nil,
			FindingCount:     0,
			DurationMS:       nil,
			EstimatedCostUSD: nil,
			CreatedAt:        now.Add(-8 * time.Hour),
		},
	}

	var filtered []ReviewSummary
	for _, it := range items {
		// Substring match, mirroring the server's ILIKE (M26).
		if repo != "" && !strings.Contains(
			strings.ToLower(it.RepoFullName), strings.ToLower(repo)) {
			continue
		}
		if status != "" && it.Status != status {
			continue
		}
		if author != "" && (it.AuthorLogin == nil || *it.AuthorLogin != author) {
			continue
		}
		filtered = append(filtered, it)
	}
	n := min(limit, len(filtered))
	return &ReviewPage{Items: filtered[:n], Total: len(filtered)}, nil
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
				PRTitle:     "feat: add stock valuation override",
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
					Title:      "Direct SQL execution bypasses ORM access rules",
					Confidence: f64Ptr(0.95), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(142),
					Body:           "The method uses `env.cr.execute()` with unsanitized input, bypassing Odoo's ORM security layer.",
					Suggestion:     strPtr("Use `env['stock.valuation'].search()` or `env['stock.valuation'].browse()` instead."),
					IsOdooSpecific: true, ThumbsUp: 0, ThumbsDown: 0,
				},
				{
					ID: 2, Severity: "major", Category: "access-control",
					Title:      "Missing `@api.model` decorator on public method",
					Confidence: f64Ptr(0.88), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(87),
					Body:           "The method `_compute_value_override` is accessible from RPC but lacks proper decorator.",
					Suggestion:     strPtr("Add `@api.model` and a `check_access_rights('read')` call."),
					IsOdooSpecific: true, ThumbsUp: 1, ThumbsDown: 0,
				},
				{
					ID: 3, Severity: "major", Category: "data-integrity",
					Title:      "Valuation update not wrapped in savepoint",
					Confidence: f64Ptr(0.82), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(201),
					Body:           "Partial failures during batch valuation update can leave data in an inconsistent state.",
					IsOdooSpecific: false, ThumbsUp: 0, ThumbsDown: 0,
				},
				{
					ID: 4, Severity: "minor", Category: "style",
					Title:      "Magic number 365 should be a constant",
					Confidence: f64Ptr(0.72), FilePath: fp("models/stock_valuation.py"), LineStart: intPtr(55),
					Body:           "The value 365 is used for annual depreciation but is not named.",
					IsOdooSpecific: false, ThumbsUp: 0, ThumbsDown: 1,
				},
				{
					ID: 5, Severity: "info", Category: "performance",
					Title:      "N+1 query in valuation report",
					Confidence: f64Ptr(0.65), FilePath: fp("reports/stock_report.py"), LineStart: intPtr(33),
					Body:           "The report iterates over lines and queries the DB per line. Prefetch or a JOIN would help.",
					IsOdooSpecific: false, ThumbsUp: 0, ThumbsDown: 0,
				},
			},
			IntentCheck: []IntentCheckItem{
				{IssueNumber: 118, Verdict: "matches", Note: "valuation override implemented as requested"},
				{IssueNumber: 119, Verdict: "partial", Note: "report export criterion not addressed"},
			},
		}, nil

	case 98:
		return &ReviewDetail{
			ReviewSummary: ReviewSummary{
				ID: 98, RepoFullName: "acme/odoo-modules", PRNumber: 310,
				PRTitle:     "refactor: extract invoice validation logic",
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
				PRTitle:     "fix: race condition in job queue",
				AuthorLogin: strPtr("bob"), HeadSHA: "f6a7b8c9",
				Status: "stale", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-8 * time.Hour),
			},
			ErrorMessage: strPtr("Review did not complete within the 10-minute timeout window."),
		}, nil

	default:
		// Generate a simple completed review for any other ID
		page, _ := m.Reviews(100, "", "", "")
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
				PRTitle:     "refactor: extract invoice validation logic",
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
				PRTitle:     "fix: race condition in job queue",
				AuthorLogin: strPtr("bob"), HeadSHA: "f6a7b8c9",
				Status: "stale", ReviewMode: "diff",
				CreatedAt: time.Now().Add(-8 * time.Hour),
			},
			ErrorMessage: strPtr("Review did not complete within the 10-minute timeout window."),
		},
		{
			ReviewSummary: ReviewSummary{
				ID: 91, RepoFullName: "acme/integrations", PRNumber: 77,
				PRTitle:     "feat: Salesforce sync",
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

func (m *MockClient) OpsEvents(limit int) (*OpsEventPage, error) {
	now := time.Now()
	items := []OpsEventEntry{
		{
			ID: 3, Component: "codegraph", Severity: "warning", Event: "index_failed",
			Detail:    map[string]any{"repo": "acme/odoo-modules"},
			CreatedAt: now.Add(-10 * time.Minute),
		},
		{
			ID: 2, Component: "odoo_callback", Severity: "error", Event: "write_field_failed",
			Detail:    map[string]any{"analysis_id": 12},
			CreatedAt: now.Add(-1 * time.Hour),
		},
		{
			ID: 1, Component: "git", Severity: "warning", Event: "timeout",
			Detail:    map[string]any{"cmd": "fetch"},
			CreatedAt: now.Add(-3 * time.Hour),
		},
	}
	n := limit
	if n > len(items) {
		n = len(items)
	}
	return &OpsEventPage{Items: items[:n], Total: len(items)}, nil
}

func (m *MockClient) Pending() (*PendingPage, error) {
	now := time.Now()
	items := []PendingReview{
		{
			ID: 1, RepoFullName: "acme/odoo-modules", PRNumber: 313,
			PRTitle: "feat: new inventory workflow", HeadSHA: "a1b2c3d4",
			ScheduledAt: now.Add(7 * time.Minute), TriggerEvent: "opened", ReviewMode: "diff",
		},
		{
			ID: 2, RepoFullName: "acme/backend", PRNumber: 202,
			PRTitle: "fix: null pointer in payment processor", HeadSHA: "b2c3d4e5",
			ScheduledAt: now.Add(-30 * time.Second), TriggerEvent: "synchronize", ReviewMode: "diff",
		},
	}
	return &PendingPage{Items: items, Total: len(items)}, nil
}

func (m *MockClient) Findings(severity, category string, limit int) (*FindingPage, error) {
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }

	all := []FindingSummary{
		{
			ID: 1, Severity: "critical", Category: "security",
			Title:      "Direct SQL execution bypasses ORM access rules",
			Confidence: f64Ptr(0.95), FilePath: strPtr("models/stock_valuation.py"), LineStart: intPtr(142),
		},
		{
			ID: 2, Severity: "major", Category: "access-control",
			Title:      "Missing @api.model decorator on public method",
			Confidence: f64Ptr(0.88), FilePath: strPtr("models/stock_valuation.py"), LineStart: intPtr(87),
		},
		{
			ID: 3, Severity: "major", Category: "data-integrity",
			Title:      "Valuation update not wrapped in savepoint",
			Confidence: f64Ptr(0.82), FilePath: strPtr("models/stock_valuation.py"), LineStart: intPtr(201),
		},
		{
			ID: 4, Severity: "minor", Category: "style",
			Title:      "Magic number 365 should be a constant",
			Confidence: f64Ptr(0.72), FilePath: strPtr("models/stock_valuation.py"), LineStart: intPtr(55),
		},
		{
			ID: 5, Severity: "info", Category: "performance",
			Title:      "N+1 query in valuation report",
			Confidence: f64Ptr(0.65), FilePath: strPtr("reports/stock_report.py"), LineStart: intPtr(33),
		},
		{
			ID: 6, Severity: "critical", Category: "security",
			Title:      "Unvalidated redirect via request parameter",
			Confidence: f64Ptr(0.97), FilePath: strPtr("controllers/main.py"), LineStart: intPtr(78),
		},
		{
			ID: 7, Severity: "major", Category: "performance",
			Title:      "Missing index on frequently queried column",
			Confidence: f64Ptr(0.79), FilePath: strPtr("models/sale_order.py"), LineStart: intPtr(310),
		},
		{
			ID: 8, Severity: "minor", Category: "style",
			Title:      "Inconsistent string quoting across module",
			Confidence: f64Ptr(0.55), FilePath: strPtr("models/sale_order.py"), LineStart: intPtr(12),
		},
		{
			ID: 9, Severity: "info", Category: "documentation",
			Title:      "Public method missing docstring",
			Confidence: f64Ptr(0.60), FilePath: strPtr("models/account_move.py"), LineStart: intPtr(44),
		},
	}

	// Spread findings across a few repos/PRs so the demo exercises the repo
	// column, the `/` filter, and `o` (open PR).
	repos := []struct {
		name string
		pr   int
	}{{"acme/odoo-modules", 312}, {"acme/widgets", 88}, {"acme/api", 145}}
	for i := range all {
		all[i].RepoFullName = repos[i%len(repos)].name
		all[i].PRNumber = repos[i%len(repos)].pr
	}

	var filtered []FindingSummary
	for _, f := range all {
		if severity == "" || f.Severity == severity {
			filtered = append(filtered, f)
		}
	}
	n := limit
	if n > len(filtered) {
		n = len(filtered)
	}
	return &FindingPage{Items: filtered[:n], Total: len(filtered)}, nil
}

func (m *MockClient) Audits(limit int) (*AuditRunPage, error) {
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	now := time.Now()
	done := now.Add(-2 * time.Hour)
	runs := []AuditRunSummary{
		{
			ID: 8, RepoFullName: "acme/api", Status: "running",
			Model: strPtr("claude-opus-4-8"), CreatedAt: now.Add(-1 * time.Minute),
		},
		{
			ID: 7, RepoFullName: "acme/widgets", Status: "completed",
			Model: strPtr("claude-opus-4-8"), FindingCount: 3, IssuedCount: 2,
			DurationMS: intPtr(221185), CreatedAt: done, CompletedAt: &done,
		},
		{
			ID: 6, RepoFullName: "acme/legacy", Status: "failed",
			Model: strPtr("claude-opus-4-8"), CreatedAt: now.Add(-3 * time.Hour),
		},
	}
	return &AuditRunPage{Items: runs, Total: len(runs)}, nil
}

func (m *MockClient) AuditFindings(auditRunID, limit int) (*AuditFindingPage, error) {
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }
	now := time.Now()

	all := []AuditFindingSummary{
		{
			ID: 1, AuditRunID: 7, RepoFullName: "acme/widgets", Severity: "critical",
			Category: "security", Title: "Hardcoded API token in settings module",
			Confidence: f64Ptr(0.96), FilePath: strPtr("config/settings.py"), LineStart: intPtr(21),
			GithubIssueNumber: intPtr(312), CreatedAt: now.Add(-2 * time.Hour),
		},
		{
			ID: 2, AuditRunID: 7, RepoFullName: "acme/widgets", Severity: "major",
			Category: "access-control", Title: "Endpoint missing auth decorator",
			Confidence: f64Ptr(0.84), FilePath: strPtr("controllers/portal.py"), LineStart: intPtr(133),
			GithubIssueNumber: intPtr(313), CreatedAt: now.Add(-2 * time.Hour),
		},
		{
			ID: 3, AuditRunID: 7, RepoFullName: "acme/widgets", Severity: "minor",
			Category: "style", Title: "Unused import in valuation model",
			Confidence: f64Ptr(0.6), FilePath: strPtr("models/valuation.py"), LineStart: intPtr(4),
			GithubIssueNumber: nil, CreatedAt: now.Add(-2 * time.Hour),
		},
	}

	var filtered []AuditFindingSummary
	for _, f := range all {
		if auditRunID == 0 || f.AuditRunID == auditRunID {
			filtered = append(filtered, f)
		}
	}
	return &AuditFindingPage{Items: filtered, Total: len(filtered)}, nil
}

func (m *MockClient) Repos() (*RepoPage, error) {
	strPtr := func(s string) *string { return &s }
	now := time.Now()
	t1 := now.Add(-10 * time.Minute)
	t2 := now.Add(-2 * time.Hour)
	t3 := now.Add(-26 * time.Hour)

	items := []RepoSummary{
		{
			ID: 1, FullName: "acme/odoo-modules", DefaultBranch: strPtr("main"),
			Enabled: true, ReviewCount: 312, LastReviewAt: &t1,
			CreatedAt: now.Add(-180 * 24 * time.Hour),
		},
		{
			ID: 2, FullName: "acme/backend", DefaultBranch: strPtr("main"),
			Enabled: true, ReviewCount: 201, LastReviewAt: &t2,
			CreatedAt: now.Add(-120 * 24 * time.Hour),
		},
		{
			ID: 3, FullName: "acme/integrations", DefaultBranch: strPtr("develop"),
			Enabled: true, ReviewCount: 88, LastReviewAt: &t3,
			CreatedAt: now.Add(-60 * 24 * time.Hour),
		},
		{
			ID: 4, FullName: "acme/legacy-erp", DefaultBranch: strPtr("master"),
			Enabled: false, ReviewCount: 5, LastReviewAt: nil,
			CreatedAt: now.Add(-365 * 24 * time.Hour),
		},
	}
	return &RepoPage{Items: items, Total: len(items)}, nil
}

func (m *MockClient) TicketAnalyses(limit int) (*TicketAnalysisPage, error) {
	now := time.Now()
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }
	t1 := now.Add(-5 * time.Minute)

	items := []TicketAnalysisSummary{
		{
			ID: 3, OdooInstanceID: intPtr(1), TicketID: 456, ModelName: "helpdesk.ticket", FieldName: "description",
			Status: "completed", Model: strPtr("claude-sonnet-4-6"),
			InputTokens: intPtr(1840), OutputTokens: intPtr(712),
			EstimatedCostUSD: f64Ptr(0.0032), CreatedAt: now.Add(-2 * time.Minute), CompletedAt: &t1,
			CallbackSentAt:   &t1,
			EstimateHoursMin: f64Ptr(12), EstimateHoursMax: f64Ptr(20),
			EstimateAnchorRef: strPtr("bom-copies#bom-copy-mechanism"), EstimateAnchorConfidence: strPtr("high"),
			RepoDocsSectionsUsed: intPtr(4),
		},
		{
			ID: 2, OdooInstanceID: intPtr(1), TicketID: 123, ModelName: "project.task", FieldName: "description",
			Status: "failed", Model: nil,
			InputTokens: nil, OutputTokens: nil,
			EstimatedCostUSD: nil, CreatedAt: now.Add(-10 * time.Minute), CompletedAt: nil,
			ErrorMessage: strPtr("Odoo callback timed out: timed out"),
		},
		{
			ID: 1, OdooInstanceID: intPtr(1), TicketID: 99, ModelName: "helpdesk.ticket", FieldName: "description",
			Status: "pending", Model: nil,
			InputTokens: nil, OutputTokens: nil,
			EstimatedCostUSD: nil, CreatedAt: now.Add(-30 * time.Second), CompletedAt: nil,
		},
		{
			// Analysis-only ticket (no create-issues run) — groups under
			// "(no repo yet)" in the Tickets tab. Completed but the Odoo callback
			// failed, so it reads "completed ⚠ not in Odoo".
			ID: 4, OdooInstanceID: intPtr(2), TicketID: 777, ModelName: "helpdesk.ticket", FieldName: "description",
			Status: "completed", Model: strPtr("claude-sonnet-4-6"),
			InputTokens: intPtr(920), OutputTokens: intPtr(204),
			EstimatedCostUSD: f64Ptr(0.0011), CreatedAt: now.Add(-45 * time.Minute),
			CompletedAt:      &t1,
			CallbackError:    strPtr("Odoo write_field timed out"),
			EstimateHoursMin: f64Ptr(3), EstimateHoursMax: f64Ptr(6),
		},
		{
			// Analysis-only ticket that carries its project repo (github_url set
			// at analysis time) — groups under "acme/portal" from the first step,
			// before any create-issues run exists.
			ID: 5, OdooInstanceID: intPtr(1), TicketID: 888, ModelName: "project.task", FieldName: "description",
			GithubURL: "https://github.com/acme/portal",
			Status:    "completed", Model: strPtr("claude-sonnet-4-6"),
			InputTokens: intPtr(1120), OutputTokens: intPtr(288),
			EstimatedCostUSD: f64Ptr(0.0015), CreatedAt: now.Add(-8 * time.Minute),
			CompletedAt:      &t1,
			CallbackSentAt:   &t1,
			EstimateHoursMin: f64Ptr(2), EstimateHoursMax: f64Ptr(5),
		},
	}
	n := limit
	if n > len(items) {
		n = len(items)
	}
	return &TicketAnalysisPage{Items: items[:n], Total: len(items)}, nil
}

func (m *MockClient) TicketIssueRuns(limit int) (*TicketIssueRunPage, error) {
	now := time.Now()
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	f64Ptr := func(f float64) *float64 { return &f }
	t1 := now.Add(-3 * time.Minute)

	items := []TicketIssueRunSummary{
		{
			ID: 3, OdooInstanceID: intPtr(1), TicketID: 456, ModelName: "helpdesk.ticket",
			GithubURL: "https://github.com/acme/widgets", Status: "completed",
			GithubUsername:   strPtr("alice"),
			GithubProjectURL: strPtr("https://github.com/orgs/acme/projects/5"),
			PlanDate:         strPtr("2026-07-15"),
			Issues: []TicketIssueRef{
				{Number: intPtr(42), Title: "Implement login form",
					URL:           strPtr("https://github.com/acme/widgets/issues/42"),
					State:         strPtr("closed"),
					EstimateHours: f64Ptr(1.5)},
				{Number: intPtr(43), Title: "Add session handling",
					URL:           strPtr("https://github.com/acme/widgets/issues/43"),
					State:         strPtr("open"),
					EstimateHours: f64Ptr(2.5)},
			},
			ParentIssue: &TicketIssueRef{Number: intPtr(41),
				Title: "[Ticket 456] Login & sessions",
				URL:   strPtr("https://github.com/acme/widgets/issues/41"),
				State: strPtr("open")},
			Model:            strPtr("claude-sonnet-4-6"),
			EstimatedCostUSD: f64Ptr(0.0048),
			CreatedAt:        now.Add(-4 * time.Minute), CompletedAt: &t1,
		},
		{
			ID: 2, OdooInstanceID: intPtr(1), TicketID: 123, ModelName: "project.task",
			GithubURL: "https://github.com/acme/odoo-modules", Status: "failed",
			Issues: []TicketIssueRef{
				{Number: intPtr(40), Title: "Create export wizard",
					URL: strPtr("https://github.com/acme/odoo-modules/issues/40")},
				{Number: nil, Title: "Add export cron", URL: nil},
			},
			ErrorMessage: strPtr("GitHub 403 secondary rate limit"),
			CreatedAt:    now.Add(-12 * time.Minute),
		},
		{
			ID: 1, OdooInstanceID: intPtr(2), TicketID: 99, ModelName: "helpdesk.ticket",
			GithubURL: "https://github.com/acme/api", Status: "pending",
			CreatedAt: now.Add(-20 * time.Second),
		},
	}
	n := min(limit, len(items))
	return &TicketIssueRunPage{Items: items[:n], Total: len(items)}, nil
}

func (m *MockClient) TimesheetReviews(limit int) (*TimesheetReviewPage, error) {
	now := time.Now()
	f64Ptr := func(f float64) *float64 { return &f }
	strPtr := func(s string) *string { return &s }
	done := now.Add(-4 * time.Minute)
	sent := now.Add(-3 * time.Minute)
	items := []TimesheetReviewSummary{
		{
			ID: 12, RequestID: "TS-2026-07-05-001", Status: "completed",
			TotalLines: 148, OkCount: 121, RewrittenCount: 23, NeedsHumanCount: 4,
			EstimatedCostUSD: f64Ptr(0.0061), CallbackSentAt: &sent,
			CreatedAt: now.Add(-6 * time.Minute), CompletedAt: &done,
		},
		{
			ID: 11, RequestID: "TS-2026-07-05-000", Status: "failed",
			TotalLines: 42, OkCount: 0, RewrittenCount: 0, NeedsHumanCount: 0,
			ErrorMessage: strPtr("Odoo /hr/timesheet-results 409 (permanent)"),
			CreatedAt:    now.Add(-40 * time.Minute),
		},
		{
			ID: 10, RequestID: "TS-2026-07-04-009", Status: "pending",
			TotalLines: 500, CreatedAt: now.Add(-2 * time.Minute),
		},
	}
	n := min(limit, len(items))
	return &TimesheetReviewPage{Items: items[:n], Total: len(items)}, nil
}

func (m *MockClient) Requeue(id int) error {
	return nil
}

func (m *MockClient) TriggerAudit(repoID int) error {
	return nil
}

func (m *MockClient) AddRepo(owner, name string) error {
	return nil
}

func (m *MockClient) RequeueTicket(id int) error {
	return nil
}

func (m *MockClient) RequeueIssueRun(id int) error {
	return nil
}

func (m *MockClient) Learning() ([]LearningStat, error) {
	return []LearningStat{
		{Repo: "acme/odoo-modules", Category: "style", Findings: 18, Dismissed: 11, ResolvedByFix: 2, StillOpenAtMerge: 1},
		{Repo: "acme/odoo-modules", Category: "security", Findings: 6, Dismissed: 0, ResolvedByFix: 5, StillOpenAtMerge: 0},
		{Repo: "acme/odoo-modules", Category: "bug", Findings: 22, Dismissed: 3, ResolvedByFix: 14, StillOpenAtMerge: 2},
	}, nil
}

func (m *MockClient) Mutes() ([]MuteEntry, error) {
	return []MuteEntry{
		{Repo: "acme/odoo-modules", Category: "style", MutedBy: "alice", CreatedAt: time.Now().Add(-72 * time.Hour)},
	}, nil
}

func (m *MockClient) LearnedMemory() ([]LearnedMemoryEntry, error) {
	cost := 0.012
	return []LearnedMemoryEntry{
		{
			Repo:    "acme/odoo-modules",
			Version: 3,
			Content: "## Learned team preferences (from review feedback)\n\n" +
				"- This team dismisses style comments on generated XML views — do not raise them. (11 signals)\n" +
				"- Raise the bar on docs findings; only flag missing docstrings on public APIs. (4 signals)",
			ItemCount:        2,
			EstimatedCostUSD: &cost,
			CreatedAt:        time.Now().Add(-24 * time.Hour),
		},
	}, nil
}

func (m *MockClient) OdooInstances() (*OdooInstancePage, error) {
	now := time.Now()
	f64Ptr := func(f float64) *float64 { return &f }
	strPtr := func(s string) *string { return &s }
	mk := func(c float64, in, out, n int) TaskCost {
		return TaskCost{CostUSD: c, InputTokens: in, OutputTokens: out, Count: n}
	}
	items := []OdooInstanceSummary{
		{
			ID: 1, Name: "ACME Production", KeyPrefix: "reva_odoo_a1b2",
			CallbackURL: "https://odoo.acme.example/write-field", Active: true,
			OdooVersion: strPtr("19.0"),
			CreatedAt:   now.Add(-30 * 24 * time.Hour), DailyBudgetUSD: f64Ptr(10),
			Cost: OdooInstanceCost{
				Lifetime: WindowCost{Analysis: mk(12.40, 900000, 120000, 320), Issues: mk(8.10, 400000, 90000, 55)},
				Last24h:  WindowCost{Analysis: mk(0.42, 30000, 4000, 11), Issues: mk(0.15, 8000, 1500, 2)},
				Last30d:  WindowCost{Analysis: mk(6.20, 450000, 60000, 160), Issues: mk(3.90, 200000, 45000, 28)},
			},
		},
		{
			ID: 2, Name: "Beta Staging", KeyPrefix: "reva_odoo_c3d4",
			CallbackURL: "", Active: false, CreatedAt: now.Add(-3 * 24 * time.Hour),
			Cost: OdooInstanceCost{},
		},
	}
	return &OdooInstancePage{Items: items, Total: len(items)}, nil
}

func (m *MockClient) CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error) {
	return &OdooInstanceCreated{ID: 99, Name: name, KeyPrefix: "reva_odoo_new9", APIKey: "reva_odoo_DEMOKEYdonotuse"}, nil
}

func (m *MockClient) RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error) {
	return &OdooInstanceCreated{ID: id, Name: "ACME Production", KeyPrefix: "reva_odoo_rot8", APIKey: "reva_odoo_ROTATEDdemo"}, nil
}

func (m *MockClient) SetOdooInstanceActive(id int, active bool) error { return nil }

func (m *MockClient) DeleteOdooInstance(id int) error { return nil }

func (m *MockClient) TicketJourney(odooInstanceID *int, modelName string, ticketID int) (*TicketJourney, error) {
	now := time.Now()
	timePtr := func(t time.Time) *time.Time { return &t }

	// Return a plausible 6-event journey
	return &TicketJourney{
		Ticket: JourneyTicket{
			OdooInstanceID: odooInstanceID,
			ModelName:      modelName,
			TicketID:       ticketID,
			Ready:          true,
		},
		Events: []JourneyEvent{
			{
				TS:      timePtr(now.Add(-4 * time.Hour)),
				Kind:    "analysis_requested",
				Summary: "Ticket analysis requested for estimate",
			},
			{
				TS:      timePtr(now.Add(-3*time.Hour - 45*time.Minute)),
				Kind:    "analysis_completed",
				Summary: "Analysis completed: estimated 12-20 hours",
			},
			{
				TS:      timePtr(now.Add(-3*time.Hour - 30*time.Minute)),
				Kind:    "issues_created",
				Summary: "2 GitHub issues created from analysis",
			},
			{
				TS:      timePtr(now.Add(-2*time.Hour - 15*time.Minute)),
				Kind:    "review_completed",
				Summary: "GitHub review posted on linked PR",
			},
			{
				TS:      timePtr(now.Add(-30 * time.Minute)),
				Kind:    "issue_closed",
				Summary: "Main issue closed; estimate ready for deployment",
			},
			{
				TS:      timePtr(now.Add(-5 * time.Minute)),
				Kind:    "ready",
				Summary: "Ticket marked ready; can proceed to implementation",
			},
		},
	}, nil
}

func (m *MockClient) SupportThreads(limit, offset int) (*SupportThreadPage, error) {
	now := time.Now()
	t1 := now.Add(-6 * time.Minute)
	t2 := now.Add(-90 * time.Minute)
	intPtr := func(i int) *int { return &i }

	items := []SupportThreadSummary{
		{
			ID: 4, OdooInstanceID: intPtr(1), TicketID: 456, ModelName: "helpdesk.ticket",
			FieldName: "x_reva_answer", GithubURL: "https://github.com/acme/widgets",
			Status: "open", CreatedAt: now.Add(-8 * time.Minute), LastTurnAt: &t1,
		},
		{
			ID: 3, OdooInstanceID: intPtr(1), TicketID: 123, ModelName: "project.task",
			FieldName: "x_reva_answer", GithubURL: "https://github.com/acme/odoo-modules",
			Status: "open", CreatedAt: now.Add(-2 * time.Hour), LastTurnAt: &t2,
		},
		{
			// Project-less ticket (no github_url) — docs-only grounding, same as
			// the Tickets tab's "(no repo yet)" bucket.
			ID: 2, OdooInstanceID: intPtr(2), TicketID: 99, ModelName: "helpdesk.ticket",
			FieldName: "x_reva_answer", GithubURL: "",
			Status: "open", CreatedAt: now.Add(-40 * time.Minute), LastTurnAt: nil,
		},
		{
			ID: 1, OdooInstanceID: intPtr(1), TicketID: 777, ModelName: "helpdesk.ticket",
			FieldName: "x_reva_answer", GithubURL: "https://github.com/acme/widgets",
			Status: "open", CreatedAt: now.Add(-26 * time.Hour), LastTurnAt: &t2,
		},
	}
	n := min(limit, len(items))
	return &SupportThreadPage{Items: items[:n], Total: len(items)}, nil
}

// SupportThread returns one thread plus its turns, oldest-first by seq
// (mirroring the real GET /support-threads/{id}). Thread 4 carries several
// turns covering the outcomes an operator needs to distinguish at a glance:
// code-grounded, a failed turn (must stay visible — this is the operator
// view), and a still-pending one. Any other id 404s, matching the real API.
func (m *MockClient) SupportThread(threadID int) (*SupportThreadDetail, error) {
	now := time.Now()
	strPtr := func(s string) *string { return &s }
	f64Ptr := func(f float64) *float64 { return &f }

	page, _ := m.SupportThreads(100, 0)
	var summary *SupportThreadSummary
	for i := range page.Items {
		if page.Items[i].ID == threadID {
			summary = &page.Items[i]
			break
		}
	}
	if summary == nil {
		return nil, fmt.Errorf("support thread %d not found", threadID)
	}

	var turns []SupportTurnDetail
	switch threadID {
	case 4:
		turns = []SupportTurnDetail{
			{
				ID: 501, ThreadID: 4, Seq: 1, JobID: strPtr("job-501"),
				Question:         "Why is the delivery date not updating from the linked sale order?",
				AnswerHTML:       strPtr("<p>The delivery date is recomputed from...</p>"),
				RequestKind:      strPtr("answer"),
				AnswerStatus:     strPtr("answered"),
				GroundingLevel:   strPtr("code"),
				Status:           "completed",
				EstimatedCostUSD: f64Ptr(0.041),
				CreatedAt:        now.Add(-9 * time.Minute),
				CompletedAt:      strPtrTime(now.Add(-8 * time.Minute)),
				CallbackSentAt:   strPtrTime(now.Add(-8 * time.Minute)),
			},
			{
				ID: 506, ThreadID: 4, Seq: 2, JobID: strPtr("job-506"),
				Question:     "Follow-up: does the same issue affect returns?",
				RequestKind:  strPtr("answer"),
				Status:       "failed",
				ErrorMessage: strPtr("Claude API returned 529 (overloaded). Retry limit exceeded."),
				CreatedAt:    now.Add(-7 * time.Minute),
				CompletedAt:  strPtrTime(now.Add(-6*time.Minute - 30*time.Second)),
			},
			{
				ID: 507, ThreadID: 4, Seq: 3, JobID: strPtr("job-507"),
				Question:  "One more: what about partial returns?",
				Status:    "pending",
				CreatedAt: now.Add(-30 * time.Second),
			},
		}
	case 3:
		turns = []SupportTurnDetail{
			{
				ID: 502, ThreadID: 3, Seq: 1, JobID: strPtr("job-502"),
				Question:         "Can we expose the margin field on the portal view?",
				AnswerHTML:       strPtr("<p>Partially — the field exists but...</p>"),
				RequestKind:      strPtr("answer"),
				AnswerStatus:     strPtr("partial"),
				GroundingLevel:   strPtr("docs"),
				Status:           "completed",
				EstimatedCostUSD: f64Ptr(0.018),
				CreatedAt:        now.Add(-95 * time.Minute),
				CompletedAt:      strPtrTime(now.Add(-92 * time.Minute)),
			},
		}
	case 2:
		turns = []SupportTurnDetail{
			{
				ID: 503, ThreadID: 2, Seq: 1, JobID: strPtr("job-503"),
				Question:         "What plan does ticket #99 belong to?",
				RequestKind:      strPtr("answer"),
				AnswerStatus:     strPtr("cannot_answer"),
				GroundingLevel:   strPtr("none"),
				Status:           "completed",
				EstimatedCostUSD: f64Ptr(0.006),
				CreatedAt:        now.Add(-42 * time.Minute),
				CompletedAt:      strPtrTime(now.Add(-41 * time.Minute)),
			},
		}
	case 1:
		turns = []SupportTurnDetail{
			{
				ID: 505, ThreadID: 1, Seq: 1, JobID: strPtr("job-505"),
				Question:         "Why did the callback to Odoo fail?",
				AnswerHTML:       strPtr("<p>The stock move is voided when...</p>"),
				RequestKind:      strPtr("answer"),
				AnswerStatus:     strPtr("answered"),
				GroundingLevel:   strPtr("code"),
				Status:           "completed",
				EstimatedCostUSD: f64Ptr(0.037),
				CreatedAt:        now.Add(-27 * time.Hour),
				CompletedAt:      strPtrTime(now.Add(-26*time.Hour - 55*time.Minute)),
				CallbackError:    strPtr("Odoo write_field timed out"),
			},
		}
	}

	return &SupportThreadDetail{SupportThreadSummary: *summary, Turns: turns}, nil
}

func strPtrTime(t time.Time) *time.Time { return &t }

func (m *MockClient) RequeueSupportTurn(turnID int) error {
	return nil
}

func (m *MockClient) Personas() (*PersonaPage, error) {
	strPtr := func(s string) *string { return &s }
	items := []Persona{
		{
			ID: 1, Scope: "default",
			Language: strPtr("auto"), Formality: strPtr("formal"),
			TechnicalDepth: strPtr("medium"), Length: strPtr("standard"),
			SignOff: strPtr("— REVA"), Active: true,
		},
		{
			ID: 2, Scope: "repo", RepoFullName: strPtr("acme/widgets"),
			Formality: strPtr("informal"), TechnicalDepth: strPtr("high"),
			StyleNotes:    strPtr("This customer's devs prefer terse, code-referenced answers"),
			ContentPolicy: strPtr("never quote a delivery date"),
			Active:        true,
		},
		{
			ID: 3, Scope: "repo", RepoFullName: strPtr("acme/legacy-erp"),
			Length: strPtr("brief"), Active: false,
		},
	}
	return &PersonaPage{Items: items, Total: len(items)}, nil
}

func (m *MockClient) ResolvedPersona(repoFullName string) (*ResolvedPersona, error) {
	strPtr := func(s string) *string { return &s }
	var repo *string
	if repoFullName != "" {
		repo = strPtr(repoFullName)
	}
	formality, depth, length := "formal", "medium", "standard"
	var styleNotes, contentPolicy *string
	if repoFullName == "acme/widgets" {
		formality = "informal"
		depth = "high"
		styleNotes = strPtr("This customer's devs prefer terse, code-referenced answers")
		contentPolicy = strPtr("never quote a delivery date")
	}
	rendered := fmt.Sprintf(
		"Write in %s language, %s tone, %s technical depth, %s length. Sign off with \"— REVA\".",
		"auto", formality, depth, length)
	if styleNotes != nil {
		rendered += " " + *styleNotes + "."
	}
	if contentPolicy != nil {
		rendered += " Constraint: " + *contentPolicy + "."
	}
	return &ResolvedPersona{
		RepoFullName: repo, Language: strPtr("auto"),
		Formality: strPtr(formality), TechnicalDepth: strPtr(depth), Length: strPtr(length),
		SignOff: strPtr("— REVA"), StyleNotes: styleNotes, ContentPolicy: contentPolicy,
		RenderedBlock: rendered, ResolvedAt: time.Now(),
	}, nil
}

func (m *MockClient) CreatePersona(body PersonaBody) (*Persona, error) {
	return &Persona{
		ID: 99, Scope: body.Scope, RepoFullName: body.RepoFullName,
		Language: body.Language, Formality: body.Formality, TechnicalDepth: body.TechnicalDepth,
		Length: body.Length, Salutation: body.Salutation, SignOff: body.SignOff,
		StyleNotes: body.StyleNotes, ContentPolicy: body.ContentPolicy, Active: body.Active,
	}, nil
}

func (m *MockClient) UpdatePersona(id int, body PersonaBody) (*Persona, error) {
	return &Persona{
		ID: id, Scope: body.Scope, RepoFullName: body.RepoFullName,
		Language: body.Language, Formality: body.Formality, TechnicalDepth: body.TechnicalDepth,
		Length: body.Length, Salutation: body.Salutation, SignOff: body.SignOff,
		StyleNotes: body.StyleNotes, ContentPolicy: body.ContentPolicy, Active: body.Active,
	}, nil
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
