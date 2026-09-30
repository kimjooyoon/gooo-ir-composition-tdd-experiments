package bodycodegen
import ("encoding/json"; "os"; "testing")
func TestIndependentFiniteReplay(t *testing.T) {
 cases := []struct { split string; input, expected int64 }{
{split: "training", input: int64(-1), expected: int64(1)},
{split: "training", input: int64(0), expected: int64(1)},
{split: "training", input: int64(1), expected: int64(0)},
{split: "holdout", input: int64(-9), expected: int64(1)},
{split: "holdout", input: int64(5), expected: int64(0)},
{split: "holdout", input: int64(10), expected: int64(0)},
 }
 results := make([]struct { Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }, 0, len(cases))
 for _, item := range cases { results = append(results, struct { Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }{item.split,item.input,item.expected,C17(item.input)}) }
 raw, err := json.Marshal(results); if err != nil { t.Fatal(err) }
 if err := os.WriteFile("independent-results.json", raw, 0o644); err != nil { t.Fatal(err) }
}
