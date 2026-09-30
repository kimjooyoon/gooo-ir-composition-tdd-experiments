package candidateeval

func candidate16b(input int64) int64 {
	var next = input + 1;
	return next * (input + 1) - input
}
