package candidateeval

func candidate22c(input int64) int64 {
	var valid = input >= 0;
	valid = valid && input <= 5;
	if (input <= 5) { return 5 } else { return -5 }
}
