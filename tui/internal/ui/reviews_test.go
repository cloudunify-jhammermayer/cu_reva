package ui

import (
	"strings"
	"testing"
	"time"

	"reva-tui/internal/api"
)

// TestReviewsListShowsCarriedFromLabel is the regression guard for the
// carry-forward review outcome (spec 2026-07-24): a review whose CarriedFrom
// is set must render a "carried from #N" marker in the list row so reuse is
// visible in the TUI, not just the DB/logs (CLAUDE.md principle #5).
func TestReviewsListShowsCarriedFromLabel(t *testing.T) {
	r := newReviews(&api.MockClient{})
	r.width, r.height = 200, 30
	page := &api.ReviewPage{
		Items: []api.ReviewSummary{
			{
				ID: 1, RepoFullName: "acme/widgets", PRNumber: 102,
				PRTitle: "promote to prod", Status: "completed",
				ReviewMode: "diff", CreatedAt: time.Now(),
				CarriedFrom: &api.CarriedFrom{RunID: 5, PR: 101},
			},
		},
		Total: 1,
	}
	r, _ = r.update(reviewsLoadedMsg{data: page})

	out := r.view(200, 30)
	if !strings.Contains(out, "carried from #101") {
		t.Fatalf("view missing carried-from marker:\n%s", out)
	}
}
