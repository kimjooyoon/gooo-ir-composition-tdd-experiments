package bodycodegen
import ("encoding/json"; "os"; "testing")
func TestIndependentFiniteReplay(t *testing.T) {
 cases := []struct { split string; input, expected int64 }{
{split: "training", input: int64(-2), expected: int64(-4)},
{split: "training", input: int64(-1), expected: int64(-2)},
{split: "training", input: int64(0), expected: int64(0)},
{split: "training", input: int64(1), expected: int64(2)},
{split: "training", input: int64(2), expected: int64(4)},
{split: "holdout", input: int64(9223372036854775807), expected: int64(9223372036854775807)},
{split: "holdout", input: int64(4611686018427387903), expected: int64(9223372036854775806)},
{split: "holdout", input: int64(4611686018427387904), expected: int64(9223372036854775807)},
{split: "holdout", input: int64(-9223372036854775808), expected: int64(-9223372036854775808)},
{split: "holdout", input: int64(-4611686018427387904), expected: int64(-9223372036854775808)},
{split: "holdout", input: int64(-4611686018427387905), expected: int64(-9223372036854775808)},
 }
 results := make([]struct { Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }, 0, len(cases))
 for _, item := range cases { results = append(results, struct { Split string `json:"split"`; Input int64 `json:"input"`; Expected int64 `json:"expected"`; Actual int64 `json:"actual"` }{item.split,item.input,item.expected,C31(item.input)}) }
 raw, err := json.Marshal(results); if err != nil { t.Fatal(err) }
 if err := os.WriteFile("independent-results.json", raw, 0o644); err != nil { t.Fatal(err) }
}
