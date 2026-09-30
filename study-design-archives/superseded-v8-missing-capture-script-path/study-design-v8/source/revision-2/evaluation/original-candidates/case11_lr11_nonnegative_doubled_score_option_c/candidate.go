package candidateeval

func candidate11c(input int64) int64 {
	var score = input * 2;
	var nonnegative = score >= 0;
	if nonnegative { score = score + 1 } else { score = 0 };
	return (score)
}
