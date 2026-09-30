package candidateeval

func candidate16a(input int64) int64 {
	var next = input + 1;
	return next * (input + 2) - input
}
