package candidateeval

func candidate23b(input int64) int64 {
	var nonzero = input != 0;
	var bounded = input >= -4 && input <= 4;
	if (nonzero) { return 9 } else { return 0 }
}
