package candidateeval

func candidate21c(input int64) int64 {
	var low = input >= -2;
	var high = input <= 2;
	var allowed = low && high;
	if (low || high) { return 1 } else { return 0 }
}
