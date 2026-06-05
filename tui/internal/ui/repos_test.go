package ui

import "testing"

func TestParseOwnerName(t *testing.T) {
	cases := []struct {
		in          string
		owner, name string
		ok          bool
	}{
		{"acme/widgets", "acme", "widgets", true},
		{"  acme/widgets  ", "acme", "widgets", true},
		{"https://github.com/acme/widgets", "acme", "widgets", true},
		{"/acme/widgets/", "acme", "widgets", true},
		{"widgets", "", "", false},
		{"acme/", "", "", false},
		{"/widgets", "", "", false},
		{"a/b/c", "", "", false},
		{"", "", "", false},
	}
	for _, c := range cases {
		o, n, ok := parseOwnerName(c.in)
		if ok != c.ok || o != c.owner || n != c.name {
			t.Errorf("parseOwnerName(%q) = (%q,%q,%v), want (%q,%q,%v)",
				c.in, o, n, ok, c.owner, c.name, c.ok)
		}
	}
}
