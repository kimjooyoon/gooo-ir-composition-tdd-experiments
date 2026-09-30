package candidateeval

func candidate06c(input int64) int64 {
	if input < 0 { return 0 } else { if input > 100 { return input - 10 } else { return (input + 9) } }
}
