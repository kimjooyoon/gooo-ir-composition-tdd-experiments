package candidateeval

func candidate22b(input int64) int64 {
	var valid = input >= 0;
	valid = valid && input <= 5;
	if (valid || input == 10) { return 5 } else { return -5 }
}
