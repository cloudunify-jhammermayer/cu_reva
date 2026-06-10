package api

import (
	"bytes"
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
	// Optional Cloudflare Access service-token credentials. When set, they are
	// sent on every request so the TUI can reach an Access-protected origin
	// directly (no `cloudflared access` proxy needed).
	cfAccessID     string
	cfAccessSecret string
	http           *http.Client
}

func NewClient(baseURL, apiKey, cfAccessID, cfAccessSecret string) *Client {
	return &Client{
		base:           baseURL,
		apiKey:         apiKey,
		cfAccessID:     cfAccessID,
		cfAccessSecret: cfAccessSecret,
		http:           &http.Client{Timeout: 10 * time.Second},
	}
}

// authHeader sets the Bearer token (REVA's own auth) and, when configured, the
// Cloudflare Access service-token headers, on a request.
func (c *Client) authHeader(req *http.Request) {
	if c.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
	}
	if c.cfAccessID != "" && c.cfAccessSecret != "" {
		req.Header.Set("CF-Access-Client-Id", c.cfAccessID)
		req.Header.Set("CF-Access-Client-Secret", c.cfAccessSecret)
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

func (c *Client) Audits(limit int) (*AuditRunPage, error) {
	var p AuditRunPage
	return &p, c.get(fmt.Sprintf("/audits?limit=%d", limit), &p)
}

func (c *Client) AuditFindings(auditRunID, limit int) (*AuditFindingPage, error) {
	var p AuditFindingPage
	return &p, c.get(fmt.Sprintf("/audit-findings?audit_run_id=%d&limit=%d", auditRunID, limit), &p)
}

func (c *Client) Repos() (*RepoPage, error) {
	var p RepoPage
	return &p, c.get("/repos", &p)
}

func (c *Client) TicketAnalyses(limit int) (*TicketAnalysisPage, error) {
	var p TicketAnalysisPage
	return &p, c.get(fmt.Sprintf("/ticket-analyses?limit=%d", limit), &p)
}

func (c *Client) TicketIssueRuns(limit int) (*TicketIssueRunPage, error) {
	var p TicketIssueRunPage
	return &p, c.get(fmt.Sprintf("/ticket-issue-runs?limit=%d", limit), &p)
}

func (c *Client) TriggerAudit(repoID int) error {
	return c.post(fmt.Sprintf("/repos/%d/audit", repoID))
}

func (c *Client) AddRepo(owner, name string) error {
	body, _ := json.Marshal(map[string]string{"owner": owner, "name": name})
	req, err := http.NewRequest(http.MethodPost, c.base+"/repos", bytes.NewReader(body))
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
	if resp.StatusCode != http.StatusCreated {
		// Surface the API's `detail` (e.g. "app is not installed on ...").
		var e struct {
			Detail string `json:"detail"`
		}
		if json.NewDecoder(resp.Body).Decode(&e) == nil && e.Detail != "" {
			return fmt.Errorf("%s", e.Detail)
		}
		return fmt.Errorf("HTTP %d adding %s/%s", resp.StatusCode, owner, name)
	}
	return nil
}

func (c *Client) Requeue(id int) error {
	return c.post(fmt.Sprintf("/reviews/%d/requeue", id))
}

func (c *Client) RequeueTicket(id int) error {
	return c.post(fmt.Sprintf("/ticket-analysis/%d/requeue", id))
}
