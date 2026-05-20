package ui

import "reva-tui/internal/api"

type dashboardLoadedMsg struct{ data *api.DashboardMetrics; err error }
type reviewsLoadedMsg struct{ data *api.ReviewPage; err error }
type reviewDetailLoadedMsg struct{ data *api.ReviewDetail; err error }
type failuresLoadedMsg struct{ data *api.FailurePage; err error }
type requeuedMsg struct{ id int; err error }
type tickMsg struct{}
