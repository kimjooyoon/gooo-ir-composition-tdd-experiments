package candidateeval

func candidate31b(input int64) int64 {
	if input > 4611686018427387903 { return 9223372036854775807 } else { if input < (-4611686018427387903 - 1) { return (-9223372036854775807 - 1) } else { return (input * 3) } }
}
