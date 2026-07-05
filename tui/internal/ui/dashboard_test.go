package ui

import (
	"strings"
	"testing"

	"reva-tui/internal/api"
)

func TestDashboardShowsReadyTicketsWhenPresent(t *testing.T) {
	d := Dashboard{data: &api.DashboardMetrics{
		Last24h:      api.PeriodStats{},
		Last7d:       api.PeriodStats{},
		TicketsReady: 2,
	}}

	out := d.view(100, 30, 0)

	if !strings.Contains(out, "Ready") || !strings.Contains(out, "2 tickets") {
		t.Fatalf("dashboard missing ready tickets line:\n%s", out)
	}
}
