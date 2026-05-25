package main

import (
	"testing"
)

func TestCheckAPIURLSecurity_safe(t *testing.T) {
	safe := []string{
		"https://reviews.example.com/api/v1",
		"http://localhost:8080/api/v1",
		"http://127.0.0.1:8080/api/v1",
		"http://localhost/api/v1",
	}
	for _, url := range safe {
		if checkAPIURLSecurity(url) {
			t.Errorf("expected %q to be safe, but got warned", url)
		}
	}
}

func TestCheckAPIURLSecurity_unsafe(t *testing.T) {
	unsafe := []string{
		"http://reviews.example.com/api/v1",
		"http://10.0.0.5:8080/api/v1",
		"http://192.168.1.100/api/v1",
	}
	for _, url := range unsafe {
		if !checkAPIURLSecurity(url) {
			t.Errorf("expected %q to be flagged as unsafe, but it was not", url)
		}
	}
}
