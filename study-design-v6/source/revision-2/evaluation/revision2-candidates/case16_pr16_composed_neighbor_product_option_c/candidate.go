package candidateeval

func candidate16c(input int64) int64 {
	var next = input + 1;
	return next * (input + 3) - input
}
