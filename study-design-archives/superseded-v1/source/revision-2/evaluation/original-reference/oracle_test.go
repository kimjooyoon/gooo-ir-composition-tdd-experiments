package oracle

import (
    "encoding/json"
    "os"
    "testing"
)

type caseVector struct {
    ID string `json:"id"`
    Training []struct { Input int64 `json:"input"`; Expected int64 `json:"expected"` } `json:"training"`
    Evaluation []struct { Input int64 `json:"input"`; Expected int64 `json:"expected"` } `json:"evaluation"`
}

func TestIndependentGoReferenceOraclesMatchPythonInt64Vectors(t *testing.T) {
    data, err := os.ReadFile("testdata/vectors.json")
    if err != nil { t.Fatal(err) }
    var vectors []caseVector
    if err := json.Unmarshal(data, &vectors); err != nil { t.Fatal(err) }
    refs := map[string]func(int64) int64{
        "cc01_inclusive_band": ref01,
        "cc02_nonzero_disjunction": ref02,
        "cc03_two_islands": ref03,
        "cc04_outer_cutoffs": ref04,
        "ni05_negative_then_double": ref05,
        "ni06_surcharge_discount": ref06,
        "ni07_zero_special_negative_offset": ref07,
        "ni08_upper_grade_excess": ref08,
        "lr09_compound_total": ref09,
        "lr10_floor_then_increment": ref10,
        "lr11_nonnegative_doubled_score": ref11,
        "lr12_cap_after_adjustment": ref12,
        "pr13_subtract_tripled_sum": ref13,
        "pr14_product_neighbor_factors": ref14,
        "pr15_square_minus_successor_sum": ref15,
        "pr16_composed_neighbor_product": ref16,
        "cp17_nonpositive_inclusive": ref17,
        "cp18_exact_release_code": ref18,
        "cp19_range_without_origin": ref19,
        "cp20_strict_symmetric_window": ref20,
        "bl21_saved_range_predicates": ref21,
        "bl22_reassigned_valid_flag": ref22,
        "bl23_nonzero_bounded_flag": ref23,
        "bl24_selected_nonnegative_codes": ref24,
        "tx25_sign_label": ref25,
        "tx26_state_label": ref26,
        "tx27_lexical_cutoff": ref27,
        "tx28_reassigned_text_state": ref28,
        "ob29_saturating_increment": ref29,
        "ob30_saturating_decrement": ref30,
        "ob31_saturating_double": ref31,
        "ob32_clamped_absolute": ref32,
    }
    if len(vectors) != 32 || len(refs) != len(vectors) { t.Fatalf("vectors=%d references=%d, want 32", len(vectors), len(refs)) }
    for _, vector := range vectors {
        reference, ok := refs[vector.ID]
        if !ok { t.Fatalf("missing reference oracle for %s", vector.ID) }
        for _, group := range [][]struct { Input int64 `json:"input"`; Expected int64 `json:"expected"` }{vector.Training, vector.Evaluation} {
            for _, row := range group {
                if got := reference(row.Input); got != row.Expected { t.Errorf("%s(%d)=%d, Python expected %d", vector.ID, row.Input, got, row.Expected) }
            }
        }
    }
}
