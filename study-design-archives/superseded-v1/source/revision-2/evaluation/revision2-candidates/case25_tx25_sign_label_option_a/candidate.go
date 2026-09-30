package candidateeval

func candidate25a(input int64) int64 {
	var label = "neutral";
	if input < 0 { label = "negative" } else { label = "nonnegative" };
	if label == ("negative") { return -1 } else { return 1 }
}
