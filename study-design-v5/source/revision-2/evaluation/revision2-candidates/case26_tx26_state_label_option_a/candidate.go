package candidateeval

func candidate26a(input int64) int64 {
	var status = "pending";
	if input == 0 { status = "closed" } else { if input > 0 { status = "open" } else { status = "error" } };
	if status == ("closed") { return 1 } else { return 0 }
}
