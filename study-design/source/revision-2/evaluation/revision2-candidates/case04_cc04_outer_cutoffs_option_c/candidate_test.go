package candidateeval

import (
    "encoding/json"
    "os"
    "testing"
)

func TestCompiledCandidateFiniteCases(t *testing.T) {
    cases := []struct { split string; input, expected int64 }{
		{split: "training", input: -7, expected: 9},
		{split: "training", input: -6, expected: 9},
		{split: "training", input: -5, expected: 0},
		{split: "training", input: 7, expected: 0},
		{split: "training", input: 8, expected: 9},
		{split: "training", input: 9, expected: 9},
		{split: "evaluation", input: -100, expected: 9},
		{split: "evaluation", input: 0, expected: 0},
		{split: "evaluation", input: 100, expected: 9},
    }
    results := make([]struct {
        Split string `json:"split"`
        Input int64 `json:"input"`
        Expected int64 `json:"expected"`
        Actual int64 `json:"actual"`
    }, 0, len(cases))
    for _, item := range cases {
        results = append(results, struct {
            Split string `json:"split"`
            Input int64 `json:"input"`
            Expected int64 `json:"expected"`
            Actual int64 `json:"actual"`
        }{Split:item.split, Input:item.input, Expected:item.expected, Actual:candidate04c(item.input)})
    }
    encoded, err := json.Marshal(results)
    if err != nil { t.Fatal(err) }
    if err := os.WriteFile("candidate-results.json", encoded, 0o644); err != nil { t.Fatal(err) }
}
