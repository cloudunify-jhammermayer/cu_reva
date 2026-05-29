package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// Client is a typed HTTP client for the REVA internal API.
type Client struct {
	base   string
	apiKey string
	http   *http.Client
}

func NewClient(baseURL, apiKey string) *Client {
	return &Client{
		base:   baseURL,
		apiKey: apiKey,
		http:   &http.Client{Timeout: 10 * time.Second},
	}
}

// authHeader sets the Bearer token on a request when an API key is configured.
func (c *Client) authHeader(req *http.Request) {
	if c.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
	}
}

func (c *Client) get(path string, out any) error {
	req, err := http.NewRequest(http.MethodGet, c.base+path, nil)
	if err != nil {
		return err
	}
	c.authHeader(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d from %s", resp.StatusCode, path)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// post issues a POST with the auth header and asserts a 202 response.
func (c *Client) post(path string) error {
	req, err := http.NewRequest(http.MethodPost, c.base+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	c.authHeader(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}

func (c *Client) Dashboard() (*DashboardMetrics, error) {
	var d DashboardMetrics
	return &d, c.get("/metrics/dashboard", &d)
}

func (c *Client) Reviews(limit int, repo, status, author string) (*ReviewPage, error) {
	path := fmt.Sprintf("/reviews?limit=%d", limit)
	if repo != "" {
		path += "&repo=" + url.QueryEscape(repo)
	}
	if status != "" {
		path += "&status=" + url.QueryEscape(status)
	}
	if author != "" {
		path += "&author=" + url.QueryEscape(author)
	}
	var p ReviewPage
	return &p, c.get(path, &p)
}

func (c *Client) ReviewDetail(id int) (*ReviewDetail, error) {
	var d ReviewDetail
	return &d, c.get(fmt.Sprintf("/reviews/%d", id), &d)
}

func (c *Client) Failures(limit int) (*FailurePage, error) {
	var p FailurePage
	return &p, c.get(fmt.Sprintf("/failures?limit=%d", limit), &p)
}

func (c *Client) Pending() (*PendingPage, error) {
	var p PendingPage
	return &p, c.get("/pending", &p)
}

func (c *Client) Findings(severity, category string, limit int) (*FindingPage, error) {
	path := fmt.Sprintf("/findings?limit=%d", limit)
	if severity != "" {
		path += "&severity=" + url.QueryEscape(severity)
	}
	if category != "" {
		path += "&category=" + url.QueryEscape(category)
	}
	var p FindingPage
	return &p, c.get(path, &p)
}

func (c *Client) Repos() (*RepoPage, error) {
	var p RepoPage
	return &p, c.get("/repos", &p)
}

func (c *Client) TicketAnalyses(limit int) (*TicketAnalysisPage, error) {
	var p TicketAnalysisPage
	return &p, c.get(fmt.Sprintf("/ticket-analyses?limit=%d", limit), &p)
}

func (c *Client) Requeue(id int) error {
	return c.post(fmt.Sprintf("/reviews/%d/requeue", id))
}

func (c *Client) RequeueTicket(id int) error {
	return c.post(fmt.Sprintf("/ticket-analysis/%d/requeue", id))
}
