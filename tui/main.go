package main

import (
	"flag"
	"fmt"
	"net/url"
	"os"

	tea "github.com/charmbracelet/bubbletea"
	"reva-tui/internal/api"
	"reva-tui/internal/ui"
)

// checkAPIURLSecurity returns true and prints a warning to stderr when baseURL
// is neither HTTPS nor a loopback address. The API key would be sent in
// plaintext in that case. Returns false when the URL is safe.
func checkAPIURLSecurity(baseURL string) bool {
	u, err := url.Parse(baseURL)
	if err == nil && u.Scheme == "https" {
		return false
	}
	if err == nil {
		h := u.Hostname()
		if h == "localhost" || h == "127.0.0.1" || h == "::1" {
			return false
		}
	}
	fmt.Fprintf(os.Stderr,
		"warning: REVA_API_URL %q is not HTTPS — credentials will be sent in plaintext\n",
		baseURL,
	)
	return true
}

func main() {
	demo := flag.Bool("demo", false, "run with mock data (no server needed)")
	flag.Parse()

	var client api.ClientIface
	if *demo {
		client = &api.MockClient{}
	} else {
		baseURL := os.Getenv("REVA_API_URL")
		if baseURL == "" {
			baseURL = "http://localhost:8080/api/v1"
		}
		apiKey := os.Getenv("REVA_API_KEY")
		if apiKey != "" {
			checkAPIURLSecurity(baseURL)
		}
		client = api.NewClient(baseURL, apiKey)
	}

	odooURL := os.Getenv("REVA_ODOO_URL")
	if odooURL == "" {
		odooURL = "http://localhost:8069"
	}
	app := ui.NewApp(client, odooURL)
	p := tea.NewProgram(app, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
