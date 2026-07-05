package ui

import (
	"strings"
	"testing"
	"time"

	"reva-tui/internal/api"
)

func TestFailuresOpsEventsView(t *testing.T) {
	f := newFailures(&api.MockClient{})
	detail := map[string]any{"repo": "acme/widgets"}
	page := &api.OpsEventPage{
		Items: []api.OpsEventEntry{
			{
				ID: 2, Component: "codegraph", Severity: "warning",
				Event: "index_failed", Detail: detail,
				CreatedAt: time.Now().Add(-5 * time.Minute),
			},
			{
				ID: 1, Component: "odoo_callback", Severity: "error",
				Event:     "write_field_failed",
				CreatedAt: time.Now().Add(-2 * time.Hour),
			},
		},
		Total: 2,
	}
	f, _ = f.update(opsEventsLoadedMsg{data: page})
	f.width, f.height = 120, 30

	if strings.Contains(f.view(120, 30), "codegraph") {
		t.Fatal("runs view must not render ops events")
	}
	f.showEvents = true
	out := f.view(120, 30)
	if !strings.Contains(out, "codegraph") || !strings.Contains(out, "index_failed") {
		t.Fatalf("events view missing rows:\n%s", out)
	}
	if !strings.Contains(out, "Component Events") {
		t.Fatalf("events view missing header:\n%s", out)
	}
}
