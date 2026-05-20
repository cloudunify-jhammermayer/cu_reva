package main

import (
	"flag"
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"
	"reva-tui/internal/api"
	"reva-tui/internal/ui"
)

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
		client = api.NewClient(baseURL)
	}

	app := ui.NewApp(client)
	p := tea.NewProgram(app, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
