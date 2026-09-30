package candidateeval

func candidate24a(input int64) int64 {
	var selected = input == 2;
	if input < 0 { selected = false } else { selected = selected || input == 4 };
	if (input == 2) { return 6 } else { return 0 }
}
