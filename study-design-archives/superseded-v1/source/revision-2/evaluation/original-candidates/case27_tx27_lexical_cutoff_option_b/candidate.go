package candidateeval

func candidate27b(input int64) int64 {
	var label = "plum";
	if input < 0 { label = "amber" } else { label = "zinc" };
	if label < ("gold") { return 1 } else { return 0 }
}
