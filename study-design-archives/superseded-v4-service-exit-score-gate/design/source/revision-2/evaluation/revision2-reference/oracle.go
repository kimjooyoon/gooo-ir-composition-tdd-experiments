package oracle

// Reference functions are independently written Go int64 specifications.

func ref01(x int64) int64 {
	if x >= 3 && x <= 8 { return 2 }; return -1;
}

func ref02(x int64) int64 {
	if x < 0 || x > 0 { return 1 }; return 0;
}

func ref03(x int64) int64 {
	if x == -4 || x == 4 { return 7 }; return 0;
}

func ref04(x int64) int64 {
	if x <= -6 || x >= 8 { return 9 }; return 0;
}

func ref05(x int64) int64 {
	if x < 0 { return -1 }; if x < 10 { return x * 2 }; return 20;
}

func ref06(x int64) int64 {
	if x < 0 { return 0 }; if x > 100 { return x - 10 }; return x + 10;
}

func ref07(x int64) int64 {
	if x >= 0 { if x == 0 { return 5 }; return 3 }; return x + 2;
}

func ref08(x int64) int64 {
	if x < 60 { return -1 }; if x < 80 { return 0 }; return x - 80;
}

func ref09(x int64) int64 {
	return (x + 5) * 2;
}

func ref10(x int64) int64 {
	if x < 0 { return 0 }; return x + 1;
}

func ref11(x int64) int64 {
	score := x * 2; if score >= 0 { return score + 1 }; return 0;
}

func ref12(x int64) int64 {
	amount := x + 3; if amount > 10 { amount = 10 }; return amount - 1;
}

func ref13(x int64) int64 {
	return 100 - (x + 2) * 3;
}

func ref14(x int64) int64 {
	return (x + x + 1) * (x - 2);
}

func ref15(x int64) int64 {
	return x*x - (x + 4);
}

func ref16(x int64) int64 {
	next := x + 1; return next * (x + 2) - x;
}

func ref17(x int64) int64 {
	if x <= 0 { return 1 }; return 0;
}

func ref18(x int64) int64 {
	if x == 7 { return 1 }; return 0;
}

func ref19(x int64) int64 {
	if x >= -3 && x <= 3 && x != 0 { return 1 }; return 0;
}

func ref20(x int64) int64 {
	if x > -5 && x < 5 { return 4 }; return -4;
}

func ref21(x int64) int64 {
	low := x >= -2; high := x <= 2; allowed := low && high; if allowed { return 1 }; return 0;
}

func ref22(x int64) int64 {
	valid := x >= 0; valid = valid && x <= 5; if valid { return 5 }; return -5;
}

func ref23(x int64) int64 {
	nonzero := x != 0; bounded := x >= -4 && x <= 4; if nonzero && bounded { return 9 }; return 0;
}

func ref24(x int64) int64 {
	selected := x == 2; if x < 0 { selected = false } else { selected = selected || x == 4 }; if selected { return 6 }; return 0;
}

func ref25(x int64) int64 {
	label := "nonnegative"; if x < 0 { label = "negative" }; if label == "negative" { return -1 }; return 1;
}

func ref26(x int64) int64 {
	status := "error"; if x == 0 { status = "closed" } else if x > 0 { status = "open" }; if status == "open" { return 1 }; return 0;
}

func ref27(x int64) int64 {
	label := "zinc"; if x < 0 { label = "amber" }; if label < "gold" { return 1 }; return 0;
}

func ref28(x int64) int64 {
	status := "idle-pending"; if x > 0 { status = "ready" }; if status == "ready" { return 2 }; return -2;
}

func ref29(x int64) int64 {
	if x == 9223372036854775807 { return x }; return x + 1;
}

func ref30(x int64) int64 {
	if x == (-9223372036854775807 - 1) { return x }; return x - 1;
}

func ref31(x int64) int64 {
	if x > 4611686018427387903 { return 9223372036854775807 }; if x < (-4611686018427387903 - 1) { return (-9223372036854775807 - 1) }; return x * 2;
}

func ref32(x int64) int64 {
	if x == (-9223372036854775807 - 1) { return 9223372036854775807 }; if x < 0 { return 0 - x }; return x;
}
func Reference(id string, input int64) int64 { switch id {
case "cc01_inclusive_band": return ref01(input)
case "cc02_nonzero_disjunction": return ref02(input)
case "cc03_two_islands": return ref03(input)
case "cc04_outer_cutoffs": return ref04(input)
case "ni05_negative_then_double": return ref05(input)
case "ni06_surcharge_discount": return ref06(input)
case "ni07_zero_special_negative_offset": return ref07(input)
case "ni08_upper_grade_excess": return ref08(input)
case "lr09_compound_total": return ref09(input)
case "lr10_floor_then_increment": return ref10(input)
case "lr11_nonnegative_doubled_score": return ref11(input)
case "lr12_cap_after_adjustment": return ref12(input)
case "pr13_subtract_tripled_sum": return ref13(input)
case "pr14_product_neighbor_factors": return ref14(input)
case "pr15_square_minus_successor_sum": return ref15(input)
case "pr16_composed_neighbor_product": return ref16(input)
case "cp17_nonpositive_inclusive": return ref17(input)
case "cp18_exact_release_code": return ref18(input)
case "cp19_range_without_origin": return ref19(input)
case "cp20_strict_symmetric_window": return ref20(input)
case "bl21_saved_range_predicates": return ref21(input)
case "bl22_reassigned_valid_flag": return ref22(input)
case "bl23_nonzero_bounded_flag": return ref23(input)
case "bl24_selected_nonnegative_codes": return ref24(input)
case "tx25_sign_label": return ref25(input)
case "tx26_state_label": return ref26(input)
case "tx27_lexical_cutoff": return ref27(input)
case "tx28_reassigned_text_state": return ref28(input)
case "ob29_saturating_increment": return ref29(input)
case "ob30_saturating_decrement": return ref30(input)
case "ob31_saturating_double": return ref31(input)
case "ob32_clamped_absolute": return ref32(input)
default: panic("unknown reference id: " + id)
} }

