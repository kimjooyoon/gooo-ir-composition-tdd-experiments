"""Independent finite-domain reference semantics for the 32 design intents."""

from __future__ import annotations

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
MASK64 = (1 << 64) - 1


def i64(value: int) -> int:
    value &= MASK64
    return value - (1 << 64) if value >> 63 else value


def expected(case_id: str, x: int) -> int:
    """Return one design's int64 reference result using Python integer math."""
    if case_id == "cc01_inclusive_band":
        return 2 if 3 <= x <= 8 else -1
    if case_id == "cc02_nonzero_disjunction":
        return 1 if x < 0 or x > 0 else 0
    if case_id == "cc03_two_islands":
        return 7 if x == -4 or x == 4 else 0
    if case_id == "cc04_outer_cutoffs":
        return 9 if x <= -6 or x >= 8 else 0

    if case_id == "ni05_negative_then_double":
        if x < 0:
            return -1
        if x < 10:
            return i64(x * 2)
        return 20
    if case_id == "ni06_surcharge_discount":
        if x < 0:
            return 0
        if x > 100:
            return i64(x - 10)
        return i64(x + 10)
    if case_id == "ni07_zero_special_negative_offset":
        if x >= 0:
            return 5 if x == 0 else 3
        return i64(x + 2)
    if case_id == "ni08_upper_grade_excess":
        if x < 60:
            return -1
        if x < 80:
            return 0
        return i64(x - 80)

    if case_id == "lr09_compound_total":
        return i64(i64(x + 5) * 2)
    if case_id == "lr10_floor_then_increment":
        return 0 if x < 0 else i64(x + 1)
    if case_id == "lr11_nonnegative_doubled_score":
        doubled = i64(x * 2)
        return i64(doubled + 1) if doubled >= 0 else 0
    if case_id == "lr12_cap_after_adjustment":
        amount = i64(x + 3)
        if amount > 10:
            amount = 10
        return i64(amount - 1)

    if case_id == "pr13_subtract_tripled_sum":
        return i64(100 - i64(i64(x + 2) * 3))
    if case_id == "pr14_product_neighbor_factors":
        return i64(i64(x + i64(x + 1)) * i64(x - 2))
    if case_id == "pr15_square_minus_successor_sum":
        return i64(i64(x * x) - i64(x + 4))
    if case_id == "pr16_composed_neighbor_product":
        next_value = i64(x + 1)
        return i64(i64(next_value * i64(x + 2)) - x)

    if case_id == "cp17_nonpositive_inclusive":
        return 1 if x <= 0 else 0
    if case_id == "cp18_exact_release_code":
        return 1 if x == 7 else 0
    if case_id == "cp19_range_without_origin":
        return 1 if -3 <= x <= 3 and x != 0 else 0
    if case_id == "cp20_strict_symmetric_window":
        return 4 if -5 < x < 5 else -4

    if case_id == "bl21_saved_range_predicates":
        low = x >= -2
        high = x <= 2
        allowed = low and high
        return 1 if allowed else 0
    if case_id == "bl22_reassigned_valid_flag":
        valid = x >= 0
        valid = valid and x <= 5
        return 5 if valid else -5
    if case_id == "bl23_nonzero_bounded_flag":
        nonzero = x != 0
        bounded = x >= -4 and x <= 4
        return 9 if nonzero and bounded else 0
    if case_id == "bl24_selected_nonnegative_codes":
        selected = x == 2
        if x < 0:
            selected = False
        else:
            selected = selected or x == 4
        return 6 if selected else 0

    if case_id == "tx25_sign_label":
        label = "negative" if x < 0 else "nonnegative"
        return -1 if label == "negative" else 1
    if case_id == "tx26_state_label":
        if x == 0:
            status = "closed"
        elif x > 0:
            status = "open"
        else:
            status = "error"
        return 1 if status == "open" else 0
    if case_id == "tx27_lexical_cutoff":
        label = "amber" if x < 0 else "zinc"
        return 1 if label < "gold" else 0
    if case_id == "tx28_reassigned_text_state":
        status = "idle-pending"
        if x > 0:
            status = "ready"
        return 2 if status == "ready" else -2

    if case_id == "ob29_saturating_increment":
        return INT64_MAX if x == INT64_MAX else i64(x + 1)
    if case_id == "ob30_saturating_decrement":
        return INT64_MIN if x == INT64_MIN else i64(x - 1)
    if case_id == "ob31_saturating_double":
        if x > INT64_MAX // 2:
            return INT64_MAX
        if x < INT64_MIN // 2:
            return INT64_MIN
        return i64(x * 2)
    if case_id == "ob32_clamped_absolute":
        if x == INT64_MIN:
            return INT64_MAX
        return -x if x < 0 else x

    raise KeyError(case_id)
