package candidateeval

import (
    "encoding/json"
    "os"
    "testing"
)

func TestCompiledCandidateFiniteCases(t *testing.T) {
    cases := []struct { split string; input, expected int64 }{
		{split: "training", input: -2, expected: -4},
		{split: "training", input: -1, expected: -2},
		{split: "training", input: 0, expected: 0},
		{split: "training", input: 1, expected: 2},
		{split: "training", input: 2, expected: 4},
		{split: "evaluation", input: 9223372036854775807, expected: 9223372036854775807},
		{split: "evaluation", input: 4611686018427387903, expected: 9223372036854775806},
		{split: "evaluation", input: 4611686018427387904, expected: 9223372036854775807},
		{split: "evaluation", input: -9223372036854775808, expected: -9223372036854775808},
		{split: "evaluation", input: -4611686018427387904, expected: -9223372036854775808},
		{split: "evaluation", input: -4611686018427387905, expected: -9223372036854775808},
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
        }{Split:item.split, Input:item.input, Expected:item.expected, Actual:candidate31c(item.input)})
    }
    encoded, err := json.Marshal(results)
    if err != nil { t.Fatal(err) }
    if err := os.WriteFile("candidate-results.json", encoded, 0o644); err != nil { t.Fatal(err) }
}
