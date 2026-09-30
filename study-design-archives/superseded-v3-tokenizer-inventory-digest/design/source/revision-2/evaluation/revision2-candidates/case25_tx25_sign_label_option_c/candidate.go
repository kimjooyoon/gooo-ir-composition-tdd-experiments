package candidateeval

func candidate25c(input int64) int64 {
	var label = "neutral";
	if input < 0 { label = "negative" } else { label = "nonnegative" };
	if label == ("neutral") { return -1 } else { return 1 }
}
