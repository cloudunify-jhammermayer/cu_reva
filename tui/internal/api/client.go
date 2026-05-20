package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// Client is a typed HTTP client for the REVA internal API.
type Client struct {
	base string
	http *http.Client
}

func NewClient(baseURL string) *Client {
	return &Client{
		base: baseURL,
		http: &http.Client{Timeout: 10 * time.Second},
	}
}

func (c *Client) get(path string, out any) error {
	resp, err := c.http.Get(c.base + path)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d from %s", resp.StatusCode, path)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) Dashboard() (*DashboardMetrics, error) {
	var d DashboardMetrics
	return &d, c.get("/metrics/dashboard", &d)
}

func (c *Client) Reviews(limit int) (*ReviewPage, error) {
	var p ReviewPage
	return &p, c.get(fmt.Sprintf("/reviews?limit=%d", limit), &p)
}

func (c *Client) ReviewDetail(id int) (*ReviewDetail, error) {
	var d ReviewDetail
	return &d, c.get(fmt.Sprintf("/reviews/%d", id), &d)
}

func (c *Client) Failures(limit int) (*FailurePage, error) {
	var p FailurePage
	return &p, c.get(fmt.Sprintf("/failures?limit=%d", limit), &p)
}

func (c *Client) Requeue(id int) error {
	resp, err := c.http.Post(
		fmt.Sprintf("%s/reviews/%d/requeue", c.base, id),
		"application/json", nil,
	)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 202 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}
