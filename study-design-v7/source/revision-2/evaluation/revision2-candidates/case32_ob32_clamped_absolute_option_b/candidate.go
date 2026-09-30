package candidateeval

func candidate32b(input int64) int64 {
	var magnitude = input;
	if input < 0 { magnitude = 0 - magnitude } else { magnitude = input };
	if input == (-9223372036854775807 - 1) { return 9223372036854775807 } else { return (0 - magnitude) }
}
