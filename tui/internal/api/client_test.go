package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
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
