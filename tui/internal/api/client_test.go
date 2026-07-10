package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestAuthHeaderSentWhenKeySet(t *testing.T) {
	var got string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{"items":[],"total":0}`))
	}))
	defer srv.Close()

	if _, err := NewClient(srv.URL, "tok123", "", "").Repos(); err != nil {
		t.Fatal(err)
	}
	if got != "Bearer tok123" {
		t.Errorf("Authorization = %q, want %q", got, "Bearer tok123")
	}
}

func TestNoAuthHeaderWhenKeyEmpty(t *testing.T) {
	var got string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{"items":[],"total":0}`))
	}))
	defer srv.Close()

	_, _ = NewClient(srv.URL, "", "", "").Repos()
	if got != "" {
		t.Errorf("unexpected Authorization header %q", got)
	}
}

func TestCFAccessHeadersSentWhenConfigured(t *testing.T) {
	var id, secret string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id = r.Header.Get("CF-Access-Client-Id")
		secret = r.Header.Get("CF-Access-Client-Secret")
		_, _ = w.Write([]byte(`{"items":[],"total":0}`))
	}))
	defer srv.Close()

	if _, err := NewClient(srv.URL, "tok", "cid.access", "csecret").Repos(); err != nil {
		t.Fatal(err)
	}
	if id != "cid.access" || secret != "csecret" {
		t.Errorf("CF-Access headers = (%q, %q), want (cid.access, csecret)", id, secret)
	}
}

func TestCFAccessHeadersAbsentWhenUnset(t *testing.T) {
	var id, secret string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id = r.Header.Get("CF-Access-Client-Id")
		secret = r.Header.Get("CF-Access-Client-Secret")
		_, _ = w.Write([]byte(`{"items":[],"total":0}`))
	}))
	defer srv.Close()

	_, _ = NewClient(srv.URL, "tok", "", "").Repos()
	if id != "" || secret != "" {
		t.Errorf("unexpected CF-Access headers (%q, %q)", id, secret)
	}
}

func TestGetErrorsOnNon200(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	if _, err := NewClient(srv.URL, "", "", "").Repos(); err == nil {
		t.Error("expected an error on HTTP 500")
	}
}

func TestAddRepoSurfacesAPIDetail(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"the REVA GitHub App is not installed on acme/x"}`))
	}))
	defer srv.Close()

	err := NewClient(srv.URL, "", "", "").AddRepo("acme", "x")
	if err == nil || !strings.Contains(err.Error(), "not installed") {
		t.Errorf("want error containing the API detail, got %v", err)
	}
}

func TestAddRepoSuccess(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/repos" {
			t.Errorf("got %s %s, want POST /repos", r.Method, r.URL.Path)
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"repository_id":1}`))
	}))
	defer srv.Close()

	if err := NewClient(srv.URL, "", "", "").AddRepo("acme", "x"); err != nil {
		t.Errorf("unexpected error: %v", err)
	}
}

func TestJourneyEventTimestampUnmarshal(t *testing.T) {
	tests := []struct {
		name    string
		json    string
		wantErr bool
		checkFn func(*JourneyEvent) bool
	}{
		{
			name: "RFC3339 with offset",
			json: `{"ts":"2026-07-10T12:00:00+00:00","kind":"test","summary":"test event"}`,
			checkFn: func(e *JourneyEvent) bool {
				return e.TS != nil && e.TS.Hour() == 12 && e.Kind == "test"
			},
		},
		{
			name: "naive timestamp",
			json: `{"ts":"2026-07-10T12:00:00","kind":"test","summary":"test event"}`,
			checkFn: func(e *JourneyEvent) bool {
				return e.TS != nil && e.TS.Hour() == 12 && e.Kind == "test"
			},
		},
		{
			name: "null timestamp",
			json: `{"ts":null,"kind":"test","summary":"test event"}`,
			checkFn: func(e *JourneyEvent) bool {
				return e.TS == nil && e.Kind == "test"
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var e JourneyEvent
			err := json.Unmarshal([]byte(tt.json), &e)
			if (err != nil) != tt.wantErr {
				t.Errorf("error = %v, wantErr %v", err, tt.wantErr)
			}
			if !tt.wantErr && !tt.checkFn(&e) {
				t.Errorf("check failed for %v", e)
			}
		})
	}
}

func TestTicketJourneyResponse(t *testing.T) {
	now := time.Now()
	naiveTS := now.Format("2006-01-02T15:04:05")
	offsetTS := now.Format(time.RFC3339)

	// Mix of naive and offset-aware timestamps in one response
	journeyJSON := `{
		"ticket": {
			"odoo_instance_id": 1,
			"model_name": "helpdesk.ticket",
			"ticket_id": 456,
			"ready": true
		},
		"events": [
			{"ts":"` + naiveTS + `","kind":"analysis_requested","summary":"Analysis started"},
			{"ts":"` + offsetTS + `","kind":"analysis_completed","summary":"Analysis finished"},
			{"ts":null,"kind":"system_event","summary":"No timestamp event"}
		]
	}`

	var tj TicketJourney
	if err := json.Unmarshal([]byte(journeyJSON), &tj); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	if tj.Ticket.TicketID != 456 || tj.Ticket.Ready != true {
		t.Errorf("ticket mismatch: got %+v", tj.Ticket)
	}
	if len(tj.Events) != 3 {
		t.Errorf("event count = %d, want 3", len(tj.Events))
	}
	if tj.Events[0].TS == nil || tj.Events[1].TS == nil {
		t.Error("expected non-nil timestamps on events 0 and 1")
	}
	if tj.Events[2].TS != nil {
		t.Error("expected nil timestamp on event 2")
	}
}
