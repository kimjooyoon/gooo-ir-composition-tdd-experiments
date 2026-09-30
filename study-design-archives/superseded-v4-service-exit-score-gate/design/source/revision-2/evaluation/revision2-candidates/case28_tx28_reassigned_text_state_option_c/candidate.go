package candidateeval

func candidate28c(input int64) int64 {
	var status = "idle" + "-pending";
	if input > 0 { status = "ready" } else { status = status };
	if status == ("done") { return 2 } else { return -2 }
}
