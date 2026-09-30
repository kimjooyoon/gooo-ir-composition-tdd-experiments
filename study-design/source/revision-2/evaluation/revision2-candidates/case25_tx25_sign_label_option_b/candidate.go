package candidateeval

func candidate25b(input int64) int64 {
	var label = "neutral";
	if input < 0 { label = "negative" } else { label = "nonnegative" };
	if label == ("nonnegative") { return -1 } else { return 1 }
}
