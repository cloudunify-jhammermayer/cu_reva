package ui

import (
	"errors"
	"strings"
	"testing"

	"reva-tui/internal/api"
)

func TestReleasesLoadAndView(t *testing.T) {
	r := newReleases(&api.MockClient{})
	r.width, r.height = 120, 30
	data, _ := (&api.MockClient{}).ReleaseNotes(100)
	r, _ = r.update(releasesLoadedMsg{data: data})

	if len(r.items) != 3 {
		t.Fatalf("expected 3 release rows, got %d", len(r.items))
	}
	out := r.view(120, 30)
	for _, want := range []string{"Release Logs", "Lollipop", "docs/releases/lollipop.html", "Marshmallow", "failed"} {
		if !strings.Contains(out, want) {
			t.Fatalf("view missing %q:\n%s", want, out)
		}
	}
}

func TestReleasesErrorView(t *testing.T) {
	r := newReleases(&api.MockClient{})
	r, _ = r.update(releasesLoadedMsg{err: errors.New("boom")})

	out := r.view(100, 20)
	if !strings.Contains(out, "Error: boom") {
		t.Fatalf("error view missing message:\n%s", out)
	}
}

func TestReleasesTabKey(t *testing.T) {
	if tabKeys["w"] != viewReleases {
		t.Fatalf("expected w to open the Releases tab")
	}
}
