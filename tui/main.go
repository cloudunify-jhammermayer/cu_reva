package main

import (
	"flag"
	"fmt"
	"os"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"reva-tui/internal/api"
	"reva-tui/internal/ui"
)

// checkAPIURLSecurity returns true and prints a warning to stderr when baseURL
// is neither HTTPS nor a loopback address. The API key would be sent in
// plaintext in that case. Returns false when the URL is safe.
func checkAPIURLSecurity(baseURL string) bool {
	if strings.HasPrefix(baseURL, "https://") {
		return false
	}
	if strings.Contains(baseURL, "localhost") || strings.Contains(baseURL, "127.0.0.1") {
		return false
	}
	fmt.Fprintf(os.Stderr,
		"warning: REVA_API_URL %q is not HTTPS — REVA_API_KEY will be sent in plaintext\n",
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
		checkAPIURLSecurity(baseURL)
		client = api.NewClient(baseURL)
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
