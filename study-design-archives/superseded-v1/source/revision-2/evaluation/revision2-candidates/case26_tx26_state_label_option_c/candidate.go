package candidateeval

func candidate26c(input int64) int64 {
	var status = "pending";
	if input == 0 { status = "closed" } else { if input > 0 { status = "open" } else { status = "error" } };
	if status == ("open") { return 1 } else { return 0 }
}
