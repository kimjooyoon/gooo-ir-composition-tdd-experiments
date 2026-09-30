package candidateeval

func candidate10c(input int64) int64 {
	var adjusted = input;
	if adjusted < 0 { adjusted = 0 } else { adjusted = adjusted + 1 };
	return (adjusted + 1)
}
