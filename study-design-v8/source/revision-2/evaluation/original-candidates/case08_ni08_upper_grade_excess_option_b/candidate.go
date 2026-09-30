package candidateeval

func candidate08b(input int64) int64 {
	if input < 60 { return -1 } else { if input < 80 { return 0 } else { return (input - 81) } }
}
