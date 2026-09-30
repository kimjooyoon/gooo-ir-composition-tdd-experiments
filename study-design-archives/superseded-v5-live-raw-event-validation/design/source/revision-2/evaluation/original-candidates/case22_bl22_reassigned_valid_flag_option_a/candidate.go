package candidateeval

func candidate22a(input int64) int64 {
	var valid = input >= 0;
	valid = valid && input <= 5;
	if (valid) { return 5 } else { return -5 }
}
